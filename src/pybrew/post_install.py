"""Run a formula's captured ``post_install`` block with the host Ruby.

Homebrew formulae can define a ``post_install`` method that runs after the keg
has been installed (and linked) to create runtime directories, initialise
databases, or register services.  The pybrew parser captures the raw Ruby
source instead of evaluating it, so this module executes that source with a
deliberately small compatibility shim:

* ``prefix``/``bin``/``sbin``/``lib``/``include``/``share``/``etc`` and the
  ``opt_*`` variants return :class:`Pathname` objects for the installed keg
  (``etc``/``var`` live under ``HOMEBREW_PREFIX``, as in Homebrew);
* ``Formula["name"]`` resolves another installed formula to the same path
  objects (unknown formulae fall back to ``<prefix>/opt/<name>``; a
  tap-qualified name falls back to its bare name);
* ``build`` exposes ``head?``/``stable?``/``bottle?``/``with?``/``without?``
  from the install receipt plus ``PYBREW_BUILD_OPTIONS``;
* ``full_name``/``pkg_version``/``revision``/``rack``/``keg_only?`` and
  ``installed?`` describe the formula;
* ``system`` runs a command and raises on a non-zero exit status, like
  Homebrew's formula DSL;
* ``Utils``, ``FileUtils`` (included into the context), ``Pathname``, ``ENV``,
  ``ohai``/``opoo``/``puts`` and the Homebrew prefix constants are available;
* the working directory is the keg itself and the environment is
  Homebrew-shaped (``HOMEBREW_PREFIX``, ``HOMEBREW_CELLAR``, ...) with an
  isolated ``HOME``/``TMPDIR``/``HOMEBREW_TEMP`` and no ``HOMEBREW_PATH``.

Because this executes arbitrary Ruby from a formula, it is only invoked when
the formula actually captured a non-empty ``post_install`` source.  Failures
raise :class:`PostInstallError` carrying the Ruby stdout/stderr so the caller
can abort the install with a useful message.  The interpreter defaults to
``/usr/bin/ruby`` and can be overridden with ``PYBREW_RUBY``.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .cellar import Cellar, read_receipt
from .errors import BuildError
from .models import Formula
from .paths import Config

#: Environment variable overriding the Ruby interpreter used for post_install.
RUBY_ENV = "PYBREW_RUBY"

#: System Ruby on macOS (also present on most Linux distributions).
DEFAULT_RUBY = "/usr/bin/ruby"

#: Upper bound for running one formula's post_install block.
DEFAULT_TIMEOUT = 300.0

#: Path helpers exposed by the Ruby context (and by ``Formula[...]`` objects).
PATH_NAMES = (
    "prefix",
    "bin",
    "sbin",
    "lib",
    "include",
    "share",
    "etc",
    "libexec",
    "opt_prefix",
    "opt_bin",
    "opt_libexec",
    "opt_sbin",
    "opt_lib",
    "opt_include",
    "opt_share",
    "pkgshare",
    "opt_pkgshare",
    "var",
    "man",
    "man1",
    "info",
    "doc",
    "appdir",
    "bash_completion",
    "zsh_completion",
    "fish_completion",
)


class PostInstallError(BuildError):
    """A formula's ``post_install`` block failed or could not be run."""

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


@dataclass
class PostInstallResult:
    """Outcome of a ``post_install`` invocation."""

    ran: bool = False
    stdout: str = ""
    stderr: str = ""
    returncode: Optional[int] = None
    command: list[str] = field(default_factory=list)
    keg: Optional[Path] = None


