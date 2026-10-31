"""Bottle/source installation, dependency resolution and uninstallation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

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


def _record_post_install(keg: Path) -> None:
    """Mark the install receipt after a successful post_install run."""

    receipt = read_receipt(keg)
    receipt["post_install_ran"] = True
    path = receipt_path(keg)
    path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def safe_extract_tar(archive: Path, destination: Path) -> None:
    """Extract a bottle/source tarball without allowing path escapes."""

    destination.mkdir(parents=True, exist_ok=True)
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
                # Hard links are rare in Homebrew bottles; copy the referenced
                # file if it has already been extracted.
                source_path = destination / member.linkname
                if source_path.is_file():
                    shutil.copy2(source_path, target)
                continue
            # Devices/FIFOs are not valid in bottles.
            raise BottleError(f"unsupported archive member type for {name!r}")


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
        if not with_dependencies:
            return [formula]
        ordered: list[Formula] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(current: Formula, chain: list[str]) -> None:
            if current.name in visited:
                return
            if current.name in visiting:
                cycle = " -> ".join(chain + [current.name])
                raise DependencyError(f"dependency cycle detected: {cycle}")
            visiting.add(current.name)
            for dep in current.runtime_dependencies():
                try:
                    child = self.loader.load(dep.name)
                except Exception as error:
                    raise DependencyError(
                        f"{current.name} depends on {dep.name}, which is not available: {error}"
                    ) from error
                visit(child, chain + [current.name])
            visiting.discard(current.name)
            visited.add(current.name)
            ordered.append(current)

        visit(formula, [])
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
                self._install_from_bottle(formula, bottle, keg, options, apply_bottle_file)

        write_receipt(
            keg,
            formula,
            poured_from_bottle=from_bottle,
            installed_as_dependency=as_dependency,
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
            _record_post_install(keg)

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
    ) -> None:
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

        with tempfile.TemporaryDirectory(prefix="pybrew-bottle-") as temporary:
            extract_root = Path(temporary)
            safe_extract_tar(archive, extract_root)
            staging = find_keg_root(extract_root, formula)
            relocate_tree(staging, self.config)
            self._commit_keg(staging, keg)

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
