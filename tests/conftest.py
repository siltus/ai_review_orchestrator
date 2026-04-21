"""Shared test fixtures."""

from __future__ import annotations

import stat
import sys
import textwrap
from pathlib import Path

import pytest

from aidor.config import RunConfig


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Initialise a bare git-less 'repo' scratch directory."""
    (tmp_path / "AGENTS.md").write_text("# project\n\nExisting content.\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def run_config(tmp_repo: Path, fake_copilot_binary: Path) -> RunConfig:
    return RunConfig(
        repo=tmp_repo,
        coder_model="test-coder",
        reviewer_model="test-reviewer",
        max_rounds=2,
        idle_timeout_s=10,
        round_timeout_s=60,
        max_restarts_per_round=0,
        keep_awake=False,
        copilot_binary=str(fake_copilot_binary),
    )


@pytest.fixture
def fake_copilot_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate an OS-appropriate fake-copilot launcher.

    On POSIX, returns a shell wrapper that execs the fake_copilot.py script.
    On Windows, returns a .cmd wrapper that invokes python with the script.
    """
    scripts_dir = tmp_path_factory.mktemp("bin")
    fake_py = Path(__file__).parent / "fake_copilot.py"

    if sys.platform == "win32":
        wrapper = scripts_dir / "fake_copilot.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{fake_py}" %*\r\n',
            encoding="utf-8",
        )
    else:
        wrapper = scripts_dir / "fake_copilot.sh"
        wrapper.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                exec "{sys.executable}" "{fake_py}" "$@"
                """
            ),
            encoding="utf-8",
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return wrapper
