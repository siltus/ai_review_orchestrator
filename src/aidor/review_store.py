"""Review & fix file naming, writing, and footer parsing.

Reviewer output goes to `.aidor/reviews/review-NNNN-YYYYMMDD-HHMMSS.md`.
Coder summary goes to `.aidor/fixes/fixes-NNNN-YYYYMMDD-HHMMSS.md`.

The AIDOR footer is expected at the end of every reviewer file:

    <!-- AIDOR:STATUS=CLEAN|ISSUES_FOUND -->
    <!-- AIDOR:ISSUES={"critical":0,"major":0,"minor":0,"nit":0} -->
    <!-- AIDOR:PRODUCTION_READY=true|false -->
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FOOTER_STATUS_RE = re.compile(r"<!--\s*AIDOR:STATUS=(CLEAN|ISSUES_FOUND)\s*-->", re.IGNORECASE)
FOOTER_ISSUES_RE = re.compile(r"<!--\s*AIDOR:ISSUES=(\{[^}]*\})\s*-->", re.IGNORECASE)
FOOTER_READY_RE = re.compile(r"<!--\s*AIDOR:PRODUCTION_READY=(true|false)\s*-->", re.IGNORECASE)


@dataclass(frozen=True)
class ReviewFooter:
    status: str  # "CLEAN" | "ISSUES_FOUND"
    issues: dict[str, int]  # severity -> count (critical/major/minor/nit at minimum)
    production_ready: bool

    @property
    def is_clean_and_ready(self) -> bool:
        return (
            self.status == "CLEAN"
            and self.production_ready
            and self.critical == 0
            and self.major == 0
        )

    @property
    def critical(self) -> int:
        return int(self.issues.get("critical", 0))

    @property
    def major(self) -> int:
        return int(self.issues.get("major", 0))

    @property
    def minor(self) -> int:
        return int(self.issues.get("minor", 0))

    @property
    def nit(self) -> int:
        return int(self.issues.get("nit", 0))

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "issues": dict(self.issues),
            "production_ready": self.production_ready,
        }


class FooterParseError(ValueError):
    """Raised when a review file does not contain a parseable AIDOR footer."""


def parse_footer(markdown: str) -> ReviewFooter:
    """Extract the AIDOR footer. Raises FooterParseError on missing/invalid."""
    status_m = FOOTER_STATUS_RE.search(markdown)
    issues_m = FOOTER_ISSUES_RE.search(markdown)
    ready_m = FOOTER_READY_RE.search(markdown)
    missing = [
        name
        for name, m in (("STATUS", status_m), ("ISSUES", issues_m), ("PRODUCTION_READY", ready_m))
        if m is None
    ]
    if missing:
        raise FooterParseError(f"review file is missing AIDOR footer fields: {', '.join(missing)}")
    try:
        assert issues_m is not None  # for type checker; already validated above
        issues = json.loads(issues_m.group(1))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise FooterParseError(f"AIDOR:ISSUES is not valid JSON: {exc}") from exc
    if not isinstance(issues, dict):
        raise FooterParseError("AIDOR:ISSUES must be a JSON object")
    assert status_m is not None and ready_m is not None
    return ReviewFooter(
        status=status_m.group(1).upper(),
        issues={str(k): int(v) for k, v in issues.items()},
        production_ready=ready_m.group(1).lower() == "true",
    )


class ReviewStore:
    """File operations for the .aidor/reviews and .aidor/fixes directories."""

    REVIEW_RE = re.compile(r"^review-(\d{4,})-.*\.md$")
    FIX_RE = re.compile(r"^fixes-(\d{4,})-.*\.md$")

    def __init__(self, reviews_dir: Path, fixes_dir: Path) -> None:
        self.reviews_dir = reviews_dir
        self.fixes_dir = fixes_dir

    def ensure_dirs(self) -> None:
        self.reviews_dir.mkdir(parents=True, exist_ok=True)
        self.fixes_dir.mkdir(parents=True, exist_ok=True)

    # ---- Review files ------------------------------------------------------

    def list_reviews(self) -> list[Path]:
        if not self.reviews_dir.exists():
            return []
        return sorted(
            (p for p in self.reviews_dir.iterdir() if self.REVIEW_RE.match(p.name)),
            key=lambda p: _index_from_name(p.name, self.REVIEW_RE),
        )

    def latest_review(self) -> Path | None:
        reviews = self.list_reviews()
        return reviews[-1] if reviews else None

    def next_review_path(self, timestamp: datetime | None = None) -> Path:
        return self._next_path(self.reviews_dir, "review", self.REVIEW_RE, timestamp)

    def read_review_footer(self, path: Path) -> ReviewFooter:
        return parse_footer(path.read_text(encoding="utf-8"))

    # ---- Fix files ---------------------------------------------------------

    def list_fixes(self) -> list[Path]:
        if not self.fixes_dir.exists():
            return []
        return sorted(
            (p for p in self.fixes_dir.iterdir() if self.FIX_RE.match(p.name)),
            key=lambda p: _index_from_name(p.name, self.FIX_RE),
        )

    def latest_fix(self) -> Path | None:
        fixes = self.list_fixes()
        return fixes[-1] if fixes else None

    def next_fix_path(self, timestamp: datetime | None = None) -> Path:
        return self._next_path(self.fixes_dir, "fixes", self.FIX_RE, timestamp)

    # ---- Internals ---------------------------------------------------------

    def _next_path(
        self,
        directory: Path,
        prefix: str,
        regex: re.Pattern[str],
        timestamp: datetime | None,
    ) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        existing = [
            _index_from_name(p.name, regex) for p in directory.iterdir() if regex.match(p.name)
        ]
        next_index = (max(existing) + 1) if existing else 1
        ts = (timestamp or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
        return directory / f"{prefix}-{next_index:04d}-{ts}.md"


def _index_from_name(name: str, regex: re.Pattern[str]) -> int:
    match = regex.match(name)
    if not match:  # pragma: no cover - defensive, callers pre-filter
        return -1
    return int(match.group(1))
