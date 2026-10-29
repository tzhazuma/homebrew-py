"""Domain models shared by the parser, installer and CLI."""

from __future__ import annotations

import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import PYBREW_VERSION
from .version import detect_version


class Sym(str):
    """A Ruby symbol.

    Symbols are string-like so that they can be compared with the string
    literals Homebrew accepts in many places, but the extra type lets the
    parser preserve the distinction when it matters.
    """

    __slots__ = ()


@dataclass(frozen=True)
class Opaque:
    """An expression the literal-only parser deliberately did not evaluate."""

    text: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


def unwrap(value: Any) -> Any:
    """Return a plain Python value for Sym/Opaque values."""

    if isinstance(value, Sym):
        return str(value)
    if isinstance(value, Opaque):
        return value.text
    if isinstance(value, (list, tuple)):
        return [unwrap(item) for item in value]
    if isinstance(value, dict):
        return {str(key): unwrap(item) for key, item in value.items()}
    return value


def _tag_set(tags: Any) -> frozenset[str]:
    if tags is None:
        return frozenset()
    if isinstance(tags, (str, Sym)):
        return frozenset({str(tags).lstrip(":")})
    if isinstance(tags, (list, tuple, set, frozenset)):
        return frozenset(str(item).lstrip(":") for item in tags)
    return frozenset()


@dataclass(frozen=True)
class Dependency:
    """A formula dependency such as ``depends_on "openssl@3"``."""

    name: str
    tags: frozenset[str] = frozenset()

    @property
    def build(self) -> bool:
        return "build" in self.tags

    @property
    def test(self) -> bool:
        return "test" in self.tags

    @property
    def optional(self) -> bool:
        return "optional" in self.tags

    @property
    def recommended(self) -> bool:
        return "recommended" in self.tags

    @property
    def runtime(self) -> bool:
        return not self.build and not self.test

    def __str__(self) -> str:  # pragma: no cover - diagnostics
        return self.name


@dataclass
class SourceSpec:
    url: Optional[str] = None
    sha256: Optional[str] = None
    tag: Optional[str] = None
    revision: Optional[str] = None
    branch: Optional[str] = None
    using: Optional[str] = None
    mirrors: list[str] = field(default_factory=list)


@dataclass
class Requirement:
    """A platform/tool requirement such as ``depends_on :macos``."""

    name: str
    tags: frozenset[str] = frozenset()


@dataclass
class PatchSpec:
    """A formula patch declared with ``patch do ... end`` or ``patch :DATA``.

    ``data`` holds an inline patch body: either the formula's embedded
    ``__END__`` section (``patch :DATA``) or a literal string passed to
    ``patch``.  ``unsupported_reason`` is set by the parser when a declaration
    cannot be represented faithfully (for example a patch that references a
    local ``file``); applying it then fails with a clear build error instead of
    silently skipping the patch.
    """

    url: Optional[str] = None
    file: Optional[str] = None
    sha256: Optional[str] = None
    strip: int = 1
    data: Optional[str] = None
    unsupported_reason: Optional[str] = None


@dataclass(frozen=True)
class BottleFile:
    tag: str
    sha256: str
    cellar: Any = None


@dataclass
class BottleSpec:
    root_url: Optional[str] = None
    files: dict[str, BottleFile] = field(default_factory=dict)
    rebuild: int = 0

    def tag_or_none(self, tag: str) -> Optional[BottleFile]:
        return self.files.get(tag)


# macOS release -> Homebrew bottle tag.  Apple skipped macOS 16-25: Tahoe is 26
# and Golden Gate is 27.
_MACOS_TAG_NAMES: dict[int, str] = {
    27: "golden_gate",
    26: "tahoe",
    15: "sequoia",
    14: "sonoma",
    13: "ventura",
    12: "monterey",
    11: "big_sur",
    10: "catalina",
}


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def macos_major_version() -> int:
    release = platform.mac_ver()[0]
    match = re.match(r"(\d+)", release or "")
    return int(match.group(1)) if match else 0


def macos_arch() -> str:
    machine = platform.machine().lower()
    return "arm64" if machine in {"arm64", "aarch64"} else "x86_64"


def candidate_bottle_tags() -> list[str]:
    """Bottle tags to try on this machine, newest first."""

    env_tag = None
    import os

    env_tag = os.environ.get("PYBREW_BOTTLE_TAG") or os.environ.get("HOMEBREW_BOTTLE_TAG")
    if env_tag:
        return [env_tag]

    if is_linux():
        machine = platform.machine().lower()
        if machine in {"arm64", "aarch64"}:
            return ["arm64_linux"]
        return ["x86_64_linux"]

    arch = macos_arch()
    prefix = "arm64_" if arch == "arm64" else ""
    major = macos_major_version()
    tags: list[str] = []
    for version in sorted(_MACOS_TAG_NAMES, reverse=True):
        if version > major:
            continue
        tags.append(prefix + _MACOS_TAG_NAMES[version])
    if not tags:
        tags.append(prefix + "sonoma")
    return tags


