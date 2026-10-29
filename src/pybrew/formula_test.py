"""Run a formula's captured ``test do ... end`` block with the host Ruby.

Homebrew formula tests are arbitrary Ruby, but the common subset used by the
vast majority of formulae is small: a few shell helpers plus assertions.  This
module executes the captured test body with the system Ruby (``/usr/bin/ruby``
by default, overridable with ``PYBREW_RUBY``) inside a throw-away ``testpath``
under the configured prefix and with a deliberately small compatibility shim.

Nothing here runs unless the ``test`` CLI command (or the public
:func:`run_formula_test` helper) is called.  Failures are reported as
:class:`FormulaTestError` (a :class:`pybrew.errors.BuildError`) carrying the
Ruby stdout/stderr so the CLI can show a useful message.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from .cellar import Cellar
from .errors import BuildError
from .models import Formula
from .paths import Config

#: Environment variable overriding the Ruby interpreter used for tests.
RUBY_ENV = "PYBREW_RUBY"

#: System Ruby on macOS (also present on most Linux distributions).
DEFAULT_RUBY = "/usr/bin/ruby"

#: Upper bound for one formula test.  Tests that need longer can pass
#: ``timeout=`` to :func:`run_formula_test`.
DEFAULT_TIMEOUT = 300.0


class FormulaTestError(BuildError):
    """A formula's test block failed, could not run, or was missing."""

    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: Optional[int] = None,
        command: Optional[list[str]] = None,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.command = list(command or [])


@dataclass(frozen=True)
class FormulaTestResult:
    """The successful result of running a formula's test block."""

    formula: str
    success: bool
    stdout: str
    stderr: str
    returncode: int
    testpath: str
    ruby: str
    prefix: str = ""
    command: tuple[str, ...] = ()
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        """Alias for :attr:`success` (convenient in scripts)."""

        return self.success

    @property
    def output(self) -> str:
        """Alias for :attr:`stdout`."""

        return self.stdout


