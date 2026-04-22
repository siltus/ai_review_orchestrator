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

# Files that anchor a pip install to the project's declared dependency
# set. A true lockfile (above) is the strongest signal, but a pinned
# `requirements*.txt` or a `pyproject.toml` is still a project-scoped
# install target — far safer than an arbitrary `pip install <pkg>`. The
# coder needs this to bootstrap test-tooling (pytest, ruff, pre-commit)
# in projects that don't ship a poetry/uv/pipenv lockfile.
_PYTHON_INSTALL_ANCHOR_MARKERS: tuple[str, ...] = (
    *_PYTHON_LOCKFILE_MARKERS,
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "requirements_dev.txt",
    "requirements_test.txt",
)

# Curated set of test/dev tooling that the coder is permitted to install
# by name even when no anchor file is present. These tools are never
# load-bearing for runtime behaviour; they only support the local quality
# gate (lint, format, type-check, test, coverage, supply-chain audit).
# Keeping this list short and conservative bounds blast radius if a
# malicious dependency name is somehow proposed by the agent.
#
# Organised per ecosystem for readability. The current consumer
# (``_pip_install_allowed``) only matches against the *Python* slice
# at runtime — pip will never see ``vitest`` or ``junit``. The other
# ecosystem entries are pre-loaded here so that future ``npm install``,
# ``dotnet tool install``, and ``cargo install`` gates can consume the
# same vocabulary without a second policy file.
_PYTHON_DEV_TOOLS: frozenset[str] = frozenset(
    {
        # test runners + plugins
        "pytest",
        "pytest-cov",
        "pytest-asyncio",
        "pytest-timeout",
        "pytest-mock",
        "pytest-xdist",
        "pytest-randomly",
        "pytest-benchmark",
        "hypothesis",
        "coverage",
        # lint / format / type
        "ruff",
        "black",
        "isort",
        "flake8",
        "pylint",
        "mypy",
        "pyright",
        "pyre-check",
        # security / supply-chain
        "bandit",
        "safety",
        "pip-audit",
        "pip_audit",
        # build / packaging / release
        "build",
        "setuptools",
        "wheel",
        "twine",
        "hatch",
        "hatchling",
        "flit",
        "flit_core",
        "poetry-core",
        # task runners / pre-commit
        "pre-commit",
        "pre_commit",
        "tox",
        "nox",
        "invoke",
        # misc
        "pip",
        "uv",
        "cookiecutter",
        "commitizen",
        "bumpversion",
        "bump2version",
    }
)

_NODE_DEV_TOOLS: frozenset[str] = frozenset(
    {
        # test runners
        "jest",
        "vitest",
        "mocha",
        "chai",
        "ava",
        "tap",
        "tape",
        "cypress",
        "playwright",
        "@playwright/test",
        "@vitest/coverage-v8",
        "nyc",
        "c8",
        # lint / format / type
        "eslint",
        "prettier",
        "typescript",
        "ts-node",
        "tsx",
        "@typescript-eslint/parser",
        "@typescript-eslint/eslint-plugin",
        "stylelint",
        # build
        "vite",
        "webpack",
        "rollup",
        "esbuild",
        "tsc",
        "tsup",
        "parcel",
        # task runners / hooks
        "husky",
        "lint-staged",
        "concurrently",
        "npm-run-all",
        # security
        "audit-ci",
        "better-npm-audit",
    }
)

_DOTNET_DEV_TOOLS: frozenset[str] = frozenset(
    {
        # test runners
        "xunit",
        "xunit.runner.console",
        "xunit.runner.visualstudio",
        "nunit",
        "nunit3.console",
        "mstest",
        "microsoft.net.test.sdk",
        "fluentassertions",
        "moq",
        "nsubstitute",
        # coverage
        "coverlet.collector",
        "coverlet.msbuild",
        "reportgenerator",
        # lint / format / analysers
        "dotnet-format",
        "csharpier",
        "stylecop.analyzers",
        "sonaranalyzer.csharp",
        "roslynator.analyzers",
        # build / dx
        "dotnet-ef",
        "dotnet-outdated-tool",
        "dotnet-reportgenerator-globaltool",
    }
)

