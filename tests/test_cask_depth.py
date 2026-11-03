"""Depth tests for cask Library artifacts and upgrade cleanup.

Every archive is a synthetic zip created under ``tmp_path``.  The tests never
touch the network, ``/Applications`` or the real ``~/Library``.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pybrew.cask import CaskError, parse_cask_source
from pybrew.cask_installer import CaskInstaller, CaskLoader
from pybrew.paths import Config
from pybrew.taps import discover_taps

# kind, source in the archive, cask DSL line, destination root relative to
# library_root, optional bundle member, payload written to the archive.
ARTIFACT_CASES = [
    (
        "font",
        "payload/Depth.ttf",
        'font "payload/Depth.ttf", target: "Custom Font.ttf"',
        "Fonts/Custom Font.ttf",
        None,
        b"font-data",
    ),
    (
        "colorpicker",
        "payload/Depth.colorPicker",
        'colorpicker "payload/Depth.colorPicker"',
        "ColorPickers/Depth.colorPicker",
        "Contents/Info.plist",
        b"color-data",
    ),
    (
        "prefpane",
        "payload/Depth.prefPane",
        'prefpane "payload/Depth.prefPane", target: "Nested/Depth.prefPane"',
        "PreferencePanes/Nested/Depth.prefPane",
        "Contents/Info.plist",
        b"pref-data",
    ),
    (
        "qlplugin",
        "payload/Depth.qlgenerator",
        'qlplugin "payload/Depth.qlgenerator"',
        "QuickLook/Depth.qlgenerator",
        "Contents/Info.plist",
        b"quicklook-data",
    ),
    (
        "mdimporter",
        "payload/Depth.mdimporter",
        'mdimporter "payload/Depth.mdimporter"',
        "Spotlight/Depth.mdimporter",
        "Contents/Info.plist",
        b"spotlight-data",
    ),
    (
        "screen_saver",
        "payload/Depth.saver",
        'screen_saver "payload/Depth.saver"',
        "Screen Savers/Depth.saver",
        "Contents/Info.plist",
        b"saver-data",
    ),
    (
        "service",
        "payload/Depth.service",
        'service "payload/Depth.service"',
        "Services/Depth.service",
        "Contents/Info.plist",
        b"service-data",
    ),
    (
        "audio_unit_plugin",
        "payload/Depth.component",
        'audio_unit_plugin "payload/Depth.component"',
        "Audio/Plug-Ins/Components/Depth.component",
        "Contents/Info.plist",
        b"audio-unit-data",
    ),
    (
        "vst_plugin",
        "payload/Depth.vst",
        'vst_plugin "payload/Depth.vst"',
        "Audio/Plug-Ins/VST/Depth.vst",
        "Contents/Info.plist",
        b"vst-data",
    ),
    (
        "vst3_plugin",
        "payload/Depth.vst3",
        'vst3_plugin "payload/Depth.vst3"',
        "Audio/Plug-Ins/VST3/Depth.vst3",
        "Contents/Info.plist",
        b"vst3-data",
    ),
    (
        "dictionary",
        "payload/Depth.dictionary",
        'dictionary "payload/Depth.dictionary"',
        "Dictionaries/Depth.dictionary",
        "Contents/Info.plist",
        b"dictionary-data",
    ),
    (
        "input_method",
        "payload/Depth.inputMethod",
        'input_method "payload/Depth.inputMethod"',
        "Input Methods/Depth.inputMethod",
        "Contents/Info.plist",
        b"input-method-data",
    ),
    (
        "keyboard_layout",
        "payload/Depth.keylayout",
        'keyboard_layout "payload/Depth.keylayout", target: "Custom Layout.keylayout"',
        "Keyboard Layouts/Custom Layout.keylayout",
        None,
        b"keyboard-data",
    ),
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_zip(path: Path, entries: dict[str, bytes]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as bundle:
        for name, data in entries.items():
            bundle.writestr(name, data)
    return _sha256(path)


def _write_cask(
    tap: Path,
    token: str,
    version: str,
    digest: str,
    archive: Path,
    artifacts: str = "",
    *,
    stage_only: bool = False,
) -> Path:
    lines = [
        f'cask "{token}" do',
        f'  version "{version}"',
        f'  sha256 "{digest}"',
        f'  url "{archive.as_uri()}"',
    ]
    if stage_only:
        lines.append("  stage_only true")
    if artifacts:
        lines.append(artifacts.rstrip("\n"))
    lines.append("end")
    path = tap / "Casks" / "t" / f"{token}.rb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def cask_env(tmp_path: Path) -> SimpleNamespace:
    tap = tmp_path / "tap"
    (tap / "Casks" / "t").mkdir(parents=True)
    config = Config.default(
        prefix=tmp_path / "prefix",
        taps=[tap],
        cache=tmp_path / "cache",
    )
    config.ensure()
    library_root = tmp_path / "Library"
    installer = CaskInstaller(config, library_root=library_root)
    loader = CaskLoader(discover_taps([tap]))
    return SimpleNamespace(
        tmp_path=tmp_path,
        tap=tap,
        config=config,
        installer=installer,
        loader=loader,
        library_root=library_root,
    )


def test_parse_library_artifact_kinds_and_stage_only() -> None:
    source = (
        'cask "depth" do\n'
        '  version "1.0"\n'
        "  stage_only true\n"
        + "".join(f"  {case[2]}\n" for case in ARTIFACT_CASES)
        + "end\n"
    )
    cask = parse_cask_source(source)
    assert cask.stage_only is True
    assert [artifact.kind for artifact in cask.artifacts] == [
        case[0] for case in ARTIFACT_CASES
    ]
    assert cask.artifacts[0].target == "Custom Font.ttf"
    assert cask.artifacts[2].target == "Nested/Depth.prefPane"

    plain = parse_cask_source('cask "plain" do\n  version "1.0"\n  font "A.ttf"\nend\n')
    assert plain.stage_only is False


def test_library_root_defaults_to_prefix(cask_env: SimpleNamespace) -> None:
    installer = CaskInstaller(cask_env.config)
    assert installer.library_root == cask_env.config.prefix / "Library"


def test_install_library_artifacts_receipt_and_uninstall(cask_env: SimpleNamespace) -> None:
    entries: dict[str, bytes] = {}
    for _kind, source, _dsl, _root, member, data in ARTIFACT_CASES:
        entries[source if member is None else f"{source}/{member}"] = data
    archive = cask_env.tmp_path / "Depth-1.0.zip"
    digest = _make_zip(archive, entries)
    _write_cask(
        cask_env.tap,
        "depth",
        "1.0",
        digest,
        archive,
        artifacts="\n".join(f"  {case[2]}" for case in ARTIFACT_CASES),
    )

    cask = cask_env.loader.load("depth")
    result = cask_env.installer.install(cask)

    expected_destinations = [cask_env.library_root / case[3] for case in ARTIFACT_CASES]
    assert result.artifacts == [str(path) for path in expected_destinations]
    for (_kind, _source, _dsl, root, member, data) in ARTIFACT_CASES:
        destination = cask_env.library_root / root
        exists = destination.is_dir() if member else destination.is_file()
        assert exists, root
        payload = destination / member if member else destination
        assert payload.read_bytes() == data

    receipt = json.loads((result.room / ".metadata" / "receipt.json").read_text())
    assert set(receipt["installed"]) == {str(path) for path in expected_destinations}
    assert receipt["stage_only"] is False
    assert {artifact["kind"] for artifact in receipt["artifacts"]} == {
        case[0] for case in ARTIFACT_CASES
    }

    cask_env.installer.uninstall("depth")
    assert not any(path.exists() for path in expected_destinations)
    assert not (cask_env.config.caskroom / "depth").exists()


def test_stage_only_installs_no_artifacts_but_writes_receipt(cask_env: SimpleNamespace) -> None:
    archive = cask_env.tmp_path / "Staged-1.0.zip"
    digest = _make_zip(archive, {"payload/Ignored.ttf": b"ignored"})
    _write_cask(
        cask_env.tap,
        "staged",
        "1.0",
        digest,
        archive,
        artifacts='  font "payload/Ignored.ttf"',
        stage_only=True,
    )

    result = cask_env.installer.install(cask_env.loader.load("staged"))

    assert result.artifacts == []
    assert not (cask_env.library_root / "Fonts").exists()
    receipt_path = result.room / ".metadata" / "receipt.json"
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text())
    assert receipt["installed"] == []
    assert receipt["stage_only"] is True

    cask_env.installer.uninstall("staged")
    assert not result.room.exists()
    assert not (cask_env.config.caskroom / "staged").exists()


def test_stage_only_without_download_metadata(cask_env: SimpleNamespace) -> None:
    cask_file = cask_env.tap / "Casks" / "t" / "bare.rb"
    cask_file.parent.mkdir(parents=True, exist_ok=True)
    cask_file.write_text(
        'cask "bare" do\n  version "1.0"\n  stage_only true\nend\n',
        encoding="utf-8",
    )

    result = cask_env.installer.install(cask_env.loader.load("bare"))

    assert result.artifacts == []
    receipt = json.loads((result.room / ".metadata" / "receipt.json").read_text())
    assert receipt["stage_only"] is True
    assert receipt["installed"] == []

    cask_env.installer.uninstall("bare")
    assert not result.room.exists()


@pytest.mark.parametrize(
    "target",
    ["../escape.ttf", "nested/../../escape.ttf", "~/escape.ttf"],
)
def test_library_artifact_target_cannot_escape(
    cask_env: SimpleNamespace, target: str
) -> None:
    archive = cask_env.tmp_path / "Escape-1.0.zip"
    digest = _make_zip(archive, {"payload/Depth.ttf": b"font-data"})
    _write_cask(
        cask_env.tap,
        "escape",
        "1.0",
        digest,
        archive,
        artifacts=f'  font "payload/Depth.ttf", target: "{target}"',
    )

    with pytest.raises(CaskError, match="unsafe artifact target"):
        cask_env.installer.install(cask_env.loader.load("escape"))
    assert not (cask_env.tmp_path / "escape.ttf").exists()


def test_library_artifact_target_rejects_absolute_path(cask_env: SimpleNamespace) -> None:
    outside = cask_env.tmp_path / "outside.ttf"
    archive = cask_env.tmp_path / "Absolute-1.0.zip"
    digest = _make_zip(archive, {"payload/Depth.ttf": b"font-data"})
    _write_cask(
        cask_env.tap,
        "absolute",
        "1.0",
        digest,
        archive,
        artifacts=f'  font "payload/Depth.ttf", target: "{outside}"',
    )

    with pytest.raises(CaskError, match="unsafe artifact target"):
        cask_env.installer.install(cask_env.loader.load("absolute"))
    assert not outside.exists()


def test_upgrade_removes_artifact_missing_from_new_version(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Depth-1.0.zip"
    digest = _make_zip(
        archive,
        {
            "payload/Shared.ttf": b"shared-font",
            "payload/Removed.ttf": b"removed-font",
        },
    )
    _write_cask(
        cask_env.tap,
        "depth",
        "1.0",
        digest,
        archive,
        artifacts='  font "payload/Shared.ttf"\n  font "payload/Removed.ttf"',
    )
    first = cask_env.installer.install(cask_env.loader.load("depth"))
    shared = cask_env.library_root / "Fonts" / "Shared.ttf"
    removed = cask_env.library_root / "Fonts" / "Removed.ttf"
    assert shared.read_bytes() == b"shared-font"
    assert removed.read_bytes() == b"removed-font"

    # 2.0 keeps the shared font but drops the second one.
    _write_cask(
        cask_env.tap,
        "depth",
        "2.0",
        digest,
        archive,
        artifacts='  font "payload/Shared.ttf"',
    )
    result = cask_env.installer.upgrade(cask_env.loader.load("depth"))

    assert result.version == "2.0"
    assert shared.exists()
    assert not removed.exists()
    assert not first.room.exists()
    receipt = json.loads((result.room / ".metadata" / "receipt.json").read_text())
    assert receipt["installed"] == [str(shared)]


# ---------------------------------------------------------------------------
# artifact target path safety beyond Library/completion artifacts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    ["../escape.app", "nested/../../escape.app", "~/escape.app"],
)
def test_app_artifact_target_cannot_escape_appdir(
    cask_env: SimpleNamespace, target: str
) -> None:
    archive = cask_env.tmp_path / "AppEscape-1.0.zip"
    digest = _make_zip(archive, {"Payload.app/Contents/Info.plist": b"<plist/>"})
    _write_cask(
        cask_env.tap,
        "appescape",
        "1.0",
        digest,
        archive,
        artifacts=f'  app "Payload.app", target: "{target}"',
    )

    with pytest.raises(CaskError, match="unsafe artifact target"):
        cask_env.installer.install(cask_env.loader.load("appescape"))
    assert not (cask_env.tmp_path / "escape.app").exists()


def test_app_artifact_absolute_target_is_rejected(
    cask_env: SimpleNamespace,
) -> None:
    outside = cask_env.tmp_path / "outside-app"
    archive = cask_env.tmp_path / "AppAbsolute-1.0.zip"
    digest = _make_zip(archive, {"Payload.app/Contents/Info.plist": b"<plist/>"})
    _write_cask(
        cask_env.tap,
        "appabsolute",
        "1.0",
        digest,
        archive,
        artifacts=f'  app "Payload.app", target: "{outside}"',
    )

    with pytest.raises(CaskError, match="unsafe artifact target"):
        cask_env.installer.install(cask_env.loader.load("appabsolute"))
    assert not outside.exists()


@pytest.mark.parametrize("target", ["../escape", "~/escape"])
def test_binary_artifact_target_cannot_escape_bin(
    cask_env: SimpleNamespace, target: str
) -> None:
    archive = cask_env.tmp_path / "BinEscape-1.0.zip"
    digest = _make_zip(archive, {"bin/tool": b"#!/bin/sh\necho tool\n"})
    _write_cask(
        cask_env.tap,
        "binescape",
        "1.0",
        digest,
        archive,
        artifacts=f'  binary "bin/tool", target: "{target}"',
    )

    with pytest.raises(CaskError, match="unsafe artifact target"):
        cask_env.installer.install(cask_env.loader.load("binescape"))
    assert not (cask_env.config.bin.parent / "escape").exists()


def test_binary_absolute_target_is_rejected(cask_env: SimpleNamespace) -> None:
    outside = cask_env.tmp_path / "outside-bin"
    archive = cask_env.tmp_path / "BinAbsolute-1.0.zip"
    digest = _make_zip(archive, {"bin/tool": b"#!/bin/sh\n"})
    _write_cask(
        cask_env.tap,
        "binabsolute",
        "1.0",
        digest,
        archive,
        artifacts=f'  binary "bin/tool", target: "{outside}"',
    )

    with pytest.raises(CaskError, match="unsafe artifact target"):
        cask_env.installer.install(cask_env.loader.load("binabsolute"))
    assert not outside.exists()


@pytest.mark.parametrize("target", ["../escape.AppImage", "~/escape.AppImage"])
def test_appimage_artifact_target_cannot_escape_appdir(
    cask_env: SimpleNamespace, target: str
) -> None:
    archive = cask_env.tmp_path / "ImageEscape-1.0.zip"
    digest = _make_zip(archive, {"Payload.AppImage": b"#!/bin/sh\n"})
    _write_cask(
        cask_env.tap,
        "imageescape",
        "1.0",
        digest,
        archive,
        artifacts=f'  appimage "Payload.AppImage", target: "{target}"',
    )

    with pytest.raises(CaskError, match="unsafe artifact target"):
        cask_env.installer.install(cask_env.loader.load("imageescape"))


# ---------------------------------------------------------------------------
# completion artifacts: uninstall and upgrade cleanup
# ---------------------------------------------------------------------------


def _completion_paths(config: Config) -> dict[str, Path]:
    return {
        "bash": config.prefix / "share/bash-completion/completions/tool.bash",
        "zsh": config.prefix / "share/zsh/site-functions/_tool",
        "fish": config.prefix / "share/fish/vendor_completions.d/tool.fish",
    }


def test_completion_artifacts_uninstall_removes_all(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Completions-1.0.zip"
    digest = _make_zip(
        archive,
        {
            "completions/tool.bash": b"complete -W tool tool\n",
            "completions/_tool": b"#compdef tool\n",
            "completions/tool.fish": b"complete -c tool\n",
        },
    )
    _write_cask(
        cask_env.tap,
        "completions",
        "1.0",
        digest,
        archive,
        artifacts=(
            '  bash_completion "completions/tool.bash"\n'
            '  zsh_completion "completions/_tool"\n'
            '  fish_completion "completions/tool.fish"'
        ),
    )
    cask_env.installer.install(cask_env.loader.load("completions"))
    paths = _completion_paths(cask_env.config)
    assert all(path.is_file() for path in paths.values())

    cask_env.installer.uninstall("completions")

    assert not any(path.exists() for path in paths.values())
    assert not (cask_env.config.caskroom / "completions").exists()


def test_upgrade_removes_completion_artifact_missing_from_new_version(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Completions-1.0.zip"
    digest = _make_zip(
        archive,
        {
            "completions/tool.bash": b"complete -W tool tool\n",
            "completions/tool.fish": b"complete -c tool\n",
        },
    )
    _write_cask(
        cask_env.tap,
        "completions",
        "1.0",
        digest,
        archive,
        artifacts=(
            '  bash_completion "completions/tool.bash"\n'
            '  fish_completion "completions/tool.fish"'
        ),
    )
    cask_env.installer.install(cask_env.loader.load("completions"))
    paths = _completion_paths(cask_env.config)
    assert paths["bash"].is_file()
    assert paths["fish"].is_file()

    # 2.0 keeps only the bash completion.
    _write_cask(
        cask_env.tap,
        "completions",
        "2.0",
        digest,
        archive,
        artifacts='  bash_completion "completions/tool.bash"',
    )
    result = cask_env.installer.upgrade(cask_env.loader.load("completions"))

    assert result.version == "2.0"
    assert paths["bash"].is_file()
    assert not paths["fish"].exists()


def test_upgrade_keeps_shared_binary_symlink(cask_env: SimpleNamespace) -> None:
    archive = cask_env.tmp_path / "BinUpgrade-1.0.zip"
    digest = _make_zip(
        archive,
        {"BinUpgrade.app/Contents/MacOS/tool": b"#!/bin/sh\necho one\n"},
    )
    _write_cask(
        cask_env.tap,
        "binupgrade",
        "1.0",
        digest,
        archive,
        artifacts=(
            '  app "BinUpgrade.app"\n'
            '  binary "BinUpgrade.app/Contents/MacOS/tool"'
        ),
    )
    cask_env.installer.install(cask_env.loader.load("binupgrade"))
    target = cask_env.config.bin / "tool"
    assert target.is_symlink()
    assert target.exists()

    _write_cask(
        cask_env.tap,
        "binupgrade",
        "2.0",
        digest,
        archive,
        artifacts=(
            '  app "BinUpgrade.app"\n'
            '  binary "BinUpgrade.app/Contents/MacOS/tool"'
        ),
    )
    cask_env.installer.upgrade(cask_env.loader.load("binupgrade"))

    assert target.is_symlink()
    assert target.exists()


# ---------------------------------------------------------------------------
# zap: glob expansion, no matches, unsafe roots
# ---------------------------------------------------------------------------


def test_zap_glob_removes_multiple_paths(cask_env: SimpleNamespace) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    root = cask_env.config.prefix / "zap"
    nested = root / "nested"
    nested.mkdir(parents=True)
    first = root / "one.txt"
    second = root / "two.txt"
    third = nested / "three.log"
    for path in (first, second, third):
        path.write_text("zap me\n", encoding="utf-8")
    cask.zap = [str(root / "*.txt"), str(nested / "*.log")]

    removed = cask_env.installer.zap(cask)

    assert set(removed) == {first, second, third}
    assert not any(path.exists() for path in (first, second, third))


def test_zap_without_matches_is_a_noop(cask_env: SimpleNamespace) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    cask.zap = [str(cask_env.config.prefix / "missing-*.txt")]

    assert cask_env.installer.zap(cask) == []


def test_zap_refuses_paths_outside_allowed_roots(
    cask_env: SimpleNamespace,
) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    outside = Path("/etc/hosts")
    cask.zap = [str(outside)]

    with pytest.raises(CaskError, match="outside allowed roots"):
        cask_env.installer.zap(cask)
    assert outside.exists()


# ---------------------------------------------------------------------------
# suite artifacts (Homebrew "moved" directory semantics) and generic artifact
# ---------------------------------------------------------------------------


def test_suite_artifact_copies_directory(cask_env: SimpleNamespace) -> None:
    archive = cask_env.tmp_path / "Suite-1.0.zip"
    digest = _make_zip(
        archive,
        {
            "Suite Folder/App1.app/Contents/Info.plist": b"<plist/>",
            "Suite Folder/readme.txt": b"readme\n",
        },
    )
    _write_cask(
        cask_env.tap,
        "suite",
        "1.0",
        digest,
        archive,
        artifacts='  suite "Suite Folder"',
    )

    result = cask_env.installer.install(cask_env.loader.load("suite"))

    destination = cask_env.config.applications / "Suite Folder"
    assert (destination / "App1.app/Contents/Info.plist").is_file()
    assert (destination / "readme.txt").read_text() == "readme\n"
    assert result.artifacts == [str(destination)]

    cask_env.installer.uninstall("suite")
    assert not destination.exists()


def test_suite_artifact_honors_explicit_target(cask_env: SimpleNamespace) -> None:
    archive = cask_env.tmp_path / "Suite-1.0.zip"
    digest = _make_zip(
        archive,
        {
            "Suite Folder/App1.app/Contents/Info.plist": b"<plist/>",
        },
    )
    _write_cask(
        cask_env.tap,
        "suite",
        "1.0",
        digest,
        archive,
        artifacts='  suite "Suite Folder", target: "My Suite"',
    )

    cask_env.installer.install(cask_env.loader.load("suite"))

    destination = cask_env.config.applications / "My Suite"
    assert (destination / "App1.app/Contents/Info.plist").is_file()
    assert not (cask_env.config.applications / "Suite Folder").exists()


@pytest.mark.parametrize("target", ["../escape", "~/escape"])
def test_suite_artifact_target_cannot_escape(
    cask_env: SimpleNamespace, target: str
) -> None:
    archive = cask_env.tmp_path / "Suite-1.0.zip"
    digest = _make_zip(
        archive, {"Suite Folder/App1.app/Contents/Info.plist": b"<plist/>"}
    )
    _write_cask(
        cask_env.tap,
        "suite",
        "1.0",
        digest,
        archive,
        artifacts=f'  suite "Suite Folder", target: "{target}"',
    )

    with pytest.raises(CaskError, match="unsafe artifact target"):
        cask_env.installer.install(cask_env.loader.load("suite"))


# ---------------------------------------------------------------------------
# receipt cleanup safety (tampered receipts must not delete outside files)
# ---------------------------------------------------------------------------


def _tamper_receipt(room: Path, extra_path: Path) -> None:
    receipt_path = room / ".metadata" / "receipt.json"
    payload = json.loads(receipt_path.read_text())
    payload["installed"].append(str(extra_path))
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")


def test_uninstall_ignores_unsafe_receipt_paths(cask_env: SimpleNamespace) -> None:
    archive = cask_env.tmp_path / "SafeReceipt-1.0.zip"
    digest = _make_zip(archive, {"payload/Safe.ttf": b"safe"})
    _write_cask(
        cask_env.tap,
        "safereceipt",
        "1.0",
        digest,
        archive,
        artifacts='  font "payload/Safe.ttf"',
    )
    result = cask_env.installer.install(cask_env.loader.load("safereceipt"))
    font = cask_env.library_root / "Fonts" / "Safe.ttf"
    assert font.is_file()

    outside = cask_env.tmp_path / "outside-receipt.txt"
    outside.write_text("do not delete", encoding="utf-8")
    _tamper_receipt(result.room, outside)

    cask_env.installer.uninstall("safereceipt")

    assert outside.read_text() == "do not delete"
    assert not font.exists()
    assert not result.room.exists()


def test_force_reinstall_ignores_unsafe_receipt_paths(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "SafeReceipt-1.0.zip"
    digest = _make_zip(archive, {"payload/Safe.ttf": b"safe"})
    _write_cask(
        cask_env.tap,
        "safereceipt",
        "1.0",
        digest,
        archive,
        artifacts='  font "payload/Safe.ttf"',
    )
    cask = cask_env.loader.load("safereceipt")
    result = cask_env.installer.install(cask)

    outside = cask_env.tmp_path / "outside-reinstall.txt"
    outside.write_text("do not delete", encoding="utf-8")
    _tamper_receipt(result.room, outside)

    cask_env.installer.install(cask_env.loader.load("safereceipt"), force=True)

    assert outside.read_text() == "do not delete"


def test_upgrade_ignores_unsafe_old_receipt_paths(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "SafeReceipt-1.0.zip"
    digest = _make_zip(archive, {"payload/Safe.ttf": b"safe"})
    _write_cask(
        cask_env.tap,
        "safereceipt",
        "1.0",
        digest,
        archive,
        artifacts='  font "payload/Safe.ttf"',
    )
    first = cask_env.installer.install(cask_env.loader.load("safereceipt"))

    outside = cask_env.tmp_path / "outside-upgrade.txt"
    outside.write_text("do not delete", encoding="utf-8")
    _tamper_receipt(first.room, outside)

    _write_cask(
        cask_env.tap,
        "safereceipt",
        "2.0",
        digest,
        archive,
        artifacts='  font "payload/Safe.ttf"',
    )
    cask_env.installer.upgrade(cask_env.loader.load("safereceipt"))

    assert outside.read_text() == "do not delete"


# ---------------------------------------------------------------------------
# zap symlink safety
# ---------------------------------------------------------------------------


def test_zap_refuses_symlink_outside_allowed_roots(
    cask_env: SimpleNamespace,
) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    # The system temp dir is deliberately an allowed zap root (tests/trash), so
    # use a target that is truly outside home/prefix/temp.
    outside = Path("/etc/hosts")
    link = cask_env.config.prefix / "outside-link.txt"
    link.symlink_to(outside)
    cask.zap = [str(link)]

    with pytest.raises(CaskError, match="outside allowed roots"):
        cask_env.installer.zap(cask)
    assert outside.exists()
    assert link.is_symlink()


def test_zap_removes_symlink_without_touching_target(
    cask_env: SimpleNamespace,
) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    real = cask_env.config.prefix / "real-target.txt"
    real.write_text("keep", encoding="utf-8")
    link = cask_env.config.prefix / "inside-link.txt"
    link.symlink_to(real)
    cask.zap = [str(link)]

    removed = cask_env.installer.zap(cask)

    assert removed == [link]
    assert not link.is_symlink()
    assert real.read_text() == "keep"


# ---------------------------------------------------------------------------
# generic `artifact` stanza: bounded moved file/directory semantics
# ---------------------------------------------------------------------------


def test_generic_artifact_relative_target_installs_into_appdir(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Generic-1.0.zip"
    digest = _make_zip(archive, {"files/notes.txt": b"hello notes\n"})
    _write_cask(
        cask_env.tap,
        "generic",
        "1.0",
        digest,
        archive,
        artifacts='  artifact "files/notes.txt", target: "notes.txt"',
    )

    result = cask_env.installer.install(cask_env.loader.load("generic"))

    destination = cask_env.config.applications / "notes.txt"
    assert destination.read_text() == "hello notes\n"
    assert result.artifacts == [str(destination)]

    cask_env.installer.uninstall("generic")
    assert not destination.exists()


def test_generic_artifact_absolute_target_below_prefix(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Generic-1.0.zip"
    digest = _make_zip(
        archive, {"Generic/readme.txt": b"generic readme\n"}
    )
    destination = cask_env.config.prefix / "share" / "generic"
    _write_cask(
        cask_env.tap,
        "generic",
        "1.0",
        digest,
        archive,
        artifacts=f'  artifact "Generic", target: "{destination}"',
    )

    cask_env.installer.install(cask_env.loader.load("generic"))

    assert (destination / "readme.txt").read_text() == "generic readme\n"

    cask_env.installer.uninstall("generic")
    assert not destination.exists()


def test_generic_artifact_requires_a_target(cask_env: SimpleNamespace) -> None:
    archive = cask_env.tmp_path / "Generic-1.0.zip"
    digest = _make_zip(archive, {"files/notes.txt": b"hello\n"})
    _write_cask(
        cask_env.tap,
        "generic",
        "1.0",
        digest,
        archive,
        artifacts='  artifact "files/notes.txt"',
    )

    with pytest.raises(CaskError, match="requires a target"):
        cask_env.installer.install(cask_env.loader.load("generic"))


def test_generic_artifact_absolute_target_outside_roots_is_rejected(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Generic-1.0.zip"
    digest = _make_zip(archive, {"files/notes.txt": b"hello\n"})
    outside = cask_env.tmp_path / "outside-artifact.txt"
    _write_cask(
        cask_env.tap,
        "generic",
        "1.0",
        digest,
        archive,
        artifacts=f'  artifact "files/notes.txt", target: "{outside}"',
    )

    with pytest.raises(CaskError, match="outside install roots"):
        cask_env.installer.install(cask_env.loader.load("generic"))
    assert not outside.exists()


# ---------------------------------------------------------------------------
# receipt cleanup for the remaining artifact kinds
# ---------------------------------------------------------------------------


def test_receipt_cleanup_covers_remaining_artifact_kinds(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "CleanAll-1.0.zip"
    digest = _make_zip(
        archive,
        {
            "Clean.app/Contents/Info.plist": b"<plist/>",
            "Clean.app/Contents/MacOS/tool": b"#!/bin/sh\n",
            "man/tool.1": b".TH TOOL 1\n",
            "completions/tool.bash": b"complete -W tool tool\n",
            "payload/Clean.ttf": b"font",
            "Clean Suite/Suite1.app/Contents/Info.plist": b"<plist/>",
            "Clean.AppImage": b"#!/bin/sh\n",
            "notes.txt": b"notes\n",
        },
    )
    _write_cask(
        cask_env.tap,
        "cleanall",
        "1.0",
        digest,
        archive,
        artifacts=(
            '  app "Clean.app"\n'
            '  binary "Clean.app/Contents/MacOS/tool"\n'
            '  manpage "man/tool.1"\n'
            '  bash_completion "completions/tool.bash"\n'
            '  font "payload/Clean.ttf"\n'
            '  suite "Clean Suite"\n'
            '  appimage "Clean.AppImage"\n'
            '  artifact "notes.txt", target: "notes.txt"'
        ),
    )
    result = cask_env.installer.install(cask_env.loader.load("cleanall"))
    receipt = json.loads(
        (result.room / ".metadata" / "receipt.json").read_text()
    )
    installed = [Path(path) for path in receipt["installed"]]
    assert len(installed) == 8
    assert all(
        path.exists() or path.is_symlink() for path in installed
    )

    cask_env.installer.uninstall("cleanall")

    assert not result.room.exists()
    assert not any(path.exists() or path.is_symlink() for path in installed)


# ---------------------------------------------------------------------------
# suite explicit nested target
# ---------------------------------------------------------------------------


def test_suite_artifact_nested_target(cask_env: SimpleNamespace) -> None:
    archive = cask_env.tmp_path / "Suite-1.0.zip"
    digest = _make_zip(
        archive, {"Suite Folder/App1.app/Contents/Info.plist": b"<plist/>"}
    )
    _write_cask(
        cask_env.tap,
        "suite",
        "1.0",
        digest,
        archive,
        artifacts='  suite "Suite Folder", target: "Nested/My Suite"',
    )

    cask_env.installer.install(cask_env.loader.load("suite"))

    destination = cask_env.config.applications / "Nested" / "My Suite"
    assert (destination / "App1.app/Contents/Info.plist").is_file()


# ---------------------------------------------------------------------------
# zap symlinked directories
# ---------------------------------------------------------------------------


def test_zap_symlinked_directory_outside_roots_is_refused(
    cask_env: SimpleNamespace,
) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    # ``/etc`` is outside home/prefix/temp; do not delete anything there.
    outside = Path("/etc")
    link = cask_env.config.prefix / "outside-dir-link"
    link.symlink_to(outside, target_is_directory=True)
    cask.zap = [str(link / "*")]

    with pytest.raises(CaskError, match="outside allowed roots"):
        cask_env.installer.zap(cask)
    assert link.is_symlink()
    assert (outside / "hosts").exists()


def test_zap_symlinked_directory_inside_roots_removes_children(
    cask_env: SimpleNamespace,
) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    real = cask_env.config.prefix / "real-dir"
    real.mkdir(parents=True)
    payload = real / "inside.txt"
    payload.write_text("remove", encoding="utf-8")
    link = cask_env.config.prefix / "inside-dir-link"
    link.symlink_to(real, target_is_directory=True)
    cask.zap = [str(link / "*.txt")]

    removed = cask_env.installer.zap(cask)

    assert removed == [link / "inside.txt"]
    assert not payload.exists()
    assert real.is_dir()
    assert link.is_symlink()


def test_zap_symlinked_directory_itself_removes_only_the_link(
    cask_env: SimpleNamespace,
) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    real = cask_env.config.prefix / "real-dir-keep"
    real.mkdir(parents=True)
    payload = real / "keep.txt"
    payload.write_text("keep", encoding="utf-8")
    link = cask_env.config.prefix / "dir-link"
    link.symlink_to(real, target_is_directory=True)
    cask.zap = [str(link)]

    removed = cask_env.installer.zap(cask)

    assert removed == [link]
    assert not link.is_symlink()
    assert payload.read_text() == "keep"


# ---------------------------------------------------------------------------
# generic artifact dirmethod/base_dir resolution
# ---------------------------------------------------------------------------


def test_generic_artifact_dirmethod_prefix(cask_env: SimpleNamespace) -> None:
    archive = cask_env.tmp_path / "Generic-1.0.zip"
    digest = _make_zip(archive, {"notes.txt": b"prefix notes\n"})
    _write_cask(
        cask_env.tap,
        "generic",
        "1.0",
        digest,
        archive,
        artifacts=(
            '  artifact "notes.txt", target: "share/generic/notes.txt", '
            'dirmethod: :prefix'
        ),
    )

    cask_env.installer.install(cask_env.loader.load("generic"))
    destination = cask_env.config.prefix / "share" / "generic" / "notes.txt"
    assert destination.read_text() == "prefix notes\n"

    cask_env.installer.uninstall("generic")
    assert not destination.exists()


def test_generic_artifact_dirmethod_library_root(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Generic-1.0.zip"
    digest = _make_zip(archive, {"notes.txt": b"library notes\n"})
    _write_cask(
        cask_env.tap,
        "generic",
        "1.0",
        digest,
        archive,
        artifacts=(
            '  artifact "notes.txt", target: "Payloads/notes.txt", '
            'base_dir: "library_root"'
        ),
    )

    cask_env.installer.install(cask_env.loader.load("generic"))
    destination = cask_env.library_root / "Payloads" / "notes.txt"
    assert destination.read_text() == "library notes\n"


def test_generic_artifact_unknown_dirmethod_is_rejected(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Generic-1.0.zip"
    digest = _make_zip(archive, {"notes.txt": b"notes\n"})
    _write_cask(
        cask_env.tap,
        "generic",
        "1.0",
        digest,
        archive,
        artifacts=(
            '  artifact "notes.txt", target: "notes.txt", '
            'dirmethod: :staged_path'
        ),
    )

    with pytest.raises(CaskError, match="unsupported generic artifact dirmethod"):
        cask_env.installer.install(cask_env.loader.load("generic"))


def test_generic_artifact_unsupported_kwargs_are_rejected(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Generic-1.0.zip"
    digest = _make_zip(archive, {"notes.txt": b"notes\n"})
    _write_cask(
        cask_env.tap,
        "generic",
        "1.0",
        digest,
        archive,
        artifacts=(
            '  artifact "notes.txt", target: "notes.txt", '
            'steps: ["move"]'
        ),
    )

    with pytest.raises(CaskError, match="cannot be combined with steps"):
        cask_env.installer.install(cask_env.loader.load("generic"))


def test_generic_artifact_unknown_kwarg_is_rejected(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Generic-1.0.zip"
    digest = _make_zip(archive, {"notes.txt": b"notes\n"})
    _write_cask(
        cask_env.tap,
        "generic",
        "1.0",
        digest,
        archive,
        artifacts='  artifact "notes.txt", target: "notes.txt", mode: :copy',
    )

    with pytest.raises(CaskError, match="options are not supported"):
        cask_env.installer.install(cask_env.loader.load("generic"))


def test_generic_artifact_explicit_appdir_absolute_target(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Generic-1.0.zip"
    digest = _make_zip(archive, {"notes.txt": b"absolute notes\n"})
    destination = cask_env.config.applications / "absolute-notes.txt"
    _write_cask(
        cask_env.tap,
        "generic",
        "1.0",
        digest,
        archive,
        artifacts=(
            f'  artifact "notes.txt", target: "{destination}", '
            'dirmethod: :appdir'
        ),
    )

    cask_env.installer.install(cask_env.loader.load("generic"))

    assert destination.read_text() == "absolute notes\n"


def test_generic_artifact_dirmethod_restricts_absolute_target(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Generic-1.0.zip"
    digest = _make_zip(archive, {"notes.txt": b"notes\n"})
    outside_base = cask_env.library_root / "notes.txt"
    _write_cask(
        cask_env.tap,
        "generic",
        "1.0",
        digest,
        archive,
        artifacts=(
            f'  artifact "notes.txt", target: "{outside_base}", '
            'dirmethod: :prefix'
        ),
    )

    with pytest.raises(CaskError, match="outside install roots"):
        cask_env.installer.install(cask_env.loader.load("generic"))


# ---------------------------------------------------------------------------
# suite receipt cleanup on upgrade
# ---------------------------------------------------------------------------


def test_upgrade_removes_suite_artifact(cask_env: SimpleNamespace) -> None:
    archive = cask_env.tmp_path / "Suite-1.0.zip"
    digest = _make_zip(
        archive, {"Suite Folder/App1.app/Contents/Info.plist": b"<plist/>"}
    )
    _write_cask(
        cask_env.tap,
        "suite",
        "1.0",
        digest,
        archive,
        artifacts='  suite "Suite Folder", target: "Old Suite"',
    )
    cask_env.installer.install(cask_env.loader.load("suite"))
    old_suite = cask_env.config.applications / "Old Suite"
    assert old_suite.is_dir()

    # 2.0 drops the suite entirely: the upgrade must clean the old directory.
    _write_cask(cask_env.tap, "suite", "2.0", digest, archive, artifacts="")
    result = cask_env.installer.upgrade(cask_env.loader.load("suite"))

    assert result.version == "2.0"
    assert not old_suite.exists()


# ---------------------------------------------------------------------------
# suite receipt edges: shared target on upgrade, symlinked target on uninstall
# ---------------------------------------------------------------------------


def test_upgrade_keeps_shared_suite_target(cask_env: SimpleNamespace) -> None:
    archive_one = cask_env.tmp_path / "Suite-1.0.zip"
    digest_one = _make_zip(
        archive_one,
        {"Suite Folder/readme.txt": b"v1\n", "Suite Folder/App1.app/Contents/Info.plist": b"<plist/>"},
    )
    _write_cask(
        cask_env.tap,
        "suite",
        "1.0",
        digest_one,
        archive_one,
        artifacts='  suite "Suite Folder", target: "Shared Suite"',
    )
    cask_env.installer.install(cask_env.loader.load("suite"))
    target = cask_env.config.applications / "Shared Suite"
    assert (target / "readme.txt").read_text() == "v1\n"

    archive_two = cask_env.tmp_path / "Suite-2.0.zip"
    digest_two = _make_zip(
        archive_two,
        {"Suite Folder/readme.txt": b"v2\n", "Suite Folder/App1.app/Contents/Info.plist": b"<plist/>"},
    )
    _write_cask(
        cask_env.tap,
        "suite",
        "2.0",
        digest_two,
        archive_two,
        artifacts='  suite "Suite Folder", target: "Shared Suite"',
    )
    cask_env.installer.upgrade(cask_env.loader.load("suite"))

    assert (target / "readme.txt").read_text() == "v2\n"


def test_uninstall_suite_symlink_does_not_touch_target(
    cask_env: SimpleNamespace,
) -> None:
    archive = cask_env.tmp_path / "Suite-1.0.zip"
    digest = _make_zip(
        archive, {"Suite Folder/App1.app/Contents/Info.plist": b"<plist/>"}
    )
    _write_cask(
        cask_env.tap,
        "suite",
        "1.0",
        digest,
        archive,
        artifacts='  suite "Suite Folder", target: "Linked Suite"',
    )
    cask_env.installer.install(cask_env.loader.load("suite"))
    suite_path = cask_env.config.applications / "Linked Suite"
    outside = cask_env.tmp_path / "outside-suite"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    # Replace the installed suite with a symlink to an outside directory.
    import shutil

    shutil.rmtree(suite_path)
    suite_path.symlink_to(outside, target_is_directory=True)

    cask_env.installer.uninstall("suite")

    assert not suite_path.is_symlink()
    assert keep.read_text() == "keep"


# ---------------------------------------------------------------------------
# generic artifact declarative `steps:` grammar
# ---------------------------------------------------------------------------


def _write_steps_cask(
    cask_env: SimpleNamespace,
    artifacts: str,
    *,
    token: str = "steps",
    entries: dict[str, bytes] | None = None,
) -> None:
    archive = cask_env.tmp_path / f"{token}-1.0.zip"
    digest = _make_zip(
        archive,
        entries
        or {
            "files/app.txt": b"app payload\n",
            "files/dir/keep.txt": b"keep\n",
        },
    )
    _write_cask(cask_env.tap, token, "1.0", digest, archive, artifacts=artifacts)


def test_generic_artifact_steps_mkdir_copy_symlink(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", steps: ['
            '["mkdir", "staging"], '
            '["copy", "files/app.txt", "staging/app.txt"], '
            '["symlink", "staging/app.txt", "staging/link.txt"]]'
        ),
    )

    result = cask_env.installer.install(cask_env.loader.load("steps"))

    staging = cask_env.config.applications / "staging"
    assert staging.is_dir()
    assert (staging / "app.txt").read_text() == "app payload\n"
    link = staging / "link.txt"
    assert link.is_symlink()
    assert (staging / "link.txt").read_text() == "app payload\n"
    assert len(result.artifacts) == 3

    cask_env.installer.uninstall("steps")
    assert not staging.exists()


def test_generic_artifact_steps_use_dirmethod_base(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", dirmethod: :prefix, steps: ['
            '["mkdir", "share/tool"], '
            '["copy", "files/app.txt", "share/tool/app.txt"]]'
        ),
    )

    cask_env.installer.install(cask_env.loader.load("steps"))

    target = cask_env.config.prefix / "share" / "tool" / "app.txt"
    assert target.read_text() == "app payload\n"


def test_generic_artifact_steps_reject_unknown_op(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env, '  artifact "ignored", steps: [["delete", "x"]]'
    )

    with pytest.raises(
        CaskError, match="unsupported generic artifact step"
    ):
        cask_env.installer.install(cask_env.loader.load("steps"))


def test_generic_artifact_steps_reject_symlink_escape(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", steps: ['
            '["symlink", "../outside", "link.txt"]]'
        ),
    )

    with pytest.raises(CaskError, match="escapes the destination base"):
        cask_env.installer.install(cask_env.loader.load("steps"))


def test_generic_artifact_steps_validate_before_install(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", steps: ['
            '["mkdir", "never-created"], '
            '["copy", "missing.txt", "never-created/missing.txt"]]'
        ),
    )

    with pytest.raises(CaskError, match="copy source"):
        cask_env.installer.install(cask_env.loader.load("steps"))
    assert not (cask_env.config.applications / "never-created").exists()


def test_generic_artifact_steps_require_array(cask_env: SimpleNamespace) -> None:
    _write_steps_cask(cask_env, '  artifact "ignored", steps: "copy"')

    with pytest.raises(CaskError, match="must be a non-empty array"):
        cask_env.installer.install(cask_env.loader.load("steps"))


# ---------------------------------------------------------------------------
# zap broken symlink edges
# ---------------------------------------------------------------------------


def test_zap_broken_symlink_outside_roots_is_refused(
    cask_env: SimpleNamespace,
) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    link = cask_env.config.prefix / "broken-outside"
    link.symlink_to(Path("/etc/pybrew-definitely-missing"))
    cask.zap = [str(link)]

    with pytest.raises(CaskError, match="outside allowed roots"):
        cask_env.installer.zap(cask)
    assert link.is_symlink()


def test_zap_broken_symlink_inside_roots_is_removed(
    cask_env: SimpleNamespace,
) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    link = cask_env.config.prefix / "broken-inside"
    link.symlink_to(cask_env.config.prefix / "missing-target")
    cask.zap = [str(link)]

    removed = cask_env.installer.zap(cask)

    assert removed == [link]
    assert not link.is_symlink()


# ---------------------------------------------------------------------------
# generic artifact steps: move / chmod / link / hardlink
# ---------------------------------------------------------------------------


def test_generic_artifact_steps_move_chmod_link_hardlink(
    cask_env: SimpleNamespace,
) -> None:
    import os
    import stat as stat_module

    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", steps: ['
            '["mkdir", "out"], '
            '["move", "files/app.txt", "out/moved.txt"], '
            '["chmod", "755", "out/moved.txt"], '
            '["hardlink", "files/dir/keep.txt", "out/hard.txt"], '
            '["link", "out/moved.txt", "out/link.txt"]]'
        ),
        entries={
            "files/app.txt": b"move payload\n",
            "files/dir/keep.txt": b"hardlink payload\n",
        },
    )

    result = cask_env.installer.install(cask_env.loader.load("steps"))

    out = cask_env.config.applications / "out"
    moved = out / "moved.txt"
    hard = out / "hard.txt"
    link = out / "link.txt"
    assert moved.read_text() == "move payload\n"
    assert stat_module.S_IMODE(os.stat(moved).st_mode) == 0o755
    assert hard.read_text() == "hardlink payload\n"
    assert link.is_symlink()
    assert link.read_text() == "move payload\n"
    # mkdir + move + hardlink + symlink are receipt-tracked; chmod is not.
    assert len(result.artifacts) == 4

    cask_env.installer.uninstall("steps")
    assert not out.exists()


def test_generic_artifact_steps_chmod_integer_mode(
    cask_env: SimpleNamespace,
) -> None:
    import os
    import stat as stat_module

    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", steps: ['
            '["copy", "files/app.txt", "mode.txt"], '
            f'["chmod", {0o600}, "mode.txt"]]'
        ),
    )

    cask_env.installer.install(cask_env.loader.load("steps"))

    target = cask_env.config.applications / "mode.txt"
    assert stat_module.S_IMODE(os.stat(target).st_mode) == 0o600


def test_generic_artifact_steps_chmod_rejects_symlink(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", steps: ['
            '["copy", "files/app.txt", "real.txt"], '
            '["symlink", "real.txt", "alias.txt"], '
            '["chmod", "644", "alias.txt"]]'
        ),
    )

    with pytest.raises(
        CaskError, match="must be an existing non-symlink path"
    ):
        cask_env.installer.install(cask_env.loader.load("steps"))
    # The earlier symlink step is rolled back.
    assert not (cask_env.config.applications / "alias.txt").is_symlink()


def test_generic_artifact_steps_move_missing_source(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env, '  artifact "ignored", steps: [["move", "missing.txt", "out.txt"]]'
    )

    with pytest.raises(CaskError, match="move source"):
        cask_env.installer.install(cask_env.loader.load("steps"))


def test_generic_artifact_steps_hardlink_directory_rejected(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env,
        '  artifact "ignored", steps: [["hardlink", "files", "out-dir"]]',
    )

    with pytest.raises(CaskError, match="hardlink source.*must be a file"):
        cask_env.installer.install(cask_env.loader.load("steps"))


def test_generic_artifact_steps_invalid_chmod_mode(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", steps: ['
            '["copy", "files/app.txt", "mode.txt"], '
            '["chmod", "not-octal", "mode.txt"]]'
        ),
    )

    with pytest.raises(CaskError, match="not an octal value"):
        cask_env.installer.install(cask_env.loader.load("steps"))


# ---------------------------------------------------------------------------
# zap root-path refusal edges
# ---------------------------------------------------------------------------


def test_zap_refuses_install_root_itself(cask_env: SimpleNamespace) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    cask.zap = [str(cask_env.config.prefix)]

    with pytest.raises(CaskError, match="refusing to zap unsafe path"):
        cask_env.installer.zap(cask)
    assert cask_env.config.prefix.is_dir()


def test_zap_refuses_home_directory(cask_env: SimpleNamespace) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    home = str(Path.home())
    cask.zap = [home]

    with pytest.raises(CaskError, match="refusing to zap unsafe path"):
        cask_env.installer.zap(cask)
    assert Path(home).exists()


# ---------------------------------------------------------------------------
# Wave 9: directory copy, chmod document/receipt, link edges
# ---------------------------------------------------------------------------


def test_generic_artifact_steps_directory_copy(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env,
        '  artifact "ignored", steps: [["copy", "tree", "nested/copied-tree"]]',
        entries={
            "tree/readme.txt": b"tree readme\n",
            "tree/nested/keep.txt": b"keep\n",
        },
    )

    result = cask_env.installer.install(cask_env.loader.load("steps"))

    copied = cask_env.config.applications / "nested" / "copied-tree"
    assert (copied / "readme.txt").read_text() == "tree readme\n"
    assert (copied / "nested" / "keep.txt").read_text() == "keep\n"
    assert result.artifacts == [str(copied)]

    cask_env.installer.uninstall("steps")
    assert not copied.exists()


def test_generic_artifact_steps_chmod_does_not_track_receipt(
    cask_env: SimpleNamespace,
) -> None:
    import os
    import stat as stat_module

    preexisting = cask_env.config.prefix / "share" / "pre.txt"
    preexisting.parent.mkdir(parents=True, exist_ok=True)
    preexisting.write_text("preexisting\n", encoding="utf-8")
    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", dirmethod: :prefix, steps: ['
            '["chmod", "600", "share/pre.txt"]]'
        ),
    )

    cask_env.installer.install(cask_env.loader.load("steps"))

    assert stat_module.S_IMODE(os.stat(preexisting).st_mode) == 0o600
    cask_env.installer.uninstall("steps")
    # chmod is a modification, not a receipt-tracked installed path.
    assert preexisting.is_file()


def test_generic_artifact_steps_symlink_directory(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", steps: ['
            '["mkdir", "real-dir"], '
            '["symlink", "real-dir", "alias-dir"]]'
        ),
    )

    cask_env.installer.install(cask_env.loader.load("steps"))

    alias = cask_env.config.applications / "alias-dir"
    assert alias.is_symlink()
    assert alias.is_dir()


def test_generic_artifact_steps_hardlink_rejects_symlink_source(
    cask_env: SimpleNamespace,
) -> None:
    staging = cask_env.tmp_path / "staging"
    staging.mkdir()
    real = staging / "real.txt"
    real.write_text("payload\n", encoding="utf-8")
    (staging / "link.txt").symlink_to(real)
    cask = parse_cask_source('cask "unit" do\n  version "1.0"\nend\n')

    with pytest.raises(CaskError, match="must be a file"):
        cask_env.installer._plan_generic_step(
            cask,
            cask_env.config.applications,
            staging,
            ["hardlink", "link.txt", "out.txt"],
        )


def test_generic_artifact_steps_unknown_op_suggests(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env, '  artifact "ignored", steps: [["cpy", "x"]]'
    )

    with pytest.raises(CaskError, match="did you mean 'copy'"):
        cask_env.installer.install(cask_env.loader.load("steps"))


def test_generic_artifact_steps_execution_failure_rolls_back_all(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", steps: ['
            '["mkdir", "rollback-dir"], '
            '["copy", "files/app.txt", "rollback-dir/app.txt"], '
            '["chmod", "644", "rollback-dir/missing.txt"]]'
        ),
    )

    with pytest.raises(
        CaskError, match="must be an existing non-symlink path"
    ):
        cask_env.installer.install(cask_env.loader.load("steps"))
    assert not (cask_env.config.applications / "rollback-dir").exists()


# ---------------------------------------------------------------------------
# Wave 9: suite regular-file target and recursive zap
# ---------------------------------------------------------------------------


def test_uninstall_suite_regular_file_target(cask_env: SimpleNamespace) -> None:
    archive = cask_env.tmp_path / "Suite-1.0.zip"
    digest = _make_zip(
        archive, {"Suite Folder/App1.app/Contents/Info.plist": b"<plist/>"}
    )
    _write_cask(
        cask_env.tap,
        "suite",
        "1.0",
        digest,
        archive,
        artifacts='  suite "Suite Folder", target: "Replaced Suite"',
    )
    cask_env.installer.install(cask_env.loader.load("suite"))
    path = cask_env.config.applications / "Replaced Suite"
    import shutil

    shutil.rmtree(path)
    path.write_text("user file\n", encoding="utf-8")

    cask_env.installer.uninstall("suite")

    assert not path.exists()


def test_zap_removes_directory_recursively(
    cask_env: SimpleNamespace,
) -> None:
    cask = parse_cask_source('cask "zappy" do\n  version "1.0"\nend\n')
    tree = cask_env.config.prefix / "zap-tree"
    nested = tree / "nested"
    nested.mkdir(parents=True)
    keep = nested / "keep.txt"
    keep.write_text("remove", encoding="utf-8")
    cask.zap = [str(tree)]

    removed = cask_env.installer.zap(cask)

    assert removed == [tree]
    assert not tree.exists()


# ---------------------------------------------------------------------------
# Wave 10: chmod tracking and rollback restoration edges
# ---------------------------------------------------------------------------


def test_generic_artifact_steps_chmod_tracked_on_created_path(
    cask_env: SimpleNamespace,
) -> None:
    import os
    import stat as stat_module

    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", steps: ['
            '["copy", "files/app.txt", "tracked.txt"], '
            '["chmod", "600", "tracked.txt", "track"]]'
        ),
    )

    result = cask_env.installer.install(cask_env.loader.load("steps"))

    target = cask_env.config.applications / "tracked.txt"
    assert stat_module.S_IMODE(os.stat(target).st_mode) == 0o600
    assert result.artifacts == [str(target)]
    cask_env.installer.uninstall("steps")
    assert not target.exists()


def test_generic_artifact_steps_chmod_tracked_inside_copied_directory(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", steps: ['
            '["copy", "tree", "tracked-tree"], '
            '["chmod", "600", "tracked-tree/readme.txt", "track"]]'
        ),
        entries={
            "tree/readme.txt": b"tree readme\n",
            "tree/nested/keep.txt": b"keep\n",
        },
    )

    cask_env.installer.install(cask_env.loader.load("steps"))

    target = cask_env.config.applications / "tracked-tree" / "readme.txt"
    assert target.is_file()
    cask_env.installer.uninstall("steps")
    assert not (cask_env.config.applications / "tracked-tree").exists()


def test_generic_artifact_steps_chmod_tracked_preexisting_rejected(
    cask_env: SimpleNamespace,
) -> None:
    import os
    import stat as stat_module

    preexisting = cask_env.config.prefix / "share" / "pre.txt"
    preexisting.parent.mkdir(parents=True, exist_ok=True)
    preexisting.write_text("preexisting\n", encoding="utf-8")
    before_mode = stat_module.S_IMODE(os.stat(preexisting).st_mode)
    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", dirmethod: :prefix, steps: ['
            '["chmod", "600", "share/pre.txt", true]]'
        ),
    )

    with pytest.raises(CaskError, match="cannot receipt-track"):
        cask_env.installer.install(cask_env.loader.load("steps"))
    assert preexisting.is_file()
    assert stat_module.S_IMODE(os.stat(preexisting).st_mode) == before_mode


def test_generic_artifact_steps_chmod_invalid_track_flag(
    cask_env: SimpleNamespace,
) -> None:
    _write_steps_cask(
        cask_env,
        '  artifact "ignored", steps: [["chmod", "644", "x.txt", "maybe"]]',
    )

    with pytest.raises(CaskError, match="track flag"):
        cask_env.installer.install(cask_env.loader.load("steps"))


def test_generic_artifact_steps_chmod_explicit_untracked(
    cask_env: SimpleNamespace,
) -> None:
    import os
    import stat as stat_module

    preexisting = cask_env.config.prefix / "share" / "untracked.txt"
    preexisting.parent.mkdir(parents=True, exist_ok=True)
    preexisting.write_text("untracked\n", encoding="utf-8")
    _write_steps_cask(
        cask_env,
        (
            '  artifact "ignored", dirmethod: :prefix, steps: ['
            '["chmod", "640", "share/untracked.txt", "untracked"]]'
        ),
    )

    cask_env.installer.install(cask_env.loader.load("steps"))

    assert (
        stat_module.S_IMODE(os.stat(preexisting).st_mode) == 0o640
    )
    cask_env.installer.uninstall("steps")
    assert preexisting.is_file()


def _unit_steps_rollback(
    cask_env: SimpleNamespace,
    staging: Path,
    steps: list[object],
) -> None:
    from pybrew.cask import Artifact

    cask = parse_cask_source(
        'cask "unit-rollback" do\n  version "1.0"\nend\n'
    )
    artifact = Artifact(
        "artifact", "ignored", None, {"steps": steps}
    )
    with pytest.raises(CaskError):
        cask_env.installer._install_generic_artifact_steps(
            cask, artifact, staging, False, []
        )


def test_steps_move_rollback_restores_staged_source(
    cask_env: SimpleNamespace,
) -> None:
    staging = cask_env.tmp_path / "staging-move"
    staging.mkdir()
    source = staging / "move.txt"
    source.write_text("move payload\n", encoding="utf-8")

    _unit_steps_rollback(
        cask_env,
        staging,
        [
            ["move", "move.txt", "out.txt"],
            ["chmod", "644", "missing.txt"],
        ],
    )

    assert source.is_file()
    assert source.read_text() == "move payload\n"
    assert not (cask_env.config.applications / "out.txt").exists()


def test_steps_copy_directory_rollback(
    cask_env: SimpleNamespace,
) -> None:
    staging = cask_env.tmp_path / "staging-copy"
    tree = staging / "tree"
    tree.mkdir(parents=True)
    (tree / "keep.txt").write_text("keep\n", encoding="utf-8")

    _unit_steps_rollback(
        cask_env,
        staging,
        [
            ["copy", "tree", "out-tree"],
            ["chmod", "644", "missing.txt"],
        ],
    )

    assert tree.is_dir()
    assert (tree / "keep.txt").is_file()
    assert not (cask_env.config.applications / "out-tree").exists()


def test_steps_hardlink_rollback(
    cask_env: SimpleNamespace,
) -> None:
    staging = cask_env.tmp_path / "staging-hard"
    staging.mkdir()
    source = staging / "hard.txt"
    source.write_text("hard payload\n", encoding="utf-8")

    _unit_steps_rollback(
        cask_env,
        staging,
        [
            ["hardlink", "hard.txt", "out-hard.txt"],
            ["chmod", "644", "missing.txt"],
        ],
    )

    assert source.is_file()
    assert not (cask_env.config.applications / "out-hard.txt").exists()


def test_steps_symlink_rollback(
    cask_env: SimpleNamespace,
) -> None:
    staging = cask_env.tmp_path / "staging-link"
    staging.mkdir()
    source = staging / "link-source.txt"
    source.write_text("source\n", encoding="utf-8")

    _unit_steps_rollback(
        cask_env,
        staging,
        [
            ["symlink", "link-source.txt", "out-link.txt"],
            ["chmod", "644", "missing.txt"],
        ],
    )

    assert source.is_file()
    assert not (cask_env.config.applications / "out-link.txt").exists()
