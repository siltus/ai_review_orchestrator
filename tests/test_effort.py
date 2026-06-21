"""Tests for the operator's `--effort` family of flags.

Covers GitHub issue #1 — forwarding Copilot CLI's
``--reasoning-effort {low,medium,high,xhigh}`` flag from aidor so users
can reach high/xhigh on GPT-family models (which do not encode the
effort into the model id the way Claude does).

Covers:

* ``RunConfig`` validation of effort slots and ``effort_for(role)``
  precedence (per-role override fully replaces the shared default).
* ``aidor.cli._resolve_effort`` — value normalisation, accept/reject
  paths via the Typer CLI runner.
* ``PhaseRunner._build_argv`` — appends ``--reasoning-effort=<value>``
  exactly when configured, omits it entirely when not, and respects
  the per-role override.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from aidor.cli import app
from aidor.config import EFFORT_LEVELS, RunConfig
from aidor.phase import PhaseRunner

runner = CliRunner()


# ---- RunConfig validation + effort_for -----------------------------------


def _make_cfg(**kw: object) -> RunConfig:
    base: dict[str, object] = {
        "repo": Path("."),
        "coder_model": "gpt-5.5",
        "reviewer_model": "gpt-5.5",
    }
    base.update(kw)
    return RunConfig(**base)  # type: ignore[arg-type]


def test_effort_for_returns_empty_when_unset():
    cfg = _make_cfg()
    assert cfg.effort_for("coder") == ""
    assert cfg.effort_for("reviewer") == ""


def test_effort_for_returns_shared_when_only_shared_set():
    cfg = _make_cfg(effort="high")
    assert cfg.effort_for("coder") == "high"
    assert cfg.effort_for("reviewer") == "high"


def test_effort_for_role_specific_overrides_shared():
    cfg = _make_cfg(effort="medium", reviewer_effort="xhigh", coder_effort="low")
    # Per-role REPLACES shared (does not append) — there's only one
    # --reasoning-effort value Copilot accepts per invocation.
    assert cfg.effort_for("reviewer") == "xhigh"
    assert cfg.effort_for("coder") == "low"


def test_effort_for_role_specific_only():
    cfg = _make_cfg(reviewer_effort="xhigh")
    assert cfg.effort_for("reviewer") == "xhigh"
    assert cfg.effort_for("coder") == ""


def test_effort_for_unknown_role_raises():
    cfg = _make_cfg(effort="high")
    with pytest.raises(ValueError, match="Unknown role"):
        cfg.effort_for("orchestrator")


@pytest.mark.parametrize("level", EFFORT_LEVELS)
def test_runconfig_accepts_all_documented_levels(level: str):
    _make_cfg(effort=level)
    _make_cfg(reviewer_effort=level)
    _make_cfg(coder_effort=level)


def test_runconfig_rejects_invalid_effort_value():
    with pytest.raises(ValueError, match="effort='ultra'"):
        _make_cfg(effort="ultra")


def test_runconfig_rejects_invalid_reviewer_effort_value():
    with pytest.raises(ValueError, match="reviewer_effort='turbo'"):
        _make_cfg(reviewer_effort="turbo")


def test_runconfig_rejects_invalid_coder_effort_value():
    with pytest.raises(ValueError, match="coder_effort='maximum'"):
        _make_cfg(coder_effort="maximum")


# ---- PhaseRunner._build_argv ---------------------------------------------


def _phase_runner(tmp_path: Path, role: str = "coder", **cfg_kw: object) -> PhaseRunner:
    cfg = _make_cfg(repo=tmp_path, copilot_binary="copilot", **cfg_kw)
    return PhaseRunner(
        config=cfg,
        role=role,
        agent_name=f"aidor-{role}",
        prompt="hello",
        phase_index=1,
        artifact_path=tmp_path / "out.md",
    )


def test_build_argv_omits_reasoning_effort_when_unset(tmp_path: Path):
    """Default behavior: no --reasoning-effort means Copilot uses its own
    per-model default. This must be a zero-diff baseline for every existing
    user / test that wasn't expecting the flag."""
    pr = _phase_runner(tmp_path)
    argv = pr._build_argv(resume=False)
    assert not any(arg.startswith("--reasoning-effort") for arg in argv), argv


def test_build_argv_appends_reasoning_effort_for_coder(tmp_path: Path):
    pr = _phase_runner(tmp_path, role="coder", effort="xhigh")
    argv = pr._build_argv(resume=False)
    assert "--reasoning-effort=xhigh" in argv


