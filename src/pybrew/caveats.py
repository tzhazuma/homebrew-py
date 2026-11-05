"""Render a formula's captured ``def caveats`` block with the host Ruby.

Homebrew caveats are ordinary Ruby: a method that returns a string (usually a
heredoc) which may be assembled conditionally with ``on_macos``/``on_linux``
and ``on_arm``/``on_intel`` blocks.  pybrew's parser captures the raw
``def caveats ... end`` source rather than evaluating Ruby, so this module
executes that source with a deliberately small compatibility shim:

* heredocs and ordinary string operations work because real Ruby runs them;
* ``prefix``/``bin``/``opt_prefix``/``pkgshare`` and the rest of Homebrew's
  formula path helpers return :class:`Pathname` objects for the (possibly not
  yet installed) keg; ``etc``/``var`` live under ``HOMEBREW_PREFIX``;
* ``name``/``token``/``version``/``full_name``/``pkg_version``/``revision``
  describe the formula (or cask for ``run_cask_caveats``);
* ``build.head?``/``build.stable?``/``build.with?`` expose the installed build
  metadata (``PYBREW_BUILD_OPTIONS`` supplies used options when available);
* ``on_macos``/``on_linux``/``on_arm``/``on_intel`` yield only when the host
  (or ``PYBREW_TARGET_OS``/``PYBREW_TARGET_ARCH``) matches, and ``OS``/
  ``Hardware``/``MacOS`` expose bounded, local-only platform helpers;
* cask caveats get ``language``/``languages``/``language_groups`` and
  ``caskroom_path``/``staged_path``/``appdir`` in addition to formula paths;
* ``ohai``/``opoo``/``puts`` write to stdout, which is captured and returned;
* ``ENV`` and a small ``Utils``/``Utils::Shell`` module cover the common
  helpers.

Nothing here needs the formula to be installed.  Failures raise
:class:`CaveatsError` carrying the Ruby stdout/stderr so the CLI can show a
useful message.  The interpreter defaults to ``/usr/bin/ruby`` and can be
overridden with ``PYBREW_RUBY``.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from .cellar import Cellar, read_receipt
from .errors import PybrewError
from .models import Formula
from .paths import Config

#: Environment variable overriding the Ruby interpreter used for caveats.
RUBY_ENV = "PYBREW_RUBY"

#: System Ruby on macOS (also present on most Linux distributions).
DEFAULT_RUBY = "/usr/bin/ruby"

#: Upper bound for rendering one formula's caveats.
DEFAULT_TIMEOUT = 60.0


class CaveatsError(PybrewError):
    """A formula's caveats block failed or could not be run."""

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


