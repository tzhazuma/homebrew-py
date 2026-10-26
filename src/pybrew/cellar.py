"""Cellar, keg, receipt and linking primitives."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import PYBREW_VERSION
from .errors import LinkConflict, PybrewError
from .models import Formula
from .paths import LINK_DIRS, Config


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


@dataclass
class LinkResult:
    created: list[Path]
    skipped: list[Path]
    opt: Optional[Path]


class Linker:
    """Create and remove the symlinks that expose a keg in a prefix."""

    def __init__(self, config: Config, verbose: bool = False):
        self.config = config
        self.verbose = verbose

    # -- desired links ---------------------------------------------------
    def _keg_entries(self, keg: Path) -> Iterable[tuple[str, Path]]:
        for directory in LINK_DIRS:
            root = keg / directory
            if not root.is_dir():
                continue
            for current, dirnames, filenames in os.walk(root, followlinks=False):
                current_path = Path(current)
                # A symlinked directory inside a keg is linked, not walked.
                for dirname in list(dirnames):
                    candidate = current_path / dirname
                    if candidate.is_symlink():
                        dirnames.remove(dirname)
                        yield directory, candidate
                for filename in filenames:
                    yield directory, current_path / filename

    def _desired_links(self, keg: Path) -> list[tuple[Path, Path]]:
        desired: list[tuple[Path, Path]] = []
        for directory, source in self._keg_entries(keg):
            relative = source.relative_to(keg / directory)
            link = self.config.prefix / directory / relative
            desired.append((link, source))
        return desired

    # -- link ------------------------------------------------------------
    def link(self, formula: Formula, keg: Path, overwrite: bool = False) -> LinkResult:
        if not keg.is_dir():
            raise PybrewError(f"keg does not exist: {keg}")
        opt_link = self.config.opt_path(formula.name)
        opt_created: Optional[Path] = None
        conflicts: list[tuple[Path, str]] = []
        desired = self._desired_links(keg) if not formula.keg_only else []

        for link, source in desired:
            if not os.path.lexists(link):
                continue
            if link.is_symlink():
                try:
                    if Path(os.path.realpath(link)) == source.resolve():
                        continue
                except OSError:
                    pass
            conflicts.append((link, "exists and is not a pybrew link"))

        if conflicts and not overwrite:
            listing = "\n".join(f"  {path} ({reason})" for path, reason in conflicts[:30])
            raise LinkConflict(
                f"cannot link {formula.name}: {len(conflicts)} conflict(s):\n{listing}"
            )

        # Preflight passed; make the opt link first so partial failures still
        # leave a discoverable keg.
        self.config.opt.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(opt_link):
            if overwrite or self._opt_points_to_rack(opt_link, formula):
                opt_link.unlink()
        if not os.path.lexists(opt_link):
            opt_link.symlink_to(os.path.relpath(keg, start=opt_link.parent))
            opt_created = opt_link

        created: list[Path] = []
        skipped: list[Path] = []
        for link, source in desired:
            if os.path.lexists(link):
                try:
                    if link.is_symlink() and Path(os.path.realpath(link)) == source.resolve():
                        skipped.append(link)
                        continue
                except OSError:
                    pass
                if overwrite:
                    if link.is_dir() and not link.is_symlink():
                        raise LinkConflict(f"cannot overwrite directory {link}")
                    link.unlink()
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(os.path.relpath(source, start=link.parent))
            created.append(link)

        return LinkResult(created=created, skipped=skipped, opt=opt_created)

    @staticmethod
    def _opt_points_to_rack(opt_link: Path, formula: Formula) -> bool:
        try:
            target = Path(os.path.realpath(opt_link))
        except OSError:
            return False
        return target.parent == (opt_link.parent.parent / "Cellar" / formula.name).resolve() or (
            target.parent.name == formula.name
        )

    # -- unlink ----------------------------------------------------------
    def unlink(self, formula: Formula, keg: Optional[Path] = None) -> list[Path]:
        if keg is None:
            keg = self.config.keg_path(formula.name, formula.pkg_version)
        if not keg.is_dir():
            return []
        removed: list[Path] = []
        keg_resolved = keg.resolve()
        for directory in LINK_DIRS:
            root = self.config.prefix / directory
            if not root.is_dir():
                continue
            for current, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
                current_path = Path(current)
                # Remove symlinks to our keg.
                for name in list(filenames) + [d for d in dirnames if (current_path / d).is_symlink()]:
                    candidate = current_path / name
                    if not candidate.is_symlink():
                        continue
                    try:
                        target = Path(os.path.realpath(candidate))
                    except OSError:
                        continue
                    if _is_within(target, keg_resolved):
                        try:
                            candidate.unlink()
                            removed.append(candidate)
                        except OSError:
                            pass
                # Prune empty directories, but never the prefix link root.
                if current_path != root:
                    try:
                        if not any(current_path.iterdir()):
                            current_path.rmdir()
                    except OSError:
                        pass
        opt_link = self.config.opt_path(formula.name)
        if opt_link.is_symlink():
            try:
                target = Path(os.path.realpath(opt_link))
            except OSError:
                target = Path()
            if _is_within(target, keg_resolved) or target.parent == keg_resolved:
                opt_link.unlink()
                removed.append(opt_link)
        return removed

    # -- introspection ---------------------------------------------------
    def linked_keg(self, name: str) -> Optional[Path]:
        opt_link = self.config.opt_path(name)
        if not opt_link.is_symlink():
            return None
        try:
            target = Path(os.path.realpath(opt_link))
        except OSError:
            return None
        if target.parent.parent == self.config.cellar:
            return target
        return None


class Cellar:
    """Read and mutate the Cellar directory."""

    def __init__(self, config: Config):
        self.config = config

    def installed(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        if not self.config.cellar.is_dir():
            return result
        for rack in sorted(self.config.cellar.iterdir()):
            if not rack.is_dir() or rack.name.startswith("."):
                continue
            versions = sorted(
                child.name for child in rack.iterdir() if child.is_dir() and not child.name.startswith(".")
            )
            if versions:
                result[rack.name] = versions
        return result

    def is_installed(self, name: str, version: Optional[str] = None) -> bool:
        installed = self.installed().get(name, [])
        if version is None:
            return bool(installed)
        return version in installed

    def kegs(self, name: str) -> list[Path]:
        rack = self.config.rack_path(name)
        if not rack.is_dir():
            return []
        return sorted(path for path in rack.iterdir() if path.is_dir())

    def keg(self, name: str, version: Optional[str] = None) -> Optional[Path]:
        kegs = self.kegs(name)
        if not kegs:
            return None
        if version is None:
            return kegs[-1]
        candidate = self.config.keg_path(name, version)
        return candidate if candidate.is_dir() else None

    def remove_keg(self, keg: Path) -> None:
        if not _is_within(keg, self.config.cellar):
            raise PybrewError(f"refusing to remove path outside Cellar: {keg}")
        if keg.exists():
            shutil.rmtree(keg)
        rack = keg.parent
        try:
            next(rack.iterdir())
        except StopIteration:
            rack.rmdir()
        except OSError:
            pass


# -- receipts ------------------------------------------------------------
def receipt_path(keg: Path) -> Path:
    return keg / "INSTALL_RECEIPT.json"


def write_receipt(
    keg: Path,
    formula: Formula,
    poured_from_bottle: bool,
    installed_as_dependency: bool = False,
    runtime_dependencies: Optional[list[str]] = None,
) -> Path:
    payload = {
        "homebrew_version": PYBREW_VERSION,
        "source": {
            "path": str(formula.path) if formula.path else None,
            "tap": formula.tap,
        },
        "version": formula.pkg_version,
        "runtime_dependencies": runtime_dependencies
        if runtime_dependencies is not None
        else [dep.name for dep in formula.runtime_dependencies()],
        "installed_as_dependency": installed_as_dependency,
        "poured_from_bottle": poured_from_bottle,
        "built_as_bottle": poured_from_bottle,
        "installed_on_request": not installed_as_dependency,
    }
    path = receipt_path(keg)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_receipt(keg: Path) -> dict:
    path = receipt_path(keg)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
