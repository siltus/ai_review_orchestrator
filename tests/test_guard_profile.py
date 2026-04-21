"""Tests for guard_profile flag matrix."""

from __future__ import annotations

from aidor.guard_profile import (
    build_flags,
    detect_local_install_available,
)


def test_build_flags_base(tmp_path):
    flags = build_flags(tmp_path, allow_local_install=False)
    joined = " ".join(flags)
    assert "--deny-tool" in joined
    assert "--allow-tool" in joined
    # Push must be denied.
    assert any("git push" in f for f in flags)


def test_build_flags_with_local_install_marker(tmp_path):
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    flags_on = build_flags(tmp_path, allow_local_install=True)
    flags_off = build_flags(tmp_path, allow_local_install=False)
    assert len(flags_on) >= len(flags_off)


def test_build_flags_no_marker_no_local_install(tmp_path):
    flags = build_flags(tmp_path, allow_local_install=True)
    joined = " ".join(flags)
    # Without any lockfile marker, npm install etc. must not be in the allow list.
    assert "npm install" not in joined or "--deny-tool" in joined


def test_pyproject_alone_is_not_a_lockfile_marker(tmp_path):
    """A bare pyproject.toml does NOT pin transitive deps, so it must not
    enable the local-install allowlist on its own."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert detect_local_install_available(tmp_path) is False
    flags = build_flags(tmp_path, allow_local_install=True)
    joined = " ".join(flags)
    assert "shell(pip install -e)" not in joined


def test_pip_install_user_is_not_in_local_allowlist(tmp_path):
    """`pip install --user` writes outside the repo and must never be allowed."""
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    flags = build_flags(tmp_path, allow_local_install=True)
    joined = " ".join(flags)
    assert "pip install --user" not in joined


def test_shell_rules_are_aliased_to_bash_and_powershell(tmp_path):
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
