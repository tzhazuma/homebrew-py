"""A small executor for the Homebrew ``def install`` Ruby DSL.

This is deliberately not a Ruby interpreter.  It understands the common
declarative/build statements used by source formulae:

* ``system "cmd", *args`` with literal arguments, variables and helper args
  (``std_configure_args``, ``std_cmake_args``, ``std_meson_args``,
  ``std_cargo_args``);
* ``args = %w[...]``, ``args << "--x"`` (also ``+=``);
* ``inreplace``;
* ``ENV[...] = ...``, ``ENV.append/prepend_path``;
* ``bin.install`` / ``prefix.install`` / ``lib.install`` ... and
  ``bin.install_symlink``;
* ``cd ... do ... end``, ``mkdir``, ``mv``, ``cp``, ``rm_rf``, ``chmod``;
* ``on_macos`` / ``on_linux`` / ``on_arm`` / ``on_intel`` blocks and simple
  ``if`` conditions over ``OS``, ``Hardware::CPU``, ``build`` and literal
  ``args.include?``-style predicates;
* ``ENV["NAME"]`` reads in expression arguments and conditions.

Anything else raises :class:`UnsupportedInstall` with a line number instead of
silently producing an incomplete keg.

When ``PYBREW_ALLOW_ARBITRARY_RUBY=1`` is set, the raw ``def install`` body is
executed with the host Ruby interpreter instead; see
:mod:`pybrew.ruby_executor`.  That bridge is strictly opt-in.
"""

from __future__ import annotations

import glob
import hashlib
import os
import platform
import posixpath
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional

from .errors import BuildError, ChecksumMismatch, DownloadError, UnsupportedInstall
from .models import Formula, PatchSpec, is_linux, is_macos
from .paths import Config
from .ruby_executor import arbitrary_ruby_enabled, execute_install_body
from .ruby_parser import Lexer, Token

# ---------------------------------------------------------------------------
# expression AST
# ---------------------------------------------------------------------------


@dataclass
class Expr:
    pass


@dataclass
class Literal(Expr):
    value: Any


@dataclass
class StringExpr(Expr):
    template: str


@dataclass
class ArrayExpr(Expr):
    items: list[Expr] = field(default_factory=list)


@dataclass
class Spread(Expr):
    expr: Expr


@dataclass
class Var(Expr):
    name: str


@dataclass
class Helper(Expr):
    name: str


@dataclass
class PathJoin(Expr):
    base: Expr
    part: str


@dataclass
class MethodExpr(Expr):
    receiver: str
    method: str
    args: list[Expr] = field(default_factory=list)


@dataclass
class EnvRead(Expr):
    name: str


@dataclass
class UnknownExpr(Expr):
    text: str


# ---------------------------------------------------------------------------
# statements
# ---------------------------------------------------------------------------


@dataclass
class Action:
    pass


@dataclass
class SystemAction(Action):
    args: list[Expr]


@dataclass
class SetVar(Action):
    name: str
    value: Expr


@dataclass
class AppendVar(Action):
    name: str
    value: Expr


@dataclass
class EnvSet(Action):
    name: str
    value: Expr


@dataclass
class EnvPrependPath(Action):
    name: str
    value: Expr
    create: bool = False


@dataclass
class EnvDelete(Action):
    name: str


@dataclass
class DeparallelizeAction(Action):
    """Force MAKEFLAGS to a single job for the rest of the build."""


@dataclass
class InreplaceAction(Action):
    paths: Expr
    search: Expr
    replacement: Expr


@dataclass
class InstallFiles(Action):
    directory: str
    args: list[Expr]
    symlink: bool = False


@dataclass
class ShellOp(Action):
    kind: str
    args: list[Expr]


@dataclass
class CdAction(Action):
    path: Expr
    actions: list[Action]


@dataclass
class ResourceStageAction(Action):
    name: str
    actions: list[Action]


@dataclass
class ConditionalAction(Action):
    condition: bool
    actions: list[Action]


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

_INSTALL_DIRS = {
    "bin",
    "sbin",
    "lib",
    "include",
    "share",
    "etc",
    "libexec",
    "prefix",
    "man",
    "man1",
    "man2",
    "man3",
    "man4",
    "man5",
    "man6",
    "man7",
    "man8",
    "pkgshare",
    "elisp",
    "vim",
}

_HELPERS = {
    "std_configure_args",
    "std_cmake_args",
    "std_meson_args",
    "std_cargo_args",
    "std_service_path_env",
    "std_go_args",
    "std_pip_args",
}

_BLOCK_OPENERS = {"do", "case", "begin", "while", "until", "for", "def", "class", "module"}

_PLATFORM_BLOCKS = (
    "on_macos",
    "on_linux",
    "on_arm",
    "on_arm64",
    "on_intel",
    "on_x86_64",
)

_ARM_MACHINES = frozenset({"arm64", "aarch64"})
_INTEL_MACHINES = frozenset({"x86_64", "amd64", "i386", "i686"})

# Marker for values that cannot be resolved while parsing (e.g. interpolated
# strings); conditions that need them raise UnsupportedInstall instead of
# guessing a branch.
_UNKNOWN = object()


def _host_machine() -> str:
    return platform.machine().lower()


def _host_is_arm() -> bool:
    return _host_machine() in _ARM_MACHINES


def _host_is_intel() -> bool:
    return _host_machine() in _INTEL_MACHINES


def _platform_block_name(token: Token) -> Optional[str]:
    """Return the platform block name for ``token``, if it starts one."""

    if token.kind in ("id", "const", "kw") and str(token.value) in _PLATFORM_BLOCKS:
        return str(token.value)
    return None


def _opens_block(token: Token, prev: Optional[Token]) -> bool:
    if token.kind != "kw":
        return False
    if token.value in _BLOCK_OPENERS:
        return True
    if token.value in ("if", "unless"):
        return prev is None or prev.kind == "nl" or prev.value in (";", "then", "do", "else", "elsif", "begin")
    return False


def _extract_body_tokens(formula: Formula) -> list[Token]:
    source = formula.install_source or ""
    if not source.strip():
        raise UnsupportedInstall(f"{formula.name}: no def install block")
    tokens = Lexer(source).tokenise()
    start = None
    for index, token in enumerate(tokens):
        if token.kind == "kw" and token.value == "def":
            start = index
            break
    if start is None:
        raise UnsupportedInstall(f"{formula.name}: cannot find def install")
    index = start + 1
    while index < len(tokens) and not (tokens[index].kind == "nl"):
        index += 1
    body_start = index + 1
    depth = 1
    prev = tokens[index] if index < len(tokens) else tokens[start]
    index = body_start
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "eof":
            break
        if _opens_block(token, prev):
            depth += 1
        elif token.kind == "kw" and token.value == "end":
            depth -= 1
            if depth == 0:
                return tokens[body_start:index]
        prev = token
        index += 1
    raise UnsupportedInstall(f"{formula.name}: unterminated def install")


class _ExprParser:
    def __init__(self, parser: "InstallParser"):
        self.p = parser

    def peek(self, offset: int = 0) -> Token:
        return self.p.peek(offset)

    def advance(self) -> Token:
        return self.p.advance()

    def skip_newlines(self) -> None:
        while self.peek().kind == "nl" or (self.peek().kind == "punct" and self.peek().value == ";"):
            self.advance()

    def parse(self) -> Expr:
        token = self.peek()
        if token.kind == "str":
            self.advance()
            return StringExpr(str(token.value))
        if token.kind == "sym":
            self.advance()
            return Literal(str(token.value))
        if token.kind == "num":
            self.advance()
            return Literal(token.value)
        if token.kind == "words":
            self.advance()
            return ArrayExpr([StringExpr(str(item)) for item in token.value])
        if token.kind == "syms":
            self.advance()
            return ArrayExpr([Literal(str(item)) for item in token.value])
        if token.kind in ("op", "punct") and token.value == "*":
            self.advance()
            return Spread(self.parse())
        if token.kind == "punct" and token.value == "[":
            return self._parse_array()
        if token.kind == "punct" and token.value == "(":
            self.advance()
            value = self.parse()
            self.skip_newlines()
            if self.peek().kind == "punct" and self.peek().value == ")":
                self.advance()
            return value
        if token.kind == "punct" and token.value == "-" and self.peek(1).kind == "num":
            self.advance()
            number = self.advance()
            return Literal(-number.value)
        if token.kind == "id":
            name = str(self.advance().value)
            if name in ("true", "false", "nil"):
                return Literal({"true": True, "false": False, "nil": None}[name])
            if name in _HELPERS:
                return Helper(name)
            return self._parse_chain(name)
        if token.kind == "const":
            name = str(self.advance().value)
            return self._parse_const_chain(name)
        # Fallback: consume a token as opaque text.
        self.advance()
        return UnknownExpr(str(token.value))

    def _parse_array(self) -> Expr:
        self.advance()  # [
        items: list[Expr] = []
        while True:
            self.skip_newlines()
            if self.peek().kind == "punct" and self.peek().value == "]":
                self.advance()
                break
            if self.peek().kind == "eof":
                break
            items.append(self.parse())
            self.skip_newlines()
            if self.peek().kind == "punct" and self.peek().value == ",":
                self.advance()
                continue
            if self.peek().kind == "punct" and self.peek().value == "]":
                self.advance()
                break
            break
        return ArrayExpr(items)

    def _parse_chain(self, name: str) -> Expr:
        # Path join: prefix/"bin", opt_bin/"foo".
        if self.peek().kind == "punct" and self.peek().value == "/" and self.peek(1).kind == "str":
            self.advance()
            part = str(self.advance().value)
            return PathJoin(Var(name), part)
        if self.peek().kind == "punct" and self.peek().value == ".":
            self.advance()
            method = str(self.advance().value)
            args: list[Expr] = []
            if self.peek().kind == "punct" and self.peek().value == "(":
                args = self._parse_call_args()
            # path join after a method call is not supported, except method
            # results such as version.major_minor.
            return MethodExpr(name, method, args)
        if self.peek().kind == "punct" and self.peek().value == "[":
            # ENV["X"] etc. outside assignment is not supported as a value.
            return UnknownExpr(name)
        return Var(name)

    def _parse_const_chain(self, name: str) -> Expr:
        if name == "ENV" and self.peek().kind == "punct" and self.peek().value == "[":
            return self._parse_env_read()
        parts = [name]
        while self.peek().kind == "op" and self.peek().value == "::":
            self.advance()
            if self.peek().kind in ("const", "id"):
                parts.append(str(self.advance().value))
        if self.peek().kind == "punct" and self.peek().value == ".":
            self.advance()
            method = str(self.advance().value)
            receiver = "::".join(parts)
            args: list[Expr] = []
            if self.peek().kind == "punct" and self.peek().value == "(":
                args = self._parse_call_args()
            return MethodExpr(receiver, method, args)
        return UnknownExpr("::".join(parts))

    def _parse_env_read(self) -> Expr:
        """Parse ``ENV["NAME"]`` as a readable expression.

        Only literal variable names are understood; a dynamic lookup stays an
        opaque expression so the executor reports it explicitly.
        """

        self.advance()  # [
        token = self.peek()
        if token.kind in ("str", "sym"):
            name = str(self.advance().value)
            if self.peek().kind == "punct" and self.peek().value == "]":
                self.advance()
            return EnvRead(name)
        while not (
            self.peek().kind == "eof"
            or (self.peek().kind == "punct" and self.peek().value == "]")
        ):
            self.advance()
        if self.peek().kind == "punct" and self.peek().value == "]":
            self.advance()
        return UnknownExpr("ENV[...]")

    def _parse_call_args(self) -> list[Expr]:
        self.advance()  # (
        args: list[Expr] = []
        while True:
            self.skip_newlines()
            if self.peek().kind == "punct" and self.peek().value == ")":
                self.advance()
                break
            if self.peek().kind == "eof":
                break
            args.append(self.parse())
            self.skip_newlines()
            if self.peek().kind == "punct" and self.peek().value == ",":
                self.advance()
                continue
            if self.peek().kind == "punct" and self.peek().value == ")":
                self.advance()
                break
            break
        return args


