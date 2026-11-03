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
import re
import shutil
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from .cellar import Cellar
from .errors import BuildError
from .models import Formula
from .paths import Config
from .ruby_parser import parse_formula_file
from .taps import Tap, discover_taps

#: Environment variable overriding the Ruby interpreter used for tests.
RUBY_ENV = "PYBREW_RUBY"

#: System Ruby on macOS (also present on most Linux distributions).
DEFAULT_RUBY = "/usr/bin/ruby"

#: Upper bound for one formula test.  Tests that need longer can pass
#: ``timeout=`` to :func:`run_formula_test`.
DEFAULT_TIMEOUT = 300.0

#: Maximum number of formula metadata entries loaded for one test run.
MAX_FORMULA_ENTRIES = 10000

#: Maximum number of tap formula files indexed for ``Formula[...]`` lookups.
MAX_TAP_FORMULA_INDEX = 20000

#: Small local taps are preloaded eagerly so dynamic ``Formula[expression]``
#: lookups (e.g. ``Formula[dep_name]``) can resolve uninstalled formulae.
MAX_EAGER_FORMULA_ENTRIES = 10000

#: Maximum number of formula sources read when an alias is missing from the
#: cheap ``Aliases`` symlink metadata.  Lookups of real formula names never
#: trigger this scan, keeping the 10000-formula path lightweight.
MAX_SOURCE_ALIAS_SCAN = 2000

#: Bounded in-process caches (parsed formulae and tap indexes).
FORMULA_CACHE_LIMIT = 256
TAP_INDEX_CACHE_LIMIT = 8

_formula_cache: dict = {}
_tap_index_cache: dict = {}


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
require "rbconfig"
require "tmpdir"

class FormulaTestFailure < StandardError
end

# Minitest raises ``Minitest::Skip`` (an ``Exception``, not a
# ``StandardError``); mirror that so ``rescue StandardError`` in a test body
# does not accidentally swallow a skip.
class FormulaTestSkip < Exception
end

unless defined?(NoMatchingPatternError)
  # Ruby 2.6 (the macOS system Ruby) has no pattern-matching support.  Define
  # the same constant so ``assert_pattern``/``refute_pattern`` can be exercised
  # offline (a test body can raise it to simulate a pattern mismatch).
  class NoMatchingPatternError < StandardError
  end
end

