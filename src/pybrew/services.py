"""Homebrew services support (launchd on macOS, systemd on Linux).

Formulae declare a ``service do ... end`` block.  pybrew parses that block into
a :class:`ServiceSpec`, renders a launchd plist (or systemd unit) and drives the
service manager.  The runner is injectable so tests never touch the real
launchd.

The literal parser keeps pybrew's formula-opt-prefix paths and the opt-in Ruby
evaluator in :mod:`pybrew.service_ruby` uses Homebrew's formula paths.  Set
``PYBREW_SERVICE_PATH_MODE=homebrew`` or ``=pybrew`` to make both parsers use
the same path semantics (see :func:`service_path_mode`).

Both parsers read Homebrew's user service env override file
(``HOMEBREW_USER_CONFIG_HOME/services/<formula>.env``, see
:func:`read_user_service_env`) and keep it separate from the declared
environment: :meth:`ServiceSpec.effective_environment_variables` follows
Homebrew and never synthesises ``PATH``, while the launchd renderer historically
adds pybrew's ``<prefix>/bin`` default when no PATH is declared or overridden.
The systemd renderer writes effective values only.

Renderers with formula context re-read the user env file at render time (see
:func:`resolve_render_user_environment`) so edits or removals after parsing are
honoured instead of a stale snapshot.  A standalone :class:`ServiceSpec` with no
context keeps using its parse-time snapshot, and explicit
``user_environment`` arguments let callers supply their own values.
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

#: Environment variable selecting the path semantics used by service blocks.
#: ``homebrew`` uses Homebrew's formula paths, ``pybrew``/``literal``/``legacy``
#: keeps pybrew's literal-parser paths and anything else keeps each parser's
#: default (literal parser: pybrew, opt-in Ruby evaluator: homebrew).
SERVICE_PATH_MODE_ENV = "PYBREW_SERVICE_PATH_MODE"

#: Path helper names exposed to service blocks (literal parser and Ruby shim).
SERVICE_PATH_KEYS = (
    "prefix",
    "opt_prefix",
    "opt_bin",
    "bin",
    "sbin",
    "lib",
    "include",
    "share",
    "etc",
    "libexec",
    "var",
)


def service_path_mode(default: str = "pybrew") -> str:
    """Return the requested service path mode (``homebrew`` or ``pybrew``).

    ``PYBREW_SERVICE_PATH_MODE`` opts both service parsers into the same path
    semantics.  Unknown or unset values keep the caller's ``default`` so the
    literal parser (pybrew) and Ruby evaluator (homebrew) remain compatible.
    """

    raw = os.environ.get(SERVICE_PATH_MODE_ENV, "").strip().lower()
    if raw in ("homebrew", "brew"):
        return "homebrew"
    if raw in ("pybrew", "literal", "legacy"):
        return "pybrew"
    return default


def _service_opt_prefix(formula: Formula, config: Any, prefix: Path) -> Path:
    """Return the stable ``opt`` prefix for ``formula``."""

    resolver = getattr(config, "opt_path", None)
    if callable(resolver):
        try:
            return Path(resolver(formula.name))
        except (AttributeError, TypeError, ValueError, OSError):
            pass
    return prefix / "opt" / formula.name


def _service_pkg_version(formula: Formula) -> str:
    """Return Homebrew's ``Formula#pkg_version`` used in keg paths."""

    value = getattr(formula, "pkg_version", None)
    if value:
        return str(value)
    return str(getattr(formula, "version", None) or "HEAD")


def _service_keg_prefix(formula: Formula, config: Any, prefix: Path) -> Path:
    """Return the versioned keg path (Homebrew's ``Formula#prefix``)."""

    resolver = getattr(config, "keg_path", None)
    if callable(resolver):
        try:
            return Path(resolver(formula.name, _service_pkg_version(formula)))
        except (AttributeError, TypeError, ValueError, OSError):
            pass
    return prefix / "Cellar" / formula.name / _service_pkg_version(formula)


