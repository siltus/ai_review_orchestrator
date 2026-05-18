"""Idempotent bootstrap of aidor runtime artifacts inside a target repo.

Writes / refreshes while a run is active:
  - AGENTS.md                                  (temporary runtime contract;
                                                any pre-existing file is backed
                                                up and restored on teardown)
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
  - .gitignore entries for `.aidor/`, `.github/hooks/aidor.json`, and
                                                `.github/aidor-backups/`
                                                (appended if missing; the
                                                hooks file and runtime backups
                                                must not be committed)

Template sources live under `aidor/agent_templates/`, `aidor/resources/`, and
`aidor/policies/` (shipped with the wheel).
"""

from __future__ import annotations

import json
import sys
from importlib import resources
from pathlib import Path

from aidor.config import RunConfig

RUNTIME_AGENTS_TEMPLATE = "resources/aidor_runtime_agents.md"
AGENTS_BACKUP_DIR = Path(".github") / "aidor-backups"
AGENTS_BACKUP_META = "AGENTS.md.meta.json"
AGENTS_BACKUP_ORIGINAL = "AGENTS.md.original"


def _read_template(relpath: str) -> str:
    """Read a packaged template file as text."""
    package, _, name = relpath.partition("/")
    pkg_ref = resources.files(f"aidor.{package}")
    return (pkg_ref / name).read_text(encoding="utf-8")


def _backup_dir(repo: Path) -> Path:
    return repo / AGENTS_BACKUP_DIR


def _backup_meta_path(repo: Path) -> Path:
    return _backup_dir(repo) / AGENTS_BACKUP_META


def _backup_original_path(repo: Path) -> Path:
    return _backup_dir(repo) / AGENTS_BACKUP_ORIGINAL


def _read_backup_meta(repo: Path) -> dict[str, object] | None:
    meta_path = _backup_meta_path(repo)
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _write_backup_meta(repo: Path, *, existed: bool) -> None:
    meta_path = _backup_meta_path(repo)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps({"existed": existed}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _install_runtime_agents_md(repo: Path, runtime_contract: str) -> list[str]:
    """Temporarily install aidor's runtime AGENTS.md in the target repo.

    A pre-existing project AGENTS.md is backed up under `.github/aidor-backups/`
    and restored by teardown. Repeated bootstrap calls during the same run keep
    the first backup intact unless the operator has replaced AGENTS.md with
    non-runtime content after a crashed run; in that case the new human-authored
    file becomes the backup source.
    """
    actions: list[str] = []
    agents_md = repo / "AGENTS.md"
    original_path = _backup_original_path(repo)
    meta = _read_backup_meta(repo)

    current: str | None = None
    if agents_md.exists():
        current = agents_md.read_text(encoding="utf-8")
    elif meta and meta.get("existed") is True and original_path.exists():
        current = original_path.read_text(encoding="utf-8")

    if meta is not None and current == runtime_contract:
        return actions

    _backup_dir(repo).mkdir(parents=True, exist_ok=True)
    if current is None:
        if original_path.exists():
            original_path.unlink()
        _write_backup_meta(repo, existed=False)
        actions.append("recorded missing AGENTS.md for runtime restore")
    else:
        original_path.write_text(current, encoding="utf-8")
        _write_backup_meta(repo, existed=True)
        actions.append("backed up AGENTS.md to .github/aidor-backups/AGENTS.md.original")

    if current != runtime_contract:
        agents_md.write_text(runtime_contract, encoding="utf-8")
        actions.append("installed runtime AGENTS.md")
    return actions


def _restore_runtime_agents_md(repo: Path) -> list[str]:
    """Restore or remove the temporary runtime AGENTS.md if bootstrap installed it."""
    meta_path = _backup_meta_path(repo)
    if not meta_path.exists():
        return []

    meta = _read_backup_meta(repo)
    if meta is None:
        raise RuntimeError(f"invalid aidor AGENTS.md backup metadata: {meta_path}")

    actions: list[str] = []
    agents_md = repo / "AGENTS.md"
    original_path = _backup_original_path(repo)
    if meta.get("existed") is True:
        if not original_path.exists():
            raise FileNotFoundError(f"missing AGENTS.md backup: {original_path}")
        agents_md.write_text(original_path.read_text(encoding="utf-8"), encoding="utf-8")
        actions.append("restored AGENTS.md from .github/aidor-backups/AGENTS.md.original")
    else:
        if agents_md.exists():
            agents_md.unlink()
            actions.append("removed runtime AGENTS.md")

    for path in (original_path, meta_path):
        if path.exists():
            path.unlink()
    backup_dir = _backup_dir(repo)
    try:
        backup_dir.rmdir()
        actions.append("removed empty .github/aidor-backups/")
    except OSError:
        pass
    return actions


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
    actions performed (for logging).
    """
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

    # ---- Runtime AGENTS.md ------------------------------------------------
    runtime_contract = _read_template(RUNTIME_AGENTS_TEMPLATE)
    actions.extend(_install_runtime_agents_md(repo, runtime_contract))

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
    #   - `.github/aidor-backups/`          temporary AGENTS.md backup state
    # Bootstrap manages all of these so a fresh target repo cannot
    # accidentally commit any of them. It intentionally does NOT ignore
    # AGENTS.md itself, because a pre-existing tracked project AGENTS.md is
    # restored on teardown.
    gi = repo / ".gitignore"
    needed_entries = (
        ".aidor/",
        ".github/hooks/aidor.json",
        ".github/agents/aidor-coder.md",
        ".github/agents/aidor-reviewer.md",
        ".github/aidor-backups/",
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
    """Remove every runtime file ``bootstrap`` writes outside ``.aidor/``.

    Restores a pre-existing project ``AGENTS.md`` from
    ``.github/aidor-backups/`` or removes the temporary runtime file when the
    target repo did not have one. Then removes aidor's generated hook and agent
    files. Idempotent when no aidor bootstrap state exists.
    """
    actions: list[str] = []
    actions.extend(_restore_runtime_agents_md(repo))

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
