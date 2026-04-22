"""Additional coverage for `aidor.hook_resolver`.

These tests exercise the deny-by-default tool allowlist (review-0001
critical fix), the `_ask_human` file-IPC pipeline, the `main()` entry
point, helper internals, and the shell allowlist's `aidor abort` / no
`aidor cancel` policy (review-0001 minor fix).
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest

import aidor.hook_resolver as hr

# ---- Tool allowlist (review-0001 critical regression) ------------------


def _pre_tool_use(tool: str, args=None, *, repo: Path) -> dict | None:
    payload = {"cwd": str(repo), "toolName": tool, "toolArgs": args or {}}
    return hr._on_pre_tool_use("preToolUse", payload)


def test_pre_tool_use_allows_listed_tool(tmp_path: Path):
    """A bog-standard read tool is in the bundled list and must pass."""
    assert _pre_tool_use("view", {"path": str(tmp_path)}, repo=tmp_path) is None


def test_pre_tool_use_denies_non_allowlisted_tool(tmp_path: Path):
    """An MCP / arbitrary tool not in the curated list is denied. This is
    the critical security gap from review-0001: previously every non-shell
    tool fell through unchecked.
    """
    decision = _pre_tool_use(
        "github-mcp-server-create_pull_request",
        {"title": "x"},
        repo=tmp_path,
    )
    assert decision is not None
    assert decision["permissionDecision"] == "deny"
    assert "tool allowlist" in decision["permissionDecisionReason"]


def test_pre_tool_use_denies_unknown_fetch_tool(tmp_path: Path):
    decision = _pre_tool_use("totally_made_up_tool", {}, repo=tmp_path)
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


def test_pre_tool_use_empty_tool_name_falls_through(tmp_path: Path):
    """Malformed payload with no tool name must NOT deny; the surrounding
    handler logs and passes through. Denying empty tools would brick
    every Copilot run on a legitimate framework hiccup."""
    payload = {"cwd": str(tmp_path), "toolName": "", "toolArgs": {}}
    assert hr._on_pre_tool_use("preToolUse", payload) is None


def test_user_yaml_extends_tool_allowlist(tmp_path: Path):
    (tmp_path / ".aidor").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aidor" / "tool_allowlist.yml").write_text(
        "tools:\n  - my_custom_tool\n  - github-mcp-server-list_issues\n",
        encoding="utf-8",
    )
    # The user-extended tool is now allowed.
    assert _pre_tool_use("my_custom_tool", {}, repo=tmp_path) is None
    assert _pre_tool_use("github-mcp-server-list_issues", {}, repo=tmp_path) is None
    # Bundled defaults still apply: bash is still allowlisted.
    # (The shell allowlist may still deny on content; that's a separate layer.)
    # Other arbitrary tools still denied.
    assert _pre_tool_use("github-mcp-server-create_pull_request", {}, repo=tmp_path) is not None


def test_permission_request_denies_non_allowlisted(tmp_path: Path):
    payload = {"cwd": str(tmp_path), "toolName": "github-mcp-server-create_pr"}
    decision = hr._on_permission_request("permissionRequest", payload)
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


def test_permission_request_passes_allowlisted(tmp_path: Path):
    payload = {"cwd": str(tmp_path), "toolName": "view"}
    assert hr._on_permission_request("permissionRequest", payload) is None


def test_permission_request_empty_tool_passes(tmp_path: Path):
    """Empty tool name must not trigger the deny path (consistent with
    `_on_pre_tool_use`)."""
    payload = {"cwd": str(tmp_path), "toolName": ""}
    assert hr._on_permission_request("permissionRequest", payload) is None


def test_load_tool_allowlist_contains_core_tools(tmp_path: Path):
    """Sanity check: the bundled YAML loads and includes the tools we
    intercept by name elsewhere in the resolver."""
    names = hr._load_tool_allowlist(tmp_path)
    assert "ask_user" in names
    assert "write" in names
    assert "edit" in names
    assert "create" in names
    assert "bash" in names
    assert "powershell" in names
    assert "shell" in names
    assert "run_in_terminal" in names


def test_user_yaml_with_malformed_tool_allowlist_keeps_defaults(tmp_path: Path):
    (tmp_path / ".aidor").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aidor" / "tool_allowlist.yml").write_text(
        "tools: [: not valid yaml at all]]]\n", encoding="utf-8"
    )
    names = hr._load_tool_allowlist(tmp_path)
    # Bundled defaults still in force.
    assert "view" in names


# ---- Shell allowlist: `aidor abort` allowed, `aidor cancel` denied --------


def _shell_allow(repo: Path, command: str):
    return hr._check_shell_allowlist({"cwd": str(repo)}, {"command": command})


def test_shell_allowlist_allows_aidor_abort(tmp_path: Path):
    """Regression for review-0001: the documented emergency-stop
    command `aidor abort` must be on the default shell allowlist so an
    operator-issued abort works through guarded shell execution."""
    assert _shell_allow(tmp_path, "aidor abort") is None


def test_shell_allowlist_denies_aidor_cancel(tmp_path: Path):
    """Regression for review-0001: `aidor cancel` is a CLI command that
    never shipped; allowlisting it is operator drift. It must NOT be in
    the default policy."""
    decision = _shell_allow(tmp_path, "aidor cancel")
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


def test_shell_allowlist_allows_aidor_summary(tmp_path: Path):
    assert _shell_allow(tmp_path, "aidor summary") is None


def test_aidor_cancel_is_not_a_real_cli_command():
    """Belt-and-suspenders: the CLI app must not expose a `cancel`
    subcommand. If someone adds one, this test will fail and force the
    author to also re-add `cancel` to the shell allowlist consciously."""
    from aidor.cli import app

    names = {(c.name or c.callback.__name__) for c in app.registered_commands if c.callback}
    assert "cancel" not in names
    assert "abort" in names


# ---- _ask_human file-IPC pipeline -----------------------------------------


def test_ask_human_returns_plain_answer(tmp_path: Path):
    """The orchestrator usually writes a JSON `{"answer": ...}` blob, but
    older fakes wrote bare text. `_ask_human` must accept both."""
    monkey = []

    def writer():
        # Wait a beat, then drop a plain-text answer file.
        time.sleep(0.05)
        pending = tmp_path / ".aidor" / "pending"
        # The hook creates the dir + writes <qid>.json; the test races to
        # spot the request file then writes the matching .answer.
        for _ in range(80):
            files = list(pending.glob("*.json")) if pending.exists() else []
            if files:
                qid = files[0].stem
                (pending / f"{qid}.answer").write_text("hi from human", encoding="utf-8")
                monkey.append(qid)
                return
            time.sleep(0.05)

    t = threading.Thread(target=writer)
    t.start()
    answer = hr._ask_human(tmp_path, "What now?", "unknown")
    t.join(timeout=5)
    assert answer == "hi from human"
    assert monkey, "writer thread did not see the request file"


def test_ask_human_returns_json_answer(tmp_path: Path):
    def writer():
        time.sleep(0.05)
        pending = tmp_path / ".aidor" / "pending"
        for _ in range(80):
            files = list(pending.glob("*.json")) if pending.exists() else []
            if files:
                qid = files[0].stem
                (pending / f"{qid}.answer").write_text(
                    json.dumps({"answer": "deep thought", "by": "operator"}),
                    encoding="utf-8",
                )
                return
            time.sleep(0.05)

    t = threading.Thread(target=writer)
    t.start()
    answer = hr._ask_human(tmp_path, "Why?", "unknown")
    t.join(timeout=5)
    assert answer == "deep thought"


def test_ask_human_per_question_cancel(tmp_path: Path):
    def writer():
        time.sleep(0.05)
        pending = tmp_path / ".aidor" / "pending"
        for _ in range(80):
            files = list(pending.glob("*.json")) if pending.exists() else []
            if files:
                qid = files[0].stem
                (pending / f"{qid}.cancel").write_text("", encoding="utf-8")
                return
            time.sleep(0.05)

    t = threading.Thread(target=writer)
    t.start()
    answer = hr._ask_human(tmp_path, "X?", "unknown")
    t.join(timeout=5)
    assert answer.startswith("__CANCELLED__")


def test_ask_human_global_abort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Speed the polling loop so the test stays snappy.
    monkeypatch.setattr(hr, "POLL_INTERVAL_S", 0.01)
    (tmp_path / ".aidor").mkdir(parents=True, exist_ok=True)

    def writer():
        time.sleep(0.05)
        (tmp_path / ".aidor" / "ABORT").write_text("", encoding="utf-8")

    t = threading.Thread(target=writer)
    t.start()
    answer = hr._ask_human(tmp_path, "X?", "unknown")
    t.join(timeout=5)
    assert answer.startswith("__CANCELLED__")


# ---- Path containment for write/edit/create -------------------------------


def test_path_containment_blocks_outside_repo(tmp_path: Path):
    """`write` / `edit` / `create` must refuse paths that resolve outside
    the repo root."""
    payload = {"cwd": str(tmp_path)}
    decision = hr._check_path_containment(payload, {"path": str(tmp_path.parent / "leak.txt")})
    assert decision is not None
    assert "outside the repo" in decision["permissionDecisionReason"]


def test_path_containment_allows_inside_repo(tmp_path: Path):
    payload = {"cwd": str(tmp_path)}
    assert hr._check_path_containment(payload, {"path": "subdir/x.txt"}) is None


def test_path_containment_blocks_traversal_via_filePath(tmp_path: Path):
    payload = {"cwd": str(tmp_path)}
    decision = hr._check_path_containment(payload, {"filePath": "../../etc/passwd"})
    assert decision is not None


def test_pre_tool_use_runs_path_containment_for_write(tmp_path: Path):
    payload = {
        "cwd": str(tmp_path),
        "toolName": "write",
        "toolArgs": {"path": str(tmp_path.parent / "out.txt")},
    }
    decision = hr._on_pre_tool_use("preToolUse", payload)
    assert decision is not None
    assert decision["permissionDecision"] == "deny"


def test_pre_tool_use_runs_path_containment_for_str_replace(tmp_path: Path):
    """The newly-allowlisted Anthropic-style edit tools must still be
    path-contained."""
    payload = {
        "cwd": str(tmp_path),
        "toolName": "str_replace_editor",
        "toolArgs": {"path": str(tmp_path.parent / "out.txt")},
    }
    decision = hr._on_pre_tool_use("preToolUse", payload)
    assert decision is not None


# ---- Audit + breadcrumb helpers ------------------------------------------


def test_audit_appends_jsonl(tmp_path: Path):
    hr._audit(tmp_path, "lint_exception", "q?", "a", "policy", 0.123)
    hr._audit(tmp_path, "unknown", "q2?", "a2", "human", 1.5)
    log = tmp_path / ".aidor" / "logs" / "qa.jsonl"
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["class"] == "lint_exception"
    assert rec["source"] == "policy"
    assert rec["wait_s"] == 0.123


def test_log_breadcrumb_writes_orchestrator_log(tmp_path: Path):
    hr._log_breadcrumb({"cwd": str(tmp_path)}, "hello world")
    log = tmp_path / ".aidor" / "logs" / "orchestrator.log"
    assert "hello world" in log.read_text(encoding="utf-8")


def test_try_unlink_is_quiet_on_missing(tmp_path: Path):
    """Must not raise on a missing file."""
    hr._try_unlink(tmp_path / "does-not-exist")


def test_utcnow_format():
    s = hr._utcnow()
    # YYYY-MM-DDTHH:MM:SSZ
    assert len(s) == 20 and s.endswith("Z") and s[10] == "T"


def test_repo_root_prefers_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AIDOR_REPO", str(tmp_path))
    assert hr._repo_root({"cwd": "/somewhere/else"}) == tmp_path


def test_repo_root_falls_back_to_payload_cwd(tmp_path: Path):
    assert hr._repo_root({"cwd": str(tmp_path)}) == tmp_path


# ---- main() entry point ---------------------------------------------------


def _run_main(monkeypatch: pytest.MonkeyPatch, event: str, payload: dict) -> tuple[int, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    code = hr.main([event])
    return code, out.getvalue()


def test_main_passthrough_for_notification(monkeypatch, tmp_path: Path):
    code, out = _run_main(
        monkeypatch,
        "notification",
        {"cwd": str(tmp_path), "notification_type": "info", "message": "hi"},
    )
    assert code == 0
    assert out == ""  # passthrough returns nothing


def test_main_passthrough_for_agent_stop(monkeypatch, tmp_path: Path):
    code, out = _run_main(
        monkeypatch, "agentStop", {"cwd": str(tmp_path), "stopReason": "end_turn"}
    )
    assert code == 0


def test_main_emits_decision_for_pre_tool_use_deny(monkeypatch, tmp_path: Path):
    code, out = _run_main(
        monkeypatch,
        "preToolUse",
        {
            "cwd": str(tmp_path),
            "toolName": "github-mcp-server-create_pr",
            "toolArgs": {},
        },
    )
    assert code == 0
    decision = json.loads(out)
    assert decision["permissionDecision"] == "deny"
    assert "tool allowlist" in decision["permissionDecisionReason"]


def test_main_handles_empty_payload(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.chdir(tmp_path)
    assert hr.main(["notification"]) == 0


def test_main_returns_2_on_missing_event(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stderr", io.StringIO())
    assert hr.main([]) == 2


def test_main_handles_unknown_event(monkeypatch, tmp_path: Path):
    code, out = _run_main(monkeypatch, "WeirdEvent", {"cwd": str(tmp_path)})
    assert code == 0


def test_read_payload_handles_garbage_json(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert hr._read_payload() == {}


# ---- Helper internals ----------------------------------------------------


def test_normalise_exe_strips_extensions():
    assert hr._normalise_exe(r".\.venv\Scripts\python.exe") == "python"
    assert hr._normalise_exe("PYTHON.EXE") == "python"
    assert hr._normalise_exe("foo.cmd") == "foo"
    assert hr._normalise_exe("foo.bat") == "foo"
    assert hr._normalise_exe("/usr/bin/python3") == "python3"


def test_split_shell_statements_respects_quotes():
    parts = hr._split_shell_statements('echo "a; b" && echo c')
    assert parts == ['echo "a; b"', "echo c"]


def test_split_shell_statements_respects_braces():
    parts = hr._split_shell_statements("ForEach-Object { $_; $_ } | Out-Null")
    assert parts == ["ForEach-Object { $_; $_ }", "Out-Null"]


def test_iter_shell_clauses_handles_python_dash_m_pip():
    clauses = list(hr._iter_shell_clauses("python -m pip install -e ."))
    assert clauses[0][0] == "pip"
    assert clauses[0][1] == ["install", "-e", "."]


def test_pip_install_allowed_denies_user_flag():
    ok, reason = hr._pip_install_allowed(
        ["install", "--user", "foo"], allow_local_install=True, python_install_anchor=True
    )
    assert not ok
    assert "--user" in reason


def test_pip_install_allowed_requires_anchor_or_dev_tool():
    ok, reason = hr._pip_install_allowed(
        ["install", "requests"], allow_local_install=True, python_install_anchor=False
    )
    assert not ok
    assert "anchor" in reason


def test_pip_install_allowed_happy_path_with_anchor():
    ok, reason = hr._pip_install_allowed(
        ["install", "-e", "."], allow_local_install=True, python_install_anchor=True
    )
    assert ok and reason == ""


def test_pip_install_allowed_dev_tool_without_anchor():
    """Curated dev/test tooling (pytest, ruff, pre-commit, ...) is
    allowed even without an anchor file so the coder can bootstrap the
    local quality gate in projects that haven't generated a lockfile."""
    for pkg in ("pytest", "ruff", "pre-commit", "pip-audit", "pyright", "pytest==8.3.3"):
        ok, reason = hr._pip_install_allowed(
            ["install", pkg], allow_local_install=True, python_install_anchor=False
        )
        assert ok, f"{pkg!r} should be allowed: {reason}"


