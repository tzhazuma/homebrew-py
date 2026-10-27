"""Bottle/source installation, dependency resolution and uninstallation."""

from __future__ import annotations

import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .cellar import Cellar, Linker, write_receipt
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
from .source_builder import build_source
from .taps import FormulaLoader

PLACEHOLDERS = {
    b"@@HOMEBREW_PREFIX@@": lambda config: str(config.prefix).encode(),
    b"@@HOMEBREW_CELLAR@@": lambda config: str(config.cellar).encode(),
    b"@@HOMEBREW_REPOSITORY@@": lambda config: str(config.prefix).encode(),
    b"@@HOMEBREW_LIBRARY_PREFIX@@": lambda config: str(config.prefix / "Library").encode(),
}


@dataclass
class InstallOptions:
    force: bool = False
    ignore_dependencies: bool = False
    build_from_source: bool = False
    bottle_file: Optional[Path] = None
    overwrite: bool = False
    verbose: bool = False
    dry_run: bool = False


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
            new_data = data
            for placeholder, replacement in replacements.items():
                if placeholder not in new_data:
                    continue
                if is_binary:
                    if len(replacement) > len(placeholder):
                        report.skipped_binaries += 1
                        continue
                    padded = replacement + b"\0" * (len(placeholder) - len(replacement))
                    new_data = new_data.replace(placeholder, padded)
                else:
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
    ):
        self.config = config
        self.loader = loader
        self.downloader = downloader or Downloader(config.downloads, config.bottle_dirs, verbose)
        self.linker = Linker(config, verbose)
        self.cellar = Cellar(config)
        self.verbose = verbose

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

        if options.build_from_source:
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
        if not formula.keg_only:
            result = self.linker.link(formula, keg, overwrite=options.overwrite or options.force)
            linked = bool(result.created or result.skipped or result.opt)
        else:
            # keg-only formulae still get the opt link.
            result = self.linker.link(formula, keg, overwrite=options.force)
            linked = True
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
        stable = formula.stable
        if not stable.url or not stable.sha256:
            raise UnsupportedInstall(
                f"{formula.name} cannot be installed from source: missing url/sha256"
            )
        archive = self.downloader.fetch(stable.url, stable.sha256, hint=f"{formula.name}-source")
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
