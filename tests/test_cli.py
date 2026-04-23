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


def test_clean_removes_hooks_file_too(tmp_path: Path):
    """``aidor clean`` must remove the active enforcement hook
    (``.github/hooks/aidor.json``) AND the agent template files
    (``.github/agents/aidor-*.md``) alongside ``.aidor/``. Leftover
    hook files were the root cause of post-run interactive copilot
    sessions seeing "outside the repo" denials on Copilot's own
    scratch files; leftover agent files keep the operator's manual
    sessions running under aidor's contract."""
    aidor_dir = tmp_path / ".aidor"
    aidor_dir.mkdir()
    (aidor_dir / "state.json").write_text("{}", encoding="utf-8")
    hooks_dir = tmp_path / ".github" / "hooks"
    hooks_dir.mkdir(parents=True)
    hooks_file = hooks_dir / "aidor.json"
    hooks_file.write_text("{}", encoding="utf-8")
    agents_dir = tmp_path / ".github" / "agents"
    agents_dir.mkdir(parents=True)
    coder = agents_dir / "aidor-coder.md"
    reviewer = agents_dir / "aidor-reviewer.md"
    coder.write_text("# coder\n", encoding="utf-8")
    reviewer.write_text("# reviewer\n", encoding="utf-8")

    result = runner.invoke(app, ["clean", "--repo", str(tmp_path), "-y"])
    assert result.exit_code == 0, result.stdout
    assert not aidor_dir.exists()
    assert not hooks_file.exists()
    assert not hooks_dir.exists()  # empty dir cleaned up
    assert not coder.exists()
    assert not reviewer.exists()
    assert not agents_dir.exists()  # empty dir cleaned up


def test_clean_removes_only_hooks_file_if_aidor_dir_absent(tmp_path: Path):
    """If only the hook file remains (e.g. operator already manually
    deleted ``.aidor/``), ``aidor clean`` must still remove it instead
    of reporting "nothing to clean"."""
    hooks_dir = tmp_path / ".github" / "hooks"
    hooks_dir.mkdir(parents=True)
    hooks_file = hooks_dir / "aidor.json"
    hooks_file.write_text("{}", encoding="utf-8")

    result = runner.invoke(app, ["clean", "--repo", str(tmp_path), "-y"])
    assert result.exit_code == 0, result.stdout
    assert not hooks_file.exists()


def test_clean_removes_only_agent_files_if_other_artefacts_absent(tmp_path: Path):
    """If only the agent files remain (the hook file was already removed
    by a prior teardown; ``.aidor/`` was manually deleted), ``aidor
    clean`` must still pick them up."""
    agents_dir = tmp_path / ".github" / "agents"
    agents_dir.mkdir(parents=True)
    coder = agents_dir / "aidor-coder.md"
    coder.write_text("# coder\n", encoding="utf-8")

    result = runner.invoke(app, ["clean", "--repo", str(tmp_path), "-y"])
    assert result.exit_code == 0, result.stdout
    assert not coder.exists()


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


# ---- doctor: Copilot CLI version floor (review-0001) --------------------


def _make_subprocess_run(stdout: str, returncode: int = 0):
    """Return a fake ``subprocess.run`` that yields the given version stdout.

    Used to exercise the ``aidor doctor`` version-floor branch without
    depending on a real ``copilot`` binary being present on PATH.
    """

    class _Completed:
        def __init__(self) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def _fake_run(*_args, **_kwargs):
        return _Completed()

    return _fake_run


def test_doctor_rejects_old_copilot_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Regression (review-0001): `aidor doctor` must enforce the documented
    minimum Copilot CLI version (1.0.32+). An older version must cause a
    non-zero exit so operators aren't fooled into thinking a stale CLI is
    healthy."""
    import subprocess

    import aidor.cli as cli_mod

    monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: "/fake/copilot")
    monkeypatch.setattr(subprocess, "run", _make_subprocess_run("copilot 1.0.31"))
    result = runner.invoke(app, ["doctor", "--repo", str(tmp_path)])
    assert result.exit_code == 2, result.stdout
    assert "copilot >= 1.0.32" in result.stdout
    assert "installed=1.0.31" in result.stdout


def test_doctor_accepts_new_enough_copilot_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Regression (review-0001): a Copilot CLI at or above the minimum must
    PASS the version-floor check."""
    import subprocess

    import aidor.cli as cli_mod

    monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: "/fake/copilot")
    monkeypatch.setattr(subprocess, "run", _make_subprocess_run("copilot 1.0.35-2"))
    result = runner.invoke(app, ["doctor", "--repo", str(tmp_path)])
    assert "copilot >= 1.0.32" in result.stdout
    assert "installed=1.0.35" in result.stdout
    # Line must be reported OK (green), not FAIL. The check helper prefixes
    # FAIL only on failures, so absence of "FAIL copilot >= 1.0.32" is the
    # signal we want.
    assert "FAIL copilot >= 1.0.32" not in result.stdout


def test_doctor_flags_unparseable_copilot_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Regression (review-0001): if `copilot --version` prints something we
    can't parse, doctor must fail the version-floor check instead of
    silently passing."""
    import subprocess

    import aidor.cli as cli_mod

    monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: "/fake/copilot")
    monkeypatch.setattr(subprocess, "run", _make_subprocess_run("copilot (unknown build)"))
    result = runner.invoke(app, ["doctor", "--repo", str(tmp_path)])
    assert result.exit_code == 2, result.stdout
    assert "could not parse version" in result.stdout


def test_parse_copilot_version_helper():
    """Unit coverage for the version-parsing helper itself."""
    from aidor.cli import _parse_copilot_version

    assert _parse_copilot_version("copilot 1.0.35") == (1, 0, 35)
    assert _parse_copilot_version("1.0.35-2") == (1, 0, 35)
    assert _parse_copilot_version("version: 2.14.0 (rc1)") == (2, 14, 0)
    assert _parse_copilot_version("no version here") is None
    assert _parse_copilot_version("") is None
