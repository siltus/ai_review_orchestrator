"""Tests for hook_resolver helpers — ask_user payload unwrapping and the
preToolUse string-args normalization.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from aidor.hook_resolver import (
    _extract_path_tokens,
    _extract_question,
    _glob_match,
    _lookup_lint_exception,
    _unwrap_ask_user_args,
)


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


# ---- lint-exception scope enforcement ------------------------------------

_ALLOWED_EXCEPTIONS_YML = textwrap.dedent(
    """\
    exceptions:
      - rule: B011
        linter: ruff
        path_glob: "tests/**/*"
        reason: bare assert is idiomatic in pytest
      - rule: B008
        linter: ruff
        path_glob: "src/aidor/cli.py"
        reason: typer requires Option/Argument defaults
      - rule: E501
        linter: ruff
        path_glob: "**/*"
        reason: long literals
    """
)


def _prep_repo(tmp_path: Path) -> Path:
    (tmp_path / ".aidor").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aidor" / "allowed_exceptions.yml").write_text(
        _ALLOWED_EXCEPTIONS_YML, encoding="utf-8"
    )
    return tmp_path


def test_lint_exception_approves_b011_in_tests(tmp_path: Path):
    repo = _prep_repo(tmp_path)
    ans = _lookup_lint_exception(
        repo,
        "May I suppress ruff rule B011 in tests/test_feature.py for a bare assert?",
    )
    assert ans is not None
    assert "approved" in ans.lower()


def test_lint_exception_rejects_b011_outside_tests(tmp_path: Path):
    """Regression: approving an allowlisted rule outside its `path_glob` is
    a policy bypass (review-0002). Must escalate instead.
    """
    repo = _prep_repo(tmp_path)
    ans = _lookup_lint_exception(
        repo,
        "Please disable ruff rule B011 in src/aidor/cli.py for a bare assert.",
    )
    assert ans is None


def test_lint_exception_rejects_when_no_path_in_question(tmp_path: Path):
    """If the entry has a `path_glob` but the question doesn't reference a
    path at all, we cannot verify scope — deny and escalate.
    """
    repo = _prep_repo(tmp_path)
    ans = _lookup_lint_exception(
        repo, "Please disable ruff rule B011 somewhere in the code."
    )
    assert ans is None


def test_lint_exception_rejects_unknown_linter(tmp_path: Path):
    repo = _prep_repo(tmp_path)
    ans = _lookup_lint_exception(
        repo,
        "May I suppress eslint rule B011 in tests/test_feature.py?",
    )
    # linter mismatch (exception says 'ruff', question mentions 'eslint' and
    # no mention of 'ruff'): must not auto-approve.
    assert ans is None


def test_lint_exception_approves_b008_only_for_cli(tmp_path: Path):
    repo = _prep_repo(tmp_path)
    ok = _lookup_lint_exception(
        repo, "Allow ruff B008 in src/aidor/cli.py for typer defaults"
    )
    no = _lookup_lint_exception(
        repo, "Allow ruff B008 in src/aidor/phase.py for typer defaults"
    )
    assert ok is not None
    assert no is None


def test_lint_exception_e501_matches_anywhere(tmp_path: Path):
    repo = _prep_repo(tmp_path)
    for target in ("src/aidor/cli.py", "tests/test_x.py", "README.md"):
        ans = _lookup_lint_exception(
            repo, f"ruff noqa E501 for a long URL in {target}"
        )
        assert ans is not None, target


def test_lint_exception_rejects_when_no_allowlist(tmp_path: Path):
    ans = _lookup_lint_exception(
        tmp_path, "disable ruff rule B011 in tests/test_x.py"
    )
    assert ans is None


# ---- helpers -------------------------------------------------------------


def test_glob_match_star_star():
    assert _glob_match("tests/test_x.py", "tests/**/*")
    assert _glob_match("tests/sub/test_x.py", "tests/**/*")
    assert not _glob_match("src/aidor/cli.py", "tests/**/*")
    assert _glob_match("src/aidor/cli.py", "**/*")
    assert _glob_match("cli.py", "**/*")
    assert _glob_match("src/aidor/cli.py", "src/aidor/cli.py")


def test_extract_path_tokens_catches_slashed_and_bare_paths():
    toks = _extract_path_tokens("please fix src/aidor/cli.py and README.md now")
    assert "src/aidor/cli.py" in toks
    assert "README.md" in toks


def test_extract_path_tokens_normalises_backslashes():
    toks = _extract_path_tokens(r"update src\aidor\cli.py please")
    assert "src/aidor/cli.py" in toks
