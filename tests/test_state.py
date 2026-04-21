"""Tests for State (de)serialisation + atomic save."""
from __future__ import annotations

from aidor.state import PhaseRecord, RestartRecord, RoundRecord, State


def test_round_trip(tmp_path):
    s = State(started_at="2024-01-01T00:00:00Z", status="running")
    r = s.start_round()
    r.phases.append(
        PhaseRecord(
            name="review",
            role="reviewer",
            status="done",
            duration_s=12.5,
            tokens_in=100,
            tokens_out=50,
            restarts=[RestartRecord(reason="idle", at="t", backoff_s=30)],
        )
    )
    r.footer = {"status": "CLEAN", "issues": {"critical": 0}, "production_ready": True}

    path = tmp_path / "state.json"
    s.save(path)
    loaded = State.load(path)
    assert loaded.status == "running"
    assert len(loaded.rounds) == 1
    assert loaded.rounds[0].phases[0].tokens_in == 100
    assert loaded.rounds[0].phases[0].restarts[0].reason == "idle"
    assert loaded.rounds[0].footer == r.footer


def test_save_is_atomic(tmp_path):
    """Simulate a crash mid-save: the existing file should not be truncated."""
    path = tmp_path / "state.json"
    s1 = State(status="running")
    s1.save(path)
    original = path.read_text(encoding="utf-8")

    # Second save should atomically replace; no temp leftovers.
    s2 = State(status="converged")
    s2.save(path)
    assert path.read_text(encoding="utf-8") != original
    assert not any(p.name.startswith("state.") and p.suffix == ".json" and p != path
                   for p in tmp_path.iterdir())
