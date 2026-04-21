"""Tests for OTel JSONL parsing into PhaseMetrics."""

from __future__ import annotations

import json

from aidor.telemetry import PhaseMetrics, parse_otel_file


def test_missing_file_returns_zero_metrics(tmp_path):
    m = parse_otel_file(tmp_path / "nope.jsonl")
    assert m == PhaseMetrics()


def test_malformed_lines_are_skipped(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text("not json\n{also not}\n", encoding="utf-8")
    m = parse_otel_file(p)
    assert m.tokens_in == 0
    assert m.tokens_out == 0


def test_invoke_agent_span_supplies_canonical_token_counts(tmp_path):
    p = tmp_path / "otel.jsonl"
    records = [
        {
            "name": "chat",
            "attributes": {
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 50,
            },
        },
        {
            "name": "invoke_agent",
            "attributes": {
                "gen_ai.usage.input_tokens": 1000,
                "gen_ai.usage.output_tokens": 500,
                "github.copilot.cost": 0.123,
                "github.copilot.turn_count": 7,
            },
        },
        {"name": "execute_tool", "attributes": {}},
        {"name": "execute_tool", "attributes": {}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    m = parse_otel_file(p)
    # invoke_agent wins for tokens + cost.
    assert m.tokens_in == 1000
    assert m.tokens_out == 500
    assert abs(m.cost - 0.123) < 1e-9
    assert m.turns == 7
    assert m.tool_calls == 2


def test_chat_only_falls_back_when_no_invoke_agent(tmp_path):
    """When there is no invoke_agent span, per-phase totals are the sum
    across all chat spans (each chat span describes one turn). Regression
    for review-0009: previously only the first chat span's tokens were
    kept and cost was dropped entirely."""
    p = tmp_path / "otel.jsonl"
    records = [
        {
            "name": "chat claude-opus-4.7",
            "attributes": {
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 50,
                "github.copilot.cost": 1.5,
            },
        },
        {
            "name": "chat claude-opus-4.7",
            "attributes": {
                "gen_ai.usage.input_tokens": 200,
                "gen_ai.usage.output_tokens": 80,
                "github.copilot.cost": 2.5,
            },
        },
        {"name": "execute_tool view", "attributes": {}},
        {"name": "execute_tool view", "attributes": {}},
        {"name": "execute_tool edit", "attributes": {}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    m = parse_otel_file(p)
    assert m.tokens_in == 300
    assert m.tokens_out == 130
    assert abs(m.cost - 4.0) < 1e-9
    assert m.tool_calls == 3
    assert m.turns == 2  # two chat spans


def test_iadd_accumulates_metrics():
    a = PhaseMetrics(tokens_in=10, tokens_out=5, cost=0.1, tool_calls=2, turns=1)
    b = PhaseMetrics(tokens_in=20, tokens_out=15, cost=0.2, tool_calls=3, turns=2)
    a += b
    assert a.tokens_in == 30
    assert a.tokens_out == 20
    assert abs(a.cost - 0.3) < 1e-9
    assert a.tool_calls == 5
    assert a.turns == 3
