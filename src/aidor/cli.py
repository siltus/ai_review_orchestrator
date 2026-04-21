"""Command-line interface for aidor."""

from __future__ import annotations

import asyncio
import logging
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
    state = State.load(state_path)
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
    state = State.load(state_path)
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
    """Delete the .aidor/ directory (keeps AGENTS.md + .github/agents/)."""
    aidor_dir = repo / ".aidor"
    if not aidor_dir.exists():
        console.print("[dim]nothing to clean[/dim]")
        return
    if not yes:
        if not typer.confirm(f"Delete {aidor_dir}?"):
            raise typer.Exit(code=1)
    shutil.rmtree(aidor_dir)
    console.print(f"[green]removed {aidor_dir}[/green]")


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
            check(
                "copilot --version", out.returncode == 0, out.stdout.strip() or out.stderr.strip()
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
    if sys.platform == "win32":
        check("Windows wake-lock via ctypes", True)

    raise typer.Exit(code=0 if ok else 2)


# ---- abort ---------------------------------------------------------------


@app.command()
def abort(
    repo: Path = typer.Option(Path.cwd(), "--repo", resolve_path=True),
) -> None:
    """Mark the current run as aborted (sets status; does not kill copilot)."""
    state_path = repo / ".aidor" / "state.json"
    if not state_path.exists():
        console.print("[red]no state.json[/red]")
        raise typer.Exit(code=1)
    state = State.load(state_path)
    state.status = "aborted"
    state.save(state_path)
    console.print("[yellow]marked aborted[/yellow]")


if __name__ == "__main__":
    app()