# The Ruby shim.  It is intentionally Ruby 2.6 compatible because that is the
# Ruby shipped by macOS (and therefore the most likely system interpreter).
_RUBY_RUNNER = r'''
# frozen_string_literal: true

require "fileutils"
require "json"
require "open3"
require "pathname"

class FormulaTestFailure < StandardError
end

class FormulaTestContext
  include FileUtils

  attr_reader :testpath

  class << self
    # The context currently executing a formula test.  Only one test runs per
    # Ruby process, so a simple class-level reference is enough for
    # ``Formula["name"]`` to find the formula under test.
    attr_accessor :current
  end

  # Minimal stand-in for Homebrew's ``Formula`` class.  The test runner only
  # knows about the formula under test (its dependency graph is not loaded), so
  # ``Formula["name"]`` is limited to that formula and fails clearly otherwise.
  class Formula
    attr_reader :name

    def initialize(context, name)
      @context = context
      @name = name.to_s
    end

    def prefix
      @context.prefix
    end

    def opt_prefix
      @context.opt_prefix
    end

    def bin
      @context.bin
    end

    def testpath
      @context.testpath
    end

    def version
      @context.version
    end

    def to_s
      @name
    end

    def inspect
      "#<Formula #{@name}>"
    end

    def self.[](name)
      context = FormulaTestContext.current
      if context.nil?
        raise FormulaTestFailure, "Formula[...] is only available inside a formula test"
      end

      context.formula_reference(name)
    end
  end

  PATH_NAMES = %w[
    prefix bin sbin lib include share etc libexec opt_prefix opt_bin pkgshare
    var man
  ].freeze

  def initialize(config)
    @config = config
    @testpath = Pathname.new(config["testpath"].to_s)
    @paths = {}
    (config["paths"] || {}).each do |name, value|
      @paths[name.to_s] = Pathname.new(value.to_s)
    end
    FormulaTestContext.current = self
  end

  # Return the ``Formula["name"]`` reference for the formula under test.
  def formula_reference(name)
    name = name.to_s
    unless name == self.name
      raise FormulaTestFailure,
            "Formula[#{name.inspect}] is not available: this test runner only exposes " \
            "the formula under test (#{self.name.inspect})"
    end

    Formula.new(self, name)
  end

  PATH_NAMES.each do |name|
    define_method(name) do
      @paths[name] || Pathname.new(File.join(@paths["prefix"].to_s, name))
    end
  end

  def name
    @config["name"].to_s
  end

  def version
    @config["version"].to_s
  end

  def formula
    self
  end

  def test(&block)
    raise FormulaTestFailure, "test block was not provided" if block.nil?

    block.call
  end

  # Run a shell command and return its standard output.  The second positional
  # argument is Homebrew's expected exit status (default 0); a different status
  # raises with the captured stderr.
  #
  # ``stderr: true`` returns the captured standard error instead.
  # ``env:`` adds/overrides environment variables for the child only and
  # ``chdir:`` runs it in another directory.
  def shell_output(command, expected_status = 0, stderr: false, env: nil, chdir: nil)
    stdout, stderr_text, status = run_shell(command, nil, env: env, chdir: chdir)
    if status.exitstatus != expected_status
      raise FormulaTestFailure,
            "shell_output failed with exit status #{status.exitstatus} " \
            "(expected #{expected_status}): #{command}\n#{stderr_text}"
    end

    stderr ? stderr_text : stdout
  end

  # Run a shell command with the optional input and return its standard
  # output, ignoring the exit status (``pipe_output`` historically behaves
  # this way).  ``stderr:``, ``env:`` and ``chdir:`` work as in
  # ``shell_output``.
  def pipe_output(command, input = nil, stderr: false, env: nil, chdir: nil)
    stdout, stderr_text, _status = run_shell(command, input, env: env, chdir: chdir)
    stderr ? stderr_text : stdout
  end

  def assert(value, message = nil)
    return if value

    raise_failure("Expected #{value.inspect} to be truthy", message)
  end

  def refute(value, message = nil)
    return unless value

    raise_failure("Expected #{value.inspect} to be falsey", message)
  end

  def assert_equal(expected, actual, message = nil)
    return if expected == actual

    raise_failure("Expected #{expected.inspect} to equal #{actual.inspect}", message)
  end

  def refute_equal(expected, actual, message = nil)
    return unless expected == actual

    raise_failure("Expected #{expected.inspect} to not equal #{actual.inspect}", message)
  end

  def assert_match(pattern, string, message = nil)
    regexp = pattern.is_a?(Regexp) ? pattern : Regexp.new(pattern.to_s)
    return if regexp.match?(string.to_s)

    raise_failure("Expected #{string.inspect} to match #{pattern.inspect}", message)
  end

  def refute_match(pattern, string, message = nil)
    regexp = pattern.is_a?(Regexp) ? pattern : Regexp.new(pattern.to_s)
    return unless regexp.match?(string.to_s)

    raise_failure("Expected #{string.inspect} to not match #{pattern.inspect}", message)
  end

  def assert_predicate(object, predicate, message = nil)
    return if object.__send__(predicate)

    raise_failure("Expected #{object.inspect} to satisfy #{predicate}", message)
  end

  def refute_predicate(object, predicate, message = nil)
    return unless object.__send__(predicate)

    raise_failure("Expected #{object.inspect} to not satisfy #{predicate}", message)
  end

  def assert_empty(object, message = nil)
    return if object.empty?

    raise_failure("Expected #{object.inspect} to be empty", message)
  end

  def refute_empty(object, message = nil)
    return unless object.empty?

    raise_failure("Expected #{object.inspect} to not be empty", message)
  end

  def assert_includes(collection, object, message = nil)
    return if collection.include?(object)

    raise_failure("Expected #{collection.inspect} to include #{object.inspect}", message)
  end

  def refute_includes(collection, object, message = nil)
    return unless collection.include?(object)

    raise_failure("Expected #{collection.inspect} to not include #{object.inspect}", message)
  end

  def assert_output(expected, actual = nil, message = nil)
    if block_given?
      require "stringio"
      old_stdout = $stdout
      buffer = StringIO.new
      $stdout = buffer
      begin
        yield
      ensure
        $stdout = old_stdout
      end
      actual = buffer.string
    end
    return if expected == actual

    raise_failure("Expected output #{expected.inspect} to equal #{actual.inspect}", message)
  end

  def refute_output(expected, actual = nil, message = nil)
    if block_given?
      require "stringio"
      old_stdout = $stdout
      buffer = StringIO.new
      $stdout = buffer
      begin
        yield
      ensure
        $stdout = old_stdout
      end
      actual = buffer.string
    end
    return unless expected == actual

    raise_failure("Expected output #{expected.inspect} to not equal #{actual.inspect}", message)
  end

  def assert_not_equal(expected, actual, message = nil)
    refute_equal(expected, actual, message)
  end

  def assert_operator(left, operator, right, message = nil)
    return if left.__send__(operator, right)

    raise_failure("Expected #{left.inspect} #{operator} #{right.inspect}", message)
  end

  def assert_kind_of(klass, object, message = nil)
    return if object.is_a?(klass)

    raise_failure("Expected #{object.inspect} to be a kind of #{klass}", message)
  end

  def assert_instance_of(klass, object, message = nil)
    return if object.instance_of?(klass)

    raise_failure("Expected #{object.inspect} to be an instance of #{klass}", message)
  end

  def assert_respond_to(object, method_name, message = nil)
    return if object.respond_to?(method_name)

    raise_failure("Expected #{object.inspect} to respond to #{method_name}", message)
  end

  def assert_raises(*exceptions)
    raise ArgumentError, "assert_raises needs at least one exception class" if exceptions.empty?
    raise ArgumentError, "assert_raises requires a block" unless block_given?

    begin
      yield
    rescue *exceptions => error
      return error
    end
    raise_failure("Expected #{exceptions.inspect} to be raised", nil)
  end

  def assert_silent(message = nil)
    require "stringio"
    old_stdout = $stdout
    old_stderr = $stderr
    out = StringIO.new
    err = StringIO.new
    $stdout = out
    $stderr = err
    begin
      yield
    ensure
      $stdout = old_stdout
      $stderr = old_stderr
    end
    if !out.string.empty? || !err.string.empty?
      raise_failure("Expected silence, got stdout=#{out.string.inspect} stderr=#{err.string.inspect}", message)
    end
  end

  def assert_path_not_exists(path, message = nil)
    refute_path_exists(path, message)
  end

  def assert_path_exists(path, message = nil)
    return if File.exist?(path.to_s)

    raise_failure("Expected path #{path.to_s.inspect} to exist", message)
  end

  def refute_path_exists(path, message = nil)
    return unless File.exist?(path.to_s)

    raise_failure("Expected path #{path.to_s.inspect} to not exist", message)
  end

  def assert_directory(path, message = nil)
    return if File.directory?(path.to_s)

    raise_failure("Expected path #{path.to_s.inspect} to be a directory", message)
  end

  def refute_directory(path, message = nil)
    return unless File.directory?(path.to_s)

    raise_failure("Expected path #{path.to_s.inspect} to not be a directory", message)
  end

  def assert_file(path, message = nil)
    return if File.file?(path.to_s)

    raise_failure("Expected path #{path.to_s.inspect} to be a file", message)
  end

  def assert_symlink(path, message = nil)
    return if File.symlink?(path.to_s)

    raise_failure("Expected path #{path.to_s.inspect} to be a symlink", message)
  end

  def assert_nil(object, message = nil)
    return if object.nil?

    raise_failure("Expected #{object.inspect} to be nil", message)
  end

  def refute_nil(object, message = nil)
    return unless object.nil?

    raise_failure("Expected #{object.inspect} to not be nil", message)
  end

  private

  def run_shell(command, input, env: nil, chdir: nil)
    args = if command.is_a?(Array)
             command.map(&:to_s)
           else
             ["/bin/sh", "-c", command.to_s]
           end
    environment = {}
    (env || {}).each do |key, value|
      environment[key.to_s] = value.nil? ? nil : value.to_s
    end
    options = {}
    options[:stdin_data] = input.to_s unless input.nil?
    options[:chdir] = chdir.to_s unless chdir.nil?
    Open3.capture3(environment, *args, **options)
  end

  def raise_failure(detail, message)
    text = message.nil? || message.to_s.empty? ? detail : "#{message}\n#{detail}"
    raise FormulaTestFailure, text
  end
end

def run_formula_test_body
  config = JSON.parse(File.read(ARGV[0]))
  source = File.read(ARGV[1])
  context = FormulaTestContext.new(config)
  stripped = source.sub(/\A\s+/, "")

  if stripped.start_with?("def test", "def self.test")
    # A formula may define ``def test`` instead of using the usual block.
    context.instance_eval(source, "(formula test)", 1)
    context.test
  elsif stripped.start_with?("do")
    # The parser captures the block as ``do ... end`` without the leading
    # ``test`` call.
    context.instance_eval("test " + stripped, "(formula test)", 1)
  else
    context.instance_eval(stripped, "(formula test)", 1)
  end
end

begin
  run_formula_test_body
rescue FormulaTestFailure => error
  $stderr.puts "Formula test failed: #{error.message}"
  exit 1
rescue SystemExit
  raise
rescue Exception => error
  $stderr.puts "Formula test raised #{error.class}: #{error.message}"
  error.backtrace.to_a.first(15).each { |line| $stderr.puts "  #{line}" }
  exit 1
end
'''