class FormulaTestContext
  include FileUtils

  # Sentinel for the single-argument ``assert_operator``/``refute_operator``
  # predicate forms (Minitest's ``UNDEFINED``).
  UNDEFINED = Object.new.freeze

  # PYBREW_* keys that are process/platform configuration rather than parent
  # test state; dependency tests inherit these.
  PYBREW_INHERITED_ENV = %w[
    PYBREW_MACOS_VERSION
    PYBREW_SDK_DIRS
    PYBREW_SDK_PATH
    PYBREW_SDK_VERSION
    PYBREW_TEST_FIXTURES
    PYBREW_XCRUN
  ].freeze

  # Available on Ruby 2.7+ and defined by the runner on older Rubies.
  NO_MATCHING_PATTERN_ERROR = NoMatchingPatternError

  attr_reader :testpath

  class << self
    # The context currently executing a formula test.  Only one test runs per
    # Ruby process, so a simple class-level reference is enough for
    # ``Formula["name"]`` to find the formula under test.
    attr_accessor :current

    # Evaluate a captured test body (``do ... end`` block, ``def test`` method
    # or a bare expression) inside ``context``.  Shared by the top-level runner
    # and by ``Formula["dep"].test``.
    def evaluate_source(context, source, label = "(formula test)")
      stripped = source.sub(/\A\s+/, "")
      if stripped.start_with?("def test", "def self.test")
        # A formula may define ``def test`` instead of using the usual block.
        context.instance_eval(source, label, 1)
        context.test
      elsif stripped.start_with?("do")
        # The parser captures the block as ``do ... end`` without the leading
        # ``test`` call.
        context.instance_eval("test " + stripped, label, 1)
      else
        context.instance_eval(stripped, label, 1)
      end
    end
  end

  # Lightweight, String-compatible formula version (Homebrew's ``Version``).
  # Mirrors the Python ``pybrew.version.Version`` token comparison, including
  # the intentional default deviation that an extra numeric segment is
  # significant (``1.2.3 < 1.2.3.0``; Homebrew treats it as equal).  The
  # closer Homebrew null-token behavior is available via ``homebrew_compare``,
  # ``Version.homebrew = true``, or ``PYBREW_VERSION_HOMEBREW=1`` (in that
  # mode 1.0 == 1.0.0 and hashes are canonical).
  class Version < String
    include Comparable

    # Approximate Homebrew pre-release ranking (lower sorts earlier).
    PRE_RELEASE_RANKS = {
      "alpha" => 0, "beta" => 1, "pre" => 2, "preview" => 2,
      "rc" => 3, "p" => 4, "post" => 5,
    }.freeze

    TOKEN_PATTERN = /(\d+)|(preview|alpha|beta|post|pre|rc|p)(\d*)|([A-Za-z]+)/i

    class << self
      # Class-level opt-in for Homebrew null-token comparison.  The
      # PYBREW_VERSION_HOMEBREW environment variable overrides this.
      attr_accessor :homebrew

      def homebrew_enabled?
        raw = ENV["PYBREW_VERSION_HOMEBREW"].to_s.strip.downcase
        return true if %w[1 true yes on].include?(raw)
        return false if %w[0 false no off].include?(raw)

        homebrew == true
      end

      def with_homebrew(enabled = true)
        previous = homebrew
        self.homebrew = enabled
        yield
      ensure
        self.homebrew = previous
      end
    end

    def initialize(value)
      super(value.to_s)
    end

    # Numeric/alphabetic tokens, e.g. "1.2.3-beta" => [1, 2, 3, "beta"].
    def segments
      @segments ||= to_s.scan(/\d+|[A-Za-z]+/).map do |part|
        part =~ /\A\d+\z/ ? part.to_i : part.downcase
      end
    end

    def tokens
      segments
    end

    # Classified tokens: "numeric", a pre-release word, or "letter".
    def token_classes
      version_tokens.map do |token|
        token[:kind] == :pre ? token[:value] : token[:kind].to_s
      end
    end

    def major
      segment_at(0)
    end

    def minor
      segment_at(1)
    end

    def patch
      segment_at(2)
    end

    def major_minor
      self.class.new(segments.first(2).join("."))
    end

    def major_minor_patch
      self.class.new(segments.first(3).join("."))
    end

    def pre_release_tokens
      segments.select do |segment|
        segment.is_a?(String) && PRE_RELEASE_RANKS.key?(segment)
      end
    end

    def pre_release?
      !pre_release_tokens.empty?
    end

    def revision
      0
    end

    # Homebrew-style comparison where a missing token is a null token:
    # numeric zero compares equal to it, pre-release words sort before it,
    # and p/post/unknown letters sort after it.  The default <=> keeps the
    # historical pybrew extra-numeric behavior for compatibility.
    def homebrew_compare(other)
      other = self.class.new(other.to_s) unless other.is_a?(Version)
      if other.is_a?(PkgVersion) && !is_a?(PkgVersion)
        comparison = homebrew_compare(other.version)
        return comparison unless comparison.zero?

        return other.revision.zero? ? 0 : -1
      end
      left = version_tokens
      right = other.send(:version_tokens)
      length = [left.length, right.length].max
      length.times do |index|
        comparison = homebrew_token_compare(left[index], right[index])
        return comparison unless comparison.zero?
      end
      0
    end

    def hash
      return [homebrew_hash_key, 0].hash if self.class.homebrew_enabled?

      super
    end

    # String defines == before Comparable in the ancestor chain for a String
    # subclass, so override it explicitly to use the mode-aware <=>.
    def ==(other)
      comparison = self <=> other
      !comparison.nil? && comparison.zero?
    end
    alias eql? ==

    def <=>(other)
      return homebrew_compare(other) if self.class.homebrew_enabled?

      other = self.class.new(other.to_s) unless other.is_a?(Version)
      if other.is_a?(PkgVersion) && !is_a?(PkgVersion)
        # Compare the revision-bearing string form so a plain Version that
        # already carries ``_N`` matches the equivalent PkgVersion.
        return self <=> Version.new(other.to_s)
      end
      left = segments
      right = other.segments
      length = [left.length, right.length].max
      length.times do |index|
        left_segment = left[index]
        right_segment = right[index]
        if left_segment.nil? && right_segment.nil?
          next
        elsif left_segment.nil?
          return right_segment.is_a?(String) ? 1 : -1
        elsif right_segment.nil?
          return left_segment.is_a?(Integer) ? 1 : -1
        end
        comparison = compare_segment(left_segment, right_segment)
        return comparison unless comparison.zero?
      end
      0
    end

    private

    def homebrew_hash_key
      key = version_tokens.map do |token|
        [token[:kind], token[:value], token[:revision]]
      end
      key.pop while !key.empty? && key.last[0] == :numeric && key.last[1].zero?
      key
    end

    def version_tokens
      to_s.scan(TOKEN_PATTERN).map do |numeric, pre, revision, letter|
        if numeric
          { kind: :numeric, value: numeric.to_i, rank: nil, revision: 0 }
        elsif pre
          word = pre.downcase
          word = "pre" if word == "preview"
          {
            kind: :pre,
            value: word,
            rank: PRE_RELEASE_RANKS[word],
            revision: revision.to_i,
          }
        else
          { kind: :letter, value: letter.downcase, rank: nil, revision: 0 }
        end
      end
    end

    def homebrew_token_compare(left, right)
      return 0 if left.nil? && right.nil?

      if left.nil?
        if right[:kind] == :numeric
          return right[:value].zero? ? 0 : -1
        elsif right[:kind] == :pre
          return right[:rank] <= 3 ? 1 : -1
        end
        return -1
      end
      if right.nil?
        if left[:kind] == :numeric
          return left[:value].zero? ? 0 : 1
        elsif left[:kind] == :pre
          return left[:rank] <= 3 ? -1 : 1
        end
        return 1
      end

      if left[:kind] == :numeric && right[:kind] == :numeric
        left[:value] <=> right[:value]
      elsif left[:kind] == :numeric
        1
      elsif right[:kind] == :numeric
        -1
      elsif left[:kind] == :pre && right[:kind] == :pre
        comparison = left[:rank] <=> right[:rank]
        return comparison unless comparison.zero?

        comparison = left[:value] <=> right[:value]
        return comparison unless comparison.zero?

        left[:revision] <=> right[:revision]
      else
        left[:value].to_s <=> right[:value].to_s
      end
    end

    def segment_at(index)
      segment = segments[index]
      segment.is_a?(Integer) ? segment : 0
    end

    def compare_segment(left, right)
      if left.is_a?(Integer) && right.is_a?(Integer)
        left <=> right
      elsif left.is_a?(Integer)
        1
      elsif right.is_a?(Integer)
        -1
      else
        left_text = left.to_s.downcase
        right_text = right.to_s.downcase
        left_rank = PRE_RELEASE_RANKS[left_text]
        right_rank = PRE_RELEASE_RANKS[right_text]
        return left_rank <=> right_rank if left_rank && right_rank

        left_text <=> right_text
      end
    end
  end

  # Formula version plus Homebrew's revision suffix (for example ``1.0_1``).
  class PkgVersion < Version
    attr_reader :version, :revision

    def self.parse(text)
      match = text.to_s.match(/\A(.+?)(?:_(\d+))?\z/)
      return new(Version.new(""), 0) if match.nil?

      new(Version.new(match[1].to_s), match[2].to_i)
    end

    def initialize(version, revision = 0)
      @version = version.is_a?(Version) ? version : Version.new(version.to_s)
      @revision = revision.to_i
      super(@revision.positive? ? "#{@version}_#{@revision}" : @version.to_s)
    end

    def segments
      @version.segments
    end

    def hash
      if self.class.homebrew_enabled?
        return [@version.send(:homebrew_hash_key), @revision].hash
      end

      super
    end

    def token_classes
      @version.token_classes
    end

    def homebrew_compare(other)
      other = PkgVersion.parse(other.to_s) unless other.is_a?(PkgVersion)
      comparison = @version.homebrew_compare(other.version)
      return comparison unless comparison.zero?

      @revision <=> other.revision
    end

    def major
      @version.major
    end

    def minor
      @version.minor
    end

    def patch
      @version.patch
    end

    def major_minor
      @version.major_minor
    end

    def major_minor_patch
      @version.major_minor_patch
    end

    def <=>(other)
      other = self.class.parse(other.to_s) unless other.is_a?(PkgVersion)
      comparison = @version <=> other.version
      return nil if comparison.nil?

      comparison.nonzero? || (@revision <=> other.revision)
    end

    def to_s
      @revision.positive? ? "#{@version}_#{@revision}" : @version.to_s
    end
  end

  # Stand-in for Homebrew's ``Formula`` class.  The test runner knows the
  # formula under test plus its declared dependencies (path metadata and, when
  # available from the configured local taps, test sources are passed in from
  # Python), so ``Formula["name"]`` resolves those and fails clearly otherwise.
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
      return @version if defined?(@version)

      @version = begin
        raw = @info["version"]
        if raw.nil? || raw.to_s.empty?
          pkg = @info["pkg_version"].to_s
          pkg.empty? ? nil : PkgVersion.parse(pkg).version
        else
          Version.new(raw.to_s)
        end
      end
    end

    def revision
      raw = @info["revision"]
      raw.nil? ? 0 : raw.to_i
    end

    def pkg_version
      return @pkg_version if defined?(@pkg_version)

      @pkg_version = begin
        raw = @info["pkg_version"]
        if raw.nil? || raw.to_s.empty?
          base = version
          base.nil? ? nil : PkgVersion.new(base, revision)
        else
          PkgVersion.parse(raw.to_s)
        end
      end
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
      @info["test_defined"] != false
    end

    def test_fixtures(file)
      @context.test_fixtures(file)
    end

    # Homebrew exposes the formula's ``test`` method (defined by ``test do``).
    # Dependency test bodies are loaded from the configured local taps on a
    # best-effort basis and run with their own throw-away testpath, cwd and
    # environment; the parent context is restored in ``ensure``.
    def test
      if @name == @context.name
        raise FormulaTestFailure,
              "Formula[#{@name.inspect}].test cannot be called from inside its own test block"
      end

      unless installed?
        raise FormulaTestFailure,
              "Formula[#{@name.inspect}].test is not available: the formula is not installed"
      end

      source = @info["test_source"].to_s
      if source.strip.empty?
        load_error = @info["test_source_error"].to_s
        if load_error.empty?
          raise FormulaTestFailure,
                "Formula[#{@name.inspect}].test is not available: it has no test block"
        end

        raise FormulaTestFailure,
              "Formula[#{@name.inspect}].test is not available: could not load its test " \
              "block (#{load_error})"
      end

      previous_context = FormulaTestContext.current
      previous_dir = Dir.pwd
      # Snapshot the process-global Homebrew constants so a dependency test
      # that rebinds them cannot leak into the parent test.  The mutation is
      # still process-wide while the dependency runs (single-threaded here);
      # only these two constants are restored.
      constant_names = %w[HOMEBREW_PREFIX HOMEBREW_CELLAR]
      previous_constants = {}
      constant_names.each do |name|
        previous_constants[name] = Object.const_get(name) if Object.const_defined?(name)
      end
      saved_env = {}
      hidden_env = {}
      dependency_testpath = nil
      begin
        dependency_testpath = @context.dependency_testpath(@name)
        dependency_env = @context.dependency_test_environment(@info, dependency_testpath)
        # Parent tests may leave PYBREW_* markers behind; the dependency must
        # not inherit them.  Framework keys are re-set explicitly below.
        ENV.keys.each do |key|
          next unless key.is_a?(String) && key.start_with?("PYBREW_")
          next if dependency_env.key?(key)
          next if FormulaTestContext::PYBREW_INHERITED_ENV.include?(key)
          hidden_env[key] = ENV[key]
          ENV.delete(key)
        end
        dependency_env.each do |key, value|
          saved_env[key] = ENV[key]
          ENV[key] = value.to_s
        end
        Dir.chdir(dependency_testpath)
        context = @context.dependency_test_context(@info, dependency_testpath)
        FormulaTestContext.evaluate_source(context, source, "(formula #{@name} test)")
      ensure
        FormulaTestContext.current = previous_context
        begin
          Dir.chdir(previous_dir)
        rescue StandardError
          # The original directory should still exist; keep going if it does not.
        end
        begin
          constant_names.each do |name|
            Object.send(:remove_const, name) if Object.const_defined?(name, false)
            Object.const_set(name, previous_constants[name]) if previous_constants.key?(name)
          end
        rescue StandardError
          # Best-effort: a dependency may have frozen or removed the constant.
        end
        hidden_env.each do |key, value|
          ENV[key] = value
        end
        saved_env.each do |key, value|
          value.nil? ? ENV.delete(key) : ENV[key] = value
        end
        if dependency_testpath && File.directory?(dependency_testpath)
          begin
            FileUtils.remove_entry(dependency_testpath)
          rescue StandardError
            # The throw-away test root is removed after the whole run anyway.
          end
        end
      end
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

  attr_reader :homebrew_prefix, :cellar, :formulae

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
      available = @formulae.keys.sort
      listing = available.first(20).join(", ")
      listing += ", ..." if available.length > 20
      raise FormulaTestFailure,
            "Formula[#{name.inspect}] is not available: it is not the formula under test, " \
            "a declared dependency, or a formula referenced by the loaded test bodies " \
            "(available: #{listing.empty? ? "none" : listing})"
    end

    canonical_name = info["name"]
    canonical_name = name if canonical_name.nil? || canonical_name.to_s.empty?
    Formula.new(self, canonical_name, info)
  end

  # Create an isolated throw-away testpath for a dependency's test body.
  def dependency_testpath(name)
    require "tmpdir"
    prefix = "pybrew-#{name.to_s.gsub(/[^A-Za-z0-9_.-]/, "-")}-test-"
    parent = @testpath.parent
    if parent.directory?
      Dir.mktmpdir(prefix, parent.to_s)
    else
      Dir.mktmpdir(prefix)
    end
  end

  # Environment for a dependency test: its own prefix/testpath/cwd/tmpdir/home
  # and a PATH that does not expose the parent formula's keg.
  def dependency_test_environment(info, testpath)
    dependency_prefix = info["prefix"].to_s
    parent_prefix = @paths["prefix"].to_s
    excluded = [File.join(parent_prefix, "bin"), File.join(parent_prefix, "sbin")]
    inherited = ENV["PATH"].to_s.split(File::PATH_SEPARATOR).reject do |entry|
      excluded.include?(entry)
    end
    path_entries = [
      File.join(dependency_prefix, "bin"),
      File.join(dependency_prefix, "sbin"),
    ] + inherited
    path_value = path_entries.reject { |entry| entry.nil? || entry.empty? }
    path_value = path_value.uniq.join(File::PATH_SEPARATOR)
    {
      "PYBREW_PREFIX" => dependency_prefix,
      "PYBREW_FORMULA_PREFIX" => dependency_prefix,
      "PYBREW_TESTPATH" => testpath.to_s,
      # HOMEBREW_PREFIX/HOMEBREW_CELLAR are process-global Ruby constants; only
      # their environment copies can be pinned per dependency.  A test body
      # that rebinds the constants still affects the whole process.
      "HOMEBREW_PREFIX" => @homebrew_prefix.to_s,
      "HOMEBREW_CELLAR" => @cellar.to_s,
      "PWD" => testpath.to_s,
      "TMPDIR" => testpath.to_s,
      "TEMP" => testpath.to_s,
      "TMP" => testpath.to_s,
      "HOME" => testpath.to_s,
      "GIT_CONFIG_GLOBAL" => File.join(testpath.to_s, ".gitconfig"),
      "XDG_CONFIG_HOME" => File.join(testpath.to_s, ".config"),
      "PATH" => path_value,
    }
  end

  # Build the context used to execute a dependency's test body.  It uses the
  # dependency's own testpath and resolves nested ``Formula[...]`` lookups
  # through the shared metadata map.
  def dependency_test_context(info, testpath = @testpath)
    FormulaTestContext.new(
      "name" => info["name"].to_s,
      "version" => info["version"].to_s,
      "testpath" => testpath.to_s,
      "prefix" => info["prefix"].to_s,
      "homebrew_prefix" => @homebrew_prefix.to_s,
      "cellar" => @cellar.to_s,
      "paths" => info["paths"] || {},
      "formulae" => @formulae
    )
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
    @version ||= Version.new(@config["version"].to_s)
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
    assert_respond_to pattern, :=~
    @assertions = assertions + 1
    regexp = pattern.is_a?(Regexp) ? pattern : Regexp.new(Regexp.escape(pattern.to_s))
    return if regexp.match?(string.to_s)

    raise_failure("Expected #{string.inspect} to match #{pattern.inspect}", message)
  end

  def refute_match(pattern, string, message = nil)
    assert_respond_to pattern, :=~
    @assertions = assertions + 1
    regexp = pattern.is_a?(Regexp) ? pattern : Regexp.new(Regexp.escape(pattern.to_s))
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
    assert_respond_to collection, :include?
    @assertions = assertions + 1
    return if collection.include?(object)

    raise_failure("Expected #{collection.inspect} to include #{object.inspect}", message)
  end

  def refute_includes(collection, object, message = nil)
    assert_respond_to collection, :include?
    @assertions = assertions + 1
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
    @assertions = assertions + 1
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
    return unless expected == actual

    raise_failure("Expected #{expected.inspect} to not equal #{actual.inspect}", message)
  end

  def assert_operator(left, operator, right = UNDEFINED, message = nil)
    if right.equal?(UNDEFINED)
      return if left.__send__(operator)

      raise_failure("Expected #{left.inspect} to satisfy #{operator}", message)
    end
    return if left.__send__(operator, right)

    raise_failure("Expected #{left.inspect} #{operator} #{right.inspect}", message)
  end

  def refute_operator(left, operator, right = UNDEFINED, message = nil)
    if right.equal?(UNDEFINED)
      return unless left.__send__(operator)

      raise_failure("Expected #{left.inspect} to not satisfy #{operator}", message)
    end
    return unless left.__send__(operator, right)

    raise_failure("Expected #{left.inspect} #{operator} #{right.inspect} to be falsey", message)
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
    rescue FormulaTestSkip
      raise
    rescue SignalException, SystemExit
      raise
    rescue *exceptions => error
      pass
      return error
    rescue Exception => error
      @assertions = assertions + 1
      raise_failure(
        "Expected #{exceptions.inspect} to be raised, but #{error.class} was raised: " \
        "#{error.message}",
        message
      )
    end
    @assertions = assertions + 1
    raise_failure("Expected #{exceptions.inspect} to be raised, but nothing was raised", message)
  end

  def assert_silent(message = nil)
    assert_output("", "", message) { yield }
  end

  def assert_path_not_exists(path, message = nil)
    return unless File.exist?(path.to_s)

    raise_failure("Expected path #{path.to_s.inspect} to not exist", message)
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
    difference = (expected - actual).abs
    delta = [expected.abs, actual.abs].min * epsilon
    return if difference <= delta

    raise_failure("Expected |#{expected} - #{actual}| (#{difference}) to be <= #{delta}", message)
  end

  def refute_in_epsilon(expected, actual, epsilon = 0.001, message = nil)
    difference = (expected - actual).abs
    delta = expected * epsilon
    return unless difference <= delta

    raise_failure("Expected |#{expected} - #{actual}| (#{difference}) to be > #{delta}", message)
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

  # Mark the formula test as skipped and stop executing it (Minitest's
  # ``skip``).  The runner reports the skip and exits successfully.
  def skip(message = nil)
    @skipped = true
    text = message.nil? || message.to_s.empty? ? "Skipped, no message given" : message.to_s
    raise FormulaTestSkip, text
  end

  def skipped?
    @skipped == true
  end

  def assert_send(send_array, message = nil)
    receiver, method_name, *arguments = Array(send_array)
    return if receiver.__send__(method_name, *arguments)

    raise_failure(
      "Expected #{receiver.inspect}.#{method_name}(*#{arguments.inspect}) to return true",
      message
    )
  end

  # Minitest's pattern-matching assertions (Ruby 2.7+ syntax in the test body).
  # On Ruby 2.6 the block cannot contain ``case ... in`` at all, but the methods
  # stay defined so bodies written for newer Rubies run when PYBREW_RUBY points
  # at a newer interpreter.
  def assert_pattern
    raise_failure("assert_pattern requires a block to capture errors.", nil) unless block_given?

    begin
      yield
    rescue NO_MATCHING_PATTERN_ERROR => error
      raise_failure(error.message, nil)
    end
  end

  def refute_pattern
    raise_failure("refute_pattern requires a block to capture errors.", nil) unless block_given?

    begin
      yield
    rescue NO_MATCHING_PATTERN_ERROR
      return
    end
    raise_failure("NoMatchingPatternError expected, but nothing was raised.", nil)
  end

  # Resolve a Homebrew-style test fixture from ``PYBREW_TEST_FIXTURES``.
  def test_fixtures(file)
    root = ENV["PYBREW_TEST_FIXTURES"]
    if root.nil? || root.to_s.empty?
      raise FormulaTestFailure,
            "test_fixtures is not configured: set PYBREW_TEST_FIXTURES to a fixtures directory"
    end
    path = Pathname.new(root.to_s)/file.to_s
    unless File.exist?(path)
      raise FormulaTestFailure,
            "test fixture #{file.to_s.inspect} does not exist under #{root}"
    end

    path
  end

  # Number of assertions executed so far (Minitest's ``assertions``).
  def assertions
    @assertions ||= 0
  end

  attr_writer :assertions

  ASSERTION_METHODS = %i[
    assert refute assert_equal refute_equal
    assert_predicate refute_predicate assert_empty refute_empty
    refute_output assert_not_equal assert_operator refute_operator assert_kind_of
    assert_instance_of assert_respond_to
    assert_path_exists refute_path_exists assert_path_not_exists
    assert_directory refute_directory assert_file refute_file
    assert_symlink refute_symlink assert_executable refute_executable
    assert_nil refute_nil assert_in_delta refute_in_delta
    assert_in_epsilon refute_in_epsilon assert_same refute_same
    refute_instance_of refute_kind_of refute_respond_to assert_throws
    assert_send assert_pattern refute_pattern flunk pass
  ].freeze

  module AssertionCounter
    ASSERTION_METHODS.each do |method_name|
      define_method(method_name) do |*args, &block|
        @assertions = assertions + 1
        super(*args, &block)
      end
    end
  end
  prepend AssertionCounter

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

    @assertions = assertions + 1
    # Minitest's assert_match also counts its internal assert_respond_to.
    @assertions = assertions + 1 if expected.is_a?(Regexp)
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

