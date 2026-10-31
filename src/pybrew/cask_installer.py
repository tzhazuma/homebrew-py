"""Cask discovery and installation."""

from __future__ import annotations

import glob
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .api import cask_from_api, fetch_cask
from .cask import Artifact, Cask, CaskError, parse_cask_file
from .download import Downloader, sha256_file
from .errors import ChecksumMismatch, DownloadError, PybrewError
from .installer import safe_extract_tar
from .paths import Config
from .taps import Tap

# Files installed by a system-wide pkg may only be removed below one of these
# roots.  ``PYBREW_SYSTEM_PKG_ROOTS`` (colon-separated) overrides the list so
# tests can point at a throwaway directory and never touch a real system path.
DEFAULT_SYSTEM_PKG_ROOTS = ("/usr/local", "/opt", "/Library", "/Applications")

# ``pkgutil --pkgs`` can be slow on machines with many receipts; this is a
# generous upper bound rather than a performance target.
_PKGUTIL_TIMEOUT = 120


class CaskNotFound(PybrewError):
    """No cask with the requested token exists in the configured taps."""


class CaskLoader:
    def __init__(self, taps: Iterable[Tap], allow_api: bool = False):
        self.taps = list(taps)
        self.allow_api = allow_api
        self._cache: dict[str, tuple[Tap, Path]] = {}

    def names(self) -> list[str]:
        names: set[str] = set()
        for tap in self.taps:
            cask_dir = tap.path / "Casks"
            if cask_dir.is_dir():
                for path in cask_dir.rglob("*.rb"):
                    names.add(path.stem)
        return sorted(names)

    def _locate(self, token: str) -> tuple[Tap, Path]:
        if token in self._cache:
            return self._cache[token]
        for tap in self.taps:
            cask_dir = tap.path / "Casks"
            if not cask_dir.is_dir():
                continue
            direct = cask_dir / f"{token}.rb"
            if direct.is_file():
                self._cache[token] = (tap, direct)
                return tap, direct
            for path in sorted(cask_dir.rglob(f"{token}.rb")):
                self._cache[token] = (tap, path)
                return tap, path
        raise CaskNotFound(
            f"no cask named {token!r} in taps: {[tap.name for tap in self.taps]}"
        )

    def exists(self, token: str) -> bool:
        try:
            self._locate(token)
            return True
        except CaskNotFound:
            return False

    def load(self, token: str) -> Cask:
        try:
            tap, path = self._locate(token)
        except CaskNotFound:
            if self.allow_api:
                return fetch_cask(token)
            raise
        if os.environ.get("PYBREW_ALLOW_CASK_RUBY") == "1":
            from .cask_ruby import evaluate_cask_file, merge_captured_scripts

            evaluated = evaluate_cask_file(path, tap=tap.name)
            try:
                parsed = parse_cask_file(path, tap=tap.name)
            except Exception:
                return evaluated
            return merge_captured_scripts(evaluated, parsed)
        return parse_cask_file(path, tap=tap.name)

    def search(self, pattern: str) -> list[Cask]:
        regex = re.compile(pattern, re.IGNORECASE)
        results: list[Cask] = []
        for token in self.names():
            try:
                cask = self.load(token)
            except Exception:
                continue
            if regex.search(f"{cask.token} {cask.name or ''} {cask.desc or ''}"):
                results.append(cask)
        return results


# The Ruby shim used for opt-in cask scripts.  It defines the small subset of
# the Homebrew cask script API that package scripts commonly use and evaluates
# the captured block body in that context.  The body itself is written to a
# separate file and never interpolated into this shim.
# macOS version symbols used by cask ``depends_on macos:`` requirements.
_MACOS_SYMBOL_VERSIONS = {
    "catalina": "10.15",
    "big_sur": "11",
    "monterey": "12",
    "ventura": "13",
    "sonoma": "14",
    "sequoia": "15",
    "tahoe": "26",
    "golden_gate": "27",
}