def homebrew_service_paths(formula: Formula, config: Any) -> dict[str, str]:
    """Return Homebrew ``Formula`` path helpers for service blocks.

    ``bin``/``sbin``/``lib``/``include``/``share``/``libexec`` are keg
    subdirectories, ``etc``/``var`` live under ``HOMEBREW_PREFIX``,
    ``opt_prefix``/``opt_bin`` are the stable ``opt`` paths and ``prefix``
    follows ``Formula#prefix`` (``opt_prefix`` once the keg exists and is
    opt-linked, the versioned keg path otherwise).
    """

    prefix = Path(config.prefix)
    opt_prefix = _service_opt_prefix(formula, config, prefix)
    keg = _service_keg_prefix(formula, config, prefix)
    install_prefix = keg
    try:
        if (
            keg.is_dir()
            and opt_prefix.is_symlink()
            and opt_prefix.resolve() == keg.resolve()
        ):
            install_prefix = opt_prefix
    except (OSError, RuntimeError):
        # Missing/broken/looping links simply mean "not opt-linked".
        install_prefix = keg
    mapping = {
        "prefix": install_prefix,
        "opt_prefix": opt_prefix,
        "opt_bin": opt_prefix / "bin",
        "bin": install_prefix / "bin",
        "sbin": install_prefix / "sbin",
        "lib": install_prefix / "lib",
        "include": install_prefix / "include",
        "share": install_prefix / "share",
        "libexec": install_prefix / "libexec",
        "etc": prefix / "etc",
        "var": prefix / "var",
    }
    return {name: str(mapping[name]) for name in SERVICE_PATH_KEYS}


def literal_service_paths(formula: Formula, config: Any) -> dict[str, str]:
    """Return pybrew's literal-parser service paths (``bin`` under ``opt``)."""

    prefix = Path(config.prefix)
    opt_prefix = _service_opt_prefix(formula, config, prefix)
    mapping = {
        "prefix": prefix,
        "opt_prefix": opt_prefix,
        "opt_bin": opt_prefix / "bin",
        "bin": opt_prefix / "bin",
        "sbin": opt_prefix / "sbin",
        "lib": opt_prefix / "lib",
        "include": opt_prefix / "include",
        "share": opt_prefix / "share",
        "etc": opt_prefix / "etc",
        "libexec": opt_prefix / "libexec",
        "var": prefix / "var",
    }
    return {name: str(mapping[name]) for name in SERVICE_PATH_KEYS}


def service_paths(
    formula: Formula, config: Any, mode: str = "pybrew"
) -> dict[str, str]:
    """Return the service path map for ``mode`` with safe fallbacks."""

    if mode == "homebrew":
        try:
            return homebrew_service_paths(formula, config)
        except (AttributeError, TypeError, ValueError, OSError):
            pass
    try:
        return literal_service_paths(formula, config)
    except (AttributeError, TypeError, ValueError, OSError):
        # No configuration at all: keep evaluation working with empty paths.
        return {name: "" for name in SERVICE_PATH_KEYS}


