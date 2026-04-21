"""Runtime configuration for a single aidor run."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_IDLE_TIMEOUT_S = 120
DEFAULT_ROUND_TIMEOUT_S = 10_800  # 3 hours
DEFAULT_MAX_ROUNDS = 10
DEFAULT_MAX_RESTARTS_PER_ROUND = 3
DEFAULT_MAX_ARTIFACT_MB = 256
DEFAULT_TOOL_TIMEOUT_S = 2700  # 45 minutes (advisory; not enforced by default)

# Restart back-off schedule for `copilot --continue` retries.
RESTART_BACKOFF_S: tuple[int, ...] = (30, 120, 600)


@dataclass
class RunConfig:
    """All settings for one aidor invocation. Serialisable to TOML."""

    repo: Path
    coder_model: str
    reviewer_model: str

    max_rounds: int = DEFAULT_MAX_ROUNDS
    idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S
    round_timeout_s: int = DEFAULT_ROUND_TIMEOUT_S
    max_restarts_per_round: int = DEFAULT_MAX_RESTARTS_PER_ROUND
    max_artifact_mb: int = DEFAULT_MAX_ARTIFACT_MB

    allow_local_install: bool = True
    keep_awake: bool = True
    kill_long_tools: bool = False
    tool_timeout_s: int = DEFAULT_TOOL_TIMEOUT_S

    dry_run: bool = False
    resume: bool = False

    # Path to the `copilot` binary. Overridable for testing with a fake.
    copilot_binary: str = "copilot"

    extra: dict[str, Any] = field(default_factory=dict)

    # ---- Derived paths (all inside the repo) -------------------------------

    @property
    def aidor_dir(self) -> Path:
        return self.repo / ".aidor"

    @property
    def reviews_dir(self) -> Path:
        return self.aidor_dir / "reviews"

    @property
    def fixes_dir(self) -> Path:
        return self.aidor_dir / "fixes"

    @property
    def transcripts_dir(self) -> Path:
        return self.aidor_dir / "transcripts"

    @property
    def logs_dir(self) -> Path:
        return self.aidor_dir / "logs"

    @property
    def state_path(self) -> Path:
        return self.aidor_dir / "state.json"

    @property
    def summary_path(self) -> Path:
        return self.aidor_dir / "summary.md"

    @property
    def qa_log_path(self) -> Path:
        return self.logs_dir / "qa.jsonl"

    @property
    def orchestrator_log_path(self) -> Path:
        return self.logs_dir / "orchestrator.log"

    @property
    def config_snapshot_path(self) -> Path:
        return self.aidor_dir / "config.snapshot.toml"

    @property
    def allowed_exceptions_path(self) -> Path:
        return self.aidor_dir / "allowed_exceptions.yml"

    def model_for(self, role: str) -> str:
        if role == "coder":
            return self.coder_model
        if role == "reviewer":
            return self.reviewer_model
        raise ValueError(f"Unknown role: {role!r}")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["repo"] = str(self.repo)
        return d
