from __future__ import annotations

import pytest

from pybrew.version import (
    AlphaToken,
    BetaToken,
    LetterToken,
    NullToken,
    NumericToken,
    PkgVersion,
    PostToken,
    PToken,
    PreToken,
    RcToken,
    Token,
    Version,
    compare_versions,
    detect_version,
)


@pytest.fixture(autouse=True)
def _reset_homebrew_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYBREW_VERSION_HOMEBREW", raising=False)
    monkeypatch.delenv("PYBREW_VERSION_LEGACY", raising=False)
    monkeypatch.setattr(Version, "homebrew", None, raising=False)
    monkeypatch.setattr(Version, "legacy", False, raising=False)
    yield


def test_detect_common_source_urls() -> None:
    assert detect_version("https://ftp.gnu.org/hello/hello-2.12.3.tar.gz") == "2.12.3"
    assert detect_version("https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-1.8.2.tar.gz") == "1.8.2"
    assert detect_version("https://www.python.org/ftp/python/3.12.16/Python-3.12.16.tgz") == "3.12.16"
    assert detect_version("https://github.com/abseil/abseil-cpp/archive/refs/tags/20260817.0.tar.gz") == "20260817.0"
    assert detect_version("https://example.com/v3.15.2.tar.gz") == "3.15.2"


def test_detect_sqlite_autoconf_version() -> None:
    assert detect_version("https://sqlite.org/2026/sqlite-autoconf-3510000.tar.gz") == "3.51.0"


def test_detect_git_tag() -> None:
    assert detect_version("v3.15.2") == "3.15.2"


def test_version_comparison() -> None:
    assert compare_versions("1.10", "1.9") > 0
    assert compare_versions("2.0", "2.0") == 0
    assert compare_versions("1.0.0", "1.0") == 0
    assert compare_versions("1.0.0", "1.0", missing_numeric_zero=False) > 0
    assert Version("5.3.20") > Version("5.3.9")
    assert Version("20260817.0") > Version("20260801.0")


def test_version_numeric_and_letter_segments() -> None:
    assert compare_versions("1.10", "1.9") > 0
    assert compare_versions("1.9", "1.10") < 0
    assert compare_versions("1.0a", "1.0b") < 0
    assert compare_versions("1.0a", "1.0a") == 0
    assert compare_versions("2.0_1", "2.0") > 0


def test_version_string_compatibility() -> None:
    version = Version("2026.11.04")

    assert str(version) == "2026.11.04"
    assert version == "2026.11.04"
    assert version == Version("2026.11.04")
    assert Version("1.10") > "1.9"
    assert sorted([Version("1.10"), Version("1.9"), Version("1.2")]) == [
        Version("1.2"),
        Version("1.9"),
        Version("1.10"),
    ]


def test_version_token_parser() -> None:
    version = Version("1.2.3-beta")

    assert version.segments == [1, 2, 3, "beta"]
    assert (version.major, version.minor, version.patch) == (1, 2, 3)
    assert str(version.major_minor) == "1.2"
    assert str(version.major_minor_patch) == "1.2.3"
    assert compare_versions("1.0", "1.0-beta") > 0
    assert compare_versions("1.0-beta", "1.0") < 0


def test_pkg_version_revision_behavior() -> None:
    pkg = PkgVersion.parse("1.2.3_2")

    assert pkg.version == Version("1.2.3")
    assert pkg.revision == 2
    assert str(pkg) == "1.2.3_2"
    assert pkg.segments == [1, 2, 3]
    assert (pkg.major, pkg.minor, pkg.patch) == (1, 2, 3)
    assert pkg > PkgVersion.parse("1.2.3_1")
    assert pkg > "1.2.3_1"
    assert pkg < "1.2.4"
    assert pkg == "1.2.3_2"
    assert PkgVersion.parse("1.2.3").revision == 0
    assert str(PkgVersion("1.2.3", 0)) == "1.2.3"


def test_version_prerelease_and_token_semantics() -> None:
    alpha = Version("1.0-alpha")
    beta = Version("1.0-beta")
    pre = Version("1.0-pre")
    rc = Version("1.0-rc1")
    patch = Version("1.0-p1")

    assert alpha.tokens == [1, 0, "alpha"]
    assert (alpha.major, alpha.minor, alpha.patch) == (1, 0, 0)
    assert alpha.pre_release is True
    assert alpha.pre_release_tokens == ["alpha"]
    assert Version("1.0").pre_release is False
    assert alpha < beta < pre < rc < Version("1.0") < patch
    assert Version("1.0-beta2") < Version("1.0-beta10")
    assert Version("1.0-post1") < Version("1.0-p1")
    assert Version("1.0.0") == Version("1.0")
    assert alpha.revision == 0
    assert PkgVersion("1.0-alpha", 3).pre_release is True
    assert PkgVersion.parse("1.0-alpha_2").revision == 2
    assert PkgVersion.parse("1.0-alpha_2").pre_release_tokens == ["alpha"]
    assert Version("1.0") == PkgVersion("1.0", 0)
    assert Version("1.0") != PkgVersion("1.0", 2)


