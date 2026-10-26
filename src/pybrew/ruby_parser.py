"""A tolerant reader for the Ruby DSL used by Homebrew formulae.

Homebrew formulae are Ruby programs.  A full Ruby interpreter is neither
needed nor desirable for metadata and bottle installs, so this module implements
a small lexer plus a literal-oriented parser for the declarative part of the
DSL.  Unknown calls and unsupported expression forms are preserved as opaque
data instead of failing the parse; that is what lets pybrew read the whole of
homebrew-core without executing Ruby.

The parser understands:

* ``desc``, ``homepage``, ``url``/``sha256``/``version``, ``license``,
  ``revision``, ``version_scheme``, ``keg_only``, ``conflicts_with``;
* ``depends_on``/``uses_from_macos`` including build/test/optional tags;
* ``bottle`` blocks (all bottle tags, ``cellar``, ``root_url``, ``rebuild``);
* ``head``/``stable``/``resource``/``on_macos``-style blocks;
* ``install``/``test``/``caveats`` method bodies, captured verbatim for the
  source installer.
"""

from __future__ import annotations

import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .errors import FormulaParseError
from .models import (
    BottleFile,
    BottleSpec,
    Dependency,
    Formula,
    Opaque,
    SourceSpec,
    Sym,
    is_linux,
    is_macos,
)

_KEYWORDS = {
    "class",
    "module",
    "def",
    "end",
    "do",
    "if",
    "unless",
    "elsif",
    "else",
    "case",
    "when",
    "begin",
    "rescue",
    "ensure",
    "while",
    "until",
    "for",
    "return",
    "yield",
    "then",
    "and",
    "or",
    "not",
    "in",
}

_OP_MULTI = ("=>", "==", "!=", "<=", ">=", "&&", "||", "::", "..", "**", "+=", "-=", "&.")

_BLOCK_OPENERS = {"def", "do", "class", "module", "case", "begin", "while", "until", "for"}


@dataclass
class Token:
    kind: str
    value: Any
    start: int
    end: int
    line: int

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"Token({self.kind}, {self.value!r}, line={self.line})"


