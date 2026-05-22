"""Hook resolver — invoked by Copilot CLI at `preToolUse`, `permissionRequest`,
`notification`, `agentStop`.

Contract (Copilot CLI hooks):
  - The hook reads a JSON payload from stdin.
  - The hook may print a JSON object to stdout to influence Copilot's decision
    (schema depends on the event, see plan.md §9.4).
  - Exit code 0 = success; the stdout JSON (if any) is honoured.

Our resolver implements the four-step pipeline for `ask_user` questions:
  1. Policy lookup    (question_classes.yml + allowed_exceptions.yml)
  2. State-derived    (read .aidor/state.json + latest review file)
  3. Human            (file-based IPC with the orchestrator; long wait,
                       capped by the hook timeout in bootstrap.py — currently 24 h)
  4. Cancellation     (orchestrator writes *.cancel → deny with reason)

Every Q&A is audited to .aidor/logs/qa.jsonl.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, cast

POLL_INTERVAL_S = 0.25


def _normalize_tool_name(name: str) -> str:
    """Canonicalise MCP-style tool names so ``server/tool`` and
    ``server-tool`` match the same allowlist entry.

    Copilot CLI uses hyphens in ``preToolUse`` payloads but slashes in
    ``permissionRequest`` payloads for the same MCP tool.  We normalise
    to hyphen form (which is what the bundled ``tool_allowlist.yml``
    uses) by replacing only the **first** ``/`` to avoid mangling tool
    names that legitimately contain deeper path segments.
    """
    return name.replace("/", "-", 1)


def _as_str_dict(value: Any) -> dict[str, Any]:
    """Narrow an arbitrary value (typically from ``json.loads`` or
    ``yaml.safe_load``) to a typed ``dict[str, Any]``.

    Returns an empty dict if ``value`` is not a mapping. This lets the
    rest of the module keep its precise typing without sprinkling
    ``cast()`` calls at every untrusted-input boundary."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """Narrow an arbitrary value to a typed ``list[Any]``."""
    return cast(list[Any], value) if isinstance(value, list) else []


# ---- Entry point ----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _err("aidor-hook: missing event name argument")
        return 2
    event = argv[0]
    try:
        payload = _read_payload()
    except Exception as exc:  # pragma: no cover - defensive
        _err(f"aidor-hook: failed to read payload: {exc}")
        return 0  # don't block Copilot on our own bugs

    try:
        handler = _HANDLERS.get(event, _passthrough)
        result = handler(event, payload)
    except Exception as exc:  # pragma: no cover - defensive
        _err(f"aidor-hook: handler error: {exc}")
        _log_breadcrumb(payload, f"handler error for {event}: {exc}")
        return 0  # never block the agent on our own crash

    if result is not None:
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
    return 0


# ---- Handlers -------------------------------------------------------------


def _passthrough(event: str, payload: dict[str, Any]) -> None:
    _log_breadcrumb(payload, f"{event} received")
    return None


def _on_pre_tool_use(event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """preToolUse: intercept ask_user + enforce path containment on writes/edits."""
    raw_tool = _get_field(payload, "toolName", "tool_name", default="")
    tool = _normalize_tool_name(raw_tool)
    raw_args = _get_field(payload, "toolArgs", "tool_input", default={})
    if isinstance(raw_args, dict):
        args: dict[str, Any] = cast(dict[str, Any], raw_args)
    elif isinstance(raw_args, str):
        # Copilot sometimes serialises the tool-arg object as a JSON
        # string (observed for `run_in_terminal` in CLI v1.0.35-2). Try
        # to parse it; fall back to wrapping the raw string as a bare
        # command for shells that genuinely deliver a single string.
        parsed: dict[str, Any] | None = None
        stripped = raw_args.lstrip()
        if stripped.startswith("{"):
            try:
                candidate = json.loads(raw_args)
                if isinstance(candidate, dict):
                    parsed = cast(dict[str, Any], candidate)
            except (json.JSONDecodeError, ValueError):
                parsed = None
        args = parsed if parsed is not None else {"command": raw_args}
    else:
        args = {}

    _log_breadcrumb(payload, f"preToolUse tool={tool!r}")
    policy = _load_tool_policy(_repo_root(payload))
    write_tools = _WRITE_TOOLS | policy.write_tools

    if tool == "ask_user":
        return _handle_ask_user(payload, args)

    def deny(decision: dict[str, Any]) -> dict[str, Any]:
        _record_failed_mcp_tool(event, payload, raw_tool, decision, policy)
        return decision

    # Tool allowlist: deny-by-default for every Copilot tool that is not
    # in the curated list. The flag matrix in `guard_profile.py` is no
    # longer in play (we spawn with `--allow-all-tools`), so without
    # this check arbitrary MCP / network / agent-spawning tools would
    # be unconstrained. See `policies/tool_allowlist.yml`.
    decision = _check_tool_allowlist(payload, tool, policy=policy)
    if decision is not None:
        return deny(decision)

    if tool in policy.memory_scoped_tools:
        decision = _check_memory_scope(tool, args, policy)
        if decision is not None:
            return deny(decision)

    if tool in policy.path_scoped_tools and tool not in write_tools:
        decision = _check_path_containment(payload, args, policy=policy)
        if decision is not None:
            return deny(decision)

    # Path containment for file writes / edits / creates.
    if tool in write_tools:
        # Role-scoped protected paths run BEFORE generic containment so
        # the operator sees a role-specific message (e.g. "coder may
        # not modify .aidor/allowed_exceptions.yml") rather than the
        # less helpful "path outside the repo" message. Protected paths
        # are inside the repo, so generic containment would not catch
        # them anyway.
        decision = _check_role_protected_paths(payload, tool, args)
        if decision is not None:
            return deny(decision)
        decision = _check_path_containment(payload, args, policy=policy)
        if decision is not None:
            return deny(decision)

    # Shell-policy enforcement. The flag matrix in earlier revisions did
    # this, but the CLI's flag grammar can't express most of what we
    # need (see `guard_profile.py` module docstring); the hook is now
    # the sole enforcer of shell policy.
    if tool in _SHELL_TOOLS:
        decision = _check_role_protected_paths(payload, tool, args)
        if decision is not None:
            return deny(decision)
        decision = _check_shell_allowlist(payload, args)
        if decision is not None:
            return deny(decision)
        decision = _check_shell_escape(payload, args)
        if decision is not None:
            return deny(decision)

    return None  # fall through to default behaviour


_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "write",
        "edit",
        "create",
        "Write",
        "Edit",
        "MultiEdit",
        "str_replace",
        "str_replace_editor",
        "NotebookEdit",
        "apply_patch",
    }
)

_SHELL_TOOLS: frozenset[str] = frozenset(
    {"bash", "Bash", "powershell", "PowerShell", "shell", "run_in_terminal"}
)

_PATH_ARG_KEYS: tuple[str, ...] = ("path", "file", "filePath", "target", "relative_path")


@dataclass(frozen=True)
class ToolPolicy:
    tools: frozenset[str]
    write_tools: frozenset[str]
    path_scoped_tools: frozenset[str]
    path_arg_keys: tuple[str, ...]
    deny_path_prefixes: tuple[str, ...]
    memory_scoped_tools: frozenset[str]
    memory_arg_keys: tuple[str, ...]
    memory_forbidden_values: frozenset[str]
    memory_forbidden_prefixes: tuple[str, ...]
    memory_deny_absolute: bool
    memory_deny_parent_segments: bool
    mcp_tools: frozenset[str]
    mcp_tool_patterns: tuple[str, ...]


def _on_permission_request(event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """permissionRequest: belt-and-suspenders enforcement of the tool
    allowlist. With `--allow-all-tools --allow-all-paths` the approval
    layer short-circuits for tools the flag matrix knows about, but
    Copilot may still raise `permissionRequest` for tools outside that
    surface (MCP servers, URL requests, ...). Re-running the allowlist
    here ensures non-allowlisted tools are denied at this layer too,
    not just in `preToolUse`.
    """
    raw_tool = _get_field(payload, "toolName", "tool_name", default="")
    tool = _normalize_tool_name(raw_tool)
    policy = _load_tool_policy(_repo_root(payload))
    decision = _check_tool_allowlist(payload, tool, policy=policy)
    if decision is not None:
        _record_failed_mcp_tool(event, payload, raw_tool, decision, policy)
        _log_breadcrumb(payload, f"permissionRequest deny tool={raw_tool!r} (not in allowlist)")
        return decision
    _log_breadcrumb(
        payload, f"permissionRequest tool={tool!r} (allowlisted; preToolUse is primary)"
    )
    return None


def _on_notification(event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    nt = payload.get("notification_type", "")
    _log_breadcrumb(payload, f"notification type={nt!r} msg={payload.get('message', '')!r}")
    return None


def _on_agent_stop(event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    reason = _get_field(payload, "stopReason", "stop_reason", default="")
    _log_breadcrumb(payload, f"agentStop reason={reason!r}")
    return None


_HANDLERS = {
    "preToolUse": _on_pre_tool_use,
    "PreToolUse": _on_pre_tool_use,
    "permissionRequest": _on_permission_request,
    "PermissionRequest": _on_permission_request,
    "notification": _on_notification,
    "Notification": _on_notification,
    "agentStop": _on_agent_stop,
    "Stop": _on_agent_stop,
}


# ---- ask_user resolver pipeline -------------------------------------------


def _handle_ask_user(payload: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Return a Copilot preToolUse decision that synthesizes an answer.

    We never actually let the `ask_user` tool run — we always resolve it here
    and feed the answer back through `permissionDecisionReason` (which is
    surfaced to the LLM). This avoids any real TTY interaction in the
    subprocess.
    """
    repo = _repo_root(payload)
    question = _extract_question(args)
    t0 = time.monotonic()

    cls, answer, source = _classify_and_answer(repo, question)

    # Any classification that did not produce a deterministic answer must
    # escalate to the human — not just the explicit `ask_human` mode. A
    # `policy_lookup` miss (e.g. a non-allowlisted lint exception) and a
    # `state_lookup` miss are both legitimate cases where the documented
    # `policy -> state -> human` contract requires a human in the loop.
    if answer is None:
        answer = _ask_human(repo, question, cls["name"])
        source = "human" if not answer.startswith("__CANCELLED__") else "cancelled"
        if source == "cancelled":
            # The orchestrator's cancel path does NOT abort the whole run —
            # it skips the question and lets the agent fall back. Surface
            # that to the model so it can choose a safe default instead of
            # being told (incorrectly) that the run was aborted.
            answer = (
                answer.removeprefix("__CANCELLED__ ")
                or "Question cancelled by human; proceed with a safe default."
            )

    # Fallback safety net: if we somehow ended up with an empty answer
    # (e.g. _ask_human returned "" for a cancelled-with-no-message case),
    # still hand the agent something actionable rather than an empty
    # decision string.
    if not answer:
        answer = "No deterministic answer available; please choose a different approach."
        source = "fallback"

    wait_s = time.monotonic() - t0
    _audit(repo, cls["name"], question, answer, source, wait_s)

    return {
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"[aidor resolver, class={cls['name']}, source={source}] {answer}"
        ),
    }


