"""Behavioural tests for the default shell allowlist
(``src/aidor/policies/shell_allowlist.yml``) routed through
``aidor.hook_resolver._check_shell_allowlist``.

Organised by executable family:

* ``git`` — destructive / history-rewriting commands must be denied
  by default; the safe forward-only subset must still be allowed.
  Also covers the per-subcommand restrictions on ``git stash`` and
  ``git worktree`` (the bare top-level verbs would otherwise let an
  autonomous coder destroy saved work via ``git stash drop`` /
  ``clear`` or ``git worktree remove --force``).
* ``python`` / ``python3`` / ``py`` — ``python -m <module>`` is
  restricted to a curated allow set, including ``pyright`` which is
  a mandatory quality gate.

Regressions covered: review-0001 (destructive git, rg in tool
allowlist), review-0002 (stash/worktree subcommand restrictions),
review-0005 (pyright launcher).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _shell_decision(repo: Path, command: str):
    from aidor.hook_resolver import _check_shell_allowlist

    return _check_shell_allowlist({"cwd": str(repo)}, {"command": command})


# ---- git: destructive / history-rewriting commands denied ---------------


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard HEAD~1",
        "git reset HEAD~5",
        "git clean -fdx",
        "git clean -f",
        "git checkout main",
        "git checkout -- file.py",
        "git rebase -i HEAD~3",
        "git rebase main",
        "git cherry-pick abc1234",
    ],
)
def test_destructive_git_commands_denied_by_default(tmp_path: Path, command: str):
    """History-rewrite / data-loss git operations must NOT be
    allowlisted by default — an autonomous coder can otherwise wipe
    uncommitted work in the target repo. (regression: review-0001)"""
    decision = _shell_decision(tmp_path, command)
    assert decision is not None, f"expected deny for {command!r}"
    assert decision["permissionDecision"] == "deny"
    assert "allowlist" in decision["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "git add .",
        "git commit -m msg",
        "git restore file.py",
        "git switch main",
        "git status",
        "git log --oneline",
    ],
)
def test_safe_git_commands_still_allowed(tmp_path: Path, command: str):
    """Sanity: tightening the allowlist must not break the safe
    forward-only commands the agents rely on. (regression: review-0001)"""
    assert _shell_decision(tmp_path, command) is None, command


# ---- git stash / git worktree: per-subcommand restrictions --------------


@pytest.mark.parametrize(
    "command",
    [
        "git stash drop",
        "git stash drop stash@{0}",
        "git stash clear",
        "git worktree remove ../wt",
        "git worktree remove --force ../wt",
        "git worktree remove -f ../wt",
    ],
)
def test_destructive_stash_and_worktree_denied(tmp_path: Path, command: str):
    """The prior allowlist rule allowed every stash/worktree
    subcommand including the destructive ones. These must be denied
    by default. (regression: review-0002)"""
    decision = _shell_decision(tmp_path, command)
    assert decision is not None, f"expected deny for {command!r}"
    assert decision["permissionDecision"] == "deny"
    assert "allowlist" in decision["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "git stash",
        "git stash list",
        "git stash show",
        "git stash push -m wip",
        "git stash pop",
        "git stash apply",
        "git stash branch tmp",
        "git worktree add ../wt feature",
        "git worktree list",
        "git worktree lock ../wt",
        "git worktree unlock ../wt",
        "git worktree move ../wt ../wt2",
        "git worktree prune",
    ],
)
def test_safe_stash_and_worktree_still_allowed(tmp_path: Path, command: str):
    """Non-destructive stash/worktree subcommands the agents
    legitimately need must still be allowed. (regression: review-0002)"""
    assert _shell_decision(tmp_path, command) is None, command


# ---- python launchers: pyright is part of the curated -m set -----------


@pytest.mark.parametrize(
    "command",
    [
        "python -m pyright",
        "python -m pyright src",
        "python3 -m pyright",
        "py -m pyright",
        "py -3 -m pyright",
        "py -3.12 -m pyright src",
        r".\.venv\Scripts\python.exe -m pyright",
        r".\.venv\Scripts\python.exe -m pyright src",
    ],
)
def test_python_dash_m_pyright_allowed(tmp_path: Path, command: str):
    """The prior allowlist omitted ``pyright`` from the curated
    ``python -m <module>`` set, so the documented direct command path
    was denied even though pyright is a mandatory quality gate.
    (regression: review-0005)"""
    assert _shell_decision(tmp_path, command) is None, command
