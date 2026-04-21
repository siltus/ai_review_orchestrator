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
    # Repo-scoped quality gates the coder is contractually required to
    # run (see AGENTS.md): linting, formatting, supply-chain audit, and
    # the test suite. These are the exact `python -m ...` invocations
    # wired into `.pre-commit-config.yaml` and `.github/workflows/ci.yml`,
    # so the launched coder can actually verify its own fixes. The scope
    # is intentionally narrow — only the validation commands themselves,
    # not arbitrary shell access.
    "shell(python -m ruff)",
    "shell(python -m pip_audit)",
    "shell(python -m pytest)",
    "shell(python -m pre_commit)",
    # Bare interpreter / launcher forms (transcript evidence: coders
    # invoke `python`, `python3`, `py`, or the `.venv\Scripts\python.exe`
    # absolute path under PowerShell). `pip install` is still blocked by
    # the conditional deny below; plain `python`/`py` access is fine.
    "shell(python)",
    "shell(python3)",
    "shell(python3.11)",
    "shell(python.exe)",
    "shell(py)",
    "shell(py.exe)",
    # Test / lint / audit runners invoked directly (not via `python -m`).
    "shell(pytest)",
    "shell(pytest.exe)",
    "shell(py.test)",
    "shell(ruff)",
    "shell(ruff.exe)",
    "shell(pip-audit)",
    "shell(pip-audit.exe)",
    "shell(pre-commit)",
    "shell(pre-commit.exe)",
    "shell(coverage)",
    "shell(coverage.exe)",
    # Safe inspection / locator utilities used by coders to discover the
    # toolchain (`where python`, `which pytest`, etc.).
    "shell(where)",
    "shell(where.exe)",
    "shell(which)",
    # Common read-only PowerShell cmdlets the coder legitimately needs
    # to inspect the workspace, check file existence, and emit small
    # amounts of text. None of these can mutate state outside what
    # `read` / `write` already permit. Aliased to bash(...)/powershell(...)
    # below — the bash variants are harmless noise on non-PowerShell
    # platforms.
    "shell(Get-ChildItem)",
    "shell(Get-Item)",
    "shell(Get-ItemProperty)",
    "shell(Get-Content)",
    "shell(Get-Command)",
    "shell(Get-Location)",
    "shell(Get-Date)",
    "shell(Get-Process)",
    "shell(Test-Path)",
    "shell(Resolve-Path)",
    "shell(Select-String)",
    "shell(Select-Object)",
    "shell(Sort-Object)",
    "shell(Where-Object)",
    "shell(ForEach-Object)",
    "shell(Measure-Object)",
    "shell(Format-List)",
    "shell(Format-Table)",
    "shell(Out-String)",
    "shell(Write-Output)",
    "shell(Write-Host)",
    "shell(New-Item)",
    "shell(Join-Path)",
    "shell(Split-Path)",
    # Bare `git` entry (broad read+local-mutation access). Dangerous
    # subcommands — push, remote, global/system config — are listed in
    # `_BASE_DENY`, and deny rules take precedence, so this does NOT
    # reopen them. It just lets `git --version`, `git help`, `git reflog`,
    # etc. work without each being enumerated.
    "shell(git)",
    # Build / test commands discovered from manifests are appended below.
)


