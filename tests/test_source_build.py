from __future__ import annotations

import hashlib
import io
import os
import shlex
import tarfile
from pathlib import Path

import pytest

from pybrew.installer import InstallOptions, Installer
from pybrew.models import Formula
from pybrew.paths import Config
from pybrew.taps import FormulaLoader, discover_taps


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tar(source_dir: Path, destination: Path, top: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as tar:
        for path in sorted(source_dir.rglob("*")):
            arcname = f"{top}/{path.relative_to(source_dir)}"
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


def _write_project(files: dict[str, str]) -> Path:
    # helper used only inside a temporary directory
    raise NotImplementedError


def _make_tap(tmp_path: Path, name: str, version: str, project: Path, install_block: str) -> tuple[Path, Path]:
    top = f"{name}-{version}"
    archive = tmp_path / "src" / f"{top}.tar.gz"
    _tar(project, archive, top)
    tap = tmp_path / "tap"
    (tap / "Formula").mkdir(parents=True, exist_ok=True)
    ruby = f'''class {name.title().replace("-", "")} < Formula
  desc "source build fixture"
  homepage "https://example.com/{name}"
  url "{archive.as_uri()}"
  sha256 "{_sha(archive)}"
  license "MIT"

  def install
{install_block}
  end
end
'''
    formula_path = tap / "Formula" / f"{name}.rb"
    formula_path.write_text(ruby)
    return tap, archive


def _install(tmp_path: Path, tap: Path, name: str):
    config = Config.default(prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache")
    config.ensure()
    loader = FormulaLoader(discover_taps([tap]))
    formula = loader.load(name)
    installer = Installer(config, loader)
    results = installer.install(formula, InstallOptions(build_from_source=True))
    return config, results[-1]


@pytest.mark.skipif(
    __import__("shutil").which("make") is None or __import__("shutil").which("cc") is None,
    reason="make/cc not installed",
)
def test_autotools_source_build_with_inreplace(tmp_path: Path) -> None:
    project = tmp_path / "hello-src"
    project.mkdir()
    (project / "configure").write_text(
        "#!/bin/sh\n"
        "prefix=/usr/local\n"
        'for arg in "$@"; do case "$arg" in --prefix=*) prefix="${arg#*=}";; esac; done\n'
        'sed "s|@PREFIX@|$prefix|g" Makefile.in > Makefile\n',
        encoding="utf-8",
    )
    (project / "configure").chmod(0o755)
    (project / "Makefile.in").write_text(
        "all:\n\t@true\n\n"
        "install:\n"
        "\tmkdir -p @PREFIX@/bin\n"
        "\tcp hello @PREFIX@/bin/hello\n"
        "\tchmod 755 @PREFIX@/bin/hello\n",
        encoding="utf-8",
    )
    (project / "hello").write_text("#!/bin/sh\necho built-hello\n", encoding="utf-8")
    (project / "hello").chmod(0o755)

    tap, _ = _make_tap(
        tmp_path,
        "hello",
        "1.0",
        project,
        "\n".join(
            [
                '    inreplace "hello", "built-hello", "patched-hello"',
                '    system "./configure", *std_configure_args',
                '    system "make", "install"',
            ]
        ),
    )
    config, result = _install(tmp_path, tap, "hello")
    script = config.keg_path("hello", "1.0") / "bin" / "hello"
    assert script.is_file()
    assert "patched-hello" in script.read_text()
    assert result.from_bottle is False


@pytest.mark.skipif(
    __import__("shutil").which("cmake") is None, reason="cmake not installed"
)
def test_cmake_source_build(tmp_path: Path) -> None:
    project = tmp_path / "hello-cmake"
    project.mkdir()
    (project / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\n"
        "project(hello C)\n"
        "add_executable(hello main.c)\n"
        "install(TARGETS hello DESTINATION bin)\n",
        encoding="utf-8",
    )
    (project / "main.c").write_text(
        '#include <stdio.h>\nint main(void) { puts("cmake-hello"); return 0; }\n',
        encoding="utf-8",
    )
    tap, _ = _make_tap(
        tmp_path,
        "hello-cmake",
        "1.0",
        project,
        "\n".join(
            [
                '    system "cmake", "-S", ".", "-B", "build", *std_cmake_args',
                '    system "cmake", "--build", "build"',
                '    system "cmake", "--install", "build"',
            ]
        ),
    )
    config, result = _install(tmp_path, tap, "hello-cmake")
    binary = config.keg_path("hello-cmake", "1.0") / "bin" / "hello"
    assert binary.is_file()
    assert binary.stat().st_mode & 0o111
    assert result.from_bottle is False


def test_unsupported_source_install_is_explicit(tmp_path: Path) -> None:
    project = tmp_path / "bad-src"
    project.mkdir()
    (project / "README").write_text("bad\n")
    tap, _ = _make_tap(
        tmp_path,
        "bad",
        "1.0",
        project,
        '    system "make", "install"\n    (buildpath/"weird").write "x"',
    )
    config = Config.default(prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache")
    config.ensure()
    loader = FormulaLoader(discover_taps([tap]))
    installer = Installer(config, loader)
    with pytest.raises(Exception) as error:
        installer.install(loader.load("bad"), InstallOptions(build_from_source=True))
    assert "unsupported" in str(error.value).lower()
    assert not (config.cellar / "bad").exists()


@pytest.mark.skipif(
    __import__("shutil").which("make") is None or __import__("shutil").which("cc") is None,
    reason="make/cc not installed",
)
def test_conditionals_choose_host_branch(tmp_path: Path) -> None:
    project = tmp_path / "cond-src"
    project.mkdir()
    (project / "configure").write_text(
        "#!/bin/sh\n"
        "prefix=/usr/local\n"
        'for arg in "$@"; do case "$arg" in --prefix=*) prefix="${arg#*=}";; esac; done\n'
        'sed "s|@PREFIX@|$prefix|g" Makefile.in > Makefile\n',
        encoding="utf-8",
    )
    (project / "configure").chmod(0o755)
    (project / "Makefile.in").write_text(
        "all:\n\t@true\n\ninstall:\n\tmkdir -p @PREFIX@/bin\n\tcp hello @PREFIX@/bin/hello\n",
        encoding="utf-8",
    )
    (project / "hello").write_text("#!/bin/sh\necho condition\n", encoding="utf-8")
    (project / "hello").chmod(0o755)
    tap, _ = _make_tap(
        tmp_path,
        "cond",
        "1.0",
        project,
        '    if OS.mac?\n      system "./configure", *std_configure_args\n      system "make", "install"\n    else\n      system "./configure", *std_configure_args\n      system "make", "install"\n    end',
    )
    config, _ = _install(tmp_path, tap, "cond")
    assert (config.keg_path("cond", "1.0") / "bin" / "hello").is_file()


def test_cargo_source_build(tmp_path: Path) -> None:
    import shutil

    if shutil.which("cargo") is None:
        pytest.skip("cargo is not installed")
    project = tmp_path / "hello-cargo"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        '[package]\nname = "hello-cargo"\nversion = "0.1.0"\nedition = "2021"\n\n'
        '[[bin]]\nname = "hello-cargo"\npath = "src/main.rs"\n',
        encoding="utf-8",
    )
    (project / "src" / "main.rs").write_text(
        'fn main() { println!("cargo-hello"); }\n', encoding="utf-8"
    )
    tap, _ = _make_tap(
        tmp_path,
        "hello-cargo",
        "0.1.0",
        project,
        '    system "cargo", "install", *std_cargo_args',
    )
    config, _ = _install(tmp_path, tap, "hello-cargo")
    assert (config.keg_path("hello-cargo", "0.1.0") / "bin" / "hello-cargo").is_file()


def test_resource_stage_downloads_and_installs(tmp_path: Path) -> None:
    main_project = tmp_path / "main"
    main_project.mkdir()
    (main_project / "README").write_text("main\n")
    resource_project = tmp_path / "resource"
    resource_project.mkdir()
    (resource_project / "data.txt").write_text("resource payload\n")

    main_archive = _tar(main_project, tmp_path / "src" / "resource-fixture-1.0.tar.gz", "resource-fixture-1.0")
    resource_archive = _tar(resource_project, tmp_path / "src" / "data-1.0.tar.gz", "data-1.0")
    tap = tmp_path / "tap"
    (tap / "Formula").mkdir(parents=True)
    (tap / "Formula" / "resource-fixture.rb").write_text(
        f'''class ResourceFixture < Formula
  desc "resource fixture"
  homepage "https://example.com/resource-fixture"
  url "{main_archive.as_uri()}"
  sha256 "{_sha(main_archive)}"

  resource "data" do
    url "{resource_archive.as_uri()}"
    sha256 "{_sha(resource_archive)}"
  end

  def install
    resource("data").stage do
      bin.install "data.txt"
    end
  end
end
'''
    )
    config, result = _install(tmp_path, tap, "resource-fixture")
    assert (result.keg / "bin" / "data.txt").read_text() == "resource payload\n"
    assert (config.prefix / "bin" / "data.txt").exists()


def test_head_install(run_cli, sample, tmp_path: Path) -> None:
    head_archive = tmp_path / "hello-head.tar.gz"
    with tarfile.open(head_archive, "w:gz") as tar:
        data = b"#!/bin/sh\necho head build\n"
        info = tarfile.TarInfo("hello-head/hello")
        info.size = len(data)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(data))
        readme = b"head readme\n"
        info = tarfile.TarInfo("hello-head/README")
        info.size = len(readme)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(readme))
    head_block = (
        "  head do\n"
        f'    url "{head_archive.as_uri()}"\n'
        f'    sha256 "{_sha(head_archive)}"\n'
        "  end\n\n"
    )
    sample.formula.write_text(sample.formula.read_text().replace("  bottle do", head_block + "  bottle do", 1))
    proc = run_cli("install", "--HEAD", "hello", bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    installed = sample.prefix / "bin" / "hello"
    assert installed.is_file()
    assert "head build" in installed.read_text()


def test_meson_source_build_with_fake_meson(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "meson-src"
    project.mkdir()
    (project / "README").write_text("meson fixture\n")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_meson = fake_bin / "meson"
    fake_meson.write_text(
        """#!/bin/sh
case "$1" in
  setup)
    shift
    for arg in "$@"; do
      case "$arg" in --prefix=*) prefix="${arg#--prefix=}";; esac
    done
    mkdir -p build
    printf '%s\n' "$prefix" > build/prefix.txt
    ;;
  compile)
    ;;
  install)
    prefix=$(cat build/prefix.txt)
    mkdir -p "$prefix/bin"
    printf '#!/bin/sh\\necho meson hello\\n' > "$prefix/bin/meson-hello"
    chmod +x "$prefix/bin/meson-hello"
    ;;
esac
"""
    )
    fake_meson.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{__import__('os').environ.get('PATH', '')}")
    tap, _archive = _make_tap(
        tmp_path,
        "meson-hello",
        "1.0",
        project,
        '    system "meson", "setup", "build", *std_meson_args\n'
        '    system "meson", "compile", "-C", "build"\n'
        '    system "meson", "install", "-C", "build"',
    )
    _config, result = _install(tmp_path, tap, "meson-hello")
    binary = result.keg / "bin" / "meson-hello"
    assert binary.is_file()
    assert "meson hello" in binary.read_text()


def test_on_arm_and_on_intel_blocks_follow_platform_machine(
    tmp_path: Path, monkeypatch
) -> None:
    import pybrew.source_builder as source_builder

    project = tmp_path / "arch-src"
    project.mkdir()
    (project / "arm-marker").write_text("arm\n")
    (project / "intel-marker").write_text("intel\n")
    tap, _ = _make_tap(
        tmp_path,
        "arch-fixture",
        "1.0",
        project,
        "\n".join(
            [
                "    on_arm do",
                '      bin.install "arm-marker"',
                "    end",
                "    on_intel do",
                '      bin.install "intel-marker"',
                "    end",
            ]
        ),
    )

    monkeypatch.setattr(source_builder.platform, "machine", lambda: "aarch64")
    config, _ = _install(tmp_path / "arm-host", tap, "arch-fixture")
    bin_dir = config.keg_path("arch-fixture", "1.0") / "bin"
    assert (bin_dir / "arm-marker").is_file()
    assert not (bin_dir / "intel-marker").exists()

    monkeypatch.setattr(source_builder.platform, "machine", lambda: "x86_64")
    config, _ = _install(tmp_path / "intel-host", tap, "arch-fixture")
    bin_dir = config.keg_path("arch-fixture", "1.0") / "bin"
    assert (bin_dir / "intel-marker").is_file()
    assert not (bin_dir / "arm-marker").exists()


def test_install_conditions_choose_expected_branches(tmp_path: Path, monkeypatch) -> None:
    import pybrew.source_builder as source_builder

    project = tmp_path / "pred-src"
    project.mkdir()
    for marker in ("included", "missing", "with", "without", "appended", "intel", "not-intel"):
        (project / f"{marker}.txt").write_text(f"{marker}\n")
    install_block = "\n".join(
        [
            "    args = %w[--with-feature --without-ssl]",
            '    if args.include?("--with-feature")',
            '      bin.install "included.txt"',
            "    end",
            '    if args.include?("--not-there")',
            '      bin.install "missing.txt"',
            "    end",
            '    args << "--extra-flag"',
            '    if args.include?("--extra-flag")',
            '      bin.install "appended.txt"',
            "    end",
            '    if build.with?("ssl")',
            '      bin.install "with.txt"',
            "    end",
            '    if build.without?("ssl")',
            '      bin.install "without.txt"',
            "    end",
            "    if Hardware::CPU.intel?",
            '      bin.install "intel.txt"',
            "    else",
            '      bin.install "not-intel.txt"',
            "    end",
        ]
    )
    tap, _ = _make_tap(tmp_path, "pred-fixture", "1.0", project, install_block)

    monkeypatch.setattr(source_builder.platform, "machine", lambda: "x86_64")
    config, _ = _install(tmp_path / "intel-host", tap, "pred-fixture")
    bin_dir = config.keg_path("pred-fixture", "1.0") / "bin"
    assert (bin_dir / "included.txt").is_file()
    assert (bin_dir / "appended.txt").is_file()
    assert (bin_dir / "without.txt").is_file()
    assert (bin_dir / "intel.txt").is_file()
    for skipped in ("missing.txt", "with.txt", "not-intel.txt"):
        assert not (bin_dir / skipped).exists()

    monkeypatch.setattr(source_builder.platform, "machine", lambda: "aarch64")
    config, _ = _install(tmp_path / "arm-host", tap, "pred-fixture")
    bin_dir = config.keg_path("pred-fixture", "1.0") / "bin"
    assert (bin_dir / "not-intel.txt").is_file()
    assert not (bin_dir / "intel.txt").exists()


def test_env_reads_in_conditions_and_system_args(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PYBREW_TEST_ENV_FLAG", "enabled")
    monkeypatch.delenv("PYBREW_TEST_UNSET_FLAG", raising=False)
    project = tmp_path / "env-src"
    project.mkdir()
    for marker in ("host-flag", "local-flag", "unset-flag"):
        (project / f"{marker}.txt").write_text(f"{marker}\n")
    install_block = (
        '    ENV["PYBREW_LOCAL_FLAG"] = "local-value"\n'
        '    if ENV["PYBREW_TEST_ENV_FLAG"]\n'
        '      bin.install "host-flag.txt"\n'
        "    end\n"
        '    if ENV["PYBREW_LOCAL_FLAG"]\n'
        '      bin.install "local-flag.txt"\n'
        "    end\n"
        '    if ENV["PYBREW_TEST_UNSET_FLAG"]\n'
        '      bin.install "unset-flag.txt"\n'
        "    end\n"
        '    system "sh", "-c", "printf \'%s\' \\"$0\\" > env.txt", ENV["PYBREW_LOCAL_FLAG"]\n'
        '    bin.install "env.txt"'
    )
    tap, _ = _make_tap(tmp_path, "env-fixture", "1.0", project, install_block)
    config, _ = _install(tmp_path, tap, "env-fixture")
    bin_dir = config.keg_path("env-fixture", "1.0") / "bin"
    assert (bin_dir / "host-flag.txt").is_file()
    assert (bin_dir / "local-flag.txt").is_file()
    assert not (bin_dir / "unset-flag.txt").exists()
    assert (bin_dir / "env.txt").read_text() == "local-value"


def test_unknown_install_condition_is_explicit() -> None:
    from pybrew.errors import UnsupportedInstall
    from pybrew.models import Formula
    from pybrew.source_builder import InstallParser

    formula = Formula(name="unknown-predicate", version="1.0")
    formula.install_source = (
        "def install\n"
        "  if build.weird?\n"
        '    bin.install "x"\n'
        "  end\n"
        "end\n"
    )
    with pytest.raises(UnsupportedInstall, match="unsupported install condition"):
        InstallParser(formula).parse()


# ---------------------------------------------------------------------------
# compiler selection / build environment
# ---------------------------------------------------------------------------

_FAKE_COMPILER_SCRIPT = """#!/bin/sh
{
  printf 'argv=%s\\n' "$*"
  printf 'CC=%s\\n' "${CC:-}"
  printf 'CXX=%s\\n' "${CXX:-}"
  printf 'CFLAGS=%s\\n' "${CFLAGS:-}"
  printf 'CXXFLAGS=%s\\n' "${CXXFLAGS:-}"
  printf 'LDFLAGS=%s\\n' "${LDFLAGS:-}"
} > "$PWD/__REPORT__"
"""


def _source_config(tmp_path: Path) -> Config:
    config = Config.default(prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache")
    config.ensure()
    return config


def _fake_compiler(directory: Path, name: str, report: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        _FAKE_COMPILER_SCRIPT.replace("__REPORT__", report), encoding="utf-8"
    )
    path.chmod(0o755)
    return path


def _compiler_report(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def _fake_xcrun(directory: Path, sdkroot: Path, name: str = "xcrun") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' {shlex.quote(str(sdkroot))}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_wrapper(directory: Path, name: str, report: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" > \"$PWD/{report}\"\n"
        'exec "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_build_environment_defaults_are_unchanged(tmp_path: Path) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    prefix = config.prefix
    prepared = source_builder.build_environment(config, {"PATH": "/usr/bin:/bin"})
    env = prepared.env
    assert env["CPPFLAGS"] == f"-I{prefix / 'include'}"
    assert env["CFLAGS"] == f"-I{prefix / 'include'}"
    assert env["CXXFLAGS"] == f"-I{prefix / 'include'}"
    assert env["LDFLAGS"] == f"-L{prefix / 'lib'}"
    assert env["HOMEBREW_PREFIX"] == str(prefix)
    assert env["HOMEBREW_CELLAR"] == str(config.cellar)
    assert env["HOMEBREW_REPOSITORY"] == str(prefix)
    assert env["CMAKE_PREFIX_PATH"] == str(prefix)
    assert env["PKG_CONFIG_PATH"] == os.pathsep.join(
        [str(prefix / "lib" / "pkgconfig"), str(prefix / "share" / "pkgconfig")]
    )
    assert env["PATH"].startswith(
        f"{prefix / 'bin'}{os.pathsep}{prefix / 'sbin'}{os.pathsep}"
    )
    assert "CC" not in env
    assert "CXX" not in env
    assert prepared.compiler == source_builder.CompilerSelection()


def test_build_environment_preserves_explicit_compilers_and_flags(
    tmp_path: Path,
) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    prefix = config.prefix
    base = {
        "PATH": "/usr/bin:/bin",
        "CC": "user-cc",
        "CXX": "user-cxx",
        "CFLAGS": "-O2 -g",
        "CXXFLAGS": "-O3",
        "CPPFLAGS": "-DUSER=1",
        "LDFLAGS": "-L/opt/user/lib",
        "MAKEFLAGS": "-j1",
    }
    env = source_builder.build_environment(config, base).env
    assert env["CC"] == "user-cc"
    assert env["CXX"] == "user-cxx"
    assert env["CFLAGS"] == f"-O2 -g -I{prefix / 'include'}"
    assert env["CXXFLAGS"] == f"-O3 -I{prefix / 'include'}"
    assert env["CPPFLAGS"] == f"-DUSER=1 -I{prefix / 'include'}"
    assert env["LDFLAGS"] == f"-L/opt/user/lib -L{prefix / 'lib'}"
    assert env["MAKEFLAGS"] == "-j1"


def test_build_environment_does_not_duplicate_prefix_flags(tmp_path: Path) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    prefix = config.prefix
    include = f"-I{prefix / 'include'}"
    library = f"-L{prefix / 'lib'}"
    env = source_builder.build_environment(
        config,
        {
            "PATH": "/usr/bin:/bin",
            "CFLAGS": include,
            "CPPFLAGS": include,
            "CXXFLAGS": include,
            "LDFLAGS": library,
        },
    ).env
    assert env["CFLAGS"] == include
    assert env["CPPFLAGS"] == include
    assert env["CXXFLAGS"] == include
    assert env["LDFLAGS"] == library


def test_pybrew_cc_and_cxx_override_plain_cc(tmp_path: Path) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    env = source_builder.build_environment(
        config,
        {
            "PATH": "/usr/bin:/bin",
            "CC": "plain-cc",
            "CXX": "plain-cxx",
            "PYBREW_CC": "brew-cc",
            "PYBREW_CXX": "brew-cxx",
        },
    ).env
    assert env["CC"] == "brew-cc"
    assert env["CXX"] == "brew-cxx"


def test_pybrew_compiler_selects_available_clang(tmp_path: Path) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    clang = _fake_compiler(fake_bin, "clang", "unused.txt")
    clangxx = _fake_compiler(fake_bin, "clang++", "unused.txt")
    prepared = source_builder.build_environment(
        config, {"PATH": str(fake_bin), "PYBREW_COMPILER": "clang"}
    )
    assert prepared.compiler.family == "clang"
    assert prepared.compiler.warning is None
    assert prepared.env["CC"] == str(clang)
    assert prepared.env["CXX"] == str(clangxx)


def test_explicit_compilers_win_over_pybrew_compiler(tmp_path: Path) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    _fake_compiler(fake_bin, "clang", "unused.txt")
    clangxx = _fake_compiler(fake_bin, "clang++", "unused.txt")

    both = source_builder.build_environment(
        config,
        {
            "PATH": str(fake_bin),
            "PYBREW_COMPILER": "clang",
            "CC": "user-cc",
            "CXX": "user-cxx",
        },
    )
    assert both.env["CC"] == "user-cc"
    assert both.env["CXX"] == "user-cxx"
    assert both.compiler.family == "clang"

    partial = source_builder.build_environment(
        config,
        {"PATH": str(fake_bin), "PYBREW_COMPILER": "clang", "CC": "user-cc"},
    )
    assert partial.env["CC"] == "user-cc"
    assert partial.env["CXX"] == str(clangxx)


def test_missing_pybrew_compiler_falls_back_with_clear_warning(
    tmp_path: Path,
) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    prepared = source_builder.build_environment(
        config, {"PATH": str(empty_bin), "PYBREW_COMPILER": "gcc"}
    )
    assert "CC" not in prepared.env
    assert "CXX" not in prepared.env
    assert prepared.compiler.family is None
    assert prepared.compiler.warning is not None
    assert "gcc" in prepared.compiler.warning
    assert "not available" in prepared.compiler.warning


def test_unknown_pybrew_compiler_value_falls_back(tmp_path: Path) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    prepared = source_builder.build_environment(
        config, {"PATH": "/usr/bin:/bin", "PYBREW_COMPILER": "icc"}
    )
    assert "CC" not in prepared.env
    assert prepared.compiler.warning is not None
    assert "not supported" in prepared.compiler.warning


def test_missing_cxx_driver_keeps_cc_and_warns(tmp_path: Path) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    clang = _fake_compiler(fake_bin, "clang", "unused.txt")
    prepared = source_builder.build_environment(
        config, {"PATH": str(fake_bin), "PYBREW_COMPILER": "clang"}
    )
    assert prepared.env["CC"] == str(clang)
    assert "CXX" not in prepared.env
    assert prepared.compiler.warning is not None
    assert "clang++" in prepared.compiler.warning


def test_source_builder_reports_compiler_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("PYBREW_COMPILER", "gcc")
    builder = source_builder.SourceBuilder(
        Formula(name="warn-fixture", version="1.0"), config
    )
    env = builder._build_env(tmp_path / "keg")
    captured = capsys.readouterr()
    assert "PYBREW_COMPILER=gcc" in captured.err
    assert "not available" in captured.err
    assert "CC" not in env


def test_source_build_passes_selected_compiler_env_and_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "fake-bin"
    _fake_compiler(fake_bin, "clang", "cc-report.txt")
    _fake_compiler(fake_bin, "clang++", "cxx-report.txt")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PYBREW_COMPILER", "clang")
    monkeypatch.setenv("CFLAGS", "-O2 -g")
    monkeypatch.setenv("CXXFLAGS", "-O3")
    monkeypatch.setenv("LDFLAGS", "-L/opt/explicit")
    for name in ("PYBREW_CC", "CC", "PYBREW_CXX", "CXX"):
        monkeypatch.delenv(name, raising=False)

    project = tmp_path / "compiler-src"
    project.mkdir()
    (project / "README").write_text("compiler fixture\n", encoding="utf-8")
    tap, _ = _make_tap(
        tmp_path,
        "compiler-fixture",
        "1.0",
        project,
        "\n".join(
            [
                '    system ENV["CC"], "--c-probe", "one"',
                '    system ENV["CXX"], "--cxx-probe"',
                '    bin.install "cc-report.txt"',
                '    bin.install "cxx-report.txt"',
            ]
        ),
    )
    config, result = _install(tmp_path, tap, "compiler-fixture")
    prefix = config.prefix

    cc = _compiler_report(result.keg / "bin" / "cc-report.txt")
    assert cc["argv"] == "--c-probe one"
    assert cc["CC"] == str(fake_bin / "clang")
    assert cc["CFLAGS"] == f"-O2 -g -I{prefix / 'include'}"
    assert cc["LDFLAGS"] == f"-L/opt/explicit -L{prefix / 'lib'}"

    cxx = _compiler_report(result.keg / "bin" / "cxx-report.txt")
    assert cxx["argv"] == "--cxx-probe"
    assert cxx["CXX"] == str(fake_bin / "clang++")
    assert cxx["CXXFLAGS"] == f"-O3 -I{prefix / 'include'}"


def test_opt_in_superenv_compiler_shims(tmp_path: Path) -> None:
    import subprocess
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    cc = _fake_compiler(fake_bin, "clang", "cc-report.txt")
    cxx = _fake_compiler(fake_bin, "clang++", "cxx-report.txt")
    prepared = source_builder.build_environment(
        config,
        {
            "PATH": str(fake_bin),
            "PYBREW_SUPERENV": "1",
            "PYBREW_CC": str(cc),
            "PYBREW_CXX": str(cxx),
        },
    )
    assert prepared.superenv is not None
    env = prepared.env
    assert env["SUPERENV"] == "1"
    assert env["PATH"].startswith(f"{prepared.superenv.root}{os.pathsep}")
    assert env["CC"] == str(prepared.superenv.root / "cc")
    assert env["CXX"] == str(prepared.superenv.root / "c++")
    assert env["HOMEBREW_CC"] == str(cc)
    assert env["HOMEBREW_CXX"] == str(cxx)

    subprocess.run([env["CC"], "-c", "hello.c"], env=env, cwd=tmp_path, check=True)
    report = _compiler_report(tmp_path / "cc-report.txt")
    assert "-isystem" in report["argv"]
    assert str(config.prefix / "include") in report["argv"]
    assert "-L" in report["argv"]
    assert report["CC"] == env["CC"]


def test_sanitize_build_flags_is_bounded_and_preserves_safe_flags(
    tmp_path: Path,
) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    prefix = config.prefix
    include = f"-I{prefix / 'include'}"
    library = f"-L{prefix / 'lib'}"
    subinclude = f"-I{prefix / 'include' / 'nested'}"
    sublibrary = f"-L{prefix / 'lib' / 'nested'}"
    flags = " ".join(
        [
            "-O3",
            "-ggdb",
            "-DNDEBUG",
            "-std=c11",
            "-fPIC",
            "-I/usr/include",
            "-I/usr/include/nested",
            "-I/usr/local/include",
            "-isystem",
            "/usr/include",
            "-L/usr/lib",
            "-L/usr/lib64",
            "-L/usr/local/lib",
            "-L/lib",
            "-L/lib64",
            "-Wl,-rpath,/usr/lib",
            "-Wl,-rpath,/usr/local/lib",
            "-Wl,-rpath,/opt/foreign/lib",
            subinclude,
            sublibrary,
            include,
            include,
            "-isystem",
            str(prefix / "include"),
            library,
            library,
            f"-Wl,-rpath,{prefix / 'lib'}",
        ]
    )
    sanitized = source_builder.sanitize_build_flags(flags, prefix)
    tokens = shlex.split(sanitized)

    for safe in ("-O3", "-ggdb", "-DNDEBUG", "-std=c11", "-fPIC"):
        assert safe in tokens
    assert subinclude in tokens
    assert sublibrary in tokens
    for unsafe in (
        "/usr/include",
        "/usr/include/nested",
        "/usr/local/include",
        "/usr/lib",
        "/usr/lib64",
        "/usr/local/lib",
        "/lib",
        "/lib64",
    ):
        assert unsafe not in tokens
        for flag_prefix in ("-I", "-L", "-isystem", "-Wl,-rpath,"):
            assert not any(
                token.startswith(flag_prefix + unsafe) for token in tokens
            ), (unsafe, tokens)
    assert tokens.count(include) == 1
    assert tokens.count(library) == 1
    assert f"-Wl,-rpath,{prefix / 'lib'}" in tokens
    assert not any(
        token.startswith("-Wl,-rpath,/opt/foreign") for token in tokens
    )
    assert "-isystem" not in tokens

    dropped = shlex.split(
        source_builder.sanitize_build_flags(
            f"{include} {subinclude} {library} {sublibrary}",
            prefix,
            drop_prefix_flags=True,
        )
    )
    assert include not in dropped
    assert library not in dropped
    assert subinclude in dropped
    assert sublibrary in dropped


def test_superenv_build_environment_sanitizes_flags_and_keeps_explicit(
    tmp_path: Path,
) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    prefix = config.prefix
    fake_bin = tmp_path / "fake-bin"
    cc = _fake_compiler(fake_bin, "clang", "cc-report.txt")
    cxx = _fake_compiler(fake_bin, "clang++", "cxx-report.txt")
    include = f"-I{prefix / 'include'}"
    library = f"-L{prefix / 'lib'}"
    prepared = source_builder.build_environment(
        config,
        {
            "PATH": str(fake_bin),
            "PYBREW_SUPERENV": "1",
            "CC": "plain-cc",
            "CXX": "plain-cxx",
            "PYBREW_CC": str(cc),
            "PYBREW_CXX": str(cxx),
            "CFLAGS": f"-O2 -g -I/usr/include {include} {include}",
            "CXXFLAGS": "-O3 -I/usr/local/include -fPIC",
            "LDFLAGS": (
                f"-L/usr/lib -Wl,-rpath,/usr/lib "
                f"-Wl,-rpath,/opt/foreign/lib {library} -lfoo"
            ),
            "CPPFLAGS": "-DSAFE=1 -I/usr/include",
        },
    )
    env = prepared.env
    assert prepared.superenv is not None
    assert prepared.superenv.helper.is_file()
    assert env["HOMEBREW_CC"] == str(cc)
    assert env["HOMEBREW_CXX"] == str(cxx)
    assert env["CC"] == str(prepared.superenv.root / "cc")
    assert env["CXX"] == str(prepared.superenv.root / "c++")
    assert env["PATH"].startswith(f"{prepared.superenv.root}{os.pathsep}")

    cflags = shlex.split(env["CFLAGS"])
    assert cflags[:3] == ["-O2", "-g", include]
    assert cflags.count(include) == 1
    assert "/usr/include" not in env["CFLAGS"]

    cxxflags = shlex.split(env["CXXFLAGS"])
    assert "-O3" in cxxflags and "-fPIC" in cxxflags
    assert "/usr/local/include" not in env["CXXFLAGS"]

    ldflags = shlex.split(env["LDFLAGS"])
    assert "-lfoo" in ldflags
    assert ldflags.count(library) == 1
    assert "/usr/lib" not in env["LDFLAGS"]
    assert not any("/usr/lib" in token for token in ldflags)
    assert "/opt/foreign/lib" not in env["LDFLAGS"]
    assert not any("/opt/foreign" in token for token in ldflags)

    cppflags = shlex.split(env["CPPFLAGS"])
    assert "-DSAFE=1" in cppflags
    assert "/usr/include" not in env["CPPFLAGS"]
    assert not any("/usr/include" in token for token in cppflags)


def test_superenv_shims_sanitize_flags_at_build_time(tmp_path: Path) -> None:
    import subprocess
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    prefix = config.prefix
    fake_bin = tmp_path / "fake-bin"
    cc = _fake_compiler(fake_bin, "clang", "cc-report.txt")
    cxx = _fake_compiler(fake_bin, "clang++", "cxx-report.txt")
    prepared = source_builder.build_environment(
        config,
        {
            "PATH": str(fake_bin),
            "PYBREW_SUPERENV": "1",
            "PYBREW_CC": str(cc),
            "PYBREW_CXX": str(cxx),
        },
    )
    env = prepared.env
    include = str(prefix / "include")
    library = str(prefix / "lib")
    subinclude = str(prefix / "include" / "nested")
    sublibrary = str(prefix / "lib" / "nested")
    unsafe_arguments = [
        "-O3",
        "-ggdb",
        "-DSAFE=1",
        "-I/usr/include",
        "-I",
        "/usr/include/nested",
        "-isystem",
        include,
        "-L/usr/lib",
        "-L",
        "/usr/local/lib",
        "-Wl,-rpath,/usr/lib",
        "-Wl,-rpath,/opt/foreign/lib",
        f"-I{include}",
        f"-L{library}",
        f"-I{subinclude}",
        f"-L{sublibrary}",
        "-fPIC",
    ]

    subprocess.run(
        [env["CC"], *unsafe_arguments],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    argv = shlex.split(_compiler_report(tmp_path / "cc-report.txt")["argv"])
    for safe in ("-O3", "-ggdb", "-DSAFE=1", "-fPIC"):
        assert safe in argv
    assert argv.count("-isystem") == 1
    assert argv.count(include) == 1
    assert argv.count("-L") == 1
    assert argv.count(library) == 1
    assert f"-I{subinclude}" in argv
    assert f"-L{sublibrary}" in argv
    assert not any("/usr/include" in token for token in argv)
    assert not any("/usr/lib" in token for token in argv)
    assert not any("/usr/local/lib" in token for token in argv)
    assert not any("/opt/foreign/lib" in token for token in argv)

    subprocess.run(
        [env["CXX"], *unsafe_arguments],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    cxx_argv = shlex.split(_compiler_report(tmp_path / "cxx-report.txt")["argv"])
    assert cxx_argv.count("-isystem") == 1
    assert cxx_argv.count(include) == 1
    assert cxx_argv.count("-L") == 1
    assert cxx_argv.count(library) == 1
    assert f"-I{subinclude}" in cxx_argv
    assert f"-L{sublibrary}" in cxx_argv
    assert "-O3" in cxx_argv and "-ggdb" in cxx_argv and "-fPIC" in cxx_argv


def test_superenv_source_build_sanitizes_flags_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "prefix"
    fake_bin = tmp_path / "fake-bin"
    cc = _fake_compiler(fake_bin, "clang", "cc-report.txt")
    cxx = _fake_compiler(fake_bin, "clang++", "cxx-report.txt")
    monkeypatch.setenv(
        "PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    monkeypatch.setenv("PYBREW_SUPERENV", "1")
    monkeypatch.setenv("PYBREW_CC", str(cc))
    monkeypatch.setenv("PYBREW_CXX", str(cxx))
    monkeypatch.setenv("CFLAGS", "-O2 -I/usr/include")
    monkeypatch.setenv("LDFLAGS", "-L/usr/lib -Wl,-rpath,/usr/lib")
    for name in ("CC", "CXX"):
        monkeypatch.delenv(name, raising=False)

    project = tmp_path / "superenv-src"
    project.mkdir()
    (project / "README").write_text("superenv fixture\n", encoding="utf-8")
    tap, _ = _make_tap(
        tmp_path,
        "superenv-fixture",
        "1.0",
        project,
        "\n".join(
            [
                '    system ENV["CC"], "-O3", "-ggdb", "-DSAFE=1", '
                '"-I/usr/include", "-L/usr/lib", '
                '"-Wl,-rpath,/usr/lib", "-Wl,-rpath,/opt/foreign/lib", '
                '"-I#{prefix}/include", "-L#{prefix}/lib"',
                '    bin.install "cc-report.txt"',
            ]
        ),
    )
    _config, result = _install(tmp_path, tap, "superenv-fixture")
    report = _compiler_report(result.keg / "bin" / "cc-report.txt")
    argv = shlex.split(report["argv"])
    for safe in ("-O3", "-ggdb", "-DSAFE=1"):
        assert safe in argv
    assert argv.count("-isystem") == 1
    assert argv.count(str(prefix / "include")) == 1
    assert argv.count("-L") == 1
    assert argv.count(str(prefix / "lib")) == 1
    assert not any("/usr/include" in token for token in argv)
    assert not any("/usr/lib" in token for token in argv)
    assert not any("/opt/foreign/lib" in token for token in argv)
    assert report["CC"] == str(
        prefix / "var" / "pybrew" / "superenv" / "shims" / "cc"
    )
    assert "/usr/include" not in report["CFLAGS"]
    assert "/usr/lib" not in report["LDFLAGS"]


def test_default_build_environment_does_not_sanitize_without_superenv(
    tmp_path: Path,
) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    prefix = config.prefix
    prepared = source_builder.build_environment(
        config,
        {
            "PATH": "/usr/bin:/bin",
            "CFLAGS": "-O2 -I/usr/include",
            "LDFLAGS": "-L/usr/lib -Wl,-rpath,/usr/lib",
        },
    )
    assert prepared.superenv is None
    assert prepared.env["CFLAGS"] == (
        f"-O2 -I/usr/include -I{prefix / 'include'}"
    )
    assert prepared.env["LDFLAGS"] == (
        f"-L/usr/lib -Wl,-rpath,/usr/lib -L{prefix / 'lib'}"
    )


def test_detect_sdkroot_with_injectable_fake_xcrun(tmp_path: Path) -> None:
    import pybrew.source_builder as source_builder

    sdk = tmp_path / "MacOSX.sdk"
    sdk.mkdir()
    xcrun = _fake_xcrun(tmp_path / "xcrun-bin", sdk)
    env = {"PATH": "/usr/bin:/bin", "PYBREW_XCRUN": str(xcrun)}

    assert source_builder.detect_sdkroot(env, system="Darwin") == sdk
    # The explicit override also makes the probe usable on non-Darwin hosts.
    assert source_builder.detect_sdkroot(env) == sdk
    assert (
        source_builder.detect_sdkroot({"PATH": "/usr/bin:/bin"}, system="Linux")
        is None
    )

    explicit = tmp_path / "Explicit.sdk"
    explicit.mkdir()
    assert (
        source_builder.detect_sdkroot(
            {**env, "PYBREW_SDKROOT": str(explicit)}, system="Linux"
        )
        == explicit
    )
    assert (
        source_builder.detect_sdkroot(
            {**env, "SDKROOT": str(explicit)}, system="Linux"
        )
        == explicit
    )
    assert source_builder.detect_sdkroot({**env, "PYBREW_XCRUN": ""}) is None


def test_sdkroot_exported_and_isysroot_added_in_superenv(
    tmp_path: Path,
) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    prefix = config.prefix
    fake_bin = tmp_path / "fake-bin"
    cc = _fake_compiler(fake_bin, "clang", "cc-report.txt")
    cxx = _fake_compiler(fake_bin, "clang++", "cxx-report.txt")
    sdk = tmp_path / "MacOSX.sdk"
    sdk.mkdir()
    xcrun = _fake_xcrun(tmp_path / "xcrun-bin", sdk)

    prepared = source_builder.build_environment(
        config,
        {
            "PATH": str(fake_bin),
            "PYBREW_SUPERENV": "1",
            "PYBREW_XCRUN": str(xcrun),
            "PYBREW_CC": str(cc),
            "PYBREW_CXX": str(cxx),
        },
    )
    env = prepared.env
    assert env["SDKROOT"] == str(sdk)
    for name in ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS"):
        tokens = shlex.split(env[name])
        assert "-isysroot" in tokens
        assert str(sdk) in tokens

    # Without Superenv, an SDKROOT explicit enough to be exported does not
    # change the default compiler flags (existing behavior stays intact).
    default = source_builder.build_environment(
        config, {"PATH": str(fake_bin), "SDKROOT": str(sdk)}
    )
    assert default.env["SDKROOT"] == str(sdk)
    assert "-isysroot" not in default.env["CFLAGS"]
    assert default.env["CFLAGS"] == f"-I{prefix / 'include'}"


def test_explicit_isysroot_is_not_overridden_by_detection(
    tmp_path: Path,
) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    cc = _fake_compiler(fake_bin, "clang", "cc-report.txt")
    cxx = _fake_compiler(fake_bin, "clang++", "cxx-report.txt")
    detected_sdk = tmp_path / "Detected.sdk"
    detected_sdk.mkdir()
    explicit_sdk = tmp_path / "Explicit.sdk"
    explicit_sdk.mkdir()
    xcrun = _fake_xcrun(tmp_path / "xcrun-bin", detected_sdk)

    prepared = source_builder.build_environment(
        config,
        {
            "PATH": str(fake_bin),
            "PYBREW_SUPERENV": "1",
            "PYBREW_XCRUN": str(xcrun),
            "PYBREW_CC": str(cc),
            "PYBREW_CXX": str(cxx),
            "CFLAGS": f"-isysroot {explicit_sdk}",
            "CPPFLAGS": f"--sysroot={explicit_sdk}",
        },
    )
    env = prepared.env
    assert env["SDKROOT"] == str(detected_sdk)
    cflags = shlex.split(env["CFLAGS"])
    assert cflags.count("-isysroot") == 1
    assert str(explicit_sdk) in cflags
    assert str(detected_sdk) not in cflags
    cppflags = shlex.split(env["CPPFLAGS"])
    assert f"--sysroot={explicit_sdk}" in cppflags
    assert str(detected_sdk) not in cppflags


def test_language_standard_flags_are_bounded_and_do_not_override(
    tmp_path: Path,
) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    base = {"PATH": "/usr/bin:/bin"}
    prepared = source_builder.build_environment(
        config,
        {
            **base,
            "PYBREW_C_STANDARD": "c11",
            "PYBREW_CXX_STANDARD": "-std=c++17",
        },
    )
    assert "-std=c11" in shlex.split(prepared.env["CFLAGS"])
    assert "-std=c++17" in shlex.split(prepared.env["CXXFLAGS"])

    explicit = source_builder.build_environment(
        config,
        {
            **base,
            "CFLAGS": "-O2 -std=gnu11",
            "PYBREW_C_STANDARD": "c11",
        },
    )
    cflags = shlex.split(explicit.env["CFLAGS"])
    assert "-std=gnu11" in cflags
    assert "-std=c11" not in cflags
    assert sum(token.startswith("-std=") for token in cflags) == 1

    alias = source_builder.build_environment(
        config,
        {**base, "PYBREW_CXX_STANDARD": "c++1z"},
    )
    assert "-std=c++17" in shlex.split(alias.env["CXXFLAGS"])

    invalid = source_builder.build_environment(
        config,
        {
            **base,
            "CFLAGS": "-O2",
            "PYBREW_C_STANDARD": "c11; rm -rf /",
            "PYBREW_CXX_STANDARD": "not-a-standard",
        },
    )
    assert not any(
        token.startswith("-std=") for token in shlex.split(invalid.env["CFLAGS"])
    )
    assert not any(
        token.startswith("-std=") for token in shlex.split(invalid.env["CXXFLAGS"])
    )


def test_superenv_compiler_wrapper_specs(tmp_path: Path) -> None:
    import subprocess
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    prefix = config.prefix
    fake_bin = tmp_path / "fake-bin"
    cc = _fake_compiler(fake_bin, "clang", "cc-report.txt")
    cxx = _fake_compiler(fake_bin, "clang++", "cxx-report.txt")
    ccache = _fake_wrapper(fake_bin, "ccache", "wrapper-cc.txt")
    ccache_cxx = _fake_wrapper(fake_bin, "ccache-cxx", "wrapper-cxx.txt")
    cc_spec = f"{ccache} {cc}"
    cxx_spec = f"{ccache_cxx} {cxx}"
    assert source_builder.split_compiler_command(cc_spec) == [
        str(ccache),
        str(cc),
    ]

    prepared = source_builder.build_environment(
        config,
        {
            "PATH": str(fake_bin),
            "PYBREW_SUPERENV": "1",
            "CC": "plain-cc",
            "CXX": "plain-cxx",
            "PYBREW_CC": cc_spec,
            "PYBREW_CXX": cxx_spec,
        },
    )
    env = prepared.env
    assert env["HOMEBREW_CC"] == cc_spec
    assert env["HOMEBREW_CXX"] == cxx_spec
    assert env["CC"] == str(prepared.superenv.root / "cc")
    assert env["CXX"] == str(prepared.superenv.root / "c++")

    subprocess.run(
        [env["CC"], "-O2", "-DWRAP=1"], cwd=tmp_path, env=env, check=True
    )
    wrapper_argv = shlex.split(
        (tmp_path / "wrapper-cc.txt").read_text().strip()
    )
    assert wrapper_argv[0] == str(cc)
    assert "-O2" in wrapper_argv and "-DWRAP=1" in wrapper_argv
    compiler = _compiler_report(tmp_path / "cc-report.txt")
    assert "-O2" in compiler["argv"]
    assert "-isystem" in compiler["argv"]
    assert str(prefix / "include") in compiler["argv"]

    subprocess.run(
        [env["CXX"], "-O3"], cwd=tmp_path, env=env, check=True
    )
    wrapper_cxx_argv = shlex.split(
        (tmp_path / "wrapper-cxx.txt").read_text().strip()
    )
    assert wrapper_cxx_argv[0] == str(cxx)
    compiler_cxx = _compiler_report(tmp_path / "cxx-report.txt")
    assert "-O3" in compiler_cxx["argv"]
    assert "-isystem" in compiler_cxx["argv"]
    assert str(prefix / "include") in compiler_cxx["argv"]


def test_sanitize_flag_list_handles_sdk_sysroots_and_dedupes(
    tmp_path: Path,
) -> None:
    import pybrew.source_builder as source_builder

    tokens = source_builder.sanitize_flag_list(
        [
            "-O2",
            "-isysroot",
            "/SDK",
            "-isysroot",
            "/SDK",
            "-Wl,-syslibroot,/SDK",
            "--sysroot=/SDK",
            "-Wl,-syslibroot,/OTHER",
            "-Wl,-syslibroot=/OTHER2",
            "-arch",
            "arm64",
            "-m64",
        ],
        tmp_path / "prefix",
    )

    assert tokens.count("/SDK") == 1
    assert tokens.count("-isysroot") == 1
    assert "-Wl,-syslibroot,/OTHER" in tokens
    assert "-Wl,-syslibroot=/OTHER2" in tokens
    assert "-O2" in tokens
    assert tokens[tokens.index("-arch") + 1] == "arm64"
    assert "-m64" in tokens


def test_superenv_shims_filter_duplicate_sdk_flags(tmp_path: Path) -> None:
    import subprocess
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    cc = _fake_compiler(fake_bin, "clang", "cc-report.txt")
    cxx = _fake_compiler(fake_bin, "clang++", "cxx-report.txt")
    prepared = source_builder.build_environment(
        config,
        {
            "PATH": str(fake_bin),
            "PYBREW_SUPERENV": "1",
            "PYBREW_CC": str(cc),
            "PYBREW_CXX": str(cxx),
        },
    )
    subprocess.run(
        [
            prepared.env["CC"],
            "-isysroot",
            "/SDK",
            "-isysroot",
            "/SDK",
            "-Wl,-syslibroot,/SDK",
            "-Wl,-syslibroot,/OTHER",
            "-O2",
        ],
        cwd=tmp_path,
        env=prepared.env,
        check=True,
    )
    argv = shlex.split(_compiler_report(tmp_path / "cc-report.txt")["argv"])
    assert argv.count("/SDK") == 1
    assert argv.count("-isysroot") == 1
    assert "-Wl,-syslibroot,/OTHER" in argv
    assert "-O2" in argv


def test_architecture_flag_is_bounded_and_platform_aware() -> None:
    import pybrew.source_builder as source_builder

    assert (
        source_builder.architecture_flag(
            {"PYBREW_ARCH": "arm64"}, system="Darwin"
        )
        == "-arch arm64"
    )
    assert (
        source_builder.architecture_flag(
            {"PYBREW_ARCH": "amd64"}, system="Darwin"
        )
        == "-arch x86_64"
    )
    assert (
        source_builder.architecture_flag(
            {"PYBREW_ARCH": "x86_64"}, system="Linux", machine="x86_64"
        )
        == "-m64"
    )
    assert (
        source_builder.architecture_flag(
            {"PYBREW_ARCH": "i386"}, system="Linux", machine="x86_64"
        )
        == "-m32"
    )
    assert (
        source_builder.architecture_flag(
            {"PYBREW_ARCH": "arm64"}, system="Linux", machine="arm64"
        )
        is None
    )
    assert (
        source_builder.architecture_flag(
            {"PYBREW_ARCH": "arm64"}, system="Linux", machine="x86_64"
        )
        is None
    )
    assert source_builder.architecture_flag({"PYBREW_ARCH": "bogus"}) is None


def test_build_environment_applies_arch_and_preserves_explicit(
    tmp_path: Path,
) -> None:
    import platform
    import pybrew.source_builder as source_builder

    host = platform.system()
    arch = "arm64" if host == "Darwin" else "x86_64"
    expected = source_builder.architecture_flag({"PYBREW_ARCH": arch})
    if expected is None:
        pytest.skip("no bounded architecture flag for this host")
    config = _source_config(tmp_path)
    prepared = source_builder.build_environment(
        config, {"PATH": "/usr/bin:/bin", "PYBREW_ARCH": arch}
    )
    expected_tokens = expected.split()
    for name in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
        tokens = shlex.split(prepared.env[name])
        assert all(token in tokens for token in expected_tokens)

    explicit = "-arch x86_64" if expected.startswith("-arch") else "-m32"
    explicit_prepared = source_builder.build_environment(
        config,
        {
            "PATH": "/usr/bin:/bin",
            "PYBREW_ARCH": arch,
            "CFLAGS": f"-O2 {explicit}",
        },
    )
    cflags = shlex.split(explicit_prepared.env["CFLAGS"])
    assert all(token in cflags for token in explicit.split())
    assert not all(token in cflags for token in expected_tokens)


def test_superenv_objc_shims_and_flags(tmp_path: Path) -> None:
    import subprocess
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    prefix = config.prefix
    fake_bin = tmp_path / "fake-bin"
    cc = _fake_compiler(fake_bin, "clang", "cc-report.txt")
    cxx = _fake_compiler(fake_bin, "clang++", "cxx-report.txt")
    prepared = source_builder.build_environment(
        config,
        {
            "PATH": str(fake_bin),
            "PYBREW_SUPERENV": "1",
            "PYBREW_CC": str(cc),
            "PYBREW_CXX": str(cxx),
            "OBJCFLAGS": "-O2 -I/usr/include",
        },
    )
    env = prepared.env
    assert prepared.superenv is not None
    assert prepared.superenv.objc is not None
    assert prepared.superenv.objcxx is not None
    assert env["OBJC"] == str(prepared.superenv.objc)
    assert env["OBJCXX"] == str(prepared.superenv.objcxx)
    assert env["HOMEBREW_OBJC"] == str(cc)
    assert "-I/usr/include" not in env["OBJCFLAGS"]

    subprocess.run(
        [env["OBJC"], "-x", "objective-c", "-O2"],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    compiler = _compiler_report(tmp_path / "cc-report.txt")
    for token in ("-x", "objective-c", "-O2", "-isystem"):
        assert token in compiler["argv"]
    assert str(prefix / "include") in compiler["argv"]


def test_build_environment_preserves_explicit_objc_and_standards(
    tmp_path: Path,
) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    prefix = config.prefix
    env = source_builder.build_environment(
        config,
        {
            "PATH": "/usr/bin:/bin",
            "OBJC": "user-objc",
            "OBJCXX": "user-objcxx",
            "OBJCFLAGS": "-O2 -std=gnu11",
            "OBJCXXFLAGS": "-O3",
            "PYBREW_OBJC_STANDARD": "c11",
            "PYBREW_OBJCXX_STANDARD": "c++17",
        },
    ).env
    assert env["OBJC"] == "user-objc"
    assert env["OBJCXX"] == "user-objcxx"
    objcflags = shlex.split(env["OBJCFLAGS"])
    assert "-O2" in objcflags and "-std=gnu11" in objcflags
    assert "-std=c11" not in objcflags
    assert f"-I{prefix / 'include'}" in objcflags
    objcxxflags = shlex.split(env["OBJCXXFLAGS"])
    assert "-O3" in objcxxflags and "-std=c++17" in objcxxflags
    assert f"-I{prefix / 'include'}" in objcxxflags

    precedence = source_builder.build_environment(
        config,
        {
            "PATH": "/usr/bin:/bin",
            "OBJC": "plain-objc",
            "PYBREW_OBJC": "brew-objc",
        },
    ).env
    assert precedence["OBJC"] == "brew-objc"


def test_explicit_linker_sysroot_is_preserved(tmp_path: Path) -> None:
    import pybrew.source_builder as source_builder

    config = _source_config(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    cc = _fake_compiler(fake_bin, "clang", "cc-report.txt")
    cxx = _fake_compiler(fake_bin, "clang++", "cxx-report.txt")
    detected_sdk = tmp_path / "Detected.sdk"
    detected_sdk.mkdir()
    explicit_sdk = tmp_path / "Explicit.sdk"
    explicit_sdk.mkdir()
    xcrun = _fake_xcrun(tmp_path / "xcrun-bin", detected_sdk)
    prepared = source_builder.build_environment(
        config,
        {
            "PATH": str(fake_bin),
            "PYBREW_SUPERENV": "1",
            "PYBREW_XCRUN": str(xcrun),
            "PYBREW_CC": str(cc),
            "PYBREW_CXX": str(cxx),
            "LDFLAGS": f"-Wl,-syslibroot,{explicit_sdk}",
        },
    )
    env = prepared.env
    ldflags = shlex.split(env["LDFLAGS"])
    assert f"-Wl,-syslibroot,{explicit_sdk}" in ldflags
    assert f"-Wl,-syslibroot,{detected_sdk}" not in ldflags
    assert "-isysroot" not in ldflags
    assert str(detected_sdk) in shlex.split(env["CFLAGS"])
