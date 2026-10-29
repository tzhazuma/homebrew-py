"""Synthetic tests for system-wide pkg upgrade cleanup and rollback.

Every test uses throwaway executables, a temporary allowed root and a
monkeypatched effective UID: no real ``installer``, no real ``pkgutil``, no
root credentials and no system paths are touched.  ``pkgutil`` itself is faked
so the tests are deterministic on macOS and Linux alike.
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

# Fake pkgutil.  The package database is a newline-separated file so
# ``--forget`` has an observable effect and ``--pkgs`` can be made to fail on
# demand.  All invocations are appended to ``$FAKE_PKGUTIL_LOG``.
_FAKE_PKGUTIL = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_PKGUTIL_LOG"
case "$1" in
  --pkgs)
    if [ -n "$FAKE_PKGUTIL_FAIL_PKGS_AFTER" ] && [ -f "$FAKE_PKGUTIL_FAIL_PKGS_AFTER" ]; then
      echo "simulated pkgutil failure" >&2
      exit 1
    fi
    if [ -f "$FAKE_PKGUTIL_DB" ]; then
      cat "$FAKE_PKGUTIL_DB"
    fi
    ;;
  --pkg-info)
    printf 'package-id: %s\\nversion: 1.0\\nvolume: /\\nlocation: %s\\n' \
      "$2" "${FAKE_PKGUTIL_LOCATION:-/}"
    ;;
  --files)
    file="$FAKE_PKGUTIL_FILES/$2"
    if [ ! -f "$file" ]; then
      echo "no files for $2" >&2
      exit 1
    fi
    cat "$file"
    ;;
  --forget)
    if [ -f "$FAKE_PKGUTIL_DB" ]; then
      grep -v -x -F "$2" "$FAKE_PKGUTIL_DB" > "$FAKE_PKGUTIL_DB.tmp" 2>/dev/null || true
      mv "$FAKE_PKGUTIL_DB.tmp" "$FAKE_PKGUTIL_DB"
    fi
    ;;
esac
exit 0
"""

# Fake macOS installer.  It can replace the package database, write files from
# a ``path|content`` manifest and optionally fail after doing both.
_FAKE_INSTALLER = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_INSTALLER_LOG"
if [ -n "$FAKE_INSTALLER_AFTER_DB" ]; then
  cp "$FAKE_INSTALLER_AFTER_DB" "$FAKE_PKGUTIL_DB"
fi
if [ -n "$FAKE_INSTALLER_FILES" ] && [ -f "$FAKE_INSTALLER_FILES" ]; then
  while IFS='|' read -r file content; do
    [ -z "$file" ] && continue
    mkdir -p "$(dirname "$file")"
    printf '%s\\n' "$content" > "$file"
  done < "$FAKE_INSTALLER_FILES"
fi
if [ -n "$FAKE_INSTALLER_FAIL_PKGS" ]; then
  : > "$FAKE_INSTALLER_FAIL_PKGS"
