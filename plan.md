# aidor — AI Review Orchestrator · Plan

> Living design doc. Keep in sync with `reqs.md`. Update as decisions are made.

## 1. Goal

Automate the review↔fix loop between two LLMs (a **coder** and a **reviewer**) driven through the GitHub Copilot CLI, on any repo, cross-platform, with watchdog + safety guard + optional human escalation — so the human can walk away and come back to a production-ready repo (or a clear escalation).

---

## 2. Research — what already exists

Before designing from scratch, we surveyed existing tooling. Summary:

### 2.1 GitHub Copilot CLI (primary backend)

The modern `copilot` binary (separate from the retired `gh copilot` extension) is far more capable than we initially assumed. It already provides most of the "orchestration plumbing" we were going to build:

| Need | Copilot CLI feature | Notes |
|------|--------------------|-------|
| Non-interactive invocation | `copilot -p "<prompt>" --output-format=json` | JSONL (one JSON object per line). Process exits when done. Clean completion signal — no sentinel parsing. |
| Autonomous multi-step work | `--autopilot` + `--max-autopilot-continues=N` | No approval-per-step. Perfect for headless rounds. |
| Model selection | `--model=MODEL` or `COPILOT_MODEL` env var | Accepts same strings as the `/model` slash command. |
| Role / persona | **Custom agents** in `.github/agents/<name>.md` invoked via `--agent=<name>` | Markdown file with frontmatter: `description`, `model`, `tools`, `mcp-servers`. A built-in `code-review` agent already exists; we specialize our own. |
| Code baseline instructions | `AGENTS.md` + `.github/copilot-instructions.md` | Auto-loaded by Copilot every session. `copilot init` bootstraps them. |
| Path sandbox | Trusted directories + `--add-dir=PATH` + `--disallow-temp-dir` | Runs from `cwd`. We launch inside the repo → confined by default. |
| Tool permissions (our "Guard") | `--allow-tool='shell(git:*)'`, `--deny-tool='shell(git push)'`, `--deny-tool='shell(rm)'`, pattern `Kind(argument)` with `shell/write/read/url/memory/MCP_SERVER` | Deny rules take precedence over allow, even with `--allow-all`. Replaces most of our custom command-interception layer. |
| Controlled interactivity | Keep `ask_user` **enabled** (do NOT pass `--no-ask-user`); intercept via hooks | We want the coder to be able to ask clarifications (e.g., lint exception approval). The hook layer answers known cases from policy; unknowns are escalated to the human with **no timeout** (the human may be asleep — see §9.4). Copilot-the-subprocess never blocks on real stdin; our hook process is the thing that waits. |
| Programmatic permission / elicitation decisions | **Hooks** in `.github/hooks/*.json` — `preToolUse`, `permissionRequest`, `notification(elicitation_dialog)`, `agentStop`, … | `preToolUse` on the `ask_user` tool can synthesize an answer via `modifiedArgs` or deny with a reason. `permissionRequest` auto-allows/denies with reason fed back to the LLM. `notification(elicitation_dialog)` can inject `additionalContext` asynchronously. `agentStop` can force another turn. |
| Completion signal | `--output-format=json` → `stopReason: "end_turn"` on the last event + process exit | Deterministic, no sentinel parsing. |
| Session persistence / resume | `--resume=<ID>` or `--continue`, `/session`, `--share=PATH` (writes full transcript to Markdown) | `--share` gives us per-round transcripts for free. |
| Session keep-alive | `/keep-alive busy` slash command | Prevents the machine from sleeping mid-round. |
| Telemetry / metrics | `COPILOT_OTEL_FILE_EXPORTER_PATH=...` → JSONL traces with `gen_ai.usage.input_tokens`, `output_tokens`, `github.copilot.cost`, `github.copilot.aiu`, per-turn durations | Feeds our final summary table for free — no scraping. |
| Programmatic driver (advanced) | **ACP server** via `copilot --acp --stdio` (Agent Client Protocol, NDJSON over stdio) | Official TypeScript SDK (`@agentclientprotocol/sdk`). Gives explicit `prompt / sessionUpdate / requestPermission` callbacks. Python SDK status: not official yet. Useful if we later want one long-lived session per agent instead of one subprocess per round. |
| Subagent concurrency safety | `COPILOT_SUBAGENT_MAX_DEPTH` / `COPILOT_SUBAGENT_MAX_CONCURRENT` | Prevents runaway agent spawning. |

**Implication:** aidor is thinner than originally planned. Most of the Guard and Watchdog are configuration + a small hook script, not hand-written stdin/stdout interception.

### 2.2 Other tools surveyed (and why not)

