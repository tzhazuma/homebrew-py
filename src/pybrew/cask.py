"""Parser and model for Homebrew Cask definitions."""

from __future__ import annotations

import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .errors import FormulaParseError, PybrewError
from .models import is_macos, macos_major_version
from .ruby_parser import Lexer, Token


class CaskError(PybrewError):
    """A cask could not be parsed or installed."""


@dataclass
class Artifact:
    kind: str
    source: str
    target: Optional[str] = None
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Cask:
    token: str
    path: Optional[Path] = None
    tap: Optional[str] = None
    version: Optional[str] = None
    sha256: Optional[str] = None
    sha256_map: dict[str, str] = field(default_factory=dict)
    url: Optional[str] = None
    name: Optional[str] = None
    desc: Optional[str] = None
    homepage: Optional[str] = None
    artifacts: list[Artifact] = field(default_factory=list)
    renames: list[tuple[str, str]] = field(default_factory=list)
    uninstall: dict[str, Any] = field(default_factory=dict)
    zap: list[str] = field(default_factory=list)
    depends_on: dict[str, Any] = field(default_factory=dict)
    os: dict[str, Any] = field(default_factory=dict)
    conflicts_with: dict[str, Any] = field(default_factory=dict)
    auto_updates: bool = False
    stage_only: bool = False
    container: Optional[str] = None
    language: Optional[str] = None
    arch: Optional[str] = None
    arch_map: dict[str, str] = field(default_factory=dict)
    appdir: Optional[str] = None
    caveats: Optional[str] = None
    caveats_source: Optional[str] = None
    livecheck_source: Optional[str] = None
    no_autobump: bool = False
    no_autobump_reason: Optional[str] = None
    deprecated: bool = False
    deprecation_reason: Optional[str] = None
    disabled: bool = False
    disable_reason: Optional[str] = None
    # Raw Ruby block bodies captured from the cask.  They are inert data: no
    # parser or installer path executes them unless a caller explicitly opts
    # in (see ``CaskInstaller(run_scripts=True)``).
    preflight: Optional[str] = None
    postflight: Optional[str] = None
    uninstall_preflight: Optional[str] = None
    uninstall_postflight: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.tap}/{self.token}" if self.tap else self.token

    @property
    def scripts(self) -> dict[str, str]:
        """Return the captured script bodies keyed by stanza name."""
        return {
            name: body
            for name in _SCRIPT_STANZAS
            if (body := getattr(self, name, None))
        }

    def resolved_version(self) -> str:
        return self.version or "latest"

    def resolved_arch(self) -> str:
        if self.arch:
            return self.arch
        machine = platform.machine().lower()
        return "arm64" if machine in ("arm64", "aarch64") else "intel"

    def resolved_sha256(self) -> Optional[str]:
        if self.sha256:
            return self.sha256
        arch = self.resolved_arch()
        return self.sha256_map.get(arch)

    def _interpolate(self, template: str) -> str:
        version = self.resolved_version()
        arch_name = self.resolved_arch()
        mapping = {
            "version": version,
            "version.major": version.split(".")[0],
            "version.minor": version.split(".")[1] if "." in version else "0",
            "version.major_minor": ".".join(version.split(".")[:2]),
            "version.dots_to_underscores": version.replace(".", "_"),
            "version.dots_to_hyphens": version.replace(".", "-"),
            "version.major_minor.dots_to_underscores": ".".join(version.split(".")[:2]).replace(".", "_"),
            "arch": {"arm64": "arm", "intel": "intel"}.get(arch_name, arch_name),
            "language": self.language or "en",
            "token": self.token,
            "appdir": self.appdir or str(Path.home() / "Applications"),
        }
        if arch_name == "arm64" and self.arch_map.get("arm"):
            mapping["arch"] = self.arch_map["arm"]
        elif arch_name == "intel" and self.arch_map.get("intel"):
            mapping["arch"] = self.arch_map["intel"]

        def replace(match: re.Match[str]) -> str:
            expression = match.group(1).strip()
            if expression in mapping:
                return mapping[expression]
            raise CaskError(f"{self.token}: unsupported cask interpolation #{{{expression}}}")

        return re.sub(r"#\{([^{}]+)\}", replace, template)

    def resolved_url(self) -> str:
        if not self.url:
            raise CaskError(f"{self.token}: cask has no url")
        return self._interpolate(self.url)

    def resolved_artifacts(self) -> list[Artifact]:
        result: list[Artifact] = []
        for artifact in self.artifacts:
            result.append(
                Artifact(
                    artifact.kind,
                    self._interpolate(artifact.source),
                    self._interpolate(artifact.target) if artifact.target else None,
                    artifact.kwargs,
                )
            )
        return result

    def to_api_json(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "full_token": self.full_name,
            "tap": self.tap,
            "name": [self.name] if self.name else [],
            "desc": self.desc,
            "homepage": self.homepage,
            "url": self.resolved_url() if self.url else None,
            "version": self.version,
            "sha256": self.resolved_sha256(),
            "artifacts": [
                {"kind": artifact.kind, "source": artifact.source, "target": artifact.target}
                for artifact in self.artifacts
            ],
            "depends_on": self.depends_on,
            "conflicts_with": self.conflicts_with,
            "auto_updates": self.auto_updates,
            "uninstall": self.uninstall,
            "zap": self.zap,
            "caveats": self.caveats,
        }


