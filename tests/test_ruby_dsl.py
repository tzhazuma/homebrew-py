"""Tests for the opt-in arbitrary-Ruby install bridge.

The bridge is only active when ``PYBREW_ALLOW_ARBITRARY_RUBY=1``; the default
declarative parser must keep behaving exactly as before.  Every fixture here is
synthetic and local: no network access and no writes outside ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import tarfile
from pathlib import Path

import pytest

from pybrew.errors import BuildError, UnsupportedInstall
from pybrew.installer import InstallOptions, Installer
from pybrew.models import Formula, is_linux, is_macos
from pybrew.paths import Config
from pybrew.source_builder import build_source
from pybrew.taps import FormulaLoader, discover_taps

ALLOW_ENV = "PYBREW_ALLOW_ARBITRARY_RUBY"
RUBY_ENV = "PYBREW_RUBY"
_RUBY = os.environ.get(RUBY_ENV) or "/usr/bin/ruby"
pytestmark = pytest.mark.skipif(
    shutil.which(_RUBY) is None and not Path(_RUBY).is_file(),
    reason="Ruby interpreter not installed; opt-in bridge tests skipped",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _formula(name: str, version: str, body: str) -> Formula:
    formula = Formula(name=name, version=version)
    formula.install_source = "def install\n" + body + "end\n"
    return formula


def _config(tmp_path: Path) -> Config:
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"
    )
    config.ensure()
    return config


def _build(
    tmp_path: Path,
    formula: Formula,
    *,
    build_root: Path | None = None,
    keg: Path | None = None,
    verbose: bool = False,
) -> Path:
    build_root = build_root or tmp_path / "build"
    build_root.mkdir(exist_ok=True)
    keg = keg or tmp_path / "keg"
    keg.mkdir(exist_ok=True)
    build_source(formula, _config(tmp_path), build_root, keg, verbose=verbose)
    return keg


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tar_project(project: Path, destination: Path, top: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as tar:
        for path in sorted(project.rglob("*")):
            arcname = f"{top}/{path.relative_to(project)}"
            info = tarfile.TarInfo(arcname)
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                data = path.read_bytes()
                info.size = len(data)
                info.mode = path.stat().st_mode & 0o777
                tar.addfile(info, io.BytesIO(data))
    return destination


def _make_tap(
    tmp_path: Path, name: str, version: str, project: Path, install_body: str
) -> Path:
    top = f"{name}-{version}"
    archive = _tar_project(project, tmp_path / "src" / f"{top}.tar.gz", top)
    tap = tmp_path / "tap"
    (tap / "Formula").mkdir(parents=True, exist_ok=True)
    (tap / "Formula" / f"{name}.rb").write_text(
        f'''class {name.title().replace("-", "")} < Formula
  desc "ruby bridge fixture"
  homepage "https://example.com/{name}"
  url "{archive.as_uri()}"
  sha256 "{_sha256(archive)}"
  license "MIT"

  def install
{install_body}
  end
end
'''
    )
    return tap


# ---------------------------------------------------------------------------
# opt-in bridge
# ---------------------------------------------------------------------------


def test_arbitrary_ruby_loop_and_interpolation_install_keg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-loop",
        "1.2.3",
        (
            '  words = ["alpha", "beta", "gamma"]\n'
            "  words.each do |word|\n"
            '    name = "tool-#{word}"\n'
            '    (buildpath/name).write "#!/bin/sh\\necho #{word} #{version}\\n"\n'
            "    bin.install name\n"
            "  end\n"
            '  (prefix/"share/ruby").mkdir\n'
            '  (prefix/"share/ruby/build.txt").write "built #{version.major_minor}\\n"\n'
            '  File.write((prefix/"share/ruby/direct.txt"), "direct #{version}\\n")\n'
            '  system "sh", "-c", "printf loop-system > generated.txt"\n'
            '  bin.install "generated.txt"\n'
        ),
    )

    keg = _build(tmp_path, formula)

    for word in ("alpha", "beta", "gamma"):
        installed = keg / "bin" / f"tool-{word}"
        assert installed.is_file()
        assert installed.read_text() == f"#!/bin/sh\necho {word} 1.2.3\n"
    assert (keg / "share" / "ruby" / "build.txt").read_text() == "built 1.2\n"
    assert (keg / "share" / "ruby" / "direct.txt").read_text() == "direct 1.2.3\n"
    assert (keg / "bin" / "generated.txt").read_text() == "loop-system"


def test_arbitrary_ruby_path_objects_symlinks_and_fileutils(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    build_root = tmp_path / "build"
    build_root.mkdir()
    (build_root / "payload").write_text("payload\n")
    formula = _formula(
        "ruby-paths",
        "2.0",
        (
            '  FileUtils.mkdir_p(prefix/"libexec")\n'
            '  Pathname.new(prefix/"libexec/data").write "data-#{version}"\n'
            '  (prefix/"libexec/extra").write "extra"\n'
            '  bin.install_symlink (buildpath/"payload")\n'
            '  (prefix/"share/ruby").mkdir\n'
            '  (prefix/"share/ruby/copied.txt").write "copied"\n'
        ),
    )

    keg = _build(tmp_path, formula, build_root=build_root)

    assert (keg / "libexec" / "data").read_text() == "data-2.0"
    assert (keg / "libexec" / "extra").read_text() == "extra"
    link = keg / "bin" / "payload"
    assert link.is_symlink()
    assert link.read_text() == "payload\n"
    assert (keg / "share" / "ruby" / "copied.txt").read_text() == "copied"


def test_bridge_handles_common_declarative_dsl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    build_root = tmp_path / "build"
    build_root.mkdir()
    (build_root / "hello").write_text("hello\n")
    (build_root / "README").write_text("readme\n")
    formula = _formula(
        "ruby-common",
        "1.0",
        (
            '  inreplace "README", "readme", "patched"\n'
            '  bin.install "hello"\n'
            '  prefix.install "README"\n'
            '  ENV["PYBREW_RUBY_TEST"] = "ruby-env"\n'
            '  system "sh", "-c", "printf \'%s\' \\"$PYBREW_RUBY_TEST\\" > env.txt"\n'
            '  bin.install "env.txt"\n'
            '  ohai "hello from ruby"\n'
            '  opoo "ruby warning"\n'
        ),
    )

    keg = _build(tmp_path, formula, build_root=build_root)

    assert (keg / "bin" / "hello").read_text() == "hello\n"
    assert (keg / "README").read_text() == "patched\n"
    assert (keg / "bin" / "env.txt").read_text() == "ruby-env"


def test_bridge_platform_build_and_args_predicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-predicates",
        "1.0",
        (
            '  (prefix/"host").write "mac=#{OS.mac?} linux=#{OS.linux?}"\n'
            '  (prefix/"cpu").write "arm=#{Hardware::CPU.arm?} intel=#{Hardware::CPU.intel?}"\n'
            '  (prefix/"build").write "head=#{build.head?} stable=#{build.stable?} '
            'with=#{build.with?("x")} without=#{build.without?("x")} '
            'args=#{args.include?("--x")}"\n'
            "  on_macos do\n"
            '    (prefix/"platform").write "macos"\n'
            "  end\n"
            "  on_linux do\n"
            '    (prefix/"platform").write "linux"\n'
            "  end\n"
        ),
    )

    keg = _build(tmp_path, formula)

    host = (keg / "host").read_text()
    assert f"mac={str(is_macos()).lower()}" in host
    assert f"linux={str(is_linux()).lower()}" in host
    arm = platform.machine().lower() in {"arm64", "aarch64"}
    cpu = (keg / "cpu").read_text()
    assert f"arm={str(arm).lower()}" in cpu
    assert f"intel={str(not arm).lower()}" in cpu
    build = (keg / "build").read_text()
    assert "head=false" in build
    assert "stable=true" in build
    assert "with=false" in build
    assert "without=true" in build
    assert "args=false" in build
    expected_platform = "macos" if is_macos() else "linux"
    assert (keg / "platform").read_text() == expected_platform


def test_installer_end_to_end_uses_ruby_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    project = tmp_path / "ruby-bridge-src"
    project.mkdir()
    (project / "payload").write_text("payload\n")
    tap = _make_tap(
        tmp_path,
        "ruby-bridge",
        "1.0",
        project,
        '    names = %w[one two three]\n'
        "    names.each do |entry|\n"
        '      (buildpath/"out-#{entry}").write "generated #{entry}"\n'
        '      bin.install "out-#{entry}"\n'
        "    end\n",
    )
    config = Config.default(prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache")
    config.ensure()
    loader = FormulaLoader(discover_taps([tap]))
    installer = Installer(config, loader)

    results = installer.install(
        loader.load("ruby-bridge"), InstallOptions(build_from_source=True)
    )

    keg = results[-1].keg
    for entry in ("one", "two", "three"):
        assert (keg / "bin" / f"out-{entry}").read_text() == f"generated {entry}"


# ---------------------------------------------------------------------------
# failure reporting
# ---------------------------------------------------------------------------


def test_ruby_failure_reports_stderr_and_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula("ruby-boom", "1.0", '  value = 1\n  raise "boom"\n')

    with pytest.raises(BuildError) as error:
        _build(tmp_path, formula)

    message = str(error.value)
    assert "arbitrary Ruby install failed" in message
    assert "boom" in message
    assert "at def install line 3" in message
    assert 'raise "boom"' in message


def test_pybrew_ruby_override_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    fake_ruby = tmp_path / "fake-ruby"
    fake_ruby.write_text('#!/bin/sh\necho "fake ruby invoked" >&2\nexit 7\n')
    fake_ruby.chmod(0o755)
    monkeypatch.setenv(RUBY_ENV, str(fake_ruby))
    formula = _formula("ruby-override", "1.0", '  (prefix/"x").write "x"\n')

    with pytest.raises(BuildError) as error:
        _build(tmp_path, formula)

    message = str(error.value)
    assert "exit 7" in message
    assert "fake ruby invoked" in message


def test_missing_ruby_reports_build_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    monkeypatch.setenv(RUBY_ENV, str(tmp_path / "no-such-ruby"))
    formula = _formula("ruby-missing", "1.0", '  (prefix/"x").write "x"\n')

    with pytest.raises(BuildError, match="Ruby interpreter not found"):
        _build(tmp_path, formula)


def test_empty_ruby_install_body_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula("ruby-empty", "1.0", "")

    with pytest.raises(UnsupportedInstall, match="install block is empty"):
        _build(tmp_path, formula)


# ---------------------------------------------------------------------------
# default path must be unchanged
# ---------------------------------------------------------------------------


def test_default_parser_rejects_arbitrary_ruby(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    formula = _formula(
        "ruby-default", "1.0", '  ["a", "b"].each { |x| (prefix/x).write x }\n'
    )

    with pytest.raises(UnsupportedInstall, match="unsupported"):
        _build(tmp_path, formula)

    assert not any((tmp_path / "keg").iterdir())


@pytest.mark.parametrize("value", [None, "0", "true", "yes"])
def test_only_exact_one_enables_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(ALLOW_ENV, raising=False)
    else:
        monkeypatch.setenv(ALLOW_ENV, value)
    formula = _formula("ruby-gate", "1.0", '  ["a"].each { |x| (prefix/x).write x }\n')

    with pytest.raises(UnsupportedInstall, match="unsupported"):
        _build(tmp_path, formula)


def test_default_parser_still_installs_declarative_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    build_root = tmp_path / "build"
    build_root.mkdir()
    (build_root / "hello").write_text("hello\n")
    formula = _formula("ruby-default-ok", "1.0", '  bin.install "hello"\n')

    keg = _build(tmp_path, formula, build_root=build_root)

    assert (keg / "bin" / "hello").read_text() == "hello\n"