def _classify_and_answer(repo: Path, question: str) -> tuple[dict[str, Any], str | None, str]:
    """Returns (class_dict, answer_or_None, source). Source is one of
    'policy', 'state', or '' (if caller must ask the human)."""
    classes = _load_question_classes()
    matched = _match_class(question, classes)

    mode = matched.get("deterministic")

    if mode == "fixed_answer":
        return matched, str(matched.get("answer", "No.")), "policy"

    if mode == "policy_lookup" and matched.get("source") == "allowed_exceptions":
        answer = _lookup_lint_exception(repo, question)
        if answer is not None:
            return matched, answer, "policy"
        return matched, None, ""  # fall through to human

    if mode == "state_lookup":
        answer = _lookup_state_answer(repo, question)
        if answer is not None:
            return matched, answer, "state"
        return matched, None, ""

    # ask_human or unknown mode → escalate
    return matched, None, ""


def _match_class(question: str, classes_cfg: dict[str, Any]) -> dict[str, Any]:
    q = question or ""
    for cls in classes_cfg.get("classes", []):
        pattern = cls.get("pattern")
        if pattern and re.search(pattern, q):
            return cls
        kws = cls.get("keywords", [])
        if any(kw.lower() in q.lower() for kw in kws):
            return cls
    fallback = classes_cfg.get("fallback", {"name": "unknown", "deterministic": "ask_human"})
    return fallback


def _lookup_lint_exception(repo: Path, question: str) -> str | None:
    """Return an approval answer only when the question matches ALL of the
    documented allowed_exceptions.yml contract fields for a given entry:

      - rule     (required): the rule id must appear as a standalone token
      - linter   (optional): if the entry declares a linter, that linter's
                 name must appear in the question
      - path_glob(optional): if the entry declares a path_glob, at least one
                 file-path-like token in the question must match that glob

    If no entry satisfies every populated field, we deny (return None) and
    escalate to a human. This prevents a mere mention of a rule id from
    auto-approving the exception outside its documented scope.
    """
    path = repo / ".aidor" / "allowed_exceptions.yml"
    if not path.exists():
        return None
    try:
        cfg = _load_yaml_simple(path)
    except Exception:  # pragma: no cover - defensive
        return None
    exceptions = _as_list(cfg.get("exceptions"))
    q = question or ""
    q_lower = q.lower()
    target_paths = _extract_path_tokens(q)
    for raw_ex in exceptions:
        ex = _as_str_dict(raw_ex)
        rule = str(ex.get("rule", "") or "")
        if not rule:
            continue
        # Rule id must appear as a whole token (case-sensitive — rule ids are
        # canonicalised by their linter, e.g. `B011`, `no-unused-vars`).
        if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(rule)}(?![A-Za-z0-9_])", q):
            continue
        linter = str(ex.get("linter", "") or "").strip().lower()
        if linter and linter not in q_lower:
            continue
        path_glob = str(ex.get("path_glob", "") or "").strip()
        if path_glob:
            if not target_paths:
                continue
            if not any(_glob_match(p, path_glob) for p in target_paths):
                continue
        reason = str(ex.get("reason", "pre-approved") or "pre-approved")
        return f"Yes, approved. Reason: {reason}"
    return None


def _extract_path_tokens(question: str) -> list[str]:
    """Pull out file-path-like tokens from the question.

    Catches both slash-bearing paths (`src/aidor/cli.py`) and bare filenames
    with a recognisable extension (`cli.py`). Backslashes are normalised.
    """
    found: set[str] = set()
    # Tokens with at least one slash separator.
    for m in re.finditer(r"[A-Za-z0-9_.\\-]+(?:[\\/][A-Za-z0-9_.\\-]+)+", question):
        found.add(m.group(0).replace("\\", "/").strip("`'\""))
    # Bare `name.ext` tokens (short extensions only to avoid grabbing URLs).
    for m in re.finditer(r"\b[A-Za-z0-9_-]+\.[A-Za-z][A-Za-z0-9]{0,5}\b", question):
        found.add(m.group(0))
    return list(found)


def _glob_match(path: str, glob: str) -> bool:
    """Minimal glob matcher with `*`, `?`, and `**` support.

    `**/` matches zero or more path segments; `**` at end matches anything;
    `*` matches within a single segment; `?` matches a single non-slash char.
    Backslashes in `path`/`glob` are normalised to forward slashes.
    """
    p = path.replace("\\", "/").lstrip("./")
    g = glob.replace("\\", "/")
    parts: list[str] = []
    i = 0
    while i < len(g):
        if g[i : i + 3] == "**/":
            parts.append("(?:.*/)?")
            i += 3
        elif g[i : i + 2] == "**":
            parts.append(".*")
            i += 2
        elif g[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif g[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(g[i]))
            i += 1
    return bool(re.match("^" + "".join(parts) + "$", p))


def _lookup_state_answer(repo: Path, question: str) -> str | None:
    """Placeholder for §9.4 step 2 — answer questions about prior reviews
    mechanically. v1 ships the scaffolding; add rules as patterns emerge."""
    # Intentionally minimal; human escalation handles everything for now.
    _ = repo, question
    return None


def _ask_human(repo: Path, question: str, class_name: str) -> str:
    """File-based IPC with the orchestrator. Blocks indefinitely.

    Returns the human's answer, or a string prefixed with `__CANCELLED__ `
    if the orchestrator wrote a `.cancel` marker.
    """
    pending_dir = repo / ".aidor" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    qid = uuid.uuid4().hex
    req_path = pending_dir / f"{qid}.json"
    ans_path = pending_dir / f"{qid}.answer"
    cancel_path = pending_dir / f"{qid}.cancel"
    global_cancel = repo / ".aidor" / "ABORT"

    request = {
        "id": qid,
        "classification": class_name,
        "question": question,
        "role": os.environ.get("AIDOR_ROLE", "?"),
        "created_at": _utcnow(),
    }
    req_path.write_text(json.dumps(request, indent=2), encoding="utf-8")

    while True:
        if ans_path.exists():
            try:
                raw = ans_path.read_text(encoding="utf-8").strip()
                # Orchestrator writes JSON {"answer": "...", ...}; older fakes
                # may write plain text. Handle both.
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict) and "answer" in data:
                        return str(cast(dict[str, Any], data)["answer"])
                except json.JSONDecodeError:
                    pass
                return raw
            finally:
                _try_unlink(req_path)
                _try_unlink(ans_path)
        if cancel_path.exists() or global_cancel.exists():
            _try_unlink(req_path)
            _try_unlink(cancel_path)
            return "__CANCELLED__ Question cancelled by human; proceed with a safe default."
        time.sleep(POLL_INTERVAL_S)


