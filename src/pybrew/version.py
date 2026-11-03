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
    """Return -1/0/1 for natural ordering of two version strings.

    Numeric segments compare numerically, letter segments compare
    case-insensitively after numeric segments, and a trailing letter token
    (for example ``1.0-beta``) sorts before the plain release while extra
    numeric segments (``1.0.0`` vs ``1.0``) keep their historical ordering.
    """

    lt, rt = _tokens(left), _tokens(right)
    sentinel = object()
    for index in range(max(len(lt), len(rt))):
        a = lt[index] if index < len(lt) else sentinel
        b = rt[index] if index < len(rt) else sentinel
        if a is sentinel:
            # Left side ended: a plain release sorts after a pre-release suffix.
            return 1 if isinstance(b, str) else -1
        if b is sentinel:
            return 1 if isinstance(a, int) else -1
        if a == b:
            continue
        if isinstance(a, int) and isinstance(b, int):
            return -1 if a < b else 1
        if isinstance(a, int):
            return 1  # numeric segment sorts after a text segment
        if isinstance(b, int):
            return -1
        return -1 if str(a) < str(b) else 1
    return 0


@total_ordering
class Version:
    """Small value object around a version string.

    The token parser exposes numeric/letter segments plus ``major``,
    ``minor`` and ``patch``; comparisons stay String-compatible and match
    :func:`compare_versions`.
    """

    __slots__ = ("value",)

    def __init__(self, value: str):
        self.value = str(value)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Version({self.value!r})"

    @property
    def segments(self) -> list[object]:
        """Numeric/letter tokens, e.g. ``"1.2.3-beta"`` -> ``[1, 2, 3, "beta"]``."""

        return _tokens(self.value)

    def _segment(self, index: int) -> int:
        tokens = self.segments
        if index >= len(tokens):
            return 0
        token = tokens[index]
        return token if isinstance(token, int) else 0

    @property
    def major(self) -> int:
        return self._segment(0)

    @property
    def minor(self) -> int:
        return self._segment(1)

    @property
    def patch(self) -> int:
        return self._segment(2)

    @property
    def major_minor(self) -> "Version":
        return Version(".".join(str(token) for token in self.segments[:2]))

    @property
    def major_minor_patch(self) -> "Version":
        return Version(".".join(str(token) for token in self.segments[:3]))

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


_PKG_VERSION_RE = re.compile(r"^(?P<version>.*?)(?:_(?P<revision>\d+))?$")


class PkgVersion(Version):
    """Formula version plus Homebrew's optional revision suffix (``1.0_2``)."""

    __slots__ = ("_version", "_revision")

    def __init__(self, version: object, revision: int = 0):
        if isinstance(version, PkgVersion):
            base = version.version
            if revision == 0:
                revision = version.revision
        elif isinstance(version, Version):
            base = version
        else:
            base = Version(str(version))
        self._version = base
        self._revision = int(revision)
        super().__init__(str(self))

    @classmethod
    def parse(cls, text: object) -> "PkgVersion":
        match = _PKG_VERSION_RE.match(str(text))
        if match is None:
            return cls(Version(""), 0)
        return cls(Version(match.group("version")), int(match.group("revision") or 0))

    @property
    def version(self) -> Version:
        return self._version

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def segments(self) -> list[object]:
        return self._version.segments

    @property
    def major(self) -> int:
        return self._version.major

    @property
    def minor(self) -> int:
        return self._version.minor

    @property
    def patch(self) -> int:
        return self._version.patch

    @property
    def major_minor(self) -> Version:
        return self._version.major_minor

    @property
    def major_minor_patch(self) -> Version:
        return self._version.major_minor_patch

    def __str__(self) -> str:
        if self._revision > 0:
            return f"{self._version}_{self._revision}"
        return str(self._version)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"PkgVersion({str(self)!r}, revision={self._revision})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PkgVersion):
            return self._version == other._version and self._revision == other._revision
        if isinstance(other, Version):
            return self._revision == 0 and self._version == other
        if isinstance(other, str):
            return self == PkgVersion.parse(other)
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, PkgVersion):
            if self._version != other._version:
                return self._version < other._version
            return self._revision < other._revision
        if isinstance(other, Version):
            if self._version != other:
                return self._version < other
            return False
        if isinstance(other, str):
            return self < PkgVersion.parse(other)
        return NotImplemented

    def __hash__(self) -> int:  # pragma: no cover - trivial
        return hash(str(self))
