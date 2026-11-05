"""Version detection and comparison.

Homebrew infers a formula version from its stable URL unless the formula sets
one explicitly.  This module implements the common detection patterns used by
homebrew-core, plus a small natural-order comparison used by ``deps``/``list``.

Token semantics are a bounded approximation of Homebrew's ``Version`` parser:
Numeric, Alpha/Beta/Pre/Rc, P/Post, Letter, and Null token classes are
exposed; pre-release classes sort before the Null/release token, while
p/post/letter sort after it and Numeric sorts above letters.  The
``PkgVersion`` revision suffix is supported too.  Homebrew
null-token comparison is the default (``1.0 == 1.0.0``; missing numeric
segments are zero).  The historical extra-numeric behavior is an explicit
opt-out via ``PYBREW_VERSION_LEGACY=1`` or ``Version.legacy = True``.
Composite tokens such as ``alpha5`` are compared as separate letter/numeric
tokens, and only :class:`PkgVersion` has a revision.  Hashes are canonical in
Homebrew mode (equal versions hash equally) and String-compatible in legacy
mode.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from functools import total_ordering
from typing import Optional

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


@total_ordering
class Token:
    """Bounded Homebrew-like version token.

    Subclasses follow Homebrew's token hierarchy: pre-release words
    (Alpha < Beta < Pre < Rc), patch/post tokens, unknown letters, numbers,
    and a null token used for missing segments.
    """

    class_name = "token"
    group = 3  # pre-release=0, null=1, letter/p/post=2, numeric=3
    rank = 0

    def __init__(self, value: object, revision: int = 0):
        self.value = value
        self.revision = int(revision)

    @property
    def kind(self) -> str:
        """Compatibility alias for :attr:`class_name`."""

        return self.class_name

    def to_s(self) -> str:
        if self.value is None:
            return ""
        suffix = str(self.revision) if self.revision else ""
        return f"{self.value}{suffix}"

    def __str__(self) -> str:
        return self.to_s()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.to_s()!r}>"

    def compare(self, other: "Token") -> int:
        if not isinstance(other, Token):
            return NotImplemented
        if type(self) is NullToken or type(other) is NullToken:
            return self._compare_null(other)
        if type(self) is NumericToken or type(other) is NumericToken:
            if type(self) is NumericToken and type(other) is NumericToken:
                return (self.value > other.value) - (self.value < other.value)
            return 1 if type(self) is NumericToken else -1

        self_pre = isinstance(self, PreReleaseToken)
        other_pre = isinstance(other, PreReleaseToken)
        if self_pre and other_pre:
            if self.rank != other.rank:
                return -1 if self.rank < other.rank else 1
            return _compare_token_values(self, other)
        if self_pre:
            # Homebrew's Pre/Alpha/Beta/Rc tokens compare lexically against
            # unknown letters but sort before P/Post.
            if isinstance(other, (PToken, PostToken)):
                return -1
            return _compare_token_text(self, other)
        if other_pre:
            if isinstance(self, (PToken, PostToken)):
                return 1
            return _compare_token_text(self, other)

        if isinstance(self, PostToken) or isinstance(other, PostToken):
            if isinstance(self, PostToken) and isinstance(other, PostToken):
                return (self.revision > other.revision) - (self.revision < other.revision)
            return -1 if isinstance(self, PostToken) else 1
        if isinstance(self, PToken) and isinstance(other, PToken):
            return (self.revision > other.revision) - (self.revision < other.revision)
        # P vs Letter and Letter vs Letter are lexical (Homebrew StringToken).
        return _compare_token_text(self, other)

    def _compare_null(self, other: "Token") -> int:
        if type(self) is NullToken and type(other) is NullToken:
            return 0
        if type(self) is NullToken:
            return -other._compare_null(self)
        # The caller asked: this non-null token vs a null token.
        if type(self) is NumericToken:
            return 0 if self.value == 0 else 1
        if self.group == 0:  # alpha/beta/pre/rc sort before the release
            return -1
        return 1  # p/post/letter sort after the release

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Token):
            return self.compare(other) == 0
        if isinstance(other, (int, str)):
            return self.revision == 0 and self.value == other
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Token):
            return NotImplemented
        return self.compare(other) < 0

    def __hash__(self) -> int:
        # Equal tokens must hash equally: Null == NumericToken(0), and
        # revision-0 tokens compare equal to their raw text form.
        if type(self) is NullToken:
            return hash(0)
        if type(self) is NumericToken:
            return hash(self.value)
        # Short pre-release aliases (``a4``/``alpha4``) compare equal, so
        # hash the canonical word + revision.  Other pre-release and
        # patch/post tokens compare textually (``p`` == LetterToken("p")).
        if isinstance(self, (AlphaToken, BetaToken)) and self.revision:
            word = "alpha" if isinstance(self, AlphaToken) else "beta"
            return hash(f"{word}{self.revision}")
        if isinstance(self, PreReleaseToken) or isinstance(self, (PToken, PostToken)):
            return hash(self.to_s())
        return hash(self.to_s())


def _compare_token_values(left: Token, right: Token) -> int:
    """Compare same-class pre-release tokens by numeric revision."""

    return (left.revision > right.revision) - (left.revision < right.revision)


def _compare_token_text(left: Token, right: Token) -> int:
    left_text = left.to_s()
    right_text = right.to_s()
    return (left_text > right_text) - (left_text < right_text)


class NumericToken(Token):
    class_name = "numeric"
    group = 3


class StringToken(Token):
    """Base class for Homebrew's string/composite token hierarchy."""

    class_name = "string"
    group = 2