- **Aider** (open-source AI pair-programmer CLI). Excellent tool, but it talks directly to LLM APIs. We are constrained to Copilot CLI permissions, so Aider would bypass our quota/auth model. Out of scope for v1. Could be an alternative coder backend later.
- **LangGraph / AutoGen / CrewAI** (multi-agent orchestration frameworks). Designed for direct-API multi-agent graphs. They'd want to own the loop and speak to models directly, not drive two CLI subprocesses. Overkill for a strict 2-agent ping-pong over Copilot CLI. Rolling our own tiny state machine stays simpler and maps 1:1 to `reqs.md`.
- **Continue.dev / Cline / Roo Code** (IDE-embedded agents). Not CLI-first; require an IDE. Rejected.
- **pexpect / pywinpty** (terminal automation). Would be needed if we had to screen-scrape Copilot's interactive TUI. Not needed — `-p`/`--autopilot`/`--output-format=json` is the proper entry point.
- **OpenTelemetry Collector** (optional sidecar). If a user wants to ship telemetry somewhere, they can; we emit JSONL to a file and parse it locally. No dependency added.

### 2.3 Revised high-level architecture

```
┌───────────────────────────── aidor (thin orchestrator) ─────────────────────────────┐
│                                                                                     │
│   State machine ──► for each round: subprocess copilot ─p "…" --agent=aidor-X …    │
│     ▲                                        │                                      │
│     │                           JSONL events │     writes .aidor/reviews/...md     │
│     │                                        ▼     or .aidor/fixes/...md            │
│     │                              ┌──────────────────┐                             │
│     │                              │  Copilot CLI     │   ← AGENTS.md (baseline)    │
│     │                              │  + custom agents │   ← .github/agents/*.md     │
│     │ hook decisions ◀────────────▶│  + hooks         │   ← .github/hooks/aidor.json│
│     │                              └──────────────────┘                             │
│     │                                        │                                      │
│     └── parse review footer, OTel JSONL, transcript.md ──► state.json, summary.md   │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

**What aidor owns (the irreducible core):**
1. Bootstrap — write `AGENTS.md`, `.github/agents/aidor-coder.md`, `.github/agents/aidor-reviewer.md`, `.github/hooks/aidor.json` (idempotent, diff-friendly).
2. Round loop + convergence rule (§6).
3. Review/fix file numbering, footer parsing, diff tracking.
4. Subprocess supervision: idle/round timeouts, restart policy, `--continue` on resume (§7).
5. Console UI + aggregated summary (pulling from Copilot's OTel JSONL + our review footers) (§10).
6. Escalation path (console v1, Telegram v1.1).

Everything else (sandboxing, non-interactive mode, per-tool allow/deny, autopilot, session transcripts, token/cost metrics) is Copilot CLI config we generate.

---

## 3. Tech stack

- **Language:** Python 3.11+.
  - Rationale: cross-platform, strong subprocess/async, rich CLI ecosystem, we don't need the ACP SDK for v1 since `-p --output-format=json` is sufficient. If/when we move to a long-lived ACP session per agent, we can add a thin Node ACP helper or wait for an official Python ACP SDK.
- **CLI framework:** `typer`.
- **Process control:** `asyncio.subprocess` — one `copilot` child per phase, JSONL parsed line-by-line.
- **Console UI:** `rich` (colored dual-stream output, spinner, final table).
- **Logging:** stdlib `logging` + rotating file handler under `<repo>/.aidor/logs/`.
- **Config:** CLI flags + optional `aidor.toml` in repo or user home.
- **Telemetry parsing:** plain stdlib JSON (Copilot OTel file-exporter is JSONL).
- **Notifications (deferred to v1.1):** Telegram via `httpx`.
- **Tests:** `pytest` + `pytest-asyncio`; a **fake `copilot` binary** (small Python script on `PATH`) for integration tests that emits scripted JSONL — avoids paying for real Copilot calls in CI.

No paid services, no sign-ups required for v1.

---

## 4. Roles — implemented as Copilot custom agents

Per `reqs.md` both agents are told a different lie about their counterpart; we do **not** reveal either is an LLM. These personas live in the target repo as Copilot custom agents (overridable by the human).

### 4.1 `.github/agents/aidor-coder.md`

```markdown
---
name: aidor-coder
description: Implements fixes for the current review on a mission-critical codebase.
model: <passed at runtime via --model>
tools: ["*"]
infer: false
---
You are a developer on a mission-critical codebase; bugs can put human life at risk.
Your reviewer is a senior, very busy developer — respect their time.
Read ONLY the newest file in `.aidor/reviews/` (you MAY follow explicit references to older reviews).
When done, write `.aidor/fixes/fixes-NNNN.md` summarizing what you changed and why.
Adhere to the code baseline in AGENTS.md without exception.
Never push to a remote, never install anything globally, never touch files outside this repo.

If you genuinely need a human decision (for example: a lint-rule exception, an
ambiguous spec, a file outside the repo you need permission to read), use the
`ask_user` tool with a short, self-contained question. The orchestrator will
answer from policy when it can, or escalate to the human. Do NOT use `ask_user`
for things you can decide yourself or read from AGENTS.md.
```

### 4.2 `.github/agents/aidor-reviewer.md`

```markdown
---
name: aidor-reviewer
description: Senior reviewer auditing the repo for production-readiness.
model: <passed at runtime via --model>
tools: ["*"]
infer: false
---
You are a very senior developer. The author is a junior human who makes silly mistakes
and often forgets to document work. Be extra thorough. You may lecture them slightly
when they repeat mistakes and reference earlier `review-NNNN.md` files when you do.

