"""Tests for hook_resolver helpers — ask_user payload unwrapping and the
preToolUse string-args normalization.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from aidor.hook_resolver import (
    _extract_path_tokens,
    _extract_question,
    _glob_match,
    _handle_ask_user,
    _lookup_lint_exception,
    _unwrap_ask_user_args,
)


def test_extract_question_plain_args():
    q = _extract_question({"question": "Should I proceed?"})
    assert q == "Should I proceed?"


def test_extract_question_with_choices():
    q = _extract_question({"question": "Pick one", "choices": ["yes", "no", "maybe"]})
    assert "Pick one" in q
    assert "1. yes" in q
    assert "2. no" in q


def test_extract_question_unwraps_command_envelope():
    """Copilot CLI sometimes wraps ask_user as {"command": "<json>"}."""
    inner = {
        "question": "Enable PowerShell?",
        "choices": ["Yes", "No"],
        "allow_freeform": True,
    }
    args = {"command": json.dumps(inner)}
    q = _extract_question(args)
    assert "Enable PowerShell?" in q
    assert "1. Yes" in q
    assert "{" not in q.splitlines()[0]  # no raw JSON in the leading line


def test_extract_question_unwraps_doubly_nested_command_envelope():
    """Defensive: handle the actual two-level wrapping seen in dogfood
    (command -> {command: {question,...}}).
    """
    inner = {"question": "Approve?", "choices": ["a", "b"]}
    middle = {"command": json.dumps(inner)}
    outer = {"command": json.dumps(middle)}
    q = _extract_question(outer)
    assert "Approve?" in q
    assert "1. a" in q


def test_unwrap_returns_original_dict_when_not_wrapped():
    args = {"foo": "bar"}
    assert _unwrap_ask_user_args(args) == args


def test_unwrap_returns_original_when_command_is_not_json():
    args = {"command": "echo hello"}
    assert _unwrap_ask_user_args(args) == args


# ---- lint-exception scope enforcement ------------------------------------

_ALLOWED_EXCEPTIONS_YML = textwrap.dedent(
    """\
    exceptions:
      - rule: B011
        linter: ruff
        path_glob: "tests/**/*"
        reason: bare assert is idiomatic in pytest
      - rule: B008
        linter: ruff
        path_glob: "src/aidor/cli.py"
        reason: typer requires Option/Argument defaults
      - rule: E501
        linter: ruff
        path_glob: "**/*"
        reason: long literals
    """
)


def _prep_repo(tmp_path: Path) -> Path:
    (tmp_path / ".aidor").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aidor" / "allowed_exceptions.yml").write_text(
        _ALLOWED_EXCEPTIONS_YML, encoding="utf-8"
    )
    return tmp_path


def test_lint_exception_approves_b011_in_tests(tmp_path: Path):
    repo = _prep_repo(tmp_path)
    ans = _lookup_lint_exception(
        repo,
        "May I suppress ruff rule B011 in tests/test_feature.py for a bare assert?",
    )
    assert ans is not None
    assert "approved" in ans.lower()


def test_lint_exception_rejects_b011_outside_tests(tmp_path: Path):
    """Regression: approving an allowlisted rule outside its `path_glob` is
    a policy bypass (review-0002). Must escalate instead.
    """
    repo = _prep_repo(tmp_path)
    ans = _lookup_lint_exception(
        repo,
        "Please disable ruff rule B011 in src/aidor/cli.py for a bare assert.",
    )
    assert ans is None


def test_lint_exception_rejects_when_no_path_in_question(tmp_path: Path):
    """If the entry has a `path_glob` but the question doesn't reference a
    path at all, we cannot verify scope — deny and escalate.
    """
    repo = _prep_repo(tmp_path)
    ans = _lookup_lint_exception(repo, "Please disable ruff rule B011 somewhere in the code.")
    assert ans is None


def test_lint_exception_rejects_unknown_linter(tmp_path: Path):
    repo = _prep_repo(tmp_path)
    ans = _lookup_lint_exception(
        repo,
        "May I suppress eslint rule B011 in tests/test_feature.py?",
    )
    # linter mismatch (exception says 'ruff', question mentions 'eslint' and
    # no mention of 'ruff'): must not auto-approve.
    assert ans is None


def test_lint_exception_approves_b008_only_for_cli(tmp_path: Path):
    repo = _prep_repo(tmp_path)
    ok = _lookup_lint_exception(repo, "Allow ruff B008 in src/aidor/cli.py for typer defaults")
    no = _lookup_lint_exception(repo, "Allow ruff B008 in src/aidor/phase.py for typer defaults")
    assert ok is not None
    assert no is None


def test_lint_exception_e501_matches_anywhere(tmp_path: Path):
    repo = _prep_repo(tmp_path)
    for target in ("src/aidor/cli.py", "tests/test_x.py", "README.md"):
        ans = _lookup_lint_exception(repo, f"ruff noqa E501 for a long URL in {target}")
        assert ans is not None, target


def test_lint_exception_rejects_when_no_allowlist(tmp_path: Path):
    ans = _lookup_lint_exception(tmp_path, "disable ruff rule B011 in tests/test_x.py")
    assert ans is None


# ---- helpers -------------------------------------------------------------


def test_glob_match_star_star():
    assert _glob_match("tests/test_x.py", "tests/**/*")
    assert _glob_match("tests/sub/test_x.py", "tests/**/*")
    assert not _glob_match("src/aidor/cli.py", "tests/**/*")
    assert _glob_match("src/aidor/cli.py", "**/*")
    assert _glob_match("cli.py", "**/*")
    assert _glob_match("src/aidor/cli.py", "src/aidor/cli.py")


def test_extract_path_tokens_catches_slashed_and_bare_paths():
    toks = _extract_path_tokens("please fix src/aidor/cli.py and README.md now")
    assert "src/aidor/cli.py" in toks
    assert "README.md" in toks


def test_extract_path_tokens_normalises_backslashes():
    toks = _extract_path_tokens(r"update src\aidor\cli.py please")
    assert "src/aidor/cli.py" in toks


# ---- ask_user miss-path escalation (regression for review-0018) ----------


def _drive_handle_ask_user(
    monkeypatch, tmp_path: Path, question: str, *, human_answer: str = "ok"
) -> tuple[dict, list[tuple[str, str]]]:
    """Helper: invoke `_handle_ask_user` with `_ask_human` stubbed so the
    caller can assert *whether* the human path was taken (and with which
    classification) without actually blocking on file IPC.
    """
    monkeypatch.setenv("AIDOR_REPO", str(tmp_path))
    calls: list[tuple[str, str]] = []

    def fake_ask_human(repo: Path, q: str, class_name: str) -> str:
        calls.append((class_name, q))
        return human_answer

    monkeypatch.setattr("aidor.hook_resolver._ask_human", fake_ask_human)
    decision = _handle_ask_user({"cwd": str(tmp_path)}, {"question": question})
    return decision, calls


def test_handle_ask_user_escalates_on_policy_lookup_miss(monkeypatch, tmp_path: Path):
    """Regression (review-0018): a lint-exception request that is NOT in
    `allowed_exceptions.yml` must escalate to the human, not be denied with
    the generic fallback message. The previous implementation only escalated
    when `deterministic == "ask_human"`, leaving policy_lookup misses stuck
    on the fallback path.
    """
    _prep_repo(tmp_path)  # contains entries for B011/B008/E501 only
    decision, calls = _drive_handle_ask_user(
        monkeypatch,
        tmp_path,
        "May I add a noqa for ruff rule B999 in src/aidor/cli.py? (not allowlisted)",
        human_answer="approved by operator",
    )
    assert calls, "policy_lookup miss must escalate to _ask_human"
    assert calls[0][0] == "lint_exception"
    reason = decision["permissionDecisionReason"]
    assert "source=human" in reason
    assert "approved by operator" in reason
    assert "No deterministic answer available" not in reason


def test_handle_ask_user_escalates_on_state_lookup_miss(monkeypatch, tmp_path: Path):
    """Regression (review-0018): a state_lookup miss must also escalate to
    the human. We synthesize one by injecting a state_lookup class via the
    classifier so the test does not depend on the shipped policy file.
    """
    fake_cfg = {
        "classes": [
            {
                "name": "prior_review_lookup",
                "keywords": ["which file from review"],
                "deterministic": "state_lookup",
            }
        ],
        "fallback": {"name": "unknown", "deterministic": "ask_human"},
    }
    monkeypatch.setattr("aidor.hook_resolver._load_question_classes", lambda: fake_cfg)
    decision, calls = _drive_handle_ask_user(
        monkeypatch,
        tmp_path,
        "Which file from review-0002 did you mean by 'the resolver'?",
        human_answer="src/aidor/hook_resolver.py",
    )
    assert calls, "state_lookup miss must escalate to _ask_human"
    assert calls[0][0] == "prior_review_lookup"
    reason = decision["permissionDecisionReason"]
    assert "source=human" in reason
    assert "src/aidor/hook_resolver.py" in reason
    assert "No deterministic answer available" not in reason


def test_handle_ask_user_propagates_human_cancellation(monkeypatch, tmp_path: Path):
    """When the human cancels the wait, the resolver must surface that as
    `source=cancelled` and strip the internal `__CANCELLED__` marker.
    The message must reflect the actual orchestrator behaviour (the agent
    falls back; the run is NOT aborted), not the historical "run aborted"
    wording.
    """
    fake_cfg = {
        "classes": [],
        "fallback": {"name": "unknown", "deterministic": "ask_human"},
    }
    monkeypatch.setattr("aidor.hook_resolver._load_question_classes", lambda: fake_cfg)
    monkeypatch.setattr(
        "aidor.hook_resolver._ask_human",
        lambda repo, q, name: (
            "__CANCELLED__ Question cancelled by human; proceed with a safe default."
        ),
    )
    monkeypatch.setenv("AIDOR_REPO", str(tmp_path))
    decision = _handle_ask_user({"cwd": str(tmp_path)}, {"question": "anything goes here"})
    reason = decision["permissionDecisionReason"]
    assert "source=cancelled" in reason
    assert "Question cancelled by human" in reason
    assert "proceed with a safe default" in reason
    assert "run aborted" not in reason
    assert "__CANCELLED__" not in reason


# ---- shell escape containment (review-0020 regression) -------------------


def _shell_decision(repo: Path, command: str):
    from aidor.hook_resolver import _check_shell_escape

    return _check_shell_escape({"cwd": str(repo)}, {"command": command})


def test_shell_escape_blocks_absolute_windows_path_outside_repo(tmp_path: Path):
    """`Get-Content C:\\Users\\x\\secret.txt` must be denied even though the
    cmdlet itself is whitelisted in `_BASE_ALLOW`."""
    decision = _shell_decision(tmp_path, r"Get-Content C:\Users\x\secret.txt")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"
    assert "outside" in decision["permissionDecisionReason"]


def test_shell_escape_blocks_unix_absolute_outside_repo(tmp_path: Path):
    decision = _shell_decision(tmp_path, "Get-Content /etc/passwd")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


def test_shell_escape_blocks_parent_traversal(tmp_path: Path):
    """Relative `..\\outside.txt` must escape the repo and be denied."""
    decision = _shell_decision(tmp_path, r"New-Item ..\outside.txt")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


def test_shell_escape_blocks_parent_traversal_with_path_flag(tmp_path: Path):
    decision = _shell_decision(tmp_path, "Get-Content -Path ../../etc/shadow")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


def test_shell_escape_blocks_tilde_home(tmp_path: Path):
    decision = _shell_decision(tmp_path, "Get-Content ~/.ssh/id_rsa")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


def test_shell_escape_blocks_env_var_userprofile(tmp_path: Path):
    decision = _shell_decision(tmp_path, "Get-Content $env:USERPROFILE/secrets.txt")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"
    assert "USERPROFILE" in decision["permissionDecisionReason"]


def test_shell_escape_blocks_env_var_appdata(tmp_path: Path):
    decision = _shell_decision(tmp_path, "New-Item %APPDATA%\\foo.txt")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


# ---- shell-statement chaining (regression: trailing-semicolon glue) ----


def test_shell_escape_allows_cd_repo_then_chained_command(tmp_path: Path):
    """`cd D:\\repo; pytest -q` is the dominant agent-issued pattern; the
    previous tokeniser glued the trailing `;` onto the path token
    (`D:\\repo;`) and resolved that to a non-existent path outside the
    repo, denying the entire chain. Splitting on shell statement
    separators before tokenising fixes this."""
    decision = _shell_decision(tmp_path, f"cd {tmp_path}; pytest -q")
    assert decision is None, decision and decision["permissionDecisionReason"]


def test_shell_escape_allows_cd_repo_then_chained_command_andand(tmp_path: Path):
    decision = _shell_decision(tmp_path, f"cd {tmp_path} && pytest -q")
    assert decision is None, decision and decision["permissionDecisionReason"]


def test_shell_escape_still_denies_traversal_in_first_clause(tmp_path: Path):
    """A bad first clause must still be denied even if the chain
    contains otherwise-safe later clauses."""
    decision = _shell_decision(tmp_path, "cd ../outside; pytest -q")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


def test_shell_escape_still_denies_traversal_in_later_clause(tmp_path: Path):
    """A bad later clause must still be denied even when the leading
    `cd <repo>` is safe."""
    decision = _shell_decision(tmp_path, f"cd {tmp_path}; cat /etc/passwd")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"
    assert "/etc/passwd" in decision["permissionDecisionReason"]


def test_shell_escape_allows_repo_relative_path(tmp_path: Path):
    """A plain relative path inside the repo must be allowed."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x", encoding="utf-8")
    decision = _shell_decision(tmp_path, "Get-Content src/foo.py")
    assert decision is None