class CompositeToken(StringToken):
    """A token made from several child tokens (upstream compatibility)."""

    class_name = "composite"

    def __init__(self, tokens: list[Token] | tuple[Token, ...] = (), revision: int = 0):
        self.tokens = list(tokens)
        super().__init__("".join(str(token) for token in self.tokens), revision)

    def to_s(self) -> str:
        return "".join(str(token) for token in self.tokens)

    def compare(self, other: "Token") -> int:
        if not isinstance(other, CompositeToken):
            return super().compare(other)
        for left, right in zip(self.tokens, other.tokens):
            comparison = left.compare(right)
            if comparison:
                return comparison
        return (len(self.tokens) > len(other.tokens)) - (
            len(self.tokens) < len(other.tokens)
        )

    def __hash__(self) -> int:
        return hash(tuple(hash(token) for token in self.tokens))


class PreReleaseToken(StringToken):
    group = 0


class AlphaToken(PreReleaseToken):
    class_name = "alpha"
    rank = 0


class BetaToken(PreReleaseToken):
    class_name = "beta"
    rank = 1


class PreToken(PreReleaseToken):
    class_name = "pre"
    rank = 2


class RcToken(PreReleaseToken):
    class_name = "rc"
    rank = 3


class PToken(StringToken):
    class_name = "p"
    group = 2
    rank = 4


class PostToken(StringToken):
    class_name = "post"
    group = 2
    rank = 5


class LetterToken(StringToken):
    class_name = "letter"
    group = 2
    rank = 50


class NullToken(Token):
    class_name = "null"
    group = 1

    def __init__(self) -> None:
        super().__init__(None, 0)


# Homebrew-compatible aliases for callers that use the upstream names.
RCToken = RcToken
PatchToken = PToken
MissingToken = NullToken

#: Shared singleton used for missing version segments (upstream ``NULL_TOKEN``).
NULL_TOKEN = NullToken()


def _token_from(value: object, revision: int = 0) -> Token:
    if isinstance(value, Token):
        return value
    if value is None or value == "":
        return NULL_TOKEN
    if isinstance(value, int) and not isinstance(value, bool):
        return NumericToken(value, revision)
    text = str(value)
    if text.isdigit():
        return NumericToken(int(text), revision)
    match = re.fullmatch(
        r"(?P<pre>preview|alpha|beta|post|pre|rc|p)(?P<revision>\d*)",
        text,
        re.IGNORECASE,
    )
    if match:
        word = match.group("pre").lower()
        if word == "preview":
            word = "pre"
        token_class = _PRE_TOKEN_CLASSES[word]
        return token_class(word, int(match.group("revision") or revision or 0))
    return LetterToken(text.lower(), revision)


Token.create = classmethod(lambda cls, value, revision=0: _token_from(value, revision))
Token.from_value = classmethod(lambda cls, value, revision=0: _token_from(value, revision))
Token.from_token = classmethod(lambda cls, value, revision=0: _token_from(value, revision))
# ``Token.from`` is a normal classmethod name; assigning it explicitly keeps
# mypy/IDE callers working without shadowing Python's builtin ``from`` syntax.
Token.from_ = Token.from_value


def _token_numeric(self: Token) -> bool:
    return isinstance(self, NumericToken)


def _token_null(self: Token) -> bool:
    return isinstance(self, NullToken)


def _token_blank(self: Token) -> bool:
    return self.value is None or str(self.value) == ""


def _token_to_i(self: Token) -> int:
    try:
        return int(self.value or 0)
    except (TypeError, ValueError):
        return 0


def _token_to_f(self: Token) -> float:
    try:
        return float(self.value or 0)
    except (TypeError, ValueError):
        return 0.0