# Read-only platform helpers used by real formula test blocks.  Everything here
# is best-effort and local (no network, no installs).
module OS
  def self.mac?
    RUBY_PLATFORM.include?("darwin")
  end

  def self.linux?
    RUBY_PLATFORM.include?("linux")
  end

  # Kernel release string (for example "25.0.0" on macOS or "6.8.0" on Linux).
  def self.kernel_version
    return @kernel_version if defined?(@kernel_version)

    @kernel_version = begin
      require "etc"
      Etc.uname.fetch(:release, "unknown").to_s
    rescue StandardError
      "unknown"
    end
  end

  # OS version: macOS exposes a MacOSVersion, Linux a distribution string.
  def self.version
    if mac?
      Mac.version
    elsif linux?
      Linux.os_version
    else
      kernel_version
    end
  end

  module Linux
    def self.os_version
      return @os_version if defined?(@os_version)

      @os_version = begin
        release_file = "/etc/os-release"
        text = File.file?(release_file) ? File.read(release_file) : ""
        pretty = text[/^PRETTY_NAME="?([^"\n]+)"?$/, 1]
        version_id = text[/^VERSION_ID="?([^"\n]+)"?$/, 1]
        pretty || version_id || OS.kernel_version
      rescue StandardError
        OS.kernel_version
      end
    end
  end

  module Mac
    class Version
      include Comparable

      # Named macOS releases (symbol => version prefix).
      SYMBOLS = {
        golden_gate: "27",
        tahoe: "26",
        sequoia: "15",
        sonoma: "14",
        ventura: "13",
        monterey: "12",
        big_sur: "11",
        catalina: "10.15",
        mojave: "10.14",
        high_sierra: "10.13",
        sierra: "10.12",
        el_capitan: "10.11",
      }.freeze

      attr_reader :major, :minor, :patch

      def initialize(version)
        parts = version.to_s.split(".")
        @major = parts[0].to_i
        @minor = parts[1].to_i
        @patch = parts[2].to_i
      end

      def self.from_symbol(symbol)
        raw = SYMBOLS[symbol.to_sym]
        raw.nil? ? nil : new(raw)
      end

      def <=>(other)
        if other.is_a?(Symbol)
          other = self.class.from_symbol(other)
          return nil if other.nil?
          return 0 if strip_patch == other.strip_patch
        elsif other.is_a?(String) || other.is_a?(Numeric)
          other = self.class.new(other.to_s)
        end
        return nil unless other.respond_to?(:major) && other.respond_to?(:minor)

        [major, minor, patch] <=> [other.major, other.minor, other.patch]
      end

      def strip_patch
        if major >= 11
          self.class.new(major.to_s)
        else
          self.class.new("#{major}.#{minor}")
        end
      end

      def release_version
        stripped = strip_patch.to_s
        SYMBOLS.each do |_name, raw|
          return raw if self.class.new(raw) == self.class.new(stripped)
        end
        stripped
      end

      def release_name
        release = release_version
        SYMBOLS.each do |name, raw|
          next unless self.class.new(raw) == self.class.new(release)

          return name.to_s.split("_").map(&:capitalize).join(" ")
        end
        nil
      end

      def pretty_name
        release_name || "Dunno"
      end

      def to_sym
        stripped = strip_patch.to_s
        SYMBOLS.each do |name, raw|
          return name if self.class.new(raw) == self.class.new(stripped)
        end
        :dunno
      end

      def to_s
        if patch.positive?
          "#{major}.#{minor}.#{patch}"
        elsif minor.positive?
          "#{major}.#{minor}"
        else
          major.to_s
        end
      end

      def inspect
        "#<MacOSVersion: #{to_s.inspect}>"
      end
    end

    # A discovered macOS SDK (subset of Homebrew's ``OS::Mac::SDK``).
    class SDK
      attr_reader :version, :path, :source

      def initialize(version, path, source = :directory)
        @version = version
        @path = Pathname.new(path.to_s)
        @source = source.to_sym
      end

      def to_s
        "#<#{self.class.name} #{version} #{path}>"
      end
      alias inspect to_s
    end

    def self.full_version
      @full_version ||= Version.new(
        begin
          raw = ENV["PYBREW_MACOS_VERSION"].to_s
          if raw.empty? && OS.mac?
            raw = `/usr/bin/sw_vers -productVersion 2>/dev/null`.strip
          end
          raw.empty? ? "0" : raw
        rescue StandardError
          "0"
        end
      )
    end

    def self.version
      @version ||= full_version.strip_patch
    end

    def self.release_name
      version.release_name
    end

    def self.pretty_name
      version.pretty_name
    end

    # All discoverable macOS SDKs, sorted by version.
    #
    # Sources (best-effort, local only): ``PYBREW_SDK_PATH`` (single SDK),
    # ``SDKROOT``, versioned ``MacOSX*.sdk`` directories
    # (``PYBREW_SDK_DIRS`` or the default CLT/Xcode locations), and finally
    # ``xcrun --show-sdk-path``.
    def self.sdks
      return @sdks if defined?(@sdks)

      @sdks = begin
        override = ENV["PYBREW_SDK_PATH"].to_s
        if !override.empty?
          path = Pathname.new(override)
          version = path.directory? ? sdk_version_for_path(path) : nil
          version.nil? ? [] : [SDK.new(version, path, :override)]
        else
          candidates = []
          sdkroot = ENV["SDKROOT"].to_s
          if !sdkroot.empty?
            root_path = Pathname.new(sdkroot)
            root_version = root_path.directory? ? sdk_version_for_path(root_path) : nil
            candidates << SDK.new(root_version, root_path, :sdkroot) unless root_version.nil?
          end
          sdk_directory_paths.each do |path|
            version = sdk_version_for_path(path)
            candidates << SDK.new(version, path, :directory) unless version.nil?
          end
          if candidates.empty?
            xcrun_sdk = sdk_from_xcrun
            candidates << xcrun_sdk unless xcrun_sdk.nil?
            if candidates.empty? && ENV["PYBREW_SDK_DIRS"].to_s.empty?
              fallback = sdk_from_fallback_path
              candidates << fallback unless fallback.nil?
            end
          end
          dedupe_sdks(candidates)
        end
      rescue StandardError
        []
      end
    end

    # The SDK selected for ``version`` (or the current/latest SDK when nil).
    def self.sdk(version = nil)
      candidates = sdks
      return nil if candidates.empty?

      if version.nil?
        sdkroot = candidates.find { |candidate| candidate.source == :sdkroot }
        return sdkroot unless sdkroot.nil?

        current = self.version.strip_patch
        match = candidates.find { |candidate| candidate.version.strip_patch == current }
        return match unless match.nil?

        candidates.max_by { |candidate| candidate.version }
      else
        wanted = sdk_version_argument(version)
        return nil if wanted.nil?

        candidates.find { |candidate| candidate.version == wanted } ||
          candidates.find { |candidate| candidate.version.strip_patch == wanted.strip_patch }
      end
    end

    # Path to the selected SDK (nil when none can be found).
    def self.sdk_path(version = nil)
      sdk(version)&.path
    end

    # Version of the selected SDK (nil when unavailable).
    def self.sdk_version
      return @sdk_version if defined?(@sdk_version)

      @sdk_version = begin
        override = ENV["PYBREW_SDK_VERSION"].to_s
        if !override.empty?
          Version.new(override)
        else
          sdk&.version
        end
      rescue StandardError
        nil
      end
    end

    # Version of the latest available SDK (falls back to the OS version).
    def self.latest_sdk_version
      latest = sdks.max_by { |candidate| candidate.version }
      latest.nil? ? version : latest.version
    end

    def self.sdk_version_argument(version)
      case version
      when Version
        version
      when Symbol
        Version.from_symbol(version)
      else
        Version.new(version.to_s)
      end
    end
    private_class_method :sdk_version_argument

    def self.sdk_directory_paths
      raw = ENV["PYBREW_SDK_DIRS"].to_s
      dirs = if raw.empty?
        [
          "/Library/Developer/CommandLineTools/SDKs",
          "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/" \
            "Developer/SDKs",
        ].map { |dir| Pathname.new(dir) }
      else
        raw.split(File::PATH_SEPARATOR).map { |dir| Pathname.new(dir) }
      end
      dirs.flat_map do |dir|
        next [] unless dir.directory?

        Dir.glob((dir/"MacOSX*.sdk").to_s).sort.map { |path| Pathname.new(path) }
      end
    end
    private_class_method :sdk_directory_paths

    def self.dedupe_sdks(candidates)
      seen = {}
      candidates.each_with_object([]) do |candidate, result|
        key = candidate.path.to_s
        next if seen[key]

        seen[key] = true
        result << candidate
      end.sort_by { |candidate| candidate.version }
    end
    private_class_method :dedupe_sdks

    def self.sdk_from_xcrun
      return nil unless OS.mac? || xcrun_available?

      raw_path = xcrun("--show-sdk-path")
      return nil if raw_path.nil? || raw_path.empty?

      path = Pathname.new(raw_path)
      return nil unless path.directory?

      version = sdk_version_for_path(path)
      if version.nil?
        raw_version = xcrun("--show-sdk-version")
        version = Version.new(raw_version) unless raw_version.nil? || raw_version.empty?
      end
      version.nil? ? nil : SDK.new(version, path, :xcrun)
    rescue StandardError
      nil
    end
    private_class_method :sdk_from_xcrun

    def self.sdk_from_fallback_path
      path = fallback_sdk_path
      return nil if path.nil?

      version = sdk_version_for_path(path)
      version.nil? ? nil : SDK.new(version, path, :fallback)
    rescue StandardError
      nil
    end
    private_class_method :sdk_from_fallback_path

    def self.xcrun_available?
      binary = xcrun_binary
      !binary.nil? && File.executable?(binary)
    end
    private_class_method :xcrun_available?

    def self.xcrun_binary
      override = ENV["PYBREW_XCRUN"].to_s
      return override unless override.empty?

      ENV["PATH"].to_s.split(File::PATH_SEPARATOR).each do |dir|
        next if dir.empty?

        candidate = File.join(dir, "xcrun")
        return candidate if File.executable?(candidate)
      end
      candidate = "/usr/bin/xcrun"
      File.executable?(candidate) ? candidate : nil
    end
    private_class_method :xcrun_binary

    def self.xcrun(*args)
      binary = xcrun_binary
      return nil if binary.nil?

      stdout, status = Open3.capture2(binary, *args)
      status.success? ? stdout.strip : nil
    rescue StandardError
      nil
    end
    private_class_method :xcrun

    def self.sdk_version_for_path(path)
      settings = path/"SDKSettings.json"
      if settings.file?
        begin
          data = JSON.parse(settings.read)
          raw = data["Version"] || data["ProductVersion"]
          if raw.nil? || raw.to_s.empty?
            match = data["CanonicalName"].to_s.match(/([0-9]+(?:\.[0-9]+)*)/)
            raw = match[1] unless match.nil?
          end
          return Version.new(raw.to_s) unless raw.nil? || raw.to_s.empty?
        rescue StandardError
          # Fall through to the directory name.
        end
      end
      match = path.basename.to_s.match(/MacOSX([0-9.]+)\.sdk\z/)
      match.nil? ? nil : Version.new(match[1])
    end

    def self.fallback_sdk_path
      [
        "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
        "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/" \
          "Developer/SDKs/MacOSX.sdk",
      ].each do |candidate|
        return Pathname.new(candidate) if File.directory?(candidate)
      end
      nil
    end
  end