class Lexer:
    """Tokenise the Ruby subset used by formulae."""

    def __init__(self, source: str):
        self.source = source
        self.length = len(source)
        self.pos = 0
        self.line = 1
        self.tokens: list[Token] = []

    def tokenise(self) -> list[Token]:
        while self.pos < self.length:
            ch = self.source[self.pos]
            if ch in " \t\r\f\v":
                self.pos += 1
                continue
            if ch == "\\" and self.source[self.pos + 1 : self.pos + 2] == "\n":
                self.pos += 2
                self.line += 1
                continue
            if ch == "\n":
                self._emit("nl", "\n", self.pos, self.pos + 1)
                self.pos += 1
                self.line += 1
                continue
            if ch == "#":
                self._skip_comment()
                continue
            if self.source.startswith("=begin", self.pos) and self._at_line_start():
                self._skip_block_comment()
                continue
            if ch in ("'", '"'):
                self._read_string(ch)
                continue
            if ch == ":":
                if self._read_symbol():
                    continue
            if ch == "%" and self._read_percent_literal():
                continue
            if ch == "<" and self.source.startswith("<<", self.pos) and self._read_heredoc():
                continue
            if ch.isdigit():
                self._read_number()
                continue
            if ch.isalpha() or ch == "_":
                self._read_identifier()
                continue
            if ch == "/" and self._regex_allowed():
                if self._read_regex():
                    continue
            matched = False
            for op in _OP_MULTI:
                if self.source.startswith(op, self.pos):
                    self._emit("op", op, self.pos, self.pos + len(op))
                    self.pos += len(op)
                    matched = True
                    break
            if matched:
                continue
            self._emit("punct", ch, self.pos, self.pos + 1)
            self.pos += 1
        self.tokens.append(Token("eof", None, self.length, self.length, self.line))
        return self.tokens

    # -- helpers ---------------------------------------------------------
    def _emit(self, kind: str, value: Any, start: int, end: int) -> None:
        self.tokens.append(Token(kind, value, start, end, self.line))

    def _at_line_start(self) -> bool:
        idx = self.pos - 1
        while idx >= 0 and self.source[idx] in " \t":
            idx -= 1
        return idx < 0 or self.source[idx] == "\n"

    def _skip_comment(self) -> None:
        while self.pos < self.length and self.source[self.pos] != "\n":
            self.pos += 1

    def _skip_block_comment(self) -> None:
        end = self.source.find("=end", self.pos)
        if end == -1:
            self.pos = self.length
            return
        newlines = self.source[self.pos : end].count("\n")
        self.line += newlines
        self.pos = end + 4
        while self.pos < self.length and self.source[self.pos] != "\n":
            self.pos += 1

    def _read_string(self, quote: str) -> None:
        start = self.pos
        start_line = self.line
        self.pos += 1
        chars: list[str] = []
        while self.pos < self.length:
            ch = self.source[self.pos]
            if ch == "\\":
                nxt = self.source[self.pos + 1 : self.pos + 2]
                if nxt == "\n":
                    self.pos += 2
                    self.line += 1
                    continue
                escapes = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'"}
                if nxt in escapes:
                    chars.append(escapes[nxt])
                    self.pos += 2
                else:
                    chars.append("\\" + nxt)
                    self.pos += 2
                continue
            if ch == quote:
                self.pos += 1
                self._emit("str", "".join(chars), start, self.pos)
                return
            if ch == "\n":
                self.line += 1
            chars.append(ch)
            self.pos += 1
        raise FormulaParseError(f"unterminated string starting at line {start_line}")

    def _read_symbol(self) -> bool:
        start = self.pos
        if self.source.startswith("::", self.pos):
            return False
        idx = self.pos + 1
        if idx < self.length and self.source[idx] in ("'", '"'):
            quote = self.source[idx]
            end = self.source.find(quote, idx + 1)
            if end == -1:
                return False
            value = self.source[idx + 1 : end]
            self.pos = end + 1
            self._emit("sym", value, start, self.pos)
            return True
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*[?!=]?", self.source[idx:])
        if not match:
            return False
        self.pos = idx + match.end()
        self._emit("sym", match.group(0), start, self.pos)
        return True

    def _read_percent_literal(self) -> bool:
        match = re.match(r"%([wiWI])([\[({<|!/])", self.source[self.pos :])
        if not match:
            return False
        kind, opener = match.group(1), match.group(2)
        closer = {"[": "]", "(": ")", "{": "}", "<": ">", "|": "|", "!": "!", "/": "/"}[opener]
        start = self.pos
        idx = self.pos + match.end()
        end = self.source.find(closer, idx)
        if end == -1:
            return False
        body = self.source[idx:end]
        items = [Sym(item) for item in body.split()] if kind.lower() == "i" else body.split()
        self.pos = end + 1
        self._emit("syms" if kind.lower() == "i" else "words", items, start, self.pos)
        return True

    def _read_number(self) -> None:
        start = self.pos
        match = re.match(r"\d[\d_]*(\.\d[\d_]*)?", self.source[self.pos :])
        if not match:
            return
        raw = match.group(0).replace("_", "")
        self.pos += match.end()
        value: Any = float(raw) if "." in raw else int(raw)
        self._emit("num", value, start, self.pos)

    def _read_identifier(self) -> None:
        start = self.pos
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*[?!=]?", self.source[self.pos :])
        if not match:
            return
        value = match.group(0)
        self.pos += match.end()
        kind = "kw" if value in _KEYWORDS else ("const" if value[0].isupper() else "id")
        self._emit(kind, value, start, self.pos)

    def _read_heredoc(self) -> bool:
        match = re.match(r"<<([~-]?)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2", self.source[self.pos :])
        if not match:
            return False
        marker = match.group(3)
        indent = match.group(1) in ("~", "-")
        start = self.pos
        # Skip the rest of the opening line.
        newline = self.source.find("\n", self.pos)
        if newline == -1:
            return False
        self.pos = newline + 1
        self.line += 1
        content_lines: list[str] = []
        while self.pos < self.length:
            line_end = self.source.find("\n", self.pos)
            if line_end == -1:
                line_end = self.length
            raw_line = self.source[self.pos : line_end]
            stripped = raw_line.strip()
            if stripped == marker or (indent and stripped == marker):
                self.pos = line_end + 1 if line_end < self.length else self.length
                if line_end < self.length:
                    self.line += 1
                content = "\n".join(content_lines)
                self._emit("str", content, start, self.pos)
                return True
            content_lines.append(raw_line.lstrip() if match.group(1) == "~" else raw_line)
            self.pos = line_end + 1 if line_end < self.length else self.length
            if line_end < self.length:
                self.line += 1
        raise FormulaParseError(f"unterminated heredoc {marker} at line {self.line}")

    def _regex_allowed(self) -> bool:
        prev = self.tokens[-1] if self.tokens else None
        if prev is None or prev.kind == "nl":
            return True
        if prev.kind == "punct" and prev.value in {"(", "[", "{", ",", "=", "!", "~"}:
            return True
        if prev.kind == "op" and prev.value in {"=>", "&&", "||"}:
            return True
        if prev.kind == "id" and prev.value in {"regex", "regexp", "url"}:
            return True
        return False

    def _read_regex(self) -> bool:
        start = self.pos
        pos = self.pos + 1
        in_class = False
        while pos < self.length:
            ch = self.source[pos]
            if ch == "\\":
                pos += 2
                continue
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                pos += 1
                while pos < self.length and self.source[pos].isalpha():
                    pos += 1
                raw = self.source[start:pos]
                self.pos = pos
                self._emit("regex", raw, start, pos)
                return True
            pos += 1
        return False


