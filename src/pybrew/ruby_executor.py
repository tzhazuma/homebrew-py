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
require "tmpdir"

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
HOMEBREW_PREFIX_PLACEHOLDER = "$HOMEBREW_PREFIX"
HOMEBREW_CELLAR_PLACEHOLDER = "$HOMEBREW_CELLAR"
HOMEBREW_HOME_PLACEHOLDER = "$HOMEBREW_HOME"

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
    return nil unless OS.mac?

    requested = version.nil? ? nil : version.to_s
    return @sdk if requested.nil? && defined?(@sdk) && @sdk

    raw = ENV["HOMEBREW_SDKROOT"].to_s.strip
    if raw.empty?
      command = if requested.nil?
        "/usr/bin/xcrun --show-sdk-path"
      else
        "/usr/bin/xcrun --sdk macosx#{requested} --show-sdk-path"
      end
      raw = `#{command} 2>/dev/null`.to_s.strip
    end
    if raw.empty?
      fallback = "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
      raw = fallback if File.directory?(fallback)
    end
    return nil if raw.empty?

    version_text = `/usr/bin/xcrun --show-sdk-version 2>/dev/null`.to_s.strip
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
    return @active_developer_dir if defined?(@active_developer_dir)

    raw = `/usr/bin/xcode-select -print-path 2>/dev/null`.to_s.strip
    if raw.empty?
      fallback = "/Applications/Xcode.app/Contents/Developer"
      raw = fallback if File.directory?(fallback)
    end
    @active_developer_dir = raw
  rescue StandardError
    ""
  end

  module CLT
    def self.installed?
      path = `/usr/bin/xcode-select -print-path 2>/dev/null`.to_s.strip
      !path.empty? && File.directory?(path)
    rescue StandardError
      false
    end

    def self.version
      raw = `/usr/sbin/pkgutil --pkg-info=com.apple.pkg.CLTools_Executables 2>/dev/null`.to_s
      match = raw[/^version:\s*(\S+)/, 1]
      match.nil? || match.empty? ? nil : PybrewVersion.new(match)
    rescue StandardError
      nil
    end

    def self.sdk_path
      MacOS.sdk_path
    end
  end

  module Xcode
    def self.installed?
      return false unless OS.mac?

      !active_developer_dir.to_s.empty? && File.directory?(active_developer_dir.to_s)
    rescue StandardError
      false
    end

    def self.version
      return nil unless installed?

      output = `/usr/bin/xcodebuild -version 2>/dev/null`.to_s
      match = output[/Xcode\s+(\S+)/, 1]
      match.nil? || match.empty? ? nil : PybrewVersion.new(match)
    rescue StandardError
      nil
    end

    def self.prefix
      installed? ? Pathname.new(active_developer_dir.to_s) : nil
    end

    def self.sdk_path
      MacOS.sdk_path
    end

    def self.default_developer_dir
      "/Applications/Xcode.app/Contents/Developer"
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
  module Git
    def self.git(*args)
      Utils.git(*args)
    end

    def self.available?
      !Utils.which("git").nil?
    end
  end

  module Curl
    def self.curl(*args)
      Utils.curl(*args)
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
    configured = self["HOMEBREW_CC"].to_s
    return configured.to_sym unless configured.empty?

    find_tool("clang") ? :clang : :gcc
  end

  def cc
    self["CC"] || RbConfig::CONFIG["CC"] || "cc"
  end

  def cxx
    self["CXX"] || RbConfig::CONFIG["CXX"] || "c++"
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
  attr_reader :tag, :sha256, :cellar

  def initialize(tag, info)
    @tag = tag.to_s
    @sha256 = (info || {})["sha256"]
    @cellar = (info || {})["cellar"]
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

  def bottle?
    !@files.empty?
  end

  def bottle_tags
    @files.keys
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
    send(requested) || stable || head
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
    nil
  end

  def with_logging(*_args, &block)
    block.call if block
  end

  def common_sandbox_env
    {}
  end

  def test_sandbox_env
    {}
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

  def spec_eval(*_args)
    nil
  end

  def spec_syms
    []
  end

  def specs
    {}
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

  def optlink
    homebrew_prefix/"opt"/name
  end

  def opt_record
    optlink
  end

  def installed?
    @path.directory?
  end

  def linked?
    return false unless File.symlink?(optlink.to_s)

    File.identical?(optlink.to_s, @path.to_s)
  end
  alias optlinked? linked?

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

  def codesign_patched_binary(_file)
    nil
  end

  def codesign_patched_binaries(_files)
    nil
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
    tab.aliases
  end

  def empty_installation?
    @path.directory? && @path.children.empty?
  end

  def require_relocation?
    false
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

