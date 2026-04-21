"""End-to-end tests for the `aidor` CLI commands.

These tests use Typer's built-in `CliRunner` to invoke the app in-process
and assert on exit codes plus filesystem side-effects. They exercise:

- `aidor abort` — must write `.aidor/ABORT` and flip state.json status,
  even when no state.json exists yet (regression test for the incomplete
  abort contract flagged in review-0001).
- `aidor status` / `aidor summary` — negative paths when no state.json is
  present, and the happy path after a minimal state is written.
- `aidor clean` — removes `.aidor/` when confirmed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aidor.cli import app
from aidor.state import State

runner = CliRunner()


# ---- abort ---------------------------------------------------------------


def test_abort_writes_abort_marker_even_without_state(tmp_path: Path):
    """Regression: previously `aidor abort` refused to run if state.json was
    missing AND never wrote `.aidor/ABORT`, so manual aborts during startup
    had no effect on running Copilot subprocesses or pending ask_user hooks.

    The abort marker is now the single source of truth for abort; the state
    file update is best-effort.
    """
    result = runner.invoke(app, ["abort", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    abort_marker = tmp_path / ".aidor" / "ABORT"
    assert abort_marker.exists()
    assert "aborted_via=cli" in abort_marker.read_text(encoding="utf-8")


def test_abort_updates_state_when_present(tmp_path: Path):
    aidor_dir = tmp_path / ".aidor"
    aidor_dir.mkdir()
    state_path = aidor_dir / "state.json"
    State(status="running").save(state_path)

    result = runner.invoke(app, ["abort", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.stdout

    assert (aidor_dir / "ABORT").exists()
    assert State.load(state_path).status == "aborted"


# ---- status --------------------------------------------------------------


def test_status_errors_without_state_json(tmp_path: Path):
    result = runner.invoke(app, ["status", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "no .aidor/state.json" in result.stdout


def test_status_prints_current_state(tmp_path: Path):
    aidor_dir = tmp_path / ".aidor"
    aidor_dir.mkdir()
    State(status="running", started_at="2026-04-21T09:00:00Z").save(aidor_dir / "state.json")
    result = runner.invoke(app, ["status", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "running" in result.stdout


# ---- summary -------------------------------------------------------------


def test_summary_errors_without_state_json(tmp_path: Path):
    result = runner.invoke(app, ["summary", "--repo", str(tmp_path)])
    assert result.exit_code == 1


def test_summary_writes_markdown(tmp_path: Path):
    aidor_dir = tmp_path / ".aidor"
    aidor_dir.mkdir()
    State(status="converged").save(aidor_dir / "state.json")
    result = runner.invoke(app, ["summary", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert (aidor_dir / "summary.md").exists()


# ---- clean ---------------------------------------------------------------


def test_clean_removes_aidor_dir(tmp_path: Path):
    aidor_dir = tmp_path / ".aidor"
    aidor_dir.mkdir()
    (aidor_dir / "state.json").write_text("{}", encoding="utf-8")
    result = runner.invoke(app, ["clean", "--repo", str(tmp_path), "-y"])
    assert result.exit_code == 0, result.stdout
    assert not aidor_dir.exists()


def test_clean_on_empty_repo_is_noop(tmp_path: Path):
    result = runner.invoke(app, ["clean", "--repo", str(tmp_path), "-y"])
    assert result.exit_code == 0
    assert "nothing to clean" in result.stdout


# ---- corrupted state.json ------------------------------------------------


def _write_corrupt_state(tmp_path: Path, payload: str) -> Path:
    aidor_dir = tmp_path / ".aidor"
    aidor_dir.mkdir()
    state_path = aidor_dir / "state.json"
    state_path.write_text(payload, encoding="utf-8")
    return state_path


def test_status_handles_corrupt_state_cleanly(tmp_path: Path):
    """Regression (review-0009): a malformed state.json must produce a
    clean operator-facing error (exit 2), not a traceback."""
    _write_corrupt_state(tmp_path, "{not valid json")
    result = runner.invoke(app, ["status", "--repo", str(tmp_path)])
    assert result.exit_code == 2, result.stdout
    assert "could not load" in result.stdout


def test_summary_handles_malformed_phases_cleanly(tmp_path: Path):
    """Regression (review-0009): malformed nested `phases` must not leak
    an AttributeError out of summary."""
    _write_corrupt_state(
        tmp_path,
        '{"rounds": [{"index": 1, "phases": "oops"}]}',
    )
    result = runner.invoke(app, ["summary", "--repo", str(tmp_path)])
    assert result.exit_code == 2, result.stdout
    assert "could not load" in result.stdout


def test_abort_handles_corrupt_state_cleanly(tmp_path: Path):
    """Regression (review-0009): even `aidor abort` should fail cleanly
    if state.json is unreadable, rather than raising."""
    _write_corrupt_state(tmp_path, "[]")
    result = runner.invoke(app, ["abort", "--repo", str(tmp_path)])
    assert result.exit_code == 2, result.stdout
    assert "could not load" in result.stdout


# ---- version -------------------------------------------------------------


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "aidor" in result.stdout


# ---- corrupted top-level scalars (review-0010) ---------------------------


def test_status_handles_corrupt_current_round_cleanly(tmp_path: Path):
    """Regression (review-0010): a malformed top-level `current_round`
    (string instead of int) must be rejected by State.load with a clean
    operator-facing error, not leak through to a TypeError on resume."""
    _write_corrupt_state(tmp_path, '{"current_round": "1", "rounds": [], "notes": []}')
    result = runner.invoke(app, ["status", "--repo", str(tmp_path)])
    assert result.exit_code == 2, result.stdout
    assert "could not load" in result.stdout
    assert "current_round" in result.stdout


def test_status_handles_corrupt_status_field_cleanly(tmp_path: Path):
    _write_corrupt_state(tmp_path, '{"status": "wat", "rounds": [], "notes": []}')
    result = runner.invoke(app, ["status", "--repo", str(tmp_path)])
    assert result.exit_code == 2, result.stdout
    assert "could not load" in result.stdout


def test_status_handles_corrupt_notes_element_cleanly(tmp_path: Path):
    _write_corrupt_state(tmp_path, '{"rounds": [], "notes": [123]}')
    result = runner.invoke(app, ["status", "--repo", str(tmp_path)])
    assert result.exit_code == 2, result.stdout
    assert "could not load" in result.stdout


def test_status_handles_null_current_round_cleanly(tmp_path: Path):
    _write_corrupt_state(tmp_path, '{"current_round": null, "rounds": [], "notes": []}')
    result = runner.invoke(app, ["status", "--repo", str(tmp_path)])
    assert result.exit_code == 2, result.stdout


# ---- doctor (review-0010) ------------------------------------------------


def test_doctor_runs_and_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Regression (review-0010): `aidor doctor` must execute end-to-end and
    report Python/copilot/repo checks. Uses a guaranteed-missing copilot
    binary so the command exits with code 2 (FAIL) but still prints all
    sections without raising."""
    result = runner.invoke(
        app,
        [
            "doctor",
            "--repo",
            str(tmp_path),
            "--copilot-binary",
            "definitely-not-a-real-binary-aidor-test",
        ],
    )
    assert result.exit_code == 2, result.stdout
    assert "Python >= 3.11" in result.stdout
    assert "copilot binary" in result.stdout
    assert "repo is a directory" in result.stdout


