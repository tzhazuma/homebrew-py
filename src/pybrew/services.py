"""Homebrew services support (launchd on macOS, systemd on Linux).

Formulae declare a ``service do ... end`` block.  pybrew parses that block into
a :class:`ServiceSpec`, renders a launchd plist (or systemd unit) and drives the
service manager.  The runner is injectable so tests never touch the real
launchd.
"""

from __future__ import annotations

import ipaddress
import os
import plistlib
import re
import shlex
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


#: Keys accepted by ``keep_alive`` in a service block.  ``interval`` is a
#: pybrew extension: it maps to launchd's ``ThrottleInterval`` (and to systemd's
#: ``RestartSec``) rather than to a key inside ``KeepAlive``.
KEEP_ALIVE_KEYS = (
    "always",
    "network_state",
    "successful_exit",
    "crashed",
    "path",
    "interval",
    "other_job_enabled",
)

#: Values accepted by ``process_type`` (the launchd spelling is capitalised).
PROCESS_TYPES = ("background", "standard", "interactive", "adaptive")
RUN_TYPES = ("immediate", "interval", "cron")

#: Socket protocols understood by launchd's ``Sockets`` dictionary.
SOCKET_TYPES = ("tcp", "udp")


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
    run_type: str = "immediate"
    environment_variables: dict[str, str] = field(default_factory=dict)
    require_root: bool = False
    throttle_interval: Optional[int] = None
    launch_only_once: bool = False
    macos_legacy_timers: bool = False
    nice: Optional[int] = None
    restart_delay: Optional[int] = None
    stop_timeout: Optional[int] = None
    root_dir: Optional[str] = None
    input_path: Optional[str] = None
    cron: dict[str, Any] = field(default_factory=dict)
    sockets: dict[str, dict[str, str]] = field(default_factory=dict)


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
                if self.peek().kind == "nl" or (
                    self.peek().kind == "punct" and self.peek().value == ","
                ):
                    self.advance()
                    continue
                key = self.advance()
                if key.kind not in ("id", "str", "sym"):
                    continue
                # Accept both Ruby hash spellings: `key: value` and
                # `:key => value` / `"key" => value`.
                if self.peek().kind == "punct" and self.peek().value == ":":
                    self.advance()
                elif self.peek().kind == "op" and self.peek().value == "=>":
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

        return re.sub(r"#\{([^{}]+)\}", replace, template)


def parse_service_block(formula: Formula, config: Config, raw: Optional[str] = None) -> ServiceSpec:
    raw = raw or (formula.service or {}).get("source") if isinstance(formula.service, dict) else None
    if not raw:
        raise ServiceError(f"{formula.name} does not define a service")
    # Arbitrary-Ruby evaluation is strictly opt-in (exactly "1"); otherwise the
    # default literal parser below is used unchanged.
    if os.environ.get("PYBREW_ALLOW_SERVICE_RUBY", "") == "1":
        from .service_ruby import evaluate_service_source

        return evaluate_service_source(raw, formula, config)
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
        paren_depth = 0
        while True:
            current = parser.peek()
            if current.kind == "eof":
                break
            if current.kind == "nl":
                if paren_depth:
                    parser.advance()
                    continue
                break
            if current.kind == "punct" and current.value == ";":
                break
            if current.kind == "punct" and current.value == ",":
                # A trailing comma continues the directive on the next line.
                parser.advance()
                while parser.peek().kind == "nl":
                    parser.advance()
                continue
            if current.kind == "punct" and current.value == "(":
                paren_depth += 1
                parser.advance()
                continue
            if current.kind == "punct" and current.value == ")":
                paren_depth = max(0, paren_depth - 1)
                parser.advance()
                continue
            # Keyword label: key: value (or the Ruby hash-rocket spelling).
            is_keyword = (
                current.kind in ("id", "const", "str", "sym")
                and parser.peek(1).kind in ("punct", "op")
                and parser.peek(1).value in (":", "=>")
            )
            if is_keyword:
                key = str(parser.advance().value)
                parser.advance()
                args.append((key, parser.parse_value()))
                continue
            args.append(parser.parse_value())
        # Consume trailing newline or statement separator.
        if parser.peek().kind == "nl" or (
            parser.peek().kind == "punct" and parser.peek().value == ";"
        ):
            parser.advance()
        _apply_service_directive(spec, name, args)
    return spec


