"""Parser and model for Homebrew Cask definitions."""

from __future__ import annotations

import codecs
import datetime
import email.utils
import functools
import hashlib
import os
import platform
import plistlib
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

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

    def english_article(self) -> str:
        return "an" if self.english_name()[:1] in "aeiou" else "a"

    def config(self) -> dict[str, Any]:
        value = self.kwargs.get("config")
        return value if isinstance(value, dict) else {}

    def dirmethod(self) -> str:
        return "mkdir_p"

    def cask_sandbox(self) -> bool:
        return False

    def run_cask_sandbox(self) -> bool:
        return False

    def requires_sudo_q(self) -> bool:
        return self.requires_sudo()

    def read_script_arguments(self, arguments: Optional[list[str]] = None) -> list[str]:
        return list(arguments or [])


@dataclass
class Cask:
    token: str
    path: Optional[Path] = None
    #: Original Ruby source for a cask loaded via ``from_content``.  Kept so
    #: installs can still preserve ``.metadata/Casks/<token>.rb`` after the
    #: temporary source file is cleaned up.
    source_content: Optional[str] = None
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
    #: Non-trash zap directives (``delete``/``rmdir``/``pkgutil``/``quit``/
    #: ``signal``/``login_item``).  Signal pairs are stored as ``[signal, id]``.
    zap_directives: dict[str, list[Any]] = field(default_factory=dict)
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

    def check_livecheck(
        self,
        *,
        fetcher: Optional[Callable[[str], Any]] = None,
        page: Optional[str | bytes] = None,
        timeout: Optional[float] = None,
        online: bool = False,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Bounded livecheck strategy check for CLI/caller use.

        Delegates to :func:`pybrew.cask_ruby.execute_livecheck_strategy` (the
        local import avoids a ``cask`` <-> ``cask_ruby`` cycle).  No network is
        performed unless the caller injects a ``fetcher`` or passes ``page``
        content; arbitrary Ruby still requires ``PYBREW_ALLOW_CASK_RUBY=1``.
        """

        from .cask_ruby import STRATEGY_TIMEOUT, execute_livecheck_strategy

        effective_fetcher = fetcher
        if effective_fetcher is None and online:
            effective_fetcher = functools.partial(
                livecheck_http_fetcher,
                headers=headers if headers is not None else self._livecheck_headers(),
            )
        return execute_livecheck_strategy(
            self,
            fetcher=effective_fetcher,
            page=page,
            timeout=STRATEGY_TIMEOUT if timeout is None else timeout,
        )

    @property
    def livecheck_result(self) -> Optional[dict[str, Any]]:
        """Return the last stored livecheck strategy result, if any."""

        if not isinstance(self.livecheck, dict):
            return None
        result = self.livecheck.get("strategy_result")
        return result if isinstance(result, dict) else None

    @property
    def version_csv(self) -> list[str]:
        """Return the cask version split on commas (Homebrew ``Version#csv``)."""

        if not self.version:
            return []
        return [
            part.strip()
            for part in str(self.version).split(",")
            if part.strip()
        ]

    def livecheck_status(
        self,
        *,
        fetcher: Optional[Callable[[str], Any]] = None,
        page: Optional[str | bytes] = None,
        timeout: Optional[float] = None,
        online: bool = False,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Run a bounded livecheck and compare it with the current version.

        Returns a structured, JSON-friendly dict with ``status`` one of
        ``up_to_date``/``outdated``/``skipped``/``unknown``/``error`` plus the
        normalized candidates.  Explicit livecheck ``skip`` and (for casks
        without a livecheck block) date-deferred deprecation/disable produce
        ``skipped`` before any fetch.  No network is performed unless the
        caller injects a ``fetcher``; a strategy block additionally requires the usual
        ``PYBREW_ALLOW_CASK_RUBY=1`` opt-in.  When no strategy block exists but
        a livecheck ``regex`` is configured, the regex is applied to the page.
        """

        livecheck = self.livecheck if isinstance(self.livecheck, dict) else {}
        status: dict[str, Any] = {
            "token": self.token,
            "status": "unknown",
            "current_version": self.version,
            "current_versions": self.version_csv,
            "latest_version": None,
            "candidates": [],
            "reason": None,
            "error": None,
            "source": None,
            "result": None,
        }

        # Homebrew's livecheck skip conditions: an explicit livecheck `skip`
        # always wins; cask deprecation/disable (already date-deferred by the
        # parser) only skips when no livecheck was defined for the cask.
        if bool(livecheck.get("skip")):
            status.update(
                status="skipped",
                reason=str(livecheck.get("skip_msg") or "livecheck is skipped"),
            )
            return status
        if not self.livecheck_defined:
            if bool(getattr(self, "disabled", False)):
                status.update(status="skipped", reason="cask is disabled")
                return status
            if bool(getattr(self, "deprecated", False)):
                status.update(status="skipped", reason="cask is deprecated")
                return status

        effective_fetcher = fetcher
        if effective_fetcher is None and online:
            effective_fetcher = functools.partial(
                livecheck_http_fetcher,
                headers=headers if headers is not None else self._livecheck_headers(),
            )

        strategy_source = livecheck.get("strategy_source")
        strategy_name = (
            str(livecheck.get("strategy") or "")
            .strip()
            .lstrip(":")
            .lower()
            .replace("-", "_")
        )
        # Built-in strategies fetch through the dispatch layer (which applies
        # their URL options/verification); only arbitrary strategy blocks and
        # regex-only livechecks need the status layer to prefetch.
        prefetch_page = bool(strategy_source) or not strategy_name

        page_text: Optional[str] = None
        if page is not None:
            if isinstance(page, bytes):
                page_text = page.decode("utf-8", errors="replace")
            else:
                page_text = str(page)
        elif effective_fetcher is not None and prefetch_page:
            url = self._livecheck_url()
            if not url:
                status.update(
                    status="unknown",
                    reason="cask has no URL to fetch for livecheck",
                )
                return status
            try:
                fetched = effective_fetcher(url)
            except Exception as error:  # caller-supplied/default transport
                status.update(
                    status="error",
                    error=f"{type(error).__name__}: {error}",
                    reason="livecheck page fetch failed",
                )
                return status
            if isinstance(fetched, bytes):
                fetched = fetched.decode("utf-8", errors="replace")
            page_text = None if fetched is None else str(fetched)

        regex_source = livecheck.get("regex")
        if strategy_source or strategy_name:
            status["source"] = "strategy"
            result = self.check_livecheck(
                page=page_text,
                fetcher=(
                    effective_fetcher if page_text is None else None
                ),
                timeout=timeout,
            )
            status["result"] = result
            if result.get("error"):
                status.update(
                    status="error",
                    error=result["error"],
                    reason="livecheck strategy failed",
                )
                return status
            if not result.get("executed"):
                status.update(
                    status="unknown",
                    reason=result.get("reason") or "livecheck strategy did not run",
                )
                return status
            candidates = _normalize_livecheck_candidates(result.get("result"))
        elif regex_source:
            status["source"] = "regex"
            if page_text is None:
                status.update(
                    status="unknown",
                    reason="no page content or fetcher supplied; no network is performed",
                )
                return status
            try:
                candidates = _livecheck_regex_candidates(
                    str(regex_source), page_text
                )
            except re.error as error:
                status.update(
                    status="error",
                    error=f"invalid livecheck regex: {error}",
                    reason="livecheck regex is invalid",
                )
                return status
        else:
            status.update(
                status="unknown",
                reason="cask has no livecheck strategy or regex",
            )
            return status

        status["candidates"] = candidates
        if not candidates:
            status.update(
                status="unknown",
                reason="livecheck produced no version candidates",
            )
            return status
        if self.version is None or str(self.version) in ("latest", ":latest"):
            status.update(
                status="unknown",
                reason="cask has no comparable current version",
            )
            return status

        current_versions = self.version_csv or [str(self.version)]
        for candidate in candidates:
            for current in current_versions:
                if compare_versions(candidate, current) == 0:
                    status.update(status="up_to_date", latest_version=candidate)
                    return status
        status.update(
            status="outdated",
            latest_version=_latest_livecheck_candidate(candidates),
        )
        return status

    def _livecheck_url(self) -> Optional[str]:
        if isinstance(self.livecheck, dict):
            value = self.livecheck.get("url")
            if isinstance(value, str) and value:
                return value
        return self.url

    def _livecheck_headers(self) -> dict[str, str]:
        """Build request headers from cask + livecheck ``url`` options.

        Cask-level ``url_options`` are the base; livecheck-specific
        ``url`` kwargs (captured by the opt-in evaluator) override them.
        """

        try:
            from .cask_installer import cask_request_headers

            headers = cask_request_headers(self)
            if isinstance(self.livecheck, dict):
                livecheck_options = self.livecheck.get("url_options")
                if isinstance(livecheck_options, dict):
                    headers.update(
                        cask_request_headers(self, options=livecheck_options)
                    )
            return headers
        except Exception:  # pragma: no cover - avoids import-cycle edge cases
            return {}

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

    def staged_path(self) -> Path:
        return self.caskroom_path / str(self.version or "")

    def os_version(self) -> str:
        import platform

        return platform.mac_ver()[0] or platform.release()

    def language_eval(self, language: Optional[str] = None) -> str:
        return language or self.language or self.default_language_group()

    def language_groups(self) -> dict[str, list[str]]:
        return {}

    def default_language_group(self) -> str:
        return "en"

    def temp(self) -> Path:
        import tempfile

        return Path(tempfile.gettempdir()) / "pybrew-cask" / self.token

    def cleanup_temp(self, retain: bool = False) -> None:
        if retain:
            return
        import shutil

        shutil.rmtree(self.temp(), ignore_errors=True)

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
        return bool(self.artifacts) and all(
            artifact.kind == "font" for artifact in self.artifacts
        )

    def url_specs(self) -> list[str]:
        return [self.url] if self.url else []

    def checksumable(self) -> bool:
        strategy = str((self.url_options or {}).get("using") or "").lstrip(":").lower()
        if strategy in {"git", "svn", "hg", "fossil"}:
            return False
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


#: Bounded defaults for the opt-in livecheck HTTP transport.
LIVECHECK_HTTP_TIMEOUT = 30.0
LIVECHECK_MAX_PAGE_BYTES = 5 * 1024 * 1024
LIVECHECK_MAX_REDIRECTS = 5
LIVECHECK_RETRIES = 2
LIVECHECK_RETRY_DELAY = 0.25
LIVECHECK_USER_AGENT = "pybrew-livecheck/0.1"

#: HTTP statuses retried by the default livecheck transport.
LIVECHECK_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Retry policy bounds for the default livecheck transport.
LIVECHECK_MAX_RETRY_AFTER = 10.0
LIVECHECK_MAX_TOTAL_RETRY_SECONDS = 10.0
LIVECHECK_JITTER = 0.2

#: Headers stripped when a redirect crosses origins (scheme/host/port).
LIVECHECK_SENSITIVE_HEADERS = frozenset(
    {"authorization", "cookie", "cookie2", "proxy-authorization"}
)

#: Default per-host rate-limit bucket for the livecheck transport.
LIVECHECK_RATE_LIMIT_CAPACITY = 10.0
LIVECHECK_RATE_LIMIT_REFILL = 2.0


class LivecheckRateLimiter:
    """Small per-host token bucket for the opt-in livecheck transport.

    ``capacity`` is the burst size and ``refill_rate`` is tokens per second.
    ``acquire(host)`` returns the seconds a caller should wait (0 when a token
    is available) and never blocks itself.
    """

    def __init__(
        self,
        *,
        capacity: float = LIVECHECK_RATE_LIMIT_CAPACITY,
        refill_rate: float = LIVECHECK_RATE_LIMIT_REFILL,
        clock: Optional[Any] = None,
    ) -> None:
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.clock = clock or time.monotonic
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        """Forget all buckets (mainly useful for tests/CLI lifecycle)."""

        with self._lock:
            self._buckets.clear()

    def acquire(self, host: str) -> float:
        with self._lock:
            now = self.clock()
            tokens, last = self._buckets.get(
                host, (self.capacity, now)
            )
            tokens = min(
                self.capacity,
                tokens + max(0.0, now - last) * self.refill_rate,
            )
            if tokens >= 1.0:
                self._buckets[host] = (tokens - 1.0, now)
                return 0.0
            if self.refill_rate <= 0:
                return float("inf")
            wait = (1.0 - tokens) / self.refill_rate
            self._buckets[host] = (0.0, now)
            return wait


_DEFAULT_LIVECHECK_RATE_LIMITER = LivecheckRateLimiter()


class _BoundedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler with bounded hops and configurable header stripping."""

    max_redirections = LIVECHECK_MAX_REDIRECTS

    def __init__(
        self,
        *,
        strip_headers: Optional[Any] = None,
        preserve_headers: Optional[Any] = None,
    ) -> None:
        super().__init__()
        if strip_headers is None:
            strip_headers = LIVECHECK_SENSITIVE_HEADERS
        self.strip_headers = frozenset(
            str(header).lower() for header in strip_headers
        )
        self.preserve_headers = frozenset(
            str(header).lower() for header in (preserve_headers or ())
        )

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_request = super().redirect_request(
            req, fp, code, msg, headers, newurl
        )
        if new_request is None:
            return None
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(new_request.full_url)
        if (old.scheme, old.hostname, old.port) != (
            new.scheme,
            new.hostname,
            new.port,
        ):
            for header in self.strip_headers - self.preserve_headers:
                new_request.remove_header(header.capitalize())
        return new_request


def _livecheck_retry_after(error: Any) -> Optional[float]:
    """Return a bounded Retry-After delay in seconds, if the error has one."""

    headers = getattr(error, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if parsed is None:
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        if parsed.tzinfo is None:
            now = now.replace(tzinfo=None)
        seconds = (parsed - now).total_seconds()
    return max(0.0, min(seconds, LIVECHECK_MAX_RETRY_AFTER))


def _livecheck_retry_delay(
    error: Optional[BaseException],
    attempt: int,
    retry_delay: float,
    jitter: float,
) -> float:
    """Delay before the next attempt: Retry-After wins over jittered backoff."""

    if error is not None:
        retry_after = _livecheck_retry_after(error)
        if retry_after is not None:
            return retry_after
    if retry_delay <= 0:
        return 0.0
    delay = retry_delay * (attempt + 1)
    if jitter > 0:
        import random

        delay += random.uniform(0.0, retry_delay * min(jitter, 1.0))
    return delay


def _livecheck_http_header(response: Any, name: str) -> Optional[str]:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except (AttributeError, TypeError):
        return None
    return None if value is None else str(value)


def _livecheck_response_charset(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if headers is not None:
        getter = getattr(headers, "get_content_charset", None)
        if callable(getter):
            try:
                charset = getter()
            except Exception:  # pragma: no cover - exotic header objects
                charset = None
            if charset:
                return str(charset)
    content_type = _livecheck_http_header(response, "Content-Type") or ""
    match = re.search(
        r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", content_type, re.IGNORECASE
    )
    return match.group(1) if match is not None else "utf-8"


def _brotli_decompress(body: bytes, max_bytes: int) -> bytes:
    """Decode a brotli body when an optional decoder is installed."""

    brotli_module: Optional[Any] = None
    for module_name in ("brotli", "brotlicffi"):
        try:
            module = __import__(module_name)
        except ImportError:
            continue
        if hasattr(module, "decompress"):
            brotli_module = module
            break
    if brotli_module is None:
        raise CaskError(
            "brotli content-encoding is not supported: install the optional "
            "'brotli' or 'brotlicffi' package"
        )
    try:
        decoded = brotli_module.decompress(body)
    except Exception as error:
        raise CaskError(f"invalid brotli livecheck page: {error}") from error
    if len(decoded) > max_bytes:
        raise CaskError(
            f"livecheck page exceeds the {max_bytes}-byte limit "
            "after brotli decompression"
        )
    return decoded


def _decode_livecheck_body(
    body: bytes, content_encoding: Optional[str], charset: str, max_bytes: int
) -> str:
    """Best-effort content-encoding + charset decoding for livecheck pages."""

    if content_encoding:
        encoding = content_encoding.strip().lower()
        decompressor: Optional[Any] = None
        if encoding in ("gzip", "x-gzip"):
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            decompressor = zlib.decompressobj()
        elif encoding == "br":
            body = _brotli_decompress(body, max_bytes)
        elif encoding not in ("identity", ""):
            raise CaskError(
                f"unsupported livecheck content-encoding: {encoding}"
            )
        if decompressor is not None:
            try:
                decoded = decompressor.decompress(body, max_bytes + 1)
            except zlib.error:
                decoded = None
            if decoded is not None:
                if len(decoded) > max_bytes:
                    raise CaskError(
                        f"livecheck page exceeds the {max_bytes}-byte limit "
                        "after decompression"
                    )
                body = decoded
    try:
        codec_name = codecs.lookup(charset).name
    except LookupError:
        codec_name = "utf-8"
    try:
        return body.decode(codec_name, errors="replace")
    except (LookupError, UnicodeError):  # pragma: no cover - codecs lookup done
        return body.decode("utf-8", errors="replace")


def _livecheck_curl_fetch(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    headers: dict[str, str],
    method: str,
    include_response: bool,
    binary: bool,
) -> Any:
    """Bounded curl transport with safe cross-origin redirect handling."""

    executable = os.environ.get("PYBREW_CURL") or shutil.which("curl")
    if not executable:
        raise CaskError("curl transport requested but no curl executable was found")
    current_url = url
    current_headers = dict(headers)
    for _redirect in range(LIVECHECK_MAX_REDIRECTS + 1):
        parsed = urllib.parse.urlsplit(current_url)
        if parsed.scheme not in ("http", "https"):
            raise CaskError(
                f"unsupported livecheck URL scheme: {parsed.scheme or '(none)'}"
            )
        command = [
            executable,
            "--silent",
            "--show-error",
            "--max-time",
            f"{timeout:g}",
            "--max-filesize",
            str(max_bytes),
            "--proto",
            "=http,https",
            "-X",
            method,
            "-D",
            "-",
        ]
        for key, value in current_headers.items():
            command.extend(["-H", f"{key}: {value}"])
        command.append(current_url)
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout + 5,
            )
        except subprocess.TimeoutExpired as error:
            raise CaskError("curl livecheck request timed out") from error
        except OSError as error:
            raise CaskError(f"could not run curl: {error}") from error
        if completed.returncode != 0:
            detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
            raise CaskError(f"curl livecheck request failed: {detail or completed.returncode}")
        raw = completed.stdout or b""
        header_part, separator, body_part = raw.partition(b"\r\n\r\n")
        if not separator:
            header_part, separator, body_part = raw.partition(b"\n\n")
        header_text = header_part.decode("latin-1", errors="replace")
        response_headers: dict[str, str] = {}
        status_code: Optional[int] = None
        for line in header_text.splitlines():
            if line.upper().startswith("HTTP/"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    status_code = int(parts[1])
                continue
            if ":" in line:
                key, value = line.split(":", 1)
                response_headers[key.strip()] = value.strip()
        location = None
        for key, value in response_headers.items():
            if key.lower() == "location":
                location = value
                break
        if status_code is not None and 300 <= status_code < 400 and location:
            target = urllib.parse.urljoin(current_url, location)
            old = urllib.parse.urlsplit(current_url)
            new = urllib.parse.urlsplit(target)
            if (old.scheme, old.hostname, old.port) != (
                new.scheme,
                new.hostname,
                new.port,
            ):
                for sensitive in LIVECHECK_SENSITIVE_HEADERS:
                    for key in list(current_headers):
                        if key.lower() == sensitive:
                            current_headers.pop(key)
            current_url = target
            continue
        if len(body_part) > max_bytes:
            raise CaskError(
                f"livecheck page exceeds the {max_bytes}-byte limit"
            )
        if binary:
            body: Any = body_part
        else:
            body = body_part.decode("utf-8", errors="replace")
        if include_response:
            return {
                "body": body,
                "headers": {
                    key.lower(): value
                    for key, value in response_headers.items()
                },
                "status": status_code,
                "url": current_url,
            }
        return body
    raise CaskError("too many livecheck redirects")


def livecheck_http_fetcher(
    url: str,
    *,
    timeout: float = LIVECHECK_HTTP_TIMEOUT,
    max_bytes: int = LIVECHECK_MAX_PAGE_BYTES,
    headers: Optional[dict[str, str]] = None,
    retries: int = LIVECHECK_RETRIES,
    retry_delay: float = LIVECHECK_RETRY_DELAY,
    jitter: float = LIVECHECK_JITTER,
    max_total_retry_seconds: float = LIVECHECK_MAX_TOTAL_RETRY_SECONDS,
    opener: Optional[Any] = None,
    rate_limiter: Optional[Any] = _DEFAULT_LIVECHECK_RATE_LIMITER,
    redirect_strip_headers: Optional[Any] = None,
    redirect_preserve_headers: Optional[Any] = None,
    method: str = "GET",
    include_response: bool = False,
    binary: bool = False,
    using: Optional[str] = None,
) -> Any:
    """Bounded default HTTP transport for ``pybrew livecheck --online``.

    Only ``http``/``https`` URLs are accepted.  Redirects are followed through
    :class:`_BoundedRedirectHandler` with configurable stripped/preserved
    headers; transient failures (connection errors, timeouts, 408/425/429/5xx)
    are retried a bounded number of times, honoring Retry-After when present.
    A per-host :class:`LivecheckRateLimiter` bucket (pass ``rate_limiter=None``
    to disable) bounds request bursts.  The response is capped and
    content-encoding/charset are decoded best-effort.
    The helper only runs when a caller explicitly passes ``online=True`` or a
    custom fetcher; default calls stay offline.
    """

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise CaskError(
            f"unsupported livecheck URL scheme: {parsed.scheme or '(none)'}"
        )

    request_headers = {"User-Agent": LIVECHECK_USER_AGENT}
    if headers:
        for key, value in headers.items():
            request_headers[str(key)] = str(value)
    if using is not None and str(using).lstrip(":").lower() in (
        "curl",
        "homebrew_curl",
    ):
        return _livecheck_curl_fetch(
            url,
            timeout=timeout,
            max_bytes=max_bytes,
            headers=request_headers,
            method=method,
            include_response=include_response,
            binary=binary,
        )
    request = urllib.request.Request(
        url, headers=request_headers, method=method
    )
    effective_opener = (
        opener
        if opener is not None
        else urllib.request.build_opener(
            _BoundedRedirectHandler(
                strip_headers=redirect_strip_headers,
                preserve_headers=redirect_preserve_headers,
            )
        )
    )

    attempts = max(0, int(retries)) + 1
    body: Optional[bytes] = None
    content_encoding: Optional[str] = None
    charset = "utf-8"
    response_headers: dict[str, str] = {}
    response_status: Optional[int] = None
    final_url = url
    last_error: Optional[BaseException] = None
    total_delay = 0.0
    host = urllib.parse.urlsplit(url).hostname or ""
    for attempt in range(attempts):
        if rate_limiter is not None:
            wait = rate_limiter.acquire(host)
            if wait > 0:
                if total_delay + wait > max_total_retry_seconds:
                    raise CaskError(
                        "livecheck rate limit wait exceeds the bounded retry "
                        "budget"
                    )
                total_delay += wait
                time.sleep(wait)
        try:
            with effective_opener.open(request, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
                content_encoding = _livecheck_http_header(
                    response, "Content-Encoding"
                )
                charset = _livecheck_response_charset(response)
                try:
                    response_headers = {
                        str(key).lower(): str(value)
                        for key, value in response.headers.items()
                    }
                except (AttributeError, TypeError):
                    response_headers = {}
                response_status = getattr(response, "status", None)
                final_url = getattr(response, "geturl", lambda: url)()
            break
        except urllib.error.HTTPError as error:
            last_error = error
            if (
                error.code not in LIVECHECK_TRANSIENT_STATUSES
                or attempt + 1 >= attempts
            ):
                raise
            delay = _livecheck_retry_delay(
                error, attempt, retry_delay, jitter
            )
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt + 1 >= attempts:
                raise
            # Some connection errors carry Retry-After-like headers; honor
            # them when present, otherwise use jittered backoff.
            delay = _livecheck_retry_delay(
                error, attempt, retry_delay, jitter
            )
        # Bounded circuit: never sleep/retry past the total retry budget.
        if total_delay + delay > max_total_retry_seconds:
            raise last_error
        total_delay += delay
        if delay > 0:
            time.sleep(delay)
    if body is None:
        if last_error is not None:
            raise last_error
        raise CaskError("livecheck HTTP request did not produce a response")
    if len(body) > max_bytes:
        raise CaskError(
            f"livecheck page exceeds the {max_bytes}-byte limit"
        )
    if binary:
        body_value: Any = body
    else:
        body_value = _decode_livecheck_body(
            body, content_encoding, charset, max_bytes
        )
    if include_response:
        return {
            "body": body_value,
            "headers": response_headers,
            "status": response_status,
            "url": final_url,
        }
    return body_value


def _normalize_livecheck_candidates(value: Any) -> list[str]:
    """Normalize a strategy result (or scalar) into version candidate strings."""

    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    candidates: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            candidates.append(text)
    return candidates


def _livecheck_regex_candidates(pattern: str, page: str) -> list[str]:
    """Extract unique version candidates from ``page`` with ``pattern``."""

    compiled = re.compile(pattern)
    candidates: list[str] = []
    seen: set[str] = set()
    for match in compiled.findall(page):
        if isinstance(match, tuple):
            text = next((group for group in match if group), "")
        else:
            text = match
        text = str(text).strip()
        if text and text not in seen:
            seen.add(text)
            candidates.append(text)
    return candidates


def _latest_livecheck_candidate(candidates: list[str]) -> Optional[str]:
    latest: Optional[str] = None
    for candidate in candidates:
        if latest is None or compare_versions(candidate, latest) > 0:
            latest = candidate
    return latest


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
    {
        "do",
        "if",
        "unless",
        "case",
        "begin",
        "while",
        "until",
        "for",
        # Ruby method/class/module definitions are balanced blocks too; the
        # extraction of best-effort script bodies must not stop at their
        # inner ``end``.
        "def",
        "class",
        "module",
    }
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


def _today() -> datetime.date:
    """Return the date used to activate ``deprecate!``/``disable!`` stanzas.

    Tests monkeypatch this hook for deterministic past/future date behavior.
    """

    return datetime.date.today()


def _parse_lifecycle_date(value: Any) -> Optional[datetime.date]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _lifecycle_date_is_future(value: Any) -> bool:
    """True when a deprecation/disable date has not been reached yet.

    Missing or unparseable dates are treated as already effective so a
    malformed cask keeps pybrew's historical immediate behavior.
    """

    parsed = _parse_lifecycle_date(value)
    if parsed is None:
        return False
    return parsed > _today()


def _apply_deprecation_semantics(cask: Cask) -> None:
    """Defer ``deprecate!`` activation until its date (Homebrew semantics)."""

    cask.deprecated = not _lifecycle_date_is_future(cask.deprecation_date)


def _apply_disable_semantics(cask: Cask) -> None:
    """Defer ``disable!`` until its date, promoting it to deprecation first."""

    if cask.disable_date is not None and _lifecycle_date_is_future(cask.disable_date):
        cask.disabled = False
        cask.deprecated = True
        cask.deprecation_reason = cask.disable_reason
        cask.deprecation_replacement_formula = cask.disable_replacement_formula
        cask.deprecation_replacement_cask = cask.disable_replacement_cask
    else:
        cask.disabled = True


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
            if name == "livecheck":
                self.skip_newlines()
                if self.peek().kind == "punct" and self.peek().value == "{":
                    brace_opener = self.advance()
                    self._parse_livecheck_block(brace_opener)
                    continue
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
                self._parse_livecheck_block(opener)
            elif block:
                self._skip_block_body(opener)
            else:
                self._apply(name, args, kwargs)

    def _parse_livecheck_block(self, opener: Token) -> None:
        """Capture livecheck metadata with literal-parser parity.

        The full raw body is kept in ``livecheck_source``; the inner ``url``
        kwargs, ``regex``, ``strategy`` symbol/source, ``skip`` and ``throttle``
        metadata mirror what the opt-in Ruby evaluator records.
        """

        cask = self.cask
        cask.livecheck_defined = True
        livecheck = cask.livecheck if isinstance(cask.livecheck, dict) else {}
        cask.livecheck = livecheck
        body_start = opener.end
        brace_block = opener.kind == "punct"
        while True:
            self.skip_newlines()
            token = self.peek()
            if token.kind == "eof":
                raise FormulaParseError("unterminated livecheck block")
            closes = (
                token.kind == "punct" and token.value == "}"
                if brace_block
                else token.kind == "kw" and token.value == "end"
            )
            if closes:
                body_end = token.start
                self.advance()
                cask.livecheck_source = self.source[body_start:body_end]
                return
            if token.kind != "id":
                self.advance()
                continue
            name = str(self.advance().value)
            args, kwargs = self._parse_arguments()
            save = self.pos
            self.skip_newlines()
            block_opener: Optional[Token] = None
            if self.peek().kind == "kw" and self.peek().value == "do":
                block_opener = self.advance()
            elif self.peek().kind == "punct" and self.peek().value == "{":
                block_opener = self.advance()
            else:
                self.pos = save
            if name == "url":
                if args:
                    livecheck["url"] = str(args[0])
                if kwargs:
                    livecheck["url_options"] = {
                        str(key).lstrip(":"): value
                        for key, value in kwargs.items()
                    }
            elif name == "regex" and args:
                pattern = str(args[0])
                match = re.fullmatch(r"/(.*)/([imx]*)", pattern, re.DOTALL)
                if match is not None:
                    pattern = match.group(1)
                livecheck["regex"] = pattern
            elif name == "strategy":
                if args:
                    livecheck["strategy"] = ":" + str(args[0]).lstrip(":")
                if block_opener is not None:
                    livecheck["strategy_source"] = (
                        self._consume_block_body(block_opener, capture=True).strip()
                    )
            elif name == "skip":
                livecheck["skip"] = True
                if args:
                    livecheck["skip_msg"] = str(args[0])
            elif name == "throttle":
                if args:
                    livecheck["throttle"] = args[0]
                if kwargs.get("days") is not None:
                    livecheck["throttle_days"] = kwargs["days"]
            elif block_opener is not None:
                self._skip_block_body(block_opener)
        return None

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
            if not parenthesised and token.kind == "punct" and token.value == ";":
                break
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

    #: Keywords that can be either a block opener or a trailing modifier.
    _MODIFIER_KEYWORDS = frozenset({"if", "unless", "while", "until"})

    def _token_starts_block(self, token: Token, index: int) -> bool:
        """True when a block keyword actually opens a block.

        Ruby allows ``puts x if cond`` style modifiers; treating those as
        openers would consume the enclosing block's ``end``.  A modifier is
        recognised when the keyword follows an expression-like token.
        """

        if token.kind != "kw" or token.value not in _BLOCK_KEYWORDS:
            return False
        if token.value not in self._MODIFIER_KEYWORDS or index <= 0:
            return True
        previous = self.tokens[index - 1]
        if previous.kind in ("id", "const", "num", "str", "sym", "words"):
            return False
        if previous.kind == "punct" and previous.value in (")", "]", "}"):
            return False
        if previous.kind == "kw" and previous.value in (
            "return",
            "break",
            "next",
            "redo",
            "retry",
            "yield",
            "end",
        ):
            return False
        return True

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
                if self._token_starts_block(token, self.pos):
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
                if self._token_starts_block(token, self.pos):
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
            for directive in ("delete", "rmdir", "pkgutil", "quit"):
                value = kwargs.get(directive) or kwargs.get(f":{directive}")
                if value is None:
                    continue
                values = value if isinstance(value, list) else [value]
                cask.zap_directives.setdefault(directive, []).extend(
                    str(item) for item in values
                )
            signal = kwargs.get("signal") or kwargs.get(":signal")
            if signal is not None:
                flat: list[str] = []
                for item in signal if isinstance(signal, list) else [signal]:
                    if isinstance(item, (list, tuple)):
                        flat.extend(str(part) for part in item)
                    else:
                        flat.append(str(item))
                pairs = [flat[index : index + 2] for index in range(0, len(flat), 2)]
                cask.zap_directives.setdefault("signal", []).extend(pairs)
            login_item = kwargs.get("login_item") or kwargs.get(":login_item")
            if login_item is not None:
                values = login_item if isinstance(login_item, list) else [login_item]
                cask.zap_directives.setdefault("login_item", []).extend(values)
            for directive in ("launchctl", "kext"):
                value = kwargs.get(directive) or kwargs.get(f":{directive}")
                if value is None:
                    continue
                values = value if isinstance(value, list) else [value]
                cask.zap_directives.setdefault(directive, []).extend(
                    str(item) for item in values
                )
            for directive in ("script", "early_script"):
                value = kwargs.get(directive) or kwargs.get(f":{directive}")
                if value is None:
                    continue
                values = value if isinstance(value, list) else [value]
                cask.zap_directives.setdefault(directive, []).extend(values)
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
            cask.deprecate_args = dict(kwargs)
            _apply_lifecycle_fields(cask, "deprecation", args, kwargs)
            _apply_deprecation_semantics(cask)
        elif name == "disable!":
            cask.disable_args = dict(kwargs)
            _apply_lifecycle_fields(cask, "disable", args, kwargs)
            _apply_disable_semantics(cask)
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
