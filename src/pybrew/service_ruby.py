"""Opt-in evaluation of arbitrary Ruby ``service do ... end`` blocks.

pybrew's default service parser in :mod:`pybrew.services` understands a small,
declarative subset of Homebrew's service DSL and never executes Ruby.  Some
formulae build the service definition with local variables, conditionals,
loops or string interpolation, which the literal parser cannot express.

This module is the explicit escape hatch.  Nothing here runs unless
``PYBREW_ALLOW_SERVICE_RUBY`` is set to exactly ``1``::

    PYBREW_ALLOW_SERVICE_RUBY=1 python3 -c \
        'from pybrew.service_ruby import evaluate_service_block; ...'

Evaluating a service block executes arbitrary code from the tap, exactly like
Homebrew does, so only enable this for formulae you trust.  The evaluator
performs no network access and no installation work; it only describes the
service.

The captured block is evaluated by the host Ruby interpreter (``/usr/bin/ruby``
or the ``PYBREW_RUBY`` override) together with a Ruby 2.6 compatible shim.
The shim implements the service DSL and records the supported directives into
JSON, which Python converts into a :class:`pybrew.services.ServiceSpec`.

Captured DSL (in addition to arbitrary Ruby control flow):

* ``run`` (string, array, or the ``macos:``/``linux:`` platform form),
  ``working_dir``, ``root_dir``, ``input_path``,
  ``log_path``, ``error_log_path``, ``keep_alive`` (boolean or hash),
  ``run_at_load``, ``interval``, ``process_type``, ``environment_variables``,
  ``require_root``, ``throttle_interval``, ``sockets``, ``macos_legacy_timers``
  and ``launch_only_once``;
* Homebrew's formula path helpers: ``opt_bin``/``opt_prefix`` are the stable
  ``opt`` paths, ``bin``/``sbin``/``lib``/``include``/``share``/``libexec``
  live under the (versioned) keg prefix, ``etc``/``var`` are
  ``HOMEBREW_PREFIX`` subdirectories and ``prefix`` follows Homebrew's
  ``Formula#prefix`` (the ``opt_prefix`` once the keg is opt-linked);
* ``std_service_path_env`` with Homebrew's service ``PATH`` value and a
  ``formula`` view (``name``/``version``);
* the Homebrew ``Service`` helpers ``path_dir``, ``path_parent_dir``,
  ``path_dirs`` and ``manual_command`` (see :class:`PybrewServiceDSL`);
* ``version`` (with ``major``/``minor``/``major_minor``) for interpolation;
* ``on_macos``/``on_linux``/``on_arm``/``on_intel`` (and the ``on_arm64``/
  ``on_x86_64`` aliases) platform blocks.

When the configuration needed for Homebrew paths is unavailable (``config`` is
``None`` or lacks a ``prefix``) the evaluator keeps working and the path
helpers fall back to pybrew's literal-parser paths (the formula ``opt``
prefix); with no configuration at all they resolve to empty paths.

``PYBREW_SERVICE_PATH_MODE`` opts both service parsers into the same path
mode: ``homebrew`` makes the literal parser use Homebrew's formula paths and
``pybrew``/``literal``/``legacy`` makes this evaluator match the literal
parser (including its reduced ``std_service_path_env``).  With the variable
unset the literal parser keeps pybrew's paths and this evaluator keeps
Homebrew's paths, so existing behaviour is preserved.

User environment overrides are read from
``$HOMEBREW_USER_CONFIG_HOME/services/<formula>.env`` (or an explicit
``user_config_home`` argument) with bounded Homebrew semantics: files for root
services and world- or group-writable files are skipped, comments/blank/invalid
lines are ignored, and the remaining ``KEY=value`` pairs override the
formula-declared environment.  The overrides and the resolved config directory
are stored separately from the declared ``environment_variables`` (which stays
intact); renderers with formula context re-read the file at render time so later
edits/removals are honoured, while the stored values remain the standalone
snapshot.  No service manager is invoked.

Host platform checks can be simulated with ``PYBREW_TARGET_OS`` and
``PYBREW_TARGET_ARCH`` (the same overrides used by the opt-in cask evaluator).

Failures (missing Ruby, a Ruby exception, a syntax error, a service block that
is absent or produces invalid JSON) raise :class:`pybrew.services.ServiceError`
with the Ruby stderr and, when Ruby reports it, the failing source line.
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
from typing import Any, Optional

from .models import Formula
from .paths import Config
from .services import (
    USER_CONFIG_HOME_ENV as USER_CONFIG_HOME_ENV,
    ServiceError,
    ServiceSpec,
    _apply_service_directive,
    read_user_service_env,
    resolve_user_config_home,
    service_path_mode,
    service_paths,
    std_service_path_env_value,
)

#: Environment variable that opts a caller into arbitrary-Ruby service parsing.
ALLOW_ENV = "PYBREW_ALLOW_SERVICE_RUBY"

#: Environment variable overriding the Ruby interpreter used for evaluation.
RUBY_ENV = "PYBREW_RUBY"

#: System Ruby on macOS (also present on most Linux distributions).
DEFAULT_RUBY = "/usr/bin/ruby"

#: Upper bound for evaluating one service block.
RUBY_TIMEOUT = 120.0

#: Directives copied from the shim's JSON result into the :class:`ServiceSpec`.
#: The order matches :class:`pybrew.services.ServiceSpec` and the default
#: parser's directive handling; values are normalised by
#: :func:`pybrew.services._apply_service_directive` so both parsers agree.
_SERVICE_FIELDS = (
    "run",
    "working_dir",
    "log_path",
    "error_log_path",
    "keep_alive",
    "run_at_load",
    "interval",
    "process_type",
    "run_type",
    "plist_name",
    "service_name",
    "environment_variables",
    "require_root",
    "throttle_interval",
    "launch_only_once",
    "macos_legacy_timers",
    "nice",
    "restart_delay",
    "stop_timeout",
    "root_dir",
    "input_path",
    "cron",
    "sockets",
)


def service_ruby_enabled() -> bool:
    """Return true when the opt-in arbitrary-Ruby service evaluator was requested."""

    return os.environ.get(ALLOW_ENV, "") == "1"


def evaluate_service_source(
    raw: str,
    formula: Formula,
    config: Config,
    user_config_home: Optional[Path] = None,
) -> ServiceSpec:
    """Evaluate arbitrary Ruby service source and return a :class:`ServiceSpec`.

    ``raw`` may be the captured ``do ... end`` block (the shape stored in
    ``formula.service["source"]``), a bare directive body, or a complete
    ``service do ... end`` declaration.  The caller must opt in with
    ``PYBREW_ALLOW_SERVICE_RUBY=1``; otherwise a
    :class:`pybrew.services.ServiceError` explains how to enable the evaluator.

    ``user_config_home`` overrides ``HOMEBREW_USER_CONFIG_HOME`` for one call;
    when it (or the environment variable) is set, a bounded
    ``services/<formula>.env`` override file is merged into the effective
    service environment (Homebrew semantics: user values win, untrusted files
    are skipped).  Ruby failures, including a missing interpreter, are reported
    as :class:`ServiceError` with the Ruby stderr and, when available, the
    failing source line.
    """

    _require_enabled()
    if not isinstance(raw, str):
        raise ServiceError("service source must be a string")
    source = _normalise_source(raw)
    context = resolve_user_config_home(user_config_home)
    user_environment = _read_user_service_env(formula, context)
    mode = service_path_mode("homebrew")
    paths = service_paths(formula, config, mode)
    std_env = std_service_path_env_value(config, mode)
    data = _evaluate(source, formula, paths, user_environment, std_env)
    return _spec_from_result(data, formula, user_environment, context)


def evaluate_service_block(
    formula: Formula,
    config: Config,
    user_config_home: Optional[Path] = None,
) -> ServiceSpec:
    """Evaluate ``formula``'s captured service block with Ruby."""

    _require_enabled()
    raw = (
        formula.service.get("source")
        if isinstance(formula.service, dict)
        else None
    )
    if not raw:
        raise ServiceError(f"{formula.name} does not define a service")
    return evaluate_service_source(raw, formula, config, user_config_home)