# ---- Path / shell defensive checks ----------------------------------------


def _load_tool_allowlist(repo: Path) -> set[str]:
    """Return the union of bundled and user-supplied allowed tool names."""
    return set(_load_tool_policy(repo).tools)


def _load_tool_policy(repo: Path) -> ToolPolicy:
    """Return the merged tool policy loaded from defaults plus repo overrides.

    ``tools`` remains the deny-by-default allowlist. Optional policy lists
    classify tools for generic checks (write/path scoped, memory-name scoped,
    MCP-denial reporting) without hard-coding any external MCP server names in
    Python.
    """
    tools: set[str] = set()
    write_tools: set[str] = set()
    path_scoped_tools: set[str] = set()
    path_arg_keys: list[str] = list(_PATH_ARG_KEYS)
    deny_path_prefixes: list[str] = []
    memory_scoped_tools: set[str] = set()
    memory_arg_keys: list[str] = []
    memory_forbidden_values: set[str] = set()
    memory_forbidden_prefixes: list[str] = []
    memory_deny_absolute = False
    memory_deny_parent_segments = False
    mcp_tools: set[str] = set()
    mcp_tool_patterns: list[str] = []

    for data in _iter_tool_policy_documents(repo):
        tools.update(_clean_str_items(data.get("tools")))
        write_tools.update(_clean_str_items(data.get("write_tools")))
        path_scoped_tools.update(_clean_str_items(data.get("path_scoped_tools")))
        _extend_unique(path_arg_keys, _clean_str_items(data.get("path_arg_keys")))
        _extend_unique(deny_path_prefixes, _clean_str_items(data.get("deny_path_prefixes")))
        memory_scoped_tools.update(_clean_str_items(data.get("memory_scoped_tools")))
        _extend_unique(memory_arg_keys, _clean_str_items(data.get("memory_arg_keys")))
        memory_forbidden_values.update(_clean_str_items(data.get("memory_forbidden_values")))
        _extend_unique(
            memory_forbidden_prefixes,
            _clean_str_items(data.get("memory_forbidden_prefixes")),
        )
        memory_deny_absolute = memory_deny_absolute or bool(data.get("memory_deny_absolute"))
        memory_deny_parent_segments = memory_deny_parent_segments or bool(
            data.get("memory_deny_parent_segments")
        )
        mcp_tools.update(_clean_str_items(data.get("mcp_tools")))
        _extend_unique(mcp_tool_patterns, _clean_str_items(data.get("mcp_tool_patterns")))

    # Expand every tool-name set so both slash and hyphen forms are
    # recognised.  Copilot CLI sends hyphens in preToolUse but slashes
    # in permissionRequest for the same MCP tool; adding the normalised
    # alias to the policy sets means the incoming name (also normalised
    # at the handler edge) always matches regardless of which form
    # Copilot chose. We keep the original entry too so allowlist YAMLs
    # that already use one form or the other keep working.
    def _with_aliases(s: set[str]) -> frozenset[str]:
        return frozenset(s | {_normalize_tool_name(n) for n in s})

    return ToolPolicy(
        tools=_with_aliases(tools),
        write_tools=_with_aliases(write_tools),
        path_scoped_tools=_with_aliases(path_scoped_tools),
        path_arg_keys=tuple(path_arg_keys),
        deny_path_prefixes=tuple(deny_path_prefixes),
        memory_scoped_tools=_with_aliases(memory_scoped_tools),
        memory_arg_keys=tuple(memory_arg_keys),
        memory_forbidden_values=frozenset(memory_forbidden_values),
        memory_forbidden_prefixes=tuple(memory_forbidden_prefixes),
        memory_deny_absolute=memory_deny_absolute,
        memory_deny_parent_segments=memory_deny_parent_segments,
        mcp_tools=_with_aliases(mcp_tools),
        mcp_tool_patterns=tuple(mcp_tool_patterns),
    )


def _iter_tool_policy_documents(repo: Path) -> Iterator[dict[str, Any]]:
    try:
        from importlib import resources

        text = (resources.files("aidor.policies") / "tool_allowlist.yml").read_text(
            encoding="utf-8"
        )
        import yaml

        yield _as_str_dict(yaml.safe_load(text))
    except Exception:  # pragma: no cover - defensive
        pass

    user = repo / ".aidor" / "tool_allowlist.yml"
    if user.is_file():
        try:
            import yaml

            yield _as_str_dict(yaml.safe_load(user.read_text(encoding="utf-8")))
        except Exception:  # pragma: no cover - defensive
            pass


def _clean_str_items(value: Any) -> list[str]:
    return [item.strip() for item in _as_list(value) if isinstance(item, str) and item.strip()]


def _extend_unique(target: list[str], items: list[str]) -> None:
    for item in items:
        if item not in target:
            target.append(item)


def _check_tool_allowlist(
    payload: dict[str, Any],
    tool: str,
    *,
    policy: ToolPolicy | None = None,
) -> dict[str, Any] | None:
    """Deny the call if ``tool`` is not in the curated allowlist.

    A missing/empty tool name is *not* denied here — that would block
    malformed payloads we have no policy for; the surrounding handler
    logs and passes through. ``ask_user`` is intercepted by its own
    handler before this runs, so it does not need to be in the list
    (though it is, for clarity).
    """
    if not tool:
        return None
    if policy is None:
        policy = _load_tool_policy(_repo_root(payload))
    if tool in policy.tools:
        return None
    return {
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"[aidor guard] refusing tool {tool!r}: not in aidor tool allowlist. "
            "Extend .aidor/tool_allowlist.yml or ask the human to amend the default policy."
        ),
    }


def _is_mcp_tool(tool: str, policy: ToolPolicy) -> bool:
    return tool in policy.mcp_tools or any(
        fnmatchcase(tool, pattern) for pattern in policy.mcp_tool_patterns
    )


def _record_failed_mcp_tool(
    event: str,
    payload: dict[str, Any],
    tool: str,
    decision: dict[str, Any],
    policy: ToolPolicy,
) -> None:
    if decision.get("permissionDecision") != "deny" or not _is_mcp_tool(tool, policy):
        return
    repo = _repo_root(payload)
    logs = repo / ".aidor" / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": _utcnow(),
            "event": event,
            "tool": tool,
            "reason": str(decision.get("permissionDecisionReason") or ""),
        }
        with (logs / "failed_mcp_tools.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return


def _check_memory_scope(
    tool: str,
    args: dict[str, Any],
    policy: ToolPolicy,
) -> dict[str, Any] | None:
    """Apply generic memory-name restrictions from the tool policy."""
    for key in policy.memory_arg_keys:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        name = value.strip().replace("\\", "/")
        drive_like = len(name) >= 2 and name[1] == ":" and name[0].isalpha()
        invalid = (
            name in policy.memory_forbidden_values
            or any(name.startswith(prefix) for prefix in policy.memory_forbidden_prefixes)
            or (policy.memory_deny_absolute and (name.startswith("/") or drive_like))
            or (
                policy.memory_deny_parent_segments and any(part == ".." for part in name.split("/"))
            )
        )
        if invalid:
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"[aidor guard] refusing {tool!r} outside permitted memory "
                    f"namespace via {key}={value!r}"
                ),
            }
    return None