def _ruby_binary(ruby: Optional[str]) -> str:
    candidate = os.path.expanduser(
        os.fspath(ruby or os.environ.get(RUBY_ENV) or DEFAULT_RUBY)
    )
    resolved = shutil.which(candidate)
    if resolved is None and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        resolved = candidate
    if resolved is None:
        raise FormulaTestError(
            f"Ruby interpreter not found: {candidate!r} "
            f"(set {RUBY_ENV} to an executable Ruby)"
        )
    return resolved


def _installed_keg(formula: Formula, config: Config) -> Path:
    keg = Cellar(config).keg(formula.name)
    if keg is None:
        raise FormulaTestError(f"formula {formula.name!r} is not installed")
    return keg


def _test_paths(formula: Formula, config: Config, keg: Path) -> dict[str, str]:
    opt = config.opt_path(formula.name)
    if not opt.exists():
        opt = keg
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
        "opt_prefix": opt,
        "opt_bin": opt / "bin",
        "pkgshare": keg / "share" / formula.name,
        "var": config.var,
        "man": man_root,
    }
    return {name: str(path) for name, path in mapping.items()}


def _test_environment(
    config: Config,
    keg: Path,
    testpath: Path,
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    env = dict(os.environ)
    path_parts = [
        str(keg / "bin"),
        str(keg / "sbin"),
        str(config.bin),
        str(config.sbin),
        env.get("PATH", ""),
    ]
    env["PATH"] = os.pathsep.join(part for part in path_parts if part)
    env["PWD"] = str(testpath)
    env["PYBREW_TESTPATH"] = str(testpath)
    env["PYBREW_PREFIX"] = str(keg)
    env["PYBREW_FORMULA_PREFIX"] = str(keg)
    env["HOMEBREW_PREFIX"] = str(config.prefix)
    env["HOMEBREW_CELLAR"] = str(config.cellar)
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _make_test_root(config: Config) -> Path:
    """Create the throw-away directory that holds the runner and testpath.

    The directory deliberately lives under the configured prefix (in
    ``var/pybrew/tmp``) so formula tests run on the same filesystem as the
    installation and ``testpath`` is under ``prefix``.  It is removed after
    every run.
    """

    parent = config.var / "pybrew" / "tmp"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="formula-test-", dir=str(parent)))
    except OSError as error:
        raise FormulaTestError(
            f"could not create a formula test directory under {parent}: {error}"
        ) from error


