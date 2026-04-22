"""Targeted coverage tests to keep the suite at the AGENTS.md ≥90% floor.

Covers wake_lock no-op + Linux/macOS branches, review_store list/latest
helpers, and a handful of state.py validation branches that the main
unit tests do not exercise directly.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from aidor import wake_lock
from aidor.review_store import ReviewFooter, ReviewStore
from aidor.state import State, _from_plain

# ---- wake_lock ------------------------------------------------------------


def test_wakelock_disabled_is_no_op():
    """`enabled=False` must skip every platform branch and never crash."""
    with wake_lock.WakeLock(enabled=False) as wl:
        assert wl.enabled is False
    # Exit path also no-ops.


def test_wakelock_unsupported_platform(monkeypatch: pytest.MonkeyPatch):
    """An unknown sys.platform falls back to a logged no-op."""
    monkeypatch.setattr(wake_lock.sys, "platform", "haiku")
    with wake_lock.WakeLock(enabled=True):
        pass


def test_wakelock_linux_without_systemd_inhibit(monkeypatch: pytest.MonkeyPatch):
    """On Linux without systemd-inhibit, acquisition is a no-op (logged)."""
    monkeypatch.setattr(wake_lock.sys, "platform", "linux")
    monkeypatch.setattr(wake_lock.shutil, "which", lambda _: None)
    with wake_lock.WakeLock(enabled=True) as wl:
        assert wl._linux_proc is None


def test_wakelock_linux_acquire_invokes_subprocess(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(wake_lock.sys, "platform", "linux")
    monkeypatch.setattr(wake_lock.shutil, "which", lambda _: "/usr/bin/systemd-inhibit")
    fake_proc = mock.MagicMock()
    fake_proc.pid = 12345
    monkeypatch.setattr(wake_lock.subprocess, "Popen", lambda *a, **k: fake_proc)
    with wake_lock.WakeLock(enabled=True):
        pass
    fake_proc.terminate.assert_called_once()


def test_wakelock_linux_release_kills_on_terminate_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(wake_lock.sys, "platform", "linux")
    monkeypatch.setattr(wake_lock.shutil, "which", lambda _: "/usr/bin/systemd-inhibit")
    fake_proc = mock.MagicMock()
    fake_proc.terminate.side_effect = RuntimeError("boom")
    monkeypatch.setattr(wake_lock.subprocess, "Popen", lambda *a, **k: fake_proc)
    with wake_lock.WakeLock(enabled=True):
        pass
    fake_proc.kill.assert_called_once()


def test_wakelock_release_without_acquire_is_safe():
    """Calling _release_subprocess_lock with no proc must not raise."""
    wl = wake_lock.WakeLock(enabled=True)
    wl._release_subprocess_lock()  # _linux_proc is None


# ---- review_store --------------------------------------------------------


def _store(tmp_path: Path) -> ReviewStore:
    s = ReviewStore(reviews_dir=tmp_path / "reviews", fixes_dir=tmp_path / "fixes")
    s.ensure_dirs()
    return s


def test_list_reviews_empty(tmp_path: Path):
    s = ReviewStore(reviews_dir=tmp_path / "r", fixes_dir=tmp_path / "f")
    assert s.list_reviews() == []
    assert s.latest_review() is None
    assert s.list_fixes() == []
    assert s.latest_fix() is None


def test_next_review_and_fix_paths_increment(tmp_path: Path):
    s = _store(tmp_path)
    p1 = s.next_review_path(timestamp=datetime(2026, 4, 22, 0, 0, 0))
    p1.write_text("# review\n", encoding="utf-8")
    p2 = s.next_review_path(timestamp=datetime(2026, 4, 22, 0, 1, 0))
    assert "0001" in p1.name
    assert "0002" in p2.name

    f1 = s.next_fix_path(timestamp=datetime(2026, 4, 22, 0, 0, 0))
    f1.write_text("# fix\n", encoding="utf-8")
    assert "0001" in f1.name


def test_review_footer_clean_and_ready():
    f = ReviewFooter(
        status="CLEAN",
        issues={"critical": 0, "major": 0, "minor": 0, "nit": 0},
        production_ready=True,
    )
    assert f.is_clean_and_ready
    assert f.critical == 0
    assert f.major == 0
    assert f.minor == 0
    assert f.nit == 0
    assert f.to_dict()["status"] == "CLEAN"


def test_review_footer_not_ready_when_critical():
    f = ReviewFooter(
        status="CLEAN",
        issues={"critical": 1, "major": 0},
        production_ready=True,
    )
    assert not f.is_clean_and_ready
    assert f.critical == 1


# ---- state.py validation branches ---------------------------------------


def test_state_load_rejects_string_current_round(tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "current_round": "1"}), encoding="utf-8")
    with pytest.raises(ValueError, match="current_round"):
        State.load(p)


def test_state_load_rejects_unknown_status(tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "status": "wat"}), encoding="utf-8")
    with pytest.raises(ValueError, match="status"):
        State.load(p)


def test_state_load_rejects_negative_current_round(tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "current_round": -1}), encoding="utf-8")
    with pytest.raises(ValueError, match="non-negative"):
        State.load(p)


def test_state_load_rejects_invalid_phase_name(tmp_path: Path):
    payload = {
        "version": 1,
        "rounds": [
            {
                "index": 1,
                "phases": [
                    {"name": "bogus", "role": "coder", "status": "done"},
                ],
            }
        ],
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="name"):
        State.load(p)


def test_state_save_and_load_roundtrip(tmp_path: Path):
    s = State()
    s.start_round()
    s.save(tmp_path / "state.json")
    loaded = State.load(tmp_path / "state.json")
    assert loaded.current_round == 1
    assert len(loaded.rounds) == 1


def test_state_load_rejects_invalid_json(tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        State.load(p)


def test_state_current_round_record_returns_none_when_zero():
    s = State()
    assert s.current_round_record() is None


def test_state_from_plain_handles_empty_dict():
    s = _from_plain({})
    assert isinstance(s, State)
    assert s.current_round == 0
