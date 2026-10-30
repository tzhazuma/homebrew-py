"""Tests for the expanded Homebrew formula Ruby API shim.

The shim is only reachable through the opt-in
``PYBREW_ALLOW_ARBITRARY_RUBY=1`` bridge; the default declarative parser must
keep behaving exactly as before.  Every fixture here is synthetic and local:
no network access and no writes outside ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import struct
import tarfile
import zipfile
from pathlib import Path

import pytest

import pybrew.download as download_module
from pybrew.download import Downloader
from pybrew.errors import BuildError, UnsupportedInstall
from pybrew.models import BottleFile, Dependency, Formula, PatchSpec, Requirement, SourceSpec, is_linux, is_macos
from pybrew.paths import Config
from pybrew.ruby_executor import execute_install_body
from pybrew.source_builder import build_source

ALLOW_ENV = "PYBREW_ALLOW_ARBITRARY_RUBY"
RUBY_ENV = "PYBREW_RUBY"
_RUBY = os.environ.get(RUBY_ENV) or "/usr/bin/ruby"
pytestmark = pytest.mark.skipif(
    shutil.which(_RUBY) is None and not Path(_RUBY).is_file(),
    reason="Ruby interpreter not installed; opt-in bridge tests skipped",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _formula(name: str, version: str, body: str) -> Formula:
    formula = Formula(name=name, version=version)
    formula.install_source = "def install\n" + body + "end\n"
    return formula


def _config(tmp_path: Path) -> Config:
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"
    )
    config.ensure()
    return config


def _build(
    tmp_path: Path,
    formula: Formula,
    *,
    build_root: Path | None = None,
    keg: Path | None = None,
    verbose: bool = False,
    downloader: Downloader | None = None,
) -> Path:
    build_root = build_root or tmp_path / "build"
    build_root.mkdir(exist_ok=True)
    keg = keg or tmp_path / "keg"
    keg.mkdir(exist_ok=True)
    build_source(
        formula,
        _config(tmp_path),
        build_root,
        keg,
        verbose=verbose,
        downloader=downloader,
    )
    return keg


def _downloader(tmp_path: Path) -> Downloader:
    return Downloader(tmp_path / "cache" / "downloads")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tar_resource(
    destination: Path, top: str, files: dict[str, str]
) -> Path:
    """Write a synthetic ``.tar.gz`` resource with a single top-level dir."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as tar:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return destination


def _zip_resource(
    destination: Path, top: str | None, files: dict[str, str]
) -> Path:
    """Write a synthetic ``.zip`` resource (optionally wrapped in a top dir)."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as bundle:
        for name, text in files.items():
            bundle.writestr(f"{top}/{name}" if top else name, text)
    return destination


def _unsafe_tar_resource(destination: Path, kind: str) -> Path:
    """Write a tar resource that must be rejected by safe extraction."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as tar:
        if kind == "traversal":
            payload = b"escape\n"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        elif kind == "symlink":
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        else:  # pragma: no cover - test authoring guard
            raise AssertionError(kind)
    return destination


