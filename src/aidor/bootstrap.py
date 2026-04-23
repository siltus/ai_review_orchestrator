"""Idempotent bootstrap of aidor artifacts inside a target repo.

Writes / refreshes:
  - AGENTS.md                                  (managed block only)
  - .github/agents/aidor-coder.md              (refreshed if stale; the
                                                packaged template is the
                                                source of truth)
  - .github/agents/aidor-reviewer.md           (refreshed if stale; the
                                                packaged template is the
                                                source of truth — these are
                                                operational instructions the
                                                orchestrator hands to the
                                                model, not user content, so
                                                bootstrap keeps them in sync
                                                with the installed wheel)
  - .github/hooks/aidor.json                   (always overwritten; contains
                                                the absolute path of the Python
                                                interpreter that installed
                                                aidor, so the hook works
                                                regardless of PATH)
  - .aidor/{reviews,fixes,transcripts,logs}/   (created)
  - .aidor/allowed_exceptions.yml              (seeded if absent)
  - .aidor/config.snapshot.toml                (always overwritten)
  - .gitignore entries for `.aidor/` and `.github/hooks/aidor.json`
                                               (appended if missing; the
                                                hooks file contains a
                                                machine-specific absolute
                                                Python path and must not
                                                be committed)

Template sources live under `aidor/agent_templates/` and `aidor/policies/`
(shipped with the wheel).
"""

from __future__ import annotations

import json
import sys
from importlib import resources
from pathlib import Path

from aidor.config import RunConfig

MANAGED_START = "<!-- AIDOR:MANAGED-BLOCK-START -->"
MANAGED_END = "<!-- AIDOR:MANAGED-BLOCK-END -->"


def _read_template(relpath: str) -> str:
    """Read a packaged template file as text."""
    package, _, name = relpath.partition("/")
    pkg_ref = resources.files(f"aidor.{package}")
    return (pkg_ref / name).read_text(encoding="utf-8")


def _render_hooks_json() -> str:
    """Generate hooks.json with sys.executable baked in for both bash and
    powershell invocations. This removes the PATH dependency — the hook runs
    in the same Python that installed aidor, which has PyYAML and the
    aidor package on its sys.path.

    Note: PowerShell requires the call operator `&` to invoke a quoted
    executable; bash does not.
    """
    py = _shell_quote(sys.executable)
    bash_template = f"{py} -m aidor.hook_resolver {{event}}"
    ps_template = f"& {py} -m aidor.hook_resolver {{event}}"

    hooks = {
        "version": 1,
        "hooks": {
            event: [
                {
                    "type": "command",
                    "bash": bash_template.format(event=event),
                    "powershell": ps_template.format(event=event),
                    "timeoutSec": timeout,
                }
            ]
            for event, timeout in (
                ("preToolUse", 86_400),
                ("permissionRequest", 86_400),
                ("notification", 60),
                ("agentStop", 60),
            )
        },
    }
    return json.dumps(hooks, indent=2) + "\n"


def _shell_quote(path: str) -> str:
    """Quote a path for embedding into a shell command line.

    On Windows, paths commonly contain spaces (e.g. `C:\\Program Files\\...`);
    surrounding with double-quotes works for both bash and PowerShell.
    """
    if " " in path or "\\" in path:
        return '"' + path.replace('"', '\\"') + '"'
    return path


