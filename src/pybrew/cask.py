"""Parser and model for Homebrew Cask definitions."""

from __future__ import annotations

import hashlib
import os
import platform
import plistlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .errors import FormulaParseError, PybrewError
from .models import is_linux, is_macos, macos_major_version
from .version import compare_versions
from .ruby_parser import Lexer, Token


class CaskError(PybrewError):
    """A cask could not be parsed or installed."""


@dataclass
class Artifact:
    kind: str
    source: str
    target: Optional[str] = None
    kwargs: dict[str, Any] = field(default_factory=dict)

    def to_s(self) -> str:
        return self.target or self.source

    def english_name(self) -> str:
        return self.kind.replace("_", " ")

    def english_description(self) -> str:
        return f"{self.english_name()} {self.source}"

    def summarize(self) -> str:
        return self.to_s()

    def staged_path_join_executable(self, staged: Path | str) -> Path:
        return Path(staged) / self.source

    def requires_sudo(self) -> bool:
        return False

    def dsl_key(self) -> str:
        return self.kind

    def sort_order(self) -> int:
        return 0

    def to_args(self) -> list[str]:
        return [self.source]


@dataclass
class Cask:
    token: str
    path: Optional[Path] = None
    tap: Optional[str] = None
    version: Optional[str] = None
    sha256: Optional[str] = None
    sha256_map: dict[str, str] = field(default_factory=dict)
    url: Optional[str] = None
    #: ``url`` stanza keyword metadata (using/user_agent/header/referer/...).
    url_options: dict[str, Any] = field(default_factory=dict)
    name: Optional[str] = None
    desc: Optional[str] = None
    homepage: Optional[str] = None
    homepage_browsed: Optional[str] = None
    artifacts: list[Artifact] = field(default_factory=list)
    renames: list[tuple[str, str]] = field(default_factory=list)
    uninstall: dict[str, Any] = field(default_factory=dict)
    zap: list[str] = field(default_factory=list)
    depends_on: dict[str, Any] = field(default_factory=dict)
    os: dict[str, Any] = field(default_factory=dict)
    conflicts_with: dict[str, Any] = field(default_factory=dict)
    auto_updates: bool = False
    stage_only: bool = False
    container: Optional[str] = None
    #: ``container nested:`` path and any other ``container`` kwargs.
    container_nested: Optional[str] = None
    container_options: dict[str, Any] = field(default_factory=dict)
    language: Optional[str] = None
    arch: Optional[str] = None
    arch_map: dict[str, str] = field(default_factory=dict)
    appdir: Optional[str] = None
    caveats: Optional[str] = None
    caveats_source: Optional[str] = None
    livecheck_source: Optional[str] = None
    #: True when the cask declares a ``livecheck`` stanza.  The default parser
    #: records the raw block source; the opt-in Ruby evaluator additionally
    #: fills :attr:`livecheck` with the metadata collected from the block.
    livecheck_defined: bool = False
    livecheck: dict[str, Any] = field(default_factory=dict)
    #: True when the cask uses ``on_macos``/``on_linux`` blocks.  Mirrors the
    #: Homebrew DSL helper ``on_system_blocks_exist?``.
    on_system_blocks: bool = False
    #: Minimum macOS release implied by the active ``on_<release>`` block.
    on_system_block_min_os: Optional[str] = None
    no_autobump: bool = False
    no_autobump_reason: Optional[str] = None
    #: Homebrew exposes the ``no_autobump!`` reason as ``no_autobump_message``.
    no_autobump_message: Optional[str] = None
    deprecated: bool = False
    deprecation_reason: Optional[str] = None
    deprecation_date: Optional[str] = None
    deprecation_replacement_formula: Optional[str] = None
    deprecation_replacement_cask: Optional[str] = None
    deprecate_args: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False
    disable_reason: Optional[str] = None
    disable_date: Optional[str] = None
    disable_replacement_formula: Optional[str] = None
    disable_replacement_cask: Optional[str] = None
    disable_args: dict[str, Any] = field(default_factory=dict)
    # Raw Ruby block bodies captured from the cask.  They are inert data: no
    # parser or installer path executes them unless a caller explicitly opts
    # in (see ``CaskInstaller(run_scripts=True)``).
    preflight: Optional[str] = None
    postflight: Optional[str] = None
    uninstall_preflight: Optional[str] = None
    uninstall_postflight: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.tap}/{self.token}" if self.tap else self.token

    @property
    def scripts(self) -> dict[str, str]:
        """Return the captured script bodies keyed by stanza name."""
        return {
            name: body
            for name in _SCRIPT_STANZAS
            if (body := getattr(self, name, None))
        }

    @property
    def on_system_blocks_exist(self) -> bool:
        """Mirror Homebrew's ``on_system_blocks_exist?`` DSL helper."""
        return self.on_system_blocks

    @property
    def autobump(self) -> bool:
        """Mirror Homebrew's ``autobump?``: false after ``no_autobump!``."""
        return not self.no_autobump

    @property
    def full_token(self) -> str:
        return self.full_name

    @property
    def caskroom_path(self) -> Path:
        prefix = Path(os.environ.get("HOMEBREW_PREFIX", "/usr/local"))
        return prefix / "Caskroom" / self.token

    def installed_versions(self) -> list[str]:
        """Return version entries currently present in the Caskroom."""
        root = self.caskroom_path
        if not root.is_dir():
            return []
        versions = []
        for entry in root.iterdir():
            if entry.name.startswith("."):
                continue
            versions.append(entry.name)
        return sorted(versions)

    def timestamped_versions(self) -> list[str]:
        """Homebrew-compatible alias for installed version entries."""
        return self.installed_versions()

    def installed_caskfile(self) -> Optional[Path]:
        """Return the metadata copy of this cask's definition, when present."""
        root = self.caskroom_path / ".metadata"
        if not root.is_dir():
            return None
        for candidate in sorted(root.glob(f"**/Casks/{self.token}.rb")):
            if candidate.is_file():
                return candidate
        return None

    def outdated_version(self) -> Optional[str]:
        if (self.version or "latest") == "latest":
            return None
        versions = [value for value in self.installed_versions() if value != self.version]
        return versions[-1] if versions else None

    def outdated_info(self) -> Optional[dict[str, Any]]:
        old = self.outdated_version()
        if old is None:
            return None
        return {
            "token": self.token,
            "current_version": self.version,
            "installed_version": old,
            "installed_versions": self.installed_versions(),
        }

    @property
    def installed(self) -> bool:
        return bool(self.installed_versions())

    def installed_version(self) -> Optional[str]:
        versions = self.installed_versions()
        return versions[-1] if versions else None

    def any_version_installed(self) -> bool:
        return self.installed

    def pinnable(self) -> bool:
        version = self.installed_version()
        return bool(version and (self.caskroom_path / version).exists())

    def outdated_download_sha(self) -> bool:
        if not self.checksumable():
            return True
        path = self.download_sha_path()
        if not path.exists():
            return True
        current = path.read_text(encoding="utf-8", errors="replace").strip()
        expected = self.new_download_sha()
        return not current or current != expected

    def auto_updates_bundle_outdated(self) -> bool:
        return False

    def uninstall_flight_blocks(self) -> bool:
        return False

    @property
    def outdated(self) -> bool:
        return self.outdated_version() is not None

    def _app_candidates(self) -> list[Path]:
        prefix = Path(os.environ.get("HOMEBREW_PREFIX", "/usr/local"))
        appdir = Path(os.environ.get("PYBREW_APPDIR", str(prefix / "Applications")))
        candidates: list[Path] = []
        for artifact in self.artifacts:
            if artifact.kind not in {"app", "suite"}:
                continue
            target = artifact.target or Path(artifact.source).name
            path = Path(target)
            candidates.append(path if path.is_absolute() else appdir / target)
        return candidates

    def installed_app_info_plist(self) -> Optional[Path]:
        for app in self._app_candidates():
            plist = app / "Contents" / "Info.plist"
            if plist.is_file():
                return plist
        return None

    def installed_app_info(self) -> dict[str, Any]:
        plist = self.installed_app_info_plist()
        if plist is None:
            return {}
        try:
            with plist.open("rb") as handle:
                payload = plistlib.load(handle)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def all(cls) -> list["Cask"]:
        """Bounded placeholder: discovery requires a configured tap set."""
        return []

    def config_path(self) -> Path:
        return self.caskroom_path / ".metadata" / "config.json"

    def languages(self) -> list[str]:
        return [self.language] if self.language else ["en"]

    def download_strategy(self) -> Optional[str]:
        value = (self.url_options or {}).get("using")
        return str(value) if value is not None else None

    def using(self) -> Optional[str]:
        return self.download_strategy()

    def api_to_local_hash(self) -> dict[str, Any]:
        payload = {
            "token": self.token,
            "full_token": self.full_name,
            "version": self.version,
            "url": self.url,
            "sha256": self.resolved_sha256(),
            "homepage": self.homepage,
            "name": self.name,
        }
        return {key: value for key, value in payload.items() if value is not None}

    def to_hash_with_variations(self) -> dict[str, Any]:
        return self.to_h()

    def dsl(self) -> "Cask":
        return self

    def populate_from_api(self, data: dict[str, Any]) -> "Cask":
        for key in ("token", "version", "url", "sha256", "name", "desc", "homepage"):
            value = data.get(key)
            if value is not None:
                setattr(self, key, value)
        if data.get("artifacts"):
            self.artifacts = [
                Artifact(
                    str(item.get("kind", "artifact")),
                    str(item.get("source", "")),
                    item.get("target"),
                    dict(item.get("kwargs") or {}),
                )
                for item in data["artifacts"]
                if isinstance(item, dict)
            ]
        return self

    def refresh(self) -> "Cask":
        if self.path is None or not self.path.is_file():
            return self
        refreshed = parse_cask_file(self.path)
        self.__dict__.update(refreshed.__dict__)
        return self

    def refresh_for_tag(self, _tag: Any = None) -> "Cask":
        return self.refresh()

    def tab(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "version": self.installed_version() or self.version,
        }

    def reloadable_ref(self) -> Optional[Path]:
        return self.path

    def tap_git_head(self) -> Optional[str]:
        return None

    def new_download_sha(self) -> Optional[str]:
        return self.resolved_sha256()

    def download_sha_path(self) -> Path:
        return self.caskroom_path / ".metadata" / "download_sha"

    def install_time(self) -> Optional[float]:
        try:
            return self.caskroom_path.stat().st_mtime
        except OSError:
            return None

    def to_s(self) -> str:
        return self.full_name

    def inspect(self) -> str:
        return f"#<Cask:{self.full_name}@{self.version or 'latest'}>"

    def hash(self) -> int:
        return hash(self.full_name)

    def to_h(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "token": self.token,
            "full_name": self.full_name,
            "tap": self.tap,
            "version": self.version,
            "sha256": self.resolved_sha256(),
            "url": self.url,
            "name": self.name,
            "desc": self.desc,
            "homepage": self.homepage,
            "artifacts": [
                {
                    "kind": artifact.kind,
                    "source": artifact.source,
                    "target": artifact.target,
                    "kwargs": dict(artifact.kwargs),
                }
                for artifact in self.artifacts
            ],
            "renames": [list(rename) for rename in self.renames],
            "caveats": self.caveats,
        }
        payload.update(self.extra)
        return payload

    def ruby_source_path(self) -> Optional[Path]:
        return self.path if self.path is not None and self.path.is_file() else None

    def ruby_source_checksum(self) -> Optional[str]:
        path = self.ruby_source_path()
        if path is None:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def loaded_from_api(self) -> bool:
        return False

    def loaded_from_internal_api(self) -> bool:
        return False

    def valid_platform(self) -> bool:
        return self.platform_supported()

    def supports_macos(self) -> bool:
        return bool((self.os or {}).get("macos", True))

    def supports_linux(self) -> bool:
        return bool((self.os or {}).get("linux", True))

    @property
    def pin_path(self) -> Path:
        return self.caskroom_path / ".metadata" / "pin"

    def pinned(self) -> bool:
        return self.pin_path.is_file()

    def pinned_version(self) -> Optional[str]:
        if not self.pinned():
            return None
        value = self.pin_path.read_text().strip()
        return value or None

    def pin(self) -> None:
        self.pin_path.parent.mkdir(parents=True, exist_ok=True)
        self.pin_path.write_text(f"{self.version or 'latest'}\n")

    def unpin(self) -> None:
        try:
            self.pin_path.unlink()
        except FileNotFoundError:
            pass

    def bundle_version(self) -> Optional[str]:
        value = self.installed_app_info().get("CFBundleVersion")
        return str(value) if value is not None else None

    def bundle_short_version(self) -> Optional[str]:
        value = self.installed_app_info().get("CFBundleShortVersionString")
        return str(value) if value is not None else None

    def bundle_long_version(self) -> Optional[str]:
        short = self.bundle_short_version()
        build = self.bundle_version()
        if short and build and short != build:
            return f"{short} ({build})"
        return short or build

    def artifacts_list(self) -> list[Artifact]:
        return list(self.artifacts)

    def single_app_artifact(self) -> Optional[Artifact]:
        apps = [artifact for artifact in self.artifacts if artifact.kind == "app"]
        return apps[0] if len(apps) == 1 else None

    def font(self) -> bool:
        return any(artifact.kind == "font" for artifact in self.artifacts)

    def url_specs(self) -> list[str]:
        return [self.url] if self.url else []

    def checksumable(self) -> bool:
        return bool(self.sha256 or self.sha256_map)

    @staticmethod
    def compare_version_strings(first: str, second: str) -> int:
        """Compare two cask version strings (Homebrew-like ordering)."""
        return compare_versions(str(first), str(second))

    def rename_list(self) -> list[tuple[str, str]]:
        return list(self.renames)

    def old_tokens(self) -> list[str]:
        return [old for old, _new in self.renames]

    def caveats_object(self) -> Optional[str]:
        return self.caveats

    def caveats_for_api(self) -> dict[str, Any]:
        if self.caveats is None:
            return {}
        return {"caveats": self.caveats}

    def loaded_from_metadata(self) -> bool:
        return False

    def platform_supported(self) -> bool:
        os_stanza = self.os or {}
        if not os_stanza:
            return True
        if is_macos():
            return bool(os_stanza.get("macos", True))
        if is_linux():
            return bool(os_stanza.get("linux", True))
        return True

    def installable_artifact(self) -> Optional[Artifact]:
        for artifact in self.artifacts:
            if artifact.kind not in {"artifact", "stage_only"}:
                return artifact
        return None

    def to_h_with_language_variations(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "token": self.token,
            "full_token": self.full_name,
            "version": self.version,
            "url": self.url,
            "homepage": self.homepage,
            "name": self.name,
        }
        payload.update(self.extra)
        return {key: value for key, value in payload.items() if value is not None}

    def to_installed_json_hash(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "version": self.version,
            "installed_versions": self.installed_versions(),
        }

    def resolved_version(self) -> str:
        return self.version or "latest"

    def resolved_arch(self) -> str:
        if self.arch:
            return self.arch
        machine = platform.machine().lower()
        return "arm64" if machine in ("arm64", "aarch64") else "intel"

    def resolved_sha256_key(self) -> str:
        """Return the platform-specific ``sha256`` map key for this host."""
        return _sha256_key(self.resolved_arch())

    def resolved_sha256(self) -> Optional[str]:
        if self.sha256:
            return self.sha256
        arch = self.resolved_arch()
        value = self.sha256_map.get(self.resolved_sha256_key())
        if value is not None:
            return value
        # Older casks only use the macOS-style ``arm``/``intel`` keys; keep
        # accepting them where a Linux-specific checksum is absent.
        return self.sha256_map.get("arm" if arch == "arm64" else "intel")

    def _interpolate(self, template: str) -> str:
        version = self.resolved_version()
        arch_name = self.resolved_arch()
        mapping = {
            "version": version,
            "version.major": version.split(".")[0],
            "version.minor": version.split(".")[1] if "." in version else "0",
            "version.major_minor": ".".join(version.split(".")[:2]),
            "version.dots_to_underscores": version.replace(".", "_"),
            "version.dots_to_hyphens": version.replace(".", "-"),
            "version.major_minor.dots_to_underscores": ".".join(version.split(".")[:2]).replace(".", "_"),
            "arch": {"arm64": "arm", "intel": "intel"}.get(arch_name, arch_name),
            "language": self.language or "en",
            "token": self.token,
            "appdir": self.appdir or str(Path.home() / "Applications"),
        }
        if arch_name == "arm64" and self.arch_map.get("arm"):
            mapping["arch"] = self.arch_map["arm"]
        elif arch_name == "intel" and self.arch_map.get("intel"):
            mapping["arch"] = self.arch_map["intel"]

        def replace(match: re.Match[str]) -> str:
            expression = match.group(1).strip()
            if expression in mapping:
                return mapping[expression]
            raise CaskError(f"{self.token}: unsupported cask interpolation #{{{expression}}}")

        return re.sub(r"#\{([^{}]+)\}", replace, template)

    def resolved_url(self) -> str:
        if not self.url:
            raise CaskError(f"{self.token}: cask has no url")
        return self._interpolate(self.url)

    def resolved_artifacts(self) -> list[Artifact]:
        result: list[Artifact] = []
        for artifact in self.artifacts:
            result.append(
                Artifact(
                    artifact.kind,
                    self._interpolate(artifact.source),
                    self._interpolate(artifact.target) if artifact.target else None,
                    artifact.kwargs,
                )
            )
        return result

    def to_api_json(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "full_token": self.full_name,
            "tap": self.tap,
            "name": [self.name] if self.name else [],
            "desc": self.desc,
            "homepage": self.homepage,
            "url": self.resolved_url() if self.url else None,
            "version": self.version,
            "sha256": self.resolved_sha256(),
            "artifacts": [
                {"kind": artifact.kind, "source": artifact.source, "target": artifact.target}
                for artifact in self.artifacts
            ],
            "depends_on": self.depends_on,
            "conflicts_with": self.conflicts_with,
            "auto_updates": self.auto_updates,
            "uninstall": self.uninstall,
            "zap": self.zap,
            "caveats": self.caveats,
        }