class Tab
  attr_reader :tabfile

  def initialize(data = {}, tabfile = nil)
    @data = data.is_a?(Hash) ? data : {}
    @tabfile = tabfile.nil? ? nil : Pathname.new(tabfile.to_s)
  end

  def self.empty
    new({})
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

  def self.for_name(name)
    cellar = Pathname.new(PybrewFormulaRef.cellar)
    rack = cellar/name.to_s
    return empty unless rack.directory?

    kegs = rack.children.select { |child| child.directory? }
    return empty if kegs.empty?

    for_keg(kegs.max_by { |child| child.mtime })
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

  def time
    @data["time"]
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

    def kernel
      Etc.uname[:machine].to_s
    end

    def branch
      "(none)"
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

    def dump_verbose_config(out = $stdout)
      out.puts "Homebrew Ruby: #{describe_homebrew_ruby}"
      out.puts "Clang: #{describe_clang}"
      out.puts "Git: #{describe_git}"
      out.puts "Curl: #{describe_curl}"
      hardware_text = hardware
      out.puts hardware_text unless hardware_text.nil?
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
end

module Formulary
  module_function

  def factory(name, *_args, **_kwargs)
    Formula[name]
  end

  def resolve(name, *_args, **_kwargs)
    Formula[name]
  end

  def load_formula(name, _path = nil, _contents = nil, _namespace = nil)
    Formula[name]
  end

  def class_s(name)
    class_name = name.to_s.capitalize
    class_name.gsub!(/[-_.\s][a-zA-Z0-9]/) { |matched| matched.chars.fetch(-1).upcase }
    class_name.tr!("+", "x")
    class_name.sub!(/(.)@(\d)/, "\\1AT\\2")
    class_name
  end

  def from_rack(rack, *_args, **_kwargs)
    keg = Keg.from_rack(rack)
    factory(keg.nil? ? Pathname.new(rack.to_s).basename.to_s : keg.name)
  end

  def keg_only?(rack)
    factory(Pathname.new(rack.to_s).basename.to_s).keg_only?
  rescue StandardError
    false
  end

  def formula_class_defined_from_path?(_path)
    false
  end

  def formula_class_defined_from_api?(_name)
    false
  end

  def formula_class_get_from_path(_path)
    raise "formula classes are not loaded by the pybrew Ruby shim"
  end

  def formula_class_get_from_api(_name)
    raise "formula classes are not loaded by the pybrew Ruby shim"
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
    nil
  end

  def factory_cached?
    false
  end

  def enable_factory_cache!
    nil
  end
end

class PybrewResource
  attr_reader :name, :url, :sha256

  def initialize(name, info)
    @name = name.to_s
    @info = info || {}
    @url = @info["url"]
    @sha256 = @info["sha256"]
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
  # produced by the archive, exactly like Homebrew's unpack helper.
  def stage(*_args, &block)
    path = staged_path
    return path if block.nil?

    Dir.chdir(path.to_s) do
      entries = Dir["*"].reject { |entry| entry == "__MACOSX" }
      if @info["staged_from_archive"] == "true" &&
         entries.length == 1 &&
         File.directory?(entries.first)
        Dir.chdir(entries.first) { block.call(Pathname.pwd) }
      else
        block.call(Pathname.pwd)
      end
    end
  end

  def fetch
    staged_path
  end

  def to_s
    name
  end

  def inspect
    "#<PybrewResource #{name}>"
  end
end

class PybrewPatch
  attr_reader :name, :url, :sha256, :strip_level

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

  def apply(*_args)
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
        "run": list(spec.run),
        "working_dir": spec.working_dir,
        "log_path": spec.log_path,
        "error_log_path": spec.error_log_path,
        "keep_alive": spec.keep_alive,
        "interval": spec.interval,
        "process_type": spec.process_type,
        "run_type": spec.run_type,
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
                        raise BuildError(
                            f"resource zip links are not supported: {name!r}"
                        )
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
                elif member.issym() or member.islnk():
                    raise BuildError(f"resource tar links are not supported: {name!r}")
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
