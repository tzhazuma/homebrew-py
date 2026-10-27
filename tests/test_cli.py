from __future__ import annotations

import json

from pathlib import Path


def test_info_json(run_cli) -> None:
    proc = run_cli("info", "--json", "hello")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    formula = payload["formulae"][0]
    assert formula["name"] == "hello"
    assert formula["versions"]["stable"] == "1.0"
    assert formula["desc"] == "Test formula for hello"
    assert formula["license"] == "MIT"
    assert formula["bottle"]["stable"]["files"]


def test_info_human(run_cli) -> None:
    proc = run_cli("info", "hello")
    assert proc.returncode == 0, proc.stderr
    assert "hello: stable 1.0" in proc.stdout
    assert "Not installed" in proc.stdout
    assert "Bottles:" in proc.stdout


def test_search(run_cli) -> None:
    proc = run_cli("search", "hello")
    assert proc.returncode == 0
    assert "hello:" in proc.stdout
    assert "hello-dep" in proc.stdout


def test_deps_tree(run_cli) -> None:
    proc = run_cli("deps", "--tree", "hello-dep")
    assert proc.returncode == 0, proc.stderr
    assert "hello-dep" in proc.stdout
    assert "dep" in proc.stdout


def test_list_and_config(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    listed = run_cli("list", "--versions")
    assert listed.returncode == 0
    assert "hello 1.0" in listed.stdout

    config = run_cli("config", "--json")
    assert config.returncode == 0
    payload = json.loads(config.stdout)
    assert payload["prefix"] == str(sample.prefix)
    assert payload["cellar"] == str(sample.prefix / "Cellar")
    assert payload["pybrew_version"].startswith("pybrew ")


def test_doctor_after_install(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    proc = run_cli("doctor")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "healthy" in proc.stdout


def test_parse_command(run_cli) -> None:
    corpus = Path(__file__).parent / "fixtures" / "core_formulas" / "hello.rb"
    proc = run_cli("parse", str(corpus), "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["name"] == "hello"
    assert payload["versions"]["stable"] == "2.12.3"


def test_unknown_formula_errors(run_cli) -> None:
    proc = run_cli("info", "definitely-not-a-formula-xyz")
    assert proc.returncode == 1
    assert "no formula named" in proc.stderr.lower()


def test_doctor_json_and_missing_cask_artifact(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    healthy = run_cli("doctor", "--json")
    assert healthy.returncode == 0, healthy.stdout + healthy.stderr
    payload = json.loads(healthy.stdout)
    assert payload["ok"] is True and payload["issues"] == []

    assert run_cli("cask", "install", "testapp").returncode == 0
    app = sample.prefix / "Applications" / "Test.app"
    import shutil

    shutil.rmtree(app)
    broken = run_cli("doctor", "--json")
    assert broken.returncode == 1
    payload = json.loads(broken.stdout)
    assert payload["ok"] is False
    assert any("missing cask artifact" in issue["message"] for issue in payload["issues"])


def test_shellenv(run_cli, sample) -> None:
    proc = run_cli("shellenv")
    assert proc.returncode == 0
    assert f'export HOMEBREW_PREFIX="{sample.prefix}"' in proc.stdout
    fish = run_cli("shellenv", "--shell", "fish")
    assert fish.returncode == 0
    assert "set -gx HOMEBREW_PREFIX" in fish.stdout


def test_cask_flags_on_search_outdated_upgrade(run_cli, sample) -> None:
    assert run_cli("install", "--cask", "testapp").returncode == 0
    search = run_cli("search", "--cask", "test")
    assert search.returncode == 0, search.stderr
    assert "testapp" in search.stdout
    cask_file = sample.tap / "Casks" / "t" / "testapp.rb"
    cask_file.write_text(cask_file.read_text().replace('version "1.0"', 'version "2.0"'))
    outdated = run_cli("outdated", "--cask")
    assert outdated.returncode == 0, outdated.stderr
    assert "testapp 1.0 < 2.0" in outdated.stdout
    payload = json.loads(run_cli("outdated", "--cask", "--json").stdout)
    assert payload["casks"][0]["current"] == "2.0"
    upgraded = run_cli("upgrade", "--cask", "testapp")
    assert upgraded.returncode == 0, upgraded.stderr
    assert "Upgraded: testapp 2.0" in upgraded.stdout


def test_fetch_formula_and_cask(run_cli, sample) -> None:
    proc = run_cli("fetch", "hello", bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr
    bottle_path = Path(proc.stdout.strip())
    assert bottle_path.is_file()
    assert not (sample.prefix / "Cellar" / "hello").exists()

    cask = run_cli("fetch", "--cask", "testapp")
    assert cask.returncode == 0, cask.stderr
    cask_path = Path(cask.stdout.strip())
    assert cask_path.is_file()
    assert not (sample.prefix / "Caskroom").exists()


def test_cat_home_edit_and_cask(run_cli, sample) -> None:
    cat = run_cli("cat", "hello")
    assert cat.returncode == 0, cat.stderr
    assert "class Hello" in cat.stdout
    cask_cat = run_cli("cat", "--cask", "testapp")
    assert cask_cat.returncode == 0
    assert 'cask "testapp"' in cask_cat.stdout

    home = run_cli("home", "hello")
    assert home.returncode == 0
    assert "https://example.com/hello" in home.stdout
    cask_home = run_cli("home", "--cask", "testapp")
    assert cask_home.returncode == 0
    assert "https://example.com/testapp" in cask_home.stdout

    edit = run_cli("edit", "hello", extra_env={"EDITOR": "true"})
    assert edit.returncode == 0, edit.stderr