def test_pip_install_allowed_mixed_dev_and_runtime_denied():
    """If even one positional target is NOT on the dev-tool allowlist,
    the whole command must be denied — otherwise smuggling a runtime
    dep alongside pytest would bypass the policy."""
    ok, _ = hr._pip_install_allowed(
        ["install", "pytest", "requests"],
        allow_local_install=True,
        python_install_anchor=False,
    )
    assert not ok


def test_pip_install_allowed_requires_gate():
    ok, reason = hr._pip_install_allowed(
        ["install", "pytest"], allow_local_install=False, python_install_anchor=False
    )
    assert not ok
    assert "AIDOR_ALLOW_LOCAL_INSTALL" in reason


def test_extract_question_from_message_field():
    assert hr._extract_question({"message": "hey"}) == "hey"


def test_extract_question_falls_back_to_json():
    out = hr._extract_question({"foo": 1, "bar": [2]})
    assert "foo" in out and "bar" in out


def test_unwrap_three_level_command_envelope():
    inner = {"question": "deep"}
    middle = {"command": json.dumps(inner)}
    outer = {"command": json.dumps(middle)}
    assert hr._unwrap_ask_user_args(outer) == inner


def test_load_yaml_simple(tmp_path: Path):
    p = tmp_path / "x.yml"
    p.write_text("foo: 1\n", encoding="utf-8")
    assert hr._load_yaml_simple(p) == {"foo": 1}