def test_version_token_classes_and_homebrew_missing_zero() -> None:
    assert Version("1.0-alpha2").token_classes == ["numeric", "numeric", "alpha"]
    assert Version("1.0-p1").token_classes == ["numeric", "numeric", "p"]
    assert Version("1.0-foo").token_classes == ["numeric", "numeric", "letter"]
    assert Version("1.0-preview").token_classes == ["numeric", "numeric", "pre"]
    assert PkgVersion.parse("1.0-alpha_2").token_classes == [
        "numeric",
        "numeric",
        "alpha",
    ]

    assert compare_versions("1.0", "1.0.0", missing_numeric_zero=True) == 0
    assert compare_versions("1.0", "1.0.1", missing_numeric_zero=True) < 0
    assert compare_versions("1.0", "1.0-alpha", missing_numeric_zero=True) > 0
    assert compare_versions("1.0", "1.0-p1", missing_numeric_zero=True) < 0
    assert compare_versions("1.0", "1.0-foo", missing_numeric_zero=True) < 0
    assert compare_versions("1.0.0", "1.0") == 0
    assert compare_versions("1.0.0", "1.0", missing_numeric_zero=False) > 0

    assert Version("1.0").homebrew_compare("1.0.0") == 0
    assert Version("1.0").homebrew_compare("1.0.1") < 0
    assert Version("1.0").homebrew_compare("1.0-alpha") > 0
    assert Version("1.0").homebrew_compare("1.0-p1") < 0
    assert Version("1.0").homebrew_compare("1.0-foo") < 0
    assert Version("1.0").homebrew_compare(PkgVersion("1.0", 2)) < 0
    assert PkgVersion("1.0", 2).homebrew_compare("1.0") > 0
    assert PkgVersion("1.0", 2).homebrew_compare("1.0_2") == 0


def test_version_string_and_hash_contract() -> None:
    version = Version("1.2.3")

    assert str(version) == "1.2.3"
    assert version.to_s() == "1.2.3"
    assert version == "1.2.3"
    assert hash(Version("1.2.3")) == hash(Version("1.2.3"))
    assert Version("1.0") == Version("1.0")
    assert Version("1.0") == Version("1.0.0")
    assert hash(Version("1.0")) == hash(Version("1.0.0"))
    assert hash(Version("1.2.3")) != hash(Version("1.2.4"))
    assert PkgVersion("1.0", 0) == Version("1.0.0")
    assert hash(PkgVersion("1.0", 0)) == hash(Version("1.0.0"))
    assert PkgVersion("1.0", 2) != Version("1.0")
    assert hash(PkgVersion("1.0", 2)) != hash(PkgVersion("1.0", 0))


def test_homebrew_mode_env_null_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # Null-token behavior is the default now.
    assert compare_versions("1.0.0", "1.0") == 0
    assert Version("1.0") == Version("1.0.0")
    assert Version("1.0") == "1.0.0"
    assert Version("1.0") != Version("1.0.1")
    assert Version("1.0") > Version("1.0-alpha")
    assert Version("1.0") < Version("1.0-p1")
    assert Version("1.0") < Version("1.0-foo")
    assert hash(Version("1.0")) == hash(Version("1.0.0"))

    monkeypatch.setenv("PYBREW_VERSION_HOMEBREW", "1")
    assert compare_versions("1.0.0", "1.0") == 0
    assert compare_versions("1.0.0", "1.0", missing_numeric_zero=False) > 0
    assert PkgVersion("1.0", 0) == Version("1.0.0")
    assert hash(PkgVersion("1.0", 0)) == hash(Version("1.0.0"))
    assert PkgVersion("1.0", 2) != Version("1.0")
    assert hash(PkgVersion("1.0", 2)) != hash(PkgVersion("1.0", 0))

    monkeypatch.delenv("PYBREW_VERSION_HOMEBREW")
    monkeypatch.setenv("PYBREW_VERSION_LEGACY", "1")
    assert compare_versions("1.0.0", "1.0") > 0
    assert Version("1.0") != Version("1.0.0")
    assert Version("1.0-p1") < Version("1.0")
    assert compare_versions("1.0.0", "1.0", missing_numeric_zero=True) == 0
    assert Version("1.0") == Version("01.0")
    assert hash(Version("1.0")) == hash(Version("01.0"))
    assert hash(PkgVersion("1.0", 0)) == hash(Version("1.0"))


