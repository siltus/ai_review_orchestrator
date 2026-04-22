"""Unit tests for bootstrap's idempotent write."""

from __future__ import annotations

import json
from pathlib import Path

from aidor.bootstrap import MANAGED_END, MANAGED_START, bootstrap


def test_bootstrap_creates_all_artifacts(run_config):
    actions = bootstrap(run_config)
    assert actions  # something was written

    repo = run_config.repo
    assert (repo / ".github" / "agents" / "aidor-coder.md").exists()
    assert (repo / ".github" / "agents" / "aidor-reviewer.md").exists()
    assert (repo / ".github" / "hooks" / "aidor.json").exists()
    assert (repo / "AGENTS.md").read_text(encoding="utf-8").count(MANAGED_START) == 1
    assert (repo / "AGENTS.md").read_text(encoding="utf-8").count(MANAGED_END) == 1
    assert run_config.aidor_dir.is_dir()
    assert run_config.allowed_exceptions_path.exists()
    assert run_config.config_snapshot_path.exists()


def test_bootstrap_is_idempotent(run_config):
    bootstrap(run_config)
    first = (run_config.repo / "AGENTS.md").read_text(encoding="utf-8")
    bootstrap(run_config)
    second = (run_config.repo / "AGENTS.md").read_text(encoding="utf-8")
    assert first == second
    # Second call may still re-write the config snapshot, but managed block
    # must be unchanged.
    assert first.count(MANAGED_START) == 1


def test_hooks_json_bakes_python_interpreter(run_config):
    bootstrap(run_config)
    hooks = json.loads(
        (run_config.repo / ".github" / "hooks" / "aidor.json").read_text(encoding="utf-8")
    )
    # Every event's command must reference aidor.hook_resolver via a concrete
    # python path, not a bare 'aidor-hook'.
    import sys

    for event_list in hooks["hooks"].values():
        for cmd in event_list:
            for key in ("bash", "powershell"):
                assert "aidor.hook_resolver" in cmd[key]
                assert "aidor-hook" not in cmd[key]
                # sys.executable (or its quoted form) should appear.
                assert (
                    sys.executable.replace("\\", "\\\\") in cmd[key] or sys.executable in cmd[key]
                )
            # PowerShell specifically needs the call operator to invoke a
            # quoted executable, otherwise PS parses it as a bare string.
            assert cmd["powershell"].startswith("& ")
            assert not cmd["bash"].startswith("& ")


def test_bootstrap_preserves_existing_agents_md_prefix(run_config):
    agents_md = run_config.repo / "AGENTS.md"
    agents_md.write_text("# project\n\nMy custom section.\n", encoding="utf-8")
    bootstrap(run_config)
    content = agents_md.read_text(encoding="utf-8")
    assert "My custom section." in content
    assert MANAGED_START in content
    assert MANAGED_END in content


def test_bootstrap_gitignores_both_aidor_and_hooks_on_fresh_repo(run_config):
    """Regression: on a fresh target repo, bootstrap must ignore BOTH
    `.aidor/` (run artefacts) and `.github/hooks/aidor.json` (which bakes a
    machine-specific Python interpreter path). Missing the second entry
    used to leak local paths to version control."""
    # Pristine target: no pre-existing .gitignore.
    gi = run_config.repo / ".gitignore"
    if gi.exists():
        gi.unlink()

    bootstrap(run_config)

    assert gi.exists()
    lines = {ln.strip() for ln in gi.read_text(encoding="utf-8").splitlines()}
    assert ".aidor/" in lines
    assert ".github/hooks/aidor.json" in lines
    # Aidor-managed agent templates are refreshed every bootstrap from the
    # packaged copy and must not be tracked in target repos.
    assert ".github/agents/aidor-coder.md" in lines
    assert ".github/agents/aidor-reviewer.md" in lines


def test_bootstrap_appends_hooks_ignore_when_only_aidor_is_present(run_config):
    """If an existing .gitignore already ignores `.aidor/` but NOT the hooks
    file, bootstrap must append the missing entry without clobbering the
    existing content."""
    gi = run_config.repo / ".gitignore"
    gi.write_text("# existing\n.aidor/\nmy_local_notes.txt\n", encoding="utf-8")

    bootstrap(run_config)

    content = gi.read_text(encoding="utf-8")
    lines = {ln.strip() for ln in content.splitlines()}
    assert ".aidor/" in lines
    assert ".github/hooks/aidor.json" in lines
    assert ".github/agents/aidor-coder.md" in lines
    assert ".github/agents/aidor-reviewer.md" in lines
    assert "my_local_notes.txt" in lines  # pre-existing line preserved
    assert "# existing" in content  # comments preserved


