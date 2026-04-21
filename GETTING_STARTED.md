# Getting started with `aidor`

> AI Review Orchestrator: drives Copilot CLI through automated review↔fix
> rounds until the reviewer signs the repo off as production-ready.

## Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Python | 3.11+ | `aidor` itself is pure Python. |
| GitHub Copilot CLI | 1.0.32+ | Install via `gh extension install github/gh-copilot` or the standalone CLI; `copilot --version` must work. |
| Git | any modern | Repo must be a git working tree. |
| OS | Windows / Linux / macOS | All three are supported. Wake-lock uses `SetThreadExecutionState` / `systemd-inhibit` / `caffeinate`. |

## Install

```pwsh
# Clone
git clone <this-repo>
cd ai_review_orchestrator

# Create a venv (recommended)
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # PowerShell
# source .venv/bin/activate    # bash/zsh

# Install in editable mode with dev extras
python -m pip install -e ".[dev]"

# Smoke check
aidor --version
aidor doctor
```

`aidor doctor` checks Python version, `copilot` on PATH, and the wake-lock
backend for your platform (`SetThreadExecutionState` on Windows,
`systemd-inhibit` on Linux, `caffeinate` on macOS).

## First run (dry-run)

A dry-run exercises bootstrap (writes `AGENTS.md` managed block,
`.github/agents/*.md`, `.github/hooks/aidor.json`, `.aidor/` skeleton) but
does NOT spawn Copilot. Safe to run anywhere.

```pwsh
cd <some-repo>
aidor run --coder gpt-5.2 --reviewer gpt-5.2 --dry-run
```

Output:

```
bootstrap: created .aidor/
bootstrap: created .aidor/reviews/
...
bootstrap: wrote .github/hooks/aidor.json
bootstrap: updated AGENTS.md
dry-run complete
```

Inspect what was written: `git status`. The `.github/hooks/aidor.json` file
is gitignored because it bakes the absolute path of `python.exe` from your
venv (so the hook works without depending on `PATH`).

## Real run

```pwsh
aidor run `
    --coder gpt-5.2 `
    --reviewer gpt-5.2 `
    --max-rounds 5 `
    --idle-timeout 300 `
    --round-timeout 1800
```

Useful flags:

| Flag | Default | What it does |
| --- | --- | --- |
| `--max-rounds N`           | 10     | Hard cap on review→fix iterations. |
| `--idle-timeout SECS`      | 120    | Kill phase if Copilot's stdout is silent this long. |
| `--round-timeout SECS`     | 10800  | Kill phase if total wall-clock (minus paused human-wait time) exceeds this. |
| `--max-restarts N`         | 3      | `copilot --continue` retries per phase before giving up. |
| `--no-allow-local-install` | (off)  | Disallow project-local installs even with a lockfile. |
| `--no-keep-awake`          | (off)  | Don't take a wake-lock. |
| `--resume`                 | off    | Pick up from existing `.aidor/state.json`. |
| `--copilot-binary PATH`    | `copilot` | Override the Copilot CLI binary (rarely needed). |

While running:

- Live status & per-phase events scroll on the terminal.
- `Ctrl-C` writes the abort marker and shuts down cleanly.
- If the coder calls `ask_user`, the orchestrator first tries to answer
  from policy (`question_classes.yml`) or state. Only when those fail does
  it prompt your TTY. The phase watchdog is paused for the duration of
  the human wait.

## Inspecting a run

```pwsh
aidor status              # current state.json snapshot
aidor summary             # render the Rich table + (re)write summary.md
```

All artefacts live under `.aidor/`:

```
.aidor/reviews/review-0001-<utc>.md   ← reviewer's findings + AIDOR footer
.aidor/fixes/fixes-0001-<utc>.md      ← coder's per-round summary
.aidor/transcripts/<role>-<NNNN>.md   ← Copilot --share log
.aidor/logs/orchestrator.log          ← hook breadcrumbs
.aidor/logs/qa.jsonl                  ← every ask_user resolution
.aidor/state.json                     ← machine state
.aidor/summary.md                     ← final human-readable summary
```

## Stopping mid-run

```pwsh
# Soft abort: orchestrator will mark the run aborted and clean up.
aidor abort

# Or write the marker by hand:
"" | Out-File .aidor/ABORT
```

## Cleaning up

```pwsh
aidor clean -y          # delete .aidor/ (keeps AGENTS.md + .github/agents/)
```

## Quality gates (this repo)

The aidor repo dogfoods its own quality bar — pre-commit and CI run:

1. `ruff check` + `ruff format --check`
2. `pip-audit --skip-editable`
3. `pytest`

To enable locally:

```pwsh
python -m pre_commit install
```

To run them once (same commands CI runs, including the coverage gate):

```pwsh
python -m ruff check src tests
python -m ruff format --check src tests
python -m pip_audit --skip-editable
python -m pytest --cov=aidor --cov-report=term-missing --cov-fail-under=90
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.
