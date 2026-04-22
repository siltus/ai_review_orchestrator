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
