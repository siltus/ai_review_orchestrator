"""End-to-end smoke test via the fake-copilot shim."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aidor.orchestrator import Orchestrator, write_abort_marker

CLEAN_REVIEW = """\
# Review

<!-- AIDOR:STATUS=CLEAN -->
<!-- AIDOR:ISSUES={"critical":0,"major":0,"minor":0,"nit":0} -->
<!-- AIDOR:PRODUCTION_READY=true -->
"""


@pytest.mark.timeout(60)
def test_converges_in_one_round(run_config, monkeypatch: pytest.MonkeyPatch):
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


# ---- abort-marker helper -------------------------------------------------


def test_write_abort_marker_creates_marker(tmp_path: Path):
    aidor_dir = tmp_path / ".aidor"
    assert write_abort_marker(aidor_dir, "unit_test") is True
    marker = aidor_dir / "ABORT"
    assert marker.exists()
    txt = marker.read_text(encoding="utf-8")
    assert "aborted_via=unit_test" in txt


def test_write_abort_marker_is_idempotent(tmp_path: Path):
    aidor_dir = tmp_path / ".aidor"
    assert write_abort_marker(aidor_dir, "first") is True
    assert write_abort_marker(aidor_dir, "second") is False
    # First write wins — the marker is single-shot.
    assert "aborted_via=first" in (aidor_dir / "ABORT").read_text(encoding="utf-8")


def test_cli_run_writes_abort_marker_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Regression (review-0002): on Windows, `Ctrl-C` propagates as
    `KeyboardInterrupt` out of `asyncio.run` rather than hitting the POSIX
    signal handler. The CLI entry point must still honour the documented
    abort contract by writing `.aidor/ABORT`.
    """
    import asyncio as _asyncio

    from typer.testing import CliRunner

    from aidor.cli import app

    def _fake_run(coro):
        # Drain the coroutine so it doesn't leak a warning, then raise.
        try:
            coro.close()
        except Exception:  # pragma: no cover - defensive
            pass
        raise KeyboardInterrupt

    monkeypatch.setattr(_asyncio, "run", _fake_run)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "--coder",
            "x",
            "--reviewer",
            "y",
            "--repo",
            str(tmp_path),
            "--copilot-binary",
            "copilot",
        ],
    )
    assert result.exit_code == 130, result.stdout
    marker = tmp_path / ".aidor" / "ABORT"
    assert marker.exists(), "CLI must write .aidor/ABORT on KeyboardInterrupt"
    assert "aborted_via=keyboard_interrupt" in marker.read_text(encoding="utf-8")


# ---- phase-status mapping ------------------------------------------------