def bootstrap(config: RunConfig) -> list[str]:
    """Write/refresh all aidor artifacts. Returns a list of human-readable
    actions performed (for logging)."""
    actions: list[str] = []

    repo = config.repo

    # ---- Directories ------------------------------------------------------
    for d in (
        config.aidor_dir,
        config.reviews_dir,
        config.fixes_dir,
        config.transcripts_dir,
        config.logs_dir,
        repo / ".github" / "agents",
        repo / ".github" / "hooks",
    ):
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            actions.append(f"created {d.relative_to(repo).as_posix()}/")

    # ---- Custom agent files ----------------------------------------------
    # These hold the operational instructions the orchestrator passes to the
    # model. They MUST stay in sync with the packaged templates, otherwise an
    # already-bootstrapped repo can keep running stale instructions while the
    # orchestrator's parser enforces a newer contract (see review-0011). Any
    # local edits to these files are overwritten on the next bootstrap by
    # design: customise the packaged template (and bump the wheel) instead.
    agents_dir = repo / ".github" / "agents"
    for name in ("aidor-coder.md", "aidor-reviewer.md"):
        dest = agents_dir / name
        content = _read_template(f"agent_templates/{name}")
        if not dest.exists():
            dest.write_text(content, encoding="utf-8")
            actions.append(f"wrote {dest.relative_to(repo).as_posix()}")
        elif dest.read_text(encoding="utf-8") != content:
            dest.write_text(content, encoding="utf-8")
            actions.append(f"refreshed {dest.relative_to(repo).as_posix()} (stale)")

    # ---- Hooks file -------------------------------------------------------
    hooks_path = repo / ".github" / "hooks" / "aidor.json"
    new_hooks = _render_hooks_json()
    if not hooks_path.exists() or hooks_path.read_text(encoding="utf-8") != new_hooks:
        hooks_path.write_text(new_hooks, encoding="utf-8")
        actions.append(f"wrote {hooks_path.relative_to(repo).as_posix()}")

    # ---- AGENTS.md managed block ------------------------------------------
    managed_block = _read_template("agent_templates/agents_md_block.md")
    agents_md = repo / "AGENTS.md"
    new_agents_md = _merge_managed_block(
        existing=agents_md.read_text(encoding="utf-8") if agents_md.exists() else "",
        block=managed_block,
    )
    if not agents_md.exists() or agents_md.read_text(encoding="utf-8") != new_agents_md:
        agents_md.write_text(new_agents_md, encoding="utf-8")
        actions.append(f"updated {agents_md.relative_to(repo).as_posix()}")

    # ---- allowed_exceptions.yml (seed if absent) --------------------------
    if not config.allowed_exceptions_path.exists():
        seed = _read_template("policies/allowed_exceptions.yml")
        config.allowed_exceptions_path.write_text(seed, encoding="utf-8")
        actions.append(f"seeded {config.allowed_exceptions_path.relative_to(repo).as_posix()}")

    # ---- config snapshot --------------------------------------------------
    config.config_snapshot_path.write_text(_render_config_snapshot(config), encoding="utf-8")
    actions.append(f"wrote {config.config_snapshot_path.relative_to(repo).as_posix()}")

    # ---- .gitignore entries -----------------------------------------------
    # Aidor-managed scaffolding that must never be committed to the target
    # repo:
    #   - `.aidor/`                         run artefacts
    #   - `.github/hooks/aidor.json`        machine-specific: bakes the
    #                                       absolute path of this Python
    #                                       interpreter
    #   - `.github/agents/aidor-coder.md`   refreshed from packaged template
    #     `.github/agents/aidor-reviewer.md`  on every bootstrap; checking
    #                                         them in causes drift between
    #                                         operator branches and pollutes
    #                                         PRs
    # Bootstrap manages all of these so a fresh target repo cannot
    # accidentally commit any of them.
    gi = repo / ".gitignore"
    needed_entries = (
        ".aidor/",
        ".github/hooks/aidor.json",
        ".github/agents/aidor-coder.md",
        ".github/agents/aidor-reviewer.md",
    )
    gi_actions = _ensure_gitignore_entries(gi, needed_entries)
    actions.extend(gi_actions)

    return actions


def teardown(config: RunConfig) -> list[str]:
    """Reverse the parts of ``bootstrap`` that constrain or instruct
    Copilot after the orchestrator exits.

    Thin wrapper around :func:`teardown_repo` for the orchestrator's
    ``finally`` block; ``aidor clean`` calls :func:`teardown_repo`
    directly with a bare ``repo`` path so it doesn't need to construct
    a full ``RunConfig`` for a destructive housekeeping command.
    """
    return teardown_repo(config.repo)


