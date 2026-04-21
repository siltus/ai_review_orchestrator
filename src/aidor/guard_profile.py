"""Build the `--allow-tool` / `--deny-tool` flag matrix for each Copilot invocation.

The flag set implements most of the Guard layer (§9 of plan.md). The path-
containment check that cannot be expressed as a flag pattern lives in the
hook resolver (`hook_resolver.py`).

Design notes (re-read the Copilot CLI docs if tempted to "fix" this):

- The only valid `--allow-tool` / `--deny-tool` kinds are `read`, `write`,
  `shell(...)`, `url(...)`, `memory`, and `<mcp-server>(...)`. `bash(...)`
  and `powershell(...)` are NOT kinds — they are tool NAMES that the shell
  kind matches against on the respective platforms. Emitting `bash(git push)`
  as a flag does nothing; it is silently ignored by the CLI.

- `shell(cmd:*)` is the prefix form — it matches `cmd` followed by a space,
  so `shell(git:*)` matches `git push` and `git pull` but NOT `gitea`. This
  is the official pattern for "all git commands except git push":
      copilot --allow-tool='shell(git:*)' --deny-tool='shell(git push)'
  Deny rules always take precedence over allow rules.

- We therefore use `shell(cmd:*)` as the broad allow and enumerate the
  specific dangerous sub-invocations (e.g. `git push`, `npm install -g`)
  on the deny side. Every such pairing is covered below.
"""

from __future__ import annotations

from pathlib import Path

_BASE_ALLOW: tuple[str, ...] = (
    "read",
    "write",
    "shell(git:*)",
    "shell(python:*)",
    "shell(python3:*)",
    "shell(python3.11:*)",
    "shell(python.exe:*)",
    "shell(py:*)",
    "shell(py.exe:*)",
    "shell(pytest:*)",
    "shell(pytest.exe:*)",
    "shell(py.test:*)",
    "shell(ruff:*)",
    "shell(ruff.exe:*)",
    "shell(pip-audit:*)",
    "shell(pip-audit.exe:*)",
    "shell(pre-commit:*)",
    "shell(pre-commit.exe:*)",
    "shell(coverage:*)",
    "shell(coverage.exe:*)",
    "shell(where:*)",
    "shell(where.exe:*)",
    "shell(which:*)",
    "shell(Get-ChildItem:*)",
    "shell(Get-Item:*)",
    "shell(Get-ItemProperty:*)",
    "shell(Get-Content:*)",
    "shell(Get-Command:*)",
    "shell(Get-Location:*)",
    "shell(Get-Date:*)",
    "shell(Get-Process:*)",
    "shell(Test-Path:*)",
    "shell(Resolve-Path:*)",
    "shell(Select-String:*)",
    "shell(Select-Object:*)",
    "shell(Sort-Object:*)",
    "shell(Where-Object:*)",
    "shell(ForEach-Object:*)",
    "shell(Measure-Object:*)",
    "shell(Format-List:*)",
    "shell(Format-Table:*)",
    "shell(Out-String:*)",
    "shell(Write-Output:*)",
    "shell(Write-Host:*)",
    "shell(New-Item:*)",
    "shell(Join-Path:*)",
    "shell(Split-Path:*)",
    "shell(node:*)",
    "shell(node.exe:*)",
    "shell(npm:*)",
    "shell(npm.cmd:*)",
    "shell(npx:*)",
    "shell(npx.cmd:*)",
    "shell(pnpm:*)",
    "shell(pnpm.cmd:*)",
    "shell(pnpx:*)",
    "shell(yarn:*)",
    "shell(yarn.cmd:*)",
    "shell(tsc:*)",
    "shell(tsc.cmd:*)",
    "shell(ts-node:*)",
    "shell(eslint:*)",
    "shell(eslint.cmd:*)",
    "shell(prettier:*)",
    "shell(prettier.cmd:*)",
    "shell(jest:*)",
    "shell(jest.cmd:*)",
    "shell(vitest:*)",
    "shell(vitest.cmd:*)",
    "shell(mocha:*)",
    "shell(playwright:*)",
    "shell(dotnet:*)",
    "shell(dotnet.exe:*)",
    "shell(msbuild:*)",
    "shell(nuget:*)",
    "shell(gradle:*)",
    "shell(gradlew:*)",
    "shell(gradlew.bat:*)",
    "shell(./gradlew:*)",
    "shell(.\\gradlew.bat:*)",
    "shell(mvn:*)",
    "shell(mvnw:*)",
    "shell(mvnw.cmd:*)",
    "shell(adb:*)",
    "shell(sdkmanager:*)",
    "shell(avdmanager:*)",
    "shell(kotlin:*)",
    "shell(kotlinc:*)",
    "shell(java:*)",
    "shell(javac:*)",
)

