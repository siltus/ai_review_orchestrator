"""Tests for review_store + footer parsing."""

from __future__ import annotations

import pytest

from aidor.review_store import FooterParseError, ReviewStore, parse_footer

CLEAN_AND_READY = """\
# Review

Looks good.

<!-- AIDOR:STATUS=CLEAN -->
<!-- AIDOR:ISSUES={"critical":0,"major":0,"minor":0,"nit":0} -->
<!-- AIDOR:PRODUCTION_READY=true -->
"""

CLEAN_NOT_READY = """\
# Review

<!-- AIDOR:STATUS=CLEAN -->
<!-- AIDOR:ISSUES={"critical":0,"major":0,"minor":0,"nit":1} -->
<!-- AIDOR:PRODUCTION_READY=false -->
"""

ISSUES = """\
# Review

<!-- AIDOR:STATUS=ISSUES_FOUND -->
<!-- AIDOR:ISSUES={"critical":1,"major":2,"minor":0,"nit":0} -->
<!-- AIDOR:PRODUCTION_READY=false -->
"""


def test_footer_clean_and_ready():
    f = parse_footer(CLEAN_AND_READY)
    assert f.status == "CLEAN"
    assert f.critical == 0 and f.major == 0
    assert f.production_ready is True
    assert f.is_clean_and_ready is True


def test_footer_clean_but_not_ready():
    f = parse_footer(CLEAN_NOT_READY)
    assert f.is_clean_and_ready is False


def test_footer_issues():
    f = parse_footer(ISSUES)
    assert f.status == "ISSUES_FOUND"
    assert f.critical == 1 and f.major == 2
    assert f.is_clean_and_ready is False


def test_footer_missing_raises():
    with pytest.raises(FooterParseError):
        parse_footer("# Review\n\nno footer here\n")


# ---- Strict end-of-file footer enforcement (review-0003) ------------------

VALID_FOOTER_LINES = (
    "<!-- AIDOR:STATUS=CLEAN -->\n"
    '<!-- AIDOR:ISSUES={"critical":0,"major":0,"minor":0,"nit":0} -->\n'
    "<!-- AIDOR:PRODUCTION_READY=true -->\n"
)


def test_footer_with_example_in_suggested_fixes_section_is_rejected():
    """Regression (review-0003): a footer-shaped example earlier in the body
    must not be silently treated as the footer. Only a contiguous trailing
    block of the three required lines is accepted."""
    body = (
        "# Review\n\n"
        "## Suggested fixes\n\n"
        "Reviewers should append:\n\n"
        "    <!-- AIDOR:STATUS=ISSUES_FOUND -->\n"
        '    <!-- AIDOR:ISSUES={"critical":1,"major":0,"minor":0,"nit":0} -->\n'
        "    <!-- AIDOR:PRODUCTION_READY=false -->\n\n" + VALID_FOOTER_LINES
    )
    with pytest.raises(FooterParseError):
        parse_footer(body)


def test_footer_duplicate_blocks_are_rejected():
    """Regression (review-0003): two footer blocks (e.g. a stale one
    followed by an updated one) must be rejected — the contract is exactly
    one footer block at end-of-file."""
    body = "# Review\n\n" + VALID_FOOTER_LINES + "\nMore prose.\n\n" + VALID_FOOTER_LINES
    # Even though the trailing block IS valid, the duplicate earlier block
    # contradicts the single-footer contract.
    with pytest.raises(FooterParseError):
        parse_footer(body)


def test_footer_with_trailing_text_after_footer_is_rejected():
    """Regression (review-0003): a valid-looking footer followed by
    additional non-blank text is not at EOF and must be rejected."""
    body = "# Review\n\n" + VALID_FOOTER_LINES + "\nOh wait, one more thing.\n"
    with pytest.raises(FooterParseError):
        parse_footer(body)


