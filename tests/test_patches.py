"""Tests for the formula ``patch`` DSL and its application to source builds.

Everything here is synthetic and local: patch/source URLs are ``file://``
URIs, so no test touches the network.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from pybrew.download import Downloader
from pybrew.errors import BuildError
from pybrew.installer import InstallOptions, Installer
from pybrew.models import Formula, PatchSpec
from pybrew.paths import Config
from pybrew.ruby_parser import parse_formula_source
from pybrew.source_builder import build_source
from pybrew.taps import FormulaLoader, discover_taps

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _diff(path: str, before: str, after: str, strip: int = 1) -> str:
    import difflib

    from_prefix = "a/" if strip else ""
    to_prefix = "b/" if strip else ""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{from_prefix}{path}",
            tofile=f"{to_prefix}{path}",
        )
    )


def _config(tmp_path: Path) -> Config:
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"
    )
    config.ensure()
    return config


def _downloader(tmp_path: Path) -> Downloader:
    return Downloader(tmp_path / "cache" / "downloads")


def _formula(name: str = "patched") -> Formula:
    formula = Formula(name=name, version="1.0")
    formula.install_source = 'def install\n  bin.install "hello.txt"\nend\n'
    return formula


def _build(
    tmp_path: Path,
    formula: Formula,
    *,
    downloader: Downloader | None = None,
    build_root: Path | None = None,
    keg: Path | None = None,
) -> Path:
    build_root = build_root or tmp_path / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    keg = keg or tmp_path / "keg"
    keg.mkdir(parents=True, exist_ok=True)
    build_source(formula, _config(tmp_path), build_root, keg, downloader=downloader)
    return keg


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


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_parse_patch_blocks_capture_url_sha256_and_strip() -> None:
    digest_a = "a" * 64
    digest_b = "b" * 64
    digest_c = "c" * 64
    source = f'''class PatchFixture < Formula
  url "https://example.com/patch-fixture-1.0.tar.gz"
  sha256 "{digest_a}"
  patch do
    url "https://example.com/default.patch"
    sha256 "{digest_b}"
  end
  patch :p0 do
    url "https://example.com/zero.patch"
    sha256 "{digest_c}"
  end
  patch do
    url "https://example.com/two.patch"
    sha256 "{digest_b}"
    strip :p2
  end
  def install
    bin.install "hello.txt"
  end
end
'''
    formula = parse_formula_source(source, name="patch-fixture")

    assert [(patch.strip, patch.url) for patch in formula.patches] == [
        (1, "https://example.com/default.patch"),
        (0, "https://example.com/zero.patch"),
        (2, "https://example.com/two.patch"),
    ]
    assert formula.patches[0].sha256 == digest_b
    assert formula.patches[1].sha256 == digest_c
    assert all(patch.data is None for patch in formula.patches)
    assert all(patch.unsupported_reason is None for patch in formula.patches)


def test_parse_inline_string_patch() -> None:
    diff = "--- hello.txt\n+++ hello.txt\n@@ -1 +1 @@\n-hello\n+inline\n"
    ruby_literal = diff.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    source = f'''class InlinePatchFixture < Formula
  url "https://example.com/inline-patch-1.0.tar.gz"
  sha256 "{"d" * 64}"
  patch :p0, "{ruby_literal}"
  def install
    bin.install "hello.txt"
  end
end
'''
    formula = parse_formula_source(source, name="inline-patch-fixture")

    assert len(formula.patches) == 1
    assert formula.patches[0].strip == 0
    assert formula.patches[0].data == diff
    assert formula.patches[0].url is None


def test_parse_patch_data_section() -> None:
    diff = _diff("hello.txt", "hello\n", "hello data\n")
    source = f'''class DataPatchFixture < Formula
  url "https://example.com/data-patch-1.0.tar.gz"
  sha256 "{"e" * 64}"
  patch :DATA
  def install
    bin.install "hello.txt"
  end
end

__END__
{diff}'''
    formula = parse_formula_source(source, name="data-patch-fixture")

    assert len(formula.patches) == 1
    assert formula.patches[0].strip == 1
    assert formula.patches[0].data == diff
    assert formula.patches[0].url is None
    # The DATA section must not disturb parsing of the install body.
    assert "bin.install" in formula.install_source


def test_parse_patch_data_with_strip() -> None:
    diff = _diff("hello.txt", "hello\n", "hello zero\n", strip=0)
    source = f'''class DataStripFixture < Formula
  url "https://example.com/data-strip-1.0.tar.gz"
  sha256 "{"f" * 64}"
  patch :p0, :DATA
  def install
    bin.install "hello.txt"
  end
end

__END__
{diff}'''
    formula = parse_formula_source(source, name="data-strip-fixture")

    assert formula.patches[0].strip == 0
    assert formula.patches[0].data == diff


def test_file_patch_is_captured() -> None:
    source = '''class FilePatchFixture < Formula
  url "https://example.com/file-patch-1.0.tar.gz"
  sha256 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  patch do
    file "Patches/fix.diff"
    type :unofficial
  end
  def install
    bin.install "hello.txt"
  end
end
'''
    formula = parse_formula_source(source, name="file-patch-fixture")

    assert len(formula.patches) == 1
    assert formula.patches[0].file == "Patches/fix.diff"
    assert formula.patches[0].unsupported_reason is None


def test_patch_data_without_end_section_is_reported() -> None:
    source = '''class MissingDataFixture < Formula
  url "https://example.com/missing-data-1.0.tar.gz"
  sha256 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  patch :DATA
  def install
    bin.install "hello.txt"
  end
end
'''
    formula = parse_formula_source(source, name="missing-data-fixture")

    assert len(formula.patches) == 1
    assert formula.patches[0].unsupported_reason
    assert "__END__" in formula.patches[0].unsupported_reason


def test_parse_class_level_post_install_block() -> None:
    source = '''class PostInstallFixture < Formula
  url "https://example.com/post-install-1.0.tar.gz"
  sha256 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  def install
    bin.install "hello.txt"
  end
  post_install do
    (prefix/"post-install-marker").write "ran"
  end
  depends_on "zlib"
end
'''
    formula = parse_formula_source(source, name="post-install-fixture")

    assert formula.post_install_source.startswith("do")
    assert "post-install-marker" in formula.post_install_source
    # The statements after the block must still be parsed.
    assert [dep.name for dep in formula.dependencies] == ["zlib"]
    assert "bin.install" in formula.install_source


def test_parse_def_post_install_is_preserved() -> None:
    source = '''class DefPostInstallFixture < Formula
  url "https://example.com/def-post-install-1.0.tar.gz"
  sha256 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  def install
    bin.install "hello.txt"
  end
  def post_install
    (prefix/"def-post-install-marker").write "ran"
  end
end
'''
    formula = parse_formula_source(source, name="def-post-install-fixture")

    assert formula.post_install_source.startswith("def post_install")
    assert "def-post-install-marker" in formula.post_install_source


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------


def test_file_url_patch_is_applied_before_install(tmp_path: Path) -> None:
    project = tmp_path / "src"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n")

    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(_diff("hello.txt", "hello\n", "hello patched\n"))

    formula = _formula()
    formula.patches.append(
        PatchSpec(url=patch_file.as_uri(), sha256=_sha(patch_file))
    )

    keg = _build(
        tmp_path, formula, downloader=_downloader(tmp_path), build_root=project
    )

    assert (keg / "bin" / "hello.txt").read_text() == "hello patched\n"


def test_strip_zero_patch_uses_plain_paths(tmp_path: Path) -> None:
    project = tmp_path / "src"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n")

    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(_diff("hello.txt", "hello\n", "hello zero\n", strip=0))

    formula = _formula()
    formula.patches.append(
        PatchSpec(url=patch_file.as_uri(), sha256=_sha(patch_file), strip=0)
    )

    keg = _build(
        tmp_path, formula, downloader=_downloader(tmp_path), build_root=project
    )

    assert (keg / "bin" / "hello.txt").read_text() == "hello zero\n"


def test_inline_data_patch_applies_without_downloader(tmp_path: Path) -> None:
    project = tmp_path / "src"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n")

    formula = _formula()
    formula.patches.append(
        PatchSpec(data=_diff("hello.txt", "hello\n", "hello inline\n"))
    )

    keg = _build(tmp_path, formula, downloader=None, build_root=project)

    assert (keg / "bin" / "hello.txt").read_text() == "hello inline\n"


def test_data_patch_from_parsed_formula_is_applied(tmp_path: Path) -> None:
    project = tmp_path / "src"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n")

    diff = _diff("hello.txt", "hello\n", "hello data\n")
    source = f'''class DataPatchFixture < Formula
  url "https://example.com/data-patch-1.0.tar.gz"
  sha256 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  patch :DATA
  def install
    bin.install "hello.txt"
  end
end

__END__
{diff}'''
    formula = parse_formula_source(source, name="data-patch-fixture")

    keg = _build(tmp_path, formula, downloader=None, build_root=project)

    assert (keg / "bin" / "hello.txt").read_text() == "hello data\n"


def test_patches_apply_in_declaration_order(tmp_path: Path) -> None:
    project = tmp_path / "src"
    project.mkdir()
    (project / "hello.txt").write_text("first\n")

    first = tmp_path / "first.patch"
    first.write_text(_diff("hello.txt", "first\n", "second\n", strip=0))
    second = tmp_path / "second.patch"
    second.write_text(_diff("hello.txt", "second\n", "third\n", strip=0))

    formula = _formula()
    formula.patches.extend(
        [
            PatchSpec(url=first.as_uri(), sha256=_sha(first), strip=0),
            PatchSpec(url=second.as_uri(), sha256=_sha(second), strip=0),
        ]
    )

    keg = _build(
        tmp_path, formula, downloader=_downloader(tmp_path), build_root=project
    )

    assert (keg / "bin" / "hello.txt").read_text() == "third\n"


def test_failed_patch_aborts_before_install(tmp_path: Path) -> None:
    project = tmp_path / "src"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n")

    patch_file = tmp_path / "broken.patch"
    patch_file.write_text(_diff("hello.txt", "does-not-exist\n", "nope\n", strip=0))

    formula = _formula()
    formula.patches.append(
        PatchSpec(url=patch_file.as_uri(), sha256=_sha(patch_file), strip=0)
    )
    keg = tmp_path / "keg"
    keg.mkdir()

    with pytest.raises(BuildError, match=r"patch #1 failed"):
        _build(
            tmp_path,
            formula,
            downloader=_downloader(tmp_path),
            build_root=project,
            keg=keg,
        )

    # The install DSL must not have run.
    assert not (keg / "bin").exists()


def test_unsupported_patch_source_fails_clearly(tmp_path: Path) -> None:
    project = tmp_path / "src"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n")

    source = '''class FilePatchFixture < Formula
  url "https://example.com/file-patch-1.0.tar.gz"
  sha256 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  patch do
    file "Patches/fix.diff"
  end
  def install
    bin.install "hello.txt"
  end
end
'''
    formula = parse_formula_source(source, name="file-patch-fixture")

    with pytest.raises(BuildError, match="needs a formula path"):
        _build(tmp_path, formula, downloader=None, build_root=project)


def test_missing_patch_sha256_is_an_error(tmp_path: Path) -> None:
    project = tmp_path / "src"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n")
    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(_diff("hello.txt", "hello\n", "hello patched\n"))

    formula = _formula()
    formula.patches.append(PatchSpec(url=patch_file.as_uri()))

    with pytest.raises(BuildError, match="sha256"):
        _build(
            tmp_path,
            formula,
            downloader=_downloader(tmp_path),
            build_root=project,
        )


def test_missing_patch_downloader_is_an_error(tmp_path: Path) -> None:
    project = tmp_path / "src"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n")
    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(_diff("hello.txt", "hello\n", "hello patched\n"))

    formula = _formula()
    formula.patches.append(
        PatchSpec(url=patch_file.as_uri(), sha256=_sha(patch_file))
    )

    with pytest.raises(BuildError, match="downloader"):
        _build(tmp_path, formula, downloader=None, build_root=project)


def test_patch_checksum_mismatch_is_an_error(tmp_path: Path) -> None:
    project = tmp_path / "src"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n")
    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(_diff("hello.txt", "hello\n", "hello patched\n"))

    formula = _formula()
    formula.patches.append(
        PatchSpec(url=patch_file.as_uri(), sha256="0" * 64)
    )

    with pytest.raises(BuildError, match="SHA-256"):
        _build(
            tmp_path,
            formula,
            downloader=_downloader(tmp_path),
            build_root=project,
        )


def test_no_patches_needs_no_downloader(tmp_path: Path) -> None:
    project = tmp_path / "src"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n")

    formula = _formula()
    assert formula.patches == []

    keg = _build(tmp_path, formula, downloader=None, build_root=project)

    assert (keg / "bin" / "hello.txt").read_text() == "hello\n"


def test_pybrew_patch_environment_override(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "src"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n")

    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(_diff("hello.txt", "hello\n", "hello patched\n"))

    log = tmp_path / "patch-args.txt"
    fake_patch = tmp_path / "fake-patch"
    fake_patch.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$PYBREW_PATCH_LOG"\n'
        "exit 1\n"
    )
    fake_patch.chmod(0o755)
    monkeypatch.setenv("PYBREW_PATCH", str(fake_patch))
    monkeypatch.setenv("PYBREW_PATCH_LOG", str(log))

    formula = _formula()
    formula.patches.append(
        PatchSpec(url=patch_file.as_uri(), sha256=_sha(patch_file))
    )

    with pytest.raises(BuildError, match=r"patch #1 failed"):
        _build(
            tmp_path,
            formula,
            downloader=_downloader(tmp_path),
            build_root=project,
        )

    recorded = log.read_text().splitlines()
    assert "-p1" in recorded
    assert "-i" in recorded


# ---------------------------------------------------------------------------
# installer integration
# ---------------------------------------------------------------------------


def test_installer_applies_patch_from_formula_source(tmp_path: Path) -> None:
    project = tmp_path / "hello-1.0"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n")
    archive = _tar(project, tmp_path / "src" / "hello-1.0.tar.gz", "hello-1.0")

    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(_diff("hello.txt", "hello\n", "hello installed patched\n"))

    tap = tmp_path / "tap"
    (tap / "Formula").mkdir(parents=True)
    (tap / "Formula" / "hello.rb").write_text(
        f'''class Hello < Formula
  url "{archive.as_uri()}"
  sha256 "{_sha(archive)}"
  patch do
    url "{patch_file.as_uri()}"
    sha256 "{_sha(patch_file)}"
  end
  def install
    bin.install "hello.txt"
  end
end
'''
    )

    config = _config(tmp_path)
    loader = FormulaLoader(discover_taps([tap]))
    installer = Installer(config, loader)
    installer.install(loader.load("hello"), InstallOptions(build_from_source=True))

    installed = config.keg_path("hello", "1.0") / "bin" / "hello.txt"
    assert installed.read_text() == "hello installed patched\n"


def test_patch_file_is_resolved_relative_to_formula(tmp_path: Path) -> None:
    build_root = tmp_path / "build"
    build_root.mkdir()
    (build_root / "hello.txt").write_text("hello\n")

    formula_dir = tmp_path / "tap" / "Formula"
    formula_dir.mkdir(parents=True)
    patch_file = formula_dir / "local.patch"
    patch_file.write_text(_diff("hello.txt", "hello\n", "hello local\n"))
    formula = _formula()
    formula.path = formula_dir / "patched.rb"
    formula.patches = [PatchSpec(file="local.patch", strip=1)]

    keg = _build(tmp_path, formula, build_root=build_root)
    assert (keg / "bin" / "hello.txt").read_text() == "hello local\n"


def test_patch_file_missing_fails_clearly(tmp_path: Path) -> None:
    formula = _formula()
    formula.path = tmp_path / "Formula" / "patched.rb"
    formula.patches = [PatchSpec(file="missing.patch")]

    with pytest.raises(BuildError, match="cannot read patch"):
        _build(tmp_path, formula)
