from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from pybrew.cask import Artifact, Cask, CaskError, parse_cask_file, parse_cask_source
from pybrew.cask_installer import CaskInstaller, CaskLoader
from pybrew.paths import Config
from pybrew.taps import discover_taps

FIXTURES = Path(__file__).parent / "fixtures" / "casks"

_RUBY = os.environ.get("PYBREW_RUBY") or "/usr/bin/ruby"
requires_ruby = pytest.mark.skipif(
    shutil.which(_RUBY) is None and not Path(_RUBY).is_file(),
    reason="Ruby interpreter not installed; cask script shim tests skipped",
)


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
    assert set(cask.sha256_map) == {"arm", "intel"}
    assert cask.uninstall.get("quit") == "com.microsoft.VSCode"
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
            {"font": ["Example.ttf"], "target": "/Users/example/Library/Fonts/Example.ttf"},
            {"stage_only": True},
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
    assert any(artifact.kind == "font" and artifact.target == "Example.ttf" for artifact in cask.artifacts)
    assert cask.stage_only is True

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


def test_multiline_keyword_arguments_are_preserved() -> None:
    arm_sha = "a" * 64
    intel_sha = "b" * 64
    cask = parse_cask_source(
        f'''cask "multiline" do
  version "1.0"
  sha256 arm:   "{arm_sha}",
         intel: "{intel_sha}"
  url "https://example.com/Multiline.zip"
  uninstall launchctl: "com.example.helper",
            quit:      "com.example.app"
  app "Multiline.app"
end
'''
    )
    assert cask.sha256_map == {"arm": arm_sha, "intel": intel_sha}
    assert cask.uninstall == {
        "launchctl": "com.example.helper",
        "quit": "com.example.app",
    }


def test_install_completion_artifacts(tmp_path: Path) -> None:
    archive = tmp_path / "Complety.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("completions/tool.bash", "complete -W tool tool\n")
        bundle.writestr("completions/_tool", "#compdef tool\n")
        bundle.writestr("completions/tool.fish", "complete -c tool\n")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "c" / "complety.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "complety" do
  version "1.0"
  sha256 "{digest}"
  url "{archive.as_uri()}"
  bash_completion "completions/tool.bash"
  zsh_completion "completions/_tool"
  fish_completion "completions/tool.fish"
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    installer.install(CaskLoader(discover_taps([tap])).load("complety"))

    assert (config.prefix / "share/bash-completion/completions/tool.bash").is_file()
    assert (config.prefix / "share/zsh/site-functions/_tool").is_file()
    assert (config.prefix / "share/fish/vendor_completions.d/tool.fish").is_file()


def test_install_manpage_artifact(tmp_path: Path) -> None:
    archive = tmp_path / "Manly.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("man/tool.1", ".TH TOOL 1\n")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "m" / "manly.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "manly" do
  version "1.0"
  sha256 "{digest}"
  url "{archive.as_uri()}"
  manpage "man/tool.1"
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    installer.install(CaskLoader(discover_taps([tap])).load("manly"))

    manpage = config.prefix / "share/man/man1/tool.1"
    assert manpage.is_file()
    assert ".TH TOOL 1" in manpage.read_text()


def test_install_rename_then_artifact(tmp_path: Path) -> None:
    archive = tmp_path / "Renamy.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Old.app/Contents/Info.plist", "<plist/>")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "r" / "renamy.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "renamy" do
  version "1.0"
  sha256 "{digest}"
  url "{archive.as_uri()}"
  rename "Old.app", "New.app"
  app "New.app"
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    installer.install(CaskLoader(discover_taps([tap])).load("renamy"))

    assert (tmp_path / "Applications" / "New.app" / "Contents" / "Info.plist").is_file()
    assert not (tmp_path / "Applications" / "Old.app").exists()


def test_parse_container_type() -> None:
    cask = parse_cask_source(
        'cask "containy" do\n'
        '  version "1.0"\n'
        '  container type: :nupkg\n'
        '  app "Containy.app"\n'
        'end\n'
    )
    assert cask.container == "nupkg"


def test_install_appimage_artifact(tmp_path: Path) -> None:
    archive = tmp_path / "Appy.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Appy.AppImage", "#!/bin/sh\necho appy\n")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "a" / "appy.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "appy" do
  version "1.0"
  sha256 "{digest}"
  url "{archive.as_uri()}"
  appimage "Appy.AppImage"
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    installer.install(CaskLoader(discover_taps([tap])).load("appy"))

    image = tmp_path / "Applications" / "Appy.AppImage"
    assert image.is_file()
    assert image.stat().st_mode & 0o111