end

# Homebrew exposes ``MacOS`` as an alias for ``OS::Mac``.
MacOS = OS::Mac

module Hardware
  module CPU
    def self.arch
      host_cpu = RbConfig::CONFIG["host_cpu"].to_s
      host_cpu =~ /aarch64|arm/ ? :arm64 : :x86_64
    end

    def self.arm?
      arch == :arm64
    end

    def self.intel?
      !arm?
    end

    def self.is_64_bit?
      host_cpu = RbConfig::CONFIG["host_cpu"].to_s
      ["x86_64", "amd64"].include?(host_cpu) || arch == :arm64
    end

    # Raw CPU feature strings reported by the OS (downcased, deduplicated).
    def self.flags
      return @flags if defined?(@flags)

      @flags = begin
        values = []
        if OS.mac?
          sysctl = "/usr/sbin/sysctl -n machdep.cpu.features " \
                   "machdep.cpu.extfeatures machdep.cpu.leaf7_features"
          output = `#{sysctl} 2>/dev/null`.to_s
          values = output.split
        elsif File.file?("/proc/cpuinfo")
          text = File.read("/proc/cpuinfo")
          match = text[/^(?:flags|Features)\s*:\s*(.*)$/, 1]
          values = match.to_s.split
        end
        values.map { |value| value.to_s.downcase }.uniq
      rescue StandardError
        []
      end
    end

    def self.features
      @features ||= flags.map { |flag| flag.to_sym }
    end

    def self.feature?(name)
      features.include?(name.to_sym)
    end

    def self.sse4_2?
      feature?(:sse4_2) || feature?(:"sse4.2") || sysctl_bool?("hw.optional.sse4_2")
    end

    def self.avx512?
      feature?(:avx512f) || sysctl_bool?("hw.optional.avx512f")
    end

    def self.sysctl_bool?(name)
      return false unless OS.mac?

      output = `/usr/sbin/sysctl -n #{name} 2>/dev/null`.strip
      output == "1"
    rescue StandardError
      false
    end

    def self.cores
      require "etc"
      Etc.nprocessors
    rescue StandardError
      1
    end

    def self.physical_cores
      cores
    end
  end