class InstallParser:
    """Parse the token stream of a formula's ``def install`` into actions."""

    def __init__(self, formula: Formula):
        self.formula = formula
        self.tokens = _extract_body_tokens(formula)
        self.pos = 0
        self.variables: set[str] = set()
        # Values known while parsing, used by conditions such as
        # ``args.include?("--x")``.  They mirror, but do not replace, the
        # runtime values stored in BuildContext.variables.
        self.static_variables: dict[str, Any] = {}
        self.static_env: dict[str, Any] = {}

    # -- token helpers ---------------------------------------------------
    def peek(self, offset: int = 0) -> Token:
        index = self.pos + offset
        if not self.tokens or index >= len(self.tokens):
            return Token("eof", None, 0, 0, 0)
        return self.tokens[index]

    def advance(self) -> Token:
        if not self.tokens or self.pos >= len(self.tokens):
            return Token("eof", None, 0, 0, 0)
        token = self.tokens[self.pos]
        if token.kind != "eof":
            self.pos += 1
        return token

    def skip_newlines(self) -> None:
        while self.peek().kind == "nl" or (self.peek().kind == "punct" and self.peek().value == ";"):
            self.advance()

    def at_end(self) -> bool:
        return self.peek().kind == "eof" or (self.peek().kind == "kw" and self.peek().value == "end")

    def parse(self) -> list[Action]:
        actions = self._parse_statements(stop_end=False)
        self.skip_newlines()
        if self.peek().kind != "eof":
            raise UnsupportedInstall(
                f"{self.formula.name}: unsupported trailing install DSL at line {self.peek().line}"
            )
        return actions

    def _parse_statements(self, stop_end: bool = True) -> list[Action]:
        actions: list[Action] = []
        while True:
            self.skip_newlines()
            token = self.peek()
            if token.kind == "eof":
                if stop_end:
                    raise UnsupportedInstall(f"{self.formula.name}: unexpected end of install block")
                break
            if token.kind == "kw" and token.value == "end":
                self.advance()
                break
            if token.kind == "kw" and token.value in ("if", "unless"):
                actions.extend(self._parse_if(token.value == "unless"))
                continue
            platform_name = _platform_block_name(token)
            if platform_name is not None:
                actions.extend(self._parse_platform_block(platform_name))
                continue
            if token.kind == "kw" and token.value in ("begin", "case", "while", "until", "for", "def", "class", "module"):
                raise UnsupportedInstall(
                    f"{self.formula.name}: unsupported Ruby block {token.value!r} at line {token.line}"
                )
            actions.extend(self._parse_statement())
        return actions

    def _parse_statement(self) -> list[Action]:
        token = self.peek()
        self.skip_newlines()
        token = self.peek()
        if token.kind != "id" and token.kind != "const":
            raise UnsupportedInstall(
                f"{self.formula.name}: unsupported install statement {token.value!r} at line {token.line}"
            )
        name = str(token.value)
        actions: list[Action] = []

        if name == "ENV" and token.kind == "const":
            actions = [self._parse_env_statement()]
        elif name == "system":
            self.advance()
            actions = [SystemAction(self._parse_argument_list())]
        elif name == "deparallelize":
            self.advance()
            actions = [DeparallelizeAction()]
        elif name == "inreplace":
            self.advance()
            args = self._parse_argument_list()
            if len(args) < 3:
                raise UnsupportedInstall(
                    f"{self.formula.name}: inreplace form not supported near line {token.line}"
                )
            actions = [InreplaceAction(args[0], args[1], args[2])]
        elif name == "cd":
            self.advance()
            args = self._parse_argument_list(stop_at_do=True)
            if not args:
                raise UnsupportedInstall(f"{self.formula.name}: cd without a path")
            if not (self.peek().kind == "kw" and self.peek().value == "do"):
                raise UnsupportedInstall(f"{self.formula.name}: cd without a block is unsupported")
            self.advance()
            actions = [CdAction(args[0], self._parse_statements(stop_end=True))]
        elif name == "resource":
            self.advance()
            args = self._parse_argument_list()
            if not args:
                raise UnsupportedInstall(
                    f"{self.formula.name}: resource without a name"
                )
            static_name = self._static_expr_value(args[0])
            if not isinstance(static_name, str) or not static_name:
                raise UnsupportedInstall(
                    f"{self.formula.name}: resource name must be a static string"
                )
            resource_name = static_name
            if not (
                self.peek().kind == "punct"
                and self.peek().value == "."
                and self.peek(1).kind == "id"
                and self.peek(1).value == "stage"
            ):
                raise UnsupportedInstall(
                    f"{self.formula.name}: only resource(...).stage is supported"
                )
            self.advance()
            self.advance()
            if self.peek().kind == "punct" and self.peek().value == "(":
                self.advance()
                if self.peek().kind == "punct" and self.peek().value == ")":
                    self.advance()
            if not (self.peek().kind == "kw" and self.peek().value == "do"):
                raise UnsupportedInstall(
                    f"{self.formula.name}: resource.stage must use a do/end block"
                )
            self.advance()
            actions = [ResourceStageAction(resource_name, self._parse_statements(stop_end=True))]
        elif name in _INSTALL_DIRS and self._is_install_call():
            actions = [self._parse_install_call(name)]
        elif name == "mkdir":
            self.advance()
            actions = [ShellOp("mkdir", self._parse_argument_list())]
        elif name in ("mv", "cp"):
            self.advance()
            actions = [ShellOp(name, self._parse_argument_list())]
        elif name in ("rm_rf", "rm", "rmdir"):
            self.advance()
            actions = [ShellOp("rm", self._parse_argument_list())]
        elif name == "chmod":
            self.advance()
            actions = [ShellOp("chmod", self._parse_argument_list())]
        elif name == "touch":
            self.advance()
            actions = [ShellOp("touch", self._parse_argument_list())]
        elif name == "raise" or name == "odie":
            raise UnsupportedInstall(f"{self.formula.name}: install block calls {name}")
        else:
            # Assignment: `args = ...`, or variable append: `args << ...`.
            nxt = self.peek(1)
            if nxt.kind == "punct" and nxt.value == "=":
                self.advance()
                self.advance()
                value = _ExprParser(self).parse()
                self.variables.add(name)
                self.static_variables[name] = self._static_expr_value(value)
                actions = [SetVar(name, value)]
            elif nxt.kind == "op" and nxt.value in ("<<", "+="):
                self.advance()
                self.advance()
                value = _ExprParser(self).parse()
                self._record_static_append(name, value)
                actions = [AppendVar(name, value)]
            elif (
                nxt.kind == "punct"
                and nxt.value == "<"
                and self.peek(2).kind == "punct"
                and self.peek(2).value == "<"
            ):
                # The shared formula lexer emits ``<<`` as two '<' tokens.
                self.advance()
                self.advance()
                self.advance()
                value = _ExprParser(self).parse()
                self._record_static_append(name, value)
                actions = [AppendVar(name, value)]
            else:
                raise UnsupportedInstall(
                    f"{self.formula.name}: unsupported install statement {name!r} at line {token.line}"
                )

        # Trailing `if OS.mac?` / `unless ...` modifier.
        token = self.peek()
        if token.kind == "kw" and token.value in ("if", "unless"):
            condition = self._parse_condition()
            if token.value == "unless":
                condition = not condition
            actions = actions if condition else []
        return actions

    @staticmethod
    def _static_name(expr: Expr) -> str:
        if isinstance(expr, StringExpr):
            return expr.template
        if isinstance(expr, Literal):
            return str(expr.value)
        raise UnsupportedInstall("ENV variable name must be a literal string")

    def _parse_env_statement(self) -> Action:
        token = self.advance()  # ENV
        if self.peek().kind == "punct" and self.peek().value == "[":
            self.advance()
            name_token = self.advance()
            name = str(name_token.value)
            if self.peek().kind == "punct" and self.peek().value == "]":
                self.advance()
            if self.peek().kind == "punct" and self.peek().value == "=":
                self.advance()
                value = _ExprParser(self).parse()
                self.static_env[name] = self._static_expr_value(value)
                return EnvSet(name, value)
            raise UnsupportedInstall(
                f"{self.formula.name}: unsupported ENV access at line {token.line}"
            )
        if self.peek().kind == "punct" and self.peek().value == ".":
            self.advance()
            method = str(self.advance().value)
            if method == "append" or method == "prepend":
                args = self._parse_argument_list()
                if len(args) < 2:
                    raise UnsupportedInstall(f"{self.formula.name}: ENV.{method} needs two args")
                return (
                    EnvPrependPath(self._static_name(args[0]), args[1], create=False)
                    if method == "prepend"
                    else ShellOp("env_append", args)
                )
            if method in ("prepend_path", "prepend_create_path"):
                args = self._parse_argument_list()
                if len(args) < 2:
                    raise UnsupportedInstall(f"{self.formula.name}: ENV.{method} needs two args")
                return EnvPrependPath(
                    self._static_name(args[0]), args[1], create=method.endswith("create_path")
                )
            if method == "delete":
                args = self._parse_argument_list()
                if not args:
                    raise UnsupportedInstall(f"{self.formula.name}: ENV.delete needs a name")
                return EnvDelete(self._static_name(args[0]))
        raise UnsupportedInstall(
            f"{self.formula.name}: unsupported ENV statement at line {token.line}"
        )

    def _is_install_call(self) -> bool:
        return (
            self.peek(1).kind == "punct"
            and self.peek(1).value == "."
            and self.peek(2).kind == "id"
            and self.peek(2).value in ("install", "install_symlink", "write_exec_script")
        )

    def _parse_install_call(self, directory: str) -> Action:
        self.advance()  # directory
        self.advance()  # .
        method = str(self.advance().value)
        args = self._parse_argument_list()
        if method == "write_exec_script":
            return ShellOp("write_exec_script", [Literal(directory)] + args)
        return InstallFiles(directory, args, symlink=method == "install_symlink")

    def _parse_argument_list(self, stop_at_do: bool = False) -> list[Expr]:
        parser = _ExprParser(self)
        args: list[Expr] = []
        while True:
            token = self.peek()
            if token.kind in ("nl", "eof"):
                break
            if token.kind == "kw" and token.value in ("do", "end", "if", "unless"):
                break
            if token.kind == "punct" and token.value in ("]", "}"):
                break
            if token.kind == "punct" and token.value == ",":
                self.advance()
                continue
            args.append(parser.parse())
            token = self.peek()
            if token.kind == "punct" and token.value == ",":
                self.advance()
                continue
            if token.kind == "kw" and token.value in ("do", "end", "if", "unless"):
                break
            if token.kind == "nl":
                break
            # Continue only when the caller is inside an implicit list; most
            # statements end at the newline.
            break
        return args

    # -- conditionals ----------------------------------------------------
    def _parse_condition(self) -> bool:
        self.advance()  # if/unless
        return self._condition_expression()

    def _condition_expression(self) -> bool:
        expression = _ExprParser(self).parse()
        token = self.peek()
        if token.kind not in ("nl", "eof") and not (
            token.kind == "kw" and token.value == "then"
        ):
            raise UnsupportedInstall(
                f"{self.formula.name}: unsupported install condition operator "
                f"{token.value!r} near line {token.line}"
            )
        self._skip_to_line_end()
        return self._evaluate_condition(expression)

    def _skip_to_line_end(self) -> None:
        while self.peek().kind not in ("nl", "eof"):
            self.advance()
        if self.peek().kind == "nl":
            self.advance()

    def _evaluate_condition(self, expression: Expr) -> bool:
        if isinstance(expression, Literal) and isinstance(expression.value, bool):
            return expression.value
        if isinstance(expression, EnvRead):
            value = self._static_env_value(expression.name)
            if value is _UNKNOWN:
                raise UnsupportedInstall(
                    f"{self.formula.name}: cannot resolve ENV[{expression.name!r}] "
                    "in an install condition"
                )
            return value is not None
        if isinstance(expression, MethodExpr):
            receiver = expression.receiver
            method = expression.method
            if receiver == "OS" and method == "mac?":
                return is_macos()
            if receiver == "OS" and method == "linux?":
                return is_linux()
            if receiver == "Hardware::CPU" and method in ("arm?", "arm64?"):
                return _host_is_arm()
            if receiver == "Hardware::CPU" and method in ("intel?", "x86?"):
                return _host_is_intel()
            if receiver == "build" and method == "head?":
                return False
            if receiver == "build" and method == "stable?":
                return True
            # This port has no formula options, so optional features are off:
            # build.with? is false and build.without? is true.
            if receiver == "build" and method == "with?":
                return False
            if receiver == "build" and method == "without?":
                return True
            if receiver in self.static_variables and method in ("include?", "exclude?"):
                found = self._static_list_contains(receiver, expression)
                return found if method == "include?" else not found
        raise UnsupportedInstall(
            f"{self.formula.name}: unsupported install condition near line {self.peek().line}"
        )

    def _static_list_contains(self, name: str, expression: MethodExpr) -> bool:
        values = self.static_variables.get(name, _UNKNOWN)
        needle = (
            self._static_expr_value(expression.args[0])
            if len(expression.args) == 1
            else _UNKNOWN
        )
        if values is _UNKNOWN or needle is _UNKNOWN or not isinstance(values, list):
            raise UnsupportedInstall(
                f"{self.formula.name}: {name}.{expression.method} condition needs a "
                "statically known list variable"
            )
        return any(str(item) == str(needle) for item in values)

    def _static_expr_value(self, expression: Expr) -> Any:
        """Best-effort value of an expression at parse time (or ``_UNKNOWN``)."""

        if isinstance(expression, Literal):
            return expression.value
        if isinstance(expression, StringExpr):
            if "#{" in expression.template:
                return _UNKNOWN
            return expression.template
        if isinstance(expression, EnvRead):
            return self._static_env_value(expression.name)
        if isinstance(expression, ArrayExpr):
            values: list[Any] = []
            for item in expression.items:
                value = self._static_expr_value(item)
                if value is _UNKNOWN:
                    return _UNKNOWN
                if isinstance(value, list):
                    values.extend(value)
                else:
                    values.append(value)
            return values
        if isinstance(expression, Spread):
            return self._static_expr_value(expression.expr)
        if isinstance(expression, Var):
            return self.static_variables.get(expression.name, _UNKNOWN)
        return _UNKNOWN

    def _static_env_value(self, name: str) -> Any:
        if name in self.static_env:
            return self.static_env[name]
        return os.environ.get(name)

    def _record_static_append(self, name: str, expression: Expr) -> None:
        current = self.static_variables.get(name, _UNKNOWN)
        value = self._static_expr_value(expression)
        if current is _UNKNOWN or value is _UNKNOWN:
            self.static_variables[name] = _UNKNOWN
            return
        values = current if isinstance(current, list) else [current]
        added = value if isinstance(value, list) else [value]
        self.static_variables[name] = values + added

    def _parse_if(self, unless: bool) -> list[Action]:
        condition = self._parse_condition()
        if unless:
            condition = not condition
        return self._parse_if_chain(condition)

    def _parse_if_chain(self, condition: bool) -> list[Action]:
        selected: list[Action] = []
        current_condition = condition
        while True:
            self.skip_newlines()
            if current_condition:
                selected = self._parse_branch_body()
                self._skip_remaining_if()
                return selected
            self._skip_branch()
            token = self.peek()
            if token.kind == "kw" and token.value == "elsif":
                self.advance()
                current_condition = self._condition_expression()
                continue
            if token.kind == "kw" and token.value == "else":
                self.advance()
                self.skip_newlines()
                selected = self._parse_branch_body()
            if self.peek().kind == "kw" and self.peek().value == "end":
                self.advance()
            return selected

    def _parse_branch_body(self) -> list[Action]:
        """Parse statements until a sibling else/elsif/end (do not consume it)."""

        actions: list[Action] = []
        while True:
            self.skip_newlines()
            token = self.peek()
            if token.kind == "eof":
                raise UnsupportedInstall(f"{self.formula.name}: unterminated if in install")
            if token.kind == "kw" and token.value in ("else", "elsif", "end"):
                return actions
            if token.kind == "kw" and token.value in ("if", "unless"):
                actions.extend(self._parse_if(token.value == "unless"))
                continue
            platform_name = _platform_block_name(token)
            if platform_name is not None:
                actions.extend(self._parse_platform_block(platform_name))
                continue
            actions.extend(self._parse_statement())

    def _skip_remaining_if(self) -> None:
        """Consume any else/elsif arms plus the closing end of this if."""

        depth = 0
        prev: Optional[Token] = None
        while self.peek().kind != "eof":
            token = self.peek()
            if token.kind == "kw" and token.value == "end":
                if depth == 0:
                    self.advance()
                    return
                depth -= 1
            elif _opens_block(token, prev):
                depth += 1
            prev = self.advance()
        raise UnsupportedInstall(f"{self.formula.name}: unterminated if in install")

    def _skip_branch(self) -> None:
        """Skip tokens through else/elsif/end without consuming the keyword."""

        depth = 0
        prev: Optional[Token] = None
        while self.peek().kind != "eof":
            token = self.peek()
            if depth == 0 and token.kind == "kw" and token.value in ("else", "elsif", "end"):
                return
            if _opens_block(token, prev):
                depth += 1
            elif token.kind == "kw" and token.value == "end":
                depth -= 1
            prev = self.advance()
        raise UnsupportedInstall(f"{self.formula.name}: unterminated conditional in install")

    def _platform_active(self, name: str) -> bool:
        if name == "on_macos":
            return is_macos()
        if name == "on_linux":
            return is_linux()
        if name in ("on_arm", "on_arm64"):
            return _host_is_arm()
        if name in ("on_intel", "on_x86_64"):
            return _host_is_intel()
        return False

    def _parse_platform_block(self, name: str) -> list[Action]:
        self.advance()  # on_* platform block
        self._parse_argument_list(stop_at_do=True)
        if not (self.peek().kind == "kw" and self.peek().value == "do"):
            raise UnsupportedInstall(f"{self.formula.name}: {name} without do block")
        self.advance()
        if self._platform_active(name):
            return self._parse_statements(stop_end=True)
        self._skip_branch()
        if self.peek().kind == "kw" and self.peek().value == "end":
            self.advance()
        return []


# ---------------------------------------------------------------------------
# executor
# ---------------------------------------------------------------------------


@dataclass
class BuildContext:
    formula: Formula
    config: Config
    keg: Path
    build_root: Path
    verbose: bool = False
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path = field(default_factory=Path)
    variables: dict[str, Any] = field(default_factory=dict)

    def path_for(self, name: str) -> Path:
        mapping = {
            "prefix": self.keg,
            "bin": self.keg / "bin",
            "sbin": self.keg / "sbin",
            "lib": self.keg / "lib",
            "include": self.keg / "include",
            "share": self.keg / "share",
            "etc": self.keg / "etc",
            "libexec": self.keg / "libexec",
            "var": self.config.prefix / "var",
            "opt_prefix": self.config.opt_path(self.formula.name),
            "opt_bin": self.config.opt_path(self.formula.name) / "bin",
            "pkgshare": self.keg / "share" / self.formula.name,
            "appdir": self.config.applications,
            "staged_path": self.build_root,
            "buildpath": self.build_root,
        }
        return mapping.get(name, self.keg / name)

    def interpolate(self, template: str) -> str:
        def replace(match: re.Match[str]) -> str:
            expression = match.group(1).strip()
            return self.resolve_text_expression(expression)

        return re.sub(r"#\{([^{}]+)\}", replace, template)

    def resolve_text_expression(self, expression: str) -> str:
        version = self.formula.version or "HEAD"
        mapping = {
            "version": version,
            "prefix": str(self.keg),
            "bin": str(self.keg / "bin"),
            "sbin": str(self.keg / "sbin"),
            "lib": str(self.keg / "lib"),
            "include": str(self.keg / "include"),
            "share": str(self.keg / "share"),
            "etc": str(self.keg / "etc"),
            "var": str(self.config.prefix / "var"),
            "opt_prefix": str(self.config.opt_path(self.formula.name)),
            "opt_bin": str(self.config.opt_path(self.formula.name) / "bin"),
            "appdir": str(self.config.applications),
            "staged_path": str(self.build_root),
            "buildpath": str(self.build_root),
            "version.major": version.split(".")[0],
            "version.major_minor": ".".join(version.split(".")[:2]),
            "version.minor": version.split(".")[1] if "." in version else "0",
            "version.dots_to_underscores": version.replace(".", "_"),
            "version.dots_to_hyphens": version.replace(".", "-"),
        }
        if expression in mapping:
            return mapping[expression]
        if expression.startswith("ENV[") and expression.endswith("]"):
            key = expression[4:-1].strip().strip('"').strip("'")
            return self.env.get(key, "")
        raise UnsupportedInstall(
            f"{self.formula.name}: unsupported string interpolation #{{{expression}}}"
        )


# ---------------------------------------------------------------------------
# compiler selection / build environment
# ---------------------------------------------------------------------------

# Homebrew-style compiler families.  The first name is the C driver, the second
# the C++ driver; both are resolved with ``shutil.which`` inside the build
# environment's PATH so a fake toolchain in tests is honoured.
_COMPILER_DRIVERS: dict[str, tuple[str, str]] = {
    "clang": ("clang", "clang++"),
    "gcc": ("gcc", "g++"),
}

_INCLUDE_FLAG = "-I{prefix}/include"
_LIBRARY_FLAG = "-L{prefix}/lib"

# Bounded Superenv-style unsafe paths.  This is intentionally a small allow/deny
# list, not a reimplementation of Homebrew's full Superenv.
_SYSTEM_INCLUDE_ROOTS = ("/usr/include", "/usr/local/include")
_SYSTEM_LIBRARY_ROOTS = (
    "/usr/lib",
    "/usr/lib64",
    "/usr/local/lib",
    "/lib",
    "/lib64",
)
_SYSTEM_FRAMEWORK_ROOTS = (
    "/System/Library/Frameworks",
    "/Library/Frameworks",
    "/usr/local/lib",
)
_SYSTEM_TOOLCHAIN_ROOTS = (
    "/usr/bin",
    "/usr/lib",
    "/usr/libexec",
    "/usr/share",
    "/usr/local/bin",
    "/usr/local/lib",
)
_RPATH_PREFIXES = (
    "-Wl,-rpath,",
    "-Wl,-rpath=",
    "-Wl,-R,",
    "-Wl,-R=",
    "-Wl,-rpath-link,",
    "-Wl,-rpath-link=",
)


def _path_under(path: str, roots: Iterable[str]) -> bool:
    normalised = posixpath.normpath(path)
    if not normalised.startswith("/"):
        return False
    for root in roots:
        root = posixpath.normpath(root)
        if normalised == root or normalised.startswith(root.rstrip("/") + "/"):
            return True
    return False


