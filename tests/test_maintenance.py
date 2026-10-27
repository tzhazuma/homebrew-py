from __future__ import annotations

import json
from pathlib import Path


def _bump_formula(sample) -> None:
    text = sample.formula.read_text()
    sample.formula.write_text(text.replace('  license "MIT"', '  license "MIT"\n  version "2.0"'))


def test_outdated_and_json(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    _bump_formula(sample)
    proc = run_cli("outdated")
    assert proc.returncode == 0, proc.stderr
    assert "hello 1.0 < 2.0" in proc.stdout
    payload = json.loads(run_cli("outdated", "--json").stdout)
    assert payload["formulae"][0]["name"] == "hello"
    assert payload["formulae"][0]["current"] == "2.0"


def test_upgrade_dry_run_does_not_install(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    _bump_formula(sample)
    proc = run_cli("upgrade", "--dry-run", "hello")
    assert proc.returncode == 0, proc.stderr
    assert "Would upgrade: hello 2.0" in proc.stdout
    assert not (sample.prefix / "Cellar" / "hello" / "2.0").exists()


def test_cleanup_keeps_linked_keg(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    old = sample.prefix / "Cellar" / "hello" / "0.9"
    old.mkdir(parents=True)
    (old / "old.txt").write_text("old\n")
    dry = run_cli("cleanup", "--dry-run")
    assert dry.returncode == 0
    assert "Would remove:" in dry.stdout and "0.9" in dry.stdout
    assert old.is_dir()
    real = run_cli("cleanup")
    assert real.returncode == 0, real.stderr
    assert not old.exists()
    assert (sample.prefix / "Cellar" / "hello" / "1.0").is_dir()


def test_reinstall(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    proc = run_cli("reinstall", "hello", bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr
    assert "Reinstalled: hello 1.0" in proc.stdout
    assert (sample.prefix / "Cellar" / "hello" / "1.0").is_dir()
    assert (sample.prefix / "bin" / "hello").is_symlink()


def test_cleanup_prunes_old_cache(run_cli, sample) -> None:
    import os
    import time

    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    downloads = sample.cache / "downloads"
    old_file = downloads / "old-download.tar.gz"
    old_file.write_text("old")
    old_time = time.time() - 90 * 86400
    os.utime(old_file, (old_time, old_time))

    dry = run_cli("cleanup", "--prune-cache", "--cache-age-days", "30", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "Would remove cache:" in dry.stdout
    assert old_file.exists()
    real = run_cli("cleanup", "--prune-cache", "--cache-age-days", "30")
    assert real.returncode == 0, real.stderr
    assert not old_file.exists()


def test_pin_prevents_outdated_and_unpin(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    assert run_cli("pin", "hello").returncode == 0
    _bump_formula(sample)
    assert run_cli("outdated").stdout.strip() == ""
    pinned = run_cli("list", "--pinned")
    assert "hello" in pinned.stdout
    assert run_cli("unpin", "hello").returncode == 0
    outdated = run_cli("outdated")
    assert "hello 1.0 < 2.0" in outdated.stdout
    missing = run_cli("pin", "ghost")
    assert missing.returncode == 1