end

config = JSON.parse(File.read(ARGV[0]))
HOMEBREW_PREFIX = Pathname.new(config["homebrew_prefix"].to_s)
HOMEBREW_CELLAR = Pathname.new(config["cellar"].to_s)

def run_formula_test_body(config)
  source = File.read(ARGV[1])
  context = FormulaTestContext.new(config)
  FormulaTestContext.evaluate_source(context, source)
end

begin
  run_formula_test_body(config)
rescue FormulaTestSkip => error
  $stdout.puts "Formula test skipped: #{error.message}"
  exit 0
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


def _test_paths(name: str, config: Config, keg: Path) -> dict[str, str]:
    opt = config.opt_path(name)
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
        "opt_pkgshare": opt / "share" / name,
        "pkgshare": share / name,
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
        "doc": share / "doc" / name,
        "info": share / "info",
        "pkgetc": config.prefix / "etc" / name,
        "bash_completion": keg / "etc" / "bash_completion.d",
        "zsh_completion": share / "zsh" / "site-functions",
        "fish_completion": share / "fish" / "vendor_completions.d",
        "elisp": share / "emacs" / "site-lisp" / name,
    }
    return {name: str(path) for name, path in mapping.items()}


def _split_pkg_version(pkg_version: str) -> tuple[str, int]:
    """Split ``1.0_2`` into ``("1.0", 2)`` (best-effort)."""

    match = re.match(r"\A(.+?)_(\d+)\Z", pkg_version)
    if match is None:
        return pkg_version, 0
    return match.group(1), int(match.group(2))


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