def _flag_value(
    arguments: list[str], index: int
) -> Optional[tuple[str, str, int]]:
    """Classify a path-bearing compiler flag as ``(kind, path, consumed)``."""

    token = arguments[index]
    if token in ("-I", "-isystem"):
        if index + 1 < len(arguments):
            return "include", arguments[index + 1], 2
        return None
    if token.startswith("-I") and len(token) > 2:
        return "include", token[2:], 1
    if token.startswith("-isystem") and len(token) > len("-isystem"):
        return "include", token[len("-isystem") :], 1
    if token in ("-B", "-resource-dir", "--gcc-toolchain", "-gcc-toolchain"):
        if index + 1 < len(arguments):
            return "toolchain", arguments[index + 1], 2
        return None
    for prefix in ("-B", "-resource-dir=", "--gcc-toolchain=", "-gcc-toolchain="):
        if token.startswith(prefix) and len(token) > len(prefix):
            return "toolchain", token[len(prefix) :], 1
    if (
        token == "-Xclang"
        and index + 2 < len(arguments)
        and arguments[index + 1] in ("-I", "-isystem", "-F")
    ):
        kind = "framework" if arguments[index + 1] == "-F" else "include"
        return kind, arguments[index + 2], 3
    if token in ("-F", "-iframework"):
        if index + 1 < len(arguments):
            return "framework", arguments[index + 1], 2
        return None
    if token.startswith("-iframework") and len(token) > len("-iframework"):
        return "framework", token[len("-iframework") :], 1
    if token.startswith("-F") and len(token) > 2:
        return "framework", token[2:], 1
    if token in ("-iquote", "-idirafter"):
        if index + 1 < len(arguments):
            return "include", arguments[index + 1], 2
        return None
    if token.startswith("-iquote") and len(token) > len("-iquote"):
        return "include", token[len("-iquote") :], 1
    if token.startswith("-idirafter") and len(token) > len("-idirafter"):
        return "include", token[len("-idirafter") :], 1
    if token == "-isysroot":
        if index + 1 < len(arguments):
            return "sysroot", arguments[index + 1], 2
        return None
    if token.startswith("-isysroot=") and len(token) > len("-isysroot="):
        return "sysroot", token[len("-isysroot=") :], 1
    if token.startswith("-isysroot") and len(token) > len("-isysroot"):
        return "sysroot", token[len("-isysroot") :], 1
    if token == "--sysroot":
        if index + 1 < len(arguments):
            return "sysroot", arguments[index + 1], 2
        return None
    if token.startswith("--sysroot=") and len(token) > len("--sysroot="):
        return "sysroot", token[len("--sysroot=") :], 1
    if token == "-Wl,-syslibroot":
        if index + 1 < len(arguments):
            return "sysroot", arguments[index + 1], 2
        return None
    for prefix in ("-Wl,-syslibroot,", "-Wl,-syslibroot="):
        if token.startswith(prefix) and len(token) > len(prefix):
            return "sysroot", token[len(prefix) :], 1
    if token == "-L":
        if index + 1 < len(arguments):
            return "library", arguments[index + 1], 2
        return None
    if token.startswith("-L") and len(token) > 2:
        return "library", token[2:], 1
    if token in ("-Wl,-L", "-Wl,-L,") and index + 1 < len(arguments):
        following = arguments[index + 1]
        if following.startswith("-Wl,"):
            following = following[len("-Wl,") :]
        return "library", following, 2
    if token == "-Wl,-F" and index + 1 < len(arguments):
        following = arguments[index + 1]
        if following.startswith("-Wl,"):
            following = following[len("-Wl,") :]
        return "framework", following, 2
    for prefix in ("-Wl,-L,", "-Wl,-L="):
        if token.startswith(prefix) and len(token) > len(prefix):
            return "library", token[len(prefix) :], 1
    if (
        token == "-Xlinker"
        and index + 2 < len(arguments)
        and arguments[index + 1] == "-L"
    ):
        if index + 3 < len(arguments) and arguments[index + 2] == "-Xlinker":
            return "library", arguments[index + 3], 4
        return "library", arguments[index + 2], 3
    if token == "-Xlinker" and index + 3 < len(arguments):
        option = arguments[index + 1]
        kind = {
            "-L": "library",
            "-F": "framework",
            "-rpath": "rpath",
            "-R": "rpath",
            "-rpath-link": "rpath",
            "-I": "include",
            "-isystem": "include",
            "--sysroot": "sysroot",
        }.get(option)
        if kind is not None and arguments[index + 2] == "-Xlinker":
            return kind, arguments[index + 3], 4
    if token in ("-Xpreprocessor", "-Xassembler") and index + 2 < len(arguments):
        option = arguments[index + 1]
        if option in ("-I", "-isystem", "-F"):
            return (
                "framework" if option == "-F" else "include",
                arguments[index + 2],
                3,
            )
    if token in ("-Wl,--sysroot",) and index + 1 < len(arguments):
        following = arguments[index + 1]
        if following.startswith("-Wl,"):
            following = following[len("-Wl,") :]
        return "sysroot", following, 2
    for prefix in ("-Wl,--sysroot=", "-Wl,--sysroot,"):
        if token.startswith(prefix) and len(token) > len(prefix):
            return "sysroot", token[len(prefix) :], 1
    if token.startswith("-Wa,") and len(token) > len("-Wa,"):
        return "assembler", token[len("-Wa,") :], 1
    for prefix in _RPATH_PREFIXES:
        if token.startswith(prefix):
            return "rpath", token[len(prefix) :], 1
    if token in ("-Wl,-rpath", "-Wl,-R", "-Wl,-rpath-link") and (
        index + 1 < len(arguments)
    ):
        following = arguments[index + 1]
        if following.startswith("-Wl,"):
            following = following[len("-Wl,") :]
        return "rpath", following, 2
    return None


_SAFE_ASSEMBLER_FLAGS = frozenset(
    {
        "--32",
        "--64",
        "--fatal-warnings",
        "--no-warn",
        "--noexecstack",
        "--version",
        "--warn",
        "-EL",
        "-EB",
        "-adhln",
        "-al",
        "-an",
        "-fPIC",
        "-fpic",
        "-g",
        "-g0",
        "-g1",
        "-g2",
        "-g3",
        "-gdwarf-2",
        "-gdwarf-4",
        "-marm",
        "-mbig-endian",
        "-mlittle-endian",
        "-mthumb",
        "-mthumb-interwork",
        "-v",
        "-V",
    }
)
_SAFE_ASSEMBLER_PREFIXES = (
    "-a",
    "-g",
    "-march=",
    "-mcpu=",
    "-mfpu=",
    "-mabi=",
    "-mfloat-abi=",
)
_ASSEMBLER_DEFSYM_RE = re.compile(
    r"^[A-Za-z_.$][A-Za-z0-9_.$]*=.*$"
)


def _is_safe_assembler_flag(part: str) -> bool:
    return part in _SAFE_ASSEMBLER_FLAGS or any(
        part.startswith(prefix) for prefix in _SAFE_ASSEMBLER_PREFIXES
    )


def _sanitize_assembler_options(
    payload: str,
    diagnostics: Optional[list[str]] = None,
) -> str:
    """Return a bounded-safe ``-Wa,`` assembler payload.

    Only known-safe switches and ``--defsym`` definitions pass through.
    Include/library paths are preserved outside the system roots.  Anything
    else is dropped; when ``diagnostics`` is provided, a short reason is
    appended for each dropped option.
    """

    parts = payload.split(",")
    result: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part in ("-I", "-L"):
            if index + 1 < len(parts):
                path_value = parts[index + 1]
                roots = (
                    _SYSTEM_INCLUDE_ROOTS
                    if part == "-I"
                    else _SYSTEM_LIBRARY_ROOTS
                )
                if _path_under(os.path.normpath(path_value), roots):
                    if diagnostics is not None:
                        diagnostics.append(
                            f"dropped unsafe assembler path {part},{path_value}"
                        )
                else:
                    result.extend((part, path_value))
                index += 2
                continue
            if diagnostics is not None:
                diagnostics.append(
                    f"dropped assembler option {part} without a path"
                )
            index += 1
            continue
        if part.startswith("-I") and len(part) > 2:
            if _path_under(os.path.normpath(part[2:]), _SYSTEM_INCLUDE_ROOTS):
                if diagnostics is not None:
                    diagnostics.append(
                        f"dropped unsafe assembler path {part}"
                    )
            else:
                result.append(part)
            index += 1
            continue
        if part.startswith("-L") and len(part) > 2:
            if _path_under(os.path.normpath(part[2:]), _SYSTEM_LIBRARY_ROOTS):
                if diagnostics is not None:
                    diagnostics.append(
                        f"dropped unsafe assembler path {part}"
                    )
            else:
                result.append(part)
            index += 1
            continue
        if part in ("--defsym", "-defsym"):
            if (
                index + 1 < len(parts)
                and _ASSEMBLER_DEFSYM_RE.match(parts[index + 1])
            ):
                result.extend((part, parts[index + 1]))
                index += 2
                continue
            if diagnostics is not None:
                diagnostics.append(
                    f"dropped invalid assembler definition {part}"
                )
            index += 1
            continue
        if part.startswith("--defsym=") and _ASSEMBLER_DEFSYM_RE.match(
            part[len("--defsym=") :]
        ):
            result.append(part)
            index += 1
            continue
        if _is_safe_assembler_flag(part):
            result.append(part)
            index += 1
            continue
        if diagnostics is not None:
            diagnostics.append(f"dropped unsafe assembler option {part!r}")
        index += 1
    return ",".join(result)


_MAX_RESPONSE_FILE_BYTES = 1 << 20
_MAX_RESPONSE_EXPANSIONS = 32


def _read_response_file(raw_path: str) -> Optional[list[str]]:
    """Read a bounded response file; return None when it should be preserved."""

    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    try:
        if not path.is_file() or path.stat().st_size > _MAX_RESPONSE_FILE_BYTES:
            return None
        source = path.read_text(encoding="utf-8", errors="replace")
        return shlex.split(source)
    except (OSError, ValueError):
        return None


def sanitize_flag_list(
    arguments: Iterable[str],
    prefix: Path,
    *,
    drop_prefix_flags: bool = False,
    diagnostics: Optional[list[str]] = None,
) -> list[str]:
    """Return bounded-safe compiler flags.

    Removes system include/library roots, system rpaths and duplicate prefix
    include/library flags while preserving optimisation/debug/define flags.
    ``drop_prefix_flags`` is used by the generated shims because they add the
    keg include/lib paths themselves.
    """

    args = list(arguments)
    prefix_root = os.path.normpath(str(Path(prefix)))
    prefix_include = os.path.normpath(str(Path(prefix) / "include"))
    prefix_library = os.path.normpath(str(Path(prefix) / "lib"))
    result: list[str] = []
    seen_prefix: set[tuple[str, str]] = set()
    seen_sysroot: set[str] = set()
    response_expansions = 0
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("-Wl,@") and len(token) > len("-Wl,@"):
            expanded = _read_response_file(token[len("-Wl,@") :])
            if expanded is not None:
                if response_expansions >= _MAX_RESPONSE_EXPANSIONS:
                    if diagnostics is not None:
                        diagnostics.append(
                            f"response file expansion limit reached for {token}"
                        )
                    index += 1
                    continue
                args[index:index + 1] = [
                    f"-Wl,{part}" for part in expanded
                ]
                response_expansions += 1
                continue
        if token.startswith("@") and len(token) > 1:
            expanded = _read_response_file(token[1:])
            if expanded is not None:
                if response_expansions >= _MAX_RESPONSE_EXPANSIONS:
                    if diagnostics is not None:
                        diagnostics.append(
                            f"response file expansion limit reached for {token}"
                        )
                    index += 1
                    continue
                args[index:index + 1] = expanded
                response_expansions += 1
                continue
        parsed = _flag_value(args, index)
        if parsed is None:
            result.append(args[index])
            index += 1
            continue
        kind, path, consumed = parsed
        if kind == "assembler":
            cleaned = _sanitize_assembler_options(path, diagnostics)
            if cleaned:
                result.append("-Wa," + cleaned)
            index += consumed
            continue
        normalised = os.path.normpath(path)
        if kind == "include":
            unsafe = _path_under(normalised, _SYSTEM_INCLUDE_ROOTS)
            is_prefix = normalised == prefix_include
        elif kind == "framework":
            unsafe = _path_under(normalised, _SYSTEM_FRAMEWORK_ROOTS)
            is_prefix = False
        elif kind == "toolchain":
            unsafe = _path_under(normalised, _SYSTEM_TOOLCHAIN_ROOTS)
            is_prefix = False
        elif kind == "library":
            unsafe = _path_under(normalised, _SYSTEM_LIBRARY_ROOTS)
            is_prefix = normalised == prefix_library
        elif kind == "sysroot":
            # SDK paths intentionally live outside the keg.  Only deduplicate
            # equivalent -isysroot/-Wl,-syslibroot forms; never strip them.
            unsafe = False
            is_prefix = False
        else:
            unsafe = _path_under(normalised, _SYSTEM_LIBRARY_ROOTS) or (
                normalised.startswith("/")
                and not _path_under(normalised, (prefix_root,))
            )
            is_prefix = False
        if unsafe or (drop_prefix_flags and is_prefix):
            index += consumed
            continue
        if kind == "sysroot":
            if normalised in seen_sysroot:
                index += consumed
                continue
            seen_sysroot.add(normalised)
        elif is_prefix:
            key = (kind, normalised)
            if key in seen_prefix:
                index += consumed
                continue
            seen_prefix.add(key)
        result.extend(args[index : index + consumed])
        index += consumed
    return result


def sanitize_build_flags(
    flags: str,
    prefix: Path,
    *,
    drop_prefix_flags: bool = False,
    diagnostics: Optional[list[str]] = None,
) -> str:
    """Sanitize a whitespace-separated flag string for a build environment."""

    text = (flags or "").strip()
    if not text:
        return ""
    try:
        arguments = shlex.split(text)
    except ValueError:
        arguments = text.split()
    return shlex.join(
        sanitize_flag_list(
            arguments,
            prefix,
            drop_prefix_flags=drop_prefix_flags,
            diagnostics=diagnostics,
        )
    )


