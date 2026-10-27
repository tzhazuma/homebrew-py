"""Homebrew services support (launchd on macOS, systemd on Linux).

Formulae declare a ``service do ... end`` block.  pybrew parses that block into
a :class:`ServiceSpec`, renders a launchd plist (or systemd unit) and drives the
service manager.  The runner is injectable so tests never touch the real
launchd.
"""

from __future__ import annotations

import os
import plistlib
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .errors import PybrewError
from .models import Formula
from .paths import Config
from .ruby_parser import Lexer, Token


class ServiceError(PybrewError):
    """A service block or service operation failed."""


@dataclass
class ServiceSpec:
    run: list[str] = field(default_factory=list)
    working_dir: Optional[str] = None
    log_path: Optional[str] = None
    error_log_path: Optional[str] = None
    keep_alive: Any = False
    run_at_load: bool = True
    interval: Optional[int] = None
    process_type: Optional[str] = None
    environment_variables: dict[str, str] = field(default_factory=dict)
    require_root: bool = False


class _ServiceValueParser:
    """Parse the literal values used inside a service block."""

    def __init__(self, tokens: list[Token], formula: Formula, config: Config):
        self.tokens = tokens
        self.pos = 0
        self.formula = formula
        self.config = config

    def peek(self, offset: int = 0) -> Token:
        index = self.pos + offset
        if index >= len(self.tokens):
            return Token("eof", None, 0, 0, 0)
        return self.tokens[index]

    def advance(self) -> Token:
        if self.pos >= len(self.tokens):
            return Token("eof", None, 0, 0, 0)
        token = self.tokens[self.pos]
        if token.kind != "eof":
            self.pos += 1
        return token

    def parse_value(self) -> Any:
        token = self.peek()
        if token.kind == "str":
            self.advance()
            return self._interpolate(str(token.value))
        if token.kind == "sym":
            self.advance()
            return str(token.value)
        if token.kind == "num":
            self.advance()
            return token.value
        if token.kind == "words":
            self.advance()
            return [self._interpolate(str(item)) for item in token.value]
        if token.kind == "punct" and token.value == "[":
            self.advance()
            values: list[Any] = []
            while self.peek().kind != "eof":
                if self.peek().kind == "punct" and self.peek().value == "]":
                    self.advance()
                    break
                if self.peek().kind == "punct" and self.peek().value == ",":
                    self.advance()
                    continue
                if self.peek().kind == "nl":
                    self.advance()
                    continue
                values.append(self.parse_value())
            return values
        if token.kind == "punct" and token.value == "{":
            self.advance()
            values: dict[str, Any] = {}
            while self.peek().kind != "eof":
                if self.peek().kind == "punct" and self.peek().value == "}":
                    self.advance()
                    break
                key = self.advance()
                if key.kind not in ("id", "str", "sym"):
                    continue
                if self.peek().kind == "punct" and self.peek().value == ":":
                    self.advance()
                values[str(key.value)] = self.parse_value()
            return values
        if token.kind == "id":
            name = str(self.advance().value)
            if name in ("true", "false", "nil"):
                return {"true": True, "false": False, "nil": None}[name]
            return self._resolve_identifier(name)
        if token.kind == "const":
            name = str(self.advance().value)
            raise ServiceError(f"unsupported service value constant {name}")
        # Path expression like opt_bin/"nginx" arrives as id, punct /, str.
        self.advance()
        return str(token.value)

    def _resolve_identifier(self, name: str) -> Any:
        if name in ("opt_bin", "opt_prefix", "bin", "sbin", "lib", "include", "share", "etc", "var", "prefix", "libexec"):
            path = self._base_path(name)
            if self.peek().kind == "punct" and self.peek().value == "/" and self.peek(1).kind == "str":
                self.advance()
                part = self._interpolate(str(self.advance().value))
                return str(path / part)
            return str(path)
        if name == "std_service_path_env":
            return str(self.config.prefix / "bin")
        raise ServiceError(f"unsupported service value {name!r}")

    def _base_path(self, name: str) -> Path:
        mapping = {
            "opt_bin": self.config.opt_path(self.formula.name) / "bin",
            "opt_prefix": self.config.opt_path(self.formula.name),
            "bin": self.config.opt_path(self.formula.name) / "bin",
            "sbin": self.config.opt_path(self.formula.name) / "sbin",
            "lib": self.config.opt_path(self.formula.name) / "lib",
            "include": self.config.opt_path(self.formula.name) / "include",
            "share": self.config.opt_path(self.formula.name) / "share",
            "etc": self.config.opt_path(self.formula.name) / "etc",
            "libexec": self.config.opt_path(self.formula.name) / "libexec",
            "var": self.config.prefix / "var",
            "prefix": self.config.prefix,
        }
        return mapping[name]

    def _interpolate(self, template: str) -> str:
        def replace(match: Any) -> str:
            expression = match.group(1).strip()
            mapping = {
                "version": self.formula.version or "HEAD",
                "prefix": str(self.config.prefix),
                "opt_prefix": str(self.config.opt_path(self.formula.name)),
                "opt_bin": str(self.config.opt_path(self.formula.name) / "bin"),
                "var": str(self.config.prefix / "var"),
                "etc": str(self.config.opt_path(self.formula.name) / "etc"),
                "version.major_minor": ".".join((self.formula.version or "0").split(".")[:2]),
            }
            if expression in mapping:
                return mapping[expression]
            raise ServiceError(f"unsupported service interpolation #{{{expression}}}")

        import re

        return re.sub(r"#\{([^{}]+)\}", replace, template)


