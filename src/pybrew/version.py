"""Version detection and comparison.

Homebrew infers a formula version from its stable URL unless the formula sets
one explicitly.  This module implements the common detection patterns used by
homebrew-core, plus a small natural-order comparison used by ``deps``/``list``.
"""

from __future__ import annotations

import re
from functools import total_ordering

_ARCHIVE_SUFFIXES = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
    ".tar.lz",
    ".tar.lzma",
    ".tbz2",
    ".tgz",
    ".txz",
    ".zip",
    ".dmg",
    ".pkg",
    ".jar",
    ".gem",
    ".tar",
)

_DOTTED_VERSION = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:[vV](?=\d))?"
    r"(?P<version>\d+(?:\.\d+)+(?:[A-Za-z]\d*)?"
    r"(?:[-_.]?(?:alpha|beta|preview|pre|rc|dev|snapshot)\.?\d*)?)",
    re.IGNORECASE,
)
_LOOSE_VERSION = re.compile(r"(?<![A-Za-z0-9])(?P<version>\d{4,})(?![A-Za-z0-9])")
_SQLITE_AUTOCONF = re.compile(r"sqlite[-_]autoconf-(\d)(\d{2})(\d{2})(\d{2})", re.IGNORECASE)


def strip_archive_suffix(filename: str) -> str:
    """Remove a trailing archive extension from ``filename``."""

    lower = filename.lower()
    for suffix in _ARCHIVE_SUFFIXES:
        if lower.endswith(suffix):
            return filename[: -len(suffix)]
    return filename


def detect_version(url: str, version_scheme: int = 0) -> str:
    """Infer a Homebrew-style version string from a source download URL.

    ``version_scheme`` is accepted for API compatibility with Homebrew's
    ``Version.detect``; it only affects a handful of special cases upstream and
    is currently preserved for future use.
    """

    if not url:
        return "HEAD"

    # Work on the last path component, dropping query strings and fragments.
    path = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    basename = path.rsplit("/", 1)[-1]
    stem = strip_archive_suffix(basename)
    # Tags and archive names commonly carry a "v" prefix (v3.15.2).
    if re.match(r"[vV]\d", stem):
        stem = stem[1:]

    sqlite_match = _SQLITE_AUTOCONF.search(stem)
    if sqlite_match:
        major, minor, patch, _ = sqlite_match.groups()
        return f"{major}.{minor}.{int(patch)}"

    match = _DOTTED_VERSION.search(stem)
    if match:
        version = match.group("version")
        # A trailing ".0" is meaningful in date versions (20260817.0) but
        # Homebrew trims redundant ".0" only for schemes it knows about.  We
        # keep the detected string verbatim; callers can override explicitly.
        return version

    loose = _LOOSE_VERSION.search(stem)
    if loose:
        return loose.group("version")

    # A "v2" style tag without a dotted version.
    tag_match = re.search(r"(?:^|[-_/])v(\d+)(?![A-Za-z0-9])", basename, re.IGNORECASE)
    if tag_match:
        return tag_match.group(1)

    return "HEAD"


def _tokens(version: str) -> list[object]:
    parts = re.findall(r"\d+|[A-Za-z]+", version)
    tokens: list[object] = []
    for part in parts:
        if part.isdigit():
            tokens.append(int(part))
        else:
            tokens.append(part.lower())
    return tokens


def compare_versions(left: str, right: str) -> int:
    """Return -1/0/1 for natural ordering of two version strings."""

    lt, rt = _tokens(left), _tokens(right)
    for a, b in zip(lt, rt):
        if a == b:
            continue
        if isinstance(a, int) and isinstance(b, int):
            return -1 if a < b else 1
        if isinstance(a, int):
            return 1  # numeric segment sorts after a text segment
        if isinstance(b, int) is False and isinstance(b, int):
            return -1
        return -1 if str(a) < str(b) else 1
    if len(lt) == len(rt):
        return 0
    return -1 if len(lt) < len(rt) else 1


@total_ordering
class Version:
    """Small value object around a version string."""

    __slots__ = ("value",)

    def __init__(self, value: str):
        self.value = str(value)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Version({self.value!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Version):
            return compare_versions(self.value, other.value) == 0
        if isinstance(other, str):
            return compare_versions(self.value, other) == 0
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, Version):
            return compare_versions(self.value, other.value) < 0
        if isinstance(other, str):
            return compare_versions(self.value, other) < 0
        return NotImplemented

    def __hash__(self) -> int:  # pragma: no cover - trivial
        return hash(self.value)