Deduce features from code + docs. Check: bugs, stale documentation, test coverage (≥90%),
missing regression tests for past bugfixes, linter compliance, supply-chain hygiene,
presence of AGENTS.md / README / ARCHITECTURE / GETTING_STARTED.

Write `.aidor/reviews/review-NNNN.md` with sections: Summary, Issues (severity/type/file/line/rationale),
Suggested fixes, Production-readiness verdict.

End the file with this machine-readable footer, on its own lines:

<!-- AIDOR:STATUS=CLEAN|ISSUES_FOUND -->
<!-- AIDOR:ISSUES={"critical":0,"major":0,"minor":0,"nit":0} -->
<!-- AIDOR:PRODUCTION_READY=true|false -->
```

Per-round invocation (conceptually):

```
copilot -p "$PHASE_PROMPT" \
        --agent=aidor-reviewer \
        --model=$REVIEWER_MODEL \
        --autopilot \
        --output-format=json \
        --share=.aidor/transcripts/reviewer-0003.md \
        --allow-tool='shell(git:*)' \
        --deny-tool='shell(git push)' \
        # NOTE: ask_user stays enabled; handled by our hooks (§9.4).
        [... see §9 full policy ...]
```

`$PHASE_PROMPT` is minimal — something like: *"Run a review of this repo for round 3. Previous review was at `.aidor/reviews/review-0002-....md`. Write your new review file now."* The persona/baseline/rules all come from the agent file + AGENTS.md.

---

## 5. Code baseline — lives in AGENTS.md

aidor writes a managed block into the repo's `AGENTS.md` on bootstrap (idempotent, between HTML comment markers so humans can edit the rest of the file). The block encodes:

1. **Supply-chain:** run the language-appropriate auditor as a build step or pre-commit hook (`pip-audit`, `npm audit`, `cargo audit`, etc.).
2. **Test coverage:** ≥90% line coverage; bugfixes MUST ship with a regression test.
3. **Linters / style:** must pass. Exclusions require human consent; pre-approved list at `.aidor/allowed_exceptions.yml`.
4. **Documentation:** `README.md`, `ARCHITECTURE.md`, `GETTING_STARTED.md` must exist and be current.
5. **Agent manifest:** this `AGENTS.md` file itself.
6. **Guard rules** (mirror of §8) — stated explicitly for belt-and-suspenders.

On rule ambiguity for a given repo, orchestrator halts and escalates so we can make the rule deterministic and update the source.

---

## 6. Repo artifacts

```
<repo>/
  AGENTS.md                             # aidor-managed block + human free-form
  .github/
    agents/
      aidor-coder.md                    # generated if absent
      aidor-reviewer.md                 # generated if absent
    hooks/
      aidor.json                        # preToolUse / permissionRequest / agentStop hooks
    copilot-instructions.md             # optional, generated by `copilot init` if user wants
  .aidor/
    reviews/
      review-0001-YYYYMMDD-HHMMSS.md
      review-0002-....md
    fixes/
      fixes-0001-....md
    transcripts/
      reviewer-0001.md                  # from --share
      coder-0001.md
    logs/
      orchestrator.log
      otel-0001.jsonl                   # from COPILOT_OTEL_FILE_EXPORTER_PATH
    state.json                          # round, status, hashes, timings
    summary.md                          # final table
    config.snapshot.toml                # effective config for this run
    allowed_exceptions.yml              # linter-exclusion allowlist