_BASE_DENY: tuple[str, ...] = (
    "shell(git push)",
    "shell(git remote)",
    "shell(git config --global)",
    "shell(git config --system)",
    "shell(sudo)",
    "shell(doas)",
    "shell(rm -rf /)",
    "shell(rmdir /s)",
    "shell(pipx install)",
    "shell(npm install -g)",
    "shell(npm i -g)",
    "shell(npm install --global)",
    "shell(pnpm add -g)",
    "shell(pnpm install -g)",
    "shell(pnpm add --global)",
    "shell(yarn global)",
    "shell(cargo install)",
    "shell(go install)",
    "shell(choco)",
    "shell(winget)",
    "shell(apt)",
    "shell(apt-get)",
    "shell(brew)",
    "shell(scoop)",
    "shell(curl)",
    "shell(wget)",
    "shell(copilot update)",
    "shell(copilot login)",
    "shell(copilot logout)",
    "shell(dotnet tool install -g)",
    "shell(dotnet tool install --global)",
    "shell(dotnet workload install)",
    "shell(npx --yes)",
    "shell(npx -y)",
    "shell(sdkmanager --install)",
    "shell(python -m pip install)",
    "shell(python3 -m pip install)",
    "shell(py -m pip install)",
)

_PYTHON_GLOBAL_INSTALL_DENY_BROAD: tuple[str, ...] = (
    "shell(pip install)",
    "shell(pip3 install)",
)

_PYTHON_GLOBAL_INSTALL_DENY_NARROW: tuple[str, ...] = (
    "shell(pip install --user)",
    "shell(pip install --target)",
    "shell(pip install --prefix)",
    "shell(pip install --root)",
    "shell(pip3 install --user)",
    "shell(pip3 install --target)",
    "shell(pip3 install --prefix)",
    "shell(pip3 install --root)",
)

_LOCAL_INSTALL_ECOSYSTEMS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("poetry.lock", "uv.lock", "Pipfile.lock"),
        (
            "shell(poetry install)",
            "shell(pip install -e)",
            "shell(uv sync)",
            "shell(uv pip install)",
        ),
    ),
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
    (("Cargo.lock",), ("shell(cargo build)", "shell(cargo fetch)")),
    (("go.sum",), ("shell(go mod download)",)),
    (("pixi.lock",), ("shell(pixi install)",)),
)

_PYTHON_LOCAL_INSTALL_MARKERS: tuple[str, ...] = (
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
)


def _ecosystems_present(repo: Path) -> list[tuple[str, ...]]:
    matched: list[tuple[str, ...]] = []
    for markers, allow in _LOCAL_INSTALL_ECOSYSTEMS:
        if any((repo / marker).exists() for marker in markers):
            matched.append(allow)
    return matched


def detect_local_install_available(repo: Path) -> bool:
    return bool(_ecosystems_present(repo))


def _detect_python_local_install_available(repo: Path) -> bool:
    return any((repo / marker).exists() for marker in _PYTHON_LOCAL_INSTALL_MARKERS)


def build_flags(
    repo: Path,
    *,
    allow_local_install: bool,
) -> list[str]:
    flags: list[str] = []

    for rule in _BASE_ALLOW:
        flags.append(f"--allow-tool={rule}")

    if allow_local_install:
        seen: set[str] = set()
        for ecosystem_allow in _ecosystems_present(repo):
            for rule in ecosystem_allow:
                if rule in seen:
                    continue
                seen.add(rule)
                flags.append(f"--allow-tool={rule}")

    if allow_local_install and _detect_python_local_install_available(repo):
        python_pip_deny = _PYTHON_GLOBAL_INSTALL_DENY_NARROW
    else:
        python_pip_deny = _PYTHON_GLOBAL_INSTALL_DENY_BROAD

    for rule in _BASE_DENY + python_pip_deny:
        flags.append(f"--deny-tool={rule}")

    return flags
