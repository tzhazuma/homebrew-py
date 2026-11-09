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
    dylib_to_dep,
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
    options: InstallOptions | None = None,
):
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    loader = FormulaLoader(discover_taps([tap]))
    formula = loader.load(name)
    installer = Installer(config, loader, environment=environment)
    results = installer.install(
        formula, options or InstallOptions(build_from_source=True)
    )
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


def _synthetic_elf(
    *needed: str,
    rpath: "str | None" = None,
    runpath: "str | None" = None,
    audit: "str | None" = None,
    depaudit: "str | None" = None,
    extended_phnum: bool = False,
    no_dynamic: bool = False,
) -> bytes:
    required = needed or ("libmissing.so",)
    strings = bytearray(b"\0")
    needed_offsets: list[int] = []
    for name in required:
        needed_offsets.append(len(strings))
        strings.extend(name.encode("utf-8") + b"\0")
    rpath_offset: "int | None" = None
    if rpath is not None:
        rpath_offset = len(strings)
        strings.extend(rpath.encode("utf-8") + b"\0")
    runpath_offset: "int | None" = None
    if runpath is not None:
        runpath_offset = len(strings)
        strings.extend(runpath.encode("utf-8") + b"\0")
    audit_offset: "int | None" = None
    if audit is not None:
        audit_offset = len(strings)
        strings.extend(audit.encode("utf-8") + b"\0")
    depaudit_offset: "int | None" = None
    if depaudit is not None:
        depaudit_offset = len(strings)
        strings.extend(depaudit.encode("utf-8") + b"\0")
    strtab_offset = 0x200
    base_address = 0x400000
    dynamic_offset = 0x100
    dynamic: list[tuple[int, int]] = []
    if not no_dynamic:
        dynamic = [(1, offset) for offset in needed_offsets]
        if rpath_offset is not None:
            dynamic.append((15, rpath_offset))
        if runpath_offset is not None:
            dynamic.append((29, runpath_offset))
        if audit_offset is not None:
            dynamic.append((0x6FFFFFFC, audit_offset))
        if depaudit_offset is not None:
            dynamic.append((0x6FFFFFFB, depaudit_offset))
        dynamic.extend(
            [
                (5, base_address + strtab_offset),
                (10, len(strings)),
                (0, 0),
            ]
        )
    dynamic_size = len(dynamic) * 16
    section_offset = 0x300 if extended_phnum else 0
    data_size = 0x340 if extended_phnum else 0x300
    data = bytearray(data_size)
    struct.pack_into("<4sBBBBB7x", data, 0, b"\x7fELF", 2, 1, 1, 0, 0)
    program_header_count = 1 if no_dynamic else 2
    struct.pack_into(
        "<HHIQQQIHHHHHH",
        data,
        16,
        3,
        62,
        1,
        0,
        64,
        section_offset,
        0,
        64,
        56,
        0xFFFF if extended_phnum else program_header_count,
        64 if extended_phnum else 0,
        1 if extended_phnum else 0,
        0,
    )
    struct.pack_into(
        "<IIQQQQQQ", data, 64, 1, 5, 0, base_address, base_address, 0x300, 0x300, 0x1000
    )
    if not no_dynamic:
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
    if extended_phnum:
        # Section header zero's sh_info carries the real PN_XNUM phnum.
        struct.pack_into(
            "<I", data, section_offset + 44, program_header_count
        )
    return bytes(data)


def _synthetic_macho(
    loads: list[str],
    rpaths: "list[str] | None" = None,
    *,
    weak: bool = False,
    command_ids: "list[int] | None" = None,
) -> bytes:
    rpaths = rpaths or []
    if command_ids is None:
        command_ids = [0x80000018 if weak else 0xC] * len(loads)
    if len(command_ids) != len(loads):
        raise ValueError("command_ids must match loads")
    commands: list[bytes] = []
    for load, command_id in zip(loads, command_ids):
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


