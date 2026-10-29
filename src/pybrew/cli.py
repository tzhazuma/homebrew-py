"""The ``pybrew`` command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from . import PYBREW_VERSION
from .analytics import (
    analytics_path,
    clear as clear_analytics,
    is_enabled as is_analytics_enabled,
    read_events,
    record_event,
    set_enabled as set_analytics_enabled,
)
from .api import fetch_formula
from .cask import CaskError
from .bundle import bundle_check, bundle_dump, bundle_install, bundle_list, parse_brewfile
from .cask_installer import CaskInstaller, CaskLoader, CaskNotFound
from .cellar import Cellar, Linker, read_receipt
from .doctor import diagnose, diagnose_issues
from .download import Downloader
from .errors import FormulaNotFound, PybrewError, UsageError
from .installer import InstallOptions, Installer
from .maintenance import (
    autoremove,
    cleanup,
    cleanup_cache,
    find_outdated,
    pin,
    pinned_names,
    reinstall,
    unpin,
    upgrade_formula,
)
from .models import Formula, candidate_bottle_tags
from .paths import Config
from .ruby_parser import parse_formula_file
from .services import ServiceError, ServiceManager, installed_formulae
from .tap_manager import TapManager
from .taps import FormulaLoader, discover_taps


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    # argparse.SUPPRESS stops a subparser's defaults from clobbering values
    # already parsed by the top-level parser (brew accepts options before the
    # subcommand).
    parser.add_argument(
        "--prefix", default=argparse.SUPPRESS, help="pybrew prefix (default: ~/.pybrew)"
    )
    parser.add_argument("--cache", default=argparse.SUPPRESS, help="download cache directory")
    parser.add_argument(
        "--tap",
        action="append",
        default=argparse.SUPPRESS,
        help="tap path (repeatable); overrides PYBREW_TAP",
    )
    parser.add_argument(
        "--bottle-tag", default=argparse.SUPPRESS, help="override the bottle tag for this run"
    )
    parser.add_argument(
        "--verbose", action="store_true", default=argparse.SUPPRESS, help="verbose output"
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="pybrew",
        parents=[common],
        description="A Python reimplementation of Homebrew's core package manager (prototype).",
    )
    parser.add_argument("--version", action="version", version=PYBREW_VERSION)

    subparsers = parser.add_subparsers(dest="command", required=True)

    info = subparsers.add_parser("info", parents=[common], help="show formula information")
    info.add_argument("formulae", nargs="+")
    info.add_argument("--json", action="store_true", help="emit brew-compatible JSON")
    info.add_argument("--api", action="store_true", help="fall back to formulae.brew.sh")
    info.add_argument("--cask", action="store_true", help="show cask information")

    search = subparsers.add_parser("search", parents=[common], help="search formulae or casks")
    search.add_argument("pattern")
    search.add_argument("--cask", action="store_true", help="search local cask taps")

    listing = subparsers.add_parser("list", parents=[common], help="list installed formulae")
    listing.add_argument("--versions", action="store_true", help="show all installed versions")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--cask", action="store_true", help="list installed casks")
    listing.add_argument("--pinned", action="store_true", help="list pinned formulae")

    deps = subparsers.add_parser("deps", parents=[common], help="show dependencies")
    deps.add_argument("formula")
    deps.add_argument("--tree", action="store_true")
    deps.add_argument("--json", action="store_true")
    deps.add_argument("--api", action="store_true")

    install = subparsers.add_parser("install", parents=[common], help="install formulae")
    install.add_argument("formulae", nargs="+")
    install.add_argument("--force", action="store_true", help="reinstall/overwrite")
    install.add_argument("--ignore-dependencies", action="store_true")
    install.add_argument("--build-from-source", action="store_true")
    install.add_argument("--bottle-file", type=Path, help="local bottle tarball")
    install.add_argument("--overwrite", action="store_true", help="overwrite link conflicts")
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--api", action="store_true", help="fall back to formulae.brew.sh")
    install.add_argument("--cask", action="store_true", help="install a cask instead of a formula")
    install.add_argument("--appdir", type=Path, help="cask application directory")
    install.add_argument("--pkg-root", type=Path, help="root for cask pkg payloads")
    install.add_argument(
        "--system-pkg",
        action="store_true",
        help="install cask pkg artifacts system-wide (requires root)",
    )
    install.add_argument(
        "--run-scripts",
        action="store_true",
        help="run cask preflight/postflight scripts (explicit opt-in)",
    )
    install.add_argument("--library-root", type=Path, help="root for cask Library artifacts")
    install.add_argument("--no-link", action="store_true", help="do not create symlinks")
    install.add_argument("--HEAD", dest="head", action="store_true", help="build the head revision")
    install.add_argument(
        "--allow-arbitrary-ruby",
        action="store_true",
        help="opt into executing formula install bodies with Ruby (unsafe)",
    )

    cat_cmd = subparsers.add_parser("cat", parents=[common], help="print a formula/cask source file")
    cat_cmd.add_argument("formulae", nargs="+")
    cat_cmd.add_argument("--cask", action="store_true")
    home_cmd = subparsers.add_parser("home", parents=[common], help="print a formula/cask homepage")
    home_cmd.add_argument("formulae", nargs="+")
    home_cmd.add_argument("--cask", action="store_true")
    edit_cmd = subparsers.add_parser("edit", parents=[common], help="open a formula/cask in $EDITOR")
    edit_cmd.add_argument("formulae", nargs="+")
    edit_cmd.add_argument("--cask", action="store_true")

    fetch_cmd = subparsers.add_parser("fetch", parents=[common], help="download formula/cask artifacts")
    fetch_cmd.add_argument("formulae", nargs="+")
    fetch_cmd.add_argument("--cask", action="store_true")
    fetch_cmd.add_argument("--api", action="store_true")

    uninstall = subparsers.add_parser("uninstall", parents=[common], help="uninstall formulae")
    uninstall.add_argument("formulae", nargs="+")
    uninstall.add_argument("--force", action="store_true")
    uninstall.add_argument("--cask", action="store_true", help="uninstall casks")
    uninstall.add_argument("--zap", action="store_true", help="also zap cask trash")

    link = subparsers.add_parser("link", parents=[common], help="link an installed keg")
    link.add_argument("formula")
    link.add_argument("--overwrite", action="store_true")

    unlink = subparsers.add_parser("unlink", parents=[common], help="unlink an installed keg")
    unlink.add_argument("formula")

    tap = subparsers.add_parser("tap", parents=[common], help="list, install or update taps")
    tap.add_argument("names", nargs="*")
    tap.add_argument("--url", help="override the clone URL (useful for local taps)")
    tap.add_argument("--list", action="store_true", help="list installed taps")
    tap.add_argument("--update", action="store_true", help="update all installed taps")

    untap = subparsers.add_parser("untap", parents=[common], help="remove a tap")
    untap.add_argument("name")
    untap.add_argument("--force", action="store_true")

    cask = subparsers.add_parser("cask", parents=[common], help="manage macOS casks (applications)")
    cask_actions = cask.add_subparsers(dest="action", required=False)
    for action_name, action_help in (
        ("install", "install casks"),
        ("uninstall", "uninstall casks"),
        ("list", "list installed casks"),
        ("info", "show cask information"),
        ("search", "search casks"),
        ("zap", "remove a cask's trash paths"),
        ("outdated", "list outdated casks"),
        ("upgrade", "upgrade casks"),
        ("reinstall", "reinstall casks"),
    ):
        action_parser = cask_actions.add_parser(action_name, help=action_help)
        action_parser.add_argument("tokens", nargs="*")
        action_parser.add_argument("--force", action="store_true")
        action_parser.add_argument("--zap", action="store_true")
        action_parser.add_argument("--json", action="store_true")
        action_parser.add_argument("--appdir", type=Path)
        action_parser.add_argument("--pkg-root", type=Path)
        action_parser.add_argument(
            "--system-pkg",
            action="store_true",
            help="install pkg artifacts system-wide (requires root)",
        )
        action_parser.add_argument(
            "--run-scripts",
            action="store_true",
            help="run preflight/postflight scripts (explicit opt-in)",
        )
        action_parser.add_argument("--library-root", type=Path)
        action_parser.add_argument("--api", action="store_true", help="fall back to formulae.brew.sh")

    outdated = subparsers.add_parser("outdated", parents=[common], help="list outdated formulae or casks")
    outdated.add_argument("--json", action="store_true")
    outdated.add_argument("--api", action="store_true")
    outdated.add_argument("--cask", action="store_true")

    upgrade = subparsers.add_parser("upgrade", parents=[common], help="upgrade outdated formulae")
    upgrade.add_argument("formulae", nargs="*")
    upgrade.add_argument("--dry-run", action="store_true")
    upgrade.add_argument("--api", action="store_true")
    upgrade.add_argument("--cask", action="store_true")
    upgrade.add_argument("--system-pkg", action="store_true")
    upgrade.add_argument("--run-scripts", action="store_true")

    update = subparsers.add_parser("update", parents=[common], help="update all taps")
    update.add_argument("--quiet", action="store_true")

    shellenv = subparsers.add_parser("shellenv", parents=[common], help="print shell environment exports")
    shellenv.add_argument("--shell", choices=["bash", "zsh", "fish"], default="bash")

    leaves = subparsers.add_parser("leaves", parents=[common], help="list explicitly installed formulae")
    uses = subparsers.add_parser("uses", parents=[common], help="show installed formulae depending on a formula")
    uses.add_argument("formula")

    autoremove = subparsers.add_parser("autoremove", parents=[common], help="remove unused dependencies")
    autoremove.add_argument("--dry-run", action="store_true")

    cleanup_cmd = subparsers.add_parser("cleanup", parents=[common], help="remove old kegs and cache files")
    cleanup_cmd.add_argument("--dry-run", action="store_true")
    cleanup_cmd.add_argument("--prune-cache", action="store_true", help="also prune the download cache")
    cleanup_cmd.add_argument("--cache-age-days", type=int, default=120)

    reinstall_cmd = subparsers.add_parser("reinstall", parents=[common], help="reinstall formulae")
    reinstall_cmd.add_argument("formulae", nargs="+")
    reinstall_cmd.add_argument("--cask", action="store_true")
    reinstall_cmd.add_argument("--system-pkg", action="store_true")
    reinstall_cmd.add_argument("--run-scripts", action="store_true")

    pin_cmd = subparsers.add_parser("pin", parents=[common], help="pin a formula at its current version")
    pin_cmd.add_argument("formulae", nargs="+")
    unpin_cmd = subparsers.add_parser("unpin", parents=[common], help="unpin a formula")
    unpin_cmd.add_argument("formulae", nargs="+")

    bundle = subparsers.add_parser("bundle", parents=[common], help="install/check a Brewfile")
    bundle_actions = bundle.add_subparsers(dest="bundle_action", required=True)
    for action_name in ("install", "check", "list", "dump"):
        action_parser = bundle_actions.add_parser(action_name)
        action_parser.add_argument("--file", type=Path, default=Path("Brewfile"))
        action_parser.add_argument("--dry-run", action="store_true")
        action_parser.add_argument("--api", action="store_true")
        action_parser.add_argument("--force", action="store_true")

    services = subparsers.add_parser("services", parents=[common], help="manage background services")
    services.add_argument("action", nargs="?", default="list", choices=["list", "start", "stop", "restart", "run", "info", "cleanup"])
    services.add_argument("formula", nargs="?")
    services.add_argument("--json", action="store_true")

    doctor_cmd = subparsers.add_parser("doctor", parents=[common], help="check the prefix for problems")
    doctor_cmd.add_argument("--json", action="store_true")
    config = subparsers.add_parser("config", parents=[common], help="show pybrew configuration")
    config.add_argument("--json", action="store_true")

    analytics_cmd = subparsers.add_parser(
        "analytics",
        parents=[common],
        help="manage local, opt-in analytics (stored on this machine only)",
    )
    analytics_cmd.add_argument(
        "action",
        nargs="?",
        default="state",
        choices=["state", "on", "enable", "off", "disable", "log", "clear"],
        help="action to perform (default: state)",
    )
    analytics_cmd.add_argument("--json", action="store_true", help="emit JSON")
    analytics_cmd.add_argument(
        "--limit",
        type=int,
        default=None,
        help="show only the most recent N events (log)",
    )

    parse = subparsers.add_parser("parse", parents=[common], help="parse a formula file (debug)")
    parse.add_argument("file", type=Path)
    parse.add_argument("--json", action="store_true")
    return parser


def _config_from_args(args: argparse.Namespace) -> Config:
    config = Config.default(
        prefix=getattr(args, "prefix", None),
        taps=getattr(args, "tap", None),
        cache=getattr(args, "cache", None),
        bottle_tag=getattr(args, "bottle_tag", None),
    )
    config.verbose = bool(getattr(args, "verbose", False))
    config.validate_prefix()
    return config


def _loader(config: Config) -> FormulaLoader:
    return FormulaLoader(discover_taps(config.taps))


def _load_formula(name: str, loader: FormulaLoader, use_api: bool = False) -> Formula:
    if loader.exists(name):
        return loader.load(name)
    if use_api:
        try:
            return fetch_formula(name)
        except FormulaNotFound:
            pass
    raise FormulaNotFound(
        f"no formula named {name!r} in taps: {loader.tap_names() or '(no taps configured)'}"
    )


def _installed_payload(formula: Formula, config: Config) -> list[dict[str, Any]]:
    cellar = Cellar(config)
    versions = cellar.installed().get(formula.name, [])
    payload: list[dict[str, Any]] = []
    for version in versions:
        keg = config.keg_path(formula.name, version)
        receipt = read_receipt(keg)
        payload.append(
            {
                "version": version,
                "used_options": [],
                "built_as_bottle": bool(receipt.get("built_as_bottle", False)),
                "poured_from_bottle": bool(receipt.get("poured_from_bottle", False)),
                "runtime_dependencies": receipt.get("runtime_dependencies", []),
                "installed_as_dependency": bool(receipt.get("installed_as_dependency", False)),
                "installed_on_request": bool(receipt.get("installed_on_request", True)),
                "time": None,
            }
        )
    return payload


def _linked_version(formula: Formula, config: Config) -> Optional[str]:
    opt = config.opt_path(formula.name)
    if not opt.is_symlink():
        return None
    target = Path(os.path.realpath(opt))
    return target.name if target.is_dir() else None


def _formula_json(formula: Formula, config: Config) -> dict[str, Any]:
    installed = _installed_payload(formula, config)
    data = formula.to_api_json(installed=installed)
    linked = _linked_version(formula, config)
    data["linked_keg"] = linked
    data["outdated"] = bool(installed) and installed[-1]["version"] != formula.pkg_version
    return data


# -- command implementations --------------------------------------------
def cmd_info(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    if getattr(args, "cask", False):
        cask_loader = _cask_loader(config, allow_api=bool(getattr(args, "api", False)))
        casks = [cask_loader.load(token) for token in args.formulae]
        if args.json:
            print(json.dumps({"casks": [cask.to_api_json() for cask in casks]}, indent=2))
            return 0
        for cask in casks:
            print(f"{cask.token}: {cask.version}")
            print(f"  {cask.name or ''}")
            print(f"  {cask.desc or ''}")
            print(f"  {cask.homepage or ''}")
        return 0
    loader = _loader(config)
    formulae = [_load_formula(name, loader, use_api=args.api) for name in args.formulae]
    if args.json:
        print(
            json.dumps(
                {"formulae": [_formula_json(formula, config) for formula in formulae], "casks": []},
                indent=2,
                sort_keys=False,
            )
        )
        return 0
    for index, formula in enumerate(formulae):
        if index:
            print()
        print(f"{formula.name}: stable {formula.version}")
        if formula.desc:
            print(formula.desc)
        if formula.homepage:
            print(formula.homepage)
        if formula.license:
            print(f"License: {formula.license}")
        if formula.keg_only:
            print(f"Keg-only: {formula.keg_only_reason or 'yes'}")
        installed = Cellar(config).installed().get(formula.name, [])
        if installed:
            print(f"Installed: {', '.join(installed)}")
            linked = _linked_version(formula, config)
            if linked:
                print(f"Linked: {linked}")
        else:
            print("Not installed")
        tags = formula.bottle_tags()
        print(f"Bottles: {len(tags)} tag(s)" + (f" ({', '.join(sorted(tags)[:4])})" if tags else ""))
        if formula.dependencies:
            print("Dependencies: " + ", ".join(sorted({dep.name for dep in formula.dependencies})))
    return 0


def _resolve_editable(args: argparse.Namespace, config: Config) -> list[Path]:
    paths: list[Path] = []
    if getattr(args, "cask", False):
        cask_loader = _cask_loader(config)
        for token in args.formulae:
            cask = cask_loader.load(token)
            if cask.path is None:
                raise UsageError(f"{token}: no local cask source file")
            paths.append(cask.path)
        return paths
    loader = _loader(config)
    for name in args.formulae:
        formula = _load_formula(name, loader, use_api=bool(getattr(args, "api", False)))
        if formula.path is None:
            raise UsageError(f"{name}: no local formula source file")
        paths.append(formula.path)
    return paths


def cmd_cat(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    try:
        paths = _resolve_editable(args, config)
    except PybrewError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    for path in paths:
        print(path.read_text(), end="" if path.read_text().endswith("\n") else "\n")
    return 0


def cmd_home(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    if getattr(args, "cask", False):
        loader = _cask_loader(config)
        for token in args.formulae:
            cask = loader.load(token)
            if not cask.homepage:
                print(f"Error: {token} has no homepage", file=sys.stderr)
                return 1
            print(cask.homepage)
        return 0
    loader = _loader(config)
    for name in args.formulae:
        formula = _load_formula(name, loader, use_api=bool(getattr(args, "api", False)))
        if not formula.homepage:
            print(f"Error: {name} has no homepage", file=sys.stderr)
            return 1
        print(formula.homepage)
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    try:
        paths = _resolve_editable(args, config)
    except PybrewError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    import shlex
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    command = shlex.split(editor) + [str(path) for path in paths]
    try:
        result = subprocess.run(command)
    except OSError as error:
        print(f"Error: cannot launch editor {editor!r}: {error}", file=sys.stderr)
        return 1
    return result.returncode


def cmd_fetch(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    downloader = Downloader(config.downloads, config.bottle_dirs, bool(getattr(args, "verbose", False)))
    exit_code = 0
    if getattr(args, "cask", False):
        cask_loader = _cask_loader(config, allow_api=bool(getattr(args, "api", False)))
        for token in args.formulae:
            try:
                cask = cask_loader.load(token)
                if not cask.url or not cask.sha256:
                    raise UsageError(f"{token}: cask has no url/sha256")
                cached = downloader.find_cached(cask.sha256)
                if cached:
                    print(cached)
                    continue
                path = downloader.fetch(cask.url, cask.sha256, hint=f"{token}-cask")
            except PybrewError as error:
                print(f"Error: {error}", file=sys.stderr)
                exit_code = 1
                continue
            print(path)
        return exit_code
    loader = _loader(config)
    for name in args.formulae:
        try:
            formula = _load_formula(name, loader, use_api=args.api)
            bottle = formula.bottle_for_tag(config.bottle_tag)
            if bottle is not None:
                tag, bottle_file = bottle
                cached = downloader.find_cached(bottle_file.sha256)
                if cached:
                    print(cached)
                    continue
                path = downloader.fetch(
                    formula.bottle_url(tag, bottle_file),
                    bottle_file.sha256,
                    hint=f"{formula.name}--{formula.pkg_version}.{tag}.bottle.tar.gz",
                )
            else:
                if not formula.stable.url or not formula.stable.sha256:
                    raise UsageError(f"{name}: no bottle and no source url/sha256")
                path = downloader.fetch(
                    formula.stable.url,
                    formula.stable.sha256,
                    hint=f"{formula.name}-source",
                )
        except PybrewError as error:
            print(f"Error: {error}", file=sys.stderr)
            exit_code = 1
            continue
        print(path)
    return exit_code


def cmd_search(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    if getattr(args, "cask", False):
        cask_loader = _cask_loader(config)
        casks = cask_loader.search(args.pattern)
        for cask in casks:
            print(f"{cask.token}: {cask.name or ''} {cask.desc or ''}".strip())
        return 0 if casks else 1
    loader = _loader(config)
    results = loader.search(args.pattern)
    for formula in results:
        print(f"{formula.name}: {formula.desc}" if formula.desc else formula.name)
    return 0 if results else 1


def cmd_list(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    if getattr(args, "pinned", False):
        for name in sorted(pinned_names(config)):
            print(name)
        return 0
    if getattr(args, "cask", False):
        rows = CaskInstaller(config).list_installed()
        if args.json:
            print(json.dumps({"casks": rows}, indent=2))
            return 0
        for row in rows:
            print(f"{row['token']} {row['version']}")
        return 0
    cellar = Cellar(config)
    installed = cellar.installed()
    if args.json:
        data = {
            "formulae": [
                {
                    "name": name,
                    "versions": versions,
                    "linked": _linked_version(Formula(name=name), config),
                }
                for name, versions in sorted(installed.items())
            ]
        }
        print(json.dumps(data, indent=2))
        return 0
    for name, versions in sorted(installed.items()):
        if args.versions:
            print(f"{name} {' '.join(versions)}")
        else:
            print(name)
    return 0


def cmd_deps(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    loader = _loader(config)
    formula = _load_formula(args.formula, loader, use_api=args.api)
    tree: dict[str, Any] = {}

    def visit(current: Formula, seen: set[str]) -> dict[str, Any]:
        node: dict[str, Any] = {}
        for dep in current.runtime_dependencies():
            if dep.name in seen:
                continue
            try:
                child = _load_formula(dep.name, loader, use_api=args.api)
            except FormulaNotFound:
                node[dep.name] = {}
                continue
            node[dep.name] = visit(child, seen | {dep.name})
        return node

    if args.tree:
        tree = {formula.name: visit(formula, {formula.name})}
    if args.json:
        print(json.dumps(tree if args.tree else [dep.name for dep in formula.runtime_dependencies()], indent=2))
        return 0
    if args.tree:
        def render(node: dict[str, Any], indent: int = 0) -> None:
            for name, children in node.items():
                print("  " * indent + name)
                render(children, indent + 1)

        render(tree)
    else:
        for dep in formula.runtime_dependencies():
            print(dep.name)
    return 0


def _installer(config: Config, loader: FormulaLoader) -> Installer:
    return Installer(config, loader, Downloader(config.downloads, config.bottle_dirs), config.verbose)


def _record_analytics(config: Config, event: str, **properties: Any) -> None:
    try:
        record_event(config, event, properties)
    except Exception:
        # Analytics must never break package management.
        pass


def cmd_install(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    config.ensure()
    if getattr(args, "cask", False):
        cask_loader = _cask_loader(config, allow_api=bool(getattr(args, "api", False)))
        cask_installer = CaskInstaller(
            config,
            appdir=getattr(args, "appdir", None),
            pkg_root=getattr(args, "pkg_root", None),
            library_root=getattr(args, "library_root", None),
            system_pkg=bool(getattr(args, "system_pkg", False)),
            run_scripts=bool(getattr(args, "run_scripts", False)),
        )
        exit_code = 0
        for token in args.formulae:
            try:
                cask = cask_loader.load(token)
                result = cask_installer.install(cask, force=args.force)
            except PybrewError as error:
                print(f"Error: {error}", file=sys.stderr)
                exit_code = 1
                continue
            if result.skipped:
                print(f"Already installed: {result.token} {result.version}")
            else:
                print(f"Installed cask: {result.token} {result.version} -> {result.room}")
                _record_analytics(config, "install", cask=result.token, version=result.version)
        return exit_code
    if getattr(args, "allow_arbitrary_ruby", False):
        os.environ["PYBREW_ALLOW_ARBITRARY_RUBY"] = "1"
    loader = _loader(config)
    installer = _installer(config, loader)
    options = InstallOptions(
        force=args.force,
        ignore_dependencies=args.ignore_dependencies,
        build_from_source=args.build_from_source,
        bottle_file=args.bottle_file,
        overwrite=args.overwrite,
        verbose=bool(getattr(args, "verbose", False)),
        dry_run=args.dry_run,
        link=not args.no_link,
        head=bool(getattr(args, "head", False)),
    )
    exit_code = 0
    for name in args.formulae:
        try:
            formula = _load_formula(name, loader, use_api=args.api)
            results = installer.install(formula, options)
        except PybrewError as error:
            print(f"Error: {error}", file=sys.stderr)
            exit_code = 1
            continue
        for result in results:
            if result.skipped:
                action = "Would install" if args.dry_run else "Already installed"
            else:
                action = "Installed" if not result.from_bottle else "Poured from bottle"
            print(f"{action}: {result.name} {result.version} ({result.keg})")
            if not args.dry_run and not result.skipped:
                _record_analytics(
                    config,
                    "install",
                    formula=result.name,
                    version=result.version,
                    bottle=result.from_bottle,
                )
    return exit_code


def cmd_uninstall(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    if getattr(args, "cask", False):
        cask_loader = _cask_loader(config, allow_api=bool(getattr(args, "api", False)))
        cask_installer = CaskInstaller(config)
        exit_code = 0
        for token in args.formulae:
            try:
                cask = None
                try:
                    cask = cask_loader.load(token)
                except CaskNotFound:
                    pass
                removed = cask_installer.uninstall(token, zap=args.zap, cask=cask)
            except PybrewError as error:
                print(f"Error: {error}", file=sys.stderr)
                exit_code = 1
                continue
            print(f"Uninstalled cask: {token}")
            _record_analytics(config, "uninstall", cask=token)
        return exit_code
    loader = _loader(config)
    installer = _installer(config, loader)
    exit_code = 0
    for name in args.formulae:
        try:
            kegs = installer.uninstall(name)
        except PybrewError as error:
            print(f"Error: {error}", file=sys.stderr)
            exit_code = 1
            continue
        for keg in kegs:
            print(f"Uninstalled {name} ({keg})")
        _record_analytics(config, "uninstall", formula=name)
    return exit_code


def cmd_link(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    loader = _loader(config)
    installer = _installer(config, loader)
    formula = _load_formula(args.formula, loader)
    result = installer.link(formula, overwrite=args.overwrite)
    print(f"Linked {formula.name} ({len(result.created)} links)")
    return 0


def cmd_unlink(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    loader = _loader(config)
    installer = _installer(config, loader)
    formula = _load_formula(args.formula, loader)
    removed = installer.unlink(formula)
    print(f"Unlinked {formula.name} ({len(removed)} links)")
    return 0


def cmd_tap(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    manager = TapManager(config)
    if args.update:
        for result in manager.update_all():
            print(f"{result.action}: {result.name}")
        return 0
    if args.list or not args.names:
        taps = manager.list()
        for tap in taps:
            formulas, casks = manager.counts(tap)
            print(f"{tap.name}\t{tap.path}\t{formulas} formula(s), {casks} cask(s)")
        return 0
    if len(args.names) > 1 and args.url:
        raise UsageError("--url can only be used with a single tap")
    for name in args.names:
        result = manager.tap(name, url=args.url)
        print(
            f"{result.action}: {result.name} -> {result.path} "
            f"({result.formulas} formula(s), {result.casks} cask(s))"
        )
    return 0


def cmd_untap(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    manager = TapManager(config)
    path = manager.untap(args.name, force=args.force)
    print(f"untapped: {args.name} ({path})")
    return 0


def _cask_loader(config: Config, allow_api: bool = False) -> CaskLoader:
    return CaskLoader(discover_taps(config.taps), allow_api=allow_api)


def cmd_bundle(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    if args.bundle_action == "dump":
        lines = bundle_dump(config)
        if args.file.exists() and not args.force:
            raise UsageError(f"{args.file} already exists; pass --force to overwrite")
        args.file.write_text("\n".join(lines) + ("\n" if lines else ""))
        print(f"Wrote {len(lines)} entrie(s) to {args.file}")
        return 0
    entries = parse_brewfile(args.file)
    if args.bundle_action == "list":
        for line in bundle_list(entries):
            print(line)
        return 0
    if args.bundle_action == "check":
        missing = bundle_check(config, entries, allow_api=bool(getattr(args, "api", False)))
        if not missing:
            print("The Brewfile's dependencies are satisfied.")
            return 0
        for item in missing:
            print(f"Missing: {item}")
        return 1
    config.ensure()
    installed, errors = bundle_install(
        config,
        entries,
        dry_run=bool(getattr(args, "dry_run", False)),
        allow_api=bool(getattr(args, "api", False)),
    )
    for item in installed:
        print(("Would install: " if args.dry_run else "Installed: ") + item)
    for error in errors:
        print(f"Error: {error}", file=sys.stderr)
    return 1 if errors else 0


def cmd_update(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    results = TapManager(config).update_all()
    if not results:
        if not args.quiet:
            print("No taps installed.")
        return 0
    for result in results:
        if not args.quiet:
            print(f"{result.action}: {result.name}")
    return 0


def cmd_shellenv(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    prefix = str(config.prefix)
    cellar = str(config.cellar)
    if args.shell == "fish":
        print(f'set -gx HOMEBREW_PREFIX "{prefix}";')
        print(f'set -gx HOMEBREW_CELLAR "{cellar}";')
        print(f'set -gx PATH "{prefix}/bin" "{prefix}/sbin" $PATH;')
    else:
        print(f'export HOMEBREW_PREFIX="{prefix}";')
        print(f'export HOMEBREW_CELLAR="{cellar}";')
        print(f'export PATH="{prefix}/bin:{prefix}/sbin:$PATH";')
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    installed = Cellar(config).installed()
    exit_code = 0
    for name in args.formulae:
        if not installed.get(name):
            print(f"Error: {name} is not installed", file=sys.stderr)
            exit_code = 1
            continue
        path = pin(config, name)
        print(f"Pinned {name} ({path})")
    return exit_code


def cmd_unpin(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    exit_code = 0
    for name in args.formulae:
        if unpin(config, name):
            print(f"Unpinned {name}")
        else:
            print(f"{name} is not pinned")
            exit_code = 1
    return exit_code


def cmd_leaves(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    for name, versions in sorted(Cellar(config).installed().items()):
        if not versions:
            continue
        receipt = read_receipt(config.keg_path(name, versions[-1]))
        if not receipt.get("installed_as_dependency"):
            print(name)
    return 0


def cmd_uses(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    dependents: list[str] = []
    for name, versions in sorted(Cellar(config).installed().items()):
        if not versions:
            continue
        receipt = read_receipt(config.keg_path(name, versions[-1]))
        if args.formula in receipt.get("runtime_dependencies", []):
            dependents.append(name)
    for name in dependents:
        print(name)
    return 0 if dependents else 1


def cmd_outdated(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    if getattr(args, "cask", False):
        cask_loader = _cask_loader(config, allow_api=bool(getattr(args, "api", False)))
        rows = CaskInstaller(config).find_outdated(cask_loader)
        if args.json:
            print(json.dumps({"formulae": [], "casks": rows}, indent=2))
            return 0
        for row in rows:
            print(f"{row['token']} {row['installed']} < {row['current']}")
        return 0
    loader = _loader(config)
    rows = [item.as_dict() for item in find_outdated(config, loader)]
    if args.json:
        print(json.dumps({"formulae": rows, "casks": []}, indent=2))
        return 0
    for row in rows:
        print(f"{row['name']} {row['installed']} < {row['current']}")
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    if getattr(args, "cask", False):
        cask_loader = _cask_loader(config, allow_api=bool(getattr(args, "api", False)))
        installer = CaskInstaller(
            config,
            system_pkg=bool(getattr(args, "system_pkg", False)),
            run_scripts=bool(getattr(args, "run_scripts", False)),
        )
        tokens = args.formulae or [row["token"] for row in installer.find_outdated(cask_loader)]
        if not tokens:
            print("Already up-to-date.")
            return 0
        exit_code = 0
        for token in tokens:
            try:
                cask = cask_loader.load(token)
                if args.dry_run:
                    print(f"Would upgrade: {token} {cask.resolved_version()}")
                    continue
                result = installer.upgrade(cask)
            except PybrewError as error:
                print(f"Error: {error}", file=sys.stderr)
                exit_code = 1
                continue
            print(f"Upgraded: {result.token} {result.version}")
            _record_analytics(config, "upgrade", cask=result.token, version=result.version)
        return exit_code
    loader = _loader(config)
    if args.formulae:
        formulae = [_load_formula(name, loader, use_api=args.api) for name in args.formulae]
    else:
        formulae = [
            _load_formula(item.name, loader, use_api=args.api)
            for item in find_outdated(config, loader)
        ]
    if not formulae:
        print("Already up-to-date.")
        return 0
    exit_code = 0
    for formula in formulae:
        installed = Cellar(config).installed().get(formula.name, [])
        if installed and installed[-1] == formula.pkg_version:
            print(f"{formula.name} is already up-to-date")
            continue
        try:
            messages = upgrade_formula(
                config, loader, formula, dry_run=args.dry_run,
                verbose=bool(getattr(args, "verbose", False)),
            )
        except PybrewError as error:
            print(f"Error: {error}", file=sys.stderr)
            exit_code = 1
            continue
        for message in messages:
            print(("Would upgrade: " if args.dry_run else "Upgraded: ") + message)
        if not args.dry_run:
            _record_analytics(config, "upgrade", formula=formula.name, version=formula.pkg_version)
    return exit_code


def cmd_autoremove(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    removed = autoremove(config, dry_run=args.dry_run)
    for name in removed:
        print(("Would remove: " if args.dry_run else "Removed: ") + name)
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    removed = cleanup(config, dry_run=args.dry_run)
    for keg in removed:
        print(("Would remove: " if args.dry_run else "Removed: ") + str(keg))
    if args.prune_cache:
        cached = cleanup_cache(config, age_days=args.cache_age_days, dry_run=args.dry_run)
        for path in cached:
            print(("Would remove cache: " if args.dry_run else "Removed cache: ") + str(path))
    return 0


def cmd_reinstall(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    config.ensure()
    if getattr(args, "cask", False):
        cask_loader = _cask_loader(config)
        installer = CaskInstaller(
            config,
            system_pkg=bool(getattr(args, "system_pkg", False)),
            run_scripts=bool(getattr(args, "run_scripts", False)),
        )
        exit_code = 0
        for token in args.formulae:
            try:
                cask = cask_loader.load(token)
                installer.uninstall(token, cask=cask)
                installer.install(cask, force=True)
            except PybrewError as error:
                print(f"Error: {error}", file=sys.stderr)
                exit_code = 1
                continue
            print(f"Reinstalled cask: {token}")
        return exit_code
    loader = _loader(config)
    exit_code = 0
    for name in args.formulae:
        try:
            formula = _load_formula(name, loader)
            messages = reinstall(config, loader, formula, verbose=bool(getattr(args, "verbose", False)))
        except PybrewError as error:
            print(f"Error: {error}", file=sys.stderr)
            exit_code = 1
            continue
        for message in messages:
            print(f"Reinstalled: {message}")
    return exit_code


def cmd_cask(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    loader = _cask_loader(config, allow_api=bool(getattr(args, "api", False)))
    if getattr(args, "action", None) is None:
        args.action = "list"
        args.tokens = []
        args.json = False
        args.force = False
        args.zap = False
    appdir = getattr(args, "appdir", None)
    pkg_root = getattr(args, "pkg_root", None)
    library_root = getattr(args, "library_root", None)
    installer = CaskInstaller(
        config,
        appdir=appdir,
        pkg_root=pkg_root,
        library_root=library_root,
        system_pkg=bool(getattr(args, "system_pkg", False)),
        run_scripts=bool(getattr(args, "run_scripts", False)),
    )
    if args.action == "list":
        rows = installer.list_installed()
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("No casks installed.")
        for row in rows:
            print(f"{row['token']} {row['version']} {row['room']}")
        return 0
    if args.action == "search":
        if not args.tokens:
            raise UsageError("cask search needs a pattern")
        for cask in loader.search(args.tokens[0]):
            print(f"{cask.token}: {cask.name or ''} {cask.desc or ''}".strip())
        return 0
    if args.action == "outdated":
        rows = installer.find_outdated(loader)
        if args.json:
            print(json.dumps({"casks": rows}, indent=2))
            return 0
        for row in rows:
            print(f"{row['token']} {row['installed']} < {row['current']}")
        return 0
    if args.action == "reinstall":
        if not args.tokens:
            raise UsageError("cask reinstall needs at least one token")
        exit_code = 0
        for token in args.tokens:
            try:
                cask = loader.load(token)
                installer.uninstall(token, cask=cask)
                result = installer.install(cask, force=True)
            except PybrewError as error:
                print(f"Error: {error}", file=sys.stderr)
                exit_code = 1
                continue
            print(f"Reinstalled cask: {result.token} {result.version}")
        return exit_code
    if args.action == "upgrade":
        tokens = args.tokens or [
            row["token"] for row in installer.find_outdated(loader)
        ]
        if not tokens:
            print("All casks are up-to-date.")
            return 0
        exit_code = 0
        for token in tokens:
            try:
                cask = loader.load(token)
                result = installer.upgrade(cask)
            except PybrewError as error:
                print(f"Error: {error}", file=sys.stderr)
                exit_code = 1
                continue
            if result.skipped:
                print(f"Already up-to-date: {result.token} {result.version}")
            else:
                print(f"Upgraded cask: {result.token} {result.version}")
        return exit_code
    if not args.tokens:
        raise UsageError(f"cask {args.action} needs at least one token")
    exit_code = 0
    for token in args.tokens:
        try:
            cask = None
            try:
                cask = loader.load(token)
            except CaskNotFound:
                if args.action in ("install", "info", "zap"):
                    raise
            if args.action == "info":
                assert cask is not None
                if args.json:
                    print(json.dumps(cask.to_api_json(), indent=2))
                else:
                    print(f"{cask.token}: {cask.version}")
                    print(f"  {cask.desc or ''}")
                    print(f"  {cask.homepage or ''}")
                    print(f"  url: {cask.resolved_url() if cask.url else 'n/a'}")
                    print(f"  sha256: {cask.resolved_sha256() or 'n/a'}")
                    print(f"  artifacts: {[(a.kind, a.source) for a in cask.artifacts]}")
                    print(f"  installed: {', '.join(installer.installed_versions(token)) or 'no'}")
            elif args.action == "install":
                assert cask is not None
                result = installer.install(cask, force=args.force)
                if result.skipped:
                    print(f"Already installed: {result.token} {result.version}")
                else:
                    print(f"Installed cask: {result.token} {result.version} -> {result.room}")
            elif args.action == "uninstall":
                removed = installer.uninstall(
                    token, zap=args.zap, cask=cask
                )
                print(f"Uninstalled cask: {token} ({', '.join(str(path) for path in removed)})")
            elif args.action == "zap":
                assert cask is not None
                removed = installer.zap(cask)
                print(f"Zapped {cask.token}: {len(removed)} path(s)")
        except PybrewError as error:
            print(f"Error: {error}", file=sys.stderr)
            exit_code = 1
    return exit_code


def cmd_services(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    loader = _loader(config)
    manager = ServiceManager(config)
    if args.action == "cleanup":
        removed = manager.cleanup()
        for path in removed:
            print(f"Removed orphan service: {path}")
        return 0
    if args.action == "list":
        rows = manager.list(installed_formulae(config, loader))
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("No services installed.")
            return 0
        print(f"{'Name':24} {'Status':10} File")
        for row in rows:
            print(f"{row['name']:24} {row['status']:10} {row.get('file') or row.get('error', '')}")
        return 0
    if not args.formula:
        raise UsageError(f"services {args.action} requires a formula name")
    formula = _load_formula(args.formula, loader)
    if args.action == "info":
        spec = manager.spec_for(formula)
        payload = {
            "name": formula.name,
            "label": manager.label(formula),
            "plist": str(manager.plist_path(formula)),
            "run": spec.run,
            "working_dir": spec.working_dir,
            "log_path": spec.log_path,
            "keep_alive": spec.keep_alive,
            "status": manager.status(formula),
        }
        print(json.dumps(payload, indent=2))
        return 0
    if args.action == "start":
        path = manager.start(formula)
        print(f"Started {formula.name} ({path})")
    elif args.action == "stop":
        path = manager.stop(formula)
        print(f"Stopped {formula.name}" + (f" ({path})" if path else ""))
    elif args.action == "restart":
        path = manager.restart(formula)
        print(f"Restarted {formula.name} ({path})")
    elif args.action == "run":
        spec = manager.spec_for(formula)
        if not spec.run:
            raise ServiceError(f"{formula.name} service has no run command")
        import subprocess

        result = subprocess.run(spec.run, cwd=spec.working_dir or None)
        return result.returncode
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    issues = diagnose_issues(config)
    if args.json:
        print(json.dumps({"ok": not issues, "issues": issues}, indent=2))
        return 0 if not issues else 1
    if not issues:
        print("Your pybrew prefix looks healthy.")
        return 0
    for issue in issues:
        print(f"Warning: {issue['message']}")
    return 1


def cmd_config(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    data = {
        "pybrew_version": PYBREW_VERSION,
        "prefix": str(config.prefix),
        "cellar": str(config.cellar),
        "cache": str(config.cache),
        "taps": [str(path) for path in config.taps],
        "bottle_tag": config.bottle_tag or candidate_bottle_tags()[0],
        "candidate_bottle_tags": candidate_bottle_tags(),
        "platform": sys.platform,
    }
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for key, value in data.items():
            print(f"{key}: {value}")
    return 0


def cmd_analytics(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    action = getattr(args, "action", None) or "state"
    state_path = analytics_path(config)

    if action in ("on", "enable"):
        set_analytics_enabled(config, True)
        if args.json:
            print(json.dumps({"enabled": True, "path": str(state_path)}, indent=2))
        else:
            print("Analytics enabled (local only; no data is uploaded).")
        return 0

    if action in ("off", "disable"):
        set_analytics_enabled(config, False)
        if args.json:
            print(json.dumps({"enabled": False, "path": str(state_path)}, indent=2))
        else:
            print(
                "Analytics disabled (existing events were kept; run "
                "'pybrew analytics clear' to delete them)."
            )
        return 0

    if action == "log":
        limit = getattr(args, "limit", None)
        if limit is not None and limit < 0:
            raise UsageError("--limit must be zero or greater")
        events = read_events(config, limit=limit)
        if args.json:
            print(json.dumps({"events": events, "count": len(events)}, indent=2))
            return 0
        if not events:
            print("No analytics events recorded.")
            return 0
        for event in events:
            properties = event.get("properties") or {}
            rendered = " ".join(
                f"{key}={value}" for key, value in sorted(properties.items())
            )
            line = f"{event.get('timestamp') or '-'}  {event.get('name') or '-'}"
            print(line + (f"  {rendered}" if rendered else ""))
        return 0

    if action == "clear":
        removed = clear_analytics(config)
        if args.json:
            print(
                json.dumps(
                    {"cleared": removed, "enabled": is_analytics_enabled(config)},
                    indent=2,
                )
            )
        else:
            print(f"Cleared {removed} analytics event(s).")
        return 0

    enabled = is_analytics_enabled(config)
    event_count = len(read_events(config))
    if args.json:
        print(
            json.dumps(
                {"enabled": enabled, "event_count": event_count, "path": str(state_path)},
                indent=2,
            )
        )
        return 0
    print(f"Analytics: {'enabled' if enabled else 'disabled'}")
    print(f"Stored events: {event_count}")
    print(f"State file: {state_path}")
    print("Data is stored locally only; nothing is uploaded.")
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise UsageError(f"formula file not found: {path}")
    formula = parse_formula_file(path)
    if args.json:
        print(json.dumps(formula.to_api_json(), indent=2))
        return 0
    print(f"{formula.name}: {formula.version}")
    print(f"  desc: {formula.desc}")
    print(f"  license: {formula.license}")
    print(f"  deps: {[dep.name for dep in formula.dependencies]}")
    print(f"  bottle tags: {formula.bottle_tags()}")
    return 0


_COMMANDS = {
    "info": cmd_info,
    "search": cmd_search,
    "cat": cmd_cat,
    "home": cmd_home,
    "edit": cmd_edit,
    "fetch": cmd_fetch,
    "list": cmd_list,
    "deps": cmd_deps,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "link": cmd_link,
    "unlink": cmd_unlink,
    "tap": cmd_tap,
    "untap": cmd_untap,
    "cask": cmd_cask,
    "bundle": cmd_bundle,
    "pin": cmd_pin,
    "unpin": cmd_unpin,
    "update": cmd_update,
    "shellenv": cmd_shellenv,
    "outdated": cmd_outdated,
    "upgrade": cmd_upgrade,
    "leaves": cmd_leaves,
    "uses": cmd_uses,
    "autoremove": cmd_autoremove,
    "cleanup": cmd_cleanup,
    "reinstall": cmd_reinstall,
    "services": cmd_services,
    "doctor": cmd_doctor,
    "config": cmd_config,
    "analytics": cmd_analytics,
    "parse": cmd_parse,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        handler = _COMMANDS[args.command]
        return handler(args)
    except PybrewError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