def test_shell_escape_allows_absolute_path_inside_repo(tmp_path: Path):
    (tmp_path / "x.txt").write_text("hello", encoding="utf-8")
    inside = tmp_path / "x.txt"
    decision = _shell_decision(tmp_path, f"Get-Content {inside}")
    assert decision is None


def test_shell_escape_ignores_url_like_tokens(tmp_path: Path):
    """`https://example.com/foo` looks like it has a `/foo` segment, but it
    is not a path token and must not trip the check."""
    decision = _shell_decision(tmp_path, "git log --grep https://example.com/foo --oneline")
    assert decision is None


def test_shell_escape_ignores_command_with_no_paths(tmp_path: Path):
    decision = _shell_decision(tmp_path, "python -m pytest -k test_foo")
    assert decision is None


def test_shell_escape_blocks_relative_traversal_behind_benign_prefix(tmp_path: Path):
    """`src/../../etc/passwd` starts with a harmless `src/` segment, so a
    naive prefix-only token regex would miss the embedded `..` traversal.
    The guard must resolve the path and notice it leaves the repo
    (regression for review-0021)."""
    decision = _shell_decision(tmp_path, "Get-Content src/../../etc/passwd")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


# ---- shell allowlist (enforced by _check_shell_allowlist) ---------------
#
# The hook denies any clause whose (exe, joined-args) does NOT match a
# rule in `aidor/policies/shell_allowlist.yml` (defaults) plus optional
# `<repo>/.aidor/shell_allowlist.yml` (user extensions).