def test_load_question_classes_returns_dict():
    cfg = hr._load_question_classes()
    assert isinstance(cfg, dict)
    assert "fallback" in cfg or "classes" in cfg


def test_glob_match_question_mark():
    assert hr._glob_match("a", "?")
    assert not hr._glob_match("ab", "?")


def test_glob_match_trailing_double_star():
    assert hr._glob_match("a/b/c", "a/**")


def test_looks_like_path_skips_uri():
    assert not hr._looks_like_path("https://example.com/foo")
    assert hr._looks_like_path("src/foo")
    assert hr._looks_like_path("~/x")
    assert hr._looks_like_path("..\\out")


def test_strip_param_prefix_handles_colon():
    assert hr._strip_param_prefix("-Path:value") == "value"
    assert hr._strip_param_prefix("-Path=value") == "value"
    assert hr._strip_param_prefix("--foo") == ""
    assert hr._strip_param_prefix("plain") == "plain"


def test_extract_question_question_field_takes_precedence():
    args = {"question": "real one", "prompt": "ignored", "message": "ignored"}
    assert hr._extract_question(args) == "real one"


# ---- on_pre_tool_use defensive arg handling ------------------------------


def test_pre_tool_use_handles_non_dict_non_string_args(tmp_path: Path):
    """If toolArgs is a list/None/int the handler must coerce to {} and
    not crash."""
    payload = {"cwd": str(tmp_path), "toolName": "view", "toolArgs": [1, 2, 3]}
    # `view` is allowlisted, args coerced to {}, no path containment
    # applies to it → None.
    assert hr._on_pre_tool_use("preToolUse", payload) is None


