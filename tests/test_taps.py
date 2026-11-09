from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pybrew.errors import PybrewError
from pybrew.paths import Config
from pybrew.tap_manager import TapManager
from pybrew.taps import FormulaLoader, discover_taps


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _make_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote"
    remote.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(remote)], check=True, capture_output=True)
    (remote / "Formula").mkdir()
    (remote / "Formula" / "hello.rb").write_text(
        'class Hello < Formula\n'
        '  desc "tap hello"\n'
        '  homepage "https://example.com"\n'
        '  url "https://example.com/hello-1.0.tar.gz"\n'
        '  sha256 "' + "0" * 64 + '"\n'
        "end\n"
    )
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "initial tap")
    return remote


def test_tap_install_list_and_untap(tmp_path: Path) -> None:
    remote = _make_remote(tmp_path)
    config = Config.default(prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache")
    manager = TapManager(config)

    result = manager.tap("tzhazuma/demo", url=str(remote))
    assert result.action == "installed"
    assert result.path == config.taps_root / "tzhazuma" / "homebrew-demo"
    assert result.formulas == 1
    assert (result.path / "Formula" / "hello.rb").is_file()

    taps = manager.list()
    assert [tap.name for tap in taps] == ["tzhazuma/demo"]
    loader = FormulaLoader(taps)
    assert loader.load("hello").desc == "tap hello"

    # Update from the fake remote.
    (remote / "Formula" / "hello.rb").write_text((remote / "Formula" / "hello.rb").read_text().replace("tap hello", "updated hello"))
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "update")
    updated = manager.tap("tzhazuma/demo", url=str(remote))
    assert updated.action == "updated"
    assert FormulaLoader(manager.list()).load("hello").desc == "updated hello"

    removed = manager.untap("tzhazuma/demo")
    assert removed == result.path
    assert not removed.exists()
    assert manager.list() == []


def test_tap_rejects_bad_name(tmp_path: Path) -> None:
    config = Config.default(prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache")
    manager = TapManager(config)
    with pytest.raises(PybrewError):
        manager.tap("not-a-valid-tap")


def test_cli_tap_list_and_install(tmp_path: Path, sample, run_cli) -> None:
    remote = _make_remote(tmp_path)
    proc = run_cli("tap", "tzhazuma/demo", "--url", str(remote))
    assert proc.returncode == 0, proc.stderr
    assert (sample.prefix / "Library" / "Taps" / "tzhazuma" / "homebrew-demo" / "Formula" / "hello.rb").is_file()

    listed = run_cli("tap", "--list")
    assert listed.returncode == 0
    assert "tzhazuma/demo" in listed.stdout

    untapped = run_cli("untap", "tzhazuma/demo")
    assert untapped.returncode == 0, untapped.stderr
    assert not (sample.prefix / "Library" / "Taps" / "tzhazuma" / "homebrew-demo").exists()


def test_update_all_taps(tmp_path: Path) -> None:
    remote = _make_remote(tmp_path)
    config = Config.default(prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache")
    manager = TapManager(config)
    assert manager.tap("tzhazuma/demo", url=str(remote)).action == "installed"
    formula = remote / "Formula" / "hello.rb"
    formula.write_text(formula.read_text().replace("tap hello", "updated all"))
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "update all")
    results = manager.update_all()
    assert results and results[0].action == "updated"
    assert FormulaLoader(manager.list()).load("hello").desc == "updated all"


def test_update_command(run_cli, sample, tmp_path: Path) -> None:
    remote = _make_remote(tmp_path)
    tapped = run_cli("tap", "tzhazuma/demo", "--url", str(remote))
    assert tapped.returncode == 0, tapped.stderr
    formula = remote / "Formula" / "hello.rb"
    formula.write_text(formula.read_text().replace("tap hello", "updated via brew update"))
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "update")
    proc = run_cli("update")
    assert proc.returncode == 0, proc.stderr
    assert "updated: tzhazuma/demo" in proc.stdout


def test_formula_loader_falls_back_to_api_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pybrew.api import formula_from_api
    from pybrew.taps import FormulaLoader

    called: list[str] = []

    def fake_fetch(name: str, **_kwargs):
        called.append(name)
        return formula_from_api(
            {
                "name": name,
                "desc": "API fallback",
                "homepage": "https://example.com",
                "versions": {"stable": "1.0"},
                "bottle": {"stable": {"files": {"arm64_sonoma": {"sha256": "a" * 64}}}},
            }
        )

    monkeypatch.setattr("pybrew.api.fetch_formula", fake_fetch)
    loader = FormulaLoader([], api_fallback=True)
    formula = loader.load("gcc")
    assert called == ["gcc"]
    assert formula.name == "gcc"
    assert formula.version == "1.0"
    assert loader.exists("gcc") is True


def test_fetch_formula_uses_mtime_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pybrew import api

    calls: list[str] = []
    payload = {
        "name": "cached-formula",
        "desc": "cached",
        "homepage": "https://example.com",
        "versions": {"stable": "1.0"},
    }

    def fake_request(url: str, timeout: int = 30):
        calls.append(url)
        return payload

    monkeypatch.setattr(api, "_request_json", fake_request)
    cache = tmp_path / "formula.json"
    first = api.fetch_formula("cached-formula", cache_path=cache)
    second = api.fetch_formula("cached-formula", cache_path=cache)
    assert first.version == second.version == "1.0"
    assert len(calls) == 1
