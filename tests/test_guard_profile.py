"""Tests for guard_profile flag matrix."""

from __future__ import annotations

from pathlib import Path

from aidor.guard_profile import (
    build_flags,
    detect_local_install_available,
)


def test_build_flags_base(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=False)
    joined = " ".join(flags)
    assert "--deny-tool" in joined
    assert "--allow-tool" in joined
    # Push must be denied.
    assert any("git push" in f for f in flags)


def test_build_flags_with_local_install_marker(tmp_path: Path):
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    flags_on = build_flags(tmp_path, allow_local_install=True)
    flags_off = build_flags(tmp_path, allow_local_install=False)
    assert len(flags_on) >= len(flags_off)


def test_build_flags_no_marker_no_local_install(tmp_path: Path):
    flags = build_flags(tmp_path, allow_local_install=True)
    joined = " ".join(flags)
    # Without any lockfile marker, npm install etc. must not be in the allow list.
    assert "npm install" not in joined or "--deny-tool" in joined


def test_pyproject_alone_is_not_a_lockfile_marker(tmp_path: Path):
    """A bare pyproject.toml does NOT pin transitive deps, so it must not
    enable the local-install allowlist on its own."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert detect_local_install_available(tmp_path) is False
    flags = build_flags(tmp_path, allow_local_install=True)
    joined = " ".join(flags)
    assert "shell(pip install -e)" not in joined


def test_pip_install_user_is_not_in_local_allowlist(tmp_path: Path):
    """`pip install --user` writes outside the repo and must never be allowed."""
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    allow_flags = [f for f in flags if f.startswith("--allow-tool=")]
    allow_joined = " ".join(allow_flags)
    # It must not appear in any allow rule. (It MAY appear as a deny
    # rule under review-0014's narrow Python deny list, which is fine.)
    assert "pip install --user" not in allow_joined


def test_shell_rules_are_aliased_to_bash_and_powershell(tmp_path: Path):
    """Every shell(...) rule must also be expressed as bash(...) and
    powershell(...) so the Guard matrix matches whichever underlying tool
    name Copilot uses on this platform."""
    flags = build_flags(tmp_path, allow_local_install=False)
    assert any(f == "--deny-tool=shell(git push)" for f in flags)
    assert any(f == "--deny-tool=bash(git push)" for f in flags)
    assert any(f == "--deny-tool=powershell(git push)" for f in flags)
    assert any(f == "--allow-tool=shell(git status)" for f in flags)
    assert any(f == "--allow-tool=bash(git status)" for f in flags)
    assert any(f == "--allow-tool=powershell(git status)" for f in flags)


# ---- Deny-precedence regression (review-0014) ----------------------------


def _split_allow_deny(flags: list[str]) -> tuple[list[str], list[str]]:
    allow = [f.removeprefix("--allow-tool=") for f in flags if f.startswith("--allow-tool=")]
    deny = [f.removeprefix("--deny-tool=") for f in flags if f.startswith("--deny-tool=")]
    return allow, deny


def _deny_shadows(rule: str, deny: list[str]) -> bool:
    """Model the documented deny-precedence rule: a deny entry shadows
    `rule` if it is a string prefix of `rule` (the same prefix-match
    semantics the broad `shell(pip install)` deny relied on)."""
    target = rule.rstrip(")")
    for d in deny:
        d_stripped = d.rstrip(")")
        if target.startswith(d_stripped):
            return True
    return False


def test_python_local_install_does_not_shadow_pip_install_editable(tmp_path: Path):
    """Regression (review-0014): with --allow-local-install on for a
    Python repo with a real lockfile, the broad `shell(pip install)` deny
    must NOT be emitted, because deny rules take precedence and would
    otherwise shadow the lockfile-gated `shell(pip install -e)` allow
    entry — making the documented `--allow-local-install` capability a
    no-op for Python repos."""
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, deny = _split_allow_deny(flags)

    assert "shell(pip install -e)" in allow
    # The broad pip install deny must be gone for Python repos with a
    # lockfile when --allow-local-install is on.
    assert "shell(pip install)" not in deny
    assert "shell(pip3 install)" not in deny
    # The narrow Python-global denies must still be present so we do not
    # silently allow installs that escape the repo (`--user`, etc.).
    assert "shell(pip install --user)" in deny
    assert "shell(pip install --target)" in deny
    # And the editable-install allow must NOT be shadowed by any deny
    # under the documented prefix-precedence semantics.
    assert not _deny_shadows("shell(pip install -e)", deny), (
        f"deny precedence still shadows pip install -e: {[d for d in deny if 'pip' in d]}"
    )


def test_python_local_install_off_keeps_broad_pip_install_deny(tmp_path: Path):
    """Without --allow-local-install, the broad `pip install` deny must
    still be in effect (defence in depth for repos that don't opt in)."""
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=False)
    _, deny = _split_allow_deny(flags)
    assert "shell(pip install)" in deny
    assert "shell(pip3 install)" in deny