def test_pre_tool_use_handles_non_json_string_args_for_powershell(tmp_path: Path):
    """A bare-string toolArgs that is not JSON is wrapped as {"command": s}."""
    payload = {
        "cwd": str(tmp_path),
        "toolName": "powershell",
        "toolArgs": "ruff check src tests",
    }
    assert hr._on_pre_tool_use("preToolUse", payload) is None


# ---- multi-language install gate ----------------------------------------


def test_package_install_allowed_npm_global_denied():
    ok, reason = hr._package_install_allowed(
        ["install", "-g", "typescript"],
        ecosystem="node",
        allow_local_install=True,
        install_anchor=True,
    )
    assert not ok and "writes outside the repo" in reason


def test_package_install_allowed_npm_dev_tool_without_anchor():
    for pkg in ("vitest", "eslint", "prettier", "typescript"):
        ok, _ = hr._package_install_allowed(
            ["install", pkg],
            ecosystem="node",
            allow_local_install=True,
            install_anchor=False,
        )
        assert ok, pkg


def test_package_install_allowed_npm_runtime_dep_denied_without_anchor():
    ok, reason = hr._package_install_allowed(
        ["install", "lodash"],
        ecosystem="node",
        allow_local_install=True,
        install_anchor=False,
    )
    assert not ok and "anchor" in reason