def test_homebrew_mode_class_flag_and_context() -> None:
    assert Version("1.0") == Version("1.0.0")
    Version.legacy = True
    try:
        assert Version("1.0") != Version("1.0.0")
    finally:
        Version.legacy = False
    with Version.legacy_mode():
        assert Version("1.0") != Version("1.0.0")
    Version.homebrew = False
    try:
        assert Version("1.0") != Version("1.0.0")
    finally:
        Version.homebrew = None
    with Version.homebrew_mode(False):
        assert Version("1.0") != Version("1.0.0")
    assert Version("1.0") == Version("1.0.0")


def test_homebrew_mode_env_false_overrides_class_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Version.homebrew = True
    monkeypatch.setenv("PYBREW_VERSION_HOMEBREW", "0")
    assert Version("1.0") != Version("1.0.0")


def test_version_token_hierarchy_and_precedence() -> None:
    tokens = Version("1.0-alpha2").tokens

    assert [type(token) for token in tokens] == [NumericToken, NumericToken, AlphaToken]
    assert tokens[2].value == "alpha"
    assert tokens[2].revision == 2
    assert tokens[2].class_name == "alpha"
    assert Version("1.0-p1").tokens[2].class_name == "p"
    assert Version("1.0-post1").tokens[2].class_name == "post"
    assert Version("1.0-foo").tokens[2].class_name == "letter"
    assert isinstance(NullToken(), Token)
    assert PkgVersion.parse("1.0-alpha_2").revision == 2
    assert PkgVersion("1.0-alpha2", 2).tokens[2].revision == 2

    assert NumericToken(1) > AlphaToken("alpha")
    assert AlphaToken("alpha") < BetaToken("beta")
    assert BetaToken("beta") < PreToken("pre")
    assert PreToken("pre") < RcToken("rc1")
    assert RcToken("rc1") < PToken("p1")
    assert PostToken("post1") < PToken("p1")
    assert PostToken("post1") < LetterToken("foo")
    assert PToken("p1") > LetterToken("foo")
    assert PToken("p1") < LetterToken("zoo")
    assert PreToken("pre") > LetterToken("foo")
    assert AlphaToken("alpha") < LetterToken("foo")
    assert RcToken("rc1") > LetterToken("foo")
    assert Version("1.0-preview") == Version("1.0-pre")
    assert Version("1.0-preview").token_classes == ["numeric", "numeric", "pre"]
    assert AlphaToken("alpha", 0) == AlphaToken("alpha")
    assert hash(AlphaToken("alpha", 0)) == hash(AlphaToken("alpha"))
    assert PToken("p", 0) == LetterToken("p")
    assert hash(PToken("p", 0)) == hash(LetterToken("p"))
    assert NullToken() == NumericToken(0)
    assert NullToken() < NumericToken(1)
    assert NullToken() > AlphaToken("alpha")
    assert NullToken() < PToken("p1")
    assert NullToken() < LetterToken("foo")
    assert hash(NullToken()) == hash(NumericToken(0))
    assert hash(AlphaToken("alpha", 1)) != hash(AlphaToken("alpha", 2))
    assert hash(PkgVersion("1.0", 2)) != hash(PkgVersion("1.0", 0))


def test_version_homebrew_bounded_table_and_hash_consistency() -> None:
    versions = [
        "1.0",
        "1.0.0",
        "1.0-alpha",
        "1.0-p1",
        "1.0-post1",
        "1.0-foo",
        "1.0.1",
    ]
    expected = [
        [0, 0, 1, -1, -1, -1, -1],
        [0, 0, 0, 0, 0, 0, -1],
        [-1, -1, 0, -1, -1, -1, -1],
        [1, 1, 1, 0, 1, 1, -1],
        [1, 1, 1, -1, 0, -1, -1],
        [1, 1, 1, -1, 1, 0, -1],
        [1, 1, 1, 1, 1, 1, 0],
    ]
    objects = [Version(value) for value in versions]

    for left_index, left_name in enumerate(versions):
        for right_index, right_name in enumerate(versions):
            comparison = expected[left_index][right_index]
            left = objects[left_index]
            right = objects[right_index]
            assert compare_versions(left_name, right_name) == comparison
            assert (left < right) == (comparison < 0)
            assert (left > right) == (comparison > 0)
            assert (left == right) == (comparison == 0)
            if comparison == 0:
                assert hash(left) == hash(right)


