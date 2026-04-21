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
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

POLL_INTERVAL_S = 0.25


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
    tool = _get_field(payload, "toolName", "tool_name", default="")
    raw_args = _get_field(payload, "toolArgs", "tool_input", default={})
    if isinstance(raw_args, dict):
        args: dict[str, Any] = raw_args
    elif isinstance(raw_args, str):
        # Some tools (powershell/bash) deliver the command as a bare string.
        args = {"command": raw_args}
    else:
        args = {}

    _log_breadcrumb(payload, f"preToolUse tool={tool!r}")

    if tool == "ask_user":
        return _handle_ask_user(payload, args)

    # Path containment for file writes / edits / creates.
    if tool in {"write", "edit", "create"}:
        decision = _check_path_containment(payload, args)
        if decision is not None:
            return decision

    # Defensive shell re-check (flag matrix is primary, this is belt-and-suspenders).
    if tool in {"bash", "powershell"}:
        decision = _check_shell_escape(payload, args)
        if decision is not None:
            return decision

    return None  # fall through to default behaviour


def _on_permission_request(event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """permissionRequest: fires when rule-based checks find no match.

    In programmatic mode (`-p`) with no interactive TTY, Copilot defaults to
    deny. We therefore only use this hook to log breadcrumbs and rely on the
    explicit --allow-tool / --deny-tool matrix for policy.
    """
    tool = _get_field(payload, "toolName", "tool_name", default="")
    _log_breadcrumb(payload, f"permissionRequest tool={tool!r} (defaulting to Copilot policy)")
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

    if answer is None and cls.get("deterministic") == "ask_human":
        answer = _ask_human(repo, question, cls["name"])
        source = "human" if not answer.startswith("__CANCELLED__") else "cancelled"
        if source == "cancelled":
            answer = answer.removeprefix("__CANCELLED__ ") or "run aborted by human"

    # Fallback safety net — should never be reached, but keeps the agent moving.
    if answer is None:
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
    exceptions = cfg.get("exceptions") or []
    q = question or ""
    q_lower = q.lower()
    target_paths = _extract_path_tokens(q)
    for ex in exceptions:
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
        reason = ex.get("reason", "pre-approved")
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
                        return str(data["answer"])
                except json.JSONDecodeError:
                    pass
                return raw
            finally:
                _try_unlink(req_path)
                _try_unlink(ans_path)
        if cancel_path.exists() or global_cancel.exists():
            _try_unlink(req_path)
            _try_unlink(cancel_path)
            return "__CANCELLED__ run aborted by human"
        time.sleep(POLL_INTERVAL_S)


# ---- Path / shell defensive checks ----------------------------------------


def _check_path_containment(payload: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
    repo = _repo_root(payload)
    for key in ("path", "file", "filePath", "target"):
        value = args.get(key)
        if value:
            target = Path(value)
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
    return None


def _check_shell_escape(payload: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
    cmd = args.get("command") or args.get("cmd") or ""
    if not isinstance(cmd, str) or not cmd:
        return None
    repo = _repo_root(payload)
    repo_str = str(repo.resolve())
    # Heuristic: flag absolute paths that clearly escape repo and aren't just
    # read-only system tools (we can't reason about /usr/bin/true safely here,
    # so we stay conservative and only flag writes to clear danger zones).
    danger_fragments = ("/etc/", r"C:\Windows", r"C:\Program Files", "~/.ssh")
    for frag in danger_fragments:
        if frag in cmd and repo_str not in cmd:
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"[aidor guard] refusing shell command touching {frag!r}: {cmd!r}"
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
    if isinstance(choices, list) and choices:
        rendered = "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(choices) if isinstance(c, str))
        if rendered:
            question = f"{question}\n\nChoices:\n{rendered}"
    return question


def _unwrap_ask_user_args(args: dict[str, Any]) -> dict[str, Any]:
    """Recursively unwrap a {"command": "<json>"} envelope (up to 3 levels)."""
    current: Any = args
    for _ in range(3):
        if not isinstance(current, dict):
            break
        cmd = current.get("command")
        if isinstance(cmd, str) and cmd.lstrip().startswith("{"):
            try:
                current = json.loads(cmd)
                continue
            except json.JSONDecodeError:
                break
        break
    return current if isinstance(current, dict) else args


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
    entry = {
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

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_question_classes() -> dict[str, Any]:
    try:
        from importlib import resources

        pkg = resources.files("aidor.policies")
        text = (pkg / "question_classes.yml").read_text(encoding="utf-8")
    except Exception:
        return {"classes": [], "fallback": {"name": "unknown", "deterministic": "ask_human"}}
    import yaml

    return yaml.safe_load(text) or {}


if __name__ == "__main__":
    raise SystemExit(main())