def _mapping_from_args(args: list[Any], directive: str) -> dict[str, Any]:
    """Turn ``key: value`` arguments (or one hash literal) into a dict."""

    if len(args) == 1 and isinstance(args[0], dict):
        return {str(key): value for key, value in args[0].items()}
    mapping: dict[str, Any] = {}
    for item in args:
        if isinstance(item, tuple) and len(item) == 2:
            mapping[str(item[0])] = item[1]
        elif isinstance(item, dict):
            mapping.update({str(key): value for key, value in item.items()})
        else:
            raise ServiceError(f"{directive} expects keyword arguments or a hash, got {item!r}")
    return mapping


def _service_bool(value: Any, directive: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1"):
            return True
        if lowered in ("false", "no", "0"):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ServiceError(f"{directive} expects true or false, got {value!r}")


def _service_int(value: Any, directive: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool):
        raise ServiceError(f"{directive} expects an integer, got {value!r}")
    result: Optional[int]
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        result = int(value.strip())
    else:
        result = None
    if result is None:
        raise ServiceError(f"{directive} expects an integer, got {value!r}")
    if minimum is not None and result < minimum:
        raise ServiceError(f"{directive} must be >= {minimum}, got {result}")
    return result


def _parse_keep_alive(args: list[Any]) -> Any:
    """Normalise the ``keep_alive`` directive to a bool or a dict.

    ``keep_alive true``/``false`` keep their existing boolean representation so
    older callers and tests keep working.  Hash forms use snake_case keys and
    are translated to launchd/systemd spellings when rendered.
    """

    if len(args) == 1 and isinstance(args[0], bool):
        return args[0]
    mapping = _mapping_from_args(args, "keep_alive")
    if not mapping:
        return False
    unknown = sorted(set(mapping) - set(KEEP_ALIVE_KEYS))
    if unknown:
        allowed = ", ".join(KEEP_ALIVE_KEYS)
        raise ServiceError(
            f"unsupported keep_alive option(s): {', '.join(unknown)}; allowed: {allowed}"
        )
    normalised: dict[str, Any] = {}
    for key, value in mapping.items():
        if key in ("always", "network_state", "successful_exit", "crashed"):
            normalised[key] = _service_bool(value, f"keep_alive {key}")
        elif key == "path":
            if value is None or value == "":
                raise ServiceError("keep_alive path expects a non-empty path")
            normalised[key] = str(value)
        elif key == "interval":
            normalised[key] = _service_int(value, "keep_alive interval", minimum=0)
        elif key == "other_job_enabled":
            if not isinstance(value, dict):
                raise ServiceError("keep_alive other_job_enabled expects a hash of job names")
            normalised[key] = {
                str(name): _service_bool(flag, "other_job_enabled")
                for name, flag in value.items()
            }
    return normalised


def _parse_socket(value: Any) -> dict[str, str]:
    if not isinstance(value, str):
        raise ServiceError(f"sockets expects '<type>://<host>:<port>', got {value!r}")
    match = re.fullmatch(
        r"(?P<type>[A-Za-z][A-Za-z0-9]*)://(?P<host>.+):(?P<port>\d+)", value.strip()
    )
    if not match:
        raise ServiceError(f"sockets expects '<type>://<host>:<port>', got {value!r}")
    socket_type = match.group("type").lower()
    if socket_type not in SOCKET_TYPES:
        raise ServiceError(
            f"sockets protocol must be one of {', '.join(SOCKET_TYPES)}, got {match.group('type')!r}"
        )
    host = match.group("host")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        ipaddress.ip_address(host)
    except ValueError as error:
        raise ServiceError(f"sockets host must be a valid IPv4/IPv6 address, got {host!r}") from error
    port = int(match.group("port"))
    if not 0 <= port <= 65535:
        raise ServiceError(f"sockets port must be between 0 and 65535, got {port}")
    return {"host": host, "port": match.group("port"), "type": socket_type}


def _parse_sockets(args: list[Any]) -> dict[str, dict[str, str]]:
    if not args:
        return {}
    if len(args) == 1 and isinstance(args[0], str):
        return {"listeners": _parse_socket(args[0])}
    mapping = _mapping_from_args(args, "sockets")
    return {str(name): _parse_socket(value) for name, value in mapping.items()}


def _parse_cron(value: Any) -> dict[str, Any]:
    text = str(value).strip()
    parsed: dict[str, Any] = {
        "Minute": "*",
        "Hour": "*",
        "Day": "*",
        "Month": "*",
        "Weekday": "*",
    }
    presets = {
        "@hourly": {"Minute": 0},
        "@daily": {"Minute": 0, "Hour": 0},
        "@weekly": {"Minute": 0, "Hour": 0, "Weekday": 0},
        "@monthly": {"Minute": 0, "Hour": 0, "Day": 1},
        "@yearly": {"Minute": 0, "Hour": 0, "Day": 1, "Month": 1},
        "@annually": {"Minute": 0, "Hour": 0, "Day": 1, "Month": 1},
    }
    if text in presets:
        parsed.update(presets[text])
        return parsed
    parts = text.split()
    if len(parts) != 5:
        raise ServiceError("cron expects a five-field expression or @daily/@hourly/... preset")
    for key, part in zip(("Minute", "Hour", "Day", "Month", "Weekday"), parts):
        if part != "*":
            try:
                parsed[key] = int(part)
            except ValueError as error:
                raise ServiceError(f"cron field {key} must be an integer or '*', got {part!r}") from error
    return parsed


def _apply_service_directive(spec: ServiceSpec, name: str, args: list[Any]) -> None:
    if name == "run":
        if len(args) == 1 and isinstance(args[0], list):
            spec.run = [str(item) for item in args[0]]
        elif args:
            spec.run = [str(item) for item in args if item is not None and str(item) != ""]
    elif name == "working_dir" and args:
        spec.working_dir = str(args[0])
    elif name == "log_path" and args:
        spec.log_path = str(args[0])
    elif name == "error_log_path" and args:
        spec.error_log_path = str(args[0])
    elif name == "keep_alive":
        if args:
            spec.keep_alive = _parse_keep_alive(args)
    elif name == "run_at_load" and args:
        spec.run_at_load = _service_bool(args[0], "run_at_load")
    elif name == "interval" and args:
        spec.interval = _service_int(args[0], "interval", minimum=0)
    elif name == "run_type" and args:
        run_type = str(args[0]).lstrip(":").strip().lower()
        if run_type not in RUN_TYPES:
            raise ServiceError(
                f"run_type must be one of {', '.join(RUN_TYPES)}, got {args[0]!r}"
            )
        spec.run_type = run_type
    elif name == "process_type" and args:
        process_type = str(args[0]).lstrip(":").strip().lower()
        if process_type not in PROCESS_TYPES:
            raise ServiceError(
                f"process_type must be one of {', '.join(PROCESS_TYPES)}, got {args[0]!r}"
            )
        spec.process_type = process_type
    elif name == "environment_variables":
        if args:
            variables = _mapping_from_args(args, "environment_variables")
            for key, value in variables.items():
                spec.environment_variables[str(key)] = "" if value is None else str(value)
    elif name == "require_root" and args:
        spec.require_root = _service_bool(args[0], "require_root")
    elif name == "throttle_interval" and args:
        spec.throttle_interval = _service_int(args[0], "throttle_interval", minimum=0)
    elif name == "launch_only_once" and args:
        spec.launch_only_once = _service_bool(args[0], "launch_only_once")
    elif name == "macos_legacy_timers" and args:
        spec.macos_legacy_timers = _service_bool(args[0], "macos_legacy_timers")
    elif name == "nice" and args:
        spec.nice = _service_int(args[0], "nice")
    elif name == "restart_delay" and args:
        spec.restart_delay = _service_int(args[0], "restart_delay", minimum=0)
    elif name == "stop_timeout" and args:
        spec.stop_timeout = _service_int(args[0], "stop_timeout", minimum=0)
    elif name == "root_dir" and args:
        spec.root_dir = str(args[0])
    elif name == "input_path" and args:
        spec.input_path = str(args[0])
    elif name == "cron" and args:
        spec.cron = _parse_cron(args[0])
    elif name == "sockets":
        spec.sockets = _parse_sockets(args)
    # Unknown directives (nice, run_type, cron, ...) are intentionally ignored
    # so pybrew keeps reading formulae that use newer Homebrew DSL features.


class SystemctlRunner:
    """Thin wrapper around ``systemctl --user`` (or a fake binary in tests)."""

    def __init__(self, executable: Optional[str] = None):
        self.executable = executable or os.environ.get("PYBREW_SYSTEMCTL", "systemctl")

    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.executable, "--user", *args],
                text=True,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ServiceError(f"could not run {self.executable}: {error}") from error

    def daemon_reload(self) -> None:
        result = self.run(["daemon-reload"])
        if result.returncode != 0:
            raise ServiceError(f"systemctl daemon-reload failed: {result.stderr.strip()}")

    def start(self, unit: str) -> None:
        result = self.run(["start", unit])
        if result.returncode != 0:
            raise ServiceError(f"systemctl start {unit} failed: {result.stderr.strip()}")

    def stop(self, unit: str) -> None:
        result = self.run(["stop", unit])
        if result.returncode != 0:
            raise ServiceError(f"systemctl stop {unit} failed: {result.stderr.strip()}")

    def status(self, unit: str) -> str:
        result = self.run(["is-active", "--quiet", unit])
        return "started" if result.returncode == 0 else "stopped"


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