# Artifact stanzas that name a file or bundle to install.  ``stage_only`` is
# deliberately not included: it is a cask-level flag rather than an artifact.
_ARTIFACT_KINDS = frozenset(
    {
        "app",
        "appimage",
        "bash_completion",
        "artifact",
        "audio_unit_plugin",
        "binary",
        "colorpicker",
        "fish_completion",
        "dictionary",
        "font",
        "input_method",
        "installer",
        "keyboard_layout",
        "manpage",
        "mdimporter",
        "pkg",
        "prefpane",
        "qlplugin",
        "screen_saver",
        "service",
        "suite",
        "vst3_plugin",
        "vst_plugin",
        "zsh_completion",
    }
)

# Cask stanzas whose ``do ... end`` body is Ruby code.  The parser keeps the
# raw source; only an explicit ``run_scripts`` install evaluates it.
_SCRIPT_STANZAS = (
    "preflight",
    "postflight",
    "uninstall_preflight",
    "uninstall_postflight",
)

# Keywords that open a nested Ruby block inside a cask script body.
_BLOCK_KEYWORDS = frozenset(
    {"do", "if", "unless", "case", "begin", "while", "until", "for"}
)

_ARCH_NAMES = {
    "big_sur": 11,
    "monterey": 12,
    "ventura": 13,
    "sonoma": 14,
    "sequoia": 15,
    "tahoe": 26,
    "golden_gate": 27,
}

