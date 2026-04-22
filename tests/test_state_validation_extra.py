"""Boundary-validation tests for `state.py` to keep coverage at the
≥90% floor required by AGENTS.md. These exercise the JSON-shape error
paths in `_from_plain` and `_validate_phase_scalars`, plus the artifact
containment helper.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aidor.state import State, validate_artifact_paths_within_repo


def _load_with(payload: dict) -> State:
    return State.from_json(json.dumps(payload))


def test_phase_numeric_field_must_be_number():
    with pytest.raises(ValueError, match="tokens_in"):
        _load_with(
            {
                "rounds": [
                    {
                        "index": 1,
                        "phases": [{"name": "review", "role": "reviewer", "tokens_in": "x"}],
                    }
                ]
            }
        )


def test_phase_optional_numeric_field_rejects_bool():
    with pytest.raises(ValueError, match="duration_s"):
        _load_with(
            {
                "rounds": [
                    {
                        "index": 1,
                        "phases": [{"name": "review", "role": "reviewer", "duration_s": True}],
                    }
                ]
            }
        )


def test_round_phases_must_be_list():
    with pytest.raises(ValueError, match="phases"):
        _load_with({"rounds": [{"index": 1, "phases": "not-a-list"}]})


def test_round_phase_entry_must_be_object():
    with pytest.raises(ValueError, match="phase entries must be objects"):
        _load_with({"rounds": [{"index": 1, "phases": ["not-a-dict"]}]})


def test_round_phase_restarts_must_be_list():
    with pytest.raises(ValueError, match="restarts"):
        _load_with(
            {
                "rounds": [
                    {
                        "index": 1,
                        "phases": [{"name": "review", "role": "reviewer", "restarts": "nope"}],
                    }
                ]
            }
        )


def test_round_restart_entry_must_be_object():
    with pytest.raises(ValueError, match="restart entries must be objects"):
        _load_with(
            {
                "rounds": [
                    {
                        "index": 1,
                        "phases": [{"name": "review", "role": "reviewer", "restarts": ["nope"]}],
                    }
                ]
            }
        )


def test_round_restart_entry_malformed():
    with pytest.raises(ValueError, match="malformed restart entry"):
        _load_with(
            {
                "rounds": [
                    {
                        "index": 1,
                        "phases": [
                            {
                                "name": "review",
                                "role": "reviewer",
                                "restarts": [{"unknown_field": 1}],
                            }
                        ],
                    }
                ]
            }
        )


def test_round_phase_malformed_entry():
    with pytest.raises(ValueError, match="malformed phase entry"):
        _load_with(
            {
                "rounds": [
                    {
                        "index": 1,
                        "phases": [{"name": "review", "role": "reviewer", "bogus_field": 1}],
                    }
                ]
            }
        )


def test_round_footer_must_be_object():
    with pytest.raises(ValueError, match="footer"):
        _load_with({"rounds": [{"index": 1, "phases": [], "footer": "nope"}]})


def test_notes_must_be_list():
    with pytest.raises(ValueError, match="notes"):
        _load_with({"notes": "not-a-list"})


def test_version_must_be_int():
    with pytest.raises(ValueError, match="version"):
        _load_with({"version": "1"})


def test_status_must_be_string():
    with pytest.raises(ValueError, match="status"):
        _load_with({"status": 123})


def test_started_at_must_be_string_or_null():
    with pytest.raises(ValueError, match="started_at"):
        _load_with({"started_at": 12})


def test_ended_at_must_be_string_or_null():
    with pytest.raises(ValueError, match="ended_at"):
        _load_with({"ended_at": 12})


def test_save_unlinks_temp_on_failure(tmp_path: Path, monkeypatch):
    """If `os.replace` fails, the temp file must be cleaned up and the
    original error propagated."""
    import os as _os

    state = State()
    target = tmp_path / "state.json"

    real_replace = _os.replace

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(_os, "replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        state.save(target)
    monkeypatch.setattr(_os, "replace", real_replace)
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("state.")]
    assert leftovers == [], f"temp files leaked: {leftovers}"


def test_artifact_path_outside_repo_is_rejected(tmp_path: Path):
    """Persisted absolute artifact paths must live inside the repo root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "evil.md"
    state = State.from_json(
        json.dumps(
            {
                "rounds": [
                    {
                        "index": 1,
                        "phases": [
                            {
                                "name": "review",
                                "role": "reviewer",
                                "artifact_path": str(outside),
                            }
                        ],
                    }
                ]
            }
        )
    )
    err = validate_artifact_paths_within_repo(state, repo)
    assert err is not None and "outside" in err


def test_artifact_path_inside_repo_is_accepted(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    inside = repo / ".aidor" / "reviews" / "review-0001.md"
    state = State.from_json(
        json.dumps(
            {
                "rounds": [
                    {
                        "index": 1,
                        "phases": [
                            {
                                "name": "review",
                                "role": "reviewer",
                                "artifact_path": str(inside),
                            }
                        ],
                    }
                ]
            }
        )
    )
    assert validate_artifact_paths_within_repo(state, repo) is None