def current_bottle_tag() -> str:
    return candidate_bottle_tags()[0]


@dataclass
class Formula:
    """A parsed Homebrew formula."""

    name: str
    path: Optional[Path] = None
    tap: Optional[str] = None
    desc: str = ""
    homepage: str = ""
    license: Any = None
    version: Optional[str] = None
    revision: int = 0
    version_scheme: int = 0
    compatibility_version: Optional[int] = None
    keg_only: bool = False
    keg_only_reason: Optional[str] = None
    oldname: Optional[str] = None
    aliases: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    deprecated_options: dict[str, str] = field(default_factory=dict)
    fails_with: list[dict[str, Any]] = field(default_factory=list)
    preserve_rpath: bool = False
    skip_clean: list[str] = field(default_factory=list)

    stable: SourceSpec = field(default_factory=SourceSpec)
    head: SourceSpec = field(default_factory=SourceSpec)
    bottle: BottleSpec = field(default_factory=BottleSpec)

    dependencies: list[Dependency] = field(default_factory=list)
    head_dependencies: list[Dependency] = field(default_factory=list)
    build_dependencies: list[Dependency] = field(default_factory=list)
    test_dependencies: list[Dependency] = field(default_factory=list)
    recommended_dependencies: list[Dependency] = field(default_factory=list)
    optional_dependencies: list[Dependency] = field(default_factory=list)
    uses_from_macos: list[Dependency] = field(default_factory=list)

    conflicts_with: list[str] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    link_overwrite: list[str] = field(default_factory=list)
    caveats: str = ""
    service: Any = None
    install_source: str = ""
    test_source: str = ""
    post_install_source: str = ""
    fetch_source: str = ""
    livecheck_source: str = ""
    resources: dict[str, SourceSpec] = field(default_factory=dict)
    patches: list[PatchSpec] = field(default_factory=list)

    deprecated: bool = False
    deprecation_reason: Optional[str] = None
    disabled: bool = False
    disable_reason: Optional[str] = None
    deny_network_access: bool = False
    extra: dict[str, list[Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.version is None:
            self.version = detect_version(self.stable.url or "", self.version_scheme)
        if self.revision and self.version and self.version != "HEAD":
            # Homebrew stores the revision separately; nothing to rewrite here.
            pass

    # -- naming ---------------------------------------------------------
    @property
    def full_name(self) -> str:
        if self.tap in (None, "", "homebrew/core", "core"):
            return self.name
        return f"{self.tap}/{self.name}"

    @property
    def pkg_version(self) -> str:
        version = self.version or "HEAD"
        return f"{version}_{self.revision}" if self.revision else version

    @property
    def full_version(self) -> str:
        return self.pkg_version

    @property
    def rack(self) -> str:
        return self.name

    def keg_name(self) -> str:
        return self.pkg_version

    # -- dependencies ----------------------------------------------------
    def runtime_dependencies(self) -> list[Dependency]:
        """Dependencies needed at runtime on this OS."""

        result: list[Dependency] = []
        seen: set[str] = set()
        for dep in self.dependencies:
            if not dep.runtime or dep.optional:
                continue
            if dep.name not in seen:
                seen.add(dep.name)
                result.append(dep)
        # On Linux, `uses_from_macos` entries are ordinary dependencies.
        if is_linux():
            for dep in self.uses_from_macos:
                if dep.name not in seen:
                    seen.add(dep.name)
                    result.append(dep)
        return result

    def dependency_names(self) -> list[str]:
        return [dep.name for dep in self.runtime_dependencies()]

    def all_declared_dependencies(self) -> list[Dependency]:
        return list(self.dependencies) + list(self.uses_from_macos)

    # -- bottles ---------------------------------------------------------
    def bottle_tags(self) -> list[str]:
        return list(self.bottle.files)

    def bottle_for_tag(self, tag: Optional[str] = None) -> Optional[tuple[str, BottleFile]]:
        """Return the best ``(tag, file)`` for this host, or ``None``."""

        explicit = tag
        env_tag = None
        import os

        env_tag = os.environ.get("PYBREW_BOTTLE_TAG") or os.environ.get("HOMEBREW_BOTTLE_TAG")
        tags = [explicit] if explicit else ([env_tag] if env_tag else candidate_bottle_tags())
        for candidate in tags:
            if candidate in self.bottle.files:
                return candidate, self.bottle.files[candidate]
        return None

    def bottle_url(self, tag: str, bottle_file: BottleFile) -> str:
        root = (self.bottle.root_url or "https://ghcr.io/v2/homebrew/core").rstrip("/")
        if "ghcr.io" in root:
            image = self.name.replace("@", "/").replace("+", "x")
            return f"{root}/{image}/blobs/sha256:{bottle_file.sha256}"
        return f"{root}/{self.name}-{self.pkg_version}.{tag}.bottle.tar.gz"

    # -- serialisation ---------------------------------------------------
    def to_api_json(self, installed: Optional[Iterable[dict[str, Any]]] = None) -> dict[str, Any]:
        """Emit a brew-compatible ``info --json=v2`` formula object.

        Only the fields pybrew models are included; the shape follows
        Homebrew's v2 API so existing tooling can consume it.
        """

        def dep_names(deps: Iterable[Dependency], predicate) -> list[str]:
            return [dep.name for dep in deps if predicate(dep)]

        def is_runtime(dep: Dependency) -> bool:
            return dep.runtime and not dep.optional and not dep.recommended

        def is_build(dep: Dependency) -> bool:
            return dep.build and not dep.test

        def is_test(dep: Dependency) -> bool:
            return dep.test

        def is_recommended(dep: Dependency) -> bool:
            return dep.recommended and not dep.build and not dep.test

        def is_optional(dep: Dependency) -> bool:
            return dep.optional and not dep.build and not dep.test

        runtime_deps = dep_names(self.dependencies, is_runtime)
        build_deps = dep_names(self.dependencies, is_build) + dep_names(
            self.build_dependencies, lambda dep: True
        )
        test_deps = dep_names(self.dependencies, is_test) + dep_names(
            self.test_dependencies, lambda dep: True
        )
        recommended_deps = dep_names(self.dependencies, is_recommended) + dep_names(
            self.recommended_dependencies, lambda dep: True
        )
        optional_deps = dep_names(self.dependencies, is_optional) + dep_names(
            self.optional_dependencies, lambda dep: True
        )

        bottle_files = {
            tag: {
                "cellar": unwrap(item.cellar) if item.cellar is not None else ":any",
                "url": self.bottle_url(tag, item),
                "sha256": item.sha256,
            }
            for tag, item in self.bottle.files.items()
        }
        bottle_hash: dict[str, Any] = {}
        if bottle_files or self.bottle.root_url:
            bottle_hash["stable"] = {
                "rebuild": self.bottle.rebuild,
                "root_url": self.bottle.root_url or "https://ghcr.io/v2/homebrew/core",
                "files": bottle_files,
            }
        options = [
            {"option": str(call[0]) if call else ""}
            for call in self.extra.get("option", [])
        ]

        data: dict[str, Any] = {
            "name": self.name,
            "full_name": self.full_name,
            "tap": self.tap or "homebrew/core",
            "oldnames": [],
            "aliases": [],
            "versioned_formulae": [],
            "desc": self.desc,
            "license": unwrap(self.license),
            "homepage": self.homepage,
            "versions": {
                "stable": self.version,
                "head": self.head.url is not None,
                "bottle": bool(self.bottle.files),
            },
            "urls": {
                "stable": {
                    "url": self.stable.url,
                    "tag": self.stable.tag,
                    "revision": self.stable.revision,
                    "using": self.stable.using,
                    "checksum": self.stable.sha256,
                },
                "head": (
                    {
                        "url": self.head.url,
                        "branch": self.head.branch,
                        "using": self.head.using,
                    }
                    if self.head.url
                    else None
                ),
            },
            "revision": self.revision,
            "version_scheme": self.version_scheme,
            "compatibility_version": self.compatibility_version,
            "bottle": bottle_hash,
            "keg_only": self.keg_only,
            "keg_only_reason": (
                {"reason": self.keg_only_reason, "explanation": ""}
                if self.keg_only_reason
                else None
            ),
            "options": options,
            "build_dependencies": build_deps,
            "dependencies": runtime_deps,
            "test_dependencies": test_deps,
            "recommended_dependencies": recommended_deps,
            "optional_dependencies": optional_deps,
            "uses_from_macos": [dep.name for dep in self.uses_from_macos],
            "uses_from_macos_bounds": [],
            "requirements": [],
            "conflicts_with": self.conflicts_with,
            "conflicts_with_reasons": [],
            "link_overwrite": self.link_overwrite,
            "caveats": self.caveats or None,
            "installed": list(installed or []),
            "linked_keg": None,
            "pinned": False,
            "outdated": False,
            "deprecated": self.deprecated,
            "deprecation_date": None,
            "deprecation_reason": self.deprecation_reason,
            "disabled": self.disabled,
            "disable_date": None,
            "disable_reason": self.disable_reason,
            "service": self.service,
            "tap_git_head": None,
            "ruby_source_path": None,
            "ruby_source_checksum": None,
        }
        return data

    def receipt(self) -> dict[str, Any]:
        return {
            "homebrew_version": PYBREW_VERSION,
            "source": {
                "path": str(self.path) if self.path else None,
                "tap": self.tap,
            },
            "version": self.pkg_version,
            "runtime_dependencies": [dep.name for dep in self.runtime_dependencies()],
            "installed_as_dependency": False,
            "poured_from_bottle": True,
            "built_as_bottle": True,
        }
