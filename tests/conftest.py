"""Shared fixtures for the pybrew test-suite."""

from __future__ import annotations

import hashlib
import io
import re
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pybrew.models import candidate_bottle_tags  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def _add_file(tar: tarfile.TarFile, arcname: str, data: bytes, mode: int = 0o755) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    tar.addfile(info, io.BytesIO(data))


def _add_symlink(tar: tarfile.TarFile, arcname: str, target: str) -> None:
    info = tarfile.TarInfo(arcname)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    info.mode = 0o777
    info.mtime = 0
    tar.addfile(info)


def make_source_tarball(path: Path, name: str = "hello", version: str = "1.0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    top = f"{name}-{version}"
    with tarfile.open(path, "w:gz") as tar:
        _add_file(tar, f"{top}/{name}", f"#!/bin/sh\necho hello from source\n".encode())
        _add_file(tar, f"{top}/README", b"hello README\n", mode=0o644)
    return path


def make_bottle(
    path: Path,
    name: str,
    version: str,
    tag: str,
    *,
    placeholder_script: bool = True,
    extra_entries: dict[str, bytes] | None = None,
    symlinks: dict[str, str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{name}/{version}"
    script = b"#!/bin/sh\n"
    if placeholder_script:
        script += b'echo "@@HOMEBREW_PREFIX@@/bin/hello"\n'
    else:
        script += b"echo hello\n"
    with tarfile.open(path, "w:gz") as tar:
        _add_file(tar, f"{prefix}/bin/{name}", script)
        _add_file(tar, f"{prefix}/lib/lib{name}.dylib", f"dylib {name}\n".encode(), mode=0o644)
        _add_file(tar, f"{prefix}/share/doc/{name}/README", b"docs\n", mode=0o644)
        if extra_entries:
            for arcname, data in extra_entries.items():
                _add_file(tar, f"{prefix}/{arcname}", data, mode=0o644)
        if symlinks:
            for arcname, target in symlinks.items():
                _add_symlink(tar, f"{prefix}/{arcname}", target)
    return path


def formula_ruby(
    name: str,
    *,
    version: str | None = None,
    source_url: str,
    source_sha: str,
    bottle_root: str,
    bottle_sha: str,
    tag: str,
    deps: list[str] | None = None,
    keg_only: bool = False,
    install_body: str = 'bin.install "hello"\nprefix.install "README"',
) -> str:
    lines = [
        f"class {''.join(part.capitalize() for part in re.split(r'[-_]', name))} < Formula",
        f'  desc "Test formula for {name}"',
        f'  homepage "https://example.com/{name}"',
        f'  url "{source_url}"',
        f'  sha256 "{source_sha}"',
        '  license "MIT"',
    ]
    if version:
        lines.append(f'  version "{version}"')
    if keg_only:
        lines.append('  keg_only "test keg-only"')
    lines.append("  bottle do")
    lines.append(f'    root_url "{bottle_root}"')
    lines.append(f'    sha256 cellar: :any, {tag}: "{bottle_sha}"')
    lines.append("  end")
    for dep in deps or []:
        lines.append(f'  depends_on "{dep}"')
    lines.append("")
    lines.append("  def install")
    for body_line in install_body.splitlines():
        lines.append(f"    {body_line}")
    lines.append("  end")
    lines.append("end")
    return "\n".join(lines) + "\n"


@pytest.fixture
def bottle_tag() -> str:
    return candidate_bottle_tags()[0]


@pytest.fixture
def sample(tmp_path: Path, bottle_tag: str) -> SimpleNamespace:
    """A complete local tap: source tarball, bottle tarball and formula."""

    root = tmp_path / "sample"
    source = make_source_tarball(root / "src" / "hello-1.0.tar.gz")
    bottle = make_bottle(
        root / "bottles" / f"hello-1.0.{bottle_tag}.bottle.tar.gz",
        "hello",
        "1.0",
        bottle_tag,
        symlinks={"lib/libhello.1.dylib": "libhello.dylib"},
    )
    tap = root / "tap"
    (tap / "Formula").mkdir(parents=True)
    formula = tap / "Formula" / "hello.rb"
    formula.write_text(
        formula_ruby(
            "hello",
            source_url=source.as_uri(),
            source_sha=sha256_file(source),
            bottle_root=(root / "bottles").as_uri(),
            bottle_sha=sha256_file(bottle),
            tag=bottle_tag,
        )
    )

    dep = make_bottle(
        root / "bottles" / f"dep-2.0.{bottle_tag}.bottle.tar.gz",
        "dep",
        "2.0",
        bottle_tag,
    )
    (tap / "Formula" / "dep.rb").write_text(
        formula_ruby(
            "dep",
            version="2.0",
            source_url=source.as_uri(),
            source_sha=sha256_file(source),
            bottle_root=(root / "bottles").as_uri(),
            bottle_sha=sha256_file(dep),
            tag=bottle_tag,
            install_body='bin.install "dep"',
        )
    )
    hello_dep_bottle = make_bottle(
        root / "bottles" / f"hello-dep-1.0.{bottle_tag}.bottle.tar.gz",
        "hello-dep",
        "1.0",
        bottle_tag,
    )
    (tap / "Formula" / "hello-dep.rb").write_text(
        formula_ruby(
            "hello-dep",
            source_url=source.as_uri(),
            source_sha=sha256_file(source),
            bottle_root=(root / "bottles").as_uri(),
            bottle_sha=sha256_file(hello_dep_bottle),
            tag=bottle_tag,
            deps=["dep"],
            install_body='bin.install "dep"',
        )
    )
    # A cask fixture: Test.app inside a zip, plus a binary inside the app.
    cask_src = root / "cask-src" / "Test.app" / "Contents"
    (cask_src / "MacOS").mkdir(parents=True)
    (cask_src / "Info.plist").write_text("<plist><dict/></plist>", encoding="utf-8")
    app_binary = cask_src / "MacOS" / "Test"
    app_binary.write_text("#!/bin/sh\necho test\n", encoding="utf-8")
    app_binary.chmod(0o755)
    cask_zip = root / "casks" / "TestApp-1.0.zip"
    cask_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(cask_zip, "w") as bundle:
        for path in sorted((root / "cask-src").rglob("*")):
            arcname = str(path.relative_to(root / "cask-src"))
            if path.is_dir():
                continue
            info = zipfile.ZipInfo(arcname)
            info.external_attr = (path.stat().st_mode & 0o777) << 16
            bundle.writestr(info, path.read_bytes())
    zap_path = root / "zapme.txt"
    cask_dir = tap / "Casks" / "t"
    cask_dir.mkdir(parents=True)
    (cask_dir / "testapp.rb").write_text(
        f'''cask "testapp" do
  version "1.0"
  sha256 "{sha256_file(cask_zip)}"
  url "{cask_zip.as_uri()}"
  name "Test App"
  desc "test cask"
  homepage "https://example.com/testapp"
  app "Test.app"
  binary "Test.app/Contents/MacOS/Test", target: "test-cli"
  uninstall quit: "com.example.test"
  zap trash: ["{zap_path}"]
end
'''
    )
    (tap / "Formula" / "svc.rb").write_text(
        f'''class Svc < Formula
  desc "service fixture"
  homepage "https://example.com/svc"
  url "{source.as_uri()}"
  sha256 "{sha256_file(source)}"
  license "MIT"

  service do
    run [opt_bin/"svc", "--foreground"]
    keep_alive true
  end
end
'''
    )
    return SimpleNamespace(
        root=root,
        prefix=root / "prefix",
        cache=root / "cache",
        tap=tap,
        formula=formula,
        source=source,
        bottle=bottle,
        dep_bottle=dep,
        hello_dep_bottle=hello_dep_bottle,
        tag=bottle_tag,
        bottle_sha=sha256_file(bottle),
        cask_zip=cask_zip,
        cask_zap=zap_path,
    )


@pytest.fixture
def run_cli(sample: SimpleNamespace):
    def run(
        *args: str,
        bottle_tag: str | None = None,
        prefix: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        env.pop("HOMEBREW_BOTTLE_TAG", None)
        env.pop("PYBREW_BOTTLE_TAG", None)
        env.setdefault("PYBREW_SERVICE_DIR", str(sample.root / "service-agents"))
        env.setdefault("PYBREW_SERVICE_BACKEND", "launchd")
        if extra_env:
            env.update(extra_env)
        if bottle_tag:
            env["PYBREW_BOTTLE_TAG"] = bottle_tag
        command = [
            sys.executable,
            "-m",
            "pybrew",
            "--prefix",
            str(prefix or sample.prefix),
            "--tap",
            str(sample.tap),
            "--cache",
            str(sample.cache),
            *args,
        ]
        return subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)

    return run
