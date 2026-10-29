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
"""

from __future__ import annotations

import glob
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .errors import BuildError, UnsupportedInstall
from .models import Formula, is_linux, is_macos
from .paths import Config
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


class SourceBuilder:
    def __init__(
        self,
        formula: Formula,
        config: Config,
        verbose: bool = False,
        downloader: Optional[Any] = None,
    ):
        self.formula = formula
        self.config = config
        self.verbose = verbose
        self.downloader = downloader
        self._resource_stages: dict[str, Path] = {}

    def build(self, build_root: Path, keg: Path) -> None:
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

    def _build_env(self, keg: Path) -> dict[str, str]:
        env = os.environ.copy()
        prefix = self.config.prefix
        paths = [str(prefix / "bin"), str(prefix / "sbin")]
        env["PATH"] = os.pathsep.join(paths + [env.get("PATH", "/usr/bin:/bin")])
        env["HOMEBREW_PREFIX"] = str(prefix)
        env["HOMEBREW_CELLAR"] = str(self.config.cellar)
        env["HOMEBREW_REPOSITORY"] = str(prefix)
        env["PKG_CONFIG_PATH"] = os.pathsep.join(
            [str(prefix / "lib" / "pkgconfig"), str(prefix / "share" / "pkgconfig")]
        )
        env["CMAKE_PREFIX_PATH"] = str(prefix)
        env["ACLOCAL_PATH"] = str(prefix / "share" / "aclocal")
        env["CPPFLAGS"] = f"-I{prefix / 'include'}"
        env["CFLAGS"] = f"-I{prefix / 'include'}"
        env["CXXFLAGS"] = f"-I{prefix / 'include'}"
        env["LDFLAGS"] = f"-L{prefix / 'lib'}"
        env["MAKEFLAGS"] = env.get("MAKEFLAGS", f"-j{max(1, (os.cpu_count() or 2))}")
        return env

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
) -> None:
    SourceBuilder(formula, config, verbose=verbose, downloader=downloader).build(build_root, keg)
