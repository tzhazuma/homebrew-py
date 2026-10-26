"""Tap discovery and formula lookup."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .errors import FormulaNotFound
from .models import Formula
from .ruby_parser import parse_formula_file


@dataclass(frozen=True)
class Tap:
    name: str
    path: Path

    @property
    def formula_dir(self) -> Path:
        return self.path / "Formula"

    def formula_paths(self) -> Iterator[Path]:
        if self.formula_dir.is_dir():
            yield from sorted(self.formula_dir.rglob("*.rb"))
        # Some lightweight taps keep formulae at the tap root.
        for candidate in sorted(self.path.glob("*.rb")):
            if candidate.parent == self.path:
                yield candidate

    def find_path(self, name: str) -> Optional[Path]:
        # Direct hit first, then a recursive scan (formulae may be sharded).
        direct = self.formula_dir / f"{name}.rb"
        if direct.is_file():
            return direct
        for path in self.formula_paths():
            if path.stem == name:
                return path
        return None


def tap_name_for_path(path: Path) -> str:
    path = Path(path)
    name = path.name
    if name.startswith("homebrew-"):
        slug = name[len("homebrew-") :]
        parent = path.parent.name
        if parent and parent != "Taps":
            return f"{parent}/{slug}"
        return f"homebrew/{slug}"
    if path.parent.name == "Taps":
        return f"homebrew/{name}"
    return name


def discover_taps(paths: Iterable[Path]) -> list[Tap]:
    taps: list[Tap] = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        if path.name == "Taps" and path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_dir():
                    taps.append(Tap(tap_name_for_path(child), child))
        else:
            taps.append(Tap(tap_name_for_path(path), path))
    return taps


class FormulaLoader:
    """Load formulae by name from a list of taps."""

    def __init__(self, taps: Iterable[Tap]):
        self.taps = list(taps)
        self._path_cache: dict[str, tuple[Tap, Path]] = {}

    def tap_names(self) -> list[str]:
        return [tap.name for tap in self.taps]

    def formula_names(self) -> list[str]:
        names: set[str] = set()
        for tap in self.taps:
            for path in tap.formula_paths():
                names.add(path.stem)
        return sorted(names)

    def _locate(self, name: str) -> tuple[Tap, Path]:
        if name in self._path_cache:
            return self._path_cache[name]
        # Allow tap-qualified names like homebrew/core/hello and aliases with
        # the tap name prefix.
        candidates = [name]
        if "/" in name:
            candidates.append(name.rsplit("/", 1)[-1])
        for candidate in candidates:
            for tap in self.taps:
                path = tap.find_path(candidate)
                if path is not None:
                    self._path_cache[name] = (tap, path)
                    return tap, path
        raise FormulaNotFound(f"no formula named {name!r} in taps: {self.tap_names()}")

    def load(self, name: str) -> Formula:
        tap, path = self._locate(name)
        formula = parse_formula_file(path, tap=tap.name)
        return formula

    def exists(self, name: str) -> bool:
        try:
            self._locate(name)
            return True
        except FormulaNotFound:
            return False

    def search(self, pattern: str) -> list[Formula]:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)
        results: list[Formula] = []
        for name in self.formula_names():
            try:
                formula = self.load(name)
            except Exception:
                continue
            haystack = f"{formula.name} {formula.desc}"
            if regex.search(haystack):
                results.append(formula)
        return results
