"""Opt-in execution of a formula's raw ``def install`` body with Ruby.

The default source installer in :mod:`pybrew.source_builder` understands a
deliberately small, declarative subset of the Homebrew DSL.  Some formulae use
arbitrary Ruby (loops, string manipulation, helper methods, ...) inside
``def install`` and cannot be expressed in that subset.

This module is the explicit escape hatch: when ``PYBREW_ALLOW_ARBITRARY_RUBY``
is set to ``1``, :class:`pybrew.source_builder.SourceBuilder` hands the raw
``def install`` body to the host Ruby interpreter (``/usr/bin/ruby`` or the
``PYBREW_RUBY`` override) together with a small compatibility shim for the
common Homebrew install DSL.  Nothing here runs unless that environment
variable is set, so the default behaviour of pybrew is unchanged.

The shim is intentionally Ruby 2.6 compatible because that is the system Ruby
shipped by macOS.  Failures are reported as :class:`pybrew.errors.BuildError`
with the Ruby stderr output and the formula line number when it can be
determined.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .errors import BuildError, UnsupportedInstall
from .models import Formula
from .paths import Config
from .ruby_parser import Lexer, Token

#: Environment variable that opts a build into the arbitrary-Ruby bridge.
ALLOW_ENV = "PYBREW_ALLOW_ARBITRARY_RUBY"

#: Environment variable overriding the Ruby interpreter used by the bridge.
RUBY_ENV = "PYBREW_RUBY"

#: System Ruby on macOS (also present on most Linux distributions).
DEFAULT_RUBY = "/usr/bin/ruby"

#: Upper bound for one Ruby install body.  Builds can legitimately be slow;
#: this matches the timeout used for ``system`` calls in the default executor.
RUBY_TIMEOUT = 1800

_PATH_NAMES = (
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
    "frameworks",
    "doc",
    "info",
    "locale",
    "man",
    "man1",
    "man2",
    "man3",
    "man4",
    "man5",
    "man6",
    "man7",
    "man8",
    "elisp",
    "vim",
    "staged_path",
    "buildpath",
)


def arbitrary_ruby_enabled() -> bool:
    """Return true when the opt-in arbitrary-Ruby bridge was requested."""

    return os.environ.get(ALLOW_ENV, "") == "1"


def _ruby_binary(formula: Formula) -> str:
    candidate = os.environ.get(RUBY_ENV) or DEFAULT_RUBY
    resolved = shutil.which(candidate)
    if resolved is None:
        raise BuildError(
            f"{formula.name}: Ruby interpreter not found: {candidate!r} "
            f"(set {RUBY_ENV} to an executable Ruby)"
        )
    return resolved


def _install_paths(
    formula: Formula, config: Config, keg: Path, build_root: Path
) -> dict[str, str]:
    """Return the Homebrew-style install path map handed to the Ruby shim.

    This mirrors ``BuildContext.path_for`` (the default executor's path map)
    and adds a few extra directories that arbitrary formulae commonly use.
    """

    man_root = keg / "share" / "man"
    mapping = {
        "prefix": keg,
        "bin": keg / "bin",
        "sbin": keg / "sbin",
        "lib": keg / "lib",
        "include": keg / "include",
        "share": keg / "share",
        "etc": keg / "etc",
        "libexec": keg / "libexec",
        "var": config.prefix / "var",
        "opt_prefix": config.opt_path(formula.name),
        "opt_bin": config.opt_path(formula.name) / "bin",
        "pkgshare": keg / "share" / formula.name,
        "appdir": config.applications,
        "frameworks": keg / "Frameworks",
        "doc": keg / "share" / "doc" / formula.name,
        "info": keg / "share" / "info",
        "locale": keg / "share" / "locale",
        "man": man_root,
        "elisp": keg / "share" / "emacs" / "site-lisp",
        "vim": keg / "share" / "vim" / "vimfiles",
        "staged_path": build_root,
        "buildpath": build_root,
    }
    for number in range(1, 9):
        mapping[f"man{number}"] = man_root / f"man{number}"
    return {name: str(mapping[name]) for name in _PATH_NAMES}


def _find_def_install(tokens: list[Token]) -> Optional[tuple[int, int]]:
    """Locate ``def install`` (or ``def self.install``) in ``tokens``.

    Returns ``(def_index, signature_end_index)`` where ``signature_end_index``
    is the index of the newline/semicolon terminating the method signature, or
    ``None`` when the source does not define an install method.
    """

    for index, token in enumerate(tokens):
        if token.kind != "kw" or token.value != "def":
            continue
        cursor = index + 1
        while cursor < len(tokens) and tokens[cursor].kind == "nl":
            cursor += 1
        if cursor >= len(tokens):
            continue
        name_token = tokens[cursor]
        if name_token.kind not in ("id", "const"):
            continue
        name = str(name_token.value)
        cursor += 1
        if (
            cursor + 1 < len(tokens)
            and tokens[cursor].kind == "punct"
            and tokens[cursor].value == "."
            and tokens[cursor + 1].kind in ("id", "const")
        ):
            name = str(tokens[cursor + 1].value)
            cursor += 2
        if name != "install":
            continue
        # Skip the rest of the signature up to the newline (or ``;`` for the
        # rare single-line form).
        while cursor < len(tokens):
            current = tokens[cursor]
            if current.kind == "nl" or (current.kind == "punct" and current.value == ";"):
                break
            cursor += 1
        return index, cursor
    return None


def extract_install_body(formula: Formula) -> tuple[str, int]:
    """Return the raw body of ``def install`` and its first source line.

    The body excludes the ``def install`` signature and the closing ``end``.
    Line numbers are 1-based within ``formula.install_source`` (which itself
    starts at the ``def install`` line).
    """

    source = formula.install_source or ""
    if not source.strip():
        raise UnsupportedInstall(f"{formula.name}: no def install block")
    tokens = Lexer(source).tokenise()
    located = _find_def_install(tokens)
    if located is None:
        raise UnsupportedInstall(f"{formula.name}: cannot find def install")
    _def_index, signature_end = located

    end_index: Optional[int] = None
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        if token.kind in ("nl", "eof"):
            continue
        end_index = index
        break
    if end_index is None or not (
        tokens[end_index].kind == "kw" and tokens[end_index].value == "end"
    ):
        raise UnsupportedInstall(f"{formula.name}: unterminated def install")

    body_start = signature_end + 1
    while body_start < len(tokens) and tokens[body_start].kind in ("nl", "eof"):
        body_start += 1
    if body_start >= end_index:
        return "", tokens[end_index].line

    first = tokens[body_start]
    body = source[first.start : tokens[end_index].start]
    return body, first.line


# ---------------------------------------------------------------------------
# Ruby compatibility shim
# ---------------------------------------------------------------------------

# The shim is generated per build with two placeholders replaced by base64
# encoded JSON (configuration) and base64 encoded Ruby source (the formula's
# raw install body).  Base64 keeps the generated script valid regardless of
# quotes, heredocs or non-ASCII text in the formula.
_RUBY_SHIM = r'''
require "base64"
require "fileutils"
require "json"
require "pathname"
require "rbconfig"

$stdout.sync = true
$stderr.sync = true

PYBREW_CONFIG = JSON.parse(Base64.strict_decode64("__PYBREW_CONFIG__"))
PYBREW_BODY = Base64.strict_decode64("__PYBREW_BODY__")

module OS
  def self.mac?
    !!(RUBY_PLATFORM =~ /darwin/)
  end

  def self.linux?
    !!(RUBY_PLATFORM =~ /linux/)
  end
end

module Hardware
  module CPU
    HOST_CPU = (PYBREW_CONFIG["arch"] || RbConfig::CONFIG["host_cpu"]).to_s.downcase

    def self.arm?
      !!(HOST_CPU =~ /arm|aarch64/)
    end

    def self.arm64?
      arm?
    end

    def self.intel?
      !!(HOST_CPU =~ /x86_64|amd64|i[3-6]86/)
    end

    def self.x86_64?
      intel?
    end

    def self.is_arm?
      arm?
    end

    def self.is_intel?
      intel?
    end

    def self.cores
      require "etc"
      Etc.nprocessors
    rescue StandardError
      1
    end
  end
end

class PybrewOptions < Array
  def include?(value)
    super(value.to_s)
  end

  def with?(name)
    include?("--with-#{name}")
  end

  def without?(name)
    !with?(name)
  end
end

class PybrewBuild
  def head?
    false
  end

  def stable?
    true
  end

  def bottle?
    false
  end

  def build_bottle?
    false
  end

  def universal?
    false
  end

  def with?(*_args)
    false
  end

  def without?(*_args)
    true
  end
end

class PybrewVersion < String
  def major
    split(".")[0].to_s
  end

  def minor
    split(".")[1].to_s
  end

  def major_minor
    split(".")[0, 2].join(".")
  end

  def dots_to_underscores
    tr(".", "_")
  end

  def dots_to_hyphens
    tr(".", "-")
  end
end

module PybrewInstall
  module_function

  def install_into(destination, sources)
    FileUtils.mkdir_p(destination.to_s)
    sources.flatten.each do |source|
      if source.is_a?(Hash)
        source.each { |src, name| install_one(destination, src, name) }
      else
        install_one(destination, source, nil)
      end
    end
  end

  def install_one(destination, source, name)
    matches = expand(source)
    if matches.empty?
      raise "install: source not found: #{source.inspect} (in #{Dir.pwd})"
    end
    matches.each do |match|
      src = Pathname.new(match)
      dst = destination + (name ? name.to_s : src.basename.to_s)
      FileUtils.rm_rf(dst.to_s) if dst.exist? || dst.symlink?
      if src.directory?
        FileUtils.cp_r(src.to_s, dst.to_s)
      else
        FileUtils.cp(src.to_s, dst.to_s)
        File.chmod(src.stat.mode & 0o777, dst.to_s)
      end
    end
  end

  def install_symlinks(destination, sources)
    FileUtils.mkdir_p(destination.to_s)
    sources.flatten.each do |source|
      if source.is_a?(Hash)
        source.each { |src, name| symlink_one(destination, src, name) }
      else
        symlink_one(destination, source, nil)
      end
    end
  end

  def symlink_one(destination, source, name)
    matches = expand(source)
    matches = [source.to_s] if matches.empty?
    matches.each do |match|
      target = Pathname.new(File.expand_path(match))
      create_link(destination, target, name)
    end
  end

  def create_link(destination, target, name)
    link = destination + (name ? name.to_s : target.basename.to_s)
    FileUtils.rm_rf(link.to_s) if link.exist? || link.symlink?
    base = Pathname.new(File.expand_path(destination.to_s))
    File.symlink(target.relative_path_from(base).to_s, link.to_s)
  end

  def expand(source)
    pattern = source.to_s
    matches = Dir.glob(pattern)
    return matches unless matches.empty?
    return [pattern] if File.exist?(pattern) || File.symlink?(pattern)
    []
  end
end

module PybrewPathHelpers
  def install(*sources)
    PybrewInstall.install_into(self, sources)
    nil
  end

  def install_symlink(*sources)
    PybrewInstall.install_symlinks(self, sources)
    nil
  end

  def mkdir(*_args)
    FileUtils.mkdir_p(to_s)
    nil
  end

  def write(content, *_args)
    FileUtils.mkdir_p(dirname.to_s)
    File.binwrite(to_s, content.to_s)
    content.to_s.bytesize
  end
end

Pathname.prepend(PybrewPathHelpers)

class << ENV
  def append_path(name, value)
    current = self[name]
    parts = [current, value.to_s].compact.reject(&:empty?)
    self[name] = parts.join(File::PATH_SEPARATOR)
  end

  def prepend_path(name, value)
    current = self[name]
    parts = [value.to_s, current].compact.reject(&:empty?)
    self[name] = parts.join(File::PATH_SEPARATOR)
  end

  def prepend_create_path(name, value)
    FileUtils.mkdir_p(value.to_s)
    prepend_path(name, value)
  end

  def append(name, value)
    current = self[name]
    parts = [current, value.to_s].compact.reject(&:empty?)
    self[name] = parts.join(" ")
  end

  def prepend(name, value)
    current = self[name]
    parts = [value.to_s, current].compact.reject(&:empty?)
    self[name] = parts.join(" ")
  end
end

class PybrewInstallContext
  include FileUtils

  PATH_METHODS = %w[
    prefix bin sbin lib include share etc libexec var opt_prefix opt_bin
    pkgshare appdir frameworks doc info locale man man1 man2 man3 man4 man5
    man6 man7 man8 elisp vim
  ].freeze

  def initialize(config)
    @config = config
    @paths = {}
    (config["paths"] || {}).each do |name, value|
      @paths[name.to_s] = Pathname.new(value)
    end
    @formula_name = config["formula"]["name"].to_s
    @formula_version = PybrewVersion.new(config["formula"]["version"].to_s)
    @build = PybrewBuild.new
    @args = PybrewOptions.new
    @verbose = !!config["verbose"]
  end

  PATH_METHODS.each do |method_name|
    define_method(method_name) do
      @paths[method_name] || Pathname.new(File.join(@paths["prefix"].to_s, method_name))
    end
  end

  def name
    @formula_name
  end

  def formula
    self
  end

  def version
    @formula_version
  end

  def build
    @build
  end

  def args
    @args
  end

  def buildpath
    @paths["buildpath"]
  end

  def staged_path
    @paths["staged_path"]
  end

  def testpath
    Pathname.new(File.join(@paths["prefix"].to_s, "test"))
  end

  def system(*args)
    environment = {}
    args = args.dup
    environment = args.shift if args.first.is_a?(Hash)
    command = args.flatten.compact.map(&:to_s)
    raise ArgumentError, "system: no command given" if command.empty?
    ohai("system #{command.join(' ')}") if @verbose
    success = Kernel.system(environment, *command)
    raise "Failed executing: #{command.join(' ')}" unless success
    true
  end

  alias safe_system system

  def quiet_system(*args)
    system(*args)
    true
  rescue RuntimeError, ArgumentError
    false
  end

  def cd(path, &block)
    if block
      Dir.chdir(path.to_s, &block)
    else
      Dir.chdir(path.to_s)
      nil
    end
  end

  def inreplace(paths, before = nil, after = nil)
    Array(paths).flatten.each do |pattern|
      matches = Dir.glob(pattern.to_s)
      matches = [pattern.to_s] if matches.empty?
      matches.each do |file|
        path = Pathname.new(file)
        raise "inreplace: file not found: #{file}" unless path.file?
        text = path.read
        if block_given?
          text = yield(text)
        else
          needle = before.to_s
          unless text.include?(needle)
            raise "inreplace: string not found in #{file}: #{needle.inspect}"
          end
          text = text.gsub(needle, after.to_s)
        end
        path.write(text)
      end
    end
  end

  def ohai(*messages)
    messages.each { |message| $stdout.puts "==> #{message}" }
    nil
  end

  def opoo(*messages)
    messages.each { |message| $stderr.puts "Warning: #{message}" }
    nil
  end

  def puts(*messages)
    Kernel.puts(*messages)
    nil
  end

  def mkdir(path, *_args)
    FileUtils.mkdir_p(path.to_s)
  end

  def mkdir_p(path, *_args)
    FileUtils.mkdir_p(path.to_s)
  end

  def cp(source, destination)
    FileUtils.cp(source.to_s, destination.to_s)
  end

  def mv(source, destination)
    FileUtils.mv(source.to_s, destination.to_s)
  end

  def rm_rf(*paths)
    paths.flatten.each { |path| FileUtils.rm_rf(path.to_s) }
    nil
  end

  def chmod(mode, *paths)
    paths.flatten.each { |path| File.chmod(mode, path.to_s) }
    nil
  end

  def touch(path)
    FileUtils.touch(path.to_s)
  end

  def which(command)
    command = command.to_s
    ENV.fetch("PATH", "").split(File::PATH_SEPARATOR).each do |directory|
      candidate = File.join(directory, command)
      return Pathname.new(candidate) if File.file?(candidate) && File.executable?(candidate)
    end
    nil
  end

  def resource(name)
    raise "resource(#{name.inspect}) staging is not supported by the opt-in Ruby bridge"
  end

  def std_configure_args
    [
      "--disable-debug",
      "--disable-dependency-tracking",
      "--prefix=#{prefix}",
      "--libdir=#{prefix}/lib",
    ]
  end

  def std_cmake_args
    [
      "-DCMAKE_INSTALL_PREFIX=#{prefix}",
      "-DCMAKE_INSTALL_LIBDIR=lib",
      "-DCMAKE_BUILD_TYPE=Release",
      "-DCMAKE_FIND_FRAMEWORK=LAST",
      "-DCMAKE_VERBOSE_MAKEFILE=ON",
      "-Wno-dev",
    ]
  end

  def std_meson_args
    ["--prefix=#{prefix}", "--libdir=lib", "--buildtype=release", "--wrap-mode=nofallback"]
  end

  def std_cargo_args
    ["--path", ".", "--root", prefix.to_s]
  end

  def std_go_args
    ["-o=#{prefix}/bin/#{name}"]
  end

  def std_pip_args
    ["--prefix", prefix.to_s]
  end

  def std_service_path_env
    File.join(ENV.fetch("HOMEBREW_PREFIX", prefix.to_s), "bin")
  end

  def on_macos
    yield if OS.mac?
  end

  def on_linux
    yield if OS.linux?
  end

  def on_arm
    yield if Hardware::CPU.arm?
  end

  def on_intel
    yield if Hardware::CPU.intel?
  end

  alias on_arm64 on_arm
  alias on_x86_64 on_intel
end

context = PybrewInstallContext.new(PYBREW_CONFIG)
context.instance_eval(
  PYBREW_BODY,
  "#{PYBREW_CONFIG['formula']['name']}.rb",
  PYBREW_CONFIG["body_line"].to_i
)
'''


def _render_script(body: str, body_line: int, config: dict) -> str:
    encoded_config = base64.b64encode(
        json.dumps(config, sort_keys=True).encode("utf-8")
    ).decode("ascii")
    encoded_body = base64.b64encode(body.encode("utf-8")).decode("ascii")
    return _RUBY_SHIM.replace("__PYBREW_CONFIG__", encoded_config).replace(
        "__PYBREW_BODY__", encoded_body
    )


def _failure_message(
    formula: Formula,
    source: str,
    result: subprocess.CompletedProcess,
    script_path: Optional[Path] = None,
) -> str:
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    details: list[str] = []
    filename = f"{formula.name}.rb"
    match = re.search(re.escape(filename) + r":(\d+)", stderr)
    if match is not None:
        line = int(match.group(1))
        details.append(f"at def install line {line}")
        source_lines = source.splitlines()
        if 1 <= line <= len(source_lines):
            details.append(f"  {source_lines[line - 1].strip()}")
    output = "\n".join(part for part in (stderr, stdout) if part)
    if not output:
        output = "(Ruby produced no output)"
    if script_path is not None:
        # Generated shim frames are noise; keep the formula line numbers.
        output = output.replace(str(script_path), "ruby-shim.rb")
    tail = "\n".join(output.splitlines()[-40:])
    heading = f"{formula.name}: arbitrary Ruby install failed (exit {result.returncode})"
    if details:
        heading = heading + " " + details[0]
    return heading + "\n" + "\n".join(details[1:] + [tail])


def execute_install_body(
    formula: Formula,
    config: Config,
    build_root: Path,
    keg: Path,
    *,
    env: Optional[dict[str, str]] = None,
    verbose: bool = False,
) -> None:
    """Execute ``formula``'s raw ``def install`` body with Ruby.

    The Ruby process runs with ``build_root`` as its working directory and the
    supplied environment (normally the source-builder's Homebrew-like build
    environment).  Any failure raises :class:`BuildError` containing the Ruby
    stderr output and, when Ruby reports it, the failing formula line.
    """

    build_root = Path(build_root)
    keg = Path(keg)
    if not build_root.is_dir():
        raise BuildError(f"{formula.name}: build directory does not exist: {build_root}")

    body, body_line = extract_install_body(formula)
    if not body.strip():
        raise UnsupportedInstall(f"{formula.name}: install block is empty")

    ruby = _ruby_binary(formula)
    version = formula.version or "HEAD"
    payload = {
        "formula": {"name": formula.name, "version": version},
        "paths": _install_paths(formula, config, keg, build_root),
        "verbose": bool(verbose),
        "arch": os.environ.get("PYBREW_TARGET_ARCH") or platform.machine(),
        "body_line": body_line,
    }
    script = _render_script(body, body_line, payload)

    if verbose:
        print(f"==> {formula.name}: executing arbitrary Ruby install body with {ruby}")

    with tempfile.TemporaryDirectory(prefix="pybrew-ruby-") as temporary:
        script_path = Path(temporary) / "install.rb"
        script_path.write_text(script, encoding="utf-8")
        try:
            result = subprocess.run(
                [ruby, str(script_path)],
                cwd=str(build_root),
                env=env if env is not None else os.environ.copy(),
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=RUBY_TIMEOUT,
            )
        except FileNotFoundError as error:  # pragma: no cover - _ruby_binary checks
            raise BuildError(f"{formula.name}: Ruby interpreter not found: {ruby}") from error
        except subprocess.TimeoutExpired as error:
            raise BuildError(
                f"{formula.name}: arbitrary Ruby install timed out after {RUBY_TIMEOUT}s"
            ) from error

    if result.returncode != 0:
        raise BuildError(
            _failure_message(
                formula, formula.install_source or "", result, script_path=script_path
            )
        )
    if verbose:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
