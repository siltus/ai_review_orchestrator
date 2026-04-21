# Architecture

`aidor` orchestrates two GitHub Copilot CLI agents — **`aidor-coder`** and
**`aidor-reviewer`** — through a deterministic review↔fix loop, until the
reviewer signs off the repo as production-ready or a hard limit (max rounds /
human abort) is reached.

It is a single Python process. Copilot CLI is invoked as a child process per
phase. All state, transcripts, reviews, fixes, OTel logs, and a final summary
are written under `.aidor/` inside the target repo.

```
┌──────────────────────────────────────────────────────────────────────┐
│  aidor (main process)                                                │
│                                                                      │
│  ┌────────────┐  bootstrap   ┌──────────────────────────────────┐    │
│  │   CLI      │─────────────▶│  Orchestrator (state machine)    │    │
│  │ (typer)    │              │                                  │    │
│  └────────────┘              │  round N:                        │    │
│                              │   review  →  parse footer        │    │
│                              │      │                           │    │
│                              │      ├─ clean+ready → gate       │    │
│                              │      │      │                    │    │
│                              │      │      ├─ ready → CONVERGED │    │
│                              │      │      └─ issues → fix      │    │
│                              │      └─ issues → fix             │    │
│                              │                                  │    │
│                              │   fix     →  next round          │    │
│                              └──────┬─────────────┬─────────────┘    │
│                                     │             │                  │
│                  ┌──────────────────▼───┐    ┌────▼──────────────┐   │
│                  │  PhaseRunner          │    │  Human watcher    │   │
│                  │  (asyncio)            │    │  (asyncio task)   │   │
│                  │                       │    │                   │   │
│                  │  spawn copilot ─┐     │    │  poll pending/    │   │
│                  │   stdout JSONL  │     │    │  prompt TTY       │   │
│                  │   stderr        │     │    │  write *.answer   │   │
│                  │   watchdog ─────┤     │    └───────────────────┘   │
│                  │   (idle/round   │     │                            │
│                  │    timeout,     │     │                            │
│                  │    pause-aware) │     │                            │
│                  └─────────┬───────┘     │                            │
│                            │             │                            │
└────────────────────────────┼─────────────┼────────────────────────────┘
                             ▼             │
                      ┌────────────┐       │       ┌──────────────────┐
                      │ copilot    │  ─────┼──────▶│ aidor.hook_      │
                      │ subprocess │ stdin │ stdin │ resolver         │
                      │ (Node)     │       │       │ (preToolUse,     │
                      └─────┬──────┘       │       │  permission-     │
                            │              │       │  Request,        │
                            │ writes        │       │  ask_user,       │
                            │ pending/<id> ─┘       │  notification,   │
                            │ via ask_user          │  agentStop)      │
                            ▼                       └────────┬─────────┘
                  ┌─────────────────┐                        │
                  │ .aidor/         │ ◀──────────────────────┘
                  │   reviews/      │   audit, breadcrumbs,
                  │   fixes/        │   ipc files
                  │   transcripts/  │
                  │   logs/         │
                  │   pending/      │
                  │   state.json    │
                  │   summary.md    │
                  └─────────────────┘
```

## Modules (`src/aidor/`)

| Module | Responsibility |
| --- | --- |
| `cli.py` | Typer entry point. Subcommands: `run`, `status`, `summary`, `clean`, `doctor`, `abort`. |
| `config.py` | `RunConfig` dataclass + path derivations. Single source of truth for tunables. |
| `bootstrap.py` | Idempotent on-disk setup: `AGENTS.md` managed block, `.github/agents/*.md`, `.github/hooks/aidor.json` (machine-specific, gitignored), `.aidor/` skeleton, `allowed_exceptions.yml` seed, config snapshot. |
| `orchestrator.py` | The state machine. Drives `PhaseRunner` per phase, parses review footers, decides next phase, handles convergence + readiness gate, runs the human-watcher task. Persists `state.json` after every transition. |
| `phase.py` | One Copilot subprocess. Builds argv (`--agent`, `--model`, `--share`, `--output-format=json`, `--allow-tool` / `--deny-tool` matrix from `guard_profile`), spawns it, streams stdout JSONL → events, streams stderr to disk, runs an idle + round-timeout watchdog (pause-aware while a hook is waiting on a human), restarts via `--continue` with exponential back-off. |
| `guard_profile.py` | The `--allow-tool` / `--deny-tool` matrix. Every `shell(...)` rule is mirrored as `bash(...)` and `powershell(...)`. Extra rules unlocked when a real lockfile is present (NOT `pyproject.toml` alone). |
| `hook_resolver.py` | Standalone process invoked by Copilot at every hook event. Implements the four-step `ask_user` resolver pipeline (policy → state → human → cancel), enforces path containment for write/edit/create, and writes the audit log + breadcrumbs. |
| `review_store.py` | Numbered file allocation + AIDOR-footer parser/validator. |
| `state.py` | `State`, `RoundRecord`, `PhaseRecord`, `RestartRecord`. Atomic save/load. |
| `telemetry.py` | Best-effort parser for the Copilot OTel JSONL file exporter (tokens, cost, tool calls, turns). |
| `wake_lock.py` | Cross-platform sleep inhibitor (Win: `SetThreadExecutionState`; Linux: `systemd-inhibit`; macOS: `caffeinate`). |
| `summary.py` | The Rich table + `summary.md` renderer consumed by `aidor summary` and the end-of-run print. |

