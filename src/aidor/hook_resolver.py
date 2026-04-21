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

    # Shell-policy enforcement. The flag matrix in earlier revisions did
    # this, but the CLI's flag grammar can't express most of what we
    # need (see `guard_profile.py` module docstring); the hook is now
    # the sole enforcer of shell policy.
    if tool in {"bash", "powershell", "shell"}:
        decision = _check_shell_allowlist(payload, args)
        if decision is not None:
            return decision
        decision = _check_shell_escape(payload, args)
        if decision is not None:
            return decision

    return None  # fall through to default behaviour


def _on_permission_request(event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """permissionRequest: with `--allow-all-tools --allow-all-paths` the
    approval layer short-circuits, so this event is rare (it can still
    fire for tools not covered by the matrix, e.g. MCP or URL requests).

    We log a breadcrumb and fall through; the `preToolUse` hook has
    already had a chance to deny."""
    tool = _get_field(payload, "toolName", "tool_name", default="")
    _log_breadcrumb(payload, f"permissionRequest tool={tool!r} (no decision; hook is primary)")
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
            return "__CANCELLED__ Question cancelled by human; proceed with a safe default."
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
            if ch in (";", "|", "&"):
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
        try:
            tokens = shlex.split(clause, posix=False)
        except ValueError:
            tokens = clause.split()
        tokens = [t.strip().strip("`").strip("'\"") for t in tokens if t.strip()]
        tokens = [t for t in tokens if t and t not in _SHELL_OPERATORS]
        if not tokens:
            continue
        exe = _normalise_exe(tokens[0])
        rest = tokens[1:]
        # Special case: `python -m pip install ...` → treat as (pip, install, ...).
        # Same for `python3`, `py`, and the `-m` module dispatch in general for pip.
        if exe in {"python", "python3", "py"} and len(rest) >= 2 and rest[0] == "-m":
            module = rest[1].lower()
            if module in {"pip", "pip3"}:
                yield ("pip", rest[2:])
                continue
        yield (exe, rest)


def _flag_stem(token: str) -> str:
    """``--global=foo`` → ``--global``. Leaves non-flags untouched."""
    if token.startswith("-") and "=" in token:
        return token.split("=", 1)[0]
    return token


def _pip_install_allowed(
    args: list[str], *, allow_local_install: bool, python_lockfile: bool
) -> tuple[bool, str]:
    """Decide whether a ``pip install ...`` invocation is permitted.

    Returns ``(allowed, reason)``. ``reason`` is empty when allowed,
    human-readable when denied.
    """
    # Any of the --user / --target / --prefix / --root forms writes
    # outside the project tree and is always denied.
    for tok in args:
        stem = _flag_stem(tok).lower()
        if stem in _PIP_INSTALL_UNSAFE_FLAGS:
            return (False, f"pip install {stem} writes outside the repo")
    if allow_local_install and python_lockfile:
        return (True, "")
    return (
        False,
        "pip install requires a Python lockfile (poetry.lock / uv.lock / Pipfile.lock) "
        "and allow_local_install=True",
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

        data = yaml.safe_load(text) or {}
        raw_rules.extend(data.get("rules") or [])
    except Exception:  # pragma: no cover - defensive
        pass
    user = repo / ".aidor" / "shell_allowlist.yml"
    if user.is_file():
        try:
            import yaml

            data = yaml.safe_load(user.read_text(encoding="utf-8")) or {}
            raw_rules.extend(data.get("rules") or [])
        except Exception:  # pragma: no cover - defensive
            pass
    compiled: list[tuple[str, re.Pattern[str], dict[str, Any]]] = []
    for rule in raw_rules:
        exe = (rule.get("exe") or "").strip().lower()
        if not exe:
            continue
        try:
            pat = re.compile(rule.get("args_regex", ".*"))
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

    from aidor.guard_profile import detect_python_lockfile

    repo = _repo_root(payload)
    rules = _load_shell_allowlist(repo)
    allow_local_install = os.environ.get("AIDOR_ALLOW_LOCAL_INSTALL", "0") == "1"
    python_lockfile = allow_local_install and detect_python_lockfile(repo)

    for exe, rest in _iter_shell_clauses(cmd):
        if not exe:
            continue

        # Special case: pip install — always go through the gate, even
        # if a permissive allowlist rule would otherwise match.
        if exe in {"pip", "pip3"}:
            positional = [t for t in rest if not t.startswith("-")]
            if positional and positional[0].lower() == "install":
                allowed, reason = _pip_install_allowed(
                    rest,
                    allow_local_install=allow_local_install,
                    python_lockfile=python_lockfile,
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