Token.numeric = property(_token_numeric)
Token.null = property(_token_null)
Token.blank = property(_token_blank)
Token.numericp = _token_numeric
Token.nullp = _token_null
Token.blankp = _token_blank
Token.to_i = _token_to_i
Token.to_f = _token_to_f
Token.rev = property(lambda self: self.revision)

_TOKEN_PATTERN = re.compile(
    r"(?P<numeric>\d+)"
    # Single-letter aliases are only pre-release tokens when followed by a
    # revision: ``a4`` is alpha4, but a bare ``a`` is a post-release letter.
    r"|(?P<shortpre>[ab])(?P<shortrev>\d+)"
    r"|(?P<pre>preview|alpha|beta|post|pre|rc|p)(?P<revision>\d*)"
    r"|(?P<letter>[A-Za-z]+)",
    re.IGNORECASE,
)

_PRE_TOKEN_CLASSES = {
    "alpha": AlphaToken,
    "beta": BetaToken,
    "pre": PreToken,
    "rc": RcToken,
    "p": PToken,
    "post": PostToken,
}


def _version_tokens(version: str) -> list[Token]:
    tokens: list[Token] = []
    for match in _TOKEN_PATTERN.finditer(version):
        if match.group("numeric") is not None:
            tokens.append(NumericToken(int(match.group("numeric"))))
        elif match.group("shortpre") is not None:
            word = match.group("shortpre").lower()
            token_class = AlphaToken if word == "a" else BetaToken
            tokens.append(token_class(word, int(match.group("shortrev") or 0)))
        elif match.group("pre") is not None:
            word = match.group("pre").lower()
            if word == "preview":
                word = "pre"
            token_class = _PRE_TOKEN_CLASSES[word]
            tokens.append(token_class(word, int(match.group("revision") or 0)))
        else:
            tokens.append(LetterToken(match.group("letter").lower()))
    return tokens


def _homebrew_compare_token_lists(left: list[Token], right: list[Token]) -> int:
    """Homebrew's token loop, including its null/zero-skipping behavior.

    A numeric zero compared with a non-numeric token is skipped (both sides),
    so e.g. ``1.0.0`` compares equal to ``1.0-alpha`` in Homebrew's bounded
    table.  The loop also treats a missing token as NullToken.
    """

    length = max(len(left), len(right))
    left_index = 0
    right_index = 0
    while left_index < length:
        left_token = left[left_index] if left_index < len(left) else NullToken()
        right_token = right[right_index] if right_index < len(right) else NullToken()
        comparison = left_token.compare(right_token)
        if comparison == 0:
            left_index += 1
            right_index += 1
            continue
        if isinstance(left_token, NumericToken) and not isinstance(
            right_token, NumericToken
        ):
            if left_token.compare(NullToken()) > 0:
                return 1
            left_index += 1
            continue
        if not isinstance(left_token, NumericToken) and isinstance(
            right_token, NumericToken
        ):
            if right_token.compare(NullToken()) > 0:
                return -1
            right_index += 1
            continue
        return comparison
    return 0


_HOMEBREW_TRUTHY = {"1", "true", "yes", "on"}
_HOMEBREW_FALSEY = {"0", "false", "no", "off"}


def _legacy_mode_enabled() -> bool:
    """Whether the legacy extra-numeric comparison is explicitly requested."""

    raw = os.environ.get("PYBREW_VERSION_LEGACY", "").strip().lower()
    if raw in _HOMEBREW_TRUTHY:
        return True
    if raw in _HOMEBREW_FALSEY:
        return False
    return bool(getattr(Version, "legacy", False))


def _homebrew_mode_enabled() -> bool:
    """Whether Homebrew null-token comparison is active (now the default)."""

    if _legacy_mode_enabled():
        return False
    raw = os.environ.get("PYBREW_VERSION_HOMEBREW", "").strip().lower()
    if raw in _HOMEBREW_TRUTHY:
        return True
    if raw in _HOMEBREW_FALSEY:
        return False
    homebrew = getattr(Version, "homebrew", None)
    if homebrew is True:
        return True
    if homebrew is False:
        return False
    return True