def _synthetic_fat_macho(slices: "list[bytes]", *, fat64: bool = False) -> bytes:
    magic = 0xCAFEBABF if fat64 else 0xCAFEBABE
    arch_size = 32 if fat64 else 20
    header = bytearray(struct.pack(">II", magic, len(slices)))
    offset = 8 + len(slices) * arch_size
    rows: list[bytes] = []
    payload = bytearray()
    for slice_data in slices:
        padding = (-offset) % 8
        payload.extend(b"\0" * padding)
        offset += padding
        if fat64:
            row = struct.pack(
                ">iiQQII",
                0x0100000C,
                0,
                offset,
                len(slice_data),
                3,
                0,
            )
        else:
            row = struct.pack(
                ">iiIII",
                0x0100000C,
                0,
                offset,
                len(slice_data),
                3,
            )
        rows.append(row)
        payload.extend(slice_data)
        offset += len(slice_data)
    return bytes(header) + b"".join(rows) + bytes(payload)


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


def test_check_linkage_reports_corrupt_and_unsupported_elf(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "bin" / "corrupt").write_bytes(b"\x7fELF" + b"\0" * 20)
    unsupported = bytearray(_synthetic_elf("libmissing.so"))
    # ELF64 e_phnum is at offset 56; zero means no program headers (object file).
    struct.pack_into("<H", unsupported, 56, 0)
    (keg / "bin" / "object").write_bytes(bytes(unsupported))

    report = check_linkage(keg)

    assert report.checked == 0
    assert report.issues == []
    assert [problem.path for problem in report.corrupt] == ["bin/corrupt"]
    assert [problem.path for problem in report.unsupported] == ["bin/object"]
    assert report.skipped == 2
    assert "corrupt" in report.format()
    assert "unsupported" in report.format()
    assert report.has_errors is True


def test_check_linkage_parses_extended_elf_phnum(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "bin" / "tool").write_bytes(
        _synthetic_elf("libmissing.so", extended_phnum=True)
    )

    report = check_linkage(keg)

    assert report.checked == 1
    assert report.corrupt == []
    assert report.unsupported == []
    assert [issue.reference for issue in report.issues] == ["libmissing.so"]


def test_check_linkage_resolves_elf_origin_runpath(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "vendor" / "lib").mkdir(parents=True)
    (keg / "vendor" / "lib" / "libhidden.so").write_bytes(b"library")
    (keg / "bin" / "tool").write_bytes(
        _synthetic_elf(
            "libhidden.so",
            rpath="$ORIGIN/../vendor/lib",
            runpath="${ORIGIN}/../vendor/lib",
        )
    )

    report = check_linkage(keg)

    assert report.checked == 1
    assert report.issues == []

    (keg / "vendor" / "lib" / "libhidden.so").unlink()
    assert [
        issue.reference for issue in check_linkage(keg).issues
    ] == ["libhidden.so"]


def test_check_linkage_parses_fat_macho_slices_and_rpaths(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "lib").mkdir()
    (keg / "lib" / "libpresent.dylib").write_bytes(b"library")
    slice_one = _synthetic_macho(
        ["@rpath/libmissing.dylib"], ["@loader_path/../lib"]
    )
    slice_two = _synthetic_macho(
        ["/usr/lib/libSystem.B.dylib", "@rpath/libpresent.dylib"],
        ["@executable_path/../lib"],
    )
    (keg / "bin" / "tool").write_bytes(
        _synthetic_fat_macho([slice_one, slice_two])
    )

    report = check_linkage(keg)

    assert report.checked == 1
    assert report.corrupt == []
    assert [issue.reference for issue in report.issues] == [
        "@rpath/libmissing.dylib"
    ]


def test_check_linkage_parses_fat64_macho(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "bin" / "tool").write_bytes(
        _synthetic_fat_macho(
            [_synthetic_macho(["@rpath/libmissing.dylib"])], fat64=True
        )
    )

    report = check_linkage(keg)

    assert report.checked == 1
    assert report.corrupt == []
    assert [issue.reference for issue in report.issues] == [
        "@rpath/libmissing.dylib"
    ]


def test_check_linkage_handles_reexport_upward_and_weak_macho(
    tmp_path: Path,
) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "bin" / "tool").write_bytes(
        _synthetic_macho(
            [
                "@rpath/libreexport.dylib",
                "@rpath/libupward.dylib",
                "@rpath/libweak.dylib",
            ],
            command_ids=[0x8000001F, 0x80000023, 0x80000018],
        )
    )

    report = check_linkage(keg)

    assert report.checked == 1
    assert [issue.reference for issue in report.issues] == [
        "@rpath/libreexport.dylib",
        "@rpath/libupward.dylib",
    ]


