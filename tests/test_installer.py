"""Installer-level source-build, bottle and dependency tests.

These tests exercise the Python installer path end to end with synthetic local
sources, bottles and fake compiler executables.  No real compiler is invoked,
no network access is required and real bottles are only used when cached.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import sys
import tarfile
from pathlib import Path

import pytest

from pybrew.cellar import (
    Cellar,
    Linker,
    read_receipt,
    receipt_path,
    write_receipt,
)
from pybrew.errors import (
    BottleError,
    ChecksumMismatch,
    DependencyError,
    PybrewError,
)
from pybrew.installer import (
    InstallOptions,
    Installer,
    LinkageError,
    build_keg_sbom,
    check_linkage,
    read_bottle_manifest,
)
from pybrew.models import (
    BottleFile,
    BottleSpec,
    Dependency,
    Formula,
    candidate_bottle_tags,
)
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


# ---------------------------------------------------------------------------
# synthetic bottles, manifests and linkage
# ---------------------------------------------------------------------------


def _add_member(
    tar: tarfile.TarFile,
    name: str,
    data: bytes = b"",
    mode: int = 0o644,
    kind: bytes = tarfile.REGTYPE,
    target: str = "",
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    info.mode = mode
    info.type = kind
    info.linkname = target
    tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)


def _make_bottle(
    path: Path,
    name: str,
    version: str,
    *,
    files: dict[str, bytes] | None = None,
    symlinks: dict[str, str] | None = None,
    hardlinks: dict[str, str] | None = None,
    receipt: dict[str, object] | None = None,
) -> Path:
    files = dict(files or {})
    symlinks = dict(symlinks or {})
    hardlinks = dict(hardlinks or {})
    if receipt is not None:
        files["INSTALL_RECEIPT.json"] = json.dumps(receipt).encode("utf-8")
    top = f"{name}/{version}"
    directories: set[str] = {top}
    for relative in list(files) + list(symlinks) + list(hardlinks):
        parent = Path(relative).parent
        while str(parent) not in ("", "."):
            directories.add(f"{top}/{parent}")
            parent = parent.parent
    for target in hardlinks.values():
        parent = Path(target).parent
        while str(parent) not in ("", "."):
            directories.add(f"{top}/{parent}")
            parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for directory in sorted(directories):
            _add_member(tar, directory, mode=0o755, kind=tarfile.DIRTYPE)
        # Hard links are intentionally written before their targets so tests
        # exercise deferred extraction rather than archive-order luck.
        for relative, target in sorted(hardlinks.items()):
            _add_member(
                tar,
                f"{top}/{relative}",
                mode=0o644,
                kind=tarfile.LNKTYPE,
                target=f"{top}/{target}",
            )
        for relative, data in sorted(files.items()):
            _add_member(tar, f"{top}/{relative}", data)
        for relative, target in sorted(symlinks.items()):
            _add_member(
                tar,
                f"{top}/{relative}",
                mode=0o777,
                kind=tarfile.SYMTYPE,
                target=target,
            )
    return path


def _formula_with_local_bottle(
    tmp_path: Path,
    name: str,
    version: str,
    *,
    files: dict[str, bytes],
    symlinks: dict[str, str] | None = None,
    hardlinks: dict[str, str] | None = None,
    receipt: dict[str, object] | None = None,
) -> tuple[Formula, Path]:
    tag = candidate_bottle_tags()[0]
    bottle = _make_bottle(
        tmp_path / "bottles" / f"{name}-{version}.{tag}.bottle.tar.gz",
        name,
        version,
        files=files,
        symlinks=symlinks,
        hardlinks=hardlinks,
        receipt=receipt,
    )
    formula = Formula(name=name, version=version)
    formula.bottle = BottleSpec(
        root_url=(tmp_path / "bottles").as_uri(),
        files={tag: BottleFile(tag, _sha(bottle))},
    )
    return formula, bottle


def _direct_installer(tmp_path: Path) -> Installer:
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"
    )
    config.ensure()
    return Installer(config, FormulaLoader([]))


def _synthetic_elf(*needed: str) -> bytes:
    required = needed or ("libmissing.so",)
    strings = bytearray(b"\0")
    offsets: list[int] = []
    for name in required:
        offsets.append(len(strings))
        strings.extend(name.encode("utf-8") + b"\0")
    strtab_offset = 0x200
    base_address = 0x400000
    dynamic_offset = 0x100
    dynamic = [(1, offset) for offset in offsets]
    dynamic.extend(
        [
            (5, base_address + strtab_offset),
            (10, len(strings)),
            (0, 0),
        ]
    )
    dynamic_size = len(dynamic) * 16
    data = bytearray(0x300)
    struct.pack_into("<4sBBBBB7x", data, 0, b"\x7fELF", 2, 1, 1, 0, 0)
    struct.pack_into(
        "<HHIQQQIHHHHHH",
        data,
        16,
        3,
        62,
        1,
        0,
        64,
        0,
        0,
        64,
        56,
        2,
        0,
        0,
        0,
    )
    struct.pack_into(
        "<IIQQQQQQ", data, 64, 1, 5, 0, base_address, base_address, 0x300, 0x300, 0x1000
    )
    struct.pack_into(
        "<IIQQQQQQ",
        data,
        120,
        2,
        6,
        dynamic_offset,
        base_address + dynamic_offset,
        base_address + dynamic_offset,
        dynamic_size,
        dynamic_size,
        0x1000,
    )
    for index, (tag, value) in enumerate(dynamic):
        struct.pack_into("<qQ", data, dynamic_offset + index * 16, tag, value)
    data[strtab_offset : strtab_offset + len(strings)] = strings
    return bytes(data)


def _synthetic_macho(
    loads: list[str], rpaths: list[str] | None = None, *, weak: bool = False
) -> bytes:
    rpaths = rpaths or []
    commands: list[bytes] = []
    command_id = 0x80000018 if weak else 0xC
    for load in loads:
        raw = load.encode("utf-8") + b"\0"
        commands.append(
            struct.pack("<IIIIII", command_id, 24 + len(raw), 24, 2, 0x10000, 0x10000)
            + raw
        )
    for rpath in rpaths:
        raw = rpath.encode("utf-8") + b"\0"
        commands.append(
            struct.pack("<III", 0x8000001C, 12 + len(raw), 12) + raw
        )
    sizeofcmds = sum(len(command) for command in commands)
    return (
        struct.pack(
            "<IiiIIIII",
            0xFEEDFACF,
            0x0100000C,
            0,
            2,
            len(commands),
            sizeofcmds,
            0,
            0,
        )
        + b"".join(commands)
    )


class _MemoryLoader:
    def __init__(self, formulas: dict[str, Formula]):
        self.formulas = formulas

    def load(self, name: str) -> Formula:
        return self.formulas[name]


def _dependency_formula(name: str, dependencies: list[str]) -> Formula:
    formula = Formula(name=name, version="1.0")
    formula.dependencies = [Dependency(name=dep) for dep in dependencies]
    return formula


# -- manifest ------------------------------------------------------------
def test_read_bottle_manifest_lists_files_symlinks_and_receipt(
    tmp_path: Path,
) -> None:
    bottle = _make_bottle(
        tmp_path / "hello.bottle.tar.gz",
        "hello",
        "1.0",
        files={
            "bin/hello": b"#!/bin/sh\n",
            "share/doc/hello/README": b"docs\n",
        },
        symlinks={"lib/libhello.1.dylib": "libhello.dylib"},
        receipt={"version": "1.0"},
    )
    manifest = read_bottle_manifest(bottle)
    top = "hello/1.0"

    assert manifest.sha256 == _sha(bottle)
    assert manifest.archive_size == bottle.stat().st_size
    assert manifest.files[f"{top}/bin/hello"] == len(b"#!/bin/sh\n")
    assert manifest.files[f"{top}/share/doc/hello/README"] == len(b"docs\n")
    assert manifest.symlinks[f"{top}/lib/libhello.1.dylib"] == "libhello.dylib"
    assert manifest.receipt == f"{top}/INSTALL_RECEIPT.json"
    assert manifest.file_count == 3
    assert manifest.symlink_count == 1
    assert manifest.total_size == sum(manifest.files.values())
    assert top in manifest.directories

    summary = manifest.summary()
    assert summary["sha256"] == manifest.sha256
    assert summary["file_count"] == 3
    assert summary["symlink_count"] == 1
    assert summary["total_size"] == manifest.total_size
    assert summary["receipt"] == f"{top}/INSTALL_RECEIPT.json"


def test_read_bottle_manifest_validates_checksum(tmp_path: Path) -> None:
    bottle = _make_bottle(
        tmp_path / "checksum.bottle.tar.gz", "checksum", "1.0", files={"bin/x": b"x"}
    )
    with pytest.raises(ChecksumMismatch):
        read_bottle_manifest(bottle, expected_sha256="0" * 64)
    assert read_bottle_manifest(bottle, expected_sha256=_sha(bottle)).sha256 == _sha(
        bottle
    )


def test_pour_validates_manifest_before_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tag = candidate_bottle_tags()[0]
    archive = tmp_path / "bottles" / f"unsafe-1.0.{tag}.bottle.tar.gz"
    archive.parent.mkdir(parents=True)
    with tarfile.open(archive, "w:gz") as tar:
        _add_member(tar, "unsafe/1.0", mode=0o755, kind=tarfile.DIRTYPE)
        _add_member(tar, "unsafe/1.0/bin/tool", b"safe member")
        _add_member(tar, "../escape.txt", b"payload")

    formula = Formula(name="unsafe", version="1.0")
    formula.bottle = BottleSpec(
        root_url=archive.parent.as_uri(),
        files={tag: BottleFile(tag, _sha(archive))},
    )
    extracted: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "pybrew.installer.safe_extract_tar",
        lambda *args, **kwargs: extracted.append(args),
    )
    installer = _direct_installer(tmp_path)
    with pytest.raises(BottleError):
        installer.install(
            formula,
            InstallOptions(ignore_dependencies=True, bottle_file=archive),
        )
    assert extracted == []
    assert not installer.config.keg_path("unsafe", "1.0").exists()


@pytest.mark.parametrize("unsafe", ["../escape.txt", "/absolute.txt"])
def test_read_bottle_manifest_rejects_unsafe_members(
    tmp_path: Path, unsafe: str
) -> None:
    archive = tmp_path / "unsafe.bottle.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        _add_member(tar, "hello/1.0", mode=0o755, kind=tarfile.DIRTYPE)
        _add_member(tar, unsafe, b"payload")
    with pytest.raises(BottleError):
        read_bottle_manifest(archive)


def test_read_bottle_manifest_rejects_escaping_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "link.bottle.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        _add_member(tar, "hello/1.0", mode=0o755, kind=tarfile.DIRTYPE)
        _add_member(
            tar,
            "hello/1.0/passwd",
            mode=0o777,
            kind=tarfile.SYMTYPE,
            target="../../../etc/passwd",
        )
    with pytest.raises(BottleError):
        read_bottle_manifest(archive)


def test_install_local_bottle_records_manifest_summary(sample) -> None:
    config = Config.default(
        prefix=sample.prefix, taps=[sample.tap], cache=sample.cache
    )
    config.ensure()
    loader = FormulaLoader(discover_taps([sample.tap]))
    installer = Installer(config, loader)
    result = installer.install(
        loader.load("hello"),
        InstallOptions(ignore_dependencies=True, bottle_file=sample.bottle),
    )[-1]

    receipt = read_receipt(result.keg)
    assert receipt["poured_from_bottle"] is True
    assert receipt["runtime_dependencies"] == []
    manifest = read_bottle_manifest(sample.bottle)
    summary = receipt["bottle_manifest"]
    assert summary["sha256"] == manifest.sha256 == sample.bottle_sha
    assert summary["file_count"] == manifest.file_count
    assert summary["symlink_count"] == manifest.symlink_count
    assert summary["total_size"] == manifest.total_size


def test_write_receipt_keeps_old_shape_without_bottle_manifest(
    tmp_path: Path,
) -> None:
    keg = tmp_path / "keg"
    keg.mkdir()
    formula = Formula(name="receipt-fixture", version="1.0")
    write_receipt(keg, formula, poured_from_bottle=False)
    receipt = read_receipt(keg)
    assert "bottle_manifest" not in receipt
    assert "sbom" not in receipt
    assert "build" not in receipt
    assert receipt["source"] == {"path": None, "tap": None}
    assert receipt["version"] == "1.0"
    assert receipt["poured_from_bottle"] is False

    write_receipt(
        keg,
        formula,
        poured_from_bottle=True,
        bottle_manifest={"file_count": 1, "symlink_count": 0},
    )
    receipt = read_receipt(keg)
    assert receipt["poured_from_bottle"] is True
    assert receipt["bottle_manifest"] == {
        "file_count": 1,
        "symlink_count": 0,
    }


# -- linkage -------------------------------------------------------------
def test_check_linkage_detects_missing_elf_reference(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "lib").mkdir()
    (keg / "bin" / "tool").write_bytes(_synthetic_elf("libmissing.so"))
    (keg / "share").mkdir()
    (keg / "share" / "README").write_text("text files are ignored\n")

    report = check_linkage(keg)
    assert report.checked == 1
    assert report.skipped == 0
    assert len(report.issues) == 1
    assert report.issues[0].path == "bin/tool"
    assert report.issues[0].reference == "libmissing.so"
    assert report.issues[0].kind == "elf"
    assert "libmissing.so" in report.format()


def test_check_linkage_accepts_present_elf_reference(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "lib").mkdir()
    (keg / "bin" / "tool").write_bytes(_synthetic_elf("libpresent.so"))
    (keg / "lib" / "libpresent.so").write_bytes(b"library")

    report = check_linkage(keg)
    assert report.checked == 1
    assert report.issues == []


def test_check_linkage_handles_macho_rpath(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "lib").mkdir()
    binary = keg / "bin" / "tool"
    binary.write_bytes(
        _synthetic_macho(["@rpath/libmissing.dylib"], ["@loader_path/../lib"])
    )

    report = check_linkage(keg)
    assert report.checked == 1
    assert [issue.reference for issue in report.issues] == [
        "@rpath/libmissing.dylib"
    ]

    (keg / "lib" / "libmissing.dylib").write_bytes(b"library")
    assert check_linkage(keg).issues == []


def test_check_linkage_ignores_weak_macho_reference(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "bin" / "tool").write_bytes(
        _synthetic_macho(["@rpath/liboptional.dylib"], weak=True)
    )
    report = check_linkage(keg)
    assert report.checked == 1
    assert report.issues == []


@pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS dyld shared-cache references"
)
def test_check_linkage_accepts_macos_shared_cache_reference(
    tmp_path: Path,
) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "bin" / "tool").write_bytes(
        _synthetic_macho(["/usr/lib/libSystem.B.dylib"])
    )
    report = check_linkage(keg)
    assert report.checked == 1
    assert report.issues == []


def test_linkage_check_is_opt_in_for_bottles(tmp_path: Path) -> None:
    formula, bottle = _formula_with_local_bottle(
        tmp_path,
        "linkage-bottle",
        "1.0",
        files={"bin/tool": _synthetic_elf("libmissing.so")},
    )
    installer = _direct_installer(tmp_path)
    options = InstallOptions(
        ignore_dependencies=True, bottle_file=bottle, linkage_check=True
    )
    with pytest.raises(LinkageError, match="dangling"):
        installer.install(formula, options)
    assert not installer.config.keg_path("linkage-bottle", "1.0").exists()

    # Default behavior is unchanged: the same synthetic bottle installs.
    result = installer.install(
        formula,
        InstallOptions(ignore_dependencies=True, bottle_file=bottle),
    )[-1]
    assert result.keg.is_dir()


def test_linkage_check_env_opt_in_for_source_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYBREW_LINKAGE_CHECK", "1")
    project = _project(tmp_path, "linkage-source")
    (project / "tool").write_bytes(_synthetic_elf("libmissing.so"))
    tap = _make_tap(
        tmp_path,
        "linkage-source",
        "1.0",
        project,
        '    bin.install "tool"',
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    loader = FormulaLoader(discover_taps([tap]))
    installer = Installer(config, loader)
    with pytest.raises(LinkageError, match="dangling"):
        installer.install(
            loader.load("linkage-source"),
            InstallOptions(build_from_source=True),
        )
    assert not config.keg_path("linkage-source", "1.0").exists()


# -- dependency ordering -------------------------------------------------
def test_resolve_is_deterministic_topological_order(tmp_path: Path) -> None:
    formulas = {
        "app": _dependency_formula("app", ["beta", "alpha"]),
        "beta": _dependency_formula("beta", ["base"]),
        "alpha": _dependency_formula("alpha", ["base"]),
        "base": _dependency_formula("base", []),
    }
    installer = Installer(
        Config.default(prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"),
        _MemoryLoader(formulas),
    )
    order = [formula.name for formula in installer.resolve(formulas["app"])]
    assert order == ["base", "alpha", "beta", "app"]

    reordered = {
        "app": _dependency_formula("app", ["alpha", "beta"]),
        "beta": formulas["beta"],
        "alpha": formulas["alpha"],
        "base": formulas["base"],
    }
    installer = Installer(
        Config.default(prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"),
        _MemoryLoader(reordered),
    )
    assert [formula.name for formula in installer.resolve(reordered["app"])] == order


def test_resolve_reports_dependency_cycle(tmp_path: Path) -> None:
    formulas = {
        "a": _dependency_formula("a", ["b"]),
        "b": _dependency_formula("b", ["c"]),
        "c": _dependency_formula("c", ["a"]),
    }
    installer = Installer(
        Config.default(prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"),
        _MemoryLoader(formulas),
    )
    with pytest.raises(DependencyError, match="dependency cycle detected") as error:
        installer.resolve(formulas["a"])
    assert "a -> b -> c -> a" in str(error.value)


def test_resolve_reports_missing_dependency(tmp_path: Path) -> None:
    formula = _dependency_formula("missing-parent", ["not-here"])
    installer = Installer(
        Config.default(prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"),
        _MemoryLoader({"missing-parent": formula}),
    )
    with pytest.raises(DependencyError, match="not available"):
        installer.resolve(formula)


# -- manifest edge cases -------------------------------------------------
def test_manifest_records_hardlinks_and_missing_receipt(tmp_path: Path) -> None:
    bottle = _make_bottle(
        tmp_path / "hardlink.bottle.tar.gz",
        "hardlink",
        "1.0",
        files={"bin/tool": b"tool payload"},
        hardlinks={"bin/tool-link": "bin/tool"},
    )
    manifest = read_bottle_manifest(bottle)
    top = "hardlink/1.0"
    assert manifest.receipt is None
    assert manifest.hardlinks == {
        f"{top}/bin/tool-link": f"{top}/bin/tool"
    }
    assert manifest.hardlink_count == 1
    summary = manifest.summary()
    assert summary["hardlink_count"] == 1
    assert summary["receipt"] is None


def test_install_extracts_deferred_hardlink_without_receipt(
    tmp_path: Path,
) -> None:
    formula, bottle = _formula_with_local_bottle(
        tmp_path,
        "hardlink-install",
        "1.0",
        files={"bin/tool": b"tool payload"},
        hardlinks={"bin/tool-link": "bin/tool"},
    )
    installer = _direct_installer(tmp_path)
    result = installer.install(
        formula,
        InstallOptions(ignore_dependencies=True, bottle_file=bottle),
    )[-1]

    assert (result.keg / "bin" / "tool").read_bytes() == b"tool payload"
    assert (result.keg / "bin" / "tool-link").read_bytes() == b"tool payload"
    receipt = read_receipt(result.keg)
    assert receipt["bottle_manifest"]["receipt"] is None
    assert receipt["bottle_manifest"]["hardlink_count"] == 1
    paths = {item["path"] for item in receipt["sbom"]["files"]}
    assert {"bin/tool", "bin/tool-link"} <= paths


def test_manifest_and_install_preserve_relative_symlink_targets(
    tmp_path: Path,
) -> None:
    formula, bottle = _formula_with_local_bottle(
        tmp_path,
        "symlink-install",
        "1.0",
        files={"share/data.txt": b"data", "bin/tool": b"tool"},
        symlinks={
            "bin/data-link": "../share/data.txt",
            "lib/libfoo.dylib": "libfoo.1.dylib",
        },
    )
    manifest = read_bottle_manifest(bottle)
    top = "symlink-install/1.0"
    assert manifest.symlinks[f"{top}/bin/data-link"] == "../share/data.txt"
    assert manifest.symlinks[f"{top}/lib/libfoo.dylib"] == "libfoo.1.dylib"

    installer = _direct_installer(tmp_path)
    result = installer.install(
        formula,
        InstallOptions(ignore_dependencies=True, bottle_file=bottle),
    )[-1]
    assert os.readlink(result.keg / "bin" / "data-link") == "../share/data.txt"
    assert (result.keg / "bin" / "data-link").read_text() == "data"
    symlinks = {
        item["path"]: item["target"]
        for item in read_receipt(result.keg)["sbom"]["symlinks"]
    }
    assert symlinks["bin/data-link"] == "../share/data.txt"


# -- receipt source/build/SBOM metadata ----------------------------------
def test_receipt_has_source_build_metadata_and_sbom_for_bottle(
    sample,
) -> None:
    config = Config.default(
        prefix=sample.prefix, taps=[sample.tap], cache=sample.cache
    )
    config.ensure()
    loader = FormulaLoader(discover_taps([sample.tap]))
    formula = loader.load("hello")
    installer = Installer(config, loader)
    result = installer.install(
        formula,
        InstallOptions(ignore_dependencies=True, bottle_file=sample.bottle),
    )[-1]
    receipt = read_receipt(result.keg)

    for old_key in (
        "homebrew_version",
        "source",
        "version",
        "runtime_dependencies",
        "installed_as_dependency",
        "poured_from_bottle",
        "built_as_bottle",
        "installed_on_request",
        "bottle_manifest",
    ):
        assert old_key in receipt

    source = receipt["source"]
    assert source["path"] == str(sample.formula)
    assert source["tap"] == formula.tap
    assert source["url"] == formula.stable.url
    assert source["sha256"] == formula.stable.sha256
    assert source["spec"] == "stable"

    build = receipt["build"]
    assert build["from_bottle"] is True
    assert build["build_from_source"] is False
    assert build["bottle_tag"] == sample.tag
    assert build["spec"] == "stable"
    assert build["options"]["ignore_dependencies"] is True
    assert "built_on" in build and "os" in build["built_on"]

    sbom = receipt["sbom"]
    assert sbom["schema"] == "pybrew-keg-sbom-1"
    assert sbom["algorithm"] == "sha256"
    assert sbom["file_count"] == len(sbom["files"])
    assert sbom["symlink_count"] == len(sbom["symlinks"])
    files = {item["path"]: item for item in sbom["files"]}
    hello = files["bin/hello"]
    assert hello["size"] == (result.keg / "bin" / "hello").stat().st_size
    assert hello["sha256"] == hashlib.sha256(
        (result.keg / "bin" / "hello").read_bytes()
    ).hexdigest()
    assert "INSTALL_RECEIPT.json" not in files
    symlinks = {item["path"]: item["target"] for item in sbom["symlinks"]}
    assert symlinks["lib/libhello.1.dylib"] == "libhello.dylib"


def test_receipt_has_source_build_metadata_and_sbom_for_source(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "metadata-source")
    (project / "PAYLOAD").write_text("source payload\n", encoding="utf-8")
    tap = _make_tap(
        tmp_path,
        "metadata-source",
        "1.0",
        project,
        '    bin.install "PAYLOAD"',
    )
    config, result = _install(tmp_path, tap, "metadata-source")
    receipt = read_receipt(result.keg)

    assert receipt["poured_from_bottle"] is False
    assert receipt["source"]["spec"] == "stable"
    assert receipt["source"]["url"].startswith("file:")
    assert receipt["source"]["sha256"]
    assert receipt["build"]["from_bottle"] is False
    assert receipt["build"]["build_from_source"] is True
    assert receipt["build"]["bottle_tag"] is None
    sbom = receipt["sbom"]
    files = {item["path"]: item for item in sbom["files"]}
    payload = files["bin/PAYLOAD"]
    assert payload["sha256"] == hashlib.sha256(b"source payload\n").hexdigest()
    assert "INSTALL_RECEIPT.json" not in files


def test_write_receipt_merges_optional_sbom_and_metadata(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    keg.mkdir()
    formula = Formula(name="metadata-fixture", version="1.0")
    write_receipt(
        keg,
        formula,
        poured_from_bottle=False,
        source_metadata={
            "url": "https://example.com/source.tar.gz",
            "sha256": "a" * 64,
            "spec": "stable",
        },
        build_metadata={"from_bottle": False, "spec": "stable"},
        sbom={"schema": "test-sbom", "file_count": 1},
    )
    receipt = read_receipt(keg)
    assert receipt["source"]["path"] is None
    assert receipt["source"]["tap"] is None
    assert receipt["source"]["url"] == "https://example.com/source.tar.gz"
    assert receipt["source"]["sha256"] == "a" * 64
    assert receipt["build"]["from_bottle"] is False
    assert receipt["sbom"]["schema"] == "test-sbom"


# -- formula-level linkage diagnostics -----------------------------------
def test_post_install_refreshes_receipt_sbom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    formula, bottle = _formula_with_local_bottle(
        tmp_path,
        "post-sbom",
        "1.0",
        files={"bin/tool": b"tool"},
    )
    installer = _direct_installer(tmp_path)

    def fake_post_install(_formula, _config, keg=None) -> None:
        assert keg is not None
        (keg / "post-marker").write_text("post", encoding="utf-8")

    monkeypatch.setattr("pybrew.installer.has_post_install", lambda _formula: True)
    monkeypatch.setattr("pybrew.installer.run_post_install", fake_post_install)
    result = installer.install(
        formula,
        InstallOptions(ignore_dependencies=True, bottle_file=bottle),
    )[-1]
    receipt = read_receipt(result.keg)
    assert receipt["post_install_ran"] is True
    paths = {item["path"] for item in receipt["sbom"]["files"]}
    assert "post-marker" in paths


def test_formula_linkage_report_after_install(tmp_path: Path) -> None:
    formula, bottle = _formula_with_local_bottle(
        tmp_path,
        "linkage-report",
        "1.0",
        files={"bin/tool": _synthetic_elf("libmissing.so")},
    )
    installer = _direct_installer(tmp_path)
    installer.install(
        formula,
        InstallOptions(ignore_dependencies=True, bottle_file=bottle),
    )

    report = installer.linkage_report("linkage-report")
    assert report.checked == 1
    assert [issue.reference for issue in report.issues] == ["libmissing.so"]
    diagnostic = installer.linkage_diagnostic(formula)
    assert "dangling" in diagnostic
    assert "bin/tool" in diagnostic
    assert "libmissing.so" in diagnostic


def test_formula_linkage_report_clean_keg(tmp_path: Path) -> None:
    formula, bottle = _formula_with_local_bottle(
        tmp_path,
        "linkage-clean",
        "1.0",
        files={
            "bin/tool": _synthetic_elf("libpresent.so"),
            "lib/libpresent.so": b"library",
        },
    )
    installer = _direct_installer(tmp_path)
    installer.install(
        formula,
        InstallOptions(ignore_dependencies=True, bottle_file=bottle),
    )
    report = installer.linkage_report("linkage-clean")
    assert report.checked == 1
    assert report.issues == []
    assert installer.linkage_diagnostic("linkage-clean") == "linkage OK"


def test_formula_linkage_report_missing_install(tmp_path: Path) -> None:
    installer = _direct_installer(tmp_path)
    with pytest.raises(PybrewError, match="not installed"):
        installer.linkage_report("not-installed")


# -- uninstall / relink / upgrade cleanup --------------------------------
def test_uninstall_removes_receipt_sbom_and_links(sample) -> None:
    config = Config.default(
        prefix=sample.prefix, taps=[sample.tap], cache=sample.cache
    )
    config.ensure()
    loader = FormulaLoader(discover_taps([sample.tap]))
    installer = Installer(config, loader)
    result = installer.install(
        loader.load("hello"),
        InstallOptions(ignore_dependencies=True, bottle_file=sample.bottle),
    )[-1]
    keg = result.keg
    assert receipt_path(keg).is_file()
    assert read_receipt(keg)["sbom"]["file_count"] >= 1
    assert (config.prefix / "bin" / "hello").is_symlink()

    removed = installer.uninstall("hello")
    assert removed == [keg]
    assert not keg.exists()
    assert not receipt_path(keg).exists()
    assert not (config.cellar / "hello").exists()
    assert not os.path.lexists(config.prefix / "bin" / "hello")
    assert not os.path.lexists(config.opt_path("hello"))


def test_relink_removes_stale_links_from_previous_keg(tmp_path: Path) -> None:
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"
    )
    config.ensure()
    old_keg = config.keg_path("relink-fixture", "1.0")
    (old_keg / "bin").mkdir(parents=True)
    (old_keg / "bin" / "old-tool").write_text("old", encoding="utf-8")
    new_keg = config.keg_path("relink-fixture", "2.0")
    (new_keg / "bin").mkdir(parents=True)
    (new_keg / "bin" / "new-tool").write_text("new", encoding="utf-8")

    linker = Linker(config)
    linker.link(Formula(name="relink-fixture", version="1.0"), old_keg)
    assert (config.prefix / "bin" / "old-tool").is_symlink()

    result = linker.link(
        Formula(name="relink-fixture", version="2.0"), new_keg, overwrite=True
    )
    assert not os.path.lexists(config.prefix / "bin" / "old-tool")
    assert (config.prefix / "bin" / "new-tool").is_symlink()
    assert any(path.name == "old-tool" for path in result.removed)
    assert config.opt_path("relink-fixture").resolve() == new_keg.resolve()


def test_upgrade_removes_old_receipt_and_stale_links(tmp_path: Path) -> None:
    from pybrew.maintenance import upgrade_formula

    formula, _bottle = _formula_with_local_bottle(
        tmp_path,
        "upgrade-fixture",
        "2.0",
        files={"bin/new-tool": b"new tool\n"},
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"
    )
    config.ensure()
    old_formula = Formula(name="upgrade-fixture", version="1.0")
    old_keg = config.keg_path("upgrade-fixture", "1.0")
    (old_keg / "bin").mkdir(parents=True)
    (old_keg / "bin" / "old-tool").write_text("old", encoding="utf-8")
    write_receipt(
        old_keg,
        old_formula,
        poured_from_bottle=False,
        bottle_manifest={"file_count": 1},
        sbom={"file_count": 1},
    )
    Linker(config).link(old_formula, old_keg)
    assert (config.prefix / "bin" / "old-tool").is_symlink()

    messages = upgrade_formula(config, FormulaLoader([]), formula)
    assert messages == ["upgrade-fixture 2.0"]
    assert not old_keg.exists()
    assert not receipt_path(old_keg).exists()
    assert not os.path.lexists(config.prefix / "bin" / "old-tool")
    assert (config.prefix / "bin" / "new-tool").is_symlink()

    new_keg = config.keg_path("upgrade-fixture", "2.0")
    receipt = read_receipt(new_keg)
    assert receipt["poured_from_bottle"] is True
    assert receipt["build"]["from_bottle"] is True
    assert receipt["sbom"]["file_count"] >= 1
    assert not (old_keg / "INSTALL_RECEIPT.json").exists()


def test_remove_keg_refuses_symlinked_keg(tmp_path: Path) -> None:
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"
    )
    config.ensure()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    rack = config.rack_path("evil")
    rack.mkdir(parents=True)
    link = rack / "1.0"
    link.symlink_to(outside)

    with pytest.raises(PybrewError, match="symlinked keg"):
        Cellar(config).remove_keg(link)
    assert (outside / "keep.txt").read_text() == "keep"
