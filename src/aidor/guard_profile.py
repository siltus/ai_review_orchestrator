"""Build the Copilot CLI permission flags for each phase spawn.

Historical context (read before touching this file):

Earlier revisions of aidor tried to encode the full security policy as a
matrix of ``--allow-tool`` / ``--deny-tool`` flags. That approach is
fundamentally broken against ``@github/copilot >=1.0``:

  * ``shell(cmd)`` multi-token patterns are ONLY honoured for ``git`` and
    ``gh``. ``shell(docker ps)``, ``shell(npm install -g)``,
    ``shell(pip install --user)`` are silently non-functional.
    (github/copilot-cli#2610)
  * ``shell(cmd:*)`` prefix match works, but matches only the literal first
    token. ``shell(python:*)`` does NOT match
    ``.\\.venv\\Scripts\\python.exe``.
  * When no rule matches, the CLI falls back to ``permissionRequest``,
    which in non-interactive mode (``--autopilot`` / ``-p``) becomes an
    auto-deny. That produced 15+ bogus denials per reviewer phase in
    transcripts we inspected.

Authoritative source: the ``PreToolUseHooksProcessor`` class in the
``@github/copilot`` npm bundle (``app.js``, v1.0.34), and
``copilot help permissions`` (reproduced verbatim in
github/copilot-cli#1482).

What we do instead:

  * Spawn with ``--allow-all-tools --allow-all-paths``, disabling the
    permission matrix entirely.
  * Enforce the ENTIRE security policy from the ``preToolUse`` hook
    (``hook_resolver.py``). The hook runs unconditionally, BEFORE the
    approval layer, and its ``{"permissionDecision": "deny"}`` forces the
    tool result to ``"denied"`` regardless of the allow flags.
    (Confirmed by reading the bundle.)

Lockfile-gated local installs (``pip install -e``, ``npm ci``,
``cargo build``, ...) were previously toggled by the flag matrix. They
are now toggled inside the hook based on the
``AIDOR_ALLOW_LOCAL_INSTALL`` environment variable (set by
``phase.py``), which makes the decision visible in tests without
needing a live Copilot subprocess.
"""

from __future__ import annotations

from pathlib import Path

_LOCAL_INSTALL_MARKERS: tuple[tuple[str, ...], ...] = (
    ("poetry.lock", "uv.lock", "Pipfile.lock"),
    ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"),
    ("Cargo.lock",),
    ("go.sum",),
    ("pixi.lock",),
)

_PYTHON_LOCKFILE_MARKERS: tuple[str, ...] = ("poetry.lock", "uv.lock", "Pipfile.lock")


def detect_local_install_available(repo: Path) -> bool:
    """True iff the repo ships at least one lockfile for a supported
    ecosystem."""
    for markers in _LOCAL_INSTALL_MARKERS:
        if any((repo / marker).exists() for marker in markers):
            return True
    return False


def detect_python_lockfile(repo: Path) -> bool:
    """True iff the repo ships a Python lockfile. Used by the hook to
    distinguish ``pip install -e`` (permitted when a lockfile is present)
    from unscoped ``pip install <pkg>`` (always denied)."""
    return any((repo / marker).exists() for marker in _PYTHON_LOCKFILE_MARKERS)


def build_flags(
    repo: Path,  # noqa: ARG001
    *,
    allow_local_install: bool,  # noqa: ARG001
) -> list[str]:
    """Return the permission-related flags to append to the ``copilot -p``
    argv.

    Always returns ``["--allow-all-tools", "--allow-all-paths"]``. The
    ``preToolUse`` hook is the sole enforcer; see the module docstring
    for why the flag matrix is unfit for purpose.

    The ``repo`` and ``allow_local_install`` arguments are kept for API
    parity with the previous signature; the hook reads the local-install
    toggle from ``$AIDOR_ALLOW_LOCAL_INSTALL``.
    """
    return ["--allow-all-tools", "--allow-all-paths"]