def run_formula_test(
    formula: Formula,
    config: Config,
    *,
    ruby: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
) -> FormulaTestResult:
    """Run ``formula.test_source`` in a throw-away testpath under ``prefix``.

    On success a :class:`FormulaTestResult` is returned.  Missing tests,
    missing installations, missing Ruby interpreters, timeouts and failing
    assertions all raise :class:`FormulaTestError` with the captured Ruby
    stderr attached.
    """

    source = formula.test_source or ""
    keg = _installed_keg(formula, config)
    if not source.strip():
        raise FormulaTestError(f"formula {formula.name!r} has no test block")

    ruby_path = _ruby_binary(ruby)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT

    root = _make_test_root(config)
    # Resolve the testpath so ``Dir.pwd`` inside the Ruby test matches
    # ``testpath.to_s`` even when the prefix contains a symlink (for example
    # /var -> /private/var on macOS).
    testpath = (root / "testpath").resolve()
    testpath.mkdir()
    runner = root / "runner.rb"
    runner.write_text(_RUBY_RUNNER, encoding="utf-8")
    body = root / "test_body.rb"
    body.write_text(source, encoding="utf-8")
    config_path = root / "config.json"
    payload = {
        "name": formula.name,
        "version": formula.pkg_version,
        "testpath": str(testpath),
        "prefix": str(keg),
        "paths": _test_paths(formula, config, keg),
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    command = [ruby_path, str(runner), str(config_path), str(body)]
    run_env = _test_environment(config, keg, testpath, extra=env)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(testpath),
            env=run_env,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise FormulaTestError(
            f"formula test for {formula.name!r} timed out after {timeout:g}s",
            stdout=_as_text(error.stdout),
            stderr=_as_text(error.stderr),
            command=command,
        ) from error
    except OSError as error:
        raise FormulaTestError(
            f"could not run Ruby for formula test {formula.name!r}: {error}",
            command=command,
        ) from error
    finally:
        shutil.rmtree(root, ignore_errors=True)

    duration = time.monotonic() - started
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if completed.returncode != 0:
        details = stderr.strip() or stdout.strip()
        message = (
            f"formula test failed for {formula.name!r} "
            f"(exit status {completed.returncode})"
        )
        if details:
            message += f":\n{details}"
        raise FormulaTestError(
            message,
            stdout=stdout,
            stderr=stderr,
            returncode=completed.returncode,
            command=command,
        )

    return FormulaTestResult(
        formula=formula.name,
        success=True,
        stdout=stdout,
        stderr=stderr,
        returncode=completed.returncode,
        testpath=str(testpath),
        ruby=ruby_path,
        prefix=str(keg),
        command=tuple(command),
        duration=duration,
    )


def run_test(
    formula: Formula,
    config: Config,
    *,
    ruby: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
) -> FormulaTestResult:
    """Alias for :func:`run_formula_test`."""

    return run_formula_test(formula, config, ruby=ruby, timeout=timeout, env=env)


#: Alias kept for symmetry with :func:`pybrew.ruby_executor.execute_install_body`.
execute_formula_test = run_formula_test


def has_test_block(formula: Formula) -> bool:
    """Return true when the formula captured a non-empty test body."""

    return bool((formula.test_source or "").strip())


class FormulaTestRunner:
    """Small stateful wrapper around :func:`run_formula_test`."""

    def __init__(
        self,
        config: Config,
        *,
        ruby: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.config = config
        self.ruby = ruby
        self.timeout = timeout

    def run(
        self,
        formula: Formula,
        *,
        env: Optional[Mapping[str, str]] = None,
    ) -> FormulaTestResult:
        return run_formula_test(
            formula,
            self.config,
            ruby=self.ruby,
            timeout=self.timeout,
            env=env,
        )
