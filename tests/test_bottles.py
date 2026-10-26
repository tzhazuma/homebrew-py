from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from pybrew.errors import BottleError
from pybrew.installer import safe_extract_tar


def _receipt(keg: Path) -> dict:
    return json.loads((keg / "INSTALL_RECEIPT.json").read_text())


def test_install_bottle_end_to_end(run_cli, sample) -> None:
    proc = run_cli("install", "hello", bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "hello 1.0" in proc.stdout

    keg = sample.prefix / "Cellar" / "hello" / "1.0"
    assert keg.is_dir()
    assert (keg / "bin" / "hello").is_file()
    assert (keg / "lib" / "libhello.dylib").is_file()
    assert (keg / "lib" / "libhello.1.dylib").is_symlink()

    receipt = _receipt(keg)
    assert receipt["poured_from_bottle"] is True
    assert receipt["built_as_bottle"] is True
    assert receipt["installed_on_request"] is True

    linked = sample.prefix / "bin" / "hello"
    assert linked.is_symlink()
    assert linked.exists()
    script = linked.read_text()
    assert "@@HOMEBREW_PREFIX@@" not in script
    assert str(sample.prefix) in script

    opt = sample.prefix / "opt" / "hello"
    assert opt.is_symlink()
    assert opt.resolve() == keg.resolve()

    # The cache holds the verified bottle under its SHA-256 prefix.
    cached = list((sample.cache / "downloads").glob(f"{sample.bottle_sha}--*"))
    assert cached, "bottle was not cached"


def test_uninstall_removes_keg_and_links(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    proc = run_cli("uninstall", "hello")
    assert proc.returncode == 0, proc.stderr
    assert not (sample.prefix / "Cellar" / "hello").exists()
    assert not (sample.prefix / "bin" / "hello").exists()
    assert not (sample.prefix / "opt" / "hello").exists()


def test_link_conflict_and_overwrite(run_cli, sample) -> None:
    bin_dir = sample.prefix / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "hello").write_text("user file\n")

    proc = run_cli("install", "hello", bottle_tag=sample.tag)
    assert proc.returncode == 1
    assert "conflict" in (proc.stderr + proc.stdout).lower()
    assert (bin_dir / "hello").read_text() == "user file\n"
    assert (sample.prefix / "Cellar" / "hello" / "1.0").is_dir()

    proc = run_cli("install", "--force", "hello", bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (bin_dir / "hello").is_symlink()


def test_bottle_checksum_mismatch_aborts(run_cli, sample) -> None:
    original = sample.formula.read_text()
    sample.formula.write_text(original.replace(sample.bottle_sha, "0" * 64))
    proc = run_cli("install", "hello", bottle_tag=sample.tag)
    assert proc.returncode == 1
    assert "SHA-256" in (proc.stderr + proc.stdout)
    assert not (sample.prefix / "Cellar" / "hello").exists()


def test_dry_run_does_not_install(run_cli, sample) -> None:
    proc = run_cli("install", "--dry-run", "hello", bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr
    assert "Would install" in proc.stdout
    assert not (sample.prefix / "Cellar" / "hello" / "1.0").exists()


def test_keg_only_only_creates_opt_link(run_cli, sample) -> None:
    original = sample.formula.read_text()
    sample.formula.write_text(original.replace('  license "MIT"', '  license "MIT"\n  keg_only "test"'))
    proc = run_cli("install", "hello", bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr
    assert (sample.prefix / "opt" / "hello").is_symlink()
    assert not (sample.prefix / "bin" / "hello").exists()


def test_dependency_install_order(run_cli, sample) -> None:
    proc = run_cli("install", "hello-dep", bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (sample.prefix / "Cellar" / "dep" / "2.0").is_dir()
    assert (sample.prefix / "Cellar" / "hello-dep" / "1.0").is_dir()
    assert (sample.prefix / "bin" / "dep").is_symlink()
    assert (sample.prefix / "bin" / "hello-dep").is_symlink()


def test_source_install_staged_formula(run_cli, sample) -> None:
    proc = run_cli("install", "--build-from-source", "hello", bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    keg = sample.prefix / "Cellar" / "hello" / "1.0"
    assert (keg / "bin" / "hello").is_file()
    assert (keg / "README").is_file()
    receipt = _receipt(keg)
    assert receipt["poured_from_bottle"] is False
    assert (sample.prefix / "bin" / "hello").is_symlink()


def test_unsafe_tar_members_are_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("../evil.txt")
        payload = b"pwned"
        info.size = len(payload)
        import io

        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(BottleError):
        safe_extract_tar(archive, tmp_path / "extract")
    assert not (tmp_path / "evil.txt").exists()


def test_absolute_symlinks_are_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "evil-link.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("hello/1.0/passwd")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(BottleError):
        safe_extract_tar(archive, tmp_path / "extract2")