# ---- Role-scoped protected paths ------------------------------------------
#
# Some files inside the repo are owned by a specific aidor role and must
# not be modified by the other one. Without this guard the coder can
# "cheat" the quality gate by editing `.aidor/allowed_exceptions.yml`,
# rewriting its own review file in `.aidor/reviews/`, flipping
# `.aidor/state.json` to "converged", overwriting the bootstrapped agent
# templates, or deleting the active hook config.
#
# Per-role lists are RELATIVE TO REPO ROOT (POSIX separators). Each
# entry is matched against the normalised relative path of the write
# target — either as an exact path or, when it ends with ``/``, as a
# directory-prefix glob ("anything inside this subtree").
#
# Reviewer ownership rationale (user spec — 2026-05-16):
#   "Only the reviewer should be allowed to touch these files and
#    ONLY AFTER CAREFFUL consideration. Ask human if in doubt."
# The reviewer is therefore NOT denied write access to the policy /
# hook / agent-template files at the hook layer — that gate is in the
# reviewer's instructions (escalate via ask_user first). The hook DOES
# still protect orchestrator-owned files (state.json, ABORT, logs,
# pending, transcripts, config snapshot) from both roles since neither
# agent ever needs to edit those.
_PROTECTED_RELPATHS_BY_ROLE: dict[str, tuple[str, ...]] = {
    "coder": (
        # Policy and gate-config files. The coder must request
        # exceptions through the reviewer (via the fixes summary)
        # rather than edit them directly.
        ".aidor/allowed_exceptions.yml",
        ".aidor/tool_allowlist.yml",
        ".aidor/shell_allowlist.yml",
        ".github/hooks/aidor.json",
        ".github/agents/aidor-coder.md",
        ".github/agents/aidor-reviewer.md",
        # Reviewer's output — coder rewriting a review to make it
        # "look clean" is a textbook cheat.
        ".aidor/reviews/",
        # Orchestrator-owned bookkeeping (also protected from the
        # reviewer — see below).
        ".aidor/state.json",
        ".aidor/config.snapshot.toml",
        ".aidor/logs/",
        ".aidor/pending/",
        ".aidor/transcripts/",
        ".aidor/ABORT",
    ),
    "reviewer": (
        # Coder's output — reviewer editing the coder's fixes
        # summary would mask which fixes were actually applied.
        ".aidor/fixes/",
        # Orchestrator-owned bookkeeping.
        ".aidor/state.json",
        ".aidor/config.snapshot.toml",
        ".aidor/logs/",
        ".aidor/pending/",
        ".aidor/transcripts/",
        ".aidor/ABORT",
    ),
}


def _normalise_relpath(repo: Path, raw: str) -> str | None:
    """Return ``raw`` normalised as a POSIX relative path under ``repo``,
    or ``None`` when the target does not resolve inside the repo. Used
    to compare write targets against ``_PROTECTED_RELPATHS_BY_ROLE``.

    ``os.path.normcase`` is applied so case-insensitive filesystems
    (Windows, macOS HFS+) cannot bypass the check via casing tricks
    (``./.AIDOR/Allowed_Exceptions.YML``)."""
    try:
        candidate = Path(raw)
        target = (
            (repo / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        )
        rel = target.relative_to(repo.resolve())
    except (OSError, ValueError):
        return None
    return os.path.normcase(rel.as_posix())


def _matches_protected(rel_normcase: str, protected: tuple[str, ...]) -> str | None:
    """Return the protected entry that ``rel_normcase`` matches, or None."""
    for entry in protected:
        entry_nc = os.path.normcase(entry)
        if entry.endswith("/"):
            if rel_normcase == entry_nc.rstrip("/") or rel_normcase.startswith(entry_nc):
                return entry
        elif rel_normcase == entry_nc:
            return entry
    return None


def _role_deny(role: str, entry: str, *, tool: str) -> dict[str, Any]:
    """Build the deny payload for a role-scoped protected-path violation."""
    if role == "coder":
        guidance = (
            "Coder must NOT modify this file. If you believe a new lint "
            "exception or policy change is genuinely warranted, document the "
            "request in your fixes summary and let the reviewer act on it. "
            "Do not add inline `# noqa` / `eslint-disable` / `#[allow]` / "
            "`@SuppressWarnings` either — the reviewer will flag those as "
            "cheating and the run will not converge."
        )
    else:
        guidance = (
            "Orchestrator-owned file. Neither role may edit this; aidor manages it across rounds."
        )
    return {
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"[aidor guard] {role} role refused {tool!r} write to "
            f"protected path {entry!r}: {guidance}"
        ),
    }


def _check_role_protected_paths(
    payload: dict[str, Any], tool: str, args: dict[str, Any]
) -> dict[str, Any] | None:
    """Deny writes targeting files protected from the active aidor role.

    The active role is read from ``$AIDOR_ROLE`` (set by ``PhaseRunner``
    when launching the Copilot subprocess). When unset, this check is a
    no-op — operator-driven manual ``copilot`` sessions in a repo that
    still has a leftover hook installed must keep their write freedom.

    Applies to both file-write tools (``write``/``edit``/``apply_patch``
    /...) and shell tools (``bash``/``powershell``/...). Shell commands
    are scanned with a normcased substring check against each protected
    entry — false positives like ``git log .aidor/reviews/`` are
    acceptable for protected files, and the coder does not need to
    inspect those through shell anyway (they can use ``view``/``read``).
    """
    role = os.environ.get("AIDOR_ROLE", "").strip().lower()
    if role not in _PROTECTED_RELPATHS_BY_ROLE:
        return None
    protected = _PROTECTED_RELPATHS_BY_ROLE[role]
    repo = _repo_root(payload)

    if tool in _SHELL_TOOLS:
        cmd = args.get("command") or args.get("cmd") or ""
        if isinstance(cmd, str) and cmd:
            cmd_nc = os.path.normcase(cmd.replace("\\", "/"))
            for entry in protected:
                entry_nc = os.path.normcase(entry.rstrip("/"))
                if entry_nc and entry_nc in cmd_nc:
                    return _role_deny(role, entry, tool=tool)
        return None

    # File-write tools: check every path arg the schema may carry.
    for key in _PATH_ARG_KEYS:
        value = args.get(key)
        if not value or not isinstance(value, str):
            continue
        rel_nc = _normalise_relpath(repo, value)
        if rel_nc is None:
            continue
        match = _matches_protected(rel_nc, protected)
        if match is not None:
            return _role_deny(role, match, tool=tool)

    # ``apply_patch`` carries targets inside the patch body.
    patch_body = args.get("input") or args.get("patch") or ""
    if isinstance(patch_body, str) and patch_body:
        for raw_path in _iter_apply_patch_paths(patch_body):
            rel_nc = _normalise_relpath(repo, raw_path)
            if rel_nc is None:
                continue
            match = _matches_protected(rel_nc, protected)
            if match is not None:
                return _role_deny(role, match, tool=tool)
    return None


def _check_path_containment(
    payload: dict[str, Any],
    args: dict[str, Any],
    *,
    policy: ToolPolicy | None = None,
) -> dict[str, Any] | None:
    repo = _repo_root(payload)
    if policy is None:
        policy = _load_tool_policy(repo)
    for key in policy.path_arg_keys:
        value = args.get(key)
        if value:
            raw_value = str(value)
            if any(raw_value.lstrip().startswith(prefix) for prefix in policy.deny_path_prefixes):
                return {
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "[aidor guard] refusing external path-like tool argument; "
                        "only paths inside the repo are allowed"
                    ),
                }
            target = Path(raw_value)
            target = (repo / target).resolve() if not target.is_absolute() else target.resolve()
            try:
                target.relative_to(repo.resolve())
            except ValueError:
                return {
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"[aidor guard] refusing to touch path outside the repo: {target}"
                    ),
                }
    # ``apply_patch`` carries its file targets inside the patch body
    # rather than as a discrete ``path`` arg. Parse the body and run
    # every affected file through the same containment check, so the
    # tool can be allowlisted without losing the path-escape guard.
    patch_body = args.get("input") or args.get("patch") or ""
    if isinstance(patch_body, str) and patch_body:
        for raw_path in _iter_apply_patch_paths(patch_body):
            target = Path(raw_path)
            target = (repo / target).resolve() if not target.is_absolute() else target.resolve()
            try:
                target.relative_to(repo.resolve())
            except ValueError:
                return {
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"[aidor guard] refusing apply_patch touching path outside "
                        f"the repo: {target}"
                    ),
                }
    return None


