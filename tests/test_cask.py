from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from pybrew.cask import CaskError, parse_cask_file, parse_cask_source
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
