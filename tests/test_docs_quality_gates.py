"""Documentation-drift tests for the project's quality-gate surface.

The mandatory quality gates are wired in ``.pre-commit-config.yaml``
and ``.github/workflows/ci.yml``. Contributor docs must enumerate the
same gates so a human bootstrapping the repo locally runs the same
checks the bots run.

Regressions covered: review-0002 (``ARCHITECTURE.md`` and
``GETTING_STARTED.md`` must mention ``pyright`` after it was added
to the gate set).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_architecture_md_documents_pyright_gate():
    text = (REPO_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "pyright" in text.lower(), (
        "ARCHITECTURE.md must list pyright in the quality-gate set "
        "(it is enforced in .pre-commit-config.yaml and CI)."
    )


def test_getting_started_md_documents_pyright_gate():
    text = (REPO_ROOT / "GETTING_STARTED.md").read_text(encoding="utf-8")
    assert "pyright" in text.lower(), (
        "GETTING_STARTED.md must tell contributors to run pyright locally "
        "(it is enforced in .pre-commit-config.yaml and CI)."
    )