_CASK_SCRIPT_SHIM = r'''
# frozen_string_literal: true

# pybrew cask script shim.  This file is generated at install time and is only
# used when a caller explicitly opts in to cask script execution.

require "etc"
require "fileutils"
require "open3"
require "pathname"
require "rbconfig"
require "shellwords"
require "timeout"

# Shellwords, FileUtils and Pathname are available to cask script bodies (for
# example Shellwords.escape or FileUtils.rm_rf) in addition to the helpers
# defined below.

# Minimal stand-in for Homebrew's MacOS::SDK.  pybrew only reports SDKs that
# are already installed on disk (or named by PYBREW_MACOS_SDK_PATH); it never
# invokes Xcode tooling or the network.
class PybrewMacOSSDK
  attr_reader :path, :version

  def initialize(path, version)
    @path = Pathname.new(path.to_s)
    @version = MacOS::Version.new(version.to_s)
  end

  def to_s
    @path.to_s
  end
end

# Host platform helpers available to opt-in cask scripts.  They mirror the
# small subset of Homebrew's MacOS/OS/Hardware helpers that casks commonly
# use; none of this runs unless the caller enabled ``run_scripts``.
module MacOS
  VERSION_SYMBOLS = {
    "10.15" => :catalina,
    "11" => :big_sur,
    "12" => :monterey,
    "13" => :ventura,
    "14" => :sonoma,
    "15" => :sequoia,
    "26" => :tahoe,
    "27" => :golden_gate,
  }.freeze

  SYMBOL_VERSIONS = VERSION_SYMBOLS.invert.freeze

  #: macOS release version -> human readable release name (read-only).
  RELEASE_NAMES = {
    "10.15" => "Catalina",
    "10.14" => "Mojave",
    "10.13" => "High Sierra",
    "10.12" => "Sierra",
    "10.11" => "El Capitan",
    "11" => "Big Sur",
    "12" => "Monterey",
    "13" => "Ventura",
    "14" => "Sonoma",
    "15" => "Sequoia",
    "26" => "Tahoe",
    "27" => "Golden Gate",
  }.freeze

  DARWIN_VERSIONS = {
    19 => "10.15",
    20 => "11",
    21 => "12",
    22 => "13",
    23 => "14",
    24 => "15",
    25 => "26",
    26 => "27",
  }.freeze

  # A version string that also compares against the version symbols casks
  # use (``MacOS.version >= :sonoma``).
  class Version < String
    include Comparable

    def <=>(other)
      other = MacOS.version_string(other) if other.is_a?(Symbol)
      MacOS.version_parts(self) <=> MacOS.version_parts(other)
    end

    def ==(other)
      (self <=> other).zero?
    end
    alias eql? ==

    def major
      split(".")[0].to_s
    end

    def minor
      split(".")[1].to_s
    end

    def major_minor
      split(".")[0, 2].join(".")
    end

    # Mirror Homebrew's MacOSVersion naming helpers.  Big Sur reports 10.16
    # under SYSTEM_VERSION_COMPAT, hence the alias.
    def release_version
      value = major.to_i >= 11 ? major : major_minor
      value == "10.16" ? "11" : value
    end

    def release_name
      MacOS::RELEASE_NAMES[release_version]
    end

    def pretty_name
      release_name || "Dunno"
    end
  end

  def self.version_string(value)
    return SYMBOL_VERSIONS[value] if value.is_a?(Symbol) && SYMBOL_VERSIONS.key?(value)
    value.to_s
  end

  def self.version_parts(value)
    value.to_s.split(".").map(&:to_i)
  end

  def self.full_version
    @full_version ||= begin
      override = ENV["PYBREW_MACOS_VERSION"].to_s
      if override.empty?
        detected = nil
        begin
          output, status = Open3.capture2("/usr/bin/sw_vers", "-productVersion")
          detected = output.strip if status.success?
        rescue StandardError
          detected = nil
        end
        if detected.nil? || detected.empty?
          darwin = RbConfig::CONFIG["host_os"].to_s[/darwin(\d+)/, 1]
          detected = DARWIN_VERSIONS[darwin.to_i] unless darwin.nil?
        end
        detected.nil? || detected.empty? ? "0" : detected
      else
        override
      end
    end
  end

  def self.version
    Version.new(full_version)
  end

  def self.version_symbol
    parts = full_version.split(".")
    key = parts[0].to_i == 10 ? "10.#{parts[1]}" : parts[0].to_s
    VERSION_SYMBOLS.fetch(key, :unknown)
  end

  def self.release_name
    version.release_name
  end

  def self.pretty_name
    version.pretty_name
  end

  #: Directories searched for an installed macOS SDK (local filesystem only).
  SDK_ROOTS = [
    "/Library/Developer/CommandLineTools/SDKs",
    "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs",
  ].freeze

  def self.sdk_paths
    @sdk_paths ||= begin
      paths = {}
      override = ENV["PYBREW_MACOS_SDK_PATH"].to_s
      if override.empty?
        SDK_ROOTS.each do |root|
          Dir.glob(File.join(root, "MacOSX*.sdk")).sort.each do |candidate|
            match = File.basename(candidate).match(/\AMacOSX(\d+(?:\.\d+)*)\.sdk\z/)
            paths[match[1]] = candidate unless match.nil?
          end
        end
      else
        match = File.basename(override).match(/\AMacOSX(\d+(?:\.\d+)*)\.sdk\z/)
        paths[match.nil? ? full_version : match[1]] = override
      end
      paths
    end
  end

  # Best-effort SDK lookup: prefer the SDK matching the running macOS release,
  # otherwise the newest installed SDK.  Returns nil when none is available.
  def self.sdk(version = nil)
    return nil unless OS.mac?

    paths = sdk_paths
    return nil if paths.empty?

    key = if version.nil?
            host = version_parts(full_version).first
            matching = paths.keys.select do |candidate|
              version_parts(candidate).first == host
            end
            (matching.empty? ? paths.keys : matching).max_by do |candidate|
              version_parts(candidate)
            end
          else
            requested = version_string(version)
            found = paths.keys.find { |candidate| candidate == requested }
            found ||= paths.keys.find do |candidate|
              version_parts(candidate).first == version_parts(requested).first
            end
            found
          end
    return nil if key.nil?

    PybrewMacOSSDK.new(paths[key], key)
  end

  def self.sdk_path(version = nil)
    sdk = self.sdk(version)
    sdk.nil? ? nil : sdk.path
  end
end

module OS
  def self.mac?
    !!(RUBY_PLATFORM =~ /darwin/)
  end

  def self.linux?
    !!(RUBY_PLATFORM =~ /linux/)
  end

  def self.version
    return MacOS.version if mac?

    release = Etc.uname[:release].to_s[/\d+(\.\d+)*/] || "0"
    MacOS::Version.new(release)
  rescue StandardError
    MacOS::Version.new("0")
  end
end

module Hardware
  module CPU
    def self.cores
      Etc.nprocessors
    rescue StandardError
      1
    end
  end
end

# Argument normalisation shared by the ``system``/``system_command`` helpers.
# Cask scripts use several call styles, so accept a leading environment hash,
# positional argument lists, a trailing options hash, and the keyword forms
# ``env:``/``chdir:``.  Commands are always executed without a shell unless the
# caller passes a single string (the usual ``Kernel.system`` behaviour).
module PybrewCaskSystem
  module_function

  def stringify_environment(environment)
    return {} unless environment.is_a?(Hash)

    environment.each_with_object({}) do |(key, value), normalized|
      normalized[key.to_s] = value.nil? ? nil : value.to_s
    end
  end

  def normalize(args, options = {}, label = "system")
    arguments = args.dup
    options = options.dup
    environment = arguments.shift if arguments.first.is_a?(Hash)
    trailing = arguments.pop if arguments.last.is_a?(Hash)
    options = trailing.merge(options) if trailing
    env_option = options.delete(:env)
    environment = (environment || {}).merge(env_option) if env_option.is_a?(Hash)
    chdir = options.delete(:chdir)
    options[:chdir] = chdir.to_s unless chdir.nil?
    command = arguments.flatten.compact.map(&:to_s)
    raise ArgumentError, "#{label}: no command given" if command.empty?

    [stringify_environment(environment), command, options]
  end

  def run(args, options = {}, label = "system")
    environment, command, spawn_options = normalize(args, options, label)
    Kernel.system(environment, *command, **spawn_options)
  end

  # Capture stdout/stderr with optional stdin data and timeout.  ``input`` is
  # written verbatim, matching Homebrew's SystemCommand (an Array is joined
  # without separators).  A timeout raises Timeout::Error after terminating the
  # child process, so a hung cask script cannot hang the installer.
  def capture(environment, command, spawn_options, input = nil, timeout = nil)
    stdin_data = if input.nil?
                   nil
                 elsif input.is_a?(Array)
                   input.join
                 else
                   input.to_s
                 end
    if timeout.nil?
      spawn = spawn_options.dup
      spawn[:stdin_data] = stdin_data unless stdin_data.nil?
      return Open3.capture3(environment, *command, **spawn)
    end
    capture_with_timeout(environment, command, spawn_options, stdin_data, timeout)
  end

  def capture_with_timeout(environment, command, spawn_options, input, timeout)
    result = nil
    Open3.popen3(environment, *command, **spawn_options) do |stdin, stdout, stderr, wait_thr|
      begin
        stdin.write(input) unless input.nil?
      rescue Errno::EPIPE
        nil
      ensure
        begin
          stdin.close unless stdin.closed?
        rescue IOError
          nil
        end
      end
      out_reader = Thread.new { stdout.read }
      err_reader = Thread.new { stderr.read }
      if wait_thr.join(timeout).nil?
        begin
          Process.kill("TERM", wait_thr.pid)
        rescue Errno::ESRCH, Errno::EPERM
          nil
        end
        unless wait_thr.join(2)
          begin
            Process.kill("KILL", wait_thr.pid)
          rescue Errno::ESRCH, Errno::EPERM
            nil
          end
          wait_thr.join
        end
        out_reader.join(2)
        err_reader.join(2)
        raise Timeout::Error,
              "command timed out after #{timeout}s: #{command.join(' ')}"
      end
      out_reader.join
      err_reader.join
      result = [out_reader.value, err_reader.value, wait_thr.value]
    end
    result
  end
end

# Command helpers used by some cask scripts through ``Utils``.  They are
# intentionally limited to non-interactive execution: no sudo helper is
# provided and pybrew never prompts for a password.
module Utils
  module_function

  def safe_system(*args, **options)
    raise ArgumentError, "Utils.safe_system: no command given" if args.empty?

    success = PybrewCaskSystem.run(args, options, "Utils.safe_system")
    unless success
      status = $?
      code = status.respond_to?(:exitstatus) ? status.exitstatus : 1
      raise "Utils.safe_system failed (exit #{code}): #{args.flatten.compact.join(' ')}"
    end
    true
  end

  def quiet_system(*args, **options)
    return false if args.empty?

    PybrewCaskSystem.run(args, { out: File::NULL, err: File::NULL }.merge(options),
                         "Utils.quiet_system")
  rescue StandardError
    false
  end

  # Non-interactive stdout capture.  Matches the parts of Homebrew's
  # ``Utils.popen_read`` that cask scripts commonly rely on.
  def popen_read(*args, **options)
    return "" if args.empty?

    environment, command, spawn_options =
      PybrewCaskSystem.normalize(args, options, "Utils.popen_read")
    stdout, _stderr, _status = Open3.capture3(environment, *command, **spawn_options)
    stdout
  end

  # Locate an executable on PATH, like Homebrew's ``Utils.which``.
  def which(command)
    return nil if command.nil?

    candidate = command.to_s
    return candidate if candidate.include?(File::SEPARATOR) && File.executable?(candidate)

    ENV["PATH"].to_s.split(File::PATH_SEPARATOR).each do |directory|
      path = File.join(directory, candidate)
      return path if File.file?(path) && File.executable?(path)
    end
    nil
  end

  # Write to a command's stdin and capture its output.  This is the safe,
  # non-interactive subset of Homebrew's ``Utils.popen_write``: no sudo and no
  # shell is involved.  With ``safe: true`` a non-zero exit raises.
  def popen_write(*args, safe: false, **options, &block)
    environment, command, spawn_options =
      PybrewCaskSystem.normalize(args, options, "Utils.popen_write")
    output = +""
    status = nil
    Open3.popen2e(environment, *command, **spawn_options) do |stdin, stdout, wait_thr|
      begin
        block.call(stdin) if block
      ensure
        begin
          stdin.close unless stdin.closed?
        rescue IOError
          nil
        end
      end
      output = stdout.read
      status = wait_thr.value
    end
    if safe && (status.nil? || !status.success?)
      code = status.nil? ? 1 : status.exitstatus
      raise "Utils.popen_write failed (exit #{code}): #{command.join(' ')}"
    end
    output
  end
end

class PybrewCaskScriptResult
  attr_reader :stdout, :stderr, :exit_status, :command

  def initialize(stdout:, stderr:, exit_status:, command: nil)
    @stdout = stdout
    @stderr = stderr
    @exit_status = exit_status
    @command = command
  end

  def success?
    @exit_status.zero?
  end

  # Homebrew's SystemCommand::Result exposes the combined output; cask scripts
  # sometimes parse it instead of ``stdout``.
  def merged_output
    "#{@stdout}#{@stderr}"
  end

  def assert_success!
    return self if success?

    label = @command.nil? || @command.empty? ? "command" : @command.join(" ")
    raise "command failed (exit #{@exit_status}): #{label}"
  end

  def to_s
    @stdout
  end
end

class PybrewCaskScriptContext
  attr_reader :token, :version, :bin, :prefix

  def initialize(token:, version:, appdir:, staged_path:, bin:, prefix:)
    @token = token
    @version = version
    @appdir = Pathname(appdir)
    @staged_path = Pathname(staged_path)
    @bin = Pathname(bin)
    @prefix = Pathname(prefix)
  end

  # ``appdir`` and ``staged_path`` are Pathname objects, matching the Homebrew
  # cask DSL, so scripts can use ``appdir.join("Foo.app")`` and friends.
  def appdir
    @appdir
  end

  def staged_path
    @staged_path
  end

  # Homebrew's cask script context delegates these to the cask.  pybrew derives
  # them from the install context without changing the argv contract:
  # ``caskroom_path`` from prefix+token, ``arch`` from the host, and
  # ``language`` from ``PYBREW_CASK_LANGUAGE`` (set by CaskInstaller) with a
  # locale fallback.
  def caskroom_path
    @caskroom_path ||= @prefix/"Caskroom"/token.to_s
  end

  def arch
    @arch ||= RUBY_PLATFORM =~ /arm64|aarch64/ ? "arm64" : "intel"
  end

  def language
    @language ||= begin
      value = ENV["PYBREW_CASK_LANGUAGE"].to_s
      if value.empty?
        locale = ENV["LANG"].to_s
        value = locale.split(".").first.to_s.split("_").first.to_s
      end
      value.empty? ? "en" : value
    end
  end

  # Accepts the Homebrew keyword form (``args:``/``env:``/``chdir:``), a plain
  # positional argument list, and the leading environment hash form:
  #
  #   system_command "/bin/echo", args: ["hi"], env: { "LC_ALL" => "C" }
  #   system_command "/bin/echo", "hi"
  #   system_command ["/bin/echo", "hi"]
  #   system_command({ "LC_ALL" => "C" }, "/bin/echo", "hi")
  def system_command(command = nil, *positional, **options)
    options = options.dup
    args_option = options.delete(:args)
    print_stdout = options.delete(:print_stdout)
    print_stderr = options.key?(:print_stderr) ? options.delete(:print_stderr) : true
    # Homebrew's cask ``system_command`` is SystemCommand.run! and raises on a
    # non-zero exit.  ``must_succeed: false`` requests the non-raising form.
    must_succeed = options.key?(:must_succeed) ? options.delete(:must_succeed) : true
    input = options.delete(:input)
    timeout = options.delete(:timeout)
    if options.delete(:sudo) || options.delete(:sudo_as_root)
      raise ArgumentError, "system_command: sudo is not supported by pybrew cask scripts"
    end
    arguments = [command, *Array(args_option), *positional]
    environment, argv, spawn_options =
      PybrewCaskSystem.normalize(arguments, options, "system_command")
    if input.nil? && timeout.nil?
      stdout, stderr, status = Open3.capture3(environment, *argv, **spawn_options)
    else
      stdout, stderr, status =
        PybrewCaskSystem.capture(environment, argv, spawn_options, input, timeout)
    end
    $stdout.print(stdout) if print_stdout && !stdout.empty?
    $stderr.print(stderr) if print_stderr && !stderr.empty?
    result = PybrewCaskScriptResult.new(stdout: stdout, stderr: stderr,
                                        exit_status: status.exitstatus || 1,
                                        command: argv)
    result.assert_success! if must_succeed
    result
  end

  def system_command!(*args, **options)
    result = system_command(*args, **options)
    unless result.success?
      raise "command failed (exit #{result.exit_status}): #{args.flatten.compact.join(' ')}"
    end
    result
  end

  # Same call styles as ``system_command`` (minus the captured-output
  # options); returns true/false and raises when no command is given.
  # ``must_succeed: true`` raises on failure, matching Utils.safe_system.
  def system(*args, **options)
    options = options.dup
    must_succeed = options.key?(:must_succeed) ? options.delete(:must_succeed) : false
    command = options.delete(:command)
    args = [command, *Array(options.delete(:args)), *args] unless command.nil?
    success = PybrewCaskSystem.run(args, options, "system") ? true : false
    if must_succeed && !success
      status = $?
      code = status.respond_to?(:exitstatus) ? status.exitstatus : 1
      raise "command failed (exit #{code}): #{args.flatten.compact.join(' ')}"
    end
    success
  end

  def shell_command(command, **options)
    system_command("/bin/sh", args: ["-c", command.to_s], **options)
  end

  def ohai(message)
    puts "==> #{message}"
  end

  def opoo(message)
    warn "Warning: #{message}"
  end
end

script_name = ARGV.fetch(0)
body_path = ARGV.fetch(7)
context = PybrewCaskScriptContext.new(
  token: ARGV.fetch(1),
  version: ARGV.fetch(2),
  appdir: ARGV.fetch(3),
  staged_path: ARGV.fetch(4),
  bin: ARGV.fetch(5),
  prefix: ARGV.fetch(6)
)

begin
  context.instance_eval(File.read(body_path), body_path, 1)
rescue Exception => error # rubocop:disable Lint/RescueException
  warn "pybrew: #{script_name} script failed: #{error.class}: #{error.message}"
  Array(error.backtrace).first(10).each { |line| warn "  #{line}" }
  exit 1
end
'''


