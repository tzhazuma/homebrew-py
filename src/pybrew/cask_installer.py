"""Cask discovery and installation."""

from __future__ import annotations

import glob
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .api import cask_from_api, fetch_cask
from .cask import Artifact, Cask, CaskError, parse_cask_file
from .download import Downloader, sha256_file
from .errors import ChecksumMismatch, DownloadError, PybrewError
from .installer import safe_extract_tar
from .paths import Config
from .taps import Tap


class CaskNotFound(PybrewError):
    """No cask with the requested token exists in the configured taps."""


class CaskLoader:
    def __init__(self, taps: Iterable[Tap], allow_api: bool = False):
        self.taps = list(taps)
        self.allow_api = allow_api
        self._cache: dict[str, tuple[Tap, Path]] = {}

    def names(self) -> list[str]:
        names: set[str] = set()
        for tap in self.taps:
            cask_dir = tap.path / "Casks"
            if cask_dir.is_dir():
                for path in cask_dir.rglob("*.rb"):
                    names.add(path.stem)
        return sorted(names)

    def _locate(self, token: str) -> tuple[Tap, Path]:
        if token in self._cache:
            return self._cache[token]
        for tap in self.taps:
            cask_dir = tap.path / "Casks"
            if not cask_dir.is_dir():
                continue
            direct = cask_dir / f"{token}.rb"
            if direct.is_file():
                self._cache[token] = (tap, direct)
                return tap, direct
            for path in sorted(cask_dir.rglob(f"{token}.rb")):
                self._cache[token] = (tap, path)
                return tap, path
        raise CaskNotFound(
            f"no cask named {token!r} in taps: {[tap.name for tap in self.taps]}"
        )

    def exists(self, token: str) -> bool:
        try:
            self._locate(token)
            return True
        except CaskNotFound:
            return False

    def load(self, token: str) -> Cask:
        try:
            tap, path = self._locate(token)
        except CaskNotFound:
            if self.allow_api:
                return fetch_cask(token)
            raise
        return parse_cask_file(path, tap=tap.name)

    def search(self, pattern: str) -> list[Cask]:
        regex = re.compile(pattern, re.IGNORECASE)
        results: list[Cask] = []
        for token in self.names():
            try:
                cask = self.load(token)
            except Exception:
                continue
            if regex.search(f"{cask.token} {cask.name or ''} {cask.desc or ''}"):
                results.append(cask)
        return results


@dataclass
class CaskInstallResult:
    token: str
    version: str
    room: Path
    artifacts: list[str]
    skipped: bool = False


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            name = info.filename
            parts = Path(name).parts
            if not name or name.startswith("/") or ".." in parts:
                raise CaskError(f"refusing zip member with unsafe path: {name!r}")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                link_target = bundle.read(info).decode("utf-8", errors="replace")
                if link_target.startswith("/") or ".." in Path(link_target).parts:
                    raise CaskError(f"refusing escaping symlink in zip: {name!r}")
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(link_target)
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            with bundle.open(info) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
            permissions = (info.external_attr >> 16) & 0o777
            if permissions:
                os.chmod(target, permissions)


def _archive_kind(path: Path) -> str:
    suffix = path.name.lower()
    if suffix.endswith(".pkg"):
        return "pkg"
    if suffix.endswith(".dmg"):
        return "dmg"
    if suffix.endswith(".zip"):
        return "zip"
    if suffix.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")):
        return "tar"
    try:
        with open(path, "rb") as handle:
            magic = handle.read(8)
    except OSError:
        return "unknown"
    if magic.startswith(b"PK\x03\x04"):
        return "zip"
    if magic.startswith(b"\x1f\x8b") or magic.startswith(b"BZh") or magic.startswith(b"\xfd7zXZ"):
        return "tar"
    if b"koly" in path.read_bytes()[-512:]:
        return "dmg"
    return "unknown"


class _MountedImage:
    def __init__(self, archive: Path, mountpoint: Path):
        self.archive = archive
        self.mountpoint = mountpoint
        self.mounted = False

    def __enter__(self) -> Path:
        self.mountpoint.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "hdiutil",
                "attach",
                "-nobrowse",
                "-readonly",
                "-mountpoint",
                str(self.mountpoint),
                str(self.archive),
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise CaskError(f"could not mount dmg {self.archive}: {detail}")
        self.mounted = True
        return self.mountpoint

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.mounted:
            subprocess.run(
                ["hdiutil", "detach", str(self.mountpoint)],
                text=True,
                capture_output=True,
            )