def test_local_install_for_non_python_repo_still_uses_broad_pip_deny(tmp_path: Path):
    """A JS-only repo opting into --allow-local-install must NOT relax the
    Python pip-install deny — only Python repos get the narrow form."""
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    _, deny = _split_allow_deny(flags)
    assert "shell(pip install)" in deny
    assert "shell(pip install --user)" not in deny


def test_pipx_install_remains_denied_under_python_local_install(tmp_path: Path):
    """pipx writes outside the repo regardless of any lockfile gate, so it
    must remain denied even when --allow-local-install relaxes the
    `pip install` deny for a Python repo."""
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    _, deny = _split_allow_deny(flags)
    assert "shell(pipx install)" in deny


# ---- Ecosystem-scoped local-install allow (review-0015) ------------------


def test_js_lockfile_does_not_unlock_python_install_commands(tmp_path: Path):
    """Regression (review-0015): a repo with only `package-lock.json` must
    NOT enable Python install commands like `uv pip install` or
    `poetry install`. Each ecosystem's allow set is gated on its own
    lockfile being present."""
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, _ = _split_allow_deny(flags)
    # JS commands are unlocked.
    assert "shell(npm ci)" in allow
    assert "shell(npm install)" in allow
    # Python commands must NOT be unlocked from a JS-only lockfile.
    assert "shell(uv pip install)" not in allow
    assert "shell(uv sync)" not in allow
    assert "shell(poetry install)" not in allow
    assert "shell(pip install -e)" not in allow


def test_python_lockfile_does_not_unlock_js_install_commands(tmp_path: Path):
    """The reverse: a Python-only repo must not unlock `npm`/`pnpm`/`yarn`."""
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
    """Regression (review-0015): a bare `requirements.txt` is not a real
    lockfile (no hashes, no pinned transitive graph). The docs frame
    `--allow-local-install` around real lockfiles, so `requirements.txt`
    on its own must NOT enable the install allowlist."""
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    assert detect_local_install_available(tmp_path) is False
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, deny = _split_allow_deny(flags)
    assert "shell(pip install -e)" not in allow
    assert "shell(poetry install)" not in allow
    # And the broad pip install deny must remain in effect — without a
    # real Python lockfile, we don't relax it.
    assert "shell(pip install)" in deny


