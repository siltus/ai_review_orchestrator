# aidor — AI Review Orchestrator

> ## ⚠️ DISCLAIMER — READ BEFORE USE
>
> **This is a personal side project. It is not a product. It is not
> intended for anyone's use except my own.**
>
> - **No warranty, no support, no guarantees** of any kind, express or
>   implied. The software is provided **"AS IS"**.
> - **You run it at your own risk.** It autonomously drives LLM agents
>   that can edit files, run shell commands, and consume large amounts
>   of API quota.
> - **The author accepts no responsibility** for any cost, loss, or
>   damage — financial, physical, intellectual, reputational, or
>   otherwise — arising from use of this tool. That explicitly includes
>   (but is not limited to) **runaway bills on LLM subscriptions, API
>   credits, cloud services, or Copilot quotas**, broken code,
>   destroyed data, leaked secrets, or any downstream consequence of
>   actions an agent takes on your behalf.
> - Long autonomous runs can consume **substantial** tokens / premium
>   requests / AI Units. Monitor your usage. Set hard budgets at the
>   provider level if you care.
>
> You are free to fork, modify, and reuse this code under the **MIT
> License** (a permissive license — do whatever you want, just keep
> the copyright notice). If you fork it, it's yours; the same
> disclaimer applies to you.

---

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
