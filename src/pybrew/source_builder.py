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
from pathlib import Path
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
            if not args or not isinstance(args[0], (StringExpr, Literal)):
                raise UnsupportedInstall(f"{self.formula.name}: resource without a literal name")
            resource_name = self._static_name(args[0])
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
_RPATH_PREFIXES = ("-Wl,-rpath,", "-Wl,-rpath=", "-Wl,-R,", "-Wl,-R=")


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
    if token == "-isysroot":
        if index + 1 < len(arguments):
            return "sysroot", arguments[index + 1], 2
        return None
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
    for prefix in _RPATH_PREFIXES:
        if token.startswith(prefix):
            return "rpath", token[len(prefix) :], 1
    if token == "-Wl,-rpath" and index + 1 < len(arguments):
        following = arguments[index + 1]
        if following.startswith("-Wl,"):
            return "rpath", following[len("-Wl,") :], 2
    return None


def sanitize_flag_list(
    arguments: Iterable[str],
    prefix: Path,
    *,
    drop_prefix_flags: bool = False,
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
    index = 0
    while index < len(args):
        parsed = _flag_value(args, index)
        if parsed is None:
            result.append(args[index])
            index += 1
            continue
        kind, path, consumed = parsed
        normalised = os.path.normpath(path)
        if kind == "include":
            unsafe = _path_under(normalised, _SYSTEM_INCLUDE_ROOTS)
            is_prefix = normalised == prefix_include
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
            arguments, prefix, drop_prefix_flags=drop_prefix_flags
        )
    )


