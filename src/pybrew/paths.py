"""Prefix/cache/tap configuration and filesystem layout helpers."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .errors import UsageError

# Directories a keg contributes to the prefix (the same top-level directories
# Homebrew links).
LINK_DIRS = ("bin", "sbin", "lib", "include", "share", "etc", "libexec", "Frameworks")


def _default_prefix() -> Path:
    env = os.environ.get("PYBREW_PREFIX")
    if env:
        return Path(env).expanduser()
    # Deliberately a private prefix: pybrew is a prototype and must never
    # silently modify an existing Homebrew installation.  Pass --prefix or set
    # PYBREW_PREFIX to operate on a Homebrew-shaped tree.
    return Path.home() / ".pybrew"


def _default_cache() -> Path:
    env = os.environ.get("PYBREW_CACHE")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "pybrew"
    return Path.home() / ".cache" / "pybrew"


def _split_paths(value: str) -> list[Path]:
    return [Path(part).expanduser() for part in value.split(os.pathsep) if part.strip()]


def _default_taps(prefix: Path) -> list[Path]:
    env = os.environ.get("PYBREW_TAP")
    taps: list[Path] = []
    if env:
        taps.extend(_split_paths(env))
    library = prefix / "Library" / "Taps"
    if library.is_dir():
        # A tap directory is a directory that contains Formula/ or has a
        # homebrew-* repository shape.
        for child in sorted(library.iterdir()):
            if child.is_dir():
                taps.append(child)
                user_taps = child / "Library" / "Taps"
                if user_taps.is_dir():
                    taps.extend(sorted(p for p in user_taps.iterdir() if p.is_dir()))
    return list(dict.fromkeys(taps))


def _default_bottle_dirs() -> list[Path]:
    env = os.environ.get("PYBREW_BOTTLE_DIRS")
    dirs = _split_paths(env) if env else []
    cache = Path.home() / "Library" / "Caches" / "Homebrew" / "downloads"
    if cache.is_dir():
        dirs.append(cache)
    return list(dict.fromkeys(dirs))


@dataclass
class Config:
    prefix: Path
    cache: Path
    taps: list[Path] = field(default_factory=list)
    bottle_dirs: list[Path] = field(default_factory=list)
    bottle_tag: Optional[str] = None
    verbose: bool = False
    dry_run: bool = False

    # -- construction ----------------------------------------------------
    @classmethod
    def default(
        cls,
        prefix: Optional[str | Path] = None,
        taps: Optional[list[str | Path]] = None,
        cache: Optional[str | Path] = None,
        bottle_tag: Optional[str] = None,
    ) -> "Config":
        prefix_path = Path(prefix).expanduser() if prefix else _default_prefix()
        cache_path = Path(cache).expanduser() if cache else _default_cache()
        tap_paths = (
            [Path(t).expanduser() for t in taps] if taps is not None else _default_taps(prefix_path)
        )
        return cls(
            prefix=prefix_path,
            cache=cache_path,
            taps=tap_paths,
            bottle_dirs=_default_bottle_dirs(),
            bottle_tag=bottle_tag,
        )

    # -- layout ----------------------------------------------------------
    @property
    def cellar(self) -> Path:
        return self.prefix / "Cellar"

    @property
    def bin(self) -> Path:
        return self.prefix / "bin"

    @property
    def sbin(self) -> Path:
        return self.prefix / "sbin"

    @property
    def lib(self) -> Path:
        return self.prefix / "lib"

    @property
    def include(self) -> Path:
        return self.prefix / "include"

    @property
    def share(self) -> Path:
        return self.prefix / "share"

    @property
    def etc(self) -> Path:
        return self.prefix / "etc"

    @property
    def opt(self) -> Path:
        return self.prefix / "opt"

    @property
    def taps_root(self) -> Path:
        return self.prefix / "Library" / "Taps"

    @property
    def caskroom(self) -> Path:
        return self.prefix / "Caskroom"

    @property
    def applications(self) -> Path:
        env = os.environ.get("PYBREW_APPDIR")
        if env:
            return Path(env).expanduser()
        return self.prefix / "Applications"

    @property
    def service_dir(self) -> Path:
        import sys

        env = os.environ.get("PYBREW_SERVICE_DIR")
        if env:
            return Path(env).expanduser()
        backend = os.environ.get("PYBREW_SERVICE_BACKEND")
        if backend == "systemd":
            return Path.home() / ".config" / "systemd" / "user"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "LaunchAgents"
        return Path.home() / ".config" / "systemd" / "user"

    @property
    def var(self) -> Path:
        return self.prefix / "var"

    @property
    def lock_dir(self) -> Path:
        return self.var / "pybrew" / "locks"

    @property
    def downloads(self) -> Path:
        return self.cache / "downloads"

    def keg_path(self, formula_name: str, pkg_version: str) -> Path:
        return self.cellar / formula_name / pkg_version

    def rack_path(self, formula_name: str) -> Path:
        return self.cellar / formula_name

    def opt_path(self, formula_name: str) -> Path:
        return self.opt / formula_name

    def ensure(self) -> None:
        for path in (
            self.prefix,
            self.cellar,
            self.bin,
            self.sbin,
            self.lib,
            self.include,
            self.share,
            self.etc,
            self.opt,
            self.var,
            self.lock_dir,
            self.downloads,
            self.taps_root,
            self.caskroom,
            self.applications,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # -- validation ------------------------------------------------------
    def validate_prefix(self) -> None:
        """Refuse obviously dangerous prefixes."""

        resolved = self.prefix.expanduser().resolve()
        if resolved == Path("/") or str(resolved) in {"/usr", "/System", "/Library"}:
            raise UsageError(f"refusing to use unsafe prefix: {resolved}")
