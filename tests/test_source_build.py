from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from pybrew.installer import InstallOptions, Installer
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