# Standalone helper written next to the shims.  It intentionally duplicates the
# bounded sanitizer above so the generated scripts work without importing
# pybrew (the build environment may be a clean shell).
_SUPERENV_HELPER_SOURCE = '''#!/usr/bin/env python3
"""Bounded Superenv shim helper generated by pybrew."""
import os
import re
import shlex
import sys

prefix, include, library, default_language, driver_cc, driver_cxx, driver_objc, driver_objcxx = sys.argv[1:9]
arguments = sys.argv[9:]
SYSTEM_INCLUDE_ROOTS = ("/usr/include", "/usr/local/include")
SYSTEM_LIBRARY_ROOTS = (
    "/usr/lib",
    "/usr/lib64",
    "/usr/local/lib",
    "/lib",
    "/lib64",
)
SYSTEM_FRAMEWORK_ROOTS = (
    "/System/Library/Frameworks",
    "/Library/Frameworks",
    "/usr/local/lib",
)
SYSTEM_TOOLCHAIN_ROOTS = (
    "/usr/bin",
    "/usr/lib",
    "/usr/libexec",
    "/usr/share",
    "/usr/local/bin",
    "/usr/local/lib",
)
RPATH_PREFIXES = (
    "-Wl,-rpath,",
    "-Wl,-rpath=",
    "-Wl,-R,",
    "-Wl,-R=",
    "-Wl,-rpath-link,",
    "-Wl,-rpath-link=",
)
PREFIX_ROOT = os.path.normpath(prefix)
PREFIX_INCLUDE = os.path.normpath(include)
PREFIX_LIBRARY = os.path.normpath(library)


def path_under(path, roots):
    normalised = os.path.normpath(path)
    if not normalised.startswith("/"):
        return False
    for root in roots:
        root = os.path.normpath(root)
        if normalised == root or normalised.startswith(root.rstrip("/") + "/"):
            return True
    return False


def flag_value(args, index):
    token = args[index]
    if token in ("-I", "-isystem"):
        if index + 1 < len(args):
            return "include", args[index + 1], 2
        return None
    if token.startswith("-I") and len(token) > 2:
        return "include", token[2:], 1
    if token.startswith("-isystem") and len(token) > len("-isystem"):
        return "include", token[len("-isystem"):], 1
    if token in ("-B", "-resource-dir", "--gcc-toolchain", "-gcc-toolchain"):
        if index + 1 < len(args):
            return "toolchain", args[index + 1], 2
        return None
    for flag_prefix in ("-B", "-resource-dir=", "--gcc-toolchain=", "-gcc-toolchain="):
        if token.startswith(flag_prefix) and len(token) > len(flag_prefix):
            return "toolchain", token[len(flag_prefix):], 1
    if (
        token == "-Xclang"
        and index + 2 < len(args)
        and args[index + 1] in ("-I", "-isystem", "-F")
    ):
        kind = "framework" if args[index + 1] == "-F" else "include"
        return kind, args[index + 2], 3
    if token in ("-F", "-iframework"):
        if index + 1 < len(args):
            return "framework", args[index + 1], 2
        return None
    if token.startswith("-iframework") and len(token) > len("-iframework"):
        return "framework", token[len("-iframework"):], 1
    if token.startswith("-F") and len(token) > 2:
        return "framework", token[2:], 1
    if token in ("-iquote", "-idirafter"):
        if index + 1 < len(args):
            return "include", args[index + 1], 2
        return None
    if token.startswith("-iquote") and len(token) > len("-iquote"):
        return "include", token[len("-iquote"):], 1
    if token.startswith("-idirafter") and len(token) > len("-idirafter"):
        return "include", token[len("-idirafter"):], 1
    if token == "-isysroot":
        if index + 1 < len(args):
            return "sysroot", args[index + 1], 2
        return None
    if token.startswith("-isysroot=") and len(token) > len("-isysroot="):
        return "sysroot", token[len("-isysroot="):], 1
    if token.startswith("-isysroot") and len(token) > len("-isysroot"):
        return "sysroot", token[len("-isysroot"):], 1
    if token == "--sysroot":
        if index + 1 < len(args):
            return "sysroot", args[index + 1], 2
        return None
    if token.startswith("--sysroot=") and len(token) > len("--sysroot="):
        return "sysroot", token[len("--sysroot="):], 1
    if token == "-Wl,-syslibroot":
        if index + 1 < len(args):
            return "sysroot", args[index + 1], 2
        return None
    for flag_prefix in ("-Wl,-syslibroot,", "-Wl,-syslibroot="):
        if token.startswith(flag_prefix) and len(token) > len(flag_prefix):
            return "sysroot", token[len(flag_prefix):], 1
    if token == "-L":
        if index + 1 < len(args):
            return "library", args[index + 1], 2
        return None
    if token.startswith("-L") and len(token) > 2:
        return "library", token[2:], 1
    if token in ("-Wl,-L", "-Wl,-L,") and index + 1 < len(args):
        following = args[index + 1]
        if following.startswith("-Wl,"):
            following = following[len("-Wl,"):]
        return "library", following, 2
    if token == "-Wl,-F" and index + 1 < len(args):
        following = args[index + 1]
        if following.startswith("-Wl,"):
            following = following[len("-Wl,"):]
        return "framework", following, 2
    for flag_prefix in ("-Wl,-L,", "-Wl,-L="):
        if token.startswith(flag_prefix) and len(token) > len(flag_prefix):
            return "library", token[len(flag_prefix):], 1
    if (
        token == "-Xlinker"
        and index + 2 < len(args)
        and args[index + 1] == "-L"
    ):
        if index + 3 < len(args) and args[index + 2] == "-Xlinker":
            return "library", args[index + 3], 4
        return "library", args[index + 2], 3
    if token == "-Xlinker" and index + 3 < len(args):
        option = args[index + 1]
        kind = {
            "-L": "library",
            "-F": "framework",
            "-rpath": "rpath",
            "-R": "rpath",
            "-rpath-link": "rpath",
            "-I": "include",
            "-isystem": "include",
            "--sysroot": "sysroot",
        }.get(option)
        if kind is not None and args[index + 2] == "-Xlinker":
            return kind, args[index + 3], 4
    if token in ("-Xpreprocessor", "-Xassembler") and index + 2 < len(args):
        option = args[index + 1]
        if option in ("-I", "-isystem", "-F"):
            return (
                "framework" if option == "-F" else "include",
                args[index + 2],
                3,
            )
    if token == "-Wl,--sysroot" and index + 1 < len(args):
        following = args[index + 1]
        if following.startswith("-Wl,"):
            following = following[len("-Wl,"):]
        return "sysroot", following, 2
    if token.startswith("-Wl,--sysroot=") and len(token) > len("-Wl,--sysroot="):
        return "sysroot", token[len("-Wl,--sysroot="):], 1
    if token.startswith("-Wl,--sysroot,") and len(token) > len("-Wl,--sysroot,"):
        return "sysroot", token[len("-Wl,--sysroot,"):], 1
    if token.startswith("-Wa,") and len(token) > len("-Wa,"):
        return "assembler", token[len("-Wa,"):], 1
    for flag_prefix in RPATH_PREFIXES:
        if token.startswith(flag_prefix):
            return "rpath", token[len(flag_prefix):], 1
    if token in ("-Wl,-rpath", "-Wl,-R", "-Wl,-rpath-link") and (
        index + 1 < len(args)
    ):
        following = args[index + 1]
        if following.startswith("-Wl,"):
            following = following[len("-Wl,"):]
        return "rpath", following, 2
    return None


SAFE_ASSEMBLER_FLAGS = frozenset(
    {
        "--32",
        "--64",
        "--fatal-warnings",
        "--no-warn",
        "--noexecstack",
        "--version",
        "--warn",
        "-EL",
        "-EB",
        "-adhln",
        "-al",
        "-an",
        "-fPIC",
        "-fpic",
        "-g",
        "-g0",
        "-g1",
        "-g2",
        "-g3",
        "-gdwarf-2",
        "-gdwarf-4",
        "-marm",
        "-mbig-endian",
        "-mlittle-endian",
        "-mthumb",
        "-mthumb-interwork",
        "-v",
        "-V",
    }
)
SAFE_ASSEMBLER_PREFIXES = (
    "-a",
    "-g",
    "-march=",
    "-mcpu=",
    "-mfpu=",
    "-mabi=",
    "-mfloat-abi=",
)
ASSEMBLER_DEFSYM_RE = re.compile(r"^[A-Za-z_.$][A-Za-z0-9_.$]*=.*$")


def is_safe_assembler_flag(part):
    return part in SAFE_ASSEMBLER_FLAGS or any(
        part.startswith(prefix) for prefix in SAFE_ASSEMBLER_PREFIXES
    )


def sanitize_assembler_options(payload, diagnostics):
    parts = payload.split(",")
    result = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part in ("-I", "-L"):
            if index + 1 < len(parts):
                path_value = parts[index + 1]
                roots = (
                    SYSTEM_INCLUDE_ROOTS
                    if part == "-I"
                    else SYSTEM_LIBRARY_ROOTS
                )
                if path_under(os.path.normpath(path_value), roots):
                    diagnostics.append(
                        "dropped unsafe assembler path %s,%s"
                        % (part, path_value)
                    )
                else:
                    result.extend((part, path_value))
                index += 2
                continue
            diagnostics.append(
                "dropped assembler option %s without a path" % part
            )
            index += 1
            continue
        if part.startswith("-I") and len(part) > 2:
            if path_under(os.path.normpath(part[2:]), SYSTEM_INCLUDE_ROOTS):
                diagnostics.append(
                    "dropped unsafe assembler path %s" % part
                )
            else:
                result.append(part)
            index += 1
            continue
        if part.startswith("-L") and len(part) > 2:
            if path_under(os.path.normpath(part[2:]), SYSTEM_LIBRARY_ROOTS):
                diagnostics.append(
                    "dropped unsafe assembler path %s" % part
                )
            else:
                result.append(part)
            index += 1
            continue
        if part in ("--defsym", "-defsym"):
            if (
                index + 1 < len(parts)
                and ASSEMBLER_DEFSYM_RE.match(parts[index + 1])
            ):
                result.extend((part, parts[index + 1]))
                index += 2
                continue
            diagnostics.append(
                "dropped invalid assembler definition %s" % part
            )
            index += 1
            continue
        if part.startswith("--defsym=") and ASSEMBLER_DEFSYM_RE.match(
            part[len("--defsym="):]
        ):
            result.append(part)
            index += 1
            continue
        if is_safe_assembler_flag(part):
            result.append(part)
            index += 1
            continue
        diagnostics.append("dropped unsafe assembler option %r" % part)
        index += 1
    return ",".join(result)


SOURCE_LANGUAGES = {
    ".c": "c",
    ".cc": "c++",
    ".cpp": "c++",
    ".cxx": "c++",
    ".m": "objc",
    ".mm": "objc++",
    ".s": "assembler",
    ".asm": "assembler",
}
EXPLICIT_LANGUAGES = {
    "c": "c",
    "c-header": "c",
    "cpp-output": "c",
    "c++": "c++",
    "c++-header": "c++",
    "c++-cpp-output": "c++",
    "objective-c": "objc",
    "objective-c-header": "objc",
    "objective-c-cpp-output": "objc",
    "objective-c++": "objc++",
    "objective-c++-header": "objc++",
    "objective-c++-cpp-output": "objc++",
    "assembler": "assembler",
    "assembler-with-cpp": "assembler-with-cpp",
}
OPTIONS_WITH_VALUES = frozenset(
    {
        "-o",
        "-I",
        "-L",
        "-isystem",
        "-isysroot",
        "-include",
        "-imacros",
        "-MF",
        "-MT",
        "-MQ",
        "-arch",
        "-target",
        "--sysroot",
        "-Xclang",
        "-Xlinker",
        "-u",
        "-T",
    }
)


def detect_language(args, default):
    """Return (language, explicit_x_seen, mixed_sources).

    The first recognized source extension selects the driver.  When several
    distinct source languages are present in one invocation the compiler is
    allowed to infer each file's language (no -x is injected); an explicit -x
    language overrides extension detection entirely.
    """

    explicit_seen = False
    explicit_language = None
    extension_language = None
    extension_languages = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "-x":
            explicit_seen = True
            if index + 1 < len(args):
                explicit_language = EXPLICIT_LANGUAGES.get(
                    args[index + 1].strip().lower()
                )
            index += 2
            continue
        if token.startswith("-x") and len(token) > 2:
            explicit_seen = True
            explicit_language = EXPLICIT_LANGUAGES.get(
                token[2:].strip().lower()
            )
            index += 1
            continue
        if token in OPTIONS_WITH_VALUES:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        suffix = os.path.splitext(token)[1]
        source_language = (
            "assembler-with-cpp"
            if suffix == ".S"
            else SOURCE_LANGUAGES.get(suffix.lower())
        )
        if source_language is not None:
            if extension_language is None:
                extension_language = source_language
            if source_language not in extension_languages:
                extension_languages.append(source_language)
        index += 1
    if explicit_seen:
        return explicit_language or default, True, False
    if extension_language is not None:
        return (
            extension_language,
            False,
            len(extension_languages) > 1,
        )
    return default, False, False


language, explicit_language, mixed_sources = detect_language(
    arguments, default_language
)
language_flags = []
if not explicit_language and not mixed_sources:
    if language == "objc":
        language_flags = ["-x", "objective-c"]
    elif language == "objc++":
        language_flags = ["-x", "objective-c++"]
    elif language == "assembler":
        language_flags = ["-x", "assembler"]
    elif language == "assembler-with-cpp":
        language_flags = ["-x", "assembler-with-cpp"]
driver_spec = {
    "c": driver_cc,
    "c++": driver_cxx,
    "objc": driver_objc,
    "objc++": driver_objcxx,
    "assembler": driver_cc,
    "assembler-with-cpp": driver_cc,
}.get(language, driver_cc)

MAX_RESPONSE_FILE_BYTES = 1 << 20
MAX_RESPONSE_EXPANSIONS = 32


def read_response_file(raw_path):
    if not raw_path:
        return None
    try:
        response_path = os.path.expanduser(raw_path)
        if not os.path.isfile(response_path):
            return None
        if os.path.getsize(response_path) > MAX_RESPONSE_FILE_BYTES:
            return None
        with open(response_path, "r", encoding="utf-8", errors="replace") as handle:
            return shlex.split(handle.read())
    except (OSError, ValueError):
        return None


diagnostics = []
filtered = []
seen_sysroot = set()
response_expansions = 0
index = 0
while index < len(arguments):
    token = arguments[index]
    if token.startswith("-Wl,@") and len(token) > len("-Wl,@"):
        expanded = read_response_file(token[len("-Wl,@"):])
        if expanded is not None:
            if response_expansions >= MAX_RESPONSE_EXPANSIONS:
                diagnostics.append(
                    "response file expansion limit reached for %s" % token
                )
                index += 1
                continue
            arguments[index:index + 1] = [
                "-Wl," + part for part in expanded
            ]
            response_expansions += 1
            continue
    if token.startswith("@") and len(token) > 1:
        expanded = read_response_file(token[1:])
        if expanded is not None:
            if response_expansions >= MAX_RESPONSE_EXPANSIONS:
                diagnostics.append(
                    "response file expansion limit reached for %s" % token
                )
                index += 1
                continue
            arguments[index:index + 1] = expanded
            response_expansions += 1
            continue
    parsed = flag_value(arguments, index)
    if parsed is None:
        filtered.append(arguments[index])
        index += 1
        continue
    kind, path, consumed = parsed
    if kind == "assembler":
        cleaned = sanitize_assembler_options(path, diagnostics)
        if cleaned:
            filtered.append("-Wa," + cleaned)
        index += consumed
        continue
    normalised = os.path.normpath(path)
    if kind == "include":
        unsafe = path_under(normalised, SYSTEM_INCLUDE_ROOTS)
        duplicate_prefix = normalised == PREFIX_INCLUDE
    elif kind == "framework":
        unsafe = path_under(normalised, SYSTEM_FRAMEWORK_ROOTS)
        duplicate_prefix = False
    elif kind == "toolchain":
        unsafe = path_under(normalised, SYSTEM_TOOLCHAIN_ROOTS)
        duplicate_prefix = False
    elif kind == "library":
        unsafe = path_under(normalised, SYSTEM_LIBRARY_ROOTS)
        duplicate_prefix = normalised == PREFIX_LIBRARY
    elif kind == "sysroot":
        unsafe = False
        duplicate_prefix = False
    else:
        unsafe = path_under(normalised, SYSTEM_LIBRARY_ROOTS) or (
            normalised.startswith("/")
            and not path_under(normalised, (PREFIX_ROOT,))
        )
        duplicate_prefix = False
    if unsafe or duplicate_prefix:
        index += consumed
        continue
    if kind == "sysroot":
        if normalised in seen_sysroot:
            index += consumed
            continue
        seen_sysroot.add(normalised)
    filtered.extend(arguments[index:index + consumed])
    index += consumed

for message in diagnostics:
    print("pybrew superenv: %s" % message, file=sys.stderr)

try:
    driver_argv = shlex.split(driver_spec)
except ValueError:
    driver_argv = [driver_spec]
if not driver_argv:
    sys.exit("empty compiler command")
os.execvp(
    driver_argv[0],
    [
        *driver_argv,
        "-isystem",
        include,
        "-L",
        library,
        *language_flags,
        *filtered,
    ],
)
'''


@dataclass(frozen=True)
class CompilerSelection:
    """The compilers selected for one source build.

    ``cc``/``cxx`` are ``None`` when the build should keep using the
    toolchain's defaults (i.e. neither ``CC``/``CXX`` nor a requested
    ``PYBREW_COMPILER`` provided a usable driver).
    """

    cc: Optional[str] = None
    cxx: Optional[str] = None
    family: Optional[str] = None
    warning: Optional[str] = None
    cc_version: Optional[str] = None
    cxx_version: Optional[str] = None
    target: Optional[str] = None
    cc_family: Optional[str] = None
    cxx_family: Optional[str] = None
    detected_family: Optional[str] = None


@dataclass(frozen=True)
class SuperenvShims:
    """Generated opt-in compiler shims (a bounded Superenv compatibility layer)."""

    root: Path
    cc: Path
    cxx: Path
    helper: Path
    objc: Optional[Path] = None
    objcxx: Optional[Path] = None


@dataclass(frozen=True)
class BuildEnvironment:
    """A fully prepared source-build environment plus its compiler choice."""

    env: dict[str, str]
    compiler: CompilerSelection
    superenv: Optional[SuperenvShims] = None
    warnings: tuple[str, ...] = ()


def _first_environment_value(
    environment: Mapping[str, str], names: Iterable[str]
) -> Optional[str]:
    """Return the first non-empty value among ``names`` (in priority order)."""

    for name in names:
        value = environment.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _which_in(environment: Mapping[str, str], command: str) -> Optional[str]:
    """Resolve ``command`` against the build environment's PATH.

    ``shutil.which`` defaults to the *host* PATH; a source build may carry an
    explicit environment (tests, callers), so look it up there instead.
    """

    search_path = environment.get("PATH") or os.defpath
    return shutil.which(command, path=search_path)