# The Ruby compatibility shim.  It must stay Ruby 2.6 compatible because that
# is the interpreter shipped by macOS (and therefore the most likely one).
_RUBY_RUNNER = r'''
# frozen_string_literal: true

require "json"
require "open3"
require "pathname"
require "shellwords"

class CaveatsFailure < StandardError
end

class PybrewVersion < String
  def major
    to_s.split(".")[0].to_i
  end

  def minor
    version_parts[1].to_i
  end

  def patch
    version_parts[2].to_i
  end

  def major_minor
    version_parts[0, 2].join(".")
  end

  private

  def version_parts
    to_s.split(".")
  end
end

# Homebrew's MacOS::Version compares against release symbols
# (``MacOS.version >= :sonoma``); this is a bounded String-compatible form.
class PybrewMacOSVersion < String
  SYMBOLS = {
    "cheetah" => "10.0", "puma" => "10.1", "jaguar" => "10.2",
    "panther" => "10.3", "tiger" => "10.4", "leopard" => "10.5",
    "snow_leopard" => "10.6", "lion" => "10.7", "mountain_lion" => "10.8",
    "mavericks" => "10.9", "yosemite" => "10.10", "el_capitan" => "10.11",
    "sierra" => "10.12", "high_sierra" => "10.13", "mojave" => "10.14",
    "catalina" => "10.15", "big_sur" => "11", "monterey" => "12",
    "ventura" => "13", "sonoma" => "14", "sequoia" => "15", "tahoe" => "26"
  }.freeze

  def self.coerce(value)
    return value if value.is_a?(PybrewMacOSVersion)

    text = value.is_a?(Symbol) ? (SYMBOLS[value.to_s] || value.to_s) : value.to_s
    new(text)
  end

  def version_segments
    to_s.split(".").map(&:to_i)
  end

  def to_sym
    segments = version_segments
    major = segments[0].to_i
    minor = segments[1].to_i
    if major == 10
      {
        15 => :catalina, 14 => :mojave, 13 => :high_sierra,
        12 => :sierra, 11 => :el_capitan, 10 => :yosemite,
        9 => :mavericks, 8 => :mountain_lion, 7 => :lion,
        6 => :snow_leopard, 5 => :leopard, 4 => :tiger
      }.fetch(minor, to_s.tr(".", "_").to_sym)
    else
      {
        26 => :tahoe, 15 => :sequoia, 14 => :sonoma, 13 => :ventura,
        12 => :monterey, 11 => :big_sur
      }.fetch(major, to_s.tr(".", "_").to_sym)
    end
  end

  def <=>(other)
    version_segments <=> self.class.coerce(other).version_segments
  end
end

module PybrewSystem
  class << self
    def os
      (PYBREW_CONFIG["os"] || "").to_s
    end

    def arch
      (PYBREW_CONFIG["arch"] || "").to_s
    end

    def os_version
      (PYBREW_CONFIG["os_version"] || "").to_s
    end

    def macos_version
      (PYBREW_CONFIG["macos_version"] || "").to_s
    end

    def cpu_cores
      (PYBREW_CONFIG["cpu_cores"] || 1).to_i
    end

    def cpu_bits
      (PYBREW_CONFIG["cpu_bits"] || 64).to_i
    end
  end
end

module OS
  class << self
    def mac?
      PybrewSystem.os == "darwin"
    end

    def linux?
      PybrewSystem.os == "linux"
    end

    def version
      if mac?
        PybrewMacOSVersion.coerce(PybrewSystem.macos_version)
      else
        PybrewSystem.os_version
      end
    end
  end
end

module MacOS
  class << self
    def version
      PybrewMacOSVersion.coerce(PybrewSystem.macos_version)
    end

    def full_version
      version.to_s
    end

    def version_symbol
      version.to_sym
    end

    def sdk_version
      version
    end

    def sdk_path
      value = (
        PYBREW_CONFIG["macos_sdk_path"] || ENV["SDKROOT"] || ENV["HOMEBREW_SDKROOT"]
      ).to_s
      value.empty? ? nil : Pathname.new(value)
    end

    alias latest_sdk_version version
  end
end

module Hardware
  module CPU
    class << self
      def arch
        PybrewSystem.arch
      end

      def arm?
        %w[arm64 aarch64].include?(arch)
      end

      def intel?
        %w[x86_64 amd64 i386 i686].include?(arch)
      end

      def is_64_bit?
        bits == 64
      end

      def bits
        PybrewSystem.cpu_bits
      end

      def cores
        PybrewSystem.cpu_cores
      end

      def physical_cores
        cores
      end

      def features
        []
      end

      def flags
        []
      end
    end
  end
end

# The handful of Utils helpers that turn up in caveats.  Commands are run
# without a shell unless Ruby itself chooses one for a single command string.
module Utils
  class << self
    def popen_read(*args, **options)
      stdout, stderr, status = Open3.capture3(*command(args), **options)
      unless status.success?
        raise CaveatsFailure,
              "command failed with status #{status.exitstatus}: #{stderr.strip}"
      end

      stdout
    end

    alias safe_popen_read popen_read

    def pipe_output(command, input = nil, **options)
      stdout, _stderr, _status = Open3.capture3(
        command.to_s, { stdin_data: input.to_s }, **options
      )
      stdout
    end

    def quiet_system(*args, **options)
      system(*command(args), **options, out: File::NULL, err: File::NULL)
    end

    def safe_system(*args, **options)
      success = system(*command(args), **options)
      raise CaveatsFailure, "command failed: #{args.inspect}" unless success

      true
    end

    def which(name)
      command = name.to_s
      ENV.fetch("PATH", "").split(File::PATH_SEPARATOR).each do |directory|
        candidate = File.join(directory, command)
        return candidate if File.file?(candidate) && File.executable?(candidate)
      end
      nil
    end

    def safe_filename?(basename)
      name = basename.to_s
      !name.empty? && name != "." && name != ".." &&
        !name.include?("/") && !name.include?("\0")
    end

    def name_from_full_name(full_name)
      full_name.to_s.split("/").last
    end

    def deconstantize(path)
      value = path.to_s
      index = value.rindex("::")
      index.nil? ? "" : value[0, index]
    end

    def demodulize(path)
      path.to_s.split("::").last
    end

    def underscore(camel_cased_word)
      word = camel_cased_word.to_s.gsub("::", "/")
      word = word.gsub(/([A-Z\d]+)([A-Z][a-z])/, '\1_\2')
      word = word.gsub(/([a-z\d])([A-Z])/, '\1_\2')
      word = word.tr("-", "_")
      word.downcase
    end

    def pluralize(stem, count, plural: "s", singular: "", include_count: false)
      word = count.to_i == 1 ? "#{stem}#{singular}" : "#{stem}#{plural}"
      include_count ? "#{count} #{word}" : word
    end

    private

    def command(args)
      args.map { |arg| arg.is_a?(Hash) ? arg : arg.to_s }
    end
  end

  # Utils::Shell helpers commonly used by caveats to print setup instructions.
  module Shell
    module_function

    def sh_quote(value)
      Shellwords.escape(value.to_s).gsub("\\~", "~")
    end

    def export_value(key, value)
      "#{key}=\"#{value}\""
    end

    def prepend_path_in_profile(path)
      "echo '#{export_value("PATH", "#{path}:$PATH")}' >> ~/.profile"
    end

    def append_path_in_profile(path)
      "echo '#{export_value("PATH", "$PATH:#{path}")}' >> ~/.profile"
    end
  end
end

# Bounded stand-in for Homebrew's BuildOptions/Tab, as used by caveats that
# branch on ``build.with?``/``build.head?``.
class PybrewBuildOptions
  def initialize(data)
    @data = data || {}
  end

  def used_options
    Array(@data["used_options"]).map(&:to_s)
  end

  def unused_options
    []
  end

  def head?
    !!@data["head"]
  end

  def stable?
    !head?
  end

  def bottle?
    !!@data["bottle"]
  end

  def built_as_bottle?
    !!@data["built_as_bottle"]
  end

  def any_args_or_options?
    !used_options.empty?
  end

  def include?(name)
    used_options.include?(name.to_s)
  end

  def with?(name)
    include?(name)
  end

  def without?(name)
    !include?(name)
  end

  def spec
    head? ? :head : :stable
  end
end

class PybrewCaveatsContext
  PATH_NAMES = %w[
    prefix bin sbin lib include share etc libexec opt_prefix opt_bin
    opt_libexec opt_sbin opt_lib opt_include opt_share opt_pkgshare pkgshare
    var man man1 info doc appdir bash_completion zsh_completion fish_completion
  ].freeze

  def initialize(config)
    @config = config
    @paths = {}
    (config["paths"] || {}).each do |name, value|
      @paths[name.to_s] = Pathname.new(value.to_s)
    end
    @name = (config["name"] || "").to_s
    @version = PybrewVersion.new((config["version"] || "HEAD").to_s)
    @build = PybrewBuildOptions.new(config["build"])
  end

  PATH_NAMES.each do |method_name|
    define_method(method_name) do
      @paths[method_name] || Pathname.new(File.join(@paths["prefix"].to_s, method_name))
    end
  end

  def name
    @name
  end

  def token
    (@config["token"] || @name).to_s
  end

  def full_name
    (@config["full_name"] || @name).to_s
  end

  def pkg_version
    (@config["pkg_version"] || @version.to_s).to_s
  end

  def revision
    (@config["revision"] || 0).to_i
  end

  def rack
    (@config["rack"] || @name).to_s
  end

  def keg_only?
    !!@config["keg_only"]
  end

  def installed?
    !!@config["installed"]
  end

  def any_version_installed?
    installed?
  end

  def build
    @build
  end

  def language
    (@config["language"] || "en").to_s
  end

  def languages
    Array(@config["languages"] || [language]).map(&:to_s)
  end

  def language_groups
    @config["language_groups"] || {}
  end

  def default_language_group
    (@config["default_language_group"] || "en").to_s
  end

  def caskroom_path
    value = @config["caskroom_path"]
    value.nil? || value.to_s.empty? ? nil : Pathname.new(value.to_s)
  end

  def staged_path
    value = @config["staged_path"]
    value.nil? || value.to_s.empty? ? nil : Pathname.new(value.to_s)
  end

  def version
    @version
  end

  def formula
    self
  end

  # Accept a captured `caveats do ... end` declaration in addition to the
  # canonical `def caveats ... end` form: store the block's return value.
  def caveats(&block)
    return @caveats_result if block.nil?

    @caveats_result = block.call
  end

  def ohai(*messages)
    messages.each { |message| $stdout.puts "==> #{message}" }
    nil
  end

  def opoo(*messages)
    messages.each { |message| $stdout.puts "Warning: #{message}" }
    nil
  end

  def puts(*messages)
    Kernel.puts(*messages)
    nil
  end

  def on_macos(&block)
    return nil unless OS.mac?
    return nil if block.nil?

    block.call
  end

  def on_linux(&block)
    return nil unless OS.linux?
    return nil if block.nil?

    block.call
  end

  def on_arm(&block)
    return nil unless Hardware::CPU.arm?
    return nil if block.nil?

    block.call
  end

  def on_intel(&block)
    return nil unless Hardware::CPU.intel?
    return nil if block.nil?

    block.call
  end

  alias on_arm64 on_arm
  alias on_x86_64 on_intel
end

def pybrew_caveats_method?(source)
  !(source =~ /^\s*def\s+(?:self\.)?caveats\b/).nil?
end

begin
  PYBREW_CONFIG = JSON.parse(File.read(ARGV[0])).freeze
  HOMEBREW_PREFIX = (
    PYBREW_CONFIG["homebrew_prefix"] || ENV["HOMEBREW_PREFIX"] || ""
  ).to_s
  HOMEBREW_CELLAR = (
    PYBREW_CONFIG["cellar"] || ENV["HOMEBREW_CELLAR"] || ""
  ).to_s
  HOMEBREW_REPOSITORY = (
    PYBREW_CONFIG["repository"] || ENV["HOMEBREW_REPOSITORY"] || HOMEBREW_PREFIX
  ).to_s
  HOMEBREW_LIBRARY = File.join(HOMEBREW_PREFIX, "Library")

  source = File.read(ARGV[1])
  context = PybrewCaveatsContext.new(PYBREW_CONFIG)
  result = context.instance_eval(source, "(caveats)", 1)
  if pybrew_caveats_method?(source) && context.respond_to?(:caveats)
    result = context.caveats
  end
  $stdout.puts(result) unless result.nil? || result.to_s.empty?
rescue SystemExit
  raise
rescue Exception => error
  $stderr.puts "Caveats failed: #{error.class}: #{error.message}"
  error.backtrace.to_a.first(12).each { |line| $stderr.puts "  #{line}" }
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
        raise CaveatsError(
            f"Ruby interpreter not found: {candidate!r} "
            f"(set {RUBY_ENV} to an executable Ruby)"
        )
    return resolved


def _target_os() -> str:
    """Return ``darwin``/``linux`` for the host or a test override."""

    target = (os.environ.get("PYBREW_TARGET_OS") or sys.platform).lower()
    if target == "darwin":
        return "darwin"
    if target.startswith("linux"):
        return "linux"
    return target


def _target_arch() -> str:
    return (os.environ.get("PYBREW_TARGET_ARCH") or platform.machine()).lower()


def _keg_path(formula: Formula, config: Config) -> Path:
    """Prefer an installed keg; otherwise point at the keg install would use."""

    installed = Cellar(config).keg(formula.name)
    if installed is not None:
        return installed
    return config.keg_path(formula.name, formula.pkg_version)


def _target_macos_version() -> str:
    """Return the macOS version used by ``MacOS.version`` (host or override)."""

    override = os.environ.get("PYBREW_MACOS_VERSION", "").strip()
    if override:
        return override
    if _target_os() == "darwin":
        return platform.mac_ver()[0] or ""
    return ""


def _target_os_version() -> str:
    """Return ``OS.version`` for the simulated/host platform."""

    if _target_os() == "darwin":
        return _target_macos_version()
    return platform.release()


def _installed_keg(formula: Formula, config: Config) -> Optional[Path]:
    try:
        return Cellar(config).keg(formula.name)
    except OSError:
        return None


def _build_payload(formula: Formula, keg: Optional[Path]) -> dict[str, Any]:
    """Return the bounded ``build`` metadata used by caveats shims.

    ``build.head?``/``build.stable?``/``build.bottle?`` come from the install
    receipt (when one exists) and ``PYBREW_BUILD_OPTIONS`` supplies used
    options because pybrew does not persist selected formula options yet.
    """

    receipt = read_receipt(keg) if keg is not None else {}
    build = receipt.get("build") if isinstance(receipt.get("build"), dict) else {}
    declared_spec = str(build.get("spec") or "")
    head = declared_spec == "head" or (formula.version or "").upper() == "HEAD"
    used_options = [
        item
        for item in re.split(r"[,\s]+", os.environ.get("PYBREW_BUILD_OPTIONS", ""))
        if item
    ]
    return {
        "spec": "head" if head else "stable",
        "head": head,
        "used_options": used_options,
        "bottle": bool(
            receipt.get("poured_from_bottle", False)
            or build.get("from_bottle", False)
        ),
        "built_as_bottle": bool(receipt.get("built_as_bottle", False)),
    }


def _formula_context(formula: Formula, config: Config) -> dict[str, Any]:
    """Return the formula metadata exposed to the caveats shim."""

    return {
        "full_name": formula.full_name,
        "pkg_version": formula.pkg_version,
        "revision": int(getattr(formula, "revision", 0) or 0),
        "rack": getattr(formula, "rack", formula.name) or formula.name,
        "keg_only": bool(getattr(formula, "keg_only", False)),
        "tap": formula.tap or "",
        "installed": _installed_keg(formula, config) is not None,
    }


def _caveats_paths(formula: Formula, config: Config) -> dict[str, str]:
    keg = _keg_path(formula, config)
    opt = config.opt_path(formula.name)
    mapping = {
        "prefix": keg,
        "bin": keg / "bin",
        "sbin": keg / "sbin",
        "lib": keg / "lib",
        "include": keg / "include",
        "share": keg / "share",
        # Formula#etc and Formula#var live under HOMEBREW_PREFIX in Homebrew.
        "etc": Path(config.prefix) / "etc",
        "libexec": keg / "libexec",
        "opt_prefix": opt,
        "opt_bin": opt / "bin",
        "opt_libexec": opt / "libexec",
        "opt_sbin": opt / "sbin",
        "opt_lib": opt / "lib",
        "opt_include": opt / "include",
        "opt_share": opt / "share",
        "pkgshare": keg / "share" / formula.name,
        "opt_pkgshare": opt / "share" / formula.name,
        "var": config.var,
        "man": keg / "share" / "man",
        "man1": keg / "share" / "man" / "man1",
        "info": keg / "share" / "info",
        "doc": keg / "share" / "doc" / formula.name,
        "appdir": config.applications,
        "bash_completion": keg / "etc" / "bash_completion.d",
        "zsh_completion": keg / "share" / "zsh" / "site-functions",
        "fish_completion": keg / "share" / "fish" / "vendor_completions.d",
    }
    return {name: str(path) for name, path in mapping.items()}


def _caveats_environment(
    formula: Formula,
    config: Config,
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    env = dict(os.environ)
    # Make the formula's own prefix authoritative even when the user happens
    # to have a real Homebrew environment exported in their shell.
    env["HOMEBREW_PREFIX"] = str(config.prefix)
    env["HOMEBREW_CELLAR"] = str(config.cellar)
    env["HOMEBREW_REPOSITORY"] = str(config.prefix)
    env["HOMEBREW_LIBRARY"] = str(config.prefix / "Library")
    env["PYBREW_PREFIX"] = str(config.prefix)
    env["PYBREW_CELLAR"] = str(config.cellar)
    env["PYBREW_FORMULA_NAME"] = formula.name
    env["PYBREW_FORMULA_VERSION"] = formula.version or "HEAD"
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def has_caveats(formula: Formula) -> bool:
    """Return true when the formula captured a non-empty caveats block."""

    return bool((formula.caveats or "").strip())


def run_caveats(
    formula: Formula,
    config: Config,
    *,
    ruby: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
    extra_config: Optional[Mapping[str, Any]] = None,
) -> str:
    """Render ``formula.caveats`` and return the captured Ruby stdout.

    The formula does not need to be installed.  A missing caveats block simply
    renders as an empty string.  Failures, timeouts and a missing Ruby
    interpreter raise :class:`CaveatsError`.  ``extra_config`` adds bounded
    context values for callers such as cask caveats (``language``,
    ``caskroom_path``, ...); a ``paths`` mapping is merged into the formula
    paths instead of replacing them.
    """

    source = formula.caveats or ""
    if not source.strip():
        return ""

    ruby_path = _ruby_binary(ruby)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT

    keg = _installed_keg(formula, config)
    root = Path(tempfile.mkdtemp(prefix="pybrew-caveats-"))
    try:
        runner = root / "caveats_runner.rb"
        runner.write_text(_RUBY_RUNNER, encoding="utf-8")
        body = root / "caveats_body.rb"
        body.write_text(source, encoding="utf-8")
        config_path = root / "config.json"
        payload: dict[str, Any] = {
            "name": formula.name,
            "version": formula.version or "HEAD",
            "os": _target_os(),
            "arch": _target_arch(),
            "os_version": _target_os_version(),
            "macos_version": _target_macos_version(),
            "cpu_cores": os.cpu_count() or 1,
            "cpu_bits": struct.calcsize("P") * 8,
            "paths": _caveats_paths(formula, config),
            "homebrew_prefix": str(config.prefix),
            "cellar": str(config.cellar),
            "repository": str(config.prefix),
            "build": _build_payload(formula, keg),
        }
        payload.update(_formula_context(formula, config))
        if extra_config:
            for key, value in extra_config.items():
                if key == "paths" and isinstance(value, Mapping):
                    payload["paths"].update(
                        {str(name): str(path) for name, path in value.items()}
                    )
                else:
                    payload[str(key)] = value
        config_path.write_text(json.dumps(payload), encoding="utf-8")

        command = [ruby_path, str(runner), str(config_path), str(body)]
        run_env = _caveats_environment(formula, config, extra=env)
        try:
            completed = subprocess.run(
                command,
                cwd=str(root),
                env=run_env,
                stdin=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise CaveatsError(
                f"caveats for {formula.name!r} timed out after {timeout:g}s",
                stdout=_as_text(error.stdout),
                stderr=_as_text(error.stderr),
                command=command,
            ) from error
        except OSError as error:
            raise CaveatsError(
                f"could not run Ruby for caveats of {formula.name!r}: {error}",
                command=command,
            ) from error

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode != 0:
            details = (stderr.strip() or stdout.strip()).replace(
                str(runner), "<caveats-shim>"
            )
            message = (
                f"caveats for {formula.name!r} failed "
                f"(exit status {completed.returncode})"
            )
            if details:
                message += f":\n{details}"
            raise CaveatsError(
                message,
                stdout=stdout,
                stderr=stderr,
                returncode=completed.returncode,
                command=command,
            )
        return stdout
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_cask_caveats(
    cask: Any,
    config: Config,
    *,
    ruby: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Render a cask's caveats block (or literal caveats string).

    Cask caveats are ordinary Ruby too; the parser stores a captured block in
    ``caveats_source`` and a literal string in ``caveats``.  Reuse the formula
    runner with a lightweight Formula carrying the cask token/version plus the
    cask context (``token``, ``language``/``languages``, ``appdir``,
    ``caskroom_path``/``staged_path``).
    """

    source = (getattr(cask, "caveats_source", None) or "").strip()
    if source:
        token = str(getattr(cask, "token", "") or "cask")
        version = getattr(cask, "version", None)
        pseudo = Formula(name=token, version=version, caveats=source)
        extra_config: dict[str, Any] = {
            "token": token,
            "language": getattr(cask, "language", None) or "en",
            "languages": _cask_languages(cask),
            "language_groups": _safe_call(cask, "language_groups", {}),
            "default_language_group": _safe_call(
                cask, "default_language_group", "en"
            ),
            "caskroom_path": _string_attr(cask, "caskroom_path"),
            "staged_path": _string_attr(cask, "staged_path"),
        }
        appdir = getattr(cask, "appdir", None)
        if appdir:
            extra_config["paths"] = {"appdir": str(Path(str(appdir)).expanduser())}
        return run_caveats(
            pseudo,
            config,
            ruby=ruby,
            timeout=timeout,
            env=env,
            extra_config=extra_config,
        )
    literal = getattr(cask, "caveats", None)
    return str(literal) if literal else ""


def _safe_call(obj: Any, name: str, default: Any) -> Any:
    value = getattr(obj, name, None)
    if callable(value):
        try:
            return value()
        except (OSError, TypeError, ValueError):
            return default
    return value if value is not None else default


def _cask_languages(cask: Any) -> list[str]:
    value = _safe_call(cask, "languages", None)
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    language = getattr(cask, "language", None)
    return [str(language)] if language else ["en"]


def _string_attr(obj: Any, name: str) -> str:
    value = getattr(obj, name, None)
    if value is None:
        return ""
    if callable(value):
        try:
            value = value()
        except (OSError, TypeError, ValueError):
            return ""
    return str(value)


def render_caveats(
    formula: Formula,
    config: Config,
    *,
    ruby: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Alias for :func:`run_caveats`."""

    return run_caveats(formula, config, ruby=ruby, timeout=timeout, env=env)


class CaveatsRunner:
    """Small stateful wrapper around :func:`run_caveats`."""

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
    ) -> str:
        return run_caveats(
            formula,
            self.config,
            ruby=self.ruby,
            timeout=self.timeout,
            env=env,
        )
