"""Minimal Brewfile support (bundle install/check/list)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .cask import CaskError
from .cask_installer import CaskInstaller, CaskLoader, CaskNotFound
from .cellar import Cellar
from .errors import PybrewError
from .installer import InstallOptions, Installer
from .paths import Config
from .services import ServiceManager
from .tap_manager import TapManager
from .taps import FormulaLoader, discover_taps

_LINE_RE = re.compile(r'^\s*(?P<kind>tap|brew|cask|mas|vscode|whalebrew)\s+"(?P<name>[^"]+)"(?P<rest>.*)$')
_OPTION_RE = re.compile(
    r'(?P<key>[A-Za-z_][A-Za-z0-9_]*):\s*'
    r'(?P<value>'
    r'"(?P<double>[^"]*)"'
    r"|'(?P<single>[^']*)'"
    r'|\[(?P<array>[^\]]*)\]'
    r'|(?P<bare>[^,\s]+)'
    r')'
)
# External package managers understood by the basic Brewfile support below.
# Each entry maps the Brewfile kind to (environment override, default executable).
_EXTERNAL_TOOLS = {
    "mas": ("PYBREW_MAS", "mas"),
    "vscode": ("PYBREW_CODE", "code"),
}


@dataclass
class BundleEntry:
    kind: str
    name: str
    options: dict[str, str] = field(default_factory=dict)


def parse_brewfile(path: Path) -> list[BundleEntry]:
    if not path.is_file():
        raise PybrewError(f"Brewfile not found: {path}")
    entries: list[BundleEntry] = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            raise PybrewError(f"unsupported Brewfile line {line_number}: {stripped}")
        rest = match.group("rest")
        options: dict[str, str] = {}
        for option in _OPTION_RE.finditer(rest):
            options[option.group("key")] = _option_value(option)
        if not options:
            positional = re.search(r',\s*"([^"]+)"', rest)
            if positional:
                options["url"] = positional.group(1)
        entries.append(BundleEntry(match.group("kind"), match.group("name"), options))
    return entries


def _option_value(match: re.Match[str]) -> str:
    """Return the raw value of a parsed Brewfile option.

    Quoted strings are unwrapped, while booleans, numbers and ``args:`` arrays
    keep their textual form so callers can interpret them.
    """

    if match.group("double") is not None:
        return match.group("double")
    if match.group("single") is not None:
        return match.group("single")
    return match.group("value")


def _option_arguments(raw: str) -> list[str]:
    """Extract the quoted strings from an ``args: [...]`` option."""

    matches = re.findall(r'"([^"]*)"|\'([^\']*)\'', raw)
    return [double if double != "" else single for double, single in matches]


def _install_options(entry: BundleEntry, dry_run: bool) -> InstallOptions:
    arguments = {
        argument.casefold() for argument in _option_arguments(entry.options.get("args", ""))
    }
    return InstallOptions(
        dry_run=dry_run,
        link=entry.options.get("link", "true").lower() != "false",
        head="--head" in arguments,
        build_from_source="--build-from-source" in arguments,
    )


def _restart_service(entry: BundleEntry) -> bool:
    """Return whether the entry opted into starting its service after install."""

    return str(entry.options.get("restart_service", "")).strip().casefold() == "true"


def _formula_has_service(formula: object) -> bool:
    service = getattr(formula, "service", None)
    return isinstance(service, dict) and bool(service.get("source"))


def _tap_paths(config: Config) -> list[Path]:
    """Configured tap paths plus taps already installed under the prefix.

    Re-reading ``TapManager.list()`` is what makes the loaders below pick up a
    tap that ``bundle install`` just cloned.
    """

    paths = [Path(path) for path in config.taps]
    paths.extend(tap.path for tap in TapManager(config).list())
    return list(dict.fromkeys(paths))


def _formula_loader(config: Config) -> FormulaLoader:
    return FormulaLoader(discover_taps(_tap_paths(config)))


def _cask_loader(config: Config, allow_api: bool) -> CaskLoader:
    return CaskLoader(discover_taps(_tap_paths(config)), allow_api=allow_api)


def _resolve_external_tool(kind: str) -> str:
    env_var, default = _EXTERNAL_TOOLS[kind]
    configured = os.environ.get(env_var) or default
    resolved = shutil.which(configured)
    if resolved is None:
        raise PybrewError(
            f"{default} executable not found ({configured!r}); "
            f"install {default} or set {env_var}"
        )
    return resolved


def _run_external(kind: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    executable = _resolve_external_tool(kind)
    try:
        return subprocess.run(
            [executable, *arguments], check=True, capture_output=True, text=True
        )
    except OSError as error:
        raise PybrewError(f"cannot run {executable}: {error}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        message = f"{executable} {' '.join(arguments)} failed"
        if detail:
            message += f": {detail}"
        raise PybrewError(message) from error


def _mas_installed(entry: BundleEntry, output: str) -> bool:
    app_id = str(entry.options.get("id", "")).strip()
    if app_id:
        for line in output.splitlines():
            fields = line.split()
            if fields and fields[0] == app_id:
                return True
        return False
    name = entry.name.casefold()
    return any(name in line.casefold() for line in output.splitlines())


def _vscode_installed(entry: BundleEntry, output: str) -> bool:
    installed = {
        line.strip().casefold() for line in output.splitlines() if line.strip()
    }
    return entry.name.casefold() in installed


def _external_installed(kind: str, entry: BundleEntry) -> bool:
    if kind == "mas":
        output = _run_external(kind, ["list"]).stdout
        return _mas_installed(entry, output)
    output = _run_external(kind, ["--list-extensions"]).stdout
    return _vscode_installed(entry, output)


def bundle_list(entries: list[BundleEntry]) -> list[str]:
    return [f"{entry.kind} {entry.name}" for entry in entries]


def bundle_check(config: Config, entries: list[BundleEntry], allow_api: bool = False) -> list[str]:
    missing: list[str] = []
    formula_loader = _formula_loader(config)
    cask_loader = _cask_loader(config, allow_api)
    installed_formulae = Cellar(config).installed()
    cask_installer = CaskInstaller(config)
    for entry in entries:
        if entry.kind == "brew":
            if not installed_formulae.get(entry.name):
                missing.append(entry.name)
        elif entry.kind == "cask":
            if not cask_installer.installed_versions(entry.name):
                missing.append(entry.name)
        elif entry.kind == "tap":
            if entry.name not in [tap.name for tap in TapManager(config).list()]:
                missing.append(f"tap:{entry.name}")
        elif entry.kind in _EXTERNAL_TOOLS:
            if not _external_installed(entry.kind, entry):
                missing.append(f"{entry.kind} {entry.name}")
    return missing


def bundle_install(
    config: Config,
    entries: list[BundleEntry],
    dry_run: bool = False,
    allow_api: bool = False,
) -> tuple[list[str], list[str]]:
    installed: list[str] = []
    errors: list[str] = []
    tap_manager = TapManager(config)

    for entry in entries:
        if entry.kind == "tap":
            try:
                tap_manager.tap(entry.name, url=entry.options.get("url"))
                installed.append(f"tap {entry.name}")
            except PybrewError as error:
                errors.append(f"{entry.name}: {error}")
            continue

    # Re-discover taps after installing any Brewfile taps.
    formula_loader = _formula_loader(config)
    cask_loader = _cask_loader(config, allow_api)

    for entry in entries:
        if entry.kind == "brew":
            try:
                formula = formula_loader.load(entry.name)
                Installer(config, formula_loader).install(
                    formula, _install_options(entry, dry_run)
                )
            except PybrewError as error:
                errors.append(f"{entry.name}: {error}")
                continue
            installed.append(f"brew {entry.name}")
            if dry_run or not _restart_service(entry):
                continue
            if not _formula_has_service(formula):
                errors.append(
                    f"{entry.name}: restart_service requested but the formula "
                    "does not define a service"
                )
                continue
            try:
                ServiceManager(config).start(formula)
            except (PybrewError, OSError) as error:
                errors.append(f"{entry.name}: failed to start service: {error}")
        elif entry.kind == "cask":
            try:
                cask = cask_loader.load(entry.name)
                if dry_run:
                    installed.append(f"cask {entry.name}")
                else:
                    CaskInstaller(config).install(cask)
                    installed.append(f"cask {entry.name}")
            except PybrewError as error:
                errors.append(f"{entry.name}: {error}")
        elif entry.kind in _EXTERNAL_TOOLS:
            try:
                arguments: list[str]
                if entry.kind == "mas":
                    app_id = str(entry.options.get("id", "")).strip()
                    if not app_id:
                        raise PybrewError(
                            f'mas entry "{entry.name}" requires id: <number>'
                        )
                    arguments = ["install", app_id]
                else:
                    arguments = ["--install-extension", entry.name]
                if not dry_run:
                    _run_external(entry.kind, arguments)
                installed.append(f"{entry.kind} {entry.name}")
            except PybrewError as error:
                errors.append(f"{entry.name}: {error}")
    return installed, errors


def bundle_dump(config: Config) -> list[str]:
    """Produce Brewfile lines for taps, installed formulae and installed casks."""

    lines: list[str] = []
    for tap in TapManager(config).list():
        lines.append(f'tap "{tap.name}"')
    for name in sorted(Cellar(config).installed()):
        lines.append(f'brew "{name}"')
    casks = CaskInstaller(config).list_installed()
    seen: set[str] = set()
    for row in casks:
        if row["token"] in seen:
            continue
        seen.add(row["token"])
        lines.append(f'cask "{row["token"]}"')
    return lines