fi
exit "${FAKE_INSTALLER_EXIT:-0}"
"""


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def _touch(path: Path, data: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


def _pretend_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")


@pytest.fixture
def pkg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    payload = b"synthetic package 2.0 contents\n"
    package = tmp_path / "Synthetic-2.0.pkg"
    package.write_bytes(payload)
    token = "synthetic-pkg"
    tap = tmp_path / "tap"
    cask_dir = tap / "Casks" / "s"
    cask_dir.mkdir(parents=True)
    (cask_dir / f"{token}.rb").write_text(
        f'''cask "{token}" do
  version "2.0"
  sha256 "{hashlib.sha256(payload).hexdigest()}"
  url "{package.as_uri()}"
  pkg "Synthetic-2.0.pkg"
end
''',
        encoding="utf-8",
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    files = tmp_path / "pkgutil-files"
    files.mkdir()
    database = tmp_path / "pkgdb.txt"
    database.write_text("")
    pkgutil_log = tmp_path / "pkgutil.log"
    installer_log = tmp_path / "installer.log"

    pkgutil = _write_executable(tmp_path / "fake-pkgutil", _FAKE_PKGUTIL)
    installer = _write_executable(tmp_path / "fake-installer", _FAKE_INSTALLER)
    monkeypatch.setenv("PYBREW_PKGUTIL", str(pkgutil))
    monkeypatch.setenv("PYBREW_INSTALLER", str(installer))
    monkeypatch.setenv("FAKE_PKGUTIL_LOG", str(pkgutil_log))
    monkeypatch.setenv("FAKE_PKGUTIL_FILES", str(files))
    monkeypatch.setenv("FAKE_PKGUTIL_DB", str(database))
    monkeypatch.setenv("FAKE_PKGUTIL_LOCATION", str(allowed))
    monkeypatch.setenv("FAKE_INSTALLER_LOG", str(installer_log))
    monkeypatch.setenv("PYBREW_SYSTEM_PKG_ROOTS", str(allowed))
    _pretend_macos(monkeypatch)

    return SimpleNamespace(
        tmp_path=tmp_path,
        config=config,
        tap=tap,
        loader=CaskLoader(discover_taps([tap])),
        package=package,
        payload=payload,
        allowed=allowed,
        files=files,
        database=database,
        pkgutil=pkgutil,
        installer=installer,
        pkgutil_log=pkgutil_log,
        installer_log=installer_log,
    )


def _write_receipt(
    config: Config,
    version: str,
    *,
    ids: list[str] | None,
    tracked: bool = True,
) -> Path:
    room = config.caskroom / "synthetic-pkg" / version
    metadata = room / ".metadata"
    metadata.mkdir(parents=True)
    receipt: dict[str, object] = {
        "token": "synthetic-pkg",
        "version": version,
        "installed": [],
        "system_pkg": True,
    }
    if ids is not None:
        receipt["system_pkg_ids"] = list(ids)
    receipt["system_pkg_tracked"] = tracked
    (metadata / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return room


def _set_database(env: SimpleNamespace, ids: list[str]) -> None:
    env.database.write_text("".join(f"{pkg_id}\n" for pkg_id in ids))


def _write_ids(path: Path, ids: list[str]) -> Path:
    path.write_text("".join(f"{pkg_id}\n" for pkg_id in ids))
    return path


def _set_pkg_files(env: SimpleNamespace, pkg_id: str, paths: list[str]) -> None:
    (env.files / pkg_id).write_text("".join(f"{path}\n" for path in paths))


def _install_manifest(env: SimpleNamespace, entries: dict[Path, str]) -> Path:
    path = env.tmp_path / "install-files.txt"
    path.write_text(
        "".join(f"{destination}|{content}\n" for destination, content in entries.items())
    )
    return path


def _prepare_successful_install(
    env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    new_ids: list[str],
    installed_files: dict[Path, str],
    before_ids: list[str] | None = None,
) -> None:
    if before_ids is None:
        before_ids = ["com.example.old"]
    monkeypatch.setenv(
        "FAKE_INSTALLER_AFTER_DB",
        str(_write_ids(env.tmp_path / "after-ids.txt", [*before_ids, *new_ids])),
    )
    monkeypatch.setenv(
        "FAKE_INSTALLER_FILES", str(_install_manifest(env, installed_files))
    )


def test_upgrade_removes_old_system_pkg_files_and_forgets_receipts(
    pkg_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    old_tool = _touch(pkg_env.allowed / "usr/local/bin/old-tool")
    new_tool = pkg_env.allowed / "usr/local/bin/new-tool"
    sentinel = _touch(pkg_env.tmp_path / "pre-existing-sentinel")
    _write_receipt(pkg_env.config, "1.0", ids=["com.example.old"])
    _set_database(pkg_env, ["com.example.old"])
    _prepare_successful_install(
        pkg_env,
        monkeypatch,
        new_ids=["com.example.new"],
        installed_files={new_tool: "new"},
    )
    _set_pkg_files(
        pkg_env, "com.example.old", ["usr/local/bin/old-tool", "usr/local/bin"]
    )
    _set_pkg_files(
        pkg_env, "com.example.new", ["usr/local/bin/new-tool", "usr/local/bin"]
    )

    installer = CaskInstaller(pkg_env.config, system_pkg=True)
    result = installer.upgrade(pkg_env.loader.load("synthetic-pkg"))

    assert result.version == "2.0"
    assert not old_tool.exists()
    assert new_tool.read_text() == "new\n"
    assert sentinel.read_text() == "x\n"
    old_room = pkg_env.config.caskroom / "synthetic-pkg" / "1.0"
    assert not old_room.exists()
    receipt = json.loads((result.room / ".metadata" / "receipt.json").read_text())
    assert receipt["system_pkg"] is True
    assert receipt["system_pkg_ids"] == ["com.example.new"]
    log = pkg_env.pkgutil_log.read_text()
    assert "--forget com.example.old" in log
    assert "--forget com.example.new" not in log
    installer_log = pkg_env.installer_log.read_text()
    assert installer_log.startswith("-pkg ")
    assert "-target /" in installer_log
    assert ".pkg" in installer_log


def test_upgrade_skips_old_package_id_shared_with_new_receipt(
    pkg_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    shared_tool = _touch(pkg_env.allowed / "usr/local/bin/shared-tool")
    _write_receipt(pkg_env.config, "1.0", ids=["com.example.shared"])
    # The old receipt is not visible in --pkgs, so the new install's diff
    # reports the shared ID as newly installed.
    _set_database(pkg_env, [])
    _prepare_successful_install(
        pkg_env,
        monkeypatch,
        new_ids=["com.example.shared"],
        installed_files={shared_tool: "new"},
        before_ids=[],
    )
    _set_pkg_files(
        pkg_env, "com.example.shared", ["usr/local/bin/shared-tool"]
    )

    installer = CaskInstaller(pkg_env.config, system_pkg=True)
    result = installer.upgrade(pkg_env.loader.load("synthetic-pkg"))

    assert shared_tool.exists()
    assert not (pkg_env.config.caskroom / "synthetic-pkg" / "1.0").exists()
    receipt = json.loads((result.room / ".metadata" / "receipt.json").read_text())
    assert receipt["system_pkg_ids"] == ["com.example.shared"]
    assert "--forget com.example.shared" not in pkg_env.pkgutil_log.read_text()


def test_upgrade_keeps_old_files_owned_by_new_system_package(
    pkg_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    shared_tool = _touch(pkg_env.allowed / "usr/local/bin/shared-tool", "old\n")
    old_only = _touch(pkg_env.allowed / "usr/local/bin/old-only")
    new_tool = pkg_env.allowed / "usr/local/bin/new-tool"
    _write_receipt(pkg_env.config, "1.0", ids=["com.example.old"])
    _set_database(pkg_env, ["com.example.old"])
    _prepare_successful_install(
        pkg_env,
        monkeypatch,
        new_ids=["com.example.new"],
        installed_files={shared_tool: "new", new_tool: "new"},
    )
    _set_pkg_files(
        pkg_env,
        "com.example.old",
        ["usr/local/bin/shared-tool", "usr/local/bin/old-only", "usr/local/bin"],
    )
    _set_pkg_files(
        pkg_env,
        "com.example.new",
        ["usr/local/bin/shared-tool", "usr/local/bin/new-tool", "usr/local/bin"],
    )

    installer = CaskInstaller(pkg_env.config, system_pkg=True)
    installer.upgrade(pkg_env.loader.load("synthetic-pkg"))

    # The new package overwrote the shared path; only the old-only file goes.
    assert shared_tool.read_text() == "new\n"
    assert not old_only.exists()
    assert new_tool.exists()
    log = pkg_env.pkgutil_log.read_text()
    assert "--forget com.example.old" in log
    assert "--forget com.example.new" not in log


def test_upgrade_system_pkg_requires_root_before_install(
    pkg_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    old_tool = _touch(pkg_env.allowed / "usr/local/bin/old-tool")
    old_room = _write_receipt(pkg_env.config, "1.0", ids=["com.example.old"])
    _set_database(pkg_env, ["com.example.old"])
    _set_pkg_files(
        pkg_env, "com.example.old", ["usr/local/bin/old-tool", "usr/local/bin"]
    )

    installer = CaskInstaller(pkg_env.config, system_pkg=True)
    with pytest.raises(CaskError, match="root"):
        installer.upgrade(pkg_env.loader.load("synthetic-pkg"))

    assert old_tool.exists()
    assert old_room.is_dir()
    assert not pkg_env.installer_log.exists()


def test_failed_upgrade_rolls_back_new_system_pkg_files(
    pkg_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    old_tool = _touch(pkg_env.allowed / "usr/local/bin/old-tool")
    new_tool = pkg_env.allowed / "usr/local/bin/new-tool"
    sentinel = _touch(pkg_env.tmp_path / "pre-existing-sentinel")
    old_room = _write_receipt(pkg_env.config, "1.0", ids=["com.example.old"])
    _set_database(pkg_env, ["com.example.old"])
    _prepare_successful_install(
        pkg_env,
        monkeypatch,
        new_ids=["com.example.new"],
        installed_files={new_tool: "new"},
    )
    monkeypatch.setenv("FAKE_INSTALLER_EXIT", "1")
    _set_pkg_files(
        pkg_env, "com.example.old", ["usr/local/bin/old-tool", "usr/local/bin"]
    )
    _set_pkg_files(
        pkg_env, "com.example.new", ["usr/local/bin/new-tool", "usr/local/bin"]
    )

    installer = CaskInstaller(pkg_env.config, system_pkg=True)
    with pytest.raises(CaskError) as excinfo:
        installer.upgrade(pkg_env.loader.load("synthetic-pkg"))

    message = str(excinfo.value)
    assert "rollback" in message.lower()
    assert "com.example.new" in message
    # Only the package installed by the failed attempt is rolled back.
    assert not new_tool.exists()
    assert old_tool.exists()
    assert sentinel.read_text() == "x\n"
    assert old_room.is_dir()
    log = pkg_env.pkgutil_log.read_text()
    assert "--forget com.example.new" in log
    assert "--forget com.example.old" not in log
    assert pkg_env.database.read_text() == "com.example.old\n"


def test_failed_upgrade_reports_rollback_when_pkgutil_after_snapshot_fails(
    pkg_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    old_tool = _touch(pkg_env.allowed / "usr/local/bin/old-tool")
    new_tool = pkg_env.allowed / "usr/local/bin/new-tool"
    old_room = _write_receipt(pkg_env.config, "1.0", ids=["com.example.old"])
    _set_database(pkg_env, ["com.example.old"])
    _prepare_successful_install(
        pkg_env,
        monkeypatch,
        new_ids=["com.example.new"],
        installed_files={new_tool: "new"},
    )
    fail_marker = pkg_env.tmp_path / "fail-pkgs-after"
    monkeypatch.setenv("FAKE_PKGUTIL_FAIL_PKGS_AFTER", str(fail_marker))
    monkeypatch.setenv("FAKE_INSTALLER_FAIL_PKGS", str(fail_marker))
    monkeypatch.setenv("FAKE_INSTALLER_EXIT", "1")
    _set_pkg_files(
        pkg_env, "com.example.old", ["usr/local/bin/old-tool", "usr/local/bin"]
    )
    _set_pkg_files(
        pkg_env, "com.example.new", ["usr/local/bin/new-tool", "usr/local/bin"]
    )

    installer = CaskInstaller(pkg_env.config, system_pkg=True)
    with pytest.raises(CaskError) as excinfo:
        installer.upgrade(pkg_env.loader.load("synthetic-pkg"))

    message = str(excinfo.value)
    assert "rollback" in message.lower()
    assert "no system files were removed" in message
    # Without a usable after-snapshot pybrew refuses to guess: nothing is
    # removed, including files written by the failed installer.
    assert new_tool.exists()
    assert old_tool.exists()
    assert old_room.is_dir()


def test_upgrade_refuses_old_system_receipt_without_package_ids(
    pkg_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    old_room = _write_receipt(pkg_env.config, "1.0", ids=None)
    _set_database(pkg_env, ["com.example.old"])

    installer = CaskInstaller(pkg_env.config, system_pkg=True)
    with pytest.raises(CaskError, match="no recorded package IDs"):
        installer.upgrade(pkg_env.loader.load("synthetic-pkg"))

    assert old_room.is_dir()
    assert not pkg_env.installer_log.exists()


def test_upgrade_refuses_untracked_old_system_receipt(
    pkg_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    old_room = _write_receipt(pkg_env.config, "1.0", ids=[], tracked=False)

    installer = CaskInstaller(pkg_env.config, system_pkg=True)
    with pytest.raises(CaskError, match="without pkgutil tracking"):
        installer.upgrade(pkg_env.loader.load("synthetic-pkg"))

    assert old_room.is_dir()
    assert not pkg_env.installer_log.exists()


def test_postflight_failure_rolls_back_successfully_installed_system_pkg(
    pkg_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    # Add a captured, failing postflight block to the cask.
    payload_digest = hashlib.sha256(pkg_env.payload).hexdigest()
    cask_file = pkg_env.tap / "Casks" / "s" / "synthetic-pkg.rb"
    cask_file.write_text(
        f'''cask "synthetic-pkg" do
  version "2.0"
  sha256 "{payload_digest}"
  url "{pkg_env.package.as_uri()}"
  pkg "Synthetic-2.0.pkg"

  postflight do
    puts "postflight"
  end
end
''',
        encoding="utf-8",
    )
    failing_ruby = _write_executable(
        pkg_env.tmp_path / "failing-ruby", "#!/bin/sh\nexit 1\n"
    )
    monkeypatch.setenv("PYBREW_RUBY", str(failing_ruby))
    old_tool = _touch(pkg_env.allowed / "usr/local/bin/old-tool")
    new_tool = pkg_env.allowed / "usr/local/bin/new-tool"
    old_room = _write_receipt(pkg_env.config, "1.0", ids=["com.example.old"])
    _set_database(pkg_env, ["com.example.old"])
    _prepare_successful_install(
        pkg_env,
        monkeypatch,
        new_ids=["com.example.new"],
        installed_files={new_tool: "new"},
    )
    _set_pkg_files(
        pkg_env, "com.example.old", ["usr/local/bin/old-tool", "usr/local/bin"]
    )
    _set_pkg_files(
        pkg_env, "com.example.new", ["usr/local/bin/new-tool", "usr/local/bin"]
    )

    installer = CaskInstaller(pkg_env.config, system_pkg=True, run_scripts=True)
    with pytest.raises(CaskError) as excinfo:
        installer.upgrade(pkg_env.loader.load("synthetic-pkg"))

    message = str(excinfo.value)
    assert "postflight" in message
    assert "rollback" in message.lower()
    assert not new_tool.exists()
    assert old_tool.exists()
    assert old_room.is_dir()
    assert pkg_env.database.read_text() == "com.example.old\n"
    # The new caskroom entry never got a receipt because the install failed.
    new_room = pkg_env.config.caskroom / "synthetic-pkg" / "2.0"
    assert not (new_room / ".metadata" / "receipt.json").exists()


def test_failed_upgrade_rollback_never_removes_pre_existing_paths(
    pkg_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    old_tool = _touch(pkg_env.allowed / "usr/local/bin/old-tool", "old\n")
    new_tool = pkg_env.allowed / "usr/local/bin/new-tool"
    old_room = _write_receipt(pkg_env.config, "1.0", ids=["com.example.old"])
    _set_database(pkg_env, ["com.example.old"])
    # The failed package both overwrites the old path and adds a new one.
    _prepare_successful_install(
        pkg_env,
        monkeypatch,
        new_ids=["com.example.new"],
        installed_files={old_tool: "new", new_tool: "new"},
    )
    monkeypatch.setenv("FAKE_INSTALLER_EXIT", "1")
    _set_pkg_files(
        pkg_env, "com.example.old", ["usr/local/bin/old-tool", "usr/local/bin"]
    )
    _set_pkg_files(
        pkg_env,
        "com.example.new",
        ["usr/local/bin/old-tool", "usr/local/bin/new-tool", "usr/local/bin"],
    )

    installer = CaskInstaller(pkg_env.config, system_pkg=True)
    with pytest.raises(CaskError) as excinfo:
        installer.upgrade(pkg_env.loader.load("synthetic-pkg"))

    assert "rollback" in str(excinfo.value).lower()
    # The old path predates the attempt: it is protected even though the new
    # package receipt also lists it.  The genuinely new file is rolled back.
    assert old_tool.exists()
    assert not new_tool.exists()
    assert old_room.is_dir()
    assert pkg_env.database.read_text() == "com.example.old\n"


def test_upgrade_carries_forward_reinstalled_same_package_id(
    pkg_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    tool = _touch(pkg_env.allowed / "usr/local/bin/same-tool", "old\n")
    _write_receipt(pkg_env.config, "1.0", ids=["com.example.same"])
    _set_database(pkg_env, ["com.example.same"])
    # A same-ID upgrade adds no new receipts but replaces the files on disk.
    _prepare_successful_install(
        pkg_env,
        monkeypatch,
        new_ids=[],
        installed_files={tool: "new"},
        before_ids=["com.example.same"],
    )
    _set_pkg_files(
        pkg_env, "com.example.same", ["usr/local/bin/same-tool", "usr/local/bin"]
    )

    installer = CaskInstaller(pkg_env.config, system_pkg=True)
    result = installer.upgrade(pkg_env.loader.load("synthetic-pkg"))

    assert tool.read_text() == "new\n"
    assert not (pkg_env.config.caskroom / "synthetic-pkg" / "1.0").exists()
    receipt = json.loads((result.room / ".metadata" / "receipt.json").read_text())
    # The ID is carried onto the new receipt so uninstall can still clean the
    # files that the upgrade just installed.
    assert receipt["system_pkg_ids"] == ["com.example.same"]
    assert "--forget com.example.same" not in pkg_env.pkgutil_log.read_text()
