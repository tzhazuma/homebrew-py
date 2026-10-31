"""Installer-level source-build environment tests.

These tests exercise the Python installer path end to end with synthetic local
sources and fake compiler executables.  No real compiler is invoked and no
network access is required; the fake compilers record the environment and argv
they received.
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest

from pybrew.installer import InstallOptions, Installer
from pybrew.paths import Config
from pybrew.taps import FormulaLoader, discover_taps

_FAKE_COMPILER_SCRIPT = """#!/bin/sh
{
  printf 'argv=%s\\n' "$*"
  printf 'CC=%s\\n' "${CC:-}"
  printf 'CXX=%s\\n' "${CXX:-}"
  printf 'CFLAGS=%s\\n' "${CFLAGS:-}"
  printf 'CXXFLAGS=%s\\n' "${CXXFLAGS:-}"
  printf 'LDFLAGS=%s\\n' "${LDFLAGS:-}"
} > "$PWD/__REPORT__"
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tar(source_dir: Path, destination: Path, top: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as tar:
        for path in sorted(source_dir.rglob("*")):
            arcname = f"{top}/{path.relative_to(source_dir)}"
            info = tarfile.TarInfo(arcname)
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                data = path.read_bytes()
                info.size = len(data)
                info.mode = path.stat().st_mode & 0o777
                tar.addfile(info, io.BytesIO(data))
    return destination


def _make_tap(
    tmp_path: Path, name: str, version: str, project: Path, install_block: str
) -> Path:
    top = f"{name}-{version}"
    archive = tmp_path / "src" / f"{top}.tar.gz"
    _tar(project, archive, top)
    tap = tmp_path / "tap"
    (tap / "Formula").mkdir(parents=True, exist_ok=True)
    ruby = f'''class {name.title().replace("-", "")} < Formula
  desc "source build fixture"
  homepage "https://example.com/{name}"
  url "{archive.as_uri()}"
  sha256 "{_sha(archive)}"
  license "MIT"

  def install
{install_block}
  end
end
'''
    (tap / "Formula" / f"{name}.rb").write_text(ruby, encoding="utf-8")
    return tap


def _install(
    tmp_path: Path,
    tap: Path,
    name: str,
    environment: dict[str, str] | None = None,
):
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    loader = FormulaLoader(discover_taps([tap]))
    formula = loader.load(name)
    installer = Installer(config, loader, environment=environment)
    results = installer.install(formula, InstallOptions(build_from_source=True))
    return config, results[-1]


def _fake_compiler(directory: Path, name: str, report: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        _FAKE_COMPILER_SCRIPT.replace("__REPORT__", report), encoding="utf-8"
    )
    path.chmod(0o755)
    return path


def _compiler_report(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def _project(tmp_path: Path, name: str) -> Path:
    project = tmp_path / f"{name}-src"
    project.mkdir()
    (project / "README").write_text(f"{name} fixture\n", encoding="utf-8")
    return project


def test_injected_compiler_environment_reaches_build_commands(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    _fake_compiler(fake_bin, "clang", "cc-report.txt")
    _fake_compiler(fake_bin, "clang++", "cxx-report.txt")

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment.get('PATH', '')}"
    environment["PYBREW_COMPILER"] = "clang"
    environment["CFLAGS"] = "-O2 -g"
    environment["CXXFLAGS"] = "-O3"
    environment["LDFLAGS"] = "-L/opt/injected"
    for name in ("PYBREW_CC", "CC", "PYBREW_CXX", "CXX"):
        environment.pop(name, None)

    project = _project(tmp_path, "injected-env")
    tap = _make_tap(
        tmp_path,
        "injected-env",
        "1.0",
        project,
        "\n".join(
            [
                '    system ENV["CC"], "injected-c"',
                '    system ENV["CXX"], "injected-cxx"',
                '    bin.install "cc-report.txt"',
                '    bin.install "cxx-report.txt"',
            ]
        ),
    )
    config, result = _install(
        tmp_path, tap, "injected-env", environment=environment
    )
    prefix = config.prefix

    cc = _compiler_report(result.keg / "bin" / "cc-report.txt")
    assert cc["argv"] == "injected-c"
    assert cc["CC"] == str(fake_bin / "clang")
    assert cc["CFLAGS"] == f"-O2 -g -I{prefix / 'include'}"
    assert cc["LDFLAGS"] == f"-L/opt/injected -L{prefix / 'lib'}"

    cxx = _compiler_report(result.keg / "bin" / "cxx-report.txt")
    assert cxx["argv"] == "injected-cxx"
    assert cxx["CXX"] == str(fake_bin / "clang++")
    assert cxx["CXXFLAGS"] == f"-O3 -I{prefix / 'include'}"


def test_explicit_pybrew_compilers_are_not_overwritten_by_family(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    # A clang family exists and is requested; explicit PYBREW_CC/CXX must win.
    _fake_compiler(fake_bin, "clang", "clang-report.txt")
    _fake_compiler(fake_bin, "clang++", "clangxx-report.txt")
    user_cc = _fake_compiler(fake_bin, "user-cc", "cc-report.txt")
    user_cxx = _fake_compiler(fake_bin, "user-cxx", "cxx-report.txt")

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment.get('PATH', '')}"
    environment["PYBREW_COMPILER"] = "clang"
    environment["PYBREW_CC"] = str(user_cc)
    environment["PYBREW_CXX"] = str(user_cxx)
    for name in ("CC", "CXX"):
        environment.pop(name, None)

    project = _project(tmp_path, "explicit-compiler")
    tap = _make_tap(
        tmp_path,
        "explicit-compiler",
        "1.0",
        project,
        "\n".join(
            [
                '    system ENV["CC"], "explicit-c"',
                '    system ENV["CXX"], "explicit-cxx"',
                '    bin.install "cc-report.txt"',
                '    bin.install "cxx-report.txt"',
            ]
        ),
    )
    _config, result = _install(
        tmp_path, tap, "explicit-compiler", environment=environment
    )

    cc = _compiler_report(result.keg / "bin" / "cc-report.txt")
    assert cc["CC"] == str(user_cc)
    assert cc["argv"] == "explicit-c"
    cxx = _compiler_report(result.keg / "bin" / "cxx-report.txt")
    assert cxx["CXX"] == str(user_cxx)
    assert cxx["argv"] == "explicit-cxx"


def test_missing_requested_compiler_falls_back_through_installer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    environment = {"PATH": str(empty_bin), "PYBREW_COMPILER": "gcc"}

    project = _project(tmp_path, "fallback-compiler")
    tap = _make_tap(
        tmp_path,
        "fallback-compiler",
        "1.0",
        project,
        "\n".join(
            [
                '    system "/bin/sh", "-c", '
                '\'printf "%s" "${CC:-unset}" > cc-value.txt\'',
                '    bin.install "cc-value.txt"',
            ]
        ),
    )
    _config, result = _install(
        tmp_path, tap, "fallback-compiler", environment=environment
    )

    assert (result.keg / "bin" / "cc-value.txt").read_text() == "unset"
    captured = capsys.readouterr()
    assert "PYBREW_COMPILER=gcc" in captured.err
    assert "not available" in captured.err


def test_default_source_build_environment_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "PYBREW_CC",
        "CC",
        "PYBREW_CXX",
        "CXX",
        "PYBREW_COMPILER",
        "CFLAGS",
        "CXXFLAGS",
        "CPPFLAGS",
        "LDFLAGS",
        "MAKEFLAGS",
    ):
        monkeypatch.delenv(name, raising=False)

    project = _project(tmp_path, "default-env")
    tap = _make_tap(
        tmp_path,
        "default-env",
        "1.0",
        project,
        "\n".join(
            [
                '    system "/bin/sh", "-c", '
                '\'printf "%s|%s|%s|%s|%s" "${CC:-unset}" "${CXX:-unset}" '
                '"${CFLAGS:-}" "${CXXFLAGS:-}" "${LDFLAGS:-}" > env-value.txt\'',
                '    bin.install "env-value.txt"',
            ]
        ),
    )
    config, result = _install(tmp_path, tap, "default-env")
    prefix = config.prefix
    expected = (
        f"unset|unset|-I{prefix / 'include'}|-I{prefix / 'include'}|"
        f"-L{prefix / 'lib'}"
    )
    assert (result.keg / "bin" / "env-value.txt").read_text() == expected
