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
from .cellar import Cellar, Linker, read_receipt
from .doctor import diagnose
from .download import Downloader
from .errors import FormulaNotFound, PybrewError, UsageError
from .installer import InstallOptions, Installer
from .models import Formula, candidate_bottle_tags
from .paths import Config
from .ruby_parser import parse_formula_file
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
