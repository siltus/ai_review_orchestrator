"""Render the final summary table + summary.md.

Consumes the aggregated State written by the orchestrator.
"""
from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

from aidor.state import State


def render_table(state: State) -> Table:
    t = Table(title="aidor run summary", show_lines=True)
    t.add_column("#", justify="right")
    t.add_column("reviewer")
    t.add_column("coder")
    t.add_column("crit")
    t.add_column("major")
    t.add_column("minor")
    t.add_column("nit")
    t.add_column("tok in", justify="right")
    t.add_column("tok out", justify="right")
    t.add_column("cost", justify="right")
    t.add_column("prod-ready")

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
            str(issues.get("critical", "")),
            str(issues.get("major", "")),
            str(issues.get("minor", "")),
            str(issues.get("nit", "")),
            _fmt_int(_sum_tokens(rnd, "in")),
            _fmt_int(_sum_tokens(rnd, "out")),
            f"${_sum_cost(rnd):.4f}" if _sum_cost(rnd) else "",
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
        "| # | reviewer | coder | crit | major | minor | nit | tok in | tok out | cost | prod-ready |",
        "|---|----------|-------|-----:|------:|------:|----:|-------:|--------:|-----:|------------|",
    ]
    for rnd in state.rounds:
        reviewer = _phase(rnd.phases, "reviewer")
        coder = _phase(rnd.phases, "coder")
        footer = rnd.footer or {}
        issues = footer.get("issues") or {}
        prod = footer.get("production_ready")
        lines.append(
            "| {n} | {rv} | {cd} | {c} | {ma} | {mi} | {ni} | {ti} | {to} | {cost} | {p} |".format(
                n=rnd.index,
                rv=_fmt_phase(reviewer),
                cd=_fmt_phase(coder),
                c=issues.get("critical", ""),
                ma=issues.get("major", ""),
                mi=issues.get("minor", ""),
                ni=issues.get("nit", ""),
                ti=_fmt_int(_sum_tokens(rnd, "in")),
                to=_fmt_int(_sum_tokens(rnd, "out")),
                cost=f"${_sum_cost(rnd):.4f}" if _sum_cost(rnd) else "",
                p=_fmt_prod(prod),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_summary(state: State, console: Console | None = None) -> None:
    (console or Console()).print(render_table(state))


# ---- Helpers --------------------------------------------------------------


def _phase(phases: list, role: str):
    for p in phases:
        if p.role == role:
            return p
    return None


def _fmt_phase(p) -> str:
    if p is None:
        return "—"
    if p.duration_s is None:
        return f"{p.status}"
    return f"{p.status} · {_fmt_dur(p.duration_s)}"


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