# Deny rules always take precedence, even if an allow rule matches.
# NOTE: Python `pip install` denies are intentionally NOT in this base
# tuple. They are emitted conditionally by `build_flags` so that the
# lockfile-gated `pip install -e` allow entry can actually take effect
# when --allow-local-install is on for a Python repo. See
# `_PYTHON_GLOBAL_INSTALL_DENY_BROAD` / `_NARROW` below.
_BASE_DENY = (
    "shell(git push)",
    "shell(git remote)",
    "shell(git config --global)",
    "shell(git config --system)",
    "shell(sudo)",
    "shell(doas)",
    "shell(rm -rf /)",
    "shell(rmdir /s)",
    # Global installs (non-Python — Python is handled separately so the
    # editable-install allowlist isn't shadowed).
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


# Default Python pip install deny — a blanket prefix that catches any
# `pip install <pkg>` invocation. Used when `--allow-local-install` is OFF
# or when the repo lacks a Python lockfile marker.
_PYTHON_GLOBAL_INSTALL_DENY_BROAD = (
    "shell(pip install)",
    "shell(pip3 install)",
)


# Narrow Python pip install deny — used when `--allow-local-install` is
# ON for a Python repo with a lockfile. We must NOT emit the broad prefix
# above because deny rules take precedence over allow rules: a broad
# `shell(pip install)` deny would shadow the lockfile-gated
# `shell(pip install -e)` allow entry, contradicting the documented
# behaviour of `--allow-local-install`. Instead we deny only the
# explicitly out-of-repo install vectors (`--user`, `--target`,
# `--prefix`, `--root`), which write outside the project tree.
_PYTHON_GLOBAL_INSTALL_DENY_NARROW = (
    "shell(pip install --user)",
    "shell(pip install --target)",
    "shell(pip install --prefix)",
    "shell(pip install --root)",
    "shell(pip3 install --user)",
    "shell(pip3 install --target)",
    "shell(pip3 install --prefix)",
    "shell(pip3 install --root)",
)


# Ecosystem mapping: each entry pairs the lockfile markers that prove a
# real, reproducible dependency graph for that ecosystem with the install
# commands that should be unlocked when --allow-local-install is on AND a
# matching marker is present.
#
# review-0015: split by ecosystem so that, e.g., a `package-lock.json` does
# NOT unlock unrelated commands like `uv pip install` or `poetry install`.
# `pyproject.toml` is intentionally NOT a marker (not a lockfile), and
# `requirements.txt` is intentionally NOT a marker either — it is just a
# free-form input file, not a pinned, hashed lockfile, and the docs frame
# this feature around "real lockfiles".
#
# `pip install --user` is intentionally NOT in the Python allow set —
# `--user` writes to `%APPDATA%\Python` (Windows) or `~/.local/` (Unix),
# which is OUTSIDE the repo and therefore violates the "no global
# installs" guard rule. Use `pip install -e .` inside a project venv.
_LOCAL_INSTALL_ECOSYSTEMS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    # Python
    (
        ("poetry.lock", "uv.lock", "Pipfile.lock"),
        (
            "shell(poetry install)",
            "shell(pip install -e)",
            "shell(uv sync)",
            "shell(uv pip install)",
        ),
    ),
    # JavaScript / TypeScript
    (
        ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"),
        (
            "shell(npm ci)",
            "shell(npm install)",
            "shell(pnpm install)",
            "shell(pnpm i)",
            "shell(yarn install)",
            "shell(yarn)",
        ),
    ),
    # Rust
    (("Cargo.lock",), ("shell(cargo build)", "shell(cargo fetch)")),
    # Go
    (("go.sum",), ("shell(go mod download)",)),
    # Pixi
    (("pixi.lock",), ("shell(pixi install)",)),
)


# Lockfile markers that specifically indicate a Python repo. When one of
# these is present AND --allow-local-install is on, we switch from the
# broad `pip install` deny to a narrower set so the editable-install allow
# can take effect.
_PYTHON_LOCAL_INSTALL_MARKERS: tuple[str, ...] = (
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
)


def _ecosystems_present(repo: Path) -> list[tuple[str, ...]]:
    """Return the list of ecosystem allow-tuples whose markers are present
    in `repo`. Each ecosystem is independent: the presence of a JS lockfile
    does NOT enable Python install commands and vice versa."""
    matched: list[tuple[str, ...]] = []
    for markers, allow in _LOCAL_INSTALL_ECOSYSTEMS:
        if any((repo / marker).exists() for marker in markers):
            matched.append(allow)
    return matched


def detect_local_install_available(repo: Path) -> bool:
    """Return True if the repo contains a lockfile / manifest indicating a
    local install is meaningful (and therefore permissible if
    --allow-local-install is on)."""
    return bool(_ecosystems_present(repo))


def _detect_python_local_install_available(repo: Path) -> bool:
    """Return True if the repo contains a Python lockfile marker."""
    return any((repo / marker).exists() for marker in _PYTHON_LOCAL_INSTALL_MARKERS)


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

    if allow_local_install:
        # review-0015: only unlock the install commands for ecosystems
        # whose lockfiles are actually present. A bare `package-lock.json`
        # must NOT enable `uv pip install` / `poetry install`, etc.
        seen: set[str] = set()
        for ecosystem_allow in _ecosystems_present(repo):
            for rule in _expand_shell_aliases(ecosystem_allow):
                if rule in seen:
                    continue
                seen.add(rule)
                flags.append(f"--allow-tool={rule}")

    # Python pip-install denies are emitted conditionally so the
    # `pip install -e` allow entry isn't shadowed by a broad `pip install`
    # deny when --allow-local-install is on for a Python repo.
    if allow_local_install and _detect_python_local_install_available(repo):
        python_pip_deny = _PYTHON_GLOBAL_INSTALL_DENY_NARROW
    else:
        python_pip_deny = _PYTHON_GLOBAL_INSTALL_DENY_BROAD

    for rule in _expand_shell_aliases(_BASE_DENY + python_pip_deny):
        flags.append(f"--deny-tool={rule}")

    return flags
