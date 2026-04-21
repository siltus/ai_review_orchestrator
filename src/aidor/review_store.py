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

# Any line that looks like an AIDOR footer marker. Used to detect stray /
# duplicate markers anywhere in the document — the contract is that the
# footer appears exactly once, as the final three lines of the file.
FOOTER_ANY_RE = re.compile(r"<!--\s*AIDOR:(STATUS|ISSUES|PRODUCTION_READY)=", re.IGNORECASE)


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
    """Extract the AIDOR footer.

    The repo contract requires the review file to **end** with exactly three
    AIDOR footer lines, in this order:

        <!-- AIDOR:STATUS=... -->
        <!-- AIDOR:ISSUES=... -->
        <!-- AIDOR:PRODUCTION_READY=... -->

    Any other AIDOR marker elsewhere in the document (a stale prior footer,
    a duplicate, or a footer-shaped example in the prose) is rejected.
    Trailing blank lines after the footer are tolerated; non-blank trailing
    text is not.
    """
    # Split into lines, dropping any trailing blank lines so that the
    # "last three lines" check is robust to a trailing newline.
    lines = markdown.splitlines()
    while lines and lines[-1].strip() == "":
        lines.pop()
    if len(lines) < 3:
        raise FooterParseError("review file is too short to contain the required AIDOR footer")

    last3 = [ln.strip() for ln in lines[-3:]]
    status_m = FOOTER_STATUS_RE.fullmatch(last3[0])
    issues_m = FOOTER_ISSUES_RE.fullmatch(last3[1])
    ready_m = FOOTER_READY_RE.fullmatch(last3[2])
    missing = [
        name
        for name, m in (
            ("STATUS", status_m),
            ("ISSUES", issues_m),
            ("PRODUCTION_READY", ready_m),
        )
        if m is None
    ]
    if missing:
        raise FooterParseError(
            "AIDOR footer must be the final three lines of the review, in order "
            "(STATUS, ISSUES, PRODUCTION_READY); missing or out-of-order: " + ", ".join(missing)
        )

    # Reject any stray AIDOR marker outside the trailing footer block.
    body_lines = lines[:-3]
    for idx, ln in enumerate(body_lines):
        if FOOTER_ANY_RE.search(ln):
            raise FooterParseError(
                f"unexpected AIDOR footer marker at line {idx + 1}; the footer "
                "must appear exactly once, as the final three lines of the file"
            )

    try:
        assert issues_m is not None  # for type checker; already validated above
        issues = json.loads(issues_m.group(1))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise FooterParseError(f"AIDOR:ISSUES is not valid JSON: {exc}") from exc
    if not isinstance(issues, dict):
        raise FooterParseError("AIDOR:ISSUES must be a JSON object")
    coerced: dict[str, int] = {}
    for k, v in issues.items():
        # Booleans are a subclass of int in Python; reject them explicitly so
        # `{"major": true}` doesn't silently parse as 1.
        if isinstance(v, bool) or not isinstance(v, int):
            raise FooterParseError(
                f"AIDOR:ISSUES count for {k!r} must be a non-negative integer; "
                f"got {v!r} ({type(v).__name__})"
            )
        if v < 0:
            raise FooterParseError(f"AIDOR:ISSUES count for {k!r} must be non-negative; got {v}")
        coerced[str(k)] = v

    # Enforce the full footer contract (review-0010): the four baseline
    # severities must be present, and STATUS / PRODUCTION_READY must not
    # contradict the counts. A malformed reviewer footer must NEVER be
    # able to satisfy the convergence gate.
    required_severities = ("critical", "major", "minor", "nit")
    missing_sev = [s for s in required_severities if s not in coerced]
    if missing_sev:
        raise FooterParseError(
            "AIDOR:ISSUES must include all baseline severities "
            f"({', '.join(required_severities)}); missing: {', '.join(missing_sev)}"
        )

    assert status_m is not None and ready_m is not None
    status = status_m.group(1).upper()
    production_ready = ready_m.group(1).lower() == "true"

    if status == "CLEAN" and (coerced["critical"] > 0 or coerced["major"] > 0):
        raise FooterParseError(
            "AIDOR:STATUS=CLEAN is invalid when critical or major issues are "
            f"non-zero (critical={coerced['critical']}, major={coerced['major']})"
        )
    if production_ready and (status != "CLEAN" or coerced["critical"] > 0 or coerced["major"] > 0):
        raise FooterParseError(
            "AIDOR:PRODUCTION_READY=true is invalid unless STATUS=CLEAN and critical=0 and major=0"
        )

    return ReviewFooter(
        status=status,
        issues=coerced,
        production_ready=production_ready,
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