def test_install_7z_container_with_fake_extractor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "Seven.zip"
    archive.write_bytes(b"fake-7z-archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    extractor = tmp_path / "fake-7z"
    extractor.write_text(
        """#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    -o*) dest="${arg#-o}" ;;
  esac
done
mkdir -p "$dest/Tool.app/Contents"
printf 'seven' > "$dest/Tool.app/Contents/Info.plist"
""",
        encoding="utf-8",
    )
    extractor.chmod(0o755)
    monkeypatch.setenv("PYBREW_7Z", str(extractor))

    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "s" / "seven.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "seven" do
  version "1.0"
  sha256 "{digest}"
  url "{archive.as_uri()}"
  container type: :7z
  app "Tool.app"
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    installer.install(CaskLoader(discover_taps([tap])).load("seven"))

    assert (tmp_path / "Applications" / "Tool.app" / "Contents" / "Info.plist").is_file()


def test_uninstall_launchctl_and_signal_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "u" / "uninstally.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "uninstally" do
  version "1.0"
  stage_only true
  uninstall launchctl: "com.example.helper", signal: ["TERM", "Uninstally"]
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    cask = CaskLoader(discover_taps([tap])).load("uninstally")
    installer.install(cask)

    log = tmp_path / "actions.log"
    launchctl = tmp_path / "fake-launchctl"
    launchctl.write_text('#!/bin/sh\necho "launchctl $*" >> "$ACTION_LOG"\n', encoding="utf-8")
    launchctl.chmod(0o755)
    pkill = tmp_path / "fake-pkill"
    pkill.write_text('#!/bin/sh\necho "pkill $*" >> "$ACTION_LOG"\n', encoding="utf-8")
    pkill.chmod(0o755)
    monkeypatch.setenv("PYBREW_LAUNCHCTL", str(launchctl))
    monkeypatch.setenv("PYBREW_PKILL", str(pkill))
    monkeypatch.setenv("ACTION_LOG", str(log))
    monkeypatch.setattr("pybrew.cask_installer.sys.platform", "darwin")

    installer.uninstall("uninstally", cask=cask)

    actions = log.read_text()
    assert "launchctl bootout gui/" in actions
    assert "com.example.helper" in actions
    assert "pkill -TERM -f Uninstally" in actions


def test_uninstall_login_item_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "l" / "loginly.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "loginly" do
  version "1.0"
  stage_only true
  uninstall login_item: "Loginly"
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    cask = CaskLoader(discover_taps([tap])).load("loginly")
    installer.install(cask)

    log = tmp_path / "osascript.log"
    osascript = tmp_path / "fake-osascript"
    osascript.write_text('#!/bin/sh\necho "$*" >> "$OSA_LOG"\n', encoding="utf-8")
    osascript.chmod(0o755)
    monkeypatch.setenv("PYBREW_OSASCRIPT", str(osascript))
    monkeypatch.setenv("OSA_LOG", str(log))
    monkeypatch.setattr("pybrew.cask_installer.sys.platform", "darwin")

    installer.uninstall("loginly", cask=cask)

    assert 'delete login item "Loginly"' in log.read_text()


def test_disabled_cask_is_rejected(tmp_path: Path) -> None:
    cask = parse_cask_source(
        'cask "gone" do\n'
        '  version "1.0"\n'
        '  disable! because: "no longer maintained"\n'
        '  app "Gone.app"\n'
        'end\n'
    )
    assert cask.disabled is True
    assert cask.disable_reason == "no longer maintained"

    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "g" / "gone.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        'cask "gone" do\n'
        '  version "1.0"\n'
        '  disable! because: "no longer maintained"\n'
        '  app "Gone.app"\n'
        'end\n'
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    with pytest.raises(CaskError, match="disabled"):
        installer.install(CaskLoader(discover_taps([tap])).load("gone"))


def test_cask_arch_dependency_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "a" / "archy.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "archy" do
  version "1.0"
  stage_only true
  depends_on arch: :arm64
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    monkeypatch.setattr("pybrew.cask_installer.platform.machine", lambda: "x86_64")
    with pytest.raises(CaskError, match="requires arch arm64"):
        installer.install(CaskLoader(discover_taps([tap])).load("archy"))


def test_cask_named_dependencies_and_conflicts(tmp_path: Path) -> None:
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "d" / "dependsy.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "dependsy" do
  version "1.0"
  stage_only true
  depends_on formula: "missing-formula"
  depends_on cask: "missing-cask"
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    with pytest.raises(CaskError, match="requires formula"):
        installer.install(CaskLoader(discover_taps([tap])).load("dependsy"))

    (config.cellar / "missing-formula").mkdir(parents=True)
    with pytest.raises(CaskError, match="requires cask"):
        installer.install(CaskLoader(discover_taps([tap])).load("dependsy"))

    (config.caskroom / "missing-cask").mkdir(parents=True)
    conflict_path = tap / "Casks" / "c" / "conflicty.rb"
    conflict_path.parent.mkdir(parents=True)
    conflict_path.write_text(
        '''cask "conflicty" do
  version "1.0"
  stage_only true
  conflicts_with cask: "missing-cask"
end
'''
    )
    with pytest.raises(CaskError, match="conflicts with installed cask"):
        installer.install(CaskLoader(discover_taps([tap])).load("conflicty"))


def test_cask_macos_dependency_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "m" / "macosy.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "macosy" do
  version "1.0"
  stage_only true
  depends_on macos: ">= :sonoma"
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    monkeypatch.setenv("PYBREW_MACOS_VERSION", "13.0")
    with pytest.raises(CaskError, match="requires macOS"):
        installer.install(CaskLoader(discover_taps([tap])).load("macosy"))

    monkeypatch.setenv("PYBREW_MACOS_VERSION", "14.0")
    installer.install(CaskLoader(discover_taps([tap])).load("macosy"))


def test_cask_deprecation_reason() -> None:
    cask = parse_cask_source(
        'cask "oldy" do\n'
        '  version "1.0"\n'
        '  deprecate! because: "replaced by newy"\n'
        '  app "Oldy.app"\n'
        'end\n'
    )
    assert cask.deprecated is True
    assert cask.deprecation_reason == "replaced by newy"


def test_cask_livecheck_source_is_captured() -> None:
    cask = parse_cask_source(
        'cask "livey" do\n'
        '  version "1.0"\n'
        '  livecheck do\n'
        '    url :url\n'
        '    regex(/v(\\d+)/i)\n'
        '  end\n'
        '  app "Livey.app"\n'
        'end\n'
    )
    assert cask.livecheck_source is not None
    assert "regex" in cask.livecheck_source


def test_cask_no_autobump_metadata() -> None:
    cask = parse_cask_source(
        'cask "bumpy" do\n'
        '  version "1.0"\n'
        '  no_autobump! because: "version is manual"\n'
        '  app "Bumpy.app"\n'
        'end\n'
    )
    assert cask.no_autobump is True
    assert cask.no_autobump_reason == "version is manual"


def test_cask_os_stanza_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "o" / "osy.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "osy" do
  version "1.0"
  stage_only true
  os macos: ">= :sonoma"
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    monkeypatch.setattr("pybrew.cask_installer.sys.platform", "linux")
    with pytest.raises(CaskError, match="only available on macOS"):
        installer.install(CaskLoader(discover_taps([tap])).load("osy"))


def test_cask_os_macos_version_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "o" / "osver.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "osver" do
  version "1.0"
  stage_only true
  os macos: ">= :sequoia"
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    monkeypatch.setattr("pybrew.cask_installer.sys.platform", "darwin")
    monkeypatch.setenv("PYBREW_MACOS_VERSION", "14.0")
    with pytest.raises(CaskError, match="requires macOS"):
        installer.install(CaskLoader(discover_taps([tap])).load("osver"))


# ---------------------------------------------------------------------------
# Cask model metadata helpers (default parser)
# ---------------------------------------------------------------------------


def test_default_parser_marks_livecheck_defined() -> None:
    cask = parse_cask_source(
        'cask "lc" do\n'
        '  version "1.0"\n'
        '  livecheck do\n'
        '    regex(/tiny/)\n'
        '  end\n'
        '  app "Tiny.app"\n'
        'end\n'
    )
    assert cask.livecheck_defined is True
    assert cask.livecheck_source is not None
    assert "regex" in cask.livecheck_source
    # The literal parser does not evaluate livecheck metadata.
    assert cask.livecheck == {}
    assert cask.autobump is True

    plain = parse_cask_source('cask "plain" do\n  version "1.0"\nend\n')
    assert plain.livecheck_defined is False
    assert plain.livecheck == {}


def test_default_parser_tracks_on_system_blocks() -> None:
    cask = parse_cask_source(
        'cask "sys" do\n'
        '  on_macos do\n'
        '    version "1.0"\n'
        '  end\n'
        'end\n'
    )
    assert cask.on_system_blocks is True
    assert cask.on_system_blocks_exist is True

    plain = parse_cask_source('cask "plain" do\n  version "1.0"\nend\n')
    assert plain.on_system_blocks is False


# ---------------------------------------------------------------------------
# opt-in cask script shim: MacOS SDK and command/Utils helpers
# ---------------------------------------------------------------------------


def test_cask_script_shim_exposes_macos_sdk_and_command_helpers() -> None:
    from pybrew.cask_installer import _CASK_SCRIPT_SHIM

    for snippet in (
        "class PybrewMacOSSDK",
        "def self.sdk_paths",
        "def self.sdk(version = nil)",
        "def self.sdk_path(version = nil)",
        "def merged_output",
        "def assert_success!",
        "must_succeed",
        "def popen_read(*args, **options)",
        "def which(command)",
    ):
        assert snippet in _CASK_SCRIPT_SHIM


@requires_ruby
def test_cask_script_shim_macos_and_command_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = tmp_path / "MacOSX14.0.sdk"
    sdk.mkdir()
    monkeypatch.setenv("PYBREW_MACOS_VERSION", "14.5")
    monkeypatch.setenv("PYBREW_MACOS_SDK_PATH", str(sdk))
    monkeypatch.setenv("PYBREW_MACOS_LANGUAGE", "zh-Hans-CN")
    # Make the cask ``arch`` stanza selection deterministic on every host.
    monkeypatch.setattr("pybrew.cask.platform.machine", lambda: "arm64")
    monkeypatch.setattr(
        "pybrew.cask_installer.platform.machine", lambda: "arm64"
    )

    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "s" / "shimmy.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "shimmy" do
  version "1.0"
  stage_only true
  arch arm: "darwin-arm64", intel: "darwin-x64"
  language "fr", default: true do
    "French"
  end
  preflight do
    if MacOS.version.to_s != "14.5"
      raise "bad version"
    end
    if MacOS.version_symbol != :sonoma
      raise "bad version symbol"
    end
    if MacOS.version.to_sym != :sonoma
      raise "bad to_sym"
    end
    if !MacOS.version.outdated_release?
      raise "bad outdated_release?"
    end
    if MacOS.language != "zh-Hans-CN"
      raise "bad MacOS.language"
    end
    if MacOS.languages != ["zh-Hans-CN"]
      raise "bad MacOS.languages"
    end
    if caskroom_path != prefix/"Caskroom"/token
      raise "bad caskroom_path"
    end
    if arch != "darwin-arm64"
      raise "bad cask arch stanza"
    end
    if language != "fr"
      raise "bad language"
    end
    if MacOS.version.release_name != "Sonoma"
      raise "bad release_name"
    end
    if MacOS.version.pretty_name != "Sonoma"
      raise "bad pretty_name"
    end
    if MacOS.release_name != "Sonoma"
      raise "bad module release_name"
    end
    if MacOS.pretty_name != "Sonoma"
      raise "bad module pretty_name"
    end
    if OS.mac?
      if MacOS.sdk_path.to_s != ENV["PYBREW_MACOS_SDK_PATH"]
        raise "bad sdk path"
      end
      if MacOS.sdk.version.to_s != "14.0"
        raise "bad sdk version"
      end
    end
    result = system_command "/bin/echo", args: ["hello"],
                                            print_stdout: false, print_stderr: false
    if result.stdout.strip != "hello"
      raise "bad stdout"
    end
    if !result.merged_output.include?("hello")
      raise "bad merged_output"
    end
    begin
      system_command "/usr/bin/false", print_stderr: false
      raise "system_command should have raised"
    rescue RuntimeError => error
      if !error.message.include?("command failed")
        raise "unexpected error"
      end
    end
    failed = system_command "/usr/bin/false", print_stderr: false,
                                               must_succeed: false
    if failed.success?
      raise "must_succeed false did not return a result"
    end
    if Utils.popen_read("/bin/echo", "world").strip != "world"
      raise "bad popen_read"
    end
    if !Utils.which("echo")
      raise "bad which"
    end
    if !Utils.safe_system("/usr/bin/true")
      raise "bad safe_system"
    end
    if Utils.quiet_system("/usr/bin/false")
      raise "bad quiet_system"
    end
    if !system("/usr/bin/true")
      raise "bad system"
    end
    begin
      system "/usr/bin/false", must_succeed: true
      raise "system should have raised"
    rescue RuntimeError
    end
    cat = system_command "/bin/cat", input: "hello\n",
                                       print_stdout: false, print_stderr: false
    if cat.stdout != "hello\n"
      raise "bad stdin input"
    end
    begin
      system_command "/bin/sleep", args: ["5"], timeout: 0.2, print_stderr: false
      raise "timeout should have raised"
    rescue Timeout::Error
    end
    piped = Utils.popen_write("/bin/cat") { |io| io.write("piped") }
    if piped != "piped"
      raise "bad popen_write"
    end
    begin
      Utils.popen_write("/usr/bin/false", safe: true) { |io| }
      raise "popen_write safe should have raised"
    rescue RuntimeError
    end
    begin
      system_command "/usr/bin/true", sudo: true
      raise "sudo should have been rejected"
    rescue ArgumentError => error
      if !error.message.include?("sudo is not supported")
        raise "unexpected sudo error"
      end
    end
    if respond_to?(:sudo)
      raise "sudo helper must not exist"
    end
  end
end
''',
        encoding="utf-8",
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
        run_scripts=True,
    )
    installer.install(CaskLoader(discover_taps([tap])).load("shimmy"))


def test_cask_scripts_stay_inert_without_run_scripts(tmp_path: Path) -> None:
    marker = tmp_path / "preflight-ran"
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "i" / "inertly.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "inertly" do
  version "1.0"
  stage_only true
  preflight do
    File.write("{marker}", "ran")
  end
end
''',
        encoding="utf-8",
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    installer.install(CaskLoader(discover_taps([tap])).load("inertly"))

    assert not marker.exists()
    receipt = json.loads(
        (tmp_path / "prefix" / "Caskroom" / "inertly" / "1.0" / ".metadata"
         / "receipt.json").read_text()
    )
    assert receipt["scripts_requested"] is False
    assert receipt["script_results"] == {}


# ---------------------------------------------------------------------------
# sha256 Linux architecture keys (default parser / Cask model)
# ---------------------------------------------------------------------------


def test_default_parser_sha256_linux_arch_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arm_digest = "a" * 64
    intel_digest = "b" * 64
    arm_linux_digest = "c" * 64
    x64_linux_digest = "d" * 64
    cask = parse_cask_source(
        'cask "multi" do\n'
        '  version "1.0"\n'
        f'  sha256 arm: "{arm_digest}", intel: "{intel_digest}", '
        f'arm64_linux: "{arm_linux_digest}", '
        f'x86_64_linux: "{x64_linux_digest}"\n'
        'end\n'
    )
    assert cask.sha256_map == {
        "arm": arm_digest,
        "intel": intel_digest,
        "arm64_linux": arm_linux_digest,
        "x86_64_linux": x64_linux_digest,
    }
    # The literal parser selected the checksum for the *host* platform; clear
    # it so the platform-specific resolution helpers are exercised directly.
    cask.sha256 = None

    monkeypatch.setattr("pybrew.cask.is_linux", lambda: True)
    monkeypatch.setattr("pybrew.cask.platform.machine", lambda: "aarch64")
    assert cask.resolved_sha256_key() == "arm64_linux"
    assert cask.resolved_sha256() == arm_linux_digest

    monkeypatch.setattr("pybrew.cask.platform.machine", lambda: "x86_64")
    assert cask.resolved_sha256_key() == "x86_64_linux"
    assert cask.resolved_sha256() == x64_linux_digest

    monkeypatch.setattr("pybrew.cask.is_linux", lambda: False)
    monkeypatch.setattr("pybrew.cask.platform.machine", lambda: "arm64")
    assert cask.resolved_sha256_key() == "arm"
    assert cask.resolved_sha256() == arm_digest


# ---------------------------------------------------------------------------
# deprecation/disable/homepage/no_autobump metadata (default parser)
# ---------------------------------------------------------------------------


def test_default_parser_lifecycle_metadata() -> None:
    cask = parse_cask_source(
        'cask "meta" do\n'
        '  version "1.0"\n'
        '  homepage "https://example.com/", browsed: "2026-07-26"\n'
        '  deprecate! date: "2024-01-01", because: "old", '
        'replacement_formula: "other"\n'
        '  disable! date: "2024-02-02", because: "broken", '
        'replacement: "newtool"\n'
        '  no_autobump! because: "test"\n'
        'end\n'
    )

    assert cask.homepage == "https://example.com/"
    assert cask.homepage_browsed == "2026-07-26"

    assert cask.deprecated is True
    assert cask.deprecation_date == "2024-01-01"
    assert cask.deprecation_reason == "old"
    assert cask.deprecation_replacement_formula == "other"
    assert cask.deprecation_replacement_cask is None
    assert cask.deprecate_args["replacement_formula"] == "other"

    assert cask.disabled is True
    assert cask.disable_date == "2024-02-02"
    assert cask.disable_reason == "broken"
    assert cask.disable_replacement_formula == "newtool"
    assert cask.disable_replacement_cask == "newtool"
    assert cask.disable_args["replacement"] == "newtool"

    assert cask.no_autobump_message == "test"


def test_default_parser_on_system_block_min_os(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pybrew.cask.macos_major_version", lambda: 14)
    sonoma = parse_cask_source(
        'cask "sonoma" do\n'
        '  version "1.0"\n'
        '  on_sonoma do\n'
        '    version "2.0"\n'
        '  end\n'
        'end\n'
    )
    assert sonoma.version == "2.0"
    assert sonoma.on_system_block_min_os == "14"

    monkeypatch.setattr("pybrew.cask.macos_major_version", lambda: 12)
    older = parse_cask_source(
        'cask "older" do\n'
        '  version "1.0"\n'
        '  on_monterey :or_older do\n'
        '    version "2.0"\n'
        '  end\n'
        'end\n'
    )
    assert older.on_system_block_min_os == "10.15"


# ---------------------------------------------------------------------------
# script shim: stdin input, timeout and Utils.popen_write
# ---------------------------------------------------------------------------


def test_cask_script_shim_supports_input_timeout_and_popen_write() -> None:
    from pybrew.cask_installer import _CASK_SCRIPT_SHIM

    for snippet in (
        "require \"timeout\"",
        "def capture(environment, command, spawn_options, input = nil, timeout = nil)",
        "def capture_with_timeout(",
        "options.delete(:input)",
        "options.delete(:timeout)",
        "def popen_write(*args, safe: false, **options, &block)",
        "Open3.popen2e",
    ):
        assert snippet in _CASK_SCRIPT_SHIM


# ---------------------------------------------------------------------------
# script shim: cask delegators and MacOS release names
# ---------------------------------------------------------------------------


def test_cask_script_shim_exposes_context_delegators_and_macos_names() -> None:
    import inspect

    from pybrew.cask_installer import CaskInstaller, _CASK_SCRIPT_SHIM

    for snippet in (
        "def caskroom_path",
        "def arch",
        "def language",
        "def self.language",
        "def self.languages",
        "def release_version",
        "def release_name",
        "def pretty_name",
        "def to_sym",
        "outdated_release?",
        "oldest_supported_version",
        "RELEASE_NAMES",
        "PYBREW_CASK_ARCH",
    ):
        assert snippet in _CASK_SCRIPT_SHIM
    # The installer passes the cask language and arch stanza to the script.
    script_source = inspect.getsource(CaskInstaller._run_cask_script)
    assert "PYBREW_CASK_LANGUAGE" in script_source
    assert "PYBREW_CASK_ARCH" in script_source


@requires_ruby
def test_cask_script_arch_falls_back_to_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "n" / "noarch.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "noarch" do
  version "1.0"
  stage_only true
  preflight do
    if !ENV["PYBREW_CASK_ARCH"].to_s.empty?
      raise "arch env should be empty without an arch stanza"
    end
    if !%w[arm64 intel].include?(arch)
      raise "bad host arch fallback"
    end
  end
end
''',
        encoding="utf-8",
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
        run_scripts=True,
    )
    installer.install(CaskLoader(discover_taps([tap])).load("noarch"))


# ---------------------------------------------------------------------------
# url kwargs, container metadata and multiple renames (default parser)
# ---------------------------------------------------------------------------


def test_parse_url_options_container_and_rename_metadata() -> None:
    cask = parse_cask_source(
        'cask "meta" do\n'
        '  version "1.0"\n'
        '  url "https://example.com/Tool-#{version}.dmg", '
        'using: :homebrew_curl, user_agent: :default, '
        'header: ["Accept: text/html"], referer: "https://example.com", '
        'cookies: { "session" => "abc" }, verified: "example.com"\n'
        '  container nested: "Tool-#{version}.dmg", type: :dmg\n'
        '  rename "Old1.app", "New1.app"\n'
        '  rename from: "Old3.app", to: "New3.app"\n'
        '  rename [["Old4.app", "New4.app"], ["Old5.app", "New5.app"]]\n'
        '  app "Tool.app"\n'
        'end\n'
    )

    assert cask.url == "https://example.com/Tool-#{version}.dmg"
    assert cask.resolved_url() == "https://example.com/Tool-1.0.dmg"
    assert cask.url_options == {
        "using": ":homebrew_curl",
        "user_agent": ":default",
        "header": ["Accept: text/html"],
        "referer": "https://example.com",
        "cookies": {"session": "abc"},
        "verified": "example.com",
    }
    assert cask.container == "dmg"
    assert cask.container_nested == "Tool-#{version}.dmg"
    assert cask.container_options == {
        "nested": "Tool-#{version}.dmg",
        "type": ":dmg",
    }
    assert cask.renames == [
        ("Old1.app", "New1.app"),
        ("Old3.app", "New3.app"),
        ("Old4.app", "New4.app"),
        ("Old5.app", "New5.app"),
    ]


def test_install_multiple_renames_then_artifacts(tmp_path: Path) -> None:
    archive = tmp_path / "Multirenamy.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Old1.app/Contents/Info.plist", "<plist>1</plist>")
        bundle.writestr("Old2.app/Contents/Info.plist", "<plist>2</plist>")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "m" / "multirenamy.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "multirenamy" do
  version "1.0"
  sha256 "{digest}"
  url "{archive.as_uri()}"
  rename "Old1.app", "New1.app"
  rename "Old2.app", "New2.app"
  app "New1.app"
  app "New2.app"
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    installer.install(CaskLoader(discover_taps([tap])).load("multirenamy"))

    applications = tmp_path / "Applications"
    assert (applications / "New1.app" / "Contents" / "Info.plist").is_file()
    assert (applications / "New2.app" / "Contents" / "Info.plist").is_file()
    assert not (applications / "Old1.app").exists()
    assert not (applications / "Old2.app").exists()


# ---------------------------------------------------------------------------
# cask url_options -> download request headers
# ---------------------------------------------------------------------------


class _RecordingDownloader:
    """Injected downloader that records how the cask archive was fetched."""

    def __init__(self, archive: Path):
        self.archive = archive
        self.calls: list[dict[str, object]] = []

    def fetch(
        self,
        url: str,
        sha256: str,
        hint: str = "download",
        headers: dict[str, str] | None = None,
    ) -> Path:
        self.calls.append(
            {"url": url, "sha256": sha256, "hint": hint, "headers": headers}
        )
        return self.archive


class _LegacyDownloader:
    """Injected downloader whose ``fetch`` predates request headers."""

    def __init__(self, archive: Path):
        self.archive = archive
        self.calls: list[tuple[str, str, str]] = []

    def fetch(self, url: str, sha256: str, hint: str = "download") -> Path:
        self.calls.append((url, sha256, hint))
        return self.archive


def _write_url_option_cask(
    tap: Path, archive: Path, digest: str, *, options: str = ""
) -> None:
    cask_path = tap / "Casks" / "o" / "optiony.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "optiony" do
  version "1.0"
  sha256 "{digest}"
  url "{archive.as_uri()}"{options}
  app "Optiony.app"
end
'''
    )


def test_cask_url_options_passed_to_injectable_downloader(
    tmp_path: Path,
) -> None:
    from pybrew.download import USER_AGENT

    archive = tmp_path / "Optiony.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Optiony.app/Contents/Info.plist", "<plist/>")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    tap = tmp_path / "tap"
    _write_url_option_cask(
        tap,
        archive,
        digest,
        options=(
            ', header: ["Accept: application/octet-stream", "X-Test: yes"], '
            'user_agent: :default, referer: "https://example.com", '
            'cookies: { "a" => "b", "c" => "d" }'
        ),
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    downloader = _RecordingDownloader(archive)
    installer = CaskInstaller(
        config,
        downloader=downloader,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    installer.install(CaskLoader(discover_taps([tap])).load("optiony"))

    assert len(downloader.calls) == 1
    assert downloader.calls[0]["headers"] == {
        "Accept": "application/octet-stream",
        "X-Test": "yes",
        "User-Agent": USER_AGENT,
        "Referer": "https://example.com",
        "Cookie": "a=b; c=d",
    }
    receipt = json.loads(
        (
            tmp_path / "prefix" / "Caskroom" / "optiony" / "1.0" / ".metadata"
            / "receipt.json"
        ).read_text()
    )
    assert receipt["url_options"] == {
        "applied": True,
        "keys": ["cookies", "header", "referer", "user_agent"],
    }
    assert (tmp_path / "Applications" / "Optiony.app").is_dir()


def test_cask_url_options_default_downloader_applies_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pybrew import download as download_module
    from pybrew.download import USER_AGENT

    archive = tmp_path / "Optiony.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Optiony.app/Contents/Info.plist", "<plist/>")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    tap = tmp_path / "tap"
    _write_url_option_cask(
        tap,
        archive,
        digest,
        options=', header: ["X-Test: yes"], user_agent: :default',
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    # The stock Downloader now accepts headers, so the cask options reach it
    # and the receipt records them as applied.
    captured: list[dict[str, str]] = []
    original_fetch = download_module.Downloader.fetch

    def recording_fetch(self, url, sha256, hint="download", headers=None):
        captured.append(dict(headers) if headers is not None else {})
        return original_fetch(self, url, sha256, hint=hint, headers=headers)

    monkeypatch.setattr(download_module.Downloader, "fetch", recording_fetch)
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    installer.install(CaskLoader(discover_taps([tap])).load("optiony"))

    assert captured == [{"X-Test": "yes", "User-Agent": USER_AGENT}]
    receipt = json.loads(
        (
            tmp_path / "prefix" / "Caskroom" / "optiony" / "1.0" / ".metadata"
            / "receipt.json"
        ).read_text()
    )
    assert receipt["url_options"] == {
        "applied": True,
        "keys": ["header", "user_agent"],
    }
    assert (tmp_path / "Applications" / "Optiony.app").is_dir()


def test_cask_url_options_legacy_injected_downloader_keeps_working(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "Optiony.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Optiony.app/Contents/Info.plist", "<plist/>")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    tap = tmp_path / "tap"
    _write_url_option_cask(tap, archive, digest, options=', header: "X-Test: yes"')
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    # Injected downloaders whose fetch() has no headers parameter still work;
    # they get the plain call and the receipt records the options as unapplied.
    downloader = _LegacyDownloader(archive)
    installer = CaskInstaller(
        config,
        downloader=downloader,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    installer.install(CaskLoader(discover_taps([tap])).load("optiony"))

    assert len(downloader.calls) == 1
    called_url, called_digest, _hint = downloader.calls[0]
    assert (called_url, called_digest) == (archive.as_uri(), digest)
    receipt = json.loads(
        (
            tmp_path / "prefix" / "Caskroom" / "optiony" / "1.0" / ".metadata"
            / "receipt.json"
        ).read_text()
    )
    assert receipt["url_options"] == {"applied": False, "keys": ["header"]}
    assert (tmp_path / "Applications" / "Optiony.app").is_dir()


def test_cask_without_url_options_uses_plain_fetch(tmp_path: Path) -> None:
    archive = tmp_path / "Plain.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Plain.app/Contents/Info.plist", "<plist/>")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "p" / "plain.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "plain" do
  version "1.0"
  sha256 "{digest}"
  url "{archive.as_uri()}"
  app "Plain.app"
end
'''
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    downloader = _RecordingDownloader(archive)
    installer = CaskInstaller(
        config,
        downloader=downloader,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    installer.install(CaskLoader(discover_taps([tap])).load("plain"))

    assert downloader.calls[0]["headers"] is None
    receipt = json.loads(
        (
            tmp_path / "prefix" / "Caskroom" / "plain" / "1.0" / ".metadata"
            / "receipt.json"
        ).read_text()
    )
    assert "url_options" not in receipt


def test_cask_request_headers_builder() -> None:
    from pybrew.cask_installer import cask_request_headers
    from pybrew.download import USER_AGENT

    cask = parse_cask_source(
        '''cask "headers" do
  version "1.0"
  url "https://example.com/x.zip",
      header: ["Accept: text/html", "X-One: 1"],
      user_agent: "custom-agent",
      referer: "https://example.com",
      cookies: "session=abc",
      using: :homebrew_curl,
      verified: "example.com"
end
'''
    )
    assert cask_request_headers(cask) == {
        "Accept": "text/html",
        "X-One": "1",
        "User-Agent": "custom-agent",
        "Referer": "https://example.com",
        "Cookie": "session=abc",
    }

    plain = parse_cask_source(
        'cask "plain" do\n  version "1.0"\n  url "https://example.com/x.zip"\nend\n'
    )
    assert cask_request_headers(plain) == {}
    symbol_agent = parse_cask_source(
        'cask "ua" do\n  version "1.0"\n'
        '  url "https://example.com/x.zip", user_agent: :default\nend\n'
    )
    assert cask_request_headers(symbol_agent)["User-Agent"] == USER_AGENT


# ---------------------------------------------------------------------------
# container nested: extraction path selection
# ---------------------------------------------------------------------------


def _write_nested_cask(
    tap: Path, outer: Path, digest: str, nested: str
) -> None:
    cask_path = tap / "Casks" / "n" / "nestedy.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "nestedy" do
  version "1.0"
  sha256 "{digest}"
  url "{outer.as_uri()}"
  container nested: "{nested}"
  app "Nested.app"
end
'''
    )


def test_install_nested_container_zip_in_zip(tmp_path: Path) -> None:
    inner = tmp_path / "Inner-1.0.zip"
    with zipfile.ZipFile(inner, "w") as bundle:
        bundle.writestr("Nested.app/Contents/Info.plist", "<plist/>")
    outer = tmp_path / "Outer.zip"
    with zipfile.ZipFile(outer, "w") as bundle:
        bundle.write(inner, "Inner-1.0.zip")
    digest = hashlib.sha256(outer.read_bytes()).hexdigest()

    tap = tmp_path / "tap"
    _write_nested_cask(tap, outer, digest, "Inner-#{version}.zip")
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    installer.install(CaskLoader(discover_taps([tap])).load("nestedy"))

    app = tmp_path / "Applications" / "Nested.app"
    assert (app / "Contents" / "Info.plist").is_file()


def test_install_nested_container_missing_is_a_clear_error(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "Outer.zip"
    with zipfile.ZipFile(outer, "w") as bundle:
        bundle.writestr("Other/thing.txt", "x")
    digest = hashlib.sha256(outer.read_bytes()).hexdigest()

    tap = tmp_path / "tap"
    _write_nested_cask(tap, outer, digest, "Missing.zip")
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    with pytest.raises(CaskError, match="nested container"):
        installer.install(CaskLoader(discover_taps([tap])).load("nestedy"))


def test_install_nested_container_unsafe_path_is_rejected(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "Outer.zip"
    with zipfile.ZipFile(outer, "w") as bundle:
        bundle.writestr("Other/thing.txt", "x")
    digest = hashlib.sha256(outer.read_bytes()).hexdigest()

    tap = tmp_path / "tap"
    _write_nested_cask(tap, outer, digest, "../evil.zip")
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    with pytest.raises(CaskError, match="unsafe container nested path"):
        installer.install(CaskLoader(discover_taps([tap])).load("nestedy"))


def test_cask_installed_inventory_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path / "prefix"))
    cask = Cask(
        token="demo",
        version="2.0",
        sha256="abc",
        url="https://example.com/demo-2.0.zip",
        artifacts=[
            Artifact("app", "Demo.app"),
            Artifact("font", "Demo.ttf"),
        ],
    )
    room = tmp_path / "prefix" / "Caskroom" / "demo"
    (room / "1.0").mkdir(parents=True)
    (room / "2.0").mkdir()
    metadata = room / ".metadata" / "2.0" / "20260101000000" / "Casks"
    metadata.mkdir(parents=True)
    (metadata / "demo.rb").write_text("cask 'demo'")

    assert cask.full_token == "demo"
    assert cask.caskroom_path == room
    assert cask.installed_versions() == ["1.0", "2.0"]
    assert cask.timestamped_versions() == ["1.0", "2.0"]
    assert cask.installed_caskfile() == metadata / "demo.rb"
    assert cask.outdated_version() == "1.0"
    assert cask.outdated_info() == {
        "token": "demo",
        "current_version": "2.0",
        "installed_version": "1.0",
        "installed_versions": ["1.0", "2.0"],
    }
    assert cask.artifacts_list() == cask.artifacts
    assert cask.single_app_artifact().source == "Demo.app"
    assert cask.font() is False
    assert cask.url_specs() == ["https://example.com/demo-2.0.zip"]
    assert cask.checksumable()


def test_install_cask_verified_url_pattern(tmp_path: Path) -> None:
    archive = tmp_path / "Verified-1.0.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("tool", "#!/bin/sh\nexit 0\n")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    def cask_source(pattern: str) -> str:
        return f'''cask "verified" do
  version "1.0"
  sha256 "{digest}"
  url "{archive.as_uri()}", verified: "{pattern}"
  stage_only true
end
'''

    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "v" / "verified.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(cask_source("Verified-1\\\\.0\\\\.zip"))

    config = Config.default(prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache")
    config.ensure()
    installer = CaskInstaller(config, appdir=tmp_path / "Applications", library_root=tmp_path / "Library")
    loader = CaskLoader(discover_taps([tap]))
    installer.install(loader.load("verified"))
    assert (config.caskroom / "verified" / "1.0").exists()

    cask_path.write_text(cask_source("Nope"))
    mismatch_config = Config.default(
        prefix=tmp_path / "prefix2", taps=[tap], cache=tmp_path / "cache2"
    )
    mismatch_config.ensure()
    mismatch = CaskInstaller(
        mismatch_config, appdir=tmp_path / "Applications2", library_root=tmp_path / "Library2"
    )
    with pytest.raises(CaskError, match="verified pattern"):
        mismatch.install(CaskLoader(discover_taps([tap])).load("verified"))
    assert not (mismatch_config.caskroom / "verified" / "1.0").exists()


def test_cask_metadata_helper_methods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path / "prefix"))
    cask = Cask(
        token="meta",
        version="1.10",
        url="https://example.com/meta.zip",
        name="Meta",
        caveats="Use it.",
        renames=[("old-meta", "meta")],
        artifacts=[Artifact("app", "Meta.app")],
        extra={"custom": "value"},
    )
    assert Cask.compare_version_strings("1.10", "1.9") > 0
    assert Cask.compare_version_strings("1.0", "1.0") == 0
    assert cask.rename_list() == [("old-meta", "meta")]
    assert cask.old_tokens() == ["old-meta"]
    assert cask.caveats_object() == "Use it."
    assert cask.caveats_for_api() == {"caveats": "Use it."}
    assert cask.loaded_from_metadata() is False
    assert cask.platform_supported() is True
    assert cask.installable_artifact().kind == "app"
    payload = cask.to_h_with_language_variations()
    assert payload["token"] == "meta"
    assert payload["full_token"] == "meta"
    assert payload["custom"] == "value"
    assert cask.to_installed_json_hash() == {
        "token": "meta",
        "version": "1.10",
        "installed_versions": [],
    }


