"""Synthetic tests for opt-in system-wide cask pkg installation.

These tests never use the real macOS installer, real root credentials or
system paths: a throwaway shell script records its argv and ``os.geteuid`` is
monkeypatched.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pybrew.cask import CaskError
from pybrew.cask_installer import CaskInstaller, CaskLoader
from pybrew.cli import build_parser
from pybrew.paths import Config
from pybrew.taps import discover_taps


def _make_pkg_cask(tmp_path: Path) -> tuple[Config, CaskLoader, bytes]:
    payload = b"synthetic package contents\n"
    package = tmp_path / "Synthetic-1.0.pkg"
    package.write_bytes(payload)
    token = "synthetic-pkg"
    tap = tmp_path / "tap"
    cask_dir = tap / "Casks" / "s"
    cask_dir.mkdir(parents=True)
    (cask_dir / f"{token}.rb").write_text(
        f'''cask "{token}" do
  version "1.0"
  sha256 "{hashlib.sha256(payload).hexdigest()}"
  url "{package.as_uri()}"
  pkg "Synthetic-1.0.pkg"
end
'''
    )
    config = Config.default(prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache")
    config.ensure()
    return config, CaskLoader(discover_taps([tap])), payload


def _fake_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, exit_code: int = 0
) -> tuple[Path, Path]:
    script = tmp_path / "fake-installer"
    log = tmp_path / "installer-argv.log"
    script.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$FAKE_INSTALLER_LOG"\n'
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PYBREW_INSTALLER", str(script))
    monkeypatch.setenv("FAKE_INSTALLER_LOG", str(log))
    return script, log


def _pretend_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    # CaskInstaller only reads sys.platform for pkg platform gating; a test
    # running on non-macOS still exercises the same code path.
    monkeypatch.setattr(sys, "platform", "darwin")


def test_system_pkg_success_invokes_installer_and_writes_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, loader, payload = _make_pkg_cask(tmp_path)
    _fake_installer(tmp_path, monkeypatch)
    _pretend_macos(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    pkg_root = tmp_path / "payload-root"
    installer = CaskInstaller(config, pkg_root=pkg_root, system_pkg=True)
    result = installer.install(loader.load("synthetic-pkg"))

    argv = (tmp_path / "installer-argv.log").read_text().splitlines()
    assert argv[0] == "-pkg"
    assert argv[2:] == ["-target", "/"]
    package = Path(argv[1])
    assert package.suffix == ".pkg"
    assert package.read_bytes() == payload

    receipt = json.loads((result.room / ".metadata" / "receipt.json").read_text())
    assert receipt["system_pkg"] is True
    assert receipt["installed"] == []
    assert result.artifacts == []
    # No payload expansion or copying in system mode.
    assert not (result.room / ".pkg-expanded").exists()
    assert not pkg_root.exists()


def test_system_pkg_non_root_raises_before_invoking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, loader, _ = _make_pkg_cask(tmp_path)
    _, log = _fake_installer(tmp_path, monkeypatch)
    _pretend_macos(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 501)

    installer = CaskInstaller(config, system_pkg=True)
    with pytest.raises(CaskError) as excinfo:
        installer.install(loader.load("synthetic-pkg"))

    message = str(excinfo.value).lower()
    assert "root" in message
    assert "--pkg-root" in message
    assert not log.exists()


def test_system_pkg_missing_installer_raises_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, loader, _ = _make_pkg_cask(tmp_path)
    _pretend_macos(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    missing = tmp_path / "missing-installer"
    monkeypatch.setenv("PYBREW_INSTALLER", str(missing))

    installer = CaskInstaller(config, system_pkg=True)
    with pytest.raises(CaskError) as excinfo:
        installer.install(loader.load("synthetic-pkg"))

    message = str(excinfo.value)
    assert "installer" in message.lower()
    assert str(missing) in message
    assert "PYBREW_INSTALLER" in message


def test_system_pkg_requires_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, loader, _ = _make_pkg_cask(tmp_path)
    _, log = _fake_installer(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    installer = CaskInstaller(config, system_pkg=True)
    with pytest.raises(CaskError, match="requires macOS"):
        installer.install(loader.load("synthetic-pkg"))
    assert not log.exists()


def test_system_pkg_installer_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, loader, _ = _make_pkg_cask(tmp_path)
    _fake_installer(tmp_path, monkeypatch, exit_code=1)
    _pretend_macos(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    installer = CaskInstaller(config, system_pkg=True)
    with pytest.raises(CaskError, match="failed"):
        installer.install(loader.load("synthetic-pkg"))


def test_cli_accepts_system_pkg_for_cask_install_paths() -> None:
    parser = build_parser()
    assert parser.parse_args(["install", "--cask", "--system-pkg", "synthetic-pkg"]).system_pkg
    assert parser.parse_args(["cask", "install", "--system-pkg", "synthetic-pkg"]).system_pkg
    assert parser.parse_args(["cask", "reinstall", "--system-pkg", "synthetic-pkg"]).system_pkg
    assert not parser.parse_args(["install", "--cask", "synthetic-pkg"]).system_pkg


def test_cli_plumbs_system_pkg_into_cask_installer(
    monkeypatch: pytest.MonkeyPatch, sample: SimpleNamespace
) -> None:
    import pybrew.cli as cli

    recorded: list[dict[str, object]] = []

    class RecordingInstaller:
        def __init__(self, config: Config, **kwargs: object) -> None:
            recorded.append(kwargs)

        def install(self, cask, force: bool = False):
            return SimpleNamespace(
                token=cask.token,
                version=cask.resolved_version(),
                room=Path("/tmp/room"),
                skipped=False,
            )

    monkeypatch.setattr(cli, "CaskInstaller", RecordingInstaller)
    base = [
        "--prefix",
        str(sample.prefix),
        "--tap",
        str(sample.tap),
        "--cache",
        str(sample.cache),
    ]
    parser = build_parser()
    install_args = parser.parse_args(
        [*base, "install", "--cask", "--system-pkg", "testapp"]
    )
    assert cli.cmd_install(install_args) == 0
    cask_args = parser.parse_args([*base, "cask", "install", "--system-pkg", "testapp"])
    assert cli.cmd_cask(cask_args) == 0
    assert [kwargs.get("system_pkg") for kwargs in recorded] == [True, True]
