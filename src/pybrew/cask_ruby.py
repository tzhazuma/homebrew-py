"""Opt-in evaluation of arbitrary Ruby cask definitions.

pybrew's default cask parser in :mod:`pybrew.cask` understands a small,
declarative subset of the Homebrew cask DSL and never executes Ruby.  Some
casks need the real thing: local variables, loops, conditionals and string
interpolation inside the cask block.  This module is the explicit escape hatch.

Nothing here runs unless ``PYBREW_ALLOW_CASK_RUBY`` is set to ``1``::

    PYBREW_ALLOW_CASK_RUBY=1 python3 -c \
        'from pybrew.cask_ruby import evaluate_cask_file; ...'

Evaluating a cask executes arbitrary code from the tap, exactly like Homebrew
does, so only enable this for casks you trust.  The evaluator itself performs no
network access and no installation work; it only describes the cask.

The cask source is evaluated by the host Ruby interpreter (``/usr/bin/ruby`` or
the ``PYBREW_RUBY`` override) together with a compatibility shim.  The shim
defines ``cask "token" do ... end`` and records the supported DSL calls into
JSON, which Python converts into :class:`pybrew.cask.Cask` and
:class:`pybrew.cask.Artifact` objects.

Captured DSL (in addition to arbitrary Ruby control flow):

* ``version``, ``sha256`` (string or ``arm:``/``intel:`` map), ``url``,
  ``name``, ``desc``, ``homepage``, ``caveats`` (literal string);
* artifact stanzas (``app``, ``binary``, ``suite``, ``pkg``, ``font``, ...);
* ``stage_only``, ``auto_updates``, ``uninstall``, ``zap trash:``,
  ``depends_on``, ``conflicts_with``, ``arch``/``arch_map``, ``language``;
* ``on_macos``/``on_linux``/``on_arm``/``on_intel`` and the
  ``on_<macos release>`` blocks (with ``:or_older``/``:or_newer``);
* the ``on_arch_conditional``/``on_system_conditional`` helpers used by older
  casks, and the ``os`` stanza used by newer multi-OS casks.

Semantics worth knowing:

* The source runs once, top to bottom, so values used in ``url`` interpolation
  (``#{version}``, ``#{arch}``, ...) must be declared before the stanza that
  references them.  This matches how Homebrew evaluates a cask body.
* ``language`` blocks are evaluated lazily like Homebrew's ``language_eval``:
  the default block is applied when the ``language`` helper is first read or
  at the end of the cask block.
* Cask *scripts* (``preflight``, ``postflight``, ``uninstall_preflight``,
  ``uninstall_postflight`` and a ``caveats do ... end`` block) are deliberately
  not executed or captured.  They are recorded in
  ``cask.extra["ruby_unsupported"]`` with a warning in
  ``cask.extra["ruby_warnings"]`` instead of breaking evaluation.  Stanzas the
  pybrew cask model does not model (``container``, ``rename``, completion
  artifacts, ...) are recorded the same way.
* Host platform checks can be simulated with ``PYBREW_TARGET_OS`` and
  ``PYBREW_TARGET_ARCH`` (the same overrides used elsewhere in pybrew);
  ``HOMEBREW_MACOS_VERSION`` overrides the macOS release for
  ``on_<release>`` blocks.

Failures (missing Ruby, a Ruby exception, a syntax error, no ``cask``
declaration, unreadable JSON) raise :class:`pybrew.cask.CaskError` with the
Ruby stderr and, when Ruby reports it, the failing source line.
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

from .cask import Artifact, Cask, CaskError, _ARTIFACT_KINDS

#: Environment variable that opts a caller into arbitrary-Ruby cask parsing.
ALLOW_ENV = "PYBREW_ALLOW_CASK_RUBY"

#: Environment variable overriding the Ruby interpreter used for evaluation.
RUBY_ENV = "PYBREW_RUBY"

#: System Ruby on macOS (also present on most Linux distributions).
DEFAULT_RUBY = "/usr/bin/ruby"

#: Upper bound for evaluating one cask definition.
RUBY_TIMEOUT = 120.0

#: Artifact stanzas converted into :class:`pybrew.cask.Artifact` objects.
#: ``_ARTIFACT_KINDS`` is the canonical list used by the default parser.
ARTIFACT_KINDS = frozenset(_ARTIFACT_KINDS)

#: Homebrew spells the AppImage stanza ``app_image``; pybrew's model uses
#: ``appimage``.  Map the DSL method name to the model's artifact kind.
ARTIFACT_ALIASES = {"app_image": "appimage"}


def cask_ruby_enabled() -> bool:
    """Return true when the opt-in arbitrary-Ruby cask evaluator was requested."""

    return os.environ.get(ALLOW_ENV, "") == "1"


def evaluate_cask_source(
    source: str, path: Optional[Path] = None, tap: Optional[str] = None
) -> Cask:
    """Evaluate arbitrary Ruby ``source`` and return a :class:`Cask`.

    The caller must opt in with ``PYBREW_ALLOW_CASK_RUBY=1``; otherwise a
    :class:`CaskError` explains how to enable the evaluator.  ``path`` is used
    for Ruby backtraces and stored on the returned cask; it does not have to
    exist.  Any Ruby failure is reported as :class:`CaskError`.
    """

    _require_enabled()
    if not isinstance(source, str):
        raise CaskError("cask source must be a string")
    cask_path = Path(path) if path is not None else None
    data = _evaluate(source, cask_path)
    return _cask_from_result(data, cask_path, tap)


def evaluate_cask_file(path: str | Path, tap: Optional[str] = None) -> Cask:
    """Read ``path`` and evaluate it as arbitrary Ruby cask source."""

    _require_enabled()
    cask_path = Path(path)
    source = cask_path.read_text(encoding="utf-8", errors="replace")
    return evaluate_cask_source(source, cask_path, tap)


# ---------------------------------------------------------------------------
# host description
# ---------------------------------------------------------------------------


def _require_enabled() -> None:
    if not cask_ruby_enabled():
        raise CaskError(
            "arbitrary Ruby cask evaluation is disabled; "
            f"set {ALLOW_ENV}=1 to enable it"
        )


def _label(path: Optional[Path]) -> str:
    return str(path) if path is not None else "cask"


def _ruby_binary() -> str:
    candidate = os.path.expanduser(
        os.fspath(os.environ.get(RUBY_ENV) or DEFAULT_RUBY)
    )
    resolved = shutil.which(candidate)
    if resolved is None and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        resolved = candidate
    if resolved is None:
        raise CaskError(
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


def _target_macos_version() -> str:
    """Return the macOS release used by ``on_<release>`` blocks."""

    override = (os.environ.get("HOMEBREW_MACOS_VERSION") or "").strip()
    if override:
        return override
    if _target_os() == "darwin":
        return platform.mac_ver()[0] or ""
    return ""


# ---------------------------------------------------------------------------
# Ruby bridge
# ---------------------------------------------------------------------------


def _render_script(source: str, config: dict[str, Any]) -> str:
    encoded_config = base64.b64encode(
        json.dumps(config, sort_keys=True).encode("utf-8")
    ).decode("ascii")
    encoded_source = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return _RUBY_SHIM.replace("__PYBREW_CASK_CONFIG__", encoded_config).replace(
        "__PYBREW_CASK_SOURCE__", encoded_source
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
        # Generated shim frames are noise; keep the cask line numbers.
        output = output.replace(str(script_path), "ruby-cask-shim.rb")
    tail = "\n".join(output.splitlines()[-40:])
    heading = (
        f"{label}: arbitrary Ruby cask evaluation failed "
        f"(exit {completed.returncode})"
    )
    if details:
        heading = f"{heading} {details[0]}"
    return heading + "\n" + "\n".join(details[1:] + [tail])


def _evaluate(source: str, path: Optional[Path]) -> dict[str, Any]:
    label = _label(path)
    ruby = _ruby_binary()
    filename = str(path) if path is not None else "cask.rb"
    root = Path(tempfile.mkdtemp(prefix="pybrew-cask-ruby-"))
    try:
        script_path = root / "cask_evaluator.rb"
        output_path = root / "cask.json"
        artifacts = {kind: kind for kind in sorted(ARTIFACT_KINDS)}
        artifacts.update(ARTIFACT_ALIASES)
        config: dict[str, Any] = {
            "appdir": str(Path.home() / "Applications"),
            "arch": _target_arch(),
            "artifacts": artifacts,
            "filename": filename,
            "macos_version": _target_macos_version(),
            "output": str(output_path),
            "platform": _target_os(),
        }
        script_path.write_text(_render_script(source, config), encoding="utf-8")
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
            raise CaskError(
                f"{label}: arbitrary Ruby cask evaluation timed out "
                f"after {RUBY_TIMEOUT:g}s"
            ) from error
        except OSError as error:
            raise CaskError(
                f"{label}: could not run the Ruby cask evaluator: {error}"
            ) from error

        if completed.returncode != 0:
            raise CaskError(
                _failure_message(label, filename, source, completed, script_path)
            )

        try:
            raw = output_path.read_text(encoding="utf-8")
        except OSError as error:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            details = stderr or stdout or "(Ruby produced no output)"
            raise CaskError(
                f"{label}: Ruby cask evaluator did not produce a result: "
                f"{error}\n{details}"
            ) from error
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            stderr = (completed.stderr or "").strip()
            message = (
                f"{label}: Ruby cask evaluator returned invalid JSON: {error}"
            )
            if stderr:
                message += f"\n{stderr}"
            raise CaskError(message) from error
        if not isinstance(data, dict):
            raise CaskError(
                f"{label}: Ruby cask evaluator returned an unexpected result"
            )
        return data
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# JSON -> Cask conversion
# ---------------------------------------------------------------------------


def _optional_text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _string_map(value: Any, allowed: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        text = str(key)
        if text in allowed:
            result[text] = str(item)
    return result


def _cask_from_result(
    data: dict[str, Any], path: Optional[Path], tap: Optional[str]
) -> Cask:
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise CaskError("Ruby cask evaluator returned a cask without a token")

    cask = Cask(token=token, path=path, tap=tap)
    cask.version = _optional_text(data.get("version"))
    cask.sha256 = _optional_text(data.get("sha256"))
    cask.sha256_map = _string_map(data.get("sha256_map"), ("arm", "intel"))
    cask.url = _optional_text(data.get("url"))
    cask.desc = _optional_text(data.get("desc"))
    cask.homepage = _optional_text(data.get("homepage"))
    cask.caveats = _optional_text(data.get("caveats"))
    cask.language = _optional_text(data.get("language"))
    cask.arch = _optional_text(data.get("arch"))
    cask.arch_map = _string_map(data.get("arch_map"), ("arm", "intel"))
    cask.auto_updates = bool(data.get("auto_updates"))
    cask.stage_only = bool(data.get("stage_only"))
    cask.deprecated = bool(data.get("deprecated"))

    names = data.get("names") or []
    if isinstance(names, list):
        rendered = [str(item) for item in names if str(item)]
        if rendered:
            cask.name = ", ".join(rendered)

    artifacts = data.get("artifacts") or []
    if isinstance(artifacts, list):
        for entry in artifacts:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if not kind:
                continue
            kwargs = entry.get("kwargs")
            cask.artifacts.append(
                Artifact(
                    kind=str(kind),
                    source="" if entry.get("source") is None else str(entry["source"]),
                    target=_optional_text(entry.get("target")),
                    kwargs=dict(kwargs) if isinstance(kwargs, dict) else {},
                )
            )

    deprecation_reason = data.get("deprecation_reason")
    if deprecation_reason is not None and str(deprecation_reason).strip():
        cask.deprecation_reason = str(deprecation_reason)

    no_autobump_reason = data.get("no_autobump_reason")
    if no_autobump_reason is not None and str(no_autobump_reason).strip():
        cask.no_autobump_reason = str(no_autobump_reason)
    if data.get("no_autobump"):
        cask.no_autobump = True

    disabled = data.get("disabled")
    if disabled:
        cask.disabled = True
        cask.disable_reason = str(data.get("disable_reason") or "") or None

    os_map = data.get("os_map")
    if isinstance(os_map, dict):
        for key, value in os_map.items():
            if str(value).strip():
                cask.os[str(key)] = str(value)
    os_value = data.get("os")
    if os_value is not None and str(os_value).strip():
        platform_key = "macos" if sys.platform.startswith("darwin") else "linux"
        cask.os[platform_key] = str(os_value)

    container = data.get("container")
    if container is not None and str(container).strip():
        cask.container = str(container).strip()

    renames = data.get("renames") or []
    if isinstance(renames, list):
        for entry in renames:
            if isinstance(entry, list) and len(entry) >= 2:
                cask.renames.append((str(entry[0]), str(entry[1])))

    uninstall = data.get("uninstall") or {}
    if isinstance(uninstall, dict):
        cask.uninstall.update({str(key): value for key, value in uninstall.items()})
    uninstall_args = data.get("uninstall_args") or []
    if isinstance(uninstall_args, list) and uninstall_args:
        cask.uninstall["_args"] = uninstall_args

    zap = data.get("zap") or []
    if isinstance(zap, list):
        cask.zap.extend(str(item) for item in zap)

    depends_on = data.get("depends_on") or {}
    if isinstance(depends_on, dict):
        cask.depends_on.update({str(key): value for key, value in depends_on.items()})
    conflicts = data.get("conflicts_with") or {}
    if isinstance(conflicts, dict):
        cask.conflicts_with.update(
            {str(key): value for key, value in conflicts.items()}
        )

    warnings = [str(item) for item in data.get("warnings") or [] if str(item).strip()]
    if warnings:
        cask.extra["ruby_warnings"] = warnings
    unsupported = [
        entry for entry in data.get("unsupported") or [] if isinstance(entry, dict)
    ]
    if unsupported:
        cask.extra["ruby_unsupported"] = unsupported
    cask.extra["ruby_evaluated"] = True
    return cask


# The Ruby compatibility shim.  It must stay Ruby 2.6 compatible because that
# is the interpreter shipped by macOS (and therefore the most likely one).
# Configuration and source are base64 encoded so that quotes, heredocs and
# non-ASCII text in a cask cannot corrupt the generated script.
_RUBY_SHIM = r'''
# frozen_string_literal: true

require "base64"
require "json"
require "pathname"

PYBREW_CASK_CONFIG = JSON.parse(Base64.strict_decode64("__PYBREW_CASK_CONFIG__"))
PYBREW_CASK_SOURCE = Base64.strict_decode64("__PYBREW_CASK_SOURCE__")

class PybrewCaskError < StandardError; end

# Homebrew's cask version object is a String with a few helpers; cask bodies
# commonly interpolate ``version.major_minor`` into URLs.
class PybrewCaskVersion < String
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

  def dots_to_underscores
    tr(".", "_")
  end

  def dots_to_hyphens
    tr(".", "-")
  end
end

module PybrewCaskJSON
  module_function

  # Convert Ruby DSL values into JSON-friendly data.  Symbols keep their colon
  # (matching the default parser's ``:symbol`` strings) and Pathnames become
  # their string form.
  def value(object)
    case object
    when Symbol
      ":#{object}"
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

  # Split a DSL call into positional arguments and a trailing keyword hash.
  # Ruby 2.6 and Ruby 3 both hand ``foo(a: 1)`` to ``def foo(*args)`` as a
  # trailing Hash, so this works on every supported interpreter.
  def split_args(args)
    if args.last.is_a?(Hash)
      [args[0...-1], value(args.last)]
    else
      [args, {}]
    end
  end
end

module PybrewCaskRuntime
  class << self
    attr_accessor :result
  end
end

module MacOS
  def self.version
    PybrewCaskVersion.new((PYBREW_CASK_CONFIG["macos_version"] || "0").to_s)
  end
end

class PybrewCaskDSL
  MACOS_RELEASES = {
    "big_sur" => 11,
    "monterey" => 12,
    "ventura" => 13,
    "sonoma" => 14,
    "sequoia" => 15,
    "tahoe" => 26,
    "golden_gate" => 27,
  }.freeze

  attr_reader :data

  def initialize(token)
    @token = token.to_s
    @version = nil
    @sha256 = nil
    @sha256_map = {}
    @arch_map = {}
    @arch_value = nil
    @os_map = {}
    @os_value = nil
    @language_blocks = {}
    @default_language_key = nil
    @language_value = nil
    @data = {
      "token" => @token,
      "version" => nil,
      "sha256" => nil,
      "sha256_map" => {},
      "url" => nil,
      "names" => [],
      "desc" => nil,
      "homepage" => nil,
      "caveats" => nil,
      "artifacts" => [],
      "renames" => [],
      "uninstall" => {},
      "uninstall_args" => [],
      "zap" => [],
      "depends_on" => {},
      "conflicts_with" => {},
      "auto_updates" => false,
      "stage_only" => false,
      "container" => nil,
      "language" => nil,
      "arch" => nil,
      "arch_map" => {},
      "deprecated" => false,
      "deprecation_reason" => nil,
      "disabled" => false,
      "os_map" => {},
      "no_autobump" => false,
      "on_system_blocks" => false,
      "no_autobump_reason" => nil,
      "disable_reason" => nil,
      "warnings" => [],
      "unsupported" => [],
    }
  end

  # -- host description -------------------------------------------------

  def host_platform
    (PYBREW_CASK_CONFIG["platform"] || "").to_s
  end

  def host_arch
    (PYBREW_CASK_CONFIG["arch"] || "").to_s
  end

  def host_arch_key
    host_arch == "arm64" ? "arm" : "intel"
  end

  def macos_major
    (PYBREW_CASK_CONFIG["macos_version"] || 0).to_i
  end

  def token
    @token
  end

  def caskroom_path
    Pathname.new(ENV.fetch("HOMEBREW_PREFIX", "/usr/local"))/"Caskroom"/token.to_s
  end

  def staged_path
    value = version
    caskroom_path/(value.nil? ? "0" : value.to_s)
  end

  def os_version
    MacOS.version
  end

  def appdir
    @appdir ||= Pathname.new((PYBREW_CASK_CONFIG["appdir"] || "").to_s)
  end

  # -- scalar stanzas ---------------------------------------------------

  def version(value = nil)
    return @version if value.nil?

    @version = PybrewCaskVersion.new(value.to_s)
    @data["version"] = @version.to_s
    @version
  end

  def sha256(*args)
    positional, kwargs = PybrewCaskJSON.split_args(args)
    return @sha256 if positional.empty? && kwargs.empty?

    if kwargs.empty?
      @sha256 = PybrewCaskJSON.value(positional[0]).to_s
      @data["sha256"] = @sha256
      return @sha256
    end

    kwargs.each do |key, value|
      next unless %w[arm intel].include?(key.to_s)

      @sha256_map[key.to_s] = value.to_s
    end
    @data["sha256_map"] = @sha256_map
    mapped = @sha256_map[host_arch_key]
    unless mapped.nil?
      @sha256 = mapped
      @data["sha256"] = mapped
    end
    @sha256
  end

  def url(*args)
    positional, kwargs = PybrewCaskJSON.split_args(args)
    return @data["url"] if positional.empty? && kwargs.empty?

    unless positional.empty?
      @data["url"] = PybrewCaskJSON.value(positional[0]).to_s
    end
    @data["url"]
  end

  def name(*args)
    return @data["names"] if args.empty?

    args.flatten.each { |value| @data["names"] << value.to_s }
    @data["names"]
  end

  def desc(value = nil)
    return @data["desc"] if value.nil?

    @data["desc"] = value.to_s
  end

  def homepage(value = nil, *_args)
    return @data["homepage"] if value.nil?

    @data["homepage"] = value.to_s
  end

  def auto_updates(value = nil)
    return @data["auto_updates"] if value.nil?

    @data["auto_updates"] = value ? true : false
  end

  def stage_only(value = nil)
    return @data["stage_only"] if value.nil?

    @data["stage_only"] = !(value == false || value.to_s.strip.downcase == "false")
  end

  def caveats(*args, &block)
    if block
      record_unsupported(
        "caveats", [], true,
        "cask `caveats` block is not captured by the opt-in Ruby evaluator; " \
        "only literal caveats strings are preserved"
      )
      return nil
    end
    positional, = PybrewCaskJSON.split_args(args)
    unless positional.empty?
      @data["caveats"] = positional.map(&:to_s).join("\n")
    end
    @data["caveats"]
  end

  # -- arch / os helpers ------------------------------------------------

  def arch(*args)
    positional, kwargs = PybrewCaskJSON.split_args(args)
    return @arch_value if positional.empty? && kwargs.empty?

    kwargs.each do |key, value|
      next unless %w[arm intel].include?(key.to_s)

      @arch_map[key.to_s] = value.to_s
    end
    unless @arch_map.empty?
      @arch_value = @arch_map[host_arch_key]
      @data["arch_map"] = @arch_map
      if @arch_map.key?("arm") && @arch_map.key?("intel")
        @data["arch"] = host_arch == "arm64" ? "arm64" : "intel"
      end
    end
    @arch_value
  end

  def on_arch_conditional(*args)
    _positional, kwargs = PybrewCaskJSON.split_args(args)
    host_arch == "arm64" ? kwargs["arm"] : kwargs["intel"]
  end

  def os(*args)
    _positional, kwargs = PybrewCaskJSON.split_args(args)
    return @os_value if kwargs.empty?

    kwargs.each { |key, value| @os_map[key.to_s] = value.to_s }
    @os_value = @os_map[host_platform == "darwin" ? "macos" : "linux"]
    @data["os"] = @os_value
    @data["os_map"] = @os_map
    @os_value
  end

  def on_system_conditional(*args)
    _positional, kwargs = PybrewCaskJSON.split_args(args)
    host_platform == "darwin" ? kwargs["macos"] : kwargs["linux"]
  end

  # -- platform blocks --------------------------------------------------

  def on_macos(&block)
    @data["on_system_blocks"] = true
    evaluate_block(block) if host_platform == "darwin"
  end

  def on_linux(&block)
    @data["on_system_blocks"] = true
    evaluate_block(block) if host_platform == "linux"
  end

  def on_arm(&block)
    evaluate_block(block) if host_arch == "arm64"
  end
  alias on_arm64 on_arm

  def on_intel(&block)
    evaluate_block(block) unless host_arch == "arm64"
  end
  alias on_x86_64 on_intel

  MACOS_RELEASES.each do |release, major|
    define_method("on_#{release}") do |*args, &block|
      positional, = PybrewCaskJSON.split_args(args)
      condition = positional.empty? ? nil : positional[0]
      if host_platform == "darwin"
        active = case condition.to_s
                 when "or_older" then macos_major <= major
                 when "or_newer" then macos_major >= major
                 else macos_major == major
                 end
        evaluate_block(block) if active
      end
      nil
    end
  end

  # -- language ---------------------------------------------------------

  def language(*args, &block)
    return language_eval if args.empty?

    positional, kwargs = PybrewCaskJSON.split_args(args)
    raise PybrewCaskError, "language stanza requires a block" if block.nil?

    code = positional.empty? ? "" : positional[0].to_s
    @language_blocks[code] = block
    if kwargs["default"] && @default_language_key.nil?
      @default_language_key = code
    end
    nil
  end

  # Evaluate the default language block once, mirroring Homebrew's lazy
  # ``language_eval``.  Without an explicit ``default: true`` block the first
  # declared language is used so a single-language cask still works.
  def languages
    @language_blocks.keys
  end

  def language_groups
    @language_blocks.keys.map { |key| [key] }
  end

  def default_language_group
    key = @default_language_key || @language_blocks.keys.first
    key.nil? ? nil : [key]
  end

  def language_eval
    return @language_value unless @language_value.nil?
    return nil if @language_blocks.empty?

    key = @default_language_key || @language_blocks.keys.first
    @language_value = key
    @data["language"] = key
    block = @language_blocks[key]
    instance_eval(&block) if block
    @language_value
  end

  # -- artifacts --------------------------------------------------------

  def record_artifact(kind, args)
    positional, kwargs = PybrewCaskJSON.split_args(args)
    source = positional.empty? ? "" : PybrewCaskJSON.value(positional[0]).to_s
    target = kwargs["target"]
    @data["artifacts"] << {
      "kind" => kind.to_s,
      "source" => source,
      "target" => target.nil? ? nil : target.to_s,
      "kwargs" => kwargs,
    }
    nil
  end

  PYBREW_CASK_CONFIG["artifacts"].each do |method_name, kind|
    define_method(method_name.to_sym) do |*args|
      record_artifact(kind.to_s, args)
    end
  end

  def container(*args)
    _positional, kwargs = PybrewCaskJSON.split_args(args)
    raw_type = kwargs["type"] || kwargs[:type]
    @data["container"] = raw_type.to_s.sub(/\A:/, "") unless raw_type.nil?
    @data["container"]
  end

  # -- uninstall / zap / dependencies -----------------------------------

  def rename(*args)
    positional, kwargs = PybrewCaskJSON.split_args(args)
    if positional.length == 2 && kwargs.empty?
      @data["renames"] << positional.map(&:to_s)
    else
      record_unsupported("rename", positional, false, "rename expects two paths")
    end
    @data["renames"]
  end

  def uninstall(*args)
    positional, kwargs = PybrewCaskJSON.split_args(args)
    kwargs.each { |key, value| @data["uninstall"][key.to_s] = value }
    @data["uninstall_args"] = PybrewCaskJSON.value(positional) unless positional.empty?
    @data["uninstall"]
  end

  def zap(*args)
    positional, kwargs = PybrewCaskJSON.split_args(args)
    trash = kwargs.delete("trash")
    unless trash.nil?
      if trash.is_a?(Array)
        @data["zap"].concat(trash.map(&:to_s))
      else
        @data["zap"] << trash.to_s
      end
    end
    unless kwargs.empty?
      record_unsupported(
        "zap", [kwargs], false,
        "cask `zap` options other than `trash` are not supported by pybrew"
      )
    end
    record_unsupported("zap", positional) unless positional.empty?
    @data["zap"]
  end

  def depends_on(*args)
    positional, kwargs = PybrewCaskJSON.split_args(args)
    kwargs.each { |key, value| @data["depends_on"][key.to_s] = value }
    positional.each do |argument|
      @data["depends_on"][argument.to_s.sub(/\A:/, "")] = true
    end
    @data["depends_on"]
  end

  def conflicts_with(*args)
    _positional, kwargs = PybrewCaskJSON.split_args(args)
    kwargs.each { |key, value| @data["conflicts_with"][key.to_s] = value }
    @data["conflicts_with"]
  end

  # -- metadata-only stanzas --------------------------------------------

  def deprecate!(*args)
    _positional, kwargs = PybrewCaskJSON.split_args(args)
    @data["deprecated"] = true
    reason = kwargs["because"] || kwargs[:because]
    @data["deprecation_reason"] = reason.to_s unless reason.nil?
    nil
  end

  def disable!(*args)
    _positional, kwargs = PybrewCaskJSON.split_args(args)
    @data["disabled"] = true
    @data["disable_reason"] = (kwargs["because"] || kwargs[:because]).to_s unless (kwargs["because"] || kwargs[:because]).nil?
    nil
  end

  def no_autobump!(*args)
    _positional, kwargs = PybrewCaskJSON.split_args(args)
    @data["no_autobump"] = true
    reason = kwargs["because"] || kwargs[:because]
    @data["no_autobump_reason"] = reason.to_s unless reason.nil?
    nil
  end

  def autobump?
    !@data["no_autobump"]
  end

  def no_autobump_defined?
    !!@data["no_autobump"]
  end

  def on_system_blocks_exist?
    !!@data["on_system_blocks"]
  end

  def on_os_blocks_exist?
    !(@data["os_map"] || {}).empty?
  end

  def depends_on_set_in_block?
    !(@data["depends_on"] || {}).empty?
  end

  def caveats_object
    @data["caveats"]
  end

  def livecheck(*_args, &_block)
    nil
  end

  # -- cask scripts and unknown stanzas ---------------------------------

  %w[preflight postflight uninstall_preflight uninstall_postflight].each do |name|
    define_method(name.to_sym) do |*_args, &block|
      @data["unsupported"] << {
        "stanza" => name,
        "args" => [],
        "kwargs" => {},
        "block" => !block.nil?,
      }
      @data["warnings"] << "cask `#{name}` script is not captured by the " \
                            "opt-in Ruby evaluator; cask install/uninstall " \
                            "scripts are not available"
      nil
    end
  end

  def record_unsupported(name, args, has_block = false, message = nil)
    positional, kwargs = PybrewCaskJSON.split_args(args)
    @data["unsupported"] << {
      "stanza" => name.to_s,
      "args" => PybrewCaskJSON.value(positional),
      "kwargs" => kwargs,
      "block" => has_block,
    }
    @data["warnings"] << (message || "cask stanza `#{name}` is not captured " \
                                     "by pybrew's cask model")
    nil
  end

  def evaluate_block(block)
    return nil if block.nil?

    instance_eval(&block)
    nil
  end

  # Unknown stanzas are recorded instead of failing so that casks using newer
  # Homebrew features still yield the fields pybrew understands.
  def method_missing(name, *args, &block)
    record_unsupported(name, args, !block.nil?)
  end
end

def cask(token, &block)
  unless PybrewCaskRuntime.result.nil?
    raise PybrewCaskError, "multiple `cask` declarations are not supported"
  end
  raise PybrewCaskError, "cask #{token.inspect} requires a block" if block.nil?

  collector = PybrewCaskDSL.new(token)
  collector.instance_eval(&block)
  collector.language_eval
  PybrewCaskRuntime.result = collector.data
  nil
end

begin
  eval(
    PYBREW_CASK_SOURCE,
    TOPLEVEL_BINDING,
    (PYBREW_CASK_CONFIG["filename"] || "cask.rb").to_s
  )
  if PybrewCaskRuntime.result.nil?
    raise PybrewCaskError, "no `cask \"token\" do ... end` declaration found"
  end

  File.open((PYBREW_CASK_CONFIG["output"] || "cask.json").to_s, "wb") do |file|
    file.write(JSON.generate(PybrewCaskRuntime.result))
  end
rescue PybrewCaskError => error
  $stderr.puts "pybrew-cask: #{error.message}"
  exit 2
end
'''


#: Script blocks captured by the default literal parser.  The Ruby evaluator
#: deliberately does not execute or capture block bodies, so callers that want
#: both arbitrary-Ruby fields and scripts can merge these raw bodies in.
CAPTURED_SCRIPT_NAMES = (
    "preflight",
    "postflight",
    "uninstall_preflight",
    "uninstall_postflight",
)


def merge_captured_scripts(evaluated: Cask, parsed: Cask) -> Cask:
    """Copy inert script/caveats bodies from a literal parse into ``evaluated``.

    The arbitrary-Ruby evaluator evaluates fields but cannot safely recover the
    raw source of block bodies.  ``CaskLoader`` uses this helper to combine the
    evaluator's result with the default parser's captured script bodies, so
    opting into Ruby does not silently lose preflight/postflight hooks.
    """

    merged: list[str] = []
    for name in CAPTURED_SCRIPT_NAMES:
        body = getattr(parsed, name, None)
        if body and not getattr(evaluated, name, None):
            setattr(evaluated, name, body)
            merged.append(name)
    if parsed.caveats_source and not evaluated.caveats_source:
        evaluated.caveats_source = parsed.caveats_source
        merged.append("caveats_source")
    if parsed.caveats and not evaluated.caveats:
        evaluated.caveats = parsed.caveats
    if merged:
        evaluated.extra.setdefault("ruby_merged_scripts", []).extend(merged)
    return evaluated
