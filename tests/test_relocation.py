from __future__ import annotations

from pathlib import Path

from pybrew.installer import relocate_tree
from pybrew.paths import Config


def test_binary_placeholder_relocation_pads_string_end(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "lib").mkdir(parents=True)
    original = (
        b"\xcf\xfa\xed\xfe"
        b"@@HOMEBREW_PREFIX@@/lib/libfoo.1.dylib\x00"
        b"@@HOMEBREW_CELLAR@@/foo/1.0\x00"
        + b"\x00" * 16
    )
    (keg / "lib" / "libfoo.dylib").write_bytes(original)
    (keg / "lib" / "foo.pc").write_text("prefix=@@HOMEBREW_PREFIX@@\n")
    config = Config.default(prefix="/opt/hb", taps=[], cache=tmp_path / "cache")
    report = relocate_tree(keg, config)
    data = (keg / "lib" / "libfoo.dylib").read_bytes()
    assert len(data) == len(original)
    assert b"/opt/hb/lib/libfoo.1.dylib\x00" in data
    assert b"/opt/hb/Cellar/foo/1.0\x00" in data
    assert b"@@HOMEBREW_PREFIX@@" not in data
    assert (keg / "lib" / "foo.pc").read_text() == "prefix=/opt/hb\n"
    assert report.binary_files == 1
    assert report.text_files == 1


def test_long_prefix_skips_binary_without_corruption(tmp_path: Path) -> None:
    keg = tmp_path / "keg"
    (keg / "lib").mkdir(parents=True)
    original = (
        b"\xcf\xfa\xed\xfe@@HOMEBREW_PREFIX@@/lib/libfoo.dylib\x00"
        + b"\x00" * 8
    )
    path = keg / "lib" / "libfoo.dylib"
    path.write_bytes(original)
    config = Config.default(prefix="/private/var/folders/very/long/prefix", taps=[], cache=tmp_path / "cache")
    report = relocate_tree(keg, config)
    assert path.read_bytes() == original
    assert report.skipped_binaries == 1