def _deny_decision(repo: Path, command: str, *, env: dict[str, str] | None = None):
    from aidor.hook_resolver import _check_shell_allowlist

    old_env = {}
    if env is not None:
        import os as _os

        for k, v in env.items():
            old_env[k] = _os.environ.get(k)
            _os.environ[k] = v
    try:
        return _check_shell_allowlist({"cwd": str(repo)}, {"command": command})
    finally:
        if env is not None:
            import os as _os

            for k, old in old_env.items():
                if old is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = old


def test_deny_git_push(tmp_path: Path):
    decision = _deny_decision(tmp_path, "git push origin master")
    assert decision is not None
    assert "allowlist" in decision["permissionDecisionReason"]


def test_allow_git_log(tmp_path: Path):
    assert _deny_decision(tmp_path, "git log --oneline -n 5") is None


def test_deny_git_config_global(tmp_path: Path):
    # `git config` is not in the default allowlist at all, so any form
    # of it is denied. Users who really need local-only `git config` can
    # extend `.aidor/shell_allowlist.yml`.
    decision = _deny_decision(tmp_path, "git config --global user.name foo")
    assert decision is not None


def test_deny_git_config_system(tmp_path: Path):
    decision = _deny_decision(tmp_path, "git config --system core.autocrlf true")
    assert decision is not None