# ``apply_patch`` mini-format header lines we recognise. The patch body
# wraps each per-file section in a ``*** <verb> File: <path>`` header;
# anything else (context, hunks, ``*** End Patch``) is ignored for
# containment purposes.
_APPLY_PATCH_HEADER_RE = re.compile(
    r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s+(.+?)\s*$",
    re.MULTILINE,
)
# ``*** Move to: <new-path>`` follows an ``*** Update File:`` header for
# rename operations; the new path must also be inside the repo.
_APPLY_PATCH_MOVE_RE = re.compile(
    r"^\*\*\*\s+Move\s+to:\s+(.+?)\s*$",
    re.MULTILINE,
)


def _iter_apply_patch_paths(body: str) -> Iterator[str]:
    """Yield every file path referenced by an ``apply_patch`` body.

    Both the per-file headers and any rename ``*** Move to:`` targets
    are surfaced so containment can reject patches that try to add /
    rewrite / rename a file outside the repo root.
    """
    for m in _APPLY_PATCH_HEADER_RE.finditer(body):
        yield m.group(1).strip().strip('"').strip("'")
    for m in _APPLY_PATCH_MOVE_RE.finditer(body):
        yield m.group(1).strip().strip('"').strip("'")


# ---- Shell policy enforcement ---------------------------------------------
#
# Allowlist-based: every clause in the shell command must match at least
# one rule loaded from `aidor/policies/shell_allowlist.yml` (defaults)
# plus optionally `.aidor/shell_allowlist.yml` (user extension). Anything
# not on the list is denied.
#
# After the allowlist match, `_check_shell_escape` runs to ensure no
# token resolves to a path outside the repo. The `pip install` form is
# special-cased: the allowlist permits it, but the code-side gate then
# requires a Python lockfile AND `AIDOR_ALLOW_LOCAL_INSTALL=1`.

# Pip-install handling is gated on the repo having a Python lockfile
# (poetry.lock / uv.lock / Pipfile.lock) AND the phase being configured
# with allow_local_install=True. We always deny the unsafe sub-forms
# (--user, --target, --prefix, --root); when a lockfile is present we
# allow bare `pip install -e .` / `pip install .` / `pip install -r ...`.
_PIP_INSTALL_UNSAFE_FLAGS: frozenset[str] = frozenset({"--user", "--target", "--prefix", "--root"})


def _normalise_exe(token: str) -> str:
    """Reduce a path-like executable token to its canonical name.

    ``.\\.venv\\Scripts\\python.exe`` → ``python``.
    """
    basename = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if basename.endswith(".exe"):
        basename = basename[:-4]
    if basename.endswith(".cmd") or basename.endswith(".bat"):
        basename = basename[:-4]
    return basename


