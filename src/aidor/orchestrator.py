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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.prompt import Prompt

from aidor.bootstrap import bootstrap
from aidor.config import RunConfig
from aidor.phase import PhaseEvent, PhaseRunner, PhaseResult
from aidor.review_store import FooterParseError, ReviewFooter, ReviewStore, parse_footer
from aidor.state import PhaseRecord, RestartRecord, RoundRecord, State
from aidor.summary import print_summary, write_summary_md
from aidor.wake_lock import WakeLock


log = logging.getLogger(__name__)


CODER_AGENT = "aidor-coder"
REVIEWER_AGENT = "aidor-reviewer"


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
        self.review_store = ReviewStore(config.reviews_dir, config.fixes_dir)
        self._human_watcher_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._pending_seen: set[str] = set()

    # ---- Public entry point ------------------------------------------------

    async def run(self) -> int:
        self.console.rule("[bold]aidor[/bold] — AI Review Orchestrator")
        actions = bootstrap(self.config)
        for a in actions:
            log.info("bootstrap: %s", a)

        # Load or initialise state.
        if self.config.resume and self.config.state_path.exists():
            self.state = State.load(self.config.state_path)
            self.console.print(f"[dim]resumed from round {self.state.current_round}[/dim]")
        else:
            self.state = State(started_at=_utcnow(), status="running")

        self.state.status = "running"
        self._save_state()

        # Signal handlers — write state and break the loop.
        self._install_signals()

        # Background human-question watcher.
        self._human_watcher_task = asyncio.create_task(
            self._human_watcher(), name="human-watcher"
        )

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
        start_round = self.state.current_round if self.state.current_round > 0 else 0

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
                review_path = Path(review_phase.artifact_path) if review_phase.artifact_path else self.review_store.next_review_path()
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
                    self.state.status = "failed"
                    return 2

            # Parse footer.
            review_path = Path(review_phase.artifact_path)
            footer: ReviewFooter | None
            try:
                footer = parse_footer(review_path.read_text(encoding="utf-8"))
            except (FooterParseError, OSError) as exc:
                self._note(f"round {round_index}: footer parse failed: {exc}")
                footer = None
            rnd.footer = footer.to_dict() if footer else None
            self._save_state()

            # Convergence check.
            if footer and footer.is_clean_and_ready:
                # Readiness-gate pass (new round record? No — reuse current slot as
                # a "readiness_gate" phase or short-circuit).
                self.console.print("[green]reviewer says CLEAN + PRODUCTION_READY — running readiness gate[/green]")
                gate_phase = _get_or_create_phase(rnd, "readiness_gate", "reviewer")
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
                    gate_footer: ReviewFooter | None = None
                    if gate_path.exists():
                        try:
                            gate_footer = parse_footer(gate_path.read_text(encoding="utf-8"))
                        except FooterParseError as exc:
                            self._note(f"round {round_index}: readiness-gate footer invalid: {exc}")
                    if gate_footer and gate_footer.is_clean_and_ready:
                        self.state.status = "converged"
                        self._save_state()
                        self.console.print("[bold green]CONVERGED[/bold green]")
                        return 0
                    if gate_footer:
                        rnd.footer = gate_footer.to_dict()
                        # Route the coder to the GATE'S review (the original
                        # one was clean; only the gate found new issues).
                        footer = gate_footer
                        review_path = gate_path
                    self._note(f"round {round_index}: readiness gate found issues; continuing")

            # Otherwise, run the fix phase.
            if footer is None:
                self._note(f"round {round_index}: review footer missing; treating as failure")
                self.state.status = "failed"
                return 2

            fix_phase = _get_or_create_phase(rnd, "fix", "coder")
            if fix_phase.status != "done":
                fixes_path = self.review_store.next_fix_path()
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
        phase_record.status = "done" if result.stop_reason in ("end_turn",) else "failed"
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
                    asyncio.create_task(
                        self._prompt_human(req), name=f"prompt-{key}"
                    )
            except OSError:  # pragma: no cover
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
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

        self.console.print()
        self.console.rule(f"[bold magenta]Human input needed[/bold magenta] ({role}/{classification})")
        self.console.print(question)
        if context:
            self.console.print(f"[dim]context: {json.dumps(context, ensure_ascii=False)}[/dim]")

        # Blocking prompt — run in a thread so we don't starve the event loop.
        answer = await asyncio.to_thread(
            Prompt.ask, "[cyan]answer[/cyan]", console=self.console, default=""
        )
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
        # Follow-up: resolve previous artefacts.
        prev = self.state.rounds[round_index - 2]
        prev_review = next((p.artifact_path for p in prev.phases if p.name == "review"), "")
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

        def _handler(sig):  # noqa: ANN001
            self.console.print(f"[yellow]received signal {sig}; shutting down...[/yellow]")
            self._stop.set()

        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _handler, sig.name)
                except (NotImplementedError, RuntimeError):  # pragma: no cover
                    pass


def _get_or_create_phase(rnd: RoundRecord, name: str, role: str) -> PhaseRecord:
    for p in rnd.phases:
        if p.name == name:
            return p
    p = PhaseRecord(name=name, role=role)  # type: ignore[arg-type]
    rnd.phases.append(p)
    return p


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