def test_check_linkage_reports_corrupt_fat_macho(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    bad = struct.pack(">II", 0xCAFEBABE, 1) + struct.pack(
        ">iiIII", 0x0100000C, 0, 0x1000, 0x20, 3
    )
    (keg / "bin" / "fat").write_bytes(bad)

    report = check_linkage(keg)

    assert report.checked == 0
    assert report.issues == []
    assert len(report.corrupt) == 1
    assert report.corrupt[0].path == "bin/fat"


def test_linkage_report_all_formulae_scans_installed_kegs(
    tmp_path: Path,
) -> None:
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"
    )
    config.ensure()
    broken = config.keg_path("alpha", "1.0")
    (broken / "bin").mkdir(parents=True)
    (broken / "bin" / "tool").write_bytes(_synthetic_elf("libmissing.so"))
    clean = config.keg_path("beta", "2.0")
    (clean / "bin").mkdir(parents=True)
    (clean / "lib").mkdir()
    (clean / "bin" / "tool").write_bytes(_synthetic_elf("libpresent.so"))
    (clean / "lib" / "libpresent.so").write_bytes(b"library")
    installer = Installer(config, FormulaLoader([]))

    report = installer.linkage_report(all_formulae=True)

    assert report.checked == 2
    assert [issue.reference for issue in report.issues] == [
        "libmissing.so"
    ]
    assert report.issues[0].keg == str(broken)
    assert installer.linkage_report("beta", all_formulae=True).issues == []
    assert "libmissing.so" in installer.linkage_diagnostic(all_formulae=True)

    with pytest.raises(PybrewError, match="not installed"):
        installer.linkage_report("missing", all_formulae=True)
    with pytest.raises(PybrewError, match="formula is required"):
        installer.linkage_report()


def test_fat_macho_keeps_valid_slice_when_another_is_corrupt(
    tmp_path: Path,
) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    good = _synthetic_macho(["@rpath/libmissing.dylib"])
    (keg / "bin" / "tool").write_bytes(
        _synthetic_fat_macho([good, b"JUNK" * 4])
    )

    report = check_linkage(keg)

    assert report.checked == 1
    assert [issue.reference for issue in report.issues] == [
        "@rpath/libmissing.dylib"
    ]
    assert len(report.corrupt) == 1
    assert "slice 1" in report.corrupt[0].detail
    assert report.skipped == 1


def test_check_linkage_expands_elf_lib_and_platform_tokens(
    tmp_path: Path,
) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "lib" / "x86_64").mkdir(parents=True)
    (keg / "lib" / "x86_64" / "libtoken.so").write_bytes(b"library")
    (keg / "bin" / "tool").write_bytes(
        _synthetic_elf(
            "libtoken.so",
            rpath="${ORIGIN}/../${LIB}/${PLATFORM}",
        )
    )
    (keg / "bin" / "tool-bare").write_bytes(
        _synthetic_elf(
            "libtoken.so",
            rpath="$ORIGIN/../$LIB/$PLATFORM",
        )
    )

    report = check_linkage(keg)

    assert report.checked == 2
    assert report.issues == []
    assert report.audit == []


def test_check_linkage_runpath_takes_precedence_over_rpath(
    tmp_path: Path,
) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "rpath-lib").mkdir()
    (keg / "runpath-lib").mkdir()
    (keg / "rpath-lib" / "libpref.so").write_bytes(b"library")
    (keg / "bin" / "tool").write_bytes(
        _synthetic_elf(
            "libpref.so",
            rpath="$ORIGIN/../rpath-lib",
            runpath="$ORIGIN/../runpath-lib",
        )
    )

    report = check_linkage(keg)
    assert [issue.reference for issue in report.issues] == ["libpref.so"]

    (keg / "runpath-lib" / "libpref.so").write_bytes(b"library")
    assert check_linkage(keg).issues == []


def test_check_linkage_static_elf_without_pt_dynamic(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "bin" / "static-tool").write_bytes(
        _synthetic_elf("libignored.so", no_dynamic=True)
    )

    report = check_linkage(keg)

    assert report.checked == 1
    assert report.issues == []
    assert report.audit == []
    assert report.corrupt == []
    assert report.unsupported == []