def test_deny_sudo(tmp_path: Path):
    assert _deny_decision(tmp_path, "sudo ls") is not None


def test_deny_doas(tmp_path: Path):
    assert _deny_decision(tmp_path, "doas ls") is not None


def test_deny_curl(tmp_path: Path):
    assert _deny_decision(tmp_path, "curl https://example.com") is not None


def test_deny_wget(tmp_path: Path):
    assert _deny_decision(tmp_path, "wget https://example.com/x") is not None


def test_deny_package_managers(tmp_path: Path):
    for cmd in (
        "apt install foo",
        "apt-get update",
        "brew install foo",
        "choco install x",
        "scoop install x",
        "winget install x",
    ):
        assert _deny_decision(tmp_path, cmd) is not None, cmd


def test_deny_pipx(tmp_path: Path):
    assert _deny_decision(tmp_path, "pipx install ruff") is not None


def test_deny_npm_install_global_short(tmp_path: Path):
    decision = _deny_decision(tmp_path, "npm install -g typescript")
    assert decision is not None
    assert "allowlist" in decision["permissionDecisionReason"]


def test_deny_npm_i_global(tmp_path: Path):
    assert _deny_decision(tmp_path, "npm i -g typescript") is not None


def test_deny_npm_install_global_long(tmp_path: Path):
    assert _deny_decision(tmp_path, "npm install --global typescript") is not None


