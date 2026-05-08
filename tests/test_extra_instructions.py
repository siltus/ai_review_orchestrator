"""Tests for the operator's `--instructions` family of flags.

Covers:

* ``RunConfig.instructions_for`` — composition of shared + per-role text,
  empty-string fallthrough, and the unknown-role guard.
* ``aidor.cli._resolve_instructions`` — inline / file / mutex / missing-file
  paths via the Typer CLI runner.
* The orchestrator prompt builders — the operator's text appears verbatim
  in the reviewer / coder prompts when set, and the prompts are unchanged
  (no leftover placeholder, no spurious header) when the flags are not set.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from aidor.cli import app
from aidor.config import RunConfig
from aidor.orchestrator import (
    FIX_PROMPT,
    READINESS_PROMPT,
    REVIEW_PROMPT_FOLLOWUP,
    REVIEW_PROMPT_INITIAL,
    Orchestrator,
    _format_extra_instructions,
)
from aidor.state import PhaseRecord, RoundRecord

runner = CliRunner()


# ---- RunConfig.instructions_for ------------------------------------------


def _make_cfg(**kw: object) -> RunConfig:
    """Construct a RunConfig with the required positional fields filled in."""
    base: dict[str, object] = {
        "repo": Path("."),
        "coder_model": "m",
        "reviewer_model": "m",
    }
    base.update(kw)
    return RunConfig(**base)  # type: ignore[arg-type]


def test_instructions_for_returns_empty_when_unset():
    cfg = _make_cfg()
    assert cfg.instructions_for("coder") == ""
    assert cfg.instructions_for("reviewer") == ""


def test_instructions_for_returns_shared_when_only_shared_set():
    cfg = _make_cfg(extra_instructions="be paranoid about security")
    assert cfg.instructions_for("coder") == "be paranoid about security"
    assert cfg.instructions_for("reviewer") == "be paranoid about security"


def test_instructions_for_returns_role_specific_when_only_role_set():
    cfg = _make_cfg(reviewer_extra_instructions="strict on API stability")
    assert cfg.instructions_for("reviewer") == "strict on API stability"
    assert cfg.instructions_for("coder") == ""


def test_instructions_for_composes_shared_and_role():
    cfg = _make_cfg(
        extra_instructions="cross-platform support is critical",
        coder_extra_instructions="prefer minimal patches",
    )
    out = cfg.instructions_for("coder")
    assert "cross-platform support is critical" in out
    assert "prefer minimal patches" in out
    # Shared MUST appear before role-specific so role text reads as
    # "in addition to the shared brief".
    assert out.index("cross-platform") < out.index("prefer minimal")
    # Reviewer only sees the shared text in this scenario.
    assert cfg.instructions_for("reviewer") == "cross-platform support is critical"


def test_instructions_for_strips_whitespace_only_inputs():
    cfg = _make_cfg(extra_instructions="   \n   ", coder_extra_instructions="")
    assert cfg.instructions_for("coder") == ""
    assert cfg.instructions_for("reviewer") == ""


def test_instructions_for_unknown_role_raises():
    cfg = _make_cfg(extra_instructions="x")
    with pytest.raises(ValueError, match="Unknown role"):
        cfg.instructions_for("orchestrator")


# ---- _format_extra_instructions ------------------------------------------


def test_format_extra_instructions_empty_yields_empty_string():
    assert _format_extra_instructions("") == ""
    assert _format_extra_instructions("   \n  \t  ") == ""


def test_format_extra_instructions_renders_labelled_block():
    out = _format_extra_instructions("be cross-platform")
    assert "Operator instructions" in out
    assert "be cross-platform" in out
    # The block starts with a newline so it doesn't run on the line that
    # ended the base template.
    assert out.startswith("\n")


# ---- CLI option parsing --------------------------------------------------


def _run_cli_dry(*extra_args: str, repo: Path) -> Result:
    """Invoke `aidor run --dry-run` with the given extra args, return result."""
    return runner.invoke(
        app,
        [
            "run",
            "--coder",
            "m",
            "--reviewer",
            "m",
            "--repo",
            str(repo),
            "--copilot-binary",
            "definitely-not-real-aidor-test",
            "--dry-run",
            *extra_args,
        ],
    )


def test_cli_accepts_inline_instructions(tmp_path: Path):
    result = _run_cli_dry("--instructions", "extra security focus", repo=tmp_path)
    assert result.exit_code == 0, result.stdout


def test_cli_accepts_instructions_file(tmp_path: Path):
    instr = tmp_path / "notes.md"
    instr.write_text("be especially thorough on threading", encoding="utf-8")
    result = _run_cli_dry("--instructions-file", str(instr), repo=tmp_path)
    assert result.exit_code == 0, result.stdout


def test_cli_rejects_inline_and_file_together(tmp_path: Path):
    instr = tmp_path / "notes.md"
    instr.write_text("hello", encoding="utf-8")
    result = _run_cli_dry(
        "--instructions",
        "inline text",
        "--instructions-file",
        str(instr),
        repo=tmp_path,
    )
    assert result.exit_code == 2, result.stdout
    assert "mutually exclusive" in result.stdout


def test_cli_rejects_missing_instructions_file(tmp_path: Path):
    missing = tmp_path / "does_not_exist.md"
    result = _run_cli_dry("--instructions-file", str(missing), repo=tmp_path)
    assert result.exit_code == 2, result.stdout
    assert "not a readable file" in result.stdout


def test_cli_per_role_inline_and_file_mutex(tmp_path: Path):
    """The per-role pairs enforce the same mutex as the shared pair."""
    instr = tmp_path / "r.md"
    instr.write_text("hi", encoding="utf-8")
    result = _run_cli_dry(
        "--reviewer-instructions",
        "x",
        "--reviewer-instructions-file",
        str(instr),
        repo=tmp_path,
    )
    assert result.exit_code == 2, result.stdout
    assert "mutually exclusive" in result.stdout

    result = _run_cli_dry(
        "--coder-instructions",
        "x",
        "--coder-instructions-file",
        str(instr),
        repo=tmp_path,
    )
    assert result.exit_code == 2, result.stdout


def test_cli_accepts_per_role_inline_options(tmp_path: Path):
    result = _run_cli_dry(
        "--instructions",
        "shared",
        "--reviewer-instructions",
        "reviewer-only",
        "--coder-instructions",
        "coder-only",
        repo=tmp_path,
    )
    assert result.exit_code == 0, result.stdout


# ---- Prompt injection ----------------------------------------------------


def _make_orchestrator(tmp_path: Path, **cfg_kw: object) -> Orchestrator:
    cfg = _make_cfg(repo=tmp_path, **cfg_kw)
    return Orchestrator(cfg)


def test_initial_review_prompt_includes_operator_instructions(tmp_path: Path):
    orch = _make_orchestrator(tmp_path, extra_instructions="security is paramount")
    prompt = orch._review_prompt(round_index=1, review_path=tmp_path / "r.md")
    assert "Operator instructions" in prompt
    assert "security is paramount" in prompt


def test_initial_review_prompt_omits_block_when_no_instructions(tmp_path: Path):
    orch = _make_orchestrator(tmp_path)
    prompt = orch._review_prompt(round_index=1, review_path=tmp_path / "r.md")
    assert "Operator instructions" not in prompt
    assert "{extra_instructions_block}" not in prompt


def test_followup_review_prompt_includes_reviewer_specific_instructions(tmp_path: Path):
    orch = _make_orchestrator(
        tmp_path,
        extra_instructions="shared brief",
        reviewer_extra_instructions="be especially strict on docs",
    )
    # Need a previous round record so the followup template can resolve
    # prev_review / prev_fixes.
    prev = RoundRecord(index=1)
    prev.phases.append(
        PhaseRecord(
            name="review",
            role="reviewer",
            status="done",
            artifact_path=str(tmp_path / "review-prev.md"),
        )
    )
    prev.phases.append(
        PhaseRecord(
            name="fix",
            role="coder",
            status="done",
            artifact_path=str(tmp_path / "fixes-prev.md"),
        )
    )
    orch.state.rounds.append(prev)

    prompt = orch._review_prompt(round_index=2, review_path=tmp_path / "r2.md")
    assert "shared brief" in prompt
    assert "be especially strict on docs" in prompt
    # Shared appears before per-role.
    assert prompt.index("shared brief") < prompt.index("be especially strict")
    # The coder-only override (none here) must NOT leak into the reviewer
    # prompt — guard against a future regression that swaps the lookup.


def test_followup_review_prompt_excludes_coder_only_instructions(tmp_path: Path):
    orch = _make_orchestrator(
        tmp_path,
        coder_extra_instructions="prefer minimal patches",
    )
    prev = RoundRecord(index=1)
    prev.phases.append(
        PhaseRecord(name="review", role="reviewer", status="done", artifact_path="x")
    )
    orch.state.rounds.append(prev)
    prompt = orch._review_prompt(round_index=2, review_path=tmp_path / "r.md")
    assert "prefer minimal patches" not in prompt


def test_static_prompt_templates_carry_extra_block_placeholder():
    """The four templates the orchestrator formats must all carry the
    `{extra_instructions_block}` placeholder. A regression that drops it
    from any template would silently cause `.format` to leave the literal
    placeholder in the prompt (or raise, depending on the call-site)."""
    for tmpl in (
        REVIEW_PROMPT_INITIAL,
        REVIEW_PROMPT_FOLLOWUP,
        FIX_PROMPT,
        READINESS_PROMPT,
    ):
        assert "{extra_instructions_block}" in tmpl
