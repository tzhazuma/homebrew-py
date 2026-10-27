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


def test_install_cask_flag(run_cli, sample) -> None:
    proc = run_cli("install", "--cask", "testapp")
    assert proc.returncode == 0, proc.stderr
    assert (sample.prefix / "Applications" / "Test.app").is_dir()
    removed = run_cli("uninstall", "--cask", "testapp")
    assert removed.returncode == 0, removed.stderr
    assert not (sample.prefix / "Applications" / "Test.app").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="pkgbuild requires macOS")
def test_cask_pkg_payload(tmp_path: Path) -> None:
    import hashlib
    import shutil
    import subprocess

    if shutil.which("pkgbuild") is None:
        pytest.skip("pkgbuild not available")
    root = tmp_path / "pkg-root"
    target = root / "usr" / "local" / "share" / "pkgtest"
    target.mkdir(parents=True)
    (target / "hello.txt").write_text("pkg payload\n")
    package = tmp_path / "PkgTest-1.0.pkg"
    result = subprocess.run(
        [
            "pkgbuild",
            "--root",
            str(root),
            "--identifier",
            "com.example.pkgtest",
            "--version",
            "1.0",
            "--install-location",
            "/",
            str(package),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"pkgbuild failed: {result.stderr}")

    tap = tmp_path / "tap"
    cask_dir = tap / "Casks" / "p"
    cask_dir.mkdir(parents=True)
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    (cask_dir / "pkgtest.rb").write_text(
        f'cask "pkgtest" do\n  version "1.0"\n  sha256 "{digest}"\n'
        f'  url "{package.as_uri()}"\n  pkg "PkgTest-1.0.pkg"\nend\n'
    )
    config = Config.default(prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache")
    config.ensure()
    pkg_root = tmp_path / "installed-pkg-root"
    installer = CaskInstaller(config, pkg_root=pkg_root)
    loader = CaskLoader(discover_taps([tap]))
    installer.install(loader.load("pkgtest"))
    assert (pkg_root / "usr" / "local" / "share" / "pkgtest" / "hello.txt").read_text() == "pkg payload\n"
    installer.uninstall("pkgtest")
    assert not (pkg_root / "usr" / "local" / "share" / "pkgtest" / "hello.txt").exists()


def test_cask_from_api_and_loader_fallback(tmp_path: Path, monkeypatch) -> None:
    from pybrew.api import cask_from_api
    from pybrew.cask_installer import CaskLoader

    payload = {
        "token": "apicask",
        "name": ["API Cask", "Alias"],
        "desc": "from api",
        "homepage": "https://example.com",
        "url": "https://example.com/API-2.0.zip",
        "version": "2.0",
        "sha256": "a" * 64,
        "auto_updates": True,
        "depends_on": {"macos": {}},
        "conflicts_with": {"cask": ["other"]},
        "artifacts": [
            {"uninstall": [{"quit": "com.example.api"}]},
            {"app": ["API Cask.app"], "target": "/Applications/API Cask.app"},
            {"binary": ["API Cask.app/Contents/MacOS/api"], "target": "$HOMEBREW_PREFIX/bin/api"},
            {"zap": [{"trash": ["~/Library/Caches/com.example.api"]}]},
        ],
    }
    cask = cask_from_api(payload, tap="homebrew/cask")
    assert cask.token == "apicask"
    assert cask.version == "2.0"
    assert cask.name == "API Cask, Alias"
    assert cask.artifacts[0].kind == "app"
    assert cask.artifacts[0].target == "API Cask.app"
    assert cask.artifacts[1].kind == "binary"
    assert cask.artifacts[1].target == "api"
    assert cask.uninstall["quit"] == "com.example.api"
    assert cask.zap == ["~/Library/Caches/com.example.api"]

    monkeypatch.setattr("pybrew.cask_installer.fetch_cask", lambda token: cask)
    loader = CaskLoader([], allow_api=True)
    assert loader.load("apicask").token == "apicask"


def test_info_and_list_cask_commands(run_cli, sample) -> None:
    assert run_cli("install", "--cask", "testapp").returncode == 0
    listing = run_cli("list", "--cask", "--json")
    assert listing.returncode == 0, listing.stderr
    assert any(row["token"] == "testapp" for row in json.loads(listing.stdout)["casks"])
    info = run_cli("info", "--cask", "--json", "testapp")
    assert info.returncode == 0, info.stderr
    assert json.loads(info.stdout)["casks"][0]["token"] == "testapp"


def test_cask_outdated_and_upgrade(run_cli, sample) -> None:
    assert run_cli("install", "--cask", "testapp").returncode == 0
    cask_file = sample.tap / "Casks" / "t" / "testapp.rb"
    cask_file.write_text(cask_file.read_text().replace('version "1.0"', 'version "2.0"'))
    outdated = run_cli("cask", "outdated")
    assert outdated.returncode == 0, outdated.stderr
    assert "testapp 1.0 < 2.0" in outdated.stdout
    payload = json.loads(run_cli("cask", "outdated", "--json").stdout)
    assert payload["casks"][0]["current"] == "2.0"

    upgraded = run_cli("cask", "upgrade", "testapp")
    assert upgraded.returncode == 0, upgraded.stderr
    assert "Upgraded cask: testapp 2.0" in upgraded.stdout
    assert (sample.prefix / "Caskroom" / "testapp" / "2.0" / ".metadata" / "receipt.json").is_file()
    assert not (sample.prefix / "Caskroom" / "testapp" / "1.0").exists()
    assert (sample.prefix / "Applications" / "Test.app").is_dir()


@pytest.mark.skipif(sys.platform != "darwin", reason="quit uses macOS osascript")
def test_cask_uninstall_quit_and_delete(run_cli, sample, tmp_path) -> None:
    assert run_cli("install", "--cask", "testapp").returncode == 0
    leftover = tmp_path / "leftover.txt"
    leftover.write_text("leftover\n")
    cask_file = sample.tap / "Casks" / "t" / "testapp.rb"
    cask_file.write_text(
        cask_file.read_text().replace(
            'uninstall quit: "com.example.test"',
            f'uninstall quit: "com.example.test", pkgutil: "com.example.test", delete: ["{leftover}"]',
        )
    )
    fake = tmp_path / "fake-osascript"
    log = tmp_path / "osascript.log"
    fake.write_text('#!/bin/sh\necho "$@" >> "$FAKE_OSASCRIPT_LOG"\nexit 0\n')
    fake.chmod(0o755)
    fake_pkgutil = tmp_path / "fake-pkgutil"
    pkgutil_log = tmp_path / "pkgutil.log"
    fake_pkgutil.write_text('#!/bin/sh\necho "$@" >> "$FAKE_PKGUTIL_LOG"\nexit 0\n')
    fake_pkgutil.chmod(0o755)
    proc = run_cli(
        "cask",
        "uninstall",
        "testapp",
        extra_env={
            "PYBREW_OSASCRIPT": str(fake),
            "FAKE_OSASCRIPT_LOG": str(log),
            "PYBREW_PKGUTIL": str(fake_pkgutil),
            "FAKE_PKGUTIL_LOG": str(pkgutil_log),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert 'tell application id "com.example.test" to quit' in log.read_text()
    assert "--forget com.example.test" in pkgutil_log.read_text()
    assert not leftover.exists()


def test_cask_reinstall(run_cli, sample) -> None:
    assert run_cli("cask", "install", "testapp").returncode == 0
    proc = run_cli("cask", "reinstall", "testapp")
    assert proc.returncode == 0, proc.stderr
    assert "Reinstalled cask: testapp 1.0" in proc.stdout
    assert (sample.prefix / "Applications" / "Test.app").is_dir()
    alias = run_cli("reinstall", "--cask", "testapp")
    assert alias.returncode == 0, alias.stderr
    assert "Reinstalled cask: testapp" in alias.stdout