class RubyFormulaParser:
    """Parse formula source into a :class:`pybrew.models.Formula`."""

    def __init__(
        self,
        source: str,
        path: Optional[Path] = None,
        name: Optional[str] = None,
        tap: Optional[str] = None,
    ):
        self.source = source
        self.path = Path(path) if path else None
        self.tap = tap
        derived_name = name or (self.path.stem if self.path else "unknown")
        self.formula = Formula(name=derived_name, path=self.path, tap=tap)
        self.tokens = Lexer(source).tokenise()
        self.pos = 0
        self._source_ctx = "stable"
        self._in_head = False
        self._conditional_depth = 0
        self._explicit_version = False

    # -- token helpers ---------------------------------------------------
    def peek(self, offset: int = 0) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def advance(self) -> Token:
        token = self.tokens[self.pos]
        if token.kind != "eof":
            self.pos += 1
        return token

    def skip_newlines(self) -> None:
        while self.peek().kind == "nl" or (self.peek().kind == "punct" and self.peek().value == ";"):
            self.advance()

    def _previous_significant(self) -> Optional[Token]:
        idx = self.pos - 1
        while idx >= 0:
            token = self.tokens[idx]
            if token.kind not in ("nl",):
                return token
            idx -= 1
        return None

    def _at_statement_start(self) -> bool:
        prev = self._previous_significant()
        return prev is None or prev.kind == "nl" or prev.value in (";", "then", "else", "elsif", "do")

    # -- entry point -----------------------------------------------------
    def parse(self) -> Formula:
        # Locate `class Name < Formula`; formulae may be preceded by comments
        # and `require` calls.
        while self.peek().kind != "eof":
            token = self.peek()
            if token.kind == "kw" and token.value == "class":
                self.advance()
                self._parse_class_header()
                break
            self.advance()
        else:
            raise FormulaParseError("no `class ... < Formula` declaration found")

        self._parse_body(end_on_end=True)
        if not self._explicit_version:
            from .version import detect_version

            tag = self.formula.stable.tag or ""
            if tag and detect_version(tag) != "HEAD":
                self.formula.version = detect_version(tag, self.formula.version_scheme)
            elif self.formula.stable.url:
                self.formula.version = detect_version(
                    self.formula.stable.url, self.formula.version_scheme
                )
        return self.formula

    def _parse_class_header(self) -> None:
        if self.peek().kind in ("const", "id"):
            class_name = self.advance().value
            if self.peek().kind == "op" and self.peek().value == "::":
                self.advance()
                if self.peek().kind in ("const", "id"):
                    class_name += "::" + str(self.advance().value)
            if not self.formula.name or self.formula.name == "unknown":
                self.formula.name = self._name_from_class(str(class_name))
        # Skip inheritance and any trailing tokens up to the class body.
        while self.peek().kind not in ("nl", "eof"):
            self.advance()
        self.skip_newlines()

    @staticmethod
    def _name_from_class(class_name: str) -> str:
        base = class_name.split("::")[-1]
        if not base:
            return "unknown"
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", base).lower()
        # PythonAT312 -> python@3.12, OpensslAT3 -> openssl@3.
        match = re.match(r"(.+?)_?at_?(\d+)$", snake)
        if match:
            name, digits = match.groups()
            if len(digits) > 1:
                version = f"{digits[0]}.{digits[1:]}"
                return f"{name}@{version}"
            return f"{name}@{digits}"
        return snake

    # -- statement parser -------------------------------------------------
    def _parse_body(self, end_on_end: bool, context: Optional[str] = None) -> None:
        while True:
            self.skip_newlines()
            token = self.peek()
            if token.kind == "eof":
                if end_on_end:
                    raise FormulaParseError("unexpected end of file inside class body")
                return
            if token.kind == "kw" and token.value == "end":
                self.advance()
                if end_on_end:
                    return
                return
            if token.kind == "kw":
                if token.value == "def":
                    self._parse_def()
                elif token.value in ("class", "module"):
                    self._skip_block(token)
                elif token.value in ("if", "unless", "case", "begin", "while", "until", "for"):
                    if token.value == "begin" and self._is_begin_rescue():
                        self._parse_conditional_block(context)
                    else:
                        self._skip_block(token)
                elif token.value in ("require", "return", "raise", "next", "break"):
                    self._skip_line()
                else:
                    self._skip_line()
                continue
            if token.kind == "const":
                self._skip_line()
                continue
            if token.kind == "id":
                self._parse_call(context)
                continue
            if token.kind == "punct" and token.value in (";",):
                self.advance()
                continue
            self.advance()

    def _is_begin_rescue(self) -> bool:
        # A `begin ... rescue` block has a nested structure; treating it as a
        # conditional block is adequate for skipping it.
        return True

    def _parse_conditional_block(self, context: Optional[str]) -> None:
        token = self.advance()  # if/unless/case/begin
        # Consume the condition up to the body.
        while self.peek().kind not in ("nl", "eof"):
            self.advance()
        self.skip_newlines()
        self._parse_body(end_on_end=True, context=context)

    def _parse_def(self) -> None:
        def_token = self.advance()  # def
        start = def_token.start
        name = ""
        # method name can be `self.foo`, `foo`, `foo=`.
        if self.peek().kind in ("id", "const"):
            name = str(self.advance().value)
            if self.peek().kind == "punct" and self.peek().value == ".":
                self.advance()
                if self.peek().kind in ("id", "const"):
                    name = str(self.advance().value)
        # Endless method: `def caveats = <<~EOS` / `def foo = expr`.
        if self._line_has_endless_equals():
            self._skip_line()
            return
        # Skip parameters and method signature.
        while self.peek().kind not in ("nl", "eof") and not (
            self.peek().kind == "punct" and self.peek().value == ";"
        ):
            self.advance()
        if self.peek().kind == "punct" and self.peek().value == ";":
            self.advance()
        self.skip_newlines()
        end_pos = self._skip_to_matching_end()
        raw = self.source[start:end_pos]
        if name == "install":
            self.formula.install_source = raw
        elif name == "test":
            self.formula.test_source = raw
        elif name == "caveats":
            self.formula.caveats = raw
        elif name == "post_install":
            self.formula.post_install_source = raw

    def _line_has_endless_equals(self) -> bool:
        """Detect ``def name(args) = expr`` on the current line."""

        depth = 0
        idx = self.pos
        while idx < len(self.tokens):
            token = self.tokens[idx]
            if token.kind == "nl" or token.kind == "eof":
                return False
            if token.kind == "punct" and token.value in "([{":
                depth += 1
            elif token.kind == "punct" and token.value in ")]}":
                depth -= 1
            elif token.kind in ("punct", "op") and token.value == "=" and depth == 0:
                return True
            idx += 1
        return False

    def _parse_call(self, context: Optional[str] = None) -> None:
        name_token = self.advance()
        name = str(name_token.value)
        # Receiver expression or assignment such as ENV.foo, foo = [...],
        # patch_checksum_pairs.each_slice(2) do ... end: not DSL, but any
        # attached do-block still has to be skipped.
        if self.peek().kind == "punct" and self.peek().value in (".", "="):
            self._skip_expression_statement()
            return

        args, kwargs = self._parse_arguments()
        # Trailing conditional modifier: `depends_on "foo" if build.with? "x"`
        if self.peek().kind == "kw" and self.peek().value in ("if", "unless"):
            self._skip_line()
        # Optional block, possibly on the following line.
        save = self.pos
        self.skip_newlines()
        block_token: Optional[Token] = None
        if self.peek().kind == "kw" and self.peek().value == "do":
            block_token = self.advance()
        elif self.peek().kind == "punct" and self.peek().value == "{":
            block_token = self.advance()
        else:
            self.pos = save
        self._handle_call(name, args, kwargs, block_token, context)
        if block_token is not None and name not in _BLOCK_CALLS:
            # Unknown block: make sure its body is consumed.
            if block_token.value == "{":
                self._skip_braces()
            else:
                current = self.peek()
                if not (current.kind == "kw" and current.value == "end"):
                    self._skip_to_matching_end()

    # -- argument parsing ------------------------------------------------
    def _parse_arguments(self) -> tuple[list[Any], dict[Any, Any]]:
        args: list[Any] = []
        kwargs: dict[Any, Any] = {}
        if self.peek().kind == "punct" and self.peek().value == "(":
            self.advance()
            while True:
                self.skip_newlines()
                if self.peek().kind == "punct" and self.peek().value == ")":
                    self.advance()
                    break
                if self.peek().kind == "eof":
                    break
                self._parse_argument(args, kwargs, paren=True)
                self.skip_newlines()
                if self.peek().kind == "punct" and self.peek().value == ",":
                    self.advance()
                    continue
                if self.peek().kind == "punct" and self.peek().value == ")":
                    self.advance()
                    break
                if self.peek().kind == "nl":
                    self.skip_newlines()
                    continue
                # Unexpected token: stop to avoid consuming the next statement.
                break
            return args, kwargs

        # Bare argument list: runs until end of line, block, or modifier.
        while True:
            self._skip_newlines_if_continuation()
            token = self.peek()
            if token.kind in ("nl", "eof"):
                break
            if token.kind == "kw" and token.value in ("do", "if", "unless"):
                break
            if token.kind == "punct" and token.value in ("{", "}"):
                break
            if token.kind == "kw" and token.value == "end":
                break
            if token.kind == "punct" and token.value == ",":
                self.advance()
                continue
            self._parse_argument(args, kwargs, paren=False)
            if self.peek().kind == "punct" and self.peek().value == ",":
                self.advance()
                # A trailing comma permits a continuation line.
                self.skip_newlines()
                continue
            if self.peek().kind == "punct" and self.peek().value == ")":
                self.advance()
                continue
            break
        return args, kwargs

    def _skip_newlines_if_continuation(self) -> None:
        # Inside a bare argument list a leading newline only appears when the
        # caller requested continuation; handled by the comma branch.
        return

    def _parse_argument(self, args: list[Any], kwargs: dict[Any, Any], paren: bool) -> None:
        token = self.peek()
        # Keyword label: `cellar: :any` or `"foo" => :bar`.
        if token.kind == "id" and self.peek(1).kind == "punct" and self.peek(1).value == ":":
            label = str(self.advance().value)
            self.advance()  # :
            kwargs[Sym(label)] = self._parse_expression()
            return
        if token.kind == "str" and self.peek(1).kind == "punct" and self.peek(1).value == ":":
            label = str(self.advance().value)
            self.advance()
            kwargs[Sym(label)] = self._parse_expression()
            return

        value = self._parse_expression()
        if self.peek().kind == "op" and self.peek().value == "=>":
            self.advance()
            rhs = self._parse_expression()
            key = str(value) if isinstance(value, (str, Sym)) else str(value)
            if isinstance(key, str):
                kwargs[key] = rhs
            else:
                args.append({value: rhs})
            return
        args.append(value)

    def _parse_expression(self) -> Any:
        token = self.peek()
        if token.kind == "str":
            self.advance()
            return token.value
        if token.kind == "sym":
            self.advance()
            return Sym(str(token.value))
        if token.kind == "num":
            self.advance()
            return token.value
        if token.kind == "words":
            self.advance()
            return list(token.value)
        if token.kind == "syms":
            self.advance()
            return [Sym(str(item)) for item in token.value]
        if token.kind == "regex":
            self.advance()
            return Opaque(str(token.value))
        if token.kind == "punct" and token.value == "[":
            return self._parse_list()
        if token.kind == "punct" and token.value == "{":
            return self._parse_hash()
        if token.kind == "punct" and token.value == "(":
            self.advance()
            value = self._parse_expression()
            self.skip_newlines()
            if self.peek().kind == "punct" and self.peek().value == ")":
                self.advance()
            return value
        if token.kind == "const":
            return self._parse_opaque()
        if token.kind == "id":
            if token.value in ("true", "false", "nil"):
                self.advance()
                return {"true": True, "false": False, "nil": None}[str(token.value)]
            return self._parse_opaque()
        # Unrecognised token: consume it as opaque.
        start = token.start
        self.advance()
        end = token.end
        return Opaque(self.source[start:end])

    def _parse_list(self) -> list[Any]:
        self.advance()  # [
        items: list[Any] = []
        while True:
            self.skip_newlines()
            if self.peek().kind == "punct" and self.peek().value == "]":
                self.advance()
                break
            if self.peek().kind == "eof":
                break
            items.append(self._parse_expression())
            if self.peek().kind == "op" and self.peek().value == "=>":
                self.advance()
                rhs = self._parse_expression()
                if items:
                    items[-1] = {items[-1]: rhs}
            self.skip_newlines()
            if self.peek().kind == "punct" and self.peek().value == ",":
                self.advance()
                continue
            if self.peek().kind == "punct" and self.peek().value == "]":
                self.advance()
                break
            break
        return items

    def _parse_hash(self) -> dict[Any, Any]:
        self.advance()  # {
        result: dict[Any, Any] = {}
        while True:
            self.skip_newlines()
            if self.peek().kind == "punct" and self.peek().value == "}":
                self.advance()
                break
            if self.peek().kind == "eof":
                break
            if self.peek().kind == "id" and self.peek(1).kind == "punct" and self.peek(1).value == ":":
                key: Any = Sym(str(self.advance().value))
                self.advance()
            elif self.peek().kind == "str" and self.peek(1).kind == "punct" and self.peek(1).value == ":":
                key = Sym(str(self.advance().value))
                self.advance()
            else:
                key = self._parse_expression()
                if self.peek().kind == "op" and self.peek().value == "=>":
                    self.advance()
            value = self._parse_expression()
            result[key] = value
            self.skip_newlines()
            if self.peek().kind == "punct" and self.peek().value == ",":
                self.advance()
                continue
            if self.peek().kind == "punct" and self.peek().value == "}":
                self.advance()
                break
            break
        return result

    def _parse_opaque(self) -> Opaque:
        start = self.peek().start
        pieces: list[str] = []
        depth = 0
        while self.peek().kind != "eof":
            token = self.peek()
            if token.kind == "nl" and depth == 0:
                break
            if token.kind == "punct" and token.value in "[({":
                depth += 1
            elif token.kind == "punct" and token.value in "])}":
                if depth == 0:
                    break
                depth -= 1
            if token.kind == "kw" and token.value in ("do", "if", "unless", "end"):
                break
            if token.kind == "punct" and token.value == "," and depth == 0:
                break
            pieces.append(self.source[token.start : token.end])
            self.advance()
            if self._opaque_piece_complete():
                break
            if depth == 0 and self.peek().kind not in ("punct", "op"):
                break
        return Opaque(" ".join(pieces).strip())

    def _opaque_piece_complete(self) -> bool:
        # A constant/method expression such as `MacOSVersion::SYMBOL` or
        # `OS.mac?` is complete once we have consumed a `::`/`.` chain.
        prev = self.target_previous()
        return prev is not None and prev.kind in ("const", "id") and prev.value not in ("MacOSVersion",)

    def target_previous(self) -> Optional[Token]:
        idx = self.pos - 1
        while idx >= 0 and self.tokens[idx].kind == "nl":
            idx -= 1
        return self.tokens[idx] if idx >= 0 else None

    # -- block skipping --------------------------------------------------
    def _skip_line(self) -> None:
        while self.peek().kind not in ("nl", "eof"):
            self.advance()
        if self.peek().kind == "nl":
            self.advance()

    def _skip_expression_statement(self) -> None:
        """Skip a non-DSL expression, including spanning lines and do-blocks."""

        depth = 0
        while self.peek().kind != "eof":
            token = self.peek()
            if token.kind == "kw" and token.value == "do" and depth == 0:
                self.advance()
                self._skip_to_matching_end()
                return
            if token.kind == "punct" and token.value == "{" and depth == 0:
                self.advance()
                self._skip_braces()
                return
            if token.kind == "punct" and token.value in "([{":
                depth += 1
            elif token.kind == "punct" and token.value in ")]}":
                depth = max(0, depth - 1)
            if token.kind == "nl" and depth == 0:
                self.advance()
                return
            self.advance()

    def _skip_braces(self) -> None:
        depth = 1
        while self.peek().kind != "eof":
            token = self.advance()
            if token.kind == "punct" and token.value == "{":
                depth += 1
            elif token.kind == "punct" and token.value == "}":
                depth -= 1
                if depth == 0:
                    return

    def _skip_block(self, opener: Token) -> None:
        """Skip a block starting at ``opener`` through its matching end."""

        if opener.kind == "punct" and opener.value == "{":
            self._skip_braces()
            return
        depth = 1
        prev = opener
        while self.peek().kind != "eof":
            token = self.advance()
            if self._opens_block(token, prev):
                depth += 1
            elif token.kind == "kw" and token.value == "end":
                depth -= 1
                if depth == 0:
                    return
            prev = token
        raise FormulaParseError(f"unterminated block beginning at line {opener.line}")

    def _skip_to_matching_end(self) -> int:
        """Consume through an `end` token and return its end offset."""

        depth = 1
        prev: Optional[Token] = None
        end_pos = self.source.__len__()
        while self.peek().kind != "eof":
            token = self.advance()
            if self._opens_block(token, prev):
                depth += 1
            elif token.kind == "kw" and token.value == "end":
                depth -= 1
                if depth == 0:
                    end_pos = token.end
                    break
            prev = token
        return end_pos

    @staticmethod
    def _opens_block(token: Token, prev: Optional[Token]) -> bool:
        if token.kind != "kw":
            return False
        if token.value in _BLOCK_OPENERS:
            return True
        if token.value in ("if", "unless"):
            if prev is None or prev.kind == "nl":
                return True
            return prev.value in (";", "then", "do", "else", "elsif", "begin", "rescue")
        return False

    # -- DSL handlers ----------------------------------------------------
    def _handle_call(
        self,
        name: str,
        args: list[Any],
        kwargs: dict[Any, Any],
        block: Optional[Token],
        context: Optional[str],
    ) -> None:
        if name in ("on_macos", "on_linux", "on_arm", "on_arm64", "on_intel", "on_x86_64"):
            if block is not None and self._platform_guard(name):
                self._parse_body(end_on_end=True, context=context)
            elif block is not None:
                self._skip_block(block)
            return
        if name in ("head", "stable"):
            if block is not None:
                previous = self._source_ctx
                previous_head = self._in_head
                self._source_ctx = name
                self._in_head = name == "head"
                self._parse_source_block(name)
                self._source_ctx = previous
                self._in_head = previous_head
            elif args:
                self._handle_url(args, kwargs, name)
            return
        if name == "bottle":
            if block is not None:
                self._parse_bottle_block(block)
            elif args and isinstance(args[0], Sym) and str(args[0]) == "unneeded":
                self.formula.bottle = BottleSpec()
            return
        if name == "resource":
            if block is not None:
                self._parse_resource_block(args, kwargs, block)
            return
        if name in ("test", "livecheck", "patch", "service", "post_install"):
            if block is not None:
                start = block.start
                end = self._skip_to_matching_end()
                raw = self.source[start:end]
                if name == "service":
                    self.formula.service = {"source": raw}
                elif name == "test":
                    self.formula.test_source = raw
                elif name == "post_install":
                    self.formula.post_install_source = raw
            return
        if name == "descriptions" or name == "deprecate_args":
            if block is not None:
                self._skip_block(block)
            return

        if name == "desc":
            if args:
                self.formula.desc = str(args[0])
        elif name == "homepage":
            if args:
                self.formula.homepage = str(args[0])
        elif name == "url":
            self._handle_url(args, kwargs, self._source_ctx)
        elif name == "mirror":
            if args:
                self.formula.stable.mirrors.append(str(args[0]))
        elif name == "sha256":
            digest = self._extract_sha256(args, kwargs)
            if digest:
                if self._source_ctx == "head":
                    self.formula.head.sha256 = digest
                else:
                    self.formula.stable.sha256 = digest
        elif name == "version":
            if args:
                self._explicit_version = True
                self.formula.version = self._scalar(args[0])
        elif name == "revision":
            if args:
                try:
                    self.formula.revision = int(args[0])
                except (TypeError, ValueError):
                    pass
        elif name == "version_scheme":
            if args:
                try:
                    self.formula.version_scheme = int(args[0])
                except (TypeError, ValueError):
                    pass
        elif name == "compatibility_version":
            if args:
                try:
                    self.formula.compatibility_version = int(args[0])
                except (TypeError, ValueError):
                    pass
        elif name == "license":
            if args:
                self.formula.license = self._format_license(args[0])
            elif kwargs:
                self.formula.license = self._format_license(kwargs)
        elif name == "keg_only":
            self.formula.keg_only = True
            reason = args[0] if args else kwargs.get(Sym("reason"))
            if reason is not None:
                self.formula.keg_only_reason = str(reason)
        elif name == "depends_on":
            target = self.formula.head_dependencies if self._in_head else self.formula.dependencies
            self._handle_depends_on(args, kwargs, target)
        elif name == "uses_from_macos":
            target = self.formula.head_dependencies if self._in_head else self.formula.uses_from_macos
            self._handle_depends_on(args, kwargs, target)
        elif name == "conflicts_with":
            if args:
                self.formula.conflicts_with.append(str(args[0]))
        elif name == "link_overwrite":
            for item in args:
                if isinstance(item, list):
                    self.formula.link_overwrite.extend(str(i) for i in item)
                else:
                    self.formula.link_overwrite.append(str(item))
        elif name == "bottle" and args:
            pass
        elif name in ("deprecated!", "deprecate!"):
            self.formula.deprecated = True
            reason = kwargs.get(Sym("because")) or (args[0] if args else None)
            if reason is not None:
                self.formula.deprecation_reason = str(reason)
        elif name == "disable!":
            self.formula.disabled = True
            reason = kwargs.get(Sym("because")) or (args[0] if args else None)
            if reason is not None:
                self.formula.disable_reason = str(reason)
        elif name == "deny_network_access!":
            self.formula.deny_network_access = True
        elif name == "option":
            self.formula.extra.setdefault("option", []).append(list(args))
        elif name in {"deprecate!", "disable!"}:  # pragma: no cover
            pass
        else:
            self.formula.extra.setdefault(name, []).append({"args": list(args), "kwargs": dict(kwargs)})

    @staticmethod
    def _platform_guard(name: str) -> bool:
        import os
        import sys

        target_os = os.environ.get("PYBREW_TARGET_OS") or sys.platform
        machine = (os.environ.get("PYBREW_TARGET_ARCH") or platform.machine()).lower()
        if name == "on_macos":
            return target_os == "darwin"
        if name == "on_linux":
            return target_os.startswith("linux")
        if name in ("on_arm", "on_arm64"):
            return machine in ("arm64", "aarch64")
        if name in ("on_intel", "on_x86_64"):
            return machine in ("x86_64", "amd64", "i386")
        return False

    @classmethod
    def _format_license(cls, value: Any) -> Any:
        """Render Homebrew's license DSL as the string used by brew JSON."""

        if isinstance(value, Sym):
            return str(value)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " AND ".join(cls._license_atom(item) for item in value)
        if isinstance(value, dict):
            pairs = {str(key): item for key, item in value.items()}
            if "all_of" in pairs:
                return " AND ".join(cls._license_atom(item) for item in pairs["all_of"])
            if "any_of" in pairs:
                return " OR ".join(cls._license_atom(item) for item in pairs["any_of"])
            if len(pairs) == 1:
                key, item = next(iter(pairs.items()))
                if isinstance(item, dict):
                    modifiers = {str(k): v for k, v in item.items()}
                    if "with" in modifiers:
                        return f"({key} WITH {modifiers['with']})"
                return key
        return str(value)

    @classmethod
    def _license_atom(cls, item: Any) -> str:
        if isinstance(item, dict):
            pairs = {str(key): value for key, value in item.items()}
            if len(pairs) == 1:
                key, value = next(iter(pairs.items()))
                if isinstance(value, dict) and "with" in {str(k) for k in value}:
                    exception = next(v for k, v in value.items() if str(k) == "with")
                    return f"({key} WITH {exception})"
                return key
            return str(cls._format_license(item))
        return str(item)

    def _parse_source_block(self, which: str) -> None:
        # The block contents are ordinary DSL calls with a changed source
        # context; _parse_body dispatches them through _handle_call.
        self._parse_body(end_on_end=True)
        # _parse_body consumed the matching `end`.

    def _handle_url(self, args: list[Any], kwargs: dict[Any, Any], source_ctx: str) -> None:
        spec = self.formula.head if source_ctx == "head" else self.formula.stable
        if args:
            first = args[0]
            if isinstance(first, Sym) and str(first) == "stable":
                return
            spec.url = str(first)
        for key, value in kwargs.items():
            key_name = str(key)
            if key_name == "tag":
                spec.tag = str(value)
            elif key_name == "revision":
                spec.revision = str(value)
            elif key_name == "branch":
                spec.branch = str(value)
            elif key_name == "using":
                spec.using = str(value)
        if source_ctx == "head" and self.formula.head.url:
            self.formula.head.url = spec.url
        if spec.url and source_ctx == "stable" and self.formula.version is None:
            # __post_init__ handles this, but keeping it explicit helps if a
            # later `version` call appears.
            pass

    def _extract_sha256(self, args: list[Any], kwargs: dict[Any, Any]) -> Optional[str]:
        if args:
            candidate = str(args[0])
            if re.fullmatch(r"[a-fA-F0-9]{64}", candidate):
                return candidate.lower()
        for value in kwargs.values():
            if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{64}", value):
                return value.lower()
        return None

    def _parse_bottle_block(self, block: Token) -> None:
        root_url = self.formula.bottle.root_url
        rebuild = self.formula.bottle.rebuild
        while True:
            self.skip_newlines()
            token = self.peek()
            if token.kind == "eof":
                raise FormulaParseError("unterminated bottle block")
            if token.kind == "kw" and token.value == "end":
                self.advance()
                break
            if token.kind != "id":
                self._skip_line()
                continue
            name = str(self.advance().value)
            args, kwargs = self._parse_arguments()
            if name == "root_url" and args:
                root_url = str(args[0])
            elif name == "rebuild" and args:
                try:
                    rebuild = int(args[0])
                except (TypeError, ValueError):
                    pass
            elif name == "sha256":
                self._add_bottle_sha256(args, kwargs)
            else:
                self._skip_line()
        self.formula.bottle.root_url = root_url
        self.formula.bottle.rebuild = rebuild

    def _add_bottle_sha256(self, args: list[Any], kwargs: dict[Any, Any]) -> None:
        # `sha256 cellar: :any, arm64_golden_gate: "<digest>", ...`
        hash_args: dict[Any, Any] = {}
        if args and isinstance(args[0], dict):
            hash_args.update(args[0])
        hash_args.update(kwargs)
        cellar = hash_args.get(Sym("cellar"), hash_args.get("cellar", ":any"))
        for key, value in hash_args.items():
            key_name = str(key)
            if key_name == "cellar":
                continue
            if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{64}", value):
                self.formula.bottle.files[key_name] = BottleFile(
                    tag=key_name,
                    sha256=value.lower(),
                    cellar=str(cellar),
                )

    def _parse_resource_block(
        self, args: list[Any], kwargs: dict[Any, Any], block: Token
    ) -> None:
        resource_name = str(args[0]) if args else f"resource-{len(self.formula.resources)}"
        spec = SourceSpec()
        while True:
            self.skip_newlines()
            token = self.peek()
            if token.kind == "eof":
                raise FormulaParseError("unterminated resource block")
            if token.kind == "kw" and token.value == "end":
                self.advance()
                break
            if token.kind != "id":
                self._skip_line()
                continue
            inner = str(self.advance().value)
            inner_args, inner_kwargs = self._parse_arguments()
            if inner == "url" and inner_args:
                spec.url = str(inner_args[0])
                for key, value in inner_kwargs.items():
                    if str(key) == "using":
                        spec.using = str(value)
            elif inner == "sha256" and inner_args:
                spec.sha256 = str(inner_args[0])
            elif inner == "version" and inner_args:
                spec.tag = None
            else:
                if self.peek().kind == "kw" and self.peek().value == "do":
                    self._skip_block(self.advance())
        self.formula.resources[resource_name] = spec

    def _handle_depends_on(
        self, args: list[Any], kwargs: dict[Any, Any], target: list[Dependency]
    ) -> None:
        entries: list[tuple[str, Any]] = []
        positional_names: list[str] = []
        positional_tags: Any = None
        for arg in args:
            if isinstance(arg, dict):
                for key, value in arg.items():
                    if isinstance(key, str) and not isinstance(key, Sym):
                        entries.append((key, value))
            elif isinstance(arg, Sym):
                positional_tags = arg
            elif isinstance(arg, str):
                positional_names.append(arg)
        for key, value in kwargs.items():
            if isinstance(key, str) and not isinstance(key, Sym):
                entries.append((key, value))
        if positional_names:
            if not entries:
                entries.extend((name, positional_tags) for name in positional_names)
            else:
                entries.extend((name, positional_tags) for name in positional_names)
        for name, tags in entries:
            if name in ("macos", "arch", "xcode", "linux"):
                continue
            tag_set = self._normalise_tags(tags)
            self._add_dependency(target, name, tag_set)

    @staticmethod
    def _normalise_tags(value: Any) -> frozenset[str]:
        if value is None:
            return frozenset()
        if isinstance(value, (str, Sym)):
            return frozenset({str(value).lstrip(":")})
        if isinstance(value, (list, tuple, set)):
            return frozenset(str(item).lstrip(":") for item in value)
        return frozenset()

    def _add_dependency(self, target: list[Dependency], name: str, tags: frozenset[str]) -> None:
        # Keep duplicate names with different tag sets: a formula can depend on
        # the same formula both at build time and at runtime (e.g. gettext in
        # git/openssl@3).
        candidate = Dependency(name, tags)
        if candidate not in target:
            target.append(candidate)

    @staticmethod
    def _scalar(value: Any) -> str:
        if isinstance(value, (Sym, Opaque)):
            return str(value)
        return str(value)


_BLOCK_CALLS = {
    "bottle",
    "head",
    "stable",
    "resource",
    "test",
    "livecheck",
    "patch",
    "service",
    "on_macos",
    "on_linux",
    "on_arm",
    "on_arm64",
    "on_intel",
    "on_x86_64",
    "descriptions",
    "deprecate_args",
}


def parse_formula_source(
    source: str,
    path: Optional[Path] = None,
    name: Optional[str] = None,
    tap: Optional[str] = None,
) -> Formula:
    """Parse formula Ruby source into a :class:`Formula`."""

    return RubyFormulaParser(source, path=path, name=name, tap=tap).parse()


def parse_formula_file(path: str | Path, tap: Optional[str] = None) -> Formula:
    formula_path = Path(path)
    source = formula_path.read_text(encoding="utf-8", errors="replace")
    return parse_formula_source(source, path=formula_path, tap=tap)
