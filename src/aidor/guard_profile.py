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
_LOCAL_INSTALL_ALLOW = (
    "shell(poetry install)",
    "shell(pip install -e)",
    "shell(pip install --user)",
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


_LOCAL_INSTALL_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pyproject.toml", ()),
    ("poetry.lock", ()),
    ("uv.lock", ()),
    ("requirements.txt", ()),
    ("package-lock.json", ()),
    ("pnpm-lock.yaml", ()),
    ("yarn.lock", ()),
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


def build_flags(
    repo: Path,
    *,
    allow_local_install: bool,
) -> list[str]:
    """Compose the list of `copilot` CLI flags for Guard policy.

    The returned list is ordered and ready to be passed to `subprocess`.
    """
    flags: list[str] = []

    for rule in _BASE_ALLOW:
        flags.append(f"--allow-tool={rule}")

    if allow_local_install and detect_local_install_available(repo):
        for rule in _LOCAL_INSTALL_ALLOW:
            flags.append(f"--allow-tool={rule}")

    for rule in _BASE_DENY:
        flags.append(f"--deny-tool={rule}")

    return flags