# Artifact stanzas that name a file or bundle to install.  ``stage_only`` is
# deliberately not included: it is a cask-level flag rather than an artifact.
_ARTIFACT_KINDS = frozenset(
    {
        "app",
        "appimage",
        "bash_completion",
        "artifact",
        "audio_unit_plugin",
        "binary",
        "colorpicker",
        "fish_completion",
        "dictionary",
        "font",
        "input_method",
        "installer",
        "keyboard_layout",
        "manpage",
        "mdimporter",
        "pkg",
        "prefpane",
        "qlplugin",
        "screen_saver",
        "service",
        "suite",
        "vst3_plugin",
        "vst_plugin",
        "zsh_completion",
    }
)

# Cask stanzas whose ``do ... end`` body is Ruby code.  The parser keeps the
# raw source; only an explicit ``run_scripts`` install evaluates it.
_SCRIPT_STANZAS = (
    "preflight",
    "postflight",
    "uninstall_preflight",
    "uninstall_postflight",
)

# Keywords that open a nested Ruby block inside a cask script body.
_BLOCK_KEYWORDS = frozenset(
    {"do", "if", "unless", "case", "begin", "while", "until", "for"}
)

_ARCH_NAMES = {
    "big_sur": 11,
    "monterey": 12,
    "ventura": 13,
    "sonoma": 14,
    "sequoia": 15,
    "tahoe": 26,
    "golden_gate": 27,
}