def test_cask_installed_app_bundle_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = tmp_path / "prefix"
    monkeypatch.setenv("HOMEBREW_PREFIX", str(prefix))
    app = prefix / "Applications" / "Meta.app"
    contents = app / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleVersion": "42",
                "CFBundleShortVersionString": "2.5",
            },
            handle,
        )
    room = prefix / "Caskroom" / "meta-app" / "2.5"
    room.mkdir(parents=True)
    cask = Cask(
        token="meta-app",
        version="2.5",
        artifacts=[Artifact("app", "Meta.app")],
    )
    assert cask.installed is True
    assert cask.installed_version() == "2.5"
    assert cask.outdated is False
    assert cask.installed_app_info_plist() == contents / "Info.plist"
    assert cask.installed_app_info()["CFBundleVersion"] == "42"
    assert cask.bundle_version() == "42"
    assert cask.bundle_short_version() == "2.5"
    assert cask.bundle_long_version() == "2.5 (42)"


def test_cask_source_platform_and_pin_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "prefix"
    monkeypatch.setenv("HOMEBREW_PREFIX", str(prefix))
    source = tmp_path / "meta.rb"
    source.write_text("cask 'meta'")
    cask = Cask(
        token="meta",
        path=source,
        version="2.0",
        os={"macos": True, "linux": False},
    )
    assert cask.ruby_source_path() == source
    assert cask.ruby_source_checksum() == hashlib.sha256(source.read_bytes()).hexdigest()
    assert cask.loaded_from_api() is False
    assert cask.loaded_from_internal_api() is False
    assert cask.valid_platform() is (sys.platform == "darwin")
    assert cask.supports_macos() is True
    assert cask.supports_linux() is False
    assert cask.pinned() is False
    assert cask.pinned_version() is None
    cask.pin()
    assert cask.pinned() is True
    assert cask.pinned_version() == "2.0"
    cask.unpin()
    assert cask.pinned() is False