def _launchd_keep_alive(spec: ServiceSpec) -> Any:
    """Return the launchd ``KeepAlive`` value (``None`` means omit the key)."""

    value = spec.keep_alive
    if isinstance(value, bool):
        return True if value else None
    if not isinstance(value, dict) or not value:
        return None
    if value.get("always") is False:
        return None
    if value.get("always") is True:
        return True
    conditions: dict[str, Any] = {}
    if "network_state" in value:
        conditions["NetworkState"] = bool(value["network_state"])
    if "successful_exit" in value:
        conditions["SuccessfulExit"] = bool(value["successful_exit"])
    if "crashed" in value:
        conditions["Crashed"] = bool(value["crashed"])
    if "path" in value:
        conditions["PathState"] = str(value["path"])
    if "other_job_enabled" in value:
        conditions["OtherJobEnabled"] = dict(value["other_job_enabled"])
    if conditions:
        return conditions
    # A hash with only `interval` still asks for the process to be kept alive;
    # the interval itself becomes ThrottleInterval below.
    return True


def _throttle_interval(spec: ServiceSpec) -> Optional[int]:
    if spec.throttle_interval is not None:
        return spec.throttle_interval
    if isinstance(spec.keep_alive, dict):
        interval = spec.keep_alive.get("interval")
        if isinstance(interval, int) and not isinstance(interval, bool):
            return interval
    return None


