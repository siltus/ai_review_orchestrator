"""Tests for the human-prompt path in the orchestrator: the watcher reads a
pending request and our prompt routine writes either an .answer or .cancel."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aidor.config import RunConfig
from aidor.orchestrator import Orchestrator


@pytest.fixture
def orchestrator(tmp_path: Path):
    repo = tmp_path
    (repo / ".aidor" / "pending").mkdir(parents=True)
    cfg = RunConfig(repo=repo, coder_model="m", reviewer_model="m")
    return Orchestrator(cfg)


def _write_request(orchestrator: Orchestrator, body: dict) -> Path:
    path = orchestrator.config.aidor_dir / "pending" / "abc123.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_prompt_writes_answer_when_user_replies(orchestrator, monkeypatch: pytest.MonkeyPatch):
    req = _write_request(
        orchestrator,
        {"question": "Continue?", "role": "coder", "classification": "unknown"},
    )

    monkeypatch.setattr("aidor.orchestrator.Prompt.ask", lambda *a, **kw: "yes, continue")
    asyncio.run(orchestrator._prompt_human(req))

    answer_path = req.with_suffix(".answer")
    assert answer_path.exists()
    body = json.loads(answer_path.read_text(encoding="utf-8"))
    assert body["answer"] == "yes, continue"
    assert "answered_at" in body


def test_prompt_writes_cancel_marker_on_keyboard_interrupt(
    orchestrator, monkeypatch: pytest.MonkeyPatch
):
    req = _write_request(
        orchestrator,
        {"question": "Continue?", "role": "coder", "classification": "unknown"},
    )

    def _raise(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr("aidor.orchestrator.Prompt.ask", _raise)
    asyncio.run(orchestrator._prompt_human(req))

    cancel_path = req.with_suffix(".cancel")
    answer_path = req.with_suffix(".answer")
    assert cancel_path.exists()
    assert not answer_path.exists()
    body = json.loads(cancel_path.read_text(encoding="utf-8"))
    assert "cancelled_at" in body


def test_prompt_skips_unreadable_request_safely(orchestrator):
    bogus = orchestrator.config.aidor_dir / "pending" / "missing.json"
    # No file exists — _prompt_human should log and return, not raise.
    asyncio.run(orchestrator._prompt_human(bogus))
