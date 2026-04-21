# aidor — AI Review Orchestrator

`aidor` drives two LLMs (a **coder** and a **reviewer**) through the GitHub
Copilot CLI in an automated review↔fix loop. It is a thin supervisor around
`copilot -p --autopilot --output-format=json` that:

- bootstraps two custom agents + hooks + an `AGENTS.md` managed block;
- runs reviewer and coder phases in alternation until convergence or a
  hard budget is hit;
- enforces a **Guard** allow/deny tool matrix (`--allow-tool` / `--deny-tool`);
- watches for idle / round-timeout, restarts via `--continue` with
  exponential back-off, and pauses timers when a hook is waiting on a
  human;
- escalates legitimate coder questions (`ask_user`) to a human with a
  long (24 h) wait — answers may come at 3 AM, but the hook will time
  out if no one responds within a day;
- keeps the machine awake across long sessions (Windows
  `SetThreadExecutionState`, Linux `systemd-inhibit`, macOS `caffeinate`).

## Install

```
pip install -e ".[dev]"
```

## Quick start

```
aidor doctor
aidor run --coder <copilot-model-id> --reviewer <copilot-model-id>
```

See [plan.md](plan.md) for design details.

## Commands

| Command          | Purpose                                        |
|------------------|------------------------------------------------|
| `aidor run`      | Full review↔fix loop                           |
| `aidor status`   | Print current state from `.aidor/state.json`   |
| `aidor summary`  | Render summary table + `.aidor/summary.md`     |
| `aidor doctor`   | Environment checks                             |
| `aidor clean`    | Remove `.aidor/` run artefacts                 |
| `aidor abort`    | Write `.aidor/ABORT`; the phase watchdog terminates the running Copilot subprocess promptly |

## Layout

```
.aidor/
  reviews/           review-NNNN-*.md (from reviewer)
  fixes/             fixes-NNNN-*.md (from coder)
  transcripts/       copilot --share outputs
  logs/              otel + qa + orchestrator logs
  pending/           human-question IPC (hook ↔ orchestrator)
  state.json         source of truth for resume / summary
  summary.md         final human-readable report
```