def _systemd_on_calendar(cron: dict[str, Any]) -> str:
    minute = "*" if cron.get("Minute") == "*" else f"{int(cron['Minute']):02d}"
    hour = "*" if cron.get("Hour") == "*" else f"{int(cron['Hour']):02d}"
    day = "*" if cron.get("Day") == "*" else str(int(cron["Day"]))
    month = "*" if cron.get("Month") == "*" else str(int(cron["Month"]))
    weekday = cron.get("Weekday", "*")
    prefix = ""
    if weekday != "*":
        names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        prefix = f"{names[int(weekday) % 7]} "
    return f"{prefix}*-{month}-{day} {hour}:{minute}:00"


def _systemd_restart(spec: ServiceSpec) -> str:
    value = spec.keep_alive
    if value is True:
        return "always"
    if not isinstance(value, dict) or not value:
        return "no"
    if value.get("always") is False:
        return "no"
    if value.get("always") is True:
        return "always"
    if "successful_exit" in value:
        return "on-success" if value["successful_exit"] else "on-failure"
    if value.get("crashed") or value.get("network_state"):
        return "on-failure"
    if "path" in value:
        return "on-failure"
    if "interval" in value:
        # `keep_alive interval:` still means "keep it running", just throttled.
        return "always"
    return "no"


def _launchd_process_type(value: str) -> str:
    """launchd spells process types capitalised (``Background``, ...)."""

    return value[:1].upper() + value[1:].lower() if value else value


def _launchd_sockets(spec: ServiceSpec) -> dict[str, dict[str, str]]:
    sockets: dict[str, dict[str, str]] = {}
    for name, info in spec.sockets.items():
        sockets[name] = {
            "SockNodeName": info.get("host", ""),
            "SockServiceName": info.get("port", ""),
            "SockProtocol": info.get("type", "").upper(),
        }
    return sockets