def test_deny_npm_install_local(tmp_path: Path):
    # `npm` is NOT in the default allowlist; bare `npm install` is denied.
    # Users who need npm in their target repo can opt in via
    # `.aidor/shell_allowlist.yml`.
    assert _deny_decision(tmp_path, "npm install lodash") is not None


def test_deny_pnpm_add_global(tmp_path: Path):
    assert _deny_decision(tmp_path, "pnpm add -g typescript") is not None


def test_deny_yarn_global(tmp_path: Path):
    assert _deny_decision(tmp_path, "yarn global add typescript") is not None


def test_deny_npx_yes(tmp_path: Path):
    assert _deny_decision(tmp_path, "npx --yes some-package") is not None


def test_deny_npx_y(tmp_path: Path):
    assert _deny_decision(tmp_path, "npx -y some-package") is not None


def test_deny_cargo_install(tmp_path: Path):
    assert _deny_decision(tmp_path, "cargo install ripgrep") is not None


def test_deny_go_install(tmp_path: Path):
    assert _deny_decision(tmp_path, "go install github.com/foo/bar@latest") is not None


def test_deny_dotnet_tool_install_global(tmp_path: Path):
    assert _deny_decision(tmp_path, "dotnet tool install -g foo") is not None
    assert _deny_decision(tmp_path, "dotnet tool install --global foo") is not None


def test_deny_dotnet_workload_install(tmp_path: Path):
    assert _deny_decision(tmp_path, "dotnet workload install android") is not None


