"""The main state machine: bootstrap → (review → fix)* → readiness-gate → done.

Also owns:
  - the human-question watcher (polls .aidor/pending/ and prompts TTY)
  - writes state.json after every transition
  - assembles PhaseRecord entries from PhaseRunner results
  - decides convergence / non-convergence / abort
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from rich.console import Console
from rich.prompt import Prompt

from aidor.bootstrap import bootstrap
from aidor.config import RunConfig, read_artifact_text
from aidor.phase import PhaseEvent, PhaseResult, PhaseRunner
from aidor.preflight import compute_warnings, render_warnings
from aidor.review_store import FooterParseError, ReviewFooter, ReviewStore, parse_footer
from aidor.state import (
    PhaseRecord,
    RestartRecord,
    RoundRecord,
    State,
    validate_artifact_paths_within_repo,
)
from aidor.summary import print_summary, write_summary_md
from aidor.wake_lock import WakeLock

log = logging.getLogger(__name__)


CODER_AGENT = "aidor-coder"
REVIEWER_AGENT = "aidor-reviewer"


# -- Shared helpers ------------------------------------------------------------


def write_abort_marker(aidor_dir: Path, via: str) -> bool:
    """Write `.aidor/ABORT` so the phase watchdog and hook resolver can
    observe the abort. Shared between the signal path (POSIX and Windows)
    and the CLI's `KeyboardInterrupt` path so a Ctrl-C on any platform
    honours the documented abort contract. Returns True iff the marker
    was written by this call (idempotent when the marker already exists).
    """
    try:
        aidor_dir.mkdir(parents=True, exist_ok=True)
        marker = aidor_dir / "ABORT"
        if marker.exists():
            return False
        marker.write_text(
            f"aborted_via={via} at={_utcnow()}\n",
            encoding="utf-8",
        )
        return True
    except OSError:  # pragma: no cover - defensive
        log.exception("failed to write .aidor/ABORT (via=%s)", via)
        return False


# -- Prompt templates ----------------------------------------------------------


REVIEW_PROMPT_INITIAL = """\
You are running as the aidor-reviewer agent for round {round_index}.

Repository: {repo}

Your task:
1. Review the current state of the repository against the rules in AGENTS.md
   and the coder's latest fixes (if any).
2. Produce a review file at {review_path} following the format in
   AGENTS.md — including the three AIDOR footer lines at the end.
3. Do NOT modify source files. Reviews only.

When the review file is fully written, end your turn.
"""

REVIEW_PROMPT_FOLLOWUP = """\
You are running as the aidor-reviewer agent for round {round_index}.

The previous review was {prev_review_path}.
The coder's fixes for that review are at {prev_fixes_path}.

Review the NEW state of the repository and produce the next review at
{review_path}, including the AIDOR footer. Focus on:
  1. Whether the previously-flagged issues are actually resolved.
  2. Any regressions introduced by the fixes.
  3. New issues visible now.

Do NOT modify source files. End your turn when the review file is written.
"""

FIX_PROMPT = """\
You are running as the aidor-coder agent for round {round_index}.

Read the review at {review_path}.

Address every critical and major issue. Prefer minimal, surgical edits;
do not refactor unrelated code. When a requirement is genuinely ambiguous,
use `ask_user` — the orchestrator will respond.

When your fixes are complete, write a summary of what you did at
{fixes_path} and end your turn.
"""

READINESS_PROMPT = """\
You are running as the aidor-reviewer agent — FINAL READINESS GATE.

All previous issues appear resolved. Perform one last pass focused on
production-readiness: obvious bugs, unhandled edge cases, security
smells, and whether AGENTS.md acceptance criteria are met.

