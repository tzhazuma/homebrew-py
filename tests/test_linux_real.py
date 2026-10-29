"""Opt-in verification against real Homebrew Linux bottles and real patchelf.

These tests are skipped unless they run on Linux with ``PYBREW_LINUX_REAL=1``::

    PYTHONPATH=src PYBREW_LINUX_REAL=1 python3 -m pytest tests/test_linux_real.py

They never download anything.  Populate the download cache first with the
reproducible verifier (which also prints every command and result)::

    python3 scripts/linux_verify.py --cache ~/.cache/pybrew/linux-verify

Bottles are located by the SHA-256 recorded in the fixture formula, first in
``PYBREW_LINUX_REAL_BOTTLE`` (an exact tarball path) and then in the
directories listed in ``PYBREW_LINUX_REAL_CACHE`` (``os.pathsep`` separated)
plus the default pybrew/linux-verify caches.  A missing bottle skips the test
instead of downloading it, so the normal CI run stays offline.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

from pybrew.cellar import read_receipt
from pybrew.download import sha256_file
from pybrew.installer import InstallOptions, Installer, _find_patchelf
from pybrew.paths import Config
from pybrew.ruby_parser import parse_formula_file
from pybrew.taps import FormulaLoader, discover_taps

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "core_formulas"
PLACEHOLDER = b"@@HOMEBREW"
DEFAULT_CACHE = Path.home() / ".cache" / "pybrew" / "linux-verify"
ELF_MAGIC = b"\x7fELF"

pytestmark = [
    pytest.mark.real_bottle,
    pytest.mark.skipif(
        not sys.platform.startswith("linux")
        or os.environ.get("PYBREW_LINUX_REAL") != "1",
        reason=(
            "real Linux verification is opt-in: run on Linux with "
            "PYBREW_LINUX_REAL=1"
        ),
    ),
]


# -- helpers -------------------------------------------------------------
def _formula(name: str):
    path = FIXTURES / f"{name}.rb"
    assert path.is_file(), f"missing fixture formula: {path}"
    return parse_formula_file(path, tap="homebrew/core")


def _host_bottle(formula):
    bottle = formula.bottle_for_tag()
    if bottle is None:
        pytest.skip(f"{formula.name} has no bottle for this host")
    tag, bottle_file = bottle
    assert tag in {"arm64_linux", "x86_64_linux"}, tag
    return tag, bottle_file


def _cache_dirs() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("PYBREW_LINUX_REAL_CACHE")
    if env:
        roots.extend(Path(part) for part in env.split(os.pathsep) if part)
    roots.extend(
        [
            DEFAULT_CACHE,
            Path.home() / ".cache" / "pybrew" / "downloads",
        ]
    )
    directories: list[Path] = []
    for root in roots:
        directories.append(root)
        # --cache points at the cache root; Downloader.fetch writes into
        # <cache>/downloads, so accept both spellings.
        directories.append(root / "downloads")
    return list(dict.fromkeys(directories))


def _find_bottle(sha256: str) -> Optional[Path]:
    candidates: list[Path] = []
    env_file = os.environ.get("PYBREW_LINUX_REAL_BOTTLE")
    if env_file:
        candidates.append(Path(env_file))
    for directory in _cache_dirs():
        if not directory.is_dir():
            continue
        candidates.extend(sorted(directory.glob(f"*{sha256}*")))
    for candidate in candidates:
        if candidate.is_file() and sha256_file(candidate) == sha256:
            return candidate
    return None


def _require_patchelf() -> str:
    patchelf = _find_patchelf()
    assert patchelf is not None, (
        "real Linux verification requires patchelf; install it with "
        "`sudo apt-get update && sudo apt-get install -y patchelf`"
    )
    path = Path(patchelf)
    assert path.is_absolute(), f"patchelf must resolve to a real path, got {patchelf!r}"
    assert path.is_file(), f"patchelf path does not exist: {patchelf}"
    assert os.access(path, os.X_OK), f"patchelf is not executable: {patchelf}"
    result = subprocess.run(
        [patchelf, "--version"], text=True, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "patchelf" in (result.stdout + result.stderr).lower()
    return patchelf


def _patchelf_query(patchelf: str, flag: str, path: Path) -> tuple[int, str]:
    result = subprocess.run(
        [patchelf, flag, str(path)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    return result.returncode, result.stdout.strip()


def _install(formula, archive: Path, tmp_path: Path):
    config = Config.default(
        prefix=tmp_path / "prefix",
        taps=[FIXTURES],
        cache=tmp_path / "cache",
    )
    config.ensure()
    loader = FormulaLoader(discover_taps([FIXTURES]))
    installer = Installer(config, loader)
    results = installer.install(
        formula,
        InstallOptions(ignore_dependencies=True, bottle_file=archive),
    )
    assert len(results) == 1
    result = results[0]
    assert not result.skipped
    assert result.from_bottle
    return config, result


def _elf_files(keg: Path) -> list[Path]:
    return [
        path
        for path in sorted(keg.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and path.read_bytes()[:4] == ELF_MAGIC
    ]


def _system_loader(patchelf: str) -> Optional[str]:
    for probe in ("/bin/sh", "/usr/bin/env"):
        path = Path(probe)
        if not path.exists():
            continue
        code, interpreter = _patchelf_query(patchelf, "--print-interpreter", path)
        if code == 0 and interpreter:
            return interpreter
    return None


def _skip_without_bottle(archive: Optional[Path], formula, sha256: str) -> Path:
    if archive is None:
        pytest.skip(
            f"{formula.name} bottle ({sha256}) is not cached; populate it with "
            "`python3 scripts/linux_verify.py --cache "
            f"{DEFAULT_CACHE}`"
        )
    return archive


# -- tests ---------------------------------------------------------------
def test_patchelf_is_a_real_executable() -> None:
    _require_patchelf()


def test_install_real_libslirp_bottle(tmp_path: Path) -> None:
    patchelf = _require_patchelf()
    formula = _formula("libslirp")
    tag, bottle_file = _host_bottle(formula)
    archive = _skip_without_bottle(
        _find_bottle(bottle_file.sha256), formula, bottle_file.sha256
    )

    config, result = _install(formula, archive, tmp_path)
    keg = config.keg_path("libslirp", "4.9.6")
    assert result.keg == keg
    assert keg.is_dir()
    assert (keg / "include" / "slirp" / "libslirp.h").is_file()
    assert (keg / "lib" / "libslirp.so.0.4.0").is_file()

    receipt = read_receipt(keg)
    assert receipt["poured_from_bottle"] is True
    assert receipt["version"] == "4.9.6"
    assert receipt["runtime_dependencies"] == ["glib"]
    assert receipt["built_as_bottle"] is True

    # Linking works on Linux: the dylib-style .so links are created too.
    assert (config.lib / "libslirp.so").is_symlink()
    assert (config.opt / "libslirp").is_symlink()

    elf_paths = _elf_files(keg)
    assert elf_paths, "a real libslirp bottle must contain ELF libraries"
    rpaths: list[str] = []
    for path in elf_paths:
        code, rpath = _patchelf_query(patchelf, "--print-rpath", path)
        assert code == 0, f"patchelf --print-rpath failed for {path}"
        assert "@@HOMEBREW" not in rpath, f"placeholder left in RPATH of {path}"
        if rpath:
            rpaths.append(rpath)
        code, interpreter = _patchelf_query(patchelf, "--print-interpreter", path)
        if code == 0 and interpreter:
            assert "@@HOMEBREW" not in interpreter
            assert interpreter.startswith(str(config.prefix))
    assert any(str(config.prefix) in rpath for rpath in rpaths), (
        "no ELF RPATH was rewritten to the install prefix; patchelf did not run"
    )
    readelf = shutil.which("readelf")
    if readelf is not None:
        dynamic = subprocess.run(
            [readelf, "-d", str(keg / "lib" / "libslirp.so.0.4.0")],
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert dynamic.returncode == 0, dynamic.stderr
        assert "@@HOMEBREW" not in dynamic.stdout
        assert str(config.prefix) in dynamic.stdout

    # Neither text files nor binaries may retain placeholder bytes.
    for path in keg.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert PLACEHOLDER not in path.read_bytes(), path


def test_install_real_tree_bottle_relocates_interpreter(tmp_path: Path) -> None:
    """Regression: real Linux executables need their PT_INTERP rewritten."""

    patchelf = _require_patchelf()
    formula = _formula("tree")
    tag, bottle_file = _host_bottle(formula)
    archive = _skip_without_bottle(
        _find_bottle(bottle_file.sha256), formula, bottle_file.sha256
    )

    config, result = _install(formula, archive, tmp_path)
    keg = config.keg_path("tree", formula.pkg_version)
    assert result.keg == keg
    tree_bin = keg / "bin" / "tree"
    assert tree_bin.is_file()
    assert tree_bin.read_bytes()[:4] == ELF_MAGIC

    code, interpreter = _patchelf_query(patchelf, "--print-interpreter", tree_bin)
    assert code == 0
    assert interpreter == str(config.prefix / "lib" / "ld.so")
    assert "@@HOMEBREW" not in interpreter
    # The old RPATH-only relocation left this placeholder in the file.
    assert PLACEHOLDER not in tree_bin.read_bytes()

    code, rpath = _patchelf_query(patchelf, "--print-rpath", tree_bin)
    assert code == 0
    assert "@@HOMEBREW" not in rpath
    assert str(config.prefix) in rpath
    readelf = shutil.which("readelf")
    if readelf is not None:
        program = subprocess.run(
            [readelf, "-l", str(tree_bin)],
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert program.returncode == 0, program.stderr
        assert "@@HOMEBREW" not in program.stdout

    # With the lib/ld.so link the glibc formula would provide, the relocated
    # executable must actually start.
    loader = _system_loader(patchelf)
    assert loader is not None, "cannot determine the system ELF interpreter"
    loader_link = config.prefix / "lib" / "ld.so"
    if not os.path.lexists(loader_link):
        loader_link.symlink_to(loader)
    completed = subprocess.run(
        [str(tree_bin), "--version"], text=True, capture_output=True, timeout=30
    )
    assert completed.returncode == 0, completed.stderr
    assert "tree" in completed.stdout.lower()