def test_doctor_macos_caffeinate_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Regression (review-0010): the macOS `caffeinate` doctor branch added
    in round 9 shipped without a regression test. Force `sys.platform` to
    `darwin` and confirm the caffeinate check is emitted."""
    import aidor.cli as cli_mod

    monkeypatch.setattr(cli_mod.sys, "platform", "darwin")
    result = runner.invoke(
        app,
        [
            "doctor",
            "--repo",
            str(tmp_path),
            "--copilot-binary",
            "definitely-not-a-real-binary-aidor-test",
        ],
    )
    # Exit code is 2 because copilot is missing (FAIL); we only care that
    # the macOS branch ran.
    assert "caffeinate" in result.stdout


def test_doctor_linux_systemd_inhibit_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import aidor.cli as cli_mod

    monkeypatch.setattr(cli_mod.sys, "platform", "linux")
    result = runner.invoke(
        app,
        [
            "doctor",
            "--repo",
            str(tmp_path),
            "--copilot-binary",
            "definitely-not-a-real-binary-aidor-test",
        ],
    )
    assert "systemd-inhibit" in result.stdout


def test_doctor_windows_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import aidor.cli as cli_mod

    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    result = runner.invoke(
        app,
        [
            "doctor",
            "--repo",
            str(tmp_path),
            "--copilot-binary",
            "definitely-not-a-real-binary-aidor-test",
        ],
    )
    assert "Windows wake-lock" in result.stdout
