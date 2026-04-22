"""Tests for the orchestrator's pre-run advisory warnings."""

from __future__ import annotations

from pathlib import Path

from aidor.preflight import compute_warnings, render_warnings


def test_no_warnings_for_empty_repo(tmp_path: Path):
    assert compute_warnings(tmp_path, host_system="Linux") == []


def test_no_warnings_for_python_repo_on_linux(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("print('hi')\n", encoding="utf-8")
    assert compute_warnings(tmp_path, host_system="Linux") == []


def test_warns_on_wpf_csproj_on_linux(tmp_path: Path):
    csproj = tmp_path / "App.csproj"
    csproj.write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>WinExe</OutputType>
    <TargetFramework>net8.0-windows</TargetFramework>
    <UseWPF>true</UseWPF>
  </PropertyGroup>
</Project>
""",
        encoding="utf-8",
    )
    warnings = compute_warnings(tmp_path, host_system="Linux")
    assert any("Windows-only" in w and "App.csproj" in w for w in warnings)


def test_no_warning_for_wpf_on_windows(tmp_path: Path):
    csproj = tmp_path / "App.csproj"
    csproj.write_text(
        "<Project><PropertyGroup><UseWPF>true</UseWPF></PropertyGroup></Project>",
        encoding="utf-8",
    )
    warnings = compute_warnings(tmp_path, host_system="Windows")
    assert not any("Windows-only" in w for w in warnings)


def test_warns_on_winforms_csproj_on_macos(tmp_path: Path):
    csproj = tmp_path / "Forms.csproj"
    csproj.write_text(
        "<Project><PropertyGroup><UseWindowsForms>true</UseWindowsForms>"
        "</PropertyGroup></Project>",
        encoding="utf-8",
    )
    warnings = compute_warnings(tmp_path, host_system="Darwin")
    assert any("Windows-only" in w for w in warnings)


def test_warns_on_large_repo(tmp_path: Path):
    # Create > threshold tracked files to trigger the size warning.
    src = tmp_path / "src"
    src.mkdir()
    for i in range(2050):
        (src / f"f{i}.txt").write_text("x", encoding="utf-8")
    warnings = compute_warnings(tmp_path, host_system="Linux")
    assert any("Large repository" in w for w in warnings)


def test_excludes_node_modules_and_venv(tmp_path: Path):
    # Files in excluded dirs must NOT count toward the size threshold.
    for excluded in ("node_modules", ".venv", ".git", "bin", "obj"):
        d = tmp_path / excluded
        d.mkdir()
        for i in range(500):
            (d / f"junk{i}.bin").write_text("z", encoding="utf-8")
    assert compute_warnings(tmp_path, host_system="Linux") == []


def test_render_warnings_empty():
    assert render_warnings([]) == ""


def test_render_warnings_formats_bullets():
    rendered = render_warnings(["alpha", "beta"])
    assert "preflight warnings" in rendered
    assert "• alpha" in rendered
    assert "• beta" in rendered
