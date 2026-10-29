from __future__ import annotations

import pytest

from pybrew.errors import FormulaParseError
from pybrew.ruby_parser import parse_formula_source

SAMPLE = r'''
# A representative formula covering the declarative DSL.
class Foo < Formula
  desc "Example formula"
  homepage "https://example.com/foo"
  url "https://example.com/foo-1.2.3.tar.gz"
  sha256 "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  license any_of: ["MIT", "Apache-2.0"]
  revision 2
  version_scheme 1
  keg_only :versioned_formula

  livecheck do
    url :stable
    regex(/^v?(\d+(?:\.\d+)+)$/i)
  end

  bottle do
    root_url "https://ghcr.io/v2/homebrew/core"
    rebuild 3
    sha256 cellar: :any, arm64_golden_gate: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    sha256 cellar: :any_skip_relocation, sonoma: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  end

  depends_on "openssl@3"
  depends_on "cmake" => :build
  depends_on "check" => [:build, :test]
  uses_from_macos "zlib"
  uses_from_macos "expat", since: :sequoia

  head do
    url "https://example.com/foo.git", branch: "main"
    depends_on "autoconf" => :build
  end

  def install
    bin.install "foo"
    prefix.install "README"
  end

  def caveats
    <<~EOS
      Foo needs a config file.
    EOS
  end

  test do
    assert_equal "foo", shell_output("#{bin}/foo").strip
  end
end
'''


def test_parse_declarative_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("PYBREW_TARGET_ARCH", "arm64")
    formula = parse_formula_source(SAMPLE, name="foo", tap="homebrew/core")
    assert formula.name == "foo"
    assert formula.desc == "Example formula"
    assert formula.homepage == "https://example.com/foo"
    assert formula.version == "1.2.3"
    assert formula.revision == 2
    assert formula.pkg_version == "1.2.3_2"
    assert formula.version_scheme == 1
    assert formula.license == "MIT OR Apache-2.0"
    assert formula.keg_only is True
    assert formula.keg_only_reason == "versioned_formula"
    assert formula.stable.sha256.startswith("0123456789")
    assert formula.bottle.root_url == "https://ghcr.io/v2/homebrew/core"
    assert formula.bottle.rebuild == 3
    assert set(formula.bottle.files) == {"arm64_golden_gate", "sonoma"}
    assert formula.bottle.files["sonoma"].cellar == "any_skip_relocation"
    assert formula.bottle.files["arm64_golden_gate"].sha256 == "a" * 64

    deps = {(dep.name, dep.build, dep.test) for dep in formula.dependencies}
    assert ("openssl@3", False, False) in deps
    assert ("cmake", True, False) in deps
    assert ("check", True, True) in deps
    assert {dep.name for dep in formula.uses_from_macos} == {"zlib", "expat"}
    assert {dep.name for dep in formula.head_dependencies} == {"autoconf"}
    assert "bin.install" in formula.install_source
    assert "Foo needs a config file." in formula.caveats
    assert "shell_output" in formula.test_source


def test_parse_url_tag_becomes_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    source = """
class GitThing < Formula
  desc "thing"
  homepage "https://example.com"
  url "https://example.com/thing.git", tag: "v3.15.2", revision: "abc"
  license "MIT"
end
"""
    formula = parse_formula_source(source, name="git-thing")
    assert formula.version == "3.15.2"
    assert formula.stable.tag == "v3.15.2"


def test_unknown_calls_are_tolerated() -> None:
    source = """
class Odd < Formula
  desc "odd"
  homepage "https://example.com"
  url "https://example.com/odd-1.0.tar.gz"
  sha256 "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  deny_network_access!
  no_autobump! because: :incompatible_version_format
  some_future_dsl :yes, nested: { a: 1 }
end
"""
    formula = parse_formula_source(source, name="odd")
    assert formula.version == "1.0"
    assert formula.deny_network_access is True
    assert "some_future_dsl" in formula.extra


def test_missing_class_is_an_error() -> None:
    with pytest.raises(FormulaParseError):
        parse_formula_source('desc "not a formula"\n', name="bad")


def test_heredoc_and_license_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    source = '''
class AutoconfLike < Formula
  desc "autoconf-like"
  homepage "https://example.com"
  url "https://example.com/autoconf-like-2.73.tar.gz"
  license all_of: [
    "GPL-3.0-or-later",
    "GPL-3.0-or-later" => { with: "Autoconf-exception-3.0" },
  ]
  def caveats
    <<~EOS
      This is a caveat.
    EOS
  end
end
'''
    formula = parse_formula_source(source, name="autoconf-like")
    assert formula.license == "GPL-3.0-or-later AND (GPL-3.0-or-later WITH Autoconf-exception-3.0)"
    assert "This is a caveat." in formula.caveats


def test_oldname_and_alias_stanzas() -> None:
    formula = parse_formula_source(
        '''class Renamed < Formula
  oldname "old-renamed"
  alias "short-renamed"
  url "https://example.com/renamed-1.0.tar.gz"
  sha256 "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
end
''',
        name="renamed",
    )
    assert formula.oldname == "old-renamed"
    assert formula.aliases == ["short-renamed"]
