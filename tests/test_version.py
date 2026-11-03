from __future__ import annotations

from pybrew.version import PkgVersion, Version, compare_versions, detect_version


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
    assert compare_versions("1.0.0", "1.0") > 0
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
