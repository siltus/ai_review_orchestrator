"""End-to-end smoke test via the fake-copilot shim."""

from __future__ import annotations

import asyncio

import pytest

from aidor.orchestrator import Orchestrator

CLEAN_REVIEW = """\
# Review

<!-- AIDOR:STATUS=CLEAN -->
<!-- AIDOR:ISSUES={"critical":0,"major":0,"minor":0,"nit":0} -->
<!-- AIDOR:PRODUCTION_READY=true -->
"""


@pytest.mark.timeout(60)
def test_converges_in_one_round(run_config, monkeypatch):
    """Fake copilot emits a CLEAN + production-ready review on first try;
    the readiness gate then also passes → status converged."""
    # Tell the fake copilot to always write a clean review to the artefact
    # path that the orchestrator will expose via AIDOR_PHASE_INDEX etc.
    # Simpler: use the default content (which is already CLEAN+READY).
    # But the fake needs to know where to write — we use an env var that
    # points at the review_store's *next* file. Since the orchestrator
    # generates paths dynamically, we use a helper approach: the fake
    # copilot writes to the path given in FAKE_COPILOT_EMIT_FILE, which we
    # can't know in advance. Instead, instruct the fake to write the review
    # based on the --share transcript path's sibling.

    # Work around by patching PhaseRunner to set FAKE_COPILOT_EMIT_FILE
    # before spawning.
    from aidor import phase as phase_mod

    original_build_env = phase_mod.PhaseRunner._build_env

    def build_env_with_emit(self):  # type: ignore[no-redef]
        env = original_build_env(self)
        env["FAKE_COPILOT_EMIT_FILE"] = str(self.artifact_path)
        env["FAKE_COPILOT_EMIT_CONTENT"] = CLEAN_REVIEW
        return env

    monkeypatch.setattr(phase_mod.PhaseRunner, "_build_env", build_env_with_emit)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())
    assert code == 0
    assert orch.state.status == "converged"
    # At least one round; the first reviewer phase produced a clean review;
    # then the readiness-gate phase produced another clean review.
    assert len(orch.state.rounds) >= 1
    rnd = orch.state.rounds[0]
    phase_names = [p.name for p in rnd.phases]
    assert "review" in phase_names
    assert "readiness_gate" in phase_names