def test_build_argv_appends_reasoning_effort_for_reviewer(tmp_path: Path):
    pr = _phase_runner(tmp_path, role="reviewer", effort="high")
    argv = pr._build_argv(resume=False)
    assert "--reasoning-effort=high" in argv


def test_build_argv_role_specific_overrides_shared(tmp_path: Path):
    pr = _phase_runner(
        tmp_path,
        role="reviewer",
        effort="low",
        reviewer_effort="xhigh",
    )
    argv = pr._build_argv(resume=False)
    assert "--reasoning-effort=xhigh" in argv
    assert "--reasoning-effort=low" not in argv


def test_build_argv_other_role_unaffected_by_role_specific_override(tmp_path: Path):
    """When only the reviewer override is set, the coder must still inherit
    the shared --effort (or get nothing if there is no shared value)."""
    pr_reviewer_only = _phase_runner(
        tmp_path,
        role="coder",
        reviewer_effort="xhigh",
    )
    argv = pr_reviewer_only._build_argv(resume=False)
    assert not any(arg.startswith("--reasoning-effort") for arg in argv), argv

    pr_coder_inherits_shared = _phase_runner(
        tmp_path,
        role="coder",
        effort="medium",
        reviewer_effort="xhigh",
    )
    argv2 = pr_coder_inherits_shared._build_argv(resume=False)
    assert "--reasoning-effort=medium" in argv2


def test_build_argv_reasoning_effort_appears_before_continue(tmp_path: Path):
    """``--reasoning-effort`` is forwarded as a global flag and must appear
    before resume's ``--continue`` (mirroring how `copilot` documents the
    flag order). This guards against accidentally placing it after the
    `build_flags(...)` extension, which Copilot may treat as positional."""
    pr = _phase_runner(tmp_path, effort="xhigh")
    argv = pr._build_argv(resume=True)
    effort_idx = next((i for i, a in enumerate(argv) if a.startswith("--reasoning-effort=")), None)
    continue_idx = argv.index("--continue") if "--continue" in argv else None
    assert effort_idx is not None
    assert continue_idx is not None
    assert effort_idx < continue_idx


# ---- CLI option parsing --------------------------------------------------


def _run_cli_dry(*extra_args: str, repo: Path) -> Result:
    return runner.invoke(
        app,
        [
            "run",
            "--coder",
            "gpt-5.5",
            "--reviewer",
            "gpt-5.5",
            "--repo",
            str(repo),
            "--copilot-binary",
            "definitely-not-real-aidor-test",
            "--dry-run",
            *extra_args,
        ],
    )


def test_cli_accepts_shared_effort(tmp_path: Path):
    result = _run_cli_dry("--effort", "xhigh", repo=tmp_path)
    assert result.exit_code == 0, result.stdout


def test_cli_accepts_per_role_effort(tmp_path: Path):
    result = _run_cli_dry(
        "--reviewer-effort",
        "xhigh",
        "--coder-effort",
        "low",
        repo=tmp_path,
    )
    assert result.exit_code == 0, result.stdout


def test_cli_accepts_shared_and_role_overrides_together(tmp_path: Path):
    result = _run_cli_dry(
        "--effort",
        "medium",
        "--reviewer-effort",
        "xhigh",
        repo=tmp_path,
    )
    assert result.exit_code == 0, result.stdout


def test_cli_rejects_invalid_effort_value(tmp_path: Path):
    result = _run_cli_dry("--effort", "ultra", repo=tmp_path)
    assert result.exit_code == 2, result.stdout
    assert "not one of" in result.stdout
    assert "low, medium, high, xhigh" in result.stdout


def test_cli_rejects_invalid_reviewer_effort_value(tmp_path: Path):
    result = _run_cli_dry("--reviewer-effort", "turbo", repo=tmp_path)
    assert result.exit_code == 2, result.stdout
    assert "--reviewer-effort" in result.stdout


def test_cli_rejects_invalid_coder_effort_value(tmp_path: Path):
    result = _run_cli_dry("--coder-effort", "max", repo=tmp_path)
    assert result.exit_code == 2, result.stdout
    assert "--coder-effort" in result.stdout


def test_cli_normalises_effort_case(tmp_path: Path):
    """--effort is documented as lowercase; accepting `XHigh` is a small
    quality-of-life affordance and matches Typer's `case_sensitive=False`."""
    result = _run_cli_dry("--effort", "XHigh", repo=tmp_path)
    assert result.exit_code == 0, result.stdout