def test_check_linkage_diagnoses_audit_modules_without_false_positive(
    tmp_path: Path,
) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "lib").mkdir()
    (keg / "lib" / "libpresent.so").write_bytes(b"library")
    (keg / "lib" / "libaudit.so").write_bytes(b"audit")
    (keg / "bin" / "tool").write_bytes(
        _synthetic_elf(
            "libpresent.so",
            audit="$ORIGIN/../lib/libaudit.so",
            depaudit="/nonexistent/libdepaudit.so",
        )
    )

    report = check_linkage(keg)

    assert report.checked == 1
    assert report.issues == []
    assert report.has_errors is False
    assert [audit.tag for audit in report.audit] == [
        "DT_AUDIT",
        "DT_DEPAUDIT",
    ]
    assert [audit.resolved for audit in report.audit] == [True, False]
    assert "audit module" in report.format()


def test_check_linkage_uses_bounded_dyld_environment_paths(
    tmp_path: Path,
) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "bin" / "tool").write_bytes(
        _synthetic_macho(
            [
                "libenv.dylib",
                "@rpath/libfallback.dylib",
                "@rpath/Foo.framework/Foo",
            ]
        )
    )
    library_dir = tmp_path / "library-dir"
    fallback_dir = tmp_path / "fallback-dir"
    framework_dir = tmp_path / "framework-dir"
    library_dir.mkdir()
    fallback_dir.mkdir()
    framework_dir.mkdir()
    (library_dir / "libenv.dylib").write_bytes(b"library")
    (fallback_dir / "libfallback.dylib").write_bytes(b"library")
    (framework_dir / "Foo.framework").mkdir()
    (framework_dir / "Foo.framework" / "Foo").write_bytes(b"framework")
    environment = {
        "DYLD_LIBRARY_PATH": str(library_dir),
        "DYLD_FALLBACK_LIBRARY_PATH": str(fallback_dir),
        "DYLD_FRAMEWORK_PATH": str(framework_dir),
    }

    assert check_linkage(keg).issues != []
    report = check_linkage(keg, environment=environment)

    assert report.checked == 1
    assert report.issues == []


def test_check_linkage_macho_rpath_stack_uses_later_entries(
    tmp_path: Path,
) -> None:
    keg = tmp_path / "keg"
    (keg / "bin").mkdir(parents=True)
    (keg / "lib2").mkdir()
    (keg / "lib2" / "libstack.dylib").write_bytes(b"library")
    (keg / "bin" / "tool").write_bytes(
        _synthetic_macho(
            ["@rpath/libstack.dylib"],
            ["/nonexistent-rpath-entry", "@loader_path/../lib2"],
        )
    )

    report = check_linkage(keg)

    assert report.checked == 1
    assert report.issues == []


def test_write_receipt_tab_compatible_fields(tmp_path: Path) -> None:
    keg = tmp_path / "Cellar" / "tab-fixture" / "1.0"
    keg.mkdir(parents=True)
    source = tmp_path / "tab-fixture.rb"
    source.write_text("class TabFixture < Formula; end\n", encoding="utf-8")
    formula = Formula(name="tab-fixture", version="1.0")
    formula.path = source
    write_receipt(
        keg,
        formula,
        poured_from_bottle=False,
        installed_time=1234,
        source_modified_time=5678,
        compiler="clang",
        arch="arm64",
        changed_files=["bin/tool"],
    )
    receipt = read_receipt(keg)
    assert receipt["time"] == 1234
    assert receipt["source_modified_time"] == 5678
    assert receipt["compiler"] == "clang"
    assert receipt["arch"] == "arm64"
    assert receipt["changed_files"] == ["bin/tool"]


