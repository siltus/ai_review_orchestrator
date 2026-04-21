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
   accompanied by a regression test. No exceptions.
3. **Linters / style.** All linters must pass. Rule exclusions require human
   consent; the pre-approved list lives at `.aidor/allowed_exceptions.yml`. Do
   not add ad-hoc suppressions.
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

## Asking questions

The coder may use the `ask_user` tool when — and only when — a decision cannot
be made from the review file, this `AGENTS.md`, or the code itself. The
orchestrator may answer from policy automatically, or escalate to a human who
could be asleep. Keep questions short, self-contained, and rare.

<!-- AIDOR:MANAGED-BLOCK-END -->
