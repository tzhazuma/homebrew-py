"""Homebrew-style terminal output helpers.

The CLI intentionally keeps these tiny and dependency-free.  They mirror the
formatting Homebrew uses for common commands without colour so output stays
stable in pipes and CI.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def ohai(title: str) -> None:
    """Print a blue-style ``==>`` headline (Homebrew ``ohai``)."""

    print(f"==> {title}")


def oh1(title: str) -> None:
    """Print a green-style ``==>`` headline (Homebrew ``oh1``)."""

    print(f"==> {title}")


def opoo(message: str) -> None:
    """Print a Homebrew ``Warning:`` line to stderr."""

    print(f"Warning: {message}", file=sys.stderr)


def ohai_error(message: str) -> None:
    """Print an error in the shape Homebrew's CLI uses."""

    print(f"Error: {message}", file=sys.stderr)


def _human_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(max(0, size))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def keg_summary(keg: Path) -> str:
    """Return Homebrew's ``files, size`` summary for an installed keg."""

    count = 0
    total = 0
    for root, _dirs, files in os.walk(keg):
        for name in files:
            path = Path(root) / name
            count += 1
            try:
                total += path.lstat().st_size
            except OSError:
                pass
    files = "1 file" if count == 1 else f"{count} files"
    return f"{files}, {_human_size(total)}"


def print_keg(keg: Path) -> None:
    """Print the final ``🍺`` install line used by ``brew install``."""

    print(f"🍺  {keg}: {keg_summary(keg)}")
