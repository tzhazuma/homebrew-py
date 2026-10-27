from __future__ import annotations

from pathlib import Path


def _brewfile(tmp_path: Path) -> Path:
    path = tmp_path / "Brewfile"
    path.write_text(
        '# test Brewfile\n'
        'brew "hello"\n'
        'cask "testapp"\n'
    )
    return path


def test_bundle_install_check_list(run_cli, sample, tmp_path) -> None:
    brewfile = _brewfile(tmp_path)
    check = run_cli("bundle", "check", "--file", str(brewfile))
    assert check.returncode == 1
    assert "Missing:" in check.stdout

    dry = run_cli("bundle", "install", "--dry-run", "--file", str(brewfile), bottle_tag=sample.tag)
    assert dry.returncode == 0, dry.stderr
    assert "Would install: brew hello" in dry.stdout
    assert "Would install: cask testapp" in dry.stdout
    assert not (sample.prefix / "Cellar" / "hello").exists()

    installed = run_cli("bundle", "install", "--file", str(brewfile), bottle_tag=sample.tag)
    assert installed.returncode == 0, installed.stderr + installed.stdout
    assert (sample.prefix / "Cellar" / "hello" / "1.0").is_dir()
    assert (sample.prefix / "Applications" / "Test.app").is_dir()

    check = run_cli("bundle", "check", "--file", str(brewfile))
    assert check.returncode == 0, check.stdout
    assert "satisfied" in check.stdout

    listing = run_cli("bundle", "list", "--file", str(brewfile))
    assert listing.returncode == 0
    assert "brew hello" in listing.stdout
    assert "cask testapp" in listing.stdout


def test_bundle_rejects_unknown_lines(run_cli, sample, tmp_path) -> None:
    path = tmp_path / "Brewfile"
    path.write_text('not_a_brewfile_line "x"\n')
    proc = run_cli("bundle", "list", "--file", str(path))
    assert proc.returncode == 1
    assert "unsupported brewfile line" in proc.stderr.lower()


def test_bundle_dump(run_cli, sample, tmp_path) -> None:
    brewfile = _brewfile(tmp_path)
    assert run_cli("bundle", "install", "--file", str(brewfile), bottle_tag=sample.tag).returncode == 0
    dump = tmp_path / "Brewfile.dump"
    proc = run_cli("bundle", "dump", "--file", str(dump))
    assert proc.returncode == 0, proc.stderr
    text = dump.read_text()
    assert 'brew "hello"' in text
    assert 'cask "testapp"' in text
    again = run_cli("bundle", "dump", "--file", str(dump))
    assert again.returncode == 1
    forced = run_cli("bundle", "dump", "--file", str(dump), "--force")
    assert forced.returncode == 0, forced.stderr


def test_bundle_link_false(run_cli, sample, tmp_path) -> None:
    brewfile = tmp_path / "Brewfile"
    brewfile.write_text('brew "hello", link: false\n')
    proc = run_cli("bundle", "install", "--file", str(brewfile), bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (sample.prefix / "Cellar" / "hello" / "1.0").is_dir()
    assert not (sample.prefix / "bin" / "hello").exists()
    assert not (sample.prefix / "opt" / "hello").exists()


def test_bundle_tap_url(run_cli, sample, tmp_path) -> None:
    import subprocess

    remote = tmp_path / "demo-remote"
    (remote / "Formula").mkdir(parents=True)
    (remote / "Formula" / "hello.rb").write_text('class Hello < Formula\nend\n')
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(remote), *args], check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
    brewfile = tmp_path / "Brewfile"
    brewfile.write_text(f'tap "tzhazuma/demo", url: "{remote.as_uri()}"\n')
    proc = run_cli("bundle", "install", "--file", str(brewfile))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "tap tzhazuma/demo" in proc.stdout
    assert (sample.prefix / "Library" / "Taps" / "tzhazuma" / "homebrew-demo" / ".git").is_dir()
