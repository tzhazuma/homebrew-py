"""Synthetic tests for system-wide pkg uninstall/rollback.

Every test uses throwaway executables and a temporary allowed root: no real
``installer``, no real ``pkgutil``, no root credentials and no system paths are
touched.  ``pkgutil`` itself is faked so the tests are deterministic on macOS
and Linux alike.
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
from pybrew.paths import Config
from pybrew.taps import discover_taps

_FAKE_PKGUTIL = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_PKGUTIL_LOG"
case "$1" in
  --pkgs)
    if [ -n "$FAKE_PKGUTIL_FAIL_AFTER" ] && [ -f "$FAKE_PKGUTIL_FAIL_AFTER" ]; then
      echo "simulated pkgutil failure" >&2
      exit 1
    fi
    if [ -f "$FAKE_PKGUTIL_AFTER" ]; then
      cat "$FAKE_PKGUTIL_AFTER"
    else
      cat "$FAKE_PKGUTIL_BEFORE"
    fi
    ;;
  --files)
    file="$FAKE_PKGUTIL_FILES/$2"
    if [ ! -f "$file" ]; then
      echo "no files for $2" >&2
      exit 1
    fi
    cat "$file"
    ;;
  --pkg-info)
    printf 'package-id: %s\\nversion: 1.0\\nvolume: /\\nlocation: %s\\n' \
      "$2" "${FAKE_PKGUTIL_LOCATION:-/}"
    ;;
  --forget)
    exit 0
    ;;
esac
exit 0
"""

_FAKE_INSTALLER = """#!/bin/sh
printf '%s\\n' "$*" > "$FAKE_INSTALLER_LOG"
if [ -n "$FAKE_INSTALLER_AFTER_FILE" ]; then
  cp "$FAKE_INSTALLER_AFTER_FILE" "$FAKE_PKGUTIL_AFTER"
fi
if [ -n "$FAKE_INSTALLER_FAIL_PKGUTIL_AFTER" ]; then
  : > "$FAKE_INSTALLER_FAIL_PKGUTIL_AFTER"
fi
exit 0
"""


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def _make_pkg_cask(tmp_path: Path) -> tuple[Config, CaskLoader]:
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
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    return config, CaskLoader(discover_taps([tap]))


def _fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    pkgutil = _write_executable(tmp_path / "fake-pkgutil", _FAKE_PKGUTIL)
    installer = _write_executable(tmp_path / "fake-installer", _FAKE_INSTALLER)
    files = tmp_path / "pkgutil-files"
    files.mkdir()
    log = tmp_path / "pkgutil.log"
    monkeypatch.setenv("PYBREW_PKGUTIL", str(pkgutil))
    monkeypatch.setenv("PYBREW_INSTALLER", str(installer))
    monkeypatch.setenv("FAKE_PKGUTIL_LOG", str(log))
    monkeypatch.setenv("FAKE_PKGUTIL_FILES", str(files))
    monkeypatch.setenv("FAKE_INSTALLER_LOG", str(tmp_path / "installer.log"))
    return SimpleNamespace(pkgutil=pkgutil, installer=installer, files=files, log=log)


def _pretend_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")