def compare_versions(
    left: str,
    right: str,
    *,
    missing_numeric_zero: Optional[bool] = None,
) -> int:
    """Return -1/0/1 for natural ordering of two version strings.

    The default is Homebrew's null-token behavior: a missing token is a null
    token, numeric zero compares equal to it, and pre-release words sort before
    the release while ``p``/``post`` (and unknown letters) sort after it.

    ``missing_numeric_zero=False`` forces the historical pybrew behavior
    (extra numeric segments significant: ``1.0.0 > 1.0``; trailing letters are
    pre-release markers).  Passing ``None`` (the default) uses the active mode:
    legacy when ``PYBREW_VERSION_LEGACY=1``/``Version.legacy = True`` (or
    ``PYBREW_VERSION_HOMEBREW=0``/``Version.homebrew = False``), otherwise
    Homebrew.
    """

    # Homebrew treats HEAD (with or without a commit suffix) as newer than
    # every numeric version, but all HEAD spellings compare equal.
    left_head = left == "HEAD" or left.startswith("HEAD-")
    right_head = right == "HEAD" or right.startswith("HEAD-")
    if left_head or right_head:
        if left_head and right_head:
            return 0
        return 1 if left_head else -1

    if missing_numeric_zero is None:
        missing_numeric_zero = _homebrew_mode_enabled()
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

    Homebrew null-token comparison (``1.0 == 1.0.0``) is the default; the
    legacy extra-numeric behavior is an explicit opt-out via
    ``Version.legacy = True`` or ``PYBREW_VERSION_LEGACY=1`` (also
    ``Version.homebrew = False`` / ``PYBREW_VERSION_HOMEBREW=0``).

    Bounded Homebrew-like token classes are exposed via :attr:`tokens`
    (NumericToken, AlphaToken, BetaToken, PreToken, RcToken, PToken,
    PostToken, LetterToken, NullToken); :attr:`token_classes` returns their
    names and :attr:`segments` keeps the raw int/str values.

    Intentional deviations from Homebrew's parser:

    * pre-release words normalize ``preview`` to ``pre`` and carry a numeric
      revision on the token (``alpha5`` is one AlphaToken with revision 5);
    * unknown words map to a single LetterToken;
    * ``revision`` is always 0 on ``Version`` and meaningful only on
      :class:`PkgVersion`.

    Hashes follow the active mode: Homebrew mode uses a canonical key (equal
    versions hash equally), legacy mode keeps the String-compatible hash.
    Configure the mode before using versions as dict/set keys.
    """

    __slots__ = ("value",)

    #: Optional explicit Homebrew flag (``None`` means use the default).
    #: The environment variables ``PYBREW_VERSION_HOMEBREW`` and
    #: ``PYBREW_VERSION_LEGACY`` override this.
    homebrew: Optional[bool] = None

    #: Explicit legacy opt-out; ``PYBREW_VERSION_LEGACY=1`` also sets it.
    legacy: bool = False

    def __init__(self, value: str):
        self.value = str(value)

    @classmethod
    @contextmanager
    def homebrew_mode(cls, enabled: bool = True):
        """Temporarily enable/disable Homebrew null-token comparison."""

        previous = cls.homebrew
        cls.homebrew = enabled
        try:
            yield cls
        finally:
            cls.homebrew = previous

    @classmethod
    @contextmanager
    def legacy_mode(cls, enabled: bool = True):
        """Temporarily enable/disable the legacy extra-numeric comparison."""

        previous = cls.legacy
        cls.legacy = enabled
        try:
            yield cls
        finally:
            cls.legacy = previous

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
    def tokens(self) -> list[Token]:
        """Homebrew-style token objects (``segments`` keeps raw values)."""

        return _version_tokens(self.value)

    @property
    def token_classes(self) -> list[str]:
        """Classified tokens: ``numeric``, pre-release word, or ``letter``.

        Composite pre-release tokens are one class (``1.0-alpha2`` ->
        ``["numeric", "numeric", "alpha"]``); raw :attr:`segments` still keep
        the numeric revision separately for String compatibility.
        """

        return [token.class_name for token in self.tokens]

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

    def _homebrew_hash_key(self) -> tuple:
        # Equal versions must hash equally even with Homebrew's zero-skipping:
        # use the leading numeric prefix with trailing zeros removed, so
        # 1.0.0 and 1.0-alpha (equal by the bounded table) share a key.
        prefix: list[int] = []
        for token in _version_tokens(self.value):
            if not isinstance(token, NumericToken):
                break
            prefix.append(token.value)
        while prefix and prefix[-1] == 0:
            prefix.pop()
        return tuple(prefix)

    def __hash__(self) -> int:
        # Equal versions must hash equally in every mode: Homebrew uses the
        # canonical numeric prefix, legacy uses its raw token list.
        if _homebrew_mode_enabled():
            key = self._homebrew_hash_key()
        else:
            key = tuple(_tokens(self.value))
        return hash((key, 0))


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
    def tokens(self) -> list[Token]:
        return self._version.tokens

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

    def __hash__(self) -> int:
        if _homebrew_mode_enabled():
            key = self._version._homebrew_hash_key()
        else:
            key = tuple(_tokens(self._version.value))
        return hash((key, self._revision))
