"""Repo-local pre-commit interpreter shim.

All quality-gate hooks declared in ``.pre-commit-config.yaml``
(``ruff-check``, ``ruff-format``, ``pyright``, ``pip-audit``, and
``pytest``) need the project's editable install **and** its dev
dependencies. Earlier revisions invoked ``python -m <module>`` directly
from the hook ``entry``; pre-commit resolved that against whichever
``python`` happened to be on ``PATH`` at hook-launch time, which on
Windows often dropped back to the system interpreter
(``C:\\Python311\\python.exe``) and failed with
``ModuleNotFoundError: No module named 'aidor'`` /
``No module named 'pip_audit'``. The supposed always-green local gate
was an environment lottery (review-0001).

This shim re-execs into the repo-local ``.venv`` interpreter so the
hooks always see the right interpreter regardless of ambient
``PATH``. It uses only the standard library so the *bootstrap* python
(whichever one ran the shim) can be anything.

Invoke it via the hook ``entry`` like::

    entry: python scripts/precommit_shim.py -m ruff check
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _venv_python(repo_root: Path | None = None) -> Path:
    """Return the path to the project-local venv interpreter.

    Raises ``SystemExit`` with a remediation message if no venv is
    found, so the pre-commit hook fails fast and loudly instead of
    silently using the wrong interpreter.
    """
    root = repo_root or REPO_ROOT
    if os.name == "nt":
        candidates = [root / ".venv" / "Scripts" / "python.exe"]
    else:
        candidates = [
            root / ".venv" / "bin" / "python",
            root / ".venv" / "bin" / "python3",
        ]
    for c in candidates:
        if c.is_file():
            return c
    raise SystemExit(
        f"[aidor pre-commit shim] no .venv interpreter found under "
        f"{root / '.venv'}.\n"
        "Bootstrap the repo first: see GETTING_STARTED.md "
        "(`python -m venv .venv` then "
        "`.venv/Scripts/pip install -e .[dev]` on Windows or "
        "`.venv/bin/pip install -e .[dev]` on Unix)."
    )


def main(argv: list[str] | None = None) -> int:
    py = _venv_python()
    args = [str(py), *(argv if argv is not None else sys.argv[1:])]
    if os.name == "nt":
        # `os.execv` is unreliable on Windows for console programs and
        # mangles exit-code propagation; use subprocess instead.
        import subprocess

        return subprocess.call(args)
    os.execv(str(py), args)
    return 0  # pragma: no cover - execv never returns


if __name__ == "__main__":  # pragma: no cover - exercised via pre-commit
    raise SystemExit(main())