def test_footer_tolerates_trailing_blank_lines():
    """Trailing blank lines after the three footer lines must NOT cause
    rejection — many editors append a final newline (or two)."""
    body = "# Review\n\nAll good.\n\n" + VALID_FOOTER_LINES + "\n\n   \n"
    f = parse_footer(body)
    assert f.is_clean_and_ready is True


def test_footer_out_of_order_is_rejected():
    """The three footer lines must appear in the documented order
    (STATUS, ISSUES, PRODUCTION_READY)."""
    body = (
        "# Review\n\n"
        '<!-- AIDOR:ISSUES={"critical":0,"major":0,"minor":0,"nit":0} -->\n'
        "<!-- AIDOR:STATUS=CLEAN -->\n"
        "<!-- AIDOR:PRODUCTION_READY=true -->\n"
    )
    with pytest.raises(FooterParseError):
        parse_footer(body)


def test_review_store_numbering(tmp_repo):
    store = ReviewStore(tmp_repo / "reviews", tmp_repo / "fixes")
    p1 = store.next_review_path()
    p1.write_text("x", encoding="utf-8")
    p2 = store.next_review_path()
    assert p1 != p2
    assert p2.name > p1.name  # 0002 > 0001
    assert "review-0001" in p1.name
    assert "review-0002" in p2.name

    f1 = store.next_fix_path()
    f1.write_text("x", encoding="utf-8")
    f2 = store.next_fix_path()
    assert "fixes-0001" in f1.name
    assert "fixes-0002" in f2.name


# ---- Strict integer counts in AIDOR:ISSUES (review-0007) ------------------


def _footer_with_issues(issues_payload: str) -> str:
    return (
        "# Review\n\n"
        "<!-- AIDOR:STATUS=ISSUES_FOUND -->\n"
        f"<!-- AIDOR:ISSUES={issues_payload} -->\n"
        "<!-- AIDOR:PRODUCTION_READY=false -->\n"
    )


@pytest.mark.parametrize(
    "issues_payload",
    [
        '{"critical":0,"major":"one","minor":0,"nit":0}',  # string count
        '{"critical":0,"major":null,"minor":0,"nit":0}',  # null count
        '{"critical":0,"major":1.5,"minor":0,"nit":0}',  # float count
        '{"critical":0,"major":true,"minor":0,"nit":0}',  # bool count
        '{"critical":0,"major":[1],"minor":0,"nit":0}',  # list count
        '{"critical":-1,"major":0,"minor":0,"nit":0}',  # negative count
    ],
)
def test_footer_rejects_non_integer_or_negative_counts(issues_payload):
    """Regression (review-0007): syntactically valid AIDOR:ISSUES payloads
    whose counts are not non-negative integers must raise FooterParseError,
    not a raw `ValueError`/`TypeError` from `int(v)` deeper down. One bad
    LLM-generated review footer must never be able to crash the orchestrator.
    """
    with pytest.raises(FooterParseError):
        parse_footer(_footer_with_issues(issues_payload))


# ---- Full footer contract enforcement (review-0010) ----------------------


def test_footer_rejects_missing_baseline_severity():
    """Regression (review-0010): omitting any baseline severity (critical,
    major, minor, nit) must be rejected. A footer of `{}` paired with
    STATUS=CLEAN previously parsed as fully clean."""
    body = (
        "# Review\n\n"
        "<!-- AIDOR:STATUS=CLEAN -->\n"
        "<!-- AIDOR:ISSUES={} -->\n"
        "<!-- AIDOR:PRODUCTION_READY=true -->\n"
    )
    with pytest.raises(FooterParseError, match="baseline severities"):
        parse_footer(body)


def test_footer_rejects_partial_baseline_severities():
    body = (
        "# Review\n\n"
        "<!-- AIDOR:STATUS=CLEAN -->\n"
        '<!-- AIDOR:ISSUES={"minor":1} -->\n'
        "<!-- AIDOR:PRODUCTION_READY=true -->\n"
    )
    with pytest.raises(FooterParseError, match="baseline severities"):
        parse_footer(body)


