"""Tests for guard_profile flag matrix.

Key invariants:

1. Each rule is emitted exactly once (no `bash(...)` / `powershell(...)` mirror
   flags — per the Copilot CLI docs those are not valid permission kinds).
2. Broad `shell(cmd:*)` allow entries must be paired with explicit
   `shell(cmd <dangerous-sub>)` deny entries so the deny-takes-precedence
   rule actually blocks the dangerous invocation. This is the exact
   pattern the Copilot docs recommend:
       --allow-tool='shell(git:*)' --deny-tool='shell(git push)'
3. Lockfile-gated local-install allows (`pip install -e`, `npm ci`, etc.)
   must not be shadowed by overly broad denies.
"""

from __future__ import annotations

from pathlib import Path

from aidor.guard_profile import (
    build_flags,
    detect_local_install_available,
)


def _split_allow_deny(flags: list[str]) -> tuple[list[str], list[str]]:
    allow = [f.removeprefix("--allow-tool=") for f in flags if f.startswith("--allow-tool=")]
    deny = [f.removeprefix("--deny-tool=") for f in flags if f.startswith("--deny-tool=")]
    return allow, deny


# ---- Basic structure ---------------------------------------------------


def test_build_flags_base(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    joined = " ".join(flags)
    assert "--deny-tool" in joined
    assert "--allow-tool" in joined
    # Push must be denied.
    assert "--deny-tool=shell(git push)" in flags


def test_flags_are_not_aliased_to_bash_or_powershell(tmp_path: Path):
    """Regression: previous versions mirrored every `shell(cmd)` rule into
    `bash(cmd)` and `powershell(cmd)` flags, but per the Copilot CLI docs
    those are NOT valid permission kinds — only `shell(...)`, `read`,
    `write`, `url(...)`, `memory`, and `<mcp-server>(...)` are. The mirror
    expansion was silently ignored and inflated argv by ~13 KB per spawn,
    which tripped the Windows cmd.exe 8 KB command-line limit."""
    flags = build_flags(tmp_path, allow_local_install=True)
    for f in flags:
        payload = f.removeprefix("--allow-tool=").removeprefix("--deny-tool=")
        assert not payload.startswith("bash("), f"invalid bash() kind leaked: {f}"
        assert not payload.startswith("powershell("), f"invalid powershell() kind leaked: {f}"


def test_argv_is_reasonably_compact(tmp_path: Path):
    """The full argv contribution must stay well under the Windows cmd.exe
    8 KB command-line limit so `fake_copilot.cmd` (and any real shell
    wrapper) doesn't silently truncate. Leave plenty of headroom for the
    rest of the argv (prompt, transcript paths, model selectors, etc.)."""
    flags = build_flags(tmp_path, allow_local_install=True)
    # +1 for the space separator between flags.
    total = sum(len(f) + 1 for f in flags)
    # Hard ceiling: stay under 7 KB so the rest of the argv (prompt
    # paths, model strings, --add-dir, etc.) has at least 1 KB of
    # headroom before hitting the cmd.exe 8191-char limit.
    assert total < 7168, f"guard flags bloated to {total} bytes ({len(flags)} flags)"


def test_each_rule_emitted_once(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=True)
    assert len(flags) == len(set(flags)), "duplicate flag emitted"


# ---- Directory-navigation prefix (regression: 148+ historical denials) -


def test_cd_and_set_location_are_allowed(tmp_path: Path):
    """Most agent-issued commands are emitted as `cd <repo>; <real cmd>`.
    Without an allow on the leading `cd` / `Set-Location` / `Push-Location`,
    the entire chain is denied even though the trailing command would have
    matched. This was the dominant failure mode in pre-rewrite dogfood
    transcripts (148 'Permission denied' hits attributable to `cd`)."""
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, _ = _split_allow_deny(flags)
    for rule in (
        "shell(cd:*)",
        "shell(chdir:*)",
        "shell(Set-Location:*)",
        "shell(sl:*)",
        "shell(Push-Location:*)",
        "shell(pushd:*)",
        "shell(Pop-Location:*)",
        "shell(popd:*)",
    ):
        assert rule in allow, f"{rule} must be allowed (regression: 148+ historical denials)"


# ---- Shell-escape / nested-interpreter denies --------------------------
#
# Without these denies an agent could route any forbidden command
# through a nested interpreter (e.g. `cmd /c "rm -rf .git"` would not
# match the prefix `shell(rm -rf /)` deny). Every nested-shell entry
# point must be explicitly closed.


def test_nested_shell_escapes_are_denied(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    _, deny = _split_allow_deny(flags)
    for rule in (
        "shell(Invoke-Expression)",
        "shell(Invoke-Expression:*)",
        "shell(iex)",
        "shell(iex:*)",
        "shell(Start-Process)",
        "shell(Start-Process:*)",
        "shell(cmd /c)",
        "shell(cmd /k)",
        "shell(cmd.exe /c)",
        "shell(cmd.exe /k)",
        "shell(powershell -c)",
        "shell(powershell -Command)",
        "shell(powershell -EncodedCommand)",
        "shell(powershell.exe -c)",
        "shell(powershell.exe -Command)",
        "shell(pwsh -c)",
        "shell(pwsh -Command)",
        "shell(bash -c)",
        "shell(sh -c)",
        "shell(zsh -c)",
    ):
        assert rule in deny, f"{rule} must be denied to prevent nested-shell escape"


def test_aidor_self_cli_allowed_but_run_subcommand_denied(tmp_path: Path):
    """Agents need `aidor --help`, `aidor doctor`, etc. while validating
    their own changes, but `aidor run` would spawn a nested orchestrator
    inside a phase: the child loop would deadlock on the parent's phase
    watchdog and create an unaudited recursion."""
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    assert "shell(aidor:*)" in allow
    assert "shell(aidor.exe:*)" in allow
    assert "shell(aidor run)" in deny
    assert "shell(aidor.exe run)" in deny


# ---- Deny-precedence pairings (user-requested) -------------------------
#
# These are the pairs where a broad `shell(cmd:*)` allow is present AND
# a specific dangerous sub-invocation must be denied. Per the Copilot
# CLI docs, `--deny-tool` takes precedence over `--allow-tool`, so the
# pair DOES block the dangerous invocation while permitting the family.


def test_git_family_allowed_but_push_and_remote_denied(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    assert "shell(git:*)" in allow
    for dangerous in (
        "shell(git push)",
        "shell(git remote)",
        "shell(git config --global)",
        "shell(git config --system)",
    ):
        assert dangerous in deny, f"{dangerous} must be denied alongside shell(git:*)"


def test_npm_family_allowed_but_global_installs_denied(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    assert "shell(npm:*)" in allow
    for dangerous in (
        "shell(npm install -g)",
        "shell(npm i -g)",
        "shell(npm install --global)",
    ):
        assert dangerous in deny


def test_npx_family_allowed_but_auto_install_denied(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    assert "shell(npx:*)" in allow
    for dangerous in ("shell(npx --yes)", "shell(npx -y)"):
        assert dangerous in deny


def test_pnpm_family_allowed_but_global_installs_denied(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    assert "shell(pnpm:*)" in allow
    for dangerous in (
        "shell(pnpm add -g)",
        "shell(pnpm install -g)",
        "shell(pnpm add --global)",
    ):
        assert dangerous in deny


def test_yarn_family_allowed_but_global_denied(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    assert "shell(yarn:*)" in allow
    assert "shell(yarn global)" in deny


def test_dotnet_family_allowed_but_global_tool_installs_denied(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    assert "shell(dotnet:*)" in allow
    for dangerous in (
        "shell(dotnet tool install -g)",
        "shell(dotnet tool install --global)",
        "shell(dotnet workload install)",
    ):
        assert dangerous in deny


def test_sdkmanager_allowed_but_install_subcommand_denied(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    assert "shell(sdkmanager:*)" in allow
    assert "shell(sdkmanager --install)" in deny


def test_python_family_allowed_but_dash_m_pip_install_denied(tmp_path: Path):
    """`shell(python:*)` matches `python -m pip install <pkg>`, which would
    bypass the explicit `shell(pip install)` deny. We therefore also
    deny the `python -m pip install` form (and `python3` / `py`)."""
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    assert "shell(python:*)" in allow
    for dangerous in (
        "shell(python -m pip install)",
        "shell(python3 -m pip install)",
        "shell(py -m pip install)",
    ):
        assert dangerous in deny


# ---- Allow families that should be present -----------------------------


def test_quality_gate_runners_are_allowed(tmp_path: Path):
    """The repo's mandatory validation commands (ruff, pip-audit, pytest,
    pre-commit, coverage) must be allowed so the coder can run the gates
    documented in AGENTS.md / pre-commit / CI."""
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, _ = _split_allow_deny(flags)
    for rule in (
        "shell(pytest:*)",
        "shell(ruff:*)",
        "shell(pip-audit:*)",
        "shell(pre-commit:*)",
        "shell(coverage:*)",
    ):
        assert rule in allow


def test_node_toolchain_allowed(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, _ = _split_allow_deny(flags)
    for rule in (
        "shell(node:*)",
        "shell(npm:*)",
        "shell(npx:*)",
        "shell(pnpm:*)",
        "shell(yarn:*)",
        "shell(tsc:*)",
        "shell(eslint:*)",
        "shell(prettier:*)",
        "shell(jest:*)",
        "shell(vitest:*)",
    ):
        assert rule in allow


def test_android_toolchain_allowed(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, _ = _split_allow_deny(flags)
    for rule in (
        "shell(gradle:*)",
        "shell(gradlew:*)",
        "shell(gradlew.bat:*)",
        "shell(mvn:*)",
        "shell(mvnw:*)",
        "shell(adb:*)",
        "shell(sdkmanager:*)",
        "shell(kotlin:*)",
        "shell(kotlinc:*)",
        "shell(java:*)",
        "shell(javac:*)",
    ):
        assert rule in allow


def test_powershell_inspection_cmdlets_allowed(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, _ = _split_allow_deny(flags)
    for rule in (
        "shell(Get-ChildItem:*)",
        "shell(Get-Content:*)",
        "shell(Test-Path:*)",
        "shell(Select-String:*)",
    ):
        assert rule in allow


# ---- Always-denied items -----------------------------------------------


def test_unrelated_shell_commands_remain_denied(tmp_path: Path):
    """Broad `cmd:*` allows must NOT reopen the universally-denied items."""
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    for cmd in (
        "shell(rm -rf /)",
        "shell(curl)",
        "shell(cargo install)",
        "shell(brew)",
        "shell(sudo)",
        "shell(doas)",
        "shell(winget)",
        "shell(apt)",
        "shell(apt-get)",
        "shell(scoop)",
        "shell(choco)",
    ):
        assert cmd not in allow
        assert cmd in deny


def test_copilot_self_management_denied(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    _, deny = _split_allow_deny(flags)
    for rule in (
        "shell(copilot update)",
        "shell(copilot login)",
        "shell(copilot logout)",
    ):
        assert rule in deny


# ---- Local-install ecosystem gating (review-0014 / review-0015) --------


def test_build_flags_with_local_install_marker(tmp_path: Path):
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    flags_on = build_flags(tmp_path, allow_local_install=True)
    flags_off = build_flags(tmp_path, allow_local_install=False)
    assert len(flags_on) >= len(flags_off)


def test_build_flags_no_marker_no_local_install(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, _ = _split_allow_deny(flags)
    # Without any lockfile marker, `shell(npm install)` must not be in the
    # allow list (it's the gated form; `shell(npm:*)` IS in the base allow).
    assert "shell(npm install)" not in allow


def test_pyproject_alone_is_not_a_lockfile_marker(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert detect_local_install_available(tmp_path) is False
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, _ = _split_allow_deny(flags)
    assert "shell(pip install -e)" not in allow


def test_pip_install_user_is_not_in_local_allowlist(tmp_path: Path):
    """`pip install --user` writes outside the repo and must never be allowed."""
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, _ = _split_allow_deny(flags)
    assert "shell(pip install --user)" not in allow


def test_python_local_install_does_not_shadow_pip_install_editable(tmp_path: Path):
    """Regression (review-0014): with --allow-local-install on for a
    Python repo with a real lockfile, the broad `shell(pip install)` deny
    must NOT be emitted, because deny rules take precedence and would
    otherwise shadow the lockfile-gated `shell(pip install -e)` allow."""
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, deny = _split_allow_deny(flags)

    assert "shell(pip install -e)" in allow
    assert "shell(pip install)" not in deny
    assert "shell(pip3 install)" not in deny
    assert "shell(pip install --user)" in deny
    assert "shell(pip install --target)" in deny


def test_python_local_install_off_keeps_broad_pip_install_deny(tmp_path: Path):
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=False)
    _, deny = _split_allow_deny(flags)
    assert "shell(pip install)" in deny
    assert "shell(pip3 install)" in deny


def test_local_install_for_non_python_repo_still_uses_broad_pip_deny(tmp_path: Path):
    """A JS-only repo opting into --allow-local-install must NOT relax the
    Python pip-install deny."""
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    _, deny = _split_allow_deny(flags)
    assert "shell(pip install)" in deny
    assert "shell(pip install --user)" not in deny


def test_pipx_install_remains_denied_under_python_local_install(tmp_path: Path):
    """pipx writes outside the repo regardless of any lockfile gate."""
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    _, deny = _split_allow_deny(flags)
    assert "shell(pipx install)" in deny


def test_js_lockfile_does_not_unlock_python_install_commands(tmp_path: Path):
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, _ = _split_allow_deny(flags)
    assert "shell(npm ci)" in allow
    assert "shell(npm install)" in allow
    assert "shell(uv pip install)" not in allow
    assert "shell(uv sync)" not in allow
    assert "shell(poetry install)" not in allow
    assert "shell(pip install -e)" not in allow


def test_python_lockfile_does_not_unlock_js_install_commands(tmp_path: Path):
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, _ = _split_allow_deny(flags)
    assert "shell(poetry install)" in allow
    assert "shell(pip install -e)" in allow
    assert "shell(npm ci)" not in allow
    assert "shell(npm install)" not in allow
    assert "shell(pnpm install)" not in allow
    assert "shell(yarn install)" not in allow


def test_requirements_txt_alone_is_not_a_lockfile_marker(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    assert detect_local_install_available(tmp_path) is False
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, deny = _split_allow_deny(flags)
    assert "shell(pip install -e)" not in allow
    assert "shell(poetry install)" not in allow
    assert "shell(pip install)" in deny


def test_rust_lockfile_only_unlocks_cargo(tmp_path: Path):
    (tmp_path / "Cargo.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, _ = _split_allow_deny(flags)
    assert "shell(cargo build)" in allow
    assert "shell(cargo fetch)" in allow
    assert "shell(npm ci)" not in allow
    assert "shell(poetry install)" not in allow
    assert "shell(go mod download)" not in allow


def test_polyglot_repo_unlocks_only_present_ecosystems(tmp_path: Path):
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, _ = _split_allow_deny(flags)
    assert "shell(poetry install)" in allow
    assert "shell(yarn install)" in allow
    assert "shell(cargo build)" not in allow
    assert "shell(go mod download)" not in allow
    assert "shell(pixi install)" not in allow