def test_cask_serialization_and_ref_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path / "prefix"))
    source = tmp_path / "serial.rb"
    source.write_text("cask 'serial'")
    cask = Cask(
        token="serial",
        path=source,
        version="3.0",
        sha256="deadbeef",
        url="https://example.com/serial.zip",
        name="Serial",
        desc="serial cask",
        artifacts=[Artifact("app", "Serial.app")],
        renames=[("old-serial", "serial")],
        extra={"custom": "value"},
    )
    room = tmp_path / "prefix" / "Caskroom" / "serial" / "3.0"
    room.mkdir(parents=True)
    assert cask.to_s() == "serial"
    assert cask.inspect() == "#<Cask:serial@3.0>"
    assert cask.hash() == hash("serial")
    assert cask.reloadable_ref() == source
    assert cask.tap_git_head() is None
    assert cask.new_download_sha() == "deadbeef"
    assert cask.download_sha_path() == cask.caskroom_path / ".metadata" / "download_sha"
    assert isinstance(cask.install_time(), float)
    payload = cask.to_h()
    assert payload["token"] == "serial"
    assert payload["sha256"] == "deadbeef"
    assert payload["artifacts"][0]["kind"] == "app"
    assert payload["renames"] == [["old-serial", "serial"]]
    assert payload["custom"] == "value"


