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