# The Ruby compatibility shim.  It must stay Ruby 2.6 compatible because that
# is the interpreter shipped by macOS (and therefore the most likely one).
_RUBY_RUNNER = r'''
# frozen_string_literal: true

require "json"
require "open3"
require "pathname"
require "fileutils"
require "shellwords"

class PostInstallFailure < StandardError
end

# String-compatible macOS version so `MacOS.version >= :sonoma` works.
class PybrewMacOSVersion < String
  def self.coerce(value)
    return value if value.is_a?(PybrewMacOSVersion)

    text = value.is_a?(Symbol) ? (SYMBOL_VERSIONS[value.to_s] || value.to_s) : value.to_s
    new(text)
  end

  SYMBOL_VERSIONS = {
    "cheetah" => "10.0", "puma" => "10.1", "jaguar" => "10.2",
    "panther" => "10.3", "tiger" => "10.4", "leopard" => "10.5",
    "snow_leopard" => "10.6", "lion" => "10.7", "mountain_lion" => "10.8",
    "mavericks" => "10.9", "yosemite" => "10.10", "el_capitan" => "10.11",
    "sierra" => "10.12", "high_sierra" => "10.13", "mojave" => "10.14",
    "catalina" => "10.15", "big_sur" => "11", "monterey" => "12",
    "ventura" => "13", "sonoma" => "14", "sequoia" => "15", "tahoe" => "26"
  }.freeze

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
        6 => :snow_leopard
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

    alias full_version version

    def version_symbol
      version.to_sym
    end
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
    end
  end
end

module Utils
  class << self
    def safe_system(*args, **options)
      success = Kernel.system(*command(args), **options)
      raise PostInstallFailure, "command failed: #{args.inspect}" unless success

      true
    end

    def quiet_system(*args, **options)
      Kernel.system(*command(args), **options, out: File::NULL, err: File::NULL)
    end

    def popen_read(*args, **options)
      stdout, stderr, status = Open3.capture3(*command(args), **options)
      unless status.success?
        raise PostInstallFailure,
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

    private

    def command(args)
      args.map { |arg| arg.is_a?(Hash) ? arg : arg.to_s }
    end
  end
end

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

class PybrewFormulaRef
  PATH_NAMES = %w[
    prefix bin sbin lib include share etc libexec opt_prefix opt_bin
    opt_libexec opt_sbin opt_lib opt_include opt_share opt_pkgshare pkgshare
    var man man1 info doc appdir bash_completion zsh_completion fish_completion
  ].freeze

  def initialize(name, payload = nil)
    @name = name.to_s
    @payload = payload || {}
    @build = PybrewBuildOptions.new(@payload["build"])
  end

  def name
    @name
  end

  def full_name
    (@payload["full_name"] || @name).to_s
  end

  def pkg_version
    (@payload["pkg_version"] || @payload["version"] || "").to_s
  end

  def revision
    (@payload["revision"] || 0).to_i
  end

  def rack
    (@payload["rack"] || @name).to_s
  end

  def keg_only?
    !!@payload["keg_only"]
  end

  def installed?
    !!@payload["installed"]
  end

  def build
    @build
  end

  def to_s
    @name
  end

  PATH_NAMES.each do |method_name|
    define_method(method_name) do
      value = @payload[method_name]
      next Pathname.new(value.to_s) if value

      opt = File.join(HOMEBREW_PREFIX, "opt", @name)
      case method_name
      when "prefix", "opt_prefix"
        Pathname.new(opt)
      when "opt_bin"
        Pathname.new(File.join(opt, "bin"))
      when "opt_libexec"
        Pathname.new(File.join(opt, "libexec"))
      when "opt_sbin"
        Pathname.new(File.join(opt, "sbin"))
      when "opt_lib"
        Pathname.new(File.join(opt, "lib"))
      when "opt_include"
        Pathname.new(File.join(opt, "include"))
      when "opt_share"
        Pathname.new(File.join(opt, "share"))
      when "pkgshare"
        Pathname.new(File.join(opt, "share", @name))
      when "opt_pkgshare"
        Pathname.new(File.join(opt, "share", @name))
      when "etc"
        Pathname.new(File.join(HOMEBREW_PREFIX, "etc"))
      when "var"
        Pathname.new(File.join(HOMEBREW_PREFIX, "var"))
      when "man1"
        Pathname.new(File.join(opt, "share/man/man1"))
      when "doc"
        Pathname.new(File.join(opt, "share/doc", @name))
      when "appdir"
        Pathname.new(File.join(HOMEBREW_PREFIX, "Applications"))
      when "bash_completion"
        Pathname.new(File.join(opt, "etc/bash_completion.d"))
      when "zsh_completion"
        Pathname.new(File.join(opt, "share/zsh/site-functions"))
      when "fish_completion"
        Pathname.new(File.join(opt, "share/fish/vendor_completions.d"))
      else
        Pathname.new(File.join(opt, method_name))
      end
    end
  end
end

class PybrewFormula
  class << self
    def [](name)
      key = name.to_s
      payload = (PYBREW_CONFIG["formulas"] || {})[key]
      bare = key
      if payload.nil? && key.include?("/")
        bare = key.split("/").last
        payload = (PYBREW_CONFIG["formulas"] || {})[bare]
      end
      PybrewFormulaRef.new(bare, payload)
    end
  end
end

Formula = PybrewFormula

class PybrewPostInstallContext
  include FileUtils

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
    @version = (config["version"] || "HEAD").to_s
    @build = PybrewBuildOptions.new(config["build"])
  end

  PATH_NAMES.each do |method_name|
    define_method(method_name) do
      @paths[method_name] ||
        Pathname.new(File.join(@paths["prefix"].to_s, method_name))
    end
  end

  def name
    @name
  end

  def version
    @version
  end

  def full_name
    (@config["full_name"] || @name).to_s
  end

  def pkg_version
    (@config["pkg_version"] || @version).to_s
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

  def build
    @build
  end

  def formula
    self
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

  # Homebrew's formula DSL raises when a command fails.  Kernel#system alone
  # would only return false and let a broken post_install silently "succeed".
  def system(*args, **options)
    success = Kernel.system(*args, **options)
    raise PostInstallFailure, "command failed: #{args.inspect}" unless success

    true
  end

  # Accept both `post_install do ... end` raw sources and the canonical
  # `def post_install ... end` form captured by the parser.
  def post_install(&block)
    block.call if block
    nil
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
end

def pybrew_post_install_method?(source)
  !(source =~ /^\s*def\s+(?:self\.)?post_install\b/).nil?
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
  context = PybrewPostInstallContext.new(PYBREW_CONFIG)
  if pybrew_post_install_method?(source)
    context.instance_eval(source, "(post_install)", 1)
    context.post_install
  elsif source.lstrip.start_with?("do") || source.lstrip.start_with?("{")
    context.instance_eval("post_install #{source}", "(post_install)", 1)
  else
    # A bare body (for example a synthetic formula payload) is evaluated
    # directly; there is no method to define.
    context.instance_eval(source, "(post_install)", 1)
  end
rescue SystemExit
  raise
rescue Exception => error
  $stderr.puts "Post-install failed: #{error.class}: #{error.message}"
  error.backtrace.to_a.first(12).each { |line| $stderr.puts "  #{line}" }
  exit 1
end
'''