#: Oldest macOS release Homebrew still allows; used by ``:or_older`` blocks.
_OLDEST_MACOS_VERSION = "10.15"

#: Homebrew accepts these ``sha256`` map keys (macOS and Linux architectures).
_SHA256_KEYS = ("arm", "intel", "arm64_linux", "x86_64_linux")


def _sha256_key(arch: str) -> str:
    """Return the Homebrew ``sha256`` map key for ``arch`` on this host."""

    if is_linux():
        return "arm64_linux" if arch == "arm64" else "x86_64_linux"
    return "arm" if arch == "arm64" else "intel"


def _apply_lifecycle_fields(
    cask: Cask, prefix: str, args: list[Any], kwargs: dict[Any, Any]
) -> None:
    """Copy ``deprecate!``/``disable!`` metadata onto ``cask``.

    ``prefix`` is ``"deprecation"`` or ``"disable"``; the attribute names match
    Homebrew's ``deprecation_*``/``disable_*`` readers.  The legacy
    ``replacement:`` keyword fills both replacement fields, like Homebrew.
    """

    reason = kwargs.get("because") or (args[0] if args else None)
    if reason is not None:
        setattr(cask, f"{prefix}_reason", str(reason))
    date = kwargs.get("date")
    if date is not None:
        setattr(cask, f"{prefix}_date", str(date))
    replacement = kwargs.get("replacement")
    replacement_formula = kwargs.get("replacement_formula")
    replacement_cask = kwargs.get("replacement_cask")
    if replacement is not None:
        if replacement_formula is None:
            replacement_formula = replacement
        if replacement_cask is None:
            replacement_cask = replacement
    if replacement_formula is not None:
        setattr(cask, f"{prefix}_replacement_formula", str(replacement_formula))
    if replacement_cask is not None:
        setattr(cask, f"{prefix}_replacement_cask", str(replacement_cask))