class _TapIndex:
    """Cheap name/alias index over formulae in the configured local taps."""

    def __init__(self, taps: list[Tap]) -> None:
        self.taps = taps
        self.by_name: dict[str, tuple[Tap, Path]] = {}
        self.by_qualified_name: dict[str, tuple[Tap, Path]] = {}
        self.by_alias: dict[str, tuple[Tap, Path]] = {}
        self.by_qualified_alias: dict[str, tuple[Tap, Path]] = {}
        self.source_aliases: dict[str, tuple[Tap, Path]] = {}
        self._source_alias_attempted = False
        self.source_alias_budget_exhausted = False

    def add(self, tap: Tap, path: Path) -> None:
        name = path.stem
        self.by_name.setdefault(name, (tap, path))
        self.by_qualified_name[f"{tap.name}/{name}"] = (tap, path)

    def add_alias(self, tap: Tap, alias: str, path: Path) -> None:
        self.by_alias.setdefault(alias, (tap, path))
        self.by_qualified_alias[f"{tap.name}/{alias}"] = (tap, path)

    def resolve(self, name: str) -> Optional[tuple[Tap, Path]]:
        candidates = [name]
        parts = name.split("/")
        if len(parts) > 1:
            candidates.append(parts[-1])
            candidates.append("/".join(parts[-2:]))
        for candidate in candidates:
            for mapping in (
                self.by_qualified_name,
                self.by_name,
                self.by_qualified_alias,
                self.by_alias,
            ):
                resolved = mapping.get(candidate)
                if resolved is not None:
                    return resolved
        return None

    def resolve_source_alias(self, name: str) -> Optional[tuple[Tap, Path]]:
        """Resolve an alias declared in formula source (no Aliases symlink).

        The scan is lazy (only when the cheap indexes miss), bounded by
        :data:`MAX_SOURCE_ALIAS_SCAN`, and cached on the index.
        """

        alias = name.split("/")[-1]
        if not self._source_alias_attempted:
            self._scan_source_aliases()
        return self.source_aliases.get(alias)

    def _scan_source_aliases(self) -> None:
        self._source_alias_attempted = True
        pattern = re.compile(r"""^\s*alias\s+["']([^"']+)["']""", re.MULTILINE)
        scanned = 0
        for tap in self.taps:
            for path in tap.formula_paths():
                if scanned >= MAX_SOURCE_ALIAS_SCAN:
                    self.source_alias_budget_exhausted = True
                    return
                scanned += 1
                try:
                    source = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for alias_name in pattern.findall(source):
                    self.source_aliases.setdefault(alias_name, (tap, path))