def has_post_install(formula: Formula) -> bool:
    """Return true when the formula captured a non-empty post_install block."""

    return bool((formula.post_install_source or "").strip())


def _ruby_binary(ruby: Optional[str] = None) -> str:
    candidate = os.path.expanduser(
        os.fspath(ruby or os.environ.get(RUBY_ENV) or DEFAULT_RUBY)
    )
    resolved = shutil.which(candidate)
    if resolved is None and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        resolved = candidate
    if resolved is None:
        raise PostInstallError(
            f"Ruby interpreter not found: {candidate!r} "
            f"(set {RUBY_ENV} to an executable Ruby)"
        )
    return resolved


def _target_os() -> str:
    target = (os.environ.get("PYBREW_TARGET_OS") or sys.platform).lower()
    if target == "darwin":
        return "darwin"
    if target.startswith("linux"):
        return "linux"
    return target


def _target_arch() -> str:
    return (os.environ.get("PYBREW_TARGET_ARCH") or platform.machine()).lower()


def _target_macos_version() -> str:
    override = os.environ.get("PYBREW_MACOS_VERSION", "").strip()
    if override:
        return override
    if _target_os() == "darwin":
        return platform.mac_ver()[0] or ""
    return ""


def _target_os_version() -> str:
    if _target_os() == "darwin":
        return _target_macos_version()
    return platform.release()


def _keg_path(formula: Formula, config: Config) -> Path:
    """Prefer an installed keg; otherwise point at the keg install would use."""

    installed = Cellar(config).keg(formula.name)
    if installed is not None:
        return installed
    return config.keg_path(formula.name, formula.pkg_version)


def _paths_for(name: str, keg: Path, config: Config) -> dict[str, str]:
    opt = config.opt_path(name)
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
        "pkgshare": keg / "share" / name,
        "opt_pkgshare": opt / "share" / name,
        "var": config.var,
        "man": keg / "share" / "man",
        "man1": keg / "share" / "man" / "man1",
        "info": keg / "share" / "info",
        "doc": keg / "share" / "doc" / name,
        "appdir": config.applications,
        "bash_completion": keg / "etc" / "bash_completion.d",
        "zsh_completion": keg / "share" / "zsh" / "site-functions",
        "fish_completion": keg / "share" / "fish" / "vendor_completions.d",
    }
    return {key: str(value) for key, value in mapping.items()}


