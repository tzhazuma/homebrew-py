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
* ``livecheck`` metadata (``url`` + strategy options, ``regex``, ``strategy``,
  ``skip``, ``throttle``) plus a best-effort copy of the raw block source and
  the ``strategy do ... end`` block body;
* ``on_macos``/``on_linux``/``on_arm``/``on_intel`` and the
  ``on_<macos release>`` blocks (with ``:or_older``/``:or_newer``);
* the ``on_arch_conditional``/``on_system_conditional`` helpers used by older
  casks, and the ``os`` stanza used by newer multi-OS casks;
* the ``MacOS`` helpers ``version``/``full_version``/``version_symbol`` and
  ``sdk``/``sdk_path`` (best effort: local SDK directories only, no network).

Semantics worth knowing:

* The source runs once, top to bottom, so values used in ``url`` interpolation
  (``#{version}``, ``#{arch}``, ...) must be declared before the stanza that
  references them.  This matches how Homebrew evaluates a cask body.
* ``language`` blocks are evaluated lazily like Homebrew's ``language_eval``:
  the default block is applied when the ``language`` helper is first read or
  at the end of the cask block.
* Cask *scripts* (``preflight``, ``postflight``, ``uninstall_preflight``,
  ``uninstall_postflight``) are never executed by the evaluator.  Their raw
  block source is recovered best-effort from Ruby's ``source_location`` and
  stored on the cask, exactly like the default parser's inert script bodies;
  only ``CaskInstaller(run_scripts=True)`` runs them.  When recovery fails the
  cask keeps a warning in ``cask.extra["ruby_warnings"]`` instead of breaking
  evaluation.  A ``caveats do ... end`` block is evaluated for its output, and
  stanzas the pybrew cask model does not model (``container``, ``rename``,
  completion artifacts, ...) are recorded in ``cask.extra["ruby_unsupported"]``.
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

from .cask import Artifact, Cask, CaskError, CaskParser, _ARTIFACT_KINDS
from .errors import FormulaParseError

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

#: Cask script stanzas whose raw block bodies are inert data.  The evaluator
#: captures the source best-effort; only ``CaskInstaller(run_scripts=True)``
#: executes it.
CAPTURED_SCRIPT_NAMES = (
    "preflight",
    "postflight",
    "uninstall_preflight",
    "uninstall_postflight",
)


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


#: Directories searched for an installed macOS SDK.  pybrew only reads the
#: local filesystem: it never runs ``xcrun``/``xcodebuild`` and never touches
#: the network.  Tests monkeypatch this tuple to use synthetic SDK trees.
_MACOS_SDK_ROOTS = (
    Path("/Library/Developer/CommandLineTools/SDKs"),
    Path(
        "/Applications/Xcode.app/Contents/Developer/Platforms/"
        "MacOSX.platform/Developer/SDKs"
    ),
)

_MACOS_SDK_NAME = re.compile(r"MacOSX(\d+(?:\.\d+)*)\.sdk\Z")


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


def _macos_sdk_version_from_path(path: Path) -> Optional[str]:
    match = _MACOS_SDK_NAME.search(path.name)
    return match.group(1) if match is not None else None


def _macos_sdk_paths() -> dict[str, str]:
    """Return a best-effort ``version -> SDK path`` map for this host.

    ``PYBREW_MACOS_SDK_PATH`` overrides the search so callers (and tests) can
    point the shim at a synthetic SDK.  Otherwise only SDKs already installed
    under the well-known CLT/Xcode directories are reported.
    """

    override = (os.environ.get("PYBREW_MACOS_SDK_PATH") or "").strip()
    if override:
        path = Path(os.path.expanduser(override))
        version = _macos_sdk_version_from_path(path)
        if version is None:
            version = _target_macos_version() or "0"
        return {version: str(path)}
    if _target_os() != "darwin":
        return {}
    paths: dict[str, tuple[tuple[int, ...], Path]] = {}
    for root in _MACOS_SDK_ROOTS:
        try:
            entries = sorted(Path(root).glob("MacOSX*.sdk"))
        except OSError:  # pragma: no cover - unreadable SDK directory
            continue
        for entry in entries:
            version = _macos_sdk_version_from_path(entry)
            if version is None or not entry.is_dir():
                continue
            key = _version_key(version)
            existing = paths.get(version)
            if existing is None or key > existing[0]:
                paths[version] = (key, entry)
    return {version: str(entry) for version, (_key, entry) in paths.items()}


def _select_macos_sdk(
    paths: dict[str, str]
) -> tuple[Optional[str], Optional[str]]:
    """Pick the SDK matching the target macOS release, else the newest one."""

    if not paths:
        return None, None
    host_major = _version_key(_target_macos_version())[:1]
    matching = [
        version
        for version in paths
        if host_major and _version_key(version)[:1] == host_major
    ]
    version = max(matching or list(paths), key=_version_key)
    return version, paths[version]


# ---------------------------------------------------------------------------
# livecheck source recovery
# ---------------------------------------------------------------------------

_LIVECHECK_SOURCE_WARNING = (
    "cask `livecheck` block source could not be recovered by the opt-in Ruby "
    "evaluator; `livecheck_defined?` and the collected livecheck metadata are "
    "still available"
)

_LIVECHECK_STRATEGY_WARNING = (
    "cask `livecheck` strategy block source could not be recovered by the "
    "opt-in Ruby evaluator; the strategy symbol and other livecheck metadata "
    "are still available"
)

_SCRIPT_SOURCE_WARNING = (
    "cask `{stanza}` script source could not be recovered by the opt-in Ruby "
    "evaluator; the literal parser may still provide it when the cask is "
    "loaded through CaskLoader"
)


def _extract_stanza_source(
    source: str, location: Any, stanza: str
) -> Optional[str]:
    """Best-effort recovery of a ``stanza do ... end`` block body.

    The Ruby shim reports the block's ``source_location``; this helper maps the
    reported line back onto the original source with the same lexer the default
    parser uses, then reuses its block scanner.  Ruby constructs the lexer does
    not understand (for example method definitions inside the block) can defeat
    the scan, in which case ``None`` is returned and the caller records a
    warning instead of failing evaluation.
    """

    if not isinstance(location, (list, tuple)) or len(location) < 2:
        return None
    try:
        line = int(location[1])
    except (TypeError, ValueError):
        return None
    try:
        parser = CaskParser(source)
    except Exception:  # pragma: no cover - lexer is lenient in practice
        return None
    tokens = parser.tokens
    opener_index: Optional[int] = None
    for index, token in enumerate(tokens):
        if token.kind != "id" or token.value != stanza or token.line != line:
            continue
        depth = 0
        for candidate in range(index + 1, len(tokens)):
            current = tokens[candidate]
            if current.kind == "nl" and depth == 0:
                break
            if current.kind == "punct" and current.value == "(":
                depth += 1
            elif current.kind == "punct" and current.value == ")":
                depth = max(0, depth - 1)
            elif depth == 0 and (
                (current.kind == "kw" and current.value == "do")
                or (current.kind == "punct" and current.value == "{")
            ):
                opener_index = candidate
                break
        if opener_index is not None:
            break
    if opener_index is None:
        return None
    parser.pos = opener_index + 1
    try:
        body = parser._consume_block_body(tokens[opener_index], capture=True)
    except FormulaParseError:
        return None
    return body.strip() or None


def _extract_livecheck_source(source: str, location: Any) -> Optional[str]:
    """Backwards-compatible livecheck wrapper around :func:`_extract_stanza_source`."""

    return _extract_stanza_source(source, location, "livecheck")


def _attach_livecheck_source(source: str, data: dict[str, Any]) -> None:
    """Fill ``livecheck_source`` (or a warning) from a reported block location."""

    if data.get("livecheck_source"):
        return
    location = data.get("livecheck_location")
    if location is None:
        return
    extracted = _extract_livecheck_source(source, location)
    if extracted is None:
        warnings = data.setdefault("warnings", [])
        if isinstance(warnings, list):
            warnings.append(_LIVECHECK_SOURCE_WARNING)
    else:
        data["livecheck_source"] = extracted
    strategy_location = data.get("livecheck_strategy_location")
    if strategy_location is None:
        return
    livecheck = data.get("livecheck")
    if not isinstance(livecheck, dict):
        livecheck = {}
    if livecheck.get("strategy_source"):
        return
    extracted = _extract_stanza_source(source, strategy_location, "strategy")
    if extracted is None:
        warnings = data.setdefault("warnings", [])
        if isinstance(warnings, list):
            warnings.append(_LIVECHECK_STRATEGY_WARNING)
        return
    livecheck["strategy_source"] = extracted
    data["livecheck"] = livecheck


def _attach_script_sources(source: str, data: dict[str, Any]) -> None:
    """Fill the ``scripts`` map (or warnings) from reported block locations.

    The evaluator never executes script blocks.  It only recovers their raw
    text so ``Cask.scripts`` (and ``CaskInstaller(run_scripts=True)``) can use
    the same bodies the literal parser would have captured.
    """

    locations = data.get("script_locations")
    if not isinstance(locations, dict):
        return
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        scripts = {}
    warnings = data.setdefault("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
        data["warnings"] = warnings
    for name in CAPTURED_SCRIPT_NAMES:
        location = locations.get(name)
        if location is None or scripts.get(name):
            continue
        extracted = _extract_stanza_source(source, location, name)
        if extracted is None:
            warnings.append(_SCRIPT_SOURCE_WARNING.format(stanza=name))
        else:
            scripts[name] = extracted
    if scripts:
        data["scripts"] = scripts


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
        sdk_paths = _macos_sdk_paths()
        sdk_version, sdk_path = _select_macos_sdk(sdk_paths)
        config: dict[str, Any] = {
            "appdir": str(Path.home() / "Applications"),
            "arch": _target_arch(),
            "artifacts": artifacts,
            "filename": filename,
            "macos_version": _target_macos_version(),
            "output": str(output_path),
            "platform": _target_os(),
            "sdk_path": sdk_path,
            "sdk_paths": sdk_paths,
            "sdk_version": sdk_version,
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
        _attach_livecheck_source(source, data)
        _attach_script_sources(source, data)
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
    cask.sha256_map = _string_map(
        data.get("sha256_map"), ("arm", "intel", "arm64_linux", "x86_64_linux")
    )
    cask.url = _optional_text(data.get("url"))
    cask.desc = _optional_text(data.get("desc"))
    cask.homepage = _optional_text(data.get("homepage"))
    cask.homepage_browsed = _optional_text(data.get("homepage_browsed"))
    cask.caveats = _optional_text(data.get("caveats"))
    cask.livecheck_source = _optional_text(data.get("livecheck_source"))
    cask.livecheck_defined = bool(data.get("livecheck_defined"))
    livecheck = data.get("livecheck")
    if isinstance(livecheck, dict):
        cask.livecheck = {
            str(key): value for key, value in livecheck.items() if value is not None
        }
    cask.on_system_blocks = bool(data.get("on_system_blocks"))
    cask.on_system_block_min_os = _optional_text(
        data.get("on_system_block_min_os")
    )
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        for name in CAPTURED_SCRIPT_NAMES:
            body = scripts.get(name)
            if body is not None and str(body).strip():
                setattr(cask, name, str(body))
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
    cask.deprecation_date = _optional_text(data.get("deprecation_date"))
    cask.deprecation_replacement_formula = _optional_text(
        data.get("deprecation_replacement_formula")
    )
    cask.deprecation_replacement_cask = _optional_text(
        data.get("deprecation_replacement_cask")
    )
    deprecate_args = data.get("deprecate_args")
    if isinstance(deprecate_args, dict):
        cask.deprecate_args = dict(deprecate_args)

    no_autobump_reason = data.get("no_autobump_reason")
    if no_autobump_reason is not None and str(no_autobump_reason).strip():
        cask.no_autobump_reason = str(no_autobump_reason)
    cask.no_autobump_message = _optional_text(data.get("no_autobump_message"))
    if data.get("no_autobump"):
        cask.no_autobump = True

    disabled = data.get("disabled")
    if disabled:
        cask.disabled = True
        cask.disable_reason = str(data.get("disable_reason") or "") or None
        cask.disable_date = _optional_text(data.get("disable_date"))
        cask.disable_replacement_formula = _optional_text(
            data.get("disable_replacement_formula")
        )
        cask.disable_replacement_cask = _optional_text(
            data.get("disable_replacement_cask")
        )
        disable_args = data.get("disable_args")
        if isinstance(disable_args, dict):
            cask.disable_args = dict(disable_args)

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
# commonly interpolate ``version.major_minor`` into URLs and compare
# ``MacOS.version`` against release symbols.
class PybrewCaskVersion < String
  include Comparable

  # Release symbol -> macOS version.  Casks compare versions against symbols
  # (``MacOS.version >= :sonoma``), so the comparison operators translate them.
  SYMBOL_VERSIONS = {
    "catalina" => "10.15",
    "big_sur" => "11",
    "monterey" => "12",
    "ventura" => "13",
    "sonoma" => "14",
    "sequoia" => "15",
    "tahoe" => "26",
    "golden_gate" => "27",
  }.freeze

  #: macOS release version -> human readable release name.
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

  def self.parts(value)
    value.to_s.split(".").map(&:to_i)
  end

  def <=>(other)
    other = SYMBOL_VERSIONS[other.to_s] if other.is_a?(Symbol)
    return nil if other.nil?

    PybrewCaskVersion.parts(self) <=> PybrewCaskVersion.parts(other)
  end

  def ==(other)
    comparison = (self <=> other)
    comparison.nil? ? super : comparison.zero?
  end
  alias eql? ==

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

  # Mirror Homebrew's MacOSVersion naming helpers.  Big Sur reports 10.16 under
  # SYSTEM_VERSION_COMPAT, hence the alias.
  def release_version
    version = major.to_i >= 11 ? major : major_minor
    version == "10.16" ? "11" : version
  end

  def release_name
    RELEASE_NAMES[release_version]
  end

  def pretty_name
    release_name || "Dunno"
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

#: Release symbol -> macOS major version, shared by ``on_<release>`` blocks and
#: ``MacOS.version_symbol``.
PYBREW_MACOS_RELEASES = {
  "big_sur" => 11,
  "monterey" => 12,
  "ventura" => 13,
  "sonoma" => 14,
  "sequoia" => 15,
  "tahoe" => 26,
  "golden_gate" => 27,
}.freeze

#: macOS version string -> release symbol, matching Homebrew's
#: ``MacOS.version_symbol``.
PYBREW_MACOS_VERSION_SYMBOLS = {
  "10.15" => :catalina,
  "11" => :big_sur,
  "12" => :monterey,
  "13" => :ventura,
  "14" => :sonoma,
  "15" => :sequoia,
  "26" => :tahoe,
  "27" => :golden_gate,
}.freeze

#: Oldest macOS release Homebrew still allows; used by ``:or_older`` blocks.
PYBREW_OLDEST_MACOS_VERSION = "10.15"

# A minimal stand-in for Homebrew's ``MacOS::SDK``.  pybrew only reports SDKs it
# can see on disk; see the ``MacOS.sdk`` documentation below.
class PybrewCaskSDK
  attr_reader :path, :version

  def initialize(path, version)
    @path = Pathname.new(path.to_s)
    @version = PybrewCaskVersion.new(version.to_s)
  end

  def to_s
    @path.to_s
  end
end

module MacOS
  Version = PybrewCaskVersion

  def self.full_version
    PybrewCaskVersion.new((PYBREW_CASK_CONFIG["macos_version"] || "0").to_s)
  end

  def self.version
    full_version
  end

  def self.version_symbol
    parts = full_version.split(".")
    key = parts[0].to_i == 10 ? "10.#{parts[1]}" : parts[0].to_s
    PYBREW_MACOS_VERSION_SYMBOLS.fetch(key, :unknown)
  end

  # Convenience delegates matching the ``MacOS.version`` naming helpers.
  def self.release_name
    version.release_name
  end

  def self.pretty_name
    version.pretty_name
  end

  # Best-effort SDK lookup.  pybrew only knows about SDKs the host already has
  # on disk (or ``PYBREW_MACOS_SDK_PATH``); it never invokes Xcode tooling or
  # the network.  Returns nil when no matching SDK is available.
  def self.sdk(version = nil)
    return nil unless (PYBREW_CASK_CONFIG["platform"] || "").to_s == "darwin"

    paths = PYBREW_CASK_CONFIG["sdk_paths"]
    paths = {} unless paths.is_a?(Hash)
    requested = if version.nil?
                  nil
                elsif version.is_a?(Symbol)
                  PybrewCaskVersion::SYMBOL_VERSIONS[version.to_s]
                else
                  version.to_s
                end
    if requested.nil?
      path = PYBREW_CASK_CONFIG["sdk_path"]
      version_string = PYBREW_CASK_CONFIG["sdk_version"]
    else
      key = paths.keys.find { |candidate| candidate == requested }
      if key.nil?
        wanted = PybrewCaskVersion.parts(requested)
        key = paths.keys.find do |candidate|
          PybrewCaskVersion.parts(candidate).first == wanted.first
        end
      end
      return nil if key.nil?

      path = paths[key]
      version_string = key
    end
    return nil if path.nil? || path.to_s.empty?

    PybrewCaskSDK.new(path, version_string || full_version)
  end

  def self.sdk_path(version = nil)
    sdk = self.sdk(version)
    sdk.nil? ? nil : sdk.path
  end
end

# Metadata collector for cask ``livecheck`` blocks.  Homebrew evaluates these
# in its Livecheck DSL; pybrew records the common metadata without performing
# any network access or livecheck work.
class PybrewCaskLivecheck
  attr_reader :data, :strategy_location

  def initialize
    @data = {
      "url" => nil,
      "url_options" => nil,
      "regex" => nil,
      "strategy" => nil,
      "skip" => false,
      "skip_msg" => nil,
      "throttle" => nil,
      "throttle_days" => nil,
      "formula" => nil,
      "cask" => nil,
    }
    @strategy_location = nil
  end

  # ``url`` accepts Homebrew's strategy options (header/user_agent/referer/
  # cookies/homebrew_curl/...).  pybrew records them without performing any
  # network request.
  def url(value = nil, **options)
    @data["url"] = PybrewCaskJSON.value(value) unless value.nil?
    unless options.empty?
      @data["url_options"] = PybrewCaskJSON.value(options)
    end
    @data["url"]
  end

  def regex(pattern = nil)
    if pattern.is_a?(Regexp)
      @data["regex"] = pattern.source
    elsif !pattern.nil?
      @data["regex"] = pattern.to_s
    end
    @data["regex"]
  end

  # The strategy block is captured by location (and extracted from the cask
  # source in Python); it is never executed here.
  def strategy(symbol = nil, &block)
    @data["strategy"] = PybrewCaskJSON.value(symbol) unless symbol.nil?
    @strategy_location = block.source_location unless block.nil?
    @data["strategy"]
  end

  def skip(message = nil)
    @data["skip"] = true
    @data["skip_msg"] = message.to_s unless message.nil?
    true
  end

  def skip?
    @data["skip"] ? true : false
  end

  def throttle(rate = nil, days: nil)
    @data["throttle"] = rate unless rate.nil?
    @data["throttle_days"] = days unless days.nil?
    @data["throttle"]
  end

  def formula(name = nil)
    @data["formula"] = PybrewCaskJSON.value(name) unless name.nil?
    @data["formula"]
  end

  def cask(name = nil)
    @data["cask"] = PybrewCaskJSON.value(name) unless name.nil?
    @data["cask"]
  end

  def method_missing(name, *args, &block)
    @data["unsupported"] ||= []
    @data["unsupported"] << {
      "stanza" => name.to_s,
      "args" => PybrewCaskJSON.value(args),
      "block" => !block.nil?,
    }
    nil
  end

  def respond_to_missing?(_name, _include_private = false)
    true
  end
end

class PybrewCaskDSL
  MACOS_RELEASES = PYBREW_MACOS_RELEASES

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
      "homepage_browsed" => nil,
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
      "deprecation_date" => nil,
      "deprecation_replacement_formula" => nil,
      "deprecation_replacement_cask" => nil,
      "deprecate_args" => {},
      "disabled" => false,
      "disable_reason" => nil,
      "disable_date" => nil,
      "disable_replacement_formula" => nil,
      "disable_replacement_cask" => nil,
      "disable_args" => {},
      "os_map" => {},
      "no_autobump" => false,
      "no_autobump_message" => nil,
      "on_system_blocks" => false,
      "on_system_block_min_os" => nil,
      "livecheck_defined" => false,
      "livecheck" => {},
      "livecheck_location" => nil,
      "livecheck_strategy_location" => nil,
      "no_autobump_reason" => nil,
      "script_locations" => {},
      "scripts" => {},
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

  # Homebrew's ``sha256`` map uses ``arm64_linux``/``x86_64_linux`` keys for
  # Linux casks and ``arm``/``intel`` keys for macOS casks.
  def sha256_key
    if host_platform == "linux"
      host_arch == "arm64" ? "arm64_linux" : "x86_64_linux"
    else
      host_arch_key
    end
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
      next unless %w[arm intel arm64_linux x86_64_linux].include?(key.to_s)

      @sha256_map[key.to_s] = value.to_s
    end
    @data["sha256_map"] = @sha256_map
    mapped = @sha256_map[sha256_key] || @sha256_map[host_arch_key]
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

  def homepage(value = nil, browsed: nil, **_options)
    @data["homepage_browsed"] = browsed.to_s unless browsed.nil?
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
      @caveats_output = +""
      value = instance_eval(&block)
      text = value.is_a?(String) ? value : ""
      @data["caveats"] = @caveats_output + text
      @caveats_output = nil
      return @data["caveats"]
    end
    positional, = PybrewCaskJSON.split_args(args)
    unless positional.empty?
      @data["caveats"] = positional.map(&:to_s).join("\n")
    end
    @data["caveats"]
  end

  def puts(*messages)
    if @caveats_output
      if messages.empty?
        @caveats_output << "\n"
      else
        messages.each { |message| @caveats_output << message.to_s << "\n" }
      end
      return nil
    end
    Kernel.puts(*messages)
    nil
  end

  def print(*messages)
    if @caveats_output
      messages.each { |message| @caveats_output << message.to_s }
      return nil
    end
    Kernel.print(*messages)
    nil
  end

  def ohai(*messages)
    if @caveats_output
      messages.each { |message| @caveats_output << "==> " << message.to_s << "\n" }
      return nil
    end
    nil
  end

  def opoo(*messages)
    if @caveats_output
      messages.each { |message| @caveats_output << "Warning: " << message.to_s << "\n" }
      return nil
    end
    nil
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
        if active
          @data["on_system_block_min_os"] =
            condition.to_s == "or_older" ? PYBREW_OLDEST_MACOS_VERSION : major.to_s
          evaluate_block(block)
        end
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
    @data["deprecate_args"] = kwargs
    record_lifecycle_metadata("deprecation", kwargs)
    nil
  end

  def disable!(*args)
    _positional, kwargs = PybrewCaskJSON.split_args(args)
    @data["disabled"] = true
    @data["disable_args"] = kwargs
    record_lifecycle_metadata("disable", kwargs)
    nil
  end

  # Copy Homebrew's deprecate!/disable! keyword metadata into the result.  The
  # legacy ``replacement:`` keyword fills both replacement fields, matching
  # Homebrew.  pybrew captures the date but does not defer the deprecation or
  # disable by date; that remains a documented limitation.
  def record_lifecycle_metadata(prefix, kwargs)
    reason = kwargs["because"] || kwargs[:because]
    @data["#{prefix}_reason"] = reason.to_s unless reason.nil?
    date = kwargs["date"] || kwargs[:date]
    @data["#{prefix}_date"] = date.to_s unless date.nil?
    replacement = kwargs["replacement"] || kwargs[:replacement]
    replacement_formula = kwargs["replacement_formula"] || kwargs[:replacement_formula]
    replacement_cask = kwargs["replacement_cask"] || kwargs[:replacement_cask]
    replacement_formula = replacement if replacement_formula.nil? && !replacement.nil?
    replacement_cask = replacement if replacement_cask.nil? && !replacement.nil?
    unless replacement_formula.nil?
      @data["#{prefix}_replacement_formula"] = replacement_formula.to_s
    end
    unless replacement_cask.nil?
      @data["#{prefix}_replacement_cask"] = replacement_cask.to_s
    end
    nil
  end

  def no_autobump!(*args)
    _positional, kwargs = PybrewCaskJSON.split_args(args)
    @data["no_autobump"] = true
    reason = kwargs["because"] || kwargs[:because]
    unless reason.nil?
      @data["no_autobump_reason"] = reason.to_s
      @data["no_autobump_message"] = reason.to_s
    end
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

  # Capture livecheck metadata by evaluating the block in a metadata collector.
  # This performs no network access.  The block's ``source_location`` lets
  # Python recover the raw source from the original cask text (best effort).
  def livecheck(*_args, &block)
    @data["livecheck_defined"] = true
    return @data["livecheck"] if block.nil?

    location = block.source_location
    @data["livecheck_location"] = location unless location.nil?
    collector = PybrewCaskLivecheck.new
    begin
      collector.instance_eval(&block)
    rescue StandardError => error
      @data["warnings"] << "cask `livecheck` block could not be fully " \
                           "evaluated: #{error.class}: #{error.message}"
    end
    unless collector.strategy_location.nil?
      @data["livecheck_strategy_location"] = collector.strategy_location
    end
    @data["livecheck"] = collector.data
    @data["livecheck"]
  end

  def livecheck_defined?
    !!@data["livecheck_defined"]
  end

  # -- cask scripts and unknown stanzas ---------------------------------

  # Record the raw source location of cask script blocks.  The blocks are
  # never evaluated here: Python recovers the text from the original cask
  # source and stores it on the Cask, where only an explicit
  # ``CaskInstaller(run_scripts=True)`` executes it.
  %w[preflight postflight uninstall_preflight uninstall_postflight].each do |name|
    define_method(name.to_sym) do |*_args, &block|
      @data["script_locations"][name] =
        block.nil? ? nil : block.source_location
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


def merge_captured_scripts(evaluated: Cask, parsed: Cask) -> Cask:
    """Copy inert script/caveats bodies from a literal parse into ``evaluated``.

    The evaluator recovers script bodies best-effort from ``source_location``;
    the literal parser is authoritative, so its body replaces the evaluator's
    copy when both captured one.  ``CaskLoader`` uses this helper so opting into
    Ruby does not silently lose preflight/postflight hooks.
    """

    merged: list[str] = []
    for name in CAPTURED_SCRIPT_NAMES:
        body = getattr(parsed, name, None)
        if body and body != getattr(evaluated, name, None):
            setattr(evaluated, name, body)
            merged.append(name)
    if parsed.caveats_source and not evaluated.caveats_source:
        evaluated.caveats_source = parsed.caveats_source
        merged.append("caveats_source")
    if parsed.caveats and not evaluated.caveats:
        evaluated.caveats = parsed.caveats
    # ``livecheck`` blocks are inert metadata.  The evaluator recovers their
    # source best-effort from the reported source location; the literal parser
    # is authoritative when it captured the block, so prefer it as a fallback.
    if parsed.livecheck_source and not evaluated.livecheck_source:
        evaluated.livecheck_source = parsed.livecheck_source
        merged.append("livecheck_source")
    if parsed.livecheck_defined and not evaluated.livecheck_defined:
        evaluated.livecheck_defined = True
    if merged:
        evaluated.extra.setdefault("ruby_merged_scripts", []).extend(merged)
    return evaluated