class ServiceManager:
    def __init__(self, config: Config, runner: Any = None):
        self.config = config
        self.backend = os.environ.get("PYBREW_SERVICE_BACKEND")
        if self.backend is None:
            self.backend = "systemd" if sys.platform.startswith("linux") else "launchd"
        if runner is not None:
            self.runner = runner
        elif self.backend == "systemd":
            self.runner = SystemctlRunner()
        else:
            self.runner = LaunchctlRunner()
        self.directory = config.service_dir

    # -- plist / unit generation ----------------------------------------
    @staticmethod
    def label(formula: Formula) -> str:
        return f"homebrew.mxcl.{formula.name}"

    def plist_path(self, formula: Formula) -> Path:
        return self.directory / f"{self.label(formula)}.plist"

    def unit_path(self, formula: Formula) -> Path:
        return self.directory / f"{self.label(formula)}.service"

    def unit_text(self, formula: Formula, spec: ServiceSpec) -> str:
        log_dir = self.config.prefix / "var" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = spec.log_path or str(log_dir / f"{formula.name}.log")
        stderr_path = spec.error_log_path or str(log_dir / f"{formula.name}.log")
        lines = [
            "[Unit]",
            f"Description={formula.name} (pybrew)",
            "After=network.target",
            "",
            "[Service]",
            f"Type={'oneshot' if spec.launch_only_once else 'simple'}",
            f"ExecStart={shlex.join(spec.run)}",
            f"Restart={_systemd_restart(spec)}",
        ]
        restart_delay = spec.restart_delay if spec.restart_delay is not None else _throttle_interval(spec)
        if restart_delay is not None:
            lines.append(f"RestartSec={restart_delay}")
        if spec.stop_timeout is not None:
            lines.append(f"TimeoutStopSec={spec.stop_timeout}")
        if spec.root_dir:
            lines.append(f"RootDirectory={spec.root_dir}")
        if spec.input_path:
            lines.append(f"StandardInput=file:{spec.input_path}")
        lines += [
            f"StandardOutput=append:{stdout_path}",
            f"StandardError=append:{stderr_path}",
        ]
        if spec.working_dir or spec.root_dir:
            lines.append(f"WorkingDirectory={spec.working_dir or spec.root_dir}")
        if spec.nice is not None:
            lines.append(f"Nice={spec.nice}")
        for key, value in spec.environment_variables.items():
            escaped = str(value).replace('"', '\\"')
            lines.append(f'Environment="{key}={escaped}"')
        lines += ["", "[Install]", "WantedBy=default.target", ""]
        return "\n".join(lines) + "\n"

    def socket_path(self, formula: Formula) -> Path:
        return self.directory / f"{self.label(formula)}.socket"

    def socket_text(self, formula: Formula, spec: ServiceSpec) -> str:
        if not spec.sockets:
            raise ServiceError(f"{formula.name} service does not define sockets")
        lines = [
            "[Unit]",
            f"Description={formula.name} (pybrew) socket",
            "",
            "[Socket]",
        ]
        for info in spec.sockets.values():
            directive = "ListenDatagram" if info.get("type") == "udp" else "ListenStream"
            lines.append(f"{directive}={info.get('host', '')}:{info.get('port', '')}")
        lines += ["", "[Install]", "WantedBy=sockets.target", ""]
        return "\n".join(lines)

    def write_socket(self, formula: Formula, spec: ServiceSpec) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.socket_path(formula)
        path.write_text(self.socket_text(formula, spec))
        return path

    def timer_path(self, formula: Formula) -> Path:
        return self.directory / f"{self.label(formula)}.timer"

    def timer_text(self, formula: Formula, spec: ServiceSpec) -> str:
        if not spec.cron:
            raise ServiceError(f"{formula.name} service does not define cron")
        return (
            "[Unit]\n"
            f"Description={formula.name} (pybrew) timer\n"
            "\n"
            "[Timer]\n"
            f"OnCalendar={_systemd_on_calendar(spec.cron)}\n"
            "Persistent=true\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )

    def write_timer(self, formula: Formula, spec: ServiceSpec) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.timer_path(formula)
        path.write_text(self.timer_text(formula, spec))
        return path

    def write_unit(self, formula: Formula, spec: ServiceSpec) -> Path:
        if not spec.run:
            raise ServiceError(f"{formula.name} service has no run command")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.unit_path(formula)
        path.write_text(self.unit_text(formula, spec))
        return path

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
        if spec.working_dir or spec.root_dir:
            payload["WorkingDirectory"] = spec.working_dir or spec.root_dir
        if spec.input_path:
            payload["StandardInPath"] = spec.input_path
        if spec.stop_timeout is not None:
            payload["ExitTimeOut"] = spec.stop_timeout
        if spec.nice is not None:
            payload["Nice"] = spec.nice
        if spec.cron:
            payload["StartCalendarInterval"] = dict(spec.cron)
        keep_alive = _launchd_keep_alive(spec)
        if keep_alive is not None:
            payload["KeepAlive"] = keep_alive
        if spec.interval is not None:
            payload["StartInterval"] = spec.interval
        throttle = spec.restart_delay if spec.restart_delay is not None else _throttle_interval(spec)
        if throttle is not None:
            payload["ThrottleInterval"] = throttle
        if spec.process_type:
            payload["ProcessType"] = _launchd_process_type(spec.process_type)
        if spec.macos_legacy_timers:
            payload["LegacyTimers"] = True
        if spec.launch_only_once:
            payload["LaunchOnlyOnce"] = True
        if spec.sockets:
            payload["Sockets"] = _launchd_sockets(spec)
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
        if self.backend == "systemd":
            path = self.write_unit(formula, spec)
            if spec.cron:
                self.write_timer(formula, spec)
            if spec.sockets:
                self.write_socket(formula, spec)
            self.runner.daemon_reload()
            if spec.cron:
                self.runner.start(f"{self.label(formula)}.timer")
            elif spec.sockets:
                self.runner.start(f"{self.label(formula)}.socket")
            else:
                self.runner.start(f"{self.label(formula)}.service")
        else:
            path = self.write_plist(formula, spec)
            self.runner.start(path)
        return path

    def stop(self, formula: Formula) -> Optional[Path]:
        path = self.unit_path(formula) if self.backend == "systemd" else self.plist_path(formula)
        if not path.exists():
            return None
        if self.backend == "systemd":
            timer = self.timer_path(formula)
            if timer.exists():
                self.runner.stop(f"{self.label(formula)}.timer")
            socket = self.socket_path(formula)
            if socket.exists():
                self.runner.stop(f"{self.label(formula)}.socket")
            self.runner.stop(f"{self.label(formula)}.service")
        else:
            self.runner.stop(self.label(formula), path)
        return path

    def restart(self, formula: Formula) -> Path:
        self.stop(formula)
        return self.start(formula)

    def remove(self, formula: Formula) -> None:
        path = self.unit_path(formula) if self.backend == "systemd" else self.plist_path(formula)
        if path.exists():
            if self.backend == "systemd":
                timer = self.timer_path(formula)
                if timer.exists():
                    self.runner.stop(f"{self.label(formula)}.timer")
                socket = self.socket_path(formula)
                if socket.exists():
                    self.runner.stop(f"{self.label(formula)}.socket")
                self.runner.stop(f"{self.label(formula)}.service")
            else:
                self.runner.stop(self.label(formula), path)
            path.unlink()
            timer = self.timer_path(formula)
            if timer.exists():
                timer.unlink()
            socket = self.socket_path(formula)
            if socket.exists():
                socket.unlink()

    def status(self, formula: Formula) -> str:
        path = self.unit_path(formula) if self.backend == "systemd" else self.plist_path(formula)
        if not path.exists():
            return "none"
        label = (
            f"{self.label(formula)}.service"
            if self.backend == "systemd"
            else self.label(formula)
        )
        return self.runner.status(label)

    def cleanup(self) -> list[Path]:
        """Remove service files belonging to formulae that are no longer installed."""

        removed: list[Path] = []
        if not self.directory.is_dir():
            return removed
        for path in sorted(self.directory.iterdir()):
            name = path.name
            if not (
                name.startswith("homebrew.mxcl.")
                and name.endswith((".plist", ".service", ".timer", ".socket"))
            ):
                continue
            formula_name = name[len("homebrew.mxcl.") :].rsplit(".", 1)[0]
            if self.config.opt_path(formula_name).is_dir():
                continue
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                pass
        return removed

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
                    "file": str(
                        self.unit_path(formula)
                        if self.backend == "systemd"
                        else self.plist_path(formula)
                    ),
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