def test_bootstrap_is_idempotent_for_gitignore(run_config):
    """Running bootstrap twice must not duplicate gitignore entries."""
    bootstrap(run_config)
    first = (run_config.repo / ".gitignore").read_text(encoding="utf-8")
    bootstrap(run_config)
    second = (run_config.repo / ".gitignore").read_text(encoding="utf-8")
    assert first == second
    assert first.count(".aidor/") == 1
    assert first.count(".github/hooks/aidor.json") == 1
    assert first.count(".github/agents/aidor-coder.md") == 1
    assert first.count(".github/agents/aidor-reviewer.md") == 1


def test_bootstrap_refreshes_stale_agent_files(run_config):
    """Regression for review-0011: an already-bootstrapped repo holding a
    stale `.github/agents/aidor-reviewer.md` (e.g. one written before the
    footer contract was tightened) must be refreshed to match the packaged
    template on the next bootstrap. Otherwise the live reviewer instructions
    drift away from what the orchestrator's parser actually enforces."""
    from importlib import resources

    bootstrap(run_config)

    reviewer_path = run_config.repo / ".github" / "agents" / "aidor-reviewer.md"
    coder_path = run_config.repo / ".github" / "agents" / "aidor-coder.md"

    stale_marker = "STALE OUTDATED REVIEWER INSTRUCTIONS — must be replaced"
    reviewer_path.write_text(
        f"---\nname: aidor-reviewer\n---\n{stale_marker}\n",
        encoding="utf-8",
    )
    coder_path.write_text("STALE CODER\n", encoding="utf-8")

    actions = bootstrap(run_config)

    refreshed = reviewer_path.read_text(encoding="utf-8")
    assert stale_marker not in refreshed, "bootstrap left a stale reviewer file in place"

    pkg_ref = resources.files("aidor.agent_templates")
    expected_reviewer = (pkg_ref / "aidor-reviewer.md").read_text(encoding="utf-8")
    expected_coder = (pkg_ref / "aidor-coder.md").read_text(encoding="utf-8")
    assert refreshed == expected_reviewer
    assert coder_path.read_text(encoding="utf-8") == expected_coder
    assert any("refreshed" in a and "aidor-reviewer.md" in a for a in actions)
    assert any("refreshed" in a and "aidor-coder.md" in a for a in actions)


def test_bootstrap_seeds_documented_ruff_exceptions(run_config):
    """Regression for review-0005: the versioned bootstrap template must
    reproduce this repo's approved Ruff exceptions on a fresh clone. If
    `.aidor/` is wiped (e.g. by `aidor clean`), re-running bootstrap must
    re-seed the same allowlist that `pyproject.toml` documents — otherwise
    the hook resolver would stop auto-approving the repo's own exceptions.
    """
    import tomllib

    import yaml

    # Pristine target: allowlist does not exist yet.
    allowed_path = run_config.allowed_exceptions_path
    if allowed_path.exists():
        allowed_path.unlink()

    bootstrap(run_config)

    assert allowed_path.exists()
    seeded = yaml.safe_load(allowed_path.read_text(encoding="utf-8"))
    assert isinstance(seeded, dict)
    entries = seeded.get("exceptions") or []
    assert entries, "seeded allowlist must not be empty; policy would silently drift"

    seeded_rules = {e["rule"] for e in entries}

    # Load the authoritative ruff ignore list from THIS repo's pyproject.toml.
    repo_root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    ruff_lint = cfg["tool"]["ruff"]["lint"]
    documented: set[str] = set(ruff_lint.get("ignore", []))
    for rules in ruff_lint.get("per-file-ignores", {}).values():
        documented.update(rules)

    missing = documented - seeded_rules
    assert not missing, (
        f"bootstrap template is missing approved ruff exceptions {sorted(missing)}; "
        "update src/aidor/policies/allowed_exceptions.yml to match pyproject.toml"
    )

    # Every seeded entry must carry a human-readable reason, otherwise the
    # hook resolver would auto-approve with no audit trail.
    for e in entries:
        assert e.get("reason"), f"entry {e!r} is missing a reason"
        assert e.get("linter"), f"entry {e!r} is missing a linter"