# Standalone helper written next to the shims.  It intentionally duplicates the
# bounded sanitizer above so the generated scripts work without importing
# pybrew (the build environment may be a clean shell).
_SUPERENV_HELPER_SOURCE = '''#!/usr/bin/env python3
"""Bounded Superenv shim helper generated by pybrew."""
import os
import shlex
import sys

prefix, include, library, driver_spec = sys.argv[1:5]
arguments = sys.argv[5:]
SYSTEM_INCLUDE_ROOTS = ("/usr/include", "/usr/local/include")
SYSTEM_LIBRARY_ROOTS = (
    "/usr/lib",
    "/usr/lib64",
    "/usr/local/lib",
    "/lib",
    "/lib64",
)
RPATH_PREFIXES = ("-Wl,-rpath,", "-Wl,-rpath=", "-Wl,-R,", "-Wl,-R=")
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
    if token == "-isysroot":
        if index + 1 < len(args):
            return "sysroot", args[index + 1], 2
        return None
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
    for flag_prefix in RPATH_PREFIXES:
        if token.startswith(flag_prefix):
            return "rpath", token[len(flag_prefix):], 1
    if token == "-Wl,-rpath" and index + 1 < len(args):
        following = args[index + 1]
        if following.startswith("-Wl,"):
            return "rpath", following[len("-Wl,"):], 2
    return None


filtered = []
seen_sysroot = set()
index = 0
while index < len(arguments):
    parsed = flag_value(arguments, index)
    if parsed is None:
        filtered.append(arguments[index])
        index += 1
        continue
    kind, path, consumed = parsed
    normalised = os.path.normpath(path)
    if kind == "include":
        unsafe = path_under(normalised, SYSTEM_INCLUDE_ROOTS)
        duplicate_prefix = normalised == PREFIX_INCLUDE
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

try:
    driver_argv = shlex.split(driver_spec)
except ValueError:
    driver_argv = [driver_spec]
if not driver_argv:
    sys.exit("empty compiler command")
os.execvp(
    driver_argv[0],
    [*driver_argv, "-isystem", include, "-L", library, *filtered],
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
        return CompilerSelection(cc=cc, cxx=cxx)

    if requested not in _COMPILER_DRIVERS:
        return CompilerSelection(
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
        return CompilerSelection(cc=cc, cxx=cxx, family=requested)

    c_driver, cxx_driver = _COMPILER_DRIVERS[requested]
    available_cc = _which_in(env, c_driver)
    available_cxx = _which_in(env, cxx_driver)
    if cc is None and available_cc is None:
        return CompilerSelection(
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
    return CompilerSelection(cc=cc, cxx=cxx, family=requested, warning=warning)


def _merge_flags(existing: str, additions: Iterable[str]) -> str:
    """Append build flags to ``existing`` without dropping user values."""

    result = (existing or "").strip()
    parts = result.split()
    for addition in additions:
        if addition not in parts:
            result = f"{result} {addition}".strip()
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

    try:
        result = subprocess.run(
            [*command, "--sdk", "macosx", "--show-sdk-path"],
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
    paths and sanitize system include/library flags at invocation time.  This
    is a bounded compatibility layer, not Homebrew's full Superenv.  The
    generated scripts are stable on disk so parallel builds can reuse them.
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
        "LDFLAGS",
    ):
        if name in env:
            env[name] = sanitize_build_flags(env[name], prefix)

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

    helper = root / "sanitize_compiler.py"
    helper.write_text(_SUPERENV_HELPER_SOURCE, encoding="utf-8")
    helper.chmod(0o755)

    def write_shim(path: Path, driver: str) -> None:
        script = (
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(helper))} "
            f"{shlex.quote(str(prefix))} {shlex.quote(include)} "
            f"{shlex.quote(lib)} {shlex.quote(driver)} \"$@\"\n"
        )
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)

    objc = _first_environment_value(env, ("OBJC", "PYBREW_OBJC")) or cc
    objcxx = _first_environment_value(
        env, ("OBJCXX", "PYBREW_OBJCXX")
    ) or cxx

    cc_path = root / "cc"
    cxx_path = root / "c++"
    objc_path = root / "objc"
    objcxx_path = root / "objc++"
    write_shim(cc_path, cc)
    write_shim(cxx_path, cxx)
    write_shim(objc_path, objc)
    write_shim(objcxx_path, objcxx)
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


def build_environment(
    config: Config,
    base_env: Optional[Mapping[str, str]] = None,
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
    """

    env = dict(os.environ if base_env is None else base_env)
    prefix = config.prefix
    paths = [str(prefix / "bin"), str(prefix / "sbin")]
    env["PATH"] = os.pathsep.join(paths + [env.get("PATH", "/usr/bin:/bin")])
    env["HOMEBREW_PREFIX"] = str(prefix)
    env["HOMEBREW_CELLAR"] = str(config.cellar)
    env["HOMEBREW_REPOSITORY"] = str(prefix)
    env["PKG_CONFIG_PATH"] = os.pathsep.join(
        [str(prefix / "lib" / "pkgconfig"), str(prefix / "share" / "pkgconfig")]
    )
    env["CMAKE_PREFIX_PATH"] = str(prefix)
    env["ACLOCAL_PATH"] = str(prefix / "share" / "aclocal")
    include_flag = _INCLUDE_FLAG.format(prefix=prefix)
    library_flag = _LIBRARY_FLAG.format(prefix=prefix)
    env["CPPFLAGS"] = _merge_flags(env.get("CPPFLAGS", ""), [include_flag])
    env["CFLAGS"] = _merge_flags(env.get("CFLAGS", ""), [include_flag])
    env["CXXFLAGS"] = _merge_flags(env.get("CXXFLAGS", ""), [include_flag])
    env["LDFLAGS"] = _merge_flags(env.get("LDFLAGS", ""), [library_flag])
    env["MAKEFLAGS"] = env.get("MAKEFLAGS", f"-j{max(1, (os.cpu_count() or 2))}")

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
            env.get("OBJCFLAGS", ""), [include_flag]
        )
    elif "OBJCFLAGS" in env:
        env["OBJCFLAGS"] = _merge_flags(env["OBJCFLAGS"], [include_flag])
    if objcxx is not None or objcxx_standard_configured:
        if objcxx is not None:
            env["OBJCXX"] = objcxx
        env["OBJCXXFLAGS"] = _merge_flags(
            env.get("OBJCXXFLAGS", ""), [include_flag]
        )
    elif "OBJCXXFLAGS" in env:
        env["OBJCXXFLAGS"] = _merge_flags(
            env["OBJCXXFLAGS"], [include_flag]
        )

    c_standard, cxx_standard = language_standard_flags(env)
    if c_standard is not None:
        env["CFLAGS"] = _ensure_language_standard(env["CFLAGS"], c_standard)
    if cxx_standard is not None:
        env["CXXFLAGS"] = _ensure_language_standard(
            env["CXXFLAGS"], cxx_standard
        )
    objc_standard, objcxx_standard = objective_language_standard_flags(env)
    if objc_standard is not None and "OBJCFLAGS" in env:
        env["OBJCFLAGS"] = _ensure_language_standard(
            env["OBJCFLAGS"], objc_standard
        )
    if objcxx_standard is not None and "OBJCXXFLAGS" in env:
        env["OBJCXXFLAGS"] = _ensure_language_standard(
            env["OBJCXXFLAGS"], objcxx_standard
        )

    arch = architecture_flag(env)
    if arch is not None:
        for name in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
            env[name] = _ensure_architecture_flag(env[name], arch)
        for name in ("OBJCFLAGS", "OBJCXXFLAGS"):
            if name in env:
                env[name] = _ensure_architecture_flag(env[name], arch)

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
        return BuildEnvironment(env=env, compiler=compiler, superenv=shims)
    return BuildEnvironment(env=env, compiler=compiler)


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

    def _build_env(self, keg: Path) -> dict[str, str]:
        prepared = build_environment(self.config, self.environment)
        if prepared.compiler.warning:
            print(f"warning: {prepared.compiler.warning}", file=sys.stderr)
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
                Path(value).mkdir(parents=True, exist_ok=True)
            current = ctx.env.get(name, "")
            ctx.env[name] = os.pathsep.join([value, current]) if current else value
        elif isinstance(action, EnvDelete):
            ctx.env.pop(action.name, None)
        elif isinstance(action, InreplaceAction):
            paths = self._as_list(self._resolve(action.paths, ctx))
            search = self._scalar(self._resolve(action.search, ctx))
            replacement = self._scalar(self._resolve(action.replacement, ctx))
            for pattern in paths:
                for path in glob.glob(str(ctx.cwd / pattern)):
                    file_path = Path(path)
                    if not file_path.is_file():
                        continue
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
            archive = self.downloader.fetch(
                resource.url,
                resource.sha256,
                hint=f"{ctx.formula.name}-{action.name}",
            )
            stage = ctx.build_root / ".pybrew-resources" / action.name
            if stage.exists():
                shutil.rmtree(stage)
            stage.mkdir(parents=True)
            self._extract_resource(archive, stage)
            children = [
                candidate for candidate in stage.iterdir() if candidate.name != "__MACOSX"
            ]
            if len(children) == 1 and children[0].is_dir():
                stage = children[0]
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
        if self.verbose:
            print(f"==> {ctx.formula.name}: system {' '.join(command)}")
        try:
            result = subprocess.run(
                command,
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
                (ctx.cwd / str(value)).mkdir(parents=True, exist_ok=True)
        elif action.kind in ("mv", "cp"):
            if len(args) < 2:
                raise BuildError(f"{self.formula.name}: {action.kind} needs two arguments")
            source = ctx.cwd / str(self._scalar(args[0]))
            destination = ctx.cwd / str(self._scalar(args[1]))
            if action.kind == "mv":
                shutil.move(str(source), str(destination))
            elif source.is_dir():
                shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)
        elif action.kind == "rm":
            for value in self._flatten(args):
                target = ctx.cwd / str(value)
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
                os.chmod(ctx.cwd / str(value), mode)
        elif action.kind == "touch":
            for value in self._flatten(args):
                (ctx.cwd / str(value)).touch()
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
