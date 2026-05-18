"""Lock-in tests for the test-placement rule (GitHub issue #2).

The coder previously dumped regression tests into round-numbered bucket
files like ``tests/test_review_0001_regressions.py``. Three artefacts now
explicitly forbid that pattern:

* ``src/aidor/agent_templates/aidor-coder.md`` — placement rule + ban.
* ``src/aidor/agent_templates/aidor-reviewer.md`` — review criterion.
* ``src/aidor/orchestrator.py:FIX_PROMPT`` — per-round reinforcement.
* ``src/aidor/resources/aidor_runtime_agents.md`` — runtime AGENTS.md baseline.

These tests assert the rule is present in every artefact so a future
edit cannot silently regress the contract.
"""

from __future__ import annotations

from importlib import resources

from aidor.orchestrator import FIX_PROMPT


def _read_template(name: str) -> str:
    pkg_ref = resources.files("aidor.agent_templates")
    return (pkg_ref / name).read_text(encoding="utf-8")


def _read_resource(name: str) -> str:
    pkg_ref = resources.files("aidor.resources")
    return (pkg_ref / name).read_text(encoding="utf-8")


# ---- Coder template ------------------------------------------------------


def test_coder_template_forbids_review_numbered_test_files():
    text = _read_template("aidor-coder.md")
    assert "test_review_" in text
    assert "test_round_" in text
    assert "test_fixes_" in text
    # Must mention the canonical placement: per-module test files.
    assert "tests/test_phase.py" in text or "tests/test_<module>.py" in text


def test_coder_template_says_place_in_existing_file_for_the_module():
    text = _read_template("aidor-coder.md")
    # The rule is placement, not just "don't bucket". Check the affirmative
    # half of the rule is present.
    assert "Place each new test in the file" in text or (
        "place each new test in the file" in text.lower()
    )


# ---- Reviewer template ---------------------------------------------------


def test_reviewer_template_lists_test_organisation_as_a_review_criterion():
    text = _read_template("aidor-reviewer.md")
    assert "Test organisation" in text
    assert "test_review_" in text
    # Must instruct the reviewer to flag, not just observe.
    assert "structural defect" in text
    # Affirmative half: the reviewer must tell the coder where the tests
    # SHOULD go, not just that they're misplaced. A future edit that
    # removes the placement guidance while keeping the ban would weaken
    # the contract — lock both halves down.
    assert "redistribute" in text
    assert "per-feature files" in text
    assert "tests/test_phase.py" in text or "tests/test_cli.py" in text


# ---- FIX_PROMPT ---------------------------------------------------------


def test_fix_prompt_includes_placement_reinforcement():
    """Belt-and-suspenders: the per-round prompt repeats the rule so the
    coder sees it fresh on every round, not just the once-per-session
    template."""
    assert "test_review_NNNN" in FIX_PROMPT or "test_review_" in FIX_PROMPT
    assert "tests/test_phase.py" in FIX_PROMPT
    assert "structural defect" in FIX_PROMPT


# ---- Runtime AGENTS.md ---------------------------------------------------


def test_runtime_agents_contract_carries_test_placement_baseline():
    text = _read_resource("aidor_runtime_agents.md")
    assert "test_review_NNNN" in text or "test_review_" in text
    assert "structural defect" in text
    # Affirmative half: the temporary runtime AGENTS.md is the contract every
    # orchestrated session reads; if the placement guidance disappears from here
    # while the ban remains, agents are told what NOT to do without
    # being told what to do. Lock both halves.
    assert "must be placed in the existing test file" in text
    assert "tests/test_<module>.py" in text