def _make_system_receipt(
    config: Config,
    *,
    token: str = "synthetic-pkg",
    version: str = "1.0",
    ids: list[str] | None = None,
    tracked: bool = True,
    system_flag: bool = True,
) -> Path:
    room = config.caskroom / token / version
    metadata = room / ".metadata"
    metadata.mkdir(parents=True)
    receipt: dict[str, object] = {
        "token": token,
        "version": version,
        "installed": [],
    }
    if system_flag:
        receipt["system_pkg"] = True
    if ids is not None:
        receipt["system_pkg_ids"] = list(ids)
    receipt["system_pkg_tracked"] = tracked
    (metadata / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return room


def _touch(path: Path, data: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


def test_system_pkg_install_records_new_package_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, loader = _make_pkg_cask(tmp_path)
    tools = _fake_tools(tmp_path, monkeypatch)
    (tmp_path / "before.txt").write_text(
        "com.example.preexisting\ncom.example.other\n"
    )
    (tmp_path / "after.txt").write_text(
        "com.example.preexisting\ncom.example.newpkg\n"
        "com.example.other\ncom.example.secondpkg\n"
    )
    monkeypatch.setenv("FAKE_PKGUTIL_BEFORE", str(tmp_path / "before.txt"))
    monkeypatch.setenv("FAKE_PKGUTIL_AFTER", str(tmp_path / "pkgs-after.txt"))
    monkeypatch.setenv("FAKE_INSTALLER_AFTER_FILE", str(tmp_path / "after.txt"))
    _pretend_macos(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    installer = CaskInstaller(config, system_pkg=True)
    result = installer.install(loader.load("synthetic-pkg"))

    receipt = json.loads((result.room / ".metadata" / "receipt.json").read_text())
    assert receipt["system_pkg"] is True
    assert receipt["system_pkg_ids"] == [
        "com.example.newpkg",
        "com.example.secondpkg",
    ]
    assert receipt["system_pkg_tracked"] is True
    assert "com.example.preexisting" not in receipt["system_pkg_ids"]

    pkgutil_calls = tools.log.read_text().splitlines()
    assert pkgutil_calls.count("--pkgs") == 2
    installer_log = (tmp_path / "installer.log").read_text()
    assert "-pkg" in installer_log
    assert "-target /" in installer_log


def test_system_pkg_install_rejects_missing_configured_pkgutil(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, loader = _make_pkg_cask(tmp_path)
    _fake_tools(tmp_path, monkeypatch)
    monkeypatch.setenv("PYBREW_PKGUTIL", str(tmp_path / "missing-pkgutil"))
    _pretend_macos(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    installer = CaskInstaller(config, system_pkg=True)
    with pytest.raises(CaskError, match="pkgutil"):
        installer.install(loader.load("synthetic-pkg"))
    # Refused before the installer ran.
    assert not (tmp_path / "installer.log").exists()


def test_system_pkg_install_after_snapshot_failure_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, loader = _make_pkg_cask(tmp_path)
    _fake_tools(tmp_path, monkeypatch)
    sentinel = _touch(tmp_path / "preexisting-file")
    (tmp_path / "before.txt").write_text("com.example.preexisting\n")
    (tmp_path / "after.txt").write_text(
        "com.example.preexisting\ncom.example.newpkg\n"
    )
    monkeypatch.setenv("FAKE_PKGUTIL_BEFORE", str(tmp_path / "before.txt"))
    monkeypatch.setenv("FAKE_PKGUTIL_AFTER", str(tmp_path / "pkgs-after.txt"))
    monkeypatch.setenv("FAKE_INSTALLER_AFTER_FILE", str(tmp_path / "after.txt"))
    fail_marker = tmp_path / "fail-after"
    monkeypatch.setenv("FAKE_PKGUTIL_FAIL_AFTER", str(fail_marker))
    monkeypatch.setenv("FAKE_INSTALLER_FAIL_PKGUTIL_AFTER", str(fail_marker))
    _pretend_macos(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    installer = CaskInstaller(config, system_pkg=True)
    with pytest.raises(CaskError) as excinfo:
        installer.install(loader.load("synthetic-pkg"))

    message = str(excinfo.value)
    assert "no files were removed" in message
    # The installer did run, but pybrew must not touch pre-existing files or
    # claim the cask is installed when tracking failed.
    assert (tmp_path / "installer.log").exists()
    assert sentinel.read_text() == "x\n"
    room = config.caskroom / "synthetic-pkg" / "1.0"
    assert not (room / ".metadata" / "receipt.json").exists()


def test_uninstall_system_pkg_removes_files_under_allowed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _ = _make_pkg_cask(tmp_path)
    tools = _fake_tools(tmp_path, monkeypatch)
    allowed = tmp_path / "allowed"
    tool = _touch(allowed / "usr/local/bin/tool")
    other = _touch(allowed / "usr/local/bin/other-tool")
    app_binary = _touch(allowed / "Applications/Test.app/Contents/MacOS/Test")
    room = _make_system_receipt(config, ids=["com.example.testpkg"])
    # Real pkgutil prints paths relative to the package's install location.
    (tools.files / "com.example.testpkg").write_text(
        "usr/local/bin/tool\n"
        "usr/local/bin\n"
        "Applications/Test.app/Contents/MacOS/Test\n"
        "Applications/Test.app/Contents/MacOS\n"
        "Applications/Test.app/Contents\n"
        "Applications/Test.app\n"
    )
    monkeypatch.setenv("PYBREW_SYSTEM_PKG_ROOTS", str(allowed))
    monkeypatch.setenv("FAKE_PKGUTIL_LOCATION", str(allowed))
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    removed = CaskInstaller(config).uninstall("synthetic-pkg")

    assert not tool.exists()
    assert not app_binary.exists()
    # Directories shared with other packages are not recursively deleted.
    assert other.exists()
    assert (allowed / "usr/local/bin").is_dir()
    assert not (allowed / "Applications/Test.app").exists()
    log = tools.log.read_text()
    assert "--pkg-info com.example.testpkg" in log
    assert "--files com.example.testpkg" in log
    assert "--forget com.example.testpkg" in log
    assert removed == [room]
    assert not (config.caskroom / "synthetic-pkg").exists()


def test_uninstall_detects_system_pkg_ids_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _ = _make_pkg_cask(tmp_path)
    tools = _fake_tools(tmp_path, monkeypatch)
    allowed = tmp_path / "allowed"
    tool = _touch(allowed / "Library/Application Support/Test/data.txt")
    _make_system_receipt(
        config, ids=["com.example.onlyids"], system_flag=False
    )
    (tools.files / "com.example.onlyids").write_text(f"{tool}\n")
    monkeypatch.setenv("PYBREW_SYSTEM_PKG_ROOTS", str(allowed))
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    CaskInstaller(config).uninstall("synthetic-pkg")

    assert not tool.exists()
    assert "--forget com.example.onlyids" in tools.log.read_text()


def test_uninstall_system_pkg_non_root_raises_before_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _ = _make_pkg_cask(tmp_path)
    tools = _fake_tools(tmp_path, monkeypatch)
    allowed = tmp_path / "allowed"
    tool = _touch(allowed / "usr/local/bin/tool")
    room = _make_system_receipt(config, ids=["com.example.testpkg"])
    (tools.files / "com.example.testpkg").write_text(f"{tool}\n")
    monkeypatch.setenv("PYBREW_SYSTEM_PKG_ROOTS", str(allowed))
    monkeypatch.setattr(os, "geteuid", lambda: 501)

    installer = CaskInstaller(config)
    actions: list[object] = []
    monkeypatch.setattr(
        installer, "_run_uninstall_actions", lambda cask: actions.append(cask)
    )
    with pytest.raises(CaskError, match="root"):
        installer.uninstall("synthetic-pkg", cask=SimpleNamespace(uninstall={}))

    assert tool.exists()
    assert room.is_dir()
    assert actions == []
    assert not tools.log.exists()


@pytest.mark.parametrize("unsafe", ["/etc/passwd", "../../../../etc/passwd"])
def test_uninstall_system_pkg_refuses_path_outside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    config, _ = _make_pkg_cask(tmp_path)
    tools = _fake_tools(tmp_path, monkeypatch)
    allowed = tmp_path / "allowed"
    safe = _touch(allowed / "usr/local/bin/tool")
    room = _make_system_receipt(config, ids=["com.example.testpkg"])
    (tools.files / "com.example.testpkg").write_text(f"{safe}\n{unsafe}\n")
    monkeypatch.setenv("PYBREW_SYSTEM_PKG_ROOTS", str(allowed))
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    installer = CaskInstaller(config)
    with pytest.raises(CaskError, match="outside allowed roots"):
        installer.uninstall("synthetic-pkg")

    # Validation happens before deletion: the safe path and receipt survive.
    assert safe.exists()
    assert room.is_dir()
    log = tools.log.read_text()
    assert "--files com.example.testpkg" in log
    assert "--forget" not in log


def test_uninstall_system_pkg_without_tracked_ids_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _ = _make_pkg_cask(tmp_path)
    tools = _fake_tools(tmp_path, monkeypatch)
    room = _make_system_receipt(config, ids=[], tracked=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    installer = CaskInstaller(config)
    with pytest.raises(CaskError, match="without pkgutil tracking"):
        installer.uninstall("synthetic-pkg")

    assert room.is_dir()
    assert not tools.log.exists()
