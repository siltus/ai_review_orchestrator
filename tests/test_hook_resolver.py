"""Tests for hook_resolver helpers — ask_user payload unwrapping and the
preToolUse string-args normalization.
"""

from __future__ import annotations

import json

from aidor.hook_resolver import _extract_question, _unwrap_ask_user_args


def test_extract_question_plain_args():
    q = _extract_question({"question": "Should I proceed?"})
    assert q == "Should I proceed?"


def test_extract_question_with_choices():
    q = _extract_question({"question": "Pick one", "choices": ["yes", "no", "maybe"]})
    assert "Pick one" in q
    assert "1. yes" in q
    assert "2. no" in q


def test_extract_question_unwraps_command_envelope():
    """Copilot CLI sometimes wraps ask_user as {"command": "<json>"}."""
    inner = {
        "question": "Enable PowerShell?",
        "choices": ["Yes", "No"],
        "allow_freeform": True,
    }
    args = {"command": json.dumps(inner)}
    q = _extract_question(args)
    assert "Enable PowerShell?" in q
    assert "1. Yes" in q
    assert "{" not in q.splitlines()[0]  # no raw JSON in the leading line


def test_extract_question_unwraps_doubly_nested_command_envelope():
    """Defensive: handle the actual two-level wrapping seen in dogfood
    (command -> {command: {question,...}}).
    """
    inner = {"question": "Approve?", "choices": ["a", "b"]}
    middle = {"command": json.dumps(inner)}
    outer = {"command": json.dumps(middle)}
    q = _extract_question(outer)
    assert "Approve?" in q
    assert "1. a" in q


def test_unwrap_returns_original_dict_when_not_wrapped():
    args = {"foo": "bar"}
    assert _unwrap_ask_user_args(args) == args


def test_unwrap_returns_original_when_command_is_not_json():
    args = {"command": "echo hello"}
    assert _unwrap_ask_user_args(args) == args