def _split_shell_statements(cmd: str) -> list[str]:
    """Split ``cmd`` on shell statement separators, respecting quotes
    and grouping characters.

    Recognises ``;``, ``&&``, ``||``, ``|``, ``&`` as separators but
    skips them when nested inside ``'...'``, ``\"...\"``, ``(...)``,
    ``{...}``, ``$(...)``, or ``${...}``. This prevents naive splits
    from shredding PowerShell pipelines like
    ``Get-Content $f | Measure-Object`` that appear inside a
    ``ForEach-Object { ... }`` block.
    """
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(cmd)
    quote: str | None = None
    paren_depth = 0
    brace_depth = 0
    while i < n:
        ch = cmd[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            paren_depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            if paren_depth > 0:
                paren_depth -= 1
            buf.append(ch)
            i += 1
            continue
        if ch == "{":
            brace_depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == "}":
            if brace_depth > 0:
                brace_depth -= 1
            buf.append(ch)
            i += 1
            continue
        if paren_depth == 0 and brace_depth == 0:
            # Two-char separators first.
            if ch == "&" and i + 1 < n and cmd[i + 1] == "&":
                out.append("".join(buf).strip())
                buf.clear()
                i += 2
                continue
            if ch == "|" and i + 1 < n and cmd[i + 1] == "|":
                out.append("".join(buf).strip())
                buf.clear()
                i += 2
                continue
            if ch == "&":
                # Redirection-aware: do NOT treat `&` as a separator
                # when it is part of a shell redirection operator like
                # `2>&1`, `>&2`, `&>`, `&>>`. Walk back over whitespace
                # in `buf` and look at the last meaningful char; walk
                # forward and look at the next char.
                prev_nonspace = ""
                for j in range(len(buf) - 1, -1, -1):
                    if buf[j] != " " and buf[j] != "\t":
                        prev_nonspace = buf[j]
                        break
                next_ch = cmd[i + 1] if i + 1 < n else ""
                # `>&` (incl. `2>&`, `N>&`) - prev is `>`.
                # `&>` / `&>>` (bash combined redirect) - next is `>`.
                if prev_nonspace == ">" or next_ch == ">":
                    buf.append(ch)
                    i += 1
                    continue
                out.append("".join(buf).strip())
                buf.clear()
                i += 1
                continue
            if ch in (";", "|"):
                out.append("".join(buf).strip())
                buf.clear()
                i += 1
                continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return [c for c in out if c]


def _iter_shell_clauses(cmd: str) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(normalised-executable, all-tokens-after-it)`` for every
    statement in a shell command line.

    Splits on the same statement separators as ``_iter_shell_path_candidates``
    so ``cd D:\\repo; npm install -g foo`` is decomposed into two clauses
    (``cd``, ``npm``) and the dangerous one is still detected.
    """
    for clause in _split_shell_statements(cmd):
        # Strip a paren-wrapped subexpression form: `(get-content X)`,
        # `(Get-Content $f -Raw | ConvertFrom-Json)`. The split has
        # already grouped `(...)` as a single clause; here we unwrap so
        # the inner verb is what hits the exe-allowlist check rather
        # than the literal token `(get-content`. We only unwrap when
        # the parens are balanced at the boundaries of the clause —
        # otherwise it's a real PowerShell sub-expression that we
        # cannot statically disassemble safely.
        stripped = clause.strip()
        while stripped.startswith("(") and stripped.endswith(")"):
            depth = 0
            balanced = True
            for i, ch in enumerate(stripped):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0 and i != len(stripped) - 1:
                        balanced = False
                        break
            if not balanced:
                break
            stripped = stripped[1:-1].strip()
        clause = stripped
        try:
            tokens = shlex.split(clause, posix=False)
        except ValueError:
            tokens = clause.split()
        tokens = [t.strip().strip("`").strip("'\"") for t in tokens if t.strip()]
        tokens = [t for t in tokens if t and t not in _SHELL_OPERATORS]
        if not tokens:
            continue
        # Skip pure PowerShell expression clauses with no side effects:
        # variable reads (`$env:FOO`, `$var`) and env-var assignments
        # (`$env:FOO='bar'`). These are inert in isolation; a real
        # command following `;` will be evaluated as its own clause.
        first = tokens[0]
        if first.startswith("$"):
            continue
        exe = _normalise_exe(first)
        rest = tokens[1:]
        # Strip git global flags that precede the subcommand so the
        # allowlist regex (which starts at the subcommand) keeps
        # working. `--no-pager`, `--paginate`, `--no-replace-objects`
        # take no value; `-C <path>`, `--git-dir=...`, `--work-tree=...`,
        # `--namespace=...`, `-c key=val` are also skipped.
        if exe == "git":
            rest = _strip_git_global_flags(rest)
        # Special case: `python -m pip install ...` → treat as (pip, install, ...).
        # Same for `python3`, `py`, and the `-m` module dispatch in general for pip.
        if exe in {"python", "python3", "py"} and len(rest) >= 2 and rest[0] == "-m":
            module = rest[1].lower()
            if module in {"pip", "pip3"}:
                yield ("pip", rest[2:])
                continue
        yield (exe, rest)


def _strip_git_global_flags(tokens: list[str]) -> list[str]:
    """Drop git's pre-subcommand global flags (``--no-pager``, ``-C dir``,
    ``--git-dir=...``, ``-c key=val``) so the allowlist regex can match
    the subcommand at position 0."""
    valueless = {
        "--no-pager",
        "--paginate",
        "--no-replace-objects",
        "--bare",
        "--literal-pathspecs",
    }
    valued_eq = ("--git-dir=", "--work-tree=", "--namespace=", "--super-prefix=", "--config-env=")
    valued_pair = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in valueless:
            i += 1
            continue
        if t.startswith(valued_eq):
            i += 1
            continue
        if t in valued_pair and i + 1 < len(tokens):
            i += 2
            continue
        break
    return tokens[i:]


def _flag_stem(token: str) -> str:
    """``--global=foo`` → ``--global``. Leaves non-flags untouched."""
    if token.startswith("-") and "=" in token:
        return token.split("=", 1)[0]
    return token


# Per-ecosystem install-gate metadata. Each entry describes one or
# more ``(exe, install-verb)`` pairs that flow through
# ``_package_install_allowed``. The matched verb is consumed (so the
# remaining positionals are the actual install targets); ``unsafe_flags``
# are denied unconditionally because they all install outside the
# project tree (`-g`, `--global`, `--prefix`, ...).
#
# The ``install_verbs`` matcher is responsible for telling
# ``npm install`` (positional install) from ``npm install-test`` (a
# different verb). Verbs are matched against the FIRST positional token
# only.
_INSTALL_GATES: dict[str, dict[str, Any]] = {
    "python": {
        "exes": ("pip", "pip3"),
        "install_verbs": ("install",),
        "unsafe_flags": frozenset({"--user", "--target", "--prefix", "--root"}),
        "anchor_hint": (
            "poetry.lock / uv.lock / Pipfile.lock / pyproject.toml / "
            "setup.{cfg,py} / requirements*.txt"
        ),
        "dev_tool_hint": "pytest, ruff, pre-commit, pip-audit, pyright, ...",
    },
    "node": {
        "exes": ("npm", "pnpm", "yarn"),
        # ``npm i`` / ``npm add`` / ``yarn add`` / ``pnpm add`` etc.
        "install_verbs": ("install", "i", "add", "ci"),
        "unsafe_flags": frozenset({"--global", "-g", "--prefix"}),
        "anchor_hint": "package.json / package-lock.json / pnpm-lock.yaml / yarn.lock",
        "dev_tool_hint": "vitest, eslint, prettier, typescript, playwright, ...",
    },
    "cargo": {
        "exes": ("cargo",),
        # ``cargo install <crate>`` (global-ish: writes to CARGO_HOME),
        # ``cargo add <crate>`` (edits Cargo.toml).
        "install_verbs": ("install", "add"),
        "unsafe_flags": frozenset({"--root"}),
        "anchor_hint": "Cargo.toml / Cargo.lock",
        "dev_tool_hint": "cargo-audit, cargo-deny, cargo-nextest, cargo-llvm-cov, ...",
    },
    "go": {
        "exes": ("go",),
        "install_verbs": ("install", "get"),
        "unsafe_flags": frozenset(),
        "anchor_hint": "go.mod / go.sum",
        "dev_tool_hint": "golangci-lint, staticcheck, gofumpt, gosec, gotestsum, ...",
    },
    "dotnet": {
        "exes": ("dotnet",),
        # ``dotnet add <pkg>``, ``dotnet tool install <pkg>``,
        # ``dotnet package add <pkg>`` (.NET 10+ unified syntax),
        # ``dotnet restore`` (anchor-only — has no positional pkg).
        "install_verbs": ("add", "tool", "restore", "package"),
        "unsafe_flags": frozenset({"--global", "-g"}),
        "anchor_hint": "*.csproj / *.sln / global.json",
        "dev_tool_hint": "xunit, nunit, coverlet.collector, dotnet-format, csharpier, husky, dotnet-stryker, ...",
    },
}


def _package_install_allowed(
    args: list[str],
    *,
    ecosystem: str,
    allow_local_install: bool,
    install_anchor: bool,
) -> tuple[bool, str]:
    """Generic install-policy gate shared by pip / npm / cargo / go /
    dotnet. ``args`` is the full token list AFTER the executable, with
    the install verb as ``args[0]`` (e.g. ``["install", "-r",
    "requirements.txt"]``).

    Policy is identical to the historical pip gate: deny unsafe
    write-outside-project flags, deny unless ``allow_local_install``,
    allow when an anchor file exists, otherwise allow only if every
    positional install target is on the curated dev-tool allowlist for
    that ecosystem."""
    from aidor.guard_profile import is_dev_tool

    spec = _INSTALL_GATES[ecosystem]
    unsafe = spec["unsafe_flags"]
    for tok in args:
        stem = _flag_stem(tok).lower()
        if stem in unsafe:
            return (False, f"{spec['exes'][0]} install {stem} writes outside the repo")
    if not allow_local_install:
        return (
            False,
            f"{spec['exes'][0]} install requires AIDOR_ALLOW_LOCAL_INSTALL=1",
        )
    if install_anchor:
        return (True, "")
    # No anchor: every positional target must be on the dev-tool list.
    # ``args[0]`` is the install verb itself; for multi-token verbs
    # like ``dotnet tool install`` the second token is consumed too.
    skip = 1
    if (
        ecosystem == "dotnet"
        and len(args) >= 2
        and (
            (args[0] == "tool" and args[1] in {"install", "update", "restore"})
            or (args[0] == "package" and args[1] in {"add", "update"})
        )
    ):
        skip = 2
    elif ecosystem == "dotnet" and args and args[0] == "restore":
        # ``dotnet restore`` with no positional → anchor-required only.
        return (
            False,
            f"dotnet restore requires a project anchor ({spec['anchor_hint']})",
        )
    positionals = [t for t in args[skip:] if not t.startswith("-")]
    if positionals and all(is_dev_tool(p, ecosystem=ecosystem) for p in positionals):
        return (True, "")
    return (
        False,
        f"{spec['exes'][0]} install requires a project dependency anchor "
        f"({spec['anchor_hint']}) or every target on the curated "
        f"dev-tool allowlist ({spec['dev_tool_hint']})",
    )


def _pip_install_allowed(
    args: list[str],
    *,
    allow_local_install: bool,
    python_install_anchor: bool,
) -> tuple[bool, str]:
    """Backwards-compatible thin wrapper around
    :func:`_package_install_allowed` for the Python ecosystem.

    Kept because the existing test suite imports this symbol directly."""
    return _package_install_allowed(
        args,
        ecosystem="python",
        allow_local_install=allow_local_install,
        install_anchor=python_install_anchor,
    )


def _load_shell_allowlist(repo: Path) -> list[tuple[str, re.Pattern[str], dict[str, Any]]]:
    """Return the compiled allowlist as a list of ``(exe, args_pattern, raw_rule)``.

    Loads the bundled defaults from ``aidor.policies.shell_allowlist.yml``
    and concatenates any user-supplied rules from
    ``<repo>/.aidor/shell_allowlist.yml``. Rules with malformed regexes
    are silently dropped (logging would happen via the breadcrumb in
    the caller).
    """
    raw_rules: list[dict[str, Any]] = []
    try:
        from importlib import resources

        text = (resources.files("aidor.policies") / "shell_allowlist.yml").read_text(
            encoding="utf-8"
        )
        import yaml

        data = _as_str_dict(yaml.safe_load(text))
        raw_rules.extend(_as_str_dict(r) for r in _as_list(data.get("rules")))
    except Exception:  # pragma: no cover - defensive
        pass
    user = repo / ".aidor" / "shell_allowlist.yml"
    if user.is_file():
        try:
            import yaml

            data = _as_str_dict(yaml.safe_load(user.read_text(encoding="utf-8")))
            raw_rules.extend(_as_str_dict(r) for r in _as_list(data.get("rules")))
        except Exception:  # pragma: no cover - defensive
            pass
    compiled: list[tuple[str, re.Pattern[str], dict[str, Any]]] = []
    for rule in raw_rules:
        exe = str(rule.get("exe") or "").strip().lower()
        if not exe:
            continue
        try:
            pat = re.compile(str(rule.get("args_regex", ".*")))
        except re.error:
            continue
        compiled.append((exe, pat, rule))
    return compiled


def _check_shell_allowlist(payload: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
    """Deny the call unless every shell clause matches an allowlist rule.

    Per-clause matching:
      * The first token is normalised to its bare executable name
        (lower-case, stripped of ``.exe``/``.cmd``/``.bat`` and any
        leading directory).
      * The remaining tokens are joined with a single space and matched
        against ``args_regex`` (Python ``re.match``).
      * The first rule whose ``exe`` matches AND whose pattern matches
        wins; if no rule wins for a clause the whole call is denied.

    Special case: ``pip install`` is gated by ``_pip_install_allowed``
    (lockfile + ``AIDOR_ALLOW_LOCAL_INSTALL=1``) regardless of any
    allowlist rule that might match. This keeps the install policy in
    code where it can read the runtime env.
    """
    cmd = args.get("command") or args.get("cmd") or ""
    if not isinstance(cmd, str) or not cmd:
        return None

    from aidor.guard_profile import detect_install_anchor, detect_python_install_anchor

    repo = _repo_root(payload)
    rules = _load_shell_allowlist(repo)
    allow_local_install = os.environ.get("AIDOR_ALLOW_LOCAL_INSTALL", "0") == "1"
    python_install_anchor = allow_local_install and detect_python_install_anchor(repo)

    # Map each install-gated executable to its ecosystem key. Built once
    # per call so we don't traverse _INSTALL_GATES inside the clause loop.
    exe_to_ecosystem: dict[str, str] = {}
    for eco, spec in _INSTALL_GATES.items():
        for exe_name in spec["exes"]:
            exe_to_ecosystem[exe_name] = eco

    for exe, rest in _iter_shell_clauses(cmd):
        if not exe:
            continue

        # Install-verb intercept: pip / npm / yarn / pnpm / cargo / go /
        # dotnet all flow through the same code-side gate, regardless of
        # any allowlist rule that might match. The allowlist still
        # decides what NON-install subcommands are permitted.
        ecosystem = exe_to_ecosystem.get(exe)
        if ecosystem is not None:
            verbs: tuple[str, ...] = _INSTALL_GATES[ecosystem]["install_verbs"]
            positional = [t for t in rest if not t.startswith("-")]
            is_install_verb = bool(positional) and positional[0].lower() in verbs
            # ``dotnet package`` is dual-purpose: ``package add`` /
            # ``package update`` install, but ``package list`` /
            # ``package search`` / ``package remove`` are NOT installs
            # and should fall through to the allowlist (so e.g.
            # ``dotnet package list --vulnerable`` — the .NET 9.0.300+
            # supply-chain audit form — works without an anchor).
            if (
                is_install_verb
                and ecosystem == "dotnet"
                and positional[0].lower() == "package"
                and (len(positional) < 2 or positional[1].lower() not in {"add", "update"})
            ):
                is_install_verb = False
            if is_install_verb:
                if ecosystem == "python":
                    install_anchor = python_install_anchor
                else:
                    install_anchor = allow_local_install and detect_install_anchor(repo, ecosystem)
                # Pass only the positional-tail-relative args (drop
                # leading flags before the verb so the gate sees the
                # verb at args[0]). We rebuild ``args_after_verb`` by
                # stripping flags that precede the verb.
                pre_verb_flags: list[str] = []
                tail = list(rest)
                while tail and tail[0].startswith("-"):
                    pre_verb_flags.append(tail.pop(0))
                # tail[0] is now the verb. Reattach flags AFTER the verb
                # so the gate's flag scan still sees them.
                gate_args = [tail[0], *pre_verb_flags, *tail[1:]]
                allowed, reason = _package_install_allowed(
                    gate_args,
                    ecosystem=ecosystem,
                    allow_local_install=allow_local_install,
                    install_anchor=install_anchor,
                )
                if allowed:
                    continue
                return _deny(reason, cmd)

        joined = " ".join(rest)
        matched = False
        for rule_exe, rule_pat, _raw in rules:
            if rule_exe != exe:
                continue
            if rule_pat.match(joined):
                matched = True
                break
        if not matched:
            preview = (exe + " " + joined).strip()
            return _deny(
                f"shell clause not in aidor allowlist: {preview!r}. "
                "Extend .aidor/shell_allowlist.yml or ask the human to amend the default policy",
                cmd,
            )

    return None


def _deny(reason: str, cmd: str) -> dict[str, Any]:
    return {
        "permissionDecision": "deny",
        "permissionDecisionReason": f"[aidor guard] refusing {reason}: {cmd!r}",
    }


# Path-like tokens we extract from a shell command line for containment
# checks. We deliberately do NOT key the check on a tight prefix regex
# (drive-absolute, `~/`, `/`, `..`) — the previous version of this guard
# made exactly that mistake and let `Get-Content src/../../etc/passwd`
# slip through, because the token started with a benign `src/` and the
# regex never noticed the embedded traversal. Instead we tokenise the
# command with `shlex`, strip PowerShell parameter prefixes
# (`-Path:value`, `-Destination=value`), and resolve every argument that
# contains a path separator, a tilde, or a `..` segment. Resolving with
# `Path.resolve()` collapses the traversal, so any escape becomes
# visible to the `relative_to(repo)` containment check below.
#
# Tokens that look like URIs (contain `://`) are skipped so flags such
# as `git log --grep https://example.com/foo` don't trip the guard.
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SHELL_OPERATORS = frozenset(
    {"|", "&", "&&", "||", ";", ";;", ">", ">>", "<", "<<", "2>", "2>>", "(", ")"}
)


def _looks_like_path(token: str) -> bool:
    if not token:
        return False
    if _URI_SCHEME_RE.match(token):
        return False
    # Real filesystem paths can never contain these characters: Windows
    # reserves `<>:"|?*` in file names (drive-letter `:` is the only
    # legitimate use, handled by ``_PATH_SHAPED_RE`` on the value side),
    # and `\n` / `\r` aren't legal in any OS. Tokens containing any of
    # them are typically PowerShell here-doc bodies (Set-Content @'...'@
    # with markdown like "Suggested fixes / Production-readiness
    # verdict") or regex patterns passed to Select-String / Where-Object
    # (`'\[coverage-gate\]|Successfully|Failed'`). Treating those as
    # paths produced spurious "outside the repo" denials in long agent
    # runs (observed 7+ times across reviewer rounds 2-5 in the
    # video-organizer dogfood).
    if any(ch in token for ch in "\n\r|*?<>"):
        return False
    if '"' in token:
        return False
    # A bare separator (`/` or `\`) with nothing else is noise from
    # shlex-splitting English markdown bodies on whitespace
    # ("Suggested fixes / Production verdict" yields a standalone `/`
    # token). Real paths always carry at least one alphanumeric char.
    if not any(ch.isalnum() for ch in token):
        return False
    return "/" in token or "\\" in token or token.startswith("~") or token.startswith("..")


def _strip_param_prefix(token: str) -> str:
    """Detach a PowerShell `-Param:value` / `-Param=value` prefix and
    return the value. Plain flags (`-Path`, `--foo`) yield ``""``."""
    if not token.startswith("-"):
        return token
    # `-Param:value` or `-Param=value` — split on the first `:` or `=`.
    for sep in (":", "="):
        idx = token.find(sep)
        if idx > 0:
            return token[idx + 1 :]
    return ""


def _iter_shell_path_candidates(cmd: str) -> Iterator[str]:
    """Yield every token in ``cmd`` that looks like a filesystem path.

    The command is first split on shell statement separators (``;``,
    ``&&``, ``||``, ``|``, ``&``) by the quote/paren-aware
    ``_split_shell_statements`` so a trailing ``;`` doesn't get glued
    onto a path token (which previously caused legitimate
    ``cd D:\\repo; pytest -q`` chains to be denied because
    ``D:\\repo;`` resolved to a non-existent path).

    Each clause is tokenised with ``shlex.split(posix=False)`` (Windows
    backslashes survive intact). Tokens are stripped of surrounding
    back-ticks/quotes, of trailing shell punctuation, and of PowerShell
    parameter prefixes before the path heuristic runs.

    Tokens containing PowerShell expansion syntax (``$``, backtick,
    parens, braces) are skipped — they are evaluated at runtime and
    cannot be statically resolved as filesystem paths.
    """
    # Split the command line on shell statement separators (quote/paren-aware).
    clauses = _split_shell_statements(cmd)
    for clause in clauses:
        try:
            # posix=False keeps Windows backslashes intact (a POSIX split would
            # treat `C:\Users\x` as `C:Usersx` because `\` is an escape).
            tokens = shlex.split(clause, posix=False)
        except ValueError:
            # Unbalanced quotes etc. -- fall back to a permissive split so
            # we still inspect what we can rather than silently allowing.
            tokens = clause.split()

        cleaned: list[str] = []
        for raw in tokens:
            # Strip surrounding quotes/backticks AND any trailing shell
            # punctuation (`;`, `&`, `|`) that may have leaked through if
            # the splitter missed an unusual separator form.
            token = raw.strip().strip("`").strip("'\"").strip("`").rstrip(";&|")
            if not token or token in _SHELL_OPERATORS:
                continue
            cleaned.append(token)

        if not cleaned:
            continue

        # Strict-mode (whitelisted-cmdlet) yielding is no longer needed:
        # with `--allow-all-tools`, every cmdlet is permitted by Copilot,
        # and we only care about path-LOOKING tokens that could escape
        # the repo. Yielding bare-name args of e.g. `Get-Content` produced
        # false positives on dynamic PowerShell tokens like `$_.FullName`.
        for token in cleaned[1:]:
            value = _strip_param_prefix(token).strip("'\"").strip("`").rstrip(";&|")
            if not value:
                continue
            # Skip PowerShell expansion / subexpression / scriptblock
            # syntax — these are evaluated at runtime and are not
            # statically resolvable as filesystem paths.
            if any(c in value for c in ("$", "`", "(", ")", "{", "}")):
                continue
            if _looks_like_path(value):
                yield value


# Environment-variable / shell expansions that resolve to user-profile or
# system locations outside the repo. We can't expand them statically, so we
# refuse any shell command that references them.
_OUT_OF_REPO_VAR_RE = re.compile(
    r"""
      \$env:(USERPROFILE|HOMEDRIVE|HOMEPATH|HOME|APPDATA|LOCALAPPDATA
            |PROGRAMFILES|PROGRAMFILES\(X86\)|PROGRAMDATA|SYSTEMROOT|WINDIR
            |TEMP|TMP|PUBLIC|ALLUSERSPROFILE)
    | \$HOME(?![A-Za-z0-9_])
    | %USERPROFILE%
    | %APPDATA%
    | %LOCALAPPDATA%
    | %SYSTEMROOT%
    | %WINDIR%
    | %TEMP%
    | %TMP%
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _check_shell_escape(payload: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
    """Belt-and-suspenders containment check for shell commands.

    The flag matrix in `guard_profile.py` whitelists a number of generic
    PowerShell cmdlets (`Get-Content`, `New-Item`, `Resolve-Path`, ...) so
    that the coder can inspect and create files inside the repo. Those
    cmdlets accept arbitrary path arguments, so without a second check an
    allowed cmdlet could still read `C:\\Users\\x\\secret.txt` or write
    `..\\outside.txt`.

    This function tokenises the command, extracts every path-like token
    (including ordinary relative paths with embedded `..` traversal such
    as ``src/../../etc/passwd``), resolves it against the repo root, and
    denies the call if any token escapes the repo. It also denies use of
    environment variables that expand to known out-of-repo locations
    (USERPROFILE, APPDATA, ...), because we cannot statically resolve
    them but they almost always point outside the project tree.
    """
    cmd = args.get("command") or args.get("cmd") or ""
    if not isinstance(cmd, str) or not cmd:
        return None
    repo = _repo_root(payload).resolve()

    var_match = _OUT_OF_REPO_VAR_RE.search(cmd)
    if var_match:
        return {
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"[aidor guard] refusing shell command referencing out-of-repo "
                f"variable {var_match.group(0)!r}: {cmd!r}"
            ),
        }

    for token in _iter_shell_path_candidates(cmd):
        try:
            if token.startswith("~"):
                # PowerShell / bash will expand ~ to the user home dir before
                # the cmdlet sees it; do the same here so containment is
                # checked against the real target, not the literal "~/...".
                resolved = Path(os.path.expanduser(token)).resolve()
            else:
                candidate = Path(token)
                resolved = (
                    candidate.resolve() if candidate.is_absolute() else (repo / candidate).resolve()
                )
        except (OSError, ValueError):
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"[aidor guard] refusing shell command with unresolvable path "
                    f"{token!r}: {cmd!r}"
                ),
            }
        try:
            resolved.relative_to(repo)
        except ValueError:
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"[aidor guard] refusing shell command touching path outside "
                    f"the repo ({token!r} -> {resolved}): {cmd!r}"
                ),
            }
    return None


