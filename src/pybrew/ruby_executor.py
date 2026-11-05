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
import functools
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from .errors import BuildError, UnsupportedInstall
from .models import Formula
from .paths import Config
from .ruby_parser import Lexer, Token
from .version import compare_versions, detect_version

#: Environment variable that opts a build into the arbitrary-Ruby bridge.
ALLOW_ENV = "PYBREW_ALLOW_ARBITRARY_RUBY"

#: Directory under the build root where the Ruby bridge stages resources.
RESOURCE_STAGE_DIR = ".pybrew-resources"

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
require "digest/sha2"
require "etc"
require "fileutils"
require "json"
require "pathname"
require "rbconfig"
require "shellwords"
require "stringio"
require "open3"
require "openssl"
require "tempfile"
require "tmpdir"
require "uri"

$stdout.sync = true
$stderr.sync = true

PYBREW_CONFIG = JSON.parse(Base64.strict_decode64("__PYBREW_CONFIG__"))
PYBREW_BODY = Base64.strict_decode64("__PYBREW_BODY__")

HOMEBREW_PREFIX = Pathname.new(
  PYBREW_CONFIG["homebrew_prefix"] || ENV["HOMEBREW_PREFIX"] || "/usr/local"
)
HOMEBREW_CELLAR = Pathname.new(
  PYBREW_CONFIG["cellar"] || ENV["HOMEBREW_CELLAR"] || (HOMEBREW_PREFIX/"Cellar").to_s
)
HOMEBREW_PINNED_KEGS = HOMEBREW_PREFIX/"var/homebrew/pinned"
HOMEBREW_TEMP = Pathname.new(ENV["HOMEBREW_TEMP"] || "/tmp")
# Homebrew always defines these; the shim derives bounded local fallbacks so
# Sandbox/SystemConfig helpers never trip over an undefined constant.
HOMEBREW_REPOSITORY = Pathname.new(
  PYBREW_CONFIG["repository"] || ENV["HOMEBREW_REPOSITORY"] || HOMEBREW_PREFIX.to_s
)
HOMEBREW_CACHE = Pathname.new(
  PYBREW_CONFIG["cache"] || ENV["HOMEBREW_CACHE"] ||
    (RUBY_PLATFORM.include?("darwin") ? "#{Dir.home}/Library/Caches/Homebrew" : "#{Dir.home}/.cache/Homebrew")
)
HOMEBREW_LOGS = Pathname.new(
  PYBREW_CONFIG["logs"] || ENV["HOMEBREW_LOGS"] || (HOMEBREW_PREFIX/"var/log").to_s
)
HOMEBREW_PREFIX_PLACEHOLDER = "$HOMEBREW_PREFIX"
HOMEBREW_CELLAR_PLACEHOLDER = "$HOMEBREW_CELLAR"
HOMEBREW_HOME_PLACEHOLDER = "$HOMEBREW_HOME"
HOMEBREW_VERSION = "0.0.0"
# Shared by Homebrew and the standalone compiler shim (upstream 7.0).
HOMEBREW_PACKAGE_NAME_REGEX = /[\w+\-.@]+/
HOMEBREW_CURL_HEADER_FILE_MIN_VERSION = "7.55.0"

module OS
  def self.mac?
    !!(RUBY_PLATFORM =~ /darwin/)
  end

  def self.linux?
    !!(RUBY_PLATFORM =~ /linux/)
  end

  def self.kernel_release
    release = (PYBREW_CONFIG["kernel_version"] || "").to_s.strip
    if release.empty?
      begin
        release = Etc.uname[:release].to_s.strip
      rescue StandardError
        release = ""
      end
    end
    release.empty? ? "0" : release
  end

  # On macOS this mirrors MacOS.version; other platforms fall back to the
  # kernel release so simple version checks keep working.
  def self.version
    return MacOS.version if mac?

    @version ||= PybrewVersion.new(kernel_release)
  end
end

module PybrewCPU
  module_function

  # A conservative subset of Homebrew's Linux CPU-model table.  Unknown
  # models fall back to :dunno rather than guessing.
  def linux_intel_family(family, model)
    return :dunno unless family == 0x06

    case model
    when 0x3a, 0x3e then :ivybridge
    when 0x2a, 0x2d then :sandybridge
    when 0x25, 0x2c, 0x2f then :westmere
    when 0x1a, 0x1e, 0x1f, 0x2e then :nehalem
    when 0x17, 0x1d then :penryn
    when 0x0f, 0x16 then :merom
    when 0x3c, 0x3f, 0x45, 0x46 then :haswell
    when 0x3d, 0x47, 0x4f, 0x56 then :broadwell
    when 0x4e, 0x5e, 0x8e, 0x9e, 0xa5, 0xa6 then :skylake
    when 0x66 then :cannonlake
    when 0x6a, 0x6c, 0x7d, 0x7e then :icelake
    when 0xa7 then :rocketlake
    when 0x8c, 0x8d then :tigerlake
    when 0x97, 0x9a, 0xbe, 0xb7, 0xba, 0xbf, 0xaa, 0xac then :alderlake
    else :dunno
    end
  end
end

module Hardware
  module CPU
    HOST_CPU = (PYBREW_CONFIG["arch"] || RbConfig::CONFIG["host_cpu"]).to_s.downcase

    INTEL_FAMILIES = {
      0x73d67300 => :core,
      0x426f69ef => :core2,
      0x78ea4fbc => :penryn,
      0x6b5a4cd2 => :nehalem,
      0x573b5eec => :westmere,
      0x5490b78c => :sandybridge,
      0x1f65e835 => :ivybridge,
      0x10b282dc => :haswell,
      0x582ed09c => :broadwell,
      0x37fc219f => :skylake,
      0x0f817246 => :kabylake,
      0x38435547 => :icelake,
      0x1cf8a03e => :cometlake,
    }.freeze

    ARM_FAMILIES = {
      0x2c91a47e => :arm_typhoon,
      0x92fb37c8 => :arm_twister,
      0x67ceee93 => :arm_hurricane_zephyr,
      0xe81e7ef6 => :arm_monsoon_mistral,
      0x07d34b9f => :arm_vortex_tempest,
      0x462504d2 => :arm_lightning_thunder,
      0x573b5eec => :arm_firestorm_icestorm,
      0xda33d83d => :arm_blizzard_avalanche,
      0xfa33415e => :arm_ibiza,
      0x5f4dea93 => :arm_lobos,
      0x72015832 => :arm_palma,
      0x6f5129ac => :arm_donan,
      0x17d5b93a => :arm_brava,
      0x1d5a87e8 => :arm_hidra,
      0xf76c5b1a => :arm_sotra,
    }.freeze

    def self.arm?
      !!(HOST_CPU =~ /arm|aarch64/)
    end

    def self.arm64?
      arm? && is_64_bit?
    end

    def self.intel?
      !!(HOST_CPU =~ /x86_64|amd64|i[3-6]86/)
    end

    def self.x86_64?
      intel? && is_64_bit?
    end

    def self.ppc?
      !!(HOST_CPU =~ /ppc|powerpc/)
    end

    def self.is_arm?
      arm?
    end

    def self.is_intel?
      intel?
    end

    def self.bits
      return @bits if defined?(@bits)

      @bits = case HOST_CPU
              when /x86_64|amd64|aarch64|arm64|ppc64|powerpc64/ then 64
              when /i[3-6]86|arm|ppc|powerpc/ then 32
              else
                RbConfig::CONFIG["host_cpu"].to_s =~ /64/ ? 64 : 32
              end
    end

    def self.is_64_bit?
      bits == 64
    end

    def self.is_32_bit?
      bits == 32
    end

    def self.arch
      case bits
      when 64
        arm? ? :arm64 : (intel? ? :x86_64 : :dunno)
      when 32
        arm? ? :arm : (intel? ? :i386 : :dunno)
      else
        :dunno
      end
    end

    def self.type
      if arm?
        :arm
      elsif intel?
        :intel
      elsif ppc?
        :ppc
      else
        :dunno
      end
    end

    def self.flags
      return @flags if defined?(@flags)

      values = []
      begin
        if OS.mac?
          output = `/usr/sbin/sysctl -n machdep.cpu.features machdep.cpu.extfeatures machdep.cpu.leaf7_features 2>/dev/null`.to_s
          values = output.split
        elsif File.file?("/proc/cpuinfo")
          text = File.read("/proc/cpuinfo")
          match = text[/^(?:flags|Features)\s*:\s*(.*)$/, 1]
          values = match.to_s.split
        end
      rescue StandardError
        values = []
      end
      @flags = values.map { |value| value.to_s.downcase }.uniq
    end

    def self.features
      @features ||= flags.map { |flag| flag.to_sym }
    end

    def self.feature?(name)
      features.include?(name.to_sym)
    end

    def self.neon?
      return true if arm? && OS.mac?

      flags.include?("neon") || flags.include?("asimd")
    end

    def self.sse4?
      flags.include?("sse4_1") || flags.include?("sse4_2") || sysctl_flag?("hw.optional.sse4_1")
    end

    def self.avx?
      flags.include?("avx") || sysctl_flag?("hw.optional.avx1_0")
    end

    def self.avx2?
      flags.include?("avx2") || sysctl_flag?("hw.optional.avx2_0")
    end

    def self.sse4_2?
      flags.include?("sse4_2") || sysctl_flag?("hw.optional.sse4_2")
    end

    def self.avx512?
      flags.include?("avx512f") || sysctl_flag?("hw.optional.avx512f")
    end

    def self.model
      return @model if defined?(@model)

      @model = begin
        if OS.mac?
          value = sysctl_value("machdep.cpu.model")
          value.nil? ? nil : (value =~ /\A\d+\z/ ? value.to_i : nil)
        elsif File.file?("/proc/cpuinfo")
          match = File.read("/proc/cpuinfo")[/^model\s*:\s*(\d+)/, 1]
          match.nil? ? nil : match.to_i
        end
      rescue StandardError
        nil
      end
    end

    def self.family
      return @family if defined?(@family)

      @family = begin
        if OS.mac?
          value = sysctl_value("hw.cpufamily")
          key = value.nil? ? nil : value.to_i
          if arm?
            ARM_FAMILIES[key] || :dunno
          elsif intel?
            INTEL_FAMILIES[key] || :dunno
          else
            :dunno
          end
        elsif File.file?("/proc/cpuinfo")
          if arm?
            :arm
          elsif intel?
            text = File.read("/proc/cpuinfo")
            family_number = text[/^cpu family\s*:\s*(\d+)/, 1].to_i
            model_number = text[/^model\s*:\s*(\d+)/, 1].to_i
            PybrewCPU.linux_intel_family(family_number, model_number)
          else
            :dunno
          end
        else
          :dunno
        end
      rescue StandardError
        :dunno
      end
    end

    def self.cores
      require "etc"
      Etc.nprocessors
    rescue StandardError
      1
    end

    def self.physical_cores
      @physical_cores ||= begin
        count = nil
        if OS.mac?
          output = `/usr/sbin/sysctl -n hw.physicalcpu 2>/dev/null`.strip
          count = output.to_i if output =~ /\A\d+\z/
        elsif OS.linux? && File.file?("/proc/cpuinfo")
          text = File.read("/proc/cpuinfo")
          sockets = text.scan(/^physical id\s*:\s*(\d+)/).flatten.uniq.length
          per_socket = text.scan(/^cpu cores\s*:\s*(\d+)/).flatten.first.to_i
          count = sockets * per_socket if sockets > 0 && per_socket > 0
        end
        count && count > 0 ? count : cores
      rescue StandardError
        cores
      end
    end

    def self.sysctl_value(key)
      return nil unless OS.mac?

      output = `/usr/sbin/sysctl -n #{key} 2>/dev/null`.to_s.strip
      output.empty? ? nil : output
    rescue StandardError
      nil
    end

    def self.sysctl_flag?(key)
      value = sysctl_value(key)
      !value.nil? && value.to_i == 1
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

  def arch
    (PYBREW_CONFIG["arch"] || RbConfig::CONFIG["host_cpu"]).to_s
  end

  def target
    arch
  end

  def options
    @options ||= PybrewOptions.new
  end
end

class PybrewChecksumMismatch < RuntimeError; end

# Formulary raises these for formula source that cannot be loaded or whose
# class cannot be found.  They mirror Homebrew's ``FormulaUnavailableError``
# family closely enough for formula/test code to rescue by class.
class PybrewFormulaUnavailableError < RuntimeError; end
class PybrewFormulaUnreadableError < PybrewFormulaUnavailableError; end
class PybrewFormulaClassUnavailableError < PybrewFormulaUnavailableError; end
class PybrewFormulaAmbiguityError < PybrewFormulaUnavailableError; end
class PybrewGitHubGraphqlError < RuntimeError; end

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

class PybrewSDK
  attr_reader :version, :path

  def initialize(version, path)
    @version = version
    @path = path
  end

  def to_s
    path.to_s
  end
end

module MacOS
  RELEASES = {
    :el_capitan => "10.11",
    :sierra => "10.12",
    :high_sierra => "10.13",
    :mojave => "10.14",
    :catalina => "10.15",
    :big_sur => "11",
    :monterey => "12",
    :ventura => "13",
    :sonoma => "14",
    :sequoia => "15",
    :tahoe => "26",
    :golden_gate => "27",
  }.freeze

  SYMBOLS = RELEASES

  class Version < PybrewVersion
    def strip_patch
      major_number = major.to_i
      major_number >= 11 ? major_number.to_s : major_minor
    end

    def to_sym
      MacOS::RELEASES.invert.fetch(strip_patch, :dunno)
    end

    def pretty_name
      symbol = to_sym
      return "Dunno" if symbol == :dunno

      symbol.to_s.split("_").map(&:capitalize).join(" ")
    end

    # Compare against release symbols as Homebrew formulae do:
    # ``MacOS.version >= :ventura``.
    def <=>(other)
      if other.is_a?(Symbol)
        other = MacOS::RELEASES[other]
        return nil if other.nil?
      end
      return nil unless other.is_a?(String)

      left = to_s.split(".").map(&:to_i)
      right = other.to_s.split(".").map(&:to_i)
      [left.length, right.length].max.times do |index|
        a = left[index] || 0
        b = right[index] || 0
        return a <=> b unless a == b
      end
      0
    end

    def ==(other)
      return to_sym == other if other.is_a?(Symbol)

      super
    end
    alias eql? ==
  end

  def self.xcrun_executable
    raw = ENV["PYBREW_XCRUN"].to_s.strip
    raw = ENV["HOMEBREW_XCRUN"].to_s.strip if raw.empty?
    raw.empty? ? "/usr/bin/xcrun" : raw
  end

  def self.xcode_select_executable
    raw = ENV["PYBREW_XCODE_SELECT"].to_s.strip
    raw = ENV["HOMEBREW_XCODE_SELECT"].to_s.strip if raw.empty?
    raw.empty? ? "/usr/bin/xcode-select" : raw
  end

  def self.pkgutil_executable
    raw = ENV["PYBREW_PKGUTIL"].to_s.strip
    raw = ENV["HOMEBREW_PKGUTIL"].to_s.strip if raw.empty?
    raw.empty? ? "/usr/sbin/pkgutil" : raw
  end

  def self.run_local_tool(*command)
    return "" if command.compact.empty?

    Utils.popen_read(*command.map(&:to_s)).to_s
  rescue ScriptError, StandardError
    ""
  end

  def self.full_version
    @full_version ||= begin
      raw = ENV["HOMEBREW_MACOS_VERSION"].to_s.strip
      raw = PYBREW_CONFIG["macos_version"].to_s.strip if raw.empty?
      if raw.empty? && OS.mac?
        raw = `/usr/bin/sw_vers -productVersion 2>/dev/null`.strip
      end
      if raw.empty?
        raise "MacOS.full_version is not available on this host: " \
              "not macOS and HOMEBREW_MACOS_VERSION is not set"
      end
      Version.new(raw)
    end
  end

  def self.version
    @version ||= Version.new(full_version.strip_patch)
  end

  def self.version_symbol
    version.to_sym
  end

  # The SDK helpers below are best-effort: they never raise just because the
  # host is not macOS or the developer tools are missing.  Callers get nil or
  # an empty fallback instead.
  def self.sdk(version = nil)
    requested = version.nil? ? nil : version.to_s
    return @sdk if requested.nil? && defined?(@sdk) && @sdk

    raw = ENV["HOMEBREW_SDK_PATH"].to_s.strip
    raw = ENV["HOMEBREW_SDKROOT"].to_s.strip if raw.empty?
    return nil if raw.empty? && !OS.mac?

    if raw.empty?
      if requested.nil?
        raw = run_local_tool(xcrun_executable, "--show-sdk-path").strip
      else
        raw = run_local_tool(
          xcrun_executable, "--sdk", "macosx#{requested}", "--show-sdk-path"
        ).strip
      end
    end
    if raw.empty?
      candidates = []
      [Xcode.bundle_path, Pathname.new(active_developer_dir.to_s)].uniq.each do |root|
        next if root.nil? || root.to_s.empty?

        sdk_root = root/"Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs"
        sdk_root = root/"Platforms/MacOSX.platform/Developer/SDKs" unless sdk_root.directory?
        next unless sdk_root.directory?

        candidates.concat(
          sdk_root.children.select { |child| child.directory? && child.extname == ".sdk" }
        )
      end
      candidates.sort_by! { |child| child.basename.to_s }
      chosen = if requested.nil?
        candidates.last
      else
        candidates.find { |child| child.basename.to_s.include?(requested) }
      end
      raw = chosen.to_s unless chosen.nil?
    end
    return nil if raw.empty?

    version_text = ENV["HOMEBREW_SDK_VERSION"].to_s.strip
    if version_text.empty?
      version_text = SDK.read_sdk_version(Pathname.new(raw)).to_s rescue ""
    end
    if version_text.empty?
      version_text = run_local_tool(xcrun_executable, "--show-sdk-version").strip
    end
    version_text = requested if version_text.empty? && !requested.nil?
    version_text = MacOS.version.to_s if version_text.empty?
    found = PybrewSDK.new(PybrewVersion.new(version_text), Pathname.new(raw))
    @sdk = found if requested.nil?
    found
  rescue StandardError
    nil
  end

  def self.sdk_path(version = nil)
    found = sdk(version)
    found.nil? ? nil : found.path
  end

  def self.latest_sdk_version
    @latest_sdk_version ||= begin
      text = if OS.mac?
        found = sdk
        found.nil? ? MacOS.version.to_s : found.version.to_s
      else
        "0"
      end
      PybrewVersion.new(text)
    rescue StandardError
      PybrewVersion.new("0")
    end
  end

  def self.preferred_perl_version
    if OS.mac? && version >= :sonoma
      "5.34"
    else
      "5.30"
    end
  end

  def self.languages
    @languages ||= begin
      values = []
      if OS.mac?
        output = `/usr/bin/defaults read -g AppleLanguages 2>/dev/null`.to_s
        if output.strip.empty?
          output = `/usr/bin/defaults read /Library/Preferences/.GlobalPreferences AppleLanguages 2>/dev/null`.to_s
        end
        values = output.scan(/[^ \n"(),]+/)
      end
      values
    rescue StandardError
      []
    end
  end

  def self.language
    languages.first
  end

  def self.active_developer_dir
    override = ENV["HOMEBREW_DEVELOPER_DIR"].to_s.strip
    return override unless override.empty?
    return @active_developer_dir if defined?(@active_developer_dir)

    raw = MacOS.run_local_tool(MacOS.xcode_select_executable, "-print-path").to_s.strip
    if raw.empty?
      fallback = "/Applications/Xcode.app/Contents/Developer"
      raw = fallback if File.directory?(fallback)
    end
    @active_developer_dir = raw
  rescue StandardError
    ""
  end

  def self.full_version=(value)
    @full_version = Version.new(value.to_s)
    @version = nil
    @version
  end

  def self.version=(value)
    @version = Version.new(value.to_s)
    @version
  end

  # Clear every memoized host-detection value (tests and long-lived shims).
  def self.reset_cache!
    @full_version = nil
    @version = nil
    @sdk = nil
    @latest_sdk_version = nil
    @languages = nil
    @active_developer_dir = nil
    nil
  end

  def self.mdfind(query)
    Utils.popen_read("mdfind", query.to_s).to_s
  end

  def self.mdfind_query(bundle_id)
    "kMDItemCFBundleIdentifier == '#{bundle_id}'"
  end

  def self.app_with_bundle_id(*bundle_ids)
    ids = bundle_ids.flatten.compact.map(&:to_s)
    return nil if ids.empty?

    query = ids.map { |bundle_id| mdfind_query(bundle_id) }.join(" || ")
    output = mdfind(query).to_s
    line = output.each_line.map(&:strip).reject(&:empty?).first
    line.nil? ? nil : Pathname.new(line)
  end

  def self.pkgutil_info(pkg)
    return "" unless File.exist?("/usr/sbin/pkgutil")

    Utils.popen_read("/usr/sbin/pkgutil", "--pkg-info", pkg.to_s).to_s
  end

  def self.sdk_locator
    self
  end

  def self.sdk_for_formula(_formula, version = nil)
    sdk(version)
  end

  def self.sdk_path_if_needed(version = nil)
    sdk_path(version)
  end

  module SDK
    def self.clt_sdk_prefix
      Pathname.new("/Library/Developer/CommandLineTools/SDKs")
    end

    def self.sdk_prefix
      override = ENV["HOMEBREW_SDK_PREFIX"].to_s.strip
      return Pathname.new(override) unless override.empty?

      xcode_sdks = Xcode.bundle_path/"Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs"
      return xcode_sdks if xcode_sdks.directory?

      clt_sdk_prefix
    end

    # Versioned SDK directories in ``sdk_prefix`` (bounded local directory
    # scan; no commands and no network).
    def self.sdk_directories
      primary = sdk_prefix
      roots = if !ENV["HOMEBREW_SDK_PREFIX"].to_s.strip.empty?
        [primary]
      elsif primary.directory? && primary != clt_sdk_prefix
        [primary]
      else
        [primary, clt_sdk_prefix].uniq
      end
      roots = roots.select(&:directory?)
      roots.flat_map do |prefix|
        prefix.children.select do |child|
          child.directory? && child.basename.to_s.start_with?("MacOSX") &&
            child.extname == ".sdk"
        end
      end.sort_by { |child| child.basename.to_s }
    rescue StandardError
      []
    end

    def self.read_sdk_version(path = nil)
      return MacOS.version if path.nil?

      pathname = Pathname.new(path.to_s)
      settings = pathname/"SDKSettings.json"
      if settings.file?
        begin
          data = JSON.parse(settings.read)
          version = data["Version"] || data["CanonicalName"] || data["ProductBuildVersion"]
          return PybrewVersion.new(version.to_s) unless version.nil? || version.to_s.empty?
        rescue StandardError
          nil
        end
      end
      match = pathname.basename(".sdk").to_s[/MacOSX(\d+(?:\.\d+)*)/, 1]
      match.nil? ? nil : PybrewVersion.new(match)
    end

    def self.all_sdks
      found = sdk_directories.map do |path|
        PybrewSDK.new(read_sdk_version(path) || MacOS.version, path)
      end
      found = [MacOS.sdk].compact if found.empty?
      found.sort_by { |sdk| sdk.version.to_s.split(".").map(&:to_i) }
    rescue StandardError
      []
    end

    def self.latest_sdk
      all_sdks.last
    end

    def self.sdk_for(version = nil)
      if version.nil?
        found = MacOS.sdk
        return found.nil? ? latest_sdk : found
      end
      wanted = if version.is_a?(Symbol)
        MacOS::RELEASES[version]
      else
        version.to_s
      end
      return latest_sdk if wanted.nil? || wanted.empty?

      all_sdks.find do |candidate|
        candidate.version.to_s == wanted ||
          candidate.version.to_s.start_with?("#{wanted}.")
      end || latest_sdk
    end

    def self.sdk_path(version = nil)
      found = sdk_for(version)
      found.nil? ? nil : found.path
    end

    def self.sdk_if_applicable(version = nil)
      sdk_for(version)
    end
  end

  module CLT
    def self.developer_dir
      override = ENV["HOMEBREW_DEVELOPER_DIR"].to_s.strip
      return Pathname.new(override) unless override.empty?

      path = MacOS.run_local_tool(MacOS.xcode_select_executable, "-print-path").to_s.strip
      path.empty? ? nil : Pathname.new(path)
    rescue StandardError
      nil
    end

    def self.local_install_dir
      dir = Pathname.new("/Library/Developer/CommandLineTools")
      dir.directory? ? dir : nil
    end

    def self.installed?
      found = developer_dir
      return true if !found.nil? && found.directory?
      return true if !local_install_dir.nil?

      false
    rescue StandardError
      false
    end

    def self.version
      override = ENV["HOMEBREW_CLT_VERSION"].to_s.strip
      return PybrewVersion.new(override) unless override.empty?

      raw = MacOS.run_local_tool(
        MacOS.pkgutil_executable, "--pkg-info=com.apple.pkg.CLTools_Executables"
      ).to_s
      match = raw[/^version:\s*(\S+)/, 1]
      return PybrewVersion.new(match) unless match.nil? || match.empty?

      CLT.clang_version
    rescue StandardError
      nil
    end

    def self.sdk_path
      found = MacOS::SDK.sdk_for
      return found.path unless found.nil?

      MacOS.sdk_path
    rescue StandardError
      MacOS.sdk_path
    end

    def self.clang_version
      clang = local_install_dir
      return nil if clang.nil?

      output = MacOS.run_local_tool(clang/"usr/bin/clang", "--version").to_s
      version = output[/clang version\s+(\S+)/, 1]
      version.nil? ? nil : PybrewVersion.new(version)
    rescue StandardError
      nil
    end
  end

  module Xcode
    def self.installed?
      return false unless OS.mac?

      developer_dir = MacOS.active_developer_dir.to_s
      !developer_dir.empty? && File.directory?(developer_dir)
    rescue StandardError
      false
    end

    def self.version
      override = ENV["HOMEBREW_XCODE_VERSION"].to_s.strip
      return PybrewVersion.new(override) unless override.empty?
      return nil unless installed?

      # Prefer the local version.plist (bounded, no subprocess) before
      # shelling out to xcodebuild.
      version_plist = bundle_path/"Contents/version.plist"
      if version_plist.file?
        begin
          text = version_plist.read
          match = text[%r{<key>CFBundleShortVersionString</key>\s*<string>([^<]+)</string>}, 1]
          return PybrewVersion.new(match) if match && !match.empty?
        rescue StandardError
          nil
        end
      end
      output = `/usr/bin/xcodebuild -version 2>/dev/null`.to_s
      match = output[/Xcode\s+(\S+)/, 1]
      match.nil? || match.empty? ? nil : PybrewVersion.new(match)
    rescue StandardError
      nil
    end

    def self.prefix
      installed? ? Pathname.new(MacOS.active_developer_dir.to_s) : nil
    end

    def self.sdk_path
      MacOS.sdk_path
    end

    def self.default_developer_dir
      "/Applications/Xcode.app/Contents/Developer"
    end

    # Discover the Xcode bundle from the active developer dir (xcode-select)
    # or a bounded local /Applications scan instead of assuming one path.
    def self.bundle_path
      override = ENV["PYBREW_XCODE_APP"].to_s.strip
      return Pathname.new(override) unless override.empty?

      developer = MacOS.active_developer_dir.to_s
      match = developer.match(%r{\A(.+?\.app)/Contents/Developer\z})
      return Pathname.new(match[1]) unless match.nil?

      candidates = Dir.glob("/Applications/Xcode*.app").sort
      return Pathname.new(candidates.last) unless candidates.empty?

      Pathname.new("/Applications/Xcode.app")
    rescue StandardError
      Pathname.new("/Applications/Xcode.app")
    end

    def self.default_prefix?
      !prefix.nil? && prefix.to_s == default_developer_dir
    end

    def self.toolchain_path
      found = prefix
      found.nil? ? nil : found/"Toolchains/XcodeDefault.xctoolchain"
    end

    def self.minimum_version
      PybrewVersion.new("1.0")
    end

    def self.below_minimum_version?
      false
    end

    def self.latest_version
      version
    end

    def self.latest_clang_version
      DevelopmentTools.clang_version
    end

    def self.latest_sdk_version?
      false
    end

    def self.needs_clt_installed?
      !CLT.installed?
    end

    def self.without_clt?
      false
    end

    def self.detect_version
      version
    end

    def self.detect_clang_version
      DevelopmentTools.clang_version
    end

    def self.update_instructions
      "Update Xcode from the App Store."
    end

    def self.reinstall_instructions
      "Reinstall Xcode from the App Store."
    end

    def self.installation_then_reinstall_instructions
      "#{update_instructions} #{reinstall_instructions}"
    end
  end
end

module DevelopmentTools
  module_function

  def clang
    Utils.which("clang")
  end

  def clang_version
    output = Utils.popen_read("clang", "--version", safe: true) rescue ""
    output[/clang version\s+(\S+)/, 1]
  end

  def clang_build_version
    output = Utils.popen_read("clang", "--version", safe: true) rescue ""
    output[%r{clang(?:-| version [^ ]+ \(tags/RELEASE_)(\d{2,})}, 1]
  end

  def default_compiler
    :clang
  end

  def preferred_compiler
    default_compiler
  end

  def llvm_clang
    Utils.which("llvm-clang") || clang
  end

  def clang_build
    clang_build_version
  end

  def gcc_version(cc = "gcc")
    output = Utils.popen_read(cc.to_s, "--version", safe: true) rescue ""
    output[/gcc(?: \([^)]*\))?\s+(\d+(?:\.\d+)+)/, 1]
  end

  def compiler_version(compiler = default_compiler)
    case compiler.to_s
    when "clang"
      clang_version
    when "gcc"
      gcc_version
    else
      nil
    end
  end

  def installed?
    !clang.nil?
  end

  def needs_clt?
    OS.mac? && !installed?
  end

  def xcode_installed?
    OS.mac? && File.directory?(MacOS.active_developer_dir.to_s)
  end

  def sdk_path
    MacOS.sdk_path
  end

  def locate
    clang
  end

  def clear_version_cache
    @clang_version_output = nil
    nil
  end

  def host_gcc_path
    Pathname.new("/usr/bin/gcc")
  end

  def clang_version_output
    Utils.popen_read("clang", "--version", safe: true) rescue nil
  end

  def ld64_version
    nil
  end

  def llvm_clang_version
    nil
  end

  def llvm_clang_build_version
    llvm_clang_version
  end

  def needs_libc_formula?
    false
  end

  def needs_compiler_formula?
    false
  end

  def needs_build_formulae?
    needs_libc_formula? || needs_compiler_formula?
  end

  def curl_handles_most_https_certificates?
    true
  end

  def ca_file_substitution_required?
    false
  end

  def curl_substitution_required?
    false
  end

  def installation_instructions
    "Install Clang or run `brew install gcc`."
  end

  def custom_installation_instructions
    installation_instructions
  end

  def insecure_download_warning(resource)
    "Using `--insecure` with curl to download #{resource} because we need it to run " \
      "`brew install ca-certificates` in order to download securely from now on. " \
      "Checksums will still be verified."
  end

  def build_system_info
    {
      "os" => RUBY_PLATFORM.include?("darwin") ? "MacOS" : "Linux",
      "os_version" => (defined?(OS_VERSION) ? OS_VERSION : RUBY_PLATFORM).to_s,
      "cpu_family" => (defined?(Hardware::CPU) ? Hardware::CPU.family.to_s : RUBY_PLATFORM)
    }
  end
end

# Homebrew's compiler constants are referenced by compiler-selection code and
# by a handful of formulae.  Keep the same names/values as upstream.
module CompilerConstants
  GNU_GCC_VERSIONS = %w[8 9 10 11 12 13 14 15 16].freeze
  GNU_GCC_REGEXP = /^gcc-(#{GNU_GCC_VERSIONS.join("|")})$/
  COMPILER_SYMBOL_MAP = {
    "gcc" => :gcc,
    "clang" => :clang,
    "llvm_clang" => :llvm_clang,
  }.freeze
  COMPILERS = (COMPILER_SYMBOL_MAP.values +
               GNU_GCC_VERSIONS.map { |version| "gcc-#{version}" }).freeze
end

# A single ``fails_with`` declaration: ``fails_with :clang do ... end`` or
# ``fails_with gcc: "7" do ... end``.  The shim only records and compares
# versions; it never actually builds with the compiler.
class CompilerFailure
  attr_reader :type

  def self.create(spec, &block)
    if spec.is_a?(Hash)
      compiler, major_version = spec.first
      compiler = compiler.to_s.to_sym
      raise ArgumentError, "The `fails_with` hash syntax only supports GCC" if compiler != :gcc

      type = compiler
      version = "#{major_version}.999"
      exact_major_match = true
    else
      type = spec.to_s.to_sym
      version = 9999
      exact_major_match = false
    end
    new(type, version, exact_major_match: exact_major_match, &block)
  end

  def self.version_segments(value)
    value.to_s.scan(/\d+/).map(&:to_i)
  end

  def self.compare_versions(left, right)
    left_parts = version_segments(left)
    right_parts = version_segments(right)
    length = [left_parts.length, right_parts.length].max
    index = 0
    while index < length
      comparison = (left_parts[index] || 0) <=> (right_parts[index] || 0)
      return comparison unless comparison.zero?

      index += 1
    end
    0
  end

  def self.major_version(value)
    version_segments(value).first || 0
  end

  def initialize(type, version, exact_major_match: false, &block)
    @type = type.to_s.to_sym
    @version = PybrewVersion.new(version.to_s)
    @exact_major_match = exact_major_match
    instance_eval(&block) if block
  end

  def version(value = nil)
    @version = PybrewVersion.new(value.to_s) unless value.nil?
    @version
  end
  alias build version

  def cause(_message)
    nil
  end

  def fails_with?(compiler)
    return false unless type == compiler.type

    compiler_version = compiler.version.to_s
    if @exact_major_match
      self.class.major_version(@version) == self.class.major_version(compiler_version) &&
        self.class.compare_versions(@version, compiler_version) >= 0
    elsif type == :gcc
      self.class.compare_versions(
        self.class.major_version(@version),
        self.class.major_version(compiler_version)
      ) >= 0
    else
      self.class.compare_versions(@version, compiler_version) >= 0
    end
  end

  def inspect
    "#<CompilerFailure: #{type} #{version}>"
  end
end

class CompilerSelectionError < RuntimeError
  def initialize(formula)
    name = formula.respond_to?(:full_name) ? formula.full_name : formula.to_s
    super("#{name} cannot be built with any available compilers.")
  end
end

# Compatibility compiler selector.  It honours ``compiler_failures`` metadata
# and the host's default compiler, but does not probe/build with compilers.
class CompilerSelector
  include CompilerConstants

  Compiler = Struct.new(:type, :name, :version)

  COMPILER_PRIORITY = {
    clang: [:clang, :llvm_clang, :gnu],
    gcc: [:gnu, :gcc, :llvm_clang, :clang],
  }.freeze

  def self.select_for(formula, compilers = nil, testing_formula: false)
    failures = if formula.respond_to?(:compiler_failures)
      Array(formula.compiler_failures)
    else
      []
    end

    Array(compilers || self.compilers).each do |candidate|
      build_compilers(candidate).each do |compiler|
        return compiler.name unless failures.any? { |failure| failure.fails_with?(compiler) }
      end
    end

    raise CompilerSelectionError, formula
  end

  def self.compilers
    COMPILER_PRIORITY.fetch(DevelopmentTools.default_compiler)
  end

  def self.preferred_gcc
    installed = CompilerConstants::GNU_GCC_VERSIONS.select { |version| !Utils.which("gcc-#{version}").nil? }
    installed.empty? ? "gcc" : "gcc-#{installed.max}"
  end

  def self.build_compilers(candidate)
    case candidate.to_sym
    when :gnu
      installed = GNU_GCC_VERSIONS.select { |gcc_version| !Utils.which("gcc-#{gcc_version}").nil? }
      installed = [preferred_gcc.sub(/^gcc-/, "")] if installed.empty? && !Utils.which("gcc").nil?
      installed.reverse.map do |gcc_version|
        Compiler.new(:gcc, "gcc-#{gcc_version}", DevelopmentTools.gcc_version("gcc-#{gcc_version}"))
      end
    when :llvm
      []
    when :gcc
      [Compiler.new(:gcc, preferred_gcc, compiler_version(:gcc))]
    when :llvm_clang
      [Compiler.new(:llvm_clang, :llvm_clang, compiler_version(:clang))]
    else
      [Compiler.new(candidate.to_sym, candidate.to_sym, compiler_version(candidate.to_sym))]
    end
  end
  private_class_method :build_compilers

  def self.compiler_version(type)
    raw = case type.to_s
          when "gcc", CompilerConstants::GNU_GCC_REGEXP
            DevelopmentTools.gcc_version
          when "llvm_clang"
            DevelopmentTools.clang_version
          else
            DevelopmentTools.compiler_version(type)
          end
    raw = DevelopmentTools.clang_version if raw.nil? || raw.to_s.empty?
    PybrewVersion.new(raw.nil? || raw.to_s.empty? ? "0" : raw.to_s)
  end
  private_class_method :compiler_version
end

module Linux
  def self.os_version
    @os_version ||= begin
      output = `/usr/bin/lsb_release -a 2>/dev/null`.to_s
      description = output[/^Description:\s*(.*)$/, 1]
      codename = output[/^Codename:\s*(.*)$/, 1]
      if !description.nil? && !description.strip.empty?
        codename = codename.to_s.strip
        if codename.empty? || codename == "n/a"
          description.strip
        else
          "#{description.strip} (#{codename})"
        end
      else
        OS_VERSION
      end
    rescue StandardError
      "Unknown"
    end
  end

  def self.languages
    []
  end

  def self.language
    languages.first
  end

  def self.wsl?
    !!(OS.kernel_release =~ /microsoft/i)
  end

  def self.inside_docker?
    File.file?("/.dockerenv") || File.file?("/run/.containerenv")
  end
end

module OS
  Mac = ::MacOS
  Linux = ::Linux
end

OS_VERSION = begin
  override = ENV["HOMEBREW_OS_VERSION"].to_s.strip
  if !override.empty?
    override
  elsif OS.mac?
    "macOS #{MacOS.full_version}"
  elsif OS.linux?
    "Linux #{OS.kernel_release}"
  else
    RUBY_PLATFORM
  end
end

module PybrewSystem
  module_function

  # Normalise the flexible call styles used by Homebrew formulae:
  # ``system(env, cmd, *args)``, ``system(cmd, env: ..., chdir: ...)`` and the
  # positional options hash accepted by ``Kernel.system``.
  def normalize(args, options)
    arguments = args.dup
    environment = {}
    environment = arguments.shift if arguments.first.is_a?(Hash)
    options = options.dup
    trailing = arguments.last.is_a?(Hash) ? arguments.pop : nil
    options = trailing.merge(options) if trailing
    env_option = options.delete(:env)
    environment = environment.merge(env_option) if env_option.is_a?(Hash)
    normalized_env = {}
    environment.each do |key, value|
      normalized_env[key.to_s] = value.nil? ? nil : value.to_s
    end
    chdir = options.delete(:chdir)
    options[:chdir] = chdir.to_s unless chdir.nil?
    command = arguments.flatten.compact.map(&:to_s)
    raise ArgumentError, "system: no command given" if command.empty?

    [normalized_env, command, options]
  end

  def run!(args, options = {})
    environment, command, options = normalize(args, options)
    success = Kernel.system(environment, *command, **options)
    raise "Failed executing: #{command.join(' ')}" unless success

    true
  end

  def quiet(args, options = {})
    environment, command, options = normalize(args, options)
    options = { out: File::NULL, err: File::NULL }.merge(options)
    Kernel.system(environment, *command, **options) ? true : false
  rescue RuntimeError, ArgumentError
    false
  end
end

module Utils
  module_function

  def safe_system(*args, **options)
    PybrewSystem.run!(args, options)
  end

  def quiet_system(*args, **options)
    PybrewSystem.quiet(args, options)
  end

  # -- small ActiveSupport-style inflections used by formula code ----------

  def deconstantize(path)
    path.to_s.rpartition("::").first
  end

  def demodulize(path)
    raise ArgumentError, "No constant path provided" if path.nil?

    path.to_s.rpartition("::").last
  end

  def underscore(camel_cased_word)
    return camel_cased_word.to_s unless /[A-Z-]|::/.match?(camel_cased_word.to_s)

    word = camel_cased_word.to_s.gsub("::", "/")
    word.gsub!(/[A-Z](?=[A-Z][a-z])|[a-z\d](?=[A-Z])/, '\0_')
    word.tr!("-", "_")
    word.downcase!
    word
  end

  def pluralize(stem, count, plural: "s", singular: "", include_count: false)
    case stem
    when "formula"
      plural = "e"
    when "dependency", "try"
      stem = stem.delete_suffix("y")
      plural = "ies"
      singular = "y"
    end

    prefix = include_count ? "#{count} " : ""
    suffix = (count == 1) ? singular : plural
    "#{prefix}#{stem}#{suffix}"
  end

  UNSAFE_FILENAME = /[[:cntrl:]\/\\]/.freeze

  def safe_filename?(basename)
    !UNSAFE_FILENAME.match?(basename.to_s)
  end

  def safe_filename(basename)
    basename.to_s.gsub(UNSAFE_FILENAME, "")
  end

  # -- deep collection helpers ---------------------------------------------

  def deep_dup(object)
    case object
    when Hash
      object.each_with_object({}) do |(key, value), result|
        result[deep_dup(key)] = deep_dup(value)
      end
    when Array
      object.map { |value| deep_dup(value) }
    else
      begin
        object.dup
      rescue TypeError
        object
      end
    end
  end

  def deep_merge(left, right, &block)
    result = left.dup
    right.each do |key, value|
      if result.key?(key) && result[key].is_a?(Hash) && value.is_a?(Hash)
        result[key] = deep_merge(result[key], value, &block)
      elsif result.key?(key) && block
        result[key] = block.call(key, result[key], value)
      else
        result[key] = value
      end
    end
    result
  end

  def deep_merge!(left, right, &block)
    left.replace(deep_merge(left, right, &block))
    left
  end

  # -- subprocess helpers ---------------------------------------------------

  def which(command, path = nil)
    command = command.to_s
    search_path = (path.nil? ? ENV.fetch("PATH", "") : path).to_s
    search_path.split(File::PATH_SEPARATOR).each do |directory|
      candidate = File.join(directory, command)
      return Pathname.new(candidate) if File.file?(candidate) && File.executable?(candidate)
    end
    nil
  end

  def popen(args, mode = "r", options = {}, &block)
    arguments = args.is_a?(Array) ? args.dup : [args]
    environment = arguments.first.is_a?(Hash) ? arguments.shift : {}
    command = arguments.flatten.compact.map { |argument| argument.to_s }
    raise ArgumentError, "popen: no command given" if command.empty?

    options = options.dup
    normalized_env = {}
    environment.each do |key, value|
      normalized_env[key.to_s] = value.nil? ? nil : value.to_s
    end
    IO.popen([normalized_env, *command], mode, options) do |pipe|
      block.nil? ? pipe.read : block.call(pipe)
    end
  end

  def popen_read(*args, safe: false, **options, &block)
    options = options.dup
    options[:in] ||= File::NULL
    output = popen(args, "rb", options, &block)
    status = $?
    if safe && !(status && status.success?)
      raise "Failed executing: #{args.flatten.compact.map(&:to_s).join(' ')}"
    end

    output
  end

  def popen_read_text(*args, **options)
    output = popen_read(*args, **options)
    raise TypeError, "Expected command output to be a String" unless output.is_a?(String)

    output.force_encoding(Encoding.default_external)
  end

  def safe_popen_read(*args, **options, &block)
    popen_read(*args, safe: true, **options, &block)
  end

  def popen_write(*args, safe: false, **options)
    output = ""
    popen(args, "w+b", options) do |pipe|
      yield pipe
      pipe.close_write
      output = pipe.read
    end
    status = $?
    if safe && !(status && status.success?)
      raise "Failed executing: #{args.flatten.compact.map(&:to_s).join(' ')}"
    end

    output
  end

  def safe_popen_write(*args, **options, &block)
    popen_write(*args, safe: true, **options, &block)
  end

  def pipe_output(command, input = nil, result = nil)
    output = IO.popen(command.to_s, "w+") do |pipe|
      pipe.write(input.to_s) unless input.nil?
      pipe.close_write
      pipe.read
    end
    unless result.nil?
      status = $?
      actual = status.nil? ? nil : status.exitstatus
      raise "pipe_output: expected exit status #{result}, got #{actual.inspect}" unless actual == result
    end
    output
  end

  def name_from_full_name(full_name)
    parts = full_name.to_s.split("/", 3)
    parts.length == 3 ? parts[2] : full_name.to_s
  end

  def tap_from_full_name(full_name)
    parts = full_name.to_s.split("/", 3)
    parts.length == 3 ? "#{parts[0]}/#{parts[1]}" : nil
  end

  def full_name?(full_name)
    full_name.to_s.count("/") == 2
  end

  def name_or_token(formula_or_cask)
    formula_or_cask.respond_to?(:token) ? formula_or_cask.token : formula_or_cask.name
  end

  def fully_qualified_name(formula_or_cask)
    tap = formula_or_cask.respond_to?(:tap) ? formula_or_cask.tap : nil
    if tap && ["homebrew/core", "homebrew/cask"].include?(tap.to_s)
      "#{formula_or_cask.full_name}"
    else
      formula_or_cask.full_name
    end
  end

  def convert_to_string_or_symbol(string)
    text = string.to_s
    text.start_with?(":") ? text[1..].to_sym : text
  end

  def deep_stringify_symbols(obj)
    case obj
    when String
      obj.start_with?(":", "\\") ? "\\#{obj}" : obj
    when Symbol
      ":#{obj}"
    when Hash
      obj.each_with_object({}) { |(key, value), result| result[deep_stringify_symbols(key)] = deep_stringify_symbols(value) }
    when Array
      obj.map { |value| deep_stringify_symbols(value) }
    else
      obj
    end
  end

  def deep_unstringify_symbols(obj)
    case obj
    when String
      if obj.start_with?("\\")
        obj[1..]
      elsif obj.start_with?(":")
        obj[1..].to_sym
      else
        obj
      end
    when Hash
      obj.each_with_object({}) { |(key, value), result| result[deep_unstringify_symbols(key)] = deep_unstringify_symbols(value) }
    when Array
      obj.map { |value| deep_unstringify_symbols(value) }
    else
      obj
    end
  end

  def deep_compact_blank(obj, compact_zero: true, compact_false: true)
    compacted = case obj
                when Hash
                  obj.each_with_object({}) do |(key, value), result|
                    item = deep_compact_blank(value, compact_zero: compact_zero, compact_false: compact_false)
                    result[key] = item unless item.nil?
                  end
                when Array
                  obj.each_with_object([]) do |value, result|
                    item = deep_compact_blank(value, compact_zero: compact_zero, compact_false: compact_false)
                    result << item unless item.nil?
                  end
                else
                  obj
                end
    return nil if compacted.nil?
    return nil if compact_false && compacted == false
    return nil if compacted.respond_to?(:empty?) && compacted.empty?
    return nil if compact_zero && compacted.is_a?(Numeric) && compacted.zero?

    compacted
  end

  def exponential_backoff_sleep(try, base: 2)
    wait = base**try
    yield wait if block_given?
    sleep wait
  end

  def parse_author!(author)
    match = /^(?<name>[^<]+?)[ \t]*<(?<email>[^>]+?)>$/.match(author.to_s)
    raise "Unable to parse name and email." if match.nil?

    { name: match[:name], email: match[:email] }
  end

  def parallel_map(items, &block)
    threads = items.map do |item|
      Thread.new do
        Thread.current.report_on_exception = false
        block.call(item)
      end
    end
    threads.map(&:value)
  end

  def git(*args)
    popen_read("git", *args)
  end

  def curl(*args)
    popen_read("curl", *args)
  end

  def gzip(*args)
    safe_system("gzip", *args)
  end
end

module Utils
  module SPDX
    def self.valid_license?(_license)
      true
    end

    def self.valid_license_exception?(_exception)
      false
    end

    def self.deprecated_license?(_license)
      false
    end

    def self.licenses_forbid_installation?(_license)
      false
    end

    def self.forbidden_licenses_include?(_license)
      false
    end

    def self.parse_license_expression(expression)
      expression.to_s
    end

    def self.string_to_license_expression(expression)
      expression.to_s
    end

    def self.license_expression_to_string(expression)
      expression.to_s
    end

    def self.truncate_license(expression, _length = 80)
      expression.to_s
    end

    def self.license_data(_license = nil)
      {}
    end

    def self.exception_data(_exception = nil)
      {}
    end

    def self.license_index
      []
    end

    def self.exception_index
      []
    end

    def self.latest_tag
      nil
    end

    def self.license_version_info(_version = nil)
      nil
    end

    def self.download_latest_license_data!(*_args)
      nil
    end
  end
end

module Utils
  module Path
    def self.rmdir_if_possible(path)
      path = Pathname.new(path.to_s)
      path.rmdir if path.directory? && path.children.empty?
      nil
    rescue StandardError
      nil
    end

    def self.resolved_path(path)
      Pathname.new(path.to_s).realpath
    rescue StandardError
      Pathname.new(path.to_s)
    end

    def self.resolved_path_exists?(path)
      resolved_path(path).exist?
    end

    def self.formula_installed_prefixes(formula_names)
      Array(formula_names).flat_map do |name|
        rack = Pathname.new(HOMEBREW_CELLAR)/name.to_s
        next [] unless rack.directory?

        rack.children.select { |child| child.directory? }
      end.sort_by { |path| path.basename.to_s }
    end

    def self.formula_opt_bin_path(formula_name, *paths)
      base = HOMEBREW_PREFIX/"opt"/formula_name.to_s/"bin"
      paths.empty? ? base : base/File.join(*paths.map(&:to_s))
    end

    def self.formula_opt_bin_env(formula_name, *paths)
      "#{formula_opt_bin_path(formula_name, *paths)}#{File::PATH_SEPARATOR}#{ENV.fetch("PATH", "")}"
    end
  end
end

module Utils
  module Git
    def self.git(*args)
      Utils.git(*args)
    end

    def self.available?
      !Utils.which("git").nil?
    end

    def self.path
      Utils.which("git")
    end

    def self.version
      output = Utils.popen_read("git", "--version").to_s
      output[/git version (\S+)/, 1]
    rescue StandardError
      nil
    end

    def self.head(path = ".")
      Utils.popen_read("git", "-C", path.to_s, "rev-parse", "HEAD").to_s.strip
    rescue StandardError
      nil
    end

    def self.last_commit(path = ".")
      Utils.popen_read("git", "-C", path.to_s, "log", "-1", "--format=%H").to_s.strip
    rescue StandardError
      nil
    end

    def self.origin_url(path = ".")
      Utils.popen_read("git", "-C", path.to_s, "remote", "get-url", "origin").to_s.strip
    rescue StandardError
      nil
    end

    def self.clear_available_cache
      nil
    end

    def self.file_at_commit(path, commit)
      Utils.popen_read("git", "show", "#{commit}:#{path}").to_s
    rescue StandardError
      nil
    end

    def self.last_revision_commit_of_file(repo, file, before: nil)
      args = ["git", "-C", repo.to_s, "log", "-1", "--format=%H"]
      args << "#{before}" unless before.nil?
      args << "--" << file.to_s
      Utils.popen_read(*args).to_s.strip
    rescue StandardError
      nil
    end

    def self.last_revision_of_file(repo, file, before: nil)
      args = ["git", "-C", repo.to_s, "log", "-1", "--format=%ct"]
      args << "#{before}" unless before.nil?
      args << "--" << file.to_s
      Utils.popen_read(*args).to_s.strip
    rescue StandardError
      nil
    end

    def self.last_revision_commit_of_files(repo, files, before: nil)
      Array(files).filter_map do |file|
        last_revision_commit_of_file(repo, file, before: before)
      end
    end

    def self.get_tar_commit_id(path = ".")
      Utils.popen_read("git", "-C", path.to_s, "rev-parse", "HEAD").to_s.strip
    rescue StandardError
      nil
    end

    def self.cherry_pick!(repo, commit)
      Utils.popen_read("git", "-C", repo.to_s, "cherry-pick", commit.to_s).to_s
    rescue StandardError
      nil
    end

    def self.set_name_email!(repo, name, email)
      Utils.popen_read("git", "-C", repo.to_s, "config", "user.name", name.to_s)
      Utils.popen_read("git", "-C", repo.to_s, "config", "user.email", email.to_s)
      nil
    end

    def self.setup_gpg!(*_args)
      nil
    end

    def self.no_global_config_env
      {}
    end

    def self.no_global_config_file
      nil
    end

    def self.supports_partial_clone_sparse_checkout?
      true
    end
  end

  module Curl
    def self.curl(*args)
      Utils.curl(*args)
    end

    def self.curl_executable
      Utils.which("curl")
    end

    def self.curl_path
      curl_executable
    end

    def self.version
      output = Utils.popen_read("curl", "--version").to_s
      output[/^curl ([\d.]+)/, 1]
    rescue StandardError
      nil
    end
  end

  module Gzip
    def self.gzip(*args)
      Utils.gzip(*args)
    end
  end

  module Tar
    def self.tar(*args)
      Utils.safe_system("tar", *args)
    end
  end

  module Zip
    def self.zip(*args)
      Utils.safe_system("zip", *args)
    end
  end

  module Cp
    def self.cp(source, destination, **options)
      FileUtils.cp_r(source.to_s, destination.to_s, **options)
    end
  end

  module Popen
    def self.popen_read(*args, **options)
      Utils.popen_read(*args, **options)
    end

    def self.pipe_output(*args)
      Utils.pipe_output(*args)
    end
  end
end

class Object
  def deep_dup
    Utils.deep_dup(self)
  end
end

class Hash
  def deep_dup
    Utils.deep_dup(self)
  end

  def deep_merge(other_hash, &block)
    Utils.deep_merge(self, other_hash, &block)
  end

  def deep_merge!(other_hash, &block)
    Utils.deep_merge!(self, other_hash, &block)
  end
end

class Array
  def deep_dup
    Utils.deep_dup(self)
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

module PybrewBinary
  module_function

  # Return :executable, :dylib, :bundle or nil for a Mach-O / ELF file.  Any
  # unreadable or unrecognised file simply reports nil.
  def kind(path)
    data = read_header(path)
    return nil if data.nil? || data.bytesize < 4

    magic = data[0, 4]
    return elf_kind(data) if magic == "\x7FELF".b

    mach_o_kind(data, magic)
  rescue StandardError
    nil
  end

  def read_header(path)
    return nil unless File.file?(path.to_s)

    File.open(path.to_s, "rb") { |file| file.read(64) }
  rescue StandardError
    nil
  end

  def elf_kind(data)
    return nil if data.bytesize < 18

    little_endian = data.getbyte(5) == 1
    type = little_endian ? data[16, 2].unpack("v").first : data[16, 2].unpack("n").first
    case type
    when 2 then :executable
    when 3 then :dylib
    end
  end

  def mach_o_kind(data, magic)
    case magic
    when "\xFE\xED\xFA\xCE".b then little_endian = false
    when "\xCE\xFA\xED\xFE".b then little_endian = true
    when "\xFE\xED\xFA\xCF".b then little_endian = false
    when "\xCF\xFA\xED\xFE".b then little_endian = true
    when "\xCA\xFE\xBA\xBE".b, "\xBE\xBA\xFE\xCA".b then return :executable
    else return nil
    end
    return nil if data.bytesize < 16

    filetype = little_endian ? data[12, 4].unpack("V").first : data[12, 4].unpack("N").first
    case filetype
    when 2 then :executable
    when 6 then :dylib
    when 8 then :bundle
    end
  end
end

module PybrewInfo
  module_function

  def executable
    return @executable if defined?(@executable)

    @executable = begin
      found = nil
      ["/usr/bin/install-info", "/usr/local/bin/install-info", "/opt/homebrew/bin/install-info"].each do |candidate|
        if File.file?(candidate) && File.executable?(candidate)
          found = candidate
          break
        end
      end
      if found.nil?
        ENV.fetch("PATH", "").split(File::PATH_SEPARATOR).each do |directory|
          candidate = File.join(directory, "install-info")
          if File.file?(candidate) && File.executable?(candidate)
            found = candidate
            break
          end
        end
      end
      found
    end
  end

  def install_info(path)
    tool = executable
    if tool.nil?
      $stderr.puts "Warning: install-info not found; skipping info entry for #{path}"
      return nil
    end
    Kernel.system(tool, "--quiet", path.to_s, (path.dirname + "dir").to_s)
    nil
  rescue StandardError
    nil
  end

  def uninstall_info(path)
    tool = executable
    if tool.nil?
      $stderr.puts "Warning: install-info not found; skipping info entry removal for #{path}"
      return nil
    end
    Kernel.system(tool, "--delete", "--quiet", path.to_s, (path.dirname + "dir").to_s)
    nil
  rescue StandardError
    nil
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

  def atomic_write(content)
    FileUtils.mkdir_p(dirname.to_s)
    temporary = dirname + ".#{basename}.#{Process.pid}.#{rand(1_000_000)}.tmp"
    begin
      File.open(temporary.to_s, "wb") { |file| file.write(content.to_s) }
      File.rename(temporary.to_s, to_s)
    ensure
      FileUtils.rm_f(temporary.to_s) if temporary.exist?
    end
    nil
  end

  def append_lines(content, **open_args)
    raise "Cannot append file that doesn't exist: #{self}" unless exist?

    File.open(to_s, "a", **open_args) { |file| file.puts(content) }
    nil
  end

  def sha256
    Digest::SHA256.file(to_s).hexdigest
  end

  def verify_checksum(expected)
    expected_text = expected.to_s.strip
    if expected_text.empty?
      raise ArgumentError, "verify_checksum: no expected checksum given for #{self}"
    end
    return nil if expected_text == "no_check"

    expected_text = expected_text.sub(/\Asha256:/i, "").downcase
    actual = sha256.downcase
    unless actual == expected_text
      raise PybrewChecksumMismatch, "SHA256 mismatch for #{self}: expected #{expected_text}, got #{actual}"
    end
    nil
  end

  def rmdir_if_possible
    Dir.rmdir(to_s)
    true
  rescue Errno::ENOTEMPTY
    ds_store = self + ".DS_Store"
    if ds_store.exist? && children.one?
      ds_store.unlink
      retry
    end
    false
  rescue Errno::EEXIST, Errno::ENOENT, Errno::EACCES, Errno::EPERM, Errno::EBUSY, Errno::ENOTDIR
    false
  end

  def subdirs
    children.select { |child| child.directory? }
  end

  def text_executable?
    return false unless file?

    /\A#!\s*\S+/n.match?(File.binread(to_s, 1024).to_s)
  rescue StandardError
    false
  end

  def binary_executable?
    PybrewBinary.kind(to_s) == :executable
  end

  def dylib?
    PybrewBinary.kind(to_s) == :dylib
  end

  def mach_o_bundle?
    PybrewBinary.kind(to_s) == :bundle
  end

  def make_relative_symlink(src)
    source = Pathname.new(File.expand_path(src.to_s))
    base = Pathname.new(File.expand_path(dirname.to_s))
    FileUtils.mkdir_p(base.to_s)
    File.symlink(source.relative_path_from(base).to_s, to_s)
    nil
  end

  def ensure_writable
    raise ArgumentError, "Pathname#ensure_writable requires a block" unless block_given?

    saved_perms = nil
    unless writable?
      saved_perms = stat.mode
      FileUtils.chmod("u+rw", to_s)
    end
    yield
  ensure
    chmod(saved_perms) if saved_perms
  end

  def install_info
    PybrewInfo.install_info(self)
    nil
  end

  def uninstall_info
    PybrewInfo.uninstall_info(self)
    nil
  end

  def write_exec_script(*targets)
    targets = targets.flatten
    if targets.empty?
      $stderr.puts "Warning: Tried to write exec scripts to #{self} for an empty list of targets"
      return nil
    end
    FileUtils.mkdir_p(to_s)
    targets.each do |target|
      target = Pathname.new(target.to_s)
      script = join(target.basename)
      script.write("#!/bin/bash\nexec \"#{target}\" \"$@\"\n")
      script.chmod(0o755)
    end
    nil
  end

  def write_env_script(target, args_or_env, env = nil)
    if env.nil?
      raise ArgumentError, "#{args_or_env.inspect} is not a Hash" unless args_or_env.is_a?(Hash)

      env = args_or_env
      args = nil
    elsif args_or_env.is_a?(Array)
      args = args_or_env.join(" ")
    else
      args = args_or_env
    end
    raise ArgumentError, "write_env_script: env must be a Hash" unless env.is_a?(Hash)

    exports = ""
    env.each { |key, value| exports << "#{key}=\"#{value}\" " }
    FileUtils.mkdir_p(dirname.to_s)
    write("#!/bin/bash\n#{exports}exec \"#{target}\" #{args} \"$@\"\n")
    chmod(0o555)
    nil
  end

  def install_p(src, new_basename = nil)
    source = Pathname.new(src.to_s)
    raise Errno::ENOENT, source.to_s if !source.symlink? && !source.exist?

    name = new_basename.nil? ? source.basename.to_s : new_basename.to_s
    destination = self + name
    FileUtils.mkdir_p(to_s)
    FileUtils.rm_rf(destination.to_s) if destination.exist? || destination.symlink?
    if source.symlink?
      File.symlink(source.readlink.to_s, destination.to_s)
      FileUtils.rm_f(source.to_s)
    else
      FileUtils.mv(source.to_s, destination.to_s)
    end
    nil
  end

  def install_symlink_p(src, new_basename = nil)
    FileUtils.mkdir_p(to_s)
    directory = realpath
    source = Pathname.new(src.to_s).expand_path(directory)
    source = source.dirname.realpath + source.basename if source.dirname.exist?
    name = new_basename.nil? ? source.basename.to_s : new_basename.to_s
    link = directory + name
    FileUtils.rm_rf(link.to_s) if link.exist? || link.symlink?
    File.symlink(source.relative_path_from(directory).to_s, link.to_s)
    nil
  end

  def resolved_path
    symlink? ? dirname + readlink.to_s : self
  end

  def resolved_path_exists?
    target = readlink
  rescue ArgumentError, Errno::EINVAL
    false
  else
    (dirname + target.to_s).exist?
  end

  def cd
    raise ArgumentError, "Pathname#cd requires a block" unless block_given?

    Dir.chdir(to_s) { yield self }
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

  def append(keys, value, separator = " ")
    value = value.to_s
    Array(keys).each do |key|
      current = self[key]
      self[key] = if current.nil? || current.empty?
        value
      else
        current + separator + value
      end
    end
    nil
  end

  def prepend(keys, value, separator = " ")
    value = value.to_s
    Array(keys).each do |key|
      current = self[key]
      self[key] = if current.nil? || current.empty?
        value
      else
        value + separator + current
      end
    end
    nil
  end

  def remove(keys, value)
    return nil if value.nil?

    Array(keys).each do |key|
      current = self[key]
      next if current.nil?

      updated = current.sub(value.to_s, "")
      if updated.empty?
        delete(key)
      else
        self[key] = updated
      end
    end
    nil
  end

  def superenv?
    true
  end

  def compiler
    return @pybrew_compiler.to_sym unless @pybrew_compiler.nil?

    configured = self["HOMEBREW_CC"].to_s
    return configured.to_sym unless configured.empty?

    DevelopmentTools.default_compiler
  end

  def cc
    self["CC"] || RbConfig::CONFIG["CC"] || "cc"
  end

  def cc=(value)
    self["CC"] = self["OBJC"] = value.to_s
  end

  def cxx
    self["CXX"] || RbConfig::CONFIG["CXX"] || "c++"
  end

  def cxx=(value)
    self["CXX"] = self["OBJCXX"] = value.to_s
  end

  # Compiler-selection helpers from Homebrew's Superenv: they only configure
  # the environment, no compiler is invoked.
  def clang
    apply_compiler(:clang)
  end

  def gcc
    apply_compiler(:gcc)
  end

  def llvm_clang
    apply_compiler(:llvm_clang)
  end

  def append_to_cccfg(value)
    self["HOMEBREW_CCCFG"] = self["HOMEBREW_CCCFG"].to_s + value.to_s
    nil
  end

  def remove_cc_etc
    keys = %w[CC CXX OBJC OBJCXX LD CPP CFLAGS CXXFLAGS OBJCFLAGS OBJCXXFLAGS LDFLAGS CPPFLAGS]
    keys.each_with_object({}) do |key, removed|
      removed[key] = delete(key)
    end
  end

  def effective_arch
    Hardware::CPU.arch
  end

  def build_bottle?
    false
  end

  def debug_symbols?
    false
  end

  def bottle_arch
    PybrewFormulaRef.arch.to_sym
  rescue StandardError
    :x86_64
  end

  def build_environment_state(*_args, **_options)
    self
  end

  def deps
    @pybrew_deps ||= []
  end

  def deps=(deps)
    @pybrew_deps = Array(deps)
  end

  def keg_only_deps
    @pybrew_keg_only_deps ||= []
  end

  def keg_only_deps=(deps)
    @pybrew_keg_only_deps = Array(deps)
  end

  def run_time_deps
    @pybrew_run_time_deps ||= []
  end

  def run_time_deps=(deps)
    @pybrew_run_time_deps = Array(deps)
  end

  def determine_cc
    cc
  end

  def determine_cxx
    cxx
  end

  def determine_cccfg
    self["HOMEBREW_CCCFG"].to_s
  end

  def determine_path
    self["PATH"].to_s.split(File::PATH_SEPARATOR)
  end

  def determine_make_jobs
    cores = if defined?(Hardware::CPU) && Hardware::CPU.respond_to?(:cores)
      Hardware::CPU.cores
    else
      1
    end
    [cores.to_i, 1].max
  end

  def determine_optflags
    self["CFLAGS"].to_s.split
  end

  def determine_isystem_paths
    []
  end

  def determine_library_paths
    []
  end

  def determine_include_paths
    []
  end

  def determine_cmake_prefix_path
    []
  end

  def determine_cmake_include_path
    []
  end

  def determine_cmake_library_path
    []
  end

  def determine_cmake_frameworks_path
    []
  end

  def determine_pkg_config_path
    []
  end

  def determine_pkg_config_libdir
    []
  end

  def determine_aclocal_path
    nil
  end

  def determine_dependencies
    []
  end

  def homebrew_extra_paths
    []
  end

  def homebrew_extra_isystem_paths
    []
  end

  def homebrew_extra_library_paths
    []
  end

  def homebrew_extra_cmake_include_paths
    []
  end

  def homebrew_extra_cmake_library_paths
    []
  end

  def homebrew_extra_cmake_frameworks_paths
    []
  end

  def homebrew_extra_pkg_config_paths
    []
  end

  def libcxx
    nil
  end

  def permit_arch_flags
    nil
  end

  def refurbish_args
    nil
  end

  def reset
    nil
  end

  def runtime_cpu_detection
    nil
  end

  def set_debug_symbols
    nil
  end

  def extended
    self
  end

  def determine_cc
    cc
  end

  def determine_cxx
    cxx
  end

  def homebrew_cc
    cc
  end

  def fcflags
    self["FCFLAGS"].to_s
  end

  def fortran
    self["FC"]
  end

  def gcc_version_formula
    nil
  end

  def append_to_rustflags(value)
    append("RUSTFLAGS", value)
  end

  def warn_about_non_apple_gcc(_compiler = nil)
    nil
  end

  def set_cpu_flags
    nil
  end

  def check_for_compiler_universal_support
    nil
  end

  def fetch_compiler
    nil
  end

  def apply_compiler(name)
    value = name.to_s
    case value
    when "clang", "llvm_clang"
      self["CC"] = self["OBJC"] = "clang"
      self["CXX"] = self["OBJCXX"] = "clang++"
    when "gcc", /\Agcc-\d+\z/
      self["CC"] = self["OBJC"] = value
      self["CXX"] = self["OBJCXX"] = value.sub(/\Agcc/, "g++")
    else
      self["CC"] = self["OBJC"] = value
      self["CXX"] = self["OBJCXX"] = value.sub(/clang\z/, "clang++").sub(/gcc\z/, "g++")
    end
    self["HOMEBREW_CC"] = value
    @pybrew_compiler = value
    nil
  end

  def cflags
    self["CFLAGS"]
  end

  def cxxflags
    self["CXXFLAGS"]
  end

  def cppflags
    self["CPPFLAGS"]
  end

  def ldflags
    self["LDFLAGS"]
  end

  def fc
    self["FC"]
  end

  def fflags
    self["FFLAGS"]
  end

  def make_jobs
    flags = self["MAKEFLAGS"].to_s
    match = flags[/-j\s*(\d+)/, 1]
    return match.to_i unless match.nil?

    begin
      require "etc"
      count = Etc.nprocessors
      count > 0 ? count : 1
    rescue StandardError
      1
    end
  end

  def append_to_cflags(*flags)
    append("CFLAGS", flags.join(" "))
    append("CXXFLAGS", flags.join(" "))
  end

  def prepend_to_cflags(*flags)
    prepend("CFLAGS", flags.join(" "))
    prepend("CXXFLAGS", flags.join(" "))
  end

  def append_to_ldflags(*flags)
    append("LDFLAGS", flags.join(" "))
  end

  def prepend_to_ldflags(*flags)
    prepend("LDFLAGS", flags.join(" "))
  end

  def O0
    set_optimization("-O0")
  end

  def O1
    set_optimization("-O1")
  end

  def O2
    set_optimization("-O2")
  end

  def O3
    set_optimization("-O3")
  end

  def deparallelize
    self["MAKEFLAGS"] = "-j1"
    self["MFLAGS"] = "-j1"
    nil
  end

  def cxx11
    append("CXXFLAGS", "-std=c++11")
  end

  def cxx14
    append("CXXFLAGS", "-std=c++14")
  end

  def cxx17
    append("CXXFLAGS", "-std=c++17")
  end

  def cxx20
    append("CXXFLAGS", "-std=c++20")
  end

  def Os
    set_optimization("-Os")
  end

  def Oz
    set_optimization("-Oz")
  end

  def Og
    set_optimization("-Og")
  end

  def remove_from_cflags(*flags)
    remove("CFLAGS", flags.join(" "))
    remove("CXXFLAGS", flags.join(" "))
  end

  def remove_from_ldflags(*flags)
    remove("LDFLAGS", flags.join(" "))
  end

  def enable_warnings
    append("CFLAGS", "-Wall")
    append("CXXFLAGS", "-Wall")
  end

  def libxml2
    xml2_config = find_tool("xml2-config")
    if xml2_config
      append("CPPFLAGS", `#{xml2_config} --cflags 2>/dev/null`.strip)
      append("LDFLAGS", `#{xml2_config} --libs 2>/dev/null`.strip)
    else
      append("CPPFLAGS", "-I#{HOMEBREW_PREFIX}/include/libxml2")
      append("LDFLAGS", "-L#{HOMEBREW_PREFIX}/lib")
    end
    nil
  end

  def perl
    find_tool("perl") || "perl"
  end

  def python
    find_tool("python3") || find_tool("python") || "python3"
  end

  def java_home
    value = self["JAVA_HOME"].to_s
    return value unless value.empty?

    value = `/usr/libexec/java_home 2>/dev/null`.to_s.strip
    value.empty? ? nil : value
  end

  def sdkroot
    found = MacOS.sdk_path
    found.nil? ? nil : found.to_s
  end

  def set_optimization(flag)
    %w[CFLAGS CXXFLAGS].each do |key|
      current = self[key].to_s
      cleaned = current.gsub(/(?:^|\s)-O\d?/, "").strip
      self[key] = cleaned.empty? ? flag : "#{cleaned} #{flag}"
    end
    nil
  end

  def find_tool(name)
    ENV.fetch("PATH", "").split(File::PATH_SEPARATOR).each do |directory|
      candidate = File.join(directory, name)
      return candidate if File.file?(candidate) && File.executable?(candidate)
    end
    nil
  end

  def with_build_environment(env: nil, cc: nil, build_bottle: false, bottle_arch: nil, debug_symbols: false)
    raise ArgumentError, "ENV.with_build_environment requires a block" unless block_given?

    old_env = to_hash
    begin
      if env.is_a?(Hash)
        env.each do |key, value|
          self[key.to_s] = value.nil? ? nil : value.to_s
        end
      end
      self["CC"] = cc.to_s unless cc.nil?
      yield
    ensure
      replace(old_env)
    end
  end

  # Superenv-compatible build environment setup.  This configures the same
  # variables the real shim would (HOMEBREW_ENV, CC/CXX, MAKEFLAGS, paths,
  # optimisation flags) without compiling anything.
  def setup_build_environment(formula: nil, cc: nil, build_bottle: false,
                              bottle_arch: nil, testing_formula: false,
                              debug_symbols: false)
    @pybrew_build_formula = formula
    @pybrew_build_bottle = build_bottle
    @pybrew_bottle_arch = bottle_arch
    @pybrew_testing_formula = testing_formula
    @pybrew_debug_symbols = debug_symbols

    selected = if cc.nil? || cc.to_s.empty?
      if !formula.nil? && formula.respond_to?(:compiler_failures)
        CompilerSelector.select_for(formula, testing_formula: testing_formula)
      else
        DevelopmentTools.default_compiler
      end
    else
      cc
    end
    apply_compiler(selected)

    self["HOMEBREW_ENV"] = "super"
    self["MAKEFLAGS"] ||= "-j#{make_jobs}"
    self["HOMEBREW_MAKE_JOBS"] = make_jobs.to_s
    self["HOMEBREW_CCCFG"] ||= ""
    self["HOMEBREW_OPTIMIZATION_LEVEL"] = "Os"
    self["HOMEBREW_OPTFLAGS"] = "-O2"
    self["HOMEBREW_PREFIX"] = HOMEBREW_PREFIX.to_s
    self["HOMEBREW_CELLAR"] = HOMEBREW_CELLAR.to_s
    self["HOMEBREW_OPT"] = "#{HOMEBREW_PREFIX}/opt"
    self["HOMEBREW_TEMP"] = HOMEBREW_TEMP.to_s
    if !formula.nil? && formula.respond_to?(:prefix)
      self["HOMEBREW_FORMULA_PREFIX"] = formula.prefix.to_s
    end
    shims = Superenv.shims_path.to_s
    path_parts = self["PATH"].to_s.split(File::PATH_SEPARATOR)
    prepend_path("PATH", shims) unless path_parts.include?(shims)
    self
  end
end

class PybrewInreplace
  attr_accessor :errors
  attr_accessor :inreplace_string

  def initialize(string)
    @inreplace_string = string.dup
    @errors = []
  end

  def gsub!(before, after, audit_result: true)
    before = before.to_s if before.is_a?(Pathname)
    result = @inreplace_string.gsub!(before, after.to_s)
    if audit_result && result.nil?
      @errors << "expected replacement of #{before.inspect} with #{after.inspect}"
    end
    result
  end

  def sub!(before, after, audit_result: true)
    before = before.to_s if before.is_a?(Pathname)
    result = @inreplace_string.sub!(before, after.to_s)
    if audit_result && result.nil?
      @errors << "expected replacement of #{before.inspect} with #{after.inspect}"
    end
    result
  end

  def change_make_var!(flag, new_value)
    return if gsub!(/^#{Regexp.escape(flag)}[ \t]*[\\?+:!]?=[ \t]*((?:.*\\\n)*.*)$/,
                    "#{flag}=#{new_value}",
                    audit_result: false)

    @errors << "expected to change #{flag.inspect} to #{new_value.inspect}"
  end

  def remove_make_var!(flags)
    Array(flags).each do |flag|
      next if gsub!(/^#{Regexp.escape(flag)}[ \t]*[\\?+:!]?=(?:.*\\\n)*.*$\n?/,
                    "",
                    audit_result: false)

      @errors << "expected to remove #{flag.inspect}"
    end
  end

  def get_make_var(flag)
    @inreplace_string[/^#{Regexp.escape(flag)}[ \t]*[\\?+:!]?=[ \t]*((?:.*\\\n)*.*)$/, 1] ||
      raise(ArgumentError, "expected to find make variable #{flag.inspect}")
  end
end

class PybrewDependencyRef
  attr_reader :name, :tags

  def initialize(name, tags = [])
    @name = name.to_s
    @tags = Array(tags).map { |tag| tag.to_s }
  end

  def full_name
    @name
  end

  def build?
    @tags.include?("build")
  end

  def test?
    @tags.include?("test")
  end

  def optional?
    @tags.include?("optional")
  end

  def recommended?
    @tags.include?("recommended")
  end

  def runtime?
    !build? && !test?
  end

  def no_linkage?
    @tags.include?("no_linkage")
  end

  def to_s
    @name
  end

  def inspect
    "#<PybrewDependencyRef #{@name}>"
  end

  def ==(other)
    if other.is_a?(String) || other.is_a?(Symbol)
      @name == other.to_s
    else
      other.respond_to?(:name) && other.name.to_s == @name
    end
  end
  alias eql? ==

  def hash
    @name.hash
  end
end

# Homebrew's ``Formula#resources`` returns an array of resources, but a lot of
# formula code also looks them up by name.  This small Array subclass supports
# both shapes without pretending to be a full Hash.
class PybrewResourceList < Array
  def [](key)
    if key.is_a?(String) || key.is_a?(Symbol)
      find { |resource| resource.name == key.to_s }
    else
      super
    end
  end

  def key?(key)
    any? { |resource| resource.name == key.to_s }
  end
  alias has_key? key?

  def keys
    map { |resource| resource.name }
  end

  def values
    to_a
  end

  # Support both Homebrew's ``resources.each { |resource| ... }`` and the
  # Hash-style ``resources.each { |name, resource| ... }`` shape.
  def each(&block)
    return enum_for(:each) unless block

    if block.arity == 2
      super() { |resource| block.call(resource.name, resource) }
    else
      super(&block)
    end
  end

  def each_value(&block)
    return enum_for(:each_value) unless block

    each(&block)
  end

  def each_pair
    return enum_for(:each_pair) unless block_given?

    each { |resource| yield(resource.name, resource) }
  end

  def to_h
    result = {}
    each { |resource| result[resource.name] = resource }
    result
  end

  def fetch(key, *args)
    if key.is_a?(String) || key.is_a?(Symbol)
      resource = self[key]
      return resource unless resource.nil?
      return args.first unless args.empty?
      raise KeyError, "resource not found: #{key.inspect}"
    else
      super
    end
  end
end

class PybrewSourceSpec
  attr_reader :url, :sha256, :version, :spec
  attr_accessor :owner

  def initialize(info, spec)
    @info = info || {}
    @spec = spec
    @url = @info["url"]
    @sha256 = @info["sha256"]
    @version = PybrewVersion.new((@info["version"] || "HEAD").to_s)
  end

  def spec
    @spec
  end

  def mirrors
    Array(@info["mirrors"])
  end

  def downloaded?
    false
  end

  def to_s
    @url.to_s
  end
end

class PybrewBottleFile
  attr_reader :tag, :sha256, :cellar, :rebuild

  def initialize(tag, info)
    @tag = tag.to_s
    @sha256 = (info || {})["sha256"]
    @cellar = (info || {})["cellar"]
    @rebuild = (info || {})["rebuild"]
  end
end

class PybrewBottleSpec
  attr_reader :root_url, :files, :current_tag

  def initialize(info)
    @info = info || {}
    @root_url = @info["root_url"]
    @files = @info["files"] || {}
    @current_tag = @info["current_tag"]
  end

  def root_url
    @root_url
  end

  def tag
    @current_tag
  end

  def sha256
    entry = @files[@current_tag] || {}
    entry["sha256"]
  end

  def cellar
    entry = @files[@current_tag] || {}
    entry["cellar"]
  end

  def rebuild
    entry = @files[@current_tag] || {}
    entry["rebuild"]
  end

  def bottle?
    !@files.empty?
  end

  def bottle_tags
    @files.keys
  end

  def tags
    @files.keys
  end

  def each_tag(&block)
    @files.each_key(&block)
    nil
  end

  def tag?(tag, no_older_versions: false)
    !find_matching_tag(tag, no_older_versions: no_older_versions).nil?
  end

  def find_matching_tag(tag, no_older_versions: false)
    key = tag.to_s
    return tag if @files.key?(key)
    return "all" if @files.key?("all")

    nil
  end

  def specification_for(tag, no_older_versions: false)
    key = find_matching_tag(tag, no_older_versions: no_older_versions)
    key.nil? ? nil : bottle_for_tag(key)
  end

  def add(tag, checksum:, cellar:)
    @files[tag.to_s] = { "sha256" => checksum, "cellar" => cellar }
    nil
  end

  def eql?(other)
    other.is_a?(PybrewBottleSpec) && @files == other.files
  end

  def json
    @info
  end

  def total_size
    @files.values.sum { |entry| entry.is_a?(Hash) ? entry["total_size"].to_i : 0 }
  end

  def built_cellar
    cellar
  end

  def compatible_locations?
    true
  end

  def extname
    ".bottle.tar.gz"
  end

  def url_encode
    nil
  end

  def github_packages
    nil
  end

  def tab_attributes
    {}
  end

  def filename
    nil
  end

  def to_str
    root_url.to_s
  end

  def to_s
    to_str
  end

  def skip_relocation?
    false
  end

  def fallback_on_error?
    false
  end

  def compatible_locations_from_tab?
    true
  end

  def stage_from_download_queue?
    false
  end

  def staged_from_download_queue?
    false
  end

  def stage_from_download_queue(*_args)
    nil
  end

  def staged_path_from_download_queue(*_args)
    nil
  end

  def staged_path_from_download_queue_marker(*_args)
    nil
  end

  def purge_staged_from_download_queue(*_args)
    nil
  end

  def discard_corrupt_cached_download(*_args)
    nil
  end

  def extract_snapshot(*_args)
    nil
  end

  def fetch_tab(*_args)
    nil
  end

  def select_download_strategy(*_args)
    nil
  end

  def github_packages_manifest_resource(*_args)
    nil
  end

  def with_corrupt_download_retry(*_args)
    block_given? ? yield : nil
  end

  def with_verified_snapshot(*_args)
    block_given? ? yield : nil
  end

  def bottle_for_tag(tag)
    key = tag.to_s
    info = @files[key]
    info.nil? ? nil : PybrewBottleFile.new(key, info)
  end

  def bottle_url(tag)
    key = tag.to_s
    info = @files[key]
    return nil if info.nil?

    root = (@root_url || "https://ghcr.io/v2/homebrew/core").to_s.sub(%r{/+\z}, "")
    if root.include?("ghcr.io")
      "#{root}/#{name_for_url}/blobs/sha256:#{info["sha256"]}"
    else
      "#{root}/#{name_for_url}-#{tag}.bottle.tar.gz"
    end
  end

  private

  def name_for_url
    @name_for_url ||= (@info["name"] || "formula").to_s
  end
end

class PybrewPatchRef
  attr_reader :url, :file, :sha256, :strip

  def initialize(info)
    @info = info || {}
    @url = @info["url"]
    @file = @info["file"]
    @sha256 = @info["sha256"]
    @strip = (@info["strip"] || 1).to_i
  end

  def data?
    @info["data"] == true
  end

  def to_s
    (@url || @file || "inline patch").to_s
  end
end

class PybrewServiceRef
  def initialize(info)
    @info = info || {}
  end

  def source
    @info["source"].to_s
  end

  def to_hash
    @info.dup
  end

  def formula_name
    @info["name"].to_s
  end

  def plist_name
    @info["plist_name"] || "sh.brew.#{formula_name}"
  end

  def canonical_plist_name
    "sh.brew.#{formula_name}"
  end

  def legacy_plist_name
    "homebrew.mxcl.#{formula_name}"
  end

  def plist_names
    return [plist_name] if @info["plist_name"]

    [plist_name, canonical_plist_name, legacy_plist_name].uniq
  end

  def service_name
    @info["service_name"] || "sh.brew.#{formula_name}"
  end

  def canonical_service_name
    "sh.brew.#{formula_name}"
  end

  def legacy_service_name
    "homebrew.#{formula_name}"
  end

  def service_names
    return [service_name] if @info["service_name"]

    [service_name, canonical_service_name, legacy_service_name].uniq
  end

  def run
    Array(@info["run"])
  end

  def working_dir
    @info["working_dir"]
  end

  def log_path
    @info["log_path"]
  end

  def error_log_path
    @info["error_log_path"]
  end

  def keep_alive
    @info["keep_alive"]
  end

  def interval
    @info["interval"]
  end

  def process_type
    @info["process_type"]
  end

  def run_type
    @info["run_type"] || "immediate"
  end

  def warnings
    Array(@info["warnings"])
  end

  def environment_variables
    @info["environment_variables"] || {}
  end

  def require_root?
    !!@info["require_root"]
  end

  def launch_only_once?
    !!@info["launch_only_once"]
  end

  def macos_legacy_timers?
    !!@info["macos_legacy_timers"]
  end

  def throttle_interval
    @info["throttle_interval"]
  end

  def nice
    @info["nice"]
  end

  def restart_delay
    @info["restart_delay"]
  end

  def stop_timeout
    @info["stop_timeout"]
  end

  def root_dir
    @info["root_dir"]
  end

  def input_path
    @info["input_path"]
  end

  def cron
    @info["cron"] || {}
  end

  def sockets
    @info["sockets"] || {}
  end

  def to_s
    source
  end
end

module Sandbox
  SANDBOX_ENV = "PYBREW_SANDBOX"
  SANDBOX_BINARY_ENV = "PYBREW_SANDBOX_BINARY"
  SANDBOX_PATH_ENV = "PYBREW_SANDBOX_PATH"
  SANDBOX_CANDIDATES = [
    "/usr/bin/sandbox-exec",
    "/usr/bin/bwrap",
    "/usr/local/bin/bwrap",
    "/opt/homebrew/bin/bwrap",
  ].freeze

  # Rules/backends that still cannot be faithfully reproduced by the bounded
  # policy renderer.  Everything else is implemented (default stays inert).
  UNSUPPORTED_POLICY_METHODS = %i[
    capture cleanup_sandbox copy_pty_output protect_homebrew_state
    record_log record_sandbox_log
  ].freeze

  # Home-relative credential paths kept readable by ``deny_read_home(except: :git)``.
  GIT_CREDENTIAL_PATHS = %w[
    .ssh .gitconfig .git-credentials .config/gh .netrc
  ].freeze

  # Personal/credential paths denied individually when the whole home cannot
  # be denied because Homebrew/CI paths live inside it.
  HOME_DENY_PATHS = %w[
    .ssh .gitconfig .git-credentials .config/gh .netrc .aws .azure .docker
    .gnupg .kube .npmrc .pypirc .gem/credentials .bundle/config
    .cargo/credentials .cargo/credentials.toml .m2/settings.xml
    .gradle/gradle.properties .env .env.local .bash_history .zsh_history
    .python_history .mysql_history .psql_history
    Documents Library/Keychains Library/Mobile Documents
  ].freeze

  class << self
    # Optional bounded sandbox backend.  Disabled by default.  When
    # PYBREW_SANDBOX=1, an explicit executable (PYBREW_SANDBOX_BINARY) wins;
    # otherwise the platform backend is detected (/usr/bin/sandbox-exec on
    # macOS, bwrap on Linux).  PYBREW_SANDBOX_PATH overrides/excludes detection
    # (used by tests to simulate unavailable backends).
    def detected_executable
      override = ENV[SANDBOX_PATH_ENV].to_s
      unless override.empty?
        path = Pathname.new(override)
        return path.to_s if path.file? && path.executable?

        return nil
      end

      SANDBOX_CANDIDATES.find do |candidate|
        File.file?(candidate.to_s) && File.executable?(candidate.to_s)
      end
    end

    def executable
      raw = ENV[SANDBOX_BINARY_ENV].to_s
      unless raw.empty?
        path = Pathname.new(raw)
        return path.to_s if path.file? && path.executable?

        return nil
      end

      detected_executable
    end

    def backend
      found = executable
      return :none if found.nil?

      name = File.basename(found.to_s)
      return :sandbox_exec if name == "sandbox-exec"
      return :bwrap if name == "bwrap" || name == "bubblewrap"

      :explicit
    end

    def enabled?
      ENV[SANDBOX_ENV].to_s == "1" && !executable.nil?
    end

    def available?
      enabled?
    end

    def full_write_isolation?
      true
    end

    def nested_sandbox?
      false
    end

    def avoid_nested_sandboxing?
      false
    end

    def use_for?(*_args, **_kwargs)
      enabled?
    end

    def inherited_sandbox?
      false
    end

    def state
      nil
    end

    def failure_reason
      @failure_reason
    end

    def reset_state!
      @failure_reason = nil
      nil
    end

    def with_preserved_brew_file
      yield if block_given?
    end

    # Wrap an explicit command with the backend arguments.  ``sandbox-exec``
    # receives the collected rules profile (or the allow-default baseline when
    # no rules were collected) instead of always ignoring the rules.
    def wrapped_command(*command)
      argv = command.flatten.compact.map(&:to_s)
      case backend
      when :sandbox_exec
        profile = rules.empty? && !strict? ? "(version 1)(allow default)" : sandbox_profile
        @applied_policy = {
          "backend" => "sandbox_exec",
          "executable" => executable.to_s,
          "strict" => strict?,
          "rules" => rules.map { |rule| rule[:kind].to_s },
          "profile" => profile,
          "command" => [executable.to_s, "-p", profile, *argv],
        }
        [executable, "-p", profile, *argv]
      when :bwrap
        args = bwrap_policy_args
        @applied_policy = {
          "backend" => "bwrap",
          "executable" => executable.to_s,
          "strict" => strict?,
          "rules" => rules.map { |rule| rule[:kind].to_s },
          "args" => args,
          "command" => [executable.to_s, *args, "--", *argv],
        }
        [executable, *args, "--", *argv]
      else
        [executable, *argv]
      end
    end

    # Run an explicit command through the sandbox backend.  Returns nil when
    # sandboxing is not enabled so callers keep their normal path; when it is
    # explicitly enabled but no backend exists, raises a clear error instead of
    # silently running unsandboxed.
    def run(*command)
      return nil unless ENV[SANDBOX_ENV].to_s == "1"
      raise ArgumentError, "Sandbox.run requires a command" if command.empty?
      if executable.nil?
        raise NotImplementedError,
              "Sandbox is enabled (PYBREW_SANDBOX=1) but no sandbox backend is " \
              "available; install bwrap/sandbox-exec or set PYBREW_SANDBOX_BINARY " \
              "(candidates: #{SANDBOX_CANDIDATES.join(", ")})"
      end

      Utils.popen_read(*wrapped_command(*command), safe: true)
    rescue NotImplementedError
      raise
    rescue ScriptError, StandardError => error
      @failure_reason = "#{error.class}: #{error.message}"
      nil
    end

    def rule_collection_enabled?
      ENV[SANDBOX_ENV].to_s == "1"
    end

    def rules
      @rules ||= []
    end

    def reset_rules!
      @rules = []
      @applied_policy = nil
      nil
    end

    def collect_rule!(kind, payload = nil)
      unless rule_collection_enabled?
        raise NotImplementedError,
              "Sandbox rule collection requires PYBREW_SANDBOX=1; sandbox policy " \
              "generation is disabled by default"
      end
      rules << { kind: kind.to_sym, payload: payload }
      self
    end

    def rule_paths(payload)
      rule_filters(payload).map { |filter| filter[:path] }
    end

    # Normalize a rule payload into Homebrew-style path filters.  Plain
    # strings keep the historical subpath semantics; hashes carry an explicit
    # ``type`` (:literal/:subpath/:regex) like upstream ``path_filter``.
    def rule_filters(payload)
      Array(payload).flatten.compact.map do |entry|
        if entry.is_a?(Hash) && entry.key?(:path)
          { path: entry[:path].to_s, type: (entry[:type] || :literal).to_sym }
        else
          path_filter(entry.to_s, :subpath)
        end
      end.compact
    end

    # Upstream-compatible path filter: returns nil for no path so callers can
    # treat "deny whole home" separately.
    def path_filter(path = nil, type = :literal)
      return nil if path.nil?

      normalized_type = type.to_s.to_sym
      unless %i[literal subpath regex].include?(normalized_type)
        raise ArgumentError, "Invalid path filter type: #{type}"
      end
      # Keep the caller-supplied path verbatim in the emitted profile (tests
      # and users reason about the given path); realpath resolution is only
      # used for internal containment checks.
      { path: path.to_s, type: normalized_type }
    end

    def expand_realpath(path)
      pathname = Pathname.new(path.to_s)
      return pathname.expand_path unless pathname.absolute?
      return pathname.realpath if pathname.exist?

      expand_realpath(pathname.parent)/pathname.basename
    rescue StandardError
      Pathname.new(path.to_s).expand_path
    end

    def filter_line(allow, operation, filter)
      verb = allow ? "allow" : "deny"
      return "(#{verb} #{operation})" if filter.nil?

      case filter[:type]
      when :literal
        "(#{verb} #{operation} (literal \"#{sandbox_escape(filter[:path])}\"))"
      when :regex
        "(#{verb} #{operation} (regex #\"#{sandbox_escape(filter[:path])}\"))"
      else
        "(#{verb} #{operation} (subpath \"#{sandbox_escape(filter[:path])}\"))"
      end
    end

    def sandbox_escape(value)
      value.to_s.gsub("\\", "\\\\").gsub("\"", "\\\"")
    end

    def applied_policy
      @applied_policy
    end

    def strict?
      ENV["PYBREW_SANDBOX_STRICT"].to_s == "1"
    end

    def sandbox_read_roots
      roots = ["/System", "/usr", "/bin", "/sbin", "/Library", "/private/etc",
               "/private/tmp", "/private/var", "/dev"]
      roots << HOMEBREW_PREFIX.to_s
      roots << HOMEBREW_CELLAR.to_s
      roots << HOMEBREW_REPOSITORY.to_s if defined?(HOMEBREW_REPOSITORY)
      roots.map(&:to_s).reject(&:empty?).uniq
    end

    def sandbox_profile
      lines = ["(version 1)"]
      if strict?
        lines << "(deny default)"
        lines << "(allow process-exec)"
        lines << "(allow process-fork)"
        lines << "(allow signal)"
        lines << "(allow sysctl-read)"
        sandbox_read_roots.each do |root|
          lines << "(allow file-read* (subpath \"#{sandbox_escape(root)}\"))"
        end
        lines << "(allow file-write* (subpath \"/private/tmp\"))"
        lines << "(allow file-write* (subpath \"/private/var/folders\"))"
        lines << "(allow file-write* (subpath \"/dev/null\"))"
        lines << "(allow file-write* (subpath \"#{sandbox_escape(HOMEBREW_CELLAR.to_s)}\"))"
        lines << "(allow file-write* (subpath \"#{sandbox_escape(HOMEBREW_PREFIX.to_s)}\"))"
      else
        lines << "(allow default)"
      end
      rules.each do |rule|
        kind = rule[:kind]
        payload = rule[:payload]
        case kind
        when :raw
          Array(payload).flatten.compact.map(&:to_s).each do |raw|
            lines << raw if raw.start_with?("(")
          end
        when :noop
          nil
        when :allow_read, :deny_read
          filters = rule_filters(payload)
          if filters.empty?
            lines << filter_line(kind == :allow_read, "file-read*", nil)
          else
            filters.each { |filter| lines << filter_line(kind == :allow_read, "file-read*", filter) }
          end
        when :allow_write
          filters = rule_filters(payload)
          if filters.empty?
            lines << filter_line(true, "file-write*", nil)
          else
            filters.each do |filter|
              lines << filter_line(true, "file-write*", filter)
              lines << filter_line(true, "file-write-setugid", filter)
              lines << filter_line(true, "file-write-mode", filter)
            end
          end
        when :deny_write
          filters = rule_filters(payload)
          if filters.empty?
            lines << filter_line(false, "file-write*", nil)
          else
            filters.each { |filter| lines << filter_line(false, "file-write*", filter) }
          end
        when :allow_network
          filters = rule_filters(payload)
          if filters.empty?
            lines << "(allow network*)"
          else
            filters.each { |filter| lines << filter_line(true, "network*", filter) }
          end
        when :deny_network
          lines << "(deny network*)"
        when :allow_process_exec
          filters = rule_filters(payload)
          if filters.empty?
            lines << "(allow process-exec)"
          else
            filters.each { |filter| lines << filter_line(true, "process-exec", filter) }
          end
        end
      end
      lines.join("\n")
    end

    def bwrap_policy_args
      write_filters = []
      read_filters = []
      deny_paths = []
      rules.each do |rule|
        case rule[:kind]
        when :allow_write
          write_filters.concat(rule_filters(rule[:payload]))
        when :allow_read
          read_filters.concat(rule_filters(rule[:payload]))
        when :deny_read, :deny_write
          deny_paths.concat(rule_filters(rule[:payload]).map { |filter| filter[:path] })
        end
      end
      denied = lambda do |path|
        deny_paths.any? do |denied_path|
          path.to_s == denied_path.to_s ||
            path.to_s.start_with?("#{denied_path}/") ||
            denied_path.to_s.start_with?("#{path}/")
        end
      end
      write_paths = write_filters.map { |filter| filter[:path] }.reject(&:empty?).reject(&denied)
      read_paths = read_filters.map { |filter| filter[:path] }.reject(&:empty?).reject(&denied)

      if strict?
        args = ["--dev", "/dev", "--proc", "/proc", "--unshare-user",
                "--cap-drop", "ALL", "--die-with-parent"]
        sandbox_read_roots.each do |root|
          args << "--ro-bind" << root.to_s << root.to_s
        end
        read_paths.each { |path| args << "--ro-bind-try" << path << path }
        write_paths.concat(["/tmp", "/private/tmp", HOMEBREW_CELLAR.to_s,
                            HOMEBREW_PREFIX.to_s])
      else
        args = ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        read_paths.each { |path| args << "--ro-bind-try" << path << path }
      end
      write_paths.map(&:to_s).reject(&:empty?).uniq.each do |path|
        args << "--bind" << path << path
      end
      network_allowed = rules.any? { |rule| rule[:kind] == :allow_network }
      args << "--unshare-net" unless network_allowed
      args
    end

    def unsupported_isolation!(name)
      raise NotImplementedError,
            "Sandbox.#{name} is not implemented in the pybrew Ruby shim: " \
            "sandbox policy/rules are bounded to the optional external wrapper " \
            "(Sandbox.available? is #{available?})"
    end

    def add_rule(*args, **_kwargs)
      collect_rule!(:raw, args)
    end

    # Upstream signature is ``add_install_hook_rules(network_access_allowed:)``.
    def add_install_hook_rules(network_access_allowed: false, **_kwargs)
      allow_write_temp_and_cache
      deny_write_homebrew_repository
      deny_read_home
      deny_all_network unless network_access_allowed
      true
    end

    def allow_cvs(*_args, **_kwargs)
      collect_rule!(:allow_write, [path_filter("#{Dir.home}/.cvspass", :literal)])
    end

    def allow_fossil(*_args, **_kwargs)
      collect_rule!(:allow_write, [
        path_filter("#{Dir.home}/.fossil", :literal),
        path_filter("#{Dir.home}/.fossil-journal", :literal),
      ])
    end

    def allow_network(*paths, path: nil, type: :subpath, **_kwargs)
      entries = [path, *paths].compact.map { |entry| path_filter(entry, type) }
      collect_rule!(:allow_network, entries)
    end

    def allow_process_exec(*paths, path: nil, type: :subpath, **_kwargs)
      entries = [path, *paths].compact.map { |entry| path_filter(entry, type) }
      collect_rule!(:allow_process_exec, entries)
    end

    def allow_read(*paths, path: nil, type: :subpath, **_kwargs)
      entries = [path, *paths].compact.map { |entry| path_filter(entry, type) }
      collect_rule!(:allow_read, entries)
    end

    def allow_read_if_exists(*paths, path: nil, type: :subpath, **_kwargs)
      entries = [path, *paths].compact.select { |entry| File.exist?(entry.to_s) }
      entries = entries.map { |entry| path_filter(entry, type) }
      collect_rule!(:allow_read, entries)
    end

    def allow_write(*paths, path: nil, type: :subpath, **_kwargs)
      entries = [path, *paths].compact.map { |entry| path_filter(entry, type) }
      collect_rule!(:allow_write, entries)
    end

    # ``allow_write_cellar(formula)`` in Homebrew grants the rack/etc/var
    # trees.  Accept a formula-like object and fall back to the shared Cellar.
    def allow_write_cellar(formula = nil, **_kwargs)
      paths = if !formula.nil? && formula.respond_to?(:rack)
        [formula.rack, (formula.etc if formula.respond_to?(:etc)), (formula.var if formula.respond_to?(:var))]
      else
        [HOMEBREW_CELLAR]
      end
      collect_rule!(:allow_write, paths.compact.map { |path| path_filter(path, :subpath) })
    end

    def allow_write_log(formula = nil, **_kwargs)
      path = if !formula.nil? && formula.respond_to?(:logs)
        formula.logs
      else
        HOMEBREW_PREFIX/"var/log"
      end
      collect_rule!(:allow_write, [path_filter(path, :subpath)])
    end

    def allow_write_path(*paths, path: nil, **_kwargs)
      entries = [path, *paths].compact.map { |entry| path_filter(entry, :subpath) }
      collect_rule!(:allow_write, entries)
    end

    def allow_write_path_if_exists(*paths, path: nil, **_kwargs)
      entries = [path, *paths].compact.select { |entry| File.exist?(entry.to_s) }
      entries = entries.map { |entry| path_filter(entry, :subpath) }
      collect_rule!(:allow_write, entries)
    end

    def allow_write_system_temp(*_args, **_kwargs)
      collect_rule!(:allow_write, [path_filter(HOMEBREW_TEMP, :subpath)])
    end

    def allow_write_temp_and_cache(*_args, **_kwargs)
      allow_write_system_temp
      cache = if defined?(HOMEBREW_CACHE)
        HOMEBREW_CACHE
      else
        HOMEBREW_PREFIX/"Library/Caches"
      end
      collect_rule!(:allow_write, [path_filter(cache, :subpath)])
    end

    def allow_write_xcode(*_args, **_kwargs)
      # Mirrors upstream: no extra rule on this bounded implementation.
      nil
    end

    def apply!(*_args, **_kwargs)
      unless enabled?
        raise NotImplementedError,
              "Sandbox.apply! requires PYBREW_SANDBOX=1 and an available sandbox " \
              "backend; the default remains disabled"
      end
      profile = sandbox_profile
      @applied_policy = {
        "backend" => backend.to_s,
        "executable" => executable.to_s,
        "strict" => strict?,
        "rules" => rules.map { |rule| rule[:kind].to_s },
        "profile" => profile,
        "command" => wrapped_command("/bin/sh", "-c", "true"),
      }
      if backend == :bwrap
        @applied_policy["args"] = bwrap_policy_args
      end
      true
    end

    def capture(*_args, **_kwargs)
      unsupported_isolation!(:capture)
    end

    def cleanup_sandbox(*_args, **_kwargs)
      unsupported_isolation!(:cleanup_sandbox)
    end

    def copy_pty_output(*_args, **_kwargs)
      unsupported_isolation!(:copy_pty_output)
    end

    def deny_all_network(*_args, **_kwargs)
      collect_rule!(:deny_network, :network)
    end

    def deny_read(*paths, path: nil, type: :subpath, **_kwargs)
      entries = [path, *paths].compact.map { |entry| path_filter(entry, type) }
      collect_rule!(:deny_read, entries)
    end

    # ``deny_read_home(except: :git)`` keeps git credentials/shortcuts
    # readable while denying the rest of the personal home paths.  Without a
    # usable HOME the call is a bounded no-op.
    def deny_read_home(except: nil, **_kwargs)
      home = ENV["HOME"].to_s
      return nil if home.empty?
      if !except.nil? && except.to_sym != :git
        raise ArgumentError, "Unknown home credential exception: #{except.inspect}"
      end

      if !except.nil? && except.to_sym == :git
        HOME_DENY_PATHS.each do |relative|
          next if GIT_CREDENTIAL_PATHS.include?(relative)

          path = Pathname.new(home)/relative
          next unless path.exist?

          collect_rule!(:deny_read, [path_filter(path, :subpath)])
        end
        GIT_CREDENTIAL_PATHS.each do |relative|
          path = Pathname.new(home)/relative
          next unless path.exist? || path.symlink?

          collect_rule!(:allow_read, [path_filter(path, :literal)])
        end
        return nil
      end

      collect_rule!(:deny_read, [path_filter(home, :subpath)])
      nil
    end

    def deny_read_path(*paths, **_kwargs)
      entries = paths.compact.map { |entry| path_filter(entry, :subpath) }
      collect_rule!(:deny_read, entries)
    end

    def deny_write(*paths, path: nil, type: :subpath, **_kwargs)
      entries = [path, *paths].compact.map { |entry| path_filter(entry, type) }
      collect_rule!(:deny_write, entries)
    end

    def deny_write_homebrew_repository(*_args, **_kwargs)
      paths = []
      brew_file = HOMEBREW_PREFIX/"bin/brew"
      paths << brew_file if brew_file.file? || brew_file.symlink?
      paths << HOMEBREW_REPOSITORY if defined?(HOMEBREW_REPOSITORY)
      paths << HOMEBREW_PREFIX/"Library" if (HOMEBREW_PREFIX/"Library").directory?
      paths = [HOMEBREW_PREFIX] if paths.empty?
      collect_rule!(:deny_write, paths.compact.map { |path| path_filter(path, :subpath) })
    end

    def deny_write_path(*paths, **_kwargs)
      entries = paths.compact.map { |entry| path_filter(entry, :subpath) }
      collect_rule!(:deny_write, entries)
    end

    def protect_homebrew_state(*_args, **_kwargs)
      unsupported_isolation!(:protect_homebrew_state)
    end

    def record_log(*_args, **_kwargs)
      unsupported_isolation!(:record_log)
    end

    def record_sandbox_log(*_args, **_kwargs)
      unsupported_isolation!(:record_sandbox_log)
    end

    def executable!
      found = executable
      return found unless found.nil?

      raise NotImplementedError,
            "Sandbox.executable! is not available: no sandbox backend found " \
            "(candidates: #{SANDBOX_CANDIDATES.join(", ")}); set " \
            "PYBREW_SANDBOX_BINARY to use a custom/fake backend"
    end

    def ensure_sandbox_available!
      return true if available?

      raise NotImplementedError,
            "Sandbox.ensure_sandbox_available! cannot provide isolation: " \
            "PYBREW_SANDBOX is not enabled or no sandbox backend is available; " \
            "the default remains safe no-sandbox mode"
    end

    def executable_candidate_paths
      SANDBOX_CANDIDATES.dup
    end

    def executable_name
      found = executable
      found.nil? ? "sandbox" : File.basename(found.to_s)
    end

    def executable_usable?
      !executable.nil?
    end

    def apply_before_exec?
      false
    end

    def sandbox_command(*command)
      command.flatten.compact.map(&:to_s)
    end

    def sandbox_environment
      {}
    end

    def run_command(*command)
      run(*command)
    end

    def ruby_command(*args)
      ["ruby", *args.flatten.compact.map(&:to_s)]
    end

    def for_operation(*_args, **_kwargs, &block)
      block.nil? ? nil : block.call
    end

    def operation
      nil
    end

    def isolate_operation?
      use_for?
    end

    def tty_state
      nil
    end

    def terminal_ioctl_request
      nil
    end

    def ensure_child_tty_available
      nil
    end

    def home_write_paths
      []
    end

    def run_or_fork(*_args, **_kwargs, &block)
      block.nil? ? nil : block.call
    end
  end
end

module Superenv
  def self.shims_path
    HOMEBREW_PREFIX/"shims"
  end

  def self.bin
    shims_path
  end

  def self.enabled_for?(_env = nil)
    false
  end
end

module PybrewStdArgs
  def std_configure_args(prefix: self.prefix, libdir: "lib")
    [
      "--disable-debug",
      "--disable-dependency-tracking",
      "--prefix=#{prefix}",
      "--libdir=#{prefix}/#{libdir}",
    ]
  end

  def std_cmake_args(install_prefix: prefix, install_libdir: "lib", find_framework: "LAST")
    [
      "-DCMAKE_INSTALL_PREFIX=#{install_prefix}",
      "-DCMAKE_INSTALL_LIBDIR=#{install_libdir}",
      "-DCMAKE_BUILD_TYPE=Release",
      "-DCMAKE_FIND_FRAMEWORK=#{find_framework}",
      "-DCMAKE_VERBOSE_MAKEFILE=ON",
      "-Wno-dev",
    ]
  end

  def std_meson_args(prefix: self.prefix, libdir: "lib")
    ["--prefix=#{prefix}", "--libdir=#{libdir}", "--buildtype=release", "--wrap-mode=nofallback"]
  end

  def std_cargo_args(root: prefix, path: ".", features: nil)
    args = ["--jobs", ENV.make_jobs.to_s, "--locked", "--root=#{root}", "--path=#{path}"]
    args << "--features=#{Array(features).join(",")}" if features
    args
  end

  def std_cargo_fetch_args
    ["--locked", "--target", "host-tuple"]
  end

  def std_go_args(output: bin/name, ldflags: nil, gcflags: nil, tags: nil)
    args = ["-o=#{output}"]
    args << "-ldflags=#{ldflags}" unless ldflags.nil?
    args << "-gcflags=#{gcflags}" unless gcflags.nil?
    args << "-tags=#{Array(tags).join(",")}" unless tags.nil?
    args
  end

  def std_pip_args(prefix: self.prefix, build_isolation: false)
    args = ["--verbose", "--no-deps", "--no-binary=:all:", "--ignore-installed", "--no-compile"]
    args << "--prefix=#{prefix}" if prefix
    args << "--no-build-isolation" unless build_isolation
    args
  end

  def std_npm_args(prefix: libexec, ignore_scripts: true)
    args = ["--prefix=#{prefix}"]
    args << "--ignore-scripts" if ignore_scripts
    args
  end

  def std_shards_args
    ["--production", "--release", "--no-debug"]
  end

  def std_swift_args
    ["--configuration", "release", "--jobs", ENV.make_jobs.to_s]
  end

  def std_cabal_v2_args(installdir: bin)
    args = ["--jobs=#{ENV.make_jobs}", "--max-backjumps=100000"]
    args += ["--install-method=copy", "--installdir=#{installdir}"] if installdir
    args
  end

  def std_zig_args(prefix: self.prefix, release_mode: :fast, cpu: nil)
    mode = release_mode.to_s.downcase
    unless %w[safe fast small].include?(mode)
      raise ArgumentError, "Invalid Zig release mode: #{release_mode}"
    end

    cpu ||= Hardware::CPU.arch.to_s
    [
      "--prefix", prefix.to_s,
      "--release=#{mode}",
      "-Doptimize=Release#{mode.capitalize}",
      "--summary", "all",
      "-Dcpu=#{cpu}",
    ]
  end
end

class FormulaInstaller
  class << self
    def attempted
      @attempted ||= []
    end

    def installed
      @installed ||= []
    end

    def fetched
      @fetched ||= []
    end

    # Dependencies skipped because they have no install block or would
    # recursively install a formula that is already being installed.
    def skipped
      @skipped ||= []
    end

    # Names of Formulary-loaded formulae currently being installed; used to
    # break dependency cycles.
    def installing
      @installing ||= []
    end

    # Test/embedding helper: forget per-process install bookkeeping.
    def reset_tracking!
      @attempted = []
      @installed = []
      @fetched = []
      @skipped = []
      @installing = []
      nil
    end
  end

  attr_reader :formula, :options

  def initialize(formula, *_args, **options)
    @formula = formula
    @options = options
  end

  def install
    unless formula.is_a?(PybrewLoadedFormula)
      raise NotImplementedError,
            "FormulaInstaller#install is only available for Formulary-loaded " \
            "formula sources in pybrew; use pybrew's Python installer for kegs"
    end

    name = formula.name.to_s
    if self.class.installing.include?(name)
      record_skipped_dependency(formula)
      return nil
    end

    self.class.installing << name
    begin
      prelude
      self.class.attempted << formula unless self.class.attempted.any? { |item| item.to_s == name }
      fetch
      install_dependencies
      build
      clean
      finish
      unless only_deps?
        unless self.class.installed.any? { |item| item.to_s == name }
          self.class.installed << formula
        end
        write_install_receipt
      end
      formula
    ensure
      self.class.installing.delete(name)
    end
  end

  def same_formula?(other)
    other.respond_to?(:name) && formula.name.to_s == other.name.to_s
  end

  def fix_dynamic_linkage?(_keg = nil)
    true
  end

  def expand_dependencies_for_formula(_formula = nil)
    formula.respond_to?(:dependencies) ? formula.dependencies : []
  end

  def expand_dependencies_for_formula_once(_formula = nil, _keepable = [], _kept = [])
    []
  end

  RECEIPT_FILENAME = "INSTALL_RECEIPT.json".freeze

  # Deterministic (sorted, size/hash bounded) SBOM of the installed keg,
  # matching the Tab fields Homebrew writes for a source build.
  def install_sbom_data
    files = {}
    return { "files" => files } unless formula.respond_to?(:prefix)

    root = formula.prefix
    return { "files" => files } unless root.directory?

    count = 0
    Dir.glob((root/"**/*").to_s, File::FNM_DOTMATCH).sort.each do |entry|
      path = Pathname.new(entry)
      next unless path.file?
      next if path.basename.to_s == RECEIPT_FILENAME
      next if path.to_s.include?("/.git/")

      relative = path.relative_path_from(root).to_s
      next if relative.start_with?("..")
      next if relative.include?("/../")

      count += 1
      break if count > 1000

      digest = begin
        Digest::SHA256.file(path.to_s).hexdigest
      rescue StandardError
        nil
      end
      files[relative] = {
        "path" => relative,
        "sha256" => digest,
        "size" => path.size,
      }
    end
    { "files" => files }
  end

  def formula_tap_name
    return formula.tap.to_s if formula.respond_to?(:tap) && !formula.tap.to_s.empty?

    source_path = formula.respond_to?(:path) ? formula.path.to_s : ""
    match = source_path.match(%r{/Library/Taps/([^/]+/[^/]+)/})
    match.nil? ? nil : match[1]
  rescue StandardError
    nil
  end

  # Homebrew-compatible Tab fields for a Formulary-loaded install.
  # ``Tab.for_keg(prefix)`` reads this back.
  def install_receipt_data
    runtime_dependencies = []
    if formula.respond_to?(:dependencies)
      formula.dependencies.each do |dep|
        next if dep.respond_to?(:runtime?) && !dep.runtime?

        dep_name = dep.name.to_s
        next if dep_name.empty? || runtime_dependencies.include?(dep_name)

        runtime_dependencies << dep_name
      end
    end
    source_path = formula.respond_to?(:path) ? formula.path : nil
    source_modified_time = if !source_path.nil? && source_path.file?
      File.mtime(source_path.to_s).to_f
    end
    stdlib = OS.mac? ? "libc++" : "libstdc++"
    {
      "version" => formula.version.to_s,
      "installed_on_request" => installed_on_request?,
      "installed_as_dependency" => !installed_on_request?,
      "runtime_dependencies" => runtime_dependencies,
      "poured_from_bottle" => false,
      "built_as_bottle" => false,
      "homebrew_version" => HOMEBREW_VERSION,
      "time" => Time.now.to_i,
      "source_modified_time" => source_modified_time,
      "compiler" => DevelopmentTools.default_compiler.to_s,
      "arch" => Hardware::CPU.arch.to_s,
      "stdlib" => stdlib,
      "changed_files" => [],
      "source" => {
        "path" => source_path.nil? ? nil : source_path.to_s,
        "checksum" => nil,
      },
      "tap" => formula_tap_name,
      "sbom" => install_sbom_data,
    }
  end

  def write_install_receipt
    return nil unless formula.is_a?(PybrewLoadedFormula)
    return nil if only_deps?

    prefix = formula.prefix
    return nil unless prefix.directory?

    path = prefix/RECEIPT_FILENAME
    path.write(JSON.pretty_generate(install_receipt_data) + "\n")
    path
  rescue ScriptError, StandardError
    nil
  end

  # Mirror Homebrew's install flow for Formulary-loaded sources: link the keg
  # and run ``post_install`` (unless asked to skip it).
  def finish
    return nil if only_deps?
    return nil unless formula.is_a?(PybrewLoadedFormula)

    link
    if build_bottle? || skip_post_install?
      nil
    elsif formula.respond_to?(:post_install)
      formula.post_install
    end
    self
  end

  def prelude
    nil
  end

  def prelude_fetch(metadata_only: false)
    nil
  end

  # Fetch dependencies and, for Formulary-loaded sources, run the formula's
  # ``fetch do ... end`` block when one was declared.
  def fetch
    fetch_dependencies
    if formula.respond_to?(:fetch_defined?) && formula.fetch_defined? &&
       formula.respond_to?(:fetch)
      formula.fetch
    elsif formula.respond_to?(:fetch?) && formula.fetch? && formula.respond_to?(:fetch)
      formula.fetch
    end
    unless self.class.fetched.any? { |item| item.to_s == formula.to_s }
      self.class.fetched << formula
    end
    self
  end

  # Run a Formulary-loaded formula's install block.  Ref-based installs stay
  # owned by pybrew's Python installer.
  def build
    unless formula.is_a?(PybrewLoadedFormula)
      raise NotImplementedError,
            "FormulaInstaller#build is only available for Formulary-loaded " \
            "formula sources in pybrew; use pybrew's Python installer for kegs"
    end

    formula.install
    self
  end

  # Homebrew's Cleaner surface, limited to the safe removals that make sense
  # for a shim-owned build: ``charset.alias`` and ``.la``/``.tbd`` files.
  def clean
    return nil unless formula.is_a?(PybrewLoadedFormula)

    prefix = formula.prefix
    return nil unless prefix.directory?

    removed = []
    skip_clean = lambda do |path|
      formula.respond_to?(:skip_clean?) && formula.skip_clean?(path)
    end

    charset = prefix/"lib"/"charset.alias"
    if charset.file? && !skip_clean.call(charset) && charset.unlink
      removed << charset
    end

    [prefix/"bin", prefix/"sbin", prefix/"lib"].each do |directory|
      next unless directory.directory?

      Dir.glob("#{directory}/**/*", File::FNM_DOTMATCH).sort.each do |entry|
        path = Pathname.new(entry)
        next if path.directory?
        next if path.symlink?
        next if skip_clean.call(path)
        next unless %w[.la .tbd].include?(path.extname)

        removed << path if path.unlink
      end
    end

    removed
  end
  alias cleanup clean

  def link(keg = nil)
    return nil unless formula.is_a?(PybrewLoadedFormula)

    keg ||= Keg.new(formula.prefix)
    return false unless keg.installed?

    if skip_link?
      optlink = keg.optlink
      if File.symlink?(optlink.to_s)
        return true if File.identical?(optlink.to_s, keg.prefix.to_s)

        FileUtils.rm_f(optlink.to_s)
      elsif optlink.exist?
        return false
      end
      optlink.dirname.mkpath
      FileUtils.ln_s(keg.prefix.to_s, optlink.to_s)
      return true
    end

    keg.link
  end

  # The shim never pours bottles for Formulary-loaded sources: their bottle
  # blocks are recorded as metadata only, so there is no local bottle to pour.
  def pour_bottle?(output_warning: false)
    false
  end

  def pour
    return nil unless formula.is_a?(PybrewLoadedFormula)
    return nil unless pour_bottle?

    raise NotImplementedError,
          "FormulaInstaller#pour cannot pour bottles for Formulary-loaded " \
          "formula sources in pybrew; use pybrew's Python bottle installer"
  end

  def install_bottle_for?(_dep, _build)
    false
  end

  def build_from_source?
    option?(:build_from_source)
  end

  def build_bottle?
    option?(:build_bottle)
  end

  def include_test?
    option?(:include_test)
  end

  def skip_post_install?
    option?(:skip_post_install)
  end

  def skip_link?
    option?(:skip_link)
  end

  def ignore_deps?
    option?(:ignore_deps)
  end

  def force?
    option?(:force)
  end

  def force_bottle?
    option?(:force_bottle)
  end

  def git?
    option?(:git)
  end

  def installed_on_request?
    option?(:installed_on_request)
  end

  def interactive?
    false
  end

  def keep_tmp?
    option?(:keep_tmp)
  end

  def only_deps?
    option?(:only_deps)
  end

  def overwrite?
    option?(:overwrite)
  end

  def quiet?
    option?(:quiet)
  end

  def show_header?
    !option?(:quiet)
  end

  def show_summary_heading?
    !option?(:quiet)
  end

  def verbose?
    option?(:verbose)
  end

  def debug?
    option?(:debug)
  end

  def debug_symbols?
    option?(:debug_symbols)
  end

  def compute_dependencies(use_cache: true)
    return [] unless formula.is_a?(PybrewLoadedFormula)

    seen = {}
    formula.dependencies.each_with_object([]) do |dep, deps|
      name = dep.name.to_s
      next if seen[name]

      seen[name] = true
      deps << dep
    end
  end

  def fetch_dependency(dep)
    unless self.class.fetched.any? { |item| item.to_s == dep.to_s }
      self.class.fetched << dep
    end
    dep
  end

  def fetch_dependencies
    return nil if ignore_deps?

    deps = compute_dependencies.reject do |dep|
      self.class.fetched.any? { |item| item.to_s == dep.to_s }
    end
    return nil if deps.empty?

    deps.each { |dep| fetch_dependency(dep) }
    deps
  end

  # Install Formulary-loaded dependencies recursively.  Dependencies without a
  # loadable install block (or that would recurse into an in-progress install)
  # are recorded in FormulaInstaller.skipped instead of failing the install.
  def install_dependencies(deps = nil)
    deps = compute_dependencies if deps.nil?
    deps.each { |dep| install_dependency(dep) }
    deps
  end

  def install_dependency(dep, dep_formula = nil)
    dep_formula ||= dependency_formula(dep)
    unless installable_dependency?(dep_formula)
      record_skipped_dependency(dep, dep_formula)
      return nil
    end

    name = dep_formula.name.to_s
    if self.class.installing.include?(name)
      record_skipped_dependency(dep, dep_formula)
      return nil
    end

    FormulaInstaller.new(dep_formula, **@options.merge(installed_on_request: false)).install
    unless self.class.installed.any? { |item| item.to_s == dep.to_s }
      self.class.installed << dep
    end
    dep_formula
  end

  def dependency_formula(dep)
    candidate = Formulary.factory(dep.name)
    if !candidate.is_a?(Class) && formula.respond_to?(:path)
      sibling = sibling_dependency_formula(dep.name)
      candidate = sibling unless sibling.nil?
    end
    return candidate.new if candidate.is_a?(Class) && candidate <= PybrewLoadedFormula

    candidate
  rescue ScriptError, StandardError
    nil
  end

  def sibling_dependency_formula(name)
    base_path = formula.respond_to?(:path) ? formula.path : nil
    return nil if base_path.nil? || base_path.to_s.empty?

    sibling = Pathname.new(base_path.to_s).dirname/"#{name}.rb"
    return nil unless sibling.file?

    Formulary.load_formula_from_path(name, sibling)
  end

  def installable_dependency?(dep_formula)
    return false if dep_formula.nil?
    return false unless dep_formula.is_a?(PybrewLoadedFormula)

    klass = dep_formula.class
    return true unless klass.loaded_install_block.nil?

    klass.instance_method(:install).owner != PybrewLoadedFormula
  end

  def record_skipped_dependency(dep, dep_formula = nil)
    record = dep_formula.nil? ? dep : dep_formula
    unless self.class.skipped.any? { |item| item.to_s == record.to_s }
      self.class.skipped << record
    end
    nil
  end

  def check_conflicts
    nil
  end

  def check_requirements(*_args)
    nil
  end

  def check_install_sanity
    nil
  end

  def verify_deps_exist
    nil
  end

  def fresh_install?(_formula = nil)
    false
  end

  def runtime_requirements(_formula = nil)
    []
  end

  def expand_requirements
    []
  end

  def expand_dependencies
    []
  end

  def display_options(_formula = nil)
    ""
  end

  def build_time
    0
  end

  def build_argv
    []
  end

  def sanitized_argv_options
    []
  end

  def caveats
    formula.respond_to?(:caveats) ? formula.caveats : nil
  end

  def summary
    nil
  end

  # -- bounded compatibility surface for formula install hooks -------------
  # These are Homebrew-internal steps; loaded-source installs either no-op or
  # expose the same safe values so formula code can call them without a
  # NoMethodError.  Real bottle/pour/linkage work remains Python-side.
  def add_build_sandbox_rules
    nil
  end

  def api_bottle
    nil
  end

  def audit_installed
    nil
  end

  def auto_link_versioned_keg_only?
    false
  end

  def build_args
    []
  end

  def build_bottle_postinstall
    nil
  end

  def build_bottle_preinstall
    nil
  end

  def check_installation_already_attempted
    raise "Formula is already installed: #{formula.full_name}" if self.class.installed.any? { |item| item.to_s == formula.name.to_s }

    nil
  end

  def create_staging_path
    formula.respond_to?(:buildpath) ? formula.buildpath : formula.prefix
  end

  def determine_bottle_tab_attributes
    {}
  end

  def downloadable
    self
  end

  def effective_build_options_for(_formula = nil)
    options
  end

  def enqueue_bottle_download
    nil
  end

  def enqueue_fetch
    nil
  end

  def expand_dependencies_for_formula(_formula = nil)
    formula.respond_to?(:deps) ? formula.deps : []
  end

  def fetch_fetch_deps
    nil
  end

  def fix_dynamic_linkage
    nil
  end

  def forbidden_formula_check
    nil
  end

  def forbidden_license_check
    nil
  end

  def forbidden_tap_check
    nil
  end

  def formula_path
    formula.respond_to?(:path) ? formula.path : nil
  end

  def install_fetch_deps
    nil
  end

  def install_service
    nil
  end

  def link_manual_command_warning
    nil
  end

  def locked
    nil
  end

  def post_install_formula_path
    formula_path
  end

  def previously_fetched_formula
    nil
  end

  def problem_if_output(_output)
    nil
  end

  def puts_requirement_messages
    nil
  end

  def run_fetch
    nil
  end

  def selected_bottle
    nil
  end

  def unbottled_dependencies
    formula.respond_to?(:deps) ? formula.deps.map(&:name) : []
  end

  def verify_bottle_attestation?
    false
  end

  private

  def option?(name)
    !!(@options[name] || @options[name.to_s])
  end
end

class PybrewFormulaRef
  include PybrewStdArgs

  attr_reader :name

  def initialize(name)
    @name = name.to_s
    @info = (PYBREW_CONFIG["formulae"] || {})[@name] || {}
  end

  def self.homebrew_prefix
    value = PYBREW_CONFIG["homebrew_prefix"] || ENV["HOMEBREW_PREFIX"]
    if value.nil? || value.to_s.empty?
      raise "Formula lookup is not available: HOMEBREW_PREFIX is not set"
    end
    value.to_s
  end

  def self.cellar
    value = PYBREW_CONFIG["cellar"] || ENV["HOMEBREW_CELLAR"]
    if value.nil? || value.to_s.empty?
      raise "Formula lookup is not available: HOMEBREW_CELLAR is not set"
    end
    value.to_s
  end

  # Natural sort key so that, for example, 2.0.1 is newer than 2.0.
  def self.version_key(text)
    text.to_s.scan(/\d+|[A-Za-z]+/).map do |part|
      part =~ /\A\d+\z/ ? [0, part.to_i] : [1, part.downcase]
    end
  end

  def to_s
    name
  end

  def inspect
    "#<PybrewFormulaRef #{name}>"
  end

  def ==(other)
    other.respond_to?(:name) && other.name.to_s == name
  end
  alias eql? ==

  def hash
    name.hash
  end

  def <=>(other)
    name <=> other.name.to_s
  end

  def full_name
    raw = @info["full_name"]
    raw.nil? || raw.to_s.empty? ? name : raw.to_s
  end

  def revision
    raw = @info["revision"]
    raw.nil? ? 0 : raw.to_i
  end

  def installed_kegs
    rack = Pathname.new(File.join(self.class.cellar, name))
    return [] unless rack.directory?

    rack.children.select { |child| child.directory? }
        .map { |child| Keg.new(child) }
        .sort_by { |keg| self.class.version_key(keg.basename.to_s) }
  end

  def installed_prefixes
    installed_kegs.map { |keg| keg.prefix }.sort_by { |path| path.basename.to_s }
  end

  def tab
    Tab.for_name(name)
  end

  def latest_head_version
    value = @info["head_version"]
    value.nil? ? nil : PybrewVersion.new(value.to_s)
  end

  def latest_head_prefix
    version = latest_head_version
    version.nil? ? nil : Pathname.new(self.class.cellar) + name + version.to_s
  end

  def latest_head_pkg_version(*_args, **_kwargs)
    latest_head_version
  end

  def head_version_outdated?(*_args, **_kwargs)
    false
  end

  def outdated_kegs(*_args, **_kwargs)
    current = pkg_version
    return [] if current.nil?

    installed_kegs.reject { |keg| keg.version.to_s == current.to_s }
  end

  def plist_name
    "sh.brew.#{name}"
  end

  def plist_names
    [plist_name, "sh.brew.#{name}", "homebrew.mxcl.#{name}"].uniq
  end

  def service_name
    "sh.brew.#{name}"
  end

  def service_names
    [service_name, "sh.brew.#{name}", "homebrew.#{name}"].uniq
  end

  def launchd_service_paths
    base = any_installed_prefix || opt_prefix
    plist_names.map { |entry| base + "#{entry}.plist" }
  end

  def launchd_service_path
    launchd_service_paths.first
  end

  def systemd_service_paths
    base = any_installed_prefix || opt_prefix
    service_names.map { |entry| base + "#{entry}.service" }
  end

  def systemd_service_path
    systemd_service_paths.first
  end

  def systemd_timer_paths
    base = any_installed_prefix || opt_prefix
    service_names.map { |entry| base + "#{entry}.timer" }
  end

  def systemd_timer_path
    systemd_timer_paths.first
  end

  def latest_installed_keg
    kegs = installed_kegs
    return nil if kegs.empty?

    kegs.sort_by { |keg| self.class.version_key(keg.basename.to_s) }.last
  end

  def opt_prefix
    raw = @info["opt_prefix"]
    raw = File.join(self.class.homebrew_prefix, "opt", name) if raw.nil? || raw.to_s.empty?
    Pathname.new(raw.to_s)
  end

  def prefix
    raw = @info["prefix"]
    if raw.nil? || raw.to_s.empty?
      keg = latest_installed_keg
      if keg.nil?
        raise "Formula[#{name.inspect}] is not installed: no keg found under #{self.class.cellar}/#{name}"
      end
      raw = keg.to_s
    end
    Pathname.new(raw.to_s)
  end

  def rack
    Pathname.new(File.join(self.class.cellar, name))
  end

  def version
    raw = @info["version"]
    if raw.nil? || raw.to_s.empty?
      keg = latest_installed_keg
      raw = keg.nil? ? nil : keg.basename.to_s
    end
    raw.nil? || raw.to_s.empty? ? nil : PybrewVersion.new(raw.to_s)
  end

  def pkg_version
    raw = @info["pkg_version"]
    if raw.nil? || raw.to_s.empty?
      base = version
      return nil if base.nil?

      raw = base.to_s
      raw = "#{raw}_#{revision}" if revision > 0
    end
    PybrewVersion.new(raw.to_s)
  end

  def build
    @build ||= PybrewBuild.new
  end

  def deps
    Array(@info["dependencies"]).map do |dependency|
      if dependency.is_a?(Hash)
        PybrewDependencyRef.new(dependency["name"], dependency["tags"])
      else
        PybrewDependencyRef.new(dependency)
      end
    end
  end

  def dependency_category(key)
    Array(@info[key]).map do |dependency|
      if dependency.is_a?(Hash)
        PybrewDependencyRef.new(dependency["name"], dependency["tags"])
      else
        PybrewDependencyRef.new(dependency)
      end
    end
  end

  def build_dependencies
    dependency_category("build_dependencies")
  end

  def test_dependencies
    dependency_category("test_dependencies")
  end

  def recommended_dependencies
    dependency_category("recommended_dependencies")
  end

  def optional_dependencies
    dependency_category("optional_dependencies")
  end

  def head_dependencies
    dependency_category("head_dependencies")
  end

  def uses_from_macos
    dependency_category("uses_from_macos")
  end

  def requirements
    Array(@info["requirements"]).map do |requirement|
      if requirement.is_a?(Hash)
        PybrewDependencyRef.new(requirement["name"], requirement["tags"])
      else
        PybrewDependencyRef.new(requirement)
      end
    end
  end

  def conflicts
    Array(@info["conflicts_with"])
  end

  def conflicts_with
    conflicts
  end

  def link_overwrite
    Array(@info["link_overwrite"])
  end

  def runtime_dependencies(*_args)
    names = @info["runtime_dependencies"]
    return deps.select { |dependency| dependency.runtime? } if names.nil?

    by_name = {}
    deps.each { |dependency| by_name[dependency.name] = dependency }
    Array(names).map do |dependency_name|
      by_name[dependency_name.to_s] || PybrewDependencyRef.new(dependency_name)
    end
  end

  # Walk the dependency entries known to the bridge recursively.  Only the
  # current formula's declared dependencies and the runtime dependencies
  # recorded in installed receipts are available without loading more formulae.
  def recursive_dependencies(*_args)
    formulae = PYBREW_CONFIG["formulae"] || {}
    seen = {}
    result = []
    queue = deps.dup
    until queue.empty?
      dependency = queue.shift
      next if seen[dependency.name]

      seen[dependency.name] = true
      result << dependency
      entry = formulae[dependency.name] || {}
      Array(entry["dependencies"]).each do |child|
        child_name = child.is_a?(Hash) ? child["name"] : child
        next if child_name.nil?

        child_name = child_name.to_s
        next if seen[child_name]

        child_tags = child.is_a?(Hash) ? child["tags"] : []
        queue << PybrewDependencyRef.new(child_name, child_tags)
      end
    end
    result
  end

  def resources
    result = PybrewResourceList.new
    (@info["resources"] || {}).each do |resource_name, info|
      result << PybrewResource.new(resource_name.to_s, info)
    end
    result
  end

  def resource(resource_name)
    key = resource_name.to_s
    info = (@info["resources"] || {})[key]
    if info.nil?
      raise "resource(#{key.inspect}) is not defined in formula #{self.name}"
    end
    PybrewResource.new(key, info)
  end

  def installed_prefix
    target = pkg_version
    return nil if target.nil?

    match = installed_kegs.find { |keg| keg.basename.to_s == target.to_s }
    return match.prefix unless match.nil?

    raw = @info["prefix"]
    return nil if raw.nil? || raw.to_s.empty?

    path = Pathname.new(raw.to_s)
    return path if path.basename.to_s == target.to_s && path.directory?

    nil
  end

  def installed_version
    path = installed_prefix
    path.nil? ? nil : PybrewVersion.new(path.basename.to_s)
  end

  def installed?
    !installed_prefix.nil?
  end

  def any_version_installed?
    !installed_kegs.empty? || installed?
  end

  def latest_version_installed?
    installed?
  end

  def any_installed_prefix
    installed? ? prefix : nil
  end

  def opt_bin
    opt_prefix + "bin"
  end

  def opt_sbin
    opt_prefix + "sbin"
  end

  def opt_lib
    opt_prefix + "lib"
  end

  def opt_include
    opt_prefix + "include"
  end

  def opt_share
    opt_prefix + "share"
  end

  def opt_pkgshare
    opt_share + name
  end

  def opt_libexec
    opt_prefix + "libexec"
  end

  def bin
    prefix + "bin"
  end

  def sbin
    prefix + "sbin"
  end

  def lib
    prefix + "lib"
  end

  def include
    prefix + "include"
  end

  def share
    prefix + "share"
  end

  def pkgshare
    share + name
  end

  def doc
    share + "doc" + name
  end

  def info
    share + "info"
  end

  def man
    share + "man"
  end

  (1..8).each do |number|
    define_method("man#{number}") do
      man + "man#{number}"
    end
  end

  def elisp
    share + "emacs/site-lisp" + name
  end

  def frameworks
    prefix + "Frameworks"
  end

  def kext_prefix
    prefix + "Library/Extensions"
  end

  def etc
    Pathname.new(@info["homebrew_prefix"].to_s) + "etc"
  end

  def pkgetc
    etc + name
  end

  def var
    Pathname.new(@info["homebrew_prefix"].to_s) + "var"
  end

  def zsh_function
    share + "zsh/site-functions"
  end

  def fish_function
    share + "fish/vendor_functions.d"
  end

  def bash_completion
    prefix + "etc/bash_completion.d"
  end

  def zsh_completion
    share + "zsh/site-functions"
  end

  def fish_completion
    share + "fish/vendor_completions.d"
  end

  def pwsh_completion
    share + "pwsh/completions"
  end

  def bottle_prefix
    prefix + ".bottle"
  end

  def logs
    Pathname.new(@info["homebrew_prefix"].to_s) + "var/log" + name
  end

  def opt_elisp
    opt_prefix + "share/emacs/site-lisp" + name
  end

  def opt_frameworks
    opt_prefix + "Frameworks"
  end

  def pour_bottle?
    true
  end

  def fetch_defined?
    !!@info["fetch?"]
  end

  def post_install_defined?
    !!@info["post_install?"]
  end

  def post_install_steps_defined?
    post_install_defined?
  end

  def oldnames
    oldname.nil? ? [] : [oldname]
  end

  def possible_names
    ([name] + oldnames).uniq
  end

  def linked?
    !linked_keg.nil?
  end

  def linked_version
    keg = linked_keg
    keg.nil? ? nil : PybrewVersion.new(keg.basename.to_s)
  end

  def skip_clean?(*paths)
    patterns = Array(@info["skip_clean"])
    return true if patterns.empty?
    paths.flatten.any? do |candidate|
      text = candidate.to_s
      patterns.any? { |pattern| text == pattern.to_s || text.start_with?(pattern.to_s) }
    end
  end

  def link_overwrite?(*paths)
    patterns = Array(@info["link_overwrite"])
    paths.flatten.any? do |candidate|
      text = candidate.to_s
      patterns.any? { |pattern| text == pattern.to_s || text.start_with?(pattern.to_s) }
    end
  end

  def outdated?(*_args)
    !outdated_kegs.empty?
  end

  def post_install?
    !!@info["post_install?"]
  end

  def post_install
    source = @info["post_install_source"].to_s
    return nil if source.empty?

    paths = {}
    PybrewInstallContext::PATH_METHODS.each do |method_name|
      next unless respond_to?(method_name)

      paths[method_name] = public_send(method_name).to_s
    end
    context = PybrewInstallContext.new(
      "paths" => paths,
      "formula" => { "name" => name, "version" => version.to_s },
      "verbose" => false
    )
    context.instance_eval(source, "post_install", 1)
  end

  def test
    source = @info["test_source"].to_s
    return nil if source.empty?

    paths = {}
    PybrewInstallContext::PATH_METHODS.each do |method_name|
      next unless respond_to?(method_name)

      paths[method_name] = public_send(method_name).to_s
    end
    config = {
      "paths" => paths,
      "formula" => { "name" => name, "version" => version.to_s },
      "verbose" => false,
    }
    PybrewTestContext.new(config, self).instance_eval(source, "test", 1)
  end

  def buildpath
    raw = @info["buildpath"]
    raw.nil? || raw.to_s.empty? ? prefix : Pathname.new(raw.to_s)
  end

  def staged_path
    raw = @info["staged_path"]
    raw.nil? || raw.to_s.empty? ? prefix : Pathname.new(raw.to_s)
  end

  def fetch
    source = @info["fetch_source"].to_s
    return nil if source.empty?

    paths = {
      "prefix" => prefix.to_s,
      "buildpath" => buildpath.to_s,
      "staged_path" => staged_path.to_s,
    }
    PybrewInstallContext::PATH_METHODS.each do |method_name|
      next if paths.key?(method_name)
      next unless respond_to?(method_name)

      paths[method_name] = public_send(method_name).to_s
    end
    context = PybrewInstallContext.new(
      "paths" => paths,
      "formula" => { "name" => name, "version" => version.to_s },
      "verbose" => false
    )
    context.instance_eval(source, "fetch", 1)
  end

  def caveats_with_placeholders
    caveats
  end

  def mktemp(prefix = name, retain: false, retain_in_cache: false, &block)
    directory = Pathname.new(Dir.mktmpdir("#{prefix}-"))
    return directory if block.nil?

    begin
      block.call(directory)
    ensure
      FileUtils.remove_entry(directory) unless retain || retain_in_cache
    end
  end

  def lock
    true
  end

  def unlock
    nil
  end

  def run_test(keep_tmp: false)
    test
  end

  def run_post_install
    post_install
  end

  def run_post_install_steps
    post_install
  end

  def print_tap_action(*_args)
    nil
  end

  def stage(interactive: false, debug_symbols: false, staging_path: nil, staged: false, &block)
    path = staging_path.nil? ? buildpath : Pathname.new(staging_path.to_s)
    return path if block.nil?

    block.call(path)
  end

  def brew(fetch: true, keep_tmp: false, debug_symbols: false, interactive: false,
           staging_path: nil, staged: false, &block)
    self.fetch if fetch
    stage(
      interactive: interactive,
      debug_symbols: debug_symbols,
      staging_path: staging_path,
      staged: staged,
      &block
    )
  end

  def post_install_steps
    []
  end

  def python_major_minor_version
    output = `python3 --version 2>&1`.to_s
    output[/Python (\d+\.\d+)/, 1] || "3.0"
  end

  def xcodebuild(*args)
    PybrewSystem.run!(["xcodebuild", *args], {})
  end

  def exec_cmd(cmd, args, _out = nil, _log_filename = nil)
    PybrewSystem.run!([cmd.to_s, *Array(args)], {})
  end

  def validate_attributes!
    if name.to_s.empty? || name.to_s.match?(/\s/) || !Utils.safe_filename?(name)
      raise "Invalid formula name: #{name.inspect}"
    end
    true
  end

  def determine_active_spec(requested)
    wanted_head = requested.to_sym == :head
    preferred = wanted_head ? head : stable
    fallback = wanted_head ? stable : head
    preferred.url ? preferred : (fallback.url ? fallback : preferred)
  end

  def update_head_version
    nil
  end

  def setup_home(home)
    home = Pathname.new(home.to_s)
    home.mkpath
    (home/".bazelrc").write "startup --output_user_root=#{home}/_bazel"
  end

  def ensure_installed!(**_options)
    raise "#{full_name} is not installed" unless installed?

    true
  end

  def install_etc_var
    nil
  end

  def fetch?
    !!@info["fetch?"]
  end

  def tap_path
    path.dirname.dirname
  end

  def reloadable_ref
    path
  end

  def full_installed_specified_name
    full_name
  end

  def full_installed_alias_name
    name
  end

  def installed_alias_name
    nil
  end

  def installed_alias_path
    nil
  end

  def alias_path
    nil
  end

  def current_installed_alias_target
    nil
  end

  def oldnames_to_migrate
    []
  end

  def migration_needed?
    false
  end

  def new_formula_available?
    false
  end

  def supersedes_an_installed_formula?
    false
  end

  def alias_changed?
    false
  end

  def installed_alias_target_changed?
    false
  end

  def latest_formula
    self
  end

  def old_installed_formulae
    []
  end

  def libexec
    prefix + "libexec"
  end

  def testpath
    prefix + "test"
  end

  def desc
    @info["desc"].to_s
  end

  def homepage
    @info["homepage"].to_s
  end

  def license
    @info["license"]
  end

  def tap
    @info["tap"]
  end

  def path
    raw = @info["path"]
    raw.nil? || raw.to_s.empty? ? nil : Pathname.new(raw.to_s)
  end

  def keg_only?
    !!@info["keg_only"]
  end

  def keg_only_reason
    @info["keg_only_reason"]
  end

  def versioned_formula?
    name.include?("@")
  end

  def unversioned_formula_name
    name.split("@", 2).first
  end

  def versioned_formulae_names
    base = unversioned_formula_name
    rack = Pathname.new(self.class.cellar)
    return [] unless rack.directory?

    rack.children.select do |child|
      child.directory? && child.basename.to_s.start_with?("#{base}@")
    end.map { |child| child.basename.to_s }.sort
  end

  def versioned_formulae
    versioned_formulae_names.map { |formula_name| PybrewFormulaRef.new(formula_name) }
  end

  def linked_keg
    link = opt_prefix
    return nil unless link.symlink?

    Pathname.new(link.readlink).basename.to_s
  rescue StandardError
    nil
  end

  def pinned?
    pin_path.symlink?
  end

  def pinnable?
    !installed_prefixes.empty?
  end

  def pin
    return nil unless pinnable?

    keg = installed_kegs.max_by { |entry| self.class.version_key(entry.basename.to_s) }
    return nil if keg.nil?

    pin_path.dirname.mkpath
    pin_path.make_relative_symlink(keg.prefix) unless pinned?
    pin_path
  end

  def unpin
    pin_path.unlink if pinned?
    begin
      directory = pin_path.dirname
      directory.rmdir if directory.directory? && directory.children.empty?
    rescue StandardError
      nil
    end
    nil
  end

  def pinned_version
    return nil unless pinned?

    PybrewVersion.new(pin_path.realpath.basename.to_s)
  rescue StandardError
    nil
  end

  def pin_path
    HOMEBREW_PINNED_KEGS/name
  end

  def optlinked?
    !linked_keg.nil?
  end

  def spec
    :stable
  end

  def active_spec
    spec
  end

  def active_spec=(sym)
    @active_spec_sym = sym.to_sym
    sym
  end

  def specs
    [stable, head]
  end

  def spec_syms
    [:stable, :head]
  end

  def spec_eval(class_spec, _name = nil)
    spec = class_spec.dup
    return nil if spec.nil? || spec.url.nil?

    spec.owner = self if spec.respond_to?(:owner=)
    spec
  end

  def build=(tab)
    @build = tab
  end

  def stable
    PybrewSourceSpec.new(@info["stable"] || {}, :stable)
  end

  def head
    PybrewSourceSpec.new(@info["head"] || {}, :head)
  end

  def bottle
    raw = @info["bottle"]
    raw.nil? ? nil : PybrewBottleSpec.new(raw)
  end

  def deprecated?
    !!@info["deprecated"]
  end

  def disabled?
    !!@info["disabled"]
  end

  def deprecation_reason
    @info["deprecation_reason"]
  end

  def disable_reason
    @info["disable_reason"]
  end

  def oldname
    @info["oldname"]
  end

  def aliases
    Array(@info["aliases"])
  end

  def patches
    Array(@info["patches"]).map { |entry| PybrewPatchRef.new(entry) }
  end

  def options
    Array(@info["options"]).map(&:to_s)
  end

  def option?(name)
    options.include?(name.to_s)
  end

  def python3
    found = Utils.which("python3")
    found.nil? ? nil : Pathname.new(found)
  end

  def service
    data = @info["service"]
    data.nil? ? nil : PybrewServiceRef.new(data)
  end

  def service?
    !!@info["service?"]
  end

  def caveats?
    !!@info["caveats?"]
  end

  def livecheck?
    !!@info["livecheck?"]
  end

  def caveats
    source = @info["caveats_source"].to_s
    return "" if source.empty?

    PybrewCaveatsContext.new(self).render(source)
  end

  def caveats_source
    @info["caveats_source"].to_s
  end

  def test_source
    @info["test_source"].to_s
  end

  def livecheck
    data = @info["livecheck"]
    data.nil? ? nil : PybrewLivecheck.new(data)
  end

  def livecheck_source
    @info["livecheck_source"].to_s
  end

  def post_install_source
    @info["post_install_source"].to_s
  end

  def fetch_source
    @info["fetch_source"].to_s
  end

  def test?
    !!@info["test?"]
  end

  def stable?
    value = @info["stable"]
    value.is_a?(Hash) && !value["url"].to_s.empty?
  end

  def head?
    value = @info["head"]
    value.is_a?(Hash) && !value["url"].to_s.empty?
  end

  def head_only?
    head? && !stable?
  end

  def bottle?
    value = @info["bottle"]
    value.is_a?(Hash) && !(value["files"] || {}).empty?
  end

  def bottled?
    bottle?
  end

  def bottle_tags
    spec = bottle
    spec.nil? ? [] : spec.bottle_tags
  end

  def bottle_for_tag(tag = nil)
    spec = bottle
    return nil if spec.nil?

    tag.nil? ? spec : spec.bottle_for_tag(tag)
  end

  def bottle_url(tag = nil)
    spec = bottle
    return nil if spec.nil?

    spec.bottle_url(tag.nil? ? spec.current_tag : tag)
  end

  def specified_name
    name
  end

  def full_specified_name
    full_name
  end

  def specified_path
    path
  end

  def deprecated_options
    @info["deprecated_options"] || {}
  end

  def deprecated_option(name)
    deprecated_options[name.to_s]
  end

  def version_scheme
    @info["version_scheme"].to_i
  end

  def preserve_rpath
    !!@info["preserve_rpath"]
  end

  def tap?
    !tap.to_s.empty?
  end

  def core_formula?
    tap.to_s == "homebrew/core"
  end

  def loaded_from_api?
    false
  end

  def loaded_from_internal_api?
    false
  end

  def supports_macos?
    true
  end

  def supports_linux?
    true
  end

  def valid_platform?
    true
  end

  def network_access_allowed?
    !@info["deny_network_access"]
  end

  def autobump?
    true
  end

  def test_defined?
    test?
  end

  def livecheck_defined?
    livecheck?
  end

  def missing_dependencies
    []
  end

  def missing_library_linkage
    []
  end

  def eligible_kegs_for_cleanup
    []
  end

  def any_installed_keg
    latest_installed_keg
  end

  def any_installed_version
    installed_version
  end

  def latest_installed_prefix
    installed_prefix
  end

  def versioned_prefix
    prefix
  end

  def build_flags
    []
  end

  def declared_runtime_dependencies
    runtime_dependencies
  end

  def runtime_formula_dependencies
    runtime_dependencies
  end

  def recursive_requirements
    []
  end

  def serialized_requirements
    []
  end

  def serialized_patches
    []
  end

  def dependencies_hash
    {}
  end

  def urls_hash
    result = {}
    stable_info = @info["stable"] || {}
    unless stable_info["url"].to_s.empty?
      result["stable"] = {
        "url" => stable_info["url"],
        "tag" => stable_info["tag"],
        "revision" => stable_info["revision"],
        "checksum" => stable_info["sha256"],
      }
    end
    head_info = @info["head"] || {}
    unless head_info["url"].to_s.empty?
      result["head"] = {
        "url" => head_info["url"],
        "tag" => head_info["tag"],
        "revision" => head_info["revision"],
        "checksum" => head_info["sha256"],
      }
    end
    result
  end

  def bottle_hash
    bottle = @info["bottle"] || {}
    files = bottle["files"] || {}
    return {} if files.empty?

    {
      "rebuild" => bottle["rebuild"] || 0,
      "root_url" => bottle["root_url"],
      "files" => files.each_with_object({}) do |(tag, info), result|
        result[tag] = {
          "cellar" => info["cellar"],
          "sha256" => info["sha256"],
        }
      end,
    }
  end

  def local_patch_checksums
    patches.each_with_object({}) do |patch, result|
      next if patch.file.nil? || patch.sha256.nil?

      result[patch.file.to_s] = patch.sha256.to_s
    end
  end

  def to_hash
    caveats_text = begin
      caveats
    rescue StandardError
      @info["caveats_source"].to_s
    end
    {
      "name" => name,
      "full_name" => full_name,
      "tap" => tap,
      "oldnames" => oldnames,
      "aliases" => aliases,
      "versioned_formulae" => versioned_formulae.map(&:name),
      "desc" => desc,
      "license" => license,
      "homepage" => homepage,
      "versions" => {
        "stable" => stable? ? version.to_s : nil,
        "head" => head? ? "HEAD" : nil,
        "bottle" => bottle?,
      },
      "urls" => urls_hash,
      "revision" => revision,
      "version_scheme" => version_scheme,
      "bottle" => { "stable" => bottle_hash },
      "keg_only" => keg_only?,
      "keg_only_reason" => keg_only_reason,
      "options" => options,
      "build_dependencies" => build_dependencies.map(&:name),
      "dependencies" => deps.map(&:name),
      "test_dependencies" => test_dependencies.map(&:name),
      "recommended_dependencies" => recommended_dependencies.map(&:name),
      "optional_dependencies" => optional_dependencies.map(&:name),
      "no_linkage_dependencies" => deps.select(&:no_linkage?).map(&:name),
      "local_patch_checksums" => local_patch_checksums,
      "uses_from_macos" => uses_from_macos.map(&:name),
      "requirements" => requirements.map(&:name),
      "conflicts_with" => conflicts_with,
      "caveats" => caveats_text,
      "installed" => installed_kegs.map do |keg|
        tab = keg.tab
        {
          "version" => keg.version.to_s,
          "used_options" => tab.used_options,
          "built_as_bottle" => tab.built_as_bottle?,
          "poured_from_bottle" => tab.poured_from_bottle?,
          "time" => tab.time,
          "runtime_dependencies" => tab.runtime_dependencies,
          "installed_on_request" => tab.installed_on_request?,
        }
      end,
      "linked_keg" => linked_keg,
      "pinned" => pinned?,
      "outdated" => outdated?,
      "deprecated" => deprecated?,
      "deprecation_reason" => deprecation_reason,
      "disabled" => disabled?,
      "disable_reason" => disable_reason,
      "post_install_defined" => post_install_defined?,
      "service" => (service.to_hash if service?),
      "tap_git_head" => tap_git_head,
      "ruby_source_path" => ruby_source_path,
      "ruby_source_checksum" => {},
    }
  end

  def to_hash_with_variations
    to_hash.merge("variations" => {})
  end

  def freeze
    self
  end

  def active_log_prefix
    logs
  end

  def on_system_blocks_exist?
    false
  end

  def url
    stable.url
  end

  def sha256
    stable.sha256
  end

  def mirror
    stable.mirrors
  end

  def system(cmd, *args)
    PybrewSystem.run!([cmd.to_s, *args], {})
  end

  def mkdir(name)
    path = Pathname.new(name.to_s)
    FileUtils.mkdir_p(path.to_s)
    if block_given?
      Dir.chdir(path.to_s) { yield(path) }
    end
    nil
  end

  def inreplace(paths, before = nil, after = nil, audit_result: true, global: true, &block)
    Array(paths).flatten.each do |pattern|
      matches = Dir.glob(pattern.to_s)
      matches = [pattern.to_s] if matches.empty?
      matches.each do |file|
        path = Pathname.new(file)
        raise "inreplace: file not found: #{file}" unless path.file?

        helper = PybrewInreplace.new(path.read)
        if before.nil? && after.nil?
          raise ArgumentError, "inreplace: must supply a block or before/after params" if block.nil?

          block.call(helper)
        elsif global
          helper.gsub!(before, after, audit_result: audit_result)
        else
          helper.sub!(before, after, audit_result: audit_result)
        end
        unless helper.errors.empty?
          raise "inreplace failed for #{file}: #{helper.errors.join('; ')}"
        end
        path.write(helper.inreplace_string)
      end
    end
    nil
  end

  def install
    source = (@info["install_body"] || @info["install_source"]).to_s
    return nil if source.empty?
    return nil if @installing || $pybrew_installing_formula == name

    @installing = true
    paths = {}
    PybrewInstallContext::PATH_METHODS.each do |method_name|
      next unless respond_to?(method_name)

      paths[method_name] = public_send(method_name).to_s
    end
    context = PybrewInstallContext.new(
      "paths" => paths,
      "formula" => { "name" => name, "version" => version.to_s },
      "verbose" => false
    )
    context.instance_eval(source, "install", 1)
  ensure
    @installing = false
  end

  def install_source
    @info["install_source"].to_s
  end

  def depends_on(*_args)
    nil
  end

  def option(*_args)
    nil
  end

  def patch(*_args, &_block)
    nil
  end

  def with_logging(*_args, &block)
    block.call if block
  end

  def common_sandbox_env(home = Pathname.new(Dir.home))
    home = Pathname.new(home.to_s)
    {
      _JAVA_OPTIONS: "-Djava.io.tmpdir=#{HOMEBREW_TEMP}",
      HOME: home.to_s,
      TMPDIR: HOMEBREW_TEMP.to_s,
      TEMP: HOMEBREW_TEMP.to_s,
      TMP: HOMEBREW_TEMP.to_s,
      GIT_TERMINAL_PROMPT: "0",
      GOENV: "off",
      BUNDLE_COOLDOWN: "0",
      PIP_CONFIG_FILE: File::NULL,
      NPM_CONFIG_USERCONFIG: File::NULL,
      PYTHONDONTWRITEBYTECODE: "1",
      XDG_CONFIG_HOME: "#{home}/.config",
    }
  end

  def test_sandbox_env(testpath)
    path = Pathname.new(testpath.to_s)
    common_sandbox_env(path).merge(GIT_CONFIG_GLOBAL: (path/".gitconfig").to_s)
  end

  def test_fixtures
    []
  end

  def pypi_packages
    {}
  end

  def shared_library
    nil
  end

  def rpath
    lib
  end

  def loader_path
    nil
  end

  def compatibility_version
    nil
  end

  def tap!
    tap
  end

  def tap_git_head
    nil
  end

  def no_autobump!(*_args, **_kwargs)
    nil
  end

  def deprecate!(*_args, **_kwargs)
    nil
  end

  def disable!(*_args, **_kwargs)
    nil
  end

  def allow_network_access!
    nil
  end

  def deny_network_access!
    nil
  end

  def keg_only
    !!@info["keg_only"]
  end

  def link_overwrite_reason
    nil
  end

  def link_overwrite_related_formula_names
    []
  end

  def link_overwrite_formulae
    []
  end

  def link_overwrite_formulae_names
    []
  end

  def link_overwrite_keg_name(*_args)
    nil
  end

  def implied_link_overwrite?(*_args)
    false
  end

  def synced_with_other_formulae?
    false
  end

  def ruby_source_checksum
    nil
  end

  def ruby_source_path
    path
  end

  def undeclared_runtime_dependencies
    []
  end

  def runtime_installed_formula_dependents
    []
  end

  def installed_runtime_formula_dependencies
    []
  end

  def merge_spec_dependables(*_args)
    nil
  end

  def add_global_deps_to_spec(*_args)
    nil
  end

  def enqueue_resources_and_patches(*_args)
    nil
  end

  def prepare_patches
    nil
  end

  def fetch_patches
    nil
  end

  def selective_patch(*_args)
    nil
  end

  def change_dylib_id(*_args)
    nil
  end

  def deuniversalize_machos(*_args)
    nil
  end

  def extract_macho_slice_from(*_args)
    nil
  end

  def generate_completions_from_executable(*_args)
    nil
  end

  def formula_namespace
    nil
  end

  def full_name_with_optional_tap(_name)
    full_name
  end

  def formula_names_for_glob(*_args)
    []
  end

  def full_formulae_names
    [full_name]
  end

  def spec_eval(class_spec, _name = nil)
    spec = class_spec.dup
    return nil if spec.nil? || spec.url.nil?

    spec.owner = self if spec.respond_to?(:owner=)
    spec
  end

  def spec_syms
    [:stable, :head]
  end

  def specs
    [stable, head]
  end

  def bottle_tab_attributes
    {}
  end

  def fetch_bottle_tab(*_args)
    nil
  end

  def fails_with
    Array(@info["fails_with"])
  end

  # Homebrew's Formula#compiler_failures: ``fails_with`` metadata as
  # ``CompilerFailure`` objects suitable for CompilerSelector.
  def compiler_failures
    Array(@info["fails_with"]).flat_map do |entry|
      if entry.is_a?(Hash)
        entry.map { |compiler, version| CompilerFailure.create(compiler.to_sym => version) }
      else
        [CompilerFailure.create(entry.to_s.to_sym)]
      end
    end
  end

  def preserve_rpath?
    !!@info["preserve_rpath"]
  end

  def skip_clean(*args)
    @info["skip_clean"] || []
  end
end

class PybrewLivecheck
  attr_reader :url, :regex, :strategy

  def initialize(data)
    @data = data || {}
    @url = @data["url"]
    @regex = @data["regex"].nil? ? nil : Regexp.new(@data["regex"].to_s)
    raw_strategy = @data["strategy"]
    @strategy = raw_strategy.nil? ? nil : raw_strategy.to_sym
  end

  def skip?
    !!@data["skip"]
  end

  def skip_reason
    @data["skip"].is_a?(String) ? @data["skip"] : nil
  end

  def source
    @data["source"].to_s
  end

  def to_s
    url.nil? ? source : url.to_s
  end

  def inspect
    "#<PybrewLivecheck #{self}>"
  end
end

class Keg
  class LinkError < RuntimeError
    attr_reader :keg, :src, :dst

    def initialize(keg, src, dst, cause)
      @keg = keg
      @src = src
      @dst = dst
      super(cause.respond_to?(:message) ? cause.message : cause.to_s)
    end
  end

  class ConflictError < LinkError
    def suggestion
      conflict = Keg.for(dst)
      "is a symlink belonging to #{conflict.name}. You can unlink it:\n" \
        "  brew unlink #{conflict.name}\n"
    rescue StandardError
      "already exists. You may want to remove it:\n  rm '#{dst}'\n"
    end

    def to_s
      "Could not symlink #{src}\nTarget #{dst} #{suggestion}\n" \
        "To force the link and overwrite all conflicting files:\n" \
        "  brew link --overwrite #{keg.name}\n"
    end
  end
  LINK_DIRS = %w[bin etc include lib sbin share var].freeze
  KEEPME_FILE = ".keepme"

  attr_reader :path

  def self.keg_link_directories
    LINK_DIRS
  end

  def self.must_exist_subdirectories
    (LINK_DIRS - ["var"] + ["opt", "var/homebrew/linked"])
      .map { |dir| HOMEBREW_PREFIX/dir }
      .sort
      .uniq
  end

  def self.must_exist_directories
    (must_exist_subdirectories + [HOMEBREW_CELLAR]).sort.uniq
  end

  def self.must_be_writable_directories
    must_exist_subdirectories
  end

  def self.for(path)
    original = Pathname.new(path.to_s)
    raise Errno::ENOENT, original.to_s unless original.exist?

    current = original.realpath
    cellar = Pathname.new(HOMEBREW_CELLAR).realpath
    until current.root?
      return new(current) if current.parent.parent == cellar

      current = current.parent.realpath
    end
    raise "#{original} is not inside a keg"
  rescue Errno::ENOENT
    raise
  rescue StandardError => error
    raise error if error.is_a?(RuntimeError) && error.message.include?("not inside a keg")

    raise "#{original} is not inside a keg"
  end

  def self.from_rack(rack)
    rack = Pathname.new(rack.to_s)
    return nil unless rack.directory?

    kegs = rack.children.select { |child| child.directory? }.map { |child| new(child) }
    return nil if kegs.empty?

    kegs.find(&:linked?) || kegs.find(&:optlinked?) ||
      kegs.max_by { |keg| PybrewFormulaRef.version_key(keg.basename.to_s) }
  end

  def self.all
    cellar = Pathname.new(HOMEBREW_CELLAR)
    return [] unless cellar.directory?

    cellar.children.select { |rack| rack.directory? }
          .flat_map { |rack| rack.children.select { |child| child.directory? } }
          .map { |child| new(child) }
          .sort_by { |keg| [keg.name, PybrewFormulaRef.version_key(keg.basename.to_s)] }
  end

  def initialize(path)
    @path = Pathname.new(path.to_s)
  end

  def rack
    @path.dirname
  end

  def name
    rack.basename.to_s
  end

  def version
    @path.basename.to_s
  end

  def prefix
    @path
  end

  def homebrew_prefix
    return rack.dirname.dirname if rack.dirname.basename.to_s == "Cellar"

    Pathname.new(PybrewFormulaRef.homebrew_prefix)
  end

  def optlink(*args, **options)
    verbose = options.fetch(:verbose, args[0] || false)
    dry_run = options.fetch(:dry_run, args[1] || false)
    overwrite = options.fetch(:overwrite, args[2] || false)
    target = homebrew_prefix/"opt"/name
    if verbose || dry_run || overwrite
      return nil if dry_run

      target.dirname.mkpath
      if File.symlink?(target.to_s) && !File.identical?(target.to_s, @path.to_s)
        return nil unless overwrite

        FileUtils.rm_f(target.to_s)
      end
      target.make_symlink(@path.to_s) unless File.identical?(target.to_s, @path.to_s)
      aliases.each do |alias_name|
        alias_target = target.dirname/alias_name.to_s
        next if File.identical?(alias_target.to_s, @path.to_s)

        FileUtils.rm_f(alias_target.to_s) if overwrite
        alias_target.make_symlink(@path.to_s) unless alias_target.symlink?
      end
      oldname_opt_records.each do |record|
        FileUtils.rm_f(record.to_s) if record.symlink?
        record.make_symlink(@path.to_s) unless record.symlink?
      end
      return nil
    end
    target
  end

  def opt_record
    optlink
  end

  def linked_keg_record
    homebrew_prefix/"var/homebrew/linked"/name
  end

  def installed?
    @path.directory?
  end

  def linked?
    return false unless File.symlink?(linked_keg_record.to_s)

    File.identical?(linked_keg_record.to_s, @path.to_s)
  end

  def optlinked?
    return false unless File.symlink?(optlink.to_s)

    File.identical?(optlink.to_s, @path.to_s)
  end

  def link
    raise "Keg does not exist: #{@path}" unless installed?

    desired = desired_links
    conflicts = desired.select do |target, source|
      next false unless target.exist? || target.symlink?
      next false if target.symlink? && same_file?(target, source)

      true
    end
    unless conflicts.empty?
      listing = conflicts.first(10).map { |target, _source| "  #{target}" }.join("\n")
      raise "cannot link #{name}: #{conflicts.length} conflict(s):\n#{listing}"
    end

    FileUtils.mkdir_p(optlink.dirname.to_s)
    if File.symlink?(optlink.to_s) && !File.identical?(optlink.to_s, @path.to_s)
      FileUtils.rm_f(optlink.to_s)
    end
    if optlink.exist? && !File.identical?(optlink.to_s, @path.to_s)
      return false
    end
    FileUtils.ln_s(@path.to_s, optlink.to_s) unless optlink.exist?

    record = linked_keg_record
    FileUtils.mkdir_p(record.dirname.to_s)
    if File.symlink?(record.to_s) && !File.identical?(record.to_s, @path.to_s)
      FileUtils.rm_f(record.to_s)
    end
    FileUtils.ln_s(@path.to_s, record.to_s) unless File.symlink?(record.to_s)

    desired.each do |target, source|
      next if target.exist? || target.symlink?

      target.make_relative_symlink(source)
    end
    true
  end

  def uninstall(raise_failures: false)
    return false unless @path.directory?

    unlink
    FileUtils.rm_rf(@path.to_s)
    rack = @path.parent
    begin
      rack.rmdir if rack.directory? && rack.children.empty?
    rescue StandardError
      nil
    end
    true
  rescue StandardError
    raise if raise_failures

    false
  end

  def unlink
    removed = false
    LINK_DIRS.each do |directory|
      root = homebrew_prefix/directory
      next unless root.directory?

      entries = Dir.glob("#{root}/**/*", File::FNM_DOTMATCH)
      entries.sort_by { |entry| -entry.length }.each do |entry|
        target = Pathname.new(entry)
        next unless target.symlink?
        next unless same_file?(target, @path) || link_targets_keg?(target)

        target.unlink
        removed = true
        prune_empty_parents(target.dirname, root)
      end
    end

    if File.symlink?(optlink.to_s) && File.identical?(optlink.to_s, @path.to_s)
      FileUtils.rm_f(optlink.to_s)
      removed = true
    end

    if linked?
      remove_linked_keg_record
      removed = true
    end
    removed
  end

  def keepme_refs
    keepme = @path/KEEPME_FILE
    return [] unless keepme.file? && keepme.readable?

    keepme.readlines.map(&:strip).reject(&:empty?).select { |ref| File.exist?(ref) }
  end

  def binary_executable_or_library_files
    []
  end

  # Ad-hoc/custom codesign after relocation.  Uses the same bounded helpers as
  # ``relocate_binary_files`` and returns a verification/entitlements result.
  def codesign_patched_binary(file)
    tool = codesign_tool
    return { "file" => file.to_s, "signed" => false, "reason" => "codesign tool not found" } if tool.nil?

    begin
      Utils.popen_read(*codesign_sign_command(file), safe: true)
    rescue ScriptError, StandardError => error
      return { "file" => file.to_s, "signed" => false,
               "reason" => "#{error.class}: #{error.message}" }
    end
    result = verify_codesign_signature(file)
    result["signed"] = true
    result["identity"] = codesign_identity
    result["entitlements"] = codesign_entitlements(file)
    result
  end

  def codesign_patched_binaries(files)
    Array(files).map { |file| codesign_patched_binary(file) }
  end

  def remove_opt_record
    return false unless File.symlink?(optlink.to_s)

    optlink.unlink
    begin
      optlink.dirname.rmdir if optlink.dirname.directory? && optlink.dirname.children.empty?
    rescue StandardError
      nil
    end
    true
  end

  def remove_linked_keg_record
    record = homebrew_prefix/"var/homebrew/linked"/name
    return false unless File.symlink?(record.to_s)

    record.unlink
    begin
      record.dirname.rmdir if record.dirname.directory? && record.dirname.children.empty?
    rescue StandardError
      nil
    end
    true
  end

  def oldname_opt_records
    opt_dir = homebrew_prefix/"opt"
    return [] unless opt_dir.directory?

    opt_dir.children.select do |entry|
      entry.symlink? && entry != optlink && entry.realpath.parent == @path.parent.realpath
    end
  rescue StandardError
    []
  end

  def remove_oldname_opt_records
    oldname_opt_records.each do |record|
      record.unlink if record.realpath == @path.realpath
    rescue StandardError
      nil
    end
    nil
  end

  def remove_old_aliases(versioned_aliases: aliases)
    opt_dir = homebrew_prefix/"opt"
    linked_dir = linked_keg_record.parent
    alias_list = Array(versioned_aliases).map(&:to_s)
    alias_list.each do |alias_name|
      next if alias_name.include?("@")

      remove_alias_symlink_at(opt_dir/alias_name)
      remove_alias_symlink_at(linked_dir/alias_name)
    end

    Dir.glob("#{opt_dir}/#{name}@*").sort.each do |entry|
      link = Pathname.new(entry)
      alias_name = link.basename.to_s
      next if alias_list.include?(alias_name) && File.exist?(link.to_s)

      remove_alias_symlink_at(link)
      remove_alias_symlink_at(linked_dir/alias_name)
    end
    nil
  end

  def link_dir(relative_dir, verbose: false, dry_run: false, overwrite: false, &_block)
    directory = relative_dir.to_s
    root = @path/directory
    return false unless root.directory?

    base = homebrew_prefix/directory
    links = desired_links.select { |target, _source| target.to_s.start_with?("#{base}/") }
    links.each do |target, source|
      if target.exist? || target.symlink?
        next if target.symlink? && same_file?(target, source)
        return false unless overwrite

        FileUtils.rm_f(target.to_s)
      end
      target.make_relative_symlink(source) unless dry_run
    end
    true
  end

  def desired_links
    result = []
    LINK_DIRS.each do |directory|
      root = @path/directory
      next unless root.directory?

      Dir.glob("#{root}/**/*", File::FNM_DOTMATCH).each do |entry|
        source = Pathname.new(entry)
        next unless source.file? || source.symlink?

        relative = source.relative_path_from(root)
        next if relative.to_s.empty?

        result << [homebrew_prefix/directory/relative, source]
      end
    end
    result
  end

  def same_file?(left, right)
    File.identical?(left.to_s, right.to_s)
  rescue StandardError
    false
  end

  def link_targets_keg?(target)
    resolved = target.realpath.to_s
    keg_root = @path.realpath.to_s
    resolved == keg_root || resolved.start_with?("#{keg_root}/")
  rescue StandardError
    false
  end

  def prune_empty_parents(directory, root)
    current = directory
    while current != root && current.directory? && current.children.empty?
      current.rmdir
      current = current.dirname
    end
  rescue StandardError
    nil
  end

  def formula
    Formula[name]
  end

  def tab
    Tab.for_keg(self)
  end

  # Whether neither the API nor a local tap provides this keg's formula any
  # more.  Bounded: checks the configured API cache and tap Formula/ files;
  # an installed keg with no resolvable source is considered deleted.
  def deleted_formula?
    tap_name = tab.tap.to_s
    reference = tap_name.empty? ? name.to_s : "#{tap_name}/#{name}"
    begin
      return false unless Formulary.formula_class_get_from_api(reference).nil?
    rescue PybrewFormulaUnavailableError
      nil
    end
    return false if Formulary.tap_roots.any? do |root|
      root = Pathname.new(root.to_s)
      (root/"Formula"/"#{name}.rb").file? ||
        (root/"Formula"/"#{name}.rb").symlink? ||
        (root/"Aliases"/"#{name}.rb").exist?
    end
    true
  end

  # Whether the installed keg's formula opts into preserve_rpath.  Loads the
  # keg's own recipe first (Homebrew keg_relocate semantics); falls back to
  # the formula reference and then false.
  def formula_preserve_rpath?
    installed_formula = path/".brew"/"#{name}.rb"
    formula = if installed_formula.file?
      Formulary.from_path(installed_formula)
    else
      Formula[name]
    end
    !!formula.preserve_rpath?
  rescue ScriptError, StandardError
    false
  end

  def version_scheme
    tab.version_scheme
  end

  def scheme_and_version
    [version_scheme, version]
  end

  def to_formula
    Formula[name]
  end

  def runtime_dependencies
    tab.runtime_dependencies
  end

  def aliases
    Array(tab.aliases).map(&:to_s).reject do |alias_name|
      alias_name.include?("/") || ["", ".", ".."].include?(alias_name)
    end
  end

  def delete_pyc_files!
    deleted = 0
    Dir.glob((@path/"**/*").to_s, File::FNM_DOTMATCH).each do |entry|
      path = Pathname.new(entry)
      if path.directory? && path.basename.to_s == "__pycache__"
        FileUtils.rm_rf(path.to_s)
        deleted += 1
      elsif path.file? && [".pyc", ".pyo"].include?(path.extname)
        FileUtils.rm_f(path.to_s)
        deleted += 1
      end
    end
    deleted
  end

  def remove_alias_symlink(alias_name)
    remove_alias_symlink_at(homebrew_prefix/"opt"/alias_name.to_s)
  end

  def remove_alias_symlink_at(path)
    path = Pathname.new(path.to_s)
    return false unless File.symlink?(path.to_s)

    target = path.realpath
    resolved_keg = @path.realpath
    resolved_parent = @path.parent.realpath
    return false unless target == resolved_keg || target.parent == resolved_parent

    path.unlink
    true
  rescue StandardError
    false
  end

  def consistent_reproducible_symlink_permissions!
    true
  end

  def normalize_pod2man_outputs!
    []
  end

  def empty_installation?
    @path.directory? && @path.children.empty?
  end

  def require_relocation!
    @require_relocation = true
  end

  def require_relocation?
    !!@require_relocation
  end

  def relocation_repository_path
    defined?(HOMEBREW_REPOSITORY) ? HOMEBREW_REPOSITORY : HOMEBREW_PREFIX
  end

  def relocation_placeholder_pairs
    pairs = [
      [HOMEBREW_PREFIX.to_s, "@@HOMEBREW_PREFIX@@"],
      [HOMEBREW_CELLAR.to_s, "@@HOMEBREW_CELLAR@@"],
    ]
    repository = relocation_repository_path.to_s
    pairs << [repository, "@@HOMEBREW_REPOSITORY@@"] unless repository == HOMEBREW_PREFIX.to_s
    pairs
  end

  def relocation_mapping(replacements = {})
    mapping = {}
    if replacements.is_a?(Hash)
      replacements.each { |from, to| mapping[from.to_s] = to.to_s }
    end
    if mapping.empty?
      relocation_placeholder_pairs.each { |from, to| mapping[from] = to }
    end
    mapping
  end

  def relocation_text_files(files = nil)
    entries = if files.nil?
      Dir.glob((@path/"**/*").to_s, File::FNM_DOTMATCH)
    else
      Array(files).map(&:to_s)
    end
    entries.map { |entry| Pathname.new(entry) }.select do |path|
      next false if path.symlink?
      next false unless path.file?
      next false if path.to_s.include?("/.git/")
      next false if path.size > 5 * 1024 * 1024
      next false if %w[
        .o .a .so .dylib .pyc .pyo .class .jar .png .jpg .jpeg .gif .zip
        .gz .bz2 .xz .tar .pdf
      ].include?(path.extname.downcase)

      true
    end
  end

  def relocation_text_file?(path)
    data = path.binread
    return false if data.include?("\0")

    data.dup.force_encoding(Encoding::UTF_8).valid_encoding?
  rescue ScriptError, StandardError
    false
  end

  def relocation_report(replacements = {}, files: nil)
    mapping = relocation_mapping(replacements)
    details = {}
    relocation_text_files(files).each do |path|
      next unless relocation_text_file?(path)

      text = path.binread
      count = mapping.keys.reject(&:empty?).reduce(0) do |sum, from|
        sum + text.scan(from).length
      end
      details[path.to_s] = count if count > 0
    end
    {
      "files" => details.keys,
      "matches" => details.values.reduce(0, :+),
      "details" => details,
      "dry_run" => true,
    }
  end

  def relocate(replacements = {}, dry_run: false, files: nil)
    mapping = relocation_mapping(replacements)
    changed = []
    replacements_count = 0
    relocation_text_files(files).each do |path|
      next unless relocation_text_file?(path)

      original = path.binread
      text = original
      mapping.sort_by { |from, _to| -from.length }.each do |from, to|
        next if from.empty?

        text = text.gsub(from, to)
      end
      next if text == original

      replacements_count += mapping.keys.reject(&:empty?).reduce(0) do |sum, from|
        sum + original.scan(from).length
      end
      changed << path.to_s
      path.binwrite(text) unless dry_run
    end
    {
      "files" => changed,
      "replacements" => replacements_count,
      "dry_run" => !!dry_run,
    }
  end

  def relocate_text_files(replacements = {}, dry_run: false, files: nil)
    relocate(replacements, dry_run: dry_run, files: files)
  end

  def relocate_to_placeholders(dry_run: false, files: nil)
    relocate({}, dry_run: dry_run, files: files)
  end

  def relocate_from_placeholders(dry_run: false, files: nil)
    mapping = {}
    relocation_placeholder_pairs.each { |from, to| mapping[to] = from }
    relocate(mapping, dry_run: dry_run, files: files)
  end

  BINARY_MACHO_MAGICS = [
    [0xFE, 0xED, 0xFA, 0xCE], [0xCE, 0xFA, 0xED, 0xFE],
    [0xFE, 0xED, 0xFA, 0xCF], [0xCF, 0xFA, 0xED, 0xFE],
    [0xCA, 0xFE, 0xBA, 0xBE], [0xBE, 0xBA, 0xFE, 0xCA],
  ].freeze
  BINARY_ELF_MAGIC = [0x7F, 0x45, 0x4C, 0x46].freeze

  MACHO_CPU_ARCHS = {
    0x01000007 => "x86_64", 0x0100000C => "arm64",
    7 => "i386", 12 => "arm", 18 => "ppc", 0x01000012 => "ppc64",
  }.freeze
  ELF_MACHINE_ARCHS = {
    62 => "x86_64", 183 => "aarch64", 3 => "i386", 40 => "arm",
    20 => "ppc", 21 => "ppc64", 8 => "mips", 243 => "riscv",
  }.freeze

  def binary_info(path)
    pathname = Pathname.new(path.to_s)
    info = {
      "path" => pathname.to_s,
      "kind" => "unknown",
      "format" => nil,
      "arch" => nil,
      "bits" => nil,
      "fat" => false,
      "executable" => File.executable?(pathname.to_s),
      "symlink" => pathname.symlink?,
      "size" => (pathname.size rescue nil),
    }
    bytes = pathname.binread(64).bytes
    if bytes[0, 4] == BINARY_ELF_MAGIC
      info["kind"] = "elf"
      elf_class = bytes[4]
      info["bits"] = elf_class == 1 ? 32 : (elf_class == 2 ? 64 : nil)
      info["format"] = elf_class == 1 ? "elf32" : "elf64"
      info["arch"] = ELF_MACHINE_ARCHS[bytes[18].to_i | (bytes[19].to_i << 8)]
    elsif BINARY_MACHO_MAGICS.include?(bytes[0, 4])
      info["kind"] = "macho"
      magic = bytes[0, 4]
      fat = [[0xCA, 0xFE, 0xBA, 0xBE], [0xBE, 0xBA, 0xFE, 0xCA]].include?(magic)
      info["fat"] = fat
      if fat
        info["format"] = magic.first == 0xCA ? "fat32" : "fat64"
      else
        big_endian = magic == [0xFE, 0xED, 0xFA, 0xCE] || magic == [0xFE, 0xED, 0xFA, 0xCF]
        if big_endian
          cputype = (bytes[4].to_i << 24) | (bytes[5].to_i << 16) | (bytes[6].to_i << 8) | bytes[7].to_i
        else
          cputype = bytes[4].to_i | (bytes[5].to_i << 8) | (bytes[6].to_i << 16) | (bytes[7].to_i << 24)
        end
        bits = magic == [0xFE, 0xED, 0xFA, 0xCF] || magic == [0xCF, 0xFA, 0xED, 0xFE] ? 64 : 32
        info["bits"] = bits
        info["format"] = bits == 64 ? "macho64" : "macho32"
        info["arch"] = MACHO_CPU_ARCHS[cputype]
      end
    end
    info
  rescue ScriptError, StandardError
    { "path" => path.to_s, "kind" => "unknown", "format" => nil,
      "arch" => nil, "bits" => nil, "fat" => false, "executable" => false,
      "symlink" => false, "size" => nil }
  end

  def binary_kind(path)
    binary_info(path)["kind"].to_sym
  end

  # Inode-aware file list: hardlinked binaries are patched once (patching the
  # same inode twice would duplicate install_name_tool/patchelf work).
  def binary_relocation_targets(files = nil)
    entries = if files.nil?
      Dir.glob((@path/"**/*").to_s, File::FNM_DOTMATCH)
    else
      Array(files).map(&:to_s)
    end
    seen = {}
    targets = []
    skipped = []
    entries.map { |entry| Pathname.new(entry) }.select do |path|
      next false if path.symlink?
      next false unless path.file?
      next false if path.to_s.include?("/.git/")

      %i[macho elf].include?(binary_kind(path))
    end.sort_by(&:to_s).each do |path|
      stat = begin
        path.stat
      rescue StandardError
        nil
      end
      key = stat.nil? ? path.to_s : [stat.dev, stat.ino]
      if !stat.nil? && seen.key?(key)
        skipped << { "file" => path.to_s, "reason" => "hardlink",
                     "hardlink_of" => seen[key] }
        next
      end
      seen[key] = path.to_s unless stat.nil?
      targets << path
    end
    [targets, skipped]
  end

  def binary_relocation_files(files = nil)
    entries = if files.nil?
      Dir.glob((@path/"**/*").to_s, File::FNM_DOTMATCH)
    else
      Array(files).map(&:to_s)
    end
    entries.map { |entry| Pathname.new(entry) }.select do |path|
      next false if path.symlink?
      next false unless path.file?
      next false if path.to_s.include?("/.git/")

      %i[macho elf].include?(binary_kind(path))
    end
  end

  def binary_tool_override(env_name)
    return [false, nil] unless ENV.key?(env_name) && !ENV[env_name].to_s.empty?

    path = Pathname.new(ENV[env_name].to_s)
    [true, path.file? && path.executable? ? path.to_s : nil]
  end

  def binary_relocation_tool(kind)
    env_name = kind == :macho ? "PYBREW_INSTALL_NAME_TOOL" : "PYBREW_PATCHELF"
    provided, override = binary_tool_override(env_name)
    return override if provided

    kind == :macho ? Utils.which("install_name_tool") : Utils.which("patchelf")
  end

  def codesign_tool
    if ENV.key?("PYBREW_CODESIGN") && !ENV["PYBREW_CODESIGN"].to_s.empty?
      path = Pathname.new(ENV["PYBREW_CODESIGN"].to_s)
      return path.file? && path.executable? ? path.to_s : nil
    end

    Utils.which("codesign")
  end

  def codesign_identity
    value = ENV["PYBREW_CODESIGN_IDENTITY"].to_s.strip
    value.empty? ? "-" : value
  end

  CODESIGN_PRESERVE_METADATA = "entitlements,requirements,flags,runtime"

  def codesign_sign_command(path)
    [codesign_tool, "--force", "--sign", codesign_identity,
     "--preserve-metadata=#{CODESIGN_PRESERVE_METADATA}", path.to_s]
  end

  def codesign_verify_command(path)
    [codesign_tool, "--verify", "--strict", path.to_s]
  end

  # Verify an existing signature (bounded local `codesign --verify` call).
  def verify_codesign_signature(path, strict: false)
    tool = codesign_tool
    return { "file" => path.to_s, "verified" => false, "reason" => "codesign tool not found" } if tool.nil?

    begin
      output = Utils.popen_read(*codesign_verify_command(path), safe: true)
      { "file" => path.to_s, "verified" => true, "output" => output.to_s }
    rescue ScriptError, StandardError => error
      result = { "file" => path.to_s, "verified" => false,
                 "reason" => "#{error.class}: #{error.message}" }
      raise "codesign verification failed for #{path}: #{result["reason"]}" if strict
      result
    end
  end

  # Capture a binary's entitlements (XML plist text) without parsing Apple's
  # full plist grammar; ``present`` is true when codesign reports a dict.
  def codesign_entitlements(path)
    tool = codesign_tool
    return { "file" => path.to_s, "present" => false, "raw" => "", "reason" => "codesign tool not found" } if tool.nil?

    begin
      output = Utils.popen_read(tool, "-d", "--entitlements", ":-", path.to_s).to_s
    rescue ScriptError, StandardError => error
      return { "file" => path.to_s, "present" => false, "raw" => "",
               "reason" => "#{error.class}: #{error.message}" }
    end
    { "file" => path.to_s, "present" => output.include?("<dict>") || output.include?("<key>"),
      "raw" => output }
  end

  def expand_relocation_placeholders(value)
    text = value.to_s
    relocation_placeholder_pairs.each { |path, placeholder| text = text.gsub(placeholder, path) }
    text
  end

  def retarget_symlinks(mapping = {}, dry_run: false, strict: false)
    report = { "files" => [], "changes" => [], "skipped" => [], "dry_run" => !!dry_run }
    keg_root = @path.expand_path
    (mapping || {}).each do |link_key, target|
      link_path = Pathname.new(link_key.to_s)
      link_path = @path/link_path unless link_path.absolute?
      expanded = link_path.expand_path
      unless expanded.to_s == keg_root.to_s || expanded.to_s.start_with?("#{keg_root}/")
        report["skipped"] << { "file" => link_key.to_s, "reason" => "outside keg" }
        next
      end
      unless File.symlink?(expanded.to_s)
        report["skipped"] << { "file" => link_key.to_s, "reason" => "not a symlink" }
        next
      end
      new_target = expand_relocation_placeholders(target)
      report["files"] << expanded.to_s
      report["changes"] << {
        "file" => expanded.to_s,
        "from" => File.readlink(expanded.to_s),
        "to" => new_target,
      }
      unless dry_run
        expanded.unlink
        File.symlink(new_target, expanded.to_s)
      end
    end
    if strict && !report["skipped"].empty?
      skipped = report["skipped"].first
      raise RuntimeError,
            "symlink retargeting skipped #{skipped["file"]}: #{skipped["reason"]}"
    end
    report
  end

  def binary_relocation_plan(replacements = {}, files: nil, rpath_additions: [],
                             rpath_deletions: [], codesign: false,
                             dylib_id: nil, dylib_ids: nil, interpreter: nil,
                             rpath_set: nil)
    mapping = relocation_mapping(replacements)
    plan = {
      "files" => [],
      "commands" => [],
      "skipped" => [],
      "matrix" => [],
      "hardlink_skipped" => [],
      "codesign_files" => [],
      "codesign_commands" => [],
      "codesign_verify_commands" => [],
      "codesign_skipped" => [],
    }
    targets, hardlink_skipped = binary_relocation_targets(files)
    plan["hardlink_skipped"] = hardlink_skipped
    targets.each do |path|
      info = binary_info(path)
      kind = info["kind"].to_sym
      tool = binary_relocation_tool(kind)
      entry = info.merge(
        "tool" => tool,
        "commands" => [],
        "codesign" => nil
      )
      if tool.nil?
        plan["skipped"] << {
          "file" => path.to_s,
          "kind" => kind.to_s,
          "reason" => "relocation tool not found",
        }
        entry["skipped"] = "relocation tool not found"
        plan["matrix"] << entry
        next
      end
      commands = []
      if kind == :macho
        mapping.each { |old, new| commands << [tool, "-change", old, new, path.to_s] }
        entry["dylib_id"] = if dylib_ids.is_a?(Hash)
          dylib_ids[path.basename.to_s] || dylib_ids[path.basename(".dylib").to_s]
        else
          dylib_id
        end
        commands << [tool, "-id", entry["dylib_id"].to_s, path.to_s] unless entry["dylib_id"].nil?
        Array(rpath_deletions).each do |value|
          commands << [tool, "-delete_rpath", value.to_s, path.to_s]
        end
        Array(rpath_additions).each do |value|
          commands << [tool, "-add_rpath", value.to_s, path.to_s]
        end
      else
        mapping.each do |old, new|
          commands << [tool, "--replace-needed", old, new, path.to_s]
        end
        entry["interpreter"] = if interpreter.is_a?(Hash)
          interpreter[path.basename.to_s]
        else
          interpreter
        end
        commands << [tool, "--set-interpreter", entry["interpreter"].to_s, path.to_s] unless entry["interpreter"].nil?
        if !rpath_set.nil?
          Array(rpath_set).each do |value|
            commands << [tool, "--set-rpath", value.to_s, path.to_s]
          end
        elsif !Array(rpath_additions).empty?
          Array(rpath_additions).each do |value|
            commands << [tool, "--set-rpath", value.to_s, path.to_s]
          end
        elsif !Array(rpath_deletions).empty?
          commands << [tool, "--remove-rpath", path.to_s]
        end
      end
      entry["commands"] = commands
      plan["matrix"] << entry
      next if commands.empty?

      if kind == :macho && codesign
        sign_tool = codesign_tool
        if sign_tool.nil?
          plan["codesign_skipped"] << {
            "file" => path.to_s,
            "reason" => "codesign tool not found",
          }
          entry["codesign"] = "tool not found"
        else
          plan["codesign_files"] << path.to_s
          plan["codesign_commands"] << codesign_sign_command(path)
          plan["codesign_verify_commands"] << codesign_verify_command(path)
          entry["codesign"] = codesign_identity
        end
      end
      plan["files"] << path.to_s
      plan["commands"].concat(commands)
    end
    plan
  end

  def binary_relocation_report(replacements = {}, files: nil, rpath_additions: [],
                               rpath_deletions: [], codesign: false, **options)
    binary_relocation_plan(
      replacements,
      files: files,
      rpath_additions: rpath_additions,
      rpath_deletions: rpath_deletions,
      codesign: codesign,
      **options
    )
  end

  def relocate_binary_files(replacements = {}, dry_run: false, files: nil,
                            strict: false, rpath_additions: [], rpath_deletions: [],
                            codesign: false, ignore_failures: false,
                            verify_placeholders: false, **options)
    plan = binary_relocation_plan(
      replacements,
      files: files,
      rpath_additions: rpath_additions,
      rpath_deletions: rpath_deletions,
      codesign: codesign,
      **options
    )
    if strict && !plan["skipped"].empty?
      skipped = plan["skipped"].first
      raise RuntimeError,
            "binary relocation tool unavailable for #{skipped["file"]} " \
            "(#{skipped["kind"]}); install install_name_tool/patchelf or set " \
            "PYBREW_INSTALL_NAME_TOOL/PYBREW_PATCHELF"
    end
    if codesign && strict && !plan["codesign_skipped"].empty?
      skipped = plan["codesign_skipped"].first
      raise RuntimeError,
            "codesign tool unavailable for #{skipped["file"]}; set PYBREW_CODESIGN " \
            "or install codesign"
    end
    executed = []
    failed = []
    unless dry_run
      plan["matrix"].each do |entry|
        next if entry["commands"].empty?

        path = entry["path"]
        original = begin
          Pathname.new(path).binread
        rescue ScriptError, StandardError
          nil
        end
        entry["commands"].each do |command|
          begin
            Utils.popen_read(*command.map(&:to_s), safe: true)
            executed << command
          rescue ScriptError, StandardError => error
            restored = false
            if !original.nil?
              begin
                Pathname.new(path).binwrite(original)
                restored = true
              rescue ScriptError, StandardError
                restored = false
              end
            end
            failure = {
              "file" => path,
              "command" => command,
              "reason" => "#{error.class}: #{error.message}",
              "restored" => restored,
            }
            failed << failure
            entry["failed"] = failure
            entry["restored"] = restored
            next if ignore_failures

            raise RuntimeError,
                  "binary relocation failed for #{path}: #{error.class}: #{error.message}"
          end
        end
        if entry["failed"].nil?
          after = binary_info(path)
          entry["verified_magic"] = after["kind"] == entry["kind"]
          if verify_placeholders
            bytes = begin
              Pathname.new(path).binread
            rescue ScriptError, StandardError
              ""
            end
            remaining = relocation_mapping(replacements).keys.select do |old|
              !old.empty? && bytes.include?(old)
            end
            entry["placeholders_removed"] = remaining.empty?
            entry["remaining_placeholders"] = remaining
          end
        end
      end
    end
    codesigned = []
    unless dry_run
      plan["codesign_commands"].each do |command|
        begin
          Utils.popen_read(*command.map(&:to_s), safe: true)
          codesigned << command
        rescue ScriptError, StandardError => error
          raise RuntimeError,
                "codesign failed for #{command.last}: #{error.class}: #{error.message}"
        end
      end
    end
    codesign_verified = []
    entitlements = []
    unless dry_run
      plan["codesign_verify_commands"].each do |command|
        file = command.last.to_s
        begin
          Utils.popen_read(*command.map(&:to_s), safe: true)
          codesign_verified << { "file" => file, "verified" => true }
          entitlements << codesign_entitlements(file)
        rescue ScriptError, StandardError => error
          result = { "file" => file, "verified" => false,
                     "reason" => "#{error.class}: #{error.message}" }
          codesign_verified << result
          raise RuntimeError,
                "codesign verification failed for #{file}: #{result["reason"]}" if strict
        end
      end
    end
    {
      "files" => plan["files"],
      "commands" => plan["commands"],
      "executed" => executed,
      "failed" => failed,
      "skipped" => plan["skipped"],
      "matrix" => plan["matrix"],
      "hardlink_skipped" => plan["hardlink_skipped"],
      "codesign_files" => plan["codesign_files"],
      "codesign_commands" => plan["codesign_commands"],
      "codesign_verify_commands" => plan["codesign_verify_commands"],
      "codesign_identity" => codesign_identity,
      "codesigned" => codesigned,
      "codesign_verified" => codesign_verified,
      "entitlements" => entitlements,
      "codesign_skipped" => plan["codesign_skipped"],
      "dry_run" => !!dry_run,
    }
  end

  def relocate_binaries(*args, **options)
    relocate_binary_files(*args, **options)
  end

  def prepare_debug_symbols
    nil
  end

  def ignore_interrupts_and_uninstall!
    uninstall
  end

  def record_cask_symlink(_dst)
    nil
  end

  def resolve_any_conflicts(_dst, **_options)
    nil
  end

  def delete_node_gyp_debris!
    Dir.glob((@path/"**/node_modules/**/build/**/*").to_s).each do |entry|
      path = Pathname.new(entry)
      next unless path.to_s.include?("/node_modules/") && path.to_s.include?("/build/")

      if path.directory? && path.basename.to_s == "obj.target"
        FileUtils.rm_rf(path.to_s)
      elsif [".o", ".d", ".a"].include?(path.extname) && path.file?
        FileUtils.rm_f(path.to_s)
      end
    end
  end

  def strip_node_gyp_addons!
    []
  end

  def lock
    yield if block_given?
    nil
  end

  def elisp_installed?
    elisp.directory? && Dir.glob("#{elisp}/*.el*").any?
  end

  def apps
    @path.children.select { |child| child.directory? && child.extname == ".app" }
  end

  def plist_installed?
    Dir.glob("#{@path}/*.plist").any?
  end

  def completion_installed?(_shell = nil)
    completion.directory?
  end

  def functions_installed?(_shell = nil)
    false
  end

  def bin
    @path/"bin"
  end

  def sbin
    @path/"sbin"
  end

  def lib
    @path/"lib"
  end

  def include
    @path/"include"
  end

  def share
    @path/"share"
  end

  def etc
    @path/"etc"
  end

  def var
    @path/"var"
  end

  def libexec
    @path/"libexec"
  end

  def doc
    share/"doc"/name
  end

  def info
    share/"info"
  end

  def man
    share/"man"
  end

  def man1
    man/"man1"
  end

  def man2
    man/"man2"
  end

  def man3
    man/"man3"
  end

  def man4
    man/"man4"
  end

  def man5
    man/"man5"
  end

  def man6
    man/"man6"
  end

  def man7
    man/"man7"
  end

  def man8
    man/"man8"
  end

  def completion
    share/"bash-completion/completions"
  end

  def pkgconfig
    lib/"pkgconfig"
  end

  def aclocal
    share/"aclocal"
  end

  def cmake
    share/"cmake"
  end

  def meson
    share/"meson"
  end

  def elisp
    share/"emacs/site-lisp"
  end

  def service
    @path/"service"
  end

  def logs
    var/"log"
  end

  def to_s
    @path.to_s
  end

  def to_path
    @path.to_s
  end

  def basename
    @path.basename
  end

  def inspect
    "#<Keg #{@path}>"
  end

  def ==(other)
    other.is_a?(Keg) && other.path == @path
  end
  alias eql? ==

  def hash
    @path.hash
  end
end

class Service
  attr_reader :formula

  def initialize(formula = nil, &block)
    @formula = formula
    @service_name = nil
    @command = nil
    @manual_command = nil
    @run_at_load = nil
    @keep_alive = false
    @require_root = false
    @launch_only_once = false
    @interval = nil
    @cron = nil
    @environment_variables = {}
    @macos_legacy_timers = false
    @working_dir = nil
    @log_path = nil
    @error_log_path = nil
    @process_type = nil
    instance_eval(&block) if block
  end

  def self.from_hash(api_hash)
    hash = api_hash.is_a?(Hash) ? api_hash : {}
    service = new(nil)
    service.name(hash["name"] || hash["service_name"]) if hash["name"] || hash["service_name"]
    service.run(hash["run"] || hash["command"]) if hash["run"] || hash["command"]
    service.keep_alive(hash["keep_alive"]) unless hash["keep_alive"].nil?
    service.require_root(hash["require_root"]) unless hash["require_root"].nil?
    service.run_at_load(hash["run_at_load"]) unless hash["run_at_load"].nil?
    service.interval(hash["interval"]) if hash["interval"]
    service.cron(hash["cron"]) if hash["cron"]
    service.environment_variables(hash["environment_variables"] || {})
    service
  end

  def f
    formula
  end

  def nice_requires_root?
    false
  end

  def name(value = nil, macos: nil, linux: nil)
    chosen = macos || linux
    @service_name = (chosen || value).to_s unless (chosen || value).nil?
    @service_name || default_service_name
  end

  def default_service_name
    formula.respond_to?(:name) ? formula.name.to_s : "service"
  end

  def default_plist_name
    "homebrew.mxcl.#{name}"
  end

  def run(command = nil, macos: nil, linux: nil)
    chosen = macos || linux
    @command = Array(chosen || command) unless (chosen || command).nil?
    @command
  end

  def command
    @command
  end

  def command?
    !command.nil? && !command.empty?
  end

  def manual_command
    @manual_command || command
  end

  def run_at_load(value = nil)
    @run_at_load = value unless value.nil?
    @run_at_load
  end

  def keep_alive(value = nil)
    @keep_alive = !!value unless value.nil?
    @keep_alive
  end

  def keep_alive?
    !!@keep_alive
  end

  def require_root(value = nil)
    @require_root = !!value unless value.nil?
    @require_root
  end

  def requires_root?
    !!@require_root
  end

  def launch_only_once(value = nil)
    @launch_only_once = !!value unless value.nil?
    @launch_only_once
  end

  def interval(value = nil)
    @interval = value unless value.nil?
    @interval
  end

  def cron(value = nil)
    @cron = value unless value.nil?
    @cron
  end

  def timed?
    !@interval.nil? || !@cron.nil?
  end

  def default_cron_values
    []
  end

  def parse_cron(cron_statement = nil)
    value = cron_statement || @cron
    value.nil? ? [] : value.to_s.split
  end

  def environment_variables(variables = {})
    @environment_variables.merge!(variables) if variables.is_a?(Hash)
    @environment_variables
  end

  def effective_environment_variables
    @environment_variables
  end

  def macos_legacy_timers(value = nil)
    @macos_legacy_timers = !!value unless value.nil?
    @macos_legacy_timers
  end

  def path_dirs
    candidates = []
    if formula.respond_to?(:opt_bin)
      candidates << formula.opt_bin
      candidates << formula.opt_sbin if formula.respond_to?(:opt_sbin)
    end
    candidates.compact
  rescue StandardError
    []
  end

  def path_dir(_path = nil)
    path_dirs.first
  end

  def path_parent_dir(_path = nil)
    found = path_dir
    found.nil? ? nil : found.parent
  end

  def cron_weekday_to_systemd_weekday(cron_weekday)
    value = cron_weekday.to_i
    value.zero? ? 7 : value
  end

  def to_plist
    payload = {
      "Label" => default_plist_name,
      "ProgramArguments" => Array(command),
      "RunAtLoad" => !!run_at_load,
      "KeepAlive" => keep_alive?
    }
    payload["StartInterval"] = @interval unless @interval.nil?
    payload["EnvironmentVariables"] = @environment_variables unless @environment_variables.empty?
    payload
  end

  def to_hash
    to_plist
  end

  def to_systemd_unit
    lines = ["[Unit]", "Description=#{default_plist_name}", "", "[Service]"]
    lines << "ExecStart=#{Array(command).join(" ")}" if command?
    lines << "Restart=always" if keep_alive?
    @environment_variables.each { |key, value| lines << "Environment=\"#{key}=#{value}\"" }
    lines.join("\n") + "\n"
  end

  def to_systemd_timer
    lines = ["[Unit]", "Description=#{default_plist_name} timer", "", "[Timer]"]
    lines << "OnUnitActiveSec=#{@interval}" unless @interval.nil?
    lines << "OnCalendar=#{@cron}" unless @cron.nil?
    lines.join("\n") + "\n"
  end
end

class Tab
  attr_reader :tabfile

  def initialize(data = {}, tabfile = nil)
    @data = data.is_a?(Hash) ? data : {}
    @tabfile = tabfile.nil? ? nil : Pathname.new(tabfile.to_s)
  end

  def self.empty
    new({})
  end

  def self.create(formula)
    tab = new({})
    tab.homebrew_version = HOMEBREW_VERSION
    tab.installed_on_request = false
    tab.time = Time.now.to_i
    tab.arch = Hardware::CPU.arch
    tab.source = {
      "tap" => (formula.respond_to?(:tap) ? formula.tap : nil),
      "tap_git_head" => nil,
    }
    tab.runtime_dependencies = []
    tab
  end

  def self.from_file(path)
    path = Pathname.new(path.to_s)
    return empty unless path.file?

    from_file_content(path.read, path)
  end

  def self.from_file_content(content, path = nil)
    data = JSON.parse(content.to_s)
    new(data, path)
  rescue StandardError
    new({}, path)
  end

  def self.for_keg(keg)
    keg = Keg.new(keg) unless keg.is_a?(Keg)
    tabfile = keg.path/"INSTALL_RECEIPT.json"
    return new({}, tabfile) unless tabfile.file?

    new(JSON.parse(tabfile.read), tabfile)
  rescue StandardError
    new({}, tabfile)
  end

  def self.formula_to_dep_hash(formula, declared_deps)
    row = {
      "full_name" => formula.full_name,
      "version" => formula.version.to_s,
      "revision" => formula.revision,
      "bottle_rebuild" => formula.bottle_for_tag&.rebuild,
      "pkg_version" => formula.pkg_version.to_s,
      "declared_directly" => Array(declared_deps).include?(formula.full_name),
      "compatibility_version" => formula.compatibility_version
    }
    row.reject { |_key, value| value.nil? }
  end

  def self.for_name(name)
    cellar = Pathname.new(PybrewFormulaRef.cellar)
    rack = cellar/name.to_s
    return empty unless rack.directory?

    kegs = rack.children.select { |child| child.directory? }
    return empty if kegs.empty?

    for_keg(kegs.max_by { |child| PybrewFormulaRef.version_key(child.basename.to_s) })
  rescue StandardError
    empty
  end

  def self.for_formula(formula)
    name = formula.respond_to?(:name) ? formula.name : formula
    for_name(name)
  end

  def [](key)
    @data[key.to_s]
  end

  def []=(key, value)
    @data[key.to_s] = value
  end

  def to_hash
    @data.dup
  end

  def to_json(*args)
    JSON.generate(@data, *args)
  end

  def tap
    value = @data["tap"]
    value = @data.dig("source", "tap") if value.nil? && @data["source"].is_a?(Hash)
    value
  end

  def tap=(value)
    @data["tap"] = value
  end

  def write
    return false if @tabfile.nil?

    @tabfile.dirname.mkpath
    @tabfile.write(JSON.pretty_generate(@data) + "\n")
    true
  end

  def installed_on_request_present?
    @data.key?("installed_on_request")
  end

  def homebrew_version
    @data["homebrew_version"]
  end

  def parsed_homebrew_version
    value = homebrew_version
    value.nil? ? nil : PybrewVersion.new(value.to_s)
  end

  def installed_on_request?
    value = @data["installed_on_request"]
    return !!value unless value.nil?

    dependency = @data["installed_as_dependency"]
    dependency.nil? ? true : !dependency
  end

  def installed_as_dependency?
    value = @data["installed_as_dependency"]
    return !!value unless value.nil?

    !installed_on_request?
  end

  def installed_on_request
    installed_on_request?
  end

  def installed_as_dependency
    installed_as_dependency?
  end

  def used_options
    Array(@data["used_options"])
  end

  def unused_options
    Array(@data["unused_options"])
  end

  def built_as_bottle?
    !!@data["built_as_bottle"]
  end

  def poured_from_bottle?
    !!@data["poured_from_bottle"]
  end

  def runtime_dependencies
    Array(@data["runtime_dependencies"])
  end

  def version_scheme
    (@data["version_scheme"] || 0).to_i
  end

  def aliases
    Array(@data["aliases"])
  end

  def build_dependencies
    Array(@data["build_dependencies"])
  end

  def source
    @data["source"] || {}
  end

  def source=(value)
    @data["source"] = value.is_a?(Hash) ? value : {}
  end

  def built_on
    @data["built_on"].is_a?(Hash) ? @data["built_on"] : {}
  end

  def built_on=(value)
    @data["built_on"] = value.is_a?(Hash) ? value : {}
  end

  def sbom
    @data["sbom"].is_a?(Hash) ? @data["sbom"] : {}
  end

  def sbom_files
    files = sbom["files"]
    files.is_a?(Hash) ? files : {}
  end

  def sbom=(value)
    @data["sbom"] = value.is_a?(Hash) ? value : {}
  end

  def arch
    @data["arch"]
  end

  def arch=(value)
    @data["arch"] = value
  end

  def built_as_bottle
    built_as_bottle?
  end

  def built_as_bottle=(value)
    @data["built_as_bottle"] = !!value
  end

  def poured_from_bottle
    poured_from_bottle?
  end

  def poured_from_bottle=(value)
    @data["poured_from_bottle"] = !!value
  end

  def compiler=(value)
    @data["compiler"] = value
  end

  def changed_files=(value)
    @data["changed_files"] = Array(value)
  end

  def source_modified_time=(value)
    @data["source_modified_time"] = value
  end

  def version_scheme=(value)
    @data["version_scheme"] = value.to_i
  end

  def runtime_dependencies=(value)
    @data["runtime_dependencies"] = Array(value)
  end

  def used_options=(value)
    @data["used_options"] = Array(value)
  end

  def unused_options=(value)
    @data["unused_options"] = Array(value)
  end

  def time
    @data["time"]
  end

  def time=(value)
    @data["time"] = value
  end

  def homebrew_version=(value)
    @data["homebrew_version"] = value
  end

  def installed_on_request=(value)
    @data["installed_on_request"] = !!value
  end

  def installed_as_dependency=(value)
    @data["installed_as_dependency"] = !!value
  end

  def head=(value)
    @data["HEAD"] = !!value
  end

  def stdlib
    @data["stdlib"]
  end

  def stdlib=(value)
    @data["stdlib"] = value
  end

  def bottle_rebuild
    @data["bottle_rebuild"]
  end

  def loaded_from_api?
    !!@data["loaded_from_api"]
  end

  def loaded_from_api=(value)
    @data["loaded_from_api"] = !!value
  end

  def loaded_from_internal_api?
    !!@data["loaded_from_internal_api"]
  end

  def compiler
    @data["compiler"]
  end

  def changed_files
    Array(@data["changed_files"])
  end

  def head?
    !!@data["HEAD"]
  end

  def versions
    @data["versions"].is_a?(Hash) ? @data["versions"] : {}
  end

  def stable_version
    versions["stable"]
  end

  def head_version
    versions["head"]
  end

  def built_bottle?
    built_as_bottle?
  end

  def to_bottle_hash
    @data.dup
  end

  def runtime_deps_hash
    runtime_dependencies.each_with_object({}) do |dependency, result|
      result[dependency.to_s] = dependency
    end
  end

  def source_modified_time
    @data["source_modified_time"] || time
  end

  def cxxstdlib
    @data["cxxstdlib"]
  end

  def empty_source_versions
    []
  end

  def remap_deprecated_options(_deprecated_options = [])
    @data
  end

  def any_args_or_options?
    !used_options.empty? || !unused_options.empty?
  end

  def inspect
    "#<Tab #{@data.inspect}>"
  end
end

module SystemConfig
  class << self
    def clang
      value = DevelopmentTools.clang_version
      value.nil? || value.to_s.empty? ? nil : PybrewVersion.new(value.to_s)
    end

    def clang_build
      value = DevelopmentTools.clang_build_version
      value.nil? || value.to_s.empty? ? nil : PybrewVersion.new(value.to_s)
    end

    def describe_clang
      return "N/A" if clang.nil?
      return clang.to_s if clang_build.nil?

      "#{clang} build #{clang_build}"
    end

    def describe_homebrew_ruby
      "#{RUBY_VERSION} => #{RbConfig.ruby}"
    end

    def hardware
      return nil if Hardware::CPU.type == :dunno

      "CPU: #{Hardware::CPU.cores}-core #{Hardware::CPU.bits}-bit #{Hardware::CPU.family}"
    end

    def describe_os
      if OS.mac?
        "macOS #{MacOS.full_version} (#{MacOS.version_symbol})"
      elsif OS.linux?
        "Linux #{OS.version}"
      else
        RUBY_PLATFORM.to_s
      end
    rescue StandardError
      "N/A"
    end

    def describe_hardware
      text = hardware
      return "N/A" if text.nil?

      model = Hardware::CPU.model.to_s
      arch = Hardware::CPU.arch.to_s
      flags = Array(Hardware::CPU.flags).map(&:to_s).join(" ")
      text += "\nModel: #{model}; Arch: #{arch}" unless model.empty?
      text += "\nFlags: #{flags}" unless flags.empty?
      text
    rescue StandardError
      hardware.to_s
    end

    def describe_xcode
      version = MacOS::Xcode.version
      if version.nil?
        MacOS::Xcode.installed? ? "installed (unknown version)" : "N/A"
      else
        "#{version} => #{MacOS::Xcode.prefix}"
      end
    rescue StandardError
      "N/A"
    end

    def describe_clt
      version = MacOS::CLT.version
      version.nil? ? (MacOS::CLT.installed? ? "installed (unknown version)" : "N/A") : version.to_s
    rescue StandardError
      "N/A"
    end

    def describe_sdk
      found = MacOS.sdk
      found.nil? ? "N/A" : "#{found.version} => #{found.path}"
    rescue StandardError
      "N/A"
    end

    def describe_kernel
      uname = Etc.uname
      [uname[:sysname], uname[:release], uname[:machine]].compact.join(" ")
    rescue StandardError
      RUBY_PLATFORM.to_s
    end

    def describe_compiler
      compiler = DevelopmentTools.default_compiler.to_s
      version = DevelopmentTools.compiler_version(compiler)
      version.nil? ? compiler : "#{compiler} #{version}"
    rescue StandardError
      "N/A"
    end

    def describe_ruby
      [RUBY_VERSION, RUBY_PLATFORM, RbConfig.ruby].compact.join(" => ")
    rescue StandardError
      RUBY_VERSION.to_s
    end

    def tap_paths
      roots = Array(PYBREW_CONFIG["taps"]).map(&:to_s).reject(&:empty?)
      ENV["PYBREW_TAPS"].to_s.split(File::PATH_SEPARATOR).each do |root|
        roots << root unless root.to_s.strip.empty?
      end
      roots.map { |root| Pathname.new(root) }.select(&:directory?).map(&:expand_path).uniq
    rescue StandardError
      []
    end

    def describe_tap(tap)
      path = tap.is_a?(Pathname) ? tap : Pathname.new(tap.to_s)
      return "#{path} (not found)" unless path.directory?

      formula_dir = path/"Formula"
      count = formula_dir.directory? ? Dir.glob((formula_dir/"**/*.rb").to_s).length : 0
      "#{path.dirname.basename}/#{path.basename} => #{path} (#{count} formulae)"
    rescue StandardError
      tap.to_s
    end

    def describe_taps
      paths = tap_paths
      return "(none)" if paths.empty?

      paths.map { |path| describe_tap(path) }.join(", ")
    rescue StandardError
      "(none)"
    end

    def describe_env
      keys = %w[
        HOMEBREW_PREFIX HOMEBREW_CELLAR HOMEBREW_REPOSITORY HOMEBREW_CACHE
        HOMEBREW_TEMP HOMEBREW_LOGS HOMEBREW_NO_AUTO_UPDATE
      ]
      keys.map do |key|
        value = key == "HOMEBREW_PREFIX" ? HOMEBREW_PREFIX.to_s : ENV[key].to_s
        "#{key}=#{value}"
      end.join(" ")
    rescue StandardError
      "N/A"
    end

    def describe_arch
      [Hardware::CPU.arch.to_s, Hardware::CPU.bits.to_s,
       Hardware::CPU.arm? ? "arm" : "intel"].join(" ")
    rescue StandardError
      RUBY_PLATFORM.to_s
    end

    def kernel_config(out = $stdout)
      out.puts "Kernel: #{describe_kernel}"
      out
    end

    def compiler_config(out = $stdout)
      out.puts "Compiler: #{describe_compiler}"
      out.puts "Ruby: #{describe_ruby}"
      out
    end

    def taps_config(out = $stdout)
      out.puts "Taps: #{tap_paths.empty? ? "(none)" : ""}"
      tap_paths.each { |path| out.puts "  #{describe_tap(path)}" }
      out
    end

    def macos_config(out = $stdout)
      out.puts "macOS: #{describe_os}" if OS.mac?
      out.puts "SDK: #{describe_sdk}" if OS.mac?
      out.puts "Xcode: #{describe_xcode}" if OS.mac?
      out.puts "CLT: #{describe_clt}" if OS.mac?
      out.puts "Arch: #{describe_arch}"
      out
    end

    def kernel
      Etc.uname[:machine].to_s
    end

    def branch
      "(none)"
    end

    def homebrew_repo
      Pathname.new((defined?(HOMEBREW_REPOSITORY) ? HOMEBREW_REPOSITORY : HOMEBREW_PREFIX).to_s)
    end

    def homebrew_head_info
      nil
    end

    def windows_version
      nil
    end

    def head
      "(none)"
    end

    def last_commit
      "never"
    end

    def origin
      "(none)"
    end

    def describe_git
      return "N/A" unless Utils::Git.available?

      output = Utils.popen_read("git", "--version").to_s
      version = output[/\d+(?:\.\d+)+/, 0]
      "#{version || "git"} => #{Utils.which("git")}"
    end

    def describe_curl
      output = Utils.popen_read("curl", "--version").to_s
      version = output[/^curl ([\d.]+)/, 1]
      version.nil? ? "N/A" : "#{version} => #{Utils.which("curl")}"
    end

    def config_sections
      [
        :homebrew_config, :homebrew_env_config, :hardware_config,
        :host_software_config, :kernel_config, :compiler_config,
        :taps_config, :macos_config,
      ]
    end

    def homebrew_config(out = $stdout)
      out.puts "HOMEBREW_PREFIX: #{HOMEBREW_PREFIX}"
      out.puts "HOMEBREW_CELLAR: #{HOMEBREW_CELLAR}"
      out.puts "HOMEBREW_REPOSITORY: #{defined?(HOMEBREW_REPOSITORY) ? HOMEBREW_REPOSITORY : HOMEBREW_PREFIX}"
      out.puts "HOMEBREW_VERSION: #{HOMEBREW_VERSION}"
      out
    end

    def homebrew_env_config(out = $stdout)
      out.puts "Homebrew Ruby: #{describe_homebrew_ruby}"
      out.puts "Homebrew Env: #{describe_env}"
      out
    end

    def hardware_config(out = $stdout)
      out.puts "Hardware: #{describe_hardware}"
      out.puts "CPU: #{Hardware::CPU.arch}" if OS.mac? || OS.linux?
      out.puts "CPU Cores: #{Hardware::CPU.cores} (#{Hardware::CPU.physical_cores} physical)" rescue nil
      out.puts "CPU Features: #{Array(Hardware::CPU.features).join(" ")}" rescue nil
      out
    end

    def host_software_config(out = $stdout)
      out.puts "OS: #{describe_os}"
      out.puts "Clang: #{describe_clang}"
      out.puts "Xcode: #{describe_xcode}" if OS.mac?
      out.puts "CLT: #{describe_clt}" if OS.mac?
      out.puts "SDK: #{describe_sdk}" if OS.mac?
      out.puts "Git: #{describe_git}"
      out.puts "Curl: #{describe_curl}"
      out
    end

    def dump_tap_config(tap = nil, out = $stdout)
      if tap.nil?
        tap_paths.each { |path| out.puts "Tap: #{describe_tap(path)}" }
      else
        out.puts "Tap: #{describe_tap(tap)}"
      end
      out
    end

    def core_tap_config(out = $stdout)
      core = tap_paths.find { |path| path.to_s.include?("homebrew-core") }
      out.puts "Core Tap: #{core.nil? ? "(none)" : describe_tap(core)}"
      out
    end

    def dump_verbose_config(out = $stdout)
      config_sections.each { |section| public_send(section, out) }
      out
    end
    alias dump dump_verbose_config
  end
end

class PybrewCaveatsContext
  def initialize(formula)
    @formula = formula
    @output = +""
  end

  def render(source)
    value = instance_eval(source, "caveats", 1)
    text = value.is_a?(String) ? value : ""
    @output.empty? ? text : @output + text
  end

  def formula
    @formula
  end

  def name
    @formula.name
  end

  def version
    @formula.version
  end

  def full_name
    @formula.full_name
  end

  def puts(*messages)
    if messages.empty?
      @output << "\n"
    else
      messages.each { |message| @output << message.to_s << "\n" }
    end
    nil
  end

  def print(*messages)
    messages.each { |message| @output << message.to_s }
    nil
  end

  def ohai(*messages)
    messages.each { |message| @output << "==> " << message.to_s << "\n" }
    nil
  end

  def opoo(*messages)
    messages.each { |message| @output << "Warning: " << message.to_s << "\n" }
    nil
  end

  def on_macos
    yield if OS.mac?
    nil
  end

  def on_linux
    yield if OS.linux?
    nil
  end

  def on_arm
    yield if Hardware::CPU.arm?
    nil
  end

  def on_intel
    yield if Hardware::CPU.intel?
    nil
  end

  def method_missing(name, *args, &block)
    if @formula.respond_to?(name)
      @formula.public_send(name, *args, &block)
    else
      super
    end
  end

  def respond_to_missing?(name, include_private = false)
    @formula.respond_to?(name, include_private) || super
  end
end

module Formula
  def self.[](name)
    PybrewFormulaRef.new(name)
  end

  def self.factory(name)
    self[name]
  end

  def self.rack_installed?(name)
    rack = HOMEBREW_CELLAR/name.to_s.split("/").last.to_s
    rack.directory? && !rack.children.select { |child| child.directory? }.empty?
  end

  def self.installed_formula_names
    return [] unless HOMEBREW_CELLAR.directory?

    HOMEBREW_CELLAR.children.select { |child| child.directory? }.map { |child| child.basename.to_s }
  end

  def self.installed
    installed_formula_names.map { |name| PybrewFormulaRef.new(name) }
  end

  def self.names
    configured = (PYBREW_CONFIG["formulae"] || {}).keys
    (configured + installed_formula_names).uniq.sort
  end

  def self.full_names
    names.map { |name| PybrewFormulaRef.new(name).full_name }.uniq.sort
  end

  def self.aliases
    names.flat_map { |name| PybrewFormulaRef.new(name).aliases }.map(&:to_s).uniq.sort
  end

  def self.alias_full_names
    names.flat_map do |name|
      ref = PybrewFormulaRef.new(name)
      ref.aliases.map { |alias_name| "#{ref.tap}/#{alias_name}".sub(%r{^/}, "") }
    end.uniq.sort
  end

  def self.core_names
    names.select { |name| PybrewFormulaRef.new(name).tap.to_s == "homebrew/core" }
  end

  def self.core_aliases
    core_names.flat_map { |name| PybrewFormulaRef.new(name).aliases }.map(&:to_s).uniq.sort
  end

  def self.tap_names
    names.select { |name| !PybrewFormulaRef.new(name).tap.to_s.empty? }
  end

  def self.tap_aliases
    tap_names.flat_map { |name| PybrewFormulaRef.new(name).aliases }.map(&:to_s).uniq.sort
  end

  def self.racks
    return [] unless HOMEBREW_CELLAR.directory?

    HOMEBREW_CELLAR.children.select { |child| child.directory? }
  end

  def self.tap_files
    names.filter_map do |name|
      ref = PybrewFormulaRef.new(name)
      path = ref.path
      path.file? ? path : nil
    end
  end

  def self.installed_with_alias_path
    installed.each_with_object({}) do |ref, result|
      result[ref.name] = ref.aliases
    end
  end

  def self.fuzzy_search(query)
    needle = query.to_s.downcase
    names.select { |name| name.downcase.include?(needle) }
  end
end

class PybrewInreplaceHelper
  attr_reader :inreplace_string

  def initialize(string)
    @inreplace_string = string.to_s
  end

  def gsub!(before, after)
    @inreplace_string = @inreplace_string.gsub(before.to_s, after.to_s)
    self
  end

  def sub!(before, after)
    @inreplace_string = @inreplace_string.sub(before.to_s, after.to_s)
    self
  end

  def change_make_var!(variable, value)
    gsub!(/^#{Regexp.escape(variable.to_s)}\s*=.*$/, "#{variable}=#{value}")
  end

  def remove_make_var!(variable)
    @inreplace_string = @inreplace_string.lines.reject do |line|
      line.start_with?("#{variable}=")
    end.join
    self
  end
end

class PybrewPypiPackages
  attr_reader :package_name, :extra_packages, :exclude_packages, :dependencies

  def initialize(package_name: nil, extra_packages: nil, exclude_packages: nil,
                 dependencies: nil)
    @package_name = package_name
    @extra_packages = Array(extra_packages).compact.map(&:to_s)
    @exclude_packages = Array(exclude_packages).compact.map(&:to_s)
    @dependencies = Array(dependencies).compact.map(&:to_s)
  end
end

class PybrewResourceDefinition
  FIELDS = %i[
    url sha256 version using mirror mirrors file data apply strip type resolves
    directory
  ].freeze

  def initialize(name, context_version = nil)
    @name = name.to_s
    @context_version = context_version
    @attrs = {}
  end

  def name
    @name
  end

  def method_missing(method_name, *args, **kwargs, &block)
    if FIELDS.include?(method_name)
      if args.empty? && kwargs.empty?
        return @attrs[method_name] if @attrs.key?(method_name)
        return @context_version if method_name == :version && !@context_version.nil?

        return nil
      end
      value = if !kwargs.empty?
        kwargs
      elsif args.length > 1
        args
      else
        args.first
      end
      @attrs[method_name] = value
      return value
    end
    super
  end

  def patch(*args, &block)
    @patches ||= []
    @patches << PybrewPatchDefinition.new(args, block)
    nil
  end

  def respond_to_missing?(method_name, include_private = false)
    FIELDS.include?(method_name) || super
  end

  def to_resource
    info = {}
    info["patches"] = Array(@patches).map(&:to_hash) unless @patches.nil?
    @attrs.each do |key, value|
      normalized = value.is_a?(Pathname) ? value.to_s : value
      normalized = normalized.map { |entry| entry.is_a?(Pathname) ? entry.to_s : entry } if normalized.is_a?(Array)
      info[key.to_s] = normalized
      info["data"] = true if key == :data && value == true
    end
    PybrewResource.new(@name, info)
  end
end

class PybrewPatchDefinition
  FIELDS = %i[url sha256 strip file directory resolves type apply data].freeze

  def initialize(args = nil, block = nil)
    @args = Array(args)
    @attrs = {}
    @block = block
    parse_arguments!
    capture_block! unless block.nil?
  end

  # Homebrew patch argument matrix:
  #   patch "url" | patch "file.diff" | patch :p0, "inline diff" |
  #   patch :DATA | patch do url ... end
  def parse_arguments!
    return if @args.empty?

    first = @args.first
    second = @args[1]
    if first.is_a?(Symbol)
      if first == :DATA
        @attrs[:data] = true
      else
        @attrs[:strip] = first
      end
      @attrs[:data] = second if second.is_a?(String)
    elsif first.is_a?(String)
      @attrs[:url] = first
    elsif first.is_a?(Pathname)
      @attrs[:file] = first
    end
  end

  def capture_block!
    @block.call(self) if @block.arity >= 1
    instance_eval(&@block) if @block.arity.zero?
  end

  def method_missing(method_name, *args, **kwargs, &block)
    if FIELDS.include?(method_name)
      value = if !kwargs.empty?
        kwargs
      elsif args.length > 1
        args
      else
        args.first
      end
      @attrs[method_name] = value
      return value
    end
    super
  end

  def respond_to_missing?(method_name, include_private = false)
    FIELDS.include?(method_name) || super
  end

  def url(value = nil)
    @attrs[:url] = value unless value.nil?
    return @attrs[:url] if @attrs.key?(:url) && !@attrs[:url].nil?

    @args.first if @args.first.is_a?(String)
  end

  def file(value = nil)
    @attrs[:file] = value unless value.nil?
    return @attrs[:file] if @attrs.key?(:file) && !@attrs[:file].nil?

    @args.first if @args.first.is_a?(Pathname)
  end

  def sha256(value = nil)
    @attrs[:sha256] = value unless value.nil?
    @attrs[:sha256]
  end

  def strip(value = nil)
    @attrs[:strip] = value unless value.nil?
    return @attrs[:strip] unless @attrs[:strip].nil?
    return nil if data?

    @args.first.is_a?(Symbol) ? @args.first : nil
  end

  # Inline diff data for ``patch :pN, "diff"`` forms.
  def data
    return @attrs[:data] if @attrs[:data].is_a?(String)
    return @args[1] if @args[1].is_a?(String)

    nil
  end

  def apply_value
    @attrs[:apply]
  end

  def patch_files
    [file].compact.map(&:to_s)
  end

  def to_hash
    {
      "url" => url,
      "file" => (file.nil? ? nil : file.to_s),
      "sha256" => sha256,
      "strip" => strip,
      "data" => data,
      "directory" => (directory.nil? ? nil : directory.to_s),
      "resolves" => resolves,
      "type" => type,
      "apply" => apply_value,
    }
  end

  def directory(value = nil)
    @attrs[:directory] = value unless value.nil?
    @attrs[:directory]
  end

  def resolves(*values)
    @attrs[:resolves] = Array(values).flatten.compact.map(&:to_s) unless values.empty?
    Array(@attrs[:resolves])
  end

  def type(value = nil)
    @attrs[:type] = value unless value.nil?
    @attrs[:type]
  end

  def apply?
    @attrs.key?(:apply)
  end

  def data?
    @args.first == :DATA || @attrs[:data] == true
  end

  def to_s
    (url || file || (data? ? ":DATA" : "inline patch")).to_s
  end
end

class PybrewLoadedFormula
  # Bounded metadata-origin marker. pybrew's source loader does not expose
  # Homebrew's namespace constant directly, so this is false for normal source
  # loads and true only for API/metadata structs (see load_formula_from_struct!).
  def loaded_from_metadata?
    self.class.loaded_from_metadata?
  end

  def pypi_packages_info
    self.class.pypi_packages_info
  end

  def follow_installed_alias?
    return @follow_installed_alias unless @follow_installed_alias.nil?

    self.class.follow_installed_alias?
  end

  def follow_installed_alias
    follow_installed_alias?
  end

  def follow_installed_alias=(value)
    @follow_installed_alias = !!value
  end
  def local_bottle_path
    @local_bottle_path
  end

  def local_bottle_path=(value)
    @local_bottle_path = value.nil? ? nil : Pathname.new(value.to_s)
  end
  def preserve_rpath?
    self.class.loaded_preserve_rpath?
  end



  class << self
    def loaded_classes
      @loaded_classes ||= []
    end

    def formula_alias(alias_name)
      (@declared_aliases ||= []) << alias_name.to_s
    end

    def alias_name(alias_name)
      formula_alias(alias_name)
    end

    def declared_aliases
      @declared_aliases ||= []
    end

    def loaded_from_metadata?
      !!@loaded_from_metadata
    end

    def follow_installed_alias?
      @follow_installed_alias.nil? ? true : !!@follow_installed_alias
    end

    def follow_installed_alias=(value)
      @follow_installed_alias = !!value
    end

    def pypi_packages(package_name: nil, extra_packages: nil,
                      exclude_packages: nil, dependencies: nil)
      @pypi_packages_info = PybrewPypiPackages.new(
        package_name: package_name,
        extra_packages: extra_packages,
        exclude_packages: exclude_packages,
        dependencies: dependencies,
      )
      nil
    end

    def pypi_packages_info
      @pypi_packages_info
    end

    def inherited(subclass)
      PybrewLoadedFormula.loaded_classes << subclass
      super
    end

    attr_accessor :loaded_name, :loaded_desc, :loaded_homepage, :loaded_url,
                  :loaded_sha256, :loaded_license, :loaded_version,
                  :loaded_revision, :loaded_version_scheme,
                  :loaded_test_block, :loaded_install_block,
                  :loaded_caveats_block, :loaded_post_install_block,
                  :loaded_bottle, :loaded_service, :loaded_livecheck,
                  :loaded_resources, :loaded_patches, :loaded_options,
                  :loaded_channel, :loaded_channels,
                  :loaded_head, :loaded_stable, :loaded_fetch_block,
                  :loaded_fails_with, :loaded_namespace, :loaded_class_name,
                  :loaded_dependencies, :loaded_path

    def name
      @loaded_name || super
    end

    def formula_name
      @loaded_name
    end

    def desc(value = nil)
      return @loaded_desc if value.nil?

      @loaded_desc = value.to_s
    end

    def homepage(value = nil)
      return @loaded_homepage if value.nil?

      @loaded_homepage = value.to_s
    end

    def url(value = nil)
      return @loaded_url if value.nil?

      @loaded_url = value.to_s
    end

    def sha256(value = nil)
      return @loaded_sha256 if value.nil?

      @loaded_sha256 = value.to_s
    end

    def version(value = nil)
      return @loaded_version if value.nil?

      @loaded_version = value.to_s
    end

    def license(value = nil)
      return @loaded_license if value.nil?

      @loaded_license = value.to_s
    end

    def revision(value = nil)
      return @loaded_revision if value.nil?

      @loaded_revision = value.to_i
    end

    def version_scheme(value = nil)
      return @loaded_version_scheme if value.nil?

      @loaded_version_scheme = value.to_i
    end

    def depends_on(name, *tags)
      @loaded_dependencies ||= []
      if name.is_a?(Hash)
        name.each do |dep_name, dep_tags|
          @loaded_dependencies << [dep_name.to_s, Array(dep_tags).flatten.map(&:to_s)]
        end
      else
        @loaded_dependencies << [name.to_s, tags.flatten.map(&:to_s)]
      end
      nil
    end

    def dependencies
      @loaded_dependencies ||= []
    end

    def uses_from_macos(name, *_args)
      depends_on(name, "uses_from_macos")
    end

    def test(&block)
      @loaded_test_block = block
    end

    def install(&block)
      @loaded_install_block = block
    end

    def fetch(&block)
      @loaded_fetch_block = block
    end

    def caveats(&block)
      @loaded_caveats_block = block
    end

    def post_install(&block)
      @loaded_post_install_block = block
    end

    def service(&block)
      @loaded_service = block
    end

    def bottle(&block)
      @loaded_bottle = block
    end

    def livecheck(&block)
      @loaded_livecheck = block
    end

    def head(url = nil, **options)
      @loaded_head = { url: url, options: options }
      nil
    end

    def stable(&block)
      @loaded_stable = block
    end

    def resource(name, &block)
      @loaded_resources ||= {}
      @loaded_resources[name.to_s] = block
      nil
    end

    def patch(*args, &block)
      @loaded_patches ||= []
      @loaded_patches << [args, block]
      nil
    end

    def option(name, description = "")
      @loaded_options ||= []
      @loaded_options << [name.to_s, description.to_s]
      nil
    end

    def fails_with(compiler = nil, &block)
      @loaded_fails_with ||= []
      @loaded_fails_with << [compiler, block]
      nil
    end

    def keg_only(*_args)
      nil
    end

    def conflicts_with(*_args)
      nil
    end

    def skip_clean(*_args)
      nil
    end

    def preserve_rpath(value = true)
      @loaded_preserve_rpath = !!value
      nil
    end

    def loaded_preserve_rpath?
      !!@loaded_preserve_rpath
    end

    def preserve_rpath?
      loaded_preserve_rpath?
    end

    def deny_network_access!(*_args)
      nil
    end

    def on_macos(&block)
      yield if OS.mac? && block
      nil
    end

    def on_linux(&block)
      yield if OS.linux? && block
      nil
    end

    def on_arm(&block)
      yield if Hardware::CPU.arm? && block
      nil
    end

    def on_intel(&block)
      yield if Hardware::CPU.intel? && block
      nil
    end
  end

  def initialize(name = nil, version = nil)
    @name = name
    @version = version
  end

  def name
    (@name || self.class.loaded_name).to_s
  end

  def full_name
    name
  end

  def path
    self.class.loaded_path
  end

  def version
    (@version || self.class.loaded_version || "HEAD").to_s
  end

  # Resources declared in the formula class body (evaluated dynamically at
  # load time) are exposed as real Resource objects, so dynamic names and
  # class-body values work even without Python metadata staging.
  def declared_resources
    @declared_resources ||= begin
      list = PybrewResourceList.new
      blocks = self.class.loaded_resources || {}
      blocks.each do |resource_name, block|
        definition = PybrewResourceDefinition.new(
          resource_name, self.class.loaded_version
        )
        definition.instance_eval(&block) if block
        list << definition.to_resource
      end
      list
    end
  end

  def resources
    declared_resources
  end

  def resource(resource_name)
    declared_resources[resource_name.to_s]
  end

  # Patches declared in the formula class body, including dynamically
  # interpolated urls/files.  They are metadata only: the Python builder
  # remains responsible for actually applying patches.
  def patches
    @declared_patches ||= Array(self.class.loaded_patches).map do |args, block|
      PybrewPatchDefinition.new(args, block)
    end
  end

  def patchlist
    patches
  end

  def patch_files
    patches.flat_map(&:patch_files)
  end

  def fetch?
    !self.class.loaded_fetch_block.nil?
  end

  def fetch_defined?
    fetch?
  end

  def fetch
    block = self.class.loaded_fetch_block
    instance_eval(&block) if block
  end

  def compiler_failures
    (self.class.loaded_fails_with || []).map do |spec, block|
      CompilerFailure.create(spec, &block)
    end
  end

  def desc
    self.class.loaded_desc
  end

  def homepage
    self.class.loaded_homepage
  end

  def url
    self.class.loaded_url
  end

  def sha256
    self.class.loaded_sha256
  end

  def license
    self.class.loaded_license
  end

  def revision
    (self.class.loaded_revision || 0).to_i
  end

  def version_scheme
    (self.class.loaded_version_scheme || 0).to_i
  end

  def dependencies
    (self.class.dependencies || []).map do |dep_name, tags|
      PybrewDependencyRef.new(dep_name, tags)
    end
  end

  def options
    (self.class.loaded_options || []).map(&:first)
  end

  def option?(name)
    options.include?(name.to_s)
  end

  def head?
    !self.class.loaded_head.nil?
  end

  def stable?
    !self.class.loaded_stable.nil?
  end

  def bottle?
    !self.class.loaded_bottle.nil?
  end

  def livecheck?
    !self.class.loaded_livecheck.nil?
  end

  def prefix
    Pathname.new(HOMEBREW_PREFIX)/"Cellar"/name/version
  end

  def opt_prefix
    Pathname.new(HOMEBREW_PREFIX)/"opt"/name
  end

  def rack
    Pathname.new(HOMEBREW_PREFIX)/"Cellar"/name
  end

  def bin
    prefix/"bin"
  end

  def sbin
    prefix/"sbin"
  end

  def lib
    prefix/"lib"
  end

  def include
    prefix/"include"
  end

  def share
    prefix/"share"
  end

  def etc
    Pathname.new(HOMEBREW_PREFIX)/"etc"/name
  end

  def var
    Pathname.new(HOMEBREW_PREFIX)/"var"
  end

  def libexec
    prefix/"libexec"
  end

  def test
    block = self.class.loaded_test_block
    instance_eval(&block) if block
  end

  def install
    block = self.class.loaded_install_block
    instance_eval(&block) if block
  end

  def inreplace(path, before = nil, after = nil, &block)
    path = Pathname.new(path.to_s)
    text = path.read
    if block
      helper = PybrewInreplaceHelper.new(text)
      block.call(helper)
      text = helper.inreplace_string
    else
      text = text.gsub(before.to_s, after.to_s)
    end
    path.write(text)
    nil
  end

  def ohai(*messages)
    messages.each { |message| puts "==> #{message}" }
    nil
  end

  def opoo(*messages)
    messages.each { |message| puts "Warning: #{message}" }
    nil
  end

  def caveats
    block = self.class.loaded_caveats_block
    block.nil? ? nil : instance_eval(&block)
  end

  def post_install
    block = self.class.loaded_post_install_block
    instance_eval(&block) if block
  end

  def to_s
    name
  end
end

module Formulary
  module_function

  # Accessors understood by the bounded API-struct mapping.
  FORMULA_STRUCT_ATTRIBUTES = %i[
    desc homepage url sha256 version license dependencies aliases oldname oldnames
  ].freeze
  # The bounded struct shape must at least carry a description and version;
  # objects missing them are rejected as malformed instead of producing an
  # empty formula class.
  FORMULA_STRUCT_REQUIRED_ATTRIBUTES = %i[desc version].freeze

  # Homebrew's factory returns a Formula.  When the metadata reference points at
  # a local formula file (tap source), evaluate it with
  # ``load_formula_from_path`` and return the loaded class; otherwise fall back
  # to the metadata reference.  Broken/unsupported source also falls back.
  def factory(ref, *args, **options)
    cache_key = factory_cache_key(ref, args, options)
    if factory_cached? && (@factory_cache ||= {}).key?(cache_key)
      return @factory_cache[cache_key]
    end

    if options[:from].to_s == "api"
      ensure_api_json_loaded
      begin
        api_formula = formula_class_get_from_api(ref)
        (@factory_cache ||= {})[cache_key] = api_formula if factory_cached?
        return api_formula
      rescue PybrewFormulaUnavailableError
        raise if api_json_configured?
      end
    end

    local_file = ref.to_s
    if File.extname(local_file) == ".rb" && File.file?(local_file)
      loaded = load_formula_from_path(
        File.basename(local_file, ".rb"),
        Pathname.new(local_file),
        flags: options[:flags] || [],
        ignore_errors: options[:ignore_errors] || false
      )
      (@factory_cache ||= {})[cache_key] = loaded if factory_cached?
      return loaded
    end

    tap_alias_formula = resolve_tap_alias(ref)
    unless tap_alias_formula.nil?
      (@factory_cache ||= {})[cache_key] = tap_alias_formula if factory_cached?
      return tap_alias_formula
    end

    local_tap_source = tap_formula_source_for(ref)
    unless local_tap_source.nil?
      tap_name, tap_path = local_tap_source
      loaded = load_formula_from_path(
        tap_name, tap_path,
        flags: options[:flags] || [],
        ignore_errors: options[:ignore_errors] || false
      )
      (@factory_cache ||= {})[cache_key] = loaded if factory_cached?
      return loaded
    end

    reference = Formula[ref]
    alias_reference = factory_alias_reference(ref)
    reference = alias_reference unless alias_reference.nil?
    formula = factory_load_source(reference, options)
    if formula.nil?
      # API aliases/oldnames resolve through the API cache even when the
      # caller did not pass ``from: :api`` (Homebrew's loader order).
      formula = factory_api_alias(ref)
    end
    formula = reference if formula.nil?
    (@factory_cache ||= {})[cache_key] = formula if factory_cached?
    formula
  end

  def tap_name_for_root(root)
    parts = Pathname.new(root.to_s).each_filename.to_a
    return parts.last(2).join("/") if parts.length >= 2
    return parts.last.to_s if parts.length == 1

    "(unknown)"
  end

  # Every local tap formula file matching ``ref`` (tap-qualified refs only look
  # at the named tap; plain names look everywhere).
  def tap_formula_candidates(ref)
    raw = ref.to_s
    return [] if raw.start_with?("http://", "https://", "file://")
    return [] if raw.start_with?("/") || raw.include?("\\")

    qualified = raw.match(%r{\A([^/]+/[^/]+)/(.+)\z})
    if qualified.nil? && raw.include?("/")
      return []
    end
    name = qualified.nil? ? File.basename(raw, ".rb") : qualified[2]
    name = File.basename(name, ".rb")
    return [] if name.empty?

    candidates = []
    tap_roots.each do |root|
      tap_name = tap_name_for_root(root)
      next if !qualified.nil? && tap_name != qualified[1]

      formula_dir = root/"Formula"
      next unless formula_dir.directory?

      files = [formula_dir/"#{name}.rb"]
      files.concat(Pathname.glob((formula_dir/"*/#{name}.rb").to_s))
      files.each do |path|
        next unless path.file?

        candidates << [tap_name, name, path]
      end
    end
    candidates.uniq { |entry| entry[2].to_s }
  end

  # Resolve a local tap formula, raising Homebrew-style ambiguity when a plain
  # name exists in more than one tap.  Returns nil when no local tap has it.
  def tap_formula_source_for(ref)
    return nil if ref.is_a?(Pathname)

    candidates = tap_formula_candidates(ref)
    return nil if candidates.empty?

    if candidates.length > 1
      full_names = candidates.map { |tap_name, name, _path| "#{tap_name}/#{name}" }.sort
      raise PybrewFormulaAmbiguityError,
            "Formulae found in multiple taps: #{full_names.join(", ")}; " \
            "use a fully-qualified name (tap/formula)"
    end

    _tap_name, name, path = candidates.first
    [name, path]
  end

  # Loader choice matrix mirroring Homebrew's loader order
  # (bottle -> uri -> api -> tap -> path -> name -> keg).
  def loader_matrix(ref)
    raw = ref.to_s
    matrix = {}
    matrix[:bottle] = File.file?(raw) && /\A.+?\.bottle(?:\.tar\.gz)?\z/.match?(raw)
    matrix[:uri] = raw.start_with?("http://", "https://", "file://")
    matrix[:api] = begin
      if api_json_configured?
        ensure_api_json_loaded
        !formula_class_get_from_api(File.basename(raw, ".rb")).nil?
      else
        false
      end
    rescue ScriptError, StandardError
      false
    end
    taps = begin
      tap_formula_candidates(ref)
    rescue ScriptError, StandardError
      []
    end
    matrix[:tap] = taps.length == 1
    matrix[:tap_ambiguous] = taps.length > 1
    matrix[:path] = File.file?(raw) && File.extname(raw) == ".rb"
    matrix[:alias] = begin
      !tap_alias_targets(File.basename(raw, ".rb")).empty?
    rescue ScriptError, StandardError
      false
    end
    matrix[:keg] = begin
      rack_name = File.basename(raw, ".rb").split("/").last
      (HOMEBREW_CELLAR/rack_name).directory?
    rescue ScriptError, StandardError
      false
    end
    matrix[:name] = !matrix[:path] && !matrix[:uri] && !matrix[:bottle]
    if matrix[:tap_ambiguous]
      matrix[:ambiguity] = taps.map { |tap_name, name, _path| "#{tap_name}/#{name}" }.sort
    end
    matrix
  end

  def loader_choice(ref)
    matrix = loader_matrix(ref)
    return :bottle if matrix[:bottle]
    return :uri if matrix[:uri]
    return :api if matrix[:api]
    return :tap if matrix[:tap]
    return :path if matrix[:path]
    return :alias if matrix[:alias]
    return :keg if matrix[:keg] && !matrix[:api] && !matrix[:tap] && !matrix[:alias]
    return :name if matrix[:name]
    return :keg if matrix[:keg]

    :name
  end

  # Look up an API alias/rename in the bounded API cache; nil when no API is
  # configured or the name is unknown.  Never raises for missing metadata.
  def factory_api_alias(ref)
    return nil unless api_json_configured?

    begin
      ensure_api_json_loaded
      formula_class_get_from_api(ref)
    rescue PybrewFormulaUnavailableError
      nil
    end
  end

  # Bounded ``Formulary.resolve``: resolve installed specs/tabs and installed
  # aliases like Homebrew, while keeping source loading in ``factory``.
  # ``follow_installed_alias`` is always disabled because a caller resolving a
  # name is referring to that specific formula.
  def resolve(ref, spec = nil, force_bottle: false, flags: [], **options)
    name = ref.to_s
    formula = if name.include?("/") || File.exist?(name)
      factory(ref, spec || :stable, force_bottle: force_bottle, flags: flags, **options)
    else
      rack = to_rack(ref)
      if rack.nil?
        factory(ref, spec || :stable, force_bottle: force_bottle, flags: flags, **options)
      else
        alias_path = begin
          factory(ref, force_bottle: force_bottle, flags: flags, **options).alias_path
        rescue ScriptError, StandardError
          nil
        end
        from_rack(rack, spec, alias_path: alias_path, force_bottle: force_bottle, flags: flags)
      end
    end

    if formula.respond_to?(:any_version_installed?) && formula.any_version_installed?
      tab = begin
        Tab.for_formula(formula)
      rescue ScriptError, StandardError
        nil
      end
      unless tab.nil?
        resolved_spec = spec
        resolved_spec ||= tab.spec if tab.respond_to?(:spec)
        if !resolved_spec.nil? && formula.respond_to?(resolved_spec) &&
           formula.public_send(resolved_spec) && formula.respond_to?(:active_spec=)
          formula.active_spec = resolved_spec
        end
        formula.build = tab if formula.respond_to?(:build=)
        if formula.respond_to?(:head?) && formula.head? &&
           tab.respond_to?(:tabfile) && !tab.tabfile.nil?
          begin
            keg = Keg.new(tab.tabfile.parent)
            if keg.version.respond_to?(:head?) && keg.version.head? &&
               formula.version.respond_to?(:update_commit)
              formula.version.update_commit(keg.version.version.commit)
            end
          rescue ScriptError, StandardError
            nil
          end
        end
      end
    end

    formula.follow_installed_alias = false if formula.respond_to?(:follow_installed_alias=)
    formula
  end

  def factory_cache_key(ref, args, options)
    spec = args.empty? ? :stable : args.first
    "#{ref}-#{spec}-#{options[:alias_path]}-#{options[:from]}"
  end

  # Bounded alias/oldname fallback: when the requested name has no metadata
  # entry but another configured formula declares it as an alias/oldname, use
  # that canonical metadata reference.  No broad tap/alias resolution.
  def factory_alias_reference(ref)
    name = ref.to_s
    entries = PYBREW_CONFIG["formulae"] || {}
    entry = entries[name]
    return nil unless entry.nil? || entry.empty?

    entries.each do |canonical_name, info|
      next if info.nil?

      aliases = Array(info["aliases"]).map(&:to_s)
      oldname = info["oldname"].to_s
      return Formula[canonical_name] if aliases.include?(name) || oldname == name
    end
    nil
  end

  API_JSON_ENV = "PYBREW_FORMULA_API_JSON"
  API_URL_ENV = "PYBREW_FORMULA_API_URL"
  API_CURL_ENV = "PYBREW_FORMULA_API_CURL"
  API_TOKEN_ENV = "PYBREW_FORMULA_API_TOKEN"
  API_SIDECAR_ENV = "PYBREW_FORMULA_API_SIDECAR"
  API_CHANNELS_ENV = "PYBREW_FORMULA_API_CHANNELS"
  API_JWS_KEY_ENV = "PYBREW_FORMULA_API_JWS_KEY"
  TAPS_ENV = "PYBREW_TAPS"

  # Bounded local Homebrew API JSON support.  The file is expected to contain
  # a single formula struct, an array of structs, or {"formulae": [...]}.
  def api_json_path
    configured = ENV[API_JSON_ENV].to_s.strip
    return Pathname.new(configured) unless configured.empty?

    candidate = HOMEBREW_PREFIX/"var/homebrew/api/formula.jws.json"
    candidate.file? ? candidate : nil
  end

  def api_json_configured?
    !ENV[API_JSON_ENV].to_s.strip.empty? || !api_json_path.nil? ||
      !api_json_url.nil? || api_channels_configured?
  end

  def api_json_channels
    raw = ENV[API_CHANNELS_ENV].to_s.strip
    channels = raw.split(",").map(&:strip).reject(&:empty?)
    channels.empty? ? ["stable"] : channels.uniq
  end

  def api_channel_suffix(channel)
    channel.to_s.upcase.gsub(/[^A-Z0-9]/, "_")
  end

  def api_channels_configured?
    channels = api_json_channels
    return true if channels.length > 1

    channels.any? do |channel|
      suffix = api_channel_suffix(channel)
      !ENV["#{API_JSON_ENV}_#{suffix}"].to_s.strip.empty? ||
        !ENV["#{API_URL_ENV}_#{suffix}"].to_s.strip.empty?
    end
  end

  def api_json_path_for_channel(channel)
    suffix = api_channel_suffix(channel)
    raw = ENV["#{API_JSON_ENV}_#{suffix}"].to_s.strip
    return Pathname.new(raw) unless raw.empty?

    api_json_path
  end

  def api_json_url_for_channel(channel)
    suffix = api_channel_suffix(channel)
    raw = ENV["#{API_URL_ENV}_#{suffix}"].to_s.strip
    return nil if raw.empty?
    unless raw.start_with?("http://", "https://")
      raise PybrewFormulaUnavailableError,
            "invalid #{API_URL_ENV}_#{suffix} #{raw.inspect}: only http/https URLs are supported"
    end

    raw
  end

  # Select one channel's payload.  Supports flat structs, ``{channel_name:
  # ...}``, and a nested ``{"channels": {...}}`` manifest, which lets a
  # multi-channel file carry per-channel subsets.
  def api_channel_data(data, channel)
    return data unless data.is_a?(Hash)

    candidates = [data[channel.to_s], data[channel.to_sym]]
    channels = data["channels"] || data[:channels]
    if channels.is_a?(Hash)
      candidates.concat([channels[channel.to_s], channels[channel.to_sym]])
    end
    selected = candidates.find do |value|
      value.is_a?(Array) || value.is_a?(Hash)
    end
    selected.nil? ? data : selected
  end

  # Deep-merge a later channel's struct over an earlier one, so a channel can
  # carry a subset of fields (e.g. only a version bump) without erasing the
  # shared metadata.  Arrays and scalars are replaced by the later value.
  def merge_api_struct(base, override)
    return override unless base.is_a?(Hash) && override.is_a?(Hash)

    base.merge(override) do |_key, old_value, new_value|
      if old_value.is_a?(Hash) && new_value.is_a?(Hash)
        merge_api_struct(old_value, new_value)
      else
        new_value
      end
    end
  end

  def api_channels_signature
    api_json_channels.map do |channel|
      suffix = api_channel_suffix(channel)
      path_text = ENV["#{API_JSON_ENV}_#{suffix}"].to_s.strip
      if path_text.empty?
        default_path = api_json_path
        path_text = default_path.nil? ? "" : default_path.to_s
      end
      url_text = ENV["#{API_URL_ENV}_#{suffix}"].to_s.strip
      url_text = api_json_url.to_s if url_text.empty? && !api_json_url.nil?
      if !path_text.empty? && File.file?(path_text)
        "#{channel}:file:#{path_text}:#{File.mtime(path_text).to_f}"
      else
        "#{channel}:url:#{url_text}"
      end
    end.join("|")
  end

  # Load every configured channel, merging duplicate formula names field-wise
  # (later channels win) before mapping structs to classes.  This keeps
  # channel files able to carry subsets of the full struct set.
  # Deterministic per-channel namespace identifier; used for diagnostics and
  # to prove channel subsets stay isolated while merging by formula name.
  def api_channel_namespace(channel)
    "FormulaNamespaceAPIChannel#{api_channel_suffix(channel)}"
  end

  def api_channel_index
    @api_channel_index ||= {}
  end

  def api_channel_for_formula(name)
    key = name.to_s.split("/").last
    Array(api_channel_index[key])
  end

  def load_formula_from_api_channels!
    entries_by_name = {}
    channels_by_name = {}
    order = []
    api_json_channels.each do |channel|
      suffix = api_channel_suffix(channel)
      channel_path = ENV["#{API_JSON_ENV}_#{suffix}"].to_s.strip
      channel_url = ENV["#{API_URL_ENV}_#{suffix}"].to_s.strip
      if !channel_path.empty?
        file = Pathname.new(channel_path)
        unless file.file?
          raise PybrewFormulaUnavailableError,
                "API JSON file for channel #{channel.inspect} not found: #{file}"
        end
        data = api_json_text_data(file.open("r") { |io| ensure_utf8_encoding(io).read }, file)
        entries = api_json_entries(
          api_channel_data(data, channel), "channel #{channel}", allow_empty: true
        )
      elsif !channel_url.empty?
        unless channel_url.start_with?("http://", "https://")
          raise PybrewFormulaUnavailableError,
                "invalid API URL for channel #{channel.inspect}: only http/https are supported"
        end
        entries = remote_api_json_entries(channel_url)
      else
        file = api_json_path
        url = api_json_url
        if !file.nil? && file.file?
          data = api_json_text_data(file.open("r") { |io| ensure_utf8_encoding(io).read }, file)
          entries = api_json_entries(
            api_channel_data(data, channel), "channel #{channel}", allow_empty: true
          )
        elsif !url.nil?
          entries = remote_api_json_entries
        else
          raise PybrewFormulaUnavailableError,
                "no API JSON source configured for channel #{channel.inspect}"
        end
      end
      entries.each do |entry|
        name = api_struct_name(entry)
        channels_by_name[name] ||= []
        channels_by_name[name] << channel unless channels_by_name[name].include?(channel)
        if entries_by_name.key?(name)
          entries_by_name[name] = merge_api_struct(entries_by_name[name], entry)
        else
          entries_by_name[name] = entry
          order << name
        end
      end
    end
    if order.empty?
      raise PybrewFormulaUnavailableError,
            "no formula structs found in configured API channels " \
            "(#{api_json_channels.join(", ")})"
    end

    api_channel_index.clear
    loaded = order.map do |name|
      entry = entries_by_name[name]
      klass = load_formula_from_struct!(api_struct_name(entry), entry)
      klass.loaded_channels = channels_by_name[name] if klass.respond_to?(:loaded_channels=)
      klass.loaded_channel = channels_by_name[name].first if klass.respond_to?(:loaded_channel=)
      api_channel_index[name] = channels_by_name[name]
      klass
    end
    values = loaded.compact
    values.length == 1 ? values.first : values
  end

  def api_json_url
    raw = ENV[API_URL_ENV].to_s.strip
    return nil if raw.empty?
    unless raw.start_with?("http://", "https://")
      raise PybrewFormulaUnavailableError,
            "invalid #{API_URL_ENV} #{raw.inspect}: only http/https URLs are supported"
    end

    raw
  end

  # Bounded remote API JSON transport.  Uses curl (or a fake transport via
  # PYBREW_FORMULA_API_CURL) and validates the same struct shapes as local
  # files.  No network happens unless PYBREW_FORMULA_API_URL is set.
  def jws_compact?(text)
    stripped = text.to_s.strip
    return false if stripped.empty?

    stripped.split(".", -1).length == 3 &&
      !stripped.match(/\A[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*\z/).nil?
  end

  # Homebrew's signed API endpoints use the embedded ``homebrew-1`` RSA
  # public key (RS*/PS* JWS).  It is public data and only used to verify
  # downloaded payloads; ``PYBREW_FORMULA_API_JWS_KEY`` can override it.
  JWS_PUBLIC_KEY_PEM = <<~'PEM'
    -----BEGIN PUBLIC KEY-----
    MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAyKoOYzp1rhwXISRi61BY
    XBEr2PalSK8lEVOL2USy7mpy0OubOlFyujawyQcBcCn+uPOJ/WaK+POhNWcLLoiK
    L2m8GViaQm7SMwdLKUXFgKSPHcG/1m6Vu+TNBKTfQqT60PjEYIrn5NW9ZrM0cUhK
    REmsbeAMBevdSaW9UwY9iIhprrgovvT8SzKhF8ZOIZKXfJX4VNk0y/7VJYNuGGqH
    3npxV7OKd4yTGRGqFcC9kJ84me3thiu0yqlOjASmfWIwIwcfp4j6BEM2LuqKd7yX
    h51/O+MTthkuxV36moDKfdgdOFsvlCFkziaYLScCX9lOlmZHtOfJTAOXxTmM7qGr
    wTGK0vhvTi8k9dBmH/dccredQBtPOfM/FEdeyakGLoTcDguiBS/4El3I2KtF6B2h
    OGoBumR915/cI4drr5yPMduZ7gjs7ZEZnVkeVzic24TfUHpnOYzrhucNJtHMBDj9
    6d1Gk82AhtuF9KlusLmCb6qXCWQSp/A4RZpN37E/p9q8rLp/7B/zp8X2TVvecPNy
    BdMagdktdEqK7WPlYMcUp56JaOph8vqYoU+oGyCpWoLvcXFb75o4eefuu6Rs5SyM
    c9JCCJ0DDFPjCRFnGPkvsKxFCzMFqH1jpWH0RQIrgmNVM5PO84iRH9YJsSPQzpMj
    KvK/ZH4YgR9wNkBNagFo7lsCAwEAAQ==
    -----END PUBLIC KEY-----
  PEM

  def decode_jws_segment(segment)
    padded = segment.to_s.tr("-_", "+/")
    padded += "=" * ((4 - padded.length % 4) % 4)
    Base64.strict_decode64(padded)
  rescue ScriptError, StandardError => error
    raise PybrewFormulaUnavailableError,
          "malformed API JWS segment: #{error.class}: #{error.message}"
  end

  def encode_jws_segment(value)
    Base64.urlsafe_encode64(value.to_s, padding: false)
  end

  def jws_digest_for(alg)
    case alg.to_s
    when "RS256", "PS256", "HS256" then "SHA256"
    when "RS384", "PS384" then "SHA384"
    when "RS512", "PS512" then "SHA512"
    end
  end

  # Parsed-key cache keyed by PEM digest; cleared by Formulary.clear_cache.
  def jws_public_key_cache
    @jws_public_key_cache ||= {}
  end

  def jws_public_key_cache_size
    jws_public_key_cache.length
  end

  def jws_public_key
    pem = jws_public_key_pem
    return nil if pem.nil? || pem.empty?

    digest = Digest::SHA256.hexdigest(pem)
    cached = jws_public_key_cache[digest]
    return cached unless cached.nil?

    begin
      key = OpenSSL::PKey::RSA.new(pem)
    rescue ScriptError, StandardError => error
      raise PybrewFormulaUnavailableError,
            "invalid API JWS public key: #{error.class}: #{error.message}"
    end
    jws_public_key_cache[digest] = key
    key
  end

  def jws_public_key_pem
    raw = ENV["PYBREW_FORMULA_API_JWS_PUBLIC_KEY"].to_s.strip
    if raw.empty?
      candidate = ENV[API_JWS_KEY_ENV].to_s.strip
      raw = candidate if candidate.include?("BEGIN PUBLIC KEY") ||
                          candidate.include?("BEGIN RSA PUBLIC KEY")
    end
    if raw.empty?
      embedded = JWS_PUBLIC_KEY_PEM.to_s.strip
      return embedded unless embedded.empty?

      return nil
    end
    if !raw.include?("BEGIN")
      path = Pathname.new(raw)
      return path.read.strip if path.file?
    end
    raw
  end

  # Verify one JWS signature (compact or JSON-envelope form) with a bounded
  # algorithm allow-list: HS256 (injectable shared secret) plus RSA
  # RS*/PS* using ``jws_public_key_pem``.
  def jws_signature_valid?(alg, signing_input, signature, payload_segment)
    digest = jws_digest_for(alg)
    if digest.nil?
      raise PybrewFormulaUnavailableError,
            "unsupported API JWS alg #{alg.to_s.inspect}: pybrew verifies " \
            "HS256/RS256/RS384/RS512/PS256/PS384/PS512"
    end

    if alg.to_s == "HS256"
      key = ENV[API_JWS_KEY_ENV].to_s
      if key.empty?
        raise PybrewFormulaUnavailableError,
              "API JWS verification requires #{API_JWS_KEY_ENV} (HS256); " \
              "refusing to trust an unverified payload"
      end
      expected = OpenSSL::HMAC.digest("SHA256", key, signing_input)
      return false unless expected.bytesize == signature.bytesize

      if OpenSSL.respond_to?(:fixed_length_secure_compare)
        return OpenSSL.fixed_length_secure_compare(expected, signature)
      end

      return expected == signature
    end

    key = jws_public_key
    if key.nil?
      raise PybrewFormulaUnavailableError,
            "API JWS verification requires a public key for #{alg}: set " \
            "PYBREW_FORMULA_API_JWS_PUBLIC_KEY (PEM or path) or " \
            "#{API_JWS_KEY_ENV} to a PEM value"
    end

    if alg.to_s.start_with?("PS")
      unless key.respond_to?(:verify_pss)
        raise PybrewFormulaUnavailableError,
              "API JWS alg #{alg} requires OpenSSL::PKey::RSA#verify_pss " \
              "(missing on this Ruby)"
      end
      key.verify_pss(digest, signature, signing_input,
                     salt_length: :digest, mgf1_hash: digest)
    else
      key.verify(digest, signature, signing_input)
    end
  rescue OpenSSL::PKey::PKeyError => error
    raise PybrewFormulaUnavailableError,
          "API JWS signature verification failed: #{error.class}: #{error.message}"
  end

  def jws_envelope?(data)
    data.is_a?(Hash) && data["payload"].is_a?(String) &&
      data["signatures"].is_a?(Array) && !data["signatures"].empty?
  end

  def verify_formula_jws!(text)
    parts = text.to_s.strip.split(".", -1)
    if parts.length != 3
      raise PybrewFormulaUnavailableError,
            "malformed API JWS: expected three dot-separated segments"
    end
    header_segment, payload_segment, signature_segment = parts
    begin
      header = JSON.parse(decode_jws_segment(header_segment).force_encoding("UTF-8"))
    rescue PybrewFormulaUnavailableError
      raise
    rescue ScriptError, StandardError => error
      raise PybrewFormulaUnavailableError,
            "malformed API JWS header: #{error.class}: #{error.message}"
    end
    alg = header.is_a?(Hash) ? header["alg"].to_s : ""
    signature = decode_jws_segment(signature_segment)
    signing_input = "#{header_segment}.#{payload_segment}"
    valid = jws_signature_valid?(alg, signing_input, signature, payload_segment)
    unless valid
      raise PybrewFormulaUnavailableError, "API JWS signature verification failed"
    end
    begin
      decode_jws_segment(payload_segment).force_encoding("UTF-8")
    rescue PybrewFormulaUnavailableError
      raise
    rescue ScriptError, StandardError => error
      raise PybrewFormulaUnavailableError,
            "malformed API JWS payload: #{error.class}: #{error.message}"
    end
  end

  # Homebrew's current API uses the JSON (RFC 7515 general) envelope form with
  # a ``homebrew-1`` signature and unencoded (b64: false) payload.
  def verify_formula_jws_envelope!(data)
    payload = data["payload"].to_s
    signatures = Array(data["signatures"]).select { |entry| entry.is_a?(Hash) }
    signature = signatures.find do |entry|
      header = entry["header"]
      header.is_a?(Hash) && header["kid"].to_s == "homebrew-1"
    end
    signature ||= signatures.first
    if signature.nil?
      raise PybrewFormulaUnavailableError,
            "malformed API JWS: no signature entry found"
    end

    protected_segment = signature["protected"].to_s
    signature_segment = signature["signature"].to_s
    if protected_segment.empty? || signature_segment.empty?
      raise PybrewFormulaUnavailableError,
            "malformed API JWS: protected/signature segments are required"
    end
    begin
      header = JSON.parse(decode_jws_segment(protected_segment).force_encoding("UTF-8"))
    rescue PybrewFormulaUnavailableError
      raise
    rescue ScriptError, StandardError => error
      raise PybrewFormulaUnavailableError,
            "malformed API JWS header: #{error.class}: #{error.message}"
    end
    alg = header.is_a?(Hash) ? header["alg"].to_s : ""
    b64_encoded = header.is_a?(Hash) && header.key?("b64") ? header["b64"] : true
    signing_input = if b64_encoded
      "#{protected_segment}.#{encode_jws_segment(payload)}"
    else
      "#{protected_segment}.#{payload}"
    end
    valid = jws_signature_valid?(
      alg, signing_input, decode_jws_segment(signature_segment), nil
    )
    unless valid
      raise PybrewFormulaUnavailableError, "API JWS signature verification failed"
    end
    payload
  end

  def parse_jws_payload(payload, source)
    JSON.parse(payload.to_s)
  rescue ScriptError, StandardError => error
    raise PybrewFormulaUnavailableError,
          "malformed JWS payload from #{source}: #{error.class}: #{error.message}"
  end

  def api_json_text_data(text, source, remote: false)
    stripped = text.to_s
    if jws_compact?(stripped)
      return parse_jws_payload(verify_formula_jws!(stripped), source)
    end
    begin
      data = JSON.parse(stripped)
    rescue ScriptError, StandardError => error
      prefix = remote ? "malformed API JSON from" : "malformed API JSON at"
      raise PybrewFormulaUnavailableError,
            "#{prefix} #{source}: #{error.class}: #{error.message}"
    end
    if jws_envelope?(data)
      return parse_jws_payload(verify_formula_jws_envelope!(data), source)
    end
    data
  end

  def remote_sidecar_path
    raw = ENV[API_SIDECAR_ENV].to_s.strip
    return nil if raw.empty?

    Pathname.new(raw)
  end

  def remote_sidecar_read(url)
    path = remote_sidecar_path
    return nil if path.nil? || !path.file?

    data = JSON.parse(path.read)
    return nil unless data.is_a?(Hash) && data["url"] == url && data["entries"].is_a?(Array)

    {
      url: data["url"],
      entries: data["entries"],
      etag: data["etag"].to_s,
      last_modified: data["last_modified"].to_s,
      content_hash: data["content_hash"].to_s,
    }
  rescue ScriptError, StandardError
    nil
  end

  def remote_sidecar_write(url:, entries:, etag:, last_modified:, content_hash:)
    path = remote_sidecar_path
    return nil if path.nil?

    path.dirname.mkpath
    path.write(JSON.pretty_generate({
      "url" => url,
      "etag" => etag,
      "last_modified" => last_modified,
      "content_hash" => content_hash,
      "entries" => entries,
    }) + "\n")
    nil
  rescue ScriptError, StandardError
    nil
  end

  def remote_sidecar_clear!
    path = remote_sidecar_path
    path.unlink if !path.nil? && path.file?
    nil
  rescue ScriptError, StandardError
    nil
  end

  def remote_api_json_entries(url_override = nil)
    override = url_override.nil? ? "" : url_override.to_s.strip
    url = override.empty? ? api_json_url : override
    return nil if url.nil?
    unless url.start_with?("http://", "https://")
      raise PybrewFormulaUnavailableError,
            "invalid API URL #{url.inspect}: only http/https URLs are supported"
    end

    memory = (@remote_api_json_cache ||= {})
    cached = memory[url]
    cached = remote_sidecar_read(url) if cached.nil?

    transport = ENV[API_CURL_ENV].to_s.strip
    transport = "curl" if transport.empty?
    header_file = Tempfile.new(["pybrew-api-headers", ".txt"])
    begin
      command = [transport, "-fsSL", "--max-time", "30", "-D", header_file.path.to_s]
      if !cached.nil? && !cached[:etag].to_s.empty?
        command += ["-H", "If-None-Match: #{cached[:etag]}"]
      end
      if !cached.nil? && !cached[:last_modified].to_s.empty?
        command += ["-H", "If-Modified-Since: #{cached[:last_modified]}"]
      end
      token_value = ENV[API_TOKEN_ENV].to_s
      command += ["-H", "Authorization: Bearer #{token_value}"] unless token_value.empty?
      command << url

      begin
        text = Utils.popen_read(*command, safe: true).to_s
      rescue ScriptError, StandardError
        raise PybrewFormulaUnavailableError,
              "failed to fetch API JSON from #{url} (transport failed; " \
              "credentials are never included in this message)"
      end

      headers = File.file?(header_file.path.to_s) ? File.read(header_file.path.to_s) : ""
      statuses = headers.scan(/\AHTTP\/\S+\s+(\d{3})/).flatten
      status = statuses.empty? ? "200" : statuses.last
      if status == "304"
        if cached.nil? || !cached[:entries].is_a?(Array)
          raise PybrewFormulaUnavailableError,
                "HTTP 304 for #{url} but no cached API JSON is available"
        end
        memory[url] = cached
        return cached[:entries]
      end
      if status.to_i >= 400
        raise PybrewFormulaUnavailableError,
              "failed to fetch API JSON from #{url}: HTTP #{status}"
      end
      if text.strip.empty?
        raise PybrewFormulaUnavailableError,
              "failed to fetch API JSON from #{url} using #{transport.inspect} " \
              "(empty response); set #{API_CURL_ENV} to a test transport if needed"
      end

      data = api_json_text_data(text, url, remote: true)
      entries = api_json_entries(data, url)
      etag = headers[/^ETag:\s*(.+)$/i, 1].to_s.strip
      last_modified = headers[/^Last-Modified:\s*(.+)$/i, 1].to_s.strip
      content_hash = Digest::SHA256.hexdigest(text)
      memory[url] = {
        url: url,
        entries: entries,
        etag: etag,
        last_modified: last_modified,
        content_hash: content_hash,
      }
      remote_sidecar_write(
        url: url,
        entries: entries,
        etag: etag,
        last_modified: last_modified,
        content_hash: content_hash
      )
      entries
    ensure
      header_file.close!
    end
  end

  def load_api_entries!(entries)
    loaded = entries.map do |entry|
      load_formula_from_struct!(api_struct_name(entry), entry)
    end
    loaded.length == 1 ? loaded.first : loaded
  end

  def api_json_file_entries(file)
    path = Pathname.new(file.to_s)
    mtime = File.mtime(path.to_s).to_f
    cached = (@api_json_cache ||= {})
    return cached[:entries] if cached[:path] == path.to_s && cached[:mtime] == mtime

    begin
      text = path.open("r") { |io| ensure_utf8_encoding(io).read }
    rescue ScriptError, StandardError => error
      raise PybrewFormulaUnavailableError,
            "failed to read API JSON at #{path}: #{error.class}: #{error.message}"
    end
    data = api_json_text_data(text, path)
    entries = api_json_entries(data, path)
    @api_json_cache = { path: path.to_s, mtime: mtime, entries: entries }
    entries
  end

  def api_json_entries(data, source = nil, allow_empty: false)
    entries = case data
              when Hash
                if data["formulae"].is_a?(Array)
                  data["formulae"]
                elsif data["formulae"].is_a?(Hash)
                  # Channel/subset manifests may key structs by name.
                  data["formulae"].map do |name, entry|
                    if entry.is_a?(Hash) && (entry["name"] || entry[:name]).to_s.empty?
                      entry.merge("name" => name.to_s)
                    else
                      entry
                    end
                  end
                elsif data["formula"].is_a?(Hash)
                  [data["formula"]]
                elsif data["name"] || data[:name] || data["full_name"] || data[:full_name]
                  [data]
                else
                  raise PybrewFormulaUnavailableError,
                        "malformed API JSON at #{source}: expected a formula struct, " \
                        "an array of structs, or {\"formulae\": [...]}"
                end
              when Array
                data
              else
                raise PybrewFormulaUnavailableError,
                      "malformed API JSON at #{source}: expected an object or array"
              end
    entries = entries.select { |entry| entry.is_a?(Hash) }
    if entries.empty? && !allow_empty
      raise PybrewFormulaUnavailableError,
            "malformed API JSON at #{source}: no formula structs found"
    end
    entries
  end

  def api_struct_name(entry)
    name = entry["name"] || entry[:name]
    name = entry["full_name"] || entry[:full_name] if name.nil? || name.to_s.empty?
    if name.nil? || name.to_s.empty?
      raise PybrewFormulaUnavailableError,
            "malformed API JSON struct: formula entry is missing name/full_name"
    end
    name.to_s.split("/").last
  end

  # Map local API JSON into PybrewLoadedFormula classes via the bounded
  # load_formula_from_struct! cache path.  Raises clear errors for missing or
  # malformed files/structs.
  def load_formula_from_api!(source = nil, path: nil)
    file = nil
    if source.is_a?(Hash) || source.is_a?(Array)
      entries = api_json_entries(source, path || "inline API JSON")
    else
      file = path
      if file.nil? && (source.is_a?(String) || source.is_a?(Pathname))
        file = source
      end
      file = api_json_path if file.nil?
      if file.nil?
        entries = remote_api_json_entries
        if entries.nil?
          raise PybrewFormulaUnavailableError,
                "API JSON file not found: (none configured); set #{API_JSON_ENV} " \
                "to a local formula JSON file or #{API_URL_ENV} to an http(s) URL"
        end
      else
        file = Pathname.new(file.to_s) unless file.is_a?(Pathname)
        if !file.file?
          raise PybrewFormulaUnavailableError,
                "API JSON file not found: #{file}; set #{API_JSON_ENV} to a local " \
                "formula JSON file or #{API_URL_ENV} to an http(s) URL"
        end
        entries = api_json_file_entries(file)
      end
    end

    loaded = load_api_entries!(entries)
    if !file.nil? && file.file?
      @api_json_loaded = { path: file.to_s, mtime: File.mtime(file.to_s).to_f }
    end
    loaded
  end

  API_TTL_ENV = "PYBREW_FORMULA_API_TTL"

  # Optional cache lifetime for channeled API JSON (seconds).  Unset keeps the
  # historical signature-only caching; 0 forces a reload on every call.
  def api_json_ttl_seconds
    raw = ENV[API_TTL_ENV].to_s.strip
    return nil if raw.empty?

    raw.to_i
  end

  def api_json_ttl_expired?(loaded)
    ttl = api_json_ttl_seconds
    return false if ttl.nil?
    return true if ttl <= 0

    fetched_at = loaded[:fetched_at]
    return true if fetched_at.nil?

    (Time.now.to_f - fetched_at.to_f) >= ttl
  end

  def ensure_api_json_loaded
    if api_channels_configured?
      signature = api_channels_signature
      loaded = @api_json_loaded
      if loaded && loaded[:channels_signature] == signature &&
         !api_json_ttl_expired?(loaded)
        return nil
      end

      load_formula_from_api_channels!
      @api_json_loaded = {
        channels_signature: signature,
        fetched_at: Time.now.to_f,
      }
      return nil
    end

    file = api_json_path
    if !file.nil? && file.file?
      mtime = File.mtime(file.to_s).to_f
      loaded = @api_json_loaded
      return nil if loaded && loaded[:path] == file.to_s && loaded[:mtime] == mtime

      load_formula_from_api!(path: file)
      @api_json_loaded = { path: file.to_s, mtime: mtime }
      return nil
    end

    url = api_json_url
    return nil if url.nil?

    loaded = @api_json_loaded
    return nil if loaded && loaded[:url] == url

    load_api_entries!(remote_api_json_entries)
    @api_json_loaded = { url: url }
    nil
  end

  # Local tap roots come from the pybrew config payload and/or PYBREW_TAPS.
  def tap_roots
    roots = []
    Array(PYBREW_CONFIG["taps"]).each { |tap| roots << Pathname.new(tap.to_s) }
    ENV[TAPS_ENV].to_s.split(File::PATH_SEPARATOR).each do |tap|
      roots << Pathname.new(tap.to_s) unless tap.to_s.empty?
    end
    roots.select(&:directory?).map { |root| root.expand_path }.uniq
  end

  def tap_alias_paths
    tap_roots.flat_map do |root|
      aliases_dir = root/"Aliases"
      next [] unless aliases_dir.directory?

      aliases_dir.children.select { |child| child.file? || child.symlink? }
                 .sort_by { |child| child.basename.to_s }
    end
  end

  def tap_aliases
    (tap_alias_paths.map { |path| path.basename(".rb").to_s } + tap_source_alias_map.keys).uniq.sort
  end

  def tap_source_alias_map
    @tap_source_alias_map ||= begin
      map = {}
      tap_roots.each do |root|
        formula_dir = root/"Formula"
        next unless formula_dir.directory?

        files = formula_dir.children.select { |child| child.file? && child.extname == ".rb" }
        files.sort_by { |child| child.basename.to_s }.first(500).each do |path|
          source = begin
            path.open("r") { |io| ensure_utf8_encoding(io).read }
          rescue ScriptError, StandardError
            nil
          end
          next if source.nil?

          source.scan(/^\s*(?:formula_alias|alias_name)\s+["']([^"']+)["']/) do |match|
            map[match[0]] ||= [path.basename(".rb").to_s, path]
          end
        end
      end
      map
    end
  end

  def tap_source_alias(alias_name)
    tap_source_alias_map[alias_name.to_s.split("/").last]
  end

  def tap_alias_path(alias_name)
    wanted = alias_name.to_s.split("/").last.to_s.sub(/\.rb\z/, "")
    return nil if wanted.empty?

    tap_alias_paths.find { |path| path.basename(".rb").to_s == wanted }
  end

  def tap_alias_target(alias_name)
    alias_path = tap_alias_path(alias_name)
    if alias_path.nil?
      source_alias = tap_source_alias(alias_name)
      return source_alias unless source_alias.nil?

      return nil
    end

    target = alias_path.symlink? ? alias_path.realpath : alias_path
    return nil unless target.file?

    [target.basename(".rb").to_s, target]
  rescue Errno::ENOENT, Errno::EACCES
    nil
  end

  # All distinct local tap alias targets, sorted for determinism.
  def tap_alias_targets(alias_name)
    wanted = alias_name.to_s.split("/").last.to_s.sub(/\.rb\z/, "")
    return [] if wanted.empty?

    targets = []
    tap_alias_paths.each do |alias_path|
      next unless alias_path.basename(".rb").to_s == wanted

      target = alias_path.symlink? ? alias_path.realpath : alias_path
      next unless target.file?

      targets << [target.basename(".rb").to_s, target]
    rescue Errno::ENOENT, Errno::EACCES
      next
    end
    if targets.empty?
      source_alias = tap_source_alias(alias_name)
      targets << source_alias unless source_alias.nil?
    end
    targets.uniq { |_name, path| path.to_s }.sort_by { |_name, path| path.to_s }
  end

  def resolve_tap_alias(alias_name)
    targets = tap_alias_targets(alias_name)
    return nil if targets.length != 1

    canonical, target = targets.first
    load_formula_from_path(canonical, target)
  rescue PybrewFormulaAmbiguityError
    raise
  rescue ScriptError, StandardError
    nil
  end

  def resolve_tap_alias!(alias_name)
    targets = tap_alias_targets(alias_name)
    if targets.empty?
      raise PybrewFormulaUnavailableError,
            "no local tap alias named #{alias_name.inspect} in " \
            "#{tap_roots.join(", ")} (expected Aliases/<name>.rb)"
    end
    if targets.length > 1
      descriptions = targets.map do |canonical, path|
        "#{tap_name_for_root(path.dirname.dirname)}/#{canonical}"
      end.sort
      raise PybrewFormulaAmbiguityError,
            "Alias #{alias_name.inspect} is defined in multiple taps: " \
            "#{descriptions.join(", ")}"
    end

    canonical, target = targets.first
    load_formula_from_path(canonical, target)
  end

  def factory_load_source(reference, options = {})
    return nil unless reference.respond_to?(:path)

    path = reference.path
    return nil if path.nil? || !path.file?

    # A formula class body must not recursively load itself through
    # Formulary.factory; fall back to the metadata reference while loading.
    @factory_loading ||= []
    name = reference.name.to_s
    return nil if @factory_loading.include?(name)

    @factory_loading << name
    begin
      load_formula_from_path(
        name,
        path,
        flags: options[:flags] || [],
        ignore_errors: options[:ignore_errors] || false
      )
    rescue ScriptError, StandardError
      nil
    ensure
      @factory_loading.delete(name)
    end
  end

  # Evaluate formula source inside a real namespace module, like Homebrew's
  # Formulary.  The namespace constant is registered under ``Formulary`` so
  # formula code (and tests) can inspect it, and BUILD_FLAGS mirrors the flags
  # Homebrew exposes to the formula's class scope.
  def load_formula(name, path = nil, contents = nil, namespace = nil,
                   flags: [], ignore_errors: false, from_metadata: false, **_kwargs)
    return Formula[name] if contents.nil?

    namespace_name = if namespace.nil? || namespace.to_s.empty?
      "FormulaNamespace#{namespace_key(path.to_s)}"
    else
      namespace.to_s
    end

    module_object = Module.new
    module_object.const_set(:Formula, PybrewLoadedFormula)
    module_object.const_set(:BUILD_FLAGS, Array(flags).map(&:to_s))
    module_object.const_set(:LOADED_FROM_METADATA, true) if from_metadata
    remove_const(namespace_name) if const_defined?(namespace_name, false)
    const_set(namespace_name, module_object)
    @namespace_constants ||= []
    @namespace_constants << namespace_name unless @namespace_constants.include?(namespace_name)

    before = PybrewLoadedFormula.loaded_classes.length
    begin
      module_object.module_eval(contents.to_s, path.to_s, 1)
    rescue NameError, ArgumentError
      unless ignore_errors
        remove_const(namespace_name) if const_defined?(namespace_name, false)
        @namespace_constants.delete(namespace_name)
        raise
      end
    rescue ScriptError, StandardError => error
      remove_const(namespace_name) if const_defined?(namespace_name, false)
      @namespace_constants.delete(namespace_name)
      raise PybrewFormulaUnreadableError,
            "Formula #{name} could not be loaded from #{path}: " \
            "#{error.class}: #{error.message}"
    end

    expected = class_s(name)
    klass = begin
      candidate = module_object.const_get(expected, false)
      candidate.is_a?(Class) && candidate <= PybrewLoadedFormula ? candidate : nil
    rescue NameError
      nil
    end

    # Tolerate a source file whose class name does not match the formula name
    # as long as it defines exactly one formula subclass in the namespace.
    if klass.nil?
      candidates = (PybrewLoadedFormula.loaded_classes[before..-1] || []).select do |candidate|
        candidate.is_a?(Class) && candidate <= PybrewLoadedFormula
      end
      klass = candidates.last
    end

    if klass.nil?
      remove_const(namespace_name) if const_defined?(namespace_name, false)
      @namespace_constants.delete(namespace_name)
      raise PybrewFormulaClassUnavailableError,
            "Formula #{name} (#{path}) does not define a formula class " \
            "(expected #{expected})"
    end

    klass.loaded_name ||= name.to_s
    klass.loaded_namespace = module_object
    klass.loaded_class_name = expected
    klass.loaded_path ||= Pathname.new(path.to_s) unless path.nil? || path.to_s.empty?
    klass
  end

  def load_formula_from_path(name, path = nil, *_args, flags: [], ignore_errors: false,
                             from_metadata: false, namespace: nil, **_kwargs)
    return Formula[name] if path.nil? || !Pathname.new(path.to_s).file?

    path = Pathname.new(path.to_s)
    flag_list = Array(flags).map(&:to_s)
    cached = path_cache_entry(path)
    if cached &&
       cached[:name] == name.to_s &&
       cached[:mtime] == path_mtime(path) &&
       cached[:flags] == flag_list &&
       cached[:from_metadata] == !!from_metadata &&
       cached[:namespace] == namespace
      return cached[:klass]
    end

    contents = path.open("r") { |file| ensure_utf8_encoding(file).read }
    namespace_name = if namespace.nil? || namespace.to_s.empty?
      "FormulaNamespace#{namespace_key(path.to_s)}"
    else
      namespace.to_s
    end
    klass = load_formula(
      name, path, contents, namespace_name,
      flags: flag_list, ignore_errors: ignore_errors, from_metadata: from_metadata
    )
    cache_path_formula(path, name: name, klass: klass, flags: flag_list,
                       from_metadata: from_metadata, namespace: namespace)
    klass
  end

  def path_mtime(path)
    File.mtime(path.to_s).to_f
  rescue StandardError
    nil
  end

  def path_cache_entry(path)
    (@path_cache ||= {})[path.to_s]
  end

  def cache_path_formula(path, name:, klass:, flags:, from_metadata:, namespace:)
    (@path_cache ||= {})[path.to_s] = {
      klass: klass,
      name: name.to_s,
      mtime: path_mtime(path),
      flags: Array(flags).map(&:to_s),
      from_metadata: !!from_metadata,
      namespace: namespace,
    }
    klass
  end

  # Build a Formulary-loaded class from an API-style formula struct and store
  # it in the API cache for later formula_class_get_from_api lookups.  The
  # mapping stays bounded to the accessors the shim understands.
  def load_formula_from_struct!(name, formula_struct = nil, *_args, **_options)
    return Formula[name] if formula_struct.nil?

    validate_formula_struct!(name, formula_struct)

    klass = Class.new(PybrewLoadedFormula)
    klass.loaded_name = name.to_s
    klass.instance_variable_set(:@loaded_from_metadata, true)
    %i[desc homepage url sha256 version license].each do |attribute|
      next unless formula_struct_responds?(formula_struct, attribute)

      value = formula_struct_read(formula_struct, attribute)
      klass.public_send("loaded_#{attribute}=", formula_struct_text(value))
    end
    if formula_struct_responds?(formula_struct, :dependencies)
      klass.loaded_dependencies = formula_struct_dependencies(
        formula_struct_read(formula_struct, :dependencies)
      )
    end

    cache_api_formula_class!(name, formula_struct, klass)
    cache_api_aliases!(name, formula_struct, klass)
    klass
  end

  def formula_struct_aliases(formula_struct, attribute)
    return [] unless formula_struct_responds?(formula_struct, attribute)

    Array(formula_struct_read(formula_struct, attribute)).map(&:to_s).reject(&:empty?)
  end

  def api_alias_map
    @api_alias_map ||= {}
  end

  # Register API aliases/oldnames (and their tap-qualified forms) so
  # ``Formulary.factory``/``canonical_name`` resolve API aliases like
  # Homebrew's loader does, and keep a canonical-name map for renames.
  def cache_api_aliases!(name, formula_struct, klass)
    @api_cache ||= {}
    aliases = formula_struct_aliases(formula_struct, :aliases)
    renames = formula_struct_aliases(formula_struct, :oldnames) +
              formula_struct_aliases(formula_struct, :oldname)
    tap_prefix = nil
    if formula_struct_responds?(formula_struct, :full_name)
      parts = formula_struct_read(formula_struct, :full_name).to_s.split("/")
      tap_prefix = parts[0..-2].join("/") if parts.length > 1
    end
    (aliases + renames).uniq.each do |alias_name|
      keys = [alias_name]
      keys << "#{tap_prefix}/#{alias_name}" unless tap_prefix.nil? || tap_prefix.empty?
      keys.each do |key|
        @api_cache[key] = klass
        api_alias_map[key] = name.to_s
        api_alias_map[key.split("/").last] = name.to_s
      end
    end
    nil
  end

  # Cache keys for an API formula: the requested name, the struct's own
  # name/full_name (when present) and the struct object itself.  Struct value
  # equality means repeated loads of the same struct hit the same object key.
  def api_cache_keys(name, formula_struct)
    keys = [name.to_s]
    if formula_struct_responds?(formula_struct, :name)
      struct_name = formula_struct_read(formula_struct, :name).to_s
      keys << struct_name unless struct_name.empty?
    end
    if formula_struct_responds?(formula_struct, :full_name)
      full_name = formula_struct_read(formula_struct, :full_name).to_s
      keys << full_name unless full_name.empty?
    end
    keys << formula_struct
    keys.compact
  end

  def formula_struct_responds?(formula_struct, attribute)
    if formula_struct.is_a?(Hash)
      formula_struct.key?(attribute) || formula_struct.key?(attribute.to_s)
    else
      formula_struct.respond_to?(attribute)
    end
  end

  def formula_struct_read(formula_struct, attribute)
    if formula_struct.is_a?(Hash)
      formula_struct.key?(attribute) ? formula_struct[attribute] : formula_struct[attribute.to_s]
    else
      formula_struct.public_send(attribute)
    end
  end

  def api_cache_key(value)
    return nil if value.nil?
    return value.to_s if value.is_a?(String) || value.is_a?(Symbol) || value.is_a?(Pathname)

    value
  end

  def cache_api_formula_class!(name, formula_struct, klass)
    @api_cache ||= {}
    previous = @api_cache[name.to_s]
    @api_cache.delete_if { |_key, value| value.equal?(previous) } unless previous.nil?
    api_cache_keys(name, formula_struct).each { |key| @api_cache[key] = klass }
    klass
  end

  def formula_struct_like?(object)
    return false if object.nil?

    if object.is_a?(Hash)
      return FORMULA_STRUCT_ATTRIBUTES.any? do |attribute|
        object.key?(attribute) || object.key?(attribute.to_s)
      end
    end
    FORMULA_STRUCT_ATTRIBUTES.any? { |attribute| object.respond_to?(attribute) }
  end

  def validate_formula_struct!(name, formula_struct)
    unless formula_struct_like?(formula_struct)
      raise PybrewFormulaUnavailableError,
            "Formulary.load_formula_from_struct! expects an API formula struct " \
            "responding to #{FORMULA_STRUCT_ATTRIBUTES.join('/')} for " \
            "#{name.inspect}; got #{formula_struct.class}"
    end

    missing = FORMULA_STRUCT_REQUIRED_ATTRIBUTES.reject do |attribute|
      formula_struct_responds?(formula_struct, attribute)
    end
    unless missing.empty?
      raise PybrewFormulaUnavailableError,
            "Formulary.load_formula_from_struct! malformed API formula struct for " \
            "#{name.inspect}: missing required accessor(s) #{missing.join('/')}"
    end

    return true unless formula_struct_responds?(formula_struct, :dependencies)

    raw = formula_struct_read(formula_struct, :dependencies)
    return true if raw.nil? || raw.is_a?(Hash) || raw.respond_to?(:each) || raw.respond_to?(:name)

    raise PybrewFormulaUnavailableError,
          "Formulary.load_formula_from_struct! malformed API formula struct for " \
          "#{name.inspect}: dependencies is not enumerable"
  end

  def formula_struct_text(value)
    return nil if value.nil?

    if value.is_a?(Hash)
      fallback = value["default"] || value[:default] || value.values.first
      return fallback.nil? ? nil : fallback.to_s
    end
    value.to_s
  end

  def formula_struct_dependencies(raw)
    dependencies = []
    append = lambda do |dep_name, tags|
      dependencies << [dep_name.to_s, Array(tags).flatten.map(&:to_s)]
    end
    if raw.is_a?(Hash)
      raw.each { |dep_name, tags| append.call(dep_name, tags) }
    else
      Array(raw).each do |dependency|
        if dependency.is_a?(Hash)
          dependency.each { |dep_name, tags| append.call(dep_name, tags) }
        elsif dependency.respond_to?(:name)
          tags = dependency.respond_to?(:tags) ? dependency.tags : []
          append.call(dependency.name, tags)
        else
          append.call(dependency, [])
        end
      end
    end
    dependencies
  end

  def class_s(name)
    class_name = name.to_s.capitalize
    class_name.gsub!(/[-_.\s][a-zA-Z0-9]/) { |matched| matched.chars.fetch(-1).upcase }
    class_name.tr!("+", "x")
    class_name.sub!(/(.)@(\d)/, "\\1AT\\2")
    class_name
  end

  def from_rack(rack, spec = nil, alias_path: nil, force_bottle: false, flags: [], keg: nil)
    keg ||= Keg.from_rack(rack)
    return from_keg(keg, spec, alias_path: alias_path, force_bottle: force_bottle, flags: flags) unless keg.nil?

    factory(
      Pathname.new(rack.to_s).basename.to_s,
      spec || :stable,
      alias_path: alias_path,
      from: :rack,
      force_bottle: force_bottle,
      flags: flags
    )
  end

  def keg_only?(rack)
    factory(Pathname.new(rack.to_s).basename.to_s).keg_only?
  rescue StandardError
    false
  end

  def formula_class_defined_from_path?(path)
    (@path_cache ||= {}).key?(path.to_s)
  end

  def formula_class_defined_from_api?(name)
    key = api_cache_key(name)
    return false if key.nil?

    (@api_cache ||= {}).key?(key)
  end

  def formula_class_get_from_path(path)
    pathname = Pathname.new(path.to_s)
    cached = path_cache_entry(pathname)
    if cached.nil?
      raise PybrewFormulaUnavailableError,
            "no formula class has been loaded from #{path}; " \
            "call Formulary.load_formula_from_path first"
    end

    current_mtime = path_mtime(pathname)
    if current_mtime.nil?
      raise PybrewFormulaUnavailableError,
            "formula source is no longer readable: #{path}"
    end
    return cached[:klass] if cached[:mtime] == current_mtime

    load_formula_from_path(
      cached[:name],
      pathname,
      flags: cached[:flags] || [],
      from_metadata: cached[:from_metadata],
      namespace: cached[:namespace]
    )
  end

  def formula_class_get_from_api(name)
    key = api_cache_key(name)
    cached = key.nil? ? nil : (@api_cache ||= {})[key]
    return cached unless cached.nil?

    raise PybrewFormulaUnavailableError,
          "API formula class is not available for #{name.inspect} in the pybrew " \
          "Ruby shim; load it with Formulary.load_formula_from_struct! first"
  end

  def namespace_key(identifier)
    Digest::SHA2.hexdigest("#{OS.mac? ? "macos" : "linux"}:#{identifier}")
  end

  def replace_placeholders(string)
    string.to_s
          .gsub(HOMEBREW_PREFIX_PLACEHOLDER, HOMEBREW_PREFIX.to_s)
          .gsub(HOMEBREW_CELLAR_PLACEHOLDER, HOMEBREW_CELLAR.to_s)
          .gsub(HOMEBREW_HOME_PLACEHOLDER, Dir.home)
  end

  def ensure_utf8_encoding(io)
    io
  end

  def clear_cache
    Array(@namespace_constants).each do |namespace_name|
      remove_const(namespace_name) if const_defined?(namespace_name, false)
    end
    @namespace_constants = []
    @path_cache = {}
    @factory_cache = {}
    @api_cache = {}
    @api_alias_map = {}
    @api_json_cache = {}
    @jws_public_key_cache = {}
    @api_json_loaded = nil
    @remote_api_json_cache = nil
    @tap_source_alias_map = nil
    remote_sidecar_clear!
    nil
  end

  def factory_cached?
    !!@factory_cache_enabled
  end

  def enable_factory_cache!
    @factory_cache_enabled = true
    @factory_cache ||= {}
    nil
  end

  def factory_cache
    @factory_cache ||= {}
  end

  def platform_cache
    @platform_cache ||= {}
  end

  def platform_cache_tag
    host = RUBY_PLATFORM.include?("darwin") ? "macos" : "linux"
    "#{host}_#{RbConfig::CONFIG["host_cpu"]}"
  end

  def to_rack(ref)
    name = File.basename(ref.to_s, ".rb")
    rack = HOMEBREW_CELLAR/name
    return rack.realpath if rack.directory?

    canonical = canonical_name(ref)
    candidate = HOMEBREW_CELLAR/canonical
    candidate.directory? ? candidate.realpath : candidate
  rescue StandardError
    nil
  end

  # Resolve aliases/renames from the bounded API cache and local tap
  # ``Aliases/`` directories before falling back to the short name.
  def canonical_name(ref)
    raw = ref.to_s.sub(/\.rb\z/, "")
    key = File.basename(raw)
    begin
      ensure_api_json_loaded if api_json_configured?
    rescue ScriptError, StandardError
      nil
    end
    target = api_alias_map[raw] || api_alias_map[key]
    return target unless target.nil?

    found = tap_alias_target(raw)
    return found[0] unless found.nil?

    key
  end

  def core_path(name)
    HOMEBREW_PREFIX/"Library/Taps/homebrew/homebrew-core/Formula"/"#{File.basename(name.to_s, ".rb").downcase}.rb"
  end

  def find_formula_in_tap(name, tap)
    base = tap.respond_to?(:formula_dir) ? tap.formula_dir : Pathname.new(tap.to_s)
    base/"#{File.basename(name.to_s, ".rb")}.rb"
  end

  def from_keg(keg, spec = nil, alias_path: nil, force_bottle: false, flags: [])
    tab = begin
      keg.tab
    rescue ScriptError, StandardError
      nil
    end
    tap = tab.respond_to?(:tap) ? tab.tap.to_s : ""
    spec ||= tab.spec if tab.respond_to?(:spec)
    name = keg.rack.basename.to_s
    formula = begin
      if tap.empty?
        factory(name, spec || :stable, alias_path: alias_path, from: :keg,
                warn: false, force_bottle: force_bottle, flags: flags)
      else
        begin
          factory("#{tap}/#{name}", spec || :stable, alias_path: alias_path, from: :keg,
                  warn: false, force_bottle: force_bottle, flags: flags)
        rescue PybrewFormulaUnavailableError
          factory(name, spec || :stable, alias_path: alias_path, from: :keg,
                  warn: false, force_bottle: force_bottle, flags: flags)
        end
      end
    rescue ScriptError, StandardError
      factory(name, spec || :stable, from: :keg, warn: false)
    end
    formula.build = tab if !tab.nil? && formula.respond_to?(:build=)
    formula.follow_installed_alias = false if formula.respond_to?(:follow_installed_alias=)
    formula
  end

  # Load a parent-selected Ruby source file (Homebrew 4.6+/7.0 API).
  # The pybrew shim is source-first: derive the class name from the file and
  # evaluate it through the existing bounded path loader.
  def from_path(path, spec = :stable, alias_path: nil, force_bottle: false,
                flags: [], ignore_errors: false)
    file = Pathname.new(path.to_s)
    unless file.exist? && file.file? && file.extname == ".rb"
      raise PybrewFormulaUnavailableError,
            "formula source is not a loadable .rb file: #{file}"
    end
    name = file.basename(".rb").to_s
    load_formula_from_path(
      name, file, flags: Array(flags), ignore_errors: !!ignore_errors
    )
  end

  def resolve_from_path(path, spec: nil, flags: [])
    formula = from_path(path, spec || :stable, flags: flags)
    formula.follow_installed_alias = false if formula.respond_to?(:follow_installed_alias=)
    formula
  end

  def from_tap_path(path)
    from_path(path)
  end

  # Load a local bottle by extracting its embedded .brew/<name>.rb recipe.
  # This mirrors Homebrew's FromBottleLoader for the pybrew shim.
  def from_bottle(path, spec = :stable, force_bottle = false, flags = [])
    bottle = Pathname.new(path.to_s)
    unless bottle.file? && /\.bottle(\.tar\.gz)?\z/.match?(bottle.to_s)
      raise PybrewFormulaUnavailableError,
            "not a local bottle file: #{bottle}"
    end
    name, _full_name = Utils::Bottles.resolve_formula_names(bottle)
    contents = Utils::Bottles.formula_contents(bottle, name)
    if contents.to_s.empty?
      raise PybrewFormulaUnavailableError,
            "bottle has no readable formula source: #{bottle}"
    end
    formula = from_contents(
      name, bottle.dirname/"#{name}.rb", contents, spec,
      flags: Array(flags), from_metadata: true
    )
    formula.local_bottle_path = bottle if formula.respond_to?(:local_bottle_path=)
    formula
  end

  def try_new(ref, **options)
    reference = ref.to_s
    if reference.include?("/") || reference.end_with?(".rb")
      path = Pathname.new(reference)
      return nil unless path.exist?
    end
    factory(ref, **options)
  end

  # Bounded loader object mirroring Homebrew's FormulaLoader surface:
  # ``name``/``path``/``get_formula``.
  class PybrewFormulaLoader
    attr_reader :name, :path

    def initialize(ref)
      @ref = ref.to_s
      @name = File.basename(@ref, ".rb").split("/").last
      candidate = Pathname.new(@ref)
      @path = if candidate.file? && candidate.extname == ".rb"
        candidate
      else
        begin
          reference = Formula[@ref]
          path = reference.respond_to?(:path) ? reference.path : nil
          path.is_a?(Pathname) ? path : nil
        rescue ScriptError, StandardError
          nil
        end
      end
    end

    def get_formula(spec = :stable, alias_path: nil, force_bottle: false,
                    flags: [], ignore_errors: false)
      Formulary.factory(
        @ref, spec,
        alias_path: alias_path,
        force_bottle: force_bottle,
        flags: flags,
        ignore_errors: ignore_errors
      )
    end
  end

  def loader_for(ref)
    PybrewFormulaLoader.new(ref)
  end

  def loader_from_name_tap_type(name, _tap = nil, _type = nil)
    Formula[name]
  end

  def tap_formula_name_type(name, tap = nil)
    [name.to_s, tap, nil, nil]
  end

  def load_from_api(name)
    formula_class_get_from_api(name)
  end

  def from_contents(name, path, contents, spec = :stable, **options)
    dir = Dir.mktmpdir("pybrew-from-contents-")
    file = Pathname.new(dir)/"#{File.basename(name.to_s, ".rb")}.rb"
    file.write(contents.to_s)
    klass = load_formula_from_path(
      name.to_s,
      file,
      flags: options[:flags] || [],
      ignore_errors: options[:ignore_errors] || false
    )
    klass.new
  ensure
    FileUtils.remove_entry(dir) if dir && File.directory?(dir)
  end

  def get_formula(ref, spec = :stable, **options)
    factory(ref, spec, **options)
  end

  def klass(ref, **options)
    reference = Formula[ref]
    loaded = factory_load_source(reference, options)
    loaded || reference.class
  end

  def load_file(name, path, **options)
    load_formula_from_path(
      name.to_s,
      Pathname.new(path.to_s),
      flags: options[:flags] || [],
      ignore_errors: options[:ignore_errors] || false
    )
  end
end

class PybrewResource
  attr_reader :name, :url, :sha256

  def initialize(name, info)
    @name = name.to_s
    @info = info || {}
    @url = @info["url"]
    @sha256 = @info["sha256"]
    @patches = Array(@info["patches"]).map do |entry|
      next nil unless entry.is_a?(Hash)

      args = if !entry["url"].nil?
        [entry["url"]]
      elsif !entry["file"].nil?
        [Pathname.new(entry["file"].to_s)]
      else
        []
      end
      PybrewPatchDefinition.new(args, nil)
    end.compact
    @patches += @info["patch_objects"] if @info["patch_objects"].is_a?(Array)
  end

  def patches
    @patches
  end

  def patch_files
    patches.flat_map(&:patch_files)
  end

  def patches?
    !patches.empty?
  end

  def version
    raw = @info["version"]
    raw.nil? || raw.to_s.empty? ? nil : PybrewVersion.new(raw.to_s)
  end

  def staged?
    staged = @info["staged"]
    !(staged.nil? || staged.to_s.empty?)
  end

  def staged_path
    staged = @info["staged"]
    if staged.nil? || staged.to_s.empty?
      reason = @info["staged_error"]
      if reason.nil? || reason.to_s.empty?
        reason = "the pybrew opt-in Ruby bridge could not stage it"
      end
      raise "resource(#{name.inspect}) has not been staged: #{reason}"
    end
    path = Pathname.new(staged.to_s)
    unless path.directory?
      raise "resource(#{name.inspect}) staged path does not exist: #{path}"
    end
    path
  end

  # Like Homebrew's ``Resource#stage``: without a block return the staged
  # directory, with a block change into it and yield that directory.  For
  # resources unpacked by the bridge, enter a single top-level directory
  # produced by the archive, exactly like Homebrew's unpack helper.  A
  # positional ``target`` extracts/copies the staged tree into that directory
  # (bounded: the bridge never downloads inside Ruby).
  def stage(target = nil, debug_symbols: false, staging_path: nil, staged: false, &block)
    if !target.nil? && !staging_path.nil?
      raise ArgumentError, "resource stage accepts either target or staging_path, not both"
    end

    path = staged_path
    destination = target.nil? ? staging_path : target
    if !destination.nil?
      target_path = Pathname.new(destination.to_s)
      target_path.mkpath
      copy_staged_tree(path, target_path)
      return target_path if block.nil?

      return block.call(target_path)
    end
    return path if block.nil?

    Dir.chdir(path.to_s) do
      entries = Dir["*"].reject { |entry| entry == "__MACOSX" }
      if @info["staged_from_archive"] == "true" &&
         entries.length == 1 &&
         File.directory?(entries.first) && !File.symlink?(entries.first)
        Dir.chdir(entries.first) { block.call(Pathname.pwd) }
      else
        block.call(Pathname.pwd)
      end
    end
  end

  # Copy a staged tree preserving symlinks (and replacing stale target links).
  def copy_staged_tree(source, destination)
    Dir.glob((source/"**/*").to_s, File::FNM_DOTMATCH).each do |entry|
      relative = Pathname.new(entry).relative_path_from(source)
      next if relative.to_s == "." || relative.to_s.start_with?("..")

      destination_path = destination/relative
      if File.symlink?(entry)
        destination_path.dirname.mkpath
        File.unlink(destination_path.to_s) if destination_path.symlink? || destination_path.exist?
        File.symlink(File.readlink(entry), destination_path.to_s)
      elsif File.directory?(entry)
        destination_path.mkpath
      else
        destination_path.dirname.mkpath
        FileUtils.cp(entry, destination_path.to_s)
      end
    end
    destination
  end

  def fetch
    staged_path
  end

  def download_name
    return @info["download_name"].to_s unless @info["download_name"].nil?

    base = url.to_s.split("?", 1).first
    name = File.basename(base.to_s)
    name.empty? ? self.name : name
  end

  def download_strategy
    @info["download_strategy"] || @info["using"] || :Using
  end

  def using
    @info["using"] || @info["download_strategy"]
  end

  def downloaded_and_valid?
    staged? && !sha256.to_s.empty?
  end

  def verify_download_integrity(_file = nil)
    nil
  end

  def download_queue_name
    download_name
  end

  def download_queue_type
    :resource
  end

  def directory
    Pathname.new((@info["directory"] || ".").to_s)
  end

  def file
    download_name
  end

  def files
    [file]
  end

  def path_exec_files
    Array(@info["path_exec_files"]).map { |path| Pathname.new(path.to_s) }
  end

  def installed_size
    (@info["installed_size"] || 0).to_i
  end

  def bottle_size
    (@info["bottle_size"] || 0).to_i
  end

  def manifest_annotations
    @info["manifest_annotations"].is_a?(Hash) ? @info["manifest_annotations"] : {}
  end

  def sbom_supplement
    @info["sbom_supplement"]
  end

  def owner=(value)
    @owner = value
  end

  def owner
    @owner
  end

  def initialize_dup(other)
    super
    nil
  end

  def stage_resource
    stage
  end

  def unpack(*_args)
    staged? ? staged_path : nil
  end

  def apply_patches
    nil
  end

  def resolves
    nil
  end

  def determine_url_mirrors
    [url].compact
  end

  def to_s
    name
  end

  def inspect
    "#<PybrewResource #{name}>"
  end
end

module Utils
  module Bottles
    class Tag
      ARCHS = %i[arm64 x86_64 intel arm aarch64].freeze

      attr_reader :system, :arch

      def self.from_symbol(value)
        return new(system: :all, arch: :all) if value.to_sym == :all

        text = value.to_s
        arch = nil
        ARCHS.each do |candidate|
          prefix = "#{candidate}_"
          if text.start_with?(prefix)
            arch = candidate
            text = text[prefix.length..]
            break
          end
        end
        raise ArgumentError, "Invalid bottle tag symbol" if text.empty?

        new(system: text.to_sym, arch: (arch || :x86_64).to_sym)
      end

      def self.from_arg(arg, os:, arch:)
        arg.nil? ? new(system: os, arch: arch) : from_symbol(arg)
      end

      def initialize(system:, arch:)
        @system = system.to_sym
        @arch = arch.to_sym
      end

      def standardized_arch
        return :x86_64 if %i[x86_64 intel].include?(arch)
        return :arm64 if %i[arm64 arm aarch64].include?(arch)

        arch
      end

      def macos?
        [:ventura, :sonoma, :sequoia, :tahoe, :monterey, :big_sur,
         :catalina, :mojave, :high_sierra, :sierra, :el_capitan,
         :golden_gate, :macos].include?(system)
      end

      def linux?
        system == :linux
      end

      def to_sym
        return :all if system == :all && arch == :all
        return system if macos? && standardized_arch == :x86_64

        :"#{standardized_arch}_#{system}"
      end

      def to_unstandardized_sym
        return to_sym if %i[intel arm].include?(arch)

        :"#{arch}_#{system}"
      end

      def to_s
        to_sym.to_s
      end

      def ==(other)
        case other
        when Symbol then to_sym == other
        when self.class then system == other.system && standardized_arch == other.standardized_arch
        else false
        end
      end
      alias eql? ==

      def hash
        [system, standardized_arch].hash
      end

      def default_prefix
        return "/home/linuxbrew/.linuxbrew" if linux?

        standardized_arch == :arm64 ? "/opt/homebrew" : "/usr/local"
      end

      def default_cellar
        "#{default_prefix}/Cellar"
      end

      def padded_prefix
        return default_prefix if linux? || standardized_arch == :arm64

        nil
      end

      def to_macos_version
        release = MacOS::RELEASES[system]
        release.nil? ? nil : MacOS::Version.new(release)
      end
    end

    class << self
      def tag(value = nil)
        case value
        when Symbol
          Tag.from_symbol(value)
        when Tag
          value
        else
          @tag ||= Tag.new(
            system: (OS.mac? ? :macos : :linux),
            arch: (Hardware::CPU.arch.to_s == "arm64" ? :arm64 : :x86_64)
          )
        end
      end

      def extname_tag_rebuild(filename)
        match = /\.([a-z0-9_]+)\.bottle\.(?:(\d+)\.)?tar\.gz$/.match(filename.to_s)
        match.nil? ? [] : match.to_a.map(&:to_s)
      end

      def bottle_file_list(bottle_file)
        @bottle_file_list ||= {}
        @bottle_file_list[bottle_file.to_s] ||= Utils.popen_read(
          "tar", "--list", "--file", bottle_file.to_s
        ).to_s.lines.map(&:chomp)
      end

      def receipt_path(bottle_file)
        bottle_file_list(bottle_file).find do |line|
          %r{.+/.+/INSTALL_RECEIPT\.json}.match?(line)
        end
      end

      def file_from_bottle(bottle_file, file_path)
        Utils.popen_read(
          "tar", "--extract", "--to-stdout", "--file", bottle_file.to_s, file_path.to_s
        ).to_s
      end

      def resolve_formula_names(bottle_file)
        first = bottle_file_list(bottle_file).first.to_s
        name = first.split("/").fetch(0)
        full_name = name
        receipt = receipt_path(bottle_file)
        unless receipt.nil?
          tab = Tab.from_file_content(file_from_bottle(bottle_file, receipt))
          tap = tab.tap
          full_name = "#{tap}/#{name}" unless tap.nil? || tap.to_s.empty?
        end
        [name, full_name]
      end

      def resolve_version(bottle_file)
        first = bottle_file_list(bottle_file).first.to_s
        version = first.split("/").fetch(1, "0")
        PybrewVersion.new(version)
      end

      def formula_contents(bottle_file, name = nil)
        name ||= resolve_formula_names(bottle_file)[0]
        version = resolve_version(bottle_file)
        file_from_bottle(bottle_file, "#{name}/#{version}/.brew/#{name}.rb")
      end

      def built_as?(formula)
        return false unless formula.respond_to?(:latest_version_installed?) && formula.latest_version_installed?

        keg = Keg.new(formula.latest_installed_prefix)
        tab = keg.tab
        !!tab.built_as_bottle?
      rescue StandardError
        false
      end

      def file_outdated?(formula, file)
        filename = File.basename(file.to_s)
        _match, bottle_tag, rebuild = extname_tag_rebuild(filename)
        return false if bottle_tag.nil? || bottle_tag.empty?
        return false unless formula.respond_to?(:bottle)

        bottle = formula.bottle
        return false if bottle.nil?

        bottle_tag != bottle.tag.to_s || rebuild.to_i != bottle.rebuild.to_i
      rescue StandardError
        false
      end

      def load_tab(formula)
        Tab.for_keg(Keg.new(formula.prefix))
      end

      def missing_all_bottle_publish_note
        "publishing without one anyway"
      end
    end
  end
end

module Utils
  module Path
    def self.child_of?(parent, child)
      parent_path = Pathname.new(parent.to_s).expand_path
      child_path = Pathname.new(child.to_s).expand_path
      child_path.ascend { |candidate| return true if candidate == parent_path }
      false
    end

    def self.ensure_child_of!(parent, child, message: nil)
      return if child_of?(resolve_symlinks(parent), resolve_symlinks(child))

      raise(message || "#{child} is not under #{parent}")
    rescue SystemCallError
      raise(message || "#{child} is not under #{parent}")
    end

    def self.clean_absolute_path?(path)
      value = path.to_s
      return false unless value.start_with?(File::SEPARATOR)

      Pathname.new(value).cleanpath.to_s == value
    end

    def self.resolve_symlinks(path)
      pathname = Pathname.new(path.to_s).expand_path
      existing = pathname
      existing = existing.parent until existing.exist? || existing.symlink? || existing.root?
      (existing.realpath/pathname.relative_path_from(existing)).cleanpath
    rescue StandardError
      pathname
    end

    def self.relative_path_from(path, start)
      Pathname.new(path.to_s).relative_path_from(Pathname.new(start.to_s))
    end

    def self.cp_path_sub(path, pattern, replacement)
      source = Pathname.new(path.to_s)
      raise "#{source} does not exist" unless source.exist?

      target = Pathname.new(source.to_s.sub(pattern.to_s, replacement.to_s))
      raise "#{source} is the same file as #{target}" if source == target

      FileUtils.mkdir_p(target.dirname)
      FileUtils.cp_r(source.to_s, target.to_s)
      target
    end

    def self.loadable_package_path?(_path, _package_type = nil)
      true
    end

    def self.formula_opt_bin(name)
      HOMEBREW_PREFIX/"opt"/name.to_s/"bin"
    end

    def self.formula_opt_lib(name)
      HOMEBREW_PREFIX/"opt"/name.to_s/"lib"
    end

    def self.formula_opt_libexec(name)
      HOMEBREW_PREFIX/"opt"/name.to_s/"libexec"
    end

    def self.formula_opt_include(name)
      HOMEBREW_PREFIX/"opt"/name.to_s/"include"
    end

    def self.formula_any_version_installed?(names)
      Array(names).any? do |name|
        rack = HOMEBREW_CELLAR/name.to_s.split("/").last.to_s
        rack.directory? && !rack.children.empty?
      end
    end

    def self.trusted_package_root(path)
      Pathname.new(path.to_s).realpath.to_s
    rescue Errno::ENOENT, Errno::EACCES, Errno::ENOTDIR
      Pathname.new(path.to_s).expand_path.to_s
    end
  end
end

module Utils
  module Curl
    def self.curl_executable(use_homebrew_curl: false)
      return HOMEBREW_PREFIX/"opt/curl/bin/curl" if use_homebrew_curl

      curl_path || "curl"
    end

    def self.curl_path
      @curl_path ||= Utils.which("curl")
    end

    def self.clear_path_cache
      @curl_path = nil
    end

    def self.curl_version
      output = Utils.popen_read(curl_executable, "--version").to_s
      output[/^curl ([\w.]+)/, 1]
    end

    def self.curl_args
      []
    end

    def self.curl_output(*args, **_options)
      Utils.popen_read(curl_executable, *args).to_s
    end

    def self.strip_progress_bar(text)
      text.to_s.gsub(/\A#*\s*\d{1,3}\.\d%\s*/, "")
    end

    def self.http_status_ok?(status)
      code = status.to_i
      code >= 200 && code < 400
    end

    def self.parse_curl_output(output)
      [strip_progress_bar(output), {}]
    end

    def self.parse_curl_response(text)
      raw = text.to_s
      separator = raw.index("\r\n\r\n")
      return ["", raw] if separator.nil?

      [raw[0...separator], raw[(separator + 4)..]]
    end

    def self.curl_supports_tls13?
      false
    end

    def self.curl_supports_fail_with_body?
      true
    end

    def self.https_redirect_curl_args
      ["--proto-redir", "=https"]
    end

    def self.curl_check_http_content(_url, *_args, **_options)
      [true, "", nil]
    end

    def self.curl_download(_url, output = nil, *_args, **_options)
      output.nil? ? nil : Pathname.new(output.to_s)
    end

    def self.curl_headers(_url)
      {}
    end

    def self.curl_http_content_headers_and_checksum(_url, *_args, **_options)
      [nil, {}, nil]
    end

    def self.curl_response_follow_redirections(_url)
      nil
    end

    def self.curl_response_last_location(_url)
      nil
    end

    def self.curl_with_workarounds(*args, **_options)
      curl_output(*args)
    end

    def self.insecure_redirect?(*_args)
      false
    end

    def self.no_insecure_redirect_curl_args
      []
    end

    def self.url_protected_by_cloudflare?(_url)
      false
    end

    def self.url_protected_by_incapsula?(_url)
      false
    end
  end
end

module Utils
  # Bounded Utils::GitHub implementation.  Read operations use a curl
  # transport that can be redirected with PYBREW_GITHUB_CURL (tests use a fake
  # script, so no network access is required).  Write operations fail loudly.
  module GitHub
    API_URL = "https://api.github.com"
    GRAPHQL_URL = "https://api.github.com/graphql"
    CURL_ENV = "PYBREW_GITHUB_CURL"
    TOKEN_ENV = "PYBREW_GITHUB_TOKEN"

    module_function

    def curl_executable
      configured = ENV[CURL_ENV].to_s
      configured.empty? ? "curl" : configured
    end

    def token
      value = ENV[TOKEN_ENV].to_s
      value.empty? ? nil : value
    end

    def api_url
      raw = ENV["PYBREW_GITHUB_API_URL"].to_s.strip
      raw.empty? ? API_URL : raw.chomp("/")
    end

    def graphql_url
      explicit = ENV["PYBREW_GITHUB_GRAPHQL_URL"].to_s.strip
      return explicit unless explicit.empty?

      base = api_url
      return GRAPHQL_URL if base == API_URL

      if base.end_with?("/api/v3")
        "#{base.sub(%r{/api/v3\z}, "")}/api/graphql"
      else
        "#{base}/graphql"
      end
    end

    def url_to(*subroutes)
      parts = subroutes.flatten.compact.map(&:to_s).reject(&:empty?)
      ([api_url] + parts).join("/")
    end

    def search_query_string(*main_params, **qualifiers)
      terms = main_params.flatten.compact.map(&:to_s)
      from = qualifiers.delete(:from)
      to = qualifiers.delete(:to)
      if !from.nil? && !to.nil?
        terms << "created:#{from}..#{to}"
      elsif !from.nil?
        terms << "created:>=#{from}"
      elsif !to.nil?
        terms << "created:<=#{to}"
      end
      qualifiers.each do |key, value|
        Array(value).flatten.each { |item| terms << "#{key}:#{item}" }
      end
      "q=#{URI.encode_www_form_component(terms.join(" "))}"
    end

    # GitHub API error surface mirroring Homebrew's ``GitHub::API`` errors.
    # Formula code rescues ``RateLimitExceededError`` and calls
    # ``sleep_for_rate_limit``; the pybrew shim keeps the same semantics for
    # REST and GraphQL calls while remaining fully injectable/no-network.
    module API
      class Error < RuntimeError
        attr_reader :github_message

        def initialize(message = nil, github_message = nil)
          @github_message = github_message
          super(message)
        end
      end

      class HTTPNotFoundError < Error; end

      class GitRepositoryIsEmptyError < Error; end

      class ValidationFailedError < Error
        attr_reader :errors

        def initialize(message = nil, errors = [])
          @errors = Array(errors).map(&:to_s)
          text = @errors.empty? ? message : "#{message}: #{@errors.join(", ")}"
          super(text, message)
        end
      end

      class AuthenticationFailedError < Error; end

      class MissingAuthenticationError < AuthenticationFailedError
        def initialize(message = nil, github_message = nil)
          super(
            message || "No GitHub credentials found. Set PYBREW_GITHUB_TOKEN " \
                       "(or HOMEBREW_GITHUB_API_TOKEN) to authenticate.",
            github_message
          )
        end
      end

      class RateLimitExceededError < Error
        attr_reader :reset, :resource, :limit

        def initialize(github_message, reset:, resource: nil, limit: nil)
          @reset = reset.to_i
          @resource = (resource.nil? ? "core" : resource.to_s)
          @limit = limit.nil? ? nil : limit.to_i
          message = <<~EOS
            GitHub API Error: #{github_message}
            Rate limit exceeded for #{@resource} resource (#{@limit.nil? ? "unknown" : @limit} limit).
            Try again in #{pretty_ratelimit_reset}
          EOS
          super(message, github_message)
        end

        def pretty_ratelimit_reset
          seconds = [@reset - Time.now.to_i, 0].max
          if seconds < 60
            "#{seconds} seconds"
          elsif seconds < 3600
            "#{(seconds / 60.0).ceil} minutes"
          else
            "#{(seconds / 3600.0).ceil} hours"
          end
        end
      end

      # Sleep until the rate limit resets (at least one second, matching
      # Homebrew) and print the same warning to stderr.
      def self.sleep_for_rate_limit(exception)
        reset = exception.respond_to?(:reset) ? exception.reset.to_i : 0
        sleep_seconds = [reset - Time.now.to_i, 1].max
        $stderr.puts "Warning: GitHub rate limit exceeded, sleeping for #{sleep_seconds} seconds..."
        sleep sleep_seconds
        sleep_seconds
      end
    end

    GITHUB_RATE_LIMIT_HEADERS = %w[
      x-ratelimit-limit x-ratelimit-remaining x-ratelimit-reset
      x-ratelimit-resource retry-after
    ].freeze

    def credentials_type
      token.nil? ? :none : :env_token
    end

    # Parse an HTTP response-header blob (``curl -D`` output) into a
    # downcased, last-wins hash.  Multiple redirect blocks are tolerated.
    def parse_response_headers(headers)
      meta = {}
      headers.to_s.each_line do |line|
        key, separator, value = line.partition(":")
        next if separator.empty?

        key = key.strip.downcase
        next if key.empty? || key.start_with?("http/")

        meta[key] = value.strip
      end
      meta
    end

    def response_status(headers, fallback = "200")
      statuses = headers.to_s.scan(/\AHTTP\/\S+\s+(\d{3})/).flatten
      statuses.empty? ? fallback : statuses.last
    end

    def retry_after_seconds(meta)
      raw = meta["retry-after"].to_s.strip
      return nil if raw.empty?
      return raw.to_i if /\A\d+\z/.match?(raw)

      begin
        require "time"
        [Time.httpdate(raw).to_i - Time.now.to_i, 1].max
      rescue StandardError
        nil
      end
    end

    # Raise the same error classes Homebrew uses for REST/GraphQL failures.
    def raise_error(output, errors = "", http_code = "200", headers = "", scopes = [])
      json = begin
        JSON.parse(output.to_s)
      rescue StandardError
        nil
      end
      message = json.is_a?(Hash) && json["message"] ? json["message"].to_s : "curl failed! #{errors}".strip
      meta = parse_response_headers(headers)
      code = http_code.to_s

      if code == "401"
        raise API::AuthenticationFailedError.new(
          "GitHub API Error: #{message}", message
        )
      end

      if code == "403" || code == "429"
        retry_after = retry_after_seconds(meta)
        remaining = meta["x-ratelimit-remaining"]
        reset = meta["x-ratelimit-reset"].to_s
        resource = meta["x-ratelimit-resource"]
        limit = meta["x-ratelimit-limit"]
        secondary_message = message.match?(/secondary rate limit/i)
        if code == "429"
          raise API::RateLimitExceededError.new(
            message,
            reset: retry_after.nil? ? (reset.empty? ? Time.now.to_i + 60 : reset.to_i) : Time.now.to_i + retry_after,
            resource: "secondary",
            limit: limit
          )
        end
        # Primary limit: the reset window is exhausted.  That is true even if
        # a retry-after header is also present, so it takes precedence.
        if !remaining.nil? && remaining.to_i <= 0
          raise API::RateLimitExceededError.new(
            message,
            reset: reset.empty? ? Time.now.to_i + 60 : reset.to_i,
            resource: resource,
            limit: limit
          )
        end
        # Secondary limit: retry-after is authoritative; 429s and explicit
        # "secondary rate limit" messages also land here.
        if !retry_after.nil? || secondary_message
          raise API::RateLimitExceededError.new(
            message,
            reset: retry_after.nil? ? (reset.empty? ? Time.now.to_i + 60 : reset.to_i) : Time.now.to_i + retry_after,
            resource: "secondary",
            limit: limit
          )
        end

        raise API::AuthenticationFailedError.new(
          "GitHub API Error: #{message}", message
        )
      end

      case code
      when "404"
        if !Array(scopes).empty? && credentials_type == :none
          raise API::MissingAuthenticationError.new(nil, message)
        end

        raise API::HTTPNotFoundError.new("GitHub API Error: #{message}", message)
      when "409"
        if message.downcase.include?("git repository is empty")
          raise API::GitRepositoryIsEmptyError.new("GitHub API Error: #{message}", message)
        end

        raise API::Error.new("GitHub API Error: #{message}", message)
      when "422"
        raise API::ValidationFailedError.new(
          "GitHub API Error: #{message}",
          json.is_a?(Hash) ? json["errors"] : []
        )
      else
        raise API::Error.new("GitHub API Error: #{message}", message)
      end
    end

    # Bounded REST request that preserves the curl injection point used by
    # tests while capturing status/headers for error classification.  A fake
    # transport that writes a ``-D`` header file gets full HTTP semantics; one
    # that only prints JSON is treated as HTTP 200.
    def get_json(url, data: nil, method: "GET", scopes: [], raise_errors: true,
                 retries: 0, max_backoff: nil, transport_retries: nil)
      command = [curl_executable, "-sS", "-L", "-H", "Accept: application/vnd.github+json"]
      command += ["-H", "Authorization: Bearer #{token}"] unless token.nil?
      command += ["-X", method.to_s] unless method.to_s == "GET"
      command += ["--data", JSON.generate(data)] unless data.nil?
      header_file = Tempfile.new(["pybrew-github-headers", ".txt"])
      command += ["-D", header_file.path.to_s]
      command << url.to_s
      begin
        output = Utils.popen_read(*command).to_s
      ensure
        headers = File.file?(header_file.path.to_s) ? File.read(header_file.path.to_s) : ""
        header_file.close!
      end

      code = response_status(headers)
      if raise_errors && code.to_i >= 400
        begin
          raise_error(output, "", code, headers, Array(scopes))
        rescue API::RateLimitExceededError => error
          remaining = [retries.to_i, 0].max
          raise if remaining <= 0

          sleep_seconds = rate_limit_backoff_seconds(error, max_backoff)
          sleep sleep_seconds if sleep_seconds.positive?
          return get_json(url, data: data, method: method, scopes: scopes,
                          raise_errors: raise_errors, retries: remaining - 1,
                          max_backoff: max_backoff,
                          transport_retries: transport_retries)
        rescue API::AuthenticationFailedError, API::HTTPNotFoundError,
               API::ValidationFailedError, API::GitRepositoryIsEmptyError
          raise
        rescue API::Error
          transport_left = if transport_retries.nil?
            github_transport_retries
          else
            [transport_retries.to_i, 0].max
          end
          raise if transport_left.zero?

          backoff = github_transport_backoff
          sleep backoff if backoff.positive?
          return get_json(url, data: data, method: method, scopes: scopes,
                          raise_errors: raise_errors, retries: retries,
                          max_backoff: max_backoff,
                          transport_retries: transport_left - 1)
        end
      end
      return nil if output.empty?

      JSON.parse(output)
    end

    def repository(user, repo)
      begin
        graphql_result = graphql_repository(user, repo)
        return graphql_result unless graphql_result.nil?
      rescue ScriptError, StandardError
        nil
      end
      get_json(url_to("repos", user, repo))
    end

    def user
      get_json(url_to("user"))
    end

    def private_repo?(full_name)
      user, repo = full_name.to_s.split("/", 2)
      return false if repo.nil?

      data = repository(user, repo)
      data.is_a?(Hash) ? !!data["private"] : false
    rescue ScriptError, StandardError
      false
    end

    def issues(repo:, **filters)
      query = filters.map { |key, value| "#{key}=#{value}" }.join("&")
      url = url_to("repos", repo, "issues")
      url = "#{url}?#{query}" unless query.empty?
      get_json(url)
    end

    def search_issues(query, **qualifiers)
      get_json("#{url_to("search", "issues")}?#{search_query_string(query, **qualifiers)}")
    end

    def issues_for_formula(name, tap: nil, tap_remote_repo: nil, state: nil, type: nil)
      repo = tap_remote_repo.to_s
      repo = "homebrew/homebrew-core" if repo.empty?
      filters = {}
      filters[:state] = state unless state.nil?
      filters[:labels] = type unless type.nil?
      issues(repo: repo, **filters)
    end

    def get_latest_release(user, repo)
      get_json(url_to("repos", user, repo, "releases", "latest"))
    end

    def get_release(user, repo, tag)
      get_json(url_to("repos", user, repo, "releases", "tags", tag))
    end

    def get_repo_license(user, repo, ref: nil)
      url = url_to("repos", user, repo, "license")
      url = "#{url}?ref=#{ref}" unless ref.nil? || ref.to_s.empty?
      get_json(url)
    end

    def get_workflow_run(user, repo, pull_request, workflow_id: "tests.yml", artifact_pattern: nil)
      url = url_to("repos", user, repo, "actions", "workflows", workflow_id, "runs")
      data = get_json("#{url}?event=pull_request&per_page=30")
      runs = data.is_a?(Hash) ? Array(data["workflow_runs"]) : []
      run = runs.find { |item| item.is_a?(Hash) && item["pull_requests"].is_a?(Array) }
      run || runs.first
    end

    def get_artifact_urls(workflow_array)
      urls = []
      Array(workflow_array).flatten.compact.each do |item|
        next unless item.is_a?(Hash)

        artifacts = item["artifacts"]
        if !artifacts.is_a?(Array) && !item["artifacts_url"].nil?
          data = get_json(item["artifacts_url"].to_s)
          artifacts = data.is_a?(Hash) ? Array(data["artifacts"]) : []
        end
        Array(artifacts).each do |artifact|
          next unless artifact.is_a?(Hash)

          url = artifact["archive_download_url"] || artifact["url"]
          urls << url.to_s unless url.nil?
        end
      end
      urls
    end

    MAX_PER_PAGE = 100

    def search(entity, *queries, **qualifiers)
      get_json("#{url_to("search", entity)}?#{search_query_string(*queries, **qualifiers)}")
    end

    def search_issues_items(query, **qualifiers)
      json = search_issues(query, **qualifiers)
      json.is_a?(Hash) ? Array(json["items"]) : Array(json)
    end

    def pull_request_title_regex(name, version = nil)
      quoted = Regexp.quote(name.to_s)
      if version.nil? || version.to_s.empty?
        /(?:^|\s)#{quoted}(?::|,|\s|$)/i
      else
        /(?:^|\s)#{quoted}(?::|,|\s)(?:.*\s)?#{Regexp.quote(version.to_s)}(?::|,|\s|$)/i
      end
    end

    def fetch_pull_requests(name, tap_remote_repo, state: nil, version: nil)
      return [] if tap_remote_repo.nil? || tap_remote_repo.to_s.empty?

      begin
        graphql_result = graphql_fetch_pull_requests(
          name, tap_remote_repo, state: state, version: version
        )
        return graphql_result unless graphql_result.nil?
      rescue ScriptError, StandardError
        nil
      end

      regex = pull_request_title_regex(name, version)
      qualifiers = { repo: tap_remote_repo.to_s, type: "pr", in: "title" }
      qualifiers[:state] = state unless state.nil?
      search_issues_items(name.to_s, **qualifiers).select do |pr|
        pr.is_a?(Hash) && pr["html_url"].to_s.include?("/pull/") && regex.match?(pr["title"].to_s)
      end
    end

    def fetch_open_pull_requests(name, tap_remote_repo, version: nil)
      fetch_pull_requests(name, tap_remote_repo, state: "open", version: version)
    end

    def print_pull_requests_matching(query, only = nil)
      qualifiers = { type: "pr", user: "Homebrew" }
      qualifiers[:is] = only unless only.nil?
      search_issues_items(query, **qualifiers)
    end

    def pull_request_commits(user, repo, pull_request, per_page: MAX_PER_PAGE)
      begin
        graphql_result = graphql_pull_request_commits(user, repo, pull_request)
        return graphql_result unless graphql_result.nil?
      rescue ScriptError, StandardError
        nil
      end
      get_json("#{url_to("repos", user, repo, "pulls", pull_request, "commits")}?per_page=#{per_page}")
    end

    def pull_request_labels(user, repo, pull_request)
      begin
        graphql_result = graphql_pull_request_labels(user, repo, pull_request)
        return graphql_result unless graphql_result.nil?
      rescue ScriptError, StandardError
        nil
      end
      get_json(url_to("repos", user, repo, "pulls", pull_request, "labels"))
    end

    def get_pull_request_changed_files(tap_remote_repo, pull_request)
      begin
        graphql_result = graphql_pull_request_changed_files(tap_remote_repo, pull_request)
        return graphql_result unless graphql_result.nil?
      rescue ScriptError, StandardError
        nil
      end
      get_json(url_to("repos", tap_remote_repo, "pulls", pull_request, "files"))
    end

    def pull_request_changed_files(tap_remote_repo, pull_request)
      get_pull_request_changed_files(tap_remote_repo, pull_request)
    end

    def members_by_team(org, team)
      begin
        graphql_result = graphql_members_by_team(org, team)
        return graphql_result unless graphql_result.nil?
      rescue ScriptError, StandardError
        nil
      end
      get_json(url_to("orgs", org, "teams", team, "members"))
    end

    # Page through a REST list endpoint.  Stops on a short/empty page, the
    # page cap, or an injected transport that returns fewer items.
    def paginate_rest(url, per_page: MAX_PER_PAGE, max_pages: 20)
      results = []
      page = 1
      per_page = [per_page.to_i, 1].max
      while page <= [max_pages.to_i, 1].max
        separator = url.to_s.include?("?") ? "&" : "?"
        page_url = "#{url}#{separator}per_page=#{per_page}&page=#{page}"
        data = get_json(page_url)
        items = if data.is_a?(Hash)
          Array(data["items"] || data["repositories"] || data["workflow_runs"])
        else
          Array(data)
        end
        break if items.empty?

        results.concat(items)
        break if items.length < per_page

        page += 1
      end
      results
    end

    def organisation_repositories(organisation, _from = nil, _to = nil, _verbose = false)
      paginate_rest(url_to("orgs", organisation, "repos"))
    end

    def issues(repo:, **filters)
      query = filters.map { |key, value| "#{key}=#{value}" }.join("&")
      url = url_to("repos", repo, "issues")
      url = "#{url}?#{query}" unless query.empty?
      paginate_rest(url)
    end

    def sponsorships(user)
      begin
        graphql_result = graphql_sponsorships(user)
        return graphql_result unless graphql_result.nil?
      rescue ScriptError, StandardError
        nil
      end
      data = get_json(url_to("users", user, "sponsors"))
      data.is_a?(Hash) ? Array(data["items"] || data["sponsors"]) : Array(data)
    end

    def sponsorship_tiers(user)
      begin
        graphql_result = graphql_sponsorship_tiers(user)
        return graphql_result unless graphql_result.nil?
      rescue ScriptError, StandardError
        nil
      end
      data = get_json(url_to("users", user, "sponsors", "tiers"))
      data.is_a?(Hash) ? Array(data["items"] || data["tiers"]) : Array(data)
    end

    def fork_exists?(repo, org: nil)
      parts = repo.to_s.split("/")
      reponame = parts.length > 1 ? parts[1] : parts.first
      username = org
      if username.nil? || username.to_s.empty?
        current = user
        username = current.is_a?(Hash) ? current["login"] : nil
      end
      return false if username.nil? || username.to_s.empty?

      data = get_json(url_to("repos", username, reponame))
      !(data.is_a?(Hash) && data["message"].to_s == "Not Found")
    rescue ScriptError, StandardError
      false
    end

    def check_for_duplicate_pull_requests(name, tap_remote_repo, file: nil, quiet: false, state: nil, version: nil)
      fetch_pull_requests(name, tap_remote_repo, state: state, version: version)
    end

    def repository_approved_reviews(user, repo, pull_request, commit: nil)
      begin
        graphql_result = graphql_repository_approved_reviews(
          user, repo, pull_request, commit: commit
        )
        return graphql_result unless graphql_result.nil?
      rescue ScriptError, StandardError
        nil
      end
      data = get_json(url_to("repos", user, repo, "pulls", pull_request, "reviews"))
      reviews = data.is_a?(Hash) ? Array(data["items"] || data["reviews"]) : Array(data)
      result = []
      reviews.each do |review|
        next unless review.is_a?(Hash) && review["state"].to_s == "APPROVED"
        next if !commit.nil? && !commit.to_s.empty? &&
                review["commit_id"].to_s != commit.to_s &&
                review.dig("commit", "oid").to_s != commit.to_s
        next if review.key?("author_association") &&
                !%w[MEMBER OWNER].include?(review["author_association"].to_s)

        author = review["user"].is_a?(Hash) ? review["user"] : {}
        result << { "login" => author["login"], "name" => author["login"] }
      end
      result
    end

    def search_approved_pull_requests_in_user_or_organisation(user, reviewed_by, from: nil, to: nil)
      qualifiers = { user: user, reviewed_by: reviewed_by }
      qualifiers[:from] = from unless from.nil?
      qualifiers[:to] = to unless to.nil?
      search_issues_items("is:pr", **qualifiers)
    end

    def multiple_short_commits_exist?(user, repo, commit)
      data = get_json(url_to("repos", user, repo, "commits", commit))
      data.is_a?(Hash) && !data.empty? && data["sha"].to_s == commit.to_s
    rescue ScriptError, StandardError
      false
    end

    def too_many_open_prs?(tap)
      repo = tap.respond_to?(:full_name) ? tap.full_name : tap.to_s
      search_issues_items("is:pr", repo: repo).length > 0
    rescue ScriptError, StandardError
      false
    end

    GITHUB_TRANSPORT_RETRIES_ENV = "PYBREW_GITHUB_TRANSPORT_RETRIES"
    GITHUB_TRANSPORT_BACKOFF_ENV = "PYBREW_GITHUB_TRANSPORT_BACKOFF"

    def github_transport_retries
      raw = ENV[GITHUB_TRANSPORT_RETRIES_ENV].to_s.strip
      return 0 if raw.empty?

      value = raw.to_i
      value.negative? ? 0 : [value, 5].min
    end

    def github_transport_backoff
      raw = ENV[GITHUB_TRANSPORT_BACKOFF_ENV].to_s.strip
      return 0 if raw.empty?

      value = raw.to_i
      value.negative? ? 0 : [value, 30].min
    end

    # GraphQL POST with bounded transport retries (in addition to the
    # rate-limit retry handled by ``graphql_paginated_nodes``).
    def graphql(query, variables = {}, retries: nil, retry_backoff: nil)
      attempts_left = retries.nil? ? github_transport_retries : [retries.to_i, 0].max
      backoff = retry_backoff.nil? ? github_transport_backoff : [retry_backoff.to_i, 0].max
      begin
        payload = JSON.generate(
          "query" => query.to_s,
          "variables" => variables.nil? ? {} : variables
        )
        command = [curl_executable, "-sS", "-L", "-X", "POST",
                   "-H", "Accept: application/json",
                   "-H", "Content-Type: application/json"]
        token_value = token
        command += ["-H", "Authorization: bearer #{token_value}"] unless token_value.to_s.empty?
        command += ["--data", payload, graphql_url]
        output = Utils.popen_read(*command, safe: true).to_s
        begin
          JSON.parse(output)
        rescue ScriptError, StandardError => error
          raise PybrewGitHubGraphqlError,
                "malformed GraphQL JSON from #{graphql_url}: #{error.class}: #{error.message}"
        end
      rescue PybrewGitHubGraphqlError, ScriptError, StandardError => error
        raise if attempts_left.zero?

        attempts_left -= 1
        sleep backoff if backoff.positive?
        retry
      end
    end

    def record_graphql_rate_limit(data)
      rate = data.is_a?(Hash) ? data["rateLimit"] : nil
      return nil unless rate.is_a?(Hash)

      @graphql_rate_limit = {
        "remaining" => rate["remaining"],
        "reset_at" => rate["resetAt"],
        "cost" => rate["cost"],
      }
      nil
    end

    def graphql_rate_limit
      @graphql_rate_limit
    end

    def graphql_rate_limit_remaining
      @graphql_rate_limit.nil? ? nil : @graphql_rate_limit["remaining"]
    end

    def graphql_pagination_truncated?
      !!@graphql_pagination_truncated
    end

    def graphql_data(query, variables = {})
      response = graphql(query, variables)
      unless response.is_a?(Hash)
        raise PybrewGitHubGraphqlError, "GitHub GraphQL response was not a JSON object"
      end
      errors = response["errors"]
      unless errors.nil? || Array(errors).empty?
        messages = Array(errors).map do |error|
          error.is_a?(Hash) ? (error["message"] || error["type"] || error.inspect) : error.to_s
        end
        joined = messages.join("; ")
        if joined.match?(/rate.?limit|RATE_LIMITED/i)
          rate = response["data"].is_a?(Hash) ? response["data"]["rateLimit"] : nil
          reset_at = rate.is_a?(Hash) ? rate["resetAt"].to_s : ""
          reset = begin
            require "time"
            reset_at.empty? ? Time.now.to_i + 60 : Time.parse(reset_at).to_i
          rescue StandardError
            Time.now.to_i + 60
          end
          raise API::RateLimitExceededError.new(
            joined, reset: reset, resource: "graphql", limit: nil
          )
        end
        raise PybrewGitHubGraphqlError, "GitHub GraphQL error: #{joined}"
      end
      data = response["data"]
      unless data.is_a?(Hash) && !data.empty?
        raise PybrewGitHubGraphqlError, "GitHub GraphQL response contained no data"
      end
      record_graphql_rate_limit(data)
      data
    end

    # Walk GraphQL cursor pagination (pageInfo.hasNextPage/endCursor), stopping
    # after max_pages.  The returned nodes are concatenated in page order.
    GRAPHQL_MAX_PAGES = 50
    GITHUB_MAX_BACKOFF_ENV = "PYBREW_GITHUB_MAX_BACKOFF"

    # Bounded secondary-rate-limit backoff: never negative, capped at
    # PYBREW_GITHUB_MAX_BACKOFF (default 300s) so injected/fake transports in
    # tests and offline runs cannot stall for long.
    def rate_limit_backoff_seconds(error, max_seconds = nil)
      reset = error.respond_to?(:reset) ? error.reset.to_i : 0
      seconds = [reset - Time.now.to_i, 0].max
      configured = ENV[GITHUB_MAX_BACKOFF_ENV].to_s.strip
      cap = if !max_seconds.nil?
        max_seconds.to_i
      elsif !configured.empty?
        configured.to_i
      else
        300
      end
      cap = 0 if cap.negative?
      [seconds, cap].min
    end

    def graphql_paginated_nodes(query, variables, data_path,
                                max_pages: GRAPHQL_MAX_PAGES,
                                rate_limit_retries: 0)
      nodes = []
      after = nil
      pages = 0
      @graphql_pagination_truncated = false
      retries_left = [rate_limit_retries.to_i, 0].max
      loop do
        vars = (variables || {}).dup
        vars["after"] = after unless after.nil?
        data = begin
          graphql_data(query, vars)
        rescue API::RateLimitExceededError => error
          raise if retries_left <= 0

          retries_left -= 1
          backoff = rate_limit_backoff_seconds(error)
          sleep backoff if backoff.positive?
          retry
        end
        container = Array(data_path).reduce(data) do |memo, key|
          memo.is_a?(Hash) ? memo[key] : nil
        end
        nodes.concat(Array(container.is_a?(Hash) ? container["nodes"] : nil))
        page_info = container.is_a?(Hash) ? container["pageInfo"] : nil
        pages += 1
        break unless page_info.is_a?(Hash) && page_info["hasNextPage"]

        if pages >= max_pages
          @graphql_pagination_truncated = true
          break
        end
        after = page_info["endCursor"]
        break if after.nil? || after.to_s.empty?
      end
      nodes
    end

    def graphql_repository(user, repo)
      data = graphql_data(<<~GRAPHQL, { "owner" => user.to_s, "name" => repo.to_s })
        query($owner: String!, $name: String!) {
          rateLimit { remaining resetAt cost }
          repository(owner: $owner, name: $name) {
            name description homepageUrl isPrivate
            licenseInfo { spdxId }
            defaultBranchRef { name }
            stargazerCount forkCount
          }
        }
      GRAPHQL
      raw = data["repository"]
      return nil if raw.nil?

      license = raw["licenseInfo"].is_a?(Hash) ? raw["licenseInfo"] : {}
      {
        "full_name" => "#{user}/#{repo}",
        "name" => raw["name"],
        "description" => raw["description"],
        "homepage" => raw["homepageUrl"],
        "private" => !!raw["isPrivate"],
        "license" => { "spdx_id" => license["spdxId"] },
        "default_branch" => raw.dig("defaultBranchRef", "name"),
        "stargazers_count" => raw["stargazerCount"],
        "forks_count" => raw["forkCount"],
      }
    end

    def graphql_fetch_pull_requests(name, tap_remote_repo, state: nil, version: nil)
      return [] if tap_remote_repo.nil? || tap_remote_repo.to_s.empty?

      owner, repo = tap_remote_repo.to_s.split("/", 2)
      return [] if owner.nil? || repo.nil?

      states = state.nil? ? nil : [state.to_s.upcase]
      nodes = graphql_paginated_nodes(<<~GRAPHQL, { "owner" => owner, "name" => repo, "states" => states },
        query($owner: String!, $name: String!, $states: [PullRequestState!], $after: String) {
          rateLimit { remaining resetAt cost }
          repository(owner: $owner, name: $name) {
            pullRequests(first: 100, states: $states, after: $after, orderBy: { field: UPDATED_AT, direction: DESC }) {
              pageInfo { hasNextPage endCursor }
              nodes { number title url state }
            }
          }
        }
      GRAPHQL
        ["repository", "pullRequests"])
      regex = pull_request_title_regex(name, version)
      Array(nodes).select do |pr|
        pr.is_a?(Hash) && pr["url"].to_s.include?("/pull/") && regex.match?(pr["title"].to_s)
      end.map do |pr|
        {
          "number" => pr["number"],
          "title" => pr["title"],
          "html_url" => pr["url"],
          "state" => pr["state"].to_s.downcase,
        }
      end
    end

    def graphql_pull_request_commits(user, repo, pull_request)
      nodes = graphql_paginated_nodes(<<~GRAPHQL,
        query($owner: String!, $name: String!, $number: Int!, $after: String) {
          rateLimit { remaining resetAt cost }
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              commits(first: 100, after: $after) {
                pageInfo { hasNextPage endCursor }
                nodes { oid messageHeadline }
              }
            }
          }
        }
      GRAPHQL
        { "owner" => user.to_s, "name" => repo.to_s, "number" => pull_request.to_i },
        ["repository", "pullRequest", "commits"])
      Array(nodes).map do |commit|
        {
          "sha" => commit["oid"],
          "commit" => { "message" => commit["messageHeadline"] },
        }
      end
    end

    def graphql_pull_request_labels(user, repo, pull_request)
      nodes = graphql_paginated_nodes(<<~GRAPHQL,
        query($owner: String!, $name: String!, $number: Int!, $after: String) {
          rateLimit { remaining resetAt cost }
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              labels(first: 100, after: $after) {
                pageInfo { hasNextPage endCursor }
                nodes { name }
              }
            }
          }
        }
      GRAPHQL
        { "owner" => user.to_s, "name" => repo.to_s, "number" => pull_request.to_i },
        ["repository", "pullRequest", "labels"])
      Array(nodes).map { |label| { "name" => label["name"] } }
    end

    def graphql_pull_request_changed_files(tap_remote_repo, pull_request)
      owner, repo = tap_remote_repo.to_s.split("/", 2)
      return [] if owner.nil? || repo.nil?

      nodes = graphql_paginated_nodes(<<~GRAPHQL,
        query($owner: String!, $name: String!, $number: Int!, $after: String) {
          rateLimit { remaining resetAt cost }
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              files(first: 100, after: $after) {
                pageInfo { hasNextPage endCursor }
                nodes { path }
              }
            }
          }
        }
      GRAPHQL
        { "owner" => owner, "name" => repo, "number" => pull_request.to_i },
        ["repository", "pullRequest", "files"])
      Array(nodes).map { |file| { "filename" => file["path"] } }
    end

    def graphql_repository_approved_reviews(user, repo, pull_request, commit: nil)
      nodes = graphql_paginated_nodes(<<~GRAPHQL,
        query($owner: String!, $name: String!, $number: Int!, $after: String) {
          rateLimit { remaining resetAt cost }
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              reviews(first: 100, after: $after) {
                pageInfo { hasNextPage endCursor }
                nodes { state authorAssociation author { login } commit { oid } }
              }
            }
          }
        }
      GRAPHQL
        { "owner" => user.to_s, "name" => repo.to_s, "number" => pull_request.to_i },
        ["repository", "pullRequest", "reviews"])
      result = []
      Array(nodes).each do |review|
        next unless review.is_a?(Hash) && review["state"].to_s == "APPROVED"
        next if !commit.nil? && !commit.to_s.empty? &&
                review.dig("commit", "oid").to_s != commit.to_s
        next if !review["authorAssociation"].nil? &&
                !%w[MEMBER OWNER].include?(review["authorAssociation"].to_s)

        author = review["author"].is_a?(Hash) ? review["author"] : {}
        result << { "login" => author["login"], "name" => author["login"] }
      end
      result
    end

    def graphql_members_by_team(org, team)
      nodes = graphql_paginated_nodes(<<~GRAPHQL, { "org" => org.to_s, "team" => team.to_s },
        query($org: String!, $team: String!, $after: String) {
          rateLimit { remaining resetAt cost }
          organization(login: $org) {
            team(slug: $team) {
              members(first: 100, after: $after) {
                pageInfo { hasNextPage endCursor }
                nodes { login }
              }
            }
          }
        }
      GRAPHQL
        ["organization", "team", "members"])
      Array(nodes).map { |member| { "login" => member["login"] } }
    end

    def graphql_sponsorships(user)
      nodes = graphql_paginated_nodes(<<~GRAPHQL, { "login" => user.to_s },
        query($login: String!, $after: String) {
          rateLimit { remaining resetAt cost }
          user(login: $login) {
            sponsorshipsAsSponsored(first: 100, after: $after) {
              pageInfo { hasNextPage endCursor }
              nodes {
                sponsorEntity {
                  __typename
                  ... on User { login }
                  ... on Organization { login }
                }
              }
            }
          }
        }
      GRAPHQL
        ["user", "sponsorshipsAsSponsored"])
      Array(nodes).map do |node|
        entity = node.is_a?(Hash) && node["sponsorEntity"].is_a?(Hash) ? node["sponsorEntity"] : {}
        { "login" => entity["login"] }
      end
    end

    def graphql_sponsorship_tiers(user)
      nodes = graphql_paginated_nodes(<<~GRAPHQL, { "login" => user.to_s },
        query($login: String!, $after: String) {
          rateLimit { remaining resetAt cost }
          user(login: $login) {
            sponsorsListing {
              tiers(first: 100, after: $after) {
                pageInfo { hasNextPage endCursor }
                nodes { name monthlyPriceInCents }
              }
            }
          }
        }
      GRAPHQL
        ["user", "sponsorsListing", "tiers"])
      Array(nodes).map do |tier|
        {
          "name" => tier["name"],
          "monthly_price_in_cents" => tier["monthlyPriceInCents"],
        }
      end
    end

    def unsupported_write!(name)
      raise NotImplementedError,
            "Utils::GitHub.#{name} is not implemented in the pybrew Ruby shim " \
            "(GitHub write operations are out of scope)"
    end

    def create_gist(*_args, **_kwargs)
      unsupported_write!(:create_gist)
    end

    def create_issue(*_args, **_kwargs)
      unsupported_write!(:create_issue)
    end

    def create_issue_comment(*_args, **_kwargs)
      unsupported_write!(:create_issue_comment)
    end

    def create_fork(*_args, **_kwargs)
      unsupported_write!(:create_fork)
    end

    def create_pull_request(*_args, **_kwargs)
      unsupported_write!(:create_pull_request)
    end

    def create_or_update_release(*_args, **_kwargs)
      unsupported_write!(:create_or_update_release)
    end

    def upload_release_asset(*_args, **_kwargs)
      unsupported_write!(:upload_release_asset)
    end

    def workflow_dispatch_event(*_args, **_kwargs)
      unsupported_write!(:workflow_dispatch_event)
    end

    def generate_release_notes(*_args, **_kwargs)
      unsupported_write!(:generate_release_notes)
    end

    # ``GitHub::API.sleep_for_rate_limit`` equivalent for formula code that
    # talks to ``Utils::GitHub`` directly.
    def sleep_for_rate_limit(exception)
      API.sleep_for_rate_limit(exception)
    end

    def rate_limit_error?(error)
      error.is_a?(API::RateLimitExceededError)
    end
  end
end

# Homebrew exposes the GitHub helpers both as top-level ``GitHub`` and under
# ``Utils``.  Alias the shim module so either constant resolves.
GitHub = Utils::GitHub unless defined?(GitHub)

module Utils
  module Analytics
    def self.config_true?(_key = nil)
      false
    end

    def self.default_package_fields
      {}
    end

    def self.default_package_tags
      {}
    end

    def self.deferred_curl(*_args)
      nil
    end

    def self.delete_uuid!
      nil
    end

    def self.enable!(*_args)
      nil
    end

    def self.format_count(value)
      value.to_s
    end

    def self.format_percent(value)
      "#{value}%"
    end

    def self.formula_output(*_args)
      nil
    end

    def self.cask_output(*_args)
      nil
    end

    def self.influx_message_displayed?
      false
    end

    def self.messages_displayed!
      nil
    end

    def self.messages_displayed?
      false
    end

    def self.no_message_output?
      true
    end

    def self.not_this_run?(*_args)
      false
    end

    def self.output(*_args)
      nil
    end

    def self.output_analytics(*_args)
      nil
    end

    def self.output_github_packages_downloads(*_args)
      nil
    end

    def self.report_build_error(*_args)
      nil
    end

    def self.report_command_run(*_args)
      nil
    end

    def self.report_influx(*_args)
      nil
    end

    def self.report_package_event(*_args)
      nil
    end

    def self.report_test_bot_test(*_args)
      nil
    end

    def self.table_output(*_args)
      []
    end

    def self.with_wsl_suffix_if_needed(text)
      text.to_s
    end
  end
end

module Utils
  module Svn
    def self.invalid_cert_flags
      []
    end

    def self.remote_exists?(url)
      output = Utils.popen_read("svn", "info", url.to_s).to_s
      !output.strip.empty?
    rescue StandardError
      false
    end
  end
end

class Patch
  CVE_PATTERN = /CVE-(\d{4})-(\d{4,})/

  def self.extract_cves(*strings)
    strings.flat_map { |text| text.to_s.scan(CVE_PATTERN) }
           .map { |year, id| "CVE-#{year}-#{id}" }
           .uniq
  end

  def self.resolves_type(id)
    text = id.to_s
    return "security" if text.match?(/\ACVE-\d{4}-\d{4,}\z/) ||
                         text.match?(/\AGHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}\z/i) ||
                         text.match?(/\AOSV-\d{4}-\d+\z/)

    "defect"
  end

  def self.ensure_targets_within!(text, strip:, base:)
    return if text.to_s.strip.empty?

    base_path = Pathname.new(base.to_s).expand_path
    output = nil
    if Utils.which("patch")
      output, _status = Open3.capture2e(
        "patch", "-g", "0", "-f", "--dry-run", "-#{strip}",
        stdin_data: text.to_s, chdir: base_path.to_s
      )
    end

    targets = []
    if output.nil? || output.to_s.strip.empty?
      # Fallback for hosts without `patch`: inspect headers directly.
      text.to_s.each_line do |line|
        match = line.match(/\A(?:\+\+\+|---) (?:[ab]\/)?(.+?)(?:\t.*)?$/)
        targets << match[1] unless match.nil?
      end
    else
      output.each_line do |line|
        line = line.chomp
        named = line[/\A(?:patching|checking) (?:file|symbolic link) (.+)\z/, 1] ||
                line[/hunks? ignored while patching (.+)\z/, 1]
        next if named.nil?

        targets << named
      end
    end

    bad = targets.any? do |target|
      path = Pathname.new(target.to_s)
      next true if path.absolute? || path.each_filename.any? { |part| part == ".." }

      resolved = (base_path + path).cleanpath
      !resolved.to_s.start_with?(base_path.to_s + File::SEPARATOR) && resolved != base_path
    end
    raise "Patch target escapes the staged source tree" if bad
    raise "Unsupported patch format: no target paths to verify." if targets.empty?

    nil
  end
end

class PybrewPatch
  attr_reader :name, :url, :sha256, :strip_level, :file, :directory,
              :resolves_list, :type_value, :apply_value, :data_value

  def initialize(name = nil)
    @name = name
  end

  def url(value = nil)
    @url = value unless value.nil?
    @url
  end

  def sha256(value = nil)
    @sha256 = value unless value.nil?
    @sha256
  end

  def strip(value = nil)
    @strip_level = value unless value.nil?
    @strip_level
  end

  # ``apply :p0`` declares the strip level for the external patch; it must not
  # execute anything while the (opt-in, inert) formula body is evaluated.
  def apply(*args)
    unless args.empty?
      @apply_value = args.first
      return @apply_value
    end
    # Bare ``apply`` on an explicitly built patch keeps the historical
    # execution behavior; ``apply :pN`` declarations only store metadata.
    return apply_patch! unless @url.nil? && @file.nil?

    @apply_value
  end

  def file(value = nil)
    @file = value unless value.nil?
    @file
  end

  def directory(value = nil)
    @directory = value unless value.nil?
    @directory
  end

  def resolves(*values)
    @resolves_list = Array(values).flatten.compact.map(&:to_s) unless values.empty?
    @resolves_list || []
  end

  def type(value = nil)
    @type_value = value unless value.nil?
    @type_value
  end

  def data(value = nil)
    @data_value = value unless value.nil?
    @data_value
  end

  def data?
    !@data_value.nil?
  end

  def patch_files
    [@file].compact.map(&:to_s)
  end

  # Actual apply is a separate, explicit opt-in operation (kept for callers
  # that construct patches deliberately); the bridge never invokes it.
  def apply_patch!
    source = (@file || @url).to_s
    text = if File.file?(source)
      File.read(source)
    elsif File.file?(File.basename(source))
      File.read(File.basename(source))
    else
      raise "Patch source not found: #{source}"
    end
    strip_value = (@strip_level || @apply_value || "p1").to_s
    strip_value = "p#{strip_value}" unless strip_value.start_with?("p")
    Patch.ensure_targets_within!(text, strip: strip_value, base: Dir.pwd)
    patch_binary = Utils.which("patch")
    raise "patch executable not found" if patch_binary.nil?

    output, status = Open3.capture2e(
      patch_binary.to_s, "-g", "0", "-f", "-#{strip_value}",
      stdin_data: text, chdir: Dir.pwd
    )
    raise "Patch failed: #{output}" unless status.success?

    nil
  end

  def method_missing(method_name, *args, &block)
    raise NoMethodError,
          "unsupported Homebrew patch API `#{method_name}' in the pybrew opt-in Ruby bridge; " \
          "patch blocks are not applied"
  end

  def respond_to_missing?(_method_name, _include_private = false)
    false
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

  def spec_eval(class_spec, _name = nil)
    spec = class_spec.dup
    return nil if spec.nil? || spec.url.nil?

    spec.owner = self if spec.respond_to?(:owner=)
    spec
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

  # The install body runs inside the formula context (``formula`` is self), so
  # expose the same object-model helpers that ``Formula["name"]`` provides.
  def pybrew_formula_ref
    @pybrew_formula_ref ||= PybrewFormulaRef.new(@formula_name)
  end

  def full_name
    pybrew_formula_ref.full_name
  end

  def revision
    pybrew_formula_ref.revision
  end

  def pkg_version
    pybrew_formula_ref.pkg_version
  end

  def deps
    pybrew_formula_ref.deps
  end

  def runtime_dependencies(*_args)
    pybrew_formula_ref.runtime_dependencies(*_args)
  end

  def recursive_dependencies(*_args)
    pybrew_formula_ref.recursive_dependencies(*_args)
  end

  def resources
    pybrew_formula_ref.resources
  end

  def installed?
    pybrew_formula_ref.installed?
  end

  def any_version_installed?
    pybrew_formula_ref.any_version_installed?
  end

  def latest_version_installed?
    pybrew_formula_ref.latest_version_installed?
  end

  def buildpath
    @paths["buildpath"]
  end

  def staged_path
    @paths["staged_path"]
  end

  # ``formula_opt_prefix``/``formula_opt`` mirror Homebrew's stable opt path
  # helpers for this or another formula name.
  def formula_opt_prefix(formula_name = nil)
    formula_name = name if formula_name.nil?
    opt_root = if @paths["opt_prefix"]
      @paths["opt_prefix"].dirname
    else
      @paths["prefix"] + "opt"
    end
    Pathname.new(File.join(opt_root.to_s, formula_name.to_s))
  end

  def formula_opt(formula_name = nil)
    formula_opt_prefix(formula_name)
  end

  def testpath
    Pathname.new(File.join(@paths["prefix"].to_s, "test"))
  end

  def system(*args, **options)
    environment, command, options = PybrewSystem.normalize(args, options)
    ohai("system #{command.join(' ')}") if @verbose
    success = Kernel.system(environment, *command, **options)
    raise "Failed executing: #{command.join(' ')}" unless success

    true
  end

  alias safe_system system

  def quiet_system(*args, **options)
    PybrewSystem.quiet(args, options)
  end

  def cd(path, &block)
    if block
      Dir.chdir(path.to_s, &block)
    else
      Dir.chdir(path.to_s)
      nil
    end
  end

  def inreplace(paths, before = nil, after = nil, audit_result: true, global: true, &block)
    if before.nil? && after.nil?
      if block.nil?
        raise ArgumentError, "inreplace: must supply a block or before/after params"
      end
    elsif before.nil? || after.nil?
      raise ArgumentError, "inreplace: must supply both before and after params"
    end
    Array(paths).flatten.each do |pattern|
      matches = Dir.glob(pattern.to_s)
      matches = [pattern.to_s] if matches.empty?
      matches.each do |file|
        path = Pathname.new(file)
        raise "inreplace: file not found: #{file}" unless path.file?
        helper = PybrewInreplace.new(path.read)
        if before.nil? && after.nil?
          block.call(helper)
        elsif global
          helper.gsub!(before, after, audit_result: audit_result)
        else
          helper.sub!(before, after, audit_result: audit_result)
        end
        unless helper.errors.empty?
          raise "inreplace failed for #{file}: #{helper.errors.join('; ')}"
        end
        path.write(helper.inreplace_string)
      end
    end
    nil
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

  def std_configure_args(prefix: self.prefix, libdir: "lib")
    [
      "--disable-debug",
      "--disable-dependency-tracking",
      "--prefix=#{prefix}",
      "--libdir=#{prefix}/#{libdir}",
    ]
  end

  def std_cmake_args(install_prefix: prefix, install_libdir: "lib", find_framework: "LAST")
    [
      "-DCMAKE_INSTALL_PREFIX=#{install_prefix}",
      "-DCMAKE_INSTALL_LIBDIR=#{install_libdir}",
      "-DCMAKE_BUILD_TYPE=Release",
      "-DCMAKE_FIND_FRAMEWORK=#{find_framework}",
      "-DCMAKE_VERBOSE_MAKEFILE=ON",
      "-Wno-dev",
    ]
  end

  def std_meson_args(prefix: self.prefix, libdir: "lib")
    ["--prefix=#{prefix}", "--libdir=#{libdir}", "--buildtype=release", "--wrap-mode=nofallback"]
  end

  def std_cargo_args(root: prefix, path: ".", features: nil)
    args = ["--jobs", ENV.make_jobs.to_s, "--locked", "--root=#{root}", "--path=#{path}"]
    args << "--features=#{Array(features).join(",")}" if features
    args
  end

  def std_cargo_fetch_args
    ["--locked", "--target", "host-tuple"]
  end

  def std_go_args(output: bin/name, ldflags: nil, gcflags: nil, tags: nil)
    args = ["-o=#{output}"]
    args << "-ldflags=#{ldflags}" unless ldflags.nil?
    args << "-gcflags=#{gcflags}" unless gcflags.nil?
    args << "-tags=#{Array(tags).join(",")}" unless tags.nil?
    args
  end

  def std_pip_args(prefix: self.prefix, build_isolation: false)
    args = ["--verbose", "--no-deps", "--no-binary=:all:", "--ignore-installed", "--no-compile"]
    args << "--prefix=#{prefix}" if prefix
    args << "--no-build-isolation" unless build_isolation
    args
  end

  def std_npm_args(prefix: libexec, ignore_scripts: true)
    args = ["--prefix=#{prefix}"]
    args << "--ignore-scripts" if ignore_scripts
    args
  end

  def std_shards_args
    ["--production", "--release", "--no-debug"]
  end

  def std_swift_args
    ["--configuration", "release", "--jobs", ENV.make_jobs.to_s]
  end

  def std_cabal_v2_args(installdir: bin)
    args = ["--jobs=#{ENV.make_jobs}", "--max-backjumps=100000"]
    args += ["--install-method=copy", "--installdir=#{installdir}"] if installdir
    args
  end

  def std_zig_args(prefix: self.prefix, release_mode: :fast, cpu: nil)
    mode = release_mode.to_s.downcase
    unless %w[safe fast small].include?(mode)
      raise ArgumentError, "Invalid Zig release mode: #{release_mode}"
    end

    cpu ||= Hardware::CPU.arch.to_s
    [
      "--prefix", prefix.to_s,
      "--release=#{mode}",
      "-Doptimize=Release#{mode.capitalize}",
      "--summary", "all",
      "-Dcpu=#{cpu}",
    ]
  end

  def python3
    found = Utils.which("python3")
    found.nil? ? nil : Pathname.new(found)
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

  # ``on_sonoma``/``on_ventura``/``on_sequoia`` and friends follow Homebrew's
  # exact-version platform blocks.  They are a clear no-op on non-macOS hosts
  # and accept the usual ``:or_newer``/``:or_older`` qualifier.
  MacOS::SYMBOLS.each_key do |symbol|
    define_method(:"on_#{symbol}") do |or_condition = nil, &block|
      return nil unless OS.mac?
      return nil if block.nil?

      condition = or_condition.nil? ? nil : or_condition.to_sym
      unless condition.nil? || condition == :or_newer || condition == :or_older
        raise ArgumentError, "Invalid OS `or_*` condition: #{or_condition.inspect}"
      end

      version = MacOS.version
      matched = case condition
                when :or_newer then version >= symbol
                when :or_older then version <= symbol
                else version == symbol
                end
      return nil unless matched

      block.call
    end
  end

  # ``resource("name")`` returns the resource metadata; resources are staged by
  # Python before the Ruby body runs when the build has a downloader and the
  # resource declares a URL and sha256.  The object can still be queried
  # (name/url/sha256/version); touching its staged path raises a clear error
  # when it could not be staged.
  def resource(name)
    pybrew_formula_ref.resource(name)
  end

  # Formula patches cannot be applied by the bridge.  Consume the block DSL
  # (so url/sha256 calls do not explode) and warn rather than silently
  # pretending the patch happened.  ``patch :DATA`` is likewise a warned
  # no-op because the bridge never applies patches.
  def patch(*args, &block)
    target = args.empty? ? nil : args.first
    if block
      definition = PybrewPatch.new(target)
      definition.instance_eval(&block)
      opoo("patch #{target.nil? ? 'block' : target.inspect} was not applied: the pybrew opt-in Ruby bridge does not apply patches")
    else
      description = target.nil? ? "without a target" : target.inspect
      opoo("patch #{description} was not applied: the pybrew opt-in Ruby bridge does not apply patches")
    end
    nil
  end

  def option(*_args)
    nil
  end

  def depends_on(*_args)
    nil
  end

  # Unknown formula APIs fail loudly with a pointer to the shim instead of a
  # bare NoMethodError deep inside instance_eval.
  def method_missing(method_name, *args, &block)
    raise NoMethodError,
          "unsupported Homebrew formula API `#{method_name}' in the pybrew arbitrary-Ruby install shim; " \
          "see src/pybrew/ruby_executor.py for the supported API"
  end
end

class PybrewTestContext < PybrewInstallContext
  class Failure < StandardError; end

  def initialize(config, formula)
    super(config)
    @test_formula = formula
  end

  def formula
    @test_formula
  end

  def shell_output(command, expected_status = 0, stderr: false, env: nil, chdir: nil)
    stdout, stderr_text, status = run_test_command(command, nil, env, chdir)
    if status.exitstatus != expected_status
      raise Failure,
            "shell_output failed with exit status #{status.exitstatus} " \
            "(expected #{expected_status}): #{command}\n#{stderr_text}"
    end
    stderr ? stderr_text : stdout
  end

  def pipe_output(command, input = nil, stderr: false, env: nil, chdir: nil)
    stdout, stderr_text, = run_test_command(command, input, env, chdir)
    stderr ? stderr_text : stdout
  end

  def assert(value, message = nil)
    return if value

    raise Failure, message || "Expected #{value.inspect} to be truthy"
  end

  def refute(value, message = nil)
    return unless value

    raise Failure, message || "Expected #{value.inspect} to be falsey"
  end

  def assert_equal(expected, actual, message = nil)
    return if expected == actual

    raise Failure, message || "Expected #{expected.inspect} to equal #{actual.inspect}"
  end

  def refute_equal(expected, actual, message = nil)
    return unless expected == actual

    raise Failure, message || "Expected #{expected.inspect} to not equal #{actual.inspect}"
  end
  alias assert_not_equal refute_equal

  def assert_match(pattern, string, message = nil)
    regexp = pattern.is_a?(Regexp) ? pattern : Regexp.new(pattern.to_s)
    return if regexp.match?(string.to_s)

    raise Failure, message || "Expected #{string.inspect} to match #{pattern.inspect}"
  end

  def refute_match(pattern, string, message = nil)
    regexp = pattern.is_a?(Regexp) ? pattern : Regexp.new(pattern.to_s)
    return unless regexp.match?(string.to_s)

    raise Failure, message || "Expected #{string.inspect} to not match #{pattern.inspect}"
  end

  def assert_predicate(object, predicate, message = nil)
    return if object.__send__(predicate)

    raise Failure, message || "Expected #{object.inspect} to satisfy #{predicate}"
  end

  def refute_predicate(object, predicate, message = nil)
    return unless object.__send__(predicate)

    raise Failure, message || "Expected #{object.inspect} to not satisfy #{predicate}"
  end

  def assert_empty(object, message = nil)
    return if object.empty?

    raise Failure, message || "Expected #{object.inspect} to be empty"
  end

  def refute_empty(object, message = nil)
    return unless object.empty?

    raise Failure, message || "Expected #{object.inspect} to not be empty"
  end

  def assert_includes(collection, object, message = nil)
    return if collection.include?(object)

    raise Failure, message || "Expected #{collection.inspect} to include #{object.inspect}"
  end

  def refute_includes(collection, object, message = nil)
    return unless collection.include?(object)

    raise Failure, message || "Expected #{collection.inspect} to not include #{object.inspect}"
  end

  def assert_nil(object, message = nil)
    return if object.nil?

    raise Failure, message || "Expected #{object.inspect} to be nil"
  end

  def refute_nil(object, message = nil)
    return unless object.nil?

    raise Failure, message || "Expected #{object.inspect} to not be nil"
  end

  def assert_output(expected, actual = nil, message = nil)
    if block_given?
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

    raise Failure, message || "Expected output #{expected.inspect} to equal #{actual.inspect}"
  end

  private

  def run_test_command(command, input, env, chdir)
    options = {}
    options[:stdin_data] = input.to_s unless input.nil?
    options[:chdir] = chdir.to_s unless chdir.nil?
    Open3.capture3(env || {}, *Array(command), **options)
  end
end

context = PybrewInstallContext.new(PYBREW_CONFIG)
$pybrew_installing_formula = PYBREW_CONFIG["formula"]["name"].to_s
begin
  context.instance_eval(
    PYBREW_BODY,
    "#{PYBREW_CONFIG['formula']['name']}.rb",
    PYBREW_CONFIG["body_line"].to_i
  )
ensure
  $pybrew_installing_formula = nil
end
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


def _latest_keg(config: Config, name: str) -> Optional[Path]:
    """Return the newest installed keg for ``name``, or ``None``."""

    rack = config.cellar / name
    try:
        candidates = [child for child in rack.iterdir() if child.is_dir()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(
        candidates,
        key=functools.cmp_to_key(
            lambda left, right: compare_versions(left.name, right.name)
        ),
    )


def _dependency_name(dependency: Any) -> str:
    """Return the name of a dependency-like object."""

    name = getattr(dependency, "name", None)
    if name is None:
        name = str(dependency)
    return str(name)


def _dependency_tags(dependency: Any) -> list[str]:
    """Return the declared Homebrew tags for a dependency."""

    tags = getattr(dependency, "tags", None) or ()
    try:
        values = [str(tag).lstrip(":") for tag in tags]
    except TypeError:
        values = [str(tags).lstrip(":")]
    return sorted({tag for tag in values if tag})


def _dependency_metadata(formula: Formula) -> list[dict[str, Any]]:
    """Return declared dependencies (name and tags) for the Ruby shim."""

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for collection in (
        formula.dependencies,
        formula.head_dependencies,
        formula.build_dependencies,
        formula.test_dependencies,
        formula.recommended_dependencies,
        formula.optional_dependencies,
        formula.uses_from_macos,
    ):
        for dependency in collection:
            name = _dependency_name(dependency)
            if not name or name in seen:
                continue
            seen.add(name)
            entries.append({"name": name, "tags": _dependency_tags(dependency)})
    return entries


def _dependency_category_metadata(formula: Formula, attribute: str) -> list[dict[str, Any]]:
    """Return name/tag metadata for one dependency category."""

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dependency in getattr(formula, attribute, []) or []:
        name = _dependency_name(dependency)
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append({"name": name, "tags": _dependency_tags(dependency)})
    return entries


def _runtime_dependency_names(formula: Formula) -> list[str]:
    """Return runtime dependency names in declaration order."""

    names: list[str] = []
    for dependency in formula.runtime_dependencies():
        name = _dependency_name(dependency)
        if name and name not in names:
            names.append(name)
    return names


def _receipt_dependency_metadata(keg: Path) -> list[dict[str, Any]]:
    """Return runtime dependencies recorded in an installed keg receipt."""

    from .cellar import read_receipt

    receipt = read_receipt(keg)
    names = receipt.get("runtime_dependencies") if isinstance(receipt, dict) else None
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(names, list):
        return entries
    for name in names:
        text = str(name)
        if not text or text in seen:
            continue
        seen.add(text)
        entries.append({"name": text, "tags": []})
    return entries


def _service_metadata(formula: Formula, config: Config) -> Optional[dict[str, Any]]:
    source = None
    if isinstance(formula.service, dict):
        source = formula.service.get("source")
    if not source:
        return None
    raw = source.strip()
    if raw.startswith("service do"):
        raw = raw[len("service do"):].strip()
    if raw.endswith("end"):
        raw = raw[:-3].strip()
    try:
        from .services import parse_service_block

        spec = parse_service_block(formula, config, raw)
    except Exception:
        return None
    return {
        "source": source,
        "name": formula.name,
        "run": list(spec.run),
        "working_dir": spec.working_dir,
        "log_path": spec.log_path,
        "error_log_path": spec.error_log_path,
        "keep_alive": spec.keep_alive,
        "interval": spec.interval,
        "process_type": spec.process_type,
        "run_type": spec.run_type,
        "plist_name": spec.plist_name,
        "service_name": spec.service_name,
        "environment_variables": dict(spec.environment_variables),
        "require_root": spec.require_root,
        "launch_only_once": spec.launch_only_once,
        "macos_legacy_timers": spec.macos_legacy_timers,
        "throttle_interval": spec.throttle_interval,
        "nice": spec.nice,
        "restart_delay": spec.restart_delay,
        "stop_timeout": spec.stop_timeout,
        "root_dir": spec.root_dir,
        "input_path": spec.input_path,
        "cron": dict(spec.cron),
        "sockets": dict(spec.sockets),
        "warnings": list(spec.warnings),
    }


def _livecheck_metadata(formula: Formula) -> Optional[dict[str, Any]]:
    """Parse the common literal ``livecheck do ... end`` directives."""
    source = formula.livecheck_source or ""
    if not source.strip():
        return None

    data: dict[str, Any] = {
        "source": source,
        "url": None,
        "regex": None,
        "strategy": None,
        "skip": None,
    }
    url_match = re.search(
        r"^\s*url\s+(?:\"([^\"]+)\"|'([^']+)'|:(\w+))", source, re.MULTILINE
    )
    if url_match is not None:
        if url_match.group(1) is not None:
            data["url"] = url_match.group(1)
        elif url_match.group(2) is not None:
            data["url"] = url_match.group(2)
        else:
            symbol = url_match.group(3)
            spec = formula.stable if symbol == "stable" else formula.head if symbol == "head" else None
            data["url"] = getattr(spec, "url", None)

    regex_match = re.search(
        r"^\s*regex\s*\(?\s*/(.+?)/[a-z]*\s*\)?\s*$", source, re.MULTILINE
    )
    if regex_match is None:
        regex_match = re.search(
            r"^\s*regex\s*\(?\s*[\"'](.+?)[\"']\s*\)?", source, re.MULTILINE
        )
    if regex_match is not None:
        data["regex"] = regex_match.group(1)

    strategy_match = re.search(r"^\s*strategy\s+:(\w+)", source, re.MULTILINE)
    if strategy_match is not None:
        data["strategy"] = strategy_match.group(1)

    skip_match = re.search(r"^\s*skip\b(?:\s+\"([^\"]*)\")?", source, re.MULTILINE)
    if skip_match is not None:
        data["skip"] = skip_match.group(1) if skip_match.group(1) is not None else True
    return data


def _install_body_for_reference(formula: Formula) -> str:
    """Return the bare ``def install`` body for ``Formula#install``."""

    try:
        body, _line = extract_install_body(formula)
        return body
    except Exception:
        return ""


def _formula_reference_map(
    formula: Formula, config: Config, keg: Path
) -> dict[str, dict[str, Any]]:
    """Metadata backing the Ruby ``Formula`` object model in the shim.

    The current formula and its declared dependencies are included.  The
    ``prefix`` entry is ``None`` when no keg is installed; the Ruby side turns
    that into a clear error only when the method is actually called.  The
    current formula's build target (``keg``) is exposed as its ``prefix`` so
    the install DSL and ``Formula[name]`` agree during a build.
    """

    dependencies = _dependency_metadata(formula)
    current: dict[str, Any] = {
        "name": formula.name,
        "full_name": formula.full_name,
        "version": formula.version or "HEAD",
        "revision": int(formula.revision or 0),
        "version_scheme": int(formula.version_scheme or 0),
        "deny_network_access": bool(formula.deny_network_access),
        "pkg_version": formula.pkg_version,
        "prefix": str(keg),
        "opt_prefix": str(config.opt_path(formula.name)),
        "dependencies": dependencies,
        "runtime_dependencies": _runtime_dependency_names(formula),
        "build_dependencies": _dependency_category_metadata(formula, "build_dependencies"),
        "test_dependencies": _dependency_category_metadata(formula, "test_dependencies"),
        "recommended_dependencies": _dependency_category_metadata(formula, "recommended_dependencies"),
        "optional_dependencies": _dependency_category_metadata(formula, "optional_dependencies"),
        "head_dependencies": _dependency_category_metadata(formula, "head_dependencies"),
        "uses_from_macos": _dependency_category_metadata(formula, "uses_from_macos"),
        "conflicts_with": list(formula.conflicts_with),
        "requirements": [
            {"name": requirement.name, "tags": sorted(requirement.tags)}
            for requirement in formula.requirements
        ],
        "link_overwrite": list(formula.link_overwrite),
        "desc": formula.desc,
        "homepage": formula.homepage,
        "license": formula.license,
        "tap": formula.tap,
        "path": str(formula.path) if formula.path is not None else None,
        "keg_only": bool(formula.keg_only),
        "keg_only_reason": formula.keg_only_reason,
        "stable": {
            "url": formula.stable.url,
            "sha256": formula.stable.sha256,
            "version": formula.version or "HEAD",
            "mirrors": list(formula.stable.mirrors),
        },
        "head": {
            "url": formula.head.url,
            "sha256": formula.head.sha256,
            "version": "HEAD",
            "mirrors": list(formula.head.mirrors),
        },
        "bottle": {
            "name": formula.name,
            "root_url": formula.bottle.root_url,
            "files": {
                tag: {"sha256": item.sha256, "cellar": item.cellar}
                for tag, item in formula.bottle.files.items()
            },
            "current_tag": (
                formula.bottle_for_tag()[0]
                if formula.bottle_for_tag() is not None
                else None
            ),
        },
        "deprecated": bool(formula.deprecated),
        "disabled": bool(formula.disabled),
        "deprecation_reason": formula.deprecation_reason,
        "disable_reason": formula.disable_reason,
        "aliases": list(formula.aliases),
        "oldname": formula.oldname,
        "homebrew_prefix": str(config.prefix),
        "cellar": str(config.cellar),
        "head_version": "HEAD" if getattr(formula.head, "url", None) else None,
        "post_install?": bool(formula.post_install_source),
        "fetch?": bool(formula.fetch_source),
        "livecheck?": bool(formula.livecheck_source),
        "livecheck": _livecheck_metadata(formula),
        "service": _service_metadata(formula, config),
        "service?": bool(
            isinstance(formula.service, dict) and formula.service.get("source")
        ),
        "caveats?": bool(formula.caveats),
        "caveats_source": formula.caveats or "",
        "test_source": formula.test_source or "",
        "install_source": formula.install_source or "",
        "install_body": _install_body_for_reference(formula),
        "livecheck_source": formula.livecheck_source or "",
        "post_install_source": formula.post_install_source or "",
        "fetch_source": formula.fetch_source or "",
        "test?": bool(formula.test_source),
        "options": list(formula.options),
        "deprecated_options": dict(formula.deprecated_options),
        "fails_with": list(formula.fails_with),
        "preserve_rpath": bool(formula.preserve_rpath),
        "skip_clean": list(formula.skip_clean),
        "patches": [
            {
                "url": patch.url,
                "file": patch.file,
                "sha256": patch.sha256,
                "strip": patch.strip,
                "data": patch.data is not None,
            }
            for patch in formula.patches
        ],
        "resources": {},
    }
    entries: dict[str, dict[str, Any]] = {formula.name: current}
    for dependency in dependencies:
        name = str(dependency["name"])
        if name in entries:
            continue
        installed = _latest_keg(config, name)
        installed_dependencies = (
            _receipt_dependency_metadata(installed) if installed is not None else []
        )
        entries[name] = {
            "name": name,
            "full_name": name,
            "version": installed.name if installed is not None else None,
            "revision": 0,
            "pkg_version": installed.name if installed is not None else None,
            "prefix": str(installed) if installed is not None else None,
            "opt_prefix": str(config.opt_path(name)),
            "dependencies": installed_dependencies,
            "runtime_dependencies": [
                item["name"] for item in installed_dependencies
            ],
            "resources": {},
        }
    return entries


def _resource_version(url: Optional[str]) -> Optional[str]:
    """Infer a Homebrew-style version from a resource URL."""

    if not url:
        return None
    version = detect_version(str(url))
    if version == "HEAD" and "head" not in str(url).lower():
        return None
    return version


def _resource_metadata(
    formula: Formula,
) -> dict[str, dict[str, Optional[str]]]:
    """Return resource name/url/sha256/version metadata for the Ruby shim.

    This function only describes the declared resources; downloading and
    extraction happen separately in :func:`_stage_resources` when the caller
    supplies a downloader.  An explicit ``staged_path``/``staged`` attribute on
    the resource is passed through as the staged path.
    """

    resources = getattr(formula, "resources", None) or {}
    payload: dict[str, dict[str, Optional[str]]] = {}
    try:
        items = resources.items()
    except AttributeError:
        return payload
    for name, resource in items:
        url = getattr(resource, "url", None)
        sha256 = getattr(resource, "sha256", None)
        staged = getattr(resource, "staged_path", None)
        if staged is None:
            staged = getattr(resource, "staged", None)
        payload[str(name)] = {
            "url": str(url) if url is not None else None,
            "sha256": str(sha256) if sha256 is not None else None,
            "version": _resource_version(url),
            "staged": str(staged) if staged is not None else None,
        }
    return payload


def _attach_staged_resource_paths(
    resources: dict[str, dict[str, Optional[str]]], build_root: Path
) -> None:
    """Expose resources already staged under ``build_root`` to the shim.

    The default source builder stages downloaded resources in
    ``build_root/.pybrew-resources/<name>``.  When that directory already
    exists the Ruby bridge reuses it instead of downloading anything.
    """

    staged_root = Path(build_root) / RESOURCE_STAGE_DIR
    for name, info in resources.items():
        if info.get("staged"):
            continue
        candidate = staged_root / str(name)
        if candidate.is_dir():
            info["staged"] = str(candidate)


def _safe_resource_name(name: str) -> str:
    """Return ``name`` when it can be used as a staging directory name."""

    text = str(name)
    if (
        not text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or Path(text).is_absolute()
    ):
        raise BuildError(f"unsafe formula resource name for staging: {text!r}")
    return text


def _extract_resource_archive(archive: Path, destination: Path) -> None:
    """Extract a zip or tar(.gz) resource archive into ``destination`` safely.

    Absolute member names, ``..`` traversal and archive links are rejected;
    only regular files and directories are written.  Extraction errors are
    reported as :class:`BuildError` so the Ruby install fails clearly.
    """

    import tarfile
    import zipfile

    if zipfile.is_zipfile(archive):
        try:
            with zipfile.ZipFile(archive) as bundle:
                for info in bundle.infolist():
                    name = info.filename
                    parts = Path(name).parts
                    if not name or name.startswith(("/", "\\")) or ".." in parts:
                        raise BuildError(f"unsafe resource zip member: {name!r}")
                    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                        link_text = bundle.read(info).decode("utf-8", "replace")
                        _create_safe_archive_link(
                            destination, destination / name, link_text,
                            kind="zip symlink", name=name
                        )
                        continue
                    target = destination / name
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(info) as source, open(target, "wb") as output:
                        shutil.copyfileobj(source, output)
        except zipfile.BadZipFile as error:
            raise BuildError(
                f"cannot read resource archive {archive}: {error}"
            ) from error
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
                elif member.issym():
                    _create_safe_archive_link(
                        destination, target, member.linkname, kind="tar symlink", name=name
                    )
                elif member.islnk():
                    link_source = destination / member.linkname
                    if not _path_within(destination, link_source) or not link_source.exists():
                        raise BuildError(
                            f"unsafe resource hard link: {name!r} -> {member.linkname!r}"
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        target.unlink()
                    os.link(link_source, target)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(member)
                    if source is None:
                        raise BuildError(f"cannot read resource tar member: {name!r}")
                    with source, open(target, "wb") as output:
                        shutil.copyfileobj(source, output)
                else:
                    raise BuildError(
                        f"resource tar member is not a file or directory: {name!r}"
                    )
    except tarfile.TarError as error:
        raise BuildError(f"cannot read resource archive {archive}: {error}") from error


_RESOURCE_LITERAL_CALL = re.compile(
    r"""resource\s*\(\s*(['"])([^'"]+)\1"""
)


def _referenced_resource_names(body: str) -> Optional[set[str]]:
    """Return literal resource names used by an install body.

    ``None`` means the body uses a dynamic form (``resources.each`` or a
    variable name), so the caller must conservatively stage every resource.
    A set means only those literal ``resource("name")`` calls need staging.
    """

    if not re.search(r"\bresource\b", body):
        return set()
    names = set(_RESOURCE_LITERAL_CALL.findall(body))
    # The regex above returns (quote, name) pairs.
    literal_names = {name for _quote, name in names}
    # Any other mention of resource/resources means we cannot prove the set.
    without_literals = _RESOURCE_LITERAL_CALL.sub("", body)
    if re.search(r"\bresource", without_literals):
        return None
    return literal_names


def _path_within(root: Path, candidate: Path) -> bool:
    """Return True when ``candidate`` stays inside ``root`` after resolution."""

    try:
        root_resolved = Path(root).resolve()
        candidate_resolved = Path(candidate).resolve()
    except OSError:
        return False
    try:
        return os.path.commonpath([str(root_resolved), str(candidate_resolved)]) == str(
            root_resolved
        )
    except ValueError:
        return False


def _create_safe_archive_link(
    destination: Path, target: Path, link_text: str, *, kind: str, name: str
) -> None:
    """Create a relative symlink that cannot escape the staging directory."""

    link_value = str(link_text or "")
    if not link_value or os.path.isabs(link_value):
        raise BuildError(f"unsafe resource {kind}: {name!r} -> {link_value!r}")
    if ".." in Path(link_value).parts:
        resolved = (target.parent / link_value).resolve()
        if not _path_within(destination, resolved):
            raise BuildError(f"unsafe resource {kind}: {name!r} -> {link_value!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    os.symlink(link_value, target)


def _stage_resources(
    formula: Formula,
    resources: dict[str, dict[str, Optional[str]]],
    build_root: Path,
    downloader: Optional[Any],
    *,
    only: Optional[set[str]] = None,
) -> None:
    """Download and stage declared resources for the Ruby shim.

    Resources that are already staged (an explicit ``staged_path``/``staged``
    attribute or a directory left under ``build_root/.pybrew-resources``) are
    reused.  With a downloader, every remaining resource that has both a URL
    and a SHA-256 is fetched with ``downloader.fetch`` and safely extracted
    under ``build_root/.pybrew-resources/<name>``; that directory becomes the
    resource's staged path.  Resources without a downloader, URL or checksum
    stay metadata-only and record a clear reason for the Ruby side: nothing is
    ever fetched silently.
    """

    for name in resources:
        _safe_resource_name(name)
    _attach_staged_resource_paths(resources, build_root)
    staged_root = Path(build_root) / RESOURCE_STAGE_DIR
    for name, info in resources.items():
        if info.get("staged"):
            continue
        if only is not None and name not in only:
            info["staged_error"] = "the resource is not referenced by the install body"
            continue
        url = info.get("url")
        sha256 = info.get("sha256")
        if downloader is None:
            info["staged_error"] = (
                "no downloader was provided to the opt-in Ruby bridge"
            )
            continue
        if not url:
            info["staged_error"] = "the resource has no url"
            continue
        if not sha256:
            info["staged_error"] = (
                "the resource has no sha256; refusing to download it without verification"
            )
            continue
        destination = staged_root / name
        archive = downloader.fetch(str(url), str(sha256), hint=f"{formula.name}-{name}")
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        _extract_resource_archive(archive, destination)
        info["staged"] = str(destination)
        info["staged_from_archive"] = "true"


def execute_install_body(
    formula: Formula,
    config: Config,
    build_root: Path,
    keg: Path,
    *,
    env: Optional[dict[str, str]] = None,
    verbose: bool = False,
    downloader: Optional[Any] = None,
) -> None:
    """Execute ``formula``'s raw ``def install`` body with Ruby.

    The Ruby process runs with ``build_root`` as its working directory and the
    supplied environment (normally the source-builder's Homebrew-like build
    environment).  When ``downloader`` is provided, declared resources with a
    URL and SHA-256 are fetched and staged under ``build_root`` before Ruby
    runs; without it they stay metadata-only.  Any failure raises
    :class:`BuildError` containing the Ruby stderr output and, when Ruby
    reports it, the failing formula line.
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
    resources = _resource_metadata(formula)
    _stage_resources(
        formula,
        resources,
        build_root,
        downloader,
        only=_referenced_resource_names(body),
    )
    formulae = _formula_reference_map(formula, config, keg)
    formulae[formula.name]["buildpath"] = str(build_root)
    formulae[formula.name]["staged_path"] = str(keg)
    formulae[formula.name]["resources"] = resources
    payload = {
        "formula": {
            "name": formula.name,
            "version": version,
            "full_name": formula.full_name,
            "revision": int(formula.revision or 0),
        "version_scheme": int(formula.version_scheme or 0),
        "deny_network_access": bool(formula.deny_network_access),
            "pkg_version": formula.pkg_version,
        },
        "paths": _install_paths(formula, config, keg, build_root),
        "verbose": bool(verbose),
        "arch": os.environ.get("PYBREW_TARGET_ARCH") or platform.machine(),
        "macos_version": platform.mac_ver()[0],
        "kernel_version": platform.release(),
        "body_line": body_line,
        "homebrew_prefix": str(config.prefix),
        "cellar": str(config.cellar),
        "head_version": "HEAD" if getattr(formula.head, "url", None) else None,
        "cellar": str(config.cellar),
        "taps": [str(tap) for tap in getattr(config, "taps", [])],
        "formulae": formulae,
        "resources": resources,
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
