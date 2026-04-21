"""Regression tests for the `PhaseRunner` watchdog.

These tests cover:

1. Pause-aware round timeout math (original bug: while a hook was waiting
   on a human, the watchdog reset `last_activity` but did NOT shift the
   round-timeout baseline, so long human waits tripped `round_timeout`
   immediately on resume).

2. The `.aidor/ABORT` marker integration: when the orchestrator (or the
   `aidor abort` CLI command) writes `.aidor/ABORT`, a running
   `PhaseRunner` must terminate its subprocess promptly with
   `stop_reason == "aborted"`.

Both (1) and (2) now exercise the real `PhaseRunner` against the
`fake_copilot` test binary so a regression in `phase.py` is caught.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from aidor.phase import PhaseRunner


def test_round_timeout_excludes_paused_seconds():
    """Original bug's arithmetic-level regression (kept for documentation)."""

    def effective(now_minus_start: float, paused_total: float) -> float:
        return now_minus_start - paused_total

    assert effective(200.0, 180.0) <= 60.0
    assert effective(250.0, 100.0) > 60.0
    assert effective(75.0, 0.0) == 75.0


def test_phase_runner_aborts_on_abort_marker(run_config, tmp_path):
    """Integration: writing `.aidor/ABORT` while a phase runs must cause
    the watchdog to terminate the subprocess with stop_reason="aborted".

    Uses the `fake_copilot` binary configured to delay long enough for the
    watchdog's 1-second poll to observe the marker.
    """
    # Make fake_copilot sit around long enough for the watchdog to act.
    os.environ["FAKE_COPILOT_DELAY_S"] = "10"
    os.environ.pop("FAKE_COPILOT_EMIT_FILE", None)
    try:
        artifact = tmp_path / "artifact.md"
        runner = PhaseRunner(
            config=run_config,
            role="reviewer",
            agent_name="aidor-reviewer",
            prompt="test",
            phase_index=1,
            artifact_path=artifact,
        )

        async def _drive() -> None:
            # Drop the ABORT marker shortly after the subprocess starts so
            # the watchdog's 1-second poll picks it up.
            async def _delayed_abort() -> None:
                await asyncio.sleep(1.5)
                run_config.aidor_dir.mkdir(parents=True, exist_ok=True)
                (run_config.aidor_dir / "ABORT").write_text("x", encoding="utf-8")

            abort_task = asyncio.create_task(_delayed_abort())
            try:
                result = await asyncio.wait_for(runner.run(), timeout=20)
            finally:
                abort_task.cancel()
                try:
                    await abort_task
                except (asyncio.CancelledError, Exception):
                    pass
            assert result.stop_reason == "aborted", (
                f"expected abort, got {result.stop_reason!r}"
            )

        asyncio.run(_drive())
    finally:
        os.environ.pop("FAKE_COPILOT_DELAY_S", None)


@pytest.mark.parametrize(
    "simulate_pause, expected_timeout",
    [
        (False, True),  # no pause -> should trip round_timeout
        (True, False),  # pause covers most of the run -> must NOT trip
    ],
)
def test_phase_runner_round_timeout_pause_is_effective(
    run_config, tmp_path, simulate_pause, expected_timeout
):
    """Integration: verify the pause-aware round-timeout math in the real
    PhaseRunner by simulating pause -> resume -> continued runtime before
    process exit.

    Timeline (simulate_pause=True):
      t=0.0s  fake_copilot starts (FAKE_COPILOT_DELAY_S=7)
      t=0.0s  pending/fake.json planted BEFORE the run -> watchdog paused
      t=4.0s  pending/fake.json removed by the background task -> resume
      t=4..7s fake_copilot continues running; watchdog unpaused
      t=7.0s  fake_copilot exits normally with stopReason=end_turn

    With round_timeout_s=3 and paused_total≈4s, effective_elapsed at exit
    is ~3s (at the boundary, `> round_timeout_s` is False). A broken
    implementation that does NOT subtract `paused_total` would observe
    effective_elapsed=~5s on the first unpaused tick after t=4s and trip
    ``timeout`` — which is exactly the regression this test guards.
    """
    run_config.round_timeout_s = 3
    run_config.idle_timeout_s = 60
    os.environ["FAKE_COPILOT_DELAY_S"] = "7"
    os.environ.pop("FAKE_COPILOT_EMIT_FILE", None)
    pending = run_config.aidor_dir / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    # Remove any leftover pending files from prior tests.
    for p in pending.glob("*.json"):
        p.unlink()
    try:
        artifact = tmp_path / "artifact.md"
        runner = PhaseRunner(
            config=run_config,
            role="reviewer",
            agent_name="aidor-reviewer",
            prompt="test",
            phase_index=2,
            artifact_path=artifact,
        )

        pending_req = pending / "fake.json"
        if simulate_pause:
            pending_req.write_text("{}", encoding="utf-8")

        async def _drive():
            async def _unpause_after(delay: float) -> None:
                await asyncio.sleep(delay)
                try:
                    pending_req.unlink()
                except FileNotFoundError:
                    pass

            unpause_task: asyncio.Task | None = None
            if simulate_pause:
                unpause_task = asyncio.create_task(_unpause_after(4.0))
            try:
                return await asyncio.wait_for(runner.run(), timeout=30)
            finally:
                if unpause_task is not None:
                    unpause_task.cancel()
                    try:
                        await unpause_task
                    except (asyncio.CancelledError, Exception):
                        pass

        result = asyncio.run(_drive())
        if expected_timeout:
            assert result.stop_reason == "timeout", (
                f"expected timeout, got {result.stop_reason!r}"
            )
        else:
            assert result.stop_reason != "timeout", (
                "watchdog tripped round_timeout despite the pause accounting "
                f"(got {result.stop_reason!r})"
            )
            assert result.stop_reason == "end_turn", (
                f"expected clean exit after resume, got {result.stop_reason!r}"
            )
    finally:
        os.environ.pop("FAKE_COPILOT_DELAY_S", None)
        for p in pending.glob("*.json"):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