class CaskParser:
    def __init__(self, source: str, path: Optional[Path] = None, tap: Optional[str] = None):
        self.source = source
        self.path = Path(path) if path else None
        self.tap = tap
        self.tokens = Lexer(source).tokenise()
        self.pos = 0
        self.cask = Cask(
            token=self.path.stem if self.path else "unknown",
            path=self.path,
            tap=tap,
        )
        self._default_language_seen = False
        self._arch_map: dict[str, str] = {}

    # -- token helpers ---------------------------------------------------
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

    def skip_newlines(self) -> None:
        while self.peek().kind == "nl" or (self.peek().kind == "punct" and self.peek().value == ";"):
            self.advance()

    def parse(self) -> Cask:
        while self.peek().kind != "eof":
            token = self.peek()
            if token.kind == "id" and token.value == "cask":
                self.advance()
                token_value = self.advance()
                if token_value.kind != "str":
                    raise FormulaParseError("cask declaration without a token string")
                self.cask.token = str(token_value.value)
                self.skip_newlines()
                if self.peek().kind == "kw" and self.peek().value == "do":
                    self.advance()
                    self._parse_block()
                    return self.cask
            self.advance()
        raise FormulaParseError("no `cask \"token\" do ... end` declaration found")

    def _parse_block(self) -> None:
        while True:
            self.skip_newlines()
            token = self.peek()
            if token.kind == "eof":
                raise FormulaParseError("unterminated cask block")
            if token.kind == "kw" and token.value == "end":
                self.advance()
                return
            if token.kind != "id":
                self.advance()
                continue
            name = str(self.advance().value)
            args, kwargs = self._parse_arguments()
            # Blocks.
            save = self.pos
            self.skip_newlines()
            block = False
            opener: Optional[Token] = None
            if self.peek().kind == "kw" and self.peek().value == "do":
                block = True
                opener = self.advance()
            elif self.peek().kind == "punct" and self.peek().value == "{":
                block = True
                opener = self.advance()
            else:
                self.pos = save
            if name == "language" and block:
                self._parse_language(args, kwargs, opener)
            elif name.startswith("on_") and block:
                self._parse_conditional(name, args, opener)
            elif name in _SCRIPT_STANZAS and block:
                self._capture_script(name, opener)
            elif name == "caveats" and block:
                self.cask.caveats_source = self._consume_block_body(opener, capture=True)
            elif name == "livecheck" and block:
                self.cask.livecheck_source = self._consume_block_body(opener, capture=True)
            elif block:
                self._skip_block_body(opener)
            else:
                self._apply(name, args, kwargs)

    def _parse_language(
        self, args: list[Any], kwargs: dict[Any, Any], opener: Optional[Token] = None
    ) -> None:
        code = str(args[0]) if args else "en"
        is_default = bool(kwargs.get("default") or kwargs.get(":default"))
        if is_default and not self._default_language_seen:
            self._default_language_seen = True
            self.cask.language = code
            self._parse_block()
            return
        self._skip_block_body(opener)

    def _parse_conditional(self, name: str, args: list[Any], opener: Optional[Token] = None) -> None:
        active = self._conditional_active(name, args)
        if active:
            self._parse_block()
        else:
            self._skip_block_body(opener)

    def _capture_script(self, name: str, opener: Optional[Token]) -> None:
        """Store a cask script body verbatim without evaluating it."""
        body = self._consume_block_body(opener, capture=True) if opener is not None else ""
        setattr(self.cask, name, body)

    def _conditional_active(self, name: str, args: list[Any]) -> bool:
        if name in ("on_arm", "on_arm64"):
            return self.cask.resolved_arch() == "arm64" or platform.machine().lower() in ("arm64", "aarch64")
        if name in ("on_intel", "on_x86_64"):
            return self.cask.resolved_arch() == "intel" or platform.machine().lower() in ("x86_64", "amd64")
        if name.startswith("on_") and name[3:] in _ARCH_NAMES:
            target = _ARCH_NAMES[name[3:]]
            current = macos_major_version()
            modifier = str(args[0]) if args else ""
            if "or_older" in modifier:
                return current <= target
            if "or_newer" in modifier:
                return current >= target
            return current == target
        return False

    def _parse_arguments(self) -> tuple[list[Any], dict[Any, Any]]:
        args: list[Any] = []
        kwargs: dict[Any, Any] = {}
        token = self.peek()
        parenthesised = token.kind == "punct" and token.value == "("
        if parenthesised:
            self.advance()
        continuation = False
        while True:
            token = self.peek()
            if token.kind == "eof":
                break
            if token.kind == "nl":
                if parenthesised or continuation:
                    self.advance()
                    continue
                break
            if token.kind == "kw" and token.value in ("do", "end"):
                break
            if token.kind == "punct" and token.value in (")", "}"):
                if parenthesised and token.value == ")":
                    self.advance()
                break
            if token.kind == "punct" and token.value == ",":
                self.advance()
                continue
            if (
                token.kind in ("id", "const", "str", "sym")
                and self.peek(1).kind == "punct"
                and self.peek(1).value == ":"
            ):
                key = str(self.advance().value)
                self.advance()
                kwargs[key] = self._parse_value()
                if self.peek().kind == "punct" and self.peek().value == ",":
                    self.advance()
                    continuation = True
                    while self.peek().kind == "nl":
                        self.advance()
                    continue
                break
            value = self._parse_value()
            if self.peek().kind == "op" and self.peek().value == "=>":
                self.advance()
                rhs = self._parse_value()
                args.append({value: rhs})
            else:
                args.append(value)
            if self.peek().kind == "punct" and self.peek().value == ",":
                self.advance()
                # A trailing comma permits a continuation line.
                while self.peek().kind == "nl":
                    self.advance()
                continue
            break
        return args, kwargs

    def _parse_value(self) -> Any:
        token = self.peek()
        if token.kind == "punct" and token.value == ":":
            self.advance()
            first = self.advance()
            text = str(first.value)
            last = first
            while self.peek().kind in ("id", "num", "const") and self.peek().start == last.end:
                part = self.advance()
                text += str(part.value)
                last = part
            return ":" + text
        if token.kind == "str":
            self.advance()
            return str(token.value)
        if token.kind == "sym":
            self.advance()
            return ":" + str(token.value)
        if token.kind == "num":
            self.advance()
            return token.value
        if token.kind == "words":
            self.advance()
            return [str(item) for item in token.value]
        if token.kind == "id":
            self.advance()
            if token.value in ("true", "false", "nil"):
                return {"true": True, "false": False, "nil": None}[str(token.value)]
            return str(token.value)
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
                values.append(self._parse_value())
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
                values[str(key.value)] = self._parse_value()
            return values
        self.advance()
        return str(token.value)

    def _skip_block_body(self, opener: Optional[Token | str] = None) -> None:
        self._consume_block_body(opener)

    def _consume_block_body(
        self, opener: Optional[Token | str] = None, capture: bool = False
    ) -> str:
        """Consume a block body, optionally returning its raw source text.

        ``opener`` is the ``do``/``{`` token that introduced the block.  For
        legacy callers that only pass a string (or nothing), a ``do`` block is
        assumed.  The scan mirrors the original skip logic but tracks a stack
        so brace-delimited blocks and hash literals are balanced correctly.
        """

        if isinstance(opener, Token):
            closer = "end" if opener.kind == "kw" else "}"
            body_start = opener.end
        else:
            closer = "end" if (opener or "do") == "do" else "}"
            body_start = self.peek().start
        stack = [closer]
        while self.peek().kind != "eof":
            token = self.peek()
            expected = stack[-1]
            if expected == "end":
                if token.kind == "kw" and token.value == "end":
                    stack.pop()
                    if not stack:
                        body_end = token.start
                        self.advance()
                        return self.source[body_start:body_end] if capture else ""
                    self.advance()
                    continue
                if token.kind == "kw" and token.value in _BLOCK_KEYWORDS:
                    stack.append("end")
                elif token.kind == "punct" and token.value == "{":
                    stack.append("}")
            else:
                if token.kind == "punct" and token.value == "}":
                    stack.pop()
                    if not stack:
                        body_end = token.start
                        self.advance()
                        return self.source[body_start:body_end] if capture else ""
                    self.advance()
                    continue
                if token.kind == "kw" and token.value in _BLOCK_KEYWORDS:
                    stack.append("end")
                elif token.kind == "punct" and token.value == "{":
                    stack.append("}")
            self.advance()
        raise FormulaParseError("unterminated cask block")

    def _apply(self, name: str, args: list[Any], kwargs: dict[Any, Any]) -> None:
        cask = self.cask
        if name == "version" and args:
            cask.version = str(args[0])
        elif name == "sha256":
            if args:
                cask.sha256 = str(args[0])
            elif kwargs:
                for key, value in kwargs.items():
                    if key in ("arm", "intel"):
                        cask.sha256_map[key] = str(value)
                if "arm" in kwargs and cask.resolved_arch() == "arm64":
                    cask.sha256 = str(kwargs["arm"])
                elif "intel" in kwargs and cask.resolved_arch() == "intel":
                    cask.sha256 = str(kwargs["intel"])
        elif name == "url" and args:
            cask.url = str(args[0])
        elif name == "name" and args:
            cask.name = str(args[0]) if cask.name is None else f"{cask.name}, {args[0]}"
        elif name == "desc" and args:
            cask.desc = str(args[0])
        elif name == "homepage" and args:
            cask.homepage = str(args[0])
        elif name == "auto_updates":
            cask.auto_updates = bool(args[0]) if args else True
        elif name == "arch":
            for key, value in kwargs.items():
                if key in ("arm", "intel"):
                    self._arch_map[key] = str(value)
                    cask.arch_map[key] = str(value)
            if "arm" in kwargs and "intel" in kwargs:
                cask.arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "intel"
        elif name in _ARTIFACT_KINDS:
            source = str(args[0]) if args else ""
            target = kwargs.get("target")
            cask.artifacts.append(Artifact(name, source, str(target) if target else None, kwargs))
        elif name == "container":
            raw_type = kwargs.get("type") or kwargs.get(":type")
            if raw_type is not None:
                cask.container = str(raw_type).lstrip(":")
        elif name == "stage_only":
            if args:
                cask.stage_only = not (
                    args[0] is False
                    or str(args[0]).strip().lower() in ("false", "0", "no")
                )
            else:
                cask.stage_only = True
        elif name == "rename":
            if len(args) >= 2:
                cask.renames.append((str(args[0]), str(args[1])))
        elif name == "uninstall":
            cask.uninstall.update({str(k): v for k, v in kwargs.items()})
            if args:
                cask.uninstall["_args"] = args
        elif name == "zap":
            trash = kwargs.get("trash") or kwargs.get(":trash")
            if isinstance(trash, list):
                cask.zap.extend(str(item) for item in trash)
            elif trash:
                cask.zap.append(str(trash))
        elif name == "os":
            for key, value in kwargs.items():
                if key in ("macos", "linux"):
                    cask.os[key] = value
        elif name == "depends_on":
            cask.depends_on.update({str(k): v for k, v in kwargs.items()})
        elif name == "conflicts_with":
            cask.conflicts_with.update({str(k): v for k, v in kwargs.items()})
        elif name == "caveats":
            if args:
                cask.caveats = str(args[0])
        elif name in ("deprecate!", "deprecate_args"):
            cask.deprecated = True
            reason = kwargs.get("because") or (args[0] if args else None)
            if reason is not None:
                cask.deprecation_reason = str(reason)
        elif name == "disable!":
            cask.disabled = True
            reason = kwargs.get("because") or (args[0] if args else None)
            if reason is not None:
                cask.disable_reason = str(reason)
        elif name == "livecheck":
            pass
        elif name == "no_autobump!":
            cask.no_autobump = True
            reason = kwargs.get("because") or (args[0] if args else None)
            if reason is not None:
                cask.no_autobump_reason = str(reason)
        else:
            cask.extra[name] = {"args": args, "kwargs": kwargs}


def parse_cask_source(source: str, path: Optional[Path] = None, tap: Optional[str] = None) -> Cask:
    return CaskParser(source, path=path, tap=tap).parse()


def parse_cask_file(path: str | Path, tap: Optional[str] = None) -> Cask:
    cask_path = Path(path)
    return parse_cask_source(cask_path.read_text(encoding="utf-8", errors="replace"), cask_path, tap)
