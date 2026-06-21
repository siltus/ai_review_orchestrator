# aidor — AI Iterative Development Orchestrator & Reviewer

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

- bootstraps two custom agents, hooks, and a temporary runtime `AGENTS.md`
  copied from `src/aidor/resources/aidor_runtime_agents.md`;
- runs reviewer and coder phases in alternation until convergence or a
  hard budget is hit;
- enforces a **deny-by-default Guard** at the Copilot `preToolUse` /
  `permissionRequest` hook layer (tool allowlist + path-containment +
  shell-clause allowlist). The historical `--allow-tool` / `--deny-tool`
  flag matrix was abandoned because the CLI's flag grammar cannot
  express the policy we need; the hook (`aidor.hook_resolver`) is now
  the single enforcement point and can be audited end-to-end without a
  live Copilot subprocess. See [ARCHITECTURE.md](ARCHITECTURE.md) for
  the full model and the policy files under `src/aidor/policies/`;
- watches for idle / round-timeout, restarts via `--continue` with
  exponential back-off, and pauses timers when a hook is waiting on a
  human;
- escalates legitimate coder questions (`ask_user`) to a human with a
  long (24 h) wait — answers may come at 3 AM, but the hook will time
  out if no one responds within a day;
- keeps the machine awake across long sessions (Windows
  `SetThreadExecutionState`, Linux `systemd-inhibit`, macOS `caffeinate`).

## Install

Install a specific tagged release straight from GitHub with `pip` — no clone
required:

```pwsh
pip install "git+https://github.com/siltus/ai_review_orchestrator.git@v1.2.2"
```

Replace `v1.2.2` with whichever tag you want; list available tags with
`git ls-remote --tags https://github.com/siltus/ai_review_orchestrator.git`.
You can also pin to a commit SHA or follow a branch (less stable):

```pwsh
# pin to a specific commit
pip install "git+https://github.com/siltus/ai_review_orchestrator.git@<sha>"

# track the default branch
pip install "git+https://github.com/siltus/ai_review_orchestrator.git@master"
```

For local development against a clone (editable install with dev extras), see
[GETTING_STARTED.md](GETTING_STARTED.md); the short form is:

```pwsh
pip install -e ".[dev]"
```

### Windows: `WinError 2` / `.deleteme` install failures

If `pip install` against a system-wide Python (e.g. `C:\Python311\`) fails with:

```
ERROR: Could not install packages due to an OSError: [WinError 2]
The system cannot find the file specified:
'C:\Python311\Scripts\markdown-it.exe' -> 'C:\Python311\Scripts\markdown-it.exe.deleteme'
```

…the `Scripts\` directory isn't writable by your user. pip installs console
scripts by renaming the existing `.exe` to `.exe.deleteme` and dropping the new
one in place; without write permission on `Scripts\`, the rename fails. Pick
**one** of the following:

**1. Per-user virtual environment (simplest)** — works with stock `pip` and
needs no bootstrap:

```pwsh
python -m venv $env:USERPROFILE\.venvs\aidor
& "$env:USERPROFILE\.venvs\aidor\Scripts\Activate.ps1"
pip install "git+https://github.com/siltus/ai_review_orchestrator.git@v1.2.2"
aidor --version
```

Re-activate the venv (`& "$env:USERPROFILE\.venvs\aidor\Scripts\Activate.ps1"`)
in each new shell where you want to run `aidor`.

**2. `pipx` (cleaner if you install many CLI tools)** — installs aidor into its
own isolated venv under `%USERPROFILE%\pipx\` and shims the `aidor` command
into a user-writable directory. Needs a one-time pipx bootstrap first:

```pwsh
python -m pip install --user pipx
python -m pipx --version     # verify; should print a version number
python -m pipx ensurepath
python -m pipx install "git+https://github.com/siltus/ai_review_orchestrator.git@v1.2.2"
```

Use `python -m pipx …` (not bare `pipx …`) until you open a fresh shell —
`ensurepath` only updates `PATH` for **new** shells. After restarting
PowerShell, `pipx` and `aidor` are on `PATH` directly.

**3. Elevated PowerShell** — only if you really want the install in the system
Python. Right-click PowerShell → *Run as administrator*, then re-run the
`pip install` command. This gives pip permission to overwrite
`C:\Python311\Scripts\*.exe`.

## Quick start

```
aidor doctor
aidor run --coder <copilot-model-id> --reviewer <copilot-model-id>
```

Prefer not to remember model ids? Use interactive setup. It caches Copilot's
structured model catalog for 24 hours by default (override with
`--model-cache-ttl-hours`; use `0` to force refresh), sorts it by model id, then
lets you select coder/reviewer models and reasoning effort with arrow-key menus.
The catalog is loaded from Copilot ACP metadata first, with the live models API
as fallback; aidor does not ship a hard-coded model list:

```
aidor run --interactive
```

Optionally steer the run with extra instructions injected into every
reviewer and coder prompt — useful for things like "make sure the code
implements X features", "extra effort on security", or "make sure it's
cross-platform":

```
aidor run --coder <id> --reviewer <id> \
    --instructions "extra effort on security; make sure it's cross-platform"

# or load from a file:
aidor run --coder <id> --reviewer <id> \
    --instructions-file ./run-notes.md

# per-role overrides (additive on top of --instructions):
aidor run --coder <id> --reviewer <id> \
    --reviewer-instructions "be especially strict about API stability" \
    --coder-instructions "prefer minimal patches; do not refactor"
```

For GPT-family models, where reasoning effort cannot be encoded into the
model id (GitHub issue #1), forward Copilot's
`--reasoning-effort {low,medium,high,xhigh}` flag:

```
aidor run --coder gpt-5.5 --reviewer gpt-5.5 --effort xhigh

# or per-role (overrides --effort for that role only):
aidor run --coder gpt-5.5 --reviewer gpt-5.5 \
    --reviewer-effort xhigh --coder-effort high
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

## Safety highlights

- The coder is blocked from changing aidor policy/orchestration files such as
  `.aidor/allowed_exceptions.yml`, `.aidor/tool_allowlist.yml`,
  `.aidor/shell_allowlist.yml`, `.github/hooks/aidor.json`, and generated
  `.github/agents/aidor-*.md`. The reviewer may touch those only after careful
  consideration and should escalate to the human when in doubt.
- Git submodules are treated as external pinned dependencies: agents ignore code
  issues inside submodule paths and only review parent-repo integration.
- Agents are instructed to scan for configured MCP tools and use them when they
  are the right source. MCP policy is YAML-driven; denied MCP tool calls are
  recorded in `.aidor/logs/failed_mcp_tools.jsonl` and summarized with allowlist
  guidance.
- `state.json` writes are atomic and retried for transient Windows
  `PermissionError`s. If persistence still fails, aidor exits before launching
  another phase instead of continuing on stale state.
- `.aidor/ABORT` is a one-shot marker. A new run clears a stale marker after
  bootstrap and before the first phase starts.
- `AGENTS.md` is runtime-only: aidor copies
  `src/aidor/resources/aidor_runtime_agents.md` to the target repo during a
  run, backs up any project `AGENTS.md` under `.github/aidor-backups/`, and
  restores or removes it during teardown / `aidor clean`.

## Layout

```
.aidor/
  reviews/           review-NNNN-*.md (from reviewer)
  fixes/             fixes-NNNN-*.md (from coder)
  transcripts/       copilot --share outputs
  logs/              otel + qa + orchestrator logs + denied MCP tools
  pending/           human-question IPC (hook ↔ orchestrator)
  state.json         source of truth for resume / summary
  summary.md         final human-readable report
```
