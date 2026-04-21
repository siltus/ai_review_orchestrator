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
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"state.json is not valid JSON: {exc}") from exc
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


_PHASE_STR_FIELDS = ("name", "role", "status")
_PHASE_OPTIONAL_STR_FIELDS = (
    "started_at",
    "ended_at",
    "artifact_path",
    "transcript_path",
    "otel_path",
    "stop_reason",
)
_PHASE_INT_FIELDS = ("tokens_in", "tokens_out", "tool_calls")
_PHASE_NUMERIC_FIELDS = ("cost",)  # int or float
_PHASE_OPTIONAL_NUMERIC_FIELDS = ("duration_s",)
_VALID_PHASE_NAMES = {"review", "fix", "readiness_gate"}
_VALID_PHASE_STATUSES = {"pending", "running", "done", "failed", "aborted"}


def _validate_phase_scalars(p: dict, round_index: Any) -> None:
    """Validate the scalar fields of a persisted phase dict (review-0012).

    Raises ``ValueError`` for any field that is present but the wrong type
    so that ``--resume`` cannot crash with a raw ``TypeError`` deeper in
    the orchestrator (e.g. ``Path(artifact_path)`` on a non-string).
    """
    prefix = f"state.json round {round_index!r} phase"
    for field_name in _PHASE_STR_FIELDS:
        if field_name in p and not isinstance(p[field_name], str):
            raise ValueError(
                f"{prefix} {field_name!r} must be a string, "
                f"got {type(p[field_name]).__name__}"
            )
    if "name" in p and p["name"] not in _VALID_PHASE_NAMES:
        raise ValueError(
            f"{prefix} 'name' must be one of {sorted(_VALID_PHASE_NAMES)}; "
            f"got {p['name']!r}"
        )
    if "status" in p and p["status"] not in _VALID_PHASE_STATUSES:
        raise ValueError(
            f"{prefix} 'status' must be one of {sorted(_VALID_PHASE_STATUSES)}; "
            f"got {p['status']!r}"
        )
    for field_name in _PHASE_OPTIONAL_STR_FIELDS:
        if field_name in p and p[field_name] is not None and not isinstance(
            p[field_name], str
        ):
            raise ValueError(
                f"{prefix} {field_name!r} must be a string or null, "
                f"got {type(p[field_name]).__name__}"
            )
    for field_name in _PHASE_INT_FIELDS:
        if field_name in p and (
            not isinstance(p[field_name], int) or isinstance(p[field_name], bool)
        ):
            raise ValueError(
                f"{prefix} {field_name!r} must be an integer, "
                f"got {type(p[field_name]).__name__}"
            )
    for field_name in _PHASE_NUMERIC_FIELDS:
        if field_name in p and (
            not isinstance(p[field_name], (int, float))
            or isinstance(p[field_name], bool)
        ):
            raise ValueError(
                f"{prefix} {field_name!r} must be a number, "
                f"got {type(p[field_name]).__name__}"
            )
    for field_name in _PHASE_OPTIONAL_NUMERIC_FIELDS:
        if field_name in p and p[field_name] is not None and (
            not isinstance(p[field_name], (int, float))
            or isinstance(p[field_name], bool)
        ):
            raise ValueError(
                f"{prefix} {field_name!r} must be a number or null, "
                f"got {type(p[field_name]).__name__}"
            )


