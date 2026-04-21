"""Single Copilot CLI invocation: build argv, spawn, stream JSONL, parse to
events, return a completion record.

The orchestrator drives one phase at a time (review / fix / readiness-gate).
Each phase gets its own subprocess and its own OTel JSONL file.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from aidor.config import RunConfig, RESTART_BACKOFF_S
from aidor.guard_profile import build_flags
from aidor.telemetry import PhaseMetrics, parse_otel_file


log = logging.getLogger(__name__)


# ---- Events emitted to the orchestrator ----------------------------------


@dataclass
class PhaseEvent:
    kind: str  # "stdout-json" | "stderr" | "exit" | "idle-warn" | "restart"
    data: Any = None


@dataclass
class PhaseResult:
    exit_code: int
    stop_reason: str  # "end_turn" | "error" | "timeout" | "aborted" | "idle" | "unknown"
    duration_s: float
    transcript_path: Path
    otel_path: Path
    metrics: PhaseMetrics
    restarts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


# ---- Runner --------------------------------------------------------------


class PhaseRunner:
    """Run a single Copilot phase with supervision.

    The runner owns the subprocess lifecycle, the idle watchdog, the round
    timeout, and the restart policy (up to `max_restarts_per_round`
    `copilot --continue` retries with exponential back-off).

    While a hook of ours is currently executing (e.g. waiting on a human),
    the idle + round timers are PAUSED via `hook_gate` — a simple counter
    the hook resolver writes to `.aidor/pending/.busy`.
    """

    def __init__(
        self,
        config: RunConfig,
        *,
        role: str,
        agent_name: str,
        prompt: str,
        phase_index: int,
        artifact_path: Path,
        on_event: Callable[[PhaseEvent], None] | None = None,
    ) -> None:
        self.config = config
        self.role = role
        self.agent_name = agent_name
        self.prompt = prompt
        self.phase_index = phase_index
        self.artifact_path = artifact_path
        self.on_event = on_event or (lambda _: None)

        self.transcript_path = (
            config.transcripts_dir / f"{role}-{phase_index:04d}.md"
        )
        self.otel_path = config.logs_dir / f"otel-{role}-{phase_index:04d}.jsonl"
        self.stderr_path = config.logs_dir / f"{role}-{phase_index:04d}.stderr"

    # ---- Public entry point -------------------------------------------------

    async def run(self) -> PhaseResult:
        self.otel_path.parent.mkdir(parents=True, exist_ok=True)
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        restarts: list[dict[str, Any]] = []
        stop_reason = "unknown"
        exit_code = -1
        error: str | None = None

        first_attempt = True
        attempts = 0
        while True:
            attempts += 1
            resume = not first_attempt
            first_attempt = False
            try:
                exit_code, stop_reason = await self._run_once(resume=resume)
                if stop_reason in ("end_turn", "aborted"):
                    break
                # Retriable outcomes: idle/timeout/error with recoverable exit.
                if attempts > self.config.max_restarts_per_round:
                    break
                backoff = RESTART_BACKOFF_S[min(len(restarts), len(RESTART_BACKOFF_S) - 1)]
                restarts.append(
                    {
                        "reason": stop_reason,
                        "backoff_s": backoff,
                        "at": _utcnow(),
                    }
                )
                self._emit(PhaseEvent("restart", data={"backoff_s": backoff, "reason": stop_reason}))
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                stop_reason = "aborted"
                error = "cancelled"
                raise
            except Exception as exc:  # pragma: no cover - defensive
                stop_reason = "error"
                error = str(exc)
                log.exception("phase %s-%d crashed", self.role, self.phase_index)
                break

        metrics = parse_otel_file(self.otel_path)
        return PhaseResult(
            exit_code=exit_code,
            stop_reason=stop_reason,
            duration_s=time.monotonic() - started,
            transcript_path=self.transcript_path,
            otel_path=self.otel_path,
            metrics=metrics,
            restarts=restarts,
            error=error,
        )

    # ---- Single-process attempt --------------------------------------------

    async def _run_once(self, *, resume: bool) -> tuple[int, str]:
        argv = self._build_argv(resume=resume)
        env = self._build_env()
        log.info("phase %s-%d: launching %s", self.role, self.phase_index, " ".join(argv))
        self._emit(PhaseEvent("spawn", data={"argv": argv, "resume": resume}))

        # Cross-platform clean-kill group setup.
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": env,
            "cwd": str(self.config.repo),
        }
        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP so we can CTRL_BREAK_EVENT the tree.
            kwargs["creationflags"] = 0x00000200  # type: ignore[assignment]
        else:
            kwargs["start_new_session"] = True

        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)

        stop_reason = "unknown"
        last_activity = time.monotonic()
        phase_start = last_activity

        async def read_stdout() -> None:
            nonlocal last_activity, stop_reason
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                last_activity = time.monotonic()
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    self._emit(PhaseEvent("stdout-text", data=text))
                    continue
                # Track stopReason opportunistically from the JSONL stream.
                sr = _deep_find(event, "stopReason") or _deep_find(event, "stop_reason")
                if isinstance(sr, str):
                    stop_reason = sr
                self._emit(PhaseEvent("stdout-json", data=event))

        async def read_stderr() -> None:
            assert proc.stderr is not None
            with self.stderr_path.open("ab") as f:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    f.write(chunk)

        async def watchdog() -> str:
            """Idle & round-timeout watchdog. Returns a stop_reason override
            when the process should be killed; empty string on clean exit."""
            nonlocal last_activity
            warned = False
            while proc.returncode is None:
                await asyncio.sleep(1.0)
                now = time.monotonic()
                if self._is_hook_busy():
                    # Pause timers while a hook (e.g. human wait) is running.
                    last_activity = now
                    continue
                if now - phase_start > self.config.round_timeout_s:
                    log.warning("phase %s-%d exceeded round timeout", self.role, self.phase_index)
                    return "timeout"
                idle_s = now - last_activity
                if idle_s > self.config.idle_timeout_s and not warned:
                    warned = True
                    self._emit(PhaseEvent("idle-warn", data={"idle_s": idle_s}))
                if idle_s > self.config.idle_timeout_s + 60:
                    log.warning("phase %s-%d idle too long", self.role, self.phase_index)
                    return "idle"
            return ""

        # Run all three tasks concurrently; kill-on-watchdog by cancelling.
        stdout_task = asyncio.create_task(read_stdout(), name=f"{self.role}-stdout")
        stderr_task = asyncio.create_task(read_stderr(), name=f"{self.role}-stderr")
        watchdog_task = asyncio.create_task(watchdog(), name=f"{self.role}-watchdog")

        done, pending = await asyncio.wait(
            {stdout_task, watchdog_task}, return_when=asyncio.FIRST_COMPLETED
        )

        watchdog_kill = ""
        if watchdog_task in done:
            watchdog_kill = watchdog_task.result() or ""
            if watchdog_kill:
                await self._terminate(proc)

        # Wait for the process to actually exit and drain the rest.
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:  # pragma: no cover - defensive
            await self._terminate(proc, force=True)
            await proc.wait()

        for t in (stdout_task, stderr_task, watchdog_task):
            if not t.done():
                t.cancel()
                with _suppress(asyncio.CancelledError, Exception):
                    await t

        exit_code = proc.returncode if proc.returncode is not None else -1
        if watchdog_kill:
            stop_reason = watchdog_kill
        elif exit_code == 0 and stop_reason == "unknown":
            # Exit 0 without seeing an explicit stopReason — treat as clean.
            stop_reason = "end_turn"
        elif exit_code != 0 and stop_reason in ("unknown", "end_turn"):
            stop_reason = "error"

        self._emit(PhaseEvent("exit", data={"code": exit_code, "stop_reason": stop_reason}))
        return exit_code, stop_reason

    # ---- Argv + env ---------------------------------------------------------

    def _build_argv(self, *, resume: bool) -> list[str]:
        cfg = self.config
        argv: list[str] = [
            cfg.copilot_binary,
            "-p",
            self.prompt,
            f"--agent={self.agent_name}",
            f"--model={cfg.model_for(self.role)}",
            "--autopilot",
            "--output-format=json",
            f"--share={self.transcript_path}",
            "--no-color",
        ]
        if resume:
            argv.append("--continue")
        argv.extend(build_flags(cfg.repo, allow_local_install=cfg.allow_local_install))
        return argv

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["AIDOR_REPO"] = str(self.config.repo)
        env["AIDOR_ROLE"] = self.role
        env["AIDOR_PHASE_INDEX"] = str(self.phase_index)
        env["COPILOT_OTEL_FILE_EXPORTER_PATH"] = str(self.otel_path)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    # ---- Hook-gate ----------------------------------------------------------

    def _is_hook_busy(self) -> bool:
        """A hook writes a marker file when it's blocked on the human."""
        pending = self.config.aidor_dir / "pending"
        if not pending.exists():
            return False
        # Presence of any .json request file (without matching .answer or .cancel)
        # means at least one hook is currently waiting.
        try:
            for p in pending.iterdir():
                if p.suffix == ".json":
                    ans = p.with_suffix(".answer")
                    cancel = p.with_suffix(".cancel")
                    if not ans.exists() and not cancel.exists():
                        return True
        except OSError:
            return False
        return False

    # ---- Process termination ------------------------------------------------

    async def _terminate(self, proc: asyncio.subprocess.Process, *, force: bool = False) -> None:
        if proc.returncode is not None:
            return
        try:
            if sys.platform == "win32":
                # CTRL_BREAK_EVENT on the process group; fall back to terminate.
                try:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                except Exception:
                    proc.terminate()
            else:
                proc.terminate()
            if force:
                await asyncio.sleep(1)
                proc.kill()
        except ProcessLookupError:
            return

    # ---- Emit ---------------------------------------------------------------

    def _emit(self, event: PhaseEvent) -> None:
        try:
            self.on_event(event)
        except Exception:  # pragma: no cover - defensive
            log.exception("phase %s-%d on_event handler raised", self.role, self.phase_index)


# ---- Helpers -------------------------------------------------------------


def _deep_find(obj: Any, key: str) -> Any:
    """Recursive dict/list search for the first occurrence of `key`."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _deep_find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _deep_find(v, key)
            if r is not None:
                return r
    return None


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _suppress:
    def __init__(self, *excs: type[BaseException]) -> None:
        self.excs = excs

    def __enter__(self) -> "_suppress":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001 - stdlib signature
        return exc_type is not None and issubclass(exc_type, self.excs)

    async def __aenter__(self) -> "_suppress":  # pragma: no cover
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # pragma: no cover
        return exc_type is not None and issubclass(exc_type, self.excs)