Write the final review at {review_path} with the AIDOR footer.
If clean AND production-ready, emit PRODUCTION_READY=true.
Do NOT modify source files. End your turn when written.
"""


# -- Orchestrator --------------------------------------------------------------


class Orchestrator:
    def __init__(self, config: RunConfig, *, console: Console | None = None) -> None:
        self.config = config
        self.console = console or Console()
        self.state: State = State()
        self.review_store = ReviewStore(
            config.reviews_dir, config.fixes_dir, max_artifact_mb=config.max_artifact_mb
        )
        self._human_watcher_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._pending_seen: set[str] = set()

    # ---- Public entry point ------------------------------------------------

    async def run(self) -> int:
        self.console.rule("[bold]aidor[/bold] — AI Review Orchestrator")
        actions = bootstrap(self.config)
        for a in actions:
            log.info("bootstrap: %s", a)

        # Advisory pre-run warnings (platform mismatch, very large repos).
        # Non-blocking — operator owns budget and platform selection.
        rendered = render_warnings(compute_warnings(self.config.repo))
        if rendered:
            self.console.print(rendered)

        # Load or initialise state.
        if self.config.resume and self.config.state_path.exists():
            try:
                self.state = State.load(self.config.state_path)
            except (ValueError, OSError) as exc:
                self.console.print(
                    f"[red]could not resume: {self.config.state_path} is corrupt "
                    f"or unreadable ({exc})[/red]"
                )
                return 2
            # review-0013: containment check on persisted artifact paths.
            # A corrupt or hand-edited state.json must not be allowed to
            # point the orchestrator at files outside the repo root.
            path_err = validate_artifact_paths_within_repo(self.state, self.config.repo)
            if path_err:
                self.console.print(
                    f"[red]could not resume: {self.config.state_path} references "
                    f"an artefact outside the repository ({path_err})[/red]"
                )
                return 2
            self.console.print(f"[dim]resumed from round {self.state.current_round}[/dim]")
        else:
            self.state = State(started_at=_utcnow(), status="running")

        self.state.status = "running"
        self._save_state()

        # Signal handlers — write state and break the loop.
        self._install_signals()

        # Background human-question watcher.
        self._human_watcher_task = asyncio.create_task(self._human_watcher(), name="human-watcher")

        exit_code = 1
        try:
            with WakeLock(enabled=self.config.keep_awake):
                exit_code = await self._main_loop()
        finally:
            self._stop.set()
            if self._human_watcher_task:
                self._human_watcher_task.cancel()
                try:
                    await self._human_watcher_task
                except (asyncio.CancelledError, Exception):
                    pass
            self.state.ended_at = _utcnow()
            self._save_state()
            try:
                write_summary_md(self.state, self.config.summary_path)
            except Exception:  # pragma: no cover - defensive
                log.exception("failed to write summary.md")
            self.console.print()
            print_summary(self.state, self.console)
            self.console.print(f"[dim]summary: {self.config.summary_path}[/dim]")
        return exit_code

    # ---- Main loop ----------------------------------------------------------

    async def _main_loop(self) -> int:
        # `current_round` is persisted as the index of the round currently in
        # flight (set by `State.start_round()` *before* its phases run). On
        # `--resume`, we must therefore re-enter that same round so any
        # incomplete phases can finish; starting at `current_round` itself
        # would skip an unfinished round entirely. The per-phase
        # `_get_or_create_phase` + `status == "done"` checks below handle the
        # case where the round was actually fully complete and we should fall
        # straight through to the next one.
        start_round = max(0, self.state.current_round - 1)

        for i in range(start_round, self.config.max_rounds):
            round_index = i + 1
            self.console.rule(f"Round {round_index} / {self.config.max_rounds}")

            # Obtain / reuse the round record.
            if len(self.state.rounds) < round_index:
                rnd = self.state.start_round()
            else:
                rnd = self.state.rounds[round_index - 1]
                self.state.current_round = round_index
            self._save_state()

            # ---- Review phase -----------------------------------------------
            review_phase = _get_or_create_phase(rnd, "review", "reviewer")
            if review_phase.status not in ("done",):
                review_path = (
                    Path(review_phase.artifact_path)
                    if review_phase.artifact_path
                    else self.review_store.next_review_path()
                )
                review_phase.artifact_path = str(review_path)
                prompt = self._review_prompt(round_index=round_index, review_path=review_path)
                result = await self._run_phase(
                    role="reviewer",
                    agent=REVIEWER_AGENT,
                    prompt=prompt,
                    phase_record=review_phase,
                    phase_index=round_index,
                    artifact_path=review_path,
                )
                if result.stop_reason == "aborted":
                    self.state.status = "aborted"
                    return 130
                if not review_path.exists():
                    self._note(f"round {round_index}: reviewer produced no file; aborting")
                    # review-0014: do not leave the phase persisted as
                    # `done` when its required artefact is missing — that
                    # makes `state.json` internally inconsistent and turns
                    # `--resume` into a repeat clean failure.
                    review_phase.status = "failed"
                    self.state.status = "failed"
                    self._save_state()
                    return 2

            # Parse footer.
            footer: ReviewFooter | None
            review_path_str = review_phase.artifact_path
            if not review_path_str:
                # Defensive: a persisted "done" review phase with no
                # `artifact_path` (stale/manual edit, future migration slip)
                # would otherwise crash on `Path(None)`. Treat it like an
                # unparseable footer so the round fails cleanly below.
                self._note(
                    f"round {round_index}: persisted review phase missing "
                    "artifact_path; treating footer as unparseable"
                )
                footer = None
                # Bind review_path to a sentinel so static analysis can
                # see it is always defined when the fix phase below
                # runs. The `if footer is None: return 2` guard below
                # ensures we never actually use this sentinel value.
                review_path = Path()
            else:
                review_path = Path(review_path_str)
                try:
                    footer = parse_footer(
                        read_artifact_text(review_path, self.config.max_artifact_mb)
                    )
                except (FooterParseError, OSError) as exc:
                    self._note(f"round {round_index}: footer parse failed: {exc}")
                    footer = None
            rnd.footer = footer.to_dict() if footer else None
            self._save_state()

            # Convergence check.
            if footer and footer.is_clean_and_ready:
                # Readiness-gate pass (new round record? No — reuse current slot as
                # a "readiness_gate" phase or short-circuit).
                self.console.print(
                    "[green]reviewer says CLEAN + PRODUCTION_READY — running readiness gate[/green]"
                )
                gate_phase = _get_or_create_phase(rnd, "readiness_gate", "reviewer")
                gate_path: Path | None = None
                if gate_phase.status != "done":
                    gate_path = self.review_store.next_review_path()
                    gate_phase.artifact_path = str(gate_path)
                    prompt = READINESS_PROMPT.format(review_path=gate_path)
                    result = await self._run_phase(
                        role="reviewer",
                        agent=REVIEWER_AGENT,
                        prompt=prompt,
                        phase_record=gate_phase,
                        phase_index=round_index + 1000,  # disambiguate transcript name
                        artifact_path=gate_path,
                    )
                    if result.stop_reason == "aborted":
                        self.state.status = "aborted"
                        return 130
                elif gate_phase.artifact_path:
                    # Resume path: the gate already ran in a prior process. Use
                    # the gate's own review file as the authoritative reviewer
                    # artefact for this round, NOT the (now-stale) original
                    # clean review.
                    gate_path = Path(gate_phase.artifact_path)

                gate_footer: ReviewFooter | None = None
                gate_footer_error: str | None = None
                if gate_path is None or not gate_path.exists():
                    gate_footer_error = "readiness-gate artefact missing"
                else:
                    try:
                        gate_footer = parse_footer(
                            read_artifact_text(gate_path, self.config.max_artifact_mb)
                        )
                    except FooterParseError as exc:
                        gate_footer_error = f"readiness-gate footer invalid: {exc}"
                    except OSError as exc:
                        gate_footer_error = f"readiness-gate artefact unreadable: {exc}"
                if gate_footer and gate_footer.is_clean_and_ready:
                    self.state.status = "converged"
                    self._save_state()
                    self.console.print("[bold green]CONVERGED[/bold green]")
                    return 0
                if gate_footer:
                    rnd.footer = gate_footer.to_dict()
                    # Route the coder to the GATE'S review (the original
                    # one was clean; only the gate found new issues).
                    # gate_path is non-None here: the only branch above
                    # that produces a truthy gate_footer also wrote to
                    # gate_path (the `gate_path is None or not exists`
                    # branch sets gate_footer_error and leaves
                    # gate_footer as None).
                    assert gate_path is not None
                    footer = gate_footer
                    review_path = gate_path
                    self._save_state()
                    self._note(f"round {round_index}: readiness gate found issues; continuing")
                else:
                    # Mirror the primary-review failure path (see below):
                    # an unparseable readiness-gate footer is a control-flow
                    # hazard — we must not silently fall back to the stale
                    # original clean review and tell the coder to fix it.
                    self._note(
                        f"round {round_index}: {gate_footer_error}; treating round as failed"
                    )
                    # review-0014: keep the persisted phase status in sync
                    # with the round outcome so `state.json` cannot show a
                    # `done` readiness gate with no usable artefact.
                    gate_phase.status = "failed"
                    self.state.status = "failed"
                    self._save_state()
                    return 2

            # Otherwise, run the fix phase.
            if footer is None:
                self._note(f"round {round_index}: review footer missing; treating as failure")
                # review-0014/0015: keep the persisted phase status in
                # sync with the round outcome. Whether the artefact was
                # missing, unreadable, or present-but-malformed, the
                # round is failing because the review phase did not
                # produce a usable footer — so `state.json` must not
                # show a `done` review phase, or `--resume` will skip
                # straight back into the same clean failure loop
                # instead of re-running the review.
                review_phase.status = "failed"
                self.state.status = "failed"
                self._save_state()
                return 2

            fix_phase = _get_or_create_phase(rnd, "fix", "coder")
            if fix_phase.status != "done":
                fixes_path = (
                    Path(fix_phase.artifact_path)
                    if fix_phase.artifact_path
                    else self.review_store.next_fix_path()
                )
                fix_phase.artifact_path = str(fixes_path)
                prompt = FIX_PROMPT.format(
                    round_index=round_index,
                    review_path=review_path,
                    fixes_path=fixes_path,
                )
                result = await self._run_phase(
                    role="coder",
                    agent=CODER_AGENT,
                    prompt=prompt,
                    phase_record=fix_phase,
                    phase_index=round_index,
                    artifact_path=fixes_path,
                )
                if result.stop_reason == "aborted":
                    self.state.status = "aborted"
                    return 130
                # review-0013: mirror the reviewer-side artefact enforcement
                # for the fix phase. PhaseRunner can return idle/timeout/error
                # which sets status="failed"; or the coder may end_turn
                # without actually writing the fixes file. Either way the
                # next reviewer prompt would point at a missing/empty
                # artefact, breaking the documented audit trail. Fail the
                # round cleanly instead of silently advancing.
                if result.stop_reason != "end_turn" or not fixes_path.exists():
                    self._note(
                        f"round {round_index}: fix phase did not complete "
                        f"cleanly (stop_reason={result.stop_reason!r}, "
                        f"artefact_exists={fixes_path.exists()}); "
                        f"treating round as failed"
                    )
                    # review-0014: when the coder ends with `end_turn` but
                    # never produced the fixes artefact, `_run_phase` has
                    # already persisted status="done". Downgrade it so the
                    # persisted state cannot show a `done` fix phase with
                    # a missing artefact.
                    fix_phase.status = "failed"
                    self.state.status = "failed"
                    self._save_state()
                    return 2
            else:
                # Resume path: the fix phase already ran in a prior process.
                # Validate the persisted artefact still exists so the next
                # reviewer prompt does not reference a vanished file.
                persisted = fix_phase.artifact_path
                if not persisted or not Path(persisted).exists():
                    self._note(
                        f"round {round_index}: persisted done fix phase is "
                        f"missing its artefact ({persisted!r}); treating "
                        f"round as failed"
                    )
                    # review-0014: a persisted `done` fix phase whose
                    # artefact has vanished is internally inconsistent —
                    # mark it failed so a re-resume sees a coherent state.
                    fix_phase.status = "failed"
                    self.state.status = "failed"
                    self._save_state()
                    return 2

            self._save_state()

        # Max rounds exhausted.
        self.state.status = "unconverged"
        self.console.print("[yellow]max rounds reached without convergence[/yellow]")
        return 3

    # ---- Phase execution ---------------------------------------------------

    async def _run_phase(
        self,
        *,
        role: str,
        agent: str,
        prompt: str,
        phase_record: PhaseRecord,
        phase_index: int,
        artifact_path: Path,
    ) -> PhaseResult:
        phase_record.status = "running"
        phase_record.started_at = _utcnow()
        self._save_state()

        self.console.print(f"[cyan]→ {role}[/cyan] phase (agent: {agent})")

        runner = PhaseRunner(
            config=self.config,
            role=role,
            agent_name=agent,
            prompt=prompt,
            phase_index=phase_index,
            artifact_path=artifact_path,
            on_event=self._on_phase_event,
        )
        try:
            result = await runner.run()
        except asyncio.CancelledError:
            phase_record.status = "aborted"
            phase_record.ended_at = _utcnow()
            phase_record.stop_reason = "aborted"
            self._save_state()
            raise

        phase_record.ended_at = _utcnow()
        phase_record.duration_s = result.duration_s
        phase_record.stop_reason = result.stop_reason
        phase_record.transcript_path = str(result.transcript_path)
        phase_record.otel_path = str(result.otel_path)
        phase_record.tokens_in = result.metrics.tokens_in
        phase_record.tokens_out = result.metrics.tokens_out
        phase_record.cost = result.metrics.cost
        phase_record.tool_calls = result.metrics.tool_calls
        phase_record.restarts = [RestartRecord(**r) for r in result.restarts]
        if result.stop_reason == "aborted":
            phase_record.status = "aborted"
        elif result.stop_reason == "end_turn":
            phase_record.status = "done"
        else:
            phase_record.status = "failed"
        self._save_state()

        self.console.print(
            f"  done: stop={result.stop_reason} · "
            f"dur={_fmt_dur(result.duration_s)} · "
            f"tok={result.metrics.tokens_in}/{result.metrics.tokens_out} · "
            f"tools={result.metrics.tool_calls}"
        )
        return result

    def _on_phase_event(self, event: PhaseEvent) -> None:
        # Keep the stream low-chatter; forward key events.
        if event.kind == "idle-warn":
            self.console.print(f"  [yellow]idle {int(event.data['idle_s'])}s[/yellow]")
        elif event.kind == "restart":
            self.console.print(
                f"  [yellow]restart (reason={event.data['reason']}, "
                f"backoff={event.data['backoff_s']}s)[/yellow]"
            )

    # ---- Human-question watcher --------------------------------------------

    async def _human_watcher(self) -> None:
        """Watch `.aidor/pending/*.json` and prompt the TTY for each."""
        pending_dir = self.config.aidor_dir / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)

        while not self._stop.is_set():
            try:
                for req in sorted(pending_dir.glob("*.json")):
                    key = req.name
                    if key in self._pending_seen:
                        continue
                    ans = req.with_suffix(".answer")
                    cancel = req.with_suffix(".cancel")
                    if ans.exists() or cancel.exists():
                        self._pending_seen.add(key)
                        continue
                    self._pending_seen.add(key)
                    asyncio.create_task(self._prompt_human(req), name=f"prompt-{key}")
            except OSError:  # pragma: no cover
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except TimeoutError:
                continue

    async def _prompt_human(self, req_path: Path) -> None:
        try:
            payload = json.loads(req_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read pending request %s: %s", req_path, exc)
            return

        question = payload.get("question", "(no question provided)")
        role = payload.get("role", "?")
        classification = payload.get("classification", "unknown")
        context = payload.get("context", {})
        cancel_path = req_path.with_suffix(".cancel")

        self.console.print()
        self.console.rule(
            f"[bold magenta]Human input needed[/bold magenta] ({role}/{classification})"
        )
        # Indent the question body so it's visually distinct from the rest
        # of the orchestrator's chatter.
        for line in str(question).splitlines() or [""]:
            self.console.print(f"  {line}")
        if context:
            self.console.print(f"  [dim]context: {json.dumps(context, ensure_ascii=False)}[/dim]")
        self.console.print(
            "  [dim](Ctrl-C to cancel this question and let the agent decide.)[/dim]"
        )

        # Blocking prompt — run in a thread so we don't starve the event loop.
        # Catch KeyboardInterrupt INSIDE the thread so it doesn't propagate to
        # the orchestrator and abort the whole run; instead, write a .cancel
        # marker so the hook resolver returns "__CANCELLED__".
        def _ask() -> str | None:
            try:
                return Prompt.ask("[cyan]answer[/cyan]", console=self.console, default="")
            except (KeyboardInterrupt, EOFError):
                return None

        answer = await asyncio.to_thread(_ask)

        if answer is None:
            cancel_path.write_text(
                json.dumps({"cancelled_at": _utcnow()}, ensure_ascii=False),
                encoding="utf-8",
            )
            self.console.print("  [yellow]question cancelled — agent will fall back[/yellow]")
        else:
            ans_path = req_path.with_suffix(".answer")
            ans_path.write_text(
                json.dumps({"answer": answer, "answered_at": _utcnow()}, ensure_ascii=False),
                encoding="utf-8",
            )
        self.console.rule()

    # ---- Utilities ----------------------------------------------------------

    def _review_prompt(self, *, round_index: int, review_path: Path) -> str:
        if round_index == 1:
            return REVIEW_PROMPT_INITIAL.format(
                round_index=round_index,
                repo=self.config.repo,
                review_path=review_path,
            )
        # Follow-up: resolve previous artefacts. If the previous round ran a
        # readiness gate (because the initial review was clean but the gate
        # found new issues), THAT is the review the coder actually fixed —
        # so it's the one to reference here, not the now-stale original.
        prev = self.state.rounds[round_index - 2]
        gate = next(
            (
                p
                for p in prev.phases
                if p.name == "readiness_gate" and p.status == "done" and p.artifact_path
            ),
            None,
        )
        review = next((p for p in prev.phases if p.name == "review"), None)
        latest_reviewer = gate or review
        prev_review = latest_reviewer.artifact_path if latest_reviewer else ""
        prev_fixes = next((p.artifact_path for p in prev.phases if p.name == "fix"), "")
        return REVIEW_PROMPT_FOLLOWUP.format(
            round_index=round_index,
            prev_review_path=prev_review,
            prev_fixes_path=prev_fixes,
            review_path=review_path,
        )

    def _save_state(self) -> None:
        try:
            self.state.save(self.config.state_path)
        except Exception:  # pragma: no cover - defensive
            log.exception("failed to save state.json")

    def _note(self, msg: str) -> None:
        self.state.notes.append(f"{_utcnow()} {msg}")
        self.console.print(f"[dim]note: {msg}[/dim]")
        self._save_state()

    def _install_signals(self) -> None:
        loop = asyncio.get_event_loop()

        def _handler(sig: str) -> None:
            self.console.print(f"[yellow]received signal {sig}; shutting down...[/yellow]")
            # Write the abort marker so the PhaseRunner watchdog terminates
            # the current Copilot subprocess promptly, and any waiting hook
            # resolver returns a __CANCELLED__ sentinel.
            write_abort_marker(self.config.aidor_dir, f"signal:{sig}")
            self._stop.set()

        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _handler, sig.name)
                except (NotImplementedError, RuntimeError):  # pragma: no cover
                    pass
        else:
            # Windows: asyncio's ProactorEventLoop does not support
            # `add_signal_handler`, but `signal.signal` in the main thread
            # still catches SIGINT (Ctrl-C) and SIGBREAK (Ctrl-Break). We
            # use it purely to write the abort marker — the subsequent
            # KeyboardInterrupt propagates out of `asyncio.run` and the CLI
            # entry point handles the actual teardown.
            def _win_signal_handler(sig: int, _frame: FrameType | None) -> None:
                write_abort_marker(self.config.aidor_dir, f"signal:{sig}")
                # Re-raise as KeyboardInterrupt so the default control flow
                # (asyncio.run -> CLI KeyboardInterrupt catch) still runs.
                raise KeyboardInterrupt

            for sig_name in ("SIGINT", "SIGBREAK"):
                sig = getattr(signal, sig_name, None)
                if sig is None:
                    continue
                try:
                    signal.signal(sig, _win_signal_handler)
                except (ValueError, OSError):  # pragma: no cover
                    pass


def _get_or_create_phase(rnd: RoundRecord, name: str, role: str) -> PhaseRecord:
    for p in rnd.phases:
        if p.name == name:
            return p
    p = PhaseRecord(name=name, role=role)  # type: ignore[arg-type]
    rnd.phases.append(p)
    return p


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"
