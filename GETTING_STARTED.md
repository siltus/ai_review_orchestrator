# Getting started with `aidor`

> AI Iterative Development Orchestrator & Reviewer: drives Copilot CLI through automated review↔fix
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

A dry-run exercises bootstrap: it installs aidor's temporary runtime
`AGENTS.md`, backs up any project `AGENTS.md` under `.github/aidor-backups/`,
writes `.github/agents/*.md`, `.github/hooks/aidor.json`, and the `.aidor/`
skeleton, but does NOT spawn Copilot.

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
bootstrap: installed runtime AGENTS.md
dry-run complete
```

Inspect what was written: `git status`. The `.github/hooks/aidor.json` file is
gitignored because it bakes the absolute path of `python.exe` from your venv
(so the hook works without depending on `PATH`). The temporary `AGENTS.md` is
restored or removed by `aidor clean` / run teardown.

## Real run

Interactive setup asks for the major settings before launch. It caches
Copilot's structured model catalog for 24 hours by default (override with
`--model-cache-ttl-hours`; use `0` to force refresh), sorts it by model id, then
presents arrow-key menus for coder/reviewer models and reasoning effort. The
catalog is discovered from Copilot ACP metadata first and the live models API
second; aidor does not keep a hard-coded list of model ids.

```pwsh
aidor run --interactive
```

For scripted runs, pass the model ids explicitly:

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
| `--interactive` / `-i`     | off    | Prompt for repo, coder/reviewer models, reasoning effort, max rounds, and timeouts. Requires a TTY and a token usable by Copilot (`COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, or `gh auth login`) when the cached model catalog is missing or expired. |
| `--model-cache-ttl-hours N` | 24     | Reuse the cached Copilot model catalog for this many hours in interactive mode. Use `0` to force a refresh every time. |
| `--instructions TEXT`      | (none) | Extra free-form instructions injected into BOTH reviewer and coder prompts every round (e.g. `"extra effort on security"`, `"make sure it's cross-platform"`). |
| `--instructions-file PATH` | (none) | Same as `--instructions`, but read from a UTF-8 file. Mutually exclusive with `--instructions`. |
| `--reviewer-instructions TEXT` / `--reviewer-instructions-file PATH` | (none) | Extra instructions appended to the reviewer prompt only (additive on top of `--instructions`). |
| `--coder-instructions TEXT` / `--coder-instructions-file PATH`       | (none) | Extra instructions appended to the coder prompt only (additive on top of `--instructions`). |
| `--effort {low\|medium\|high\|xhigh}` | (Copilot default) | Forwarded to Copilot CLI as `--reasoning-effort=<value>` for BOTH roles. Required for GPT-family models, where `xhigh` is unreachable through the model id alone. |
| `--reviewer-effort` / `--coder-effort` | (Copilot default) | Per-role override of `--effort`. The role-specific value fully replaces the shared one (Copilot only accepts a single `--reasoning-effort` per invocation). |

While running:

- Live status & per-phase events scroll on the terminal.
- `Ctrl-C` writes the abort marker and shuts down cleanly. The marker is
  one-shot: a later `aidor run` clears a stale `.aidor/ABORT` after bootstrap
  and before launching the first phase.
- If `.aidor/state.json` cannot be saved after the bounded retry loop, aidor
  exits with code 4 before launching another phase, so agents never continue
  against stale state.
- If the coder calls `ask_user`, the orchestrator first tries to answer
  from policy (`question_classes.yml`). In v0.1 the state-derived answer
  step (§9.4 step 2 of `plan.md`) is scaffolding only — it always falls
  through, so any non-policy question is escalated to your TTY. The phase
  watchdog is paused for the duration of the human wait.

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
.aidor/logs/failed_mcp_tools.jsonl    ← denied MCP tool attempts + reasons
.aidor/state.json                     ← machine state
.aidor/summary.md                     ← final human-readable summary
```

Denied MCP tools are also surfaced in `aidor summary` with YAML allowlist
guidance. Treat that as a policy prompt, not a reason to hard-code a specific
external MCP in Python.

## Stopping mid-run

```pwsh
# Soft abort: orchestrator will mark the run aborted and clean up.
aidor abort

# Or write the marker by hand:
"" | Out-File .aidor/ABORT
```

## Cleaning up

```pwsh
aidor clean -y          # restore AGENTS.md, remove .aidor/ + runtime files
```

## Quality gates (this repo)

The aidor repo dogfoods its own quality bar — pre-commit and CI run:

1. `ruff check` + `ruff format --check`
2. `pyright`
3. `pip-audit --skip-editable`
4. `pytest`

> **Operator note:** the GitHub Actions `ci` workflow can be administratively
> disabled (Repository → Settings → Actions → Workflows → `ci` → "Disable
> workflow"). When that happens the gates above stop running on push/PR even
> though `.github/workflows/ci.yml` and `.pre-commit-config.yaml` are still
> wired up. After re-enabling the workflow, run `ruff format src tests`
> locally once to clear any drift introduced while CI was off, then push.

To enable locally:

```pwsh
python -m pre_commit install
```

To run them once (same commands CI runs, including the coverage gate):

```pwsh
python -m ruff check src tests
python -m ruff format --check src tests
python -m pyright
python -m pip_audit --skip-editable
python -m pytest --cov=aidor --cov-report=term-missing --cov-fail-under=90
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.
