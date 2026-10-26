"""Optional Homebrew JSON API client.

This is used when a formula is not available in a local tap.  pybrew can read
the same ``formulae.brew.sh`` API that Homebrew uses, which makes the prototype
usable on a machine without a homebrew-core checkout.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

from .download import USER_AGENT
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