def test_package_install_allowed_cargo_dev_tool_without_anchor():
    ok, _ = hr._package_install_allowed(
        ["install", "cargo-audit"],
        ecosystem="cargo",
        allow_local_install=True,
        install_anchor=False,
    )
    assert ok


def test_package_install_allowed_cargo_runtime_denied():
    ok, reason = hr._package_install_allowed(
        ["install", "ripgrep"],
        ecosystem="cargo",
        allow_local_install=True,
        install_anchor=False,
    )
    assert not ok and "anchor" in reason


def test_package_install_allowed_go_install_dev_tool():
    ok, _ = hr._package_install_allowed(
        ["install", "github.com/golangci/golangci-lint/cmd/golangci-lint@latest"],
        ecosystem="go",
        allow_local_install=True,
        install_anchor=False,
    )
    assert ok


def test_package_install_allowed_dotnet_tool_install_global_denied():
    ok, reason = hr._package_install_allowed(
        ["tool", "install", "-g", "dotnet-format"],
        ecosystem="dotnet",
        allow_local_install=True,
        install_anchor=True,
    )
    assert not ok and "writes outside the repo" in reason


def test_package_install_allowed_dotnet_tool_install_dev_tool_local():
    ok, _ = hr._package_install_allowed(
        ["tool", "install", "dotnet-format"],
        ecosystem="dotnet",
        allow_local_install=True,
        install_anchor=False,
    )
    assert ok


