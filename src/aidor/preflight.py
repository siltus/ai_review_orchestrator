"""Pre-run advisory checks.

Emits non-blocking warnings the operator should see before the agents
spend tokens. The orchestrator owns wall-clock cost and platform
selection — these checks only surface conditions the operator might
not have noticed (e.g. running a Windows-only WPF repo on Linux, or
launching agents at a 10k-file monorepo without realising the token
implications). All findings are returned as plain strings; the caller
decides how to render them.
"""

from __future__ import annotations

import platform
import re
from collections.abc import Iterable
from pathlib import Path

# Heuristic thresholds. Generous enough that small/medium repos never
# trigger; loud enough that genuinely large monorepos do.
_LARGE_REPO_FILE_COUNT = 2000
_LARGE_REPO_BYTES = 100 * 1024 * 1024  # 100 MiB of tracked source

# Regexes against raw csproj XML — cheap and reliable enough for an
# advisory check; we don't need a real XML parser.
_USE_WPF_RE = re.compile(r"<UseWPF>\s*true\s*</UseWPF>", re.IGNORECASE)
_USE_WINFORMS_RE = re.compile(r"<UseWindowsForms>\s*true\s*</UseWindowsForms>", re.IGNORECASE)
_TFM_WINDOWS_RE = re.compile(r"<TargetFramework[s]?>[^<]*-windows[\d.]*[^<]*</TargetFramework[s]?>", re.IGNORECASE)


def compute_warnings(repo: Path, *, host_system: str | None = None) -> list[str]:
    """Return advisory warnings for ``repo``. Empty list = nothing to say."""
    warnings: list[str] = []
    host = (host_system or platform.system() or "").lower()

    win_only = _windows_only_csprojs(repo)
    if win_only and host != "windows":
        sample = ", ".join(sorted(p.name for p in win_only)[:3])
        more = f" (+{len(win_only) - 3} more)" if len(win_only) > 3 else ""
        warnings.append(
            f"This repository targets Windows-only .NET frameworks "
            f"(WPF / WinForms / net*-windows) — found in: {sample}{more}. "
            f"Host platform is '{platform.system() or 'unknown'}'. Tests "
            f"that need a desktop session will fail here. Platform "
            f"selection is the operator's responsibility; the agents "
            f"will run regardless."
        )

    file_count, total_bytes = _repo_size(repo)
    if file_count >= _LARGE_REPO_FILE_COUNT or total_bytes >= _LARGE_REPO_BYTES:
        size_mb = total_bytes / (1024 * 1024)
        warnings.append(
            f"Large repository: {file_count} tracked source files, "
            f"~{size_mb:.0f} MiB. Each round can consume substantial "
            f"context tokens; long runs may be expensive. The operator "
            f"owns budget — see the cost disclaimer in README.md."
        )

    return warnings


def _windows_only_csprojs(repo: Path) -> list[Path]:
    """Return csproj files that opt into a Windows-only desktop stack."""
    hits: list[Path] = []
    if not repo.exists():
        return hits
    try:
        candidates = [
            p
            for p in repo.rglob("*.csproj")
            if not _is_under_excluded(p, repo)
        ]
    except OSError:
        return hits
    for csproj in candidates:
        try:
            text = csproj.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _USE_WPF_RE.search(text) or _USE_WINFORMS_RE.search(text) or _TFM_WINDOWS_RE.search(text):
            hits.append(csproj)
    return hits


def _repo_size(repo: Path) -> tuple[int, int]:
    """Approximate (file count, total bytes) of source-relevant files."""
    if not repo.exists():
        return (0, 0)
    count = 0
    size = 0
    try:
        for path in repo.rglob("*"):
            if not path.is_file():
                continue
            if _is_under_excluded(path, repo):
                continue
            count += 1
            try:
                size += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return (count, size)
    return (count, size)


_EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "bin",
        "obj",
        "target",
        "build",
        "dist",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        ".aidor",
    }
)


def _is_under_excluded(path: Path, repo: Path) -> bool:
    try:
        rel = path.relative_to(repo)
    except ValueError:
        return True
    return any(part in _EXCLUDED_DIR_NAMES for part in rel.parts)


def render_warnings(warnings: Iterable[str]) -> str:
    """Render ``warnings`` as a single Rich-markup string for the console."""
    items = list(warnings)
    if not items:
        return ""
    body = "\n".join(f"  • {w}" for w in items)
    return f"[bold yellow]preflight warnings[/bold yellow]\n{body}"