def std_service_path_env_value(config: Any, mode: str = "pybrew") -> str:
    """Return ``std_service_path_env`` for ``mode``.

    Homebrew exposes the full search path as a ``String``; pybrew's literal
    parser keeps its reduced ``<prefix>/bin`` value.
    """

    prefix = getattr(config, "prefix", None)
    if prefix is None:
        return ""
    prefix = Path(prefix)
    if mode == "homebrew":
        return ":".join(
            str(path)
            for path in (
                prefix / "bin",
                prefix / "sbin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            )
        )
    return str(prefix / "bin")


#: Homebrew's user configuration directory; service env overrides live in its
#: ``services/<formula>.env`` subdirectory.  Also injectable per parse/render.
USER_CONFIG_HOME_ENV = "HOMEBREW_USER_CONFIG_HOME"


def resolve_user_config_home(
    user_config_home: Optional[Path] = None,
) -> Optional[Path]:
    """Resolve the user config directory from an argument or Homebrew's env."""

    raw: Any = user_config_home
    if raw is None:
        raw = os.environ.get(USER_CONFIG_HOME_ENV)
    if raw is None:
        return None
    text = os.path.expanduser(os.fspath(raw))
    if not text:
        return None
    return Path(text)


def read_user_service_env(
    formula: Formula,
    user_config_home: Optional[Path] = None,
    euid: Optional[int] = None,
) -> dict[str, str]:
    """Read Homebrew's ``services/<formula>.env`` user environment file.

    This is a bounded implementation of Homebrew's ``Service`` behavior shared
    by the literal parser and the opt-in Ruby evaluator: the file is skipped for
    root services and for world- or group-writable files, blank/comment lines
    are ignored, invalid lines are ignored, and the remaining ``KEY=value``
    pairs override the formula-declared environment.  ``user_config_home``
    injects a directory for tests/callers; otherwise ``HOMEBREW_USER_CONFIG_HOME``
    is used and an unset value means "no overrides".
    """

    base = resolve_user_config_home(user_config_home)
    if base is None:
        return {}
    name = str(formula.name or "")
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        return {}
    try:
        effective_uid = os.geteuid() if euid is None else euid
    except AttributeError:  # pragma: no cover - non-POSIX hosts
        effective_uid = -1
    if effective_uid == 0:
        # Homebrew refuses user env overrides for root services.
        return {}
    path = base / "services" / f"{name}.env"
    try:
        if not path.is_file():
            return {}
        mode = path.stat().st_mode
    except OSError:
        return {}
    if mode & 0o002 or mode & 0o020:
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    overrides: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue
        overrides[key] = value.strip()
    return overrides


def resolve_render_user_environment(
    formula: Formula,
    spec: Optional["ServiceSpec"] = None,
    user_config_home: Optional[Path] = None,
) -> Optional[dict[str, str]]:
    """Resolve user env overrides at render time when a context exists.

    Precedence: an explicit ``user_config_home`` argument, then the current
    ``HOMEBREW_USER_CONFIG_HOME`` environment, then the home captured on the
    spec at parse time.  ``None`` means no user context is configured, so a
    renderer should fall back to the spec's parse-time snapshot; an empty dict
    means a context exists but the file currently provides no overrides, which
    intentionally supersedes the stale snapshot.
    """

    context: Optional[Path] = None
    if user_config_home is not None:
        context = resolve_user_config_home(user_config_home)
    if context is None:
        context = resolve_user_config_home(None)
    if context is None and spec is not None:
        stored = getattr(spec, "user_config_home", None)
        if stored:
            context = resolve_user_config_home(Path(str(stored)))
    if context is None:
        return None
    return read_user_service_env(formula, context)


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
    plist_name: Optional[str] = None
    service_name: Optional[str] = None
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
    warnings: list[str] = field(default_factory=list)
    #: Homebrew user env file overrides, applied only by
    #: :meth:`effective_environment_variables` and the renderers.  Appended at
    #: the end so existing positional constructions keep their field order.
    user_environment_variables: dict[str, str] = field(default_factory=dict)
    #: User config home (``HOMEBREW_USER_CONFIG_HOME`` style directory) captured
    #: at parse time when one was available.  Renderers use it to re-read the
    #: env file instead of trusting the snapshot in
    #: :attr:`user_environment_variables`; it stays optional so ServiceSpec
    #: objects remain standalone.
    user_config_home: Optional[str] = None

    @classmethod
    def from_hash(cls, data: dict[str, Any]) -> "ServiceSpec":
        known = cls.__dataclass_fields__
        values = {key: value for key, value in data.items() if key in known}
        if "user_environment" in data and "user_environment_variables" not in values:
            # Accept Homebrew's shorter spelling as an alias in serialised data.
            values["user_environment_variables"] = data["user_environment"]
        return cls(**values)

    def command(self) -> list[str]:
        return list(self.run)

    def command_p(self) -> bool:
        return bool(self.run)

    def manual_command(self) -> list[str]:
        return list(self.run)

    def keep_alive_p(self) -> bool:
        return bool(self.keep_alive)

    def run_at_load_p(self) -> bool:
        return bool(self.run_at_load)

    def timed_p(self) -> bool:
        return bool(self.cron) or self.interval is not None

    def launch_only_once_p(self) -> bool:
        return bool(self.launch_only_once)

    def macos_legacy_timers_p(self) -> bool:
        return bool(self.macos_legacy_timers)

    def requires_root_p(self) -> bool:
        return bool(self.require_root)

    def nice_requires_root_p(self) -> bool:
        return bool(self.nice) and bool(self.require_root)

    def default_service_name(self, formula_name: str) -> str:
        return f"homebrew.mxcl.{formula_name}"

    def default_plist_name(self, formula_name: str) -> str:
        return f"{self.default_service_name(formula_name)}.plist"

    def path_dir(self, prefix: Path) -> Path:
        return Path(prefix) / "bin"

    def path_dirs(self, prefix: Path) -> list[Path]:
        base = Path(prefix)
        return [base / "bin", base / "sbin"]

    def path_parent_dir(self, prefix: Path) -> Path:
        return Path(prefix).parent

    @property
    def user_environment(self) -> dict[str, str]:
        """Alias for :attr:`user_environment_variables`."""

        return self.user_environment_variables

    @user_environment.setter
    def user_environment(self, value: dict[str, str]) -> None:
        self.user_environment_variables = dict(value)

    def declared_environment_variables(self) -> dict[str, str]:
        """Return the service-block environment without user overrides.

        This mirrors Homebrew's ``Service#environment_variables`` getter; use
        :meth:`effective_environment_variables` for the values that should be
        written to a plist or systemd unit.
        """

        return {
            str(key): str(value) for key, value in self.environment_variables.items()
        }

    def effective_environment_variables(
        self,
        prefix: Optional[Path] = None,
        user_environment: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        """Return declared environment merged with Homebrew user overrides.

        User values win, matching Homebrew's
        ``Service#effective_environment_variables``.  ``prefix`` is accepted for
        backwards compatibility but no longer synthesises ``PATH``; renderers
        keep pybrew's default search path separately.

        ``user_environment`` explicitly supplies fresh user overrides (for
        example re-read at render time); ``None`` uses the parse-time snapshot
        stored in :attr:`user_environment_variables`, while an empty dict means
        "no current overrides" and ignores that snapshot.
        """

        environment = self.declared_environment_variables()
        overrides = (
            self.user_environment_variables
            if user_environment is None
            else user_environment
        )
        for key, value in overrides.items():
            environment[str(key)] = str(value)
        return environment

    def render_environment_variables(
        self,
        default_path: Optional[str] = None,
        user_environment: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        """Return the environment a renderer should write.

        Homebrew's effective environment never synthesises ``PATH``; pybrew's
        launchd renderer historically adds ``<prefix>/bin`` when neither the
        declared nor the user environment provides one.  Passing
        ``default_path`` reproduces that renderer default, while systemd (and
        callers that need Homebrew semantics) pass nothing.

        ``user_environment`` lets a renderer with a formula/config context pass
        freshly resolved overrides instead of the spec's stale parse-time
        snapshot (see :func:`resolve_render_user_environment`).
        """

        environment = self.effective_environment_variables(
            user_environment=user_environment
        )
        if default_path is not None:
            environment.setdefault("PATH", default_path)
        return environment

    def default_cron_values(self) -> dict[str, int]:
        return {"Minute": 0, "Hour": 0, "Day": 1, "Month": 1, "Weekday": 0}

    def parse_cron(self) -> dict[str, int]:
        values = self.default_cron_values()
        for key, value in (self.cron or {}).items():
            normalized = CRON_KEY_ALIASES.get(str(key), str(key))
            if normalized in values:
                try:
                    values[normalized] = int(value)
                except (TypeError, ValueError):
                    values[normalized] = 0
        return values

    @staticmethod
    def cron_weekday_to_systemd_weekday(weekday: Any) -> str:
        mapping = {
            "0": "Sun",
            "1": "Mon",
            "2": "Tue",
            "3": "Wed",
            "4": "Thu",
            "5": "Fri",
            "6": "Sat",
            "7": "Sun",
            "Sun": "Sun",
            "Mon": "Mon",
            "Tue": "Tue",
            "Wed": "Wed",
            "Thu": "Thu",
            "Fri": "Fri",
            "Sat": "Sat",
        }
        return mapping.get(str(weekday), str(weekday))


#: Cron keys accepted by Homebrew's service DSL.
CRON_KEY_ALIASES = {
    "minute": "Minute",
    "hour": "Hour",
    "day": "Day",
    "month": "Month",
    "weekday": "Weekday",
}


class _ServiceValueParser:
    """Parse the literal values used inside a service block."""

    def __init__(self, tokens: list[Token], formula: Formula, config: Config):
        self.tokens = tokens
        self.pos = 0
        self.formula = formula
        self.config = config
        # The literal parser defaults to pybrew's paths; set
        # PYBREW_SERVICE_PATH_MODE=homebrew to align it with the Ruby evaluator.
        self.path_mode = service_path_mode("pybrew")
        self._path_map_cache: Optional[dict[str, str]] = None

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

    def path_map(self) -> dict[str, str]:
        """Return (and cache) the path helper map for the active mode."""

        if self._path_map_cache is None:
            self._path_map_cache = service_paths(
                self.formula, self.config, self.path_mode
            )
        return self._path_map_cache

    def _resolve_identifier(self, name: str) -> Any:
        if name in SERVICE_PATH_KEYS:
            path = self._base_path(name)
            if self.peek().kind == "punct" and self.peek().value == "/" and self.peek(1).kind == "str":
                self.advance()
                part = self._interpolate(str(self.advance().value))
                return str(path / part)
            return str(path)
        if name == "std_service_path_env":
            return std_service_path_env_value(self.config, self.path_mode)
        raise ServiceError(f"unsupported service value {name!r}")

    def _base_path(self, name: str) -> Path:
        return Path(self.path_map()[name])

    def _interpolate(self, template: str) -> str:
        paths = self.path_map()

        def replace(match: Any) -> str:
            expression = match.group(1).strip()
            mapping = {
                "version": self.formula.version or "HEAD",
                "prefix": paths["prefix"],
                "opt_prefix": paths["opt_prefix"],
                "opt_bin": paths["opt_bin"],
                "var": paths["var"],
                "etc": paths["etc"],
                "version.major_minor": ".".join((self.formula.version or "0").split(".")[:2]),
            }
            if expression in mapping:
                return mapping[expression]
            raise ServiceError(f"unsupported service interpolation #{{{expression}}}")

        return re.sub(r"#\{([^{}]+)\}", replace, template)


def parse_service_block(
    formula: Formula,
    config: Config,
    raw: Optional[str] = None,
    user_config_home: Optional[Path] = None,
) -> ServiceSpec:
    raw = raw or (formula.service or {}).get("source") if isinstance(formula.service, dict) else None
    if not raw:
        raise ServiceError(f"{formula.name} does not define a service")
    # Arbitrary-Ruby evaluation is strictly opt-in (exactly "1"); otherwise the
    # default literal parser below is used unchanged.
    if os.environ.get("PYBREW_ALLOW_SERVICE_RUBY", "") == "1":
        from .service_ruby import evaluate_service_source

        return evaluate_service_source(
            raw, formula, config, user_config_home=user_config_home
        )
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
    # Homebrew user env overrides apply to the literal parser as well.  They are
    # stored separately so the declared environment stays intact; effective
    # values are produced by ServiceSpec.effective_environment_variables and the
    # renderers.  Capture the context directory too so renderers can re-read the
    # file instead of trusting this snapshot.
    context = resolve_user_config_home(user_config_home)
    overrides = read_user_service_env(formula, context)
    if overrides:
        spec.user_environment_variables = overrides
    if context is not None:
        spec.user_config_home = str(context)
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
    elif name == "name":
        mapping = _mapping_from_args(args, "name") if args else {}
        if mapping.get("macos") is not None:
            spec.plist_name = str(mapping["macos"])
        if mapping.get("linux") is not None:
            spec.service_name = str(mapping["linux"])
    elif name == "plist_name" and args:
        spec.plist_name = str(args[0])
    elif name == "service_name" and args:
        spec.service_name = str(args[0])
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
    else:
        # Keep reading formulae that use newer Homebrew DSL features, but make
        # the unsupported directive visible instead of silently dropping it.
        spec.warnings.append(f"unsupported service directive `{name}`")


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


def _expand_service_path(value: Optional[str]) -> Optional[str]:
    """Expand ``~`` in a service path the way Homebrew's renderers do.

    Homebrew uses ``File.expand_path`` here.  pybrew keeps relative paths as
    written (bounded) and only resolves the home-directory shorthand so tests
    and user supplied paths do not depend on the process working directory.
    """

    if not value:
        return value
    return os.path.expanduser(str(value))


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
    def __init__(
        self,
        config: Config,
        runner: Any = None,
        user_config_home: Optional[Path] = None,
    ):
        self.config = config
        self.user_config_home = user_config_home
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

    def _render_user_config_home(
        self, user_config_home: Optional[Path] = None
    ) -> Optional[Path]:
        """Return the user config home to use for render-time env resolution."""

        if user_config_home is not None:
            return user_config_home
        return self.user_config_home

    # -- plist / unit generation ----------------------------------------
    def label(self, formula: Formula, spec: Optional[ServiceSpec] = None) -> str:
        if spec is not None:
            if self.backend == "systemd" and spec.service_name:
                return spec.service_name
            if self.backend != "systemd" and spec.plist_name:
                return spec.plist_name
        return f"homebrew.mxcl.{formula.name}"

    def to_plist(
        self,
        formula: Formula,
        spec: Optional[ServiceSpec] = None,
        user_config_home: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Return the launchd plist payload for *formula* (Homebrew Service API)."""
        return self.plist_payload(
            formula,
            spec or self.spec_for(formula),
            user_config_home=user_config_home,
        )

    def to_systemd_unit(
        self,
        formula: Formula,
        spec: Optional[ServiceSpec] = None,
        user_config_home: Optional[Path] = None,
    ) -> str:
        """Return the systemd unit text for *formula*."""
        return self.unit_text(
            formula,
            spec or self.spec_for(formula),
            user_config_home=user_config_home,
        )

    def plist_path(
        self, formula: Formula, spec: Optional[ServiceSpec] = None
    ) -> Path:
        return self.directory / f"{self.label(formula, spec)}.plist"

    def unit_path(
        self, formula: Formula, spec: Optional[ServiceSpec] = None
    ) -> Path:
        return self.directory / f"{self.label(formula, spec)}.service"

    def unit_text(
        self,
        formula: Formula,
        spec: ServiceSpec,
        user_config_home: Optional[Path] = None,
    ) -> str:
        log_dir = self.config.prefix / "var" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = _expand_service_path(spec.log_path) or str(
            log_dir / f"{formula.name}.log"
        )
        stderr_path = _expand_service_path(spec.error_log_path) or str(
            log_dir / f"{formula.name}.log"
        )
        working_dir = _expand_service_path(spec.working_dir)
        root_dir = _expand_service_path(spec.root_dir)
        input_path = _expand_service_path(spec.input_path)
        # Re-read user overrides at render time when a context is available so
        # edits/removals since parse time are honoured (no stale snapshots).
        user_environment = resolve_render_user_environment(
            formula,
            spec,
            self._render_user_config_home(user_config_home),
        )
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
        if root_dir:
            lines.append(f"RootDirectory={root_dir}")
        if input_path:
            lines.append(f"StandardInput=file:{input_path}")
        lines += [
            f"StandardOutput=append:{stdout_path}",
            f"StandardError=append:{stderr_path}",
        ]
        if working_dir or root_dir:
            lines.append(f"WorkingDirectory={working_dir or root_dir}")
        if spec.nice is not None:
            lines.append(f"Nice={spec.nice}")
        for key, value in spec.render_environment_variables(
            user_environment=user_environment
        ).items():
            escaped = str(value).replace('"', '\\"')
            lines.append(f'Environment="{key}={escaped}"')
        lines += ["", "[Install]", "WantedBy=default.target", ""]
        return "\n".join(lines) + "\n"

    def socket_path(
        self, formula: Formula, spec: Optional[ServiceSpec] = None
    ) -> Path:
        return self.directory / f"{self.label(formula, spec)}.socket"

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
        path = self.socket_path(formula, spec)
        path.write_text(self.socket_text(formula, spec))
        return path

    def timer_path(
        self, formula: Formula, spec: Optional[ServiceSpec] = None
    ) -> Path:
        return self.directory / f"{self.label(formula, spec)}.timer"

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
        path = self.timer_path(formula, spec)
        path.write_text(self.timer_text(formula, spec))
        return path

    def write_unit(
        self,
        formula: Formula,
        spec: ServiceSpec,
        user_config_home: Optional[Path] = None,
    ) -> Path:
        if not spec.run:
            raise ServiceError(f"{formula.name} service has no run command")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.unit_path(formula, spec)
        path.write_text(
            self.unit_text(formula, spec, user_config_home=user_config_home)
        )
        return path

    def plist_payload(
        self,
        formula: Formula,
        spec: ServiceSpec,
        user_config_home: Optional[Path] = None,
    ) -> dict[str, Any]:
        label = self.label(formula, spec)
        log_dir = self.config.prefix / "var" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = _expand_service_path(spec.log_path) or str(
            log_dir / f"{formula.name}.log"
        )
        stderr_path = _expand_service_path(spec.error_log_path) or str(
            log_dir / f"{formula.name}.log"
        )
        working_dir = _expand_service_path(spec.working_dir)
        root_dir = _expand_service_path(spec.root_dir)
        input_path = _expand_service_path(spec.input_path)
        # Re-read user overrides at render time when a context is available so
        # edits/removals since parse time are honoured (no stale snapshots).
        user_environment = resolve_render_user_environment(
            formula,
            spec,
            self._render_user_config_home(user_config_home),
        )
        payload: dict[str, Any] = {
            "Label": label,
            "ProgramArguments": spec.run,
            "RunAtLoad": spec.run_at_load,
            "StandardOutPath": stdout_path,
            "StandardErrorPath": stderr_path,
        }
        if working_dir or root_dir:
            payload["WorkingDirectory"] = working_dir or root_dir
        # Homebrew emits RootDirectory for launchd as well; keep that bounded
        # parity while retaining pybrew's root_dir-as-working-dir fallback.
        if root_dir:
            payload["RootDirectory"] = root_dir
        if input_path:
            payload["StandardInPath"] = input_path
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
        # pybrew's launchd renderer keeps its historical default search path
        # unless the declared or user environment already provides PATH.
        # ``effective_environment_variables`` itself stays Homebrew-style.
        environment = spec.render_environment_variables(
            default_path=str(self.config.prefix / "bin"),
            user_environment=user_environment,
        )
        if environment:
            payload["EnvironmentVariables"] = environment
        return payload

    def write_plist(
        self,
        formula: Formula,
        spec: ServiceSpec,
        user_config_home: Optional[Path] = None,
    ) -> Path:
        if not spec.run:
            raise ServiceError(f"{formula.name} service has no run command")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.plist_path(formula, spec)
        path.write_bytes(
            plistlib.dumps(
                self.plist_payload(
                    formula, spec, user_config_home=user_config_home
                )
            )
        )
        return path

    # -- operations ------------------------------------------------------
    def spec_for(
        self,
        formula: Formula,
        user_config_home: Optional[Path] = None,
    ) -> ServiceSpec:
        raw = formula.service.get("source") if isinstance(formula.service, dict) else None
        return parse_service_block(
            formula,
            self.config,
            raw=raw,
            user_config_home=self._render_user_config_home(user_config_home),
        )

    def _spec_for_paths(self, formula: Formula) -> Optional[ServiceSpec]:
        try:
            return self.spec_for(formula)
        except Exception:
            return None

    def start(self, formula: Formula, spec: Optional[ServiceSpec] = None) -> Path:
        if not self.config.opt_path(formula.name).is_dir():
            raise ServiceError(f"{formula.name} is not installed")
        spec = spec or self.spec_for(formula)
        if self.backend == "systemd":
            path = self.write_unit(formula, spec)
            if spec.cron:
                self.write_timer(formula, spec)
            if spec.sockets:
                self.write_socket(formula, spec)
            self.runner.daemon_reload()
            if spec.cron:
                self.runner.start(f"{self.label(formula, spec)}.timer")
            elif spec.sockets:
                self.runner.start(f"{self.label(formula, spec)}.socket")
            else:
                self.runner.start(f"{self.label(formula, spec)}.service")
        else:
            path = self.write_plist(formula, spec)
            self.runner.start(path)
        return path

    def stop(self, formula: Formula, spec: Optional[ServiceSpec] = None) -> Optional[Path]:
        spec = spec or self._spec_for_paths(formula)
        path = (
            self.unit_path(formula, spec)
            if self.backend == "systemd"
            else self.plist_path(formula, spec)
        )
        if not path.exists():
            return None
        if self.backend == "systemd":
            timer = self.timer_path(formula, spec)
            if timer.exists():
                self.runner.stop(f"{self.label(formula, spec)}.timer")
            socket = self.socket_path(formula, spec)
            if socket.exists():
                self.runner.stop(f"{self.label(formula, spec)}.socket")
            self.runner.stop(f"{self.label(formula, spec)}.service")
        else:
            self.runner.stop(self.label(formula, spec), path)
        return path

    def restart(self, formula: Formula, spec: Optional[ServiceSpec] = None) -> Path:
        self.stop(formula, spec)
        return self.start(formula, spec)

    def remove(self, formula: Formula) -> None:
        spec = self._spec_for_paths(formula)
        path = (
            self.unit_path(formula, spec)
            if self.backend == "systemd"
            else self.plist_path(formula, spec)
        )
        if path.exists():
            if self.backend == "systemd":
                timer = self.timer_path(formula, spec)
                if timer.exists():
                    self.runner.stop(f"{self.label(formula, spec)}.timer")
                socket = self.socket_path(formula, spec)
                if socket.exists():
                    self.runner.stop(f"{self.label(formula, spec)}.socket")
                self.runner.stop(f"{self.label(formula, spec)}.service")
            else:
                self.runner.stop(self.label(formula, spec), path)
            path.unlink()
            timer = self.timer_path(formula, spec)
            if timer.exists():
                timer.unlink()
            socket = self.socket_path(formula, spec)
            if socket.exists():
                socket.unlink()

    def status(self, formula: Formula) -> str:
        spec = self._spec_for_paths(formula)
        path = (
            self.unit_path(formula, spec)
            if self.backend == "systemd"
            else self.plist_path(formula, spec)
        )
        if not path.exists():
            return "none"
        label = (
            f"{self.label(formula, spec)}.service"
            if self.backend == "systemd"
            else self.label(formula, spec)
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