def test_version_legacy_hash_consistency() -> None:
    with Version.legacy_mode():
        assert compare_versions("1.0.0", "1.0") > 0
        assert Version("1.0.0") > Version("1.0")
        assert Version("1.0-p1") < Version("1.0")
        assert Version("1.0") == Version("01.0")
        assert hash(Version("1.0")) == hash(Version("01.0"))
        assert hash(PkgVersion("1.0", 0)) == hash(Version("1.0"))


def test_token_hierarchy_factory_and_predicates() -> None:
    from pybrew.version import (
        NULL_TOKEN,
        AlphaToken,
        CompositeToken,
        LetterToken,
        NumericToken,
        StringToken,
        Token,
    )

    assert issubclass(AlphaToken, StringToken)
    assert issubclass(CompositeToken, StringToken)
    alpha = Token.create("alpha5")
    assert isinstance(alpha, AlphaToken)
    assert alpha.rev == 5
    assert alpha.to_s() == "alpha5"
    assert Token.create(3).numeric
    assert Token.create(None) is NULL_TOKEN
    assert NULL_TOKEN.null and NULL_TOKEN.blank
    assert NumericToken(12).to_i() == 12
    assert NumericToken(12).to_f() == 12.0
    assert LetterToken("x").to_i() == 0
    composite = CompositeToken([NumericToken(1), LetterToken("a")])
    assert composite.to_s() == "1a"
    assert isinstance(composite, StringToken)


def test_homebrew_short_prerelease_aliases_and_head() -> None:
    from pybrew.version import AlphaToken, Version, compare_versions

    assert compare_versions("1.2.3alpha4", "1.2.3a4") == 0
    assert compare_versions("1.2.3beta2", "1.2.3b2") == 0
    assert compare_versions("1.2.3", "1.2.3a") == -1
    assert compare_versions("1.2.3a4", "1.2.3") == -1
    assert compare_versions("1.2.3alpha4", "1.2.3beta2") == -1
    assert compare_versions("1.2.3beta2", "1.2.3rc3") == -1
    assert compare_versions("1.2.3", "1.2.3-p34") == -1
    assert compare_versions("HEAD", "1.2.3") == 1
    assert compare_versions("1.2.3", "HEAD-fedcba") == -1
    assert compare_versions("HEAD-abcdef", "HEAD-fedcba") == 0
    tokens = Version("1.2.3a4").tokens
    assert isinstance(tokens[-1], AlphaToken)
    assert tokens[-1].revision == 4


def test_version_short_alias_and_head_edges() -> None:
    assert compare_versions("1.2.3a4", "1.2.3alpha4") == 0
    assert compare_versions("1.2.3b2", "1.2.3beta2") == 0
    assert compare_versions("1.2.3a4", "1.2.3") < 0
    assert compare_versions("1.2.3", "1.2.3b2") > 0
    assert compare_versions("1.2.3-p34", "1.2.3") > 0
    assert Version("1.2.3a4") == Version("1.2.3alpha4")
    assert Version("1.2.3b2") == Version("1.2.3beta2")
    assert hash(Version("1.2.3a4")) == hash(Version("1.2.3alpha4"))
    assert Version("1.2.3a4").tokens[-1].class_name == "alpha"
    assert Version("1.2.3b2").tokens[-1].class_name == "beta"
    assert Version("1.2.3a4").tokens[-1].revision == 4
    assert Version("HEAD") > Version("1.2.3")
    assert Version("HEAD-fedcba") > Version("1.2.3")
    assert Version("1.2.3") < Version("HEAD")
    assert Version("HEAD-abcdef") == Version("HEAD-fedcba")
    assert hash(Version("HEAD")) == hash(Version("HEAD-abc"))
    assert PkgVersion.parse("HEAD_2") > Version("1.2.3")


def test_short_alias_token_hash_is_canonical() -> None:
    from pybrew.version import AlphaToken, BetaToken

    assert AlphaToken("a", 4) == AlphaToken("alpha", 4)
    assert hash(AlphaToken("a", 4)) == hash(AlphaToken("alpha", 4))
    assert BetaToken("b", 2) == BetaToken("beta", 2)
    assert hash(BetaToken("b", 2)) == hash(BetaToken("beta", 2))