```

`.aidor/` is added to `.gitignore` on first run (asking the human once). `.github/agents/*` and `AGENTS.md` are intentionally NOT gitignored — they are part of the repo's contract.

---

## 7. Orchestration state machine

```
IDLE
  → BOOTSTRAP            (write AGENTS.md block, agents, hook file, snapshot config)
  → REVIEW_REQUESTED → REVIEW_RUNNING → REVIEW_DONE
  → [convergence check]
      ├─ if CLEAN & PRODUCTION_READY=true → SUMMARIZE → EXIT
      └─ else → FIX_REQUESTED → FIX_RUNNING → FIX_DONE → loop
  → SUMMARIZE → EXIT
```

### 7.1 Convergence rule

End the loop when **all** of:

- `AIDOR:STATUS=CLEAN`, **and**
- `critical == 0 && major == 0`, **and**
- `AIDOR:PRODUCTION_READY=true`.

The first time a review comes back with `critical==0 && major==0` but `PRODUCTION_READY` is not yet `true`, the orchestrator runs a dedicated **readiness-gate round** with the prompt: *"No critical/major issues remain. State whether this repo is production-ready and answer with the AIDOR footer."*

- **Hard cap:** `--max-rounds 10`. On cap hit → SUMMARIZE with status `UNCONVERGED` and escalate.
- Expected typical run: 5–6 rounds + 1 readiness gate.

---

## 8. Watchdog

`ask_user` stays enabled (§4, §9.4) so the agent CAN legitimately stop and wait for an answer — but that wait is handled by our hook, not by stdin of the subprocess. From the orchestrator's point of view, a healthy agent is either (a) emitting JSONL events or (b) blocked in a hook we ourselves are running. Both are observable.

Per phase, the orchestrator monitors:

- **JSONL activity timeout** (default 120 s, `--idle-timeout`): no new event from Copilot AND no aidor hook currently executing. On trigger:
  1. Log a warning, flush buffers.
  2. If still silent +60 s → `SIGTERM`, then `copilot --continue` to resume the most recent session with a nudge prompt: *"Please continue and finish the current task. Re-read `.aidor/reviews/review-NNNN.md` and `.aidor/state.json` for context."*
- **Hook-bounded human-wait:** while a hook is waiting for the human (console / Telegram), **all aidor timers are paused**. The human can take as long as needed — hours, overnight — without tripping the watchdog or the round timeout. The only bound is a **Telegram re-ping cadence** (v1.1, default every 30 min) so the notification isn't lost; there is no "give up and default" step. The round resumes the moment the human replies.
- **Total round timeout:** `--round-timeout` (default **3 h = 10 800 s**), clock **excludes any time spent in human-wait**. Early rounds on buggy repos can legitimately take hours; we optimize for "don't give up too early" over "fail fast". Cross that → kill + one `--continue` retry → if that also times out, escalate.
- **Max 3 restarts per round** → escalate.
- **Hook-based assist:** `.github/hooks/aidor.json` registers a `notification` hook matching `agent_idle` and `elicitation_dialog` to emit breadcrumbs into `orchestrator.log`; and an `agentStop` hook that records the final stop reason.
- **Escalation (v1):** loud red banner, non-zero exit. **(v1.1):** Telegram ping with periodic re-ping until answered; no auto-default.

See §17 for the full long-session durability contract.

---

## 9. Guard (safety layer)

Implemented as Copilot CLI flags + one small hook script. No stdin/stdout interception.

### 9.1 Allow/deny flag baseline (applied to every invocation)

Allowed without asking:

```
--allow-tool='read'                        # any file read inside trusted dirs
--allow-tool='write'                       # any file write inside trusted dirs
--allow-tool='shell(git status)'
--allow-tool='shell(git diff)'
--allow-tool='shell(git log)'
--allow-tool='shell(git add)'
--allow-tool='shell(git commit)'
--allow-tool='shell(git restore)'
--allow-tool='shell(git stash)'
--allow-tool='shell(git branch)'
--allow-tool='shell(git switch)'
--allow-tool='shell(git checkout)'
# + project build/test/lint/audit commands detected from manifests
```

Always denied (deny takes precedence):

```
--deny-tool='shell(git push)'
--deny-tool='shell(git remote)'
--deny-tool='shell(git config --global)'
--deny-tool='shell(sudo)'
--deny-tool='shell(rm -rf /)'
--deny-tool='shell(npm install -g)'
--deny-tool='shell(pnpm add -g)'
--deny-tool='shell(pip install)'           # use venv inside repo, see §9.3
--deny-tool='shell(cargo install)'
--deny-tool='shell(go install)'
--deny-tool='shell(choco)'
--deny-tool='shell(winget)'
--deny-tool='shell(apt)'
--deny-tool='shell(apt-get)'
--deny-tool='shell(brew)'
--deny-tool='shell(scoop)'
--deny-tool='shell(curl)'                  # blanket; project-approved script whitelist lifts this
--deny-tool='shell(wget)'
--deny-url='*'                             # url allowlist managed below
```

Path sandbox:

```
--cwd <repo>                               # implicit
--disallow-temp-dir                        # force writes under repo
# no --allow-all-paths, no --add-dir outside repo
```

### 9.2 Hook script (`.github/hooks/aidor.json`)

Covers patterns the flag matrix cannot express (path containment checks, environment checks, stronger shell-line parsing). `bootstrap.py` writes `.github/hooks/aidor.json` whose entries invoke `python -m aidor.hook_resolver` (see `src/aidor/hook_resolver.py`); the resolver receives the JSON payload from `preToolUse` / `permissionRequest` on stdin and returns `{"permissionDecision":"deny","permissionDecisionReason":"..."}` on violation. Responsibilities:

- Re-check that any `write`/`edit`/`create` target resolves under the canonical repo root (symlink-safe).
- Veto shell commands whose fully expanded argv escapes repo bounds (e.g., `cp file /etc/...`).
- Log every tool invocation to `.aidor/logs/orchestrator.log`.

### 9.3 Project-local installs

Lockfile-gated (`poetry.lock`, `package-lock.json`, `Cargo.lock`, etc.). Flag `--allow-local-install` (default: on; disable for hermetic runs). When on, aidor adds targeted allow rules: `--allow-tool='shell(poetry install)'`, `--allow-tool='shell(npm ci)'`, etc.

Violations → hook denies with reason → halt phase, red banner, v1.1 Telegram.

### 9.4 Interactivity — answering the agent's questions

The coder legitimately needs to ask the human in a few well-defined cases (e.g., "May I add `# noqa: E501` in `src/foo.py:42` because the URL literal can't be wrapped?"). Ban-by-default (`--no-ask-user`) is too blunt. Instead we keep `ask_user` enabled and wrap it.

**Resolver pipeline (runs inside our hook script):**

1. **Policy lookup.** Classify the question using short regex/keyword rules from `.aidor/allowed_exceptions.yml` and a small shipped `question_classes.yml` (lint-exception request, missing-spec clarification, permission-to-read-outside-repo, tool-not-installed, etc.). If the class has a deterministic answer (e.g., "lint-exception for rule X in context Y is pre-approved"), the hook synthesizes the answer and returns.
2. **State-derived answer.** If the question is about a previous review ("which file from review-0002 did you mean?"), the hook reads `.aidor/state.json` + review store and can answer mechanically.
3. **Human escalation — bounded by the hook timeout.** Otherwise the hook writes a pending request under `.aidor/pending/<uuid>.json` and waits for the orchestrator to drop a `<uuid>.answer` (or a `<uuid>.cancel` / global `.aidor/ABORT`). The watchdog is paused (§8) and the round-timeout clock excludes this interval. The wait is capped only by the Copilot hook-process timeout, currently configured to **24 hours** in `.github/hooks/aidor.json`; in practice the human may answer immediately, in an hour, or the next morning. In v1.1 the same prompt is mirrored to Telegram, with a configurable **re-ping cadence** (default every 30 min) until answered, so the notification isn't forgotten on a silenced phone.
4. **Cancellation.** The wait can also end if the human cancels the question (Ctrl-C at the orchestrator's prompt, or an `aidor abort <repo>` command from a second terminal which writes `.aidor/ABORT`). On a per-question cancel the hook returns `deny` with a "question cancelled by human; proceed with a safe default" reason and the agent is expected to choose a safe fallback — the round continues. On a global abort (`aidor abort` / `.aidor/ABORT`) the phase watchdog terminates the Copilot subprocess and the orchestrator shuts down cleanly. (v0.1: `aidor abort` flips `state.json` to `aborted` and the watchdog reacts within ~1 s.)
5. **Audit.** Every Q&A pair is appended to `.aidor/logs/qa.jsonl` (timestamp, class, question, answer, wait duration, source=policy|state|human|cancelled).

**Hook wiring:**

- `preToolUse` matcher `^ask_user$` is the primary entry point; the hook outputs `modifiedArgs` to inject the synthesized answer, or `permissionDecision: deny` with `permissionDecisionReason` carrying the answer text (the LLM reads this).
- `notification` matcher `^elicitation_dialog$` is a belt-and-suspenders path for flows where Copilot elicits outside a tool call; it returns `additionalContext` with the resolved answer.
- Both run the same Python resolver (`python -m aidor.hook_resolver`, implemented in `src/aidor/hook_resolver.py`) for consistency; the hook JSON just points at it.

> **v0.1 status:** step 1 (policy lookup) is implemented against `src/aidor/policies/question_classes.yml`; step 2 (state-derived answers) is scaffolded as `_lookup_state_answer()` and currently always returns `None`, so every non-policy question escalates straight to the human. `GETTING_STARTED.md` must reflect this until state lookup gains real rules.

This keeps interactivity intact for genuine clarifications while guaranteeing the subprocess never hangs waiting on a forgotten TTY — and without ever forcing the human to answer on a schedule.

---

## 10. CLI surface

```
aidor run --coder <model> --reviewer <model> --repo <path>
          [--max-rounds 10]
          [--idle-timeout 120] [--round-timeout 10800]
          [--allow-local-install/--no-allow-local-install]
          [--telegram]           # v1.1
          [--resume]             # pick up from state.json, use copilot --continue
          [--dry-run]            # print plan & generated Copilot invocations, don't run

aidor status  <repo>    # show current state.json + last review
aidor summary <repo>    # render summary table for completed run
aidor clean   <repo>    # wipe .aidor/ (keeps AGENTS.md + .github/agents/*)
aidor doctor  <repo>    # verify python version, copilot binary + --version, repo path, wake-lock availability
```

Example matching `reqs.md`:

```
aidor run --coder opus4.7 --reviewer gpt5 --repo d:\src\somerepo
```

Model strings are passed verbatim to Copilot CLI (`--model=...`); validation against `copilot /model` is not implemented in v0.1 — invalid strings surface as the first round's launch failure. Documented in `GETTING_STARTED.md`.

---

## 11. Console output

- Banner per phase: `** orchestrator asking for a review (round #N) **`.
- Live-stream: parse Copilot's JSONL (agent messages, tool calls, hook decisions) and render:
  - `[reviewer]` (cyan) / `[coder]` (magenta) / `[aidor]` (yellow) / `[guard]` (red for denies).
- Right-aligned spinner + elapsed time per phase.
- Everything mirrored to `.aidor/logs/orchestrator.log` (no ANSI) and per-phase `.aidor/transcripts/*.md` (via Copilot's `--share`).
- Final summary table (`rich.table`) — columns built from `.aidor/logs/otel-*.jsonl` + review footers:
  - round # | phase durations (reviewer/coder) | tokens in/out | premium requests | issues found | by severity | by type | fixed this round | carried over | production-ready verdict.

---

## 12. Module layout

Actual v0.1 layout (keep in sync with `src/aidor/` + `tests/`):

```
src/aidor/
  __main__.py
  cli.py                 # typer app (run/status/summary/clean/doctor)
  orchestrator.py        # state machine, main loop
  phase.py               # single Copilot invocation: build argv, stream JSONL,
                         # parse; also owns idle / round-timeout watchdog and
                         # SIGTERM + --continue restart policy
  bootstrap.py           # idempotent write of AGENTS.md block, agents/, hooks/
  review_store.py        # file numbering, footer parser, diff between rounds
  state.py               # state.json load/save, resume
  hook_resolver.py       # Copilot hook entry point: policy → state → human
                         # (`python -m aidor.hook_resolver`)
  guard_profile.py       # build --allow-tool/--deny-tool flag lists + hook JSON
  telemetry.py           # parse Copilot OTel JSONL
  summary.py             # aggregate + render final table, write summary.md
  wake_lock.py           # cross-platform wake lock (keep-alive)
  config.py
  policies/
    allowed_exceptions.yml
    question_classes.yml # source of truth for policy-answered ask_user classes
  agent_templates/
    aidor-coder.md
    aidor-reviewer.md
    agents_md_block.md   # the managed AGENTS.md section
  # (hook JSON and guard flag matrix are generated in-code from
  #  `bootstrap.py` + `guard_profile.py`, not shipped as separate template
  #  files.)
  # notify/telegram.py — deferred to v1.1, not present yet.

tests/
  conftest.py
  fake_copilot.py                 # PATH-shimmable script emitting scripted JSONL
  test_bootstrap.py
  test_cli.py
  test_guard_profile.py
  test_hook_resolver.py
  test_orchestrator_integration.py
  test_orchestrator_prompt.py
  test_phase_watchdog.py
  test_review_store.py
  test_state.py
  test_summary.py
  test_telemetry.py
  test_wake_lock.py

pyproject.toml
README.md
GETTING_STARTED.md
ARCHITECTURE.md
AGENTS.md
plan.md
reqs.md
```

---

## 13. Copilot CLI spike (M0)

Narrower than before — most questions are answered from docs. The spike was a historical M0 deliverable (no `scripts/spike_copilot.py` ships in v0.1); its checklist is preserved here for traceability and is now covered by `aidor doctor` plus the fake-copilot test harness. The items verified empirically were:

1. `copilot` binary is on PATH and authenticated (`copilot /user show`).
2. `copilot /model` lists the exact model strings we'll pass (record the list for `GETTING_STARTED.md` and `aidor doctor`).
3. `copilot -p "say hi" --autopilot --output-format=json --no-ask-user` exits cleanly and produces parseable JSONL with a final `stopReason: "end_turn"`.
4. A minimal `.github/agents/aidor-test.md` is picked up via `--agent=aidor-test`.
5. A minimal `.github/hooks/aidor.json` `preToolUse` hook can deny a tool call and the reason reaches the LLM.
6. `--share=out.md` writes a readable transcript.
7. `COPILOT_OTEL_FILE_EXPORTER_PATH=trace.jsonl` produces JSONL with the token/cost attributes we need.
8. `--continue` successfully resumes the prior session after SIGTERM.

Output of the spike → pins `phase.py` argv template + `GETTING_STARTED.md`.

---

## 14. Milestones

(Updated: M7 now includes the question resolver; M8 picks up the long-session durability work.)


1. **M0 Spike** — Copilot CLI verification (§13), document findings in `GETTING_STARTED.md`.
2. **M1 Skeleton** — `typer` CLI, config, logging, `.aidor/` bootstrap, `state.json`, `aidor doctor`.
3. **M2 Bootstrap writer** — idempotent generation of `AGENTS.md` block, `.github/agents/aidor-{coder,reviewer}.md`, `.github/hooks/aidor.json`, guard scripts.
4. **M3 Phase runner** — `phase.py`: build argv from `guard_profile` + flags, spawn, stream JSONL, capture transcript/OTel, detect `end_turn`.
5. **M4 One round** — reviewer phase → writes review file → coder phase → writes fixes file. Parse footer.
6. **M5 Multi-round loop** — convergence rule (§7.1), readiness-gate round, `--max-rounds`, `--resume` via `--continue`.
7. **M6 Watchdog** — idle + round timeouts, restart policy, hook-fed breadcrumbs.
8. **M7 Guard + question resolver** — path-containment hook, linter allowed-exceptions loader, `--allow-local-install` lockfile gating, **`ask_user` interception pipeline (policy → state → human → cancel/hook-timeout) with `qa.jsonl` audit**.
9. **M8 Long-session durability** — wake-lock, `--continue` retry policy with back-off, OTel JSONL rotation, artifact pruning, `aidor status` live view, stderr draining, hook-bounded idle detection.
10. **M9 Summary** — OTel parsing + final table + `summary.md`.
11. **M10 Tests & docs** — fake-copilot integration harness (including scripted `ask_user` + mid-round SIGTERM + `--continue`), unit tests, polish `README.md` / `GETTING_STARTED.md`.
12. **M11 (v1.1)** — Telegram notifier, `--web-status`, persistent permission hints, optional ACP-based long-lived sessions.

---

## 15. Open items / decisions pending

- [ ] Empirical results of the M0 spike (model-string list, JSONL shape, hook decision propagation).
- [ ] Should we use Copilot's built-in `code-review` agent as a *supplement* to `aidor-reviewer` (delegated sub-agent for mechanical diff audits)?
- [ ] Default list of **Allowed Exceptions** per linter (grow organically from real repos).
- [ ] Summary table final column set (confirm after first full run).
- [ ] Whether to expose hook-based human-approval bridge in v1 (simple CLI prompt) or wait for v1.1 Telegram.
- [ ] If Copilot CLI's `-p`/`--autopilot` round-trip overhead becomes a problem, migrate `phase.py` to ACP (long-lived session per agent). Python ACP SDK maturity at that time will decide.

---

## 17. Long-session durability

Rounds can legitimately run **hours** (per `reqs.md` and confirmed user expectation). A multi-hour `copilot -p` subprocess is supported, but several failure modes become non-trivial at that scale. We address each explicitly.

### 17.1 Context / token budget

- Copilot CLI has built-in **auto-compaction at ~95% of context**; no action needed from us.
- We additionally emit a `preCompact` hook breadcrumb into `orchestrator.log` so we can see when it happened and correlate with any quality dips.
- If a phase compacts more than N=3 times (configurable), we log a warning — it's a signal the task is too big for a single round and the reviewer should probably partition issues across rounds.

### 17.2 Authentication / token refresh

- Copilot CLI manages its own OAuth token via `~/.copilot` (or `COPILOT_HOME`); long sessions renew transparently.
- v1.1: `aidor doctor` will check auth before kickoff (`copilot /user show`) so we fail fast instead of 2 h in. v0.1's `doctor` only verifies the `copilot` binary is on PATH and reports `--version`.
- If the session errors out with auth (`errorOccurred` hook → `error_context: system` + auth keywords), we DO NOT silently retry — we escalate immediately (re-auth requires the human).

### 17.3 Subprocess I/O

- JSONL is read line-by-line with `asyncio.StreamReader.readline()`; we never buffer the full transcript in memory.
- Line length cap: defensive 1 MiB per line; over → truncate + warn (should never happen, but prevents pathological OOM).
- stderr is drained to `.aidor/logs/<role>-<round>.stderr`.
- We use `PYTHONUNBUFFERED=1` and, on Windows, `CREATE_NEW_PROCESS_GROUP` so `SIGTERM` cleanly terminates the whole Copilot tree.

### 17.4 Machine sleep / lid-close

- Copilot CLI has `/keep-alive busy`, but that's a slash command usable only in interactive mode — not from `-p`.
- aidor itself requests a system-level wake lock for the duration of a run:
  - **Windows:** `ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)` on start, cleared on exit.
  - **Linux:** best-effort `systemd-inhibit --what=idle:sleep --who=aidor --why="long review run" -- <child>`; fall back to `caffeine`/`xdg-screensaver` if available; otherwise log a one-line warning (we do not hard-require this).
  - **macOS** (future): `caffeinate -imsu`.
- Flag `--no-keep-awake` to disable, for hermetic CI / server environments.

### 17.5 Network blips

- Copilot CLI retries model calls internally; transient network errors do not kill the session.
- If the session does die (`errorOccurred` with `recoverable: true` or a non-zero exit without `end_turn`), aidor performs up to **3** `copilot --continue` retries per round, with exponential back-off (30 s, 2 min, 10 min). This is the same restart counter as §8.
- All retries record a row in `.aidor/state.json` under `rounds[i].restarts[]` for the final summary.

### 17.6 Sub-command timeouts inside the agent

- The coder may kick off a very long test suite. Copilot's own shell tool has no hard timeout by default, which is what we want.
- Our hook script logs every `shell` tool invocation start/end so the human running `aidor status` can see "currently blocked on `pytest`, 42 min elapsed".
- If a single `shell` tool exceeds `--tool-timeout` (default 45 min), our `preToolUse` for subsequent calls starts annotating a warning; we do NOT forcibly kill, because killing a long test run rarely helps — the human should step in. (Configurable via `--kill-long-tools`.)

### 17.7 Disk pressure (transcripts / telemetry)

- Per-phase transcript (`--share`) plus OTel JSONL can reach tens of MB over a 3 h round.
- aidor enforces `--max-artifact-mb` (default 256 MB total under `.aidor/`) and prunes oldest `.aidor/transcripts/*.md` beyond that limit. Reviews and fixes files are never pruned.
- Telemetry JSONL is rotated per round (`otel-0001.jsonl`, `otel-0002.jsonl`, ...); summarized values are extracted into `state.json` so we don't need to re-parse old files.

### 17.8 Observability while it runs

- Live console already shows streaming events. For longer runs, `aidor status <repo>` in a second terminal prints:
  - current round + phase + elapsed,
  - last 5 tool calls with durations,
  - whether a hook is currently awaiting human input,
  - running token / cost totals extracted from the in-progress OTel JSONL.
- Optional `--web-status :PORT` (v1.1) exposes the same info over an HTTP/SSE endpoint so the human can glance at it from a phone.

### 17.9 Resume semantics

- `aidor run --resume` reads `.aidor/state.json`, determines the last completed phase, and invokes `copilot --continue` (Copilot's own most-recent-session resume) — or, if the process had exited cleanly mid-round, re-invokes `copilot -p ...` with a prompt that points at the latest review/fix files. Sessions older than Copilot's retention are detected via `copilot /resume` listing; if missing, we fall back to a fresh session with full context re-injected from `.aidor/` (the agent files + AGENTS.md + latest review already carry most of it).

---

## 18. Decisions log

- **2026-04-21 (a)**
  - Language: **Python 3.11+**.
  - Max rounds: **10** (hard cap). Typical expected: 5–6 + readiness gate.
  - Convergence requires reviewer to explicitly confirm `PRODUCTION_READY=true` once no critical/major remain (dedicated readiness-gate round).
  - Model strings: pass through verbatim to Copilot CLI; documented in `GETTING_STARTED.md`.
  - Telegram: deferred to v1.1; v1 escalates by loud console banner + non-zero exit.
  - Roles: coder and reviewer each get a distinct persona; neither is told the counterpart is an LLM.
  - Code baseline (supply-chain, ≥90% coverage, regression test per bugfix, linters, docs, AGENTS.md) mandatory.

- **2026-04-21 (b) — round timeout**
  - `--round-timeout` default raised from 30 min → **3 h (10 800 s)** to accommodate long early rounds on buggy repos.

- **2026-04-21 (d) — interactivity preserved; human wait bounded only by the hook timeout**
  - Do NOT pass `--no-ask-user`. The `ask_user` tool stays enabled so the coder can ask clarifying questions (e.g., lint-exception approval).
  - All questions are intercepted by our hook resolver (`preToolUse` on `ask_user` + `notification` on `elicitation_dialog`) which (1) consults policy, (2) consults state, (3) escalates to human — the human may be asleep or away for hours, and the watchdog + round-timeout are paused during that wait. The wait ends when the human answers, when the human cancels the question (Ctrl-C → `.aidor/pending/<uuid>.cancel`, the agent then proceeds with a safe default; the run is NOT aborted), when the run is globally aborted (`aidor abort` writes `.aidor/ABORT` → watchdog kills the subprocess), or when the Copilot hook-process timeout fires (currently configured to **24 h** in `.github/hooks/aidor.json`).
  - There is **no `--human-response-timeout`** in aidor itself and no auto-default answer beyond the hook-process cap. In v1.1 Telegram notifications get a configurable re-ping cadence (default every 30 min) so a silenced phone doesn't lose the prompt.
  - Every Q&A is recorded in `.aidor/logs/qa.jsonl` with wait duration. See §9.4.
  - The Copilot subprocess never blocks on a real TTY — our hook process is the one that waits, so the watchdog can distinguish "agent hung" from "agent waiting for human".

- **2026-04-21 (e) — long-session durability**
  - Runs are expected to last hours; a full §17 added.
  - System-level wake-lock on Windows via `SetThreadExecutionState`, `systemd-inhibit` on Linux (best-effort), `--no-keep-awake` to opt out.
  - Up to 3 `--continue` retries per round with exponential back-off for network / session death.
  - Per-round OTel JSONL rotation; `.aidor/` artifact pruning at `--max-artifact-mb` (default 256 MB); reviews/fixes never pruned.
  - `aidor status` can be run from a second terminal for live inspection; `--web-status` for remote glance in v1.1.
  - No hard kill on long-running sub-commands (e.g., a 90-min test run); warn only, `--kill-long-tools` to opt into killing.

- **2026-04-21 (c) — architecture pivot after Copilot CLI research**
  - **Reuse Copilot CLI features** instead of rebuilding them:
    - Non-interactive driver = `copilot -p --autopilot --output-format=json --no-ask-user`.
    - Roles = Copilot **custom agents** (`.github/agents/aidor-*.md`) invoked via `--agent=`.
    - Code baseline = **AGENTS.md** managed block.
    - Guard = `--allow-tool` / `--deny-tool` flag matrix + a single `preToolUse` / `permissionRequest` **hook script** for path containment.
    - Watchdog simplified by `--no-ask-user` (agent cannot hang on a question) + `--continue` for post-SIGTERM resume.
    - Per-round transcripts = `--share`.
    - Summary metrics = parse Copilot's OpenTelemetry JSONL file exporter — no custom scraping.
  - **Rejected for v1** (may revisit):
    - Aider as alternative coder backend (bypasses Copilot quota/auth model).
    - LangGraph / AutoGen / CrewAI multi-agent frameworks (overkill, API-centric, not CLI-centric).
    - ACP long-lived sessions (nice-to-have; Python SDK not mature; `-p` is enough for v1).
  - Module layout revised accordingly (§12): `phase.py`, `bootstrap.py`, `guard_profile.py`, `telemetry.py` replace the old `agent.py` / `copilot_backend.py` / `guard.py` interception stack.
