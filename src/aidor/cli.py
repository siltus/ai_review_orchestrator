"""Command-line interface for aidor."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sys
from pathlib import Path

import typer
from rich.console import Console

from aidor import __version__
from aidor.bootstrap import bootstrap
from aidor.config import (
    DEFAULT_IDLE_TIMEOUT_S,
    DEFAULT_MAX_RESTARTS_PER_ROUND,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_ROUND_TIMEOUT_S,
    RunConfig,
)
from aidor.orchestrator import Orchestrator
from aidor.state import State
from aidor.summary import print_summary, write_summary_md

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="AI Review Orchestrator — drive Copilot CLI through review↔fix cycles.",
)

console = Console()


# Minimum GitHub Copilot CLI version aidor is tested against. Keep in sync
# with the prerequisites table in GETTING_STARTED.md. `aidor doctor` fails
# when the installed CLI is below this floor because older builds lack the
# hook / JSON / agent behaviour the orchestrator depends on.
MIN_COPILOT_VERSION: tuple[int, int, int] = (1, 0, 32)
_COPILOT_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse_copilot_version(text: str) -> tuple[int, int, int] | None:
    """Extract the first ``MAJOR.MINOR.PATCH`` triple from Copilot's version
    output. Copilot has shipped strings like ``copilot 1.0.35`` and
    ``1.0.35-2`` across releases; we only care about the leading numeric
    triple. Returns ``None`` when no recognisable version is present."""
    match = _COPILOT_VERSION_RE.search(text or "")
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _load_state_or_exit(state_path: Path) -> State:
    """Load state.json with operator-friendly errors instead of tracebacks."""
    try:
        return State.load(state_path)
    except (ValueError, OSError) as exc:
        console.print(f"[red]could not load {state_path}: {exc}[/red]")
        raise typer.Exit(code=2) from exc


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"aidor {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Enable debug logging."),
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ---- run -----------------------------------------------------------------


@app.command()
def run(
    coder: str = typer.Option(
        ..., "--coder", help="Coder model id (verbatim Copilot model string)."
    ),
    reviewer: str = typer.Option(..., "--reviewer", help="Reviewer model id."),
    repo: Path = typer.Option(Path.cwd(), "--repo", help="Repository root.", resolve_path=True),
    max_rounds: int = typer.Option(DEFAULT_MAX_ROUNDS, "--max-rounds"),
    idle_timeout: int = typer.Option(DEFAULT_IDLE_TIMEOUT_S, "--idle-timeout", help="Seconds."),
    round_timeout: int = typer.Option(DEFAULT_ROUND_TIMEOUT_S, "--round-timeout", help="Seconds."),
    max_restarts: int = typer.Option(DEFAULT_MAX_RESTARTS_PER_ROUND, "--max-restarts"),
    allow_local_install: bool = typer.Option(
        True,
        "--allow-local-install/--no-allow-local-install",
        help="Allow repo-scoped package installs when a lockfile is detected.",
    ),
    keep_awake: bool = typer.Option(True, "--keep-awake/--no-keep-awake"),
    resume: bool = typer.Option(False, "--resume", help="Resume from existing state.json."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    copilot_binary: str = typer.Option(
        "copilot", "--copilot-binary", help="Path to copilot binary."
    ),
) -> None:
    """Run the full review↔fix loop until convergence, abort, or max rounds."""
    config = RunConfig(
        repo=repo,
        coder_model=coder,
        reviewer_model=reviewer,
        max_rounds=max_rounds,
        idle_timeout_s=idle_timeout,
        round_timeout_s=round_timeout,
        max_restarts_per_round=max_restarts,
        allow_local_install=allow_local_install,
        keep_awake=keep_awake,
        resume=resume,
        dry_run=dry_run,
        copilot_binary=copilot_binary,
    )
    if dry_run:
        actions = bootstrap(config)
        for a in actions:
            console.print(f"[dim]bootstrap:[/dim] {a}")
        console.print("[green]dry-run complete[/green]")
        raise typer.Exit(code=0)

    orchestrator = Orchestrator(config, console=console)
    try:
        code = asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        # Ensure the abort contract holds even when the interrupt arrives
        # before/after `Orchestrator._install_signals` owns the handler —
        # e.g. on Windows where Ctrl-C propagates as KeyboardInterrupt out
        # of `asyncio.run`. Writing the marker is idempotent.
        from aidor.orchestrator import write_abort_marker

        write_abort_marker(config.aidor_dir, "keyboard_interrupt")
        console.print("[yellow]interrupted[/yellow]")
        code = 130
    raise typer.Exit(code=code)


# ---- status --------------------------------------------------------------


@app.command()
def status(
    repo: Path = typer.Option(Path.cwd(), "--repo", resolve_path=True),
) -> None:
    """Print current run state from .aidor/state.json."""
    state_path = repo / ".aidor" / "state.json"
    if not state_path.exists():
        console.print("[red]no .aidor/state.json found[/red]")
        raise typer.Exit(code=1)
    state = _load_state_or_exit(state_path)
    console.print(f"status: [bold]{state.status}[/bold]")
    console.print(f"rounds: {len(state.rounds)}")
    console.print(f"started: {state.started_at or '—'}  ended: {state.ended_at or '—'}")
    print_summary(state, console)


# ---- summary -------------------------------------------------------------


@app.command()
def summary(
    repo: Path = typer.Option(Path.cwd(), "--repo", resolve_path=True),
    write: bool = typer.Option(
        True, "--write/--no-write", help="Write summary.md as well as print."
    ),
) -> None:
    """Render the summary table; optionally (re)write summary.md."""
    state_path = repo / ".aidor" / "state.json"
    if not state_path.exists():
        console.print("[red]no .aidor/state.json found[/red]")
        raise typer.Exit(code=1)
    state = _load_state_or_exit(state_path)
    print_summary(state, console)
    if write:
        out = repo / ".aidor" / "summary.md"
        write_summary_md(state, out)
        console.print(f"[dim]wrote {out}[/dim]")


# ---- clean ---------------------------------------------------------------


@app.command()
def clean(
    repo: Path = typer.Option(Path.cwd(), "--repo", resolve_path=True),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation."),
) -> None:
    """Delete the .aidor/ directory and all bootstrap-installed files.

    Removes:
      * ``.aidor/`` (run artefacts)
      * ``.github/hooks/aidor.json`` (the active enforcement hook — left
        behind it would constrain follow-up interactive ``copilot``
        sessions in the same repo)
      * ``.github/agents/aidor-coder.md`` and ``.github/agents/aidor-reviewer.md``
        (operational agent instructions — Copilot picks them up
        automatically in interactive sessions even after the run ends)

    Keeps ``AGENTS.md`` (it has a managed block but also operator-edited
    sections outside it).
    """
    from aidor.bootstrap import teardown_repo

    aidor_dir = repo / ".aidor"
    hooks_path = repo / ".github" / "hooks" / "aidor.json"
    coder_agent = repo / ".github" / "agents" / "aidor-coder.md"
    reviewer_agent = repo / ".github" / "agents" / "aidor-reviewer.md"
    teardown_targets = (hooks_path, coder_agent, reviewer_agent)

    has_anything = aidor_dir.exists() or any(t.exists() for t in teardown_targets)
    if not has_anything:
        console.print("[dim]nothing to clean[/dim]")
        return
    if not yes:
        targets = []
        if aidor_dir.exists():
            targets.append(str(aidor_dir))
        for t in teardown_targets:
            if t.exists():
                targets.append(str(t))
        if not typer.confirm(f"Delete {', '.join(targets)}?"):
            raise typer.Exit(code=1)

    if aidor_dir.exists():
        shutil.rmtree(aidor_dir)
        console.print(f"[green]removed {aidor_dir}[/green]")

    # Reuse teardown_repo() so behaviour stays consistent with the
    # run-end cleanup (same target list, same empty-dir semantics).
    for action in teardown_repo(repo):
        console.print(f"[green]{action}[/green]")


# ---- doctor --------------------------------------------------------------


@app.command()
def doctor(
    repo: Path = typer.Option(Path.cwd(), "--repo", resolve_path=True),
    copilot_binary: str = typer.Option("copilot", "--copilot-binary"),
) -> None:
    """Run environment checks."""
    import subprocess

    ok = True

    def check(label: str, cond: bool, detail: str = "", *, required: bool = True) -> None:
        nonlocal ok
        mark = (
            "[green]OK[/green]"
            if cond
            else ("[red]FAIL[/red]" if required else "[yellow]SKIP[/yellow]")
        )
        console.print(f"{mark} {label}{(' - ' + detail) if detail else ''}")
        if not cond and required:
            ok = False

    check("Python >= 3.11", sys.version_info >= (3, 11), f"python={sys.version.split()[0]}")
    copilot_path = shutil.which(copilot_binary)
    check(
        f"copilot binary ({copilot_binary}) on PATH", copilot_path is not None, copilot_path or ""
    )
    if copilot_path:
        try:
            out = subprocess.run(
                [copilot_path, "--version"], capture_output=True, text=True, timeout=10
            )
            version_text = out.stdout.strip() or out.stderr.strip()
            check("copilot --version", out.returncode == 0, version_text)
            if out.returncode == 0:
                parsed = _parse_copilot_version(version_text)
                min_str = ".".join(str(n) for n in MIN_COPILOT_VERSION)
                if parsed is None:
                    check(
                        f"copilot >= {min_str}",
                        False,
                        f"could not parse version from {version_text!r}",
                    )
                else:
                    parsed_str = ".".join(str(n) for n in parsed)
                    check(
                        f"copilot >= {min_str}",
                        parsed >= MIN_COPILOT_VERSION,
                        f"installed={parsed_str}",
                    )
        except Exception as exc:  # pragma: no cover
            check("copilot --version", False, str(exc))
    check("repo is a directory", repo.is_dir(), str(repo))
    check(
        "AGENTS.md exists (optional)",
        (repo / "AGENTS.md").exists(),
        "will be created by bootstrap",
        required=False,
    )
    if sys.platform == "linux":
        check(
            "systemd-inhibit (keep-awake)",
            shutil.which("systemd-inhibit") is not None,
            required=False,
        )
    if sys.platform == "darwin":
        check(
            "caffeinate (keep-awake)",
            shutil.which("caffeinate") is not None,
            required=False,
        )
    if sys.platform == "win32":
        check("Windows wake-lock via ctypes", True)

    raise typer.Exit(code=0 if ok else 2)


# ---- abort ---------------------------------------------------------------


@app.command()
def abort(
    repo: Path = typer.Option(Path.cwd(), "--repo", resolve_path=True),
) -> None:
    """Mark the current run as aborted.

    Writes `.aidor/ABORT` (the global abort marker watched by the phase
    watchdog and the hook resolver) and flips `state.json` to ``aborted``.
    The current Copilot subprocess is not signalled directly — the phase
    watchdog detects the marker within ~1 s and terminates it cleanly.
    """
    aidor_dir = repo / ".aidor"
    aidor_dir.mkdir(parents=True, exist_ok=True)
    abort_marker = aidor_dir / "ABORT"
    abort_marker.write_text(
        f"aborted_via=cli at={_utcnow()}\n",
        encoding="utf-8",
    )

    state_path = aidor_dir / "state.json"
    if state_path.exists():
        state = _load_state_or_exit(state_path)
        state.status = "aborted"
        state.save(state_path)
        console.print("[yellow]marked aborted (wrote .aidor/ABORT + state.json)[/yellow]")
    else:
        console.print("[yellow]wrote .aidor/ABORT (no state.json to update)[/yellow]")


def _utcnow() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    app()
