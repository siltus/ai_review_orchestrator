<!-- AIDOR:MANAGED-BLOCK-START -->
<!--
    This block is maintained by aidor (https://…). Do not edit between the
    MANAGED-BLOCK-START / END markers — your changes will be overwritten on
    the next run. Add your own content ABOVE or BELOW this block.
-->

# Contract for automated agents working in this repository

This repository is being driven by the **aidor** orchestrator. Two agents —
`aidor-coder` and `aidor-reviewer` — take turns: the reviewer audits the repo,
the coder fixes the issues, the reviewer audits again, and so on, until the
reviewer declares the repo production-ready.

## Code baseline (non-negotiable)

1. **Supply-chain security.** The repository must run the language-appropriate
   auditor — `pip-audit`, `pysentry`, `npm audit`, `cargo audit`, etc. — as a
   build step or a git pre-commit hook. If none exists, add one.
2. **Test coverage.** Line coverage must be ≥ 90 %. Every bugfix must be
   accompanied by a regression test. No exceptions. New regression tests
   must be placed in the existing test file that covers the module/feature
   under fix (`tests/test_<module>.py`); files of the form
   `test_review_NNNN_*.py`, `test_round_*.py`, or `test_fixes_*.py` are a
   structural defect and the reviewer will flag them as **major**.
3. **Linters / style.** All linters must pass. Rule exclusions require human
   consent; the pre-approved list lives at `.aidor/allowed_exceptions.yml`.
   Do not add ad-hoc suppressions. Across all languages, lines containing
   inline or block suppressions (`noqa`, `type: ignore`, `pylint: disable`,
   `eslint-disable`, `@ts-ignore`, `@ts-expect-error`, `istanbul ignore`,
   `prettier-ignore`, `nolint`, `lint:ignore`, `#[allow]`, `#[expect]`,
   `#pragma warning disable`, `SuppressMessage`, `@SuppressWarnings`,
   `@SuppressFBWarnings`, `CHECKSTYLE:OFF`, `rubocop:disable`,
   `shellcheck disable`, or equivalents) must be <= 0.1% of non-blank
   source lines. Above that threshold is a **major** defect unless the
   human explicitly approved a narrow, documented exception.
4. **Documentation.** `README.md`, `ARCHITECTURE.md`, and `GETTING_STARTED.md`
   must exist and be current with the code. Stale docs are bugs.
5. **This file.** Keep `AGENTS.md` (this file) accurate. It is the persistent
   contract agents read every session.

## Where the gates run (mandatory vs. optional)

The **mandatory** enforcement point is the **local pre-commit gate**
(`.pre-commit-config.yaml`). Every quality gate listed above — lint,
format, supply-chain audit, test suite, coverage floor — must run there
and block the commit on failure. That is what the coder is required to
keep green.

A **GitHub Actions / CI workflow** (`.github/workflows/*.yml`) is
**optional and may be disabled by the human** for any reason (cost,
flakiness, repo policy). Do **not** treat a missing, disabled, or
manually-disabled workflow as a defect, and do **not** re-enable a
workflow that the human disabled. If the pre-commit gate is green, the
baseline is met regardless of CI status.

If the CLI sandbox blocks you from running the local gate (e.g. `ruff`,
`pytest`, `pip-audit` are denied above the aidor guard hook), record
that in your fixes summary and skip — do NOT block the round on it,
and do NOT hand-format files to fake a green gate. The orchestrator
will run the real gate before any commit lands.

## Guard rules (enforced by aidor)

- Never push to a git remote.
- Never change global git config or the user's home directory.
- Never install anything globally (`npm -g`, `pip install`, `cargo install`,
  `choco`, `winget`, `apt`, `brew`, `scoop`, …).
- Never read or write files outside the repository root.
- Project-local installs (`poetry install`, `npm ci`, …) are allowed only when
  a lockfile already exists.
- The coder must not modify `.aidor/allowed_exceptions.yml`,
  `.aidor/tool_allowlist.yml`, `.aidor/shell_allowlist.yml`,
  `.github/hooks/aidor.json`, or `.github/agents/aidor-*.md`. If an
  exception or policy change seems warranted, the coder documents the request
  in the fixes summary; the reviewer may make the change only after careful
  consideration and should use `ask_user` when in doubt.
- Ignore git submodules as external pinned dependencies. If `.gitmodules`
  exists or `git submodule status` lists entries, do not review or fix code
  inside those submodule paths. Only parent-repo integration, pinning, build,
  or exclusion wiring is in scope.
- At the start of each turn, scan the available tool list for MCP tools
  (namespaced forms such as `github-mcp-server/*` or
  `github-mcp-server-*`). Use configured MCPs when relevant: GitHub MCP for
  GitHub issues/PRs/repos/code search, Tavily or other web MCPs for external
  docs, and filesystem/code-intelligence MCPs for local inspection. Do not
  install or configure new MCP servers, and do not assume optional external
  MCPs are present in every environment.
- If an MCP tool is denied by the aidor guard, do not work around the denial
  through another tool. Record the denied tool and rationale so the reviewer or
  human can decide whether to extend `.aidor/tool_allowlist.yml`.

## Asking questions

The coder may use the `ask_user` tool when — and only when — a decision cannot
be made from the review file, this `AGENTS.md`, or the code itself. The
orchestrator may answer from policy automatically, or escalate to a human who
could be asleep. Keep questions short, self-contained, and rare.

<!-- AIDOR:MANAGED-BLOCK-END -->