def test_cask_api_refresh_and_variation_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path / "prefix"))
    source = tmp_path / "api.rb"
    source.write_text(
        'cask "api" do\n'
        '  version "1.0"\n'
        '  sha256 "abc"\n'
        '  url "https://example.com/api-1.0.zip"\n'
        '  name "Api"\n'
        '  homepage "https://example.com/api"\n'
        '  app "Api.app"\n'
        'end\n'
    )
    cask = parse_cask_file(source)
    assert cask.dsl() is cask
    assert cask.api_to_local_hash()["token"] == "api"
    assert cask.to_hash_with_variations()["version"] == "1.0"
    cask.version = "9.9"
    refreshed = cask.refresh()
    assert refreshed.version == "1.0"
    assert cask.refresh_for_tag("arm") is cask
    assert cask.tab() == {"token": "api", "version": "1.0"}

    populated = Cask(token="empty").populate_from_api(
        {
            "token": "populated",
            "version": "2.0",
            "url": "https://example.com/populated.zip",
            "sha256": "def",
            "artifacts": [{"kind": "app", "source": "Populated.app"}],
        }
    )
    assert populated.token == "populated"
    assert populated.version == "2.0"
    assert populated.artifacts[0].source == "Populated.app"


def test_cask_config_language_and_strategy_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path / "prefix"))
    cask = Cask(
        token="strategy",
        version="1.0",
        language="zh",
        url_options={"using": ":curl"},
    )
    assert Cask.all() == []
    assert cask.config_path() == cask.caskroom_path / ".metadata" / "config.json"
    assert cask.languages() == ["zh"]
    assert cask.download_strategy() == ":curl"
    assert cask.using() == ":curl"
    assert Cask(token="default").languages() == ["en"]


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not installed")
def test_install_cask_using_curl_strategy(tmp_path: Path) -> None:
    archive = tmp_path / "CurlStrategy-1.0.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("data.txt", "curl strategy\n")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "c" / "curl-strategy.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "curl-strategy" do
  version "1.0"
  sha256 "{digest}"
  url "{archive.as_uri()}", using: :curl
  stage_only true
