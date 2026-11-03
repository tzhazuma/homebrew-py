"""Bottle/source installation, dependency resolution and uninstallation."""

from __future__ import annotations

import heapq
import json
import os
import platform
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional

from .cellar import Cellar, Linker, read_receipt, receipt_path, write_receipt
from .download import Downloader, sha256_file
from .errors import (
    BottleError,
    ChecksumMismatch,
    DependencyError,
    PybrewError,
    UnsupportedInstall,
)
from .models import BottleFile, Formula
from .paths import Config
from .post_install import has_post_install, run_post_install
from .source_builder import build_source
from .taps import FormulaLoader

PLACEHOLDERS = {
    b"@@HOMEBREW_PREFIX@@": lambda config: str(config.prefix).encode(),
    b"@@HOMEBREW_CELLAR@@": lambda config: str(config.cellar).encode(),
    b"@@HOMEBREW_REPOSITORY@@": lambda config: str(config.prefix).encode(),
    b"@@HOMEBREW_LIBRARY_PREFIX@@": lambda config: str(config.prefix / "Library").encode(),
}

ELF_MAGIC = b"\x7fELF"


@dataclass
class InstallOptions:
    force: bool = False
    ignore_dependencies: bool = False
    build_from_source: bool = False
    bottle_file: Optional[Path] = None
    overwrite: bool = False
    verbose: bool = False
    dry_run: bool = False
    link: bool = True
    head: bool = False
    # Opt-in: scan keg binaries for dangling dylib/ELF references.  Kept off
    # by default so normal installs (and synthetic bottles) are unaffected.
    linkage_check: bool = False


@dataclass
class InstallResult:
    name: str
    version: str
    keg: Path
    from_bottle: bool
    linked: bool
    skipped: bool = False
    dependencies: list[str] = field(default_factory=list)


@dataclass
class RelocationReport:
    text_files: int = 0
    binary_files: int = 0
    skipped_binaries: int = 0