_JVM_DEV_TOOLS: frozenset[str] = frozenset(
    {
        # test runners + assertion libs (Maven/Gradle artefact names)
        "junit",
        "junit-jupiter",
        "junit-jupiter-api",
        "junit-jupiter-engine",
        "junit-vintage-engine",
        "testng",
        "spock-core",
        "mockito-core",
        "mockito-junit-jupiter",
        "assertj-core",
        "hamcrest",
        "truth",
        # coverage
        "jacoco",
        "cobertura",
        # lint / format / static analysis
        "checkstyle",
        "spotbugs",
        "pmd",
        "errorprone",
        "spotless",
        "google-java-format",
        "ktlint",
        "detekt",
        # build helpers
        "gradle-wrapper",
        "maven-wrapper",
    }
)

_ANDROID_DEV_TOOLS: frozenset[str] = frozenset(
    {
        # AndroidX test
        "androidx.test:runner",
        "androidx.test:rules",
        "androidx.test.ext:junit",
        "androidx.test.espresso:espresso-core",
        "androidx.test.uiautomator:uiautomator",
        "androidx.benchmark:benchmark-junit4",
        "robolectric",
        # lint / format
        "android-lint",
        "ktlint",
        "detekt",
    }
)

_RUST_DEV_TOOLS: frozenset[str] = frozenset(
    {
        # cargo install <name> targets
        "cargo-audit",
        "cargo-deny",
        "cargo-tarpaulin",
        "cargo-llvm-cov",
        "cargo-nextest",
        "cargo-watch",
        "cargo-edit",
        "cargo-outdated",
        "cargo-msrv",
        "cargo-machete",
        "cargo-bloat",
        # rustup components are gated separately, but the names are
        # commonly typed as `rustup component add <name>`
        "rustfmt",
        "clippy",
        "rust-analyzer",
        # criterion is a dev-dependency, not a binary
        "criterion",
    }
)

_GO_DEV_TOOLS: frozenset[str] = frozenset(
    {
        # `go install <path>` targets
        "github.com/golangci/golangci-lint/cmd/golangci-lint",
        "honnef.co/go/tools/cmd/staticcheck",
        "golang.org/x/tools/cmd/goimports",
        "mvdan.cc/gofumpt",
        "github.com/securego/gosec/v2/cmd/gosec",
        "github.com/sonatype-nexus-community/nancy",
        "github.com/jstemmer/go-junit-report/v2",
        "gotest.tools/gotestsum",
    }
)

# Union exposed to the existing pip-only ``is_dev_tool`` consumer.
# When an ``npm install`` / ``dotnet tool install`` / ``cargo install``
# gate lands, it should consume the per-ecosystem set above directly
# rather than this union, to keep cross-ecosystem name collisions from
# silently widening the policy.
_DEV_TOOL_ALLOWLIST: frozenset[str] = (
    _PYTHON_DEV_TOOLS
    | _NODE_DEV_TOOLS
    | _DOTNET_DEV_TOOLS
    | _JVM_DEV_TOOLS
    | _ANDROID_DEV_TOOLS
    | _RUST_DEV_TOOLS
    | _GO_DEV_TOOLS
)


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


def detect_python_install_anchor(repo: Path) -> bool:
    """True iff the repo ships any project-scoped dependency declaration:
    a true lockfile, a ``pyproject.toml`` / ``setup.{cfg,py}``, or a
    pinned ``requirements*.txt``. Wider than ``detect_python_lockfile``
    because it accepts the common bootstrap state where the project
    declares its dependencies but hasn't generated a transitive lockfile."""
    return any((repo / marker).exists() for marker in _PYTHON_INSTALL_ANCHOR_MARKERS)


def is_dev_tool(name: str) -> bool:
    """True iff ``name`` (a pip install target) is on the curated
    test/dev tooling allowlist. Strips a trailing ``[extra]`` and any
    version specifier (``pkg==1.2.3`` -> ``pkg``)."""
    base = name.split("[", 1)[0]
    for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "==="):
        if sep in base:
            base = base.split(sep, 1)[0]
            break
    return base.strip().lower() in _DEV_TOOL_ALLOWLIST


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