class CaskParser:
    def __init__(self, source: str, path: Optional[Path] = None, tap: Optional[str] = None):
        self.source = source
        self.path = Path(path) if path else None
        self.tap = tap
        self.tokens = Lexer(source).tokenise()
        self.pos = 0
        self.cask = Cask(
            token=self.path.stem if self.path else "unknown",
            path=self.path,
            tap=tap,
        )
        self._default_language_seen = False
        self._arch_map: dict[str, str] = {}

    # -- token helpers ---------------------------------------------------
    def peek(self, offset: int = 0) -> Token:
        index = self.pos + offset
        if index >= len(self.tokens):
            return Token("eof", None, 0, 0, 0)
        return self.tokens[index]

    def advance(self) -> Token:
        if self.pos >= len(self.tokens):
            return Token("eof", None, 0, 0, 0)
        token = self.tokens[self.pos]
        if token.kind != "eof":
            self.pos += 1
        return token

    def skip_newlines(self) -> None:
        while self.peek().kind == "nl" or (self.peek().kind == "punct" and self.peek().value == ";"):
            self.advance()

    def parse(self) -> Cask:
        while self.peek().kind != "eof":
            token = self.peek()
            if token.kind == "id" and token.value == "cask":
                self.advance()
                token_value = self.advance()
                if token_value.kind != "str":
                    raise FormulaParseError("cask declaration without a token string")
                self.cask.token = str(token_value.value)
                self.skip_newlines()
                if self.peek().kind == "kw" and self.peek().value == "do":
                    self.advance()
                    self._parse_block()
                    return self.cask
            self.advance()
        raise FormulaParseError("no `cask \"token\" do ... end` declaration found")

    def _parse_block(self) -> None:
        while True:
            self.skip_newlines()
            token = self.peek()
            if token.kind == "eof":
                raise FormulaParseError("unterminated cask block")
            if token.kind == "kw" and token.value == "end":
                self.advance()
                return
            if token.kind != "id":
                self.advance()
                continue
            name = str(self.advance().value)
            args, kwargs = self._parse_arguments()
            # Blocks.
            save = self.pos
            self.skip_newlines()
            block = False
            opener: Optional[Token] = None
            if self.peek().kind == "kw" and self.peek().value == "do":
                block = True
                opener = self.advance()
            elif self.peek().kind == "punct" and self.peek().value == "{":
                block = True
                opener = self.advance()
            else:
                self.pos = save
            if name == "language" and block:
                self._parse_language(args, kwargs, opener)
            elif name.startswith("on_") and block:
                self._parse_conditional(name, args, opener)
            elif name in _SCRIPT_STANZAS and block:
                self._capture_script(name, opener)
            elif name == "caveats" and block:
                self.cask.caveats_source = self._consume_block_body(opener, capture=True)
            elif name == "livecheck" and block:
                self.cask.livecheck_source = self._consume_block_body(opener, capture=True)
                self.cask.livecheck_defined = True
            elif block:
                self._skip_block_body(opener)
            else:
                self._apply(name, args, kwargs)

    def _parse_language(
        self, args: list[Any], kwargs: dict[Any, Any], opener: Optional[Token] = None
    ) -> None:
        code = str(args[0]) if args else "en"
        is_default = bool(kwargs.get("default") or kwargs.get(":default"))
        if is_default and not self._default_language_seen:
            self._default_language_seen = True
            self.cask.language = code
            self._parse_block()
            return
        self._skip_block_body(opener)

    def _parse_conditional(self, name: str, args: list[Any], opener: Optional[Token] = None) -> None:
        if name in ("on_macos", "on_linux"):
            self.cask.on_system_blocks = True
        active = self._conditional_active(name, args)
        if active:
            release = name[3:] if name.startswith("on_") else ""
            if release in _ARCH_NAMES:
                modifier = str(args[0]) if args else ""
                if "or_older" in modifier:
                    self.cask.on_system_block_min_os = _OLDEST_MACOS_VERSION
                else:
                    self.cask.on_system_block_min_os = str(_ARCH_NAMES[release])
            self._parse_block()
        else:
            self._skip_block_body(opener)

    def _capture_script(self, name: str, opener: Optional[Token]) -> None:
        """Store a cask script body verbatim without evaluating it."""
        body = self._consume_block_body(opener, capture=True) if opener is not None else ""
        setattr(self.cask, name, body)

    def _conditional_active(self, name: str, args: list[Any]) -> bool:
        if name in ("on_arm", "on_arm64"):
            return self.cask.resolved_arch() == "arm64" or platform.machine().lower() in ("arm64", "aarch64")
        if name in ("on_intel", "on_x86_64"):
            return self.cask.resolved_arch() == "intel" or platform.machine().lower() in ("x86_64", "amd64")
        if name.startswith("on_") and name[3:] in _ARCH_NAMES:
            target = _ARCH_NAMES[name[3:]]
            current = macos_major_version()
            modifier = str(args[0]) if args else ""
            if "or_older" in modifier:
                return current <= target
            if "or_newer" in modifier:
                return current >= target
            return current == target
        return False

    def _parse_arguments(self) -> tuple[list[Any], dict[Any, Any]]:
        args: list[Any] = []
        kwargs: dict[Any, Any] = {}
        token = self.peek()
        parenthesised = token.kind == "punct" and token.value == "("
        if parenthesised:
            self.advance()
        continuation = False
        while True:
            token = self.peek()
            if token.kind == "eof":
                break
            if token.kind == "nl":
                if parenthesised or continuation:
                    self.advance()
                    continue
                break
            if token.kind == "kw" and token.value in ("do", "end"):
                break
            if token.kind == "punct" and token.value in (")", "}"):
                if parenthesised and token.value == ")":
                    self.advance()
                break
            if token.kind == "punct" and token.value == ",":
                self.advance()
                continue
            if (
                token.kind in ("id", "const", "str", "sym")
                and self.peek(1).kind == "punct"
                and self.peek(1).value == ":"
            ):
                key = str(self.advance().value)
                self.advance()
                kwargs[key] = self._parse_value()
                if self.peek().kind == "punct" and self.peek().value == ",":
                    self.advance()
                    continuation = True
                    while self.peek().kind == "nl":
                        self.advance()
                    continue
                break
            value = self._parse_value()
            if self.peek().kind == "op" and self.peek().value == "=>":
                self.advance()
                rhs = self._parse_value()
                args.append({value: rhs})
            else:
                args.append(value)
            if self.peek().kind == "punct" and self.peek().value == ",":
                self.advance()
                # A trailing comma permits a continuation line.
                while self.peek().kind == "nl":
                    self.advance()
                continue
            break
        return args, kwargs

    def _parse_value(self) -> Any:
        token = self.peek()
        if token.kind == "punct" and token.value == ":":
            self.advance()
            first = self.advance()
            text = str(first.value)
            last = first
            while self.peek().kind in ("id", "num", "const") and self.peek().start == last.end:
                part = self.advance()
                text += str(part.value)
                last = part
            return ":" + text
        if token.kind == "str":
            self.advance()
            return str(token.value)
        if token.kind == "sym":
            self.advance()
            return ":" + str(token.value)
        if token.kind == "num":
            self.advance()
            return token.value
        if token.kind == "words":
            self.advance()
            return [str(item) for item in token.value]
        if token.kind == "id":
            self.advance()
            if token.value in ("true", "false", "nil"):
                return {"true": True, "false": False, "nil": None}[str(token.value)]
            return str(token.value)
        if token.kind == "punct" and token.value == "[":
            self.advance()
            values: list[Any] = []
            while self.peek().kind != "eof":
                if self.peek().kind == "punct" and self.peek().value == "]":
                    self.advance()
                    break
                if self.peek().kind == "punct" and self.peek().value == ",":
                    self.advance()
                    continue
                if self.peek().kind == "nl":
                    self.advance()
                    continue
                values.append(self._parse_value())
            return values
        if token.kind == "punct" and token.value == "{":
            self.advance()
            values: dict[str, Any] = {}
            while self.peek().kind != "eof":
                if self.peek().kind == "punct" and self.peek().value == "}":
                    self.advance()
                    break
                key = self.advance()
                if key.kind not in ("id", "str", "sym"):
                    continue
                if self.peek().kind == "punct" and self.peek().value == ":":
                    self.advance()
                elif self.peek().kind == "op" and self.peek().value == "=>":
                    # Ruby hash-rocket form, common in ``url ... cookies:``.
                    self.advance()
                values[str(key.value)] = self._parse_value()
            return values
        self.advance()
        return str(token.value)

    def _skip_block_body(self, opener: Optional[Token | str] = None) -> None:
        self._consume_block_body(opener)

    def _consume_block_body(
        self, opener: Optional[Token | str] = None, capture: bool = False
    ) -> str:
        """Consume a block body, optionally returning its raw source text.

        ``opener`` is the ``do``/``{`` token that introduced the block.  For
        legacy callers that only pass a string (or nothing), a ``do`` block is
        assumed.  The scan mirrors the original skip logic but tracks a stack
        so brace-delimited blocks and hash literals are balanced correctly.
        """

        if isinstance(opener, Token):
            closer = "end" if opener.kind == "kw" else "}"
            body_start = opener.end
        else:
            closer = "end" if (opener or "do") == "do" else "}"
            body_start = self.peek().start
        stack = [closer]
        while self.peek().kind != "eof":
            token = self.peek()
            expected = stack[-1]
            if expected == "end":
                if token.kind == "kw" and token.value == "end":
                    stack.pop()
                    if not stack:
                        body_end = token.start
                        self.advance()
                        return self.source[body_start:body_end] if capture else ""
                    self.advance()
                    continue
                if token.kind == "kw" and token.value in _BLOCK_KEYWORDS:
                    stack.append("end")
                elif token.kind == "punct" and token.value == "{":
                    stack.append("}")
            else:
                if token.kind == "punct" and token.value == "}":
                    stack.pop()
                    if not stack:
                        body_end = token.start
                        self.advance()
                        return self.source[body_start:body_end] if capture else ""
                    self.advance()
                    continue
                if token.kind == "kw" and token.value in _BLOCK_KEYWORDS:
                    stack.append("end")
                elif token.kind == "punct" and token.value == "{":
                    stack.append("}")
            self.advance()
        raise FormulaParseError("unterminated cask block")

    def _apply(self, name: str, args: list[Any], kwargs: dict[Any, Any]) -> None:
        cask = self.cask
        if name == "version" and args:
            cask.version = str(args[0])
        elif name == "sha256":
            if args:
                cask.sha256 = str(args[0])
            elif kwargs:
                for key in _SHA256_KEYS:
                    if key in kwargs:
                        cask.sha256_map[key] = str(kwargs[key])
                arch = cask.resolved_arch()
                preferred = _sha256_key(arch)
                if preferred in cask.sha256_map:
                    cask.sha256 = cask.sha256_map[preferred]
                elif arch == "arm64" and "arm" in cask.sha256_map:
                    cask.sha256 = cask.sha256_map["arm"]
                elif arch != "arm64" and "intel" in cask.sha256_map:
                    cask.sha256 = cask.sha256_map["intel"]
        elif name == "url":
            if args:
                cask.url = str(args[0])
            if kwargs:
                cask.url_options = {str(key): value for key, value in kwargs.items()}
        elif name == "name" and args:
            cask.name = str(args[0]) if cask.name is None else f"{cask.name}, {args[0]}"
        elif name == "desc" and args:
            cask.desc = str(args[0])
        elif name == "homepage":
            if args:
                cask.homepage = str(args[0])
            browsed = kwargs.get("browsed") or kwargs.get(":browsed")
            if browsed is not None:
                cask.homepage_browsed = str(browsed)
        elif name == "auto_updates":
            cask.auto_updates = bool(args[0]) if args else True
        elif name == "arch":
            for key, value in kwargs.items():
                if key in ("arm", "intel"):
                    self._arch_map[key] = str(value)
                    cask.arch_map[key] = str(value)
            if "arm" in kwargs and "intel" in kwargs:
                cask.arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "intel"
        elif name in _ARTIFACT_KINDS:
            source = str(args[0]) if args else ""
            target = kwargs.get("target")
            cask.artifacts.append(Artifact(name, source, str(target) if target else None, kwargs))
        elif name == "container":
            raw_type = kwargs.get("type") or kwargs.get(":type")
            if raw_type is not None:
                cask.container = str(raw_type).lstrip(":")
            nested = kwargs.get("nested") or kwargs.get(":nested")
            if nested is not None:
                cask.container_nested = str(nested)
            if kwargs:
                cask.container_options = {
                    str(key): value for key, value in kwargs.items()
                }
        elif name == "stage_only":
            if args:
                cask.stage_only = not (
                    args[0] is False
                    or str(args[0]).strip().lower() in ("false", "0", "no")
                )
            else:
                cask.stage_only = True
        elif name == "rename":
            pairs: list[tuple[str, str]] = []
            from_value = kwargs.get("from") or kwargs.get(":from")
            to_value = kwargs.get("to") or kwargs.get(":to")
            if from_value is not None and to_value is not None:
                pairs.append((str(from_value), str(to_value)))
            if len(args) == 1 and isinstance(args[0], list):
                for entry in args[0]:
                    if isinstance(entry, list) and len(entry) >= 2:
                        pairs.append((str(entry[0]), str(entry[1])))
            elif len(args) >= 2:
                for index in range(0, len(args) - 1, 2):
                    pairs.append((str(args[index]), str(args[index + 1])))
            if pairs:
                cask.renames.extend(pairs)
            else:
                cask.extra["rename"] = {"args": args, "kwargs": kwargs}
        elif name == "uninstall":
            cask.uninstall.update({str(k): v for k, v in kwargs.items()})
            if args:
                cask.uninstall["_args"] = args
        elif name == "zap":
            trash = kwargs.get("trash") or kwargs.get(":trash")
            if isinstance(trash, list):
                cask.zap.extend(str(item) for item in trash)
            elif trash:
                cask.zap.append(str(trash))
        elif name == "os":
            for key, value in kwargs.items():
                if key in ("macos", "linux"):
                    cask.os[key] = value
        elif name == "depends_on":
            cask.depends_on.update({str(k): v for k, v in kwargs.items()})
        elif name == "conflicts_with":
            cask.conflicts_with.update({str(k): v for k, v in kwargs.items()})
        elif name == "caveats":
            if args:
                cask.caveats = str(args[0])
        elif name in ("deprecate!", "deprecate_args"):
            cask.deprecated = True
            cask.deprecate_args = dict(kwargs)
            _apply_lifecycle_fields(cask, "deprecation", args, kwargs)
        elif name == "disable!":
            cask.disabled = True
            cask.disable_args = dict(kwargs)
            _apply_lifecycle_fields(cask, "disable", args, kwargs)
        elif name == "livecheck":
            self.cask.livecheck_defined = True
        elif name == "no_autobump!":
            cask.no_autobump = True
            reason = kwargs.get("because") or (args[0] if args else None)
            if reason is not None:
                cask.no_autobump_reason = str(reason)
                cask.no_autobump_message = str(reason)
        else:
            cask.extra[name] = {"args": args, "kwargs": kwargs}


def parse_cask_source(source: str, path: Optional[Path] = None, tap: Optional[str] = None) -> Cask:
    return CaskParser(source, path=path, tap=tap).parse()


def parse_cask_file(path: str | Path, tap: Optional[str] = None) -> Cask:
    cask_path = Path(path)
    return parse_cask_source(cask_path.read_text(encoding="utf-8", errors="replace"), cask_path, tap)
