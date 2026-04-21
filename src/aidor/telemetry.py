"""Parse Copilot CLI's OpenTelemetry file-exporter JSONL into simple per-phase metrics.

Enabled by setting `COPILOT_OTEL_FILE_EXPORTER_PATH=<file>` in the child
environment (the orchestrator does this per phase).

We only care about a handful of attributes per invocation:
    - gen_ai.usage.input_tokens
    - gen_ai.usage.output_tokens
    - github.copilot.cost
    - github.copilot.tool.call.count (or execute_tool span count)
Turn counts / premium requests are captured opportunistically if present.

Span shapes seen in the wild (Copilot CLI 1.0.32+):

* Multiple ``chat <model>`` spans per phase, each carrying its own
  ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens`` /
  ``github.copilot.cost``. Tokens and cost in each chat span describe **that
  turn only**, so per-phase totals are the *sum* across chat spans.
* Optionally a single outer ``invoke_agent`` span carrying the canonical
  aggregate. When present, this is preferred over the chat-span sum.
* ``execute_tool …`` spans, one per tool call.
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
    if not path.exists():
        return PhaseMetrics()

    chat_tokens_in = 0
    chat_tokens_out = 0
    chat_cost = 0.0
    chat_turns = 0

    invoke_tokens_in = 0
    invoke_tokens_out = 0
    invoke_cost = 0.0
    invoke_seen = False

    tool_calls = 0
    turn_count_attr = 0

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

                attrs = rec.get("attributes") or rec.get("attrs") or {}
                if not isinstance(attrs, dict):
                    continue
                name = (
                    rec.get("name")
                    or rec.get("spanName")
                    or rec.get("operation")
                    or ""
                )
                lname = name.lower()

                ti = attrs.get("gen_ai.usage.input_tokens")
                to = attrs.get("gen_ai.usage.output_tokens")
                cost = attrs.get("github.copilot.cost")

                if "invoke_agent" in lname:
                    invoke_seen = True
                    if isinstance(ti, (int, float)):
                        invoke_tokens_in = max(invoke_tokens_in, int(ti))
                    if isinstance(to, (int, float)):
                        invoke_tokens_out = max(invoke_tokens_out, int(to))
                    if isinstance(cost, (int, float)):
                        invoke_cost = max(invoke_cost, float(cost))
                elif lname.startswith("chat") or "chat " in lname:
                    if isinstance(ti, (int, float)):
                        chat_tokens_in += int(ti)
                    if isinstance(to, (int, float)):
                        chat_tokens_out += int(to)
                    if isinstance(cost, (int, float)):
                        chat_cost += float(cost)
                    chat_turns += 1

                t = attrs.get("github.copilot.turn_count")
                if isinstance(t, (int, float)):
                    turn_count_attr = max(turn_count_attr, int(t))

                if "execute_tool" in lname:
                    tool_calls += 1
    except OSError:
        return PhaseMetrics()

    if invoke_seen:
        tokens_in = invoke_tokens_in
        tokens_out = invoke_tokens_out
        cost_total = invoke_cost
    else:
        tokens_in = chat_tokens_in
        tokens_out = chat_tokens_out
        cost_total = chat_cost

    turns = turn_count_attr if turn_count_attr else chat_turns

    return PhaseMetrics(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost_total,
        tool_calls=tool_calls,
        turns=turns,
    )
