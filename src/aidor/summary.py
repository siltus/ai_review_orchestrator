"""Render the final summary table + summary.md.

Consumes the aggregated State written by the orchestrator.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from aidor.state import State


@dataclass(frozen=True)
class FailedMcpTool:
    tool: str
    count: int
    reason: str


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
    failed_mcp = collect_failed_mcp_tools(path.parent)
    if failed_mcp:
        lines.extend(_failed_mcp_markdown(failed_mcp))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_summary(
    state: State,
    console: Console | None = None,
    *,
    aidor_dir: Path | None = None,
) -> None:
    c = console or Console()
    c.print(render_table(state))
    if aidor_dir is None:
        return
    failed_mcp = collect_failed_mcp_tools(aidor_dir)
    if not failed_mcp:
        return
    tools = ", ".join(f"{item.tool} ({item.count})" for item in failed_mcp)
    c.print(f"[yellow]Denied MCP tools:[/yellow] {tools}")
    c.print("[dim]Add reviewed tools to .aidor/tool_allowlist.yml for the next run.[/dim]")


def collect_failed_mcp_tools(aidor_dir: Path) -> list[FailedMcpTool]:
    log_path = aidor_dir / "logs" / "failed_mcp_tools.jsonl"
    if not log_path.is_file():
        return []
    counts: Counter[str] = Counter()
    reasons: dict[str, str] = {}
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        tool = data.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            continue
        cleaned = tool.strip()
        counts[cleaned] += 1
        reason = data.get("reason")
        if isinstance(reason, str) and reason.strip():
            reasons[cleaned] = reason.strip()
    return [
        FailedMcpTool(tool=tool, count=count, reason=reasons.get(tool, ""))
        for tool, count in sorted(counts.items())
    ]


def _failed_mcp_markdown(items: list[FailedMcpTool]) -> list[str]:
    lines = [
        "",
        "## Denied MCP tools",
        "",
        "The aidor guard denied these MCP tool calls during the run:",
        "",
        "| tool | count | last reason |",
        "|---|---:|---|",
    ]
    for item in items:
        lines.append(
            f"| `{_md_escape(item.tool)}` | {item.count} | {_md_escape(item.reason) or '—'} |"
        )
    lines.extend(
        [
            "",
            "To allow reviewed MCP tools next time, add them to `.aidor/tool_allowlist.yml`:",
            "",
            "```yaml",
            "tools:",
            *[f"  - {item.tool}" for item in items],
            "mcp_tools:",
            *[f"  - {item.tool}" for item in items],
            "```",
            "",
            "Only whitelist side-effecting MCP tools after reviewing their behavior. "
            "If a tool reads or writes repo files, also classify it with "
            "`path_scoped_tools`, `write_tools`, and the appropriate `path_arg_keys`.",
        ]
    )
    return lines


def _md_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


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
