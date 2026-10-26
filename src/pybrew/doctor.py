"""Health checks for a pybrew prefix."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .cellar import Cellar, read_receipt
from .paths import LINK_DIRS, Config


def _broken_symlinks(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(current) / name
            if path.is_symlink() and not path.exists():
                yield path


def diagnose(config: Config) -> list[str]:
    issues: list[str] = []
    if not config.prefix.exists():
        return [f"prefix does not exist: {config.prefix}"]
    if not os.access(config.prefix, os.W_OK):
        issues.append(f"prefix is not writable: {config.prefix}")
    for directory in ("Cellar", "opt", "bin"):
        if not (config.prefix / directory).is_dir():
            issues.append(f"missing prefix directory: {config.prefix / directory}")

    cellar = Cellar(config)
    installed = cellar.installed()
    for name, versions in sorted(installed.items()):
        for version in versions:
            keg = config.keg_path(name, version)
            if not read_receipt(keg):
                issues.append(f"missing INSTALL_RECEIPT.json: {keg}")
        opt = config.opt_path(name)
        if opt.is_symlink():
            target = Path(os.path.realpath(opt))
            if not target.is_dir():
                issues.append(f"dangling opt link: {opt} -> {os.readlink(opt)}")
        else:
            issues.append(f"missing opt link for installed formula: {name}")

    for directory in LINK_DIRS:
        for broken in _broken_symlinks(config.prefix / directory):
            issues.append(f"broken link: {broken}")
    return issues
