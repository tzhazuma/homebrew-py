from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

from pybrew.cask import parse_cask_file
from pybrew.cask_installer import CaskInstaller, CaskLoader
from pybrew.paths import Config
from pybrew.taps import discover_taps

FIXTURES = Path(__file__).parent / "fixtures" / "casks"


def test_parse_rectangle_cask() -> None:
    cask = parse_cask_file(FIXTURES / "rectangle.rb", tap="homebrew/cask")
    assert cask.token == "rectangle"
    assert cask.version == "2.0.4"
    assert cask.name == "Rectangle"
    assert cask.resolved_sha256() == "9f820a2e56bd2c27cdda4fb4556de3cbcfcbac052d9eeb2179d49e2dae277abc"
    assert "Rectangle2.0.4.dmg" in cask.resolved_url()
    assert [artifact.kind for artifact in cask.artifacts] == ["app"]
    assert cask.artifacts[0].source == "Rectangle.app"
    assert cask.uninstall.get("quit") == "com.knollsoft.Rectangle"
    assert any("Rectangle" in entry for entry in cask.zap)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-version-conditional cask")
def test_parse_vscode_conditional_and_arch() -> None:
    cask = parse_cask_file(FIXTURES / "visual-studio-code.rb", tap="homebrew/cask")
    # on_monterey :or_newer is the current branch.
    assert cask.version == "1.143.1"
    assert "Microsoft Visual Studio Code" in (cask.name or "")
    assert cask.resolved_sha256()
    assert any(artifact.kind == "app" for artifact in cask.artifacts)
    assert any(artifact.kind == "binary" for artifact in cask.artifacts)
    url = cask.resolved_url()
    assert "1.143.1" in url


def test_cask_loader(tmp_path: Path) -> None:
    tap_dir = tmp_path / "tap"
    cask_dir = tap_dir / "Casks" / "t"
    cask_dir.mkdir(parents=True)
    (cask_dir / "tiny.rb").write_text(
        'cask "tiny" do\n  version "1.0"\n  sha256 "' + "0" * 64 + '"\n  url "file:///tmp/tiny.zip"\n  app "Tiny.app"\nend\n'
    )
    taps = discover_taps([tap_dir])
    loader = CaskLoader(taps)
    assert loader.names() == ["tiny"]
    assert loader.load("tiny").version == "1.0"


def test_install_zip_cask_end_to_end(run_cli, sample) -> None:
    proc = run_cli("cask", "install", "testapp")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    app = sample.prefix / "Applications" / "Test.app"
    assert (app / "Contents" / "MacOS" / "Test").is_file()
    binary = sample.prefix / "bin" / "test-cli"
    assert binary.is_symlink()
    assert binary.exists()

    room = sample.prefix / "Caskroom" / "testapp" / "1.0"
    receipt = json.loads((room / ".metadata" / "receipt.json").read_text())
    assert receipt["token"] == "testapp"
    assert str(app) in receipt["installed"]

    listing = run_cli("cask", "list", "--json")
    assert listing.returncode == 0
    assert any(row["token"] == "testapp" for row in json.loads(listing.stdout))

    info = run_cli("cask", "info", "testapp", "--json")
    assert info.returncode == 0
    payload = json.loads(info.stdout)
    assert payload["version"] == "1.0"
    assert payload["artifacts"][0]["kind"] == "app"


def test_uninstall_cask_removes_artifacts(run_cli, sample) -> None:
    assert run_cli("cask", "install", "testapp").returncode == 0
    proc = run_cli("cask", "uninstall", "testapp")
    assert proc.returncode == 0, proc.stderr
    assert not (sample.prefix / "Applications" / "Test.app").exists()
    assert not (sample.prefix / "bin" / "test-cli").exists()
    assert not (sample.prefix / "Caskroom" / "testapp").exists()


def test_cask_zap_removes_trash(run_cli, sample) -> None:
    assert run_cli("cask", "install", "testapp").returncode == 0
    sample.cask_zap.write_text("trash\n")
    proc = run_cli("cask", "uninstall", "--zap", "testapp")
    assert proc.returncode == 0, proc.stderr
    assert not sample.cask_zap.exists()


def test_cask_force_reinstall(run_cli, sample) -> None:
    assert run_cli("cask", "install", "testapp").returncode == 0
    again = run_cli("cask", "install", "testapp")
    assert again.returncode == 0
    assert "Already installed" in again.stdout
    forced = run_cli("cask", "install", "--force", "testapp")
    assert forced.returncode == 0, forced.stderr


def test_missing_cask_errors(run_cli) -> None:
    proc = run_cli("cask", "info", "not-a-real-cask")
    assert proc.returncode == 1
    assert "no cask named" in proc.stderr.lower()


def test_unsafe_zip_rejected(tmp_path: Path) -> None:
    from pybrew.cask_installer import _safe_extract_zip
    from pybrew.errors import PybrewError

    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../evil.txt", "pwned")
    with pytest.raises(PybrewError):
        _safe_extract_zip(archive, tmp_path / "extract")
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="dmg requires macOS hdiutil")
def test_install_dmg_cask(tmp_path: Path) -> None:
    import hashlib
    import subprocess

    source = tmp_path / "dmg-source"
    app = source / "Tiny.app" / "Contents"
    app.mkdir(parents=True)
    (app / "Info.plist").write_text("<plist><dict/></plist>")
    (app / "MacOS").mkdir()
    executable = app / "MacOS" / "Tiny"
    executable.write_text("#!/bin/sh\necho tiny\n")
    executable.chmod(0o755)
    dmg = tmp_path / "Tiny-1.0.dmg"
    result = subprocess.run(
        ["hdiutil", "create", "-volname", "Tiny", "-srcfolder", str(source), "-ov", "-format", "UDZO", str(dmg)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"hdiutil unavailable: {result.stderr}")

    tap = tmp_path / "tap"
    cask_dir = tap / "Casks" / "t"
    cask_dir.mkdir(parents=True)
    digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
    (cask_dir / "tiny.rb").write_text(
        f'cask "tiny" do\n  version "1.0"\n  sha256 "{digest}"\n  url "{dmg.as_uri()}"\n  app "Tiny.app"\nend\n'
    )
    config = Config.default(prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache")
    config.ensure()
    installer = CaskInstaller(config)
    loader = CaskLoader(discover_taps([tap]))
    result = installer.install(loader.load("tiny"))
    assert (config.applications / "Tiny.app" / "Contents" / "MacOS" / "Tiny").is_file()
    assert result.version == "1.0"