def _build_payload_for_keg(
    formula: Formula,
    keg: Path,
    extra_env: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Return bounded ``build`` metadata for the post_install shim."""

    receipt = read_receipt(keg)
    build = receipt.get("build") if isinstance(receipt.get("build"), dict) else {}
    head = str(build.get("spec") or "") == "head" or (
        formula.version or ""
    ).upper() == "HEAD"
    raw_options = extra_env.get("PYBREW_BUILD_OPTIONS") if extra_env is not None else None
    if raw_options is None:
        raw_options = os.environ.get("PYBREW_BUILD_OPTIONS", "")
    used_options = [item for item in raw_options.replace(",", " ").split() if item]
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


def _installed_formulas(
    formula: Formula,
    config: Config,
    keg: Path,
    extra_env: Optional[Mapping[str, str]] = None,
) -> dict[str, dict[str, Any]]:
    """Path/build payloads for ``Formula["name"]`` lookups in the shim."""

    formulas: dict[str, dict[str, Any]] = {}
    try:
        installed = Cellar(config).installed()
    except OSError:
        installed = {}
    for name, versions in installed.items():
        if not versions:
            continue
        other_keg = config.keg_path(name, versions[-1])
        payload: dict[str, Any] = dict(_paths_for(name, other_keg, config))
        payload.update(
            {
                "full_name": name,
                "pkg_version": versions[-1],
                "revision": 0,
                "rack": name,
                "keg_only": False,
                "installed": True,
                "build": {
                    "spec": "stable",
                    "head": False,
                    "used_options": [],
                    "bottle": bool(
                        read_receipt(other_keg).get("poured_from_bottle", False)
                    ),
                    "built_as_bottle": bool(
                        read_receipt(other_keg).get("built_as_bottle", False)
                    ),
                },
            }
        )
        formulas[name] = payload
    # The formula being installed wins over a stale installed version.
    own = dict(_paths_for(formula.name, keg, config))
    own.update(
        {
            "full_name": formula.full_name,
            "pkg_version": formula.pkg_version,
            "revision": int(getattr(formula, "revision", 0) or 0),
            "rack": getattr(formula, "rack", formula.name) or formula.name,
            "keg_only": bool(getattr(formula, "keg_only", False)),
            "installed": True,
            "build": _build_payload_for_keg(formula, keg, extra_env),
        }
    )
    formulas[formula.name] = own
    return formulas


def _post_install_environment(
    formula: Formula,
    config: Config,
    *,
    home: Optional[Path] = None,
    temp: Optional[Path] = None,
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    env = dict(os.environ)
    # Homebrew clears HOMEBREW_PATH for post_install and runs it with an
    # isolated HOME/TMPDIR so installs are deterministic.
    env.pop("HOMEBREW_PATH", None)
    env["HOMEBREW_PREFIX"] = str(config.prefix)
    env["HOMEBREW_CELLAR"] = str(config.cellar)
    env["HOMEBREW_REPOSITORY"] = str(config.prefix)
    env["HOMEBREW_LIBRARY"] = str(config.prefix / "Library")
    env["PYBREW_PREFIX"] = str(config.prefix)
    env["PYBREW_CELLAR"] = str(config.cellar)
    env["PYBREW_FORMULA_NAME"] = formula.name
    env["PYBREW_FORMULA_VERSION"] = formula.version or "HEAD"
    env["PYBREW_FORMULA_PKG_VERSION"] = formula.pkg_version
    if home is not None:
        env["HOME"] = str(home)
    if temp is not None:
        env["TMPDIR"] = str(temp)
        env["TEMP"] = str(temp)
        env["TMP"] = str(temp)
        env["HOMEBREW_TEMP"] = str(temp)
    # Homebrew runs post_install with the prefix's bin/sbin on PATH so that
    # `system "tool"` can find binaries installed by this formula or a dep.
    path_parts = [str(config.bin), str(config.sbin)]
    existing = env.get("PATH")
    if existing:
        path_parts.append(existing)
    env["PATH"] = os.pathsep.join(path_parts)
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def run_post_install(
    formula: Formula,
    config: Config,
    keg: Optional[Path] = None,
    *,
    ruby: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
) -> PostInstallResult:
    """Execute ``formula.post_install_source`` in the installed keg.

    The block runs under a real Ruby interpreter (``/usr/bin/ruby`` by
    default, ``PYBREW_RUBY`` overrides it) with ``cwd`` set to the keg and a
    Homebrew-like environment.  A missing block is a no-op and returns a
    result with ``ran=False``.  Missing interpreters, timeouts, non-zero exits
    and unreadable kegs raise :class:`PostInstallError`.
    """

    source = formula.post_install_source or ""
    if not source.strip():
        return PostInstallResult(ran=False, keg=Path(keg) if keg is not None else None)

    keg_path = Path(keg) if keg is not None else _keg_path(formula, config)
    if not keg_path.is_dir():
        raise PostInstallError(
            f"cannot run post_install for {formula.name!r}: keg does not exist: {keg_path}"
        )

    ruby_path = _ruby_binary(ruby)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT

    root = Path(tempfile.mkdtemp(prefix="pybrew-post-install-"))
    try:
        home_dir = root / "home"
        home_dir.mkdir()
        tmp_dir = root / "tmp"
        tmp_dir.mkdir()
        (home_dir / ".bazelrc").write_text(
            f"startup --output_user_root={home_dir}/_bazel\n",
            encoding="utf-8",
        )
        runner = root / "post_install_runner.rb"
        runner.write_text(_RUBY_RUNNER, encoding="utf-8")
        body = root / "post_install_body.rb"
        body.write_text(source, encoding="utf-8")
        config_path = root / "config.json"
        payload = {
            "name": formula.name,
            "version": formula.version or "HEAD",
            "full_name": formula.full_name,
            "pkg_version": formula.pkg_version,
            "revision": int(getattr(formula, "revision", 0) or 0),
            "rack": getattr(formula, "rack", formula.name) or formula.name,
            "keg_only": bool(getattr(formula, "keg_only", False)),
            "installed": True,
            "build": _build_payload_for_keg(formula, keg_path, env),
            "os": _target_os(),
            "arch": _target_arch(),
            "os_version": _target_os_version(),
            "macos_version": _target_macos_version(),
            "cpu_cores": os.cpu_count() or 1,
            "cpu_bits": struct.calcsize("P") * 8,
            "paths": _paths_for(formula.name, keg_path, config),
            "formulas": _installed_formulas(formula, config, keg_path, env),
            "homebrew_prefix": str(config.prefix),
            "cellar": str(config.cellar),
            "repository": str(config.prefix),
        }
        config_path.write_text(json.dumps(payload), encoding="utf-8")

        command = [ruby_path, str(runner), str(config_path), str(body)]
        run_env = _post_install_environment(
            formula,
            config,
            home=home_dir,
            temp=tmp_dir,
            extra=env,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=str(keg_path),
                env=run_env,
                stdin=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise PostInstallError(
                f"post_install for {formula.name!r} timed out after {timeout:g}s",
                stdout=_as_text(error.stdout),
                stderr=_as_text(error.stderr),
                command=command,
            ) from error
        except OSError as error:
            raise PostInstallError(
                f"could not run Ruby for post_install of {formula.name!r}: {error}",
                command=command,
            ) from error

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode != 0:
            details = (stderr.strip() or stdout.strip()).replace(
                str(runner), "<post-install-shim>"
            )
            message = (
                f"post_install for {formula.name!r} failed "
                f"(exit status {completed.returncode})"
            )
            if details:
                message += f":\n{details}"
            raise PostInstallError(
                message,
                stdout=stdout,
                stderr=stderr,
                returncode=completed.returncode,
                command=command,
            )
        return PostInstallResult(
            ran=True,
            stdout=stdout,
            stderr=stderr,
            returncode=completed.returncode,
            command=command,
            keg=keg_path,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


class PostInstallRunner:
    """Small stateful wrapper around :func:`run_post_install`."""

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
        keg: Optional[Path] = None,
        *,
        env: Optional[Mapping[str, str]] = None,
    ) -> PostInstallResult:
        return run_post_install(
            formula,
            self.config,
            keg=keg,
            ruby=self.ruby,
            timeout=self.timeout,
            env=env,
        )