def test_footer_rejects_clean_when_critical_nonzero():
    """Regression (review-0010): STATUS=CLEAN must contradict-check against
    the actual counts."""
    body = (
        "# Review\n\n"
        "<!-- AIDOR:STATUS=CLEAN -->\n"
        '<!-- AIDOR:ISSUES={"critical":1,"major":0,"minor":0,"nit":0} -->\n'
        "<!-- AIDOR:PRODUCTION_READY=false -->\n"
    )
    with pytest.raises(FooterParseError, match="CLEAN"):
        parse_footer(body)


def test_footer_rejects_clean_when_major_nonzero():
    body = (
        "# Review\n\n"
        "<!-- AIDOR:STATUS=CLEAN -->\n"
        '<!-- AIDOR:ISSUES={"critical":0,"major":2,"minor":0,"nit":0} -->\n'
        "<!-- AIDOR:PRODUCTION_READY=false -->\n"
    )
    with pytest.raises(FooterParseError, match="CLEAN"):
        parse_footer(body)


def test_footer_rejects_production_ready_when_not_clean():
    """Regression (review-0010): PRODUCTION_READY=true must not be allowed
    when STATUS=ISSUES_FOUND."""
    body = (
        "# Review\n\n"
        "<!-- AIDOR:STATUS=ISSUES_FOUND -->\n"
        '<!-- AIDOR:ISSUES={"critical":0,"major":0,"minor":1,"nit":0} -->\n'
        "<!-- AIDOR:PRODUCTION_READY=true -->\n"
    )
    with pytest.raises(FooterParseError, match="PRODUCTION_READY"):
        parse_footer(body)


def test_footer_rejects_production_ready_when_critical_nonzero():
    body = (
        "# Review\n\n"
        "<!-- AIDOR:STATUS=CLEAN -->\n"
        '<!-- AIDOR:ISSUES={"critical":3,"major":0,"minor":0,"nit":0} -->\n'
        "<!-- AIDOR:PRODUCTION_READY=true -->\n"
    )
    # Either the CLEAN check or the PRODUCTION_READY check trips first; both
    # are correct rejections — we just require it to raise.
    with pytest.raises(FooterParseError):
        parse_footer(body)


# ---- Artefact size enforcement (review-0001) ----------------------------


def test_read_artifact_text_raises_when_over_limit(tmp_path):
    """Regression (review-0001): `max_artifact_mb` must actually be
    enforced. A runaway agent writing a huge review file must not be
    silently slurped into memory by the orchestrator."""
    from aidor.config import ArtifactTooLargeError, read_artifact_text

    target = tmp_path / "huge.md"
    target.write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(ArtifactTooLargeError) as excinfo:
        read_artifact_text(target, max_mb=1)
    assert excinfo.value.limit_mb == 1
    assert excinfo.value.size_bytes >= 2 * 1024 * 1024
    assert isinstance(excinfo.value, OSError)


def test_read_artifact_text_allows_small_file(tmp_path):
    from aidor.config import read_artifact_text

    target = tmp_path / "ok.md"
    target.write_text("hello", encoding="utf-8")
    assert read_artifact_text(target, max_mb=1) == "hello"


def test_review_store_read_footer_enforces_size_limit(tmp_path):
    """Regression (review-0001): `ReviewStore.read_review_footer` must honour
    the configured `max_artifact_mb` limit."""
    from aidor.config import ArtifactTooLargeError

    reviews = tmp_path / "reviews"
    reviews.mkdir()
    fixes = tmp_path / "fixes"
    fixes.mkdir()
    store = ReviewStore(reviews, fixes, max_artifact_mb=1)
    target = reviews / "review-0001-20260101-000000.md"
    target.write_bytes(b"y" * (2 * 1024 * 1024))
    with pytest.raises(ArtifactTooLargeError):
        store.read_review_footer(target)
