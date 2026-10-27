"""Outdated detection, upgrade orchestration and cleanup."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .cellar import Cellar, Linker
from .installer import InstallOptions, Installer
from .models import Formula
from .paths import Config
from .taps import FormulaLoader


@dataclass
class OutdatedFormula:
    name: str
    installed: str
    current: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "installed": self.installed, "current": self.current}


def pinned_names(config: Config) -> set[str]:
    if not config.pinned_dir.is_dir():
        return set()
    return {path.name for path in config.pinned_dir.iterdir() if path.is_file()}


def pin(config: Config, name: str) -> Path:
    config.pinned_dir.mkdir(parents=True, exist_ok=True)
    path = config.pinned_dir / name
    path.write_text("pinned by pybrew\n")
    return path


def unpin(config: Config, name: str) -> bool:
    path = config.pinned_dir / name
    if not path.exists():
        return False
    path.unlink()
    return True


def find_outdated(config: Config, loader: FormulaLoader) -> list[OutdatedFormula]:
    cellar = Cellar(config)
    installed = cellar.installed()
    pinned = pinned_names(config)
    result: list[OutdatedFormula] = []
    for name, versions in sorted(installed.items()):
        if name in pinned:
            continue
        try:
            formula = loader.load(name)
        except Exception:
            continue
        latest = versions[-1]
        if formula.pkg_version != latest:
            result.append(OutdatedFormula(name, latest, formula.pkg_version))
    return result


def upgrade_formula(
    config: Config,
    loader: FormulaLoader,
    formula: Formula,
    dry_run: bool = False,
    verbose: bool = False,
) -> list[str]:
    installer = Installer(config, loader)
    results = installer.install(
        formula,
        InstallOptions(dry_run=dry_run, verbose=verbose),
    )
    return [f"{result.name} {result.version}" for result in results if not result.skipped or dry_run]


def cleanup(
    config: Config,
    dry_run: bool = False,
    keep_installed: Optional[Iterable[str]] = None,
) -> list[Path]:
    """Remove old kegs, keeping the opt-linked keg (or the newest one)."""

    cellar = Cellar(config)
    linker = Linker(config)
    installed = cellar.installed()
    removed: list[Path] = []
    for name, versions in sorted(installed.items()):
        if keep_installed is not None and name in set(keep_installed):
            continue
        keep: Optional[str] = None
        linked = linker.linked_keg(name)
        if linked is not None and linked.parent.name == name:
            keep = linked.name
        if keep is None or keep not in versions:
            keep = versions[-1]
        for version in versions:
            if version == keep:
                continue
            keg = config.keg_path(name, version)
            if dry_run:
                removed.append(keg)
            else:
                cellar.remove_keg(keg)
                removed.append(keg)
    return removed


def reinstall(
    config: Config,
    loader: FormulaLoader,
    formula: Formula,
    verbose: bool = False,
) -> list[str]:
    installer = Installer(config, loader)
    installer.uninstall(formula.name)
    results = installer.install(formula, InstallOptions(verbose=verbose))
    return [f"{result.name} {result.version}" for result in results]


def cleanup_cache(config: Config, age_days: int = 120, dry_run: bool = False) -> list[Path]:
    """Remove cached downloads older than ``age_days``."""

    removed: list[Path] = []
    downloads = config.downloads
    if not downloads.is_dir():
        return removed
    cutoff = time.time() - max(0, age_days) * 86400
    for path in sorted(downloads.iterdir()):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        removed.append(path)
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                pass
    return removed
