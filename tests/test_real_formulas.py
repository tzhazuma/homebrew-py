from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pybrew.ruby_parser import parse_formula_file

CORPUS = Path(__file__).parent / "fixtures" / "core_formulas"
EXPECTED = json.loads((CORPUS / "expected.json").read_text())


@pytest.fixture(autouse=True)
def match_homebrew_api_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    # expected.json was produced by the Homebrew JSON API on this machine,
    # where brew reports x86_64 (Rosetta) on macOS.  The parser honours these
    # overrides, so the test is deterministic on other CI operating systems.
    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("PYBREW_TARGET_ARCH", "x86_64")


def test_corpus_is_present() -> None:
    assert len(EXPECTED) >= 15
    for name, expected in EXPECTED.items():
        path = CORPUS / f"{name}.rb"
        assert path.is_file(), f"missing corpus formula {name}"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == expected["ruby_source_sha256"], f"corpus checksum drift for {name}"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_real_formula_matches_brew_api(name: str) -> None:
    expected = EXPECTED[name]
    formula = parse_formula_file(CORPUS / f"{name}.rb", tap="homebrew/core")

    assert formula.name == name
    assert formula.desc == expected["desc"]
    assert formula.homepage == expected["homepage"]
    assert formula.version == expected["version"]
    assert formula.revision == expected["revision"]
    assert formula.keg_only == expected["keg_only"]
    if expected["license"] is not None:
        assert formula.license == expected["license"]

    runtime = sorted(
        {
            dep.name
            for dep in formula.dependencies
            if dep.runtime and not dep.optional and not dep.recommended
        }
    )
    assert runtime == sorted(expected["dependencies"])
    build = sorted({dep.name for dep in formula.dependencies if dep.build})
    assert build == sorted(expected["build_dependencies"])
    test_deps = sorted({dep.name for dep in formula.dependencies if dep.test})
    assert test_deps == sorted(expected["test_dependencies"])
    uses = sorted({dep.name for dep in formula.uses_from_macos})
    assert uses == sorted(expected["uses_from_macos"])

    # Every real formula in the corpus ships at least one bottle.
    assert formula.bottle.files, f"{name} has no parsed bottle block"


def test_license_and_dependency_parity_examples() -> None:
    autoconf = parse_formula_file(CORPUS / "autoconf.rb", tap="homebrew/core")
    assert autoconf.license == (
        "GPL-3.0-or-later AND (GPL-3.0-or-later WITH Autoconf-exception-3.0)"
    )
    git = parse_formula_file(CORPUS / "git.rb", tap="homebrew/core")
    assert git.license == (
        "GPL-2.0-only AND GPL-2.0-or-later AND LGPL-2.1-or-later AND BSD-3-Clause AND MIT"
    )
    # gettext appears in both runtime and build dependency sets.
    assert "gettext" in {dep.name for dep in git.dependencies if dep.runtime}
    assert "gettext" in {dep.name for dep in git.dependencies if dep.build}
