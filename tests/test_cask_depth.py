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