def _unsafe_zip_resource(destination: Path, kind: str) -> Path:
    """Write a zip resource that must be rejected by safe extraction."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as bundle:
        if kind == "traversal":
            bundle.writestr("../escape.txt", "escape\n")
        elif kind == "symlink":
            info = zipfile.ZipInfo("link")
            info.external_attr = 0o120777 << 16
            bundle.writestr(info, "/etc/passwd")
        else:  # pragma: no cover - test authoring guard
            raise AssertionError(kind)
    return destination


def _elf_header(elf_type: int) -> bytes:
    ident = b"\x7fELF" + bytes([2, 1, 1]) + b"\x00" * 9
    return ident + struct.pack("<H", elf_type) + b"\x00" * 46


def _macho_header(filetype: int) -> bytes:
    return (
        b"\xcf\xfa\xed\xfe"
        + struct.pack("<I", 0x0100000C)  # CPU_TYPE_ARM64
        + struct.pack("<I", 0)
        + struct.pack("<I", filetype)
        + b"\x00" * 16
    )


# ---------------------------------------------------------------------------
# Pathname extensions
# ---------------------------------------------------------------------------


def test_pathname_file_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-pathname-files",
        "1.0",
        r'''
  target = prefix/"share/data.txt"
  target.atomic_write("one\n")
  target.append_lines("two")
  (prefix/"share/sha").write target.sha256
  target.verify_checksum target.sha256
  target.verify_checksum "sha256:#{target.sha256.upcase}"
  empty = prefix/"share/empty"
  empty.mkpath
  (prefix/"share/rmdir").write empty.rmdir_if_possible.to_s
  nonempty = prefix/"share/nonempty"
  nonempty.mkpath
  (nonempty/"child").write "x"
  (prefix/"share/rmdir-nonempty").write nonempty.rmdir_if_possible.to_s
  (prefix/"share/sub").mkpath
  dirs = (prefix/"share").subdirs.map { |dir| dir.basename.to_s }.sort
  (prefix/"share/subdirs").write dirs.join(",")
''',
    )

    keg = _build(tmp_path, formula)

    data = (keg / "share" / "data.txt").read_text()
    assert data == "one\ntwo\n"
    assert (keg / "share" / "sha").read_text() == hashlib.sha256(
        data.encode()
    ).hexdigest()
    assert (keg / "share" / "rmdir").read_text() == "true"
    assert (keg / "share" / "rmdir-nonempty").read_text() == "false"
    assert (keg / "share" / "subdirs").read_text() == "nonempty,sub"


def test_pathname_ensure_writable_restores_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-ensure-writable",
        "1.0",
        r'''
  target = prefix/"readonly"
  target.write "data"
  File.chmod(0o444, target.to_s)
  inside_writable = nil
  target.ensure_writable do
    inside_writable = target.writable?
    target.write "updated"
  end
  (prefix/"ensure").write [
    inside_writable,
    (target.stat.mode & 0o777).to_s(8),
    target.read,
  ].join("|")
''',
    )

    keg = _build(tmp_path, formula)

    parts = (keg / "ensure").read_text().split("|")
    assert parts[0] == "true"
    assert parts[1] == "444"
    assert parts[2] == "updated"


def test_pathname_verify_checksum_mismatch_is_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-checksum-mismatch",
        "1.0",
        r'''
  target = prefix/"payload"
  target.write "data"
  target.verify_checksum("0" * 64)
''',
    )

    with pytest.raises(BuildError, match="SHA256 mismatch"):
        _build(tmp_path, formula)


def test_pathname_executable_and_binary_predicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    build_root = tmp_path / "build"
    build_root.mkdir()
    (build_root / "script").write_text("#!/bin/sh\necho hi\n")
    (build_root / "plain").write_text("hello\n")
    (build_root / "elf-exe").write_bytes(_elf_header(2))
    (build_root / "elf-lib").write_bytes(_elf_header(3))
    (build_root / "macho-exe").write_bytes(_macho_header(2))
    (build_root / "macho-lib").write_bytes(_macho_header(6))
    (build_root / "macho-bundle").write_bytes(_macho_header(8))
    formula = _formula(
        "ruby-binary-predicates",
        "1.0",
        r'''
  results = {
    "text-script" => (buildpath/"script").text_executable?,
    "text-plain" => (buildpath/"plain").text_executable?,
    "elf-exe" => (buildpath/"elf-exe").binary_executable?,
    "elf-lib" => (buildpath/"elf-lib").dylib?,
    "macho-exe" => (buildpath/"macho-exe").binary_executable?,
    "macho-lib" => (buildpath/"macho-lib").dylib?,
    "macho-bundle" => (buildpath/"macho-bundle").mach_o_bundle?,
  }
  (prefix/"predicates").write(
    results.map { |key, value| "#{key}=#{value}" }.join("\n")
  )
''',
    )

    keg = _build(tmp_path, formula, build_root=build_root)

    predicates = dict(
        line.split("=", 1)
        for line in (keg / "predicates").read_text().splitlines()
    )
    assert predicates == {
        "text-script": "true",
        "text-plain": "false",
        "elf-exe": "true",
        "elf-lib": "true",
        "macho-exe": "true",
        "macho-lib": "true",
        "macho-bundle": "true",
    }


def test_pathname_scripts_symlinks_install_helpers_and_cd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    build_root = tmp_path / "build"
    build_root.mkdir()
    tool = build_root / "tool"
    tool.write_text("#!/bin/sh\necho tool\n")
    tool.chmod(0o755)
    (build_root / "movable").write_text("movable\n")
    formula = _formula(
        "ruby-pathname-scripts",
        "1.0",
        r'''
  bin.mkpath
  bin.write_exec_script buildpath/"tool"
  env_dir = prefix/"env"
  env_dir.mkpath
  (env_dir/"tool-args").write_env_script(
    "/usr/bin/env", ["FOO=bar"], { "FOO" => "bar" }
  )
  (env_dir/"tool-hash").write_env_script("/usr/bin/env", { "BAR" => "baz" })
  link = prefix/"tool-link"
  link.make_relative_symlink buildpath/"tool"
  (prefix/"resolved").write [
    link.resolved_path.to_s,
    link.resolved_path_exists?,
    File.readlink(link).start_with?("/"),
  ].join("|")
  prefix.install_symlink_p buildpath/"tool", "sym-tool"
  prefix.install_p buildpath/"movable", "moved-tool"
  sub = buildpath/"sub"
  sub.mkpath
  inside = sub.cd { Dir.pwd }
  (prefix/"cd").write [
    inside,
    File.realpath(Dir.pwd) == File.realpath(buildpath.to_s),
  ].join("|")
  info = prefix/"share/info/tool.info"
  info.dirname.mkpath
  info.write "info body"
  info.install_info
  info.uninstall_info
  (prefix/"info-ok").write "true"
''',
    )

    keg = _build(tmp_path, formula, build_root=build_root)

    exec_script = keg / "bin" / "tool"
    assert exec_script.is_file()
    assert exec_script.stat().st_mode & 0o777 == 0o755
    assert f'exec "{tool}" "$@"' in exec_script.read_text()

    env_args = keg / "env" / "tool-args"
    assert env_args.stat().st_mode & 0o777 == 0o555
    assert 'FOO="bar" exec "/usr/bin/env" FOO=bar "$@"' in env_args.read_text()
    env_hash = keg / "env" / "tool-hash"
    assert 'BAR="baz" exec "/usr/bin/env"' in env_hash.read_text()

    link = keg / "tool-link"
    assert link.is_symlink()
    assert not os.path.isabs(os.readlink(link))
    assert link.resolve() == tool.resolve()
    resolved = (keg / "resolved").read_text().split("|")
    assert Path(resolved[0]).resolve() == tool.resolve()
    assert resolved[1] == "true"
    assert resolved[2] == "false"

    sym_tool = keg / "sym-tool"
    assert sym_tool.is_symlink()
    assert sym_tool.resolve() == tool.resolve()

    moved = keg / "moved-tool"
    assert moved.read_text() == "movable\n"
    assert not (build_root / "movable").exists()

    inside, cwd_restored = (keg / "cd").read_text().split("|")
    assert Path(inside).resolve() == (build_root / "sub").resolve()
    assert cwd_restored == "true"
    assert (keg / "info-ok").read_text() == "true"


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------


def test_utils_inflections_safe_filename_which_and_deep_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-utils-strings",
        "1.0",
        r'''
  merged = Utils.deep_merge(
    { "a" => { "b" => 1 } },
    { "a" => { "c" => 2 } }
  )
  original = { "a" => [1, 2] }
  copy = original.deep_dup
  copy["a"] << 3
  values = {
    "deconstantize" => Utils.deconstantize("Alpha::Beta::Gamma"),
    "demodulize" => Utils.demodulize("Alpha::Beta::Gamma"),
    "underscore" => Utils.underscore("ActiveModel::Errors"),
    "pluralize-formula" => Utils.pluralize("formula", 2),
    "pluralize-dependency" => Utils.pluralize("dependency", 3),
    "pluralize-try" => Utils.pluralize("try", 2, include_count: true),
    "safe-filename" => Utils.safe_filename("a/b"),
    "safe-filename-ok" => Utils.safe_filename?("plain-name"),
    "safe-filename-bad" => Utils.safe_filename?("a/b"),
    "which-sh" => Utils.which("sh").is_a?(Pathname),
    "which-missing" => Utils.which("pybrew-definitely-missing").inspect,
    "deep-merge" => JSON.generate(merged),
    "hash-deep-merge" => JSON.generate({ "a" => 1 }.deep_merge({ "b" => 2 })),
    "deep-dup" => JSON.generate(original),
  }
  (prefix/"utils").write(
    values.map { |key, value| "#{key}=#{value}" }.join("\n")
  )
''',
    )

    keg = _build(tmp_path, formula)

    values = dict(
        line.split("=", 1) for line in (keg / "utils").read_text().splitlines()
    )
    assert values["deconstantize"] == "Alpha::Beta"
    assert values["demodulize"] == "Gamma"
    assert values["underscore"] == "active_model/errors"
    assert values["pluralize-formula"] == "formulae"
    assert values["pluralize-dependency"] == "dependencies"
    assert values["pluralize-try"] == "2 tries"
    assert values["safe-filename"] == "ab"
    assert values["safe-filename-ok"] == "true"
    assert values["safe-filename-bad"] == "false"
    assert values["which-sh"] == "true"
    assert values["which-missing"] == "nil"
    assert values["deep-merge"] == '{"a":{"b":1,"c":2}}'
    assert values["hash-deep-merge"] == '{"a":1,"b":2}'
    assert values["deep-dup"] == '{"a":[1,2]}'


def test_utils_subprocess_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-utils-process",
        "1.0",
        r'''
  (prefix/"popen-read").write Utils.popen_read("sh", "-c", "printf popen")
  (prefix/"safe-read").write Utils.safe_popen_read("sh", "-c", "printf safe")
  (prefix/"block-read").write(
    Utils.popen_read("sh", "-c", "printf block") { |io| io.read.upcase }
  )
  (prefix/"text-read").write Utils.popen_read_text("sh", "-c", "printf text")
  (prefix/"pipe").write Utils.pipe_output("cat", "piped")
  (prefix/"popen-write").write(
    Utils.popen_write("cat") { |pipe| pipe.write("written") }
  )
  (prefix/"git").write Utils.git("--version").include?("git version").to_s
  (prefix/"curl").write Utils.curl("--version").include?("curl").to_s
  (prefix/"gzip").write Utils.gzip("--version").to_s
''',
    )

    keg = _build(tmp_path, formula)

    assert (keg / "popen-read").read_text() == "popen"
    assert (keg / "safe-read").read_text() == "safe"
    assert (keg / "block-read").read_text() == "BLOCK"
    assert (keg / "text-read").read_text() == "text"
    assert (keg / "pipe").read_text() == "piped"
    assert (keg / "popen-write").read_text() == "written"
    if shutil.which("git"):
        assert (keg / "git").read_text() == "true"
    if shutil.which("curl"):
        assert (keg / "curl").read_text() == "true"
    assert (keg / "gzip").read_text() == "true"


def test_utils_safe_popen_read_failure_is_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-utils-process-fail",
        "1.0",
        r'''
  Utils.safe_popen_read("sh", "-c", "exit 9")
''',
    )

    with pytest.raises(BuildError, match="Failed executing"):
        _build(tmp_path, formula)


# ---------------------------------------------------------------------------
# Hardware::CPU / OS / MacOS
# ---------------------------------------------------------------------------


def test_hardware_cpu_extended_predicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-hardware-cpu",
        "1.0",
        r'''
  values = [
    Hardware::CPU.type,
    Hardware::CPU.arch,
    Hardware::CPU.bits,
    Hardware::CPU.is_64_bit?,
    Hardware::CPU.is_32_bit?,
    Hardware::CPU.arm?,
    Hardware::CPU.intel?,
    Hardware::CPU.family,
    Hardware::CPU.model.inspect,
    Hardware::CPU.flags.is_a?(Array),
    Hardware::CPU.features.is_a?(Array),
    Hardware::CPU.neon?,
    Hardware::CPU.sse4?,
    Hardware::CPU.avx?,
    Hardware::CPU.avx2?,
    Hardware::CPU.feature?(:pybrew_unknown_feature),
  ]
  (prefix/"cpu").write values.map { |value| value.inspect }.join("|")
''',
    )

    keg = _build(tmp_path, formula)

    parts = (keg / "cpu").read_text().split("|")
    arm = platform.machine().lower() in {"arm64", "aarch64"}
    assert parts[0] == (":arm" if arm else ":intel")
    assert parts[1] in {":arm64", ":x86_64", ":i386", ":dunno"}
    assert parts[2] in {"64", "32"}
    assert parts[3] == ("true" if parts[2] == "64" else "false")
    assert parts[4] == ("false" if parts[2] == "64" else "true")
    assert parts[5] == ("true" if arm else "false")
    assert parts[6] == ("false" if arm else "true")
    assert parts[7].startswith(":")
    model = parts[8].strip('"')
    assert model == "nil" or model.isdigit()
    assert parts[9] == "true"
    assert parts[10] == "true"
    for value in parts[11:15]:
        assert value in {"true", "false"}
    assert parts[15] == "false"


def test_macos_sdk_language_fallbacks_and_os_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "14.5")
    formula = _formula(
        "ruby-macos-api",
        "1.0",
        r'''
  sdk = MacOS.sdk
  (prefix/"macos").write [
    OS::Mac.equal?(MacOS),
    OS::Linux.respond_to?(:os_version),
    OS::Linux.os_version.to_s.empty?,
    MacOS.latest_sdk_version.to_s,
    MacOS.sdk_path.inspect,
    sdk.nil?,
    MacOS.preferred_perl_version,
    MacOS.languages.is_a?(Array),
    MacOS.languages.length,
    MacOS.language.inspect,
    MacOS.active_developer_dir.is_a?(String),
    OS.version.to_s,
  ].join("|")
''',
    )

    keg = _build(tmp_path, formula)

    parts = (keg / "macos").read_text().split("|")
    assert parts[0] == "true"
    assert parts[1] == "true"
    assert parts[2] == "false"
    assert parts[3]
    if is_macos():
        assert parts[4] != "nil" or parts[5] == "true"
        assert parts[6] == "5.34"
        assert parts[11] == "14"
    else:
        assert parts[4] == "nil"
        assert parts[5] == "true"
        assert parts[6] == "5.30"
        assert parts[11] == platform.release()
    assert parts[7] == "true"
    assert int(parts[8]) >= 0
    assert parts[9] == "nil" or parts[9].startswith('"')
    assert parts[10] == "true"


# ---------------------------------------------------------------------------
# ENV compiler helpers
# ---------------------------------------------------------------------------


def test_env_compiler_helpers_and_with_build_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-env-helpers",
        "1.0",
        r'''
  ENV["CC"] = "clang-test"
  ENV["CXX"] = "clang++-test"
  ENV["CFLAGS"] = "-O2"
  ENV["CPPFLAGS"] = "-I/one"
  ENV["LDFLAGS"] = "-L/one"
  ENV.append "CFLAGS", "-g"
  ENV.prepend "CFLAGS", "-Wall"
  ENV.append "CFLAGS", "-pipe", ";"
  ENV.remove "CPPFLAGS", "-I/one"
  inside = nil
  ENV.with_build_environment(
    env: { "WITH_BUILD" => "yes" }, cc: "cc-test"
  ) do
    inside = [ENV["WITH_BUILD"], ENV["CC"], ENV["CFLAGS"]].join(",")
  end
  after = [ENV["WITH_BUILD"].inspect, ENV["CC"], ENV["CFLAGS"]].join(",")
  values = [
    ENV.cc,
    ENV.cxx,
    ENV.cflags,
    ENV.cppflags.inspect,
    ENV.ldflags,
    ENV.make_jobs,
    inside,
    after,
  ]
  (prefix/"env").write values.map { |value| value.to_s }.join("|")
''',
    )

    keg = _build(tmp_path, formula)

    parts = (keg / "env").read_text().split("|")
    assert parts[0] == "clang-test"
    assert parts[1] == "clang++-test"
    assert parts[2] == "-Wall -O2 -g;-pipe"
    assert parts[3] == "nil"
    assert parts[4] == "-L/one"
    assert int(parts[5]) >= 1
    assert parts[6] == "yes,cc-test,-Wall -O2 -g;-pipe"
    assert parts[7] == "nil,clang-test,-Wall -O2 -g;-pipe"


# ---------------------------------------------------------------------------
# Formula / resource / patch / option / depends_on
# ---------------------------------------------------------------------------


def test_formula_lookup_resource_and_patch_noops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    prefix = tmp_path / "prefix"
    (prefix / "Cellar" / "dep" / "1.0").mkdir(parents=True)
    (prefix / "Cellar" / "dep" / "2.0").mkdir(parents=True)
    (prefix / "Cellar" / "declared-dep" / "3.0").mkdir(parents=True)
    formula = _formula(
        "ruby-formula-lookup",
        "1.0",
        r'''
  dep = Formula["dep"]
  (prefix/"formula").write [
    dep.opt_prefix.to_s,
    dep.prefix.to_s,
    dep.opt_bin.to_s,
    dep.version.to_s,
    dep.installed?,
    dep.rack.to_s,
  ].join("|")
  (prefix/"declared-formula").write Formula["declared-dep"].prefix.to_s
  patch :DATA
  patch do
    url "https://example.com/fix.patch"
    sha256 "abc"
  end
  option "with-extra"
  depends_on "dep"
  begin
    resource("missing").stage { }
  rescue => error
    (prefix/"missing-error").write error.message
  end
  begin
    resource("declared").stage { }
  rescue => error
    (prefix/"declared-error").write error.message
  end
  (prefix/"declared-url").write resource("declared").url.to_s
''',
    )
    formula.dependencies.append(Dependency(name="declared-dep"))
    formula.resources["declared"] = SourceSpec(
        url="https://example.com/resource.tar.gz", sha256="a" * 64
    )

    keg = _build(tmp_path, formula)

    parts = (keg / "formula").read_text().split("|")
    assert parts[0] == str(prefix / "opt" / "dep")
    assert parts[1] == str(prefix / "Cellar" / "dep" / "2.0")
    assert parts[2] == str(prefix / "opt" / "dep" / "bin")
    assert parts[3] == "2.0"
    assert parts[4] == "true"
    assert parts[5] == str(prefix / "Cellar" / "dep")
    assert (keg / "declared-formula").read_text() == str(
        prefix / "Cellar" / "declared-dep" / "3.0"
    )
    assert "is not defined" in (keg / "missing-error").read_text()
    assert "has not been staged" in (keg / "declared-error").read_text()
    assert (keg / "declared-url").read_text() == "https://example.com/resource.tar.gz"


def test_formula_lookup_missing_keg_is_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-formula-missing",
        "1.0",
        r'''
  Formula["not-installed"].prefix
''',
    )
    formula.dependencies.append(Dependency(name="not-installed"))

    with pytest.raises(BuildError, match="is not installed"):
        _build(tmp_path, formula)


def test_formula_object_metadata_paths_and_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-formula-object",
        "1.2.3",
        r'''
  ref = Formula["ruby-formula-object"]
  (prefix/"formula-object").write [
    ref.name,
    ref.full_name,
    ref.version.to_s,
    ref.revision,
    ref.pkg_version.to_s,
    ref.prefix.to_s,
    ref.opt_prefix.to_s,
    ref.opt_bin.to_s,
    ref.bin.to_s,
    ref.lib.to_s,
    ref.include.to_s,
    ref.share.to_s,
    ref.pkgshare.to_s,
    ref.testpath.to_s,
    ref.build.stable?,
    ref.build.head?,
    ref.build.bottle?,
    ref.build.with?("extra"),
    ref.build.without?("extra"),
    Formula.factory("dep-runtime").name,
    Formula.factory("dep-runtime").full_name,
    ref.installed?,
    ref.any_version_installed?,
    ref.latest_version_installed?,
  ].join("|")
  (prefix/"formula-deps").write ref.deps.map { |dep| dep.name }.join(",")
  (prefix/"formula-runtime").write(
    ref.runtime_dependencies.map { |dep| dep.name }.join(",")
  )
  (prefix/"formula-recursive").write(
    ref.recursive_dependencies.map { |dep| dep.name }.join(",")
  )
  (prefix/"formula-include").write ref.deps.include?("dep-runtime")
''',
    )
    formula.tap = "acme/tap"
    formula.revision = 2
    formula.dependencies.append(Dependency(name="dep-runtime"))
    formula.dependencies.append(
        Dependency(name="dep-build", tags=frozenset({"build"}))
    )
    formula.dependencies.append(
        Dependency(name="dep-test", tags=frozenset({"test"}))
    )
    formula.dependencies.append(
        Dependency(name="dep-optional", tags=frozenset({"optional"}))
    )

    cellar_keg = tmp_path / "prefix" / "Cellar" / "ruby-formula-object" / "1.2.3_2"
    cellar_keg.mkdir(parents=True)
    keg = _build(tmp_path, formula, keg=cellar_keg)

    prefix = tmp_path / "prefix"
    parts = (keg / "formula-object").read_text().split("|")
    assert parts[0] == "ruby-formula-object"
    assert parts[1] == "acme/tap/ruby-formula-object"
    assert parts[2] == "1.2.3"
    assert parts[3] == "2"
    assert parts[4] == "1.2.3_2"
    assert parts[5] == str(cellar_keg)
    assert parts[6] == str(prefix / "opt" / "ruby-formula-object")
    assert parts[7] == str(prefix / "opt" / "ruby-formula-object" / "bin")
    assert parts[8] == str(cellar_keg / "bin")
    assert parts[9] == str(cellar_keg / "lib")
    assert parts[10] == str(cellar_keg / "include")
    assert parts[11] == str(cellar_keg / "share")
    assert parts[12] == str(cellar_keg / "share" / "ruby-formula-object")
    assert parts[13] == str(cellar_keg / "test")
    assert parts[14:19] == ["true", "false", "false", "false", "true"]
    assert parts[19] == "dep-runtime"
    assert parts[20] == "dep-runtime"
    assert parts[21] == "true"
    assert parts[22] == "true"
    assert parts[23] == "true"
    assert (keg / "formula-deps").read_text() == (
        "dep-runtime,dep-build,dep-test,dep-optional"
    )
    assert (keg / "formula-runtime").read_text() == "dep-runtime"
    assert (keg / "formula-recursive").read_text() == (
        "dep-runtime,dep-build,dep-test,dep-optional"
    )
    assert (keg / "formula-include").read_text() == "true"


def test_recursive_dependencies_follow_installed_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    dep_keg = tmp_path / "prefix" / "Cellar" / "dep-a" / "1.0"
    dep_keg.mkdir(parents=True)
    (dep_keg / "INSTALL_RECEIPT.json").write_text(
        json.dumps({"runtime_dependencies": ["dep-b"]})
    )
    formula = _formula(
        "ruby-recursive-deps",
        "1.0",
        r'''
  ref = Formula["ruby-recursive-deps"]
  (prefix/"recursive").write(
    ref.recursive_dependencies.map { |dep| dep.name }.join(",")
  )
  (prefix/"dep-a-deps").write(
    Formula["dep-a"].deps.map { |dep| dep.name }.join(",")
  )
  (prefix/"dep-a-runtime").write(
    Formula["dep-a"].runtime_dependencies.map { |dep| dep.name }.join(",")
  )
''',
    )
    formula.dependencies.append(Dependency(name="dep-a"))

    keg = _build(tmp_path, formula)

    assert (keg / "recursive").read_text() == "dep-a,dep-b"
    assert (keg / "dep-a-deps").read_text() == "dep-b"
    assert (keg / "dep-a-runtime").read_text() == "dep-b"


def test_formula_installed_predicates_for_versions_and_missing_deps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    prefix = tmp_path / "prefix"
    (prefix / "Cellar" / "old-dep" / "1.0").mkdir(parents=True)
    (prefix / "Cellar" / "new-dep" / "2.0").mkdir(parents=True)
    (prefix / "Cellar" / "ruby-installed-predicates" / "1.0").mkdir(
        parents=True
    )
    formula = _formula(
        "ruby-installed-predicates",
        "2.0",
        r'''
  old = Formula["old-dep"]
  newest = Formula["new-dep"]
  absent = Formula["absent-dep"]
  current = Formula["ruby-installed-predicates"]
  (prefix/"installed").write [
    old.installed?,
    old.any_version_installed?,
    old.latest_version_installed?,
    old.version.to_s,
    old.pkg_version.to_s,
    newest.installed?,
    newest.version.to_s,
    absent.installed?,
    absent.any_version_installed?,
    absent.latest_version_installed?,
    current.installed?,
    current.any_version_installed?,
    current.latest_version_installed?,
  ].join("|")
''',
    )
    formula.dependencies.append(Dependency(name="old-dep"))
    formula.dependencies.append(Dependency(name="new-dep"))
    formula.dependencies.append(Dependency(name="absent-dep"))

    keg = _build(tmp_path, formula)

    parts = (keg / "installed").read_text().split("|")
    assert parts[0:5] == ["true", "true", "true", "1.0", "1.0"]
    assert parts[5:7] == ["true", "2.0"]
    assert parts[7:10] == ["false", "false", "false"]
    # The current formula is 2.0 and only 1.0 is installed in the Cellar.
    assert parts[10:13] == ["false", "true", "false"]


def test_resource_metadata_lookup_and_staged_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-resource-metadata",
        "1.0",
        r'''
  declared = resource("declared")
  list = formula.resources
  (prefix/"resource-meta").write [
    declared.name,
    declared.url,
    declared.sha256,
    declared.version.to_s,
    declared.staged?,
    list.length,
    list["declared"].name,
    list.map(&:name).join(","),
    list.key?(:declared),
    list.keys.join(","),
    list.to_h["declared"].version.to_s,
    formula.resource("declared").name,
  ].join("|")
  declared.stage do |path|
    (prefix/"resource-stage").write(Dir.pwd + "|" + path.to_s)
  end
  (prefix/"resource-fetch").write(declared.fetch.to_s)
  pairs = []
  list.each { |name, item| pairs << "#{name}=#{item.name}" }
  values = []
  list.each_value { |item| values << item.name }
  (prefix/"resource-shapes").write [pairs.join(","), values.join(",")].join("|")
''',
    )
    formula.resources["declared"] = SourceSpec(
        url="https://example.com/declared-2.4.1.tar.gz", sha256="b" * 64
    )
    build_root = tmp_path / "build"
    staged = build_root / ".pybrew-resources" / "declared"
    staged.mkdir(parents=True)
    (staged / "payload.txt").write_text("staged payload\n")

    keg = _build(tmp_path, formula, build_root=build_root)

    parts = (keg / "resource-meta").read_text().split("|")
    assert parts[0] == "declared"
    assert parts[1] == "https://example.com/declared-2.4.1.tar.gz"
    assert parts[2] == "b" * 64
    assert parts[3] == "2.4.1"
    assert parts[4] == "true"
    assert parts[5] == "1"
    assert parts[6] == "declared"
    assert parts[7] == "declared"
    assert parts[8] == "true"
    assert parts[9] == "declared"
    assert parts[10] == "2.4.1"
    assert parts[11] == "declared"

    stage = (keg / "resource-stage").read_text().split("|")
    assert Path(stage[0]).resolve() == staged.resolve()
    assert Path(stage[1]).resolve() == staged.resolve()
    assert (keg / "resource-fetch").read_text() == str(staged)
    assert (keg / "resource-shapes").read_text() == (
        "declared=declared|declared"
    )


def test_resource_stage_and_fetch_without_staged_path_are_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-resource-unstaged",
        "1.0",
        r'''
  declared = resource("declared")
  begin
    declared.stage { }
  rescue => error
    (prefix/"stage-error").write error.message
  end
  begin
    declared.fetch
  rescue => error
    (prefix/"fetch-error").write error.message
  end
  (prefix/"resource-version").write declared.version.to_s
''',
    )
    formula.resources["declared"] = SourceSpec(
        url="https://example.com/plain.tar.gz", sha256="c" * 64
    )

    keg = _build(tmp_path, formula)

    assert "has not been staged" in (keg / "stage-error").read_text()
    assert "has not been staged" in (keg / "fetch-error").read_text()
    # A URL without a detectable version yields no resource version.
    assert (keg / "resource-version").read_text() == ""


def test_resource_auto_stage_tar_and_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")

    # Fail loudly if the resource staging path ever tries to use the network.
    original_open_url = download_module.open_url

    def local_only(url, headers=None):
        assert str(url).startswith("file://"), f"unexpected network access: {url}"
        return original_open_url(url, headers)

    monkeypatch.setattr(download_module, "open_url", local_only)

    tar_archive = _tar_resource(
        tmp_path / "src" / "data-1.0.tar.gz",
        "data-1.0",
        {"tool": "#!/bin/sh\necho tar-tool\n"},
    )
    zip_archive = _zip_resource(
        tmp_path / "src" / "zdata-2.0.zip",
        "zdata-2.0",
        {"ziptool": "#!/bin/sh\necho zip-tool\n"},
    )
    formula = _formula(
        "ruby-resource-auto-stage",
        "1.0",
        r'''
  resource("data").stage do |dir|
    (prefix/"data-stage").write dir.to_s
    bin.install "tool"
  end
  resource("zdata").stage do
    bin.install "ziptool"
  end
  (prefix/"data-path").write resource("data").staged_path.to_s
  (prefix/"data-fetch").write resource("data").fetch.to_s
  (prefix/"data-stage-noblock").write resource("data").stage.to_s
  (prefix/"data-staged").write resource("data").staged?.to_s
  (prefix/"zdata-staged").write resource("zdata").staged?.to_s
''',
    )
    formula.resources["data"] = SourceSpec(
        url=tar_archive.as_uri(), sha256=_sha256(tar_archive)
    )
    formula.resources["zdata"] = SourceSpec(
        url=zip_archive.as_uri(), sha256=_sha256(zip_archive)
    )

    downloader = _downloader(tmp_path)
    keg = _build(tmp_path, formula, downloader=downloader)

    build_root = tmp_path / "build"
    tar_stage = build_root / ".pybrew-resources" / "data"
    zip_stage = build_root / ".pybrew-resources" / "zdata"
    assert tar_stage.is_dir()
    assert zip_stage.is_dir()
    assert (tar_stage / "data-1.0" / "tool").is_file()
    assert (zip_stage / "zdata-2.0" / "ziptool").is_file()

    assert (keg / "bin" / "tool").read_text() == "#!/bin/sh\necho tar-tool\n"
    assert (keg / "bin" / "ziptool").read_text() == "#!/bin/sh\necho zip-tool\n"

    assert (keg / "data-staged").read_text() == "true"
    assert (keg / "zdata-staged").read_text() == "true"
    assert Path((keg / "data-path").read_text()).resolve() == tar_stage.resolve()
    assert Path((keg / "data-fetch").read_text()).resolve() == tar_stage.resolve()
    # ``stage`` without a block returns the staged directory.
    assert (
        Path((keg / "data-stage-noblock").read_text()).resolve()
        == tar_stage.resolve()
    )
    # ``stage`` enters the single top-level directory of the archive.
    assert (
        Path((keg / "data-stage").read_text()).resolve()
        == (tar_stage / "data-1.0").resolve()
    )

    cached = sorted(path.name for path in downloader.cache_dir.iterdir())
    assert len(cached) == 2
    assert any(name.startswith(_sha256(tar_archive)) for name in cached)
    assert any(name.startswith(_sha256(zip_archive)) for name in cached)


def test_resource_pre_staged_directory_is_reused_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-resource-pre-staged",
        "1.0",
        r'''
  resource("data").stage do |dir|
    (prefix/"stage-path").write dir.to_s
    bin.install "tool"
  end
''',
    )
    # The URL is unusable on purpose: the pre-staged directory must win.
    formula.resources["data"] = SourceSpec(
        url="file:///does/not/exist/data-1.0.tar.gz", sha256="d" * 64
    )
    build_root = tmp_path / "build"
    staged = build_root / ".pybrew-resources" / "data"
    staged.mkdir(parents=True)
    (staged / "tool").write_text("#!/bin/sh\necho pre-staged\n")

    downloader = _downloader(tmp_path)
    keg = _build(tmp_path, formula, build_root=build_root, downloader=downloader)

    assert (keg / "bin" / "tool").read_text() == "#!/bin/sh\necho pre-staged\n"
    assert Path((keg / "stage-path").read_text()).resolve() == staged.resolve()
    assert list(downloader.cache_dir.iterdir()) == []


def test_resource_auto_stage_without_sha256_stays_metadata_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    archive = _tar_resource(
        tmp_path / "src" / "data-1.0.tar.gz",
        "data-1.0",
        {"tool": "tool\n"},
    )
    formula = _formula(
        "ruby-resource-no-sha",
        "1.0",
        r'''
  declared = resource("data")
  (prefix/"staged").write declared.staged?.to_s
  begin
    declared.stage { }
  rescue => error
    (prefix/"stage-error").write error.message
  end
''',
    )
    formula.resources["data"] = SourceSpec(url=archive.as_uri(), sha256=None)

    downloader = _downloader(tmp_path)
    keg = _build(tmp_path, formula, downloader=downloader)

    assert (keg / "staged").read_text() == "false"
    error = (keg / "stage-error").read_text()
    assert "has not been staged" in error
    assert "sha256" in error
    assert not (tmp_path / "build" / ".pybrew-resources" / "data").exists()
    # Nothing was fetched without a checksum.
    assert list(downloader.cache_dir.iterdir()) == []


def test_resource_auto_stage_without_downloader_stays_metadata_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    archive = _tar_resource(
        tmp_path / "src" / "data-1.0.tar.gz",
        "data-1.0",
        {"tool": "tool\n"},
    )
    formula = _formula(
        "ruby-resource-no-downloader",
        "1.0",
        r'''
  declared = resource("data")
  (prefix/"staged").write declared.staged?.to_s
  begin
    declared.fetch
  rescue => error
    (prefix/"fetch-error").write error.message
  end
''',
    )
    formula.resources["data"] = SourceSpec(
        url=archive.as_uri(), sha256=_sha256(archive)
    )

    keg = _build(tmp_path, formula)

    assert (keg / "staged").read_text() == "false"
    error = (keg / "fetch-error").read_text()
    assert "has not been staged" in error
    assert "no downloader" in error
    assert not (tmp_path / "build" / ".pybrew-resources" / "data").exists()


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("traversal", "unsafe resource tar member"),
        ("symlink", "resource tar links are not supported"),
    ],
)
def test_resource_auto_stage_rejects_unsafe_tar_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected: str,
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    archive = _unsafe_tar_resource(tmp_path / "src" / "unsafe.tar.gz", kind)
    formula = _formula(
        "ruby-resource-unsafe-tar",
        "1.0",
        '  resource("data").stage { }\n',
    )
    formula.resources["data"] = SourceSpec(
        url=archive.as_uri(), sha256=_sha256(archive)
    )

    with pytest.raises(BuildError, match=expected):
        _build(tmp_path, formula, downloader=_downloader(tmp_path))

    staged_root = tmp_path / "build" / ".pybrew-resources"
    assert not (staged_root / "escape.txt").exists()
    assert not (staged_root / "link").exists()


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("traversal", "unsafe resource zip member"),
        ("symlink", "resource zip links are not supported"),
    ],
)
def test_resource_auto_stage_rejects_unsafe_zip_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected: str,
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    archive = _unsafe_zip_resource(tmp_path / "src" / "unsafe.zip", kind)
    formula = _formula(
        "ruby-resource-unsafe-zip",
        "1.0",
        '  resource("data").stage { }\n',
    )
    formula.resources["data"] = SourceSpec(
        url=archive.as_uri(), sha256=_sha256(archive)
    )

    with pytest.raises(BuildError, match=expected):
        _build(tmp_path, formula, downloader=_downloader(tmp_path))

    staged_root = tmp_path / "build" / ".pybrew-resources"
    assert not (staged_root / "escape.txt").exists()
    assert not (staged_root / "link").exists()


def test_default_declarative_resource_staging_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    archive = _tar_resource(
        tmp_path / "src" / "data-1.0.tar.gz",
        "data-1.0",
        {"tool": "#!/bin/sh\necho declarative\n"},
    )
    formula = Formula(name="declarative-resource", version="1.0")
    formula.install_source = (
        "def install\n"
        '  resource("data").stage do\n'
        '    bin.install "tool"\n'
        "  end\n"
        "end\n"
    )
    formula.resources["data"] = SourceSpec(
        url=archive.as_uri(), sha256=_sha256(archive)
    )

    keg = _build(tmp_path, formula, downloader=_downloader(tmp_path))

    assert (keg / "bin" / "tool").read_text() == "#!/bin/sh\necho declarative\n"
    staged = tmp_path / "build" / ".pybrew-resources" / "data" / "data-1.0"
    assert (staged / "tool").is_file()


def test_patch_data_and_block_are_warned_noops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-patch-noops",
        "1.0",
        r'''
  patch :DATA
  patch do
    url "https://example.com/fix.patch"
    sha256 "abc"
    strip :p1
  end
  patch :p2 do
    url "https://example.com/other.patch"
  end
  (prefix/"patch-ok").write "ok"
''',
    )

    keg = _build(tmp_path, formula, verbose=True)

    assert (keg / "patch-ok").read_text() == "ok"
    captured = capsys.readouterr()
    assert "patch :DATA was not applied" in captured.err
    assert "patch block was not applied" in captured.err
    assert "patch :p2 was not applied" in captured.err


def test_patch_block_unsupported_api_is_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-patch-unsupported",
        "1.0",
        r'''
  patch do
    definitely_not_a_patch_api "x"
  end
''',
    )

    with pytest.raises(BuildError, match="unsupported Homebrew patch API"):
        _build(tmp_path, formula)


def test_unsupported_api_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-unsupported-api",
        "1.0",
        r'''
  definitely_not_a_homebrew_api "x"
''',
    )

    with pytest.raises(BuildError) as error:
        _build(tmp_path, formula)

    message = str(error.value)
    assert "unsupported Homebrew formula API" in message
    assert "definitely_not_a_homebrew_api" in message


# ---------------------------------------------------------------------------
# default path must be unchanged
# ---------------------------------------------------------------------------


def test_expanded_api_stays_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    formula = _formula(
        "ruby-api-default",
        "1.0",
        r'''
  (prefix/"payload").atomic_write("x")
''',
    )

    with pytest.raises(UnsupportedInstall, match="unsupported"):
        _build(tmp_path, formula)


def test_superenv_compiler_and_build_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-superenv",
        "1.0",
        (
            '  ENV.append_to_cflags "-DFOO"\n'
            '  ENV.prepend_to_cflags "-DBAR"\n'
            '  ENV.append_to_ldflags "-lfoo"\n'
            '  ENV.O2\n'
            '  ENV.deparallelize\n'
            '  ENV.cxx11\n'
            '  (prefix/"env").write [\n'
            '    ENV["CFLAGS"], ENV["CXXFLAGS"], ENV["LDFLAGS"],\n'
            '    ENV["MAKEFLAGS"], build.arch, build.target,\n'
            '    build.options.length, Hardware::CPU.sse4_2?,\n'
            '    Hardware::CPU.avx512?, Hardware::CPU.arm?\n'
            '  ].join("\\n")\n'
        ),
    )

    keg = _build(tmp_path, formula)
    lines = (keg / "env").read_text().splitlines()

    assert "-DBAR" in lines[0] and "-DFOO" in lines[0] and "-O2" in lines[0]
    assert "-DBAR" in lines[1] and "-DFOO" in lines[1] and "-O2" in lines[1]
    assert "-lfoo" in lines[2]
    assert lines[3] == "-j1"
    assert lines[4] == lines[5] and lines[4]
    assert lines[6] == "0"
    assert lines[7] in ("true", "false")
    assert lines[8] in ("true", "false")
    assert lines[9] in ("true", "false")


def test_formula_metadata_and_keg_only_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-metadata",
        "1.0",
        (
            '  ref = Formula["ruby-metadata"]\n'
            '  (prefix/"meta").write [\n'
            '    ref.desc, ref.homepage, ref.license.to_s, ref.tap.to_s,\n'
            '    ref.versioned_formula?, ref.keg_only?, ref.spec,\n'
            '    ref.linked_keg.to_s, ref.optlinked?\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.desc = "metadata fixture"
    formula.homepage = "https://example.com/metadata"
    formula.license = "MIT"
    formula.tap = "homebrew/core"
    formula.keg_only = True
    formula.path = tmp_path / "Formula" / "ruby-metadata.rb"

    keg = _build(tmp_path, formula)
    lines = (keg / "meta").read_text().splitlines()
    assert lines[0] == "metadata fixture"
    assert lines[1] == "https://example.com/metadata"
    assert lines[2] == "MIT"
    assert lines[3] == "homebrew/core"
    assert lines[4] == "false"
    assert lines[5] == "true"
    assert lines[6] == "stable"
    assert lines[7] == ""
    assert lines[8] == "false"


def test_formula_spec_bottle_and_status_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    monkeypatch.setenv("PYBREW_BOTTLE_TAG", "sonoma")
    formula = _formula(
        "ruby-spec",
        "1.2.3",
        (
            '  ref = Formula["ruby-spec"]\n'
            '  (prefix/"spec").write [\n'
            '    ref.stable.url, ref.stable.sha256, ref.stable.version.to_s,\n'
            '    ref.head.url, ref.head.version.to_s,\n'
            '    ref.bottle.tag, ref.bottle.sha256, ref.bottle.files.length,\n'
            '    ref.deprecated?, ref.disabled?,\n'
            '    ref.deprecation_reason.to_s, ref.disable_reason.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.stable = SourceSpec(url="https://example.com/stable.tar.gz", sha256="a" * 64)
    formula.head = SourceSpec(url="https://example.com/head.tar.gz")
    formula.bottle.root_url = "https://ghcr.io/v2/homebrew/core"
    formula.bottle.files["sonoma"] = BottleFile(tag="sonoma", sha256="b" * 64)
    formula.bottle.files["arm64_sonoma"] = BottleFile(tag="arm64_sonoma", sha256="c" * 64)
    formula.deprecated = True
    formula.disabled = True
    formula.deprecation_reason = "use other"
    formula.disable_reason = "broken"

    keg = _build(tmp_path, formula)
    lines = (keg / "spec").read_text().splitlines()
    assert lines[0] == "https://example.com/stable.tar.gz"
    assert lines[1] == "a" * 64
    assert lines[2] == "1.2.3"
    assert lines[3] == "https://example.com/head.tar.gz"
    assert lines[4] == "HEAD"
    assert lines[5] == "sonoma"
    assert lines[6] == "b" * 64
    assert lines[7] == "2"
    assert lines[8] == "true"
    assert lines[9] == "true"
    assert lines[10] == "use other"
    assert lines[11] == "broken"


def test_only_referenced_resources_are_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    archive = _tar_resource(
        tmp_path / "src" / "used-1.0.tar.gz",
        "used-1.0",
        {"tool": "#!/bin/sh\necho used\n"},
    )
    formula = _formula(
        "ruby-lazy-resource",
        "1.0",
        '''  resource("used").stage { bin.install "tool" }\n''',
    )
    formula.resources["used"] = SourceSpec(
        url=archive.as_uri(), sha256=_sha256(archive)
    )
    # This URL must never be fetched: it does not exist and would fail loudly.
    formula.resources["unused"] = SourceSpec(
        url=(tmp_path / "does-not-exist.tar.gz").as_uri(),
        sha256="0" * 64,
    )

    keg = _build(tmp_path, formula, downloader=_downloader(tmp_path))
    assert (keg / "bin" / "tool").read_text() == "#!/bin/sh\necho used\n"
    assert not (tmp_path / "build" / ".pybrew-resources" / "unused").exists()


def test_formula_dependency_categories_and_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-deps",
        "1.0",
        (
            '  ref = Formula["ruby-deps"]\n'
            '  (prefix/"deps").write [\n'
            '    ref.build_dependencies.map(&:name).join(","),\n'
            '    ref.test_dependencies.map(&:name).join(","),\n'
            '    ref.recommended_dependencies.map(&:name).join(","),\n'
            '    ref.optional_dependencies.map(&:name).join(","),\n'
            '    ref.head_dependencies.map(&:name).join(","),\n'
            '    ref.uses_from_macos.map(&:name).join(","),\n'
            '    ref.conflicts.join(","),\n'
            '    ref.link_overwrite.join(","),\n'
            '    ref.requirements.map(&:name).join(",")\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.dependencies = [Dependency("runtime")]
    formula.build_dependencies = [Dependency("build", frozenset({"build"}))]
    formula.test_dependencies = [Dependency("test", frozenset({"test"}))]
    formula.recommended_dependencies = [Dependency("recommended", frozenset({"recommended"}))]
    formula.optional_dependencies = [Dependency("optional", frozenset({"optional"}))]
    formula.head_dependencies = [Dependency("head")]
    formula.uses_from_macos = [Dependency("macos")]
    formula.conflicts_with = ["old-ruby-deps"]
    formula.link_overwrite = ["bin/ruby-deps"]
    formula.requirements = [Requirement("macos")]

    keg = _build(tmp_path, formula)
    lines = (keg / "deps").read_text().splitlines()
    assert lines == [
        "build",
        "test",
        "recommended",
        "optional",
        "head",
        "macos",
        "old-ruby-deps",
        "bin/ruby-deps",
        "macos",
    ]


def test_development_tools_clt_and_compiler_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-tools",
        "1.0",
        (
            '  (prefix/"tools").write [\n'
            '    DevelopmentTools.default_compiler.to_s,\n'
            '    DevelopmentTools.installed?.to_s,\n'
            '    DevelopmentTools.needs_clt?.to_s,\n'
            '    MacOS::CLT.installed?.to_s,\n'
            '    ENV.compiler.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "tools").read_text().splitlines()
    assert lines[0] == "clang"
    assert lines[1] in ("true", "false")
    assert lines[2] in ("true", "false")
    assert lines[3] in ("true", "false")
    assert lines[4] in ("clang", "gcc")


def test_env_cxx_optimization_and_removal_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-env-flags",
        "1.0",
        (
            '  ENV["CFLAGS"] = "-Wall -DFOO"\n'
            '  ENV["CXXFLAGS"] = "-Wall -DFOO"\n'
            '  ENV.remove_from_cflags("-DFOO")\n'
            '  ENV.cxx17\n'
            '  ENV.Oz\n'
            '  ENV.enable_warnings\n'
            '  ENV["LDFLAGS"] = "-lfoo -lbar"\n'
            '  ENV.remove_from_ldflags("-lbar")\n'
            '  (prefix/"flags").write [ENV["CFLAGS"], ENV["CXXFLAGS"], ENV["LDFLAGS"]].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "flags").read_text().splitlines()
    assert "-DFOO" not in lines[0]
    assert "-Oz" in lines[0] and "-Wall" in lines[0]
    assert "-std=c++17" in lines[1] and "-DFOO" not in lines[1]
    assert "-lfoo" in lines[2] and "-lbar" not in lines[2]


def test_utils_submodules_and_superenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-utils-modules",
        "1.0",
        (
            '  (prefix/"utils").write [\n'
            '    Utils::Git.available?.to_s,\n'
            '    Utils::Curl.respond_to?(:curl).to_s,\n'
            '    Utils::Gzip.respond_to?(:gzip).to_s,\n'
            '    Utils::Tar.respond_to?(:tar).to_s,\n'
            '    Utils::Zip.respond_to?(:zip).to_s,\n'
            '    Utils::Cp.respond_to?(:cp).to_s,\n'
            '    Utils::Popen.respond_to?(:popen_read).to_s,\n'
            '    ENV.superenv?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "utils").read_text().splitlines()
    assert lines[0] in ("true", "false")
    assert lines[1:] == ["true"] * 7


def test_formula_patches_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-patches",
        "1.0",
        (
            '  ref = Formula["ruby-patches"]\n'
            '  (prefix/"patches").write ref.patches.map { |patch|\n'
            '    [patch.url.to_s, patch.file.to_s, patch.sha256.to_s, patch.strip, patch.data?].join("|")\n'
            '  }.join("\\n")\n'
        ),
    )
    formula.patches = [
        PatchSpec(url="https://example.com/a.patch", sha256="a" * 64, strip=0),
        PatchSpec(file="Patches/b.patch"),
        PatchSpec(data="diff --git a/x b/x\n"),
    ]

    build_root = tmp_path / "patch-build"
    build_root.mkdir()
    keg = tmp_path / "patch-keg"
    keg.mkdir()
    execute_install_body(formula, _config(tmp_path), build_root, keg)
    lines = (keg / "patches").read_text().splitlines()
    assert lines[0] == f"https://example.com/a.patch||{'a' * 64}|0|false"
    assert lines[1] == "|Patches/b.patch||1|false"
    assert lines[2] == "|||1|true"


def test_formula_oldname_and_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-aliases",
        "1.0",
        (
            '  ref = Formula["ruby-aliases"]\n'
            '  (prefix/"aliases").write [ref.oldname.to_s, ref.aliases.join(",")].join("\\n")\n'
        ),
    )
    formula.oldname = "old-ruby-aliases"
    formula.aliases = ["short-name", "other-name"]

    keg = _build(tmp_path, formula)
    lines = (keg / "aliases").read_text().splitlines()
    assert lines[0] == "old-ruby-aliases"
    assert lines[1] == "short-name,other-name"


def test_formula_option_metadata_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-options",
        "1.0",
        (
            '  ref = Formula["ruby-options"]\n'
            '  (prefix/"options").write [\n'
            '    ref.options.join(","),\n'
            '    ref.deprecated_options.to_a.map { |pair| pair.join("=>") }.join(","),\n'
            '    ref.preserve_rpath?.to_s,\n'
            '    ref.skip_clean.join(",")\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.options = ["with-extra"]
    formula.deprecated_options = {"with-old": "with-extra"}
    formula.preserve_rpath = True
    formula.skip_clean = ["bin"]

    keg = _build(tmp_path, formula)
    lines = (keg / "options").read_text().splitlines()
    assert lines[0] == "with-extra"
    assert lines[1] == "with-old=>with-extra"
    assert lines[2] == "true"
    assert lines[3] == "bin"


def test_formula_stable_head_and_option_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-state",
        "1.0",
        (
            '  ref = Formula["ruby-state"]\n'
            '  (prefix/"state").write [\n'
            '    ref.stable?.to_s, ref.head?.to_s, ref.head_only?.to_s,\n'
            '    ref.bottle?.to_s, ref.option?("with-extra").to_s,\n'
            '    ref.specified_name, ref.full_specified_name\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.stable = SourceSpec(url="https://example.com/stable.tar.gz")
    formula.head = SourceSpec(url="https://example.com/head.tar.gz")
    formula.bottle.files["sonoma"] = BottleFile(tag="sonoma", sha256="a" * 64)
    formula.options = ["with-extra"]

    keg = _build(tmp_path, formula)
    lines = (keg / "state").read_text().splitlines()
    assert lines[:6] == ["true", "true", "false", "true", "true", "ruby-state"]
    assert lines[6] == "ruby-state"


def test_formula_bottle_tags_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-bottle-tags",
        "1.0",
        (
            '  ref = Formula["ruby-bottle-tags"]\n'
            '  (prefix/"bottles").write [\n'
            '    ref.bottle.bottle_tags.join(","),\n'
            '    ref.bottle.bottle_for_tag("sonoma").sha256,\n'
            '    ref.bottle.bottle_url("sonoma")\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.bottle.root_url = "https://ghcr.io/v2/homebrew/core"
    formula.bottle.files["sonoma"] = BottleFile(tag="sonoma", sha256="a" * 64)

    keg = _build(tmp_path, formula)
    lines = (keg / "bottles").read_text().splitlines()
    assert lines[0] == "sonoma"
    assert lines[1] == "a" * 64
    assert lines[2].endswith("/ruby-bottle-tags/blobs/sha256:" + "a" * 64)


def test_formula_versioned_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-versioned@1.0",
        "1.0",
        (
            '  ref = Formula["ruby-versioned@1.0"]\n'
            '  (prefix/"versioned").write [\n'
            '    ref.versioned_formula?.to_s,\n'
            '    ref.unversioned_formula_name,\n'
            '    ref.versioned_formulae_names.join(",")\n'
            '  ].join("\\n")\n'
        ),
    )
    config = _config(tmp_path)
    rack = config.cellar / "ruby-versioned@2.0"
    rack.mkdir(parents=True)
    keg = _build(tmp_path, formula)
    lines = (keg / "versioned").read_text().splitlines()
    assert lines[0] == "true"
    assert lines[1] == "ruby-versioned"
    assert "ruby-versioned@2.0" in lines[2]


def test_formula_block_presence_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-blocks",
        "1.0",
        (
            '  ref = Formula["ruby-blocks"]\n'
            '  (prefix/"blocks").write [\n'
            '    ref.service?.to_s, ref.caveats?.to_s, ref.test?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.service = {"source": "service do\n  run \"x\"\nend\n"}
    formula.caveats = "This is a caveat."
    formula.test_source = "test do\n  assert true\nend\n"

    keg = _build(tmp_path, formula)
    assert (keg / "blocks").read_text().splitlines() == ["true", "true", "true"]


def test_formula_path_and_cleanup_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-paths",
        "1.0",
        (
            '  ref = Formula["ruby-paths"]\n'
            '  (prefix/"paths").write [\n'
            '    ref.doc.to_s, ref.man1.to_s, ref.etc.to_s, ref.pkgetc.to_s,\n'
            '    ref.var.to_s, ref.opt_share.to_s, ref.bottle_prefix.to_s,\n'
            '    ref.pour_bottle?.to_s, ref.post_install_defined?.to_s,\n'
            '    ref.oldnames.join(","), ref.possible_names.join(","),\n'
            '    ref.skip_clean?("bin/x").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.oldname = "old-ruby-paths"
    formula.skip_clean = ["bin"]
    config = _config(tmp_path)
    keg = _build(tmp_path, formula)
    lines = (keg / "paths").read_text().splitlines()
    assert lines[0] == str(keg / "share" / "doc" / "ruby-paths")
    assert lines[1] == str(keg / "share" / "man" / "man1")
    assert lines[2] == str(config.prefix / "etc")
    assert lines[3] == str(config.prefix / "etc" / "ruby-paths")
    assert lines[4] == str(config.prefix / "var")
    assert lines[5] == str(config.opt_path("ruby-paths") / "share")
    assert lines[6] == str(keg / ".bottle")
    assert lines[7:11] == ["true", "false", "old-ruby-paths", "ruby-paths,old-ruby-paths"]
    assert lines[11] == "true"


def test_formula_std_build_args_and_python3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-std-args",
        "1.0",
        (
                        '  (prefix/"stdargs").write [\n'
            '    std_npm_args.join(","),\n'
            '    std_npm_args(ignore_scripts: false).join(","),\n'
            '    std_shards_args.join(","),\n'
            '    std_cabal_v2_args.include?("--installdir=#{bin}").to_s,\n'
            '    python3.nil?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "stdargs").read_text().splitlines()
    assert lines[0] == f"--prefix={keg}/libexec,--ignore-scripts"
    assert lines[1] == f"--prefix={keg}/libexec"
    assert lines[2] == "--production,--release,--no-debug"
    assert lines[3] == "true"
    assert lines[4] in ("true", "false")


def test_std_build_args_keywords(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-std-keywords",
        "1.0",
        (
            '  (prefix/"keywords").write [\n'
            '    std_configure_args(libdir: "lib64").join(","),\n'
            '    std_cmake_args(install_libdir: "lib64", find_framework: "NEVER").join(","),\n'
            '    std_go_args(output: bin/"tool", ldflags: "-s").join(","),\n'
            '    std_pip_args(build_isolation: true).join(","),\n'
            '    std_cargo_args(features: ["a", "b"]).join(",")\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "keywords").read_text().splitlines()
    assert f"--libdir={keg}/lib64" in lines[0]
    assert "-DCMAKE_INSTALL_LIBDIR=lib64" in lines[1]
    assert "-DCMAKE_FIND_FRAMEWORK=NEVER" in lines[1]
    assert f"-o={keg}/bin/tool" in lines[2]
    assert "-ldflags=-s" in lines[2]
    assert "--no-build-isolation" not in lines[3]
    assert "--features=a,b" in lines[4]


def test_std_zig_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-zig",
        "1.0",
        (
            '  (prefix/"zig").write [\n'
            '    std_zig_args(release_mode: :small).join(","),\n'
            '    (begin\n'
            '      std_zig_args(release_mode: :bad)\n'
            '      "no-error"\n'
            '    rescue ArgumentError => error\n'
            '      error.message\n'
            '    end)\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "zig").read_text().splitlines()
    assert "--release=small" in lines[0]
    assert "-Doptimize=ReleaseSmall" in lines[0]
    assert "Invalid Zig release mode" in lines[1]


def test_formula_head_and_installed_prefix_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-headpaths",
        "1.0",
        (
            '  ref = Formula["ruby-headpaths"]\n'
            '  (prefix/"headpaths").write [\n'
            '    ref.latest_head_version.to_s,\n'
            '    ref.latest_head_prefix.to_s,\n'
            '    ref.installed_prefixes.map { |path| path.basename.to_s }.join(",")\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.head = SourceSpec(url="https://example.com/head.tar.gz")
    config = _config(tmp_path)
    (config.cellar / "ruby-headpaths" / "1.0").mkdir(parents=True)
    (config.cellar / "ruby-headpaths" / "HEAD").mkdir(parents=True)
    keg = _build(tmp_path, formula)
    lines = (keg / "headpaths").read_text().splitlines()
    assert lines[0] == "HEAD"
    assert lines[1] == str(config.cellar / "ruby-headpaths" / "HEAD")
    assert lines[2] == "1.0,HEAD"


def test_formula_service_name_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-service-paths",
        "1.0",
        (
            '  ref = Formula["ruby-service-paths"]\n'
            '  (prefix/"servicepaths").write [\n'
            '    ref.plist_name, ref.service_name,\n'
            '    ref.launchd_service_path.to_s,\n'
            '    ref.systemd_service_path.to_s,\n'
            '    ref.systemd_timer_path.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "servicepaths").read_text().splitlines()
    assert lines[0] == "sh.brew.ruby-service-paths"
    assert lines[1] == "sh.brew.ruby-service-paths"
    assert lines[2].endswith("sh.brew.ruby-service-paths.plist")
    assert lines[3].endswith("sh.brew.ruby-service-paths.service")
    assert lines[4].endswith("sh.brew.ruby-service-paths.timer")


def test_formula_ref_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-identity",
        "1.0",
        (
            '  a = Formula["ruby-identity"]\n'
            '  b = Formula["ruby-identity"]\n'
            '  (prefix/"identity").write [\n'
            '    (a == b).to_s, (a.to_s == "ruby-identity").to_s,\n'
            '    (a.hash == b.hash).to_s, a.inspect.include?("ruby-identity").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "identity").read_text().splitlines() == ["true", "true", "true", "true"]


def test_formula_head_pkg_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-headpkg",
        "1.0",
        (
            '  ref = Formula["ruby-headpkg"]\n'
            '  (prefix/"headpkg").write [\n'
            '    ref.latest_head_pkg_version.to_s,\n'
            '    ref.head_version_outdated?.to_s,\n'
            '    ref.outdated_kegs.length.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.head = SourceSpec(url="https://example.com/head.tar.gz")
    keg = _build(tmp_path, formula)
    assert (keg / "headpkg").read_text().splitlines() == ["HEAD", "false", "0"]


def test_formula_metadata_compatibility_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-metadata",
        "1.0",
        (
            '  ref = Formula["ruby-metadata"]\n'
            '  (prefix/"metadata").write [\n'
            '    ref.full_installed_specified_name,\n'
            '    ref.full_installed_alias_name,\n'
            '    ref.installed_alias_name.nil?.to_s,\n'
            '    ref.migration_needed?.to_s,\n'
            '    (ref.latest_formula == ref).to_s,\n'
            '    ref.old_installed_formulae.length.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "metadata").read_text().splitlines() == [
        "ruby-metadata",
        "ruby-metadata",
        "true",
        "false",
        "true",
        "0",
    ]


def test_formula_service_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-service-ref",
        "1.0",
        (
            '  ref = Formula["ruby-service-ref"]\n'
            '  (prefix/"serviceref").write [\n'
            '    ref.service?.to_s,\n'
            '    ref.service.run.join(","),\n'
            '    ref.service.environment_variables["FOO"].to_s,\n'
            '    ref.service.source.include?("service do").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.service = {
        "source": (
            "service do\n"
            "  run \"ruby-service-ref\", \"--foreground\"\n"
            "  environment_variables FOO: \"bar\"\n"
            "end\n"
        )
    }
    keg = _build(tmp_path, formula)
    lines = (keg / "serviceref").read_text().splitlines()
    assert lines[0] == "true"
    assert lines[1] == "ruby-service-ref,--foreground"
    assert lines[2] == "bar"
    assert lines[3] == "true"


def test_formula_python3_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-python3",
        "1.0",
        (
            '  ref = Formula["ruby-python3"]\n'
            '  value = ref.python3\n'
            '  (prefix/"python3").write [(value.nil? ? "nil" : "path")].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "python3").read_text().strip() in ("nil", "path")


def test_formula_service_ref_extended_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-service-extended",
        "1.0",
        (
            '  service = Formula["ruby-service-extended"].service\n'
            '  (prefix/"serviceextended").write [\n'
            '    service.nice.to_s, service.restart_delay.to_s,\n'
            '    service.stop_timeout.to_s, service.root_dir.to_s,\n'
            '    service.input_path.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.service = {
        "source": (
            "service do\n"
            "  run \"ruby-service-extended\"\n"
            "  nice 5\n"
            "  restart_delay 7\n"
            "  stop_timeout 9\n"
            "  root_dir \"/tmp\"\n"
            "  input_path \"/tmp/in\"\n"
            "end\n"
        )
    }
    keg = _build(tmp_path, formula)
    assert (keg / "serviceextended").read_text().splitlines() == [
        "5",
        "7",
        "9",
        "/tmp",
        "/tmp/in",
    ]


def test_formula_compatibility_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-compat",
        "1.0",
        (
            '  ref = Formula["ruby-compat"]\n'
            '  (prefix/"compat").write [\n'
            '    ref.version_scheme.to_s,\n'
            '    ref.preserve_rpath.to_s,\n'
            '    ref.loaded_from_api?.to_s,\n'
            '    ref.network_access_allowed?.to_s,\n'
            '    ref.autobump?.to_s,\n'
            '    ref.test_defined?.to_s,\n'
            '    ref.missing_dependencies.length.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.version_scheme = 2
    formula.preserve_rpath = True
    formula.deny_network_access = True
    formula.test_source = "test do\nend\n"
    keg = _build(tmp_path, formula)
    assert (keg / "compat").read_text().splitlines() == [
        "2",
        "true",
        "false",
        "false",
        "true",
        "true",
        "0",
    ]


def test_formula_broad_compatibility_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-broad",
        "1.0",
        (
            '  ref = Formula["ruby-broad"]\n'
            '  (prefix/"broad").write [\n'
            '    ref.rpath.to_s, ref.common_sandbox_env.empty?.to_s,\n'
            '    ref.test_fixtures.empty?.to_s, ref.pypi_packages.nil?.to_s,\n'
            '    ref.ruby_source_path.to_s, ref.full_formulae_names.join(","),\n'
            '    ref.specs.empty?.to_s, ref.link_overwrite_reason.nil?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "broad").read_text().splitlines()
    assert lines[0] == str(keg / "lib")
    assert lines[1:3] == ["true", "true"]
    assert lines[3] == "false"
    assert lines[4] == "" or lines[4].endswith("ruby-broad.rb")
    assert lines[5] == "ruby-broad"
    assert lines[6:] == ["true", "true"]


def test_formula_with_logging_yields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-logging",
        "1.0",
        (
            '  ref = Formula["ruby-logging"]\n'
            '  value = ref.with_logging { "yielded" }\n'
            '  (prefix/"logging").write value\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "logging").read_text() == "yielded"


def test_formula_source_accessors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-sources",
        "1.0",
        (
            '  ref = Formula["ruby-sources"]\n'
            '  (prefix/"sources").write [\n'
            '    ref.caveats_source.include?("caveat").to_s,\n'
            '    ref.test_source.include?("assert").to_s,\n'
            '    ref.livecheck_source.include?("regex").to_s,\n'
            '    ref.post_install_source.include?("puts").to_s,\n'
            '    ref.fetch_source.include?("system").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.caveats = "caveat text"
    formula.test_source = "assert true"
    formula.livecheck_source = "regex(/x/)"
    formula.post_install_source = "puts 1"
    formula.fetch_source = "system true"
    keg = _build(tmp_path, formula)
    assert (keg / "sources").read_text().splitlines() == ["true"] * 5


def test_keg_paths_and_optlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-keg",
        "1.0",
        (
            '  keg_path = Pathname.new(ENV["HOMEBREW_CELLAR"])/"ruby-keg/1.0"\n'
            '  (keg_path/"bin").mkpath\n'
            '  keg = Keg.new(keg_path)\n'
            '  linked_before = keg.linked?\n'
            '  linked = keg.link\n'
            '  (prefix/"keg.txt").write [\n'
            '    keg.name, keg.version, keg.rack.basename.to_s,\n'
            '    keg.bin.to_s, keg.optlink.to_s,\n'
            '    linked_before.to_s, keg.linked?.to_s, linked.to_s,\n'
            '    keg.installed?.to_s, (keg == Keg.new(keg_path)).to_s,\n'
            '    keg.formula.name\n'
            '  ].join("\\n")\n'
            '  (prefix/"unlink.txt").write keg.unlink.to_s\n'
        ),
    )
    keg = _build(tmp_path, formula)
    prefix = tmp_path / "prefix"
    cellar_keg = prefix / "Cellar" / "ruby-keg" / "1.0"
    optlink = prefix / "opt" / "ruby-keg"
    assert (keg / "keg.txt").read_text().splitlines() == [
        "ruby-keg",
        "1.0",
        "ruby-keg",
        str(cellar_keg / "bin"),
        str(optlink),
        "false",
        "true",
        "true",
        "true",
        "true",
        "ruby-keg",
    ]
    assert (keg / "unlink.txt").read_text() == "true"
    assert not optlink.exists()


def test_tab_receipt_and_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-tab",
        "1.0",
        (
            '  receipt = {\n'
            '    "installed_on_request" => false,\n'
            '    "poured_from_bottle" => true,\n'
            '    "runtime_dependencies" => [{"full_name" => "foo", "version" => "1.0"}],\n'
            '    "source" => {"path" => "/tmp/foo.rb", "tap" => "homebrew/core"},\n'
            '    "time" => 123,\n'
            '    "HEAD" => true\n'
            '  }\n'
            '  keg_path = Pathname.new(ENV["HOMEBREW_CELLAR"])/"ruby-tab/1.0"\n'
            '  keg_path.mkpath\n'
            '  (keg_path/"INSTALL_RECEIPT.json").write JSON.generate(receipt)\n'
            '  keg = Keg.new(keg_path)\n'
            '  tab = Tab.for_keg(keg)\n'
            '  by_name = Tab.for_name("ruby-tab")\n'
            '  by_formula = Tab.for_formula(Formula["ruby-tab"])\n'
            '  (prefix/"tab.txt").write [\n'
            '    tab.installed_on_request?.to_s, tab.installed_as_dependency?.to_s,\n'
            '    tab.poured_from_bottle?.to_s, tab.runtime_dependencies.length.to_s,\n'
            '    tab.source["tap"].to_s, tab.time.to_s, tab.head?.to_s,\n'
            '    by_name.poured_from_bottle?.to_s, by_formula.installed_on_request?.to_s,\n'
            '    keg.tab.runtime_dependencies.first["full_name"].to_s,\n'
            '    tab.to_hash["time"].to_s, JSON.parse(tab.to_json)["HEAD"].to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "tab.txt").read_text().splitlines() == [
        "false",
        "true",
        "true",
        "1",
        "homebrew/core",
        "123",
        "true",
        "true",
        "false",
        "foo",
        "123",
        "true",
    ]


def test_formulary_factory_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-formulary",
        "1.0",
        (
            '  (prefix/"formulary").write [\n'
            '    Formulary.factory("ruby-formulary").name,\n'
            '    Formulary.resolve("ruby-formulary").name,\n'
            '    Formulary.class_s("foo-bar").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "formulary").read_text().splitlines() == [
        "ruby-formulary",
        "ruby-formulary",
        "FooBar",
    ]
