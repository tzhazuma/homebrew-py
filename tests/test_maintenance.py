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