# ---- Helpers --------------------------------------------------------------


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _get_field(payload: dict[str, Any], *names: str, default: Any = None) -> Any:
    for n in names:
        if n in payload:
            return payload[n]
    return default


def _repo_root(payload: dict[str, Any]) -> Path:
    """Prefer env (set by orchestrator when spawning copilot); fall back to
    payload cwd."""
    env = os.environ.get("AIDOR_REPO")
    if env:
        return Path(env)
    cwd = payload.get("cwd") or os.getcwd()
    return Path(cwd)


def _extract_question(args: dict[str, Any]) -> str:
    """Pull the human-readable question out of the ask_user tool args.

    Copilot CLI sometimes wraps the ask_user payload as
        {"command": "<json-encoded body>"}
    where the inner body itself contains {"question": "...", "choices": [...]}.
    We unwrap up to two levels and return the plain question text (suffixing
    choices when present), so the operator sees a clean prompt instead of
    nested JSON.
    """
    unwrapped = _unwrap_ask_user_args(args)
    question = ""
    for key in ("question", "prompt", "message", "text"):
        val = unwrapped.get(key)
        if isinstance(val, str) and val.strip():
            question = val.strip()
            break
    if not question:
        return json.dumps(unwrapped, ensure_ascii=False)

    choices = unwrapped.get("choices")
    if isinstance(choices, list):
        choice_list = cast(list[Any], choices)
        if choice_list:
            rendered = "\n".join(
                f"  {i + 1}. {c}" for i, c in enumerate(choice_list) if isinstance(c, str)
            )
            if rendered:
                question = f"{question}\n\nChoices:\n{rendered}"
    return question