@dataclass
class CaskInstallResult:
    token: str
    version: str
    room: Path
    artifacts: list[str]
    skipped: bool = False


def _extract_7z(archive: Path, destination: Path) -> None:
    executable = os.environ.get("PYBREW_7Z") or shutil.which("7z") or shutil.which("7za")
    if executable is None:
        raise CaskError("7z casks require the 7z/7za executable (set PYBREW_7Z)")
    destination.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [executable, "x", "-y", f"-o{destination}", str(archive)],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise CaskError(
            f"could not extract 7z archive {archive}: "
            f"{(result.stderr or result.stdout).strip()}"
        )


def _extract_rar(archive: Path, destination: Path) -> None:
    executable = os.environ.get("PYBREW_UNRAR") or shutil.which("unrar")
    if executable is None:
        raise CaskError("rar casks require unrar (set PYBREW_UNRAR)")
    destination.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [executable, "x", "-o+", str(archive), str(destination)],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise CaskError(
            f"could not extract rar archive {archive}: "
            f"{(result.stderr or result.stdout).strip()}"
        )


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            name = info.filename
            parts = Path(name).parts
            if not name or name.startswith("/") or ".." in parts:
                raise CaskError(f"refusing zip member with unsafe path: {name!r}")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                link_target = bundle.read(info).decode("utf-8", errors="replace")
                if link_target.startswith("/") or ".." in Path(link_target).parts:
                    raise CaskError(f"refusing escaping symlink in zip: {name!r}")
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(link_target)
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            with bundle.open(info) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
            permissions = (info.external_attr >> 16) & 0o777
            if permissions:
                os.chmod(target, permissions)


# Homebrew-style artifact kinds that install into the user's Library tree.
# Values are paths relative to CaskInstaller.library_root.
COMPLETION_ARTIFACT_DIRS: dict[str, str] = {
    "bash_completion": "share/bash-completion/completions",
    "zsh_completion": "share/zsh/site-functions",
    "fish_completion": "share/fish/vendor_completions.d",
}

LIBRARY_ARTIFACT_DIRS: dict[str, str] = {
    "font": "Fonts",
    "colorpicker": "ColorPickers",
    "prefpane": "PreferencePanes",
    "qlplugin": "QuickLook",
    "mdimporter": "Spotlight",
    "screen_saver": "Screen Savers",
    "service": "Services",
    "audio_unit_plugin": "Audio/Plug-Ins/Components",
    "vst_plugin": "Audio/Plug-Ins/VST",
    "vst3_plugin": "Audio/Plug-Ins/VST3",
    "dictionary": "Dictionaries",
    "input_method": "Input Methods",
    "keyboard_layout": "Keyboard Layouts",
}


def _safe_artifact_target(target: Optional[str], fallback: str, token: str) -> Path:
    """Turn an optional artifact target into a safe relative path.

    Cask targets are normally basenames (or a small relative path) inside the
    artifact's Library directory.  Refuse absolute paths, home-relative paths
    and ``..`` traversal so a malformed cask cannot escape ``library_root``.
    """

    if target is None or not str(target).strip():
        candidate = Path(fallback)
    else:
        text = str(target)
        if text.startswith("~"):
            raise CaskError(f"{token}: unsafe artifact target {target!r}")
        candidate = Path(text)
    if (
        candidate.is_absolute()
        or candidate.drive
        or not candidate.parts
        or any(part in ("", "..") for part in candidate.parts)
    ):
        raise CaskError(f"{token}: unsafe artifact target {target!r}")
    return candidate


def _archive_kind(path: Path) -> str:
    suffix = path.name.lower()
    if suffix.endswith(".pkg"):
        return "pkg"
    if suffix.endswith(".dmg"):
        return "dmg"
    if suffix.endswith(".zip"):
        return "zip"
    if suffix.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")):
        return "tar"
    try:
        with open(path, "rb") as handle:
            magic = handle.read(8)
    except OSError:
        return "unknown"
    if magic.startswith(b"PK\x03\x04"):
        return "zip"
    if magic.startswith(b"\x1f\x8b") or magic.startswith(b"BZh") or magic.startswith(b"\xfd7zXZ"):
        return "tar"
    if b"koly" in path.read_bytes()[-512:]:
        return "dmg"
    return "unknown"


class _MountedImage:
    def __init__(self, archive: Path, mountpoint: Path):
        self.archive = archive
        self.mountpoint = mountpoint
        self.mounted = False

    def __enter__(self) -> Path:
        self.mountpoint.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "hdiutil",
                "attach",
                "-nobrowse",
                "-readonly",
                "-mountpoint",
                str(self.mountpoint),
                str(self.archive),
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise CaskError(f"could not mount dmg {self.archive}: {detail}")
        self.mounted = True
        return self.mountpoint

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.mounted:
            subprocess.run(
                ["hdiutil", "detach", str(self.mountpoint)],
                text=True,
                capture_output=True,
            )