class CaskInstaller:
    def __init__(
        self,
        config: Config,
        downloader: Optional[Downloader] = None,
        appdir: Optional[Path] = None,
        pkg_root: Optional[Path] = None,
    ):
        self.config = config
        self.downloader = downloader or Downloader(config.downloads, config.bottle_dirs)
        self.appdir = Path(appdir).expanduser() if appdir else config.applications
        self.pkg_root = Path(pkg_root).expanduser() if pkg_root else (config.prefix / "pkgroot")

    # -- paths -----------------------------------------------------------
    def room(self, cask: Cask, version: Optional[str] = None) -> Path:
        return self.config.caskroom / cask.token / (version or cask.resolved_version())

    def installed_versions(self, token: str) -> list[str]:
        rack = self.config.caskroom / token
        if not rack.is_dir():
            return []
        return sorted(path.name for path in rack.iterdir() if path.is_dir())

    def list_installed(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.config.caskroom.is_dir():
            return rows
        for rack in sorted(self.config.caskroom.iterdir()):
            if not rack.is_dir():
                continue
            for version in sorted(path.name for path in rack.iterdir() if path.is_dir()):
                rows.append({"token": rack.name, "version": version, "room": str(rack / version)})
        return rows

    # -- install ---------------------------------------------------------
    def install(self, cask: Cask, force: bool = False) -> CaskInstallResult:
        version = cask.resolved_version()
        if version in ("latest", "0"):
            raise CaskError(f"{cask.token}: version {version!r} cannot be installed reliably")
        room = self.room(cask, version)
        if room.is_dir() and not force:
            return CaskInstallResult(cask.token, version, room, [], skipped=True)
        if room.is_dir():
            self._remove_room(room)

        cask.appdir = str(self.appdir)
        url = cask.resolved_url()
        digest = cask.resolved_sha256()
        if not digest:
            raise CaskError(f"{cask.token}: no sha256 for {cask.resolved_arch()}")
        hint = f"{cask.token}-{version}.{Path(url.split('?', 1)[0]).suffix or 'download'}"
        archive = self.downloader.fetch(url, digest, hint=hint)

        kind = _archive_kind(archive)
        room.parent.mkdir(parents=True, exist_ok=True)
        room.mkdir(parents=True, exist_ok=True)
        metadata = room / ".metadata"
        metadata.mkdir(exist_ok=True)
        installed: list[str] = []

        with tempfile.TemporaryDirectory(prefix="pybrew-cask-") as temporary:
            temp_root = Path(temporary)
            if kind == "dmg":
                if not os.path.exists("/usr/bin/hdiutil"):
                    raise CaskError("dmg casks require hdiutil (macOS)")
                with _MountedImage(archive, temp_root / "mount") as source_root:
                    installed = self._install_artifacts(cask, source_root, room, force)
            elif kind == "zip":
                _safe_extract_zip(archive, temp_root)
                installed = self._install_artifacts(cask, temp_root, room, force)
            elif kind == "tar":
                safe_extract_tar(archive, temp_root)
                installed = self._install_artifacts(cask, temp_root, room, force)
            elif kind == "pkg":
                installed = self._install_artifacts(cask, archive.parent, room, force, archive=archive)
            else:
                shutil.rmtree(room, ignore_errors=True)
                raise CaskError(f"{cask.token}: unsupported cask archive type for {url}")

        receipt = {
            "token": cask.token,
            "version": version,
            "url": url,
            "sha256": digest,
            "artifacts": [
                {"kind": artifact.kind, "source": artifact.source, "target": artifact.target}
                for artifact in cask.resolved_artifacts()
            ],
            "installed": installed,
            "tap": cask.tap,
        }
        (metadata / "receipt.json").write_text(
            __import__("json").dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return CaskInstallResult(cask.token, version, room, installed)

    def _install_artifacts(
        self,
        cask: Cask,
        source_root: Path,
        room: Path,
        force: bool,
        archive: Optional[Path] = None,
    ) -> list[str]:
        installed: list[str] = []
        artifacts = cask.resolved_artifacts()
        appdir = self.appdir
        appdir.mkdir(parents=True, exist_ok=True)
        # Apps first so binary artifacts inside them can resolve.
        ordered = sorted(artifacts, key=lambda artifact: 0 if artifact.kind == "app" else 1)
        for artifact in ordered:
            if artifact.kind == "app":
                source = self._find(source_root, artifact.source)
                if source is None:
                    raise CaskError(f"{cask.token}: app {artifact.source!r} not found in archive")
                destination = appdir / (artifact.target or source.name)
                if destination.exists():
                    if not force:
                        raise CaskError(f"{cask.token}: {destination} already exists")
                    self._remove_path(destination)
                shutil.copytree(source, destination, symlinks=True)
                installed.append(str(destination))
            elif artifact.kind == "binary":
                source_path = Path(artifact.source)
                if not source_path.is_absolute():
                    # Prefer the installed app location: the downloaded/extracted
                    # staging directory is deleted after installation.
                    app_candidate = appdir / artifact.source
                    source_path = (
                        app_candidate if app_candidate.exists() else source_root / artifact.source
                    )
                if not source_path.exists():
                    # Binary paths often point into an installed app.
                    candidate = Path(artifact.source)
                    if candidate.exists():
                        source_path = candidate
                    else:
                        found = self._find(source_root, Path(artifact.source).name)
                        if found is None:
                            raise CaskError(
                                f"{cask.token}: binary {artifact.source!r} not found"
                            )
                        source_path = found
                target = self.config.bin / (artifact.target or source_path.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    if not force:
                        raise CaskError(f"{cask.token}: {target} already exists")
                    target.unlink()
                target.symlink_to(source_path)
                installed.append(str(target))
            elif artifact.kind in ("pkg", "installer"):
                self._install_pkg(cask, artifact, source_root, room, force, installed, archive)

            elif artifact.kind == "suite":
                source = self._find(source_root, artifact.source)
                if source is None:
                    raise CaskError(f"{cask.token}: suite {artifact.source!r} not found")
                for app_source in source.rglob("*.app"):
                    destination = appdir / app_source.name
                    if destination.exists() and force:
                        self._remove_path(destination)
                    if not destination.exists():
                        shutil.copytree(app_source, destination, symlinks=True)
                        installed.append(str(destination))
            else:
                raise CaskError(f"{cask.token}: unsupported artifact {artifact.kind}")
        return installed

    def _install_pkg(
        self,
        cask: Cask,
        artifact: Artifact,
        source_root: Path,
        room: Path,
        force: bool,
        installed: list[str],
        archive: Optional[Path],
    ) -> None:
        if sys.platform != "darwin" or shutil.which("pkgutil") is None:
            raise CaskError("pkg artifacts require macOS pkgutil")
        package: Optional[Path] = None
        if artifact.source:
            package = self._find(source_root, artifact.source)
        if package is None and archive is not None:
            package = archive
        if package is None:
            raise CaskError(f"{cask.token}: pkg {artifact.source!r} not found")
        expanded = room / ".pkg-expanded"
        if expanded.exists():
            shutil.rmtree(expanded)
        result = subprocess.run(
            ["pkgutil", "--expand-full", str(package), str(expanded)],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            shutil.rmtree(expanded, ignore_errors=True)
            raise CaskError(
                f"{cask.token}: could not expand package {package}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        payload = expanded / "Payload"
        if not payload.is_dir():
            candidates = sorted(expanded.glob("**/Payload"))
            if not candidates:
                raise CaskError(f"{cask.token}: package has no Payload directory")
            payload = candidates[0]
        self.pkg_root.mkdir(parents=True, exist_ok=True)
        # The package payload is rooted at its install location; copy it under
        # the configured pkg-root rather than writing to the system volume.
        shutil.copytree(payload, self.pkg_root, dirs_exist_ok=True, symlinks=True)
        for path in payload.rglob("*"):
            if path.is_file() or path.is_symlink():
                relative = path.relative_to(payload)
                target = self.pkg_root / relative
                installed.append(str(target))
        shutil.rmtree(expanded, ignore_errors=True)

    @staticmethod
    def _find(root: Path, name: str) -> Optional[Path]:
        direct = root / name
        if direct.exists():
            return direct
        matches = sorted(root.rglob(name))
        return matches[0] if matches else None

    def _remove_room(self, room: Path) -> None:
        receipt = room / ".metadata" / "receipt.json"
        if receipt.is_file():
            try:
                data = __import__("json").loads(receipt.read_text())
                for path_str in data.get("installed", []):
                    self._remove_path(Path(path_str))
            except Exception:
                pass
        self._remove_path(room)

    @staticmethod
    def _remove_path(path: Path) -> None:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise CaskError(f"could not remove {path}: {error}") from error

    # -- outdated / upgrade ---------------------------------------------
    def find_outdated(self, loader: CaskLoader) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for installed in self.list_installed():
            token = installed["token"]
            try:
                cask = loader.load(token)
            except Exception:
                continue
            current = cask.resolved_version()
            versions = self.installed_versions(token)
            latest = versions[-1] if versions else installed["version"]
            if current and current != latest:
                rows.append({"token": token, "installed": latest, "current": current})
        return rows

    def upgrade(self, cask: Cask) -> CaskInstallResult:
        """Install the current cask version and remove superseded room metadata."""

        old_versions = self.installed_versions(cask.token)
        result = self.install(cask, force=True)
        if result.skipped:
            return result
        new_receipt = result.room / ".metadata" / "receipt.json"
        new_paths: set[str] = set()
        if new_receipt.is_file():
            try:
                data = __import__("json").loads(new_receipt.read_text())
                new_paths = {str(path) for path in data.get("installed", [])}
            except Exception:
                new_paths = set()
        for version in old_versions:
            if version == result.version:
                continue
            old_room = self.config.caskroom / cask.token / version
            old_receipt = old_room / ".metadata" / "receipt.json"
            if old_receipt.is_file():
                try:
                    data = __import__("json").loads(old_receipt.read_text())
                    for path_str in data.get("installed", []):
                        if str(path_str) in new_paths:
                            continue
                        path = Path(path_str)
                        if path.exists() or path.is_symlink():
                            self._remove_path(path)
                except Exception:
                    pass
            self._remove_path(old_room)
        return result

    # -- uninstall -------------------------------------------------------
    def _run_uninstall_actions(self, cask: Cask) -> None:
        """Execute the safe subset of cask uninstall actions.

        ``quit`` apps are asked to quit via osascript; ``delete`` paths are
        removed after checking they are inside the user home/prefix/temp roots.
        Unsupported actions (launchctl, pkgutil, scripts, login items) are
        deliberately not executed.
        """

        if not cask.uninstall:
            return
        quit_value = cask.uninstall.get("quit")
        if quit_value and sys.platform == "darwin":
            apps = quit_value if isinstance(quit_value, list) else [quit_value]
            osascript = os.environ.get("PYBREW_OSASCRIPT", "/usr/bin/osascript")
            for app in apps:
                try:
                    subprocess.run(
                        [osascript, "-e", f'tell application id "{app}" to quit'],
                        text=True,
                        capture_output=True,
                        timeout=15,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
        delete_value = cask.uninstall.get("delete")
        if delete_value:
            paths = delete_value if isinstance(delete_value, list) else [delete_value]
            for pattern in paths:
                for match in glob.glob(os.path.expanduser(str(pattern))):
                    self._remove_user_path(Path(match))

    def _remove_user_path(self, path: Path) -> None:
        resolved = path.resolve()
        home = Path.home().resolve()
        prefix = self.config.prefix.resolve()
        temp = Path(tempfile.gettempdir()).resolve()
        if resolved in (Path("/"), home, prefix):
            raise CaskError(f"refusing to delete unsafe path: {path}")
        if not (
            home in resolved.parents
            or prefix in resolved.parents
            or temp in resolved.parents
        ):
            raise CaskError(f"refusing to delete path outside allowed roots: {path}")
        self._remove_path(path)

    def uninstall(
        self,
        token: str,
        version: Optional[str] = None,
        zap: bool = False,
        cask: Optional[Cask] = None,
    ) -> list[Path]:
        if cask is not None:
            self._run_uninstall_actions(cask)
        rack = self.config.caskroom / token
        if not rack.is_dir():
            raise CaskError(f"cask {token} is not installed")
        versions = [version] if version else sorted(path.name for path in rack.iterdir() if path.is_dir())
        removed: list[Path] = []
        for item in versions:
            room = rack / item
            if not room.is_dir():
                continue
            receipt = room / ".metadata" / "receipt.json"
            installed: list[str] = []
            if receipt.is_file():
                try:
                    installed = __import__("json").loads(receipt.read_text()).get("installed", [])
                except Exception:
                    installed = []
            for path_str in installed:
                path = Path(path_str)
                if path.exists() or path.is_symlink():
                    self._remove_path(path)
            self._remove_path(room)
            removed.append(room)
        try:
            if not any(rack.iterdir()):
                rack.rmdir()
        except OSError:
            pass
        if zap and cask is not None:
            self.zap(cask)
        return removed

    def zap(self, cask: Cask) -> list[Path]:
        removed: list[Path] = []
        for pattern in cask.zap:
            expanded = os.path.expanduser(pattern)
            for match in glob.glob(expanded):
                path = Path(match)
                resolved = path.resolve()
                home = Path.home().resolve()
                prefix = self.config.prefix.resolve()
                if resolved in (Path("/"), home, prefix):
                    raise CaskError(f"refusing to zap unsafe path: {path}")
                if not (
                    home in resolved.parents
                    or prefix in resolved.parents
                    or str(resolved).startswith(str(Path(tempfile.gettempdir()).resolve()))
                ):
                    raise CaskError(f"refusing to zap path outside allowed roots: {path}")
                self._remove_path(path)
                removed.append(path)
        return removed
