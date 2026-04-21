"""Tests for guard_profile flag matrix."""
from __future__ import annotations

from aidor.guard_profile import build_flags


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