def _tap_alias_entries(tap: Tap) -> list[tuple[str, Path]]:
    """Return ``(alias, formula_path)`` pairs from a tap's ``Aliases`` dir.

    Homebrew taps expose aliases as symlinks such as
    ``Aliases/foo -> ../Formula/bar.rb``; only that cheap metadata is used (no
    formula sources are parsed here).
    """

    aliases_dir = tap.path / "Aliases"
    if not aliases_dir.is_dir():
        return []
    entries: list[tuple[str, Path]] = []
    for alias_path in sorted(aliases_dir.iterdir()):
        if alias_path.name.startswith("."):
            continue
        try:
            target = Path(os.readlink(alias_path))
        except OSError:
            continue
        if not target.is_absolute():
            target = alias_path.parent / target
        target = Path(os.path.normpath(target))
        if target.suffix != ".rb":
            continue
        entries.append((alias_path.name, target))
    return entries


def _tap_index_key(config: Config) -> tuple[tuple[str, int], ...]:
    parts: list[tuple[str, int]] = []
    for path in config.taps:
        for candidate in (path, path / "Aliases"):
            try:
                stamp = candidate.stat().st_mtime_ns if candidate.exists() else 0
            except OSError:
                stamp = 0
            parts.append((str(candidate), stamp))
    return tuple(parts)


def _tap_index(config: Config) -> _TapIndex:
    """Return a bounded, cached index of formulae in the local taps."""

    key = _tap_index_key(config)
    cached = _tap_index_cache.get(key)
    if cached is not None:
        return cached
    index = _TapIndex(discover_taps(config.taps))
    count = 0
    for tap in index.taps:
        for path in tap.formula_paths():
            if count >= MAX_TAP_FORMULA_INDEX:
                break
            index.add(tap, path)
            count += 1
        if count >= MAX_TAP_FORMULA_INDEX:
            break
    alias_count = 0
    for tap in index.taps:
        for alias_name, alias_target in _tap_alias_entries(tap):
            if alias_count >= MAX_TAP_FORMULA_INDEX:
                break
            index.add_alias(tap, alias_name, alias_target)
            alias_count += 1
        if alias_count >= MAX_TAP_FORMULA_INDEX:
            break
    if len(_tap_index_cache) >= TAP_INDEX_CACHE_LIMIT:
        _tap_index_cache.clear()
    _tap_index_cache[key] = index
    return index


def _load_tap_formula(path: Path, tap_name: str) -> Formula:
    """Parse a local tap formula file, cached by path/mtime/size."""

    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    if key is not None:
        cached = _formula_cache.get(key)
        if cached is not None:
            return cached
    formula = parse_formula_file(path, tap=tap_name)
    if key is not None:
        if len(_formula_cache) >= FORMULA_CACHE_LIMIT:
            _formula_cache.clear()
        _formula_cache[key] = formula
    return formula


_FORMULA_REFERENCE_RE = re.compile(
    r"""Formula\s*\[\s*["'](?P<bracket>[^"']+)["']\s*\]"""
    r"""|(?:Formula|Formulary)\.factory\(\s*["'](?P<factory>[^"']+)["']\s*\)"""
)


def _referenced_formula_names(source: str) -> list[str]:
    """Return literal ``Formula[...]``/``Formula.factory(...)`` names in source.

    Dynamic lookups such as ``Formula[some_variable]`` cannot be resolved ahead
    of time; those names stay unavailable and fail with a clear error.
    """

    names: list[str] = []
    for match in _FORMULA_REFERENCE_RE.finditer(source or ""):
        name = match.group("bracket") or match.group("factory")
        if name:
            names.append(name)
    return names


def _formula_entry(
    name: str,
    config: Config,
    loaded: Optional[Formula] = None,
    *,
    load_error: Optional[str] = None,
) -> dict[str, object]:
    """Build one ``Formula[...]`` metadata entry for ``name``."""

    keg = Cellar(config).keg(name)
    if loaded is not None:
        base_version = loaded.version
        revision = int(loaded.revision or 0)
        pkg_version = loaded.pkg_version
        dependencies = _declared_dependency_names(loaded)
        test_source = loaded.test_source or ""
        test_defined = bool(test_source.strip())
        full_name = loaded.full_name
    else:
        base_version = None
        revision = 0
        pkg_version = None
        dependencies = []
        test_source = ""
        test_defined = False
        full_name = name
    if keg is not None:
        pkg_version = keg.name
        base_version, revision = _split_pkg_version(keg.name)
    entry: dict[str, object] = {
        "name": name,
        "full_name": full_name,
        "version": base_version,
        "revision": revision,
        "pkg_version": pkg_version,
        "prefix": str(keg) if keg is not None else None,
        "opt_prefix": str(config.opt_path(name)),
        "installed": keg is not None,
        "dependencies": dependencies,
        "test_source": test_source,
        "test_source_error": load_error,
        "test_defined": test_defined,
    }
    if keg is not None:
        entry["paths"] = _test_paths(name, config, keg)
    return entry


