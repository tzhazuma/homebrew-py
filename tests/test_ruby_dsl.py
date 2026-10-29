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


# ---------------------------------------------------------------------------
# expanded shim APIs
# ---------------------------------------------------------------------------


def test_bridge_utils_shellwords_system_options_and_env_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    monkeypatch.setenv("PYBREW_SHIM_TEST_ENV", "from-parent")
    build_root = tmp_path / "build"
    build_root.mkdir()
    formula = _formula(
        "ruby-system",
        "1.0",
        (
            '  (prefix/"shellwords").write Shellwords.escape("hello world")\n'
            '  Utils.safe_system("sh", "-c", "printf safe > safe.txt")\n'
            '  quiet_ok = Utils.quiet_system("sh", "-c", "exit 0")\n'
            '  quiet_bad = Utils.quiet_system("sh", "-c", "exit 3")\n'
            '  (prefix/"system").write "ok=#{quiet_ok} bad=#{quiet_bad}"\n'
            '  (prefix/"env").write ENV.fetch("PYBREW_SHIM_TEST_ENV") + "/" +\n'
            '    ENV.fetch("PYBREW_SHIM_MISSING", "default") + "/" +\n'
            '    ENV.fetch("PYBREW_SHIM_MISSING") { "block-default" }\n'
            '  system "sh", "-c", "printf %s \\"$SHIM_CHILD_ENV\\" > child.txt",\n'
            '    env: { "SHIM_CHILD_ENV" => "child-env" }\n'
            '  (prefix/"child").write File.read("child.txt")\n'
            '  (buildpath/"subdir").mkdir\n'
            '  system "sh", "-c", "pwd > where.txt", chdir: "subdir"\n'
            '  (prefix/"chdir").write File.read("subdir/where.txt")\n'
            '  system({ "SHIM_POSITIONAL" => "positional" }, "sh", "-c",\n'
            '    "printf %s \\"$SHIM_POSITIONAL\\" > positional.txt")\n'
            '  (prefix/"positional").write File.read("positional.txt")\n'
            '  bin.install "safe.txt"\n'
        ),
    )

    keg = _build(tmp_path, formula, build_root=build_root)

    assert (keg / "shellwords").read_text() == "hello\\ world"
    assert (keg / "system").read_text() == "ok=true bad=false"
    assert (keg / "env").read_text() == "from-parent/default/block-default"
    assert (keg / "child").read_text() == "child-env"
    assert Path((keg / "chdir").read_text().strip()).resolve() == (
        build_root / "subdir"
    ).resolve()
    assert (keg / "positional").read_text() == "positional"
    assert (keg / "bin" / "safe.txt").read_text() == "safe"


def test_utils_safe_system_failure_raises_build_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-safe-fail",
        "1.0",
        (
            '  Utils.safe_system("sh", "-c", "exit 4")\n'
            '  (prefix/"never").write "x"\n'
        ),
    )

    with pytest.raises(BuildError, match="Failed executing"):
        _build(tmp_path, formula)

    assert not (tmp_path / "keg" / "never").exists()


def test_bridge_inreplace_block_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    build_root = tmp_path / "build"
    build_root.mkdir()
    (build_root / "Makefile").write_text(
        "PREFIX = /usr/local\nCFLAGS = -O2\nLDFLAGS = -lm\nall: build\n"
    )
    formula = _formula(
        "ruby-inreplace",
        "1.0",
        (
            '  inreplace "Makefile" do |s|\n'
            '    s.gsub! "/usr/local", prefix.to_s\n'
            '    s.change_make_var! "CFLAGS", "-O3"\n'
            '    s.remove_make_var! "LDFLAGS"\n'
            '    s.sub! "all: build", "all: patched"\n'
            '    (prefix/"makevar").write s.get_make_var("PREFIX")\n'
            "  end\n"
            '  prefix.install "Makefile"\n'
        ),
    )

    keg = _build(tmp_path, formula, build_root=build_root)

    text = (keg / "Makefile").read_text()
    assert f"PREFIX = {keg}" in text
    assert "CFLAGS=-O3" in text
    assert "LDFLAGS" not in text
    assert "all: patched" in text
    assert (keg / "makevar").read_text() == str(keg)


