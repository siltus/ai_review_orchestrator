"""Tests for guard_profile.

The module returns a fixed two-flag set (--allow-all-tools
--allow-all-paths); the security policy is enforced in
hook_resolver.py. See the guard_profile module docstring for the
rationale and source references.
"""

from __future__ import annotations

from pathlib import Path

from aidor.guard_profile import (
    build_flags,
    detect_local_install_available,
    detect_python_lockfile,
)


def test_build_flags_emits_allow_all(tmp_path: Path):
    assert build_flags(tmp_path, allow_local_install=False) == [
        "--allow-all-tools",
        "--allow-all-paths",
    ]


def test_build_flags_is_independent_of_local_install_toggle(tmp_path: Path):
    on = build_flags(tmp_path, allow_local_install=True)
    off = build_flags(tmp_path, allow_local_install=False)
    assert on == off == ["--allow-all-tools", "--allow-all-paths"]


def test_build_flags_is_independent_of_repo_contents(tmp_path: Path):
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "Cargo.lock").write_text("", encoding="utf-8")
    assert build_flags(tmp_path, allow_local_install=True) == [
        "--allow-all-tools",
        "--allow-all-paths",
    ]


def test_detect_local_install_false_on_empty_repo(tmp_path: Path):
    assert detect_local_install_available(tmp_path) is False


def test_detect_local_install_true_with_package_lock(tmp_path: Path):
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert detect_local_install_available(tmp_path) is True


def test_detect_local_install_true_with_poetry_lock(tmp_path: Path):
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    assert detect_local_install_available(tmp_path) is True


def test_detect_local_install_true_with_cargo_lock(tmp_path: Path):
    (tmp_path / "Cargo.lock").write_text("", encoding="utf-8")
    assert detect_local_install_available(tmp_path) is True


def test_detect_local_install_true_with_go_sum(tmp_path: Path):
    (tmp_path / "go.sum").write_text("", encoding="utf-8")
    assert detect_local_install_available(tmp_path) is True


def test_detect_local_install_false_on_pyproject_only(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert detect_local_install_available(tmp_path) is False


def test_detect_python_lockfile_false_on_empty_repo(tmp_path: Path):
    assert detect_python_lockfile(tmp_path) is False


def test_detect_python_lockfile_true_with_poetry_lock(tmp_path: Path):
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    assert detect_python_lockfile(tmp_path) is True


def test_detect_python_lockfile_true_with_uv_lock(tmp_path: Path):
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    assert detect_python_lockfile(tmp_path) is True


def test_detect_python_lockfile_true_with_pipfile_lock(tmp_path: Path):
    (tmp_path / "Pipfile.lock").write_text("", encoding="utf-8")
    assert detect_python_lockfile(tmp_path) is True


def test_detect_python_lockfile_false_with_cargo_only(tmp_path: Path):
    (tmp_path / "Cargo.lock").write_text("", encoding="utf-8")
    assert detect_python_lockfile(tmp_path) is False