Static assets shipped with the wheel:

- `src/aidor/agent_templates/` — `aidor-coder.md`, `aidor-reviewer.md`,
  `agents_md_block.md` (the managed block injected into `AGENTS.md`).
- `src/aidor/policies/` — `allowed_exceptions.yml` seed,
  `question_classes.yml` (deterministic-answer rules).

## Per-run on-disk layout

Everything is written into `<repo>/.aidor/` (gitignored by default):

```
.aidor/
├── state.json              # current state machine snapshot
├── summary.md              # final human-readable summary
├── config.snapshot.toml    # effective config for this run
├── allowed_exceptions.yml  # editable exceptions list
├── reviews/                # review-NNNN-<utc>.md   (reviewer outputs)
├── fixes/                  # fix-NNNN-<utc>.md      (coder summaries)
├── transcripts/            # reviewer-NNNN.md, coder-NNNN.md (--share)
├── logs/
│   ├── orchestrator.log    # hook breadcrumbs + orchestrator events
│   ├── qa.jsonl            # one line per ask_user resolution
│   ├── otel-<role>-<NNNN>.jsonl
│   └── <role>-<NNNN>.stderr
└── pending/                # IPC scratch dir for human questions:
                            #   <uuid>.json     — request from coder
                            #   <uuid>.answer   — orchestrator's answer
                            #   <uuid>.cancel   — human aborted this Q
                            #   ABORT           — global abort marker
```

## State machine

```
                  ┌───────────────┐
                  │  bootstrap    │
                  └──────┬────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │ round N: review      │
              │   (aidor-reviewer)   │
              └──────────┬───────────┘
                         │
              parse AIDOR footer
                         │
       ┌─────────────────┼──────────────────────┐
       │                 │                      │
   no footer       footer.is_clean         footer has
       │           AND production_ready     issues
       ▼                 │                      │
   FAILED                ▼                      ▼
              ┌─────────────────────┐  ┌────────────────────┐
              │ readiness_gate      │  │ round N: fix       │
              │  (aidor-reviewer)   │  │   (aidor-coder)    │
              └──────────┬──────────┘  └────────┬───────────┘
                         │                      │
                  parse footer                  │
                         │                      ▼
                ┌────────┼─────────┐    ┌──────────────────┐
                │        │         │    │  next round      │
            ready    issues    no foot. │                  │
                │        │         │    │  (or max rounds  │
                ▼        ▼         ▼    │   → UNCONVERGED) │
            CONVERGED  fix      FAILED  └──────────────────┘
                       (uses
                        gate's
                        review)
```

Stop reasons surfaced by `phase.py`:

| `stop_reason` | Meaning |
| --- | --- |
| `end_turn`     | Copilot exited cleanly with `stopReason=end_turn` (or exit 0 + nothing else seen). |
| `error`        | Non-zero exit, no recognised stop reason. |
| `idle`         | Idle watchdog fired (no stdout for `idle_timeout_s + 60s`). |
| `timeout`      | Round timer exceeded (paused while a hook is waiting on a human). |
| `aborted`      | User signal / `.aidor/ABORT` marker. |

## Hook resolver pipeline (`ask_user`)

For every `ask_user` tool invocation Copilot makes, our `preToolUse` hook
intercepts the call and resolves it locally so the tool itself never runs.
The decision returned to Copilot is `permissionDecision=deny` with the
resolved answer stuffed into `permissionDecisionReason` (which the model
sees verbatim).

```
question
   │
   ▼
1. policy lookup     ── matches a class in question_classes.yml
   │                       └── fixed_answer? → return immediately
   │                       └── policy_lookup (allowed_exceptions.yml)?
   │                                            → return if matched
   ▼
2. state-derived     ── inspect .aidor/state.json + latest review
   │                    (placeholder in v0.1)
   ▼
3. human escalation  ── write pending/<uuid>.json
   │                    block on .answer or .cancel
   │                    (timers paused in the watchdog while we wait)
   ▼
4. fallback          ── "no deterministic answer; choose another approach"
```

Every resolution is appended to `.aidor/logs/qa.jsonl` for audit.

## Pause-aware watchdog

The phase watchdog runs an `asyncio.sleep(1)` loop and tracks two clocks:

- `idle_s = now - last_activity` (reset whenever stdout arrives).
- `elapsed_s = now - phase_start - paused_total`.

While `pending/<uuid>.json` exists without `.answer`/`.cancel`, the
watchdog accumulates wall-clock seconds into `paused_total` and forces
`last_activity = now`. On resume, the round timer measures only the time
Copilot was actually working — a long human wait does not poison the next
phase. (This was the bug that killed the first dogfood run.)

## Quality gates

The repo runs three gates in pre-commit and CI:

1. **`ruff check` + `ruff format --check`** — lint + style.
2. **`pip-audit --skip-editable`** — supply-chain vulnerability scan
   (AGENTS.md baseline #1).
3. **`pytest`** — full unit suite (with `--cov` in CI).

See [.pre-commit-config.yaml](.pre-commit-config.yaml) and
[.github/workflows/ci.yml](.github/workflows/ci.yml).