def test_bridge_macos_os_versions_and_platform_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "14.5")
    formula = _formula(
        "ruby-versions",
        "1.0",
        (
            '  (prefix/"macos").write '
            '"#{MacOS.version}|#{MacOS.version_symbol}|#{MacOS.full_version}"\n'
            '  (prefix/"os").write OS.version.to_s\n'
            '  (prefix/"compare").write "#{MacOS.version >= :ventura}/'
            '#{MacOS.version < :sequoia}/#{MacOS.version == :sonoma}"\n'
            "  on_sonoma do\n"
            '    (prefix/"sonoma").write "yes"\n'
            "  end\n"
            "  on_ventura do\n"
            '    (prefix/"ventura").write "yes"\n'
            "  end\n"
            "  on_sequoia do\n"
            '    (prefix/"sequoia").write "yes"\n'
            "  end\n"
            "  on_monterey :or_older do\n"
            '    (prefix/"monterey-or-older").write "yes"\n'
            "  end\n"
            "  on_sonoma :or_newer do\n"
            '    (prefix/"sonoma-or-newer").write "yes"\n'
            "  end\n"
        ),
    )

    keg = _build(tmp_path, formula)

    assert (keg / "macos").read_text() == "14|sonoma|14.5"
    assert (keg / "compare").read_text() == "true/true/true"
    if is_macos():
        assert (keg / "os").read_text() == "14"
        assert (keg / "sonoma").read_text() == "yes"
        assert (keg / "sonoma-or-newer").read_text() == "yes"
        assert not (keg / "ventura").exists()
        assert not (keg / "sequoia").exists()
        assert not (keg / "monterey-or-older").exists()
    else:
        assert (keg / "os").read_text() == platform.release()
        assert not (keg / "sonoma").exists()
        assert not (keg / "sonoma-or-newer").exists()


def test_bridge_opt_paths_buildpath_and_hardware_cores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    build_root = tmp_path / "build"
    build_root.mkdir()
    formula = _formula(
        "ruby-opt",
        "1.0",
        (
            '  (prefix/"paths").write [opt_prefix, buildpath, formula_opt, '
            'formula_opt_prefix(name)].join("|")\n'
            '  (prefix/"cores").write '
            '"#{Hardware::CPU.cores}|#{Hardware::CPU.physical_cores}"\n'
        ),
    )

    keg = _build(tmp_path, formula, build_root=build_root)

    expected_opt = tmp_path / "prefix" / "opt" / "ruby-opt"
    parts = (keg / "paths").read_text().split("|")
    assert parts == [
        str(expected_opt),
        str(build_root),
        str(expected_opt),
        str(expected_opt),
    ]
    cores, physical = (int(value) for value in (keg / "cores").read_text().split("|"))
    assert cores >= 1
    assert 1 <= physical <= cores


def test_macos_version_host_fallback_or_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    monkeypatch.delenv("HOMEBREW_MACOS_VERSION", raising=False)
    formula = _formula(
        "ruby-host-version",
        "1.0",
        (
            '  (prefix/"macos").write '
            '"#{MacOS.version}|#{MacOS.full_version}|#{OS.version}"\n'
        ),
    )

    if not is_macos():
        with pytest.raises(BuildError, match="not available on this host"):
            _build(tmp_path, formula)
        return

    keg = _build(tmp_path, formula)
    full = platform.mac_ver()[0]
    assert full
    major = int(full.split(".")[0])
    stripped = str(major) if major >= 11 else ".".join(full.split(".")[:2])
    assert (keg / "macos").read_text() == f"{stripped}|{full}|{stripped}"


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
