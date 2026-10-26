from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from pybrew.cellar import read_receipt
from pybrew.installer import InstallOptions, Installer
from pybrew.paths import Config
from pybrew.ruby_parser import parse_formula_file
from pybrew.taps import FormulaLoader, discover_taps

pytestmark = pytest.mark.real_bottle


def _find_cached_libslirp() -> Path | None:
    candidates = sorted(
        (Path.home() / "Library" / "Caches" / "Homebrew" / "downloads").glob(
            "*libslirp*.bottle.tar.gz"
        )
    )
    return candidates[0] if candidates else None


def test_install_real_homebrew_bottle(tmp_path: Path) -> None:
    bottle = _find_cached_libslirp()
    if bottle is None:
        pytest.skip("no cached libslirp bottle on this machine")

    with tarfile.open(bottle) as tar:
        member = next(
            (item for item in tar.getmembers() if item.name.endswith(".brew/libslirp.rb")),
            None,
        )
        assert member is not None, "bottle does not contain its formula source"
        source = tar.extractfile(member)
        assert source is not None
        formula_ruby = source.read().decode()

    tap_root = tmp_path / "tap"
    (tap_root / "Formula").mkdir(parents=True)
    formula_path = tap_root / "Formula" / "libslirp.rb"
    formula_path.write_text(formula_ruby)
    formula = parse_formula_file(formula_path, tap="homebrew/core")
    assert formula.version == "4.9.6"
    # The formula copy inside .brew/ is the source formula; bottles do not
    # carry their own bottle block, so attach the cached artefact's metadata.
    from pybrew.download import sha256_file
    from pybrew.models import BottleFile, BottleSpec, candidate_bottle_tags

    tag = candidate_bottle_tags()[0]
    formula.bottle = BottleSpec(
        root_url="file://" + str(bottle.parent),
        files={tag: BottleFile(tag, sha256_file(bottle), ":any")},
    )

    config = Config.default(
        prefix=tmp_path / "prefix",
        taps=[tap_root],
        cache=tmp_path / "cache",
    )
    config.ensure()
    loader = FormulaLoader(discover_taps([tap_root]))
    installer = Installer(config, loader)
    results = installer.install(
        formula,
        InstallOptions(ignore_dependencies=True, bottle_file=bottle),
    )
    result = results[-1]
    keg = config.keg_path("libslirp", "4.9.6")
    assert result.keg == keg
    assert keg.is_dir()
    assert (keg / "lib" / "libslirp.dylib").exists()
    assert (keg / "include" / "slirp" / "libslirp.h").is_file()
    receipt = read_receipt(keg)
    assert receipt["poured_from_bottle"] is True
    assert receipt["runtime_dependencies"] == ["glib"]
    # Real bottles are relocatable; text files should not retain placeholders.
    for path in keg.rglob("*.pc"):
        if path.is_file():
            assert b"@@HOMEBREW_PREFIX@@" not in path.read_bytes()
