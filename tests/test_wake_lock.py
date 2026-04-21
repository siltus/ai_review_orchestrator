"""Tests for the cross-platform wake-lock context manager."""

from __future__ import annotations

import sys

import pytest

from aidor.wake_lock import WakeLock


def test_wake_lock_disabled_is_a_noop():
    """When enabled=False, WakeLock must not touch the OS at all."""
    with WakeLock(enabled=False) as lock:
        assert lock.enabled is False
    # No exception, nothing acquired.
    assert lock._windows_prev is None
    assert lock._linux_proc is None


def test_wake_lock_unsupported_platform_does_not_raise(monkeypatch: pytest.MonkeyPatch):
    """On a fictitious platform the context manager must still be safe."""
    monkeypatch.setattr(sys, "platform", "haiku")
    with WakeLock(enabled=True) as lock:
        assert lock._windows_prev is None
        assert lock._linux_proc is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_wake_lock_windows_acquires_and_releases():
    """Smoke test: Windows path must successfully call SetThreadExecutionState."""
    with WakeLock(enabled=True) as lock:
        # SetThreadExecutionState returns the previous flags; should be a non-None int.
        assert lock._windows_prev is not None
