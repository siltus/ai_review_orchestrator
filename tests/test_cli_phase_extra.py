"""Coverage tests for `phase.py` argv/env builders and CLI dry-run / clean
prompt branches that the main suites do not exercise directly. Keeps the
project at the AGENTS.md ≥90% line-coverage floor.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from aidor.cli import app
from aidor.config import RunConfig
from aidor.phase import PhaseRunner, _deep_find, _utcnow

runner = CliRunner()


def _runner(tmp_path: Path) -> PhaseRunner:
    cfg = RunConfig(
        repo=tmp_path,
        coder_model="coder-model",
        reviewer_model="reviewer-model",
        max_rounds=1,
        idle_timeout_s=10,
        round_timeout_s=60,
        max_restarts_per_round=0,
        keep_awake=False,
        copilot_binary="copilot",
        allow_local_install=True,
    )
    return PhaseRunner(
        config=cfg,
        role="coder",
        agent_name="aidor-coder",
        prompt="hello",
        phase_index=1,
        artifact_path=tmp_path / "fix.md",
    )


def test_build_argv_contains_required_flags(tmp_path: Path):
    pr = _runner(tmp_path)
    argv = pr._build_argv(resume=False)
    assert "-p" in argv
    assert "--autopilot" in argv
    assert "--output-format=json" in argv
    assert "--allow-all-tools" in argv
    assert "--allow-all-paths" in argv
    assert "--continue" not in argv


def test_build_argv_includes_continue_on_resume(tmp_path: Path):
    pr = _runner(tmp_path)
    argv = pr._build_argv(resume=True)
    assert "--continue" in argv


def test_build_env_sets_aidor_vars(tmp_path: Path):
    pr = _runner(tmp_path)
    env = pr._build_env()
    assert env["AIDOR_REPO"] == str(tmp_path)
    assert env["AIDOR_ROLE"] == "coder"
    assert env["AIDOR_PHASE_INDEX"] == "1"
    assert env["AIDOR_ALLOW_LOCAL_INSTALL"] == "1"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_build_env_disables_local_install_when_off(tmp_path: Path):
    cfg = RunConfig(
        repo=tmp_path,
        coder_model="coder-model",
        reviewer_model="reviewer-model",
        keep_awake=False,
        copilot_binary="copilot",
        allow_local_install=False,
    )
    pr = PhaseRunner(
        config=cfg,
        role="reviewer",
        agent_name="aidor-reviewer",
        prompt="x",
        phase_index=2,
        artifact_path=tmp_path / "review.md",
    )
    env = pr._build_env()
    assert env["AIDOR_ALLOW_LOCAL_INSTALL"] == "0"
    assert env["AIDOR_ROLE"] == "reviewer"


def test_deep_find_finds_nested_value():
    obj = {"a": {"b": [{"stop_reason": "end_turn"}, {"x": 1}]}}
    assert _deep_find(obj, "stop_reason") == "end_turn"


def test_deep_find_returns_none_when_missing():
    assert _deep_find({"a": [1, {"b": 2}]}, "missing") is None


def test_deep_find_handles_scalars():
    assert _deep_find("scalar", "x") is None
    assert _deep_find(42, "x") is None


def test_utcnow_returns_iso_z_string():
    s = _utcnow()
    assert s.endswith("Z")
    assert len(s) == len("2026-04-21T23:29:04Z")


def test_is_hook_busy_no_pending_dir(tmp_path: Path):
    pr = _runner(tmp_path)
    assert pr._is_hook_busy() is False


def test_is_hook_busy_with_unanswered_request(tmp_path: Path):
    pr = _runner(tmp_path)
    pending = pr.config.aidor_dir / "pending"
    pending.mkdir(parents=True)
    (pending / "q1.json").write_text("{}", encoding="utf-8")
    assert pr._is_hook_busy() is True


def test_is_hook_busy_ignores_answered(tmp_path: Path):
    pr = _runner(tmp_path)
    pending = pr.config.aidor_dir / "pending"
    pending.mkdir(parents=True)
    (pending / "q1.json").write_text("{}", encoding="utf-8")
    (pending / "q1.answer").write_text("ok", encoding="utf-8")
    assert pr._is_hook_busy() is False


def test_is_hook_busy_ignores_cancelled(tmp_path: Path):
    pr = _runner(tmp_path)
    pending = pr.config.aidor_dir / "pending"
    pending.mkdir(parents=True)
    (pending / "q1.json").write_text("{}", encoding="utf-8")
    (pending / "q1.cancel").write_text("", encoding="utf-8")
    assert pr._is_hook_busy() is False


def test_terminate_no_op_when_already_exited(tmp_path: Path):
    import asyncio

    class _FakeProc:
        returncode = 0

    pr = _runner(tmp_path)
    asyncio.run(pr._terminate(_FakeProc()))


def test_terminate_calls_terminate_and_kill_when_force(tmp_path: Path):
    import asyncio

    calls: list[str] = []

    class _FakeProc:
        returncode = None

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

        def send_signal(self, sig):
            calls.append(f"signal:{sig}")

    pr = _runner(tmp_path)
    asyncio.run(pr._terminate(_FakeProc(), force=True))
    assert "kill" in calls
    assert any(c == "terminate" or c.startswith("signal:") for c in calls)


def test_terminate_swallows_process_lookup(tmp_path: Path):
    import asyncio

    class _FakeProc:
        returncode = None

        def terminate(self):
            raise ProcessLookupError

        def send_signal(self, sig):
            raise ProcessLookupError

    pr = _runner(tmp_path)
    asyncio.run(pr._terminate(_FakeProc()))


# ---- aidor run --dry-run ---------------------------------------------------


def test_run_dry_run_completes_and_writes_skeleton(tmp_path: Path):
    """`aidor run --dry-run` must execute bootstrap and exit 0 without
    spawning Copilot. This exercises the dry-run branch of `cli.run`."""
    (tmp_path / "AGENTS.md").write_text("# x\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(tmp_path),
            "--coder",
            "test-coder",
            "--reviewer",
            "test-reviewer",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / ".aidor").is_dir()


# ---- aidor clean confirm-no path ------------------------------------------


def test_clean_aborts_when_user_declines(tmp_path: Path):
    """`aidor clean` without `-y` must respect a `n` answer to the prompt."""
    (tmp_path / ".aidor").mkdir()
    (tmp_path / ".aidor" / "marker").write_text("keep me", encoding="utf-8")
    result = runner.invoke(app, ["clean", "--repo", str(tmp_path)], input="n\n")
    assert result.exit_code == 1
    assert (tmp_path / ".aidor" / "marker").exists()
