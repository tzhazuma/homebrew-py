"""Minimal Brewfile support (bundle install/check/list)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .cask import CaskError
from .cask_installer import CaskInstaller, CaskLoader, CaskNotFound
from .cellar import Cellar
from .errors import PybrewError
from .installer import InstallOptions, Installer
from .paths import Config
from .tap_manager import TapManager
from .taps import FormulaLoader, discover_taps

_LINE_RE = re.compile(r'^\s*(?P<kind>tap|brew|cask|mas|vscode|whalebrew)\s+"(?P<name>[^"]+)"(?P<rest>.*)$')


@dataclass
class BundleEntry:
    kind: str
    name: str
    options: dict[str, str] = field(default_factory=dict)


def parse_brewfile(path: Path) -> list[BundleEntry]:
    if not path.is_file():
        raise PybrewError(f"Brewfile not found: {path}")
    entries: list[BundleEntry] = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            raise PybrewError(f"unsupported Brewfile line {line_number}: {stripped}")
        entries.append(BundleEntry(match.group("kind"), match.group("name")))
    return entries


def _formula_loader(config: Config) -> FormulaLoader:
    return FormulaLoader(discover_taps(config.taps))


def _cask_loader(config: Config, allow_api: bool) -> CaskLoader:
    return CaskLoader(discover_taps(config.taps), allow_api=allow_api)


def bundle_list(entries: list[BundleEntry]) -> list[str]:
    return [f"{entry.kind} {entry.name}" for entry in entries]


def bundle_check(config: Config, entries: list[BundleEntry], allow_api: bool = False) -> list[str]:
    missing: list[str] = []
    formula_loader = _formula_loader(config)
    cask_loader = _cask_loader(config, allow_api)
    installed_formulae = Cellar(config).installed()
    cask_installer = CaskInstaller(config)
    for entry in entries:
        if entry.kind == "brew":
            if not installed_formulae.get(entry.name):
                missing.append(entry.name)
        elif entry.kind == "cask":
            if not cask_installer.installed_versions(entry.name):
                missing.append(entry.name)
        elif entry.kind == "tap":
            if entry.name not in [tap.name for tap in TapManager(config).list()]:
                missing.append(f"tap:{entry.name}")
    return missing


def bundle_install(
    config: Config,
    entries: list[BundleEntry],
    dry_run: bool = False,
    allow_api: bool = False,
) -> tuple[list[str], list[str]]:
    installed: list[str] = []
    errors: list[str] = []
    tap_manager = TapManager(config)
    formula_loader = _formula_loader(config)
    cask_loader = _cask_loader(config, allow_api)

    for entry in entries:
        if entry.kind == "tap":
            try:
                tap_manager.tap(entry.name)
                installed.append(f"tap {entry.name}")
            except PybrewError as error:
                errors.append(f"{entry.name}: {error}")
            continue

    for entry in entries:
        if entry.kind == "brew":
            try:
                formula = formula_loader.load(entry.name)
                Installer(config, formula_loader).install(
                    formula, InstallOptions(dry_run=dry_run)
                )
                installed.append(f"brew {entry.name}")
            except PybrewError as error:
                errors.append(f"{entry.name}: {error}")
        elif entry.kind == "cask":
            try:
                cask = cask_loader.load(entry.name)
                if dry_run:
                    installed.append(f"cask {entry.name}")
                else:
                    CaskInstaller(config).install(cask)
                    installed.append(f"cask {entry.name}")
            except PybrewError as error:
                errors.append(f"{entry.name}: {error}")
    return installed, errors