def teardown_repo(repo: Path) -> list[str]:
    """Remove every file ``bootstrap`` writes outside ``.aidor/``.

    Removes:

    * ``.github/hooks/aidor.json`` — the active enforcement hook. Left
      behind, it routes follow-up interactive ``copilot`` sessions in
      the same repo through ``aidor.hook_resolver`` and denies normal
      operator commands like ``Get-Content D:\\TEMP\\copilot-tool-output-*.txt``
      because the path is outside the repo root.
    * ``.github/agents/aidor-coder.md`` and ``aidor-reviewer.md`` —
      operational instructions Copilot picks up automatically when the
      operator opens a follow-up interactive session in the repo. They
      shape the model's behaviour even though they don't *enforce*
      anything, so an aidor-driven run leaving them behind makes
      manual sessions inherit aidor's contract whether the operator
      wants it or not.
    * ``.github/hooks/`` and ``.github/agents/`` directories if empty
      after the removals (preserves unrelated files an operator may
      have placed alongside ours).

    Never touches:

    * ``.aidor/``      — run artefacts the operator wants to keep
    * ``AGENTS.md``    — passive contract document; the human-edited
                         portions outside the managed block are theirs
    * ``.gitignore``   — entries are harmless when the files are gone

    Idempotent: missing files are silently skipped. Returns a list of
    human-readable actions performed (empty if nothing was removed).
    """
    actions: list[str] = []
    targets = (
        repo / ".github" / "hooks" / "aidor.json",
        repo / ".github" / "agents" / "aidor-coder.md",
        repo / ".github" / "agents" / "aidor-reviewer.md",
    )
    parent_dirs: set[Path] = set()
    for target in targets:
        if target.exists():
            try:
                target.unlink()
                actions.append(f"removed {target.relative_to(repo).as_posix()}")
                parent_dirs.add(target.parent)
            except OSError:  # pragma: no cover - defensive
                pass
    for parent in parent_dirs:
        try:
            # Only rmdir if empty; preserves any unrelated files an
            # operator may have placed alongside ours.
            next(parent.iterdir())
        except StopIteration:
            try:
                parent.rmdir()
                actions.append(f"removed empty {parent.relative_to(repo).as_posix()}/")
            except OSError:  # pragma: no cover - defensive
                pass
        except OSError:  # pragma: no cover - defensive
            pass
    return actions


def _ensure_gitignore_entries(gitignore: Path, entries: tuple[str, ...]) -> list[str]:
    """Make sure every entry in `entries` appears as a line in `.gitignore`.

    Creates the file if missing. Returns a list of human-readable actions
    describing what changed (empty if nothing changed).
    """
    actions: list[str] = []
    if gitignore.exists():
        existing = gitignore.read_text(encoding="utf-8")
        present = {ln.strip() for ln in existing.splitlines()}
        missing = [e for e in entries if e not in present]
        if missing:
            suffix = "\n".join(missing) + "\n"
            gitignore.write_text(
                existing.rstrip() + "\n" + suffix,
                encoding="utf-8",
            )
            for e in missing:
                actions.append(f"appended {e} to .gitignore")
    else:
        gitignore.write_text("\n".join(entries) + "\n", encoding="utf-8")
        actions.append("created .gitignore with " + ", ".join(entries))
    return actions


def _merge_managed_block(*, existing: str, block: str) -> str:
    """Insert or replace the MANAGED block inside an existing AGENTS.md.

    If the existing file has no managed markers, the block is appended to the
    end (preceded by a blank line). If it has markers, the content between
    them is replaced atomically.
    """
    if not existing.strip():
        return block if block.endswith("\n") else block + "\n"

    start_idx = existing.find(MANAGED_START)
    end_idx = existing.find(MANAGED_END)
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        end_idx_full = end_idx + len(MANAGED_END)
        prefix = existing[:start_idx].rstrip() + ("\n\n" if existing[:start_idx].strip() else "")
        suffix = existing[end_idx_full:].lstrip("\n")
        merged = prefix + block.strip() + ("\n\n" + suffix if suffix else "\n")
        return merged
    sep = "\n" if existing.endswith("\n") else "\n\n"
    return existing + sep + block.strip() + "\n"


def _render_config_snapshot(config: RunConfig) -> str:
    """Minimal TOML rendering without a dep. Only strings/ints/bools/paths."""
    d = config.to_dict()
    lines = ["# Effective configuration for this run — generated by aidor.", ""]
    for k, v in d.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k} = {v}")
        elif isinstance(v, dict):
            if not v:
                continue
            lines.append(f"[{k}]")
            for ik, iv in v.items():
                lines.append(f"{ik} = {json.dumps(iv)}")
        else:
            lines.append(f"{k} = {json.dumps(str(v))}")
    return "\n".join(lines) + "\n"
