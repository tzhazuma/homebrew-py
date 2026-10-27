"""The ``pybrew`` command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from . import PYBREW_VERSION
from .api import fetch_formula
from .cask import CaskError
from .cask_installer import CaskInstaller, CaskLoader, CaskNotFound
from .cellar import Cellar, Linker, read_receipt
from .doctor import diagnose
from .download import Downloader
from .errors import FormulaNotFound, PybrewError, UsageError
from .installer import InstallOptions, Installer
from .maintenance import cleanup, find_outdated, reinstall, upgrade_formula
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

    search = subparsers.add_parser("search", parents=[common], help="search formulae")
    search.add_argument("pattern")

    listing = subparsers.add_parser("list", parents=[common], help="list installed formulae")
    listing.add_argument("--versions", action="store_true", help="show all installed versions")
    listing.add_argument("--json", action="store_true")

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

    uninstall = subparsers.add_parser("uninstall", parents=[common], help="uninstall formulae")
    uninstall.add_argument("formulae", nargs="+")
    uninstall.add_argument("--force", action="store_true")

    link = subparsers.add_parser("link", parents=[common], help="link an installed keg")
    link.add_argument("formula")
    link.add_argument("--overwrite", action="store_true")

    unlink = subparsers.add_parser("unlink", parents=[common], help="unlink an installed keg")
    unlink.add_argument("formula")

    tap = subparsers.add_parser("tap", parents=[common], help="list, install or update taps")
    tap.add_argument("names", nargs="*")
    tap.add_argument("--url", help="override the clone URL (useful for local taps)")
    tap.add_argument("--list", action="store_true", help="list installed taps")

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
    ):
        action_parser = cask_actions.add_parser(action_name, help=action_help)
        action_parser.add_argument("tokens", nargs="*")
        action_parser.add_argument("--force", action="store_true")
        action_parser.add_argument("--zap", action="store_true")
        action_parser.add_argument("--json", action="store_true")
        action_parser.add_argument("--appdir", type=Path)

    outdated = subparsers.add_parser("outdated", parents=[common], help="list outdated formulae")
    outdated.add_argument("--json", action="store_true")
    outdated.add_argument("--api", action="store_true")

    upgrade = subparsers.add_parser("upgrade", parents=[common], help="upgrade outdated formulae")
    upgrade.add_argument("formulae", nargs="*")
    upgrade.add_argument("--dry-run", action="store_true")
    upgrade.add_argument("--api", action="store_true")

    cleanup_cmd = subparsers.add_parser("cleanup", parents=[common], help="remove old kegs")
    cleanup_cmd.add_argument("--dry-run", action="store_true")

    reinstall_cmd = subparsers.add_parser("reinstall", parents=[common], help="reinstall formulae")
    reinstall_cmd.add_argument("formulae", nargs="+")

    services = subparsers.add_parser("services", parents=[common], help="manage background services")
    services.add_argument("action", nargs="?", default="list", choices=["list", "start", "stop", "restart", "run", "info"])
    services.add_argument("formula", nargs="?")
    services.add_argument("--json", action="store_true")

    subparsers.add_parser("doctor", parents=[common], help="check the prefix for problems")
    config = subparsers.add_parser("config", parents=[common], help="show pybrew configuration")
    config.add_argument("--json", action="store_true")

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


def cmd_search(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    loader = _loader(config)
    results = loader.search(args.pattern)
    for formula in results:
        print(f"{formula.name}: {formula.desc}" if formula.desc else formula.name)
    return 0 if results else 1


def cmd_list(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
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


def cmd_install(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    config.ensure()
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
    return exit_code


def cmd_uninstall(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
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


def _cask_loader(config: Config) -> CaskLoader:
    return CaskLoader(discover_taps(config.taps))


def cmd_outdated(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
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
    return exit_code


def cmd_cleanup(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    removed = cleanup(config, dry_run=args.dry_run)
    for keg in removed:
        print(("Would remove: " if args.dry_run else "Removed: ") + str(keg))
    return 0


def cmd_reinstall(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    config.ensure()
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
    loader = _cask_loader(config)
    if getattr(args, "action", None) is None:
        args.action = "list"
        args.tokens = []
        args.json = False
        args.force = False
        args.zap = False
    appdir = getattr(args, "appdir", None)
    installer = CaskInstaller(config, appdir=appdir)
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
    issues = diagnose(config)
    if not issues:
        print("Your pybrew prefix looks healthy.")
        return 0
    for issue in issues:
        print(f"Warning: {issue}")
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
    "list": cmd_list,
    "deps": cmd_deps,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "link": cmd_link,
    "unlink": cmd_unlink,
    "tap": cmd_tap,
    "untap": cmd_untap,
    "cask": cmd_cask,
    "outdated": cmd_outdated,
    "upgrade": cmd_upgrade,
    "cleanup": cmd_cleanup,
    "reinstall": cmd_reinstall,
    "services": cmd_services,
    "doctor": cmd_doctor,
    "config": cmd_config,
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