def test_rust_lockfile_only_unlocks_cargo(tmp_path: Path):
    """A Rust repo with `Cargo.lock` unlocks cargo commands only."""
    (tmp_path / "Cargo.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, _ = _split_allow_deny(flags)
    assert "shell(cargo build)" in allow
    assert "shell(cargo fetch)" in allow
    assert "shell(npm ci)" not in allow
    assert "shell(poetry install)" not in allow
    assert "shell(go mod download)" not in allow


def test_polyglot_repo_unlocks_only_present_ecosystems(tmp_path: Path):
    """A repo with both Python and JS lockfiles unlocks both — but
    nothing else (Rust/Go/pixi remain denied)."""
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    allow, _ = _split_allow_deny(flags)
    assert "shell(poetry install)" in allow
    assert "shell(yarn install)" in allow
    assert "shell(cargo build)" not in allow
    assert "shell(go mod download)" not in allow
    assert "shell(pixi install)" not in allow


# ---- Quality-gate validation commands (review-0017) ---------------------


def test_quality_gate_commands_are_allowed(tmp_path: Path):
    """Regression (review-0017): the allowlist must permit the repo's
    own mandatory validation commands (ruff, pip-audit, pytest,
    pre-commit) so the launched coder can actually run the quality
    gates documented in AGENTS.md / pre-commit / CI. Each shell rule
    must also be aliased to bash() and powershell()."""
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, _ = _split_allow_deny(flags)
    for rule in (
        "shell(python -m ruff)",
        "shell(python -m pip_audit)",
        "shell(python -m pytest)",
        "shell(python -m pre_commit)",
        "shell(pytest)",
        "shell(ruff)",
        "shell(pip-audit)",
        "shell(pre-commit)",
        # Bare interpreter + launcher forms (transcript evidence from
        # the 17-round dogfood run: PowerShell coders repeatedly invoked
        # `python`, `py`, `pytest.exe`, `ruff.exe`, etc. and got denied).
        "shell(python)",
        "shell(python.exe)",
        "shell(py)",
        "shell(pytest.exe)",
        "shell(ruff.exe)",
        "shell(pip-audit.exe)",
        "shell(pre-commit.exe)",
        # Inspection utilities.
        "shell(where)",
        "shell(Get-ChildItem)",
        "shell(Get-Content)",
        "shell(Test-Path)",
        # Bare `git` (broad but dangerous subcommands remain denied).
        "shell(git)",
    ):
        assert rule in allow, f"missing allow for {rule}"
        # Aliased forms must also appear so the matrix matches whichever
        # tool name Copilot picks on this platform.
        inner = rule[len("shell(") : -1]
        assert f"bash({inner})" in allow
        assert f"powershell({inner})" in allow


def test_unrelated_shell_commands_remain_denied(tmp_path: Path):
    """Whitelisting the quality gates must NOT reopen arbitrary shell
    access — unrelated commands like `curl` and `npm install -g` must
    still be denied."""
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    # Commands that have no allow entry at all.
    for cmd in (
        "shell(rm -rf /)",
        "shell(curl)",
        "shell(npm install -g)",
        "shell(cargo install)",
        "shell(brew)",
    ):
        assert cmd not in allow
    # And the explicit denies are still emitted.
    for d in (
        "shell(git push)",
        "shell(curl)",
        "shell(npm install -g)",
        "shell(sudo)",
        # New ecosystems: install-global / self-update escape hatches
        # stay denied even though the bare tool is allowed.
        "shell(dotnet tool install -g)",
        "shell(dotnet tool install --global)",
        "shell(npx --yes)",
        "shell(npx -y)",
        "shell(sdkmanager --install)",
    ):
        assert d in deny


# ---- Extended ecosystem allowlist (Node / .NET / Android) --------------


def test_node_toolchain_allowed(tmp_path: Path):
    """Node / TypeScript coders must be able to invoke the bare
    toolchain without tripping the Guard. Global-install forms stay
    denied."""
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    for rule in (
        "shell(node)",
        "shell(npm)",
        "shell(npx)",
        "shell(pnpm)",
        "shell(yarn)",
        "shell(tsc)",
        "shell(eslint)",
        "shell(prettier)",
        "shell(jest)",
        "shell(vitest)",
    ):
        assert rule in allow, f"missing node allow for {rule}"
    for rule in (
        "shell(npm install -g)",
        "shell(npm i -g)",
        "shell(pnpm add -g)",
        "shell(yarn global)",
        "shell(npx --yes)",
    ):
        assert rule in deny, f"expected deny for {rule}"


def test_dotnet_toolchain_allowed(tmp_path: Path):
    """.NET / C# coders must be able to invoke `dotnet`, `msbuild`,
    `nuget`. Global-tool installs stay denied."""
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    for rule in ("shell(dotnet)", "shell(msbuild)", "shell(nuget)"):
        assert rule in allow, f"missing dotnet allow for {rule}"
    for rule in (
        "shell(dotnet tool install -g)",
        "shell(dotnet tool install --global)",
        "shell(dotnet workload install)",
    ):
        assert rule in deny, f"expected deny for {rule}"


def test_android_toolchain_allowed(tmp_path: Path):
    """Android / Gradle coders must be able to invoke Gradle wrappers,
    Maven, `adb`, and the JDK. SDK-install commands stay denied."""
    flags = build_flags(tmp_path, allow_local_install=False)
    allow, deny = _split_allow_deny(flags)
    for rule in (
        "shell(gradle)",
        "shell(gradlew)",
        "shell(gradlew.bat)",
        "shell(mvn)",
        "shell(mvnw)",
        "shell(adb)",
        "shell(sdkmanager)",
        "shell(kotlin)",
        "shell(kotlinc)",
        "shell(java)",
        "shell(javac)",
    ):
        assert rule in allow, f"missing android allow for {rule}"
    assert "shell(sdkmanager --install)" in deny
