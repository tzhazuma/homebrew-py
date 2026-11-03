#!/usr/bin/env python3
"""Reproducible real pybrew install/uninstall check.

Builds a tiny local source formula (no network), installs it into a temporary
prefix, and verifies the keg, opt link, receipt, executable, and uninstall.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_tarball(root: Path) -> Path:
    source = root / "hello-1.0"
    source.mkdir(parents=True)
    script = source / "hello.sh"
    script.write_text("#!/bin/sh\necho hello-from-pybrew\n")
    script.chmod(0o755)
    archive = root / "hello-1.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname="hello-1.0")
    return archive


def _make_tap(root: Path, archive: Path) -> Path:
    tap = root / "tap"
    (tap / "Formula").mkdir(parents=True)
    (tap / "Formula" / "hello-pybrew.rb").write_text(
        f'''class HelloPybrew < Formula
  desc "pybrew real install check"
  homepage "https://example.invalid/hello-pybrew"
  url "{archive.as_uri()}"
  sha256 "{_sha256(archive)}"
  version "1.0"

  def install
    bin.install "hello.sh"
  end
end
'''
    )
    return tap


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args))
    return subprocess.run(args, env=env, text=True, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the temporary prefix")
    ns = parser.parse_args()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")

    with tempfile.TemporaryDirectory(prefix="pybrew-real-install-") as tmp:
        root = Path(tmp)
        archive = _make_tarball(root)
        tap = _make_tap(root, archive)
        prefix = root / "prefix"
        cache = root / "cache"
        base = [sys.executable, "-m", "pybrew"]

        result = _run(
            base
            + [
                "install",
                "hello-pybrew",
                "--tap",
                str(tap),
                "--prefix",
                str(prefix),
                "--cache",
                str(cache),
                "--ignore-dependencies",
            ],
            env,
        )
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode != 0:
            return result.returncode

        keg = prefix / "Cellar" / "hello-pybrew" / "1.0"
        binary = keg / "bin" / "hello.sh"
        receipt = json.loads((keg / "INSTALL_RECEIPT.json").read_text())
        opt = prefix / "opt" / "hello-pybrew"
        assert binary.is_file(), f"missing {binary}"
        assert os.access(binary, os.X_OK), f"not executable: {binary}"
        assert opt.is_symlink(), f"missing opt link: {opt}"
        assert receipt["version"] == "1.0", receipt
        assert receipt["poured_from_bottle"] is False, receipt
        assert receipt["build"]["from_bottle"] is False, receipt
        assert receipt["sbom"]["file_count"] >= 1, receipt
        assert any(item["path"] == "bin/hello.sh" for item in receipt["sbom"]["files"])
        run = subprocess.run([str(binary)], text=True, capture_output=True)
        assert run.stdout.strip() == "hello-from-pybrew", run
        print("installed:", keg)
        print("receipt:", json.dumps(receipt, sort_keys=True))
        print("run:", run.stdout.strip())

        result = _run(
            base
            + [
                "uninstall",
                "hello-pybrew",
                "--tap",
                str(tap),
                "--prefix",
                str(prefix),
                "--cache",
                str(cache),
            ],
            env,
        )
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode != 0:
            return result.returncode
        assert not keg.exists(), f"keg still exists: {keg}"
        assert not opt.exists() and not opt.is_symlink(), f"opt still exists: {opt}"
        print("uninstalled: ok")

        if ns.keep:
            keep = Path(tempfile.mkdtemp(prefix="pybrew-real-install-keep-"))
            print("temporary tree was under:", root, "(removed); keep requested:", keep)

    print("REAL INSTALL VERIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