end
'''
    )
    config = Config.default(prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache")
    config.ensure()
    installer = CaskInstaller(config)
    installer.install(CaskLoader(discover_taps([tap])).load("curl-strategy"))
    assert (config.caskroom / "curl-strategy" / "1.0").is_dir()


@pytest.mark.skipif(sys.platform != "darwin", reason="dmg requires macOS hdiutil")
def test_install_nested_container_inside_dmg(tmp_path: Path) -> None:
    import subprocess

    payload = tmp_path / "Payload.zip"
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr("inside.txt", "nested dmg payload\n")
    source = tmp_path / "dmg-source"
    source.mkdir()
    shutil.copy2(payload, source / "Payload.zip")
    dmg = tmp_path / "Nested-1.0.dmg"
    result = subprocess.run(
        [
            "hdiutil", "create", "-volname", "Nested", "-srcfolder", str(source),
            "-ov", "-format", "UDZO", str(dmg),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"hdiutil unavailable: {result.stderr}")
    digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "n" / "nested-dmg.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "nested-dmg" do
  version "1.0"
  sha256 "{digest}"
  url "{dmg.as_uri()}"
  container nested: "Payload.zip"
  stage_only true
end
'''
    )
    config = Config.default(prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache")
    config.ensure()
    installer = CaskInstaller(config)
    installer.install(CaskLoader(discover_taps([tap])).load("nested-dmg"))
    assert (config.caskroom / "nested-dmg" / "1.0").is_dir()


def test_cask_artifact_helper_methods(tmp_path: Path) -> None:
    artifact = Artifact("app", "Example.app", target="Renamed.app")
    assert artifact.to_s() == "Renamed.app"
    assert artifact.english_name() == "app"
    assert artifact.english_description() == "app Example.app"
    assert artifact.summarize() == "Renamed.app"
    assert artifact.staged_path_join_executable(tmp_path) == tmp_path / "Example.app"
    assert artifact.requires_sudo() is False
    assert artifact.dsl_key() == "app"
    assert artifact.sort_order() == 0
    assert artifact.to_args() == ["Example.app"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_install_cask_git_strategy(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    tool = repo / "tool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "tool"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)

    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "g" / "gitty.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "gitty" do
  version "1.0"
  sha256 "no-check"
  url "{repo.as_uri()}", using: :git
  binary "tool"
end
'''
    )
    config = Config.default(prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache")
    config.ensure()
    installer = CaskInstaller(config)
    installer.install(CaskLoader(discover_taps([tap])).load("gitty"))
    installed = config.prefix / "bin" / "tool"
    assert installed.is_file()
    assert installed.stat().st_mode & 0o111


def test_cask_inventory_bundle_and_sha_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "prefix"
    monkeypatch.setenv("HOMEBREW_PREFIX", str(prefix))
    monkeypatch.setenv("PYBREW_APPDIR", str(tmp_path / "Applications"))
    cask = Cask(
        token="demo",
        version="1.0",
        url="https://example.com/demo.zip",
        sha256="abc123",
        artifacts=[Artifact("app", "Demo.app")],
    )
    version_dir = cask.caskroom_path / "1.0"
    version_dir.mkdir(parents=True)
    sha_path = cask.download_sha_path()
    sha_path.parent.mkdir(parents=True, exist_ok=True)
    sha_path.write_text("abc123")
    assert cask.any_version_installed() is True
    assert cask.pinnable() is True
    assert cask.outdated_download_sha() is False
    sha_path.write_text("different")
    assert cask.outdated_download_sha() is True

    app = tmp_path / "Applications" / "Demo.app"
    plist = app / "Contents" / "Info.plist"
    plist.parent.mkdir(parents=True)
    import plistlib

    plist.write_bytes(
        plistlib.dumps(
            {"CFBundleShortVersionString": "1.2", "CFBundleVersion": "345"}
        )
    )
    assert cask.bundle_short_version() == "1.2"
    assert cask.bundle_long_version() == "1.2 (345)"
    assert cask.bundle_version() == "345"
    assert cask.auto_updates_bundle_outdated() is False
    assert cask.uninstall_flight_blocks() is False


def test_cask_temp_cleanup_helpers(tmp_path: Path) -> None:
    cask = Cask(token="tempy", version="1.0")
    path = cask.temp()
    path.mkdir(parents=True, exist_ok=True)
    (path / "file").write_text("x")
    cask.cleanup_temp(retain=True)
    assert path.is_dir()
    cask.cleanup_temp()
    assert not path.exists()


def test_cask_artifact_article_config_and_sandbox_helpers() -> None:
    artifact = Artifact("app", "Example.app", kwargs={"config": {"x": 1}})
    assert artifact.english_article() == "an"
    assert artifact.config() == {"x": 1}
    assert artifact.dirmethod() == "mkdir_p"
    assert artifact.cask_sandbox() is False
    assert artifact.run_cask_sandbox() is False
    assert artifact.requires_sudo_q() is False
    assert artifact.read_script_arguments(["a", "b"]) == ["a", "b"]


@pytest.mark.parametrize("tool", ["svn", "hg"])
def test_install_cask_vcs_strategies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    script = repo / "tool"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / tool
    fake.write_text(
        "#!/bin/sh\n"
        'for last in "$@"; do :; done\n'
        'mkdir -p "$last"\n'
        'cp -R "$FAKE_REPO"/. "$last"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_REPO", str(repo))
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "v" / f"{tool}gy.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        f'''cask "{tool}gy" do
  version "1.0"
  sha256 "no-check"
  url "https://example.invalid/repo", using: :{tool}
  binary "tool"
end
'''
    )
    config = Config.default(prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache")
    config.ensure()
    CaskInstaller(config).install(CaskLoader(discover_taps([tap])).load(f"{tool}gy"))
    assert (config.prefix / "bin" / "tool").is_file()


def test_install_cask_fossil_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    script = repo / "tool"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "fossil"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "clone" ]; then cp -R "$FAKE_REPO" "$3"; exit 0; fi\n'
        'if [ "$1" = "open" ]; then cp -R "$FAKE_REPO"/. .; exit 0; fi\n'
        "exit 1\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_REPO", str(repo))
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "f" / "fossilgy.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "fossilgy" do
  version "1.0"
  sha256 "no-check"
  url "https://example.invalid/repo", using: :fossil
  binary "tool"
end
'''
    )
    config = Config.default(prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache")
    config.ensure()
    CaskInstaller(config).install(CaskLoader(discover_taps([tap])).load("fossilgy"))
    assert (config.prefix / "bin" / "tool").is_file()


def test_cask_staged_path_and_language_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path / "prefix"))
    cask = Cask(token="demo", version="2.0", language="fr")
    assert cask.staged_path() == cask.caskroom_path / "2.0"
    assert cask.language_eval() == "fr"
    assert cask.language_eval("de") == "de"
    assert cask.language_groups() == {}
    assert cask.default_language_group() == "en"
    assert cask.os_version()


def test_cask_font_and_checksumable_semantics() -> None:
    font_only = Cask(token="fonty", version="1.0", artifacts=[Artifact("font", "A.ttf")])
    mixed = Cask(
        token="mixed", version="1.0",
        artifacts=[Artifact("font", "A.ttf"), Artifact("app", "A.app")],
    )
    git_cask = Cask(
        token="gitty", version="1.0", sha256="no-check",
        url="https://example.invalid/repo", url_options={"using": "git"},
    )
    assert font_only.font() is True
    assert mixed.font() is False
    assert font_only.checksumable() is False
    assert git_cask.checksumable() is False


# ---------------------------------------------------------------------------
# date-deferred deprecation/disable semantics (default parser)
# ---------------------------------------------------------------------------


def _fixed_cask_today(
    monkeypatch: pytest.MonkeyPatch, value: str = "2024-06-01"
) -> None:
    import datetime

    monkeypatch.setattr(
        "pybrew.cask._today", lambda: datetime.date.fromisoformat(value)
    )


def test_deprecate_date_deferral_default_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_cask_today(monkeypatch)

    future = parse_cask_source(
        'cask "future" do\n'
        '  version "1.0"\n'
        '  deprecate! date: "2025-01-01", because: "later"\n'
        'end\n'
    )
    assert future.deprecated is False
    assert future.deprecation_date == "2025-01-01"
    assert future.deprecation_reason == "later"

    past = parse_cask_source(
        'cask "past" do\n'
        '  version "1.0"\n'
        '  deprecate! date: "2024-01-01", because: "old"\n'
        'end\n'
    )
    assert past.deprecated is True

    today = parse_cask_source(
        'cask "today" do\n'
        '  version "1.0"\n'
        '  deprecate! date: "2024-06-01", because: "today"\n'
        'end\n'
    )
    assert today.deprecated is True

    no_date = parse_cask_source(
        'cask "nodate" do\n  version "1.0"\n  deprecate! because: "now"\nend\n'
    )
    assert no_date.deprecated is True


def test_disable_date_deferral_default_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_cask_today(monkeypatch)

    future = parse_cask_source(
        'cask "future" do\n'
        '  version "1.0"\n'
        '  disable! date: "2025-01-01", because: "broken", '
        'replacement_cask: "newer"\n'
        'end\n'
    )
    # A future disable! is announced as a deprecation first.
    assert future.disabled is False
    assert future.deprecated is True
    assert future.deprecation_date is None
    assert future.deprecation_reason == "broken"
    assert future.deprecation_replacement_cask == "newer"
    assert future.disable_date == "2025-01-01"

    past = parse_cask_source(
        'cask "past" do\n'
        '  version "1.0"\n'
        '  disable! date: "2024-01-01", because: "broken"\n'
        'end\n'
    )
    assert past.disabled is True
    assert past.deprecated is False

    no_date = parse_cask_source(
        'cask "nodate" do\n  version "1.0"\n  disable! because: "now"\nend\n'
    )
    assert no_date.disabled is True


def test_future_disable_cask_is_installable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixed_cask_today(monkeypatch)
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "f" / "future-disable.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "future-disable" do
  version "1.0"
  stage_only true
  disable! date: "2999-01-01", because: "not yet"
end
''',
        encoding="utf-8",
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    result = installer.install(
        CaskLoader(discover_taps([tap])).load("future-disable")
    )
    assert result.version == "1.0"


def test_past_disable_cask_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixed_cask_today(monkeypatch)
    tap = tmp_path / "tap"
    cask_path = tap / "Casks" / "p" / "past-disable.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "past-disable" do
  version "1.0"
  stage_only true
  disable! date: "2000-01-01", because: "gone"
end
''',
        encoding="utf-8",
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    with pytest.raises(CaskError, match="disabled"):
        installer.install(
            CaskLoader(discover_taps([tap])).load("past-disable")
        )


def test_deprecate_and_disable_invalid_dates_are_immediate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_cask_today(monkeypatch)

    deprecated = parse_cask_source(
        'cask "bad-date" do\n'
        '  version "1.0"\n'
        '  deprecate! date: "not-a-date", because: "still old"\n'
        'end\n'
    )
    # Invalid dates fall back to pybrew's historical immediate behavior.
    assert deprecated.deprecated is True
    assert deprecated.deprecation_date == "not-a-date"

    disabled = parse_cask_source(
        'cask "bad-disable" do\n'
        '  version "1.0"\n'
        '  disable! date: "2025-13-40", because: "still broken"\n'
        'end\n'
    )
    assert disabled.disabled is True


def test_deprecate_and_disable_date_ordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_cask_today(monkeypatch)

    # Future deprecation + already-effective disable.
    mixed = parse_cask_source(
        'cask "mixed" do\n'
        '  version "1.0"\n'
        '  deprecate! date: "2025-01-01", because: "later"\n'
        '  disable! date: "2024-01-01", because: "broken"\n'
        'end\n'
    )
    assert mixed.deprecated is False
    assert mixed.disabled is True

    # Effective deprecation + future disable promotes to deprecation.
    promoted = parse_cask_source(
        'cask "promoted" do\n'
        '  version "1.0"\n'
        '  deprecate! date: "2024-01-01", because: "old"\n'
        '  disable! date: "2025-01-01", because: "broken"\n'
        'end\n'
    )
    assert promoted.deprecated is True
    assert promoted.disabled is False
    assert promoted.deprecation_reason == "broken"


def test_lifecycle_date_equal_to_today_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_cask_today(monkeypatch, "2024-06-01")

    deprecated = parse_cask_source(
        'cask "today" do\n'
        '  version "1.0"\n'
        '  deprecate! date: "2024-06-01", because: "today"\n'
        'end\n'
    )
    assert deprecated.deprecated is True

    disabled = parse_cask_source(
        'cask "today-disable" do\n'
        '  version "1.0"\n'
        '  disable! date: "2024-06-01", because: "today"\n'
        'end\n'
    )
    assert disabled.disabled is True


def test_cask_check_livecheck_default_parser_without_strategy() -> None:
    cask = parse_cask_source(
        'cask "plain-livecheck" do\n'
        '  version "1.0"\n'
        '  livecheck do\n'
        '    regex(/tiny/)\n'
        '  end\n'
        'end\n'
    )

    result = cask.check_livecheck(page="anything")

    assert result["executed"] is False
    assert "no livecheck strategy block" in (result["reason"] or "")
    assert cask.livecheck_result is None


def test_cask_livecheck_result_property() -> None:
    from pybrew.cask import Cask

    payload = {"executed": True, "result": ["1.0"], "error": None}
    cask = Cask(token="result", livecheck={"strategy_result": payload})
    assert cask.livecheck_result == payload

    bare = Cask(token="bare")
    assert bare.livecheck_result is None