def _from_plain(data: Any) -> State:
    if not isinstance(data, dict):
        raise ValueError(
            f"state.json must contain a JSON object at the top level, got {type(data).__name__}"
        )
    raw_rounds = data.get("rounds", [])
    if not isinstance(raw_rounds, list):
        raise ValueError("state.json 'rounds' must be a list")
    rounds = []
    for r in raw_rounds:
        if not isinstance(r, dict) or "index" not in r:
            raise ValueError("state.json round entries must be objects with an 'index'")
        raw_phases = r.get("phases", [])
        if not isinstance(raw_phases, list):
            raise ValueError(
                f"state.json round {r.get('index')!r} 'phases' must be a list, "
                f"got {type(raw_phases).__name__}"
            )
        phases = []
        for p in raw_phases:
            if not isinstance(p, dict):
                raise ValueError(
                    f"state.json round {r.get('index')!r} phase entries must be objects, "
                    f"got {type(p).__name__}"
                )
            raw_restarts = p.get("restarts", [])
            if not isinstance(raw_restarts, list):
                raise ValueError(
                    f"state.json round {r.get('index')!r} phase 'restarts' must be a list, "
                    f"got {type(raw_restarts).__name__}"
                )
            restarts = []
            for rs in raw_restarts:
                if not isinstance(rs, dict):
                    raise ValueError(
                        f"state.json round {r.get('index')!r} restart entries must be objects, "
                        f"got {type(rs).__name__}"
                    )
                try:
                    restarts.append(RestartRecord(**rs))
                except TypeError as exc:
                    raise ValueError(
                        f"state.json round {r.get('index')!r} malformed restart entry: {exc}"
                    ) from exc
            _validate_phase_scalars(p, r.get("index"))
            try:
                phases.append(PhaseRecord(**{**p, "restarts": restarts}))
            except TypeError as exc:
                raise ValueError(
                    f"state.json round {r.get('index')!r} malformed phase entry: {exc}"
                ) from exc
        footer = r.get("footer")
        if footer is not None and not isinstance(footer, dict):
            raise ValueError(
                f"state.json round {r.get('index')!r} 'footer' must be an object or null"
            )
        rounds.append(
            RoundRecord(
                index=r["index"],
                phases=phases,
                footer=footer,
                fixes_summary=r.get("fixes_summary"),
            )
        )
    notes = data.get("notes", [])
    if not isinstance(notes, list):
        raise ValueError("state.json 'notes' must be a list")
    for i, n in enumerate(notes):
        if not isinstance(n, str):
            raise ValueError(
                f"state.json 'notes[{i}]' must be a string, got {type(n).__name__}"
            )

    # Top-level scalar boundary validation (review-0010): a corrupt
    # `current_round: "1"` or `null` must be rejected at load time, not
    # leak out as a TypeError from `current_round - 1` deeper in the run.
    version = data.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(
            f"state.json 'version' must be an integer, got {type(version).__name__}"
        )

    valid_statuses = {
        "initializing", "running", "converged", "unconverged", "aborted", "failed",
    }
    status = data.get("status", "initializing")
    if not isinstance(status, str):
        raise ValueError(
            f"state.json 'status' must be a string, got {type(status).__name__}"
        )
    if status not in valid_statuses:
        raise ValueError(
            f"state.json 'status' must be one of {sorted(valid_statuses)}; got {status!r}"
        )

    started_at = data.get("started_at")
    if started_at is not None and not isinstance(started_at, str):
        raise ValueError(
            f"state.json 'started_at' must be a string or null, got {type(started_at).__name__}"
        )
    ended_at = data.get("ended_at")
    if ended_at is not None and not isinstance(ended_at, str):
        raise ValueError(
            f"state.json 'ended_at' must be a string or null, got {type(ended_at).__name__}"
        )

    current_round = data.get("current_round", 0)
    if not isinstance(current_round, int) or isinstance(current_round, bool):
        raise ValueError(
            f"state.json 'current_round' must be an integer, "
            f"got {type(current_round).__name__}"
        )
    if current_round < 0:
        raise ValueError(
            f"state.json 'current_round' must be non-negative, got {current_round}"
        )

    return State(
        version=version,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        current_round=current_round,
        rounds=rounds,
        notes=notes,
    )


def validate_artifact_paths_within_repo(state: State, repo_root: Path) -> str | None:
    """Return an error message if any persisted ``artifact_path`` escapes
    ``repo_root`` (review-0013).

    Round 12 made ``--resume`` reject wrong-typed phase scalars before
    constructing ``PhaseRecord``. It still trusted any string blindly: a
    hand-edited or corrupt ``state.json`` could pin ``artifact_path`` to
    an absolute path outside the repository, and the orchestrator would
    then ``Path(...).read_text()`` it during review/footer parsing,
    violating the repo's "never read or write files outside the
    repository root" guard. This helper is the second layer of the
    boundary: enforce containment before any disk read happens.

    Returns ``None`` when every persisted ``artifact_path`` resolves to a
    path strictly inside ``repo_root``; otherwise returns a human-readable
    error suitable for surfacing on the CLI.
    """
    try:
        repo_resolved = repo_root.resolve(strict=False)
    except OSError as exc:
        return f"could not resolve repo root {repo_root}: {exc}"
    for rnd in state.rounds:
        for p in rnd.phases:
            ap = p.artifact_path
            if ap is None:
                continue
            if not isinstance(ap, str):  # defensive; _validate_phase_scalars already enforces
                return (
                    f"round {rnd.index} phase {p.name!r} artifact_path "
                    f"must be a string, got {type(ap).__name__}"
                )
            try:
                candidate = Path(ap)
                resolved = candidate.resolve(strict=False)
            except (OSError, ValueError) as exc:
                return (
                    f"round {rnd.index} phase {p.name!r} artifact_path "
                    f"{ap!r} could not be resolved: {exc}"
                )
            if candidate.is_absolute() and not _is_within(resolved, repo_resolved):
                return (
                    f"round {rnd.index} phase {p.name!r} artifact_path "
                    f"{ap!r} is outside the repository root {repo_root}"
                )
            if not candidate.is_absolute() and not _is_within(resolved, repo_resolved):
                return (
                    f"round {rnd.index} phase {p.name!r} artifact_path "
                    f"{ap!r} resolves to {resolved}, outside the repository root "
                    f"{repo_root}"
                )
    return None


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return child == parent or parent in child.parents
    except (OSError, ValueError):
        return False