def test_deny_copilot_self_management(tmp_path: Path):
    assert _deny_decision(tmp_path, "copilot update") is not None
    assert _deny_decision(tmp_path, "copilot login") is not None
    assert _deny_decision(tmp_path, "copilot logout") is not None


def test_deny_aidor_run_recursion(tmp_path: Path):
    decision = _deny_decision(tmp_path, "aidor run")
    assert decision is not None
    assert "allowlist" in decision["permissionDecisionReason"]


def test_allow_aidor_doctor(tmp_path: Path):
    assert _deny_decision(tmp_path, "aidor doctor") is None


# ---- nested-shell escape --------------------------------------------------


def test_deny_cmd_slash_c(tmp_path: Path):
    assert _deny_decision(tmp_path, "cmd /c dir") is not None


def test_deny_cmd_slash_k(tmp_path: Path):
    assert _deny_decision(tmp_path, "cmd /k dir") is not None


def test_deny_powershell_command(tmp_path: Path):
    assert _deny_decision(tmp_path, "powershell -Command Get-ChildItem") is not None
    assert _deny_decision(tmp_path, "powershell -c 'Get-ChildItem'") is not None
    assert _deny_decision(tmp_path, "powershell -EncodedCommand ABCDEF") is not None


def test_deny_pwsh_command(tmp_path: Path):
    assert _deny_decision(tmp_path, "pwsh -c 'Get-ChildItem'") is not None


def test_deny_bash_c(tmp_path: Path):
    assert _deny_decision(tmp_path, "bash -c 'ls'") is not None


def test_deny_sh_c(tmp_path: Path):
    assert _deny_decision(tmp_path, "sh -c 'ls'") is not None


def test_deny_iex(tmp_path: Path):
    assert _deny_decision(tmp_path, 'iex "rm -rf ."') is not None


def test_deny_invoke_expression(tmp_path: Path):
    assert _deny_decision(tmp_path, "Invoke-Expression 'dir'") is not None


def test_deny_start_process(tmp_path: Path):
    assert _deny_decision(tmp_path, "Start-Process notepad") is not None


# ---- pip-install gating ---------------------------------------------------


def test_deny_pip_install_without_lockfile_or_gate(tmp_path: Path):
    decision = _deny_decision(tmp_path, "pip install requests")
    assert decision is not None
    assert "lockfile" in decision["permissionDecisionReason"]


def test_deny_pip_install_user_always(tmp_path: Path):
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    decision = _deny_decision(
        tmp_path,
        "pip install --user ruff",
        env={"AIDOR_REPO": str(tmp_path), "AIDOR_ALLOW_LOCAL_INSTALL": "1"},
    )
    assert decision is not None
    assert "--user" in decision["permissionDecisionReason"]


def test_allow_pip_install_editable_with_poetry_lock_and_gate(tmp_path: Path):
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    decision = _deny_decision(
        tmp_path,
        "pip install -e .",
        env={"AIDOR_REPO": str(tmp_path), "AIDOR_ALLOW_LOCAL_INSTALL": "1"},
    )
    assert decision is None


def test_allow_python_dash_m_pip_install_with_poetry_lock_and_gate(tmp_path: Path):
    """`python -m pip install -e .` is remapped to `pip install -e .` by the
    clause iterator, so the same gating applies."""
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    decision = _deny_decision(
        tmp_path,
        "python -m pip install -e .",
        env={"AIDOR_REPO": str(tmp_path), "AIDOR_ALLOW_LOCAL_INSTALL": "1"},
    )
    assert decision is None


def test_deny_python_dash_m_pip_install_without_gate(tmp_path: Path):
    decision = _deny_decision(tmp_path, "python -m pip install requests")
    assert decision is not None


def test_deny_pip_install_target(tmp_path: Path):
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    decision = _deny_decision(
        tmp_path,
        "pip install --target=/tmp/x ruff",
        env={"AIDOR_REPO": str(tmp_path), "AIDOR_ALLOW_LOCAL_INSTALL": "1"},
    )
    assert decision is not None
    assert "--target" in decision["permissionDecisionReason"]


