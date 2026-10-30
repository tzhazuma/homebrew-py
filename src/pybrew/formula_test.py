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

  # Stand-in for Homebrew's ``Formula`` class.  The test runner knows the
  # formula under test plus its declared dependencies (their path metadata is
  # passed in from Python), so ``Formula["name"]`` resolves those and fails
  # clearly for anything else.
  class Formula
    PATH_METHODS = %w[
      bin sbin lib include share etc libexec opt_bin opt_sbin opt_lib
      opt_include opt_share opt_libexec opt_pkgshare pkgshare var man
      man1 man2 man3 man4 man5 man6 man7 man8 doc info pkgetc
      bash_completion zsh_completion fish_completion elisp
    ].freeze

    attr_reader :name

    def initialize(context, name, info = nil)
      @context = context
      @name = name.to_s
      @info = info || {}
    end

    def self.[](name)
      context = FormulaTestContext.current
      if context.nil?
        raise FormulaTestFailure, "Formula[...] is only available inside a formula test"
      end

      context.formula_reference(name)
    end

    def self.factory(name)
      self[name]
    end

    def prefix
      raw = @info["prefix"]
      return Pathname.new(raw.to_s) unless raw.nil? || raw.to_s.empty?

      raise FormulaTestFailure, "Formula[#{@name.inspect}] is not installed: no keg found"
    end

    def opt_prefix
      raw = @info["opt_prefix"]
      return Pathname.new(raw.to_s) unless raw.nil? || raw.to_s.empty?

      Pathname.new(File.join(@context.homebrew_prefix.to_s, "opt", @name))
    end

    def testpath
      return @context.testpath if @name == @context.name

      nil
    end

    def version
      raw = @info["version"]
      raw.nil? || raw.to_s.empty? ? nil : raw.to_s
    end

    def pkg_version
      version
    end

    def revision
      0
    end

    def full_name
      raw = @info["full_name"]
      raw.nil? || raw.to_s.empty? ? @name : raw.to_s
    end

    def installed?
      @info["installed"] == true
    end
    alias any_version_installed? installed?
    alias latest_version_installed? installed?

    def test_defined?
      @name == @context.name && @info["test_defined"] != false
    end

    # Homebrew exposes the formula's ``test`` method (defined by ``test do``).
    # This runner only loads the test body of the formula under test, so the
    # entry point exists but cannot run another formula's test block.
    def test
      if @name == @context.name
        raise FormulaTestFailure,
              "Formula[#{@name.inspect}].test cannot be called from inside its own test block"
      end

      raise FormulaTestFailure,
            "Formula[#{@name.inspect}].test is not available: this test runner does " \
            "not load dependency test blocks"
    end

    def deps
      Array(@info["dependencies"]).map do |dependency_name|
        @context.formula_reference(dependency_name)
      end
    end

    def runtime_dependencies(*_args)
      deps
    end

    PATH_METHODS.each do |method_name|
      define_method(method_name) do
        formula_path(method_name)
      end
    end

    def to_s
      @name
    end

    def inspect
      "#<Formula #{@name}>"
    end

    def ==(other)
      other.respond_to?(:name) && other.name.to_s == @name
    end
    alias eql? ==

    def hash
      @name.hash
    end

    private

    def formula_path(method_name)
      case method_name
      when "bin" then prefix/"bin"
      when "sbin" then prefix/"sbin"
      when "lib" then prefix/"lib"
      when "include" then prefix/"include"
      when "share" then prefix/"share"
      when "etc" then prefix/"etc"
      when "libexec" then prefix/"libexec"
      when "opt_bin" then opt_prefix/"bin"
      when "opt_sbin" then opt_prefix/"sbin"
      when "opt_lib" then opt_prefix/"lib"
      when "opt_include" then opt_prefix/"include"
      when "opt_share" then opt_prefix/"share"
      when "opt_libexec" then opt_prefix/"libexec"
      when "opt_pkgshare" then opt_prefix/"share"/name
      when "pkgshare" then prefix/"share"/name
      when "var" then Pathname.new(File.join(@context.homebrew_prefix.to_s, "var"))
      when "man" then prefix/"share"/"man"
      when "man1" then man/"man1"
      when "man2" then man/"man2"
      when "man3" then man/"man3"
      when "man4" then man/"man4"
      when "man5" then man/"man5"
      when "man6" then man/"man6"
      when "man7" then man/"man7"
      when "man8" then man/"man8"
      when "doc" then prefix/"share"/"doc"/name
      when "info" then prefix/"share"/"info"
      when "pkgetc" then Pathname.new(File.join(@context.homebrew_prefix.to_s, "etc", name))
      when "bash_completion" then prefix/"etc"/"bash_completion.d"
      when "zsh_completion" then share/"zsh"/"site-functions"
      when "fish_completion" then share/"fish"/"vendor_completions.d"
      when "elisp" then prefix/"share"/"emacs"/"site-lisp"/name
      else
        prefix/method_name
      end
    end
  end

  PATH_NAMES = %w[
    prefix bin sbin lib include share etc libexec opt_prefix opt_bin opt_sbin
    opt_lib opt_include opt_share opt_libexec opt_pkgshare pkgshare var man
    man1 man2 man3 man4 man5 man6 man7 man8 doc info pkgetc bash_completion
    zsh_completion fish_completion elisp
  ].freeze

  attr_reader :homebrew_prefix, :cellar

  def initialize(config)
    @config = config
    @testpath = Pathname.new(config["testpath"].to_s)
    @homebrew_prefix = Pathname.new((config["homebrew_prefix"] || config["prefix"]).to_s)
    @cellar = Pathname.new(
      (config["cellar"] || File.join(@homebrew_prefix.to_s, "Cellar")).to_s
    )
    @paths = {}
    (config["paths"] || {}).each do |name, value|
      @paths[name.to_s] = Pathname.new(value.to_s)
    end
    @formulae = config["formulae"] || {}
    prefix_path = @paths["prefix"] || Pathname.new(config["prefix"].to_s)
    @formulae[name] ||= {
      "name" => name,
      "full_name" => name,
      "version" => version,
      "prefix" => prefix_path.to_s,
      "opt_prefix" => (@paths["opt_prefix"] || prefix_path).to_s,
      "installed" => true,
      "dependencies" => [],
      "test_defined" => true,
    }
    FormulaTestContext.current = self
  end

  # Return the ``Formula["name"]`` reference for the formula under test or one
  # of its declared dependencies.
  def formula_reference(name)
    name = name.to_s
    info = @formulae[name]
    if info.nil?
      available = @formulae.keys.sort.join(", ")
      raise FormulaTestFailure,
            "Formula[#{name.inspect}] is not available: this test runner only exposes " \
            "the formula under test (#{self.name.inspect}) and its declared dependencies" \
            "#{available.empty? ? "" : " (#{available})"}"
    end

    Formula.new(self, name, info)
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
    command = resolve_command(command)
    stdout, stderr_text, status = run_shell(command, nil, env: env, chdir: chdir)
    if status.exitstatus != expected_status
      raise FormulaTestFailure,
            "shell_output failed with exit status #{status.exitstatus} " \
            "(expected #{expected_status}): #{command}\n#{stderr_text}"
    end

    stderr ? stderr_text : stdout
  end

  # Run a shell command with the optional input and return its standard
  # output.  When ``expected_status`` is given the exit status is asserted
  # (Homebrew's ``pipe_output(cmd, input, result)`` form); otherwise the exit
  # status is ignored.  ``stderr:``, ``env:`` and ``chdir:`` work as in
  # ``shell_output``.
  def pipe_output(command, input = nil, expected_status = nil, stderr: false, env: nil, chdir: nil)
    command = resolve_command(command)
    stdout, stderr_text, status = run_shell(command, input, env: env, chdir: chdir)
    if !expected_status.nil? && status.exitstatus != expected_status
      raise FormulaTestFailure,
            "pipe_output failed with exit status #{status.exitstatus} " \
            "(expected #{expected_status}): #{command}\n#{stderr_text}"
    end

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

  # Minitest-compatible block form: ``assert_output(stdout, stderr) { ... }``
  # compares both captured streams, treating ``nil`` as "don't care" and a
  # Regexp as a pattern.  The two-argument form without a block compares
  # ``expected`` with ``actual`` (pybrew's original helper).
  def assert_output(expected = nil, actual = nil, message = nil)
    if block_given?
      out, err = capture_io { yield }
      assert_stream("stdout", expected, out, message)
      assert_stream("stderr", actual, err, message)
      return
    end
    return if expected == actual

    raise_failure("Expected output #{expected.inspect} to equal #{actual.inspect}", message)
  end

  def refute_output(expected, actual = nil, message = nil)
    if block_given?
      out, _err = capture_io { yield }
      return unless output_matches?(expected, out)

      raise_failure("Expected output #{out.inspect} to not equal #{expected.inspect}", message)
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
    message = exceptions.pop if exceptions.last.is_a?(String)
    exceptions = [StandardError] if exceptions.empty?
    raise ArgumentError, "assert_raises requires a block" unless block_given?

    begin
      yield
    rescue FormulaTestFailure
      raise
    rescue SignalException, SystemExit
      raise
    rescue *exceptions => error
      return error
    rescue Exception => error
      raise_failure(
        "Expected #{exceptions.inspect} to be raised, but #{error.class} was raised: " \
        "#{error.message}",
        message
      )
    end
    raise_failure("Expected #{exceptions.inspect} to be raised, but nothing was raised", message)
  end

  def assert_silent(message = nil)
    out, err = capture_io { yield }
    return if out.empty? && err.empty?

    raise_failure("Expected silence, got stdout=#{out.inspect} stderr=#{err.inspect}", message)
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

  def refute_file(path, message = nil)
    return unless File.file?(path.to_s)

    raise_failure("Expected path #{path.to_s.inspect} to not be a file", message)
  end

  def refute_symlink(path, message = nil)
    return unless File.symlink?(path.to_s)

    raise_failure("Expected path #{path.to_s.inspect} to not be a symlink", message)
  end

  def assert_executable(path, message = nil)
    return if File.executable?(path.to_s)

    raise_failure("Expected path #{path.to_s.inspect} to be executable", message)
  end

  def refute_executable(path, message = nil)
    return unless File.executable?(path.to_s)

    raise_failure("Expected path #{path.to_s.inspect} to not be executable", message)
  end

  def assert_nil(object, message = nil)
    return if object.nil?

    raise_failure("Expected #{object.inspect} to be nil", message)
  end

  def refute_nil(object, message = nil)
    return unless object.nil?

    raise_failure("Expected #{object.inspect} to not be nil", message)
  end

  # Capture ``$stdout``/``$stderr`` written by Ruby code in the block.
  def capture_io
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
    [out.string, err.string]
  end

  # Capture output written by subprocesses as well (Minitest's
  # ``capture_subprocess_io``): redirect the real file descriptors.
  def capture_subprocess_io
    require "tempfile"
    out_file = Tempfile.new("pybrew-test-out")
    err_file = Tempfile.new("pybrew-test-err")
    old_stdout = $stdout.dup
    old_stderr = $stderr.dup
    begin
      $stdout.reopen(out_file)
      $stderr.reopen(err_file)
      yield
      $stdout.flush
      $stderr.flush
      $stdout.rewind
      $stderr.rewind
      [out_file.read, err_file.read]
    ensure
      $stdout.reopen(old_stdout)
      $stderr.reopen(old_stderr)
      out_file.close!
      err_file.close!
    end
  end

  def assert_in_delta(expected, actual, delta = 0.001, message = nil)
    difference = (expected - actual).abs
    return if difference <= delta

    raise_failure("Expected |#{expected} - #{actual}| (#{difference}) to be <= #{delta}", message)
  end

  def refute_in_delta(expected, actual, delta = 0.001, message = nil)
    difference = (expected - actual).abs
    return unless difference <= delta

    raise_failure("Expected |#{expected} - #{actual}| (#{difference}) to be > #{delta}", message)
  end

  def assert_in_epsilon(expected, actual, epsilon = 0.001, message = nil)
    assert_in_delta(expected, actual, [expected.abs, actual.abs].min * epsilon, message)
  end

  def refute_in_epsilon(expected, actual, epsilon = 0.001, message = nil)
    refute_in_delta(expected, actual, expected * epsilon, message)
  end

  def assert_same(expected, actual, message = nil)
    return if expected.equal?(actual)

    raise_failure(
      "Expected #{actual.inspect} to be the same object as #{expected.inspect}",
      message
    )
  end

  def refute_same(expected, actual, message = nil)
    return unless expected.equal?(actual)

    raise_failure(
      "Expected #{actual.inspect} to not be the same object as #{expected.inspect}",
      message
    )
  end

  def refute_instance_of(klass, object, message = nil)
    return unless object.instance_of?(klass)

    raise_failure("Expected #{object.inspect} to not be an instance of #{klass}", message)
  end

  def refute_kind_of(klass, object, message = nil)
    return unless object.is_a?(klass)

    raise_failure("Expected #{object.inspect} to not be a kind of #{klass}", message)
  end

  def refute_respond_to(object, method_name, message = nil)
    return unless object.respond_to?(method_name)

    raise_failure("Expected #{object.inspect} to not respond to #{method_name}", message)
  end

  def assert_throws(symbol, message = nil)
    sentinel = Object.new
    result = catch(symbol) do
      yield
      sentinel
    end
    return unless result.equal?(sentinel)

    raise_failure("Expected #{symbol.inspect} to have been thrown", message)
  end

  def flunk(message = nil)
    text = message.nil? || message.to_s.empty? ? "Epic Fail!" : message.to_s
    raise FormulaTestFailure, text
  end

  def pass(_message = nil)
    true
  end

  private

  def resolve_command(command)
    return command unless command.is_a?(Pathname)

    path = command.expand_path
    unless File.exist?(path)
      raise FormulaTestFailure, "Pathname #{command.to_s.inspect} does not exist!"
    end

    ["/bin/sh", "-c", 'exec "$1"', "pybrew-test", path.to_s]
  end

  def assert_stream(stream, expected, actual, message)
    return if expected.nil?
    return if output_matches?(expected, actual)

    if expected.is_a?(Regexp)
      raise_failure("Expected #{stream} #{actual.inspect} to match #{expected.inspect}", message)
    end

    raise_failure("Expected #{stream} #{actual.inspect} to equal #{expected.inspect}", message)
  end

  def output_matches?(expected, actual)
    expected.is_a?(Regexp) ? expected.match?(actual.to_s) : expected == actual
  end

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
    share = keg / "share"
    man_root = share / "man"
    mapping = {
        "prefix": keg,
        "bin": keg / "bin",
        "sbin": keg / "sbin",
        "lib": keg / "lib",
        "include": keg / "include",
        "share": share,
        "etc": keg / "etc",
        "libexec": keg / "libexec",
        "opt_prefix": opt,
        "opt_bin": opt / "bin",
        "opt_sbin": opt / "sbin",
        "opt_lib": opt / "lib",
        "opt_include": opt / "include",
        "opt_share": opt / "share",
        "opt_libexec": opt / "libexec",
        "opt_pkgshare": opt / "share" / formula.name,
        "pkgshare": share / formula.name,
        "var": config.var,
        "man": man_root,
        "man1": man_root / "man1",
        "man2": man_root / "man2",
        "man3": man_root / "man3",
        "man4": man_root / "man4",
        "man5": man_root / "man5",
        "man6": man_root / "man6",
        "man7": man_root / "man7",
        "man8": man_root / "man8",
        "doc": share / "doc" / formula.name,
        "info": share / "info",
        "pkgetc": config.prefix / "etc" / formula.name,
        "bash_completion": keg / "etc" / "bash_completion.d",
        "zsh_completion": share / "zsh" / "site-functions",
        "fish_completion": share / "fish" / "vendor_completions.d",
        "elisp": share / "emacs" / "site-lisp" / formula.name,
    }
    return {name: str(path) for name, path in mapping.items()}


def _declared_dependency_names(formula: Formula) -> list[str]:
    """Return the dependency names declared by ``formula`` (deduplicated)."""

    names: list[str] = []
    seen: set[str] = set()
    attributes = (
        "dependencies",
        "build_dependencies",
        "test_dependencies",
        "recommended_dependencies",
        "optional_dependencies",
        "head_dependencies",
        "uses_from_macos",
    )
    for attribute in attributes:
        for dependency in getattr(formula, attribute, None) or []:
            name = str(getattr(dependency, "name", dependency))
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _formula_entries(
    formula: Formula, config: Config, keg: Path
) -> dict[str, dict[str, object]]:
    """Path metadata for ``Formula[...]`` lookups inside the test body.

    Homebrew's ``Formula[]`` can load any formula; pybrew only parses the
    formula under test, so the runner exposes that formula plus its declared
    dependencies (installed ones carry their keg path).
    """

    dependencies = _declared_dependency_names(formula)
    opt = config.opt_path(formula.name)
    if not opt.exists():
        opt = keg
    entries: dict[str, dict[str, object]] = {
        formula.name: {
            "name": formula.name,
            "full_name": formula.full_name,
            "version": formula.pkg_version,
            "prefix": str(keg),
            "opt_prefix": str(opt),
            "installed": True,
            "dependencies": dependencies,
            "test_defined": bool((formula.test_source or "").strip()),
        }
    }
    cellar = Cellar(config)
    for name in dependencies:
        if name in entries:
            continue
        dependency_keg = cellar.keg(name)
        entries[name] = {
            "name": name,
            "full_name": name,
            "version": dependency_keg.name if dependency_keg is not None else None,
            "prefix": str(dependency_keg) if dependency_keg is not None else None,
            "opt_prefix": str(config.opt_path(name)),
            "installed": dependency_keg is not None,
            "dependencies": [],
        }
    return entries


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
        "homebrew_prefix": str(config.prefix),
        "cellar": str(config.cellar),
        "paths": _test_paths(formula, config, keg),
        "formulae": _formula_entries(formula, config, keg),
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