def parse_service_block(formula: Formula, config: Config, raw: Optional[str] = None) -> ServiceSpec:
    raw = raw or (formula.service or {}).get("source") if isinstance(formula.service, dict) else None
    if not raw:
        raise ServiceError(f"{formula.name} does not define a service")
    tokens = Lexer(raw).tokenise()
    # Drop the opening `do` and the trailing `end`; parse the inner statements.
    if tokens and tokens[0].kind == "kw" and tokens[0].value == "do":
        tokens = tokens[1:]
    if tokens and tokens[-2].kind == "kw" and tokens[-2].value == "end":
        tokens = tokens[:-2]
    spec = ServiceSpec()
    parser = _ServiceValueParser(tokens, formula, config)
    while parser.peek().kind != "eof":
        token = parser.peek()
        if token.kind == "nl" or (token.kind == "punct" and token.value == ";"):
            parser.advance()
            continue
        if token.kind != "id":
            raise ServiceError(f"unsupported service DSL token {token.value!r}")
        name = str(parser.advance().value)
        args: list[Any] = []
        while parser.peek().kind not in ("nl", "eof"):
            if parser.peek().kind == "punct" and parser.peek().value == ",":
                parser.advance()
                continue
            # Keyword label: key: value
            if (
                parser.peek().kind in ("id", "const", "str", "sym")
                and parser.peek(1).kind == "punct"
                and parser.peek(1).value == ":"
            ):
                key = str(parser.advance().value)
                parser.advance()
                args.append((key, parser.parse_value()))
                continue
            args.append(parser.parse_value())
        # Consume trailing newline.
        if parser.peek().kind == "nl":
            parser.advance()
        _apply_service_directive(spec, name, args)
    return spec


def _apply_service_directive(spec: ServiceSpec, name: str, args: list[Any]) -> None:
    if name == "run":
        value = args[0] if args else []
        if isinstance(value, list):
            spec.run = [str(item) for item in value]
        elif value is not None and value != "":
            spec.run = [str(value)]
    elif name == "working_dir" and args:
        spec.working_dir = str(args[0])
    elif name == "log_path" and args:
        spec.log_path = str(args[0])
    elif name == "error_log_path" and args:
        spec.error_log_path = str(args[0])
    elif name == "keep_alive" and args:
        spec.keep_alive = args[0]
    elif name == "run_at_load" and args:
        spec.run_at_load = bool(args[0])
    elif name == "interval" and args:
        try:
            spec.interval = int(args[0])
        except (TypeError, ValueError):
            pass
    elif name == "process_type" and args:
        spec.process_type = str(args[0])
    elif name == "environment_variables":
        for item in args:
            if isinstance(item, tuple):
                spec.environment_variables[item[0]] = str(item[1])
            elif isinstance(item, dict):
                spec.environment_variables.update({str(k): str(v) for k, v in item.items()})
    elif name == "require_root" and args:
        spec.require_root = bool(args[0])
    # Unknown directives (nice, sockets, ...) are intentionally ignored for now.


