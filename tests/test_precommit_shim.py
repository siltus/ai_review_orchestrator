"""Tests for ``scripts/precommit_shim.py`` and the structural
guarantees of ``.pre-commit-config.yaml`` that depend on it.

The shim re-execs into the repo-local ``.venv`` interpreter so the
mandatory pre-commit gate is hermetic regardless of which ``python``
happens to be on ``PATH`` at hook-launch time. Without the shim,
pre-commit dropped back to the system interpreter on Windows and
failed with ``ModuleNotFoundError: No module named 'aidor'`` /
``No module named 'pip_audit'`` — the supposed always-green local
gate was an environment lottery.

Regressions covered: review-0001 (shim resolution + no bare
``python -m`` entries in ``.pre-commit-config.yaml``), review-0008
(the header comment in ``.pre-commit-config.yaml`` and the opening
docstring in this shim must stay in sync with the actual hook set).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_shim():
    """Import ``scripts/precommit_shim.py`` by path (it isn't a package)."""
    shim_path = REPO_ROOT / "scripts" / "precommit_shim.py"
    spec = importlib.util.spec_from_file_location("precommit_shim", shim_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- Shim interpreter resolution ----------------------------------------


def test_precommit_shim_file_exists():
    assert (REPO_ROOT / "scripts" / "precommit_shim.py").is_file()


def test_precommit_shim_resolves_windows_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    shim = _load_shim()
    venv_py = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")
    monkeypatch.setattr(shim.os, "name", "nt", raising=False)
    assert shim._venv_python(tmp_path) == venv_py


def test_precommit_shim_resolves_posix_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    shim = _load_shim()
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")
    monkeypatch.setattr(shim.os, "name", "posix", raising=False)
    assert shim._venv_python(tmp_path) == venv_py


def test_precommit_shim_falls_back_to_python3_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    shim = _load_shim()
    venv_py = tmp_path / ".venv" / "bin" / "python3"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")
    monkeypatch.setattr(shim.os, "name", "posix", raising=False)
    assert shim._venv_python(tmp_path) == venv_py


def test_precommit_shim_errors_when_no_venv(tmp_path: Path):
    shim = _load_shim()
    with pytest.raises(SystemExit) as excinfo:
        shim._venv_python(tmp_path)
    assert ".venv" in str(excinfo.value)


# ---- .pre-commit-config.yaml structural guarantees ----------------------


def test_precommit_config_does_not_use_bare_python_dash_m():
    """Every hook entry must route through the shim. A bare
    ``python -m ...`` re-introduces the PATH-roulette bug.
    (regression: review-0001)"""
    cfg = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    for line in cfg.splitlines():
        stripped = line.strip()
        if stripped.startswith("entry:"):
            assert "scripts/precommit_shim.py" in stripped, (
                f"pre-commit hook entry must use the shim, got: {stripped!r}"
            )
            assert not stripped.startswith("entry: python -m"), (
                f"bare `python -m` re-introduces PATH lottery: {stripped!r}"
            )


def test_pre_commit_config_header_mentions_five_hooks_and_pyright() -> None:
    """The header comment must stay in sync with the actual hook
    count (was stale after pyright became the fifth hook).
    (regression: review-0008)"""
    text = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "All four hooks" not in text, (
        "stale comment from before pyright was added; should say five hooks"
    )
    assert "five hooks" in text, "header comment should reflect actual hook count"
    assert "pyright" in text.split("repos:", 1)[1].split("- id:", 1)[0], (
        "header comment should enumerate pyright alongside the other hooks"
    )


def test_precommit_shim_docstring_has_no_fused_sentence() -> None:
    """The opening docstring lost a space across a sentence boundary
    in an earlier edit. (regression: review-0008)"""
    text = (REPO_ROOT / "scripts" / "precommit_shim.py").read_text(encoding="utf-8")
    assert "dependencies.Earlier" not in text, "missing space after period in opening docstring"
    assert "dependencies. Earlier" in text
