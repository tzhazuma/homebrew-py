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


ELF_MAGIC = b"\x7fELF"


def _elf_blob(rpath: bytes) -> bytes:
    """Synthetic ELF-magic file with the placeholder bytes a real rpath has."""

    return (
        ELF_MAGIC
        + b"\x02\x01\x01"
        + b"\x00" * 9
        + rpath
        + b"\x00"
        + b"\x00" * 16
    )


def _write_keg(tmp_path: Path, original: bytes) -> tuple[Path, Path]:
    keg = tmp_path / "keg"
    binary = keg / "bin" / "tool"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(original)
    return keg, binary


def _fake_patchelf(
    tmp_path: Path, monkeypatch, rpath: str, *, fail_set_rpath: bool = False
) -> Path:
    """Create a shell-script patchelf stand-in that records its arguments."""

    script = tmp_path / "patchelf"
    record = tmp_path / "patchelf-args.log"
    new_rpath_file = tmp_path / "patchelf-new-rpath.txt"
    if fail_set_rpath:
        # Fail after truncating the target so the caller must restore it.
        body = (
            '  --print-rpath) printf "%s\\n" "${PYBREW_PATCHELF_RPATH:-}" ;;\n'
            '  --set-rpath) : > "$3"; exit 1 ;;\n'
        )
    else:
        body = (
            '  --print-rpath) printf "%s\\n" "${PYBREW_PATCHELF_RPATH:-}" ;;\n'
            '  --set-rpath) printf "%s\\n" "$2" > "${PYBREW_PATCHELF_NEW_RPATH:?}" ;;\n'
        )
    script.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "${PYBREW_PATCHELF_LOG:?}"\n'
        'case "$1" in\n'
        + body
        + "  *) exit 64 ;;\n"
        "esac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PYBREW_PATCHELF", str(script))
    monkeypatch.setenv("PYBREW_PATCHELF_LOG", str(record))
    monkeypatch.setenv("PYBREW_PATCHELF_RPATH", rpath)
    monkeypatch.setenv("PYBREW_PATCHELF_NEW_RPATH", str(new_rpath_file))
    return record


def test_elf_rpath_relocated_with_patchelf(tmp_path: Path, monkeypatch) -> None:
    old_rpath = "@@HOMEBREW_PREFIX@@/lib:@@HOMEBREW_CELLAR@@/foo/1.0"
    original = _elf_blob(old_rpath.encode())
    keg, binary = _write_keg(tmp_path, original)
    # Bottle binaries are often not user-writable; relocation must cope.
    binary.chmod(0o555)
    record = _fake_patchelf(tmp_path, monkeypatch, old_rpath)

    config = Config.default(prefix="/opt/pybrew-prefix-long", taps=[], cache=tmp_path / "cache")
    report = relocate_tree(keg, config)

    calls = record.read_text().splitlines()
    expected_rpath = "/opt/pybrew-prefix-long/lib:/opt/pybrew-prefix-long/Cellar/foo/1.0"
    assert calls[0] == f"--print-rpath {binary}"
    assert calls[1] == f"--set-rpath {expected_rpath} {binary}"
    assert (tmp_path / "patchelf-new-rpath.txt").read_text().strip() == expected_rpath
    assert report.binary_files == 1
    assert report.skipped_binaries == 0
    # The fake does not edit the file itself, so the caller must not either.
    assert binary.read_bytes() == original
    assert binary.stat().st_mode & 0o777 == 0o555


def test_elf_without_patchelf_is_skipped_unchanged(tmp_path: Path, monkeypatch) -> None:
    original = _elf_blob(b"@@HOMEBREW_PREFIX@@/lib:@@HOMEBREW_CELLAR@@/foo/1.0")
    keg, binary = _write_keg(tmp_path, original)
    monkeypatch.setenv("PYBREW_PATCHELF", "")
    monkeypatch.setenv("PATH", "")

    config = Config.default(prefix="/opt/pybrew-prefix-long", taps=[], cache=tmp_path / "cache")
    report = relocate_tree(keg, config)

    assert binary.read_bytes() == original
    assert report.skipped_binaries == 1
    assert report.binary_files == 0


def test_elf_patchelf_failure_restores_file_and_skips(tmp_path: Path, monkeypatch) -> None:
    old_rpath = "@@HOMEBREW_PREFIX@@/lib"
    original = _elf_blob(old_rpath.encode())
    keg, binary = _write_keg(tmp_path, original)
    _fake_patchelf(tmp_path, monkeypatch, old_rpath, fail_set_rpath=True)

    config = Config.default(prefix="/opt/pybrew-prefix-long", taps=[], cache=tmp_path / "cache")
    report = relocate_tree(keg, config)

    assert binary.read_bytes() == original
    assert report.skipped_binaries == 1
    assert report.binary_files == 0
