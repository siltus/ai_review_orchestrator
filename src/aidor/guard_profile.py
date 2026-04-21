"""Build the `--allow-tool` / `--deny-tool` flag matrix for each Copilot invocation.

The flag set implements most of the Guard layer (§9 of plan.md). The path-
containment check that cannot be expressed as a flag pattern lives in the
hook resolver (`hook_resolver.py`).
"""
from __future__ import annotations

from pathlib import Path


# Tools allowed unconditionally. Reading and writing are bounded by Copilot's
# trusted-directory mechanism (we launch from the repo root and never pass
# `--add-dir` for paths outside it).
_BASE_ALLOW = (
    "read",
    "write",
    # Local git operations are fine.
    "shell(git status)",
    "shell(git diff)",
    "shell(git log)",
    "shell(git show)",
    "shell(git add)",
    "shell(git commit)",
    "shell(git restore)",
    "shell(git stash)",
    "shell(git branch)",
    "shell(git switch)",
    "shell(git checkout)",
    "shell(git rev-parse)",
    "shell(git ls-files)",
    # Build / test commands discovered from manifests are appended below.
)


# Deny rules always take precedence, even if an allow rule matches.
_BASE_DENY = (
    "shell(git push)",
    "shell(git remote)",
    "shell(git config --global)",
    "shell(git config --system)",
    "shell(sudo)",
    "shell(doas)",
    "shell(rm -rf /)",
    "shell(rmdir /s)",
    # Global installs.
    "shell(pip install)",
    "shell(pip3 install)",
    "shell(pipx install)",
    "shell(npm install -g)",
    "shell(npm i -g)",
    "shell(pnpm add -g)",
    "shell(pnpm install -g)",
    "shell(yarn global)",
    "shell(cargo install)",
    "shell(go install)",
    "shell(choco)",
    "shell(winget)",
    "shell(apt)",
    "shell(apt-get)",
    "shell(brew)",
    "shell(scoop)",
    # Arbitrary network fetches — if a project needs this, it goes through
    # its own build/test command, which has its own allow entry.
    "shell(curl)",
    "shell(wget)",
    # Self-update of the agent itself.
    "shell(copilot update)",
    "shell(copilot login)",
    "shell(copilot logout)",
)


# When --allow-local-install is on we add these back to the allow list.
# Note: `pip install --user` is intentionally absent — `--user` writes to
# `%APPDATA%\Python` (Windows) or `~/.local/` (Unix), which is OUTSIDE the
# repo and therefore violates the "no global installs" guard rule. Use
# `pip install -e .` inside a project venv instead.
_LOCAL_INSTALL_ALLOW = (
    "shell(poetry install)",
    "shell(pip install -e)",
    "shell(npm ci)",
    "shell(npm install)",
    "shell(pnpm install)",
    "shell(pnpm i)",
    "shell(yarn install)",
    "shell(yarn)",
    "shell(cargo build)",
    "shell(cargo fetch)",
    "shell(go mod download)",
    "shell(uv sync)",
    "shell(uv pip install)",
    "shell(pixi install)",
)


# Files whose presence indicates the repo has a real lockfile (and therefore
# project-local installs are meaningful + reproducible). Note: `pyproject.toml`
# is NOT a lockfile — its presence does not guarantee a pinned dependency
# graph, so it is intentionally excluded.
_LOCAL_INSTALL_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Python
    ("poetry.lock", ()),
    ("uv.lock", ()),
    ("requirements.txt", ()),
    ("Pipfile.lock", ()),
    # JS
    ("package-lock.json", ()),
    ("pnpm-lock.yaml", ()),
    ("yarn.lock", ()),
    # Rust / Go / pixi
    ("Cargo.lock", ()),
    ("go.sum", ()),
    ("pixi.lock", ()),
)


def detect_local_install_available(repo: Path) -> bool:
    """Return True if the repo contains a lockfile / manifest indicating a
    local install is meaningful (and therefore permissible if
    --allow-local-install is on)."""
    for marker, _ in _LOCAL_INSTALL_MARKERS:
        if (repo / marker).exists():
            return True
    return False


def _expand_shell_aliases(rules: tuple[str, ...]) -> list[str]:
    """For every `shell(<cmd>)` rule, also emit `bash(<cmd>)` and
    `powershell(<cmd>)` so the Guard matrix matches whichever underlying
    tool name Copilot uses for shell execution on this platform.

    Non-shell rules (e.g. `read`, `write`) pass through unchanged.
    """
    out: list[str] = []
    for rule in rules:
        out.append(rule)
        if rule.startswith("shell(") and rule.endswith(")"):
            inner = rule[len("shell(") : -1]
            out.append(f"bash({inner})")
            out.append(f"powershell({inner})")
    return out


def build_flags(
    repo: Path,
    *,
    allow_local_install: bool,
) -> list[str]:
    """Compose the list of `copilot` CLI flags for Guard policy.

    The returned list is ordered and ready to be passed to `subprocess`.
    Every `shell(...)` rule is mirrored as `bash(...)` and `powershell(...)`
    so the matrix evaluates regardless of which shell tool the model picks.
    """
    flags: list[str] = []

    for rule in _expand_shell_aliases(_BASE_ALLOW):
        flags.append(f"--allow-tool={rule}")

    if allow_local_install and detect_local_install_available(repo):
        for rule in _expand_shell_aliases(_LOCAL_INSTALL_ALLOW):
            flags.append(f"--allow-tool={rule}")

    for rule in _expand_shell_aliases(_BASE_DENY):
        flags.append(f"--deny-tool={rule}")

    return flags