class CaskInstaller:
    def __init__(
        self,
        config: Config,
        downloader: Optional[Downloader] = None,
        appdir: Optional[Path] = None,
        pkg_root: Optional[Path] = None,
        library_root: Optional[Path] = None,
        system_pkg: bool = False,
        run_scripts: bool = False,
    ):
        self.config = config
        self.downloader = downloader or Downloader(config.downloads, config.bottle_dirs)
        self.appdir = Path(appdir).expanduser() if appdir else config.applications
        self.pkg_root = Path(pkg_root).expanduser() if pkg_root else (config.prefix / "pkgroot")
        self.library_root = (
            Path(library_root).expanduser() if library_root else (config.prefix / "Library")
        )
        # Opt-in system-wide pkg installation.  The default remains the safe
        # path that expands the payload beneath ``pkg_root``.
        self.system_pkg = system_pkg
        # Opt-in cask preflight/postflight scripts.  Captured script source is
        # inert data unless this flag is enabled; when disabled nothing is
        # executed and no Ruby process is started.
        self.run_scripts = run_scripts
        self._system_pkg_used = False
        # Package IDs installed by the current system-wide pkg invocation and
        # whether ``pkgutil --pkgs`` tracking was available at all.
        self._system_pkg_ids: list[str] = []
        self._system_pkg_tracked = False
        # Upgrade rollback state: whether the macOS installer was invoked and
        # the package ID snapshot taken just before it ran.  The snapshot is
        # what lets a failed upgrade identify *newly* installed packages
        # without ever touching files that predate the attempt.
        self._system_pkg_attempted = False
        self._system_pkg_before_ids: Optional[set[str]] = None
        self._script_results: dict[str, dict[str, Any]] = {}

    # -- paths -----------------------------------------------------------
    def room(self, cask: Cask, version: Optional[str] = None) -> Path:
        return self.config.caskroom / cask.token / (version or cask.resolved_version())

    def installed_versions(self, token: str) -> list[str]:
        rack = self.config.caskroom / token
        if not rack.is_dir():
            return []
        return sorted(path.name for path in rack.iterdir() if path.is_dir())

    def list_installed(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.config.caskroom.is_dir():
            return rows
        for rack in sorted(self.config.caskroom.iterdir()):
            if not rack.is_dir():
                continue
            for version in sorted(path.name for path in rack.iterdir() if path.is_dir()):
                rows.append({"token": rack.name, "version": version, "room": str(rack / version)})
        return rows

    # -- install ---------------------------------------------------------
    def install(self, cask: Cask, force: bool = False) -> CaskInstallResult:
        self._system_pkg_used = False
        self._system_pkg_ids = []
        self._system_pkg_tracked = False
        self._system_pkg_attempted = False
        self._system_pkg_before_ids = None
        self._script_results = {}
        self._check_arch_dependency(cask)
        self._check_os_dependency(cask)
        self._check_macos_dependency(cask)
        self._check_named_dependencies(cask)
        if cask.disabled:
            detail = f" ({cask.disable_reason})" if cask.disable_reason else ""
            raise CaskError(f"{cask.token} is disabled{detail}")
        version = cask.resolved_version()
        if version in ("latest", "0"):
            raise CaskError(f"{cask.token}: version {version!r} cannot be installed reliably")
        room = self.room(cask, version)
        if room.is_dir() and not force:
            return CaskInstallResult(cask.token, version, room, [], skipped=True)
        if room.is_dir():
            self._remove_room(room)

        cask.appdir = str(self.appdir)
        installed: list[str] = []
        # A bare ``stage_only`` cask has nothing to download.  When download
        # metadata is present it is still fetched and verified, matching normal
        # cask staging; only artifact installation is skipped.
        bookkeeping_only = cask.stage_only and not cask.url and not cask.resolved_sha256()
        if not bookkeeping_only:
            url = cask.resolved_url()
            digest = cask.resolved_sha256()
            if not digest:
                raise CaskError(f"{cask.token}: no sha256 for {cask.resolved_arch()}")
            hint = f"{cask.token}-{version}.{Path(url.split('?', 1)[0]).suffix or 'download'}"
            archive = self.downloader.fetch(url, digest, hint=hint)
            kind = _archive_kind(archive)
            if cask.container in ("nupkg", "zip"):
                kind = "zip"
            elif cask.container in ("7z", "rar"):
                kind = cask.container
        else:
            url = None
            digest = None
            kind = None

        room.parent.mkdir(parents=True, exist_ok=True)
        room.mkdir(parents=True, exist_ok=True)
        metadata = room / ".metadata"
        metadata.mkdir(exist_ok=True)

        if not bookkeeping_only:
            with tempfile.TemporaryDirectory(prefix="pybrew-cask-") as temporary:
                temp_root = Path(temporary)
                if kind == "dmg":
                    if not os.path.exists("/usr/bin/hdiutil"):
                        raise CaskError("dmg casks require hdiutil (macOS)")
                    with _MountedImage(archive, temp_root / "mount") as source_root:
                        installed = self._install_artifacts_with_scripts(
                            cask, source_root, room, version, force, cwd=source_root
                        )
                elif kind == "zip":
                    _safe_extract_zip(archive, temp_root)
                    installed = self._install_artifacts_with_scripts(
                        cask, temp_root, room, version, force, cwd=temp_root
                    )
                elif kind == "tar":
                    safe_extract_tar(archive, temp_root)
                    installed = self._install_artifacts_with_scripts(
                        cask, temp_root, room, version, force, cwd=temp_root
                    )
                elif kind == "7z":
                    _extract_7z(archive, temp_root)
                    installed = self._install_artifacts_with_scripts(
                        cask, temp_root, room, version, force, cwd=temp_root
                    )
                elif kind == "rar":
                    _extract_rar(archive, temp_root)
                    installed = self._install_artifacts_with_scripts(
                        cask, temp_root, room, version, force, cwd=temp_root
                    )
                elif kind == "pkg":
                    installed = self._install_artifacts_with_scripts(
                        cask, archive.parent, room, version, force,
                        archive=archive, cwd=self.pkg_root,
                    )
                else:
                    shutil.rmtree(room, ignore_errors=True)
                    raise CaskError(f"{cask.token}: unsupported cask archive type for {url}")
        else:
            # A bare stage_only cask still runs its scripts (there are no
            # artifacts, so preflight and postflight are back to back).
            self._run_preflight(cask, room, version, room)
            self._run_cask_script(cask, "postflight", room, version, room)

        receipt = {
            "token": cask.token,
            "version": version,
            "url": url,
            "sha256": digest,
            "stage_only": cask.stage_only,
            "artifacts": [
                {"kind": artifact.kind, "source": artifact.source, "target": artifact.target}
                for artifact in cask.resolved_artifacts()
            ],
            "installed": installed,
            "tap": cask.tap,
            # Script opt-in and per-script outcome.  ``scripts_requested`` is
            # False for every default install, even when the cask defines
            # preflight/postflight blocks.
            "scripts_requested": self.run_scripts,
            "script_results": dict(self._script_results),
        }
        if self._system_pkg_used:
            # No payload paths are recorded for system-wide pkg installs: the
            # macOS installer owns the files on disk.  The package IDs
            # installed by this invocation are diffed from ``pkgutil --pkgs``
            # so uninstall can later remove exactly those files and forget the
            # corresponding receipts.
            receipt["system_pkg"] = True
            receipt["system_pkg_ids"] = sorted(self._system_pkg_ids)
            receipt["system_pkg_tracked"] = self._system_pkg_tracked
        (metadata / "receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return CaskInstallResult(cask.token, version, room, installed)

    # -- opt-in cask scripts --------------------------------------------
    def _install_artifacts_with_scripts(
        self,
        cask: Cask,
        source_root: Path,
        room: Path,
        version: str,
        force: bool,
        archive: Optional[Path] = None,
        cwd: Optional[Path] = None,
    ) -> list[str]:
        self._run_preflight(cask, room, version, cwd)
        installed = self._install_artifacts(cask, source_root, room, force, archive=archive)
        self._run_cask_script(cask, "postflight", room, version, cwd)
        return installed

    def _run_preflight(
        self, cask: Cask, room: Path, version: str, cwd: Optional[Path] = None
    ) -> None:
        try:
            self._run_cask_script(cask, "preflight", room, version, cwd)
        except CaskError:
            # No artifacts have been installed yet: drop the empty room so a
            # retry is not mistaken for an already-installed cask.
            self._remove_path(room)
            raise

    def _run_cask_script(
        self,
        cask: Cask,
        name: str,
        room: Path,
        version: str,
        cwd: Optional[Path] = None,
    ) -> None:
        """Execute one captured cask script block with the Ruby shim.

        This only runs when ``run_scripts`` was explicitly enabled.  The block
        body is passed to Ruby as a file; it is never evaluated in Python.
        Scripts run with the extracted source root as their working directory
        (``pkg_root`` for pkg casks) when one is available.
        """

        body = getattr(cask, name, None)
        if not self.run_scripts or not body or not str(body).strip():
            return
        ruby = os.path.expanduser(os.environ.get("PYBREW_RUBY") or "/usr/bin/ruby")
        executable = shutil.which(ruby)
        if executable is None:
            raise CaskError(
                f"{cask.token}: {name} script requires a Ruby interpreter at "
                f"{ruby!r}; install Ruby or set PYBREW_RUBY"
            )
        if cwd is not None:
            # pkg_root may not exist until the package is installed; create it
            # only when a script actually needs a working directory there.
            Path(cwd).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="pybrew-cask-script-") as temporary:
            temp_root = Path(temporary)
            shim = temp_root / "cask_script_shim.rb"
            shim.write_text(_CASK_SCRIPT_SHIM, encoding="utf-8")
            body_path = temp_root / f"{name}.rb"
            body_path.write_text(str(body) + "\n", encoding="utf-8")
            command = [
                executable,
                str(shim),
                name,
                cask.token,
                version,
                str(self.appdir),
                str(room),
                str(self.config.bin),
                str(self.config.prefix),
                str(body_path),
            ]
            script_env = dict(os.environ)
            script_env["PYBREW_CASK_LANGUAGE"] = cask.language or ""
            try:
                result = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    cwd=str(cwd) if cwd is not None else None,
                    env=script_env,
                )
            except OSError as error:
                raise CaskError(
                    f"{cask.token}: could not run {name} script with {executable}: {error}"
                ) from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise CaskError(
                f"{cask.token}: {name} script failed (exit {result.returncode}): "
                f"{detail[-1000:] or 'no output'}"
            )
        self._script_results[name] = {"status": "ok", "exit_code": 0}

    def _install_artifacts(
        self,
        cask: Cask,
        source_root: Path,
        room: Path,
        force: bool,
        archive: Optional[Path] = None,
    ) -> list[str]:
        installed: list[str] = []
        if cask.stage_only:
            return installed
        self._apply_renames(cask, source_root)
        artifacts = cask.resolved_artifacts()
        appdir = self.appdir
        appdir.mkdir(parents=True, exist_ok=True)
        # Apps first so binary artifacts inside them can resolve.
        ordered = sorted(artifacts, key=lambda artifact: 0 if artifact.kind == "app" else 1)
        for artifact in ordered:
            if artifact.kind == "app":
                source = self._find(source_root, artifact.source)
                if source is None:
                    raise CaskError(f"{cask.token}: app {artifact.source!r} not found in archive")
                destination = appdir / (artifact.target or source.name)
                if destination.exists():
                    if not force:
                        raise CaskError(f"{cask.token}: {destination} already exists")
                    self._remove_path(destination)
                shutil.copytree(source, destination, symlinks=True)
                installed.append(str(destination))
            elif artifact.kind == "binary":
                source_path = Path(artifact.source)
                if not source_path.is_absolute():
                    # Prefer the installed app location: the downloaded/extracted
                    # staging directory is deleted after installation.
                    app_candidate = appdir / artifact.source
                    source_path = (
                        app_candidate if app_candidate.exists() else source_root / artifact.source
                    )
                if not source_path.exists():
                    # Binary paths often point into an installed app.
                    candidate = Path(artifact.source)
                    if candidate.exists():
                        source_path = candidate
                    else:
                        found = self._find(source_root, Path(artifact.source).name)
                        if found is None:
                            raise CaskError(
                                f"{cask.token}: binary {artifact.source!r} not found"
                            )
                        source_path = found
                target = self.config.bin / (artifact.target or source_path.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    if not force:
                        raise CaskError(f"{cask.token}: {target} already exists")
                    target.unlink()
                target.symlink_to(source_path)
                installed.append(str(target))
            elif artifact.kind in ("pkg", "installer"):
                self._install_pkg(cask, artifact, source_root, room, force, installed, archive)

            elif artifact.kind == "appimage":
                source = self._find(source_root, artifact.source)
                if source is None:
                    raise CaskError(
                        f"{cask.token}: appimage {artifact.source!r} not found in archive"
                    )
                destination = appdir / (artifact.target or source.name)
                if destination.exists() or destination.is_symlink():
                    if not force:
                        raise CaskError(f"{cask.token}: {destination} already exists")
                    self._remove_path(destination)
                shutil.copy2(source, destination)
                destination.chmod(destination.stat().st_mode | 0o111)
                installed.append(str(destination))
            elif artifact.kind == "suite":
                source = self._find(source_root, artifact.source)
                if source is None:
                    raise CaskError(f"{cask.token}: suite {artifact.source!r} not found")
                for app_source in source.rglob("*.app"):
                    destination = appdir / app_source.name
                    if destination.exists() and force:
                        self._remove_path(destination)
                    if not destination.exists():
                        shutil.copytree(app_source, destination, symlinks=True)
                        installed.append(str(destination))
            elif artifact.kind == "manpage":
                self._install_manpage_artifact(
                    cask, artifact, source_root, force, installed
                )
            elif artifact.kind in COMPLETION_ARTIFACT_DIRS:
                self._install_completion_artifact(
                    cask, artifact, source_root, force, installed
                )
            elif artifact.kind in LIBRARY_ARTIFACT_DIRS:
                self._install_library_artifact(
                    cask, artifact, source_root, force, installed
                )
            elif artifact.kind == "stage_only":
                # A stage_only artifact entry installs nothing.
                continue
            else:
                raise CaskError(f"{cask.token}: unsupported artifact {artifact.kind}")
        return installed

    def _check_arch_dependency(self, cask: Cask) -> None:
        value = cask.depends_on.get("arch")
        if value is None:
            return
        requested = value if isinstance(value, list) else [value]
        machine = platform.machine().lower()
        current = "arm64" if machine in ("arm64", "aarch64") else "intel"
        allowed = {str(item).lstrip(":") for item in requested}
        if current not in allowed:
            raise CaskError(
                f"{cask.token} requires arch {', '.join(sorted(allowed))} "
                f"but this machine is {current}"
            )

    @staticmethod
    def _dependency_names(value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        return [str(item) for item in values if str(item)]

    def _check_os_dependency(self, cask: Cask) -> None:
        if cask.os.get("macos"):
            if not sys.platform.startswith("darwin"):
                raise CaskError(f"{cask.token} is only available on macOS")
            self._check_macos_requirement(cask, cask.os["macos"])
        if cask.os.get("linux") and not sys.platform.startswith("linux"):
            raise CaskError(f"{cask.token} is only available on Linux")

    def _check_macos_dependency(self, cask: Cask) -> None:
        value = cask.depends_on.get("macos")
        if value is not None:
            self._check_macos_requirement(cask, value)

    def _check_macos_requirement(self, cask: Cask, value: Any) -> None:
        requirements = value if isinstance(value, list) else [value]
        current = (os.environ.get("PYBREW_MACOS_VERSION") or platform.mac_ver()[0]).strip()
        if current in ("", "0"):
            raise CaskError(f"{cask.token} requires macOS but this host is not macOS")
        current_parts = self._version_parts(current)
        for requirement in requirements:
            text = str(requirement).strip()
            match = re.match(r"^(>=|<=|>|<|=)?\s*:?(\w+)$", text)
            if not match:
                continue
            operator = match.group(1) or "="
            target = match.group(2)
            target_version = _MACOS_SYMBOL_VERSIONS.get(target, target)
            target_parts = self._version_parts(target_version)
            if not self._compare_version(current_parts, target_parts, operator):
                raise CaskError(
                    f"{cask.token} requires macOS {text} but this host is {current}"
                )

    @staticmethod
    def _version_parts(value: str) -> list[int]:
        return [int(part) if part.isdigit() else 0 for part in str(value).split(".")]

    @staticmethod
    def _compare_version(current: list[int], target: list[int], operator: str) -> bool:
        width = max(len(current), len(target))
        left = list(current) + [0] * (width - len(current))
        right = list(target) + [0] * (width - len(target))
        if operator == ">=":
            return left >= right
        if operator == "<=":
            return left <= right
        if operator == ">":
            return left > right
        if operator == "<":
            return left < right
        return left == right

    def _check_named_dependencies(self, cask: Cask) -> None:
        for formula_name in self._dependency_names(cask.depends_on.get("formula")):
            if not (self.config.cellar / formula_name).is_dir():
                raise CaskError(
                    f"{cask.token} requires formula {formula_name!r} to be installed"
                )
        for dependency in self._dependency_names(cask.depends_on.get("cask")):
            if not (self.config.caskroom / dependency).is_dir():
                raise CaskError(
                    f"{cask.token} requires cask {dependency!r} to be installed"
                )
        for formula_name in self._dependency_names(cask.conflicts_with.get("formula")):
            if (self.config.cellar / formula_name).is_dir():
                raise CaskError(
                    f"{cask.token} conflicts with installed formula {formula_name!r}"
                )
        for conflict in self._dependency_names(cask.conflicts_with.get("cask")):
            if (self.config.caskroom / conflict).is_dir():
                raise CaskError(
                    f"{cask.token} conflicts with installed cask {conflict!r}"
                )

    def _install_manpage_artifact(
        self,
        cask: Cask,
        artifact: Artifact,
        source_root: Path,
        force: bool,
        installed: list[str],
    ) -> None:
        source = self._find(source_root, artifact.source)
        if source is None:
            raise CaskError(
                f"{cask.token}: manpage {artifact.source!r} not found in archive"
            )
        suffix = source.suffix.lstrip(".")
        section = suffix[0] if suffix and suffix[0].isdigit() else "1"
        relative = _safe_artifact_target(artifact.target, source.name, cask.token)
        destination = self.config.prefix / "share" / "man" / f"man{section}" / relative
        if destination.exists() or destination.is_symlink():
            if not force:
                raise CaskError(f"{cask.token}: {destination} already exists")
            self._remove_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        installed.append(str(destination))

    def _apply_renames(self, cask: Cask, source_root: Path) -> None:
        for old, new in cask.renames:
            source = self._find(source_root, old)
            if source is None:
                raise CaskError(f"{cask.token}: rename source {old!r} not found in archive")
            relative = _safe_artifact_target(new, new, cask.token)
            destination = source_root / relative
            if destination.exists() or destination.is_symlink():
                raise CaskError(f"{cask.token}: rename target {destination} already exists")
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)

    def _install_completion_artifact(
        self,
        cask: Cask,
        artifact: Artifact,
        source_root: Path,
        force: bool,
        installed: list[str],
    ) -> None:
        source = self._find(source_root, artifact.source)
        if source is None:
            raise CaskError(
                f"{cask.token}: {artifact.kind} {artifact.source!r} not found in archive"
            )
        relative = _safe_artifact_target(artifact.target, source.name, cask.token)
        destination = self.config.prefix / COMPLETION_ARTIFACT_DIRS[artifact.kind] / relative
        if destination.exists() or destination.is_symlink():
            if not force:
                raise CaskError(f"{cask.token}: {destination} already exists")
            self._remove_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        installed.append(str(destination))

    def _install_library_artifact(
        self,
        cask: Cask,
        artifact: Artifact,
        source_root: Path,
        force: bool,
        installed: list[str],
    ) -> None:
        source = self._find(source_root, artifact.source)
        if source is None:
            raise CaskError(
                f"{cask.token}: {artifact.kind} {artifact.source!r} not found in archive"
            )
        relative = _safe_artifact_target(artifact.target, source.name, cask.token)
        destination = self.library_root / LIBRARY_ARTIFACT_DIRS[artifact.kind] / relative
        if destination.exists() or destination.is_symlink():
            if not force:
                raise CaskError(f"{cask.token}: {destination} already exists")
            self._remove_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination)
        installed.append(str(destination))

    def _install_pkg(
        self,
        cask: Cask,
        artifact: Artifact,
        source_root: Path,
        room: Path,
        force: bool,
        installed: list[str],
        archive: Optional[Path],
    ) -> None:
        if not self.system_pkg and (
            sys.platform != "darwin" or shutil.which("pkgutil") is None
        ):
            raise CaskError("pkg artifacts require macOS pkgutil")
        package: Optional[Path] = None
        if artifact.source:
            package = self._find(source_root, artifact.source)
        if package is None and archive is not None:
            package = archive
        if package is None:
            raise CaskError(f"{cask.token}: pkg {artifact.source!r} not found")
        if self.system_pkg:
            # Explicit opt-in: hand the package to macOS ``installer`` instead
            # of expanding and copying its payload beneath ``pkg_root``.
            self._install_pkg_system(cask, package)
            return
        expanded = room / ".pkg-expanded"
        if expanded.exists():
            shutil.rmtree(expanded)
        result = subprocess.run(
            ["pkgutil", "--expand-full", str(package), str(expanded)],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            shutil.rmtree(expanded, ignore_errors=True)
            raise CaskError(
                f"{cask.token}: could not expand package {package}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        payload = expanded / "Payload"
        if not payload.is_dir():
            candidates = sorted(expanded.glob("**/Payload"))
            if not candidates:
                raise CaskError(f"{cask.token}: package has no Payload directory")
            payload = candidates[0]
        self.pkg_root.mkdir(parents=True, exist_ok=True)
        # The package payload is rooted at its install location; copy it under
        # the configured pkg-root rather than writing to the system volume.
        shutil.copytree(payload, self.pkg_root, dirs_exist_ok=True, symlinks=True)
        for path in payload.rglob("*"):
            if path.is_file() or path.is_symlink():
                relative = path.relative_to(payload)
                target = self.pkg_root / relative
                installed.append(str(target))
        shutil.rmtree(expanded, ignore_errors=True)

    def _install_pkg_system(self, cask: Cask, package: Path) -> None:
        """Install a pkg artifact system-wide with macOS ``installer``.

        This is an explicit opt-in escape hatch.  It requires macOS, an
        installer executable and effective root; pybrew never expands or
        copies the payload in this mode.

        The package IDs installed by this invocation are captured by diffing
        ``pkgutil --pkgs`` before and after the installer run.  They are stored
        in the cask receipt so ``uninstall`` can remove exactly those files.
        """

        if sys.platform != "darwin":
            raise CaskError(f"{cask.token}: system-wide pkg installation requires macOS")
        installer = os.path.expanduser(
            os.environ.get("PYBREW_INSTALLER") or "/usr/sbin/installer"
        )
        executable = shutil.which(installer)
        if executable is None:
            raise CaskError(
                f"{cask.token}: installer executable not found or not executable: "
                f"{installer}; set PYBREW_INSTALLER to the macOS installer tool"
            )
        if os.geteuid() != 0:
            raise CaskError(
                f"{cask.token}: system-wide pkg installation requires root; "
                "rerun with sudo or use --pkg-root for a payload-only install"
            )
        # Tracking is best effort when the host has no pkgutil at all (the
        # synthetic test path pretends to be macOS).  An explicitly configured
        # pkgutil that is missing is an error: silently losing uninstall
        # tracking is worse than refusing the install.
        pkgutil = self._pkgutil_executable(required=False)
        before: Optional[set[str]] = None
        if pkgutil is not None:
            before = self._pkgutil_list_ids(cask.token, pkgutil)
        if before is not None and self._system_pkg_before_ids is None:
            # Keep the earliest snapshot: a multi-pkg cask must roll back
            # every package installed by the whole failed upgrade, including
            # packages from earlier artifacts.
            self._system_pkg_before_ids = set(before)
        self._system_pkg_attempted = True
        command = [executable, "-pkg", str(package), "-target", "/"]
        try:
            result = subprocess.run(command, text=True, capture_output=True)
        except OSError as error:
            raise CaskError(
                f"{cask.token}: could not run installer {executable}: {error}"
            ) from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise CaskError(
                f"{cask.token}: system-wide package installation failed "
                f"(exit {result.returncode}): {detail or package}"
            )
        if pkgutil is not None and before is not None:
            try:
                after = self._pkgutil_list_ids(cask.token, pkgutil)
            except CaskError as error:
                # Never guess at files that may not belong to us: the package
                # is installed, but without a before/after diff pybrew cannot
                # safely roll it back.  Nothing is deleted here.
                raise CaskError(
                    f"{cask.token}: package installation completed but pybrew could "
                    f"not record the installed package IDs; no files were removed "
                    f"and the receipt was not written. {error}"
                ) from error
            for pkg_id in sorted(after - before):
                if pkg_id not in self._system_pkg_ids:
                    self._system_pkg_ids.append(pkg_id)
            self._system_pkg_tracked = True
        self._system_pkg_used = True

    # -- system-wide pkg tracking / uninstall helpers --------------------
    @staticmethod
    def _pkgutil_executable(*, required: bool = True) -> Optional[str]:
        configured = os.environ.get("PYBREW_PKGUTIL")
        candidate = os.path.expanduser(configured) if configured else "/usr/sbin/pkgutil"
        executable = shutil.which(candidate)
        if executable is not None:
            return executable
        if configured or required:
            raise CaskError(
                f"pkgutil executable not found or not executable: {candidate}; "
                "set PYBREW_PKGUTIL to the macOS pkgutil tool"
            )
        return None

    @staticmethod
    def _run_pkgutil(
        token: str,
        executable: str,
        args: list[str],
        action: str,
        *,
        timeout: int = _PKGUTIL_TIMEOUT,
        allow_missing_receipt: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [executable, *args], text=True, capture_output=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as error:
            raise CaskError(f"cask {token}: pkgutil {action} timed out") from error
        except OSError as error:
            raise CaskError(
                f"cask {token}: could not run pkgutil {executable}: {error}"
            ) from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if allow_missing_receipt and "no receipt" in detail.lower():
                return result
            raise CaskError(
                f"cask {token}: pkgutil {action} failed "
                f"(exit {result.returncode}): {detail or 'no output'}"
            )
        return result

    def _pkgutil_list_ids(self, token: str, executable: str) -> set[str]:
        result = self._run_pkgutil(
            token, executable, ["--pkgs"], "list installed package receipts"
        )
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def _pkgutil_install_location(
        self, token: str, executable: str, pkg_id: str
    ) -> Path:
        """Return the directory ``pkgutil --files`` paths are relative to."""

        result = self._run_pkgutil(
            token,
            executable,
            ["--pkg-info", pkg_id],
            f"read package info for {pkg_id}",
        )
        volume = "/"
        location = ""
        for line in result.stdout.splitlines():
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "volume" and value:
                volume = value
            elif key == "location":
                location = value
        return Path(volume) / location.lstrip("/")

    def _system_pkg_files(self, token: str, ids: Iterable[str]) -> list[Path]:
        """Best-effort list of the files owned by ``ids``.

        Upgrade cleanup uses this to avoid deleting an old package path that
        the new installation also owns (a moved payload, or a new system
        package that overwrites the same files).  Receipts that cannot be read
        are skipped: the package-ID comparison still protects shared IDs.
        """

        pkg_ids = list(ids)
        if not pkg_ids:
            return []
        try:
            executable = self._pkgutil_executable(required=True)
            roots = self._system_pkg_roots()
        except CaskError:
            return []
        files: list[Path] = []
        seen: set[str] = set()
        for pkg_id in pkg_ids:
            try:
                base = self._pkgutil_install_location(token, executable, pkg_id)
                result = self._run_pkgutil(
                    token,
                    executable,
                    ["--files", pkg_id],
                    f"list files for package {pkg_id}",
                )
            except CaskError:
                continue
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                raw = Path(line)
                candidate = raw if raw.is_absolute() else base / raw
                try:
                    validated = self._validate_system_pkg_path(candidate, roots)
                except CaskError:
                    continue
                key = str(validated)
                if key not in seen:
                    seen.add(key)
                    files.append(validated)
        return files

    @staticmethod
    def _system_pkg_roots() -> list[Path]:
        configured = os.environ.get("PYBREW_SYSTEM_PKG_ROOTS")
        values = (
            configured.split(":")
            if configured is not None
            else list(DEFAULT_SYSTEM_PKG_ROOTS)
        )
        roots: list[Path] = []
        for value in values:
            value = value.strip()
            if not value:
                continue
            path = Path(os.path.expanduser(value))
            if not path.is_absolute():
                raise CaskError(f"system pkg root must be an absolute path: {value!r}")
            resolved = path.resolve()
            if resolved == Path("/"):
                raise CaskError("refusing to use / as an allowed system pkg root")
            if resolved not in roots:
                roots.append(resolved)
        if not roots:
            raise CaskError("no allowed system pkg roots configured")
        return roots

    @staticmethod
    def _validate_system_pkg_path(path: Path, roots: list[Path]) -> Path:
        normalized = Path(os.path.normpath(str(path)))
        if not normalized.is_absolute():
            raise CaskError(f"refusing to remove non-absolute pkg path: {path}")
        try:
            if normalized.is_symlink():
                # Removing a symlink does not touch its target, so validate the
                # directory that contains it rather than the link target.
                probe = normalized.parent.resolve() / normalized.name
            else:
                probe = normalized.resolve()
        except (OSError, RuntimeError) as error:
            raise CaskError(
                f"refusing to remove unresolvable pkg path {normalized}: {error}"
            ) from error
        if probe == Path("/"):
            raise CaskError(f"refusing to remove unsafe pkg path: {normalized}")
        for root in roots:
            if probe == root:
                raise CaskError(
                    f"refusing to remove allowed system pkg root itself: {normalized}"
                )
            if root in probe.parents:
                return normalized
        allowed = ", ".join(str(root) for root in roots)
        raise CaskError(
            f"refusing to remove pkg path outside allowed roots: {normalized} "
            f"(allowed roots: {allowed})"
        )

    @staticmethod
    def _system_pkg_ids_from_receipt(token: str, receipt: dict[str, Any]) -> list[str]:
        if "system_pkg_ids" not in receipt:
            raise CaskError(
                f"cask {token}: system pkg receipt has no recorded package IDs; "
                "cannot safely remove system files; remove them manually"
            )
        raw = receipt.get("system_pkg_ids")
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise CaskError(f"cask {token}: invalid system_pkg_ids in receipt")
        ids: list[str] = []
        for value in raw:
            if not isinstance(value, str) or not value.strip():
                raise CaskError(
                    f"cask {token}: invalid system pkg id in receipt: {value!r}"
                )
            pkg_id = value.strip()
            if pkg_id.startswith("-") or any(
                character.isspace() or ord(character) < 32 for character in pkg_id
            ):
                raise CaskError(
                    f"cask {token}: refusing unsafe system pkg id: {value!r}"
                )
            if pkg_id not in ids:
                ids.append(pkg_id)
        return ids

    def _plan_system_pkg_uninstall(
        self, token: str, receipt: dict[str, Any]
    ) -> list[tuple[str, list[Path]]]:
        """List and validate every path to remove before deleting anything."""

        ids = self._system_pkg_ids_from_receipt(token, receipt)
        if not ids:
            if receipt.get("system_pkg_tracked") is False:
                raise CaskError(
                    f"cask {token}: system pkg receipt was written without pkgutil "
                    "tracking; cannot safely remove system files; remove them manually"
                )
            return []
        executable = self._pkgutil_executable(required=True)
        roots = self._system_pkg_roots()
        plan: list[tuple[str, list[Path]]] = []
        for pkg_id in ids:
            base = self._pkgutil_install_location(token, executable, pkg_id)
            result = self._run_pkgutil(
                token,
                executable,
                ["--files", pkg_id],
                f"list files for package {pkg_id}",
            )
            paths: list[Path] = []
            seen: set[str] = set()
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                raw = Path(line)
                candidate = raw if raw.is_absolute() else base / raw
                validated = self._validate_system_pkg_path(candidate, roots)
                key = str(validated)
                if key not in seen:
                    seen.add(key)
                    paths.append(validated)
            plan.append((pkg_id, paths))
        return plan

    def _execute_system_pkg_uninstall(
        self,
        token: str,
        plan: list[tuple[str, list[Path]]],
        shared_paths: Iterable[Path] = (),
        shared_pkg_files: Iterable[Path] = (),
    ) -> None:
        if not plan:
            return
        shared = list(shared_paths)
        pkg_files = list(shared_pkg_files)
        for _pkg_id, paths in plan:
            # Deepest paths first so package-owned directories can be removed
            # with rmdir once they are empty.  Shared directories are left in
            # place rather than recursively deleted.  Paths owned by the new
            # installation are never removed during an upgrade.
            for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
                if shared and self._path_shared_with_receipt(path, shared):
                    continue
                if pkg_files and self._path_shared_with_pkg_files(path, pkg_files):
                    continue
                self._remove_system_pkg_path(token, path)
        executable = self._pkgutil_executable(required=True)
        for pkg_id, _paths in plan:
            self._run_pkgutil(
                token,
                executable,
                ["--forget", pkg_id],
                f"forget package {pkg_id}",
                allow_missing_receipt=True,
            )

    @staticmethod
    def _remove_system_pkg_path(token: str, path: Path) -> None:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    # A directory shared with other packages is not empty;
                    # never recurse into it.
                    pass
        except FileNotFoundError:
            pass
        except OSError as error:
            raise CaskError(
                f"cask {token}: could not remove system pkg file {path}: {error}"
            ) from error

    @staticmethod
    def _find(root: Path, name: str) -> Optional[Path]:
        direct = root / name
        if direct.exists():
            return direct
        matches = sorted(root.rglob(name))
        return matches[0] if matches else None

    def _remove_room(self, room: Path) -> None:
        receipt = room / ".metadata" / "receipt.json"
        if receipt.is_file():
            try:
                data = __import__("json").loads(receipt.read_text())
                for path_str in data.get("installed", []):
                    self._remove_path(Path(path_str))
            except Exception:
                pass
        self._remove_path(room)

    @staticmethod
    def _remove_path(path: Path) -> None:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise CaskError(f"could not remove {path}: {error}") from error

    # -- outdated / upgrade ---------------------------------------------
    def find_outdated(self, loader: CaskLoader) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for installed in self.list_installed():
            token = installed["token"]
            try:
                cask = loader.load(token)
            except Exception:
                continue
            current = cask.resolved_version()
            versions = self.installed_versions(token)
            latest = versions[-1] if versions else installed["version"]
            if current and current != latest:
                rows.append({"token": token, "installed": latest, "current": current})
        return rows

    def upgrade(self, cask: Cask) -> CaskInstallResult:
        """Install the current cask version and clean up superseded artifacts.

        Receipts are the source of truth for installed paths.  Paths recorded
        by old versions are removed unless the new receipt also owns the same
        path (or an overlapping parent/child path, which can happen when a
        bundle payload moves); those belong to the new installation.

        For system-wide pkg casks the previous receipts' ``system_pkg_ids``
        are read before the new version is installed.  Old package files are
        only removed after the new install succeeds, are validated with the
        same pkgutil/allowed-root checks as uninstall, and the corresponding
        package receipts are forgotten.  If the new install fails after the
        macOS installer was invoked, package IDs that did not exist before the
        attempt are rolled back on a best-effort basis: files that predate the
        upgrade are never removed.
        """

        token = cask.token
        new_version = cask.resolved_version()
        old_versions = self.installed_versions(token)

        # Read the old receipts before installing: a same-version force
        # reinstall removes the old room, and a failed install must not
        # destroy the information about the previous installation.
        old_rooms: list[Path] = []
        old_system_receipts: dict[Path, dict[str, Any]] = {}
        for version in old_versions:
            old_room = self.config.caskroom / token / version
            receipt = self._read_receipt(old_room)
            if version == new_version:
                # install(force=True) replaces this room in place.
                continue
            old_rooms.append(old_room)
            if self._is_system_pkg_receipt(receipt):
                old_system_receipts[old_room] = receipt

        # Validate the old system pkg receipts and plan every removal before
        # touching the new version.  A non-root or unsafe upgrade therefore
        # fails before anything is installed or removed.
        old_system_plans: dict[Path, list[tuple[str, list[Path]]]] = {}
        old_system_ids: set[str] = set()
        if old_system_receipts:
            has_ids = any(
                self._system_pkg_ids_from_receipt(token, receipt)
                for receipt in old_system_receipts.values()
            )
            if has_ids and os.geteuid() != 0:
                raise CaskError(
                    f"cask {token}: upgrading a system pkg cask requires root to "
                    "remove the previous version's system files; rerun with sudo"
                )
            for room, receipt in old_system_receipts.items():
                plan = self._plan_system_pkg_uninstall(token, receipt)
                old_system_plans[room] = plan
                old_system_ids.update(pkg_id for pkg_id, _paths in plan)

        try:
            result = self.install(cask, force=True)
        except Exception as error:
            if not self._system_pkg_attempted:
                raise
            protected = self._upgrade_protected_paths(old_rooms, old_system_plans)
            rollback = self._rollback_failed_system_pkg_upgrade(
                token, protected_paths=protected
            )
            message = str(error)
            if rollback:
                message = f"{message}\n{rollback}"
            raise CaskError(message) from error
        if result.skipped:
            return result

        new_receipt = self._read_receipt(result.room)
        new_ids: set[str] = set()
        if self._is_system_pkg_receipt(new_receipt):
            new_ids = set(self._system_pkg_ids_from_receipt(token, new_receipt))
            if new_receipt.get("system_pkg_tracked") is False and any(
                plan for plan in old_system_plans.values()
            ):
                raise CaskError(
                    f"cask {token}: the new system pkg install could not be tracked "
                    "by pkgutil; the previous version's system files were left in "
                    "place and its caskroom entry was kept"
                )
            if (
                not new_ids
                and old_system_ids
                and new_receipt.get("system_pkg_tracked") is not False
            ):
                # The installer added no new receipts, which means it replaced
                # packages that already existed (the usual same-ID upgrade).
                # Those IDs now belong to the new version: carry them forward
                # instead of deleting the files that were just installed.
                carried = sorted(old_system_ids)
                pkgutil = self._pkgutil_executable(required=False)
                if pkgutil is not None:
                    try:
                        installed = self._pkgutil_list_ids(token, pkgutil)
                    except CaskError:
                        installed = None
                    if installed is not None:
                        carried = sorted(old_system_ids & installed)
                if carried:
                    new_receipt["system_pkg_ids"] = carried
                    (result.room / ".metadata" / "receipt.json").write_text(
                        json.dumps(new_receipt, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    new_ids = set(carried)
        # Paths owned by the new installation.  Receipt paths share
        # parent/child overlaps (a moved bundle payload).  Package file lists
        # also contain container directories, so an old file is only shared
        # when the new package owns that exact path (or a path below it);
        # owning ``/usr/local/bin`` must not protect an unrelated old binary.
        new_paths = self._installed_paths(result.room)
        new_pkg_files = self._system_pkg_files(token, new_ids)
        for old_room in old_rooms:
            plan = old_system_plans.get(old_room)
            if plan is not None:
                filtered = [
                    (pkg_id, paths)
                    for pkg_id, paths in plan
                    if pkg_id not in new_ids
                ]
                self._execute_system_pkg_uninstall(
                    token,
                    filtered,
                    shared_paths=new_paths,
                    shared_pkg_files=new_pkg_files,
                )
            else:
                for path in self._installed_paths(old_room):
                    if self._path_shared_with_receipt(path, new_paths):
                        continue
                    if self._path_shared_with_pkg_files(path, new_pkg_files):
                        continue
                    if path.exists() or path.is_symlink():
                        self._remove_path(path)
            self._remove_path(old_room)
        return result

    def _upgrade_protected_paths(
        self,
        old_rooms: Iterable[Path],
        old_system_plans: dict[Path, list[tuple[str, list[Path]]]],
    ) -> list[Path]:
        """Paths that existed before the upgrade attempt.

        A failed upgrade must never remove these, even when the new package
        happens to list the same path.  Only exact old paths (and old
        directories that still contain new files) are protected, so a new
        package writing a fresh file into a pre-existing directory is still
        rolled back.
        """

        protected: list[Path] = []
        seen: set[str] = set()
        for room in old_rooms:
            paths = self._installed_paths(room)
            for _pkg_id, pkg_paths in old_system_plans.get(room, []):
                paths.extend(pkg_paths)
            for path in paths:
                key = str(path)
                if key not in seen:
                    seen.add(key)
                    protected.append(path)
        return protected

    def _rollback_failed_system_pkg_upgrade(
        self,
        token: str,
        protected_paths: Iterable[Path] = (),
    ) -> str:
        """Best-effort rollback of packages installed by a failed upgrade.

        Only package IDs that did not exist before the installer ran are
        touched, and paths recorded by the previous installation are never
        removed.  The returned summary is appended to the re-raised error;
        this helper never raises.
        """

        before = self._system_pkg_before_ids
        if before is None:
            return (
                "rollback unavailable: pybrew could not snapshot the installed "
                "system package IDs before the upgrade; no system files were removed"
            )
        if os.geteuid() != 0:
            return (
                "rollback unavailable: removing system package files requires root; "
                "no system files were removed"
            )
        pkgutil = self._pkgutil_executable(required=False)
        if pkgutil is None:
            return (
                "rollback unavailable: pkgutil was not found; no system files "
                "were removed"
            )
        try:
            after: Optional[set[str]] = self._pkgutil_list_ids(token, pkgutil)
        except CaskError:
            after = None
        if after is not None:
            new_ids = sorted(after - before)
        else:
            # The after snapshot failed; fall back to IDs the install had
            # already recorded before it failed (if any).
            new_ids = sorted(set(self._system_pkg_ids))
        if not new_ids:
            return (
                "rollback: no newly installed system packages were identified; "
                "no system files were removed"
            )
        protected = list(protected_paths)
        removed: list[str] = []
        failures: list[str] = []
        for pkg_id in new_ids:
            receipt = {"system_pkg_ids": [pkg_id], "system_pkg_tracked": True}
            try:
                plan = self._plan_system_pkg_uninstall(token, receipt)
                self._execute_system_pkg_uninstall(
                    token, plan, shared_pkg_files=protected
                )
                removed.append(pkg_id)
            except CaskError as error:
                failures.append(f"{pkg_id}: {error}")
        if failures:
            return (
                "rollback incomplete: removed "
                f"{', '.join(removed) or 'none'}; failed to roll back "
                + "; ".join(failures)
            )
        return (
            "rollback removed newly installed system package(s): "
            + ", ".join(removed)
        )

    @staticmethod
    def _installed_paths(room: Path) -> list[Path]:
        receipt = room / ".metadata" / "receipt.json"
        if not receipt.is_file():
            return []
        try:
            data = __import__("json").loads(receipt.read_text())
        except Exception:
            return []
        return [Path(str(path)) for path in data.get("installed", [])]

    @staticmethod
    def _path_shared_with_receipt(path: Path, receipt_paths: Iterable[Path]) -> bool:
        for other in receipt_paths:
            if path == other or path in other.parents or other in path.parents:
                return True
        return False

    @staticmethod
    def _path_shared_with_pkg_files(path: Path, pkg_files: Iterable[Path]) -> bool:
        """Return true when a new package owns ``path`` or files below it.

        Unlike receipt paths, pkgutil file lists include container
        directories, so a new package owning ``/usr/local/bin`` does not make
        every old file below it "shared".  The old path itself (or an old
        directory that still contains new package files) is protected.
        """

        for other in pkg_files:
            if path == other or path in other.parents:
                return True
        return False

    # -- uninstall -------------------------------------------------------
    def _run_uninstall_actions(self, cask: Cask) -> None:
        """Execute the safe subset of cask uninstall actions.

        ``quit`` apps are asked to quit via osascript; ``delete`` paths are
        removed after checking they are inside the user home/prefix/temp roots.
        Unsupported actions (launchctl, pkgutil, scripts, login items) are
        deliberately not executed.
        """

        if not cask.uninstall:
            return
        quit_value = cask.uninstall.get("quit")
        if quit_value and sys.platform == "darwin":
            apps = quit_value if isinstance(quit_value, list) else [quit_value]
            osascript = os.environ.get("PYBREW_OSASCRIPT", "/usr/bin/osascript")
            for app in apps:
                try:
                    subprocess.run(
                        [osascript, "-e", f'tell application id "{app}" to quit'],
                        text=True,
                        capture_output=True,
                        timeout=15,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
        pkgutil_value = cask.uninstall.get("pkgutil")
        if pkgutil_value and sys.platform == "darwin":
            receipts = pkgutil_value if isinstance(pkgutil_value, list) else [pkgutil_value]
            executable = os.environ.get("PYBREW_PKGUTIL", "pkgutil")
            for receipt in receipts:
                try:
                    subprocess.run(
                        [executable, "--forget", str(receipt)],
                        text=True,
                        capture_output=True,
                        timeout=30,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
        launchctl_value = cask.uninstall.get("launchctl")
        if launchctl_value and sys.platform == "darwin":
            labels = launchctl_value if isinstance(launchctl_value, list) else [launchctl_value]
            executable = os.environ.get("PYBREW_LAUNCHCTL", "launchctl")
            for label in labels:
                try:
                    result = subprocess.run(
                        [executable, "bootout", f"gui/{os.getuid()}/{label}"],
                        text=True,
                        capture_output=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        subprocess.run(
                            [executable, "remove", str(label)],
                            text=True,
                            capture_output=True,
                            timeout=30,
                        )
                except (OSError, subprocess.TimeoutExpired):
                    pass
        login_item_value = cask.uninstall.get("login_item")
        if login_item_value and sys.platform == "darwin":
            items = login_item_value if isinstance(login_item_value, list) else [login_item_value]
            executable = os.environ.get("PYBREW_OSASCRIPT", "/usr/bin/osascript")
            for item in items:
                try:
                    subprocess.run(
                        [
                            executable,
                            "-e",
                            f'tell application "System Events" to delete login item "{item}"',
                        ],
                        text=True,
                        capture_output=True,
                        timeout=30,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
        signal_value = cask.uninstall.get("signal")
        if signal_value:
            values = signal_value if isinstance(signal_value, list) else [signal_value]
            if len(values) >= 2:
                signal_name, target = str(values[0]), str(values[1])
                executable = os.environ.get("PYBREW_PKILL", "pkill")
                try:
                    subprocess.run(
                        [executable, f"-{signal_name}", "-f", target],
                        text=True,
                        capture_output=True,
                        timeout=30,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
        delete_value = cask.uninstall.get("delete")
        if delete_value:
            paths = delete_value if isinstance(delete_value, list) else [delete_value]
            for pattern in paths:
                for match in glob.glob(os.path.expanduser(str(pattern))):
                    self._remove_user_path(Path(match))

    def _remove_user_path(self, path: Path) -> None:
        resolved = path.resolve()
        home = Path.home().resolve()
        prefix = self.config.prefix.resolve()
        temp = Path(tempfile.gettempdir()).resolve()
        if resolved in (Path("/"), home, prefix):
            raise CaskError(f"refusing to delete unsafe path: {path}")
        if not (
            home in resolved.parents
            or prefix in resolved.parents
            or temp in resolved.parents
        ):
            raise CaskError(f"refusing to delete path outside allowed roots: {path}")
        self._remove_path(path)

    @staticmethod
    def _read_receipt(room: Path) -> dict[str, Any]:
        receipt = room / ".metadata" / "receipt.json"
        if not receipt.is_file():
            return {}
        try:
            data = json.loads(receipt.read_text())
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _is_system_pkg_receipt(receipt: dict[str, Any]) -> bool:
        return bool(receipt.get("system_pkg")) or "system_pkg_ids" in receipt

    def uninstall(
        self,
        token: str,
        version: Optional[str] = None,
        zap: bool = False,
        cask: Optional[Cask] = None,
        run_scripts: bool = False,
    ) -> list[Path]:
        rack = self.config.caskroom / token
        rooms: list[tuple[Path, dict[str, Any]]] = []
        if rack.is_dir():
            versions = (
                [version]
                if version
                else sorted(path.name for path in rack.iterdir() if path.is_dir())
            )
            for item in versions:
                room = rack / item
                if room.is_dir():
                    rooms.append((room, self._read_receipt(room)))
        # Plan system-wide pkg removal up front: root is required and every
        # path is validated before *anything* is deleted (including cask
        # uninstall actions).  A non-root caller therefore fails before any
        # removal, as does an unsafe path.
        plans: dict[Path, list[tuple[str, list[Path]]]] = {}
        for room, receipt in rooms:
            if not self._is_system_pkg_receipt(receipt):
                continue
            if os.geteuid() != 0:
                raise CaskError(
                    f"cask {token}: uninstalling a system pkg cask requires root; "
                    "rerun with sudo"
                )
            plans[room] = self._plan_system_pkg_uninstall(token, receipt)
        if not rack.is_dir():
            raise CaskError(f"cask {token} is not installed")
        script_cwd = rooms[0][0] if rooms else rack
        previous_run_scripts = self.run_scripts
        if run_scripts:
            self.run_scripts = True
        try:
            if cask is not None:
                self._run_cask_script(
                    cask, "uninstall_preflight", script_cwd, script_cwd.name, cwd=script_cwd
                )
                self._run_uninstall_actions(cask)
        finally:
            if run_scripts:
                self.run_scripts = previous_run_scripts
        removed: list[Path] = []
        for room, receipt in rooms:
            if room in plans:
                self._execute_system_pkg_uninstall(token, plans[room])
            installed = receipt.get("installed") or []
            if isinstance(installed, list):
                for path_str in installed:
                    path = Path(str(path_str))
                    if path.exists() or path.is_symlink():
                        self._remove_path(path)
            self._remove_path(room)
            removed.append(room)
        try:
            if not any(rack.iterdir()):
                rack.rmdir()
        except OSError:
            pass
        if cask is not None and run_scripts:
            previous_run_scripts = self.run_scripts
            self.run_scripts = True
            try:
                self._run_cask_script(
                    cask,
                    "uninstall_postflight",
                    rack,
                    version or "",
                    cwd=rack if rack.is_dir() else self.config.prefix,
                )
            finally:
                self.run_scripts = previous_run_scripts
        if zap and cask is not None:
            self.zap(cask)
        return removed

    def zap(self, cask: Cask) -> list[Path]:
        removed: list[Path] = []
        for pattern in cask.zap:
            expanded = os.path.expanduser(pattern)
            for match in glob.glob(expanded):
                path = Path(match)
                resolved = path.resolve()
                home = Path.home().resolve()
                prefix = self.config.prefix.resolve()
                if resolved in (Path("/"), home, prefix):
                    raise CaskError(f"refusing to zap unsafe path: {path}")
                if not (
                    home in resolved.parents
                    or prefix in resolved.parents
                    or str(resolved).startswith(str(Path(tempfile.gettempdir()).resolve()))
                ):
                    raise CaskError(f"refusing to zap path outside allowed roots: {path}")
                self._remove_path(path)
                removed.append(path)
        return removed