# ---- statement-separator handling (regression: cd <repo>; <cmd> chain) ---


def test_deny_applies_to_later_clause_in_chain(tmp_path: Path):
    """`cd <repo>; npm install -g foo` must be denied on the second clause."""
    decision = _deny_decision(tmp_path, "cd /tmp; npm install -g typescript")
    assert decision is not None
    assert "allowlist" in decision["permissionDecisionReason"]


def test_normalises_python_dot_exe_from_venv(tmp_path: Path):
    """`.\\.venv\\Scripts\\python.exe -m pip install` must be recognised
    as a pip-install call despite the absolute-path executable. This is
    the exact pattern that broke the old flag matrix."""
    decision = _deny_decision(tmp_path, r".\.venv\Scripts\python.exe -m pip install requests")
    assert decision is not None


def test_normalises_git_dot_exe(tmp_path: Path):
    decision = _deny_decision(tmp_path, "git.exe push origin master")
    assert decision is not None


# ---- permissionRequest is now a no-op -------------------------------------


def test_permission_request_returns_none(tmp_path: Path):
    from aidor.hook_resolver import _on_permission_request

    payload = {"toolName": "shell", "cwd": str(tmp_path)}
    assert _on_permission_request("permissionRequest", payload) is None


def test_shell_escape_blocks_relative_traversal_with_path_flag(tmp_path: Path):
    """Same idea, but the path is supplied via `-Path .\\work\\..\\..\\outside.txt`
    so we also exercise the explicit flag form (regression for
    review-0021)."""
    decision = _shell_decision(tmp_path, r"Get-Content -Path .\work\..\..\outside.txt")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"
    assert "outside" in decision["permissionDecisionReason"]


def test_shell_escape_blocks_quoted_relative_traversal(tmp_path: Path):
    """A double-quoted argument with embedded traversal must still be
    extracted, unquoted, and denied (regression for review-0021)."""
    decision = _shell_decision(tmp_path, 'Get-Content "src/../../etc/shadow"')
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


def test_shell_escape_blocks_attached_param_value_traversal(tmp_path: Path):
    """PowerShell also accepts `-Path:value` / `-Path=value`. The value
    must be extracted from the parameter prefix and containment-checked."""
    decision = _shell_decision(tmp_path, "Get-Content -Path:src/../../etc/passwd")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


# ---- review-0022 regressions: shell-tool surface + bare-filename symlink --


def test_shell_escape_blocks_bare_relative_symlink_outside_repo(tmp_path: Path):
    """`Get-Content linked-secret.txt` — the file is a repo-local symlink
    whose target is *outside* the repo. The previous heuristic skipped any
    token without a `/`, `\\`, `~`, or `..`, so this bare filename would
    sail through. The strict cmdlet-aware extractor must resolve it,
    follow the symlink, and deny (regression for review-0022)."""
    import os as _os

    outside_dir = tmp_path.parent / "aidor-outside-target"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "secret.txt"
    outside.write_text("top secret", encoding="utf-8")

    link = tmp_path / "linked-secret.txt"
    try:
        _os.symlink(outside, link)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("symlink creation not permitted on this platform")

    decision = _shell_decision(tmp_path, "Get-Content linked-secret.txt")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"
    assert "outside" in decision["permissionDecisionReason"]


def test_shell_escape_blocks_bare_filename_outside_repo_via_resolve_path(tmp_path: Path):
    """Even without a real symlink, a bare filename whose resolved target
    leaves the repo (e.g. via a junction set up out-of-band) must be
    rejected by the strict cmdlet-aware extractor. We simulate the
    "resolved target is outside" case by passing an absolute path that
    obviously escapes — the cmdlet-aware path bypasses the
    separator-based heuristic and runs the containment check on every
    non-flag token."""
    decision = _shell_decision(tmp_path, "Resolve-Path linked-out")
    # `linked-out` does not exist; resolve() yields tmp_path/linked-out
    # which IS inside the repo, so this should be allowed. The point is
    # that the extractor *sees* the token at all (the separator heuristic
    # would have ignored it). To prove the see-through path also denies
    # when the target leaves the tree, we use the symlink test above.
    assert decision is None


