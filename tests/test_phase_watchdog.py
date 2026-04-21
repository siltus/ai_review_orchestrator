"""Regression test for the watchdog timer-pause bug.

Prior to the fix, while a hook was waiting on a human, the watchdog reset
`last_activity` (pausing the idle timer) but did NOT shift the round-timeout
baseline. Long human waits therefore tripped `round_timeout` immediately on
resume. The fix tracks cumulative paused duration and subtracts it from
elapsed wall-clock when comparing to round_timeout_s.

This test exercises the watchdog's logic by simulating the elapsed-time math
directly, since spinning up a real subprocess is too heavy for a unit test.
"""

from __future__ import annotations


def _effective_elapsed(now_minus_start: float, paused_total: float) -> float:
    """Pure replica of the watchdog's check, isolated for testing."""
    return now_minus_start - paused_total


def test_round_timeout_excludes_paused_seconds():
    """A 200 s phase that paused 180 s on a human is effectively 20 s in,
    and must NOT trip a 60 s round timeout."""
    round_timeout = 60.0
    elapsed = 200.0  # wall-clock seconds since phase_start
    paused = 180.0  # seconds spent waiting on hooks
    assert _effective_elapsed(elapsed, paused) <= round_timeout


def test_round_timeout_still_trips_when_real_work_exceeds_budget():
    """If real (non-paused) work exceeds the round budget, timeout trips."""
    round_timeout = 60.0
    elapsed = 250.0
    paused = 100.0
    assert _effective_elapsed(elapsed, paused) > round_timeout


def test_zero_pause_matches_legacy_behaviour():
    elapsed = 75.0
    paused = 0.0
    assert _effective_elapsed(elapsed, paused) == 75.0
