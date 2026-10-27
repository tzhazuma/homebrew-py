"""Tap management: clone/update/remove third-party formula and cask taps."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .errors import PybrewError, UsageError
from .paths import Config
from .taps import Tap, discover_taps, tap_name_for_path


@dataclass
class TapResult:
    name: str
    path: Path
    action: str  # installed | updated | unchanged
    formulas: int = 0
    casks: int = 0


class TapManager:
    """Manage taps under ``<prefix>/Library/Taps/<user>/homebrew-<repo>``."""

    def __init__(self, config: Config):
        self.config = config
        self.root = config.taps_root

    # -- naming ----------------------------------------------------------
    @staticmethod
    def parse_name(name: str) -> tuple[str, str]:
        cleaned = name.strip().strip("/")
        if cleaned.startswith("homebrew/"):
            cleaned = "homebrew/" + cleaned.split("/", 1)[1]
        parts = cleaned.split("/")
        if len(parts) != 2 or not all(parts):
            raise UsageError(f"tap name must look like 'user/repo', got {name!r}")
        user, repo = parts
        if not all(ch.isalnum() or ch in "-_." for ch in user + repo):
            raise UsageError(f"invalid tap name: {name!r}")
        return user, repo

    def path_for(self, name: str) -> Path:
        user, repo = self.parse_name(name)
        return self.root / user / f"homebrew-{repo}"

    def tap_name_for(self, name: str) -> str:
        user, repo = self.parse_name(name)
        return f"{user}/{repo}"

    def default_url(self, name: str) -> str:
        user, repo = self.parse_name(name)
        return f"https://github.com/{user}/homebrew-{repo}.git"

    # -- listing ---------------------------------------------------------
    def list(self) -> list[Tap]:
        if not self.root.is_dir():
            return []
        paths: list[Path] = []
        for user_dir in sorted(self.root.iterdir()):
            if not user_dir.is_dir():
                continue
            for tap_dir in sorted(user_dir.iterdir()):
                if tap_dir.is_dir() and tap_dir.name.startswith("homebrew-"):
                    paths.append(tap_dir)
        return discover_taps(paths)

    def counts(self, tap: Tap) -> tuple[int, int]:
        formulas = (
            sum(1 for _ in (tap.path / "Formula").rglob("*.rb"))
            if (tap.path / "Formula").is_dir()
            else 0
        )
        casks = (
            sum(1 for _ in (tap.path / "Casks").rglob("*.rb"))
            if (tap.path / "Casks").is_dir()
            else 0
        )
        return formulas, casks

    # -- install/update --------------------------------------------------
    def tap(self, name: str, url: Optional[str] = None) -> TapResult:
        tap_name = self.tap_name_for(name)
        destination = self.path_for(name)
        source_url = url or self.default_url(name)
        action = "installed"

        if destination.exists():
            if not (destination / ".git").exists():
                raise PybrewError(f"tap path exists but is not a git checkout: {destination}")
            action = self._pull(destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._run(["git", "clone", "--depth", "1", source_url, str(destination)])

        if not ((destination / "Formula").is_dir() or (destination / "Casks").is_dir()):
            if destination.exists() and action == "installed":
                shutil.rmtree(destination, ignore_errors=True)
            raise PybrewError(
                f"{tap_name} does not look like a Homebrew tap (missing Formula/ or Casks/)"
            )

        tap = Tap(tap_name_for_path(destination), destination)
        formulas, casks = self.counts(tap)
        return TapResult(tap_name, destination, action, formulas, casks)

    def _pull(self, destination: Path) -> str:
        before = self._git_head(destination)
        self._run(["git", "-C", str(destination), "pull", "--ff-only"])
        after = self._git_head(destination)
        return "unchanged" if before == after else "updated"

    @staticmethod
    def _git_head(path: Path) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    def update_all(self) -> list[TapResult]:
        results: list[TapResult] = []
        for tap in self.list():
            results.append(self.tap(tap.name))
        return results

    def untap(self, name: str, force: bool = False) -> Path:
        destination = self.path_for(name)
        if not destination.exists():
            raise PybrewError(f"tap {self.tap_name_for(name)} is not installed")
        if destination.resolve().parent != self.root.resolve() and self.root.resolve() not in destination.resolve().parents:
            raise PybrewError(f"refusing to remove path outside tap root: {destination}")
        shutil.rmtree(destination)
        # Prune the (usually empty) user directory.
        try:
            if not any(destination.parent.iterdir()):
                destination.parent.rmdir()
        except OSError:
            pass
        return destination

    @staticmethod
    def _run(command: list[str], cwd: Optional[Path] = None) -> None:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                text=True,
                capture_output=True,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PybrewError(f"failed to run {command[0]}: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise PybrewError(
                f"command failed ({' '.join(command)}):\n{detail[-2000:]}"
            )
