"""Parse Copilot CLI's OpenTelemetry file-exporter JSONL into simple per-phase metrics.

Enabled by setting `COPILOT_OTEL_FILE_EXPORTER_PATH=<file>` in the child
environment (the orchestrator does this per phase).

We only care about a handful of attributes per invocation:
    - gen_ai.usage.input_tokens
    - gen_ai.usage.output_tokens
    - github.copilot.cost
    - github.copilot.tool.call.count (or execute_tool span count)
Turn counts / premium requests are captured opportunistically if present.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PhaseMetrics:
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    tool_calls: int = 0
    turns: int = 0

    def __iadd__(self, other: PhaseMetrics) -> PhaseMetrics:
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.cost += other.cost
        self.tool_calls += other.tool_calls
        self.turns += other.turns
        return self


def parse_otel_file(path: Path) -> PhaseMetrics:
    """Best-effort parse. Missing file or malformed lines → zeros; never raises."""
    metrics = PhaseMetrics()
    if not path.exists():
        return metrics
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _absorb(metrics, rec)
    except OSError:
        return metrics
    return metrics


def _absorb(m: PhaseMetrics, rec: dict) -> None:
    # Handle both OTel-SDK shapes and the simpler Copilot file-exporter shape.
    # Spans are often under rec["attributes"] as a flat dict.
    attrs = rec.get("attributes") or rec.get("attrs") or {}
    if not isinstance(attrs, dict):
        return

    name = rec.get("name") or rec.get("spanName") or rec.get("operation") or ""

    # Token counts (chat span + invoke_agent span both carry these).
    ti = attrs.get("gen_ai.usage.input_tokens")
    to = attrs.get("gen_ai.usage.output_tokens")
    if isinstance(ti, (int, float)) and isinstance(to, (int, float)):
        # Only count at the outermost level per the docs: invoke_agent carries
        # totals for all turns. If both chat + invoke_agent appear, invoke_agent
        # is the truth. Heuristic: prefer invoke_agent, ignore chat spans
        # unless we haven't seen invoke_agent yet.
        if "invoke_agent" in name.lower() or (m.tokens_in == 0 and m.tokens_out == 0):
            m.tokens_in = max(m.tokens_in, int(ti))
            m.tokens_out = max(m.tokens_out, int(to))

    cost = attrs.get("github.copilot.cost")
    if isinstance(cost, (int, float)) and "invoke_agent" in name.lower():
        m.cost = max(m.cost, float(cost))

    turns = attrs.get("github.copilot.turn_count")
    if isinstance(turns, (int, float)):
        m.turns = max(m.turns, int(turns))

    if "execute_tool" in name.lower():
        m.tool_calls += 1