def _unwrap_ask_user_args(args: dict[str, Any]) -> dict[str, Any]:
    """Recursively unwrap a {"command": "<json>"} envelope (up to 3 levels)."""
    current: dict[str, Any] = args
    for _ in range(3):
        cmd = current.get("command")
        if isinstance(cmd, str) and cmd.lstrip().startswith("{"):
            try:
                parsed = json.loads(cmd)
            except json.JSONDecodeError:
                break
            if not isinstance(parsed, dict):
                break
            current = cast(dict[str, Any], parsed)
            continue
        break
    return current


def _audit(
    repo: Path,
    class_name: str,
    question: str,
    answer: str,
    source: str,
    wait_s: float,
) -> None:
    logs = repo / ".aidor" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "ts": _utcnow(),
        "class": class_name,
        "source": source,
        "wait_s": round(wait_s, 3),
        "question": question,
        "answer": answer,
    }
    with (logs / "qa.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _log_breadcrumb(payload: dict[str, Any], msg: str) -> None:
    repo = _repo_root(payload)
    logs = repo / ".aidor" / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "orchestrator.log").open("a", encoding="utf-8") as f:
            f.write(f"{_utcnow()} [hook] {msg}\n")
    except OSError:
        pass


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _try_unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# ---- YAML loaders (PyYAML is a project dep; bootstrap pins sys.executable) --


def _load_yaml_simple(path: Path) -> dict[str, Any]:
    import yaml

    return _as_str_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def _load_question_classes() -> dict[str, Any]:
    try:
        from importlib import resources

        pkg = resources.files("aidor.policies")
        text = (pkg / "question_classes.yml").read_text(encoding="utf-8")
    except Exception:
        return {"classes": [], "fallback": {"name": "unknown", "deterministic": "ask_human"}}
    import yaml

    return _as_str_dict(yaml.safe_load(text))


if __name__ == "__main__":
    raise SystemExit(main())