def test_dylib_to_dep_attributes_opt_and_cellar_paths(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    assert (
        dylib_to_dep(str(prefix / "opt" / "openssl@3" / "lib" / "libssl.dylib"), prefix)
        == "openssl@3"
    )
    assert (
        dylib_to_dep(str(prefix / "Cellar" / "libslirp" / "4.9.6" / "lib" / "libslirp.so"), prefix)
        == "libslirp"
    )
    assert dylib_to_dep("/usr/lib/libSystem.B.dylib", prefix) is None
    assert dylib_to_dep(str(prefix / "opt" / "../escape" / "lib" / "x.dylib"), prefix) is None


def _write_dependency_fixtures(tmp_path: Path) -> tuple[Path, Path]:
    """A target formula with recommended/optional/tagged dependencies."""

    target_project = _project(tmp_path, "opt-target")
    (target_project / "target.txt").write_text("target\n", encoding="utf-8")
    rec_project = _project(tmp_path, "rec")
    (rec_project / "rec.txt").write_text("rec\n", encoding="utf-8")
    opt_project = _project(tmp_path, "opt")
    (opt_project / "opt.txt").write_text("opt\n", encoding="utf-8")
    tag_project = _project(tmp_path, "tagdep")
    (tag_project / "extra.txt").write_text("extra\n", encoding="utf-8")
    (tag_project / "plain.txt").write_text("plain\n", encoding="utf-8")

    _make_tap(
        tmp_path, "rec", "2.0", rec_project, 'bin.install "rec.txt"'
    )
    _make_tap(
        tmp_path, "opt", "2.0", opt_project, 'bin.install "opt.txt"'
    )
    tap = _make_tap(
        tmp_path,
        "tagdep",
        "2.0",
        tag_project,
        "\n".join(
            [
                '    if build.with?("extra")',
                '      bin.install "extra.txt"',
                "    end",
                '    bin.install "plain.txt"',
            ]
        ),
    )
    tap = _make_tap(
        tmp_path, "opt-target", "1.0", target_project, 'bin.install "target.txt"'
    )
    tag_path = tap / "Formula" / "tagdep.rb"
    tag_text = tag_path.read_text(encoding="utf-8")
    tag_text = tag_text.replace(
        "  def install",
        '  option "with-extra", "tagged dependency option"\n\n  def install',
    )
    tag_path.write_text(tag_text, encoding="utf-8")

    target_path = tap / "Formula" / "opt-target.rb"
    target_text = target_path.read_text(encoding="utf-8")
    target_text = target_text.replace(
        "  def install",
        '  depends_on "rec" => :recommended\n'
        '  depends_on "opt" => :optional\n'
        '  depends_on "tagdep" => "with-extra"\n'
        "\n  def install",
    )
    target_path.write_text(target_text, encoding="utf-8")
    return tap, target_path


def test_optional_and_recommended_dependency_selection(
    tmp_path: Path,
) -> None:
    tap, _ = _write_dependency_fixtures(tmp_path)
    config, result = _install(tmp_path / "default", tap, "opt-target")
    assert config.keg_path("rec", "2.0").is_dir()
    assert not config.keg_path("opt", "2.0").exists()
    assert (config.keg_path("tagdep", "2.0") / "bin" / "extra.txt").is_file()
    assert result.dependencies == ["rec", "tagdep"]

    without_config, _ = _install(
        tmp_path / "without-rec",
        tap,
        "opt-target",
        options=InstallOptions(
            build_from_source=True, build_options=("--without-rec",)
        ),
    )
    assert not without_config.keg_path("rec", "2.0").exists()
    assert not without_config.keg_path("opt", "2.0").exists()

    with_config, with_result = _install(
        tmp_path / "with-opt",
        tap,
        "opt-target",
        options=InstallOptions(
            build_from_source=True, build_options=("--with-opt",)
        ),
    )
    assert with_config.keg_path("opt", "2.0").is_dir()
    assert with_config.keg_path("rec", "2.0").is_dir()
    receipt = read_receipt(with_result.keg)
    assert receipt["build"]["used_options"] == ["--with-opt"]
    assert receipt["runtime_dependencies"] == ["rec", "opt", "tagdep"]

    env_config, _ = _install(
        tmp_path / "env-without-rec",
        tap,
        "opt-target",
        environment={"PYBREW_BUILD_OPTIONS": "--without-rec"},
    )
    assert not env_config.keg_path("rec", "2.0").exists()


def test_dependency_bottles_are_prefetched_concurrently(
    sample, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    config = Config.default(
        prefix=sample.prefix, taps=[sample.tap], cache=sample.cache
    )
    loader = FormulaLoader(discover_taps([sample.tap]))
    installer = Installer(config, loader)
    active = 0
    peak = 0
    lock = threading.Lock()
    original = installer.downloader.fetch

    def slow_fetch(url, sha256, hint="download", **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.1)
            return original(url, sha256, hint=hint, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setenv("PYBREW_DOWNLOAD_CONCURRENCY", "4")
    monkeypatch.setattr(installer.downloader, "fetch", slow_fetch)
    installer.install(loader.load("hello-dep"), InstallOptions())
    assert peak >= 2
