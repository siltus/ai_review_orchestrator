"""Tests for summary table + summary.md rendering."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from aidor.state import PhaseRecord, RoundRecord, State
from aidor.summary import (
    _fmt_dur,
    _fmt_int,
    _fmt_prod,
    _phase,
    _sum_cost,
    _sum_tokens,
    print_summary,
    render_table,
    write_summary_md,
)


def _state_with_one_round() -> State:
    s = State(
        started_at="2026-04-21T00:00:00Z", ended_at="2026-04-21T00:30:00Z", status="converged"
    )
    rnd = RoundRecord(index=1)
    rnd.phases.append(
        PhaseRecord(
            name="review",
            role="reviewer",
            status="done",
            duration_s=125.0,
            tokens_in=1234,
            tokens_out=567,
            cost=0.0123,
        )
    )
    rnd.phases.append(
        PhaseRecord(
            name="fix",
            role="coder",
            status="done",
            duration_s=300.0,
            tokens_in=2222,
            tokens_out=888,
            cost=0.0456,
        )
    )
    rnd.footer = {
        "issues": {"critical": 0, "major": 0, "minor": 1, "nit": 2},
        "production_ready": True,
    }
    s.rounds.append(rnd)
    return s


def test_render_table_returns_a_table_with_summary_caption():
    state = _state_with_one_round()
    table = render_table(state)
    # Caption mentions the run status.
    assert "converged" in str(table.caption)


def test_print_summary_writes_to_console():
    state = _state_with_one_round()
    buf = StringIO()
    console = Console(file=buf, width=200, color_system=None)
    print_summary(state, console)
    out = buf.getvalue()
    assert "aidor run summary" in out
    assert "converged" in out


def test_write_summary_md_renders_markdown_table(tmp_path: Path):
    state = _state_with_one_round()
    out = tmp_path / "summary.md"
    write_summary_md(state, out)
    body = out.read_text(encoding="utf-8")
    assert "# aidor run summary" in body
    assert "| # | reviewer | coder |" in body
    assert "converged" in body
    assert "✓" in body  # production-ready glyph


def test_fmt_dur_formats_h_m_s():
    assert _fmt_dur(0) == "0s"
    assert _fmt_dur(45) == "45s"
    assert _fmt_dur(125) == "2m05s"
    assert _fmt_dur(3725) == "1h02m"


def test_fmt_int_blank_for_zero():
    assert _fmt_int(0) == ""
    assert _fmt_int(1500) == "1,500"


def test_fmt_prod_handles_three_states():
    assert _fmt_prod(True) == "✓"
    assert _fmt_prod(False) == "✗"
    assert _fmt_prod(None) == "—"


def test_sum_helpers_aggregate_across_phases():
    state = _state_with_one_round()
    rnd = state.rounds[0]
    assert _sum_tokens(rnd, "in") == 1234 + 2222
    assert _sum_tokens(rnd, "out") == 567 + 888
    assert abs(_sum_cost(rnd) - (0.0123 + 0.0456)) < 1e-9


def _state_with_readiness_gate_round() -> State:
    """A round where the initial review was clean but the readiness gate
    found issues — i.e. the round contains *two* reviewer phases."""
    s = State(status="unconverged")
    rnd = RoundRecord(index=1)
    rnd.phases.append(
        PhaseRecord(
            name="review",
            role="reviewer",
            status="done",
            duration_s=60.0,
            tokens_in=100,
            tokens_out=50,
            cost=0.001,
        )
    )
    rnd.phases.append(
        PhaseRecord(
            name="readiness_gate",
            role="reviewer",
            status="done",
            duration_s=200.0,
            tokens_in=300,
            tokens_out=150,
            cost=0.005,
        )
    )
    rnd.footer = {
        "issues": {"critical": 0, "major": 1, "minor": 0, "nit": 0},
        "production_ready": False,
    }
    s.rounds.append(rnd)
    return s


def test_phase_returns_latest_reviewer_for_readiness_gate_round():
    """Regression: a round with both `review` and `readiness_gate` phases must
    report the *latest* reviewer phase (the gate) in the summary, not the
    initial review — otherwise the row mixes the initial review's
    status/duration with the gate's footer counts."""
    state = _state_with_readiness_gate_round()
    rnd = state.rounds[0]
    reviewer = _phase(rnd.phases, "reviewer")
    assert reviewer is not None
    assert reviewer.name == "readiness_gate"
    assert reviewer.duration_s == 200.0


def test_summary_md_reflects_readiness_gate_phase(tmp_path: Path):
    state = _state_with_readiness_gate_round()
    out = tmp_path / "summary.md"
    write_summary_md(state, out)
    body = out.read_text(encoding="utf-8")
    # The reviewer column should reflect the gate's duration (3m20s), not
    # the initial review's (1m00s).
    assert "3m20s" in body
    assert "1m00s" not in body
    # Tokens still aggregate across both reviewer phases (operator total).
    assert "400" in body  # 100 + 300 tokens in
