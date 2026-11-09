"""Homebrew-style terminal output helpers.

Colour is only emitted for a TTY (and is disabled by ``NO_COLOR`` or
``TERM=dumb``), so piped output and tests stay byte-compatible.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

RESET = "\033[0m"
BOLD = "\033[1m"
BLUE = "\033[34m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"


def color_enabled(stream: object = None) -> bool:
    """Return whether ANSI colour should be used for ``stream``."""

    target = sys.stdout if stream is None else stream
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    isatty = getattr(target, "isatty", None)
    return bool(isatty and isatty())


def style(text: str, code: str, *, bold: bool = False) -> str:
    if not color_enabled():
        return text
    prefix = (BOLD if bold else "") + code
    return f"{prefix}{text}{RESET}"


def bold(text: str) -> str:
    return style(text, "", bold=True)


def green(text: str) -> str:
    return style(text, GREEN)


def yellow(text: str) -> str:
    return style(text, YELLOW)


def red(text: str) -> str:
    return style(text, RED)


def ohai(title: str) -> None:
    """Print a blue ``==>`` headline (Homebrew ``ohai``)."""

    print(f"{style('==>', BLUE, bold=True)} {title}")


def oh1(title: str) -> None:
    """Print a blue ``==>`` headline (Homebrew ``oh1``)."""

    print(f"{style('==>', BLUE, bold=True)} {title}")


def opoo(message: str) -> None:
    """Print a Homebrew ``Warning:`` line to stderr."""

    print(f"{style('Warning:', YELLOW, bold=True)} {message}", file=sys.stderr)


def ohai_error(message: str) -> None:
    """Print an error in the shape Homebrew's CLI uses."""

    print(f"{style('Error:', RED, bold=True)} {message}", file=sys.stderr)


def visible_width(text: str) -> int:
    """Length of ``text`` excluding ANSI escape sequences."""

    width = 0
    index = 0
    while index < len(text):
        if text[index] == "\033" and index + 1 < len(text) and text[index + 1] == "[":
            index += 2
            while index < len(text) and text[index] != "m":
                index += 1
            index += 1
            continue
        width += 1
        index += 1
    return width


def print_columns(items: list[str], *, stream: object = None) -> None:
    """Print ``items`` in Homebrew-style columns, preserving ANSI styles."""

    if not items:
        return
    width = shutil.get_terminal_size((80, 24)).columns
    cell = max(visible_width(item) for item in items) + 2
    columns = max(1, width // cell)
    rows = (len(items) + columns - 1) // columns
    for row in range(rows):
        line = []
        for column in range(columns):
            index = column * rows + row
            if index >= len(items):
                continue
            line.append(f"{items[index]:<{cell + len(items[index]) - visible_width(items[index])}}")
        print("".join(line).rstrip(), file=stream if stream is not None else sys.stdout)


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

    print(f"{green('🍺')}  {keg}: {keg_summary(keg)}")