# ---------------------------------------------------------------------------
# host description and interpreter lookup
# ---------------------------------------------------------------------------


def _require_enabled() -> None:
    if not service_ruby_enabled():
        raise ServiceError(
            "arbitrary Ruby service evaluation is disabled; "
            f"set {ALLOW_ENV}=1 to enable it"
        )


def _ruby_binary() -> str:
    candidate = os.path.expanduser(
        os.fspath(os.environ.get(RUBY_ENV) or DEFAULT_RUBY)
    )
    resolved = shutil.which(candidate)
    if resolved is None and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        resolved = candidate
    if resolved is None:
        raise ServiceError(
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
    """Return ``arm64``/``intel`` for the host or a test override."""

    machine = (os.environ.get("PYBREW_TARGET_ARCH") or platform.machine()).lower()
    return "arm64" if machine in ("arm64", "aarch64") else "intel"


# ---------------------------------------------------------------------------
# source preparation
# ---------------------------------------------------------------------------


def _normalise_source(raw: str) -> str:
    """Turn any accepted ``raw`` shape into a complete ``service`` declaration."""

    text = raw.strip()
    if not text:
        raise ServiceError("service source is empty")
    if re.match(r"\Ado\b", text):
        # The default parser stores the block including its ``do``/``end``.
        return "service " + text
    if re.match(r"\Aservice\b", text):
        return text
    return "service do\n" + text + "\nend"


# ---------------------------------------------------------------------------
# user service environment overrides (shared with the literal parser)
# ---------------------------------------------------------------------------


def _user_config_home(user_config_home: Optional[Path]) -> Optional[Path]:
    """Backward-compatible alias for the shared config-home resolver."""

    return resolve_user_config_home(user_config_home)


def _read_user_service_env(
    formula: Formula,
    user_config_home: Optional[Path] = None,
    euid: Optional[int] = None,
) -> dict[str, str]:
    """Backward-compatible alias for the shared user env reader.

    The implementation lives in :mod:`pybrew.services` so the default literal
    parser applies exactly the same bounded/security rules as this evaluator.
    """

    return read_user_service_env(formula, user_config_home, euid)


# ---------------------------------------------------------------------------
# path map handed to the Ruby shim
# ---------------------------------------------------------------------------


def _path_mode() -> str:
    """Return the Ruby evaluator's path mode (Homebrew unless overridden)."""

    return service_path_mode("homebrew")


def _path_map(formula: Formula, config: Any) -> dict[str, str]:
    """Return the path map handed to the Ruby shim for the active mode."""

    return service_paths(formula, config, _path_mode())


def _std_service_path_env(config: Any) -> str:
    """Return ``std_service_path_env`` for the evaluator's active path mode."""

    return std_service_path_env_value(config, _path_mode())


# ---------------------------------------------------------------------------
# Ruby bridge
# ---------------------------------------------------------------------------


def _render_script(source: str, config: dict[str, Any]) -> str:
    encoded_config = base64.b64encode(
        json.dumps(config, sort_keys=True).encode("utf-8")
    ).decode("ascii")
    encoded_source = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return _RUBY_SHIM.replace("__PYBREW_SERVICE_CONFIG__", encoded_config).replace(
        "__PYBREW_SERVICE_SOURCE__", encoded_source
    )


def _failure_message(
    label: str,
    filename: str,
    source: str,
    completed: subprocess.CompletedProcess,
    script_path: Optional[Path] = None,
) -> str:
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    details: list[str] = []
    match = re.search(re.escape(filename) + r":(\d+)", stderr)
    if match is not None:
        line = int(match.group(1))
        details.append(f"at {filename} line {line}")
        source_lines = source.splitlines()
        if 1 <= line <= len(source_lines):
            details.append(f"  {source_lines[line - 1].strip()}")
    output = "\n".join(part for part in (stderr, stdout) if part)
    if not output:
        output = "(Ruby produced no output)"
    if script_path is not None:
        # Generated shim frames are noise; keep the service line numbers.
        output = output.replace(str(script_path), "ruby-service-shim.rb")
    tail = "\n".join(output.splitlines()[-40:])
    heading = (
        f"{label}: arbitrary Ruby service evaluation failed "
        f"(exit {completed.returncode})"
    )
    if details:
        heading = f"{heading} {details[0]}"
    return heading + "\n" + "\n".join(details[1:] + [tail])


def _evaluate(
    source: str,
    formula: Formula,
    paths: dict[str, str],
    user_environment: dict[str, str],
    std_service_path_env: str,
) -> dict[str, Any]:
    label = formula.name
    ruby = _ruby_binary()
    filename = f"{label}-service.rb"
    root = Path(tempfile.mkdtemp(prefix="pybrew-service-ruby-"))
    try:
        script_path = root / "service_evaluator.rb"
        output_path = root / "service.json"
        payload: dict[str, Any] = {
            "arch": _target_arch(),
            "filename": filename,
            "formula": {
                "name": formula.name,
                "version": formula.version or "HEAD",
            },
            "output": str(output_path),
            "paths": paths,
            "platform": _target_os(),
            "std_service_path_env": std_service_path_env,
            "user_environment": user_environment,
        }
        script_path.write_text(_render_script(source, payload), encoding="utf-8")
        try:
            completed = subprocess.run(
                [ruby, str(script_path)],
                cwd=str(root),
                env=dict(os.environ),
                stdin=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=RUBY_TIMEOUT,
            )
        except subprocess.TimeoutExpired as error:
            raise ServiceError(
                f"{label}: arbitrary Ruby service evaluation timed out "
                f"after {RUBY_TIMEOUT:g}s"
            ) from error
        except OSError as error:
            raise ServiceError(
                f"{label}: could not run the Ruby service evaluator: {error}"
            ) from error

        if completed.returncode != 0:
            raise ServiceError(
                _failure_message(label, filename, source, completed, script_path)
            )

        try:
            text = output_path.read_text(encoding="utf-8")
        except OSError as error:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            details = stderr or stdout or "(Ruby produced no output)"
            raise ServiceError(
                f"{label}: Ruby service evaluator did not produce a result: "
                f"{error}\n{details}"
            ) from error
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            stderr = (completed.stderr or "").strip()
            message = (
                f"{label}: Ruby service evaluator returned invalid JSON: {error}"
            )
            if stderr:
                message += f"\n{stderr}"
            raise ServiceError(message) from error
        if not isinstance(data, dict):
            raise ServiceError(
                f"{label}: Ruby service evaluator returned an unexpected result"
            )
        return data
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# JSON -> ServiceSpec conversion
# ---------------------------------------------------------------------------


def _spec_from_result(
    data: dict[str, Any],
    formula: Formula,
    user_environment: Optional[dict[str, str]] = None,
    user_config_home: Optional[Path] = None,
) -> ServiceSpec:
    """Convert the shim's JSON payload into a validated :class:`ServiceSpec`.

    The same helpers as the default literal parser normalise every field, so an
    invalid value fails with the same :class:`ServiceError` in both modes.
    Homebrew user env overrides are stored separately from the declared
    ``environment_variables`` and only applied by
    :meth:`ServiceSpec.effective_environment_variables` / the renderers, so the
    declared environment is never destructively merged.  The context directory
    is captured too so renderers can re-read the file instead of using this
    snapshot.
    """

    spec = ServiceSpec()
    for name in _SERVICE_FIELDS:
        if name not in data:
            continue
        value = data[name]
        if value is None:
            continue
        try:
            if name == "run_type" and str(value) == "immediate":
                # The shim always reports its default; keep pybrew's schedule
                # inference for interval/cron-only blocks instead of marking
                # "immediate" as an explicit caller choice.
                spec.run_type = "immediate"
                spec.run_type_explicit = False
                continue
            _apply_service_directive(spec, name, [value])
        except ServiceError as error:
            raise ServiceError(f"{formula.name}: {error}") from error
    if user_environment:
        spec.user_environment_variables = {
            str(key): str(value) for key, value in user_environment.items()
        }
    if user_config_home is not None:
        spec.user_config_home = str(user_config_home)
    spec.formula_name = str(formula.name)
    warnings = data.get("warnings")
    if isinstance(warnings, list):
        spec.warnings = [str(warning) for warning in warnings]
    return spec


# The Ruby compatibility shim.  It must stay Ruby 2.6 compatible because that
# is the interpreter shipped by macOS (and therefore the most likely one).
# Configuration and source are base64 encoded so that quotes, heredocs and
# non-ASCII text in a service block cannot corrupt the generated script.
_RUBY_SHIM = r'''
# frozen_string_literal: true

require "base64"
require "json"
require "pathname"
require "shellwords"

PYBREW_SERVICE_CONFIG = JSON.parse(Base64.strict_decode64("__PYBREW_SERVICE_CONFIG__"))
PYBREW_SERVICE_SOURCE = Base64.strict_decode64("__PYBREW_SERVICE_SOURCE__")

class PybrewServiceError < StandardError; end

# Shell quoting for ``manual_command``.  Mirrors Homebrew's
# ``Utils::Shell.sh_quote``: ``Shellwords.escape`` except that a leading tilde
# is left alone so the shell can expand it.
module PybrewServiceShell
  module_function

  def quote(value)
    Shellwords.escape(value.to_s).gsub("\\~", "~")
  end
end

# Homebrew's formula version object is a String with a few helpers; service
# blocks commonly interpolate ``version.major_minor`` into paths.
class PybrewServiceVersion < String
  def major
    split(".")[0].to_s
  end

  def minor
    split(".")[1].to_s
  end

  def patch
    split(".")[2].to_s
  end

  def major_minor
    split(".")[0, 2].join(".")
  end
end

module PybrewServiceJSON
  module_function

  # Convert Ruby DSL values into JSON-friendly data.  Symbols lose their colon
  # (matching the literal parser's service semantics) and Pathnames become
  # their string form.
  def value(object)
    case object
    when Symbol
      object.to_s
    when Pathname
      object.to_s
    when Hash
      result = {}
      object.each { |key, item| result[key.to_s] = value(item) }
      result
    when Array
      object.map { |item| value(item) }
    when String, Integer, Float, TrueClass, FalseClass, NilClass
      object
    else
      object.to_s
    end
  end
end

module PybrewServiceRuntime
  class << self
    attr_accessor :result
  end
end

PYBREW_SERVICE_PATHS = (PYBREW_SERVICE_CONFIG["paths"] || {})

HOMEBREW_PREFIX = Pathname.new((PYBREW_SERVICE_PATHS["prefix"] || "").to_s)

# A read-only view of the formula, so blocks can use ``formula.name`` and
# ``formula.version`` the way Homebrew's service helpers do.
class PybrewServiceFormula
  def initialize(dsl)
    @dsl = dsl
  end

  def name
    @dsl.formula_name
  end

  def version
    @dsl.version
  end

  def method_missing(method_name, *args, &block)
    @dsl.public_send(method_name, *args, &block)
  end

  def respond_to_missing?(method_name, include_private = false)
    @dsl.respond_to?(method_name, include_private) || super
  end
end

class PybrewServiceDSL
  # Directives whose value is a single scalar/path argument.
  SCALAR_FIELDS = %w[
    run_type
    working_dir log_path error_log_path interval process_type run_at_load
    require_root throttle_interval launch_only_once macos_legacy_timers nice
    restart_delay stop_timeout root_dir input_path
  ].freeze

  def initialize
    @config = PYBREW_SERVICE_CONFIG
    @version = nil
    @formula = nil
    @data = {
      "run" => [],
      "working_dir" => nil,
      "log_path" => nil,
      "error_log_path" => nil,
      "keep_alive" => false,
      "run_at_load" => true,
      "interval" => nil,
      "process_type" => nil,
      "run_type" => "immediate",
      "plist_name" => nil,
      "service_name" => nil,
      "environment_variables" => {},
      "require_root" => false,
      "throttle_interval" => nil,
      "launch_only_once" => false,
      "macos_legacy_timers" => false,
      "nice" => nil,
      "restart_delay" => nil,
      "stop_timeout" => nil,
      "root_dir" => nil,
      "input_path" => nil,
      "cron" => nil,
      "sockets" => {},
      "warnings" => []
    }
  end

  def data
    @data
  end

  # -- configuration ----------------------------------------------------

  def formula_name
    (@config["formula"] || {})["name"].to_s
  end

  def version
    return @version unless @version.nil?

    raw = (@config["formula"] || {})["version"].to_s
    @version = PybrewServiceVersion.new(raw.empty? ? "HEAD" : raw)
  end

  def formula
    @formula ||= PybrewServiceFormula.new(self)
  end

  def platform
    (@config["platform"] || "").to_s
  end

  def arch
    (@config["arch"] || "").to_s
  end

  # -- path helpers -----------------------------------------------------

  def path_for(name)
    Pathname.new((PYBREW_SERVICE_PATHS[name] || "").to_s)
  end

  def prefix
    path_for("prefix")
  end

  def opt_prefix
    path_for("opt_prefix")
  end

  def opt_bin
    path_for("opt_bin")
  end

  def bin
    path_for("bin")
  end

  def sbin
    path_for("sbin")
  end

  def lib
    path_for("lib")
  end

  def include
    path_for("include")
  end

  def share
    path_for("share")
  end

  def etc
    path_for("etc")
  end

  def libexec
    path_for("libexec")
  end

  def var
    path_for("var")
  end

  def std_service_path_env
    (@config["std_service_path_env"] || "").to_s
  end

  # -- Service-object compatibility helpers ------------------------------
  # These mirror Homebrew's Service helpers: command expands leading tildes,
  # path_dirs derives from the configured service paths and manual_command is
  # the shell command line a user would run manually.
  def command
    Array(@data["run"]).map(&:to_s).map do |argument|
      argument.start_with?("~") ? File.expand_path(argument) : argument
    end
  end

  def command?
    !command.empty?
  end

  def manual_command
    variables = effective_environment_variables.reject do |key, _value|
      key.to_s == "PATH"
    end
    parts = variables.map { |key, value| "#{key}=\"#{value}\"" }
    parts.concat(command.map { |argument| PybrewServiceShell.quote(argument) })
    parts.join(" ")
  end

  def keep_alive?
    !!@data["keep_alive"]
  end

  def effective_run_type
    run_type = (@data["run_type"] || "immediate").to_s
    return run_type unless run_type == "immediate"
    return "cron" if !@data["cron"].nil? && @data["cron"].to_s.strip != ""
    return "interval" unless @data["interval"].nil?

    "immediate"
  end

  def timed?
    %w[cron interval].include?(effective_run_type)
  end

  def launch_only_once
    !!@data["launch_only_once"]
  end

  def macos_legacy_timers
    !!@data["macos_legacy_timers"]
  end

  def require_root
    !!@data["require_root"]
  end

  def requires_root?
    require_root
  end

  def nice_requires_root?
    !@data["nice"].nil? && require_root
  end

  def default_service_name
    "homebrew.mxcl.#{formula_name}"
  end

  def default_plist_name
    "#{default_service_name}.plist"
  end

  # Homebrew's Service#path_dir expands an absolute (or ``~``) path and
  # ignores anything else; relative service paths cannot be resolved safely.
  def path_dir(path = nil)
    return nil if path.nil?

    text = path.to_s
    return nil if text.empty?
    return nil unless text.start_with?("/", "~")

    Pathname.new(File.expand_path(text))
  end

  def path_parent_dir(path = nil)
    directory = path_dir(path)
    directory.nil? ? nil : directory.dirname
  end

  # Homebrew's Service#path_dirs: the directories relevant to the configured
  # service paths, in declaration order, without duplicates.
  def path_dirs
    [
      path_dir(@data["working_dir"]),
      path_dir(@data["root_dir"]),
      path_parent_dir(@data["input_path"]),
      path_parent_dir(@data["log_path"]),
      path_parent_dir(@data["error_log_path"])
    ].compact.uniq
  end

  # The formula-declared environment, without user overrides (Homebrew's
  # ``Service#environment_variables`` getter).
  def declared_environment_variables
    (@data["environment_variables"] || {}).dup
  end

  # Bounded Homebrew parity: the formula-declared environment merged with the
  # user override file read by the Python side
  # (``HOMEBREW_USER_CONFIG_HOME/services/<formula>.env``); user values win.
  def effective_environment_variables
    environment = declared_environment_variables
    (@config["user_environment"] || {}).each do |key, value|
      environment[key.to_s] = value.to_s
    end
    environment
  end

  def default_cron_values
    { "Minute" => "*", "Hour" => "*", "Day" => "*", "Month" => "*", "Weekday" => "*" }
  end

  def parse_cron(cron_statement = nil)
    values = default_cron_values
    raw = cron_statement.nil? ? @data["cron"] : cron_statement
    if raw.is_a?(Hash)
      raw.each do |key, value|
        normalized = key.to_s.downcase
        normalized = normalized[0].upcase + normalized[1..]
        next unless values.key?(normalized)

        values[normalized] = value.to_s.strip == "*" ? "*" : value.to_i
      end
    elsif raw.is_a?(String) && !raw.strip.empty?
      text = raw.strip
      presets = {
        "@hourly" => { "Minute" => 0 },
        "@daily" => { "Minute" => 0, "Hour" => 0 },
        "@weekly" => { "Minute" => 0, "Hour" => 0, "Weekday" => 0 },
        "@monthly" => { "Minute" => 0, "Hour" => 0, "Day" => 1 },
        "@yearly" => { "Minute" => 0, "Hour" => 0, "Day" => 1, "Month" => 1 },
        "@annually" => { "Minute" => 0, "Hour" => 0, "Day" => 1, "Month" => 1 }
      }
      if presets.key?(text)
        values.merge!(presets[text])
      else
        parts = text.split
        raise PybrewServiceError, "cron expects a five-field expression" if parts.length != 5

        %w[Minute Hour Day Month Weekday].each_with_index do |key, index|
          part = parts[index]
          values[key] = part == "*" ? "*" : Integer(part)
        end
      end
    end
    values
  end

  def cron(value = nil)
    return parse_cron if value.nil?

    @data["cron"] = value.to_s
    parse_cron
  end

  def cron_weekday_to_systemd_weekday(weekday)
    mapping = { "0" => "Sun", "1" => "Mon", "2" => "Tue", "3" => "Wed",
                "4" => "Thu", "5" => "Fri", "6" => "Sat", "7" => "Sun" }
    mapping[weekday.to_s] || weekday.to_s
  end

  def self.from_hash(hash)
    dsl = new
    hash.each { |key, value| dsl.data[key.to_s] = value }
    dsl
  end

  def to_plist
    payload = {
      "Label" => default_service_name,
      "ProgramArguments" => command
    }
    environment = effective_environment_variables
    payload["EnvironmentVariables"] = environment unless environment.empty?
    payload
  end

  def to_systemd_unit
    "[Service]\nExecStart=#{command.join(" ")}\n"
  end

  def to_systemd_timer
    "[Timer]\nOnCalendar=*-*-* 00:00:00\n"
  end

  def f
    self
  end

  # -- directives -------------------------------------------------------

  def run(*args, **kwargs)
    unless args.empty?
      value = args.length == 1 ? args[0] : args
      @data["run"] = PybrewServiceJSON.value(value)
      return @data["run"]
    end
    unless kwargs.empty?
      # Homebrew's platform form: `run macos: "...", linux: "..."`.
      platform = (@config["platform"] || "").to_s
      selected = kwargs[platform == "darwin" ? :macos : :linux]
      @data["run"] = selected.nil? ? [] : PybrewServiceJSON.value(selected)
      return @data["run"]
    end
    @data["run"]
  end

  def keep_alive(*args)
    return @data["keep_alive"] if args.empty?

    value = args.length == 1 ? args[0] : args
    @data["keep_alive"] = PybrewServiceJSON.value(value)
  end

  def environment_variables(*args)
    return @data["environment_variables"] if args.empty?

    value = args.length == 1 ? args[0] : args
    value = {} if value.nil?
    @data["environment_variables"] = PybrewServiceJSON.value(value)
  end

  def name(*args)
    return @data["plist_name"] || @data["service_name"] if args.empty?

    value = args.length == 1 ? args[0] : args
    hash = PybrewServiceJSON.value(value)
    if hash.is_a?(Hash)
      @data["plist_name"] = hash["macos"] if hash.key?("macos")
      @data["service_name"] = hash["linux"] if hash.key?("linux")
    end
    nil
  end

  def sockets(*args)
    return @data["sockets"] if args.empty?

    value = args.length == 1 ? args[0] : args
    @data["sockets"] = PybrewServiceJSON.value(value)
  end

  SCALAR_FIELDS.each do |field|
    define_method(field) do |*args|
      if args.empty?
        @data[field]
      else
        @data[field] = PybrewServiceJSON.value(args[0])
      end
    end
  end

  # -- platform blocks --------------------------------------------------

  def on_macos(&block)
    evaluate_block(block) if platform == "darwin"
  end

  def on_linux(&block)
    evaluate_block(block) if platform == "linux"
  end

  def on_arm(&block)
    evaluate_block(block) if arch == "arm64"
  end
  alias on_arm64 on_arm

  def on_intel(&block)
    evaluate_block(block) unless arch == "arm64"
  end
  alias on_x86_64 on_intel

  def evaluate_block(block)
    return nil if block.nil?

    instance_eval(&block)
    nil
  end

  # Unknown directives (nice, run_type, cron, ...) are intentionally ignored
  # so pybrew keeps reading formulae that use newer Homebrew DSL features.
  def method_missing(name, *_args, &_block)
    @data["warnings"] << "unsupported service directive `#{name}`"
    nil
  end

  def respond_to_missing?(_name, _include_private = false)
    false
  end
end

def service(*_args, &block)
  unless PybrewServiceRuntime.result.nil?
    raise PybrewServiceError, "multiple `service` declarations are not supported"
  end
  raise PybrewServiceError, "`service` requires a block" if block.nil?

  collector = PybrewServiceDSL.new
  collector.instance_eval(&block)
  PybrewServiceRuntime.result = collector.data
  nil
end

begin
  eval(
    PYBREW_SERVICE_SOURCE,
    TOPLEVEL_BINDING,
    (PYBREW_SERVICE_CONFIG["filename"] || "service.rb").to_s
  )
  if PybrewServiceRuntime.result.nil?
    raise PybrewServiceError, "no `service do ... end` block found"
  end

  File.open((PYBREW_SERVICE_CONFIG["output"] || "service.json").to_s, "wb") do |file|
    file.write(JSON.generate(PybrewServiceRuntime.result))
  end
rescue PybrewServiceError => error
  $stderr.puts "pybrew-service: #{error.message}"
  exit 2
end
'''
