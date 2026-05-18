"""Command-line interface for aidor."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import TypedDict

import click
import typer
from rich.console import Console
from rich.live import Live
from rich.prompt import IntPrompt, Prompt
from rich.text import Text

from aidor import __version__
from aidor.bootstrap import bootstrap
from aidor.config import (
    DEFAULT_IDLE_TIMEOUT_S,
    DEFAULT_MAX_RESTARTS_PER_ROUND,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_ROUND_TIMEOUT_S,
    EFFORT_LEVELS,
    RunConfig,
)
from aidor.model_history import (
    DEFAULT_SUPPORTED_MODELS_CACHE_TTL_S,
    ModelInfo,
    discover_supported_models,
    load_recent_models,
    load_supported_models,
    record_supported_models,
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


class InteractivePicks(TypedDict):
    repo: Path
    coder: str
    reviewer: str
    effort: str | None
    reviewer_effort: str | None
    coder_effort: str | None
    max_rounds: int
    idle_timeout: int
    round_timeout: int


def _resolve_instructions(
    inline_flag: str,
    inline_value: str | None,
    file_flag: str,
    file_value: Path | None,
) -> str:
    """Resolve an inline-text + file-path option pair into a single string.

    Used by the ``aidor run`` extra-instructions options. Enforces:

    * The two flags are mutually exclusive — supplying both is an operator
      error and exits with code 2.
    * The file (if used) must exist and be readable as UTF-8; otherwise we
      print a clean error and exit with code 2 instead of leaking a
      traceback.

    Returns an empty string when neither flag is supplied. Inline text is
    returned as-is (no normalisation here; the orchestrator's prompt
    formatter handles whitespace).
    """
    if inline_value is not None and file_value is not None:
        console.print(
            f"[red]{inline_flag} and {file_flag} are mutually exclusive — "
            "pass one or the other, not both[/red]"
        )
        raise typer.Exit(code=2)
    if inline_value is not None:
        return inline_value
    if file_value is not None:
        if not file_value.is_file():
            console.print(f"[red]{file_flag}: {file_value} is not a readable file[/red]")
            raise typer.Exit(code=2)
        try:
            return file_value.read_text(encoding="utf-8")
        except OSError as exc:
            console.print(f"[red]{file_flag}: could not read {file_value}: {exc}[/red]")
            raise typer.Exit(code=2) from exc
    return ""


def _resolve_effort(flag: str, value: str | None) -> str:
    """Validate a Copilot reasoning-effort CLI option, returning a normalised
    lower-cased string (or "" when ``value`` is ``None``).

    Typer does not have a true choice-validator built in for raw ``str``
    options, and we want a clean operator-facing error rather than a
    ``ValueError`` traceback when the value is bogus. Validation is centralised
    here so all three ``--effort`` flag variants share the same behaviour.
    """
    if value is None:
        return ""
    normalised = value.strip().lower()
    if normalised and normalised not in EFFORT_LEVELS:
        allowed = ", ".join(EFFORT_LEVELS)
        console.print(f"[red]{flag}={value!r} is not one of {{{allowed}}}[/red]")
        raise typer.Exit(code=2)
    return normalised


# ---- Interactive picker helpers ------------------------------------------


def _load_live_model_catalog(
    *, cache_ttl_s: float = DEFAULT_SUPPORTED_MODELS_CACHE_TTL_S
) -> list[ModelInfo]:
    """Fetch or reuse the current Copilot model catalog.

    Fresh cache entries avoid spawning Copilot ACP for every interactive run.
    Cache misses intentionally call Copilot's structured ACP/API model sources
    (via ``model_history.discover_supported_models``), not ``copilot help``.
    """
    cached = load_supported_models(max_age_s=cache_ttl_s)
    if cached:
        console.print(
            f"[dim]using cached Copilot model list ({len(cached)} entries; "
            f"TTL {cache_ttl_s / 3600:g}h)[/dim]"
        )
        return cached

    console.print("[dim]fetching current Copilot model list...[/dim]")
    models = discover_supported_models()
    if models:
        record_supported_models(models)
        return models

    stale = load_supported_models()
    detail = ""
    if stale:
        detail = (
            f" Last cached catalog has {len(stale)} entries, but it is older "
            "than the configured model-cache TTL."
        )
    console.print(
        "[red]could not fetch the current Copilot model list from "
        "Copilot ACP or the fallback /models API.[/red]" + detail
    )
    console.print(
        "[yellow]Authenticate a token Copilot can use via COPILOT_GITHUB_TOKEN, "
        "GH_TOKEN, GITHUB_TOKEN, or `gh auth login`, then retry --interactive.[/yellow]"
    )
    raise typer.Exit(code=2)


def _natural_sort_key(value: str) -> tuple[object, ...]:
    parts = re.split(r"(\d+)", value.casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def _sort_models(models: list[ModelInfo]) -> list[ModelInfo]:
    return sorted(models, key=lambda m: (_natural_sort_key(m.model_id), _natural_sort_key(m.name)))


def _menu_action(ch: str) -> str:
    if ch in ("\r", "\n"):
        return "enter"
    if ch in ("k", "K", "\x1b[A", "\x00H", "\xe0H") or ch.endswith("[A"):
        return "up"
    if ch in ("j", "J", "\x1b[B", "\x00P", "\xe0P") or ch.endswith("[B"):
        return "down"
    if ch in ("q", "Q", "\x1b"):
        return "cancel"
    return ""


def _render_menu(title: str, options: list[str], selected: int) -> Text:
    out = Text()
    out.append(f"\n{title}\n", style="bold cyan")
    out.append("Use Up/Down (or j/k), Enter to select, q to cancel.\n", style="dim")
    for idx, option in enumerate(options):
        if idx == selected:
            out.append(f"> {option}\n", style="reverse")
        else:
            out.append(f"  {option}\n")
    return out


def _select_from_menu(title: str, options: list[str], *, default_index: int = 0) -> int:
    if not options:
        raise ValueError("menu requires at least one option")
    selected = max(0, min(default_index, len(options) - 1))
    with Live(
        _render_menu(title, options, selected),
        console=console,
        refresh_per_second=20,
        transient=False,
    ) as live:
        while True:
            action = _menu_action(click.getchar())
            if action == "enter":
                return selected
            if action == "cancel":
                raise typer.Exit(code=1)
            if action == "up":
                selected = (selected - 1) % len(options)
            elif action == "down":
                selected = (selected + 1) % len(options)
            else:
                continue
            live.update(_render_menu(title, options, selected))


def _pick_model(
    role: str,
    *,
    default: str | None,
    label: str,
    models: list[ModelInfo],
) -> str:
    """Prompt the operator to pick a live Copilot model id for ``role``."""
    history = set(load_recent_models(role))
    sorted_models = _sort_models(models)
    model_options = [
        f"{model.label}{' [recent]' if model.model_id in history else ''}"
        for model in sorted_models
    ]
    custom_label = "(type a custom/BYOK id)"
    options = [*model_options, custom_label]
    default_index = 0
    if default:
        default_index = next(
            (idx for idx, model in enumerate(sorted_models) if model.model_id == default),
            len(options) - 1,
        )
    while True:
        selected = _select_from_menu(
            f"{label} model id - current live Copilot catalog (sorted)",
            options,
            default_index=default_index,
        )
        if selected < len(sorted_models):
            return sorted_models[selected].model_id
        free = Prompt.ask(
            "custom model id",
            default=default or "",
            console=console,
        ).strip()
        if free:
            return free
        console.print("[red]model id cannot be empty[/red]")


def _pick_effort(label: str, *, default: str | None) -> str:
    """Prompt for a Copilot ``--reasoning-effort`` value, or empty for "none".

    Returns the empty string when the operator picks "(none)" - same
    sentinel the non-interactive path uses to mean "let Copilot apply
    its own per-model default".
    """
    options = ["(none)", *EFFORT_LEVELS]
    default_index = 0
    if default in EFFORT_LEVELS:
        default_index = EFFORT_LEVELS.index(default) + 1
    selected = _select_from_menu(
        f"{label} reasoning effort",
        options,
        default_index=default_index,
    )
    return "" if selected == 0 else options[selected]


def _run_interactive_prompts(
    *,
    repo: Path,
    coder: str | None,
    reviewer: str | None,
    effort: str | None,
    reviewer_effort: str | None,
    coder_effort: str | None,
    max_rounds: int,
    idle_timeout: int,
    round_timeout: int,
    model_cache_ttl_s: float,
) -> InteractivePicks:
    """Run the interactive picker for ``aidor run --interactive``.

    The defaults shown to the operator match what the non-interactive
    CLI would use, so accepting every prompt is equivalent to running
    the same command with no extras. Returns a dict the caller folds
    back into the local variables of ``run`` before building RunConfig.
    """
    if not sys.stdin.isatty():
        console.print(
            "[red]--interactive requires a TTY on stdin; "
            "pass --coder and --reviewer explicitly in non-interactive contexts[/red]"
        )
        raise typer.Exit(code=2)

    console.rule("[bold]aidor run - interactive setup[/bold]")
    console.print(
        "[dim]Hit Enter to accept the shown default. The model picker is loaded "
        "from a fresh cached Copilot ACP/API catalog when available, otherwise "
        "refreshed from Copilot. Entries are sorted by model id with recent "
        "choices marked.[/dim]"
    )
    model_catalog = _load_live_model_catalog(cache_ttl_s=model_cache_ttl_s)

    repo_str = Prompt.ask(
        "\n[bold cyan]repository[/bold cyan] path",
        default=str(repo),
        console=console,
    ).strip()
    chosen_repo = Path(repo_str).expanduser().resolve()
    if not chosen_repo.is_dir():
        console.print(f"[red]repo {chosen_repo} is not an existing directory[/red]")
        raise typer.Exit(code=2)

    chosen_coder = _pick_model("coder", default=coder, label="coder", models=model_catalog)
    chosen_reviewer = _pick_model(
        "reviewer", default=reviewer, label="reviewer", models=model_catalog
    )
    chosen_effort = _pick_effort("shared", default=effort)

    chosen_reviewer_effort = reviewer_effort
    chosen_coder_effort = coder_effort
    if (
        Prompt.ask(
            "\n[bold cyan]per-role effort overrides?[/bold cyan]",
            choices=["y", "n"],
            default="n",
            console=console,
        )
        .strip()
        .lower()
        == "y"
    ):
        chosen_reviewer_effort = _pick_effort("reviewer (override)", default=reviewer_effort)
        chosen_coder_effort = _pick_effort("coder (override)", default=coder_effort)

    chosen_max_rounds = IntPrompt.ask(
        "\n[bold cyan]max rounds[/bold cyan]",
        default=max_rounds,
        console=console,
    )
    chosen_idle = IntPrompt.ask(
        "[bold cyan]idle timeout[/bold cyan] (seconds)",
        default=idle_timeout,
        console=console,
    )
    chosen_round = IntPrompt.ask(
        "[bold cyan]round timeout[/bold cyan] (seconds)",
        default=round_timeout,
        console=console,
    )

    console.print("\n[bold]Summary[/bold]")
    console.print(f"  repo:           {chosen_repo}")
    console.print(f"  coder model:    {chosen_coder}")
    console.print(f"  reviewer model: {chosen_reviewer}")
    console.print(f"  effort:         {chosen_effort or '(default)'}")
    if chosen_reviewer_effort:
        console.print(f"  reviewer eff:   {chosen_reviewer_effort}")
    if chosen_coder_effort:
        console.print(f"  coder eff:      {chosen_coder_effort}")
    console.print(f"  max rounds:     {chosen_max_rounds}")
    console.print(f"  idle timeout:   {chosen_idle}s")
    console.print(f"  round timeout:  {chosen_round}s")
    if (
        Prompt.ask(
            "\nproceed?",
            choices=["y", "n"],
            default="y",
            console=console,
        )
        .strip()
        .lower()
        != "y"
    ):
        console.print("[yellow]cancelled[/yellow]")
        raise typer.Exit(code=1)

    return {
        "repo": chosen_repo,
        "coder": chosen_coder,
        "reviewer": chosen_reviewer,
        "effort": chosen_effort or None,
        "reviewer_effort": chosen_reviewer_effort,
        "coder_effort": chosen_coder_effort,
        "max_rounds": chosen_max_rounds,
        "idle_timeout": chosen_idle,
        "round_timeout": chosen_round,
    }


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
    coder: str | None = typer.Option(
        None,
        "--coder",
        help=(
            "Coder model id (verbatim Copilot model string). Required unless --interactive is set."
        ),
    ),
    reviewer: str | None = typer.Option(
        None,
        "--reviewer",
        help="Reviewer model id. Required unless --interactive is set.",
    ),
    interactive: bool = typer.Option(
        False,
        "-i",
        "--interactive",
        help=(
            "Prompt for the major settings (repo, coder/reviewer models, "
            "effort, max-rounds, timeouts) before launching. Models are loaded "
            "from the cached/live Copilot ACP/API catalog and sorted by model id. "
            "Requires a TTY on stdin."
        ),
    ),
    model_cache_ttl_hours: float = typer.Option(
        DEFAULT_SUPPORTED_MODELS_CACHE_TTL_S / 3600,
        "--model-cache-ttl-hours",
        help=(
            "Hours to reuse the cached Copilot model catalog in --interactive. "
            "Use 0 to refresh every time."
        ),
    ),
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
    instructions: str | None = typer.Option(
        None,
        "--instructions",
        help=(
            "Inline extra instructions injected into BOTH reviewer and coder "
            "prompts (e.g. 'extra effort on security'). Mutually exclusive "
            "with --instructions-file."
        ),
    ),
    instructions_file: Path | None = typer.Option(
        None,
        "--instructions-file",
        help=(
            "Path to a UTF-8 file whose contents are injected into BOTH "
            "reviewer and coder prompts. Mutually exclusive with --instructions."
        ),
        exists=False,  # explicit error below for friendlier message
        resolve_path=True,
    ),
    reviewer_instructions: str | None = typer.Option(
        None,
        "--reviewer-instructions",
        help=(
            "Inline extra instructions for the reviewer ONLY. Appended to "
            "--instructions when both are supplied. Mutually exclusive with "
            "--reviewer-instructions-file."
        ),
    ),
    reviewer_instructions_file: Path | None = typer.Option(
        None,
        "--reviewer-instructions-file",
        help=(
            "Path to a UTF-8 file whose contents are appended to the reviewer "
            "prompt only. Mutually exclusive with --reviewer-instructions."
        ),
        exists=False,
        resolve_path=True,
    ),
    coder_instructions: str | None = typer.Option(
        None,
        "--coder-instructions",
        help=(
            "Inline extra instructions for the coder ONLY. Appended to "
            "--instructions when both are supplied. Mutually exclusive with "
            "--coder-instructions-file."
        ),
    ),
    coder_instructions_file: Path | None = typer.Option(
        None,
        "--coder-instructions-file",
        help=(
            "Path to a UTF-8 file whose contents are appended to the coder "
            "prompt only. Mutually exclusive with --coder-instructions."
        ),
        exists=False,
        resolve_path=True,
    ),
    effort: str | None = typer.Option(
        None,
        "--effort",
        help=(
            "Copilot --reasoning-effort applied to BOTH reviewer and coder "
            f"(one of: {', '.join(EFFORT_LEVELS)}). Useful for GPT-family "
            "models where xhigh is only reachable via this flag, not via the "
            "model id (see GitHub issue #1)."
        ),
        case_sensitive=False,
    ),
    reviewer_effort: str | None = typer.Option(
        None,
        "--reviewer-effort",
        help=(
            "Copilot --reasoning-effort for the reviewer ONLY (one of: "
            f"{', '.join(EFFORT_LEVELS)}). Overrides --effort for this role."
        ),
        case_sensitive=False,
    ),
    coder_effort: str | None = typer.Option(
        None,
        "--coder-effort",
        help=(
            "Copilot --reasoning-effort for the coder ONLY (one of: "
            f"{', '.join(EFFORT_LEVELS)}). Overrides --effort for this role."
        ),
        case_sensitive=False,
    ),
) -> None:
    """Run the full review↔fix loop until convergence, abort, or max rounds."""
    if model_cache_ttl_hours < 0:
        console.print("[red]--model-cache-ttl-hours must be >= 0[/red]")
        raise typer.Exit(code=2)

    if interactive:
        picks = _run_interactive_prompts(
            repo=repo,
            coder=coder,
            reviewer=reviewer,
            effort=effort,
            reviewer_effort=reviewer_effort,
            coder_effort=coder_effort,
            max_rounds=max_rounds,
            idle_timeout=idle_timeout,
            round_timeout=round_timeout,
            model_cache_ttl_s=model_cache_ttl_hours * 3600,
        )
        repo = picks["repo"]
        coder = picks["coder"]
        reviewer = picks["reviewer"]
        effort = picks["effort"]
        reviewer_effort = picks["reviewer_effort"]
        coder_effort = picks["coder_effort"]
        max_rounds = picks["max_rounds"]
        idle_timeout = picks["idle_timeout"]
        round_timeout = picks["round_timeout"]

    if not coder or not reviewer:
        missing = ", ".join(
            name for name, val in (("--coder", coder), ("--reviewer", reviewer)) if not val
        )
        console.print(f"[red]{missing} required (or pass --interactive to be prompted)[/red]")
        raise typer.Exit(code=2)

    extra_shared = _resolve_instructions(
        "--instructions", instructions, "--instructions-file", instructions_file
    )
    extra_reviewer = _resolve_instructions(
        "--reviewer-instructions",
        reviewer_instructions,
        "--reviewer-instructions-file",
        reviewer_instructions_file,
    )
    extra_coder = _resolve_instructions(
        "--coder-instructions",
        coder_instructions,
        "--coder-instructions-file",
        coder_instructions_file,
    )
    effort_shared = _resolve_effort("--effort", effort)
    effort_reviewer = _resolve_effort("--reviewer-effort", reviewer_effort)
    effort_coder = _resolve_effort("--coder-effort", coder_effort)

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
        extra_instructions=extra_shared,
        reviewer_extra_instructions=extra_reviewer,
        coder_extra_instructions=extra_coder,
        effort=effort_shared,
        reviewer_effort=effort_reviewer,
        coder_effort=effort_coder,
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
    print_summary(state, console, aidor_dir=repo / ".aidor")


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
    """Delete .aidor/ and all bootstrap-installed runtime files.

    Removes:
      * temporary runtime ``AGENTS.md`` or restores the project original from
        ``.github/aidor-backups/``
      * ``.github/hooks/aidor.json`` (the active enforcement hook — left
        behind it would constrain follow-up interactive ``copilot``
        sessions in the same repo)
      * ``.github/agents/aidor-coder.md`` and ``.github/agents/aidor-reviewer.md``
        (operational agent instructions — Copilot picks them up
        automatically in interactive sessions even after the run ends)
      * ``.aidor/`` (run artefacts)
    """
    from aidor.bootstrap import AGENTS_BACKUP_DIR, teardown_repo

    aidor_dir = repo / ".aidor"
    hooks_path = repo / ".github" / "hooks" / "aidor.json"
    coder_agent = repo / ".github" / "agents" / "aidor-coder.md"
    reviewer_agent = repo / ".github" / "agents" / "aidor-reviewer.md"
    backup_dir = repo / AGENTS_BACKUP_DIR
    teardown_targets = (hooks_path, coder_agent, reviewer_agent, backup_dir)

    has_anything = aidor_dir.exists() or any(t.exists() for t in teardown_targets)
    if not has_anything:
        console.print("[dim]nothing to clean[/dim]")
        return
    if not yes:
        targets = []
        for t in teardown_targets:
            if t.exists():
                targets.append(str(t))
        if aidor_dir.exists():
            targets.append(str(aidor_dir))
        if not typer.confirm(f"Delete {', '.join(targets)}?"):
            raise typer.Exit(code=1)

    # Restore/remove runtime files before deleting `.aidor/` so cleanup still
    # works after interrupted runs and before operators wipe run artefacts.
    for action in teardown_repo(repo):
        console.print(f"[green]{action}[/green]")

    if aidor_dir.exists():
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
        "project AGENTS.md exists (optional)",
        (repo / "AGENTS.md").exists(),
        "will be backed up while aidor installs its runtime contract",
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