def test_check_shell_allowlist_cargo_install_dev_tool(tmp_path: Path):
    import os as _os

    _os.environ["AIDOR_ALLOW_LOCAL_INSTALL"] = "1"
    try:
        decision = hr._check_shell_allowlist(
            {"cwd": str(tmp_path)},
            {"command": "cargo install cargo-nextest"},
        )
    finally:
        _os.environ.pop("AIDOR_ALLOW_LOCAL_INSTALL", None)
    assert decision is None


def test_check_shell_allowlist_cargo_build_with_anchor(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname="x"\n', encoding="utf-8")
    decision = hr._check_shell_allowlist(
        {"cwd": str(tmp_path)},
        {"command": "cargo build --release"},
    )
    assert decision is None


def test_check_shell_allowlist_npm_test_allowed(tmp_path: Path):
    decision = hr._check_shell_allowlist(
        {"cwd": str(tmp_path)},
        {"command": "npm test"},
    )
    assert decision is None


def test_check_shell_allowlist_npx_denied(tmp_path: Path):
    """
    px is intentionally NOT allowlisted (would bypass the install
        gate). Operators who need it can opt in via per-repo allowlist."""
    decision = hr._check_shell_allowlist(
        {"cwd": str(tmp_path)},
        {"command": "npx -y create-react-app foo"},
    )
    assert decision is not None


def test_detect_install_anchor_node(tmp_path: Path):
    from aidor.guard_profile import detect_install_anchor

    assert not detect_install_anchor(tmp_path, "node")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert detect_install_anchor(tmp_path, "node")


def test_detect_install_anchor_dotnet_csproj_glob(tmp_path: Path):
    from aidor.guard_profile import detect_install_anchor

    assert not detect_install_anchor(tmp_path, "dotnet")
    (tmp_path / "MyApp.csproj").write_text("<Project/>", encoding="utf-8")
    assert detect_install_anchor(tmp_path, "dotnet")


def test_is_dev_tool_ecosystem_scope():
    from aidor.guard_profile import is_dev_tool

    # coverage is a Python dev tool; it must NOT authorise a Node
    # install of a hypothetical package of the same name.
    assert is_dev_tool("coverage", ecosystem="python")
    assert not is_dev_tool("coverage", ecosystem="node")
    # And the ecosystem-less form still accepts it (legacy).
    assert is_dev_tool("coverage")


def test_is_dev_tool_strips_node_at_version():
    from aidor.guard_profile import is_dev_tool

    assert is_dev_tool("vitest@1.6.0", ecosystem="node")
    # Scoped names must keep the leading @scope.
    assert is_dev_tool("@playwright/test", ecosystem="node")
