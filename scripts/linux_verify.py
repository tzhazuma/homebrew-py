#!/usr/bin/env python3
"""Reproducible real-Linux verification for pybrew bottles and patchelf.

Run this inside a Linux machine or container that shares this checkout (the
OrbStack Ubuntu machine used during development works):

    orb -m ubuntu bash -lc \
      'cd /path/to/homebrew-py && python3 scripts/linux_verify.py'

The script:

1. checks that it really is running on Linux and that a real ``patchelf`` is
   available (installing it with apt is printed if it is missing);
2. downloads the real Homebrew bottles referenced by
   ``tests/fixtures/core_formulas/*.rb`` from ghcr.io and verifies their
   SHA-256, or reuses the download cache with ``--offline``;
3. installs them with ``--ignore-dependencies`` into a throwaway prefix;
4. reads ``INSTALL_RECEIPT.json`` and inspects every ELF file with
   ``readelf -d``, ``patchelf --print-rpath`` and
   ``patchelf --print-interpreter``, asserting that no ``@@HOMEBREW_*@@``
   placeholder survives in RPATH, RUNPATH or PT_INTERP;
5. runs the relocated executables (after providing the ``lib/ld.so`` symlink a
   real Homebrew prefix would get from its glibc formula).

Every command is printed together with its observed result.  Exit status is 0
only when all checks pass, 1 on a failed check and 2 on a missing prerequisite.

The pytest counterpart is ``tests/test_linux_real.py``; enable it with
``PYBREW_LINUX_REAL=1`` after populating the cache with this script:

    python3 scripts/linux_verify.py --cache ~/.cache/pybrew/linux-verify
    PYTHONPATH=src PYBREW_LINUX_REAL=1 python3 -m pytest tests/test_linux_real.py
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pybrew.cellar import read_receipt  # noqa: E402
from pybrew.download import Downloader, sha256_file  # noqa: E402
from pybrew.installer import (  # noqa: E402
    InstallOptions,
    Installer,
    _find_patchelf,
)
from pybrew.models import BottleFile, Formula, candidate_bottle_tags  # noqa: E402
from pybrew.paths import Config  # noqa: E402
from pybrew.ruby_parser import parse_formula_file  # noqa: E402
from pybrew.taps import FormulaLoader, discover_taps  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "core_formulas"
PLACEHOLDER = b"@@HOMEBREW"
DEFAULT_FORMULAS = ("libslirp", "tree")
DEFAULT_CACHE = Path.home() / ".cache" / "pybrew" / "linux-verify"


class CheckFailure(RuntimeError):
    """A verification step failed."""


class PrerequisiteError(RuntimeError):
    """The environment cannot run the verification at all."""


def say(message: str = "") -> None:
    print(message, flush=True)


def command_text(command: Iterable[object]) -> str:
    return " ".join(str(part) for part in command)


def run(command: list[str], *, timeout: int = 120, check: bool = False):
    """Print and execute a command, echoing its output."""

    say(f"  $ {command_text(command)}")
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckFailure(f"could not run {command_text(command)}: {error}") from error
    if result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            say(f"    {line}")
    if result.returncode != 0 and result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            say(f"    [stderr] {line}")
    if check and result.returncode != 0:
        raise CheckFailure(
            f"command failed ({result.returncode}): {command_text(command)}"
        )
    return result


def patchelf_query(patchelf: str, flag: str, path: Path) -> tuple[int, str]:
    """Run a read-only patchelf query; return (exit code, stripped stdout)."""

    try:
        result = subprocess.run(
            [patchelf, flag, str(path)],
            text=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout.strip()


def check_platform() -> None:
    say("[1/5] platform")
    say(f"  sys.platform = {sys.platform}")
    say(f"  machine      = {platform.machine()}")
    say(f"  python       = {sys.executable} {platform.python_version()}")
    if not sys.platform.startswith("linux"):
        raise PrerequisiteError(
            "this verifier must run inside Linux; from macOS use:\n"
            f"  orb -m ubuntu bash -lc 'cd {ROOT} && python3 scripts/linux_verify.py'"
        )


def check_tools() -> tuple[str, Optional[str]]:
    say("[2/5] tools")
    patchelf = _find_patchelf()
    if patchelf is None:
        raise PrerequisiteError(
            "patchelf is required for real ELF relocation; install it with:\n"
            "  sudo apt-get update && sudo apt-get install -y patchelf"
        )
    path = Path(patchelf)
    say(f"  PYBREW_PATCHELF = {os.environ.get('PYBREW_PATCHELF')!r}")
    say(f"  patchelf        = {patchelf}")
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise PrerequisiteError(f"patchelf is not a real executable file: {patchelf}")
    run([patchelf, "--version"], timeout=30, check=True)

    readelf = shutil.which("readelf")
    if readelf is None:
        say("  readelf not found; skipping the readelf -d cross-check")
    else:
        run([readelf, "--version"], timeout=30)
    return patchelf, readelf


def check_https() -> None:
    """Fail early when the interpreter cannot download from ghcr.io."""

    try:
        import ssl  # noqa: F401
    except ImportError as error:
        raise PrerequisiteError(
            "this Python cannot do HTTPS (the _ssl module is missing), so the "
            "live ghcr.io download would fail with 'unknown url type: https'.\n"
            f"  interpreter: {sys.executable}\n"
            "Use a Python built with SSL (e.g. /usr/bin/python3 on Ubuntu) or "
            "pass --offline."
        ) from error


def load_formula(name: str) -> Formula:
    path = FIXTURES / f"{name}.rb"
    if not path.is_file():
        raise CheckFailure(f"fixture formula not found: {path}")
    return parse_formula_file(path, tap="homebrew/core")


def resolve_bottle(formula: Formula) -> tuple[str, BottleFile]:
    bottle = formula.bottle_for_tag()
    if bottle is None:
        raise CheckFailure(
            f"{formula.name} has no bottle for this host "
            f"(candidate tags: {candidate_bottle_tags()})"
        )
    tag, bottle_file = bottle
    if not tag.endswith("_linux"):
        raise CheckFailure(f"{formula.name}: expected a Linux bottle tag, got {tag!r}")
    return tag, bottle_file


def fetch_bottle(
    formula: Formula, tag: str, bottle_file: BottleFile, config: Config, offline: bool
) -> Path:
    url = formula.bottle_url(tag, bottle_file)
    hint = f"{formula.name}--{formula.pkg_version}.{tag}.bottle.tar.gz"
    downloader = Downloader(config.downloads, config.bottle_dirs)
    say(f"  bottle URL   = {url}")
    say(f"  expected sha = {bottle_file.sha256}")
    if offline:
        cached = downloader.find_cached(bottle_file.sha256)
        if cached is None:
            raise CheckFailure(
                f"--offline: no cached bottle with sha256 {bottle_file.sha256} "
                f"under {config.downloads}"
            )
        archive = cached
        say(f"  cached       = {archive}")
    else:
        archive = downloader.fetch(url, bottle_file.sha256, hint=hint)
        say(f"  downloaded   = {archive}")
    actual = sha256_file(archive)
    say(f"  actual sha   = {actual} ({archive.stat().st_size} bytes)")
    if actual != bottle_file.sha256:
        raise CheckFailure(
            f"bottle checksum mismatch: expected {bottle_file.sha256}, got {actual}"
        )
    return archive


def install_formula(formula, archive: Path, config: Config):
    loader = FormulaLoader(discover_taps(config.taps))
    installer = Installer(config, loader)
    say(
        "  equivalent CLI:\n"
        f"    PYTHONPATH=src python3 -m pybrew --prefix {config.prefix} "
        f"--cache {config.cache} --tap {FIXTURES} install {formula.name} "
        f"--ignore-dependencies --bottle-file {archive}"
    )
    results = installer.install(
        formula,
        InstallOptions(ignore_dependencies=True, bottle_file=archive),
    )
    result = results[-1]
    if result.skipped:
        raise CheckFailure(f"{formula.name} was unexpectedly skipped (already installed?)")
    kind = "poured from bottle" if result.from_bottle else "built from source"
    say(f"  result: {kind} -> {result.keg}")
    return result


def elf_files(keg: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(keg.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if path.read_bytes()[:4] == b"\x7fELF":
                found.append(path)
        except OSError:
            continue
    return found


def check_receipt(keg: Path, formula) -> dict:
    receipt = read_receipt(keg)
    say("  INSTALL_RECEIPT.json:")
    for line in json.dumps(receipt, indent=2, sort_keys=True).splitlines():
        say(f"    {line}")
    if receipt.get("poured_from_bottle") is not True:
        raise CheckFailure("receipt does not record poured_from_bottle=true")
    if receipt.get("version") != formula.pkg_version:
        raise CheckFailure(
            f"receipt version {receipt.get('version')!r} != {formula.pkg_version!r}"
        )
    expected_deps = [dep.name for dep in formula.runtime_dependencies()]
    if receipt.get("runtime_dependencies") != expected_deps:
        raise CheckFailure(
            f"receipt runtime_dependencies {receipt.get('runtime_dependencies')!r} "
            f"!= {expected_deps!r}"
        )
    return receipt


def inspect_elf_files(
    keg: Path, prefix: Path, patchelf: str, readelf: Optional[str]
) -> tuple[int, int]:
    paths = elf_files(keg)
    if not paths:
        raise CheckFailure("no ELF files found in the keg")
    relocated_rpath = 0
    relocated_interpreter = 0
    for path in paths:
        say(f"  ELF {path.relative_to(keg)}")
        if readelf is not None:
            result = run([readelf, "-d", str(path)], timeout=30)
            for line in result.stdout.splitlines():
                if "RPATH" in line or "RUNPATH" in line:
                    say(f"    readelf: {line.strip()}")

        rcode, rpath = patchelf_query(patchelf, "--print-rpath", path)
        say(f"    patchelf --print-rpath      = {rpath!r} (exit {rcode})")
        if rcode == 0 and PLACEHOLDER.decode() in rpath:
            raise CheckFailure(f"placeholder left in RPATH of {path}")
        if rcode == 0 and rpath:
            if str(prefix) in rpath:
                relocated_rpath += 1
            else:
                say("    note: RPATH keeps no prefix entry (left as built)")

        icode, interpreter = patchelf_query(patchelf, "--print-interpreter", path)
        if icode == 0:
            say(f"    patchelf --print-interpreter = {interpreter!r}")
            if PLACEHOLDER.decode() in interpreter:
                raise CheckFailure(f"placeholder left in PT_INTERP of {path}")
            if interpreter and str(prefix) in interpreter:
                relocated_interpreter += 1

    if relocated_rpath == 0 and relocated_interpreter == 0:
        raise CheckFailure("no ELF file shows a relocated prefix path")
    say(
        f"  ELF relocation: {len(paths)} file(s), "
        f"{relocated_rpath} RPATH/RUNPATH and "
        f"{relocated_interpreter} PT_INTERP rewritten"
    )
    return relocated_rpath, relocated_interpreter


def check_placeholders(keg: Path) -> None:
    checked = 0
    offenders: list[Path] = []
    for path in keg.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        checked += 1
        try:
            if PLACEHOLDER in path.read_bytes():
                offenders.append(path)
        except OSError:
            continue
    if offenders:
        listing = "\n".join(f"    {path}" for path in offenders)
        raise CheckFailure(f"placeholder bytes left in {len(offenders)} file(s):\n{listing}")
    say(f"  no @@HOMEBREW placeholder bytes in {checked} keg file(s)")


def system_loader(patchelf: str) -> Optional[str]:
    for probe in ("/bin/sh", "/usr/bin/env"):
        path = Path(probe)
        if not path.exists():
            continue
        code, interpreter = patchelf_query(patchelf, "--print-interpreter", path)
        if code == 0 and interpreter:
            return interpreter
    return None


def run_relocated_binaries(keg: Path, prefix: Path, patchelf: str) -> None:
    bin_dir = keg / "bin"
    if not bin_dir.is_dir():
        say("  no bin/ directory; skipping executable run")
        return
    loader = system_loader(patchelf)
    if loader is None:
        say("  cannot determine the system loader; skipping executable run")
        return
    loader_link = prefix / "lib" / "ld.so"
    loader_link.parent.mkdir(parents=True, exist_ok=True)
    if not os.path.lexists(loader_link):
        # A real Homebrew prefix gets this from the glibc formula.
        loader_link.symlink_to(loader)
        say(f"  created {loader_link} -> {loader} (mirrors the glibc formula)")
    for candidate in sorted(bin_dir.iterdir()):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            if candidate.read_bytes()[:4] != b"\x7fELF":
                continue
        except OSError:
            continue
        code, interpreter = patchelf_query(patchelf, "--print-interpreter", candidate)
        if code != 0 or not interpreter:
            continue
        result = run([str(candidate), "--version"], timeout=30)
        if result.returncode != 0:
            raise CheckFailure(f"relocated executable failed to run: {candidate}")
        say(f"  ran {candidate.name} --version successfully")


def verify_formula(
    name: str,
    config: Config,
    patchelf: str,
    readelf: Optional[str],
    offline: bool,
) -> None:
    formula = load_formula(name)
    tag, bottle_file = resolve_bottle(formula)
    say(f"  formula      = {formula.name} {formula.pkg_version}")
    say(f"  bottle tag   = {tag}")
    archive = fetch_bottle(formula, tag, bottle_file, config, offline)
    result = install_formula(formula, archive, config)
    keg = result.keg
    if not keg.is_dir():
        raise CheckFailure(f"keg was not created: {keg}")
    check_receipt(keg, formula)
    inspect_elf_files(keg, config.prefix, patchelf, readelf)
    check_placeholders(keg)
    run_relocated_binaries(keg, config.prefix, patchelf)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Install real Homebrew Linux bottles and verify that patchelf "
            "relocated every ELF RPATH/RUNPATH/interpreter placeholder."
        )
    )
    parser.add_argument(
        "--formula",
        action="append",
        dest="formulas",
        metavar="NAME",
        help=(
            "fixture formula to verify (repeatable; default: "
            + ", ".join(DEFAULT_FORMULAS)
            + ")"
        ),
    )
    parser.add_argument(
        "--prefix",
        type=Path,
        help="install prefix (default: a fresh temporary directory)",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        help=f"download cache (default: {DEFAULT_CACHE})",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="never download; fail unless the bottle is already cached",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove the temporary prefix on success",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    formulas = args.formulas or list(DEFAULT_FORMULAS)
    cache = Path(args.cache).expanduser()
    temporary_prefix = args.prefix is None
    try:
        say("pybrew real-Linux bottle verification")
        say(f"  repo  = {ROOT}")
        say(f"  cache = {cache}")
        check_platform()
        patchelf, readelf = check_tools()
        if not args.offline:
            check_https()

        prefix = (
            Path(tempfile.mkdtemp(prefix="pybrew-linux-verify-"))
            if args.prefix is None
            else Path(args.prefix).expanduser()
        )
        say(f"  prefix = {prefix}")
        say("[3/5] prefix and cache")
        config = Config.default(prefix=prefix, taps=[FIXTURES], cache=cache)
        config.ensure()
        say(f"  prepared {config.prefix} and {config.downloads}")

        say("[4/5] bottles")
        for name in formulas:
            say(f"  -- {name} --")
            verify_formula(name, config, patchelf, readelf, args.offline)

        say("[5/5] summary")
        say(
            f"  PASS: {len(formulas)} real Linux bottle(s) verified "
            f"({', '.join(formulas)})"
        )
        say(f"  kegs: {config.cellar}")
        elapsed = time.monotonic() - started
        say(f"  elapsed: {elapsed:.1f}s")
        if args.clean and temporary_prefix:
            shutil.rmtree(prefix, ignore_errors=True)
            say(f"  removed temporary prefix {prefix}")
        return 0
    except PrerequisiteError as error:
        say(f"PREREQUISITE ERROR: {error}")
        return 2
    except CheckFailure as error:
        say(f"FAIL: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