def test_phase_status_aborted_is_reported_as_aborted(
    run_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Regression (review-0002): phases that ended with stop_reason=='aborted'
    must report phase.status=='aborted', not 'failed', so state.json and the
    summary reflect an operator abort correctly.
    """
    from aidor import phase as phase_mod
    from aidor.phase import PhaseResult
    from aidor.telemetry import PhaseMetrics

    async def _fake_run(self):  # type: ignore[no-redef]
        return PhaseResult(
            exit_code=-1,
            stop_reason="aborted",
            duration_s=0.1,
            transcript_path=self.transcript_path,
            otel_path=self.otel_path,
            metrics=PhaseMetrics(),
            restarts=[],
        )

    monkeypatch.setattr(phase_mod.PhaseRunner, "run", _fake_run)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())
    # Status flipped to aborted; exit code is 130 per the abort path.
    assert code == 130
    assert orch.state.status == "aborted"
    rnd = orch.state.rounds[0]
    review_phase = next(p for p in rnd.phases if p.name == "review")
    assert review_phase.status == "aborted", f"expected aborted, got {review_phase.status!r}"
    assert review_phase.stop_reason == "aborted"


# ---- Readiness-gate routing (review-0003) ---------------------------------


CLEAN_AND_READY_FOOTER = (
    "# Review\n\nLooks good.\n\n"
    "<!-- AIDOR:STATUS=CLEAN -->\n"
    '<!-- AIDOR:ISSUES={"critical":0,"major":0,"minor":0,"nit":0} -->\n'
    "<!-- AIDOR:PRODUCTION_READY=true -->\n"
)

GATE_FOUND_ISSUES_FOOTER = (
    "# Readiness gate\n\nFound a major issue.\n\n"
    "<!-- AIDOR:STATUS=ISSUES_FOUND -->\n"
    '<!-- AIDOR:ISSUES={"critical":0,"major":1,"minor":0,"nit":0} -->\n'
    "<!-- AIDOR:PRODUCTION_READY=false -->\n"
)


def _patch_phase_runner(monkeypatch: pytest.MonkeyPatch, content_for):
    """Patch PhaseRunner.run to write `content_for(self.artifact_path)` to
    the artifact path and return a successful PhaseResult."""
    from aidor import phase as phase_mod
    from aidor.phase import PhaseResult
    from aidor.telemetry import PhaseMetrics

    async def _fake_run(self):  # type: ignore[no-redef]
        body = content_for(self)
        if body is not None:
            self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
            self.artifact_path.write_text(body, encoding="utf-8")
        return PhaseResult(
            exit_code=0,
            stop_reason="end_turn",
            duration_s=0.01,
            transcript_path=self.transcript_path,
            otel_path=self.otel_path,
            metrics=PhaseMetrics(),
            restarts=[],
        )

    monkeypatch.setattr(phase_mod.PhaseRunner, "run", _fake_run)


def test_readiness_gate_found_issues_routes_fix_to_gate_review(
    run_config, monkeypatch: pytest.MonkeyPatch
):
    """Regression (review-0003): when the readiness gate finds NEW issues
    after a clean review, the coder's fix prompt must reference the gate's
    review file, not the original (now-stale) clean review."""
    captured_fix_prompts: list[str] = []

    # Sequence:
    #   Round 1 review → CLEAN+READY (triggers gate)
    #   Round 1 gate   → ISSUES (triggers fix)
    #   Round 1 fix    → coder writes fixes summary
    #   Round 2 review → CLEAN+READY
    #   Round 2 gate   → CLEAN+READY → CONVERGED
    state = {"review_calls": 0, "gate_calls": 0}

    def content_for(runner):
        if runner.role == "reviewer":
            # Distinguish between the round's `review` and the `readiness_gate`
            # by looking at which artefact path was used most recently. The
            # orchestrator allocates a fresh review file for each, so we just
            # alternate based on call count.
            # Round 1 review → CLEAN, gate → ISSUES.
            # Round 2 review → CLEAN, gate → CLEAN.
            if "fix" in str(runner.artifact_path):  # never; safety
                return None
            # Identify gate vs review via phase_index: gate uses round+1000.
            is_gate = runner.phase_index >= 1000
            if is_gate:
                state["gate_calls"] += 1
                if state["gate_calls"] == 1:
                    return GATE_FOUND_ISSUES_FOOTER
                return CLEAN_AND_READY_FOOTER
            else:
                state["review_calls"] += 1
                return CLEAN_AND_READY_FOOTER
        else:
            # Coder fix phase. Capture the prompt.
            captured_fix_prompts.append(runner.prompt)
            return "# Fixes\n\nApplied.\n"

    _patch_phase_runner(monkeypatch, content_for)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 0, f"expected convergence, got exit={code}, status={orch.state.status}"
    assert orch.state.status == "converged"
    assert len(captured_fix_prompts) == 1, "exactly one fix phase should have run"

    # The fix prompt MUST reference the gate's review file, not the original.
    rnd1 = orch.state.rounds[0]
    review_phase = next(p for p in rnd1.phases if p.name == "review")
    gate_phase = next(p for p in rnd1.phases if p.name == "readiness_gate")
    assert review_phase.artifact_path != gate_phase.artifact_path
    fix_prompt = captured_fix_prompts[0]
    assert gate_phase.artifact_path is not None
    assert review_phase.artifact_path is not None
    assert gate_phase.artifact_path in fix_prompt, (
        f"fix prompt must reference gate review path "
        f"{gate_phase.artifact_path!r}; prompt was:\n{fix_prompt}"
    )
    assert review_phase.artifact_path not in fix_prompt, (
        "fix prompt must NOT reference the original (stale) clean review"
    )


def test_resume_after_gate_routes_fix_to_gate_review(run_config, monkeypatch: pytest.MonkeyPatch):
    """Regression (review-0003): on `--resume` after the readiness gate has
    already produced an ISSUES_FOUND review (but the fix phase had not yet
    started), the orchestrator must use the GATE's review path for the fix
    prompt — not the original clean review parsed from the rerun."""
    from aidor.review_store import ReviewStore
    from aidor.state import PhaseRecord, RoundRecord, State

    # Pre-populate state.json: round 1 has review done (clean) + gate done
    # (issues), and the fix phase is still pending.
    store = ReviewStore(run_config.reviews_dir, run_config.fixes_dir)
    store.ensure_dirs()
    review_path = store.next_review_path()
    review_path.write_text(CLEAN_AND_READY_FOOTER, encoding="utf-8")
    gate_path = store.next_review_path()
    gate_path.write_text(GATE_FOUND_ISSUES_FOOTER, encoding="utf-8")

    state = State(status="running", current_round=1)
    rnd = RoundRecord(
        index=1,
        phases=[
            PhaseRecord(
                name="review",
                role="reviewer",
                status="done",
                artifact_path=str(review_path),
                stop_reason="end_turn",
            ),
            PhaseRecord(
                name="readiness_gate",
                role="reviewer",
                status="done",
                artifact_path=str(gate_path),
                stop_reason="end_turn",
            ),
        ],
    )
    state.rounds.append(rnd)
    run_config.aidor_dir.mkdir(parents=True, exist_ok=True)
    state.save(run_config.state_path)

    # Enable resume.
    run_config.resume = True

    captured_fix_prompts: list[str] = []

    def content_for(runner):
        if runner.role == "reviewer":
            # Round 2 review/gate → CLEAN+READY → CONVERGED.
            return CLEAN_AND_READY_FOOTER
        captured_fix_prompts.append(runner.prompt)
        return "# Fixes\n\nApplied.\n"

    _patch_phase_runner(monkeypatch, content_for)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 0, f"expected convergence, got exit={code}, status={orch.state.status}"
    assert len(captured_fix_prompts) == 1
    fix_prompt = captured_fix_prompts[0]
    assert str(gate_path) in fix_prompt, (
        f"on resume, fix prompt must reference gate review {gate_path}; prompt was:\n{fix_prompt}"
    )
    assert str(review_path) not in fix_prompt, (
        "on resume, fix prompt must NOT reference the original clean review"
    )


def test_followup_review_prompt_references_gate_review(run_config):
    """Regression (review-0003): the round-N follow-up reviewer prompt must
    reference the LATEST reviewer artefact from round N-1 (the readiness
    gate, when one ran), not always the original `review` phase."""
    from aidor.state import PhaseRecord, RoundRecord

    orch = Orchestrator(run_config)
    rnd1 = RoundRecord(
        index=1,
        phases=[
            PhaseRecord(
                name="review",
                role="reviewer",
                status="done",
                artifact_path="/tmp/reviews/review-0001-x.md",
            ),
            PhaseRecord(
                name="readiness_gate",
                role="reviewer",
                status="done",
                artifact_path="/tmp/reviews/review-0002-gate.md",
            ),
            PhaseRecord(
                name="fix",
                role="coder",
                status="done",
                artifact_path="/tmp/fixes/fixes-0001-x.md",
            ),
        ],
    )
    orch.state.rounds.append(rnd1)

    prompt = orch._review_prompt(round_index=2, review_path=Path("/tmp/reviews/review-0003-new.md"))
    assert "/tmp/reviews/review-0002-gate.md" in prompt
    assert "/tmp/reviews/review-0001-x.md" not in prompt
    assert "/tmp/fixes/fixes-0001-x.md" in prompt


# ---- Readiness-gate invalid-footer routing (review-0004) -----------------


GATE_INVALID_FOOTER = (
    "# Readiness gate\n\nFound issues but the footer is malformed.\n\n"
    "<!-- AIDOR:STATUS=ISSUES_FOUND -->\n"
    "this trailing prose breaks the strict footer contract\n"
)


def test_readiness_gate_invalid_footer_fails_round(run_config, monkeypatch: pytest.MonkeyPatch):
    """Regression (review-0004): when the readiness-gate artefact exists
    but its AIDOR footer is malformed/missing, the orchestrator must NOT
    silently fall back to the (now-stale) original clean review and ask the
    coder to fix it. It must mark the round as failed, mirroring the
    primary-review failure path.
    """
    state = {"review_calls": 0, "gate_calls": 0}

    def content_for(runner):
        if runner.role == "reviewer":
            is_gate = runner.phase_index >= 1000
            if is_gate:
                state["gate_calls"] += 1
                return GATE_INVALID_FOOTER
            state["review_calls"] += 1
            return CLEAN_AND_READY_FOOTER
        # Should never reach the coder for a failed round.
        raise AssertionError("fix phase must not run when the readiness-gate footer is invalid")

    _patch_phase_runner(monkeypatch, content_for)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 2, f"expected failure exit, got {code}"
    assert orch.state.status == "failed"
    assert state["review_calls"] == 1
    assert state["gate_calls"] == 1
    rnd = orch.state.rounds[0]
    assert any(p.name == "readiness_gate" for p in rnd.phases)
    assert not any(p.name == "fix" for p in rnd.phases), (
        "no fix phase should be recorded when the gate footer is invalid"
    )
    # review-0014: the readiness-gate phase must be downgraded to
    # `failed` so the persisted state cannot show a `done` gate phase
    # whose artefact has an unparseable footer.
    gate_phase = next(p for p in rnd.phases if p.name == "readiness_gate")
    assert gate_phase.status == "failed"


def test_resume_with_invalid_gate_footer_fails_round(run_config, monkeypatch: pytest.MonkeyPatch):
    """Regression (review-0004): on `--resume` after a previous process
    completed the readiness-gate phase but the gate artefact has a malformed
    footer, the orchestrator must again refuse to route the coder at the
    stale original clean review — it must fail the round.
    """
    from aidor.review_store import ReviewStore
    from aidor.state import PhaseRecord, RoundRecord, State

    store = ReviewStore(run_config.reviews_dir, run_config.fixes_dir)
    store.ensure_dirs()
    review_path = store.next_review_path()
    review_path.write_text(CLEAN_AND_READY_FOOTER, encoding="utf-8")
    gate_path = store.next_review_path()
    gate_path.write_text(GATE_INVALID_FOOTER, encoding="utf-8")

    state = State(status="running", current_round=1)
    rnd = RoundRecord(
        index=1,
        phases=[
            PhaseRecord(
                name="review",
                role="reviewer",
                status="done",
                artifact_path=str(review_path),
                stop_reason="end_turn",
            ),
            PhaseRecord(
                name="readiness_gate",
                role="reviewer",
                status="done",
                artifact_path=str(gate_path),
                stop_reason="end_turn",
            ),
        ],
    )
    state.rounds.append(rnd)
    run_config.aidor_dir.mkdir(parents=True, exist_ok=True)
    state.save(run_config.state_path)
    run_config.resume = True

    def content_for(runner):
        if runner.role == "coder":
            raise AssertionError("fix phase must not run when the gate footer is invalid on resume")
        # No reviewer phases should run in this resume path either, but be
        # defensive: emit a clean review if asked.
        return CLEAN_AND_READY_FOOTER

    _patch_phase_runner(monkeypatch, content_for)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 2
    assert orch.state.status == "failed"
    rnd1 = orch.state.rounds[0]
    assert not any(p.name == "fix" for p in rnd1.phases)


# ---- Resume must not skip an unfinished round (review-0007) ---------------


def test_resume_after_crash_mid_round_completes_same_round(
    run_config, monkeypatch: pytest.MonkeyPatch
):
    """Regression (review-0007): `State.start_round()` persists
    `current_round` as soon as a round begins. If the process dies after the
    review/readiness-gate phases ran but before the fix phase started, a
    subsequent `--resume` must re-enter the SAME round and run the missing
    fix — not silently skip to round N+1. Uses the realistic persisted
    shape (`current_round=1` with round 1 partially complete)."""
    from aidor.review_store import ReviewStore
    from aidor.state import PhaseRecord, RoundRecord, State

    store = ReviewStore(run_config.reviews_dir, run_config.fixes_dir)
    store.ensure_dirs()
    review_path = store.next_review_path()
    review_path.write_text(GATE_FOUND_ISSUES_FOOTER, encoding="utf-8")

    # Realistic crash state: round 1 in flight, review done, fix not yet
    # started. `current_round` is 1 because `start_round()` persisted it
    # before the review phase ran — exactly what the live code writes.
    state = State(status="running", current_round=1)
    state.rounds.append(
        RoundRecord(
            index=1,
            phases=[
                PhaseRecord(
                    name="review",
                    role="reviewer",
                    status="done",
                    artifact_path=str(review_path),
                    stop_reason="end_turn",
                ),
            ],
        )
    )
    run_config.aidor_dir.mkdir(parents=True, exist_ok=True)
    state.save(run_config.state_path)
    run_config.resume = True

    captured_fix_prompts: list[str] = []

    def content_for(runner):
        if runner.role == "reviewer":
            # Round 2 review → CLEAN+READY → CONVERGED.
            return CLEAN_AND_READY_FOOTER
        captured_fix_prompts.append(runner.prompt)
        return "# Fixes\n\nApplied.\n"

    _patch_phase_runner(monkeypatch, content_for)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 0, f"expected convergence, got exit={code}, status={orch.state.status}"
    # The unfinished round 1 must have been completed (fix phase appended),
    # and we must NOT have skipped it by jumping straight to round 2.
    rnd1 = orch.state.rounds[0]
    fix_phase = next((p for p in rnd1.phases if p.name == "fix"), None)
    assert fix_phase is not None, (
        "resume must run the missing fix phase from the unfinished round, "
        "not skip ahead to a new round"
    )
    assert fix_phase.status == "done"
    assert len(captured_fix_prompts) == 1
    assert str(review_path) in captured_fix_prompts[0]


# ---- Malformed footer count must not crash the orchestrator (review-0007)


BAD_COUNT_REVIEW = (
    "# Review\n\nLooks bad.\n\n"
    "<!-- AIDOR:STATUS=ISSUES_FOUND -->\n"
    '<!-- AIDOR:ISSUES={"critical":0,"major":"one","minor":0,"nit":0} -->\n'
    "<!-- AIDOR:PRODUCTION_READY=false -->\n"
)


def test_review_with_non_integer_count_fails_round_cleanly(
    run_config, monkeypatch: pytest.MonkeyPatch
):
    """Regression (review-0007): a syntactically valid AIDOR:ISSUES JSON
    payload with a non-integer count (e.g. `"major":"one"`) must surface as
    a `FooterParseError` and cause the orchestrator to fail the round
    cleanly, not crash with an uncaught `ValueError`/`TypeError`."""

    def content_for(runner):
        if runner.role == "reviewer":
            return BAD_COUNT_REVIEW
        raise AssertionError("fix phase must not run on an unparseable footer")

    _patch_phase_runner(monkeypatch, content_for)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 2, f"expected clean failure exit, got {code}"
    assert orch.state.status == "failed"
    rnd1 = orch.state.rounds[0]
    assert not any(p.name == "fix" for p in rnd1.phases)
    # review-0015: malformed-footer path must also downgrade the persisted
    # review phase, otherwise `--resume` re-enters the same clean failure
    # loop forever instead of re-running the review.
    review_phase = next(p for p in rnd1.phases if p.name == "review")
    assert review_phase.status == "failed", (
        f"review phase should be failed, got {review_phase.status}"
    )


# ---- Resume with malformed persisted state (review-0008) ------------------


def test_resume_with_done_review_phase_missing_artifact_path_fails_round(
    run_config, monkeypatch: pytest.MonkeyPatch
):
    """Regression (review-0008): on `--resume`, if a persisted `done` review
    phase is missing its `artifact_path` (stale/manual state file or future
    migration slip), the orchestrator must NOT crash with `TypeError` from
    `Path(None)`. It must take the same clean round-failure path used for
    unparseable footers (note + status=failed + exit 2)."""
    from aidor.state import PhaseRecord, RoundRecord, State

    state = State(status="running", current_round=1)
    state.rounds.append(
        RoundRecord(
            index=1,
            phases=[
                PhaseRecord(
                    name="review",
                    role="reviewer",
                    status="done",
                    artifact_path=None,  # malformed persisted state
                    stop_reason="end_turn",
                ),
            ],
        )
    )
    run_config.aidor_dir.mkdir(parents=True, exist_ok=True)
    state.save(run_config.state_path)
    run_config.resume = True

    def content_for(runner):
        if runner.role == "coder":
            raise AssertionError(
                "fix phase must not run when persisted review phase has no artifact_path"
            )
        return CLEAN_AND_READY_FOOTER

    _patch_phase_runner(monkeypatch, content_for)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 2, f"expected clean failure exit, got {code}"
    assert orch.state.status == "failed"
    rnd1 = orch.state.rounds[0]
    assert not any(p.name == "fix" for p in rnd1.phases)
    # The orchestrator should have logged a note explaining the failure.
    assert any("artifact_path" in n for n in orch.state.notes)
    # review-0014: the persisted `done` review phase whose artefact path
    # is missing must be downgraded to `failed` so the saved state never
    # claims a `done` phase with no usable artefact.
    review_phase = next(p for p in rnd1.phases if p.name == "review")
    assert review_phase.status == "failed"


# ---- Resume must reject out-of-repo artifact paths (review-0013) ----------


def test_resume_rejects_out_of_repo_review_artifact_path(
    run_config, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
):
    """Regression (review-0013): a hand-edited `state.json` that pins a
    persisted review `artifact_path` to a file OUTSIDE the repo root must
    be refused at resume time. The orchestrator must NOT call
    `Path(...).read_text()` on that path — that would violate the
    documented "never read or write files outside the repository root"
    guard."""
    from aidor.state import PhaseRecord, RoundRecord, State

    # Place a "secret" file in a separate tmp directory, NOT under the repo.
    outside_dir = tmp_path_factory.mktemp("outside_repo")
    outside = outside_dir / "outside_secret.md"
    outside.write_text(CLEAN_AND_READY_FOOTER, encoding="utf-8")
    assert not str(outside.resolve()).startswith(str(run_config.repo.resolve()))

    state = State(status="running", current_round=1)
    state.rounds.append(
        RoundRecord(
            index=1,
            phases=[
                PhaseRecord(
                    name="review",
                    role="reviewer",
                    status="done",
                    artifact_path=str(outside),  # malicious / corrupt
                    stop_reason="end_turn",
                ),
            ],
        )
    )
    run_config.aidor_dir.mkdir(parents=True, exist_ok=True)
    state.save(run_config.state_path)
    run_config.resume = True

    def content_for(runner):
        raise AssertionError("no phase must run when persisted artifact_path is outside the repo")

    _patch_phase_runner(monkeypatch, content_for)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 2, f"expected clean failure exit, got {code}"


def test_resume_rejects_out_of_repo_gate_artifact_path(
    run_config, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
):
    """Regression (review-0013): same containment requirement for the
    readiness-gate phase's persisted artifact_path."""
    from aidor.review_store import ReviewStore
    from aidor.state import PhaseRecord, RoundRecord, State

    store = ReviewStore(run_config.reviews_dir, run_config.fixes_dir)
    store.ensure_dirs()
    review_path = store.next_review_path()
    review_path.write_text(CLEAN_AND_READY_FOOTER, encoding="utf-8")

    outside_dir = tmp_path_factory.mktemp("outside_gate")
    outside = outside_dir / "outside_gate.md"
    outside.write_text(CLEAN_AND_READY_FOOTER, encoding="utf-8")
    assert not str(outside.resolve()).startswith(str(run_config.repo.resolve()))

    state = State(status="running", current_round=1)
    state.rounds.append(
        RoundRecord(
            index=1,
            phases=[
                PhaseRecord(
                    name="review",
                    role="reviewer",
                    status="done",
                    artifact_path=str(review_path),
                    stop_reason="end_turn",
                ),
                PhaseRecord(
                    name="readiness_gate",
                    role="reviewer",
                    status="done",
                    artifact_path=str(outside),  # corrupt
                    stop_reason="end_turn",
                ),
            ],
        )
    )
    run_config.aidor_dir.mkdir(parents=True, exist_ok=True)
    state.save(run_config.state_path)
    run_config.resume = True

    def content_for(runner):
        raise AssertionError(
            "no phase must run when persisted gate artifact_path is outside the repo"
        )

    _patch_phase_runner(monkeypatch, content_for)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 2


# ---- Fix-phase contract enforcement (review-0013) -------------------------


def test_fix_phase_failed_stop_reason_fails_round(run_config, monkeypatch: pytest.MonkeyPatch):
    """Regression (review-0013): if the coder turn ends with anything other
    than `end_turn` (e.g. idle, timeout, error → status="failed"), the
    orchestrator must NOT silently advance to the next round. It must fail
    the round cleanly so the next reviewer prompt does not reference a
    non-existent fixes artefact."""
    from aidor import phase as phase_mod
    from aidor.phase import PhaseResult
    from aidor.telemetry import PhaseMetrics

    call_count = {"reviewer": 0, "coder": 0}

    async def _fake_run(self):
        if self.role == "reviewer":
            call_count["reviewer"] += 1
            self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
            self.artifact_path.write_text(GATE_FOUND_ISSUES_FOOTER, encoding="utf-8")
            return PhaseResult(
                exit_code=0,
                stop_reason="end_turn",
                duration_s=0.01,
                transcript_path=self.transcript_path,
                otel_path=self.otel_path,
                metrics=PhaseMetrics(),
                restarts=[],
            )
        # Coder: simulate a timeout / idle exit. Do NOT write the fixes file.
        call_count["coder"] += 1
        return PhaseResult(
            exit_code=1,
            stop_reason="idle-timeout",
            duration_s=0.01,
            transcript_path=self.transcript_path,
            otel_path=self.otel_path,
            metrics=PhaseMetrics(),
            restarts=[],
        )

    monkeypatch.setattr(phase_mod.PhaseRunner, "run", _fake_run)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 2, f"expected clean failure exit, got {code}"
    assert orch.state.status == "failed"
    # Exactly one round, one reviewer + one (failed) coder call.
    assert len(orch.state.rounds) == 1
    assert call_count["reviewer"] == 1
    assert call_count["coder"] == 1
    rnd1 = orch.state.rounds[0]
    fix_phase = next(p for p in rnd1.phases if p.name == "fix")
    assert fix_phase.status == "failed"
    assert any("fix phase did not complete cleanly" in n for n in orch.state.notes)


def test_fix_phase_summaryless_end_turn_fails_round(run_config, monkeypatch: pytest.MonkeyPatch):
    """Regression (review-0013): if the coder ends its turn with end_turn
    but never writes the fixes-NNNN-*.md artefact, the orchestrator must
    not advance to a follow-up review that references a missing file."""
    from aidor import phase as phase_mod
    from aidor.phase import PhaseResult
    from aidor.telemetry import PhaseMetrics

    async def _fake_run(self):
        if self.role == "reviewer":
            self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
            self.artifact_path.write_text(GATE_FOUND_ISSUES_FOOTER, encoding="utf-8")
            return PhaseResult(
                exit_code=0,
                stop_reason="end_turn",
                duration_s=0.01,
                transcript_path=self.transcript_path,
                otel_path=self.otel_path,
                metrics=PhaseMetrics(),
                restarts=[],
            )
        # Coder ends turn but does NOT write the fixes file.
        return PhaseResult(
            exit_code=0,
            stop_reason="end_turn",
            duration_s=0.01,
            transcript_path=self.transcript_path,
            otel_path=self.otel_path,
            metrics=PhaseMetrics(),
            restarts=[],
        )

    monkeypatch.setattr(phase_mod.PhaseRunner, "run", _fake_run)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 2
    assert orch.state.status == "failed"
    rnd1 = orch.state.rounds[0]
    fix_phase = next(p for p in rnd1.phases if p.name == "fix")
    assert fix_phase.artifact_path is not None
    assert not Path(fix_phase.artifact_path).exists()
    assert any("artefact_exists=False" in n for n in orch.state.notes)
    # review-0014: phase status must be downgraded — `done` cannot
    # coexist with a missing artefact in the persisted state.
    assert fix_phase.status == "failed"


def test_resume_with_done_fix_phase_missing_artefact_fails_round(
    run_config, monkeypatch: pytest.MonkeyPatch
):
    """Regression (review-0013): on `--resume`, a persisted `done` fix
    phase whose artefact file no longer exists must fail the round
    cleanly rather than feed a missing path into the next reviewer
    prompt."""
    from aidor.review_store import ReviewStore
    from aidor.state import PhaseRecord, RoundRecord, State

    store = ReviewStore(run_config.reviews_dir, run_config.fixes_dir)
    store.ensure_dirs()
    review_path = store.next_review_path()
    review_path.write_text(GATE_FOUND_ISSUES_FOOTER, encoding="utf-8")

    # The fix path is recorded as "done" but the file does not exist.
    missing_fix = run_config.fixes_dir / "fixes-0001-missing.md"

    state = State(status="running", current_round=1)
    state.rounds.append(
        RoundRecord(
            index=1,
            phases=[
                PhaseRecord(
                    name="review",
                    role="reviewer",
                    status="done",
                    artifact_path=str(review_path),
                    stop_reason="end_turn",
                ),
                PhaseRecord(
                    name="fix",
                    role="coder",
                    status="done",
                    artifact_path=str(missing_fix),
                    stop_reason="end_turn",
                ),
            ],
        )
    )
    run_config.aidor_dir.mkdir(parents=True, exist_ok=True)
    state.save(run_config.state_path)
    run_config.resume = True

    def content_for(runner):
        raise AssertionError("no phase must run when persisted done fix phase has no artefact")

    _patch_phase_runner(monkeypatch, content_for)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 2
    assert orch.state.status == "failed"
    assert any("missing its artefact" in n for n in orch.state.notes)
    # review-0014: the persisted `done` fix phase must be downgraded to
    # `failed` so the saved state cannot show a `done` phase whose
    # artefact does not exist.
    rnd1 = orch.state.rounds[0]
    fix_phase = next(p for p in rnd1.phases if p.name == "fix")
    assert fix_phase.status == "failed"


# ---- Live review phase artefact-missing must downgrade phase (review-0014)


def test_review_phase_end_turn_without_file_marks_phase_failed(
    run_config, monkeypatch: pytest.MonkeyPatch
):
    """Regression (review-0014): if the reviewer ends its turn cleanly
    (`stop_reason="end_turn"`) but never wrote the review artefact, the
    orchestrator must NOT leave the persisted phase as `done` while the
    round is failed. `done` + missing artefact is an internally
    inconsistent state that breaks summary reporting and `--resume`."""
    from aidor import phase as phase_mod
    from aidor.phase import PhaseResult
    from aidor.telemetry import PhaseMetrics

    async def _fake_run(self):
        # Reviewer ends turn but never writes the file.
        return PhaseResult(
            exit_code=0,
            stop_reason="end_turn",
            duration_s=0.01,
            transcript_path=self.transcript_path,
            otel_path=self.otel_path,
            metrics=PhaseMetrics(),
            restarts=[],
        )

    monkeypatch.setattr(phase_mod.PhaseRunner, "run", _fake_run)

    orch = Orchestrator(run_config)
    code = asyncio.run(orch.run())

    assert code == 2
    assert orch.state.status == "failed"
    rnd1 = orch.state.rounds[0]
    review_phase = next(p for p in rnd1.phases if p.name == "review")
    assert review_phase.status == "failed", (
        f"review phase must be downgraded to failed when its artefact is "
        f"missing; got status={review_phase.status!r}"
    )
    assert review_phase.artifact_path is not None
    assert not Path(review_phase.artifact_path).exists()