def _probe_compiler(
    environment: Mapping[str, str], compiler: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Best-effort (version, target) probe for one compiler specification."""

    if not compiler:
        return None, None
    argv = split_compiler_command(compiler)
    if not argv:
        return None, None

    def run(flag: str) -> Optional[str]:
        try:
            result = subprocess.run(
                [*argv, flag],
                env=dict(environment),
                cwd=tempfile.gettempdir(),
                text=True,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        lines = (result.stdout or "").strip().splitlines()
        if not lines:
            return None
        value = lines[0].strip()
        return value[:200] or None

    return run("--version"), run("-dumpmachine")


def _classify_compiler(version: Optional[str]) -> Optional[str]:
    if not version:
        return None
    lowered = version.lower()
    if "apple clang" in lowered:
        return "apple-clang"
    if "clang" in lowered:
        return "clang"
    if "gcc" in lowered or "free software foundation" in lowered:
        return "gcc"
    if lowered.startswith("cc ") or lowered == "cc":
        return "cc"
    return None


def _compiler_host_warning(target: Optional[str]) -> Optional[str]:
    if not target:
        return None
    lowered = target.lower()
    host = platform.system()
    if host == "Darwin" and not any(
        marker in lowered for marker in ("darwin", "apple")
    ):
        return (
            f"compiler target {target!r} does not look like a macOS target; "
            "the selected compiler may be a cross-toolchain"
        )
    if host == "Linux" and "linux" not in lowered:
        return (
            f"compiler target {target!r} does not look like a Linux target; "
            "the selected compiler may be a cross-toolchain"
        )
    return None


def _finalize_compiler_selection(
    environment: Mapping[str, str],
    *,
    cc: Optional[str],
    cxx: Optional[str],
    family: Optional[str] = None,
    warning: Optional[str] = None,
) -> CompilerSelection:
    if cc is None and cxx is None:
        return CompilerSelection(
            cc=cc, cxx=cxx, family=family, warning=warning
        )
    cc_version, cc_target = _probe_compiler(environment, cc)
    cxx_version, cxx_target = _probe_compiler(environment, cxx)
    target = cc_target or cxx_target
    if warning is None:
        warning = _compiler_host_warning(target)
    cc_family = _classify_compiler(cc_version)
    cxx_family = _classify_compiler(cxx_version)
    return CompilerSelection(
        cc=cc,
        cxx=cxx,
        family=family,
        warning=warning,
        cc_version=cc_version,
        cxx_version=cxx_version,
        target=target,
        cc_family=cc_family,
        cxx_family=cxx_family,
        detected_family=cc_family or cxx_family,
    )


def select_compiler(
    environment: Optional[Mapping[str, str]] = None,
) -> CompilerSelection:
    """Resolve the compiler choice from environment variables.

    Explicit values always win and are never replaced:

    * ``PYBREW_CC`` (then ``CC``) selects the C compiler;
    * ``PYBREW_CXX`` (then ``CXX``) selects the C++ compiler;
    * ``PYBREW_COMPILER=clang|gcc`` fills in whichever of the two is still
      unset, but only when that family's driver is available.

    An unavailable/unknown ``PYBREW_COMPILER`` is a soft failure: the function
    returns a :class:`CompilerSelection` with ``warning`` set and the default
    toolchain is used.
    """

    env = os.environ if environment is None else environment
    cc = _first_environment_value(env, ("PYBREW_CC", "CC"))
    cxx = _first_environment_value(env, ("PYBREW_CXX", "CXX"))
    requested = (env.get("PYBREW_COMPILER") or "").strip().lower()
    if not requested:
        return _finalize_compiler_selection(env, cc=cc, cxx=cxx)

    if requested not in _COMPILER_DRIVERS:
        return _finalize_compiler_selection(
            env,
            cc=cc,
            cxx=cxx,
            warning=(
                f"PYBREW_COMPILER={requested!r} is not supported "
                "(expected 'clang' or 'gcc'); using the default compiler"
            ),
        )

    # Both channels are explicitly configured: PYBREW_COMPILER has nothing
    # left to fill in, so do not validate or replace the user's choice.
    if cc is not None and cxx is not None:
        return _finalize_compiler_selection(
            env, cc=cc, cxx=cxx, family=requested
        )

    c_driver, cxx_driver = _COMPILER_DRIVERS[requested]
    available_cc = _which_in(env, c_driver)
    available_cxx = _which_in(env, cxx_driver)
    if cc is None and available_cc is None:
        return _finalize_compiler_selection(
            env,
            cc=cc,
            cxx=cxx,
            warning=(
                f"PYBREW_COMPILER={requested} was requested but {c_driver} is "
                "not available; using the default compiler"
            ),
        )

    warning: Optional[str] = None
    if cc is None:
        cc = available_cc
    if cxx is None:
        if available_cxx is not None:
            cxx = available_cxx
        else:
            warning = (
                f"PYBREW_COMPILER={requested} was requested but {cxx_driver} "
                "is not available; CXX will use the default toolchain"
            )
    return _finalize_compiler_selection(
        env,
        cc=cc,
        cxx=cxx,
        family=requested,
        warning=warning,
    )


def _merge_flags(existing: str, additions: Iterable[str]) -> str:
    """Append build flags to ``existing`` without dropping user values."""

    result = (existing or "").strip()
    try:
        parts = shlex.split(result)
    except ValueError:
        parts = result.split()
    for addition in additions:
        rendered = (
            shlex.quote(addition)
            if any(character.isspace() for character in addition)
            else addition
        )
        if addition not in parts and rendered not in parts:
            result = f"{result} {rendered}".strip()
            parts.append(addition)
    return result


# Bounded language-standard support.  These are the standards clang and gcc
# commonly accept; unknown values are ignored rather than passed through.
_C_STANDARDS = frozenset(
    {
        "c89",
        "c90",
        "c99",
        "c11",
        "c17",
        "c18",
        "c2x",
        "gnu89",
        "gnu90",
        "gnu99",
        "gnu11",
        "gnu17",
        "gnu18",
        "gnu2x",
    }
)
_C_STANDARD_ALIASES = {
    "c9x": "c99",
    "c1x": "c11",
    "gnu9x": "gnu99",
    "gnu1x": "gnu11",
}

_CXX_STANDARD_ALIASES = {
    "c++0x": "c++11",
    "c++1y": "c++14",
    "c++1z": "c++17",
    "gnu++0x": "gnu++11",
    "gnu++1y": "gnu++14",
    "gnu++1z": "gnu++17",
}

_CXX_STANDARDS = frozenset(
    {
        "c++98",
        "c++03",
        "c++11",
        "c++14",
        "c++17",
        "c++20",
        "c++23",
        "c++26",
        "c++2a",
        "c++2b",
        "gnu++98",
        "gnu++03",
        "gnu++11",
        "gnu++14",
        "gnu++17",
        "gnu++20",
        "gnu++23",
        "gnu++2a",
        "gnu++2b",
    }
)


def _flag_tokens(flags: str) -> list[str]:
    try:
        return shlex.split(flags or "")
    except ValueError:
        return (flags or "").split()


def _has_flag(
    flags: str, names: Iterable[str], prefixes: Iterable[str] = ()
) -> bool:
    names = tuple(names)
    prefixes = tuple(prefixes)
    for token in _flag_tokens(flags):
        if token in names or any(token.startswith(prefix) for prefix in prefixes):
            return True
    return False


def _append_flag(flags: str, flag: str) -> str:
    return f"{flags} {flag}".strip() if flags else flag


def language_standard_flags(
    environment: Optional[Mapping[str, str]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Return bounded ``(c_standard, cxx_standard)`` from environment vars.

    Supported knobs are ``PYBREW_C_STANDARD``/``PYBREW_CSTD`` and
    ``PYBREW_CXX_STANDARD``/``PYBREW_CXXSTD``.  Values may include a leading
    ``-std=``; unsupported values are ignored.
    """

    env = os.environ if environment is None else environment

    def resolve(names: Iterable[str], allowed: frozenset[str]) -> Optional[str]:
        raw = _first_environment_value(env, names)
        if not raw:
            return None
        value = raw.strip().lower()
        if value.startswith("-std="):
            value = value[len("-std=") :]
        if allowed is _CXX_STANDARDS:
            value = _CXX_STANDARD_ALIASES.get(value, value)
        elif allowed is _C_STANDARDS:
            value = _C_STANDARD_ALIASES.get(value, value)
        return value if value in allowed else None

    return (
        resolve(("PYBREW_C_STANDARD", "PYBREW_CSTD"), _C_STANDARDS),
        resolve(("PYBREW_CXX_STANDARD", "PYBREW_CXXSTD"), _CXX_STANDARDS),
    )


def _ensure_language_standard(flags: str, standard: str) -> str:
    # Explicit user -std flags always win, even if they differ from the knob.
    if _has_flag(flags, ("-std",), prefixes=("-std=",)):
        return flags
    return _append_flag(flags, f"-std={standard}")


_MACOS_ARCH_ALIASES = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
}
_LINUX_ARCH_FLAGS = {
    "x86_64": "-m64",
    "amd64": "-m64",
    "i386": "-m32",
    "i686": "-m32",
    "x86": "-m32",
}


_MINIMUM_MACOS_BY_ARCH = {
    "arm64": "11.0",
    "aarch64": "11.0",
    "x86_64": "10.13",
    "amd64": "10.13",
}
_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,2}$")


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def minimum_platform_version(
    environment: Optional[Mapping[str, str]] = None,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
) -> Optional[str]:
    """Return a bounded macOS deployment target from the arch/version matrix.

    Only an explicit ``PYBREW_MIN_OS``/``MACOSX_DEPLOYMENT_TARGET`` enables this;
    the architecture floor is applied when the request is lower (arm64 11.0,
    x86_64 10.13).  Non-macOS hosts have no minimum-version matrix here.
    """

    env = os.environ if environment is None else environment
    host = platform.system() if system is None else system
    if host != "Darwin":
        return None
    raw = _first_environment_value(
        env, ("PYBREW_MIN_OS", "MACOSX_DEPLOYMENT_TARGET")
    )
    if raw is None or not _VERSION_RE.fullmatch(raw):
        return None
    arch = (
        _first_environment_value(env, ("PYBREW_ARCH",)) or machine or platform.machine()
    ).lower()
    floor = _MINIMUM_MACOS_BY_ARCH.get(arch)
    if floor is not None and _version_tuple(raw) < _version_tuple(floor):
        return floor
    return raw


def minimum_version_warning(
    environment: Optional[Mapping[str, str]] = None,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
) -> Optional[str]:
    env = os.environ if environment is None else environment
    host = platform.system() if system is None else system
    if host != "Darwin":
        return None
    explicit = _first_environment_value(
        env, ("PYBREW_MIN_OS", "MACOSX_DEPLOYMENT_TARGET")
    )
    if explicit is None or not _VERSION_RE.fullmatch(explicit):
        return None
    arch = (
        _first_environment_value(env, ("PYBREW_ARCH",)) or machine or platform.machine()
    ).lower()
    floor = _MINIMUM_MACOS_BY_ARCH.get(arch)
    if floor is not None and _version_tuple(explicit) < _version_tuple(floor):
        return (
            f"minimum macOS {explicit} is below the {arch} floor {floor}; "
            "using the architecture floor"
        )
    return None


def _environment_target_triple(environment: Mapping[str, str]) -> Optional[str]:
    explicit = _first_environment_value(environment, ("PYBREW_TARGET",))
    if explicit:
        return explicit
    for name in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
        try:
            tokens = shlex.split(environment.get(name, ""))
        except ValueError:
            tokens = environment.get(name, "").split()
        for index, token in enumerate(tokens):
            if token in ("-target", "--target") and index + 1 < len(tokens):
                return tokens[index + 1]
            if token.startswith("--target=") and len(token) > len("--target="):
                return token[len("--target="):]
            if token.startswith("-target=") and len(token) > len("-target="):
                return token[len("-target="):]
    return None


def cross_target_warning(
    environment: Optional[Mapping[str, str]] = None,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
) -> Optional[str]:
    """Warn when an explicit target triple conflicts with the host/arch matrix."""

    env = os.environ if environment is None else environment
    triple = _environment_target_triple(env)
    if not triple:
        return None
    host = platform.system() if system is None else system
    expected_arch = (
        _first_environment_value(env, ("PYBREW_ARCH",)) or machine or platform.machine()
    ).lower()
    expected_arch = {"aarch64": "arm64", "amd64": "x86_64"}.get(
        expected_arch, expected_arch
    )
    triple_arch = triple.split("-", 1)[0].lower()
    triple_arch = {"aarch64": "arm64", "amd64": "x86_64"}.get(
        triple_arch, triple_arch
    )
    lowered = triple.lower()
    expected_os = "darwin" if host == "Darwin" else "linux"
    os_matches = (
        any(part in lowered for part in ("darwin", "apple"))
        if expected_os == "darwin"
        else "linux" in lowered
    )
    if triple_arch != expected_arch or not os_matches:
        return (
            f"target triple {triple!r} is not in the host/arch matrix "
            f"(expected {expected_arch}/{expected_os})"
        )
    return None


def architecture_flag(
    environment: Optional[Mapping[str, str]] = None,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
) -> Optional[str]:
    """Return a bounded PYBREW_ARCH flag for Darwin/Linux, if safe.

    macOS accepts ``-arch arm64|x86_64``.  On Linux only the x86 family has a
    safe bounded mapping (``-m64``/``-m32``); non-x86 hosts return ``None``.
    ``system``/``machine`` are injectable for deterministic tests.
    """

    env = os.environ if environment is None else environment
    raw = env.get("PYBREW_ARCH")
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    host = platform.system() if system is None else system
    host_machine = (
        platform.machine() if machine is None else machine
    ).strip().lower()
    if host == "Darwin":
        arch = _MACOS_ARCH_ALIASES.get(value)
        return f"-arch {arch}" if arch else None
    if host == "Linux":
        flag = _LINUX_ARCH_FLAGS.get(value)
        if flag is None or host_machine not in _INTEL_MACHINES:
            return None
        return flag
    return None


_SOURCE_LANGUAGE_EXTENSIONS = {
    ".c": "c",
    ".cc": "c++",
    ".cpp": "c++",
    ".cxx": "c++",
    ".m": "objc",
    ".mm": "objc++",
    ".s": "assembler",
    ".asm": "assembler",
}
_SOURCE_EXPLICIT_LANGUAGES = {
    "c": "c",
    "c-header": "c",
    "cpp-output": "c",
    "c++": "c++",
    "c++-header": "c++",
    "c++-cpp-output": "c++",
    "objective-c": "objc",
    "objective-c-header": "objc",
    "objective-c-cpp-output": "objc",
    "objective-c++": "objc++",
    "objective-c++-header": "objc++",
    "objective-c++-cpp-output": "objc++",
    "assembler": "assembler",
    "assembler-with-cpp": "assembler-with-cpp",
}


def source_language(path: str) -> Optional[str]:
    """Return the bounded language for a source filename extension."""

    suffix = os.path.splitext(path)[1]
    if suffix == ".S":
        return "assembler-with-cpp"
    return _SOURCE_LANGUAGE_EXTENSIONS.get(suffix.lower())


_SOURCE_DRIVER_BY_LANGUAGE = {
    "c": "cc",
    "c++": "c++",
    "objc": "objc",
    "objc++": "objc++",
    "assembler": "cc",
    "assembler-with-cpp": "cc",
}
_SOURCE_OPTIONS_WITH_VALUES = frozenset(
    {
        "-o",
        "-I",
        "-L",
        "-F",
        "-framework",
        "-isystem",
        "-isysroot",
        "-include",
        "-imacros",
        "-MF",
        "-MT",
        "-MQ",
        "-arch",
        "-target",
        "--sysroot",
        "-Xclang",
        "-Xlinker",
        "-u",
        "-T",
    }
)


@dataclass(frozen=True)
class SourceCompilationUnit:
    """Bounded per-file driver mapping for a compiler invocation."""

    source: str
    language: str
    driver: str


def split_mixed_language_sources(
    arguments: Iterable[str],
    *,
    default_language: str = "c",
) -> list[SourceCompilationUnit]:
    """Map recognized source arguments to bounded driver names.

    This is a planning helper, not a process splitter: a mixed invocation is
    represented as one unit per source file so callers can drive per-file
    compilation if they need it.  Generated shims deliberately keep a single
    process and suppress ``-x`` for mixed sources.  Explicit ``-x`` applies to
    every returned unit and takes precedence over filename extensions.
    """

    args = list(arguments)
    explicit_seen = False
    explicit_language: Optional[str] = None
    sources: list[tuple[str, str]] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "-x":
            explicit_seen = True
            if index + 1 < len(args):
                explicit_language = _SOURCE_EXPLICIT_LANGUAGES.get(
                    args[index + 1].strip().lower()
                )
            index += 2
            continue
        if token.startswith("-x") and len(token) > 2:
            explicit_seen = True
            explicit_language = _SOURCE_EXPLICIT_LANGUAGES.get(
                token[2:].strip().lower()
            )
            index += 1
            continue
        if token in _SOURCE_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        language = source_language(token)
        if language is not None:
            sources.append((token, language))
        index += 1

    units: list[SourceCompilationUnit] = []
    for source, extension_language in sources:
        if explicit_seen:
            language = explicit_language or default_language
        else:
            language = extension_language
        driver = _SOURCE_DRIVER_BY_LANGUAGE.get(language)
        if driver is None:
            continue
        units.append(
            SourceCompilationUnit(
                source=source, language=language, driver=driver
            )
        )
    return units


def architecture_environment(
    environment: Optional[Mapping[str, str]] = None,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
) -> dict[str, str]:
    """Return bounded ARCH/ARCHFLAGS values from ``PYBREW_ARCH``.

    Explicit user ``ARCH``/``ARCHFLAGS`` values always win.  The arch flag is
    only exported when :func:`architecture_flag` considers it safe.
    """

    env = os.environ if environment is None else environment
    flag = architecture_flag(env, system=system, machine=machine)
    if flag is None:
        return {}
    raw = env.get("PYBREW_ARCH")
    if raw is None or not raw.strip():
        return {}
    value = raw.strip().lower()
    host = platform.system() if system is None else system
    if host == "Darwin":
        arch = _MACOS_ARCH_ALIASES.get(value)
    elif host == "Linux":
        if value in ("x86_64", "amd64"):
            arch = "x86_64"
        elif value in ("i386", "i686", "x86"):
            arch = "i386"
        else:
            arch = None
    else:
        arch = None
    if arch is None:
        return {}
    result: dict[str, str] = {}
    if not env.get("ARCHFLAGS"):
        result["ARCHFLAGS"] = flag
    if not env.get("ARCH"):
        result["ARCH"] = arch
    return result


def _ensure_architecture_flag(flags: str, flag: str) -> str:
    if flag.startswith("-arch"):
        if _has_flag(flags, ("-arch",), prefixes=("-arch=",)):
            return flags
    elif _has_flag(flags, ("-m32", "-m64")):
        return flags
    return _append_flag(flags, flag)