class LaunchctlRunner:
    """Thin wrapper around launchctl (or a fake binary in tests)."""

    def __init__(self, executable: Optional[str] = None):
        self.executable = executable or os.environ.get("PYBREW_LAUNCHCTL", "launchctl")

    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.executable, *args],
                text=True,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ServiceError(f"could not run {self.executable}: {error}") from error

    def status(self, label: str) -> str:
        result = self.run(["print", f"gui/{os.getuid()}/{label}"])
        return "started" if result.returncode == 0 else "stopped"

    def start(self, plist: Path) -> None:
        result = self.run(["bootstrap", f"gui/{os.getuid()}", str(plist)])
        if result.returncode != 0:
            # Old launchctl versions use load.
            fallback = self.run(["load", "-w", str(plist)])
            if fallback.returncode != 0:
                detail = (result.stderr or fallback.stderr or "").strip()
                raise ServiceError(f"launchctl bootstrap failed: {detail}")

    def stop(self, label: str, plist: Path) -> None:
        result = self.run(["bootout", f"gui/{os.getuid()}/{label}"])
        if result.returncode != 0:
            self.run(["unload", "-w", str(plist)])


class ServiceManager:
    def __init__(self, config: Config, runner: Optional[LaunchctlRunner] = None):
        self.config = config
        self.runner = runner or LaunchctlRunner()
        self.directory = config.service_dir

    # -- plist / unit generation ----------------------------------------
    @staticmethod
    def label(formula: Formula) -> str:
        return f"homebrew.mxcl.{formula.name}"

    def plist_path(self, formula: Formula) -> Path:
        return self.directory / f"{self.label(formula)}.plist"

    def plist_payload(self, formula: Formula, spec: ServiceSpec) -> dict[str, Any]:
        label = self.label(formula)
        log_dir = self.config.prefix / "var" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = spec.log_path or str(log_dir / f"{formula.name}.log")
        stderr_path = spec.error_log_path or str(log_dir / f"{formula.name}.log")
        payload: dict[str, Any] = {
            "Label": label,
            "ProgramArguments": spec.run,
            "RunAtLoad": spec.run_at_load,
            "StandardOutPath": stdout_path,
            "StandardErrorPath": stderr_path,
        }
        if spec.working_dir:
            payload["WorkingDirectory"] = spec.working_dir
        if spec.keep_alive:
            payload["KeepAlive"] = spec.keep_alive if isinstance(spec.keep_alive, dict) else True
        if spec.interval:
            payload["StartInterval"] = spec.interval
        if spec.process_type:
            payload["ProcessType"] = spec.process_type
        environment = dict(spec.environment_variables)
        environment.setdefault("PATH", str(self.config.prefix / "bin"))
        if environment:
            payload["EnvironmentVariables"] = environment
        return payload

    def write_plist(self, formula: Formula, spec: ServiceSpec) -> Path:
        if not spec.run:
            raise ServiceError(f"{formula.name} service has no run command")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.plist_path(formula)
        path.write_bytes(plistlib.dumps(self.plist_payload(formula, spec)))
        return path

    # -- operations ------------------------------------------------------
    def spec_for(self, formula: Formula) -> ServiceSpec:
        raw = formula.service.get("source") if isinstance(formula.service, dict) else None
        return parse_service_block(formula, self.config, raw=raw)

    def start(self, formula: Formula) -> Path:
        if not self.config.opt_path(formula.name).is_dir():
            raise ServiceError(f"{formula.name} is not installed")
        spec = self.spec_for(formula)
        path = self.write_plist(formula, spec)
        self.runner.start(path)
        return path

    def stop(self, formula: Formula) -> Optional[Path]:
        path = self.plist_path(formula)
        if not path.exists():
            return None
        self.runner.stop(self.label(formula), path)
        return path

    def restart(self, formula: Formula) -> Path:
        self.stop(formula)
        return self.start(formula)

    def remove(self, formula: Formula) -> None:
        path = self.plist_path(formula)
        if path.exists():
            self.runner.stop(self.label(formula), path)
            path.unlink()

    def status(self, formula: Formula) -> str:
        path = self.plist_path(formula)
        if not path.exists():
            return "none"
        if sys.platform == "darwin":
            return self.runner.status(self.label(formula))
        return "started"  # systemd path is future work

    def list(self, formulae: Iterable[Formula]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for formula in formulae:
            if not isinstance(formula.service, dict) or not formula.service.get("source"):
                continue
            try:
                spec = self.spec_for(formula)
            except ServiceError as error:
                rows.append(
                    {
                        "name": formula.name,
                        "status": "error",
                        "file": None,
                        "error": str(error),
                    }
                )
                continue
            rows.append(
                {
                    "name": formula.name,
                    "status": self.status(formula),
                    "file": str(self.plist_path(formula)),
                    "run": " ".join(spec.run),
                }
            )
        return rows


def installed_formulae(config: Config, loader) -> list[Formula]:
    from .cellar import Cellar

    names = Cellar(config).installed()
    result: list[Formula] = []
    for name in names:
        try:
            result.append(loader.load(name))
        except Exception:
            continue
    return result
