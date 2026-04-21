"""Tests for State (de)serialisation + atomic save."""

from __future__ import annotations

from aidor.state import PhaseRecord, RestartRecord, State


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
    assert not any(
        p.name.startswith("state.") and p.suffix == ".json" and p != path
        for p in tmp_path.iterdir()
    )


def test_load_rejects_non_object_top_level(tmp_path):
    """Regression (review-0008): `State.load` must validate the on-disk
    schema rather than blindly trust a hand-edited / corrupted state.json.
    A non-object top-level payload must raise a clear `ValueError`, not an
    obscure attribute/key error deep in `_from_plain`."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        State.load(path)


def test_load_rejects_malformed_round_entries(tmp_path):
    """Regression (review-0008): a round entry that is not an object (or
    lacks `index`) must raise a clear `ValueError` instead of a `KeyError`
    on `r["index"]`."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text('{"rounds": ["not-an-object"]}', encoding="utf-8")
    with pytest.raises(ValueError, match="round entries"):
        State.load(path)


def test_load_rejects_malformed_phases_list(tmp_path):
    """Regression (review-0009): nested `phases` must be validated, not
    blindly trusted. A non-list `phases` must surface as a clear
    ValueError rather than an `AttributeError` on `.get`."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text(
        '{"rounds": [{"index": 1, "phases": "oops"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'phases' must be a list"):
        State.load(path)


def test_load_rejects_non_object_phase_entry(tmp_path):
    """Regression (review-0009): each phase entry must be a dict; a bare
    string here used to crash with a TypeError inside ``PhaseRecord(**p)``."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text(
        '{"rounds": [{"index": 1, "phases": ["not-a-dict"]}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="phase entries must be objects"):
        State.load(path)


def test_load_rejects_malformed_restart_entries(tmp_path):
    """Regression (review-0009): nested `restarts` must be validated.
    A non-dict restart entry used to surface as `TypeError` deep in
    deserialisation."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text(
        '{"rounds": [{"index": 1, "phases": ['
        '{"name": "review", "role": "reviewer", "restarts": ["bad"]}'
        "]}]}",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="restart entries must be objects"):
        State.load(path)


def test_load_rejects_non_json(tmp_path):
    """Regression (review-0009): a corrupted state.json (not valid JSON)
    must raise a clear ValueError so the CLI can render a friendly error
    instead of a JSONDecodeError traceback."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        State.load(path)


def test_load_rejects_non_string_artifact_path(tmp_path):
    """Regression (review-0012): a persisted phase whose ``artifact_path``
    is not a string must be rejected at load time. Otherwise the resume
    path crashes with a raw ``TypeError`` from ``Path(123)`` deep in the
    orchestrator instead of failing cleanly at the disk boundary."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text(
        '{"rounds": [{"index": 1, "phases": ['
        '{"name": "review", "role": "reviewer", "status": "done", '
        '"artifact_path": 123}'
        "]}]}",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'artifact_path' must be a string or null"):
        State.load(path)


def test_load_rejects_list_artifact_path(tmp_path):
    """Regression (review-0012): a persisted phase whose ``artifact_path``
    is a list must be rejected at load time."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text(
        '{"rounds": [{"index": 1, "phases": ['
        '{"name": "readiness_gate", "role": "reviewer", "status": "done", '
        '"artifact_path": []}'
        "]}]}",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'artifact_path' must be a string or null"):
        State.load(path)


def test_load_rejects_bool_artifact_path(tmp_path):
    """Regression (review-0012): a persisted phase whose ``artifact_path``
    is a JSON boolean must be rejected at load time. Booleans are an
    ``int`` subclass in Python — explicitly excluding them prevents a
    ``Path(True)`` crash on resume."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text(
        '{"rounds": [{"index": 1, "phases": ['
        '{"name": "review", "role": "reviewer", "status": "done", '
        '"artifact_path": true}'
        "]}]}",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'artifact_path' must be a string or null"):
        State.load(path)


def test_load_rejects_non_string_transcript_path(tmp_path):
    """Regression (review-0012): scalar fields like ``transcript_path``
    must be type-checked at load time, not blindly trusted."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text(
        '{"rounds": [{"index": 1, "phases": ['
        '{"name": "review", "role": "reviewer", "status": "done", '
        '"transcript_path": 7}'
        "]}]}",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'transcript_path' must be a string or null"):
        State.load(path)


def test_load_rejects_non_string_status(tmp_path):
    """Regression (review-0012): a persisted phase ``status`` of the
    wrong type must be rejected, not propagated into orchestrator
    branches that compare it to known string values."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text(
        '{"rounds": [{"index": 1, "phases": ['
        '{"name": "review", "role": "reviewer", "status": 1}'
        "]}]}",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'status' must be a string"):
        State.load(path)


def test_load_rejects_unknown_status_enum(tmp_path):
    """Regression (review-0012): an out-of-vocabulary phase ``status``
    string (e.g. typo) must be rejected at load time."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text(
        '{"rounds": [{"index": 1, "phases": ['
        '{"name": "review", "role": "reviewer", "status": "DONE!"}'
        "]}]}",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'status' must be one of"):
        State.load(path)


def test_load_rejects_non_int_tokens(tmp_path):
    """Regression (review-0012): numeric fields persisted as strings
    must be rejected so cost/token aggregation cannot blow up later."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text(
        '{"rounds": [{"index": 1, "phases": ['
        '{"name": "review", "role": "reviewer", "status": "done", '
        '"tokens_in": "100"}'
        "]}]}",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'tokens_in' must be an integer"):
        State.load(path)


def test_load_rejects_non_string_stop_reason(tmp_path):
    """Regression (review-0012): readiness-gate resume reads
    ``stop_reason`` as a string; a wrong-type persisted value must
    fail at load time."""
    import pytest

    path = tmp_path / "state.json"
    path.write_text(
        '{"rounds": [{"index": 1, "phases": ['
        '{"name": "readiness_gate", "role": "reviewer", "status": "done", '
        '"stop_reason": 0}'
        "]}]}",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'stop_reason' must be a string or null"):
        State.load(path)