def objective_language_standard_flags(
    environment: Optional[Mapping[str, str]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Bounded Objective-C/Objective-C++ standard knobs."""

    env = os.environ if environment is None else environment

    def resolve(names: Iterable[str], allowed: frozenset[str]) -> Optional[str]:
        raw = _first_environment_value(env, names)
        if not raw:
            return None
        value = raw.strip().lower()
        if value.startswith("-std="):
            value = value[len("-std=") :]
        if allowed is _CXX_STANDARDS:
            value = _CXX_STANDARD_ALIASES.get(value, value)
        elif allowed is _C_STANDARDS:
            value = _C_STANDARD_ALIASES.get(value, value)
        return value if value in allowed else None

    return (
        resolve(
            ("PYBREW_OBJC_STANDARD", "PYBREW_OBJCSTD"), _C_STANDARDS
        ),
        resolve(
            ("PYBREW_OBJCXX_STANDARD", "PYBREW_OBJCXXSTD"), _CXX_STANDARDS
        ),
    )


_SDK_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,2}$")


def sdk_name_for_version(version: Optional[str]) -> str:
    """Map an explicit SDK version to the xcrun SDK name (bounded)."""

    raw = (version or "").strip()
    if not raw:
        return "macosx"
    if raw.startswith("macosx"):
        return raw
    if not _SDK_VERSION_RE.fullmatch(raw):
        return "macosx"
    return f"macosx{raw}"


def xcode_version(
    environment: Optional[Mapping[str, str]] = None,
    *,
    system: Optional[str] = None,
) -> Optional[str]:
    """Best-effort Xcode version probe, injectable via PYBREW_XCODEBUILD."""

    env = os.environ if environment is None else environment
    explicit = _first_environment_value(
        env, ("PYBREW_XCODE_VERSION", "XCODE_VERSION")
    )
    if explicit:
        return explicit
    host = platform.system() if system is None else system
    override = env.get("PYBREW_XCODEBUILD")
    if host != "Darwin" and not override:
        return None
    if override:
        try:
            command = shlex.split(override)
        except ValueError:
            command = [override]
    else:
        found = shutil.which("xcodebuild", path=env.get("PATH") or os.defpath)
        if not found:
            return None
        command = [found]
    if not command:
        return None
    try:
        result = subprocess.run(
            [*command, "-version"],
            env=dict(env),
            cwd=tempfile.gettempdir(),
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("xcode "):
            value = stripped[len("xcode ") :].strip()
            return value[:100] or None
    return None


def sdk_version_matrix(
    environment: Optional[Mapping[str, str]] = None,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
) -> dict[str, Optional[str]]:
    """Return the bounded SDK/deployment-target selection matrix."""

    env = os.environ if environment is None else environment
    requested = _first_environment_value(
        env, ("PYBREW_SDK_VERSION", "SDK_VERSION")
    )
    valid = requested if requested and _SDK_VERSION_RE.fullmatch(requested) else None
    return {
        "requested": requested,
        "sdk_name": sdk_name_for_version(valid),
        "minimum": minimum_platform_version(env, system=system, machine=machine),
        "deployment_target": _first_environment_value(
            env, ("PYBREW_MIN_OS", "MACOSX_DEPLOYMENT_TARGET")
        ),
        "xcode_version": xcode_version(env, system=system),
    }


def sdk_version_warning(
    environment: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    requested = _first_environment_value(
        environment or os.environ, ("PYBREW_SDK_VERSION", "SDK_VERSION")
    )
    if requested and not _SDK_VERSION_RE.fullmatch(requested):
        return f"invalid SDK version {requested!r}; using the default macOS SDK"
    return None


def detect_sdkroot(
    environment: Optional[Mapping[str, str]] = None,
    *,
    system: Optional[str] = None,
) -> Optional[Path]:
    """Best-effort macOS SDKROOT detection.

    Explicit ``PYBREW_SDKROOT`` and ``SDKROOT`` values win without running a
    command.  Otherwise ``xcrun --sdk macosx --show-sdk-path`` is used on
    Darwin; ``PYBREW_XCRUN`` may point at an injectable fake xcrun (and also
    makes the probe available in tests on non-Darwin hosts).  This is bounded:
    no SDK is required for Linux/default builds and failures simply return
    ``None``.
    """

    env = os.environ if environment is None else environment
    if "PYBREW_SDKROOT" in env:
        value = env.get("PYBREW_SDKROOT", "").strip()
        return Path(value).expanduser() if value else None
    if "SDKROOT" in env:
        value = env.get("SDKROOT", "").strip()
        return Path(value).expanduser() if value else None

    host = platform.system() if system is None else system
    xcrun_override = env.get("PYBREW_XCRUN")
    if host != "Darwin" and xcrun_override is None:
        return None
    if xcrun_override is not None:
        try:
            command = shlex.split(xcrun_override)
        except ValueError:
            command = [xcrun_override]
        if not command:
            return None
    else:
        xcrun = shutil.which("xcrun", path=env.get("PATH") or os.defpath)
        if not xcrun:
            return None
        command = [xcrun]

    sdk_version = _first_environment_value(
        env, ("PYBREW_SDK_VERSION", "SDK_VERSION")
    )
    sdk_name = sdk_name_for_version(sdk_version)
    try:
        result = subprocess.run(
            [*command, "--sdk", sdk_name, "--show-sdk-path"],
            env=dict(env),
            text=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    if not lines:
        return None
    path = Path(lines[-1].strip()).expanduser()
    return path if path.is_dir() else None


def _ensure_isysroot(flags: str, sdkroot: Path) -> str:
    # A user-provided SDK flag (or any --sysroot) is never overridden.
    if _has_flag(
        flags,
        ("-isysroot", "--sysroot", "-Wl,-syslibroot"),
        prefixes=(
            "-isysroot",
            "--sysroot=",
            "-Wl,-syslibroot,",
            "-Wl,-syslibroot=",
        ),
    ):
        return flags
    return _append_flag(flags, f"-isysroot {shlex.quote(str(sdkroot))}")


def split_compiler_command(command: str) -> list[str]:
    """Split a CC/CXX value that may contain wrapper arguments.

    For example ``"ccache clang"`` becomes ``["ccache", "clang"]``.  This is
    used by generated shims; the raw value still wins during compiler
    selection so explicit precedence is preserved.
    """

    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    return argv or [command]


def install_superenv_shims(
    config: Config, environment: Mapping[str, str]
) -> tuple[dict[str, str], SuperenvShims]:
    """Install opt-in compiler shims and prepend them to ``PATH``.

    This is deliberately opt-in via ``PYBREW_SUPERENV=1``.  The shims are thin
    wrappers around the selected real compilers that add the keg include/lib
    paths and sanitize system include/library flags at invocation time.  The
    helper also detects .c/.cc/.cpp/.m/.mm/.s/.S/.asm inputs and dispatches to
    the corresponding C/C++/ObjC/ObjC++/assembler driver.  The first recognized
    source extension selects the driver; an explicit ``-x`` wins.  For a mixed
    source invocation the first source still selects the driver but no ``-x``
    is injected, letting the compiler infer each file by extension.  Bounded
    language flags are added only for a single known source language and no
    explicit ``-x``.  Callers that need an actual per-file split can use
    :func:`split_mixed_language_sources`, which returns a bounded driver map
    without spawning processes.  This is a bounded compatibility layer, not
    Homebrew's full Superenv.
    """

    env = dict(environment)
    prefix = config.prefix
    include = str(prefix / "include")
    lib = str(prefix / "lib")
    root = prefix / "var" / "pybrew" / "superenv" / "shims"
    root.mkdir(parents=True, exist_ok=True)

    for name in (
        "CPPFLAGS",
        "CFLAGS",
        "CXXFLAGS",
        "OBJCFLAGS",
        "OBJCXXFLAGS",
        "ARCHFLAGS",
        "LDFLAGS",
    ):
        if name in env:
            diagnostics: list[str] = []
            env[name] = sanitize_build_flags(
                env[name], prefix, diagnostics=diagnostics
            )
            for message in diagnostics:
                print(f"warning: {message}", file=sys.stderr)

    def real_driver(names: Iterable[str], fallback: str) -> str:
        for name in names:
            found = _which_in(env, name)
            if found:
                return found
        resolved = shutil.which(fallback)
        return resolved or f"/usr/bin/{fallback}"

    cc = _first_environment_value(env, ("CC", "PYBREW_CC")) or real_driver(
        ("clang", "gcc", "cc"), "cc"
    )
    cxx = _first_environment_value(env, ("CXX", "PYBREW_CXX")) or real_driver(
        ("clang++", "g++", "c++"), "c++"
    )
    objc = _first_environment_value(env, ("OBJC", "PYBREW_OBJC")) or cc
    objcxx = _first_environment_value(
        env, ("OBJCXX", "PYBREW_OBJCXX")
    ) or cxx

    helper = root / "sanitize_compiler.py"
    helper.write_text(_SUPERENV_HELPER_SOURCE, encoding="utf-8")
    helper.chmod(0o755)

    def write_shim(path: Path, default_language: str) -> None:
        script = (
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(helper))} "
            f"{shlex.quote(str(prefix))} {shlex.quote(include)} "
            f"{shlex.quote(lib)} {shlex.quote(default_language)} "
            f"{shlex.quote(cc)} {shlex.quote(cxx)} "
            f"{shlex.quote(objc)} {shlex.quote(objcxx)} \"$@\"\n"
        )
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)

    cc_path = root / "cc"
    cxx_path = root / "c++"
    objc_path = root / "objc"
    objcxx_path = root / "objc++"
    write_shim(cc_path, "c")
    write_shim(cxx_path, "c++")
    write_shim(objc_path, "objc")
    write_shim(objcxx_path, "objc++")
    env["PATH"] = os.pathsep.join([str(root), env.get("PATH", os.defpath)])
    env["CC"] = str(cc_path)
    env["CXX"] = str(cxx_path)
    env["OBJC"] = str(objc_path)
    env["OBJCXX"] = str(objcxx_path)
    env["HOMEBREW_CC"] = cc
    env["HOMEBREW_CXX"] = cxx
    env["HOMEBREW_OBJC"] = objc
    env["HOMEBREW_OBJCXX"] = objcxx
    env["SUPERENV"] = "1"
    return env, SuperenvShims(
        root=root,
        cc=cc_path,
        cxx=cxx_path,
        helper=helper,
        objc=objc_path,
        objcxx=objcxx_path,
    )


def _force_make_jobs(flags: str, jobs: int) -> str:
    """Replace any -j/jobserver flags with an explicit bounded job count."""

    try:
        tokens = shlex.split(flags)
    except ValueError:
        tokens = flags.split()
    result: list[str] = []
    replaced = False
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in ("-j", "--jobs"):
            if not replaced:
                result.append(f"-j{int(jobs)}")
                replaced = True
            skip_next = True
            continue
        if (
            token.startswith("-j")
            or token.startswith("--jobs")
            or token.startswith("--jobserver")
        ):
            if not replaced:
                result.append(f"-j{int(jobs)}")
                replaced = True
            continue
        result.append(token)
    if not replaced:
        result.append(f"-j{int(jobs)}")
    return shlex.join(result)


_MAX_PKG_CONFIG_MODULES = 32


def pkg_config_flags(
    dependency_paths: Iterable[Path],
    *,
    environment: Mapping[str, str],
    prefix: Path,
) -> tuple[list[str], list[str]]:
    """Return bounded cflags/libs from pkg-config for installed deps.

    ``PYBREW_PKG_CONFIG`` (then ``PKG_CONFIG``) overrides the executable;
    an explicit empty value disables the integration.  Only ``.pc`` modules
    found under the supplied dependency roots are queried, at most 32, with a
    short timeout and no network access.
    """

    if "PYBREW_PKG_CONFIG" in environment:
        raw = environment.get("PYBREW_PKG_CONFIG", "").strip()
        if not raw:
            return [], []
        try:
            command = shlex.split(raw)
        except ValueError:
            command = [raw]
    elif environment.get("PKG_CONFIG"):
        try:
            command = shlex.split(environment["PKG_CONFIG"])
        except ValueError:
            command = [environment["PKG_CONFIG"]]
    else:
        found = shutil.which(
            "pkg-config", path=environment.get("PATH") or os.defpath
        )
        if not found:
            return [], []
        command = [found]
    if not command:
        return [], []

    pc_dirs: list[str] = []
    modules: list[str] = []
    for raw_path in dependency_paths:
        for leaf in ("lib/pkgconfig", "lib64/pkgconfig", "share/pkgconfig"):
            directory = Path(raw_path) / leaf
            if not directory.is_dir() or str(directory) in pc_dirs:
                continue
            pc_dirs.append(str(directory))
            for pc_file in sorted(directory.glob("*.pc")):
                module = pc_file.stem
                if module and module not in modules:
                    modules.append(module)
    if not modules:
        return [], []
    modules = modules[:_MAX_PKG_CONFIG_MODULES]

    child_env = dict(environment)
    existing = environment.get("PKG_CONFIG_PATH", "")
    path_parts = list(pc_dirs)
    if existing:
        path_parts.extend(existing.split(os.pathsep))
    child_env["PKG_CONFIG_PATH"] = os.pathsep.join(
        dict.fromkeys(part for part in path_parts if part)
    )

    def query(flag: str) -> list[str]:
        try:
            result = subprocess.run(
                [*command, flag, *modules],
                env=child_env,
                cwd=tempfile.gettempdir(),
                text=True,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0:
            return []
        try:
            tokens = shlex.split(result.stdout or "")
        except ValueError:
            tokens = (result.stdout or "").split()
        return sanitize_flag_list(tokens, prefix)

    return query("--cflags"), query("--libs")


@dataclass(frozen=True)
class BuildSandboxPlan:
    """A bounded external wrapper for one build command."""

    backend: str
    command: list[str]
    profile: Optional[str] = None


def _sandbox_escape(path: str) -> str:
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _sandbox_executable(environment: Mapping[str, str]) -> Optional[str]:
    override = environment.get("PYBREW_SANDBOX_BINARY", "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    path_override = environment.get("PYBREW_SANDBOX_PATH")
    if path_override is not None:
        candidate = Path(path_override)
        if path_override.strip() and candidate.is_file() and os.access(
            candidate, os.X_OK
        ):
            return str(candidate)
        return None
    candidates = ("/usr/bin/sandbox-exec", "/usr/bin/bwrap")
    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    for name in ("sandbox-exec", "bwrap"):
        found = shutil.which(name, path=environment.get("PATH") or os.defpath)
        if found:
            return found
    return None


def sandbox_backend_capabilities(
    environment: Optional[Mapping[str, str]] = None,
    *,
    system: Optional[str] = None,
    executable: Optional[str] = None,
) -> dict[str, Any]:
    """Report the isolation capabilities of the selected sandbox backend.

    This is reporting, not a security guarantee: sandbox-exec uses its policy
    profile, bwrap reports capability-drop/network support, and an explicit
    custom backend is reported as unverified.
    """

    env = os.environ if environment is None else environment
    host = platform.system() if system is None else system
    found = executable or _sandbox_executable(env)
    if found is None:
        return {
            "available": False,
            "backend": "none",
            "network_isolation": False,
            "capability_drop": False,
            "seccomp": False,
            "seccomp_enforced": False,
            "in_process_write_boundary": True,
        }
    name = Path(found).name
    if name == "sandbox-exec":
        return {
            "available": True,
            "backend": "sandbox-exec",
            "lsm": "seatbelt",
            "user_namespace": False,
            "network_isolation": True,
            "namespace_isolation": False,
            "capability_drop": False,
            "seccomp": False,
            "seccomp_enforced": False,
            "in_process_write_boundary": True,
        }
    if name in ("bwrap", "bubblewrap"):
        seccomp_available = host == "Linux" and (
            Path("/proc/self/status").is_file()
            or Path("/proc/sys/kernel/seccomp/actions_avail").is_file()
        )
        return {
            "available": True,
            "backend": "bwrap",
            "lsm": "seccomp-available" if seccomp_available else "none",
            "user_namespace": False,
            "network_isolation": True,
            "namespace_isolation": True,
            "capability_drop": True,
            "seccomp": seccomp_available,
            "seccomp_enforced": False,
            "in_process_write_boundary": True,
        }
    return {
        "available": True,
        "backend": "explicit",
        "lsm": "none",
        "user_namespace": False,
        "network_isolation": False,
        "namespace_isolation": False,
        "capability_drop": False,
        "seccomp": False,
        "seccomp_enforced": False,
        "in_process_write_boundary": True,
    }


sandbox_capabilities = sandbox_backend_capabilities


def seccomp_status(
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Report whether an explicit seccomp profile can be enforced."""

    env = os.environ if environment is None else environment
    requested = env.get("PYBREW_SECCOMP_PROFILE", "").strip()
    if not requested:
        return {
            "requested": False,
            "enforced": False,
            "supported": False,
            "reason": "no seccomp profile requested",
        }
    path = Path(requested).expanduser()
    exists = path.is_file()
    return {
        "requested": True,
        "profile": str(path),
        "exists": exists,
        "enforced": False,
        "supported": False,
        "reason": (
            "bwrap --seccomp requires an inherited/passed file descriptor and "
            "the Python wrapper cannot install it; sandbox-exec uses its policy "
            "profile instead"
        ),
    }


def plan_build_sandbox(
    command: Iterable[str],
    *,
    cwd: Path,
    writable_roots: Iterable[Path] = (),
    environment: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
) -> Optional[BuildSandboxPlan]:
    """Return an opt-in sandbox wrapper for a build command, or ``None``.

    ``PYBREW_SANDBOX=1`` enables the wrapper.  ``PYBREW_SANDBOX_BINARY`` or
    ``PYBREW_SANDBOX_PATH`` can select/disable the backend; otherwise
    sandbox-exec/bwrap is auto-detected.  Enabled-but-unavailable is a hard
    error rather than a silent unsandboxed build.  Rules are intentionally
    bounded: read-only root, explicit writable roots, and no network.

    The Python DSL additionally enforces a best-effort in-process write
    boundary (build root/keg/cwd) while sandboxing is enabled.  That boundary
    is Python-level validation, not an OS-enforced seccomp/LSM policy.
    """

    env = os.environ if environment is None else environment
    if env.get("PYBREW_SANDBOX") != "1":
        return None
    if env.get("PYBREW_SECCOMP_PROFILE", "").strip():
        raise BuildError(
            "PYBREW_SECCOMP_PROFILE was requested, but the Python build "
            "sandbox cannot pass a seccomp file descriptor; remove the profile "
            "or use an external wrapper"
        )
    argv = [str(item) for item in command]
    executable = _sandbox_executable(env)
    if executable is None:
        raise BuildError(
            "PYBREW_SANDBOX=1 but no sandbox backend is available; "
            "install bwrap/sandbox-exec or set PYBREW_SANDBOX_BINARY"
        )
    backend_name = Path(executable).name
    roots: list[str] = []
    for raw_root in (Path(cwd), *[Path(item) for item in writable_roots]):
        try:
            resolved = raw_root.resolve()
        except OSError:
            continue
        if resolved.is_dir() and str(resolved) not in roots:
            roots.append(str(resolved))
        if len(roots) >= 16:
            break
    host = platform.system() if system is None else system
    if backend_name == "sandbox-exec":
        strict = env.get("PYBREW_SANDBOX_STRICT") == "1"
        lines = ["(version 1)"]
        if strict:
            lines.extend(
                [
                    "(deny default)",
                    "(allow process-exec)",
                    "(allow process-fork)",
                    "(allow signal)",
                    "(allow sysctl-read)",
                    '(allow file-read* (subpath "/"))',
                ]
            )
            for root in roots:
                lines.append(
                    f'(allow file-write* (subpath "{_sandbox_escape(root)}"))'
                )
            lines.append("(deny network*)")
        else:
            lines.extend(["(allow default)", "(deny network*)"])
            for root in roots:
                lines.append(
                    f'(allow file-write* (subpath "{_sandbox_escape(root)}"))'
                )
        return BuildSandboxPlan(
            backend="sandbox-exec",
            command=[executable, "-p", "\n".join(lines), *argv],
            profile="\n".join(lines),
        )
    if backend_name in ("bwrap", "bubblewrap"):
        args = [
            executable,
            "--die-with-parent",
            "--cap-drop",
            "ALL",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--unshare-net",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup",
            "--new-session",
        ]
        for root in roots:
            args.extend(["--bind", root, root])
        args.extend(["--chdir", str(Path(cwd).resolve()), "--", *argv])
        return BuildSandboxPlan(backend="bwrap", command=args)
    # Explicit custom backends receive the command unchanged after their path.
    return BuildSandboxPlan(backend="explicit", command=[executable, *argv])


def build_environment(
    config: Config,
    base_env: Optional[Mapping[str, str]] = None,
    *,
    dependency_paths: Iterable[Path] = (),
) -> BuildEnvironment:
    """Prepare the environment for a source build.

    The Homebrew-like variables (prefix paths, pkg-config paths, include and
    library flags) are derived from ``config``.  User-supplied compiler flags
    are preserved and extended, and an explicit compiler in the environment is
    never overwritten.  The previous default values are kept exactly when the
    user did not set the corresponding variable.

    Bounded opt-ins: language standards via ``PYBREW_C_STANDARD`` /
    ``PYBREW_CXX_STANDARD`` (plus OBJC/OBJCXX variants), architecture via
    ``PYBREW_ARCH``, best-effort macOS SDKROOT detection via
    ``PYBREW_SDKROOT``/``PYBREW_XCRUN``, and Superenv shims via
    ``PYBREW_SUPERENV=1``.  None of these require full Homebrew Superenv and
    explicit user flags always win.

    Residual Superenv gaps kept intentionally bounded: no full SDK version
    matrix/Xcode layout emulation, no Homebrew formula-option dependency
    resolution, no automatic rpath injection for dependencies, no shell quoting
    for dependency paths containing spaces, pkg-config is limited to discovered
    ``.pc`` modules (<=32, 5s, no network), and signature/entitlements
    verification remains outside this module.
    """

    env = dict(os.environ if base_env is None else base_env)
    prefix = config.prefix
    dependencies: list[Path] = []
    for raw_path in dependency_paths:
        candidate = Path(raw_path)
        if candidate.is_dir() and candidate not in dependencies:
            dependencies.append(candidate)
    paths = [str(prefix / "bin"), str(prefix / "sbin")]
    for dependency in dependencies:
        for leaf in ("bin", "sbin"):
            candidate = dependency / leaf
            if candidate.is_dir():
                paths.append(str(candidate))
    paths.append(env.get("PATH", "/usr/bin:/bin"))
    env["PATH"] = os.pathsep.join(paths)
    env["HOMEBREW_PREFIX"] = str(prefix)
    env["HOMEBREW_CELLAR"] = str(config.cellar)
    env["HOMEBREW_REPOSITORY"] = str(prefix)
    pkg_config_paths = [
        str(prefix / "lib" / "pkgconfig"),
        str(prefix / "share" / "pkgconfig"),
    ]
    cmake_prefix_paths = [str(prefix)]
    aclocal_paths = [str(prefix / "share" / "aclocal")]
    include_flags = [_INCLUDE_FLAG.format(prefix=prefix)]
    library_flags = [_LIBRARY_FLAG.format(prefix=prefix)]
    for dependency in dependencies:
        for pkg_dir in (
            dependency / "lib" / "pkgconfig",
            dependency / "lib64" / "pkgconfig",
            dependency / "share" / "pkgconfig",
        ):
            if pkg_dir.is_dir():
                pkg_config_paths.append(str(pkg_dir))
        cmake_prefix_paths.append(str(dependency))
        aclocal_dir = dependency / "share" / "aclocal"
        if aclocal_dir.is_dir():
            aclocal_paths.append(str(aclocal_dir))
        include_dir = dependency / "include"
        if include_dir.is_dir():
            include_flags.append(f"-I{include_dir}")
        for library_dir in (dependency / "lib", dependency / "lib64"):
            if library_dir.is_dir():
                library_flags.append(f"-L{library_dir}")
    pkg_cflags, pkg_libs = pkg_config_flags(
        dependencies, environment=env, prefix=prefix
    )
    compile_flags = include_flags + pkg_cflags
    link_flags = library_flags + pkg_libs
    existing_pkg_config = env.get("PKG_CONFIG_PATH", "")
    if existing_pkg_config:
        pkg_config_paths.extend(existing_pkg_config.split(os.pathsep))
    env["PKG_CONFIG_PATH"] = os.pathsep.join(
        dict.fromkeys(path for path in pkg_config_paths if path)
    )
    existing_cmake = env.get("CMAKE_PREFIX_PATH", "")
    if existing_cmake:
        cmake_prefix_paths.extend(existing_cmake.split(os.pathsep))
    env["CMAKE_PREFIX_PATH"] = os.pathsep.join(
        dict.fromkeys(path for path in cmake_prefix_paths if path)
    )
    existing_aclocal = env.get("ACLOCAL_PATH", "")
    if existing_aclocal:
        aclocal_paths.extend(existing_aclocal.split(os.pathsep))
    env["ACLOCAL_PATH"] = os.pathsep.join(
        dict.fromkeys(path for path in aclocal_paths if path)
    )
    env["CPPFLAGS"] = _merge_flags(env.get("CPPFLAGS", ""), compile_flags)
    env["CFLAGS"] = _merge_flags(env.get("CFLAGS", ""), compile_flags)
    env["CXXFLAGS"] = _merge_flags(env.get("CXXFLAGS", ""), compile_flags)
    env["LDFLAGS"] = _merge_flags(env.get("LDFLAGS", ""), link_flags)
    jobs_value = _first_environment_value(
        env, ("PYBREW_MAKE_JOBS", "HOMEBREW_MAKE_JOBS")
    )
    jobs: Optional[int] = None
    if jobs_value is not None and jobs_value.isdigit():
        candidate_jobs = int(jobs_value)
        if 1 <= candidate_jobs <= 1024:
            jobs = candidate_jobs
    if jobs is None:
        jobs = max(1, (os.cpu_count() or 2))
    env["MAKEFLAGS"] = env.get("MAKEFLAGS") or f"-j{jobs}"

    # Objective-C/Objective-C++ support is opt-in: only touch the language
    # variables when the caller supplied a compiler or flags for them.
    objc = _first_environment_value(env, ("PYBREW_OBJC", "OBJC"))
    objcxx = _first_environment_value(env, ("PYBREW_OBJCXX", "OBJCXX"))
    objc_standard_configured = (
        _first_environment_value(
            env, ("PYBREW_OBJC_STANDARD", "PYBREW_OBJCSTD")
        )
        is not None
    )
    objcxx_standard_configured = (
        _first_environment_value(
            env, ("PYBREW_OBJCXX_STANDARD", "PYBREW_OBJCXXSTD")
        )
        is not None
    )
    if objc is not None or objc_standard_configured:
        if objc is not None:
            env["OBJC"] = objc
        env["OBJCFLAGS"] = _merge_flags(
            env.get("OBJCFLAGS", ""), compile_flags
        )
    elif "OBJCFLAGS" in env:
        env["OBJCFLAGS"] = _merge_flags(env["OBJCFLAGS"], compile_flags)
    if objcxx is not None or objcxx_standard_configured:
        if objcxx is not None:
            env["OBJCXX"] = objcxx
        env["OBJCXXFLAGS"] = _merge_flags(
            env.get("OBJCXXFLAGS", ""), compile_flags
        )
    elif "OBJCXXFLAGS" in env:
        env["OBJCXXFLAGS"] = _merge_flags(
            env["OBJCXXFLAGS"], compile_flags
        )

    c_standard, cxx_standard = language_standard_flags(env)
    if c_standard is not None:
        env["CFLAGS"] = _ensure_language_standard(env["CFLAGS"], c_standard)
    if cxx_standard is not None:
        env["CXXFLAGS"] = _ensure_language_standard(
            env["CXXFLAGS"], cxx_standard
        )
    objc_standard, objcxx_standard = objective_language_standard_flags(env)
    # Objective-C is a C dialect and Objective-C++ a C++ dialect: fall back to
    # the corresponding C/C++ standard when no language-specific knob is set.
    effective_objc_standard = (
        objc_standard if objc_standard is not None else c_standard
    )
    effective_objcxx_standard = (
        objcxx_standard if objcxx_standard is not None else cxx_standard
    )
    if effective_objc_standard is not None and "OBJCFLAGS" in env:
        env["OBJCFLAGS"] = _ensure_language_standard(
            env["OBJCFLAGS"], effective_objc_standard
        )
    if effective_objcxx_standard is not None and "OBJCXXFLAGS" in env:
        env["OBJCXXFLAGS"] = _ensure_language_standard(
            env["OBJCXXFLAGS"], effective_objcxx_standard
        )

    warnings: list[str] = []
    minimum_version = minimum_platform_version(env)
    minimum_warning = minimum_version_warning(env)
    if minimum_warning:
        warnings.append(minimum_warning)
    if minimum_version is not None:
        env["MACOSX_DEPLOYMENT_TARGET"] = minimum_version
        min_flag = f"-mmacosx-version-min={minimum_version}"
        for name in ("CFLAGS", "CXXFLAGS", "OBJCFLAGS", "OBJCXXFLAGS", "LDFLAGS"):
            if name in env:
                env[name] = _merge_flags(env[name], [min_flag])
    target_warning = cross_target_warning(env)
    if target_warning:
        warnings.append(target_warning)
    sdk_warning = sdk_version_warning(env)
    if sdk_warning:
        warnings.append(sdk_warning)
    sdk_version = _first_environment_value(env, ("PYBREW_SDK_VERSION", "SDK_VERSION"))
    if sdk_version is not None and _SDK_VERSION_RE.fullmatch(sdk_version):
        env.setdefault("SDK_VERSION", sdk_version)
    detected_xcode = xcode_version(env)
    if detected_xcode is not None:
        env.setdefault("XCODE_VERSION", detected_xcode)

    arch = architecture_flag(env)
    if arch is not None:
        for name in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
            env[name] = _ensure_architecture_flag(env[name], arch)
        for name in ("OBJCFLAGS", "OBJCXXFLAGS"):
            if name in env:
                env[name] = _ensure_architecture_flag(env[name], arch)
        for name, value in architecture_environment(env).items():
            env.setdefault(name, value)

    # SDK detection is best-effort and never required.  Generic inherited
    # SDKROOT is exported, but -isysroot flags are only added for explicit
    # pybrew SDK requests (PYBREW_SDKROOT/PYBREW_XCRUN) or the opt-in Superenv
    # layer, keeping default build flags unchanged on macOS/Linux/CI.
    explicit_sdk = bool(env.get("PYBREW_SDKROOT") or env.get("PYBREW_XCRUN"))
    sdkroot = detect_sdkroot(env)
    if sdkroot is not None:
        env.setdefault("SDKROOT", str(sdkroot))
        if explicit_sdk or env.get("PYBREW_SUPERENV") == "1":
            for name in ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS"):
                env[name] = _ensure_isysroot(env[name], sdkroot)
            for name in ("OBJCFLAGS", "OBJCXXFLAGS"):
                if name in env:
                    env[name] = _ensure_isysroot(env[name], sdkroot)

    compiler = select_compiler(env)
    if compiler.cc is not None:
        env["CC"] = compiler.cc
    if compiler.cxx is not None:
        env["CXX"] = compiler.cxx
    if env.get("PYBREW_SUPERENV") == "1":
        env, shims = install_superenv_shims(config, env)
        return BuildEnvironment(
            env=env,
            compiler=compiler,
            superenv=shims,
            warnings=tuple(warnings),
        )
    return BuildEnvironment(
        env=env, compiler=compiler, warnings=tuple(warnings)
    )


class SourceBuilder:
    def __init__(
        self,
        formula: Formula,
        config: Config,
        verbose: bool = False,
        downloader: Optional[Any] = None,
        environment: Optional[Mapping[str, str]] = None,
    ):
        self.formula = formula
        self.config = config
        self.verbose = verbose
        self.downloader = downloader
        self.environment = environment
        self._resource_stages: dict[str, Path] = {}

    def build(self, build_root: Path, keg: Path) -> None:
        # Homebrew applies a formula's patches to the unpacked source before
        # running its install block; both install backends share that step.
        self._apply_patches(build_root)
        if arbitrary_ruby_enabled():
            # Explicit opt-in (PYBREW_ALLOW_ARBITRARY_RUBY=1): hand the raw
            # `def install` body to Ruby instead of the declarative parser.
            execute_install_body(
                self.formula,
                self.config,
                build_root,
                keg,
                env=self._build_env(keg),
                verbose=self.verbose,
                downloader=self.downloader,
            )
            if not any(keg.iterdir()):
                raise BuildError(
                    f"{self.formula.name}: build completed but installed keg is empty"
                )
            return
        parser = InstallParser(self.formula)
        actions = parser.parse()
        if not actions:
            raise UnsupportedInstall(f"{self.formula.name}: install block is empty")
        context = BuildContext(
            formula=self.formula,
            config=self.config,
            keg=keg,
            build_root=build_root,
            verbose=self.verbose,
            env=self._build_env(keg),
            cwd=build_root,
        )
        if context.env.get("PYBREW_SANDBOX") == "1":
            # Validate the backend before any in-process write or system command;
            # enabled-but-unavailable must fail instead of silently running
            # unsandboxed.
            plan_build_sandbox(
                ["true"],
                cwd=context.cwd,
                writable_roots=self._sandbox_writable_roots(context),
                environment=context.env,
            )
        for action in actions:
            self._execute(action, context)
        if not any(keg.iterdir()):
            raise BuildError(
                f"{self.formula.name}: build completed but installed keg is empty"
            )

    # -- patches ---------------------------------------------------------
    def _apply_patches(self, build_root: Path) -> None:
        """Apply the formula's patches, in declaration order, to the source."""

        for index, patch in enumerate(self.formula.patches, start=1):
            self._apply_patch(patch, build_root, index)

    def _apply_patch(self, patch: PatchSpec, build_root: Path, index: int) -> None:
        name = self.formula.name
        if patch.unsupported_reason:
            raise BuildError(
                f"{name}: patch #{index} is unsupported: {patch.unsupported_reason}"
            )
        if patch.data is not None:
            content = patch.data.encode("utf-8")
            label = f"inline patch #{index}"
        elif patch.file:
            path = Path(patch.file)
            if not path.is_absolute():
                if self.formula.path is None:
                    raise BuildError(
                        f"{name}: patch #{index} file {patch.file!r} needs a formula path"
                    )
                path = Path(self.formula.path).parent / path
            try:
                content = path.read_bytes()
            except OSError as error:
                raise BuildError(
                    f"{name}: cannot read patch #{index} file {path}: {error}"
                ) from error
            label = str(path)
        else:
            if not patch.url:
                raise BuildError(f"{name}: patch #{index} has no url or inline data")
            if not patch.sha256:
                raise BuildError(
                    f"{name}: patch #{index} ({patch.url}) has no sha256 checksum"
                )
            if self.downloader is None:
                raise BuildError(
                    f"{name}: patch #{index} ({patch.url}) requires a downloader"
                )
            try:
                archive = self.downloader.fetch(
                    patch.url,
                    patch.sha256,
                    hint=f"{name}-patch-{index}",
                )
            except BuildError:
                raise
            except (ChecksumMismatch, DownloadError, OSError) as error:
                raise BuildError(
                    f"{name}: cannot download patch #{index} ({patch.url}): {error}"
                ) from error
            content = Path(archive).read_bytes()
            label = patch.url

        patch_command = os.environ.get("PYBREW_PATCH") or "/usr/bin/patch"
        if self.verbose:
            print(f"==> {name}: applying patch #{index} ({label}) with -p{patch.strip}")
        with tempfile.TemporaryDirectory(prefix="pybrew-patch-") as temporary:
            patch_file = Path(temporary) / "patch.diff"
            patch_file.write_bytes(content)
            dry_run_command = [
                patch_command,
                f"-p{patch.strip}",
                "--dry-run",
                "-f",
                "-i",
                str(patch_file),
            ]
            try:
                dry_run = subprocess.run(
                    dry_run_command,
                    cwd=build_root,
                    env=os.environ.copy(),
                    text=True,
                    capture_output=True,
                    timeout=600,
                )
            except FileNotFoundError as error:
                raise BuildError(
                    f"{name}: patch command not found: {patch_command}"
                ) from error
            except subprocess.TimeoutExpired as error:
                raise BuildError(f"{name}: patch #{index} timed out") from error
            dry_output = (dry_run.stdout or "") + (dry_run.stderr or "")
            targets = self._patch_targets(dry_output)
            if not targets:
                tail = "\n".join(dry_output.strip().splitlines()[-40:])
                raise BuildError(
                    f"{name}: patch #{index} failed: no verifiable patch targets"
                    + (f"\n{tail}" if tail else "")
                )
            root = build_root.resolve()
            for target in targets:
                posix_target = PurePosixPath(target)
                if posix_target.is_absolute() or ".." in posix_target.parts:
                    raise BuildError(
                        f"{name}: patch #{index} target escapes build root: {target!r}"
                    )
                candidate = (build_root / target).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError as error:
                    raise BuildError(
                        f"{name}: patch #{index} target escapes build root: {target!r}"
                    ) from error

            command = [
                patch_command,
                f"-p{patch.strip}",
                "-f",
                "-i",
                str(patch_file),
            ]
            try:
                result = subprocess.run(
                    command,
                    cwd=build_root,
                    env=os.environ.copy(),
                    text=True,
                    capture_output=True,
                    timeout=600,
                )
            except FileNotFoundError as error:
                raise BuildError(
                    f"{name}: patch command not found: {patch_command}"
                ) from error
            except subprocess.TimeoutExpired as error:
                raise BuildError(f"{name}: patch #{index} timed out") from error
        if result.returncode != 0:
            output = (result.stdout or "") + (result.stderr or "")
            tail = "\n".join(output.strip().splitlines()[-40:])
            detail = f" ({patch.url})" if patch.url else ""
            raise BuildError(
                f"{name}: patch #{index} failed with patch -p{patch.strip}{detail}"
                + (f"\n{tail}" if tail else "")
            )

    @staticmethod
    def _patch_targets(output: str) -> list[str]:
        """Extract patch targets from dry-run ``patching``/``checking`` lines."""

        targets: list[str] = []
        for line in output.splitlines():
            stripped = line.strip()
            for prefix in ("patching file ", "checking file "):
                if not stripped.startswith(prefix):
                    continue
                target = stripped[len(prefix) :].strip()
                if (
                    len(target) >= 2
                    and target[0] == target[-1]
                    and target[0] in {'"', "'"}
                ):
                    target = target[1:-1]
                if target and target not in targets:
                    targets.append(target)
        return targets

    def _dependency_paths(self) -> list[Path]:
        formula = self.formula
        dependencies = (
            list(formula.dependencies)
            + list(formula.build_dependencies)
            + list(formula.head_dependencies)
            + list(formula.uses_from_macos)
            + list(formula.recommended_dependencies)
            + list(formula.optional_dependencies)
        )
        paths: list[Path] = []
        for dependency in dependencies:
            name = dependency.name
            opted = self.config.opt_path(name)
            candidate: Optional[Path] = opted if opted.is_dir() else None
            if candidate is None:
                rack = self.config.cellar / name
                if rack.is_dir():
                    versions = sorted(
                        child for child in rack.iterdir() if child.is_dir()
                    )
                    if versions:
                        candidate = versions[-1]
            if candidate is not None and candidate not in paths:
                paths.append(candidate)
        return paths

    def _sandbox_writable_roots(self, ctx: BuildContext) -> tuple[Path, ...]:
        roots = (ctx.cwd, ctx.build_root, ctx.keg)
        unique: list[Path] = []
        for root in roots:
            resolved = root.resolve()
            if resolved not in unique:
                unique.append(resolved)
        return tuple(unique)

    def _check_sandbox_write(self, path: Path, ctx: BuildContext) -> None:
        """Enforce the in-process DSL write boundary when sandboxing is enabled."""

        if ctx.env.get("PYBREW_SANDBOX") != "1":
            return
        candidate = path if path.is_absolute() else ctx.cwd / path
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        for root in self._sandbox_writable_roots(ctx):
            try:
                resolved.relative_to(root)
                return
            except ValueError:
                continue
        raise BuildError(
            f"{self.formula.name}: in-process sandbox write boundary rejects {resolved}"
        )

    def _build_env(self, keg: Path) -> dict[str, str]:
        prepared = build_environment(
            self.config,
            self.environment,
            dependency_paths=self._dependency_paths(),
        )
        if prepared.compiler.warning:
            print(f"warning: {prepared.compiler.warning}", file=sys.stderr)
        for message in prepared.warnings:
            print(f"warning: {message}", file=sys.stderr)
        return prepared.env

    # -- AST evaluation --------------------------------------------------
    def _execute(self, action: Action, ctx: BuildContext) -> None:
        if isinstance(action, SystemAction):
            args = self._flatten([self._resolve(arg, ctx) for arg in action.args])
            self._run(args, ctx)
        elif isinstance(action, SetVar):
            ctx.variables[action.name] = self._resolve(action.value, ctx)
        elif isinstance(action, AppendVar):
            current = ctx.variables.get(action.name, [])
            if not isinstance(current, list):
                current = [str(current)]
            value = self._resolve(action.value, ctx)
            added = value if isinstance(value, list) else [str(value)]
            ctx.variables[action.name] = current + added
        elif isinstance(action, EnvSet):
            ctx.env[action.name] = self._scalar(self._resolve(action.value, ctx))
        elif isinstance(action, EnvPrependPath):
            name = action.name
            value = self._scalar(self._resolve(action.value, ctx))
            if action.create:
                created = Path(value)
                self._check_sandbox_write(created, ctx)
                created.mkdir(parents=True, exist_ok=True)
            current = ctx.env.get(name, "")
            ctx.env[name] = os.pathsep.join([value, current]) if current else value
        elif isinstance(action, EnvDelete):
            ctx.env.pop(action.name, None)
        elif isinstance(action, DeparallelizeAction):
            ctx.env["MAKEFLAGS"] = _force_make_jobs(
                ctx.env.get("MAKEFLAGS", ""), 1
            )
        elif isinstance(action, InreplaceAction):
            paths = self._as_list(self._resolve(action.paths, ctx))
            search = self._scalar(self._resolve(action.search, ctx))
            replacement = self._scalar(self._resolve(action.replacement, ctx))
            for pattern in paths:
                for path in glob.glob(str(ctx.cwd / pattern)):
                    file_path = Path(path)
                    if not file_path.is_file():
                        continue
                    self._check_sandbox_write(file_path, ctx)
                    text = file_path.read_text(errors="replace")
                    if search not in text:
                        raise BuildError(
                            f"{self.formula.name}: inreplace string not found in {file_path}: {search!r}"
                        )
                    file_path.write_text(text.replace(search, replacement))
        elif isinstance(action, InstallFiles):
            self._install_files(action, ctx)
        elif isinstance(action, ShellOp):
            self._shell_op(action, ctx)
        elif isinstance(action, CdAction):
            directory = self._scalar(self._resolve(action.path, ctx))
            previous = ctx.cwd
            ctx.cwd = (ctx.cwd / directory).resolve()
            try:
                for child in action.actions:
                    self._execute(child, ctx)
            finally:
                ctx.cwd = previous
        elif isinstance(action, ResourceStageAction):
            resource = ctx.formula.resources.get(action.name)
            if resource is None or not resource.url:
                raise UnsupportedInstall(
                    f"{ctx.formula.name}: resource {action.name!r} is not defined"
                )
            if not resource.sha256:
                raise UnsupportedInstall(
                    f"{ctx.formula.name}: resource {action.name!r} has no sha256"
                )
            if self.downloader is None:
                raise UnsupportedInstall(
                    f"{ctx.formula.name}: resource {action.name!r} requires a downloader"
                )
            stage = self._resource_stages.get(action.name)
            if stage is None or not stage.is_dir():
                archive = self.downloader.fetch(
                    resource.url,
                    resource.sha256,
                    hint=f"{ctx.formula.name}-{action.name}",
                )
                stage = self._resource_stage_path(ctx.build_root, action.name)
                self._check_sandbox_write(stage, ctx)
                if stage.exists():
                    shutil.rmtree(stage)
                stage.mkdir(parents=True)
                self._extract_resource(archive, stage)
                children = [
                    candidate
                    for candidate in stage.iterdir()
                    if candidate.name != "__MACOSX"
                ]
                if len(children) == 1 and children[0].is_dir():
                    stage = children[0]
                self._resource_stages[action.name] = stage
            previous = ctx.cwd
            ctx.cwd = stage.resolve()
            try:
                for child in action.actions:
                    self._execute(child, ctx)
            finally:
                ctx.cwd = previous
        elif isinstance(action, ConditionalAction):
            if action.condition:
                for child in action.actions:
                    self._execute(child, ctx)
        else:  # pragma: no cover
            raise UnsupportedInstall(f"unknown build action: {action!r}")

    @staticmethod
    def _resource_stage_path(build_root: Path, name: str) -> Path:
        """Deterministic, path-safe stage directory for a resource name."""

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
        if not safe:
            safe = "resource"
        if len(safe) > 80:
            safe = safe[:80].rstrip("._") or "resource"
        if safe != name or len(name) > 80:
            digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
            safe = f"{safe[:80]}-{digest}"
        return build_root / ".pybrew-resources" / safe

    def _extract_resource(self, archive: Path, destination: Path) -> None:
        import tarfile
        import zipfile

        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                for info in bundle.infolist():
                    name = info.filename
                    parts = Path(name).parts
                    if not name or name.startswith("/") or ".." in parts:
                        raise BuildError(f"unsafe resource zip member: {name!r}")
                    target = destination / name
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(info) as source, open(target, "wb") as output:
                        shutil.copyfileobj(source, output)
            return
        try:
            with tarfile.open(archive) as tar:
                for member in tar.getmembers():
                    name = member.name
                    parts = Path(name).parts
                    if not name or name.startswith("/") or ".." in parts:
                        raise BuildError(f"unsafe resource tar member: {name!r}")
                    target = destination / name
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif member.issym() or member.islnk():
                        raise BuildError(f"resource tar links are not supported: {name!r}")
                    elif member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source = tar.extractfile(member)
                        if source is None:
                            raise BuildError(f"cannot read resource tar member: {name!r}")
                        with source, open(target, "wb") as output:
                            shutil.copyfileobj(source, output)
        except tarfile.TarError as error:
            raise BuildError(f"cannot read resource archive {archive}: {error}") from error

    def _resolve(self, expr: Expr, ctx: BuildContext) -> Any:
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, StringExpr):
            return ctx.interpolate(expr.template)
        if isinstance(expr, EnvRead):
            return ctx.env.get(expr.name, "")
        if isinstance(expr, Var):
            if expr.name in ctx.variables:
                return ctx.variables[expr.name]
            known = {
                "prefix",
                "bin",
                "sbin",
                "lib",
                "include",
                "share",
                "etc",
                "libexec",
                "var",
                "opt_prefix",
                "opt_bin",
                "pkgshare",
                "appdir",
                "staged_path",
                "buildpath",
            }
            if expr.name in known:
                return str(ctx.path_for(expr.name))
            raise UnsupportedInstall(f"{self.formula.name}: unknown install variable {expr.name!r}")
        if isinstance(expr, PathJoin):
            base = self._resolve(expr.base, ctx)
            return str(Path(str(base)) / expr.part)
        if isinstance(expr, MethodExpr):
            return self._resolve_method(expr, ctx)
        if isinstance(expr, Helper):
            return self._expand_helper(expr.name, ctx)
        if isinstance(expr, ArrayExpr):
            result: list[Any] = []
            for item in expr.items:
                value = self._resolve(item, ctx)
                if isinstance(value, list):
                    result.extend(value)
                else:
                    result.append(value)
            return result
        if isinstance(expr, Spread):
            return self._resolve(expr.expr, ctx)
        if isinstance(expr, UnknownExpr):
            raise UnsupportedInstall(
                f"{self.formula.name}: unsupported install expression {expr.text!r}"
            )
        raise UnsupportedInstall(f"{self.formula.name}: unsupported expression {expr!r}")

    def _resolve_method(self, expr: MethodExpr, ctx: BuildContext) -> Any:
        version = ctx.formula.version or "HEAD"
        receiver, method = expr.receiver, expr.method
        if receiver == "version":
            if method == "dots_to_underscores":
                return version.replace(".", "_")
            if method == "dots_to_hyphens":
                return version.replace(".", "-")
            if method == "major":
                return version.split(".")[0]
            if method == "minor":
                return version.split(".")[1] if "." in version else "0"
            if method == "major_minor":
                return ".".join(version.split(".")[:2])
        if receiver == "formula" and method == "name":
            return ctx.formula.name
        if receiver == "f" and method == "name":
            return ctx.formula.name
        raise UnsupportedInstall(
            f"{self.formula.name}: unsupported install method call {receiver}.{method}"
        )

    def _expand_helper(self, name: str, ctx: BuildContext) -> list[str]:
        keg = str(ctx.keg)
        if name == "std_configure_args":
            return [
                "--disable-debug",
                "--disable-dependency-tracking",
                f"--prefix={keg}",
                f"--libdir={ctx.keg / 'lib'}",
            ]
        if name == "std_cmake_args":
            return [
                f"-DCMAKE_INSTALL_PREFIX={keg}",
                "-DCMAKE_INSTALL_LIBDIR=lib",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_FIND_FRAMEWORK=LAST",
                "-DCMAKE_VERBOSE_MAKEFILE=ON",
                "-Wno-dev",
            ]
        if name == "std_meson_args":
            return [
                f"--prefix={keg}",
                "--libdir=lib",
                "--buildtype=release",
                "--wrap-mode=nofallback",
            ]
        if name == "std_cargo_args":
            return ["--path", ".", "--root", keg]
        if name == "std_service_path_env":
            return str(ctx.config.prefix / "bin")
        if name == "std_go_args":
            return [f"-o={ctx.keg / 'bin' / ctx.formula.name}"]
        if name == "std_pip_args":
            return ["--prefix", keg]
        raise UnsupportedInstall(f"{self.formula.name}: unknown build helper {name}")

    # -- operations ------------------------------------------------------
    def _run(self, args: list[str], ctx: BuildContext) -> None:
        if not args:
            raise BuildError(f"{self.formula.name}: empty system command")
        command = [str(arg) for arg in args]
        sandbox = plan_build_sandbox(
            command,
            cwd=ctx.cwd,
            writable_roots=(
                ctx.build_root,
                ctx.keg,
                Path(tempfile.gettempdir()),
            ),
            environment=ctx.env,
        )
        execution_command = sandbox.command if sandbox else command
        if self.verbose:
            print(f"==> {ctx.formula.name}: system {' '.join(execution_command)}")
        try:
            result = subprocess.run(
                execution_command,
                cwd=ctx.cwd,
                env=ctx.env,
                text=True,
                capture_output=True,
                timeout=1800,
            )
        except FileNotFoundError as error:
            raise BuildError(f"{self.formula.name}: command not found: {command[0]}") from error
        except subprocess.TimeoutExpired as error:
            raise BuildError(f"{self.formula.name}: build timed out running {command[0]}") from error
        if result.returncode != 0:
            output = (result.stdout or "") + (result.stderr or "")
            tail = "\n".join(output.strip().splitlines()[-40:])
            raise BuildError(
                f"{self.formula.name}: build command failed: {' '.join(command)}\n{tail}"
            )

    def _install_files(self, action: InstallFiles, ctx: BuildContext) -> None:
        target_dir = self._destination_dir(action.directory, ctx)
        self._check_sandbox_write(target_dir, ctx)
        target_dir.mkdir(parents=True, exist_ok=True)
        if not action.args:
            return
        patterns: list[str] = []
        for expr in action.args:
            value = self._resolve(expr, ctx)
            patterns.extend(self._as_list(value))
        copied = False
        for pattern in patterns:
            matches = sorted(glob.glob(str(ctx.cwd / pattern)))
            if not matches and not any(char in pattern for char in "*?["):
                # Homebrew also accepts an absolute/variable-resolved path.
                candidate = Path(pattern)
                if candidate.is_file() or candidate.is_dir():
                    matches = [str(candidate)]
            for match in matches:
                copied = True
                source = Path(match)
                destination = target_dir / source.name
                self._check_sandbox_write(destination, ctx)
                if destination.exists() or destination.is_symlink():
                    if destination.is_dir() and not destination.is_symlink():
                        shutil.rmtree(destination)
                    else:
                        destination.unlink()
                if action.symlink:
                    try:
                        relative = os.path.relpath(source, destination.parent)
                    except OSError:
                        relative = str(source)
                    destination.symlink_to(relative)
                elif source.is_dir():
                    shutil.copytree(source, destination, symlinks=True)
                else:
                    shutil.copy2(source, destination)
                    os.chmod(destination, source.stat().st_mode & 0o777)
        if not copied:
            raise BuildError(
                f"{self.formula.name}: install source not found: {patterns} (in {ctx.cwd})"
            )

    def _destination_dir(self, directory: str, ctx: BuildContext) -> Path:
        mapping = {
            "bin": ctx.keg / "bin",
            "sbin": ctx.keg / "sbin",
            "lib": ctx.keg / "lib",
            "include": ctx.keg / "include",
            "share": ctx.keg / "share",
            "etc": ctx.keg / "etc",
            "libexec": ctx.keg / "libexec",
            "prefix": ctx.keg,
            "pkgshare": ctx.keg / "share" / ctx.formula.name,
        }
        if directory.startswith("man"):
            number = directory[3:] or ""
            return ctx.keg / "share" / "man" / (f"man{number}" if number else "man1")
        if directory in ("elisp", "vim"):
            return ctx.keg / "share" / directory
        return mapping.get(directory, ctx.keg / directory)

    def _shell_op(self, action: ShellOp, ctx: BuildContext) -> None:
        args = [self._resolve(arg, ctx) for arg in action.args]
        if action.kind == "mkdir":
            for value in self._flatten(args):
                target = ctx.cwd / str(value)
                self._check_sandbox_write(target, ctx)
                target.mkdir(parents=True, exist_ok=True)
        elif action.kind in ("mv", "cp"):
            if len(args) < 2:
                raise BuildError(f"{self.formula.name}: {action.kind} needs two arguments")
            source = ctx.cwd / str(self._scalar(args[0]))
            destination = ctx.cwd / str(self._scalar(args[1]))
            self._check_sandbox_write(destination, ctx)
            if action.kind == "mv":
                shutil.move(str(source), str(destination))
            elif source.is_dir():
                shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)
        elif action.kind == "rm":
            for value in self._flatten(args):
                target = ctx.cwd / str(value)
                self._check_sandbox_write(target, ctx)
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    try:
                        target.unlink()
                    except FileNotFoundError:
                        pass
        elif action.kind == "chmod":
            if len(args) < 2:
                raise BuildError(f"{self.formula.name}: chmod needs mode and path")
            mode = int(str(args[0]), 8) if isinstance(args[0], str) else int(args[0])
            for value in self._flatten(args[1:]):
                target = ctx.cwd / str(value)
                self._check_sandbox_write(target, ctx)
                os.chmod(target, mode)
        elif action.kind == "touch":
            for value in self._flatten(args):
                target = ctx.cwd / str(value)
                self._check_sandbox_write(target, ctx)
                target.touch()
        elif action.kind == "env_append":
            if len(args) < 2:
                raise BuildError(f"{self.formula.name}: ENV append needs name and value")
            name = self._scalar(args[0])
            value = self._scalar(args[1])
            current = ctx.env.get(name, "")
            ctx.env[name] = f"{current} {value}".strip()
        elif action.kind == "write_exec_script":
            # Simplest useful implementation: copy the source to bin and make
            # it executable.  Formula-supplied path interpolation is handled by
            # the caller.
            if len(args) < 2:
                raise BuildError(f"{self.formula.name}: write_exec_script needs a source")
            source = ctx.cwd / str(args[1])
            target = ctx.keg / "bin" / source.name
            self._check_sandbox_write(target, ctx)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            os.chmod(target, 0o755)

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _flatten(values: Iterable[Any]) -> list[Any]:
        result: list[Any] = []
        for value in values:
            if isinstance(value, list):
                result.extend(SourceBuilder._flatten(value))
            else:
                result.append(value)
        return result

    def _as_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]

    @staticmethod
    def _scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)


def build_source(
    formula: Formula,
    config: Config,
    build_root: Path,
    keg: Path,
    verbose: bool = False,
    downloader: Optional[Any] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> None:
    SourceBuilder(
        formula,
        config,
        verbose=verbose,
        downloader=downloader,
        environment=environment,
    ).build(build_root, keg)
