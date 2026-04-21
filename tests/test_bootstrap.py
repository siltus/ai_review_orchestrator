"""Unit tests for bootstrap's idempotent write."""
from __future__ import annotations

import json

from aidor.bootstrap import bootstrap, MANAGED_START, MANAGED_END


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
    second_actions = bootstrap(run_config)
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
                assert sys.executable.replace("\\", "\\\\") in cmd[key] or sys.executable in cmd[key]


def test_bootstrap_preserves_existing_agents_md_prefix(run_config):
    agents_md = run_config.repo / "AGENTS.md"
    agents_md.write_text("# project\n\nMy custom section.\n", encoding="utf-8")
    bootstrap(run_config)
    content = agents_md.read_text(encoding="utf-8")
    assert "My custom section." in content
    assert MANAGED_START in content
    assert MANAGED_END in content
