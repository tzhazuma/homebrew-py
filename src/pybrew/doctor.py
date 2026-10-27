"""Health checks for a pybrew prefix."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

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


def diagnose_issues(config: Config) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def issue(category: str, message: str) -> None:
        issues.append({"category": category, "message": message})

    if not config.prefix.exists():
        return [
            {"category": "prefix", "message": f"prefix does not exist: {config.prefix}"}
        ]
    if not os.access(config.prefix, os.W_OK):
        issue("prefix", f"prefix is not writable: {config.prefix}")
    for directory in ("Cellar", "opt", "bin"):
        path = config.prefix / directory
        if not path.is_dir():
            issue("prefix", f"missing prefix directory: {path}")

    cellar = Cellar(config)
    installed = cellar.installed()
    for name, versions in sorted(installed.items()):
        for version in versions:
            keg = config.keg_path(name, version)
            if not read_receipt(keg):
                issue("cellar", f"missing INSTALL_RECEIPT.json: {keg}")
        opt = config.opt_path(name)
        if opt.is_symlink():
            target = Path(os.path.realpath(opt))
            if not target.is_dir():
                issue("cellar", f"dangling opt link: {opt} -> {os.readlink(opt)}")
        else:
            issue("cellar", f"missing opt link for installed formula: {name}")

    for directory in LINK_DIRS:
        for broken in _broken_symlinks(config.prefix / directory):
            issue("links", f"broken link: {broken}")

    if config.caskroom.is_dir():
        for rack in sorted(config.caskroom.iterdir()):
            if not rack.is_dir():
                continue
            for room in sorted(path for path in rack.iterdir() if path.is_dir()):
                receipt_path = room / ".metadata" / "receipt.json"
                if not receipt_path.is_file():
                    issue("cask", f"missing cask receipt: {receipt_path}")
                    continue
                try:
                    data = json.loads(receipt_path.read_text())
                except (OSError, ValueError):
                    issue("cask", f"invalid cask receipt: {receipt_path}")
                    continue
                for path_str in data.get("installed", []):
                    path = Path(path_str)
                    if not path.exists() and not path.is_symlink():
                        issue("cask", f"missing cask artifact: {path}")

    service_dir = config.service_dir
    if service_dir.is_dir():
        for path in sorted(service_dir.iterdir()):
            name = path.name
            if name.startswith("homebrew.mxcl.") and name.endswith((".plist", ".service")):
                formula_name = name[len("homebrew.mxcl.") :].rsplit(".", 1)[0]
                if not config.opt_path(formula_name).is_dir():
                    issue("services", f"orphan service file for uninstalled formula: {path}")

    return issues


def diagnose(config: Config) -> list[str]:
    return [issue["message"] for issue in diagnose_issues(config)]