def _record_post_install(keg: Path, sbom: Optional[Mapping[str, Any]] = None) -> None:
    """Mark the install receipt after a successful post_install run."""

    receipt = read_receipt(keg)
    receipt["post_install_ran"] = True
    if sbom is not None:
        # post_install may add files, so refresh the SBOM after it runs.
        receipt["sbom"] = dict(sbom)
    path = receipt_path(keg)
    path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def safe_extract_tar(archive: Path, destination: Path) -> None:
    """Extract a bottle/source tarball without allowing path escapes."""

    destination.mkdir(parents=True, exist_ok=True)
    pending_hardlinks: list[tuple[str, str, int]] = []
    try:
        tar = tarfile.open(archive)
    except tarfile.TarError as error:
        raise BottleError(f"cannot read archive {archive}: {error}") from error
    with tar:
        for member in tar.getmembers():
            name = member.name
            parts = Path(name).parts
            if not name or name.startswith("/") or ".." in parts:
                raise BottleError(f"refusing archive member with unsafe path: {name!r}")
            target = destination / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                linkname = member.linkname
                if not linkname or linkname.startswith("/"):
                    raise BottleError(f"refusing absolute symlink in archive: {name!r}")
                resolved = (target.parent / linkname).resolve()
                if destination.resolve() not in (resolved, *resolved.parents):
                    raise BottleError(f"symlink escapes archive root: {name!r} -> {linkname!r}")
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(linkname)
                continue
            if member.isfile():
                source = tar.extractfile(member)
                if source is None:
                    continue
                with source, open(target, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                os.chmod(target, member.mode & 0o777)
                continue
            if member.islnk():
                linkname = member.linkname
                link_parts = Path(linkname).parts
                if not linkname or linkname.startswith("/") or ".." in link_parts:
                    raise BottleError(
                        f"refusing unsafe hard link target in archive: {name!r} -> {linkname!r}"
                    )
                # The target may appear later in the tarball; resolve all
                # hard links after regular files have been extracted.
                pending_hardlinks.append((name, linkname, member.mode & 0o777))
                continue
            # Devices/FIFOs are not valid in bottles.
            raise BottleError(f"unsupported archive member type for {name!r}")

    remaining = pending_hardlinks
    while remaining:
        deferred: list[tuple[str, str, int]] = []
        for name, linkname, mode in remaining:
            source_path = destination / linkname
            target = destination / name
            if not source_path.is_file():
                deferred.append((name, linkname, mode))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            shutil.copy2(source_path, target)
            os.chmod(target, mode)
        if len(deferred) == len(remaining):
            name, linkname, _mode = deferred[0]
            raise BottleError(
                f"hard link target not found in archive: {name!r} -> {linkname!r}"
            )
        remaining = deferred


# ---------------------------------------------------------------------------
# bottle manifests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BottleManifestEntry:
    """One member of a bottle tarball."""

    path: str
    kind: str
    size: int = 0
    target: Optional[str] = None


@dataclass(frozen=True)
class BottleManifest:
    """Read-only description of a local bottle tarball.

    ``files``/``symlinks``/``directories`` are derived views; ``receipt`` is
    the archive-relative path of a bundled ``INSTALL_RECEIPT.json`` when the
    bottle carries one (Homebrew bottles usually do not; pybrew writes it after
    pouring).
    """

    archive: Path
    sha256: str
    archive_size: int
    entries: tuple[BottleManifestEntry, ...]
    receipt: Optional[str] = None

    @property
    def files(self) -> dict[str, int]:
        return {
            entry.path: entry.size for entry in self.entries if entry.kind == "file"
        }

    @property
    def symlinks(self) -> dict[str, str]:
        return {
            entry.path: entry.target or ""
            for entry in self.entries
            if entry.kind == "symlink"
        }

    @property
    def directories(self) -> tuple[str, ...]:
        return tuple(
            entry.path for entry in self.entries if entry.kind == "directory"
        )

    @property
    def hardlinks(self) -> dict[str, str]:
        return {
            entry.path: entry.target or ""
            for entry in self.entries
            if entry.kind == "hardlink"
        }

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def symlink_count(self) -> int:
        return len(self.symlinks)

    @property
    def directory_count(self) -> int:
        return len(self.directories)

    @property
    def hardlink_count(self) -> int:
        return len(self.hardlinks)

    @property
    def total_size(self) -> int:
        return sum(self.files.values())

    def summary(self) -> dict[str, Any]:
        """Compact, JSON-serialisable summary recorded in install receipts."""

        return {
            "sha256": self.sha256,
            "archive_size": self.archive_size,
            "file_count": self.file_count,
            "symlink_count": self.symlink_count,
            "directory_count": self.directory_count,
            "hardlink_count": self.hardlink_count,
            "total_size": self.total_size,
            "receipt": self.receipt,
        }


def _validated_member_path(name: str) -> str:
    if not name:
        raise BottleError("refusing archive member with an empty path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise BottleError(f"refusing archive member with unsafe path: {name!r}")
    normalised = str(path)
    return "" if normalised == "." else normalised


def _validated_symlink_target(name: str, linkname: str) -> str:
    if not linkname or PurePosixPath(linkname).is_absolute():
        raise BottleError(f"refusing absolute symlink in archive: {name!r}")
    parent = PurePosixPath(name).parent
    resolved = posixpath.normpath(str(parent / linkname))
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        raise BottleError(f"symlink escapes archive root: {name!r} -> {linkname!r}")
    return linkname


def read_bottle_manifest(
    archive: Path,
    expected_sha256: Optional[str] = None,
    *,
    checksum_verified: bool = False,
) -> BottleManifest:
    """List the members of a local bottle tarball and validate it.

    This is the stable verification helper: it never extracts anything.  When
    ``expected_sha256`` is supplied the archive digest is checked (unless the
    caller already verified it and passes ``checksum_verified=True``).  Path
    escapes, absolute symlinks and unsupported member types raise
    :class:`BottleError` before any extraction happens.
    """

    archive = Path(archive)
    if not archive.is_file():
        raise BottleError(f"bottle file not found: {archive}")
    expected = expected_sha256.strip().lower() if expected_sha256 else None
    if expected is not None and checksum_verified:
        digest = expected
    else:
        try:
            actual = sha256_file(archive)
        except OSError as error:
            raise BottleError(f"cannot read bottle archive {archive}: {error}") from error
        if expected is not None and actual != expected:
            raise ChecksumMismatch(
                f"bottle file SHA-256 mismatch for {archive}:\n"
                f"  expected {expected}\n  actual   {actual}"
            )
        digest = actual
    try:
        archive_size = archive.stat().st_size
    except OSError as error:
        raise BottleError(f"cannot stat bottle file {archive}: {error}") from error

    entries: list[BottleManifestEntry] = []
    receipt: Optional[str] = None
    try:
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                name = _validated_member_path(member.name)
                if not name:
                    continue
                if member.isdir():
                    entries.append(BottleManifestEntry(name, "directory"))
                elif member.isfile():
                    entries.append(BottleManifestEntry(name, "file", member.size))
                    if (
                        PurePosixPath(name).name == "INSTALL_RECEIPT.json"
                        and receipt is None
                    ):
                        receipt = name
                elif member.issym():
                    target = _validated_symlink_target(name, member.linkname)
                    entries.append(BottleManifestEntry(name, "symlink", 0, target))
                elif member.islnk():
                    target = _validated_member_path(member.linkname)
                    entries.append(BottleManifestEntry(name, "hardlink", 0, target))
                else:
                    raise BottleError(
                        f"unsupported archive member type for {name!r}"
                    )
    except (OSError, tarfile.TarError) as error:
        raise BottleError(f"cannot read bottle archive {archive}: {error}") from error
    return BottleManifest(
        archive=archive,
        sha256=digest,
        archive_size=archive_size,
        entries=tuple(entries),
        receipt=receipt,
    )


# ---------------------------------------------------------------------------
# receipt SBOM / source metadata
# ---------------------------------------------------------------------------

SBOM_SCHEMA = "pybrew-keg-sbom-1"


def build_keg_sbom(
    keg: Path,
    *,
    exclude: Iterable[str] = ("INSTALL_RECEIPT.json",),
) -> dict[str, Any]:
    """Build a compact, deterministic SBOM for the files in a keg.

    The receipt itself is excluded by default: it is written after this helper
    runs and would otherwise make its own checksum self-referential.  Symlinks
    are recorded with their target instead of hashing the target.
    """

    keg = Path(keg)
    excluded = set(exclude)
    files: list[dict[str, Any]] = []
    symlinks: list[dict[str, str]] = []
    if keg.is_dir():
        for current_root, dirnames, filenames in os.walk(keg, followlinks=False):
            dirnames.sort()
            current_path = Path(current_root)
            for dirname in list(dirnames):
                candidate = current_path / dirname
                if not candidate.is_symlink():
                    continue
                relative = candidate.relative_to(keg).as_posix()
                if relative in excluded:
                    dirnames.remove(dirname)
                    continue
                symlinks.append(
                    {"path": relative, "target": os.readlink(candidate)}
                )
                dirnames.remove(dirname)
            for filename in sorted(filenames):
                candidate = current_path / filename
                relative = candidate.relative_to(keg).as_posix()
                if relative in excluded:
                    continue
                if candidate.is_symlink():
                    symlinks.append(
                        {"path": relative, "target": os.readlink(candidate)}
                    )
                    continue
                if not candidate.is_file():
                    continue
                try:
                    size = candidate.stat().st_size
                    digest = sha256_file(candidate)
                except OSError:
                    continue
                files.append({"path": relative, "size": size, "sha256": digest})
    files.sort(key=lambda item: item["path"])
    symlinks.sort(key=lambda item: item["path"])
    return {
        "schema": SBOM_SCHEMA,
        "algorithm": "sha256",
        "file_count": len(files),
        "symlink_count": len(symlinks),
        "total_size": sum(int(item["size"]) for item in files),
        "files": files,
        "symlinks": symlinks,
    }


def _source_metadata(formula: Formula, options: InstallOptions) -> dict[str, Any]:
    """Source fields added to the existing receipt ``source`` object."""

    spec = formula.head if options.head else formula.stable
    return {
        "spec": "head" if options.head else "stable",
        "url": spec.url,
        "sha256": spec.sha256,
        "tag": spec.tag,
        "branch": spec.branch,
        "revision": spec.revision,
        "using": spec.using,
        "mirrors": list(spec.mirrors),
        "version": formula.version,
        "pkg_version": formula.pkg_version,
    }


def _build_metadata(
    options: InstallOptions,
    from_bottle: bool,
    bottle_tag: Optional[str],
    environment: Optional[Mapping[str, str]],
) -> dict[str, Any]:
    """Compact build information for the install receipt."""

    env = os.environ if environment is None else environment
    compiler_keys = (
        "PYBREW_COMPILER",
        "PYBREW_CC",
        "PYBREW_CXX",
        "CC",
        "CXX",
        "CFLAGS",
        "CXXFLAGS",
        "LDFLAGS",
    )
    return {
        "from_bottle": from_bottle,
        "build_from_source": not from_bottle,
        "spec": "head" if options.head else "stable",
        "bottle_tag": bottle_tag,
        "options": {
            "force": options.force,
            "ignore_dependencies": options.ignore_dependencies,
            "build_from_source": options.build_from_source,
            "head": options.head,
            "overwrite": options.overwrite,
            "link": options.link,
        },
        "environment": {
            key: env[key] for key in compiler_keys if env.get(key)
        },
        "built_on": {
            "os": platform.system(),
            "os_version": platform.release(),
            "arch": platform.machine(),
        },
    }


def find_keg_root(extract_root: Path, formula: Formula) -> Path:
    expected = extract_root / formula.name / formula.pkg_version
    if expected.is_dir():
        return expected
    rack = extract_root / formula.name
    if rack.is_dir():
        children = [child for child in rack.iterdir() if child.is_dir()]
        if len(children) == 1:
            return children[0]
        if children:
            return sorted(children)[-1]
    children = [child for child in extract_root.iterdir() if child.is_dir()]
    if len(children) == 1:
        nested = children[0] / formula.pkg_version
        if nested.is_dir():
            return nested
        return children[0]
    raise BottleError(f"cannot locate {formula.name}/{formula.pkg_version} inside archive")


def _replace_binary_placeholder(
    data: bytes, placeholder: bytes, replacement: bytes
) -> Optional[bytes]:
    """Replace a placeholder inside NUL-terminated Mach-O strings safely.

    Mach-O load commands store fixed-length strings.  Replacing the placeholder
    in the middle of a string would insert NUL padding before the path suffix;
    instead shrink the whole string and pad its end.  If the replacement is
    longer than the placeholder, return ``None`` and let the caller skip the
    binary rather than corrupt it.
    """

    if len(replacement) > len(placeholder):
        return None
    result = data
    search_from = 0
    while True:
        position = result.find(placeholder, search_from)
        if position < 0:
            return result
        start = result.rfind(b"\0", 0, position) + 1
        end = result.find(b"\0", position)
        if end < 0:
            return None
        segment = result[start:end]
        if placeholder not in segment:
            return None
        new_segment = segment.replace(placeholder, replacement)
        padding = len(segment) - len(new_segment)
        if padding < 0:
            return None
        result = result[:start] + new_segment + b"\0" * padding + result[end:]
        search_from = start + len(new_segment)


def _relocate_macho(path: Path, replacements: dict[bytes, bytes]) -> bool:
    """Use install_name_tool to relocate Mach-O load commands.

    Returns True when at least one load command changed.  The caller falls back
    to byte-level placeholder replacement when no changes were necessary or the
    tools are unavailable.
    """

    if shutil.which("otool") is None or shutil.which("install_name_tool") is None:
        return False
    try:
        listing = subprocess.run(
            ["otool", "-L", str(path)], text=True, capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if listing.returncode != 0:
        return False
    old_paths: list[str] = []
    for line in listing.stdout.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        old = stripped.partition(" (compatibility version")[0].strip()
        if any(placeholder.decode(errors="ignore") in old for placeholder in replacements):
            old_paths.append(old)

    changed = False
    mode = path.stat().st_mode
    os.chmod(path, mode | 0o200)
    try:
        for old in old_paths:
            new = old
            for placeholder, replacement in replacements.items():
                new = new.replace(placeholder.decode(errors="ignore"), replacement.decode(errors="ignore"))
            if new == old:
                continue
            result = subprocess.run(
                ["install_name_tool", "-change", old, new, str(path)],
                text=True,
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                return False
            changed = True
        id_result = subprocess.run(
            ["otool", "-D", str(path)], text=True, capture_output=True, timeout=30
        )
        if id_result.returncode == 0:
            id_lines = [line.strip() for line in id_result.stdout.splitlines()[1:] if line.strip()]
            for old in id_lines:
                new = old
                for placeholder, replacement in replacements.items():
                    new = new.replace(placeholder.decode(errors="ignore"), replacement.decode(errors="ignore"))
                if new != old:
                    result = subprocess.run(
                        ["install_name_tool", "-id", new, str(path)],
                        text=True,
                        capture_output=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        return False
                    changed = True
    finally:
        os.chmod(path, mode)
    if changed and shutil.which("codesign"):
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(path)],
            text=True,
            capture_output=True,
            timeout=30,
        )
    return changed


def _find_patchelf() -> Optional[str]:
    """Locate patchelf, honouring ``PYBREW_PATCHELF``.

    The environment variable wins over ``PATH`` so tests (and users with a
    private build) can pin an explicit executable.  An empty value disables
    patchelf and keeps the byte-level fallback.
    """

    override = os.environ.get("PYBREW_PATCHELF")
    if override is not None:
        return override or None
    return shutil.which("patchelf")


def _patchelf_output(
    patchelf: str, flag: str, path: Path, timeout: int = 30
) -> Optional[str]:
    """Return the stripped stdout of a read-only patchelf query, or None."""

    try:
        result = subprocess.run(
            [patchelf, flag, str(path)],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _relocate_elf(path: Path, replacements: dict[bytes, bytes]) -> bool:
    """Rewrite placeholder RPATH and interpreter paths in an ELF binary.

    ELF stores both the dynamic RPATH and the program interpreter (PT_INTERP)
    as strings in the file.  Homebrew's Linux bottles point the interpreter at
    ``@@HOMEBREW_PREFIX@@/lib/ld.so``; leaving that placeholder behind makes
    every executable in the bottle unrunnable ("required file not found").
    A longer replacement cannot be written in place, so the length-preserving
    byte fallback has to skip it; patchelf moves the strings and updates the
    dynamic/program headers instead.

    Returns True when patchelf rewrote the file.  A missing tool, a failed
    invocation or an ELF without placeholders returns False so the caller
    keeps the safe byte-level fallback (which never grows a string in place).
    """

    patchelf = _find_patchelf()
    if patchelf is None:
        return False

    substitutions = {
        placeholder.decode(errors="surrogateescape"): replacement.decode(
            errors="surrogateescape"
        )
        for placeholder, replacement in replacements.items()
    }

    def substitute(value: str) -> str:
        for placeholder, replacement in substitutions.items():
            value = value.replace(placeholder, replacement)
        return value

    old_rpath = _patchelf_output(patchelf, "--print-rpath", path)
    new_rpath = substitute(old_rpath) if old_rpath else old_rpath
    # Shared libraries have no PT_INTERP, so a failed query means "nothing to
    # relocate" rather than an error.
    old_interpreter = _patchelf_output(patchelf, "--print-interpreter", path)
    new_interpreter = substitute(old_interpreter) if old_interpreter else old_interpreter

    rpath_changed = old_rpath is not None and new_rpath != old_rpath
    interpreter_changed = (
        old_interpreter is not None and new_interpreter != old_interpreter
    )
    if not rpath_changed and not interpreter_changed:
        return False

    commands: list[list[str]] = []
    if interpreter_changed:
        assert new_interpreter is not None
        commands.append(["--set-interpreter", new_interpreter, str(path)])
    if rpath_changed:
        assert new_rpath is not None
        commands.append(["--set-rpath", new_rpath, str(path)])

    original = path.read_bytes()
    mode = path.stat().st_mode
    os.chmod(path, mode | 0o200)
    try:
        for command in commands:
            try:
                result = subprocess.run(
                    [patchelf, *command],
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired):
                result = None
            if result is not None and result.returncode == 0:
                continue
            # patchelf may fail after partially touching the file: restore it
            # so the caller's fallback still starts from pristine bytes.
            try:
                if path.read_bytes() != original:
                    path.write_bytes(original)
            except OSError:
                pass
            return False
        return True
    finally:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def relocate_tree(keg: Path, config: Config) -> RelocationReport:
    """Replace Homebrew's bottle placeholders in a freshly unpacked keg."""

    report = RelocationReport()
    replacements = {
        placeholder: factory(config) for placeholder, factory in PLACEHOLDERS.items()
    }
    for root, _dirs, files in os.walk(keg, followlinks=False):
        root_path = Path(root)
        for name in files:
            path = root_path / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if not any(placeholder in data for placeholder in replacements):
                continue
            is_binary = b"\0" in data[:4096]
            if is_binary and sys.platform == "darwin":
                try:
                    if _relocate_macho(path, replacements):
                        report.binary_files += 1
                        continue
                except OSError:
                    pass
            if data.startswith(ELF_MAGIC):
                try:
                    if _relocate_elf(path, replacements):
                        report.binary_files += 1
                        continue
                except OSError:
                    pass
            new_data = data
            if is_binary:
                for placeholder, replacement in replacements.items():
                    if placeholder not in new_data:
                        continue
                    replaced = _replace_binary_placeholder(new_data, placeholder, replacement)
                    if replaced is None:
                        report.skipped_binaries += 1
                        new_data = None
                        break
                    new_data = replaced
                if new_data is None:
                    continue
            else:
                for placeholder, replacement in replacements.items():
                    new_data = new_data.replace(placeholder, replacement)
            if new_data != data:
                original_mode = path.stat().st_mode
                import stat as stat_module

                os.chmod(path, original_mode | stat_module.S_IWUSR)
                try:
                    path.write_bytes(new_data)
                finally:
                    os.chmod(path, original_mode)
                if is_binary:
                    report.binary_files += 1
                else:
                    report.text_files += 1
    return report


# ---------------------------------------------------------------------------
# opt-in linkage sanity checks
# ---------------------------------------------------------------------------

_MACHO_MAGICS: dict[bytes, tuple[str, bool]] = {
    b"\xcf\xfa\xed\xfe": ("<", True),
    b"\xce\xfa\xed\xfe": ("<", False),
    b"\xfe\xed\xfa\xcf": (">", True),
    b"\xfe\xed\xfa\xce": (">", False),
}

_LC_LOAD_DYLIB = 0xC
_LC_LOAD_WEAK_DYLIB = 0x80000018
_LC_REEXPORT_DYLIB = 0x8000001F
_LC_LOAD_UPWARD_DYLIB = 0x80000023
_LC_RPATH = 0x8000001C
_MACHO_DYLIB_COMMANDS = frozenset(
    {_LC_LOAD_DYLIB, _LC_LOAD_WEAK_DYLIB, _LC_REEXPORT_DYLIB, _LC_LOAD_UPWARD_DYLIB}
)

# ELF dynamic tags.
_DT_NULL = 0
_DT_NEEDED = 1
_DT_STRTAB = 5
_DT_STRSZ = 10
_DT_RPATH = 15
_DT_RUNPATH = 29
_PT_LOAD = 1
_PT_DYNAMIC = 2

# Kernel-provided ELF "libraries" never exist as files on disk.
_ELF_VIRTUAL_LIBRARIES = frozenset({"linux-vdso.so.1", "linux-gate.so.1"})

# Modern macOS keeps these in the dyld shared cache instead of on disk, so a
# normal executable references them by an absolute path that does not resolve.
_MACHO_SYSTEM_PREFIXES = (
    "/usr/lib/",
    "/System/Library/",
    "/Library/Apple/",
)


class LinkageError(PybrewError):
    """An opt-in linkage scan found dangling dynamic-library references."""


@dataclass(frozen=True)
class LinkageIssue:
    """One unresolved dynamic-library reference."""

    path: str
    reference: str
    kind: str
    reason: str = "missing"


@dataclass
class LinkageReport:
    """Result of :func:`check_linkage`."""

    checked: int = 0
    skipped: int = 0
    issues: list[LinkageIssue] = field(default_factory=list)

    @property
    def dangling(self) -> list[LinkageIssue]:
        return self.issues

    def format(self) -> str:
        if not self.issues:
            return "linkage OK"
        lines = [
            f"{len(self.issues)} dangling dynamic library reference(s) found:"
        ]
        for issue in self.issues[:20]:
            lines.append(
                f"  {issue.path}: {issue.reference} ({issue.kind}, {issue.reason})"
            )
        if len(self.issues) > 20:
            lines.append(f"  ... and {len(self.issues) - 20} more")
        return "\n".join(lines)


def _macho_dynamic_references(
    data: bytes,
) -> Optional[tuple[list[tuple[str, bool]], list[str]]]:
    """Return ``(dylib_loads, rpaths)`` for a thin Mach-O binary.

    ``dylib_loads`` preserves weak-library information so the checker can skip
    weak references (they are allowed to be missing).  Malformed input returns
    ``None`` and is reported as skipped rather than dangling.
    """

    info = _MACHO_MAGICS.get(data[:4])
    if info is None:
        return None
    endian, is_64 = info
    header_size = 32 if is_64 else 28
    if len(data) < header_size:
        return None
    ncmds = struct.unpack_from(endian + "I", data, 16)[0]
    offset = header_size
    loads: list[tuple[str, bool]] = []
    rpaths: list[str] = []
    for _ in range(ncmds):
        if offset + 8 > len(data):
            return None
        command, cmdsize = struct.unpack_from(endian + "II", data, offset)
        if cmdsize < 8 or offset + cmdsize > len(data):
            return None
        if command in _MACHO_DYLIB_COMMANDS:
            name_offset = struct.unpack_from(endian + "I", data, offset + 8)[0]
            if not 8 <= name_offset < cmdsize:
                return None
            raw = data[offset + name_offset : offset + cmdsize]
            name = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
            if name:
                loads.append((name, command == _LC_LOAD_WEAK_DYLIB))
        elif command == _LC_RPATH:
            path_offset = struct.unpack_from(endian + "I", data, offset + 8)[0]
            if not 8 <= path_offset < cmdsize:
                return None
            raw = data[offset + path_offset : offset + cmdsize]
            name = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
            if name:
                rpaths.append(name)
        offset += cmdsize
    return loads, rpaths


def _elf_dynamic_references(data: bytes) -> Optional[tuple[list[str], list[str]]]:
    """Return ``(needed, rpaths)`` from an ELF file's dynamic section.

    No external tools are required.  Malformed/unsupported ELF files return
    ``None`` so they are skipped instead of producing false positives.
    """

    if len(data) < 52 or data[:4] != ELF_MAGIC:
        return None
    elf_class = data[4]
    elf_data = data[5]
    if elf_class not in (1, 2) or elf_data not in (1, 2):
        return None
    endian = "<" if elf_data == 1 else ">"
    is_64 = elf_class == 2
    if is_64:
        if len(data) < 64:
            return None
        e_phoff = struct.unpack_from(endian + "Q", data, 32)[0]
        e_phentsize = struct.unpack_from(endian + "H", data, 54)[0]
        e_phnum = struct.unpack_from(endian + "H", data, 56)[0]
        program_header_size = 56
    else:
        e_phoff = struct.unpack_from(endian + "I", data, 28)[0]
        e_phentsize = struct.unpack_from(endian + "H", data, 42)[0]
        e_phnum = struct.unpack_from(endian + "H", data, 44)[0]
        program_header_size = 32
    if not e_phoff or not e_phnum or e_phentsize < program_header_size:
        return None
    if e_phoff + e_phnum * e_phentsize > len(data):
        return None

    load_segments: list[tuple[int, int, int]] = []
    dynamic: Optional[tuple[int, int]] = None
    for index in range(e_phnum):
        offset = e_phoff + index * e_phentsize
        p_type = struct.unpack_from(endian + "I", data, offset)[0]
        if is_64:
            p_offset = struct.unpack_from(endian + "Q", data, offset + 8)[0]
            p_vaddr = struct.unpack_from(endian + "Q", data, offset + 16)[0]
            p_filesz = struct.unpack_from(endian + "Q", data, offset + 32)[0]
        else:
            p_offset = struct.unpack_from(endian + "I", data, offset + 4)[0]
            p_vaddr = struct.unpack_from(endian + "I", data, offset + 8)[0]
            p_filesz = struct.unpack_from(endian + "I", data, offset + 16)[0]
        if p_type == _PT_LOAD:
            load_segments.append((p_vaddr, p_offset, p_filesz))
        elif p_type == _PT_DYNAMIC:
            dynamic = (p_offset, p_filesz)
    if dynamic is None:
        return None

    needed_offsets: list[int] = []
    rpath_offsets: list[int] = []
    strtab_address: Optional[int] = None
    strtab_size = 0
    entry_size = 16 if is_64 else 8
    offset, dynamic_size = dynamic
    end = min(offset + dynamic_size, len(data))
    while offset + entry_size <= end:
        if is_64:
            tag, value = struct.unpack_from(endian + "qQ", data, offset)
        else:
            tag, value = struct.unpack_from(endian + "iI", data, offset)
        if tag == _DT_NULL:
            break
        if tag == _DT_NEEDED:
            needed_offsets.append(value)
        elif tag == _DT_STRTAB:
            strtab_address = value
        elif tag == _DT_STRSZ:
            strtab_size = value
        elif tag in (_DT_RPATH, _DT_RUNPATH):
            rpath_offsets.append(value)
        offset += entry_size
    if strtab_address is None:
        return None

    strtab_offset: Optional[int] = None
    for p_vaddr, p_offset, p_filesz in load_segments:
        if p_vaddr <= strtab_address < p_vaddr + p_filesz:
            strtab_offset = p_offset + (strtab_address - p_vaddr)
            break
    if strtab_offset is None:
        return None

    def read_string(relative: int) -> Optional[str]:
        if relative < 0 or (strtab_size and relative >= strtab_size):
            return None
        start = strtab_offset + relative
        if start < 0 or start >= len(data):
            return None
        stop = data.find(b"\0", start)
        if stop < 0:
            return None
        return data[start:stop].decode("utf-8", "replace")

    needed: list[str] = []
    for relative in needed_offsets:
        value = read_string(relative)
        if value:
            needed.append(value)
    rpaths: list[str] = []
    for relative in rpath_offsets:
        value = read_string(relative)
        if not value:
            continue
        rpaths.extend(part for part in value.split(":") if part)
    return needed, rpaths


def _multiarch_library_paths() -> list[Path]:
    """Linux multiarch directories such as ``/usr/lib/x86_64-linux-gnu``."""

    paths: list[Path] = []
    for root in (Path("/lib"), Path("/usr/lib"), Path("/lib64"), Path("/usr/lib64")):
        if not root.is_dir():
            continue
        try:
            candidates = sorted(root.glob("*-linux-gnu*"))
        except OSError:
            continue
        paths.extend(candidate for candidate in candidates if candidate.is_dir())
    return paths


def _default_library_paths(
    keg: Path, binary: Path, config: Optional[Config]
) -> list[Path]:
    paths = [
        binary.parent,
        keg / "lib",
        keg / "lib64",
        Path("/usr/lib"),
        Path("/lib"),
        Path("/usr/lib64"),
        Path("/lib64"),
        Path("/usr/local/lib"),
    ]
    paths.extend(_multiarch_library_paths())
    if config is not None:
        paths.extend([config.prefix / "lib", config.prefix / "lib64"])
    return paths


def _candidate_references(
    reference: str,
    binary: Path,
    keg: Path,
    config: Optional[Config],
    rpaths: Iterable[str],
    extra_search_paths: Iterable[Path],
) -> Iterable[Path]:
    """Yield filesystem candidates for a dynamic library reference."""

    tail: Optional[str] = None
    if reference.startswith("@rpath/"):
        tail = reference[len("@rpath/") :]
    elif reference.startswith("@loader_path/"):
        tail = reference[len("@loader_path/") :]
        yield binary.parent / tail
        return
    elif reference.startswith("@executable_path/"):
        tail = reference[len("@executable_path/") :]
        yield binary.parent / tail
        return
    elif reference.startswith("/"):
        yield Path(reference)
        return
    else:
        yield binary.parent / reference

    search_dirs: list[Path] = []
    for raw in rpaths:
        value = raw.replace("$ORIGIN", str(binary.parent))
        value = value.replace("@loader_path", str(binary.parent))
        value = value.replace("@executable_path", str(binary.parent))
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = binary.parent / candidate
        search_dirs.append(candidate)
    search_dirs.extend(_default_library_paths(keg, binary, config))
    search_dirs.extend(Path(path) for path in extra_search_paths)

    if reference.startswith("@rpath/"):
        for directory in search_dirs:
            yield directory / (tail or "")
    else:
        for directory in search_dirs:
            yield directory / reference


def _is_macos_system_reference(reference: str) -> bool:
    # Modern macOS resolves these through the dyld shared cache, so checking
    # the filesystem would produce false positives on every normal binary.
    return sys.platform == "darwin" and reference.startswith(_MACHO_SYSTEM_PREFIXES)


def _reference_exists(
    reference: str,
    binary: Path,
    keg: Path,
    config: Optional[Config],
    rpaths: Iterable[str],
    extra_search_paths: Iterable[Path],
) -> bool:
    if _is_macos_system_reference(reference):
        return True
    for candidate in _candidate_references(
        reference, binary, keg, config, rpaths, extra_search_paths
    ):
        try:
            if candidate.exists():
                return True
        except OSError:
            continue
    return False


def check_linkage(
    keg: Path,
    config: Optional[Config] = None,
    extra_search_paths: Iterable[Path] = (),
) -> LinkageReport:
    """Scan a keg for dangling Mach-O dylib / ELF DT_NEEDED references.

    This is read-only and intentionally tolerant: files that are not binary or
    that cannot be parsed are counted as ``skipped`` rather than reported.
    Weak Mach-O libraries are ignored, system search paths (``/usr/lib`` etc.)
    and the keg/prefix ``lib`` directories are honoured, and callers may pass
    extra search roots for dependencies installed outside the prefix.
    """

    keg = Path(keg)
    report = LinkageReport()
    if not keg.is_dir():
        return report
    extra = [Path(path) for path in extra_search_paths]
    for current, _dirnames, filenames in os.walk(keg, followlinks=False):
        for filename in sorted(filenames):
            path = Path(current) / filename
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open("rb") as handle:
                    magic = handle.read(4)
            except OSError:
                continue
            if magic != ELF_MAGIC and magic not in _MACHO_MAGICS:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if magic == ELF_MAGIC:
                parsed = _elf_dynamic_references(data)
                if parsed is None:
                    report.skipped += 1
                    continue
                needed, rpaths = parsed
                report.checked += 1
                relative = _relative_to_keg(path, keg)
                for reference in dict.fromkeys(needed):
                    if reference in _ELF_VIRTUAL_LIBRARIES:
                        continue
                    if not _reference_exists(
                        reference, path, keg, config, rpaths, extra
                    ):
                        report.issues.append(
                            LinkageIssue(relative, reference, "elf")
                        )
            elif magic in _MACHO_MAGICS:
                parsed_macho = _macho_dynamic_references(data)
                if parsed_macho is None:
                    report.skipped += 1
                    continue
                loads, rpaths = parsed_macho
                report.checked += 1
                relative = _relative_to_keg(path, keg)
                for reference, weak in loads:
                    if weak:
                        continue
                    if not _reference_exists(
                        reference, path, keg, config, rpaths, extra
                    ):
                        report.issues.append(
                            LinkageIssue(relative, reference, "macho")
                        )
    return report


def _relative_to_keg(path: Path, keg: Path) -> str:
    try:
        return str(path.relative_to(keg))
    except ValueError:
        return str(path)


def _linkage_check_requested(options: InstallOptions) -> bool:
    if options.linkage_check:
        return True
    return os.environ.get("PYBREW_LINKAGE_CHECK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _raise_for_linkage(keg: Path, report: LinkageReport) -> None:
    if report.issues:
        raise LinkageError(f"{keg}: {report.format()}")


class Installer:
    """Install formulas from bottles or from simple staged source formulae."""

    def __init__(
        self,
        config: Config,
        loader: FormulaLoader,
        downloader: Optional[Downloader] = None,
        verbose: bool = False,
        environment: Optional[Mapping[str, str]] = None,
    ):
        self.config = config
        self.loader = loader
        self.downloader = downloader or Downloader(config.downloads, config.bottle_dirs, verbose)
        self.linker = Linker(config, verbose)
        self.cellar = Cellar(config)
        self.verbose = verbose
        # Base environment for source builds; ``None`` inherits ``os.environ``.
        # Callers/tests can inject a controlled environment without mutating
        # the process environment.
        self.environment = environment

    # -- dependency resolution ------------------------------------------
    def resolve(self, formula: Formula, with_dependencies: bool = True) -> list[Formula]:
        """Return formulas in deterministic topological install order.

        Dependencies always precede their dependents.  Ready nodes are
        processed in lexicographic order, so the result does not depend on the
        order in which a formula declares its dependencies.  Cycles and missing
        dependencies raise :class:`DependencyError` with the offending path.
        """

        if not with_dependencies:
            return [formula]
        formulas: dict[str, Formula] = {}
        dependencies: dict[str, set[str]] = {}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(current: Formula, chain: list[str]) -> None:
            if current.name in visited:
                return
            if current.name in visiting:
                start = chain.index(current.name) if current.name in chain else 0
                cycle = " -> ".join(chain[start:] + [current.name])
                raise DependencyError(f"dependency cycle detected: {cycle}")
            visiting.add(current.name)
            formulas[current.name] = current
            children: set[str] = set()
            for dep in sorted(current.runtime_dependencies(), key=lambda item: item.name):
                try:
                    child = self.loader.load(dep.name)
                except Exception as error:
                    raise DependencyError(
                        f"{current.name} depends on {dep.name}, which is not available: {error}"
                    ) from error
                children.add(child.name)
                visit(child, chain + [current.name])
            dependencies[current.name] = children
            visiting.discard(current.name)
            visited.add(current.name)

        visit(formula, [])

        # Kahn's algorithm with a lexicographic ready queue gives one stable
        # topological order for any declaration order.
        dependents: dict[str, set[str]] = {name: set() for name in formulas}
        indegree: dict[str, int] = {name: 0 for name in formulas}
        for name, children in dependencies.items():
            for child in children:
                if child in formulas:
                    dependents[child].add(name)
                    indegree[name] += 1
        ready = [name for name, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        ordered: list[Formula] = []
        while ready:
            name = heapq.heappop(ready)
            ordered.append(formulas[name])
            for dependent in sorted(dependents[name]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(ready, dependent)
        if len(ordered) != len(formulas):  # pragma: no cover - visit() catches cycles
            remaining = sorted(set(formulas) - {item.name for item in ordered})
            raise DependencyError(
                "dependency cycle detected: " + " -> ".join(remaining + [remaining[0]])
            )
        return ordered

    # -- install ---------------------------------------------------------
    def install(self, formula: Formula, options: InstallOptions) -> list[InstallResult]:
        with_dependencies = not options.ignore_dependencies
        order = self.resolve(formula, with_dependencies=with_dependencies)
        results: list[InstallResult] = []
        for index, item in enumerate(order):
            is_target = item.name == formula.name
            result = self.install_one(
                item,
                options,
                as_dependency=not is_target,
                apply_bottle_file=is_target,
            )
            results.append(result)
        return results

    def install_one(
        self,
        formula: Formula,
        options: InstallOptions,
        as_dependency: bool = False,
        apply_bottle_file: bool = False,
    ) -> InstallResult:
        package_version = formula.pkg_version
        keg = self.config.keg_path(formula.name, package_version)
        if keg.is_dir():
            if options.force:
                self.linker.unlink(formula, keg)
                self.cellar.remove_keg(keg)
            else:
                return InstallResult(
                    formula.name,
                    package_version,
                    keg,
                    from_bottle=True,
                    linked=True,
                    skipped=True,
                )

        if options.dry_run:
            return InstallResult(
                formula.name,
                package_version,
                keg,
                from_bottle=not options.build_from_source,
                linked=False,
                skipped=True,
            )

        bottle_manifest: Optional[BottleManifest] = None
        bottle_tag: Optional[str] = None
        if options.head or options.build_from_source:
            from_bottle = False
            self._install_from_source(formula, keg, options)
        else:
            bottle = formula.bottle_for_tag(self.config.bottle_tag)
            if bottle is None:
                if formula.stable.url and formula.stable.sha256:
                    from_bottle = False
                    self._install_from_source(formula, keg, options)
                else:
                    raise BottleError(
                        f"{formula.name} has no bottle for this platform and no source checksum"
                    )
            else:
                from_bottle = True
                bottle_tag = bottle[0]
                bottle_manifest = self._install_from_bottle(
                    formula, bottle, keg, options, apply_bottle_file
                )

        sbom = build_keg_sbom(keg)
        write_receipt(
            keg,
            formula,
            poured_from_bottle=from_bottle,
            installed_as_dependency=as_dependency,
            bottle_manifest=(
                bottle_manifest.summary() if bottle_manifest is not None else None
            ),
            sbom=sbom,
            source_metadata=_source_metadata(formula, options),
            build_metadata=_build_metadata(
                options, from_bottle, bottle_tag, self.environment
            ),
        )
        linked = False
        if options.link:
            if not formula.keg_only:
                result = self.linker.link(
                    formula, keg, overwrite=options.overwrite or options.force
                )
                linked = bool(result.created or result.skipped or result.opt)
            else:
                # keg-only formulae still get the opt link.
                result = self.linker.link(formula, keg, overwrite=options.force)
                linked = True

        # Homebrew runs post_install as the final step of a successful install,
        # after the keg has been committed and linked.  A failure aborts the
        # install (raising PostInstallError with the Ruby stderr attached) and
        # leaves the keg in place so it can be inspected or retried.
        if has_post_install(formula):
            run_post_install(formula, self.config, keg=keg)
            _record_post_install(keg, sbom=build_keg_sbom(keg))

        return InstallResult(
            formula.name,
            package_version,
            keg,
            from_bottle=from_bottle,
            linked=linked,
            dependencies=[dep.name for dep in formula.runtime_dependencies()],
        )

    # -- bottles ---------------------------------------------------------
    def _install_from_bottle(
        self,
        formula: Formula,
        bottle: tuple[str, BottleFile],
        keg: Path,
        options: InstallOptions,
        apply_bottle_file: bool,
    ) -> BottleManifest:
        tag, bottle_file = bottle
        if apply_bottle_file and options.bottle_file is not None:
            archive = Path(options.bottle_file)
            if not archive.is_file():
                raise BottleError(f"bottle file not found: {archive}")
            actual = sha256_file(archive)
            if actual != bottle_file.sha256:
                raise ChecksumMismatch(
                    f"bottle file SHA-256 mismatch for {formula.name}:\n"
                    f"  expected {bottle_file.sha256}\n  actual   {actual}"
                )
        else:
            cached = self.downloader.find_cached(bottle_file.sha256)
            if cached is not None:
                actual = sha256_file(cached)
                if actual != bottle_file.sha256:
                    raise ChecksumMismatch(
                        f"cached bottle SHA-256 mismatch for {formula.name}:\n"
                        f"  expected {bottle_file.sha256}\n  actual   {actual}"
                    )
                archive = cached
            else:
                url = formula.bottle_url(tag, bottle_file)
                hint = f"{formula.name}--{formula.pkg_version}.{tag}.bottle.tar.gz"
                archive = self.downloader.fetch(url, bottle_file.sha256, hint=hint)

        # Validate the manifest (and, if needed, the checksum) before touching
        # the extraction directory.  The branches above have already verified
        # the digest, hence ``checksum_verified=True``.
        manifest = read_bottle_manifest(
            archive,
            expected_sha256=bottle_file.sha256,
            checksum_verified=True,
        )
        with tempfile.TemporaryDirectory(prefix="pybrew-bottle-") as temporary:
            extract_root = Path(temporary)
            safe_extract_tar(archive, extract_root)
            staging = find_keg_root(extract_root, formula)
            relocate_tree(staging, self.config)
            if _linkage_check_requested(options):
                _raise_for_linkage(staging, check_linkage(staging, self.config))
            self._commit_keg(staging, keg)
        return manifest

    # -- source ----------------------------------------------------------
    def _install_from_source(self, formula: Formula, keg: Path, options: InstallOptions) -> None:
        stable = formula.head if options.head else formula.stable
        if not stable.url:
            raise UnsupportedInstall(
                f"{formula.name} cannot be installed from source: missing url"
            )
        if stable.sha256:
            archive = self.downloader.fetch(
                stable.url, stable.sha256, hint=f"{formula.name}-source"
            )
        elif options.head:
            archive = self.downloader.fetch_unverified(
                stable.url, hint=f"{formula.name}-head-source"
            )
        else:
            raise UnsupportedInstall(
                f"{formula.name} cannot be installed from source: missing sha256"
            )
        with tempfile.TemporaryDirectory(prefix="pybrew-source-") as temporary:
            extract_root = Path(temporary)
            safe_extract_tar(archive, extract_root)
            build_root = self._find_source_root(extract_root)
            staging = Path(tempfile.mkdtemp(prefix="pybrew-keg-"))
            try:
                build_source(
                    formula,
                    self.config,
                    build_root,
                    staging,
                    verbose=self.verbose or options.verbose,
                    downloader=self.downloader,
                    environment=self.environment,
                )
                if _linkage_check_requested(options):
                    _raise_for_linkage(staging, check_linkage(staging, self.config))
                self._commit_keg(staging, keg)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _find_source_root(extract_root: Path) -> Path:
        children = [child for child in extract_root.iterdir() if child.is_dir()]
        if len(children) == 1:
            return children[0]
        return extract_root

    def _commit_keg(self, staging: Path, keg: Path) -> None:
        keg.parent.mkdir(parents=True, exist_ok=True)
        if keg.exists():
            shutil.rmtree(keg)
        shutil.move(str(staging), str(keg))

    # -- uninstall / link ------------------------------------------------
    def uninstall(self, name: str, version: Optional[str] = None) -> list[Path]:
        formula = self._formula_for_uninstall(name, version)
        kegs = [self.cellar.keg(name, version)] if version else self.cellar.kegs(name)
        removed: list[Path] = []
        for keg in kegs:
            if keg is None or not keg.is_dir():
                continue
            self.linker.unlink(formula, keg)
            self.cellar.remove_keg(keg)
            removed.append(keg)
        if not removed:
            raise PybrewError(f"{name} is not installed")
        return removed

    def _formula_for_uninstall(self, name: str, version: Optional[str]) -> Formula:
        try:
            return self.loader.load(name)
        except Exception:
            keg = self.cellar.keg(name, version)
            from .cellar import read_receipt

            receipt = read_receipt(keg) if keg else {}
            formula = Formula(name=name)
            formula.version = version or receipt.get("version") or "unknown"
            if formula.version and "_" in str(formula.version):
                base, _, revision = str(formula.version).partition("_")
                formula.version = base
                formula.revision = int(revision) if revision.isdigit() else 0
            return formula

    def link(self, formula: Formula, overwrite: bool = False):
        keg = self.cellar.keg(formula.name)
        if keg is None:
            raise PybrewError(f"{formula.name} is not installed")
        return self.linker.link(formula, keg, overwrite=overwrite)

    def unlink(self, formula: Formula):
        keg = self.cellar.keg(formula.name)
        if keg is None:
            raise PybrewError(f"{formula.name} is not installed")
        return self.linker.unlink(formula, keg)

    # -- linkage diagnostics ---------------------------------------------
    def linkage_report(
        self,
        formula: "str | Formula",
        version: Optional[str] = None,
        extra_search_paths: Iterable[Path] = (),
    ) -> LinkageReport:
        """Return the linkage report for an installed formula's keg.

        This is the formula-level diagnostic path: the same :func:`check_linkage`
        used by the opt-in install check, but callable on a committed keg
        without reinstalling.  A missing formula raises ``PybrewError``.
        """

        name = formula.name if isinstance(formula, Formula) else str(formula)
        keg = self.cellar.keg(name, version)
        if keg is None:
            raise PybrewError(f"{name} is not installed")
        return check_linkage(keg, self.config, extra_search_paths)

    def linkage_diagnostic(
        self,
        formula: "str | Formula",
        version: Optional[str] = None,
        extra_search_paths: Iterable[Path] = (),
    ) -> str:
        """Human-readable linkage diagnostic for an installed formula."""

        return self.linkage_report(
            formula, version, extra_search_paths
        ).format()


# -- the tiny source-install DSL -----------------------------------------
@dataclass
class InstallAction:
    directory: str
    patterns: list[str]
    rename: Optional[str] = None

    def destination_dir(self, keg: Path) -> Path:
        mapping = {
            "bin": keg / "bin",
            "sbin": keg / "sbin",
            "lib": keg / "lib",
            "include": keg / "include",
            "share": keg / "share",
            "etc": keg / "etc",
            "libexec": keg / "libexec",
            "prefix": keg,
            "man": keg / "share" / "man",
            "man1": keg / "share" / "man" / "man1",
            "man2": keg / "share" / "man" / "man2",
            "man3": keg / "share" / "man" / "man3",
            "man8": keg / "share" / "man" / "man8",
        }
        return mapping.get(self.directory, keg / self.directory)

    def run(self, build_root: Path, keg: Path) -> None:
        destination_root = self.destination_dir(keg)
        destination_root.mkdir(parents=True, exist_ok=True)
        import glob

        matched = False
        for pattern in self.patterns:
            for match in sorted(glob.glob(str(build_root / pattern))):
                source = Path(match)
                matched = True
                if not source.exists():
                    continue
                name = self.rename or source.name
                target = destination_root / name
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                if source.is_dir():
                    shutil.copytree(source, target, symlinks=True)
                else:
                    shutil.copy2(source, target)
                    os.chmod(target, source.stat().st_mode & 0o777)
        if not matched:
            raise UnsupportedInstall(f"install source pattern matched nothing: {self.patterns}")


_ACTION_RE = re.compile(
    r"^\s*(?P<dir>bin|sbin|lib|include|share|etc|libexec|prefix|man1|man2|man3|man8|man)"
    r"\.install\s+(?P<args>.+?)\s*$"
)
_QUOTED_RE = re.compile(r'"([^"]+)"|\'([^\']+)\'')


def parse_install_actions(install_source: str) -> list[InstallAction]:
    """Translate the small, declarative subset of ``def install`` pybrew runs.

    Only ``<dir>.install "file"`` statements are supported.  Formulae that run
    build commands (``system "make install"``) raise ``UnsupportedInstall`` so
    the limitation is explicit rather than silently producing a broken keg.
    """

    actions: list[InstallAction] = []
    if not install_source:
        return actions
    for raw_line in install_source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line in ("end",) or line.startswith("def install"):
            continue
        match = _ACTION_RE.match(line)
        if not match:
            raise UnsupportedInstall(
                "source install uses unsupported Ruby: " + line[:120]
            )
        args = match.group("args")
        strings = [left or right for left, right in _QUOTED_RE.findall(args)]
        if not strings:
            raise UnsupportedInstall("install statement without string arguments: " + line[:120])
        actions.append(InstallAction(match.group("dir"), strings))
    return actions