def _formula_entries(
    formula: Formula, config: Config, keg: Path
) -> dict[str, dict[str, object]]:
    """Metadata for ``Formula[...]`` lookups inside the test body.

    Beyond the formula under test and its declared dependency graph, formulae
    referenced literally in captured test bodies and formulae with an installed
    keg are loaded from the configured local taps (best-effort, bounded and
    cached).  Tap-qualified names (``tap/name``) and aliases are exposed as
    extra keys; dynamic ``Formula[expression]`` lookups cannot be preloaded and
    fail with a clear error.
    """

    dependencies = _declared_dependency_names(formula)
    opt = config.opt_path(formula.name)
    if not opt.exists():
        opt = keg
    self_entry: dict[str, object] = {
        "name": formula.name,
        "full_name": formula.full_name,
        "version": formula.version,
        "revision": int(formula.revision or 0),
        "pkg_version": formula.pkg_version,
        "prefix": str(keg),
        "opt_prefix": str(opt),
        "installed": True,
        "dependencies": dependencies,
        "test_defined": bool((formula.test_source or "").strip()),
        "test_source": "",
        "test_source_error": "re-entrant test execution is not supported",
    }
    entries: dict[str, dict[str, object]] = {formula.name: self_entry}
    tap_name = getattr(formula, "tap", None)
    if tap_name:
        entries.setdefault(f"{tap_name}/{formula.name}", self_entry)
    for alias in getattr(formula, "aliases", None) or []:
        entries.setdefault(str(alias), self_entry)

    index = _tap_index(config)
    queue: deque[tuple[str, bool]] = deque()
    for dependency in dependencies:
        queue.append((dependency, True))
    for reference in _referenced_formula_names(formula.test_source):
        queue.append((reference, False))
    try:
        installed_names = sorted(Cellar(config).installed())
    except OSError:
        installed_names = []
    for installed_name in installed_names:
        queue.append((installed_name, False))
    taps_listing = ", ".join(tap.name for tap in index.taps) or "no taps configured"
    seen: set[str] = {formula.name}
    while queue and len(seen) < MAX_FORMULA_ENTRIES:
        requested, required = queue.popleft()
        if requested in seen or requested in entries:
            continue
        seen.add(requested)
        resolved = index.resolve(requested)
        if resolved is None:
            resolved = index.resolve_source_alias(requested)
        if resolved is None:
            if required:
                entries[requested] = _formula_entry(
                    requested,
                    config,
                    load_error=(
                        f"no formula named {requested!r} in configured taps "
                        f"({taps_listing})"
                    ),
                )
            continue
        tap, path = resolved
        canonical = path.stem
        seen.add(canonical)
        loaded: Optional[Formula] = None
        load_error: Optional[str] = None
        try:
            loaded = _load_tap_formula(path, tap.name)
        except Exception as error:  # noqa: BLE001 - best-effort local lookup
            load_error = f"{type(error).__name__}: {error}"
        if loaded is None:
            entry = _formula_entry(canonical, config, load_error=load_error)
            keys = (requested, canonical, f"{tap.name}/{canonical}")
        else:
            entry = _formula_entry(canonical, config, loaded)
            keys = (
                requested,
                canonical,
                f"{tap.name}/{canonical}",
                loaded.full_name,
            )
            keys += tuple(str(alias) for alias in (loaded.aliases or []))
            for child in _declared_dependency_names(loaded):
                queue.append((child, False))
            for reference in _referenced_formula_names(loaded.test_source):
                queue.append((reference, False))
        for key in keys:
            if key and key not in entries:
                entries[key] = entry

    # Lightweight fallback entries make dynamic ``Formula[expression]`` lookups
    # work across a small local tap without parsing every formula source.  The
    # real test body is only loaded when the formula is actually referenced
    # literally, installed, or declared as a dependency.
    if len(index.by_name) <= MAX_EAGER_FORMULA_ENTRIES:
        for eager_name in sorted(index.by_name):
            if eager_name in entries or len(entries) >= MAX_FORMULA_ENTRIES:
                continue
            resolved = index.resolve(eager_name)
            if resolved is None:
                continue
            tap, _path = resolved
            entry = _formula_entry(
                eager_name,
                config,
                load_error="formula source was not eagerly loaded for this test",
            )
            entries[eager_name] = entry
            entries.setdefault(f"{tap.name}/{eager_name}", entry)

    # Alias metadata is cheap (tap ``Aliases`` symlinks) and can point at
    # formulae whose sources were not parsed eagerly.
    for alias_name in sorted(index.by_alias):
        if len(entries) >= MAX_FORMULA_ENTRIES:
            break
        alias_tap, alias_target = index.by_alias[alias_name]
        canonical = alias_target.stem
        entry = entries.get(canonical) or entries.get(f"{alias_tap.name}/{canonical}")
        if entry is None:
            continue
        entries.setdefault(alias_name, entry)
        entries.setdefault(f"{alias_tap.name}/{alias_name}", entry)

    # Source-only aliases are only known when the lazy scan already ran; keep
    # the normal (name) lookup path free of source parsing.
    for alias_name in sorted(index.source_aliases):
        if len(entries) >= MAX_FORMULA_ENTRIES:
            break
        alias_tap, alias_target = index.source_aliases[alias_name]
        canonical = alias_target.stem
        entry = entries.get(canonical) or entries.get(f"{alias_tap.name}/{canonical}")
        if entry is None:
            continue
        entries.setdefault(alias_name, entry)
        entries.setdefault(f"{alias_tap.name}/{alias_name}", entry)
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
    stderr attached.  A test body that calls ``skip`` exits successfully and
    reports ``Formula test skipped: ...`` on stdout.

    ``Formula["dep"].test`` executes a dependency's captured test body when the
    dependency is installed and its formula source is available in the
    configured local taps; otherwise it fails with a clear error.  Dependency
    tests run with their own throw-away ``testpath``, cwd and environment, and
    the parent context is restored afterwards.

    ``Formula[...]`` lookups resolve the formula under test, its declared
    dependencies, formulae referenced literally in loaded test bodies, and
    installed formulae, using metadata from the configured local taps only
    (never the network).  Dynamic lookups of formulae that were not preloaded
    fail with a clear error.

    ``PYBREW_TEST_FIXTURES`` (directory used by the ``test_fixtures`` helper)
    and ``PYBREW_MACOS_VERSION`` (override used by ``MacOS.version`` inside
    tests) are read from the child environment and can be supplied via
    ``env``.
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
        "version": formula.version or "HEAD",
        "testpath": str(testpath),
        "prefix": str(keg),
        "homebrew_prefix": str(config.prefix),
        "cellar": str(config.cellar),
        "paths": _test_paths(formula.name, config, keg),
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
