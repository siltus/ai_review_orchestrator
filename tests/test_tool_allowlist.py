"""Behavioural tests for the default Copilot tool allowlist
(``src/aidor/policies/tool_allowlist.yml``) loaded via
``aidor.hook_resolver._load_tool_allowlist``.

Regressions covered: review-0001 (rg / ripgrep must be present so
guarded runs can use the dedicated code-search surface AGENTS.md
tells agents to prefer instead of falling back to shell ``rg``).
"""

from __future__ import annotations

from pathlib import Path


def test_tool_allowlist_includes_rg(tmp_path: Path):
    """``rg`` and ``ripgrep`` must be on the deny-by-default Copilot
    tool allowlist; AGENTS.md tells agents to prefer them over shell
    searches. (regression: review-0001)"""
    from aidor.hook_resolver import _load_tool_allowlist

    names = _load_tool_allowlist(tmp_path)
    assert "rg" in names
    assert "ripgrep" in names


def test_tool_allowlist_includes_apply_patch(tmp_path: Path):
    """``apply_patch`` is the Codex multi-file patch tool. Patch bodies
    are containment-checked by ``_check_path_containment``; allowlisting
    here lets the reviewer write its review file in one call instead of
    falling back to ``create``/``edit`` (~5 denials/run pre-fix)."""
    from aidor.hook_resolver import _load_tool_allowlist

    names = _load_tool_allowlist(tmp_path)
    assert "apply_patch" in names


def test_default_mcp_policy_entries_are_allowlisted(tmp_path: Path):
    """Every default MCP/tool classification must refer to allowed tools.

    External MCP server details live in YAML policy data; Python only checks
    that the configured policy is internally consistent.
    """
    from aidor.hook_resolver import _load_tool_policy

    policy = _load_tool_policy(tmp_path)

    assert policy.mcp_tools
    assert policy.mcp_tool_patterns
    assert policy.mcp_tools <= policy.tools
    assert policy.write_tools <= policy.tools
    assert policy.path_scoped_tools <= policy.tools
    assert policy.memory_scoped_tools <= policy.tools
