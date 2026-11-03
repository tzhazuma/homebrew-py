"""Version detection and comparison.

Homebrew infers a formula version from its stable URL unless the formula sets
one explicitly.  This module implements the common detection patterns used by
homebrew-core, plus a small natural-order comparison used by ``deps``/``list``.

Token semantics are a bounded approximation of Homebrew's ``Version`` parser:
numeric and letter tokens, ranked pre-release words (alpha < beta < pre < rc <
p < post), and the ``PkgVersion`` revision suffix are supported.  Deviations
are intentional and documented on :class:`Version`: most notably the default
comparison keeps ``Version("1.0.0") > Version("1.0")`` (Homebrew's null token
compares equal to a numeric zero), composite tokens such as ``alpha5`` are
compared as separate letter/numeric tokens, and only :class:`PkgVersion` has a
revision.  The closer Homebrew null-token behavior is available opt-in via
``compare_versions(..., missing_numeric_zero=True)`` and
``Version.homebrew_compare``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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


#: Approximate Homebrew pre-release ranking (lower sorts earlier).
_PRE_RELEASE_RANK = {
    "alpha": 0,
    "beta": 1,
    "pre": 2,
    "preview": 2,
    "rc": 3,
    "p": 4,
    "post": 5,
}


def _compare_text_tokens(left: str, right: str) -> int:
    left_rank = _PRE_RELEASE_RANK.get(left)
    right_rank = _PRE_RELEASE_RANK.get(right)
    if left_rank is not None and right_rank is not None:
        if left_rank == right_rank:
            return 0
        return -1 if left_rank < right_rank else 1
    if left == right:
        return 0
    return -1 if left < right else 1


@dataclass(frozen=True)
class _VersionToken:
    """One classified version token (numeric, pre-release word, or letter)."""

    kind: str  # "numeric", "pre", or "letter"
    value: object  # int for numeric tokens, lower-case str otherwise
    rank: int | None = None  # pre-release rank for kind == "pre"
    revision: int = 0  # alpha2 / beta10 / p1 revision number


_TOKEN_PATTERN = re.compile(
    r"(?P<numeric>\d+)"
    r"|(?P<pre>preview|alpha|beta|post|pre|rc|p)(?P<revision>\d*)"
    r"|(?P<letter>[A-Za-z]+)",
    re.IGNORECASE,
)


def _version_tokens(version: str) -> list[_VersionToken]:
    tokens: list[_VersionToken] = []
    for match in _TOKEN_PATTERN.finditer(version):
        if match.group("numeric") is not None:
            tokens.append(_VersionToken("numeric", int(match.group("numeric"))))
        elif match.group("pre") is not None:
            word = match.group("pre").lower()
            if word == "preview":
                word = "pre"
            tokens.append(
                _VersionToken(
                    "pre",
                    word,
                    _PRE_RELEASE_RANK[word],
                    int(match.group("revision") or 0),
                )
            )
        else:
            tokens.append(_VersionToken("letter", match.group("letter").lower()))
    return tokens


def _homebrew_token_compare(left: _VersionToken, right: _VersionToken) -> int:
    """Compare two Homebrew-style tokens; missing tokens are handled by caller."""

    if left.kind == "numeric" and right.kind == "numeric":
        return (left.value > right.value) - (left.value < right.value)
    if left.kind == "numeric":
        return 1  # numeric tokens sort after letters/pre-release words
    if right.kind == "numeric":
        return -1
    if left.kind == "pre" and right.kind == "pre":
        left_rank = left.rank if left.rank is not None else 99
        right_rank = right.rank if right.rank is not None else 99
        if left_rank != right_rank:
            return -1 if left_rank < right_rank else 1
        if left.value != right.value:
            return -1 if str(left.value) < str(right.value) else 1
        return (left.revision > right.revision) - (left.revision < right.revision)
    return _compare_text_tokens(str(left.value), str(right.value))


def _homebrew_compare_token_lists(left: list[_VersionToken], right: list[_VersionToken]) -> int:
    """Homebrew-style comparison where a missing token is a null token.

    A null token compares equal to numeric zero, below non-zero numerics,
    below pre-release words (alpha/beta/pre/rc), and below patch/post/unknown
    letters.  This is opt-in so callers relying on pybrew's historical
    extra-numeric behavior keep it.
    """

    length = max(len(left), len(right))
    for index in range(length):
        left_token = left[index] if index < len(left) else None
        right_token = right[index] if index < len(right) else None
        if left_token is None and right_token is None:
            continue
        if left_token is None:
            assert right_token is not None
            if right_token.kind == "numeric":
                return 0 if right_token.value == 0 else -1
            if right_token.kind == "pre":
                return 1 if right_token.rank is not None and right_token.rank <= 3 else -1
            return -1
        if right_token is None:
            if left_token.kind == "numeric":
                return 0 if left_token.value == 0 else 1
            if left_token.kind == "pre":
                return -1 if left_token.rank is not None and left_token.rank <= 3 else 1
            return 1
        comparison = _homebrew_token_compare(left_token, right_token)
        if comparison:
            return comparison
    return 0


def compare_versions(
    left: str, right: str, *, missing_numeric_zero: bool = False
) -> int:
    """Return -1/0/1 for natural ordering of two version strings.

    By default pybrew keeps its historical behavior: numeric segments compare
    numerically, letter segments compare after numeric segments, trailing
    letters are pre-release markers, and extra numeric segments are
    significant (``1.0.0 > 1.0``) because existing callers/tests rely on it.

    ``missing_numeric_zero=True`` selects the closer Homebrew token-class
    behavior: a missing token is a null token, numeric zero compares equal to
    it, and pre-release words sort before the release while ``p``/``post``
    (and unknown letters) sort after it.
    """

    if missing_numeric_zero:
        left_tokens = _version_tokens(left)
        right_tokens = _version_tokens(right)
        return _homebrew_compare_token_lists(left_tokens, right_tokens)

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
        return _compare_text_tokens(str(a), str(b))
    return 0


@total_ordering
class Version:
    """Small value object around a version string.

    The token parser exposes numeric/letter segments plus ``major``,
    ``minor`` and ``patch``; comparisons stay String-compatible and match
    :func:`compare_versions`.

    Intentional deviations from Homebrew's parser:

    * no composite token classes (``alpha5`` is the tokens ``"alpha", 5``);
    * pre-release words are ranked approximately rather than by token class;
    * extra numeric segments stay significant: ``Version("1.0.0") >
      Version("1.0")`` whereas Homebrew compares the missing/null token equal
      to a numeric zero;
    * ``revision`` is always 0 on ``Version`` and meaningful only on
      :class:`PkgVersion`.

    Use :meth:`homebrew_compare` for the opt-in Homebrew null-token behavior
    where a missing numeric segment compares equal to zero, pre-release words
    sort before the release, and ``p``/``post``/unknown letters sort after it.
    """

    __slots__ = ("value",)

    def __init__(self, value: str):
        self.value = str(value)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    def to_s(self) -> str:
        """String form (Ruby-compatible alias for ``str(version)``)."""

        return self.value

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Version({self.value!r})"

    @property
    def segments(self) -> list[object]:
        """Numeric/letter tokens, e.g. ``"1.2.3-beta"`` -> ``[1, 2, 3, "beta"]``."""

        return _tokens(self.value)

    @property
    def tokens(self) -> list[object]:
        """Alias for :attr:`segments` (Homebrew's token list)."""

        return self.segments

    @property
    def token_classes(self) -> list[str]:
        """Classified tokens: ``numeric``, pre-release word, or ``letter``.

        Composite pre-release tokens are one class (``1.0-alpha2`` ->
        ``["numeric", "numeric", "alpha"]``); raw :attr:`segments` still keep
        the numeric revision separately for String compatibility.
        """

        return [
            str(token.value) if token.kind == "pre" else token.kind
            for token in _version_tokens(self.value)
        ]

    @property
    def pre_release_tokens(self) -> list[str]:
        """Recognized pre-release words in this version (lower case)."""

        return [
            token
            for token in self.segments
            if isinstance(token, str) and token in _PRE_RELEASE_RANK
        ]

    @property
    def pre_release(self) -> bool:
        """Whether the version contains a recognized pre-release token."""

        return bool(self.pre_release_tokens)

    @property
    def revision(self) -> int:
        """Revision number; plain versions have no revision (0)."""

        return 0

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

    def homebrew_compare(self, other: object) -> int:
        """Compare with Homebrew token classes (missing numeric equals zero).

        This opts into the closer Homebrew semantics without changing the
        default ``<=>``/``==`` behavior that existing pybrew callers rely on.
        """

        if isinstance(other, PkgVersion):
            comparison = self.homebrew_compare(other.version)
            if comparison:
                return comparison
            return 0 if other.revision == 0 else -1
        return compare_versions(self.value, str(other), missing_numeric_zero=True)

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
    def pre_release_tokens(self) -> list[str]:
        return self._version.pre_release_tokens

    @property
    def pre_release(self) -> bool:
        return self._version.pre_release

    @property
    def token_classes(self) -> list[str]:
        return self._version.token_classes

    def homebrew_compare(self, other: object) -> int:
        parsed = other if isinstance(other, PkgVersion) else PkgVersion.parse(str(other))
        comparison = self._version.homebrew_compare(parsed.version)
        if comparison:
            return comparison
        return (self._revision > parsed.revision) - (self._revision < parsed.revision)

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
