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
