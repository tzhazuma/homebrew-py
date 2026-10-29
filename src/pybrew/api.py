"""Optional Homebrew JSON API client.

This is used when a formula is not available in a local tap.  pybrew can read
the same ``formulae.brew.sh`` API that Homebrew uses, which makes the prototype
usable on a machine without a homebrew-core checkout.
"""

from __future__ import annotations

import json
from pathlib import Path
import urllib.error
import urllib.request
from typing import Any, Optional

from .download import USER_AGENT
from .cask import Artifact, Cask
from .errors import DownloadError, FormulaNotFound
from .models import BottleFile, BottleSpec, Dependency, Formula, SourceSpec

DEFAULT_API = "https://formulae.brew.sh/api/formula"


def _request_json(url: str, timeout: int = 30) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise FormulaNotFound(url) from error
        raise DownloadError(f"{url}: HTTP {error.code}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise DownloadError(f"{url}: {error}") from error


def _deps(values: list[Any]) -> list[Dependency]:
    return [Dependency(str(value)) for value in values or []]


def formula_from_api(data: dict[str, Any], tap: Optional[str] = None) -> Formula:
    if "formulae" in data:
        data = data["formulae"][0]
    formula = Formula(name=str(data["name"]))
    formula.tap = tap or data.get("tap") or "homebrew/core"
    formula.desc = data.get("desc") or ""
    formula.homepage = data.get("homepage") or ""
    formula.license = data.get("license")
    formula.version = (data.get("versions") or {}).get("stable")
    formula.revision = int(data.get("revision") or 0)
    formula.version_scheme = int(data.get("version_scheme") or 0)
    formula.keg_only = bool(data.get("keg_only"))
    reason = data.get("keg_only_reason") or {}
    formula.keg_only_reason = reason.get("reason") if isinstance(reason, dict) else None
    formula.dependencies = _deps(data.get("dependencies"))
    formula.build_dependencies = _deps(data.get("build_dependencies"))
    formula.test_dependencies = _deps(data.get("test_dependencies"))
    formula.recommended_dependencies = _deps(data.get("recommended_dependencies"))
    formula.optional_dependencies = _deps(data.get("optional_dependencies"))
    formula.uses_from_macos = _deps(data.get("uses_from_macos"))
    formula.conflicts_with = [str(item) for item in data.get("conflicts_with") or []]
    formula.link_overwrite = [str(item) for item in data.get("link_overwrite") or []]
    formula.caveats = data.get("caveats") or ""

    stable_url = (data.get("urls") or {}).get("stable") or {}
    formula.stable = SourceSpec(
        url=stable_url.get("url"),
        sha256=stable_url.get("checksum"),
        tag=stable_url.get("tag"),
        revision=stable_url.get("revision"),
        using=stable_url.get("using"),
    )
    head_url = (data.get("urls") or {}).get("head") or {}
    formula.head = SourceSpec(
        url=head_url.get("url"),
        branch=head_url.get("branch"),
        using=head_url.get("using"),
    )

    bottle = (data.get("bottle") or {}).get("stable") or {}
    formula.bottle = BottleSpec(
        root_url=bottle.get("root_url"),
        rebuild=int(bottle.get("rebuild") or 0),
        files={
            str(tag): BottleFile(str(tag), str(info.get("sha256")), info.get("cellar"))
            for tag, info in (bottle.get("files") or {}).items()
            if isinstance(info, dict) and info.get("sha256")
        },
    )
    return formula


def fetch_formula(name: str, api_url: str = DEFAULT_API, timeout: int = 30) -> Formula:
    data = _request_json(f"{api_url.rstrip('/')}/{name}.json", timeout=timeout)
    return formula_from_api(data)


def cask_from_api(data: dict[str, Any], tap: Optional[str] = None) -> Cask:
    """Convert a formulae.brew.sh cask JSON object into a :class:`Cask`."""

    cask = Cask(token=str(data["token"]))
    cask.tap = tap or data.get("tap") or "homebrew/cask"
    cask.version = str(data.get("version")) if data.get("version") is not None else None
    cask.sha256 = data.get("sha256")
    cask.url = data.get("url")
    names = data.get("name") or []
    if isinstance(names, list):
        cask.name = ", ".join(str(item) for item in names) or None
    else:
        cask.name = str(names)
    cask.desc = data.get("desc")
    cask.homepage = data.get("homepage")
    cask.auto_updates = bool(data.get("auto_updates"))
    cask.stage_only = bool(data.get("stage_only", False))
    cask.caveats = data.get("caveats")
    depends = data.get("depends_on") or {}
    if isinstance(depends, dict):
        cask.depends_on = {str(key): value for key, value in depends.items()}
    conflicts = data.get("conflicts_with") or {}
    if isinstance(conflicts, dict):
        cask.conflicts_with = {str(key): value for key, value in conflicts.items()}

    for entry in data.get("artifacts") or []:
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            if key == "uninstall":
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, dict):
                        cask.uninstall.update({str(k): v for k, v in item.items()})
            elif key == "zap":
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, dict):
                        trash = item.get("trash")
                        if isinstance(trash, list):
                            cask.zap.extend(str(path) for path in trash)
                        elif trash:
                            cask.zap.append(str(trash))
            elif key in (
                "app",
                "binary",
                "pkg",
                "suite",
                "artifact",
                "installer",
                "appimage",
                "font",
                "colorpicker",
                "prefpane",
                "qlplugin",
                "mdimporter",
                "screen_saver",
                "service",
                "audio_unit_plugin",
                "vst_plugin",
                "vst3_plugin",
                "dictionary",
                "input_method",
                "keyboard_layout",
            ):
                if isinstance(value, list):
                    sources = [str(item) for item in value if isinstance(item, str)]
                else:
                    sources = [str(value)]
                if not sources:
                    continue
                target = entry.get("target")
                target_string = str(target) if target else None
                if target_string:
                    # API cask targets are absolute system paths; keep only the
                    # basename so pybrew installs inside its own prefix.
                    target_string = Path(
                        target_string.replace("$HOMEBREW_PREFIX", "")
                    ).name
                for source in sources:
                    cask.artifacts.append(
                        Artifact(
                            key,
                            source,
                            target_string if key in ("app", "binary") or key in (
                                "font",
                                "colorpicker",
                                "prefpane",
                                "qlplugin",
                                "mdimporter",
                                "screen_saver",
                                "service",
                                "audio_unit_plugin",
                                "vst_plugin",
                                "vst3_plugin",
                                "dictionary",
                                "input_method",
                                "keyboard_layout",
                            ) else None,
                        )
                    )
            elif key == "stage_only":
                cask.stage_only = bool(value)
    return cask


def fetch_cask(token: str, api_url: str = "https://formulae.brew.sh/api/cask", timeout: int = 30) -> Cask:
    data = _request_json(f"{api_url.rstrip('/')}/{token}.json", timeout=timeout)
    return cask_from_api(data)
