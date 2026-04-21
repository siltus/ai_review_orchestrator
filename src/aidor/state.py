"""Persistent run state stored in `.aidor/state.json`.

The state file is the source of truth for resume semantics and for the final
summary. It is written atomically after every phase transition.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

PhaseName = Literal["review", "fix", "readiness_gate"]
PhaseStatus = Literal["pending", "running", "done", "failed", "aborted"]
OverallStatus = Literal[
    "initializing",
    "running",
    "converged",
    "unconverged",
    "aborted",
    "failed",
]


@dataclass
class RestartRecord:
    reason: str
    at: str  # ISO-8601 UTC
    backoff_s: int


@dataclass
class PhaseRecord:
    name: PhaseName
    role: str  # "reviewer" | "coder"
    status: PhaseStatus = "pending"
    started_at: str | None = None
    ended_at: str | None = None
    duration_s: float | None = None
    artifact_path: str | None = None  # review-NNNN.md or fixes-NNNN.md
    transcript_path: str | None = None
    otel_path: str | None = None
    stop_reason: str | None = None  # "end_turn" | ... | "aborted"
    restarts: list[RestartRecord] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    tool_calls: int = 0


@dataclass
class RoundRecord:
    index: int  # 1-based
    phases: list[PhaseRecord] = field(default_factory=list)
    footer: dict[str, Any] | None = None  # parsed AIDOR footer from review
    fixes_summary: str | None = None


@dataclass
class State:
    version: int = 1
    status: OverallStatus = "initializing"
    started_at: str | None = None
    ended_at: str | None = None
    current_round: int = 0  # 0 = not yet started
    rounds: list[RoundRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ---- Serialisation -----------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(_to_plain(self), indent=2, sort_keys=False)

    @classmethod
    def from_json(cls, raw: str) -> State:
        data = json.loads(raw)
        return _from_plain(data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp file in same dir + os.replace.
        fd, tmp = tempfile.mkstemp(prefix="state.", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.to_json())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path) -> State:
        return cls.from_json(path.read_text(encoding="utf-8"))

    # ---- Helpers -----------------------------------------------------------

    def current_round_record(self) -> RoundRecord | None:
        if not self.rounds or self.current_round == 0:
            return None
        return self.rounds[self.current_round - 1]

    def start_round(self) -> RoundRecord:
        self.current_round = len(self.rounds) + 1
        record = RoundRecord(index=self.current_round)
        self.rounds.append(record)
        return record


# ---- (de)serialisation helpers (dataclass-tree ⇄ plain dicts) -------------


def _to_plain(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_plain(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    return obj


def _from_plain(data: dict[str, Any]) -> State:
    rounds = []
    for r in data.get("rounds", []):
        phases = [
            PhaseRecord(
                **{
                    **p,
                    "restarts": [RestartRecord(**rs) for rs in p.get("restarts", [])],
                }
            )
            for p in r.get("phases", [])
        ]
        rounds.append(
            RoundRecord(
                index=r["index"],
                phases=phases,
                footer=r.get("footer"),
                fixes_summary=r.get("fixes_summary"),
            )
        )
    return State(
        version=data.get("version", 1),
        status=data.get("status", "initializing"),
        started_at=data.get("started_at"),
        ended_at=data.get("ended_at"),
        current_round=data.get("current_round", 0),
        rounds=rounds,
        notes=data.get("notes", []),
    )