def test_pre_tool_use_runs_shell_escape_for_generic_shell_tool(tmp_path: Path):
    """`guard_profile.py` emits `shell(...)`, `bash(...)`, and
    `powershell(...)` allow rules because Copilot may dispatch the tool
    under any of those names. The pre-tool-use hook must therefore run
    the shell containment check for the generic ``shell`` tool too —
    otherwise allowed file cmdlets become an out-of-repo escape on the
    very surface this guard is supposed to harden (regression for
    review-0022)."""
    from aidor.hook_resolver import _on_pre_tool_use

    payload = {
        "cwd": str(tmp_path),
        "toolName": "shell",
        "toolArgs": {"command": r"Get-Content C:\Users\x\secret.txt"},
    }
    decision = _on_pre_tool_use("preToolUse", payload)
    assert decision is not None
    assert decision["permissionDecision"] == "deny"
    assert "outside" in decision["permissionDecisionReason"]


def test_pre_tool_use_runs_shell_escape_for_generic_shell_tool_string_args(tmp_path: Path):
    """Same as above, but with the command supplied as a bare string
    (the shape Copilot uses for some shell dispatches)."""
    from aidor.hook_resolver import _on_pre_tool_use

    payload = {
        "cwd": str(tmp_path),
        "toolName": "shell",
        "toolArgs": "New-Item ..\\outside.txt",
    }
    decision = _on_pre_tool_use("preToolUse", payload)
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


# ---- allowlist positive smoke tests + YAML extension --------------------


def test_allow_ruff_check(tmp_path: Path):
    assert _deny_decision(tmp_path, "ruff check src tests") is None


def test_allow_pytest(tmp_path: Path):
    assert _deny_decision(tmp_path, "pytest -q") is None


def test_allow_pip_audit(tmp_path: Path):
    assert _deny_decision(tmp_path, "pip-audit") is None


def test_allow_powershell_pipeline(tmp_path: Path):
    """The exact false-positive pattern from the dogfood run that broke
    when we shipped a deny-only policy: a Get-ChildItem | ForEach-Object
    pipeline with PowerShell expansion syntax inside the script block."""
    cmd = (
        r"Get-ChildItem src\aidor\*.py | "
        r'ForEach-Object { "$(.Name)	$((Get-Content $_.FullName | Measure-Object -Line).Lines)" }'
    )
    assert _deny_decision(tmp_path, cmd) is None


def test_user_yaml_extends_default_allowlist(tmp_path: Path):
    """A .aidor/shell_allowlist.yml extension must add to (not replace)
    the bundled defaults."""
    (tmp_path / ".aidor").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aidor" / "shell_allowlist.yml").write_text(
        "rules:\n  - { exe: docker, args_regex: '^ps( .*)?$' }\n",
        encoding="utf-8",
    )
    # User-extended rule allows docker ps.
    assert _deny_decision(tmp_path, "docker ps") is None
    # Defaults still in force: git push still denied.
    assert _deny_decision(tmp_path, "git push origin main") is not None
    # Outside the user regex: docker run still denied.
    assert _deny_decision(tmp_path, "docker run alpine") is not None


def test_user_yaml_with_malformed_regex_is_ignored(tmp_path: Path):
    """Malformed entries are dropped silently rather than crashing the
    hook (which would block the whole tool call)."""
    (tmp_path / ".aidor").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aidor" / "shell_allowlist.yml").write_text(
        "rules:\n  - { exe: foo, args_regex: '[invalid' }\n",
        encoding="utf-8",
    )
    # Bundled defaults still apply.
    assert _deny_decision(tmp_path, "ruff check") is None
