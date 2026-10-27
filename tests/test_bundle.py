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
