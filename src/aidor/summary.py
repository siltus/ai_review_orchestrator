"""Render the final summary table + summary.md.

Consumes the aggregated State written by the orchestrator.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

from aidor.state import State


def render_table(state: State) -> Table:
    t = Table(title="aidor run summary", show_lines=False, pad_edge=False)
    t.add_column("#", justify="right", no_wrap=True)
    t.add_column("reviewer", no_wrap=True)
    t.add_column("coder", no_wrap=True)
    t.add_column("issues (c/M/m/n)", no_wrap=True)
    t.add_column("tok in", justify="right", no_wrap=True)
    t.add_column("tok out", justify="right", no_wrap=True)
    t.add_column("cost", justify="right", no_wrap=True)
    t.add_column("prod", justify="center", no_wrap=True)

    for rnd in state.rounds:
        reviewer = _phase(rnd.phases, "reviewer")
        coder = _phase(rnd.phases, "coder")
        footer = rnd.footer or {}
        issues = footer.get("issues") or {}
        prod = footer.get("production_ready")
        t.add_row(
            str(rnd.index),
            _fmt_phase(reviewer),
            _fmt_phase(coder),
            _fmt_issues(issues),
            _fmt_int(_sum_tokens(rnd, "in")),
            _fmt_int(_sum_tokens(rnd, "out")),
            _fmt_cost(_sum_cost(rnd)),
            _fmt_prod(prod),
        )
    t.caption = f"status: {state.status}  ·  rounds: {len(state.rounds)}"
    return t


def write_summary_md(state: State, path: Path) -> None:
    lines = [
        "# aidor run summary",
        "",
        f"- Status: **{state.status}**",
        f"- Rounds: {len(state.rounds)}",
        f"- Started: {state.started_at or '—'}",
        f"- Ended: {state.ended_at or '—'}",
        "",
        "| # | reviewer | coder | issues (c/M/m/n) | tok in | tok out | cost | prod |",
        "|---|----------|-------|------------------|-------:|--------:|-----:|:----:|",
    ]
    for rnd in state.rounds:
        reviewer = _phase(rnd.phases, "reviewer")
        coder = _phase(rnd.phases, "coder")
        footer = rnd.footer or {}
        issues = footer.get("issues") or {}
        prod = footer.get("production_ready")
        lines.append(
            "| {n} | {rv} | {cd} | {iss} | {ti} | {to} | {cost} | {p} |".format(
                n=rnd.index,
                rv=_fmt_phase(reviewer),
                cd=_fmt_phase(coder),
                iss=_fmt_issues(issues),
                ti=_fmt_int(_sum_tokens(rnd, "in")),
                to=_fmt_int(_sum_tokens(rnd, "out")),
                cost=_fmt_cost(_sum_cost(rnd)),
                p=_fmt_prod(prod),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_summary(state: State, console: Console | None = None) -> None:
    (console or Console()).print(render_table(state))


# ---- Helpers --------------------------------------------------------------


def _phase(phases: list, role: str):
    # Return the *latest* phase for this role. A round can contain more than
    # one reviewer phase (e.g. the initial `review` plus a `readiness_gate`),
    # and we must not silently hide the gate by reporting only the first
    # reviewer phase — that would mix the initial review's status/duration
    # with the gate's footer counts in the same row.
    match = None
    for p in phases:
        if p.role == role:
            match = p
    return match


def _fmt_phase(p) -> str:
    if p is None:
        return "—"
    if p.duration_s is None:
        return p.status
    dur = _fmt_dur(p.duration_s)
    # Hide redundant "done · " prefix: when a phase finished cleanly the
    # duration alone is the useful signal. Surface non-clean statuses
    # (timeout, aborted, error, ...) so they don't disappear silently.
    if p.status in ("done", ""):
        return dur
    return f"{p.status} · {dur}"


def _fmt_issues(issues: dict) -> str:
    """Render the four issue counts as a single compact ``c/M/m/n`` cell.

    Empty / all-zero footers render as ``—`` so a clean round is visually
    distinct from a missing footer."""
    if not issues:
        return "—"
    c = int(issues.get("critical", 0) or 0)
    m_ = int(issues.get("major", 0) or 0)
    mi = int(issues.get("minor", 0) or 0)
    ni = int(issues.get("nit", 0) or 0)
    if c == m_ == mi == ni == 0:
        return "0/0/0/0"
    return f"{c}/{m_}/{mi}/{ni}"


def _fmt_cost(cost: float) -> str:
    if not cost:
        return ""
    if cost >= 1.0:
        return f"${cost:,.2f}"
    return f"${cost:.4f}"


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_int(n: int) -> str:
    return f"{n:,}" if n else ""


def _fmt_prod(prod) -> str:
    if prod is True:
        return "✓"
    if prod is False:
        return "✗"
    return "—"


def _sum_tokens(rnd, direction: str) -> int:
    total = 0
    for p in rnd.phases:
        total += p.tokens_in if direction == "in" else p.tokens_out
    return total


def _sum_cost(rnd) -> float:
    return sum((p.cost or 0.0) for p in rnd.phases)
