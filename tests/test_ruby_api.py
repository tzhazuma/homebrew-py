"""Tests for the expanded Homebrew formula Ruby API shim.

The shim is only reachable through the opt-in
``PYBREW_ALLOW_ARBITRARY_RUBY=1`` bridge; the default declarative parser must
keep behaving exactly as before.  Every fixture here is synthetic and local:
no network access and no writes outside ``tmp_path``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import platform
import shutil
import struct
import subprocess
import tarfile
import time
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


def _run_raw_body(tmp_path: Path, body: str, name: str = "raw-body") -> Path:
    """Execute arbitrary install-body Ruby directly, bypassing the DSL parser."""

    formula = _formula(name, "1.0", body)
    config = _config(tmp_path)
    build_root = tmp_path / "build"
    keg = tmp_path / "keg"
    build_root.mkdir(exist_ok=True)
    keg.mkdir(exist_ok=True)
    execute_install_body(formula, config, build_root, keg)
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
        ("symlink", "unsafe resource tar symlink"),
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
        ("symlink", "unsafe resource zip symlink"),
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
            '    ref.rpath.to_s, ref.common_sandbox_env.key?(:HOME).to_s,\n'
            '    ref.test_fixtures.empty?.to_s, ref.pypi_packages.nil?.to_s,\n'
            '    ref.ruby_source_path.to_s, ref.full_formulae_names.join(","),\n'
            '    ref.specs.length.to_s, ref.link_overwrite_reason.nil?.to_s\n'
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
    assert lines[6:] == ["2", "true"]


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


def test_formula_caveats_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    caveats_source = (
        '  lines = []\n'
        '  puts "hello"\n'
        '  lines << "name=#{name}"\n'
        '  lines << "prefix=#{prefix}"\n'
        '  lines << "global=#{HOMEBREW_PREFIX}"\n'
        '  on_macos { lines << "mac" }\n'
        '  on_linux { lines << "linux" }\n'
        '  lines.join("\\n")\n'
    )
    formula = _formula(
        "ruby-caveats",
        "1.0",
        (
            '  ref = Formula["ruby-caveats"]\n'
            '  (prefix/"caveats.txt").write ref.caveats\n'
        ),
    )
    formula.caveats = caveats_source
    keg = _build(tmp_path, formula)
    lines = (keg / "caveats.txt").read_text().splitlines()
    assert lines[0] == "hello"
    assert lines[1] == "name=ruby-caveats"
    assert lines[2] == f"prefix={keg}"
    assert lines[3] == f"global={tmp_path / 'prefix'}"
    expected = "mac" if is_macos() else "linux"
    assert lines[4] == expected


def test_formula_installed_kegs_are_keg_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-kegs",
        "1.0",
        (
            '  ref = Formula["ruby-kegs"]\n'
            '  (prefix/"kegs.txt").write [\n'
            '    ref.installed_kegs.map { |keg| keg.class.name }.join(","),\n'
            '    ref.installed_kegs.map { |keg| keg.version }.join(","),\n'
            '    ref.installed_prefix.to_s,\n'
            '    ref.latest_installed_keg.version.to_s,\n'
            '    ref.any_installed_keg.version.to_s,\n'
            '    ref.installed_prefixes.map { |path| path.basename.to_s }.join(","),\n'
            '    ref.tab.installed_on_request?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    config = _config(tmp_path)
    for version in ("1.0", "2.0"):
        (config.cellar / "ruby-kegs" / version).mkdir(parents=True)
    (config.cellar / "ruby-kegs" / "2.0" / "INSTALL_RECEIPT.json").write_text(
        json.dumps({"installed_on_request": False})
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "kegs.txt").read_text().splitlines()
    assert lines[0] == "Keg,Keg"
    assert lines[1] == "1.0,2.0"
    assert lines[2] == str(config.cellar / "ruby-kegs" / "1.0")
    assert lines[3] == "2.0"
    assert lines[4] == "2.0"
    assert lines[5] == "1.0,2.0"
    assert lines[6] == "false"


def test_system_config_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-systemconfig",
        "1.0",
        (
            '  io = StringIO.new\n'
            '  SystemConfig.dump_verbose_config(io)\n'
            '  (prefix/"config.txt").write [\n'
            '    ["NilClass", "PybrewVersion"].include?(SystemConfig.clang.class.name).to_s,\n'
            '    (!SystemConfig.describe_clang.to_s.empty?).to_s,\n'
            '    SystemConfig.describe_homebrew_ruby.include?("=>").to_s,\n'
            '    (!SystemConfig.kernel.to_s.empty?).to_s,\n'
            '    (!SystemConfig.describe_git.to_s.empty?).to_s,\n'
            '    (!SystemConfig.describe_curl.to_s.empty?).to_s,\n'
            '    io.string.include?("Homebrew Ruby:").to_s,\n'
            '    io.string.include?("Clang:").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "config.txt").read_text().splitlines() == ["true"] * 8


def test_formulary_from_rack_and_class_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-rack",
        "1.0",
        (
            '  rack = Pathname.new(ENV["HOMEBREW_CELLAR"])/"ruby-rack"\n'
            '  keg = Keg.from_rack(rack)\n'
            '  ref = Formulary.from_rack(rack)\n'
            '  (prefix/"rack.txt").write [\n'
            '    Formulary.class_s("foo-bar"), Formulary.class_s("foo@1.2"),\n'
            '    Formulary.class_s("libxml2"), Formulary.class_s("c++"),\n'
            '    keg.name, keg.version, ref.name,\n'
            '    Formulary.keg_only?(rack).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    config = _config(tmp_path)
    for version in ("1.0", "2.0"):
        (config.cellar / "ruby-rack" / version).mkdir(parents=True)
    keg = _build(tmp_path, formula)
    assert (keg / "rack.txt").read_text().splitlines() == [
        "FooBar",
        "FooAT12",
        "Libxml2",
        "Cxx",
        "ruby-rack",
        "2.0",
        "ruby-rack",
        "false",
    ]


def test_formula_livecheck_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-livecheck",
        "1.0",
        (
            '  ref = Formula["ruby-livecheck"]\n'
            '  (prefix/"livecheck.txt").write [\n'
            '    ref.livecheck.url,\n'
            '    ref.livecheck.regex.source,\n'
            '    ref.livecheck.strategy.to_s,\n'
            '    ref.livecheck.skip?.to_s,\n'
            '    ref.livecheck.skip_reason,\n'
            '    ref.livecheck.to_s,\n'
            '    ref.livecheck_source.include?("regex").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.livecheck_source = (
        '  url "https://example.com/downloads/"\n'
        '  regex(/v?(\\d+(?:\\.\\d+)+)/i)\n'
        '  strategy :page_match\n'
        '  skip "not supported"\n'
    )
    keg = _build(tmp_path, formula)
    assert (keg / "livecheck.txt").read_text().splitlines() == [
        "https://example.com/downloads/",
        r"v?(\d+(?:\.\d+)+)",
        "page_match",
        "true",
        "not supported",
        "https://example.com/downloads/",
        "true",
    ]


def test_formula_livecheck_symbol_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-livecheck-symbol",
        "1.0",
        (
            '  ref = Formula["ruby-livecheck-symbol"]\n'
            '  (prefix/"livecheck.txt").write ref.livecheck.url\n'
        ),
    )
    formula.stable = SourceSpec(url="https://example.com/stable.tar.gz")
    formula.livecheck_source = "  url :stable\n  regex(/v(\\d+)/)\n"
    keg = _build(tmp_path, formula)
    assert (keg / "livecheck.txt").read_text() == "https://example.com/stable.tar.gz"


def test_formula_to_hash_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-hash",
        "1.0",
        (
            '  ref = Formula["ruby-hash"]\n'
            '  (prefix/"hash.json").write JSON.generate(ref.to_hash)\n'
            '  (prefix/"variations.json").write JSON.generate(ref.to_hash_with_variations)\n'
        ),
    )
    formula.desc = "Hash fixture"
    formula.homepage = "https://example.com/ruby-hash"
    formula.license = "MIT"
    formula.aliases = ["ruby-hash-alias"]
    formula.oldname = "ruby-hash-old"
    formula.options = ["--with-x"]
    formula.stable = SourceSpec(
        url="https://example.com/ruby-hash-1.0.tar.gz", sha256="b" * 64
    )
    formula.bottle.root_url = "https://example.com/bottles"
    formula.bottle.files["sonoma"] = BottleFile(
        tag="sonoma", sha256="a" * 64, cellar=":any"
    )
    formula.caveats = '  "caveat text"\n'
    config = _config(tmp_path)
    installed = config.cellar / "ruby-hash" / "1.0"
    installed.mkdir(parents=True)
    (installed / "INSTALL_RECEIPT.json").write_text(
        json.dumps({"installed_on_request": True, "time": 123})
    )
    keg = _build(tmp_path, formula)
    data = json.loads((keg / "hash.json").read_text())
    assert data["name"] == "ruby-hash"
    assert data["desc"] == "Hash fixture"
    assert data["homepage"] == "https://example.com/ruby-hash"
    assert data["license"] == "MIT"
    assert data["aliases"] == ["ruby-hash-alias"]
    assert data["oldnames"] == ["ruby-hash-old"]
    assert data["versions"]["stable"] == "1.0"
    assert data["versions"]["bottle"] is True
    assert data["urls"]["stable"]["url"] == "https://example.com/ruby-hash-1.0.tar.gz"
    assert data["bottle"]["stable"]["files"]["sonoma"]["sha256"] == "a" * 64
    assert data["options"] == ["--with-x"]
    assert data["installed"][0]["version"] == "1.0"
    assert data["installed"][0]["time"] == 123
    assert data["caveats"] == "caveat text"
    assert data["keg_only"] is False
    assert data["post_install_defined"] is False
    variations = json.loads((keg / "variations.json").read_text())
    assert variations["variations"] == {}


def test_formula_outdated_kegs_and_defined_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-outdated",
        "1.0",
        (
            '  ref = Formula["ruby-outdated"]\n'
            '  (prefix/"outdated.txt").write [\n'
            '    ref.post_install?.to_s,\n'
            '    ref.fetch?.to_s,\n'
            '    ref.outdated_kegs.map { |keg| keg.version }.join(","),\n'
            '    ref.outdated?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.post_install_source = "puts 1"
    formula.fetch_source = "system true"
    config = _config(tmp_path)
    (config.cellar / "ruby-outdated" / "0.9").mkdir(parents=True)
    (config.cellar / "ruby-outdated" / "1.0").mkdir(parents=True)
    keg = _build(tmp_path, formula)
    assert (keg / "outdated.txt").read_text().splitlines() == [
        "true",
        "true",
        "0.9",
        "true",
    ]


def test_formula_pin_and_unpin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-pin",
        "1.0",
        (
            '  ref = Formula["ruby-pin"]\n'
            '  (prefix/"pin-before.txt").write [\n'
            '    ref.pinnable?.to_s, ref.pinned?.to_s\n'
            '  ].join("\\n")\n'
            '  ref.pin\n'
            '  (prefix/"pin-after.txt").write [\n'
            '    ref.pinned?.to_s, ref.pinned_version.to_s, ref.pin_path.to_s\n'
            '  ].join("\\n")\n'
            '  ref.unpin\n'
            '  (prefix/"pin-unpin.txt").write ref.pinned?.to_s\n'
        ),
    )
    config = _config(tmp_path)
    (config.cellar / "ruby-pin" / "1.0").mkdir(parents=True)
    keg = _build(tmp_path, formula)
    assert (keg / "pin-before.txt").read_text().splitlines() == ["true", "false"]
    pin_path = config.prefix / "var" / "homebrew" / "pinned" / "ruby-pin"
    assert (keg / "pin-after.txt").read_text().splitlines() == [
        "true",
        "1.0",
        str(pin_path),
    ]
    assert (keg / "pin-unpin.txt").read_text() == "false"
    assert not pin_path.exists()


def test_formula_post_install_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-postinstall",
        "1.0",
        (
            '  ref = Formula["ruby-postinstall"]\n'
            '  ref.post_install\n'
            '  (prefix/"postinstall.txt").write [\n'
            '    ref.post_install?.to_s,\n'
            '    (prefix/"post.txt").read\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.post_install_source = '(prefix/"post.txt").write "post install ran"'
    keg = _build(tmp_path, formula)
    assert (keg / "postinstall.txt").read_text().splitlines() == [
        "true",
        "post install ran",
    ]


def test_tab_receipt_options_and_bottle_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-tab-options",
        "1.0",
        (
            '  keg_path = Pathname.new(ENV["HOMEBREW_CELLAR"])/"ruby-tab-options/1.0"\n'
            '  keg_path.mkpath\n'
            '  (keg_path/"INSTALL_RECEIPT.json").write JSON.generate({\n'
            '    "used_options" => ["--with-x"],\n'
            '    "built_as_bottle" => true,\n'
            '    "installed_as_dependency" => true,\n'
            '    "time" => 456\n'
            '  })\n'
            '  tab = Keg.new(keg_path).tab\n'
            '  (prefix/"tab.txt").write [\n'
            '    tab.used_options.join(","),\n'
            '    tab.unused_options.empty?.to_s,\n'
            '    tab.built_as_bottle?.to_s,\n'
            '    tab.installed_as_dependency?.to_s,\n'
            '    tab.installed_on_request?.to_s,\n'
            '    tab.installed_on_request.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "tab.txt").read_text().splitlines() == [
        "--with-x",
        "true",
        "true",
        "true",
        "false",
        "false",
    ]


def test_formula_test_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-test",
        "1.0",
        (
            '  ref = Formula["ruby-test"]\n'
            '  ref.test\n'
            '  (prefix/"test.txt").write "ok"\n'
        ),
    )
    formula.test_source = (
        '  assert_equal "hi", shell_output("printf hi")\n'
        '  assert_match(/h/, "hi")\n'
        '  assert_predicate Pathname.new(prefix), :directory?\n'
        '  assert_output("hello\\n") { puts "hello" }\n'
    )
    keg = _build(tmp_path, formula)
    assert (keg / "test.txt").read_text() == "ok"


def test_formula_fetch_and_buildpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-fetch",
        "1.0",
        (
            '  ref = Formula["ruby-fetch"]\n'
            '  ref.fetch\n'
            '  (prefix/"fetch-check.txt").write [\n'
            '    ref.fetch?.to_s,\n'
            '    ref.buildpath.basename.to_s,\n'
            '    (ref.buildpath/"fetch.txt").read,\n'
            '    (ref.staged_path == prefix).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.fetch_source = '(buildpath/"fetch.txt").write "fetched"'
    keg = _build(tmp_path, formula)
    assert (keg / "fetch-check.txt").read_text().splitlines() == [
        "true",
        "build",
        "fetched",
        "true",
    ]


def test_formula_misc_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-misc",
        "1.0",
        (
            '  ref = Formula["ruby-misc"]\n'
            '  inside = nil\n'
            '  after = nil\n'
            '  ref.mktemp("ruby-misc") do |dir|\n'
            '    inside = dir.directory?.to_s\n'
            '  end\n'
            '  home = prefix/"home"\n'
            '  ref.setup_home(home)\n'
            '  ref.run_test\n'
            '  ref.run_post_install\n'
            '  (prefix/"misc.txt").write [\n'
            '    inside, home.join(".bazelrc").file?.to_s,\n'
            '    ref.post_install_steps.empty?.to_s,\n'
            '    ref.validate_attributes!.to_s,\n'
            '    ref.determine_active_spec(:stable).url.to_s,\n'
            '    ref.update_head_version.nil?.to_s,\n'
            '    ref.install_etc_var.nil?.to_s,\n'
            '    ref.python_major_minor_version.to_s.match?(/\\d+\\.\\d+/).to_s,\n'
            '    ref.lock.to_s, ref.unlock.nil?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.test_source = '(prefix/"test-ran.txt").write "test"'
    formula.post_install_source = '(prefix/"post-ran.txt").write "post"'
    formula.stable = SourceSpec(url="https://example.com/ruby-misc.tar.gz")
    keg = _build(tmp_path, formula)
    assert (keg / "test-ran.txt").read_text() == "test"
    assert (keg / "post-ran.txt").read_text() == "post"
    assert (keg / "misc.txt").read_text().splitlines() == [
        "true",
        "true",
        "true",
        "true",
        "https://example.com/ruby-misc.tar.gz",
        "true",
        "true",
        "true",
        "true",
        "true",
    ]


def test_formula_bottle_accessors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-bottle-access",
        "1.0",
        (
            '  ref = Formula["ruby-bottle-access"]\n'
            '  (prefix/"bottle.txt").write [\n'
            '    ref.bottled?.to_s,\n'
            '    ref.bottle_tags.join(","),\n'
            '    ref.bottle_for_tag("sonoma").sha256,\n'
            '    ref.bottle_url("sonoma").include?("bottles").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.bottle.root_url = "https://example.com/bottles"
    formula.bottle.files["sonoma"] = BottleFile(
        tag="sonoma", sha256="a" * 64, cellar=":any"
    )
    keg = _build(tmp_path, formula)
    assert (keg / "bottle.txt").read_text().splitlines() == [
        "true",
        "sonoma",
        "a" * 64,
        "true",
    ]


def test_keg_links_files_and_unlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-keglink",
        "1.0",
        (
            '  keg_path = Pathname.new(ENV["HOMEBREW_CELLAR"])/"ruby-keglink/1.0"\n'
            '  (keg_path/"bin").mkpath\n'
            '  (keg_path/"bin"/"ruby-keglink").write "#!/bin/sh\\n"\n'
            '  keg = Keg.new(keg_path)\n'
            '  keg.link\n'
            '  target = Pathname.new(ENV["HOMEBREW_PREFIX"])/"bin/ruby-keglink"\n'
            '  (prefix/"link.txt").write [\n'
            '    target.symlink?.to_s,\n'
            '    (target.realpath == (keg_path/"bin"/"ruby-keglink").realpath).to_s\n'
            '  ].join("\\n")\n'
            '  keg.unlink\n'
            '  (prefix/"unlink.txt").write [(target.symlink? || target.exist?).to_s].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "link.txt").read_text().splitlines() == ["true", "true"]
    assert (keg / "unlink.txt").read_text() == "false"


def test_keg_introspection_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-kegapi",
        "1.0",
        (
            '  keg_path = Pathname.new(ENV["HOMEBREW_CELLAR"])/"ruby-kegapi/1.0"\n'
            '  (keg_path/"bin").mkpath\n'
            '  (keg_path/"bin"/"ruby-kegapi").write "#!/bin/sh\\n"\n'
            '  (keg_path/"INSTALL_RECEIPT.json").write JSON.generate({\n'
            '    "version_scheme" => 2, "aliases" => ["ruby-kegapi-alias"]\n'
            '  })\n'
            '  keg = Keg.new(keg_path)\n'
            '  found = Keg.for(keg_path/"bin"/"ruby-kegapi")\n'
            '  (prefix/"kegapi.txt").write [\n'
            '    Keg.all.map(&:name).include?("ruby-kegapi").to_s,\n'
            '    found.name,\n'
            '    keg.version_scheme.to_s,\n'
            '    keg.scheme_and_version[0].to_s,\n'
            '    keg.to_formula.name,\n'
            '    keg.aliases.join(","),\n'
            '    keg.runtime_dependencies.empty?.to_s,\n'
            '    keg.empty_installation?.to_s,\n'
            '    keg.require_relocation?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "kegapi.txt").read_text().splitlines() == [
        "true",
        "ruby-kegapi",
        "2",
        "2",
        "ruby-kegapi",
        "ruby-kegapi-alias",
        "true",
        "false",
        "false",
    ]


def test_formulary_keywords_tab_write_and_keg_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-misc2",
        "1.0",
        (
            '  ref = Formula["ruby-misc2"]\n'
            '  factory = Formulary.factory("ruby-misc2", :stable, from: :api, warn: false)\n'
            '  resolved = Formulary.resolve("ruby-misc2", :stable)\n'
            '  tabfile = prefix/"tab.json"\n'
            '  tab = Tab.new({"tap" => "homebrew/core", "source" => {"tap" => "ignored"}}, tabfile)\n'
            '  tab["installed_on_request"] = true\n'
            '  written = tab.write\n'
            '  keg_path = Pathname.new(ENV["HOMEBREW_CELLAR"])/"ruby-misc2/0.9"\n'
            '  (keg_path/"bin").mkpath\n'
            '  (keg_path/"bin"/"old").write "old"\n'
            '  removed = Keg.new(keg_path).uninstall\n'
            '  (prefix/"misc2.txt").write [\n'
            '    factory.name, resolved.name, tab.tap, written.to_s,\n'
            '    tabfile.file?.to_s, JSON.parse(tabfile.read)["tap"],\n'
            '    removed.to_s, keg_path.exist?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "misc2.txt").read_text().splitlines() == [
        "ruby-misc2",
        "ruby-misc2",
        "homebrew/core",
        "true",
        "true",
        "homebrew/core",
        "true",
        "false",
    ]


def test_tab_class_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-tabfile",
        "1.0",
        (
            '  tabfile = prefix/"receipt.json"\n'
            '  tabfile.write JSON.generate({\n'
            '    "homebrew_version" => "5.0.0",\n'
            '    "installed_on_request" => true,\n'
            '    "source" => {"tap" => "homebrew/core"}\n'
            '  })\n'
            '  tab = Tab.from_file(tabfile)\n'
            '  other = Tab.from_file_content(tabfile.read)\n'
            '  (prefix/"tabfile.txt").write [\n'
            '    tab.homebrew_version,\n'
            '    tab.parsed_homebrew_version.to_s,\n'
            '    tab.installed_on_request_present?.to_s,\n'
            '    tab.installed_on_request?.to_s,\n'
            '    other.tap,\n'
            '    Formula["ruby-tabfile"].on_system_blocks_exist?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "tabfile.txt").read_text().splitlines() == [
        "5.0.0",
        "5.0.0",
        "true",
        "true",
        "homebrew/core",
        "false",
    ]


def test_macos_xcode_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-xcode",
        "1.0",
        (
            '  (prefix/"xcode.txt").write [\n'
            '    MacOS::Xcode.installed?.to_s,\n'
            '    (MacOS::Xcode.version.nil? || MacOS::Xcode.version.is_a?(PybrewVersion)).to_s,\n'
            '    (MacOS::Xcode.prefix.nil? || MacOS::Xcode.prefix.directory?).to_s,\n'
            '    MacOS::Xcode.default_developer_dir.include?("Xcode.app").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "xcode.txt").read_text().splitlines()
    assert lines[0] in {"true", "false"}
    assert lines[1:] == ["true", "true", "true"]


def test_formula_stage_and_brew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-brew",
        "1.0",
        (
            '  ref = Formula["ruby-brew"]\n'
            '  direct = ref.stage\n'
            '  yielded = nil\n'
            '  ref.brew { |path| yielded = path }\n'
            '  (prefix/"brew.txt").write [\n'
            '    direct.basename.to_s,\n'
            '    yielded.basename.to_s,\n'
            '    (yielded/"fetch.txt").read,\n'
            '    ref.run_post_install_steps.nil?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.fetch_source = '(buildpath/"fetch.txt").write "fetched"'
    keg = _build(tmp_path, formula)
    assert (keg / "brew.txt").read_text().splitlines() == [
        "build",
        "build",
        "fetched",
        "true",
    ]


def test_keg_link_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-linkdir",
        "1.0",
        (
            '  keg_path = Pathname.new(ENV["HOMEBREW_CELLAR"])/"ruby-linkdir/1.0"\n'
            '  (keg_path/"bin").mkpath\n'
            '  (keg_path/"bin"/"ruby-linkdir").write "#!/bin/sh\\n"\n'
            '  (keg_path/"share/doc").mkpath\n'
            '  (keg_path/"share/doc"/"readme").write "readme"\n'
            '  keg = Keg.new(keg_path)\n'
            '  prefix_path = Pathname.new(ENV["HOMEBREW_PREFIX"])\n'
            '  linked = keg.link_dir("bin")\n'
            '  (prefix/"linkdir.txt").write [\n'
            '    linked.to_s,\n'
            '    (prefix_path/"bin/ruby-linkdir").symlink?.to_s,\n'
            '    (prefix_path/"share/doc/readme").exist?.to_s,\n'
            '    (keg.opt_record == keg.optlink).to_s\n'
            '  ].join("\\n")\n'
            '  keg.unlink\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "linkdir.txt").read_text().splitlines() == [
        "true",
        "true",
        "false",
        "true",
    ]


def test_formula_shell_and_source_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-shell",
        "1.0",
        (
            '  ref = Formula["ruby-shell"]\n'
            '  (prefix/"file").write "old"\n'
            '  ref.inreplace(prefix/"file", "old", "new")\n'
            '  ref.mkdir(prefix/"made")\n'
            '  ref.system("true")\n'
            '  (prefix/"shell.txt").write [\n'
            '    ref.url,\n'
            '    ref.sha256,\n'
            '    ref.mirror.first,\n'
            '    (prefix/"file").read,\n'
            '    (prefix/"made").directory?.to_s,\n'
            '    ref.install.nil?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.stable = SourceSpec(
        url="https://example.com/ruby-shell.tar.gz",
        sha256="c" * 64,
        mirrors=["https://mirror.example.com/ruby-shell.tar.gz"],
    )
    keg = _build(tmp_path, formula)
    assert (keg / "shell.txt").read_text().splitlines() == [
        "https://example.com/ruby-shell.tar.gz",
        "c" * 64,
        "https://mirror.example.com/ruby-shell.tar.gz",
        "new",
        "true",
        "true",
    ]


def test_keg_record_and_class_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-kegrec",
        "1.0",
        (
            '  keg_path = Pathname.new(ENV["HOMEBREW_CELLAR"])/"ruby-kegrec/1.0"\n'
            '  (keg_path/"bin").mkpath\n'
            '  (keg_path/"bin"/"ruby-kegrec").write "#!/bin/sh\\n"\n'
            '  keep = Pathname.new(ENV["HOMEBREW_PREFIX"])/"keepme-target"\n'
            '  keep.write "x"\n'
            '  (keg_path/".keepme").write "#{keep}\\n/does/not/exist\\n"\n'
            '  keg = Keg.new(keg_path)\n'
            '  old_record = Pathname.new(ENV["HOMEBREW_PREFIX"])/"opt/ruby-kegrec-old"\n'
            '  old_record.dirname.mkpath\n'
            '  old_record.make_relative_symlink(keg_path)\n'
            '  keg.link\n'
            '  old_names = keg.oldname_opt_records.map { |path| path.basename.to_s }.join(",")\n'
            '  keg.remove_oldname_opt_records\n'
            '  removed_opt = keg.remove_opt_record\n'
            '  (prefix/"kegrec.txt").write [\n'
            '    Keg.keg_link_directories.include?("bin").to_s,\n'
            '    Keg.must_exist_subdirectories.map(&:basename).map(&:to_s).include?("opt").to_s,\n'
            '    keg.keepme_refs.length.to_s,\n'
            '    old_names,\n'
            '    old_record.symlink?.to_s,\n'
            '    removed_opt.to_s,\n'
            '    keg.optlink.symlink?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "kegrec.txt").read_text().splitlines() == [
        "true",
        "true",
        "1",
        "ruby-kegrec-old",
        "false",
        "true",
        "false",
    ]


def test_formulary_placeholder_and_cache_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-formulary2",
        "1.0",
        (
            '  io = StringIO.new("x")\n'
            '  (prefix/"formulary2.txt").write [\n'
            '    Formulary.replace_placeholders("$HOMEBREW_PREFIX/bin"),\n'
            '    Formulary.namespace_key("x").length.to_s,\n'
            '    Formulary.formula_class_defined_from_path?("/x").to_s,\n'
            '    Formulary.formula_class_defined_from_api?("x").to_s,\n'
            '    Formulary.ensure_utf8_encoding(io).equal?(io).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "formulary2.txt").read_text().splitlines() == [
        f"{tmp_path / 'prefix'}/bin",
        "64",
        "false",
        "false",
        "true",
    ]


def test_formula_std_build_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-stdargs",
        "1.0",
        (
            '  ref = Formula["ruby-stdargs"]\n'
            '  (prefix/"stdargs.txt").write [\n'
            '    ref.std_configure_args.include?("--prefix=#{prefix}").to_s,\n'
            '    ref.std_cmake_args.include?("-DCMAKE_INSTALL_PREFIX=#{prefix}").to_s,\n'
            '    ref.std_go_args.first,\n'
            '    ref.std_zig_args.include?("-Dcpu=#{Hardware::CPU.arch}").to_s,\n'
            '    ref.std_cargo_fetch_args.include?("--locked").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "stdargs.txt").read_text().splitlines() == [
        "true",
        "true",
        f"-o={keg}/bin/ruby-stdargs",
        "true",
        "true",
    ]


def test_superenv_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-superenv",
        "1.0",
        (
            '  (prefix/"superenv.txt").write [\n'
            '    Superenv.shims_path.to_s,\n'
            '    (Superenv.bin == Superenv.shims_path).to_s,\n'
            '    Superenv.enabled_for?(ENV).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "superenv.txt").read_text().splitlines() == [
        f"{tmp_path / 'prefix'}/shims",
        "true",
        "false",
    ]


def test_system_config_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-sysconfig-sections",
        "1.0",
        (
            '  io = StringIO.new\n'
            '  SystemConfig.homebrew_config(io)\n'
            '  io2 = StringIO.new\n'
            '  returned = SystemConfig.dump_tap_config(nil, io2)\n'
            '  (prefix/"config-sections.txt").write [\n'
            '    SystemConfig.config_sections.include?(:homebrew_config).to_s,\n'
            '    io.string.include?("HOMEBREW_PREFIX:").to_s,\n'
            '    returned.equal?(io2).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "config-sections.txt").read_text().splitlines() == ["true"] * 3


def test_sandbox_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-sandbox",
        "1.0",
        (
            '  preserved = Sandbox.with_preserved_brew_file { "ok" }\n'
            '  ran = Sandbox.run_or_fork { "ran" }\n'
            '  (prefix/"sandbox.txt").write [\n'
            '    Sandbox.available?.to_s,\n'
            '    Sandbox.full_write_isolation?.to_s,\n'
            '    Sandbox.nested_sandbox?.to_s,\n'
            '    Sandbox.avoid_nested_sandboxing?.to_s,\n'
            '    Sandbox.use_for?("x").to_s,\n'
            '    Sandbox.inherited_sandbox?.to_s,\n'
            '    Sandbox.state.nil?.to_s,\n'
            '    Sandbox.failure_reason.nil?.to_s,\n'
            '    Sandbox.reset_state!.nil?.to_s,\n'
            '    preserved, ran\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "sandbox.txt").read_text().splitlines() == [
        "false",
        "true",
        "false",
        "false",
        "false",
        "false",
        "true",
        "true",
        "true",
        "ok",
        "ran",
    ]


def test_utils_path_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-utilspath",
        "1.0",
        (
            '  cellar = Pathname.new(ENV["HOMEBREW_CELLAR"])/"ruby-utilspath"\n'
            '  (cellar/"1.0").mkpath\n'
            '  (cellar/"2.0").mkpath\n'
            '  empty = prefix/"empty"\n'
            '  empty.mkpath\n'
            '  Utils::Path.rmdir_if_possible(empty)\n'
            '  (prefix/"utilspath.txt").write [\n'
            '    Utils::Path.formula_installed_prefixes(["ruby-utilspath"]).map { |path| path.basename.to_s }.join(","),\n'
            '    (Utils::Path.resolved_path(prefix) == prefix.realpath).to_s,\n'
            '    Utils::Path.formula_opt_bin_path("foo", "svc").to_s,\n'
            '    Utils::Path.formula_opt_bin_env("foo").include?("opt/foo/bin").to_s,\n'
            '    empty.exist?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "utilspath.txt").read_text().splitlines() == [
        "1.0,2.0",
        "true",
        str(tmp_path / "prefix" / "opt" / "foo" / "bin" / "svc"),
        "true",
        "false",
    ]


def test_utils_git_and_curl_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-utilsgit",
        "1.0",
        (
            '  (prefix/"utilsgit.txt").write [\n'
            '    (Utils::Git.version.nil? || Utils::Git.version.is_a?(String)).to_s,\n'
            '    (Utils::Git.path.nil? || Utils::Git.path.is_a?(Pathname)).to_s,\n'
            '    (Utils::Curl.version.nil? || Utils::Curl.version.is_a?(String)).to_s,\n'
            '    (Utils::Curl.curl_path.nil? || Utils::Curl.curl_path.is_a?(Pathname)).to_s,\n'
            '    (Utils::Curl.curl_executable == Utils::Curl.curl_path).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "utilsgit.txt").read_text().splitlines() == ["true"] * 5


def test_formula_sandbox_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-sandboxenv",
        "1.0",
        (
            '  home = prefix/"home"\n'
            '  home.mkpath\n'
            '  ref = Formula["ruby-sandboxenv"]\n'
            '  env = ref.common_sandbox_env(home)\n'
            '  test_env = ref.test_sandbox_env(home)\n'
            '  (prefix/"sandboxenv.txt").write [\n'
            '    env[:HOME], env[:TMPDIR], env[:PIP_CONFIG_FILE],\n'
            '    env[:XDG_CONFIG_HOME], test_env[:GIT_CONFIG_GLOBAL]\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    home = keg / "home"
    assert (keg / "sandboxenv.txt").read_text().splitlines() == [
        str(home),
        os.environ.get("HOMEBREW_TEMP", "/tmp"),
        os.devnull,
        f"{home}/.config",
        f"{home}/.gitconfig",
    ]


def test_formula_service_ref_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-service-names",
        "1.0",
        (
            '  service = Formula["ruby-service-names"].service\n'
            '  (prefix/"servicenames.txt").write [\n'
            '    service.plist_name,\n'
            '    service.canonical_plist_name,\n'
            '    service.legacy_plist_name,\n'
            '    service.plist_names.join(","),\n'
            '    service.service_name,\n'
            '    service.service_names.join(",")\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.service = {"source": 'service do\n  run [opt_bin/"svc"]\nend\n'}
    keg = _build(tmp_path, formula)
    assert (keg / "servicenames.txt").read_text().splitlines() == [
        "sh.brew.ruby-service-names",
        "sh.brew.ruby-service-names",
        "homebrew.mxcl.ruby-service-names",
        "sh.brew.ruby-service-names,homebrew.mxcl.ruby-service-names",
        "sh.brew.ruby-service-names",
        "sh.brew.ruby-service-names,homebrew.ruby-service-names",
    ]


def test_formula_and_formulary_surface_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-surface",
        "1.0",
        (
            '  ref = Formula["ruby-surface"]\n'
            '  (prefix/"surface.txt").write [\n'
            '    ref.respond_to?(:depends_on).to_s,\n'
            '    ref.respond_to?(:option).to_s,\n'
            '    ref.respond_to?(:patch).to_s,\n'
            '    ref.depends_on("foo").nil?.to_s,\n'
            '    Formulary.load_formula_from_path("ruby-surface", "/x").name,\n'
            '    Formulary.load_formula_from_struct!("ruby-surface").name\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "surface.txt").read_text().splitlines() == [
        "true",
        "true",
        "true",
        "true",
        "ruby-surface",
        "ruby-surface",
    ]


def test_formula_service_ref_custom_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-service-custom",
        "1.0",
        (
            '  service = Formula["ruby-service-custom"].service\n'
            '  (prefix/"servicecustom.txt").write [\n'
            '    service.plist_name,\n'
            '    service.plist_names.join(","),\n'
            '    service.service_name,\n'
            '    service.service_names.join(",")\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.service = {
        "source": 'service do\n'
        '  run [opt_bin/"svc"]\n'
        '  name macos: "com.example.custom", linux: "custom-svc"\n'
        'end\n'
    }
    keg = _build(tmp_path, formula)
    assert (keg / "servicecustom.txt").read_text().splitlines() == [
        "com.example.custom",
        "com.example.custom",
        "custom-svc",
        "custom-svc",
    ]


def test_formula_installer_compatibility_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-installer-stub",
        "1.0",
        (
            '  ref = Formula["ruby-installer-stub"]\n'
            '  installer = FormulaInstaller.new(ref, verbose: true, build_bottle: true)\n'
            '  begin\n'
            '    installer.install\n'
            '    error = "no error"\n'
            '  rescue NotImplementedError => e\n'
            '    error = e.class.name\n'
            '  end\n'
            '  (prefix/"installer.txt").write [\n'
            '    installer.verbose?.to_s,\n'
            '    installer.build_bottle?.to_s,\n'
            '    installer.build_from_source?.to_s,\n'
            '    installer.compute_dependencies.empty?.to_s,\n'
            '    installer.formula.name,\n'
            '    error,\n'
            '    FormulaInstaller.attempted.empty?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "installer.txt").read_text().splitlines() == [
        "true",
        "true",
        "false",
        "true",
        "ruby-installer-stub",
        "NotImplementedError",
        "true",
    ]


def test_formula_install_method_guards_recursion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-install-method",
        "1.0",
        (
            '  count = (prefix/"install-count.txt").exist? ? (prefix/"install-count.txt").read.to_i : 0\n'
            '  (prefix/"install-count.txt").write((count + 1).to_s)\n'
            '  if count.zero?\n'
            '    (prefix/"caveats.txt").write Formula["ruby-install-method"].caveats\n'
            '  end\n'
        ),
    )
    formula.caveats = (
        '  Formula["ruby-install-method"].install\n'
        '  (prefix/"caveats-ran.txt").write("ok")\n'
        '  "done"\n'
    )
    keg = _build(tmp_path, formula)
    # The install body is already running, so Formula#install must not recurse.
    assert (keg / "install-count.txt").read_text() == "1"
    assert (keg / "caveats-ran.txt").read_text() == "ok"
    assert (keg / "caveats.txt").read_text() == "done"


def test_formula_no_linkage_and_local_patch_checksums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-upstream-sync",
        "1.0",
        (
            '  ref = Formula["ruby-upstream-sync"]\n'
            '  dep = ref.deps.find { |item| item.name == "dep" }\n'
            '  (prefix/"upstream.txt").write [\n'
            '    dep.no_linkage?.to_s,\n'
            '    ref.to_hash["no_linkage_dependencies"].join(",")\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.dependencies.append(Dependency(name="dep", tags=frozenset({"no_linkage"})))
    keg = _build(tmp_path, formula)
    assert (keg / "upstream.txt").read_text().splitlines() == ["true", "dep"]


def test_formulary_loads_formula_source_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-formulary-source",
        "1.0",
        (
            '  path = prefix/"loaded-formula.rb"\n'
            '  path.write <<~RUBY\n'
            '    class LoadedFixture < Formula\n'
            '      desc "loaded fixture"\n'
            '      homepage "https://example.com/loaded"\n'
            '      url "https://example.com/loaded-2.0.tar.gz"\n'
            '      sha256 "abc"\n'
            '      version "2.0"\n'
            '      depends_on "dep" => :build\n'
            '      test do\n'
            '        "loaded test"\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  klass = Formulary.load_formula_from_path("loaded-fixture", path)\n'
            '  (prefix/"formulary-loaded.txt").write [\n'
            '    klass.loaded_name,\n'
            '    klass.loaded_version,\n'
            '    klass.loaded_desc,\n'
            '    klass.new.dependencies.map(&:name).join(","),\n'
            '    klass.loaded_test_block.nil?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "formulary-loaded.txt").read_text().splitlines() == [
        "loaded-fixture",
        "2.0",
        "loaded fixture",
        "dep",
        "false",
    ]


def test_formula_installer_installs_loaded_formula_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-loaded-installer",
        "1.0",
        (
            '  path = prefix/"loaded-installed.rb"\n'
            '  path.write <<~RUBY\n'
            '    class LoadedInstalled < Formula\n'
            '      version "2.0"\n'
            '      install do\n'
            '        prefix.mkpath\n'
            '        (prefix/"installed.txt").write("ok")\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  klass = Formulary.load_formula_from_path("loaded-installed", path)\n'
            '  FormulaInstaller.new(klass.new).install\n'
            '  (prefix/"installer-loaded.txt").write("done")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "installer-loaded.txt").read_text() == "done"
    installed = (
        tmp_path
        / "prefix"
        / "Cellar"
        / "loaded-installed"
        / "2.0"
        / "installed.txt"
    )
    assert installed.read_text() == "ok"


def test_formulary_loaded_formula_metadata_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-loaded-meta",
        "1.0",
        (
            '  path = prefix/"loaded-meta.rb"\n'
            '  path.write <<~RUBY\n'
            '    class LoadedMeta < Formula\n'
            '      version "3.0"\n'
            '      option "with-x", "description"\n'
            '      bottle do\n'
            '        sha256 "abc"\n'
            '      end\n'
            '      service do\n'
            '        run ["svc"]\n'
            '      end\n'
            '      livecheck do\n'
            '        url "https://example.com/live"\n'
            '      end\n'
            '      resource "res" do\n'
            '        url "https://example.com/res"\n'
            '      end\n'
            '      patch :DATA\n'
            '      head "https://example.com/head"\n'
            '      stable do\n'
            '        url "https://example.com/stable"\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  klass = Formulary.load_formula_from_path("loaded-meta", path)\n'
            '  instance = klass.new\n'
            '  (prefix/"loaded-meta.txt").write [\n'
            '    klass.loaded_bottle.nil?.to_s,\n'
            '    klass.loaded_service.nil?.to_s,\n'
            '    klass.loaded_livecheck.nil?.to_s,\n'
            '    klass.loaded_resources.keys.join(","),\n'
            '    klass.loaded_patches.length.to_s,\n'
            '    instance.options.join(","),\n'
            '    klass.loaded_head[:url],\n'
            '    klass.loaded_stable.nil?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "loaded-meta.txt").read_text().splitlines() == [
        "false",
        "false",
        "false",
        "res",
        "1",
        "with-x",
        "https://example.com/head",
        "false",
    ]


def test_formulary_loaded_formula_inreplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-loaded-inreplace",
        "1.0",
        (
            '  path = prefix/"loaded-inreplace.rb"\n'
            '  path.write <<~RUBY\n'
            '    class LoadedInreplace < Formula\n'
            '      version "2.0"\n'
            '      install do\n'
            '        prefix.mkpath\n'
            '        (prefix/"data.txt").write("hello old")\n'
            '        inreplace prefix/"data.txt", "old", "new"\n'
            '        ohai "loaded install"\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  klass = Formulary.load_formula_from_path("loaded-inreplace", path)\n'
            '  FormulaInstaller.new(klass.new).install\n'
        ),
    )
    _build(tmp_path, formula)
    installed = (
        tmp_path
        / "prefix"
        / "Cellar"
        / "loaded-inreplace"
        / "2.0"
        / "data.txt"
    )
    assert installed.read_text() == "hello new"


def test_development_tools_extra_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-devtools",
        "1.0",
        (
            '  (prefix/"devtools.txt").write [\n'
            '    DevelopmentTools.preferred_compiler.to_s,\n'
            '    (DevelopmentTools.llvm_clang.nil? || DevelopmentTools.llvm_clang.is_a?(Pathname)).to_s,\n'
            '    (DevelopmentTools.compiler_version.nil? || DevelopmentTools.compiler_version.is_a?(String)).to_s,\n'
            '    DevelopmentTools.respond_to?(:clang_build).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "devtools.txt").read_text().splitlines() == [
        "clang",
        "true",
        "true",
        "true",
    ]


# ---------------------------------------------------------------------------
# Formulary path cache / namespacing, loaded-source installer flow, and the
# Superenv compiler-selection surface
# ---------------------------------------------------------------------------


def test_formulary_path_cache_namespacing_and_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-formulary-cache",
        "1.0",
        (
            '  path = prefix/"cache-formula.rb"\n'
            '  path.write <<~RUBY\n'
            '    class CacheFixture < Formula\n'
            '      desc "cache fixture"\n'
            '      version "2.0"\n'
            '    end\n'
            '  RUBY\n'
            '  before_defined = Formulary.formula_class_defined_from_path?(path).to_s\n'
            '  begin\n'
            '    Formulary.formula_class_get_from_path(path)\n'
            '    missing_error = "none"\n'
            '  rescue => error\n'
            '    missing_error = error.class.name\n'
            '  end\n'
            '  klass = Formulary.load_formula_from_path("cache-fixture", path, flags: ["--with-x"])\n'
            '  namespace = klass.loaded_namespace\n'
            '  namespace_name = namespace.name.split("::").last\n'
            '  cached = Formulary.formula_class_get_from_path(path)\n'
            '  values = [\n'
            '    before_defined,\n'
            '    Formulary.formula_class_defined_from_path?(path).to_s,\n'
            '    missing_error,\n'
            '    cached.equal?(klass).to_s,\n'
            '    klass.loaded_class_name,\n'
            '    klass.loaded_name,\n'
            '    klass.new.name,\n'
            '    Formulary.const_defined?(namespace_name).to_s,\n'
            '    namespace.const_defined?(:Formula).to_s,\n'
            '    namespace.const_get(:BUILD_FLAGS).join(","),\n'
            '    namespace.const_defined?(:LOADED_FROM_METADATA).to_s\n'
            '  ]\n'
            '  Formulary.clear_cache\n'
            '  values << Formulary.formula_class_defined_from_path?(path).to_s\n'
            '  begin\n'
            '    Formulary.formula_class_get_from_path(path)\n'
            '    cleared_error = "none"\n'
            '  rescue => error\n'
            '    cleared_error = error.class.name\n'
            '  end\n'
            '  values << cleared_error\n'
            '  values << Formulary.const_defined?(namespace_name).to_s\n'
            '  (prefix/"cache.txt").write values.join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "cache.txt").read_text().splitlines() == [
        "false",
        "true",
        "PybrewFormulaUnavailableError",
        "true",
        "CacheFixture",
        "cache-fixture",
        "cache-fixture",
        "true",
        "true",
        "--with-x",
        "false",
        "false",
        "PybrewFormulaUnavailableError",
        "false",
    ]


def test_formulary_namespace_metadata_and_class_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-formulary-namespace",
        "1.0",
        (
            '  path = prefix/"namespaced.rb"\n'
            '  path.write <<~RUBY\n'
            '    class NamespacedFixture < Formula\n'
            '      version "1.0"\n'
            '    end\n'
            '  RUBY\n'
            '  klass = Formulary.load_formula("namespaced-fixture", path, path.read,\n'
            '                                 "PybrewFixtureNamespace",\n'
            '                                 flags: ["--a", "--b"], from_metadata: true)\n'
            '  namespace = Formulary.const_get(:PybrewFixtureNamespace)\n'
            '  bad_path = prefix/"bad.rb"\n'
            '  bad_path.write "DESCRIPTION = \\"no class here\\"\\n"\n'
            '  begin\n'
            '    Formulary.load_formula_from_path("bad-formula", bad_path)\n'
            '    bad_error = "none"\n'
            '  rescue => error\n'
            '    bad_error = error.class.name\n'
            '  end\n'
            '  begin\n'
            '    Formulary.formula_class_get_from_api("x")\n'
            '    api_error = "none"\n'
            '  rescue => error\n'
            '    api_error = error.class.name\n'
            '  end\n'
            '  (prefix/"namespace.txt").write [\n'
            '    namespace.const_defined?(:Formula).to_s,\n'
            '    namespace.const_get(:BUILD_FLAGS).join(" "),\n'
            '    namespace.const_defined?(:LOADED_FROM_METADATA).to_s,\n'
            '    klass.loaded_namespace.name,\n'
            '    klass.loaded_class_name,\n'
            '    klass.loaded_name,\n'
            '    bad_error,\n'
            '    Formulary.formula_class_defined_from_path?(bad_path).to_s,\n'
            '    api_error\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "namespace.txt").read_text().splitlines() == [
        "true",
        "--a --b",
        "true",
        "Formulary::PybrewFixtureNamespace",
        "NamespacedFixture",
        "namespaced-fixture",
        "PybrewFormulaClassUnavailableError",
        "false",
        "PybrewFormulaUnavailableError",
    ]


def test_formula_installer_loaded_fetch_build_link_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-installer-loaded-api",
        "1.0",
        (
            '  path = prefix/"installer-api.rb"\n'
            '  path.write <<~RUBY\n'
            '    class InstallerApiFixture < Formula\n'
            '      version "2.0"\n'
            '      depends_on "dep"\n'
            '      fetch do\n'
            '        (HOMEBREW_PREFIX/"installer-fetched.txt").write("fetched")\n'
            '      end\n'
            '      install do\n'
            '        prefix.mkpath\n'
            '        (prefix/"installed.txt").write("ok")\n'
            '        (prefix/"lib").mkpath\n'
            '        (prefix/"lib"/"junk.la").write("la")\n'
            '        (prefix/"lib"/"charset.alias").write("alias")\n'
            '      end\n'
            '      post_install do\n'
            '        (prefix/"post-install.txt").write("post")\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  klass = Formulary.load_formula_from_path("installer-api-fixture", path)\n'
            '  installer = FormulaInstaller.new(klass.new, verbose: true)\n'
            '  deps = installer.compute_dependencies\n'
            '  installer.fetch\n'
            '  fetched = (HOMEBREW_PREFIX/"installer-fetched.txt").read\n'
            '  fetched_names = FormulaInstaller.fetched.map(&:name).join(",")\n'
            '  installer.build\n'
            '  loaded_prefix = HOMEBREW_PREFIX/"Cellar"/"installer-api-fixture"/"2.0"\n'
            '  built = (loaded_prefix/"installed.txt").read\n'
            '  la_before = (loaded_prefix/"lib"/"junk.la").exist?.to_s\n'
            '  installer.clean\n'
            '  la_after = (loaded_prefix/"lib"/"junk.la").exist?.to_s\n'
            '  charset_after = (loaded_prefix/"lib"/"charset.alias").exist?.to_s\n'
            '  linked = installer.link.to_s\n'
            '  opt = HOMEBREW_PREFIX/"opt"/"installer-api-fixture"\n'
            '  installer.finish\n'
            '  post = (loaded_prefix/"post-install.txt").read\n'
            '  (prefix/"installer-api.txt").write [\n'
            '    deps.map(&:name).join(","),\n'
            '    fetched,\n'
            '    fetched_names,\n'
            '    built,\n'
            '    la_before,\n'
            '    la_after,\n'
            '    charset_after,\n'
            '    linked,\n'
            '    opt.symlink?.to_s,\n'
            '    post,\n'
            '    FormulaInstaller.attempted.empty?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "installer-api.txt").read_text().splitlines() == [
        "dep",
        "fetched",
        "dep,installer-api-fixture",
        "ok",
        "true",
        "false",
        "false",
        "true",
        "true",
        "post",
        "true",
    ]


def test_formula_installer_install_runs_full_loaded_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-installer-loaded-flow",
        "1.0",
        (
            '  path = prefix/"loaded-flow.rb"\n'
            '  path.write <<~RUBY\n'
            '    class LoadedFlowFixture < Formula\n'
            '      version "3.0"\n'
            '      depends_on "dep"\n'
            '      fetch do\n'
            '        (HOMEBREW_PREFIX/"flow-fetched.txt").write("fetched")\n'
            '      end\n'
            '      install do\n'
            '        prefix.mkpath\n'
            '        (prefix/"installed.txt").write("ok")\n'
            '        (prefix/"lib").mkpath\n'
            '        (prefix/"lib"/"old.la").write("la")\n'
            '      end\n'
            '      post_install do\n'
            '        (prefix/"post.txt").write("post")\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  klass = Formulary.load_formula_from_path("loaded-flow", path)\n'
            '  FormulaInstaller.new(klass.new).install\n'
            '  loaded_prefix = HOMEBREW_PREFIX/"Cellar"/"loaded-flow"/"3.0"\n'
            '  (prefix/"flow.txt").write [\n'
            '    (HOMEBREW_PREFIX/"flow-fetched.txt").read,\n'
            '    (loaded_prefix/"installed.txt").read,\n'
            '    (loaded_prefix/"lib"/"old.la").exist?.to_s,\n'
            '    (loaded_prefix/"post.txt").read,\n'
            '    (HOMEBREW_PREFIX/"opt"/"loaded-flow").symlink?.to_s,\n'
            '    FormulaInstaller.attempted.map(&:name).join(","),\n'
            '    FormulaInstaller.installed.map(&:name).join(","),\n'
            '    FormulaInstaller.skipped.map(&:name).join(","),\n'
            '    FormulaInstaller.fetched.map(&:name).join(",")\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "flow.txt").read_text().splitlines() == [
        "fetched",
        "ok",
        "false",
        "post",
        "true",
        "loaded-flow",
        "loaded-flow",
        "dep",
        "dep,loaded-flow",
    ]


def test_compiler_selection_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-compiler-selection",
        "1.0",
        (
            '  clang_failure = CompilerFailure.create(:clang) { version "99.0" }\n'
            '  gcc_failure = CompilerFailure.create(gcc: "7")\n'
            '  clang_compiler = CompilerSelector::Compiler.new(:clang, :clang, "15.0")\n'
            '  gcc_compiler = CompilerSelector::Compiler.new(:gcc, :gcc, "12.2.0")\n'
            '  failing = Object.new\n'
            '  def failing.compiler_failures\n'
            '    [CompilerFailure.create(:clang) { version "99.0" }]\n'
            '  end\n'
            '  clean = Object.new\n'
            '  def clean.compiler_failures\n'
            '    []\n'
            '  end\n'
            '  hopeless = Object.new\n'
            '  def hopeless.compiler_failures\n'
            '    [\n'
            '      CompilerFailure.create(:clang) { version "99.0" },\n'
            '      CompilerFailure.create(:llvm_clang) { version "99.0" },\n'
            '      CompilerFailure.create(:gcc) { version "99.0" },\n'
            '    ]\n'
            '  end\n'
            '  begin\n'
            '    CompilerSelector.select_for(hopeless)\n'
            '    selection_error = "none"\n'
            '  rescue => error\n'
            '    selection_error = error.class.name\n'
            '  end\n'
            '  failure_path = prefix/"failure-fixture.rb"\n'
            '  failure_path.write <<~RUBY\n'
            '    class FailureFixture < Formula\n'
            '      fails_with :clang do\n'
            '        version "99.0"\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  loaded = Formulary.load_formula_from_path("failure-fixture", failure_path)\n'
            '  ref = Formula["ruby-compiler-selection"]\n'
            '  (prefix/"compilers.txt").write [\n'
            '    CompilerConstants::GNU_GCC_VERSIONS.include?("12").to_s,\n'
            '    CompilerConstants::GNU_GCC_REGEXP.match?("gcc-12").to_s,\n'
            '    CompilerConstants::COMPILER_SYMBOL_MAP["clang"].to_s,\n'
            '    CompilerConstants::COMPILERS.include?("gcc-12").to_s,\n'
            '    gcc_failure.type.to_s,\n'
            '    gcc_failure.version.to_s,\n'
            '    (gcc_failure.build == gcc_failure.version).to_s,\n'
            '    clang_failure.fails_with?(clang_compiler).to_s,\n'
            '    clang_failure.fails_with?(gcc_compiler).to_s,\n'
            '    gcc_failure.fails_with?(gcc_compiler).to_s,\n'
            '    gcc_failure.fails_with?(clang_compiler).to_s,\n'
            '    CompilerSelector.compilers.join(","),\n'
            '    CompilerSelector.select_for(failing).to_s,\n'
            '    CompilerSelector.select_for(clean).to_s,\n'
            '    CompilerSelector.preferred_gcc.to_s.start_with?(\"gcc\").to_s,\n'
            '    gcc_failure.inspect,\n'
            '    ref.compiler_failures.map(&:inspect).join(";"),\n'
            '    CompilerSelector.select_for(ref).to_s,\n'
            '    loaded.new.compiler_failures.map(&:inspect).join(";"),\n'
            '    CompilerSelector.select_for(loaded.new).to_s,\n'
            '    selection_error\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.fails_with = [{"gcc": "7"}]
    keg = _build(tmp_path, formula)
    assert (keg / "compilers.txt").read_text().splitlines() == [
        "true",
        "true",
        "clang",
        "true",
        "gcc",
        "7.999",
        "true",
        "true",
        "false",
        "false",
        "false",
        "clang,llvm_clang,gnu",
        "llvm_clang",
        "clang",
        "true",
        "#<CompilerFailure: gcc 7.999>",
        "#<CompilerFailure: gcc 7.999>",
        "clang",
        "#<CompilerFailure: clang 99.0>",
        "llvm_clang",
        "CompilerSelectionError",
    ]


def test_env_superenv_setup_and_compiler_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-env-superenv",
        "1.0",
        (
            '  ENV.clang\n'
            '  clang_cc = ENV.cc\n'
            '  clang_cxx = ENV.cxx\n'
            '  clang_compiler = ENV.compiler.to_s\n'
            '  ENV.gcc\n'
            '  gcc_cc = ENV["CC"]\n'
            '  gcc_cxx = ENV["CXX"]\n'
            '  ENV.llvm_clang\n'
            '  llvm_cc = ENV["CC"]\n'
            '  ENV.cc = "cc-custom"\n'
            '  ENV.cxx = "cxx-custom"\n'
            '  custom_cc = ENV["CC"]\n'
            '  custom_objc = ENV["OBJC"]\n'
            '  custom_cxx = ENV["CXX"]\n'
            '  ENV.append_to_cccfg "x"\n'
            '  ENV.append_to_cccfg "y"\n'
            '  cccfg = ENV["HOMEBREW_CCCFG"]\n'
            '  removed = ENV.remove_cc_etc\n'
            '  removed_cc = removed["CC"]\n'
            '  after_remove = ENV["CC"].inspect\n'
            '  ENV.setup_build_environment(formula: Formula["ruby-env-superenv"], cc: "clang")\n'
            '  shims = Superenv.shims_path.to_s\n'
            '  (prefix/"superenv.txt").write [\n'
            '    clang_cc, clang_cxx, clang_compiler,\n'
            '    gcc_cc, gcc_cxx, llvm_cc,\n'
            '    custom_cc, custom_objc, custom_cxx, cccfg,\n'
            '    removed_cc.to_s, after_remove,\n'
            '    ENV["HOMEBREW_ENV"], ENV["HOMEBREW_CC"], ENV["CC"], ENV["CXX"],\n'
            '    ENV["MAKEFLAGS"], ENV["HOMEBREW_MAKE_JOBS"],\n'
            '    ENV["HOMEBREW_PREFIX"], ENV["HOMEBREW_CELLAR"], ENV["HOMEBREW_OPT"],\n'
            '    ENV["HOMEBREW_OPTFLAGS"], ENV["HOMEBREW_OPTIMIZATION_LEVEL"],\n'
            '    ENV["HOMEBREW_FORMULA_PREFIX"],\n'
            '    ENV["PATH"].split(File::PATH_SEPARATOR).include?(shims).to_s,\n'
            '    ENV.compiler.to_s, ENV.effective_arch.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "superenv.txt").read_text().splitlines()
    assert lines[0:11] == [
        "clang",
        "clang++",
        "clang",
        "gcc",
        "g++",
        "clang",
        "cc-custom",
        "cc-custom",
        "cxx-custom",
        "xy",
        "cc-custom",
    ]
    assert lines[11] == "nil"
    assert lines[12:16] == ["super", "clang", "clang", "clang++"]
    assert lines[16].startswith("-j") and int(lines[16][2:]) >= 1
    assert int(lines[17]) >= 1
    prefix = tmp_path / "prefix"
    assert lines[18] == str(prefix)
    assert lines[19] == str(prefix / "Cellar")
    assert lines[20] == f"{prefix}/opt"
    assert lines[21] == "-O2"
    assert lines[22] == "Os"
    # Formula["..."] resolves the current formula to its pybrew keg prefix.
    assert lines[23] == str(keg)
    assert lines[24] == "true"
    assert lines[25] == "clang"
    assert lines[26] in ("arm64", "x86_64", "i386", "arm", "dunno")


# ---------------------------------------------------------------------------
# Formulary factory source loading / factory cache and API-struct mapping
# ---------------------------------------------------------------------------


def test_formulary_factory_loads_local_source_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    source = tmp_path / "factory-source.rb"
    source.write_text(
        'class FactorySource < Formula\n'
        '  desc "factory source desc"\n'
        '  homepage "https://example.com/factory"\n'
        '  url "https://example.com/factory-2.0.tar.gz"\n'
        '  sha256 "factory-sha"\n'
        '  version "2.0"\n'
        '  depends_on "dep" => :build\n'
        'end\n'
    )
    formula = _formula(
        "ruby-factory-source",
        "1.0",
        (
            '  path = Formula["ruby-factory-source"].path\n'
            '  before_cache = Formulary.factory_cached?.to_s\n'
            '  factory = Formulary.factory("ruby-factory-source")\n'
            '  defined_after_load = Formulary.formula_class_defined_from_path?(path).to_s\n'
            '  resolved = Formulary.resolve("ruby-factory-source")\n'
            '  Formulary.enable_factory_cache!\n'
            '  cached = Formulary.factory("ruby-factory-source")\n'
            '  cached_again = Formulary.factory("ruby-factory-source")\n'
            '  missing_first = Formulary.factory("missing-factory-formula")\n'
            '  missing_again = Formulary.factory("missing-factory-formula")\n'
            '  missing_api = Formulary.factory("missing-factory-formula", :stable, from: :api)\n'
            '  missing_api_again = Formulary.factory("missing-factory-formula", :stable, from: :api)\n'
            '  Formulary.clear_cache\n'
            '  after_clear = Formulary.factory("missing-factory-formula")\n'
            '  path.write "raise \\"broken factory source\\"\\n"\n'
            '  broken = Formulary.factory("ruby-factory-source")\n'
            '  Formulary.clear_cache\n'
            '  path.write <<~RUBY\n'
            '    class FactorySource < Formula\n'
            '      version "3.0"\n'
            '      Formulary.factory("ruby-factory-source")\n'
            '    end\n'
            '  RUBY\n'
            '  recursive = Formulary.factory("ruby-factory-source")\n'
            '  (prefix/"factory.txt").write [\n'
            '    before_cache,\n'
            '    factory.is_a?(Class).to_s,\n'
            '    factory.name,\n'
            '    factory.loaded_desc,\n'
            '    factory.loaded_homepage,\n'
            '    factory.loaded_version,\n'
            '    factory.new.name,\n'
            '    factory.new.dependencies.map(&:name).join(","),\n'
            '    resolved.is_a?(Class).to_s,\n'
            '    defined_after_load,\n'
            '    Formulary.factory_cached?.to_s,\n'
            '    cached_again.equal?(cached).to_s,\n'
            '    missing_again.equal?(missing_first).to_s,\n'
            '    missing_api_again.equal?(missing_api).to_s,\n'
            '    missing_api.equal?(missing_first).to_s,\n'
            '    after_clear.equal?(missing_first).to_s,\n'
            '    broken.is_a?(Class).to_s,\n'
            '    broken.name,\n'
            '    recursive.is_a?(Class).to_s,\n'
            '    recursive.loaded_version,\n'
            '    Formulary.formula_class_defined_from_path?(path).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.path = source
    keg = _build(tmp_path, formula)
    assert (keg / "factory.txt").read_text().splitlines() == [
        "false",
        "true",
        "ruby-factory-source",
        "factory source desc",
        "https://example.com/factory",
        "2.0",
        "ruby-factory-source",
        "dep",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "false",
        "false",
        "false",
        "ruby-factory-source",
        "true",
        "3.0",
        "true",
    ]


def test_formulary_load_formula_from_struct_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-struct-mapping",
        "1.0",
        (
            '  dep_struct = Struct.new(:name, :tags)\n'
            '  dep = dep_struct.new("dep", ["build"])\n'
            '  struct = Struct.new(\n'
            '    :desc, :homepage, :url, :sha256, :version, :license, :dependencies\n'
            '  ).new(\n'
            '    "struct desc", "https://example.com/struct",\n'
            '    "https://example.com/struct-4.2.tar.gz", "struct-sha",\n'
            '    "4.2", "MIT", [dep, {"dep2" => "test"}]\n'
            '  )\n'
            '  klass = Formulary.load_formula_from_struct!("struct-fixture", struct)\n'
            '  instance = klass.new\n'
            '  partial = Struct.new(:desc, :version).new("partial desc", "1.0")\n'
            '  partial_klass = Formulary.load_formula_from_struct!("partial-fixture", partial)\n'
            '  fallback = Formulary.load_formula_from_struct!("fallback-fixture")\n'
            '  begin\n'
            '    Formulary.load_formula_from_struct!("bad-struct", Object.new)\n'
            '    bad_error = "none"\n'
            '  rescue => error\n'
            '    bad_error = error.class.name + ":" + error.message.include?("formula struct").to_s\n'
            '  end\n'
            '  begin\n'
            '    Formulary.load_formula_from_struct!(\n'
            '      "hash-license", Struct.new(:license).new({"default" => "Apache-2.0"})\n'
            '    )\n'
            '    hash_error = "none"\n'
            '  rescue => error\n'
            '    hash_error = error.class.name + ":" + error.message.include?("malformed").to_s\n'
            '  end\n'
            '  (prefix/"struct.txt").write [\n'
            '    klass.is_a?(Class).to_s,\n'
            '    klass.name,\n'
            '    klass.loaded_name,\n'
            '    klass.loaded_desc,\n'
            '    klass.loaded_homepage,\n'
            '    klass.loaded_url,\n'
            '    klass.loaded_sha256,\n'
            '    klass.loaded_version,\n'
            '    klass.loaded_license,\n'
            '    instance.desc,\n'
            '    instance.homepage,\n'
            '    instance.version,\n'
            '    instance.license,\n'
            '    instance.dependencies.map(&:name).join(","),\n'
            '    instance.dependencies.first.build?.to_s,\n'
            '    instance.dependencies[1].test?.to_s,\n'
            '    partial_klass.loaded_desc,\n'
            '    partial_klass.loaded_homepage.inspect,\n'
            '    partial_klass.new.dependencies.empty?.to_s,\n'
            '    fallback.name,\n'
            '    fallback.is_a?(Class).to_s,\n'
            '    bad_error,\n'
            '    hash_error\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "struct.txt").read_text().splitlines() == [
        "true",
        "struct-fixture",
        "struct-fixture",
        "struct desc",
        "https://example.com/struct",
        "https://example.com/struct-4.2.tar.gz",
        "struct-sha",
        "4.2",
        "MIT",
        "struct desc",
        "https://example.com/struct",
        "4.2",
        "MIT",
        "dep,dep2",
        "true",
        "true",
        "partial desc",
        "nil",
        "true",
        "fallback-fixture",
        "false",
        "PybrewFormulaUnavailableError:true",
        "PybrewFormulaUnavailableError:true",
    ]


def test_formulary_api_struct_cache_lookup_and_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-api-cache",
        "1.0",
        (
            '  struct_class = Struct.new(\n'
            '    :name, :full_name, :desc, :homepage, :url, :sha256, :version, :license, :dependencies\n'
            '  )\n'
            '  struct = struct_class.new(\n'
            '    "api-cache-fixture", "homebrew/core/api-cache-fixture",\n'
            '    "api cache desc", "https://example.com/api",\n'
            '    "https://example.com/api-1.0.tar.gz", "api-sha", "1.0", "MIT", []\n'
            '  )\n'
            '  before = Formulary.formula_class_defined_from_api?("api-cache-fixture").to_s\n'
            '  klass = Formulary.load_formula_from_struct!("api-cache-fixture", struct)\n'
            '  defined_name = Formulary.formula_class_defined_from_api?("api-cache-fixture").to_s\n'
            '  defined_full = Formulary.formula_class_defined_from_api?("homebrew/core/api-cache-fixture").to_s\n'
            '  defined_struct = Formulary.formula_class_defined_from_api?(struct).to_s\n'
            '  got = Formulary.formula_class_get_from_api("api-cache-fixture")\n'
            '  got_full = Formulary.formula_class_get_from_api("homebrew/core/api-cache-fixture")\n'
            '  got_struct = Formulary.formula_class_get_from_api(struct)\n'
            '  fallback = Formulary.load_formula_from_struct!("api-fallback-fixture")\n'
            '  partial = Struct.new(:desc).new("partial desc")\n'
            '  begin\n'
            '    Formulary.load_formula_from_struct!("api-partial-fixture", partial)\n'
            '    partial_error = "none"\n'
            '  rescue => error\n'
            '    partial_error = error.class.name + ":" + error.message.include?("malformed").to_s\n'
            '  end\n'
            '  fallback_defined = Formulary.formula_class_defined_from_api?("api-fallback-fixture").to_s\n'
            '  partial_defined = Formulary.formula_class_defined_from_api?("api-partial-fixture").to_s\n'
            '  begin\n'
            '    Formulary.formula_class_get_from_api("api-unknown")\n'
            '    unknown_error = "none"\n'
            '  rescue => error\n'
            '    unknown_error = error.class.name + "|" + error.message.include?("API").to_s\n'
            '  end\n'
            '  Formulary.clear_cache\n'
            '  after_clear_name = Formulary.formula_class_defined_from_api?("api-cache-fixture").to_s\n'
            '  after_clear_full = Formulary.formula_class_defined_from_api?("homebrew/core/api-cache-fixture").to_s\n'
            '  after_clear_struct = Formulary.formula_class_defined_from_api?(struct).to_s\n'
            '  begin\n'
            '    Formulary.formula_class_get_from_api("api-cache-fixture")\n'
            '    cleared_error = "none"\n'
            '  rescue => error\n'
            '    cleared_error = error.class.name + "|" + error.message.include?("API").to_s\n'
            '  end\n'
            '  (prefix/"api-cache.txt").write [\n'
            '    before,\n'
            '    defined_name, defined_full, defined_struct,\n'
            '    got.equal?(klass).to_s, got_full.equal?(klass).to_s, got_struct.equal?(klass).to_s,\n'
            '    klass.loaded_name, klass.loaded_desc, klass.loaded_version, klass.loaded_license,\n'
            '    got.new.desc,\n'
            '    fallback.is_a?(Class).to_s,\n'
            '    fallback_defined,\n'
            '    partial_error,\n'
            '    partial_defined,\n'
            '    unknown_error,\n'
            '    after_clear_name, after_clear_full, after_clear_struct, cleared_error\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "api-cache.txt").read_text().splitlines() == [
        "false",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "api-cache-fixture",
        "api cache desc",
        "1.0",
        "MIT",
        "api cache desc",
        "false",
        "false",
        "PybrewFormulaUnavailableError:true",
        "false",
        "PybrewFormulaUnavailableError|true",
        "false",
        "false",
        "false",
        "PybrewFormulaUnavailableError|true",
    ]


def test_formulary_api_struct_cache_reload_replaces_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-api-reload",
        "1.0",
        (
            '  struct_class = Struct.new(:name, :full_name, :desc, :version)\n'
            '  first = struct_class.new(\n'
            '    "api-reload-fixture", "homebrew/core/api-reload-fixture", "first", "1.0"\n'
            '  )\n'
            '  second = struct_class.new(\n'
            '    "api-reload-fixture", "homebrew/core/api-reload-fixture", "second", "2.0"\n'
            '  )\n'
            '  first_klass = Formulary.load_formula_from_struct!("api-reload-fixture", first)\n'
            '  second_klass = Formulary.load_formula_from_struct!("api-reload-fixture", second)\n'
            '  got = Formulary.formula_class_get_from_api("api-reload-fixture")\n'
            '  got_full = Formulary.formula_class_get_from_api("homebrew/core/api-reload-fixture")\n'
            '  (prefix/"api-reload.txt").write [\n'
            '    got.equal?(second_klass).to_s,\n'
            '    got.equal?(first_klass).to_s,\n'
            '    got_full.equal?(second_klass).to_s,\n'
            '    got.loaded_desc,\n'
            '    got.loaded_version,\n'
            '    Formulary.formula_class_defined_from_api?(first).to_s,\n'
            '    Formulary.formula_class_defined_from_api?(second).to_s,\n'
            '    Formulary.formula_class_defined_from_api?("api-reload-fixture").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "api-reload.txt").read_text().splitlines() == [
        "true",
        "false",
        "true",
        "second",
        "2.0",
        "false",
        "true",
        "true",
    ]


# ---------------------------------------------------------------------------
# FormulaInstaller recursive loaded dependencies, bottle/pour compatibility,
# and Keg linked-keg record semantics
# ---------------------------------------------------------------------------


def test_formula_installer_recursive_loaded_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    tap = tmp_path / "dep-tap"
    tap.mkdir()
    root_source = tap / "ruby-dep-root.rb"
    root_source.write_text(
        'class RubyDepRoot < Formula\n'
        '  version "1.0"\n'
        '  depends_on "ruby-dep-child"\n'
        '  depends_on "ruby-no-install"\n'
        '  install do\n'
        '    prefix.mkpath\n'
        '    (prefix/"root-installed.txt").write("root")\n'
        '  end\n'
        'end\n'
    )
    (tap / "ruby-dep-child.rb").write_text(
        'class RubyDepChild < Formula\n'
        '  version "2.0"\n'
        '  depends_on "ruby-dep-missing"\n'
        '  install do\n'
        '    prefix.mkpath\n'
        '    (prefix/"child-installed.txt").write("child")\n'
        '  end\n'
        'end\n'
    )
    (tap / "ruby-no-install.rb").write_text(
        'class RubyNoInstall < Formula\n'
        '  version "3.0"\n'
        'end\n'
    )
    formula = _formula(
        "ruby-dep-root",
        "1.0",
        (
            '  root_path = Formula["ruby-dep-root"].path\n'
            '  klass = Formulary.load_formula_from_path("ruby-dep-root", root_path)\n'
            '  FormulaInstaller.new(klass.new).install\n'
            '  root_prefix = HOMEBREW_PREFIX/"Cellar"/"ruby-dep-root"/"1.0"\n'
            '  child_prefix = HOMEBREW_PREFIX/"Cellar"/"ruby-dep-child"/"2.0"\n'
            '  child_path = root_path.dirname/"ruby-dep-child.rb"\n'
            '  no_install_path = root_path.dirname/"ruby-no-install.rb"\n'
            '  (prefix/"recursive-deps.txt").write [\n'
            '    (root_prefix/"root-installed.txt").read,\n'
            '    (child_prefix/"child-installed.txt").read,\n'
            '    FormulaInstaller.attempted.map(&:name).join(","),\n'
            '    FormulaInstaller.installed.map(&:name).join(","),\n'
            '    FormulaInstaller.skipped.map(&:name).join(","),\n'
            '    Formulary.formula_class_defined_from_path?(child_path).to_s,\n'
            '    Formulary.formula_class_defined_from_path?(no_install_path).to_s,\n'
            '    FormulaInstaller.installing.empty?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.path = root_source
    keg = _build(tmp_path, formula)
    assert (keg / "recursive-deps.txt").read_text().splitlines() == [
        "root",
        "child",
        "ruby-dep-root,ruby-dep-child",
        "ruby-dep-child,ruby-dep-root",
        "ruby-dep-missing,ruby-no-install",
        "true",
        "true",
        "true",
    ]


def test_formula_installer_loaded_dependency_cycle_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    tap = tmp_path / "cycle-tap"
    tap.mkdir()
    cycle_source = tap / "ruby-cycle.rb"
    cycle_source.write_text(
        'class RubyCycle < Formula\n'
        '  version "1.0"\n'
        '  depends_on "ruby-cycle"\n'
        '  install do\n'
        '    prefix.mkpath\n'
        '    count_file = prefix/"cycle-count.txt"\n'
        '    count = count_file.exist? ? count_file.read.to_i : 0\n'
        '    count_file.write((count + 1).to_s)\n'
        '  end\n'
        'end\n'
    )
    formula = _formula(
        "ruby-cycle",
        "1.0",
        (
            '  klass = Formulary.load_formula_from_path("ruby-cycle", Formula["ruby-cycle"].path)\n'
            '  FormulaInstaller.new(klass.new).install\n'
            '  cycle_prefix = HOMEBREW_PREFIX/"Cellar"/"ruby-cycle"/"1.0"\n'
            '  (prefix/"cycle.txt").write [\n'
            '    (cycle_prefix/"cycle-count.txt").read,\n'
            '    FormulaInstaller.attempted.map(&:name).join(","),\n'
            '    FormulaInstaller.installed.empty?.to_s,\n'
            '    FormulaInstaller.skipped.map(&:name).join(","),\n'
            '    FormulaInstaller.installing.empty?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.path = cycle_source
    keg = _build(tmp_path, formula)
    assert (keg / "cycle.txt").read_text().splitlines() == [
        "1",
        "ruby-cycle",
        "false",
        "ruby-cycle",
        "true",
    ]


def test_formula_installer_loaded_bottle_pour_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-bottle-fixture",
        "1.0",
        (
            '  path = prefix/"bottle-fixture.rb"\n'
            '  path.write <<~RUBY\n'
            '    class BottleFixture < Formula\n'
            '      version "2.0"\n'
            '      bottle do\n'
            '        sha256 "abc"\n'
            '      end\n'
            '      install do\n'
            '        prefix.mkpath\n'
            '        (prefix/"installed.txt").write("ok")\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  klass = Formulary.load_formula_from_path("bottle-fixture", path)\n'
            '  installer = FormulaInstaller.new(klass.new)\n'
            '  dep = Object.new\n'
            '  values = [\n'
            '    klass.new.bottle?.to_s,\n'
            '    installer.pour_bottle?.to_s,\n'
            '    installer.pour_bottle?(output_warning: true).to_s,\n'
            '    installer.pour.nil?.to_s,\n'
            '    installer.install_bottle_for?(dep, false).to_s,\n'
            '    installer.respond_to?(:pour).to_s\n'
            '  ]\n'
            '  installer.install\n'
            '  (prefix/"bottle.txt").write values.join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "bottle.txt").read_text().splitlines() == [
        "true",
        "false",
        "false",
        "true",
        "false",
        "true",
    ]
    installed = (
        tmp_path / "prefix" / "Cellar" / "bottle-fixture" / "2.0" / "installed.txt"
    )
    assert installed.read_text() == "ok"


def test_keg_linked_keg_record_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-kegrecord2",
        "1.0",
        (
            '  keg_path = Pathname.new(ENV["HOMEBREW_CELLAR"])/"ruby-kegrecord/1.0"\n'
            '  (keg_path/"bin").mkpath\n'
            '  (keg_path/"bin"/"tool").write "#!/bin/sh\\n"\n'
            '  keg = Keg.new(keg_path)\n'
            '  record = keg.linked_keg_record\n'
            '  before = [keg.linked?, keg.optlinked?]\n'
            '  keg.link\n'
            '  after_link = [keg.linked?, keg.optlinked?]\n'
            '  record_target = record.symlink? && File.identical?(record.to_s, keg_path.to_s)\n'
            '  opt_target = File.symlink?(keg.optlink.to_s) &&\n'
            '               File.identical?(keg.optlink.to_s, keg_path.to_s)\n'
            '  removed = keg.remove_linked_keg_record\n'
            '  after_remove = [keg.linked?, keg.optlinked?]\n'
            '  keg.link\n'
            '  relinked = keg.linked?\n'
            '  unlinked = keg.unlink\n'
            '  (prefix/"kegrecord.txt").write [\n'
            '    record.to_s,\n'
            '    before[0].to_s, before[1].to_s,\n'
            '    after_link[0].to_s, after_link[1].to_s,\n'
            '    record_target.to_s, opt_target.to_s,\n'
            '    removed.to_s,\n'
            '    after_remove[0].to_s, after_remove[1].to_s,\n'
            '    relinked.to_s,\n'
            '    unlinked.to_s,\n'
            '    record.symlink?.to_s,\n'
            '    File.symlink?(keg.optlink.to_s).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    prefix = tmp_path / "prefix"
    assert (keg / "kegrecord.txt").read_text().splitlines() == [
        str(prefix / "var" / "homebrew" / "linked" / "ruby-kegrecord"),
        "false",
        "false",
        "true",
        "true",
        "true",
        "true",
        "true",
        "false",
        "true",
        "true",
        "true",
        "false",
        "false",
    ]


def test_formula_active_spec_prefers_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-active-spec",
        "1.0",
        (
            '  ref = Formula["ruby-active-spec"]\n'
            '  (prefix/"activespec.txt").write [\n'
            '    ref.determine_active_spec(:stable).url.to_s,\n'
            '    ref.determine_active_spec(:head).url.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.stable = SourceSpec(url="https://example.com/ruby-active-spec-1.0.tar.gz")
    keg = _build(tmp_path, formula)
    lines = (keg / "activespec.txt").read_text().splitlines()
    assert lines[0] == "https://example.com/ruby-active-spec-1.0.tar.gz"
    assert lines[1] == lines[0]


def test_formula_rack_installed_class_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-rack-installed",
        "1.0",
        (
            '  rack = Pathname.new(ENV["HOMEBREW_CELLAR"])/"rack-target"/"1.0"\n'
            '  rack.mkpath\n'
            '  (prefix/"rack-installed.txt").write [\n'
            '    Formula.rack_installed?("rack-target").to_s,\n'
            '    Formula.rack_installed?("missing-target").to_s,\n'
            '    Formula.installed_formula_names.include?("rack-target").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "rack-installed.txt").read_text().splitlines() == [
        "true",
        "false",
        "true",
    ]


def test_formula_specs_and_build_accessors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-spec-accessors",
        "1.0",
        (
            '  ref = Formula["ruby-spec-accessors"]\n'
            '  ref.active_spec = :head\n'
            '  ref.build = :build_marker\n'
            '  (prefix/"spec-accessors.txt").write [\n'
            '    ref.spec_syms.join(","),\n'
            '    ref.specs.map { |spec| spec.spec.to_s }.join(","),\n'
            '    ref.active_spec.to_s,\n'
            '    ref.build.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.stable = SourceSpec(url="https://example.com/spec-accessors-1.0.tar.gz")
    keg = _build(tmp_path, formula)
    assert (keg / "spec-accessors.txt").read_text().splitlines() == [
        "stable,head",
        "stable,head",
        "stable",
        "build_marker",
    ]


# ---------------------------------------------------------------------------
# FormulaInstaller loaded-source INSTALL_RECEIPT.json / Tab writing
# ---------------------------------------------------------------------------


def test_formula_installer_loaded_writes_install_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-receipt",
        "1.0",
        (
            '  path = prefix/"receipt-fixture.rb"\n'
            '  path.write <<~RUBY\n'
            '    class ReceiptFixture < Formula\n'
            '      version "2.0"\n'
            '      depends_on "dep"\n'
            '      depends_on "build-dep" => :build\n'
            '      install do\n'
            '        prefix.mkpath\n'
            '        (prefix/"installed.txt").write("ok")\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  klass = Formulary.load_formula_from_path("receipt-fixture", path)\n'
            '  FormulaInstaller.new(klass.new, installed_on_request: true).install\n'
            '  receipt_path = HOMEBREW_PREFIX/"Cellar"/"receipt-fixture"/"2.0"/"INSTALL_RECEIPT.json"\n'
            '  receipt = JSON.parse(receipt_path.read)\n'
            '  tab = Tab.for_keg(receipt_path.dirname)\n'
            '  default_path = prefix/"receipt-default.rb"\n'
            '  default_path.write <<~RUBY\n'
            '    class ReceiptDefault < Formula\n'
            '      version "1.0"\n'
            '      install do\n'
            '        prefix.mkpath\n'
            '        (prefix/"installed.txt").write("default")\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  default_klass = Formulary.load_formula_from_path("receipt-default", default_path)\n'
            '  FormulaInstaller.new(default_klass.new).install\n'
            '  default_receipt = JSON.parse(\n'
            '    (HOMEBREW_PREFIX/"Cellar"/"receipt-default"/"1.0"/"INSTALL_RECEIPT.json").read\n'
            '  )\n'
            '  (prefix/"receipt.txt").write [\n'
            '    receipt_path.file?.to_s,\n'
            '    receipt.keys.sort.join(","),\n'
            '    receipt["version"],\n'
            '    receipt["installed_on_request"].to_s,\n'
            '    receipt["runtime_dependencies"].join(","),\n'
            '    receipt["poured_from_bottle"].to_s,\n'
            '    tab["version"],\n'
            '    tab.installed_on_request?.to_s,\n'
            '    tab.poured_from_bottle?.to_s,\n'
            '    tab.runtime_dependencies.join(","),\n'
            '    FormulaInstaller.installed.map(&:name).join(","),\n'
            '    (receipt_path.dirname/"installed.txt").read,\n'
            '    default_receipt["installed_on_request"].to_s,\n'
            '    default_receipt["runtime_dependencies"].empty?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "receipt.txt").read_text().splitlines() == [
        "true",
        "HEAD,arch,bottle,built_as_bottle,built_on,changed_files,compiler,"
        "cxxstdlib,homebrew_version,installed_as_dependency,"
        "installed_on_request,loaded_from_api,poured_from_bottle,"
        "runtime_dependencies,sbom,source,source_modified_time,stdlib,tap,"
        "time,unused_options,used_options,version,version_scheme",
        "2.0",
        "true",
        "dep",
        "false",
        "2.0",
        "true",
        "false",
        "dep",
        "receipt-fixture,receipt-default",
        "ok",
        "false",
        "true",
    ]


def test_formula_installer_loaded_receipt_not_written_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-receipt-failure",
        "1.0",
        (
            '  path = prefix/"failing-receipt.rb"\n'
            '  path.write <<~RUBY\n'
            '    class FailingReceipt < Formula\n'
            '      version "1.0"\n'
            '      install do\n'
            '        prefix.mkpath\n'
            '        (prefix/"partial.txt").write("partial")\n'
            '        raise "receipt install boom"\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  klass = Formulary.load_formula_from_path("failing-receipt", path)\n'
            '  begin\n'
            '    FormulaInstaller.new(klass.new).install\n'
            '    failure = "none"\n'
            '  rescue => error\n'
            '    failure = error.class.name\n'
            '  end\n'
            '  failing_prefix = HOMEBREW_PREFIX/"Cellar"/"failing-receipt"/"1.0"\n'
            '  (prefix/"receipt-failure.txt").write [\n'
            '    failure,\n'
            '    (failing_prefix/"partial.txt").exist?.to_s,\n'
            '    (failing_prefix/"INSTALL_RECEIPT.json").exist?.to_s,\n'
            '    FormulaInstaller.installed.empty?.to_s,\n'
            '    FormulaInstaller.attempted.map(&:name).join(",")\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "receipt-failure.txt").read_text().splitlines() == [
        "RuntimeError",
        "true",
        "false",
        "true",
        "failing-receipt",
    ]


def test_formula_class_level_inventory_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-class-inventory",
        "1.0",
        (
            '  cellar = Pathname.new(ENV["HOMEBREW_CELLAR"])\n'
            '  (cellar/"ruby-class-inventory"/"1.0").mkpath\n'
            '  (cellar/"inventory-other"/"2.0").mkpath\n'
            '  (prefix/"inventory.txt").write [\n'
            '    Formula.installed.map(&:name).include?("ruby-class-inventory").to_s,\n'
            '    Formula.names.include?("inventory-other").to_s,\n'
            '    Formula.full_names.include?("ruby-class-inventory").to_s,\n'
            '    Formula.racks.map { |rack| rack.basename.to_s }.include?("inventory-other").to_s,\n'
            '    Formula.fuzzy_search("inventory").include?("inventory-other").to_s,\n'
            '    Formula.installed_with_alias_path.key?("inventory-other").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "inventory.txt").read_text().splitlines() == [
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
    ]


def test_keg_cleanup_and_alias_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-keg-cleanup",
        "1.0",
        (
            '  (prefix/"pkg"/"__pycache__").mkpath\n'
            '  (prefix/"pkg"/"__pycache__"/"mod.pyc").write "x"\n'
            '  (prefix/"pkg"/"plain.pyc").write "x"\n'
            '  (prefix/"pkg"/"keep.txt").write "keep"\n'
            '  keg = Keg.new(prefix)\n'
            '  deleted = keg.delete_pyc_files!\n'
            '  alias_link = Pathname.new(ENV["HOMEBREW_PREFIX"])/"opt"/"keg-cleanup-old"\n'
            '  alias_link.parent.mkpath\n'
            '  alias_link.make_symlink(prefix.to_s) unless alias_link.symlink?\n'
            '  removed = keg.remove_alias_symlink("keg-cleanup-old")\n'
            '  (prefix/"cleanup.txt").write [\n'
            '    (deleted >= 2).to_s,\n'
            '    (prefix/"pkg"/"__pycache__").exist?.to_s,\n'
            '    (prefix/"pkg"/"plain.pyc").exist?.to_s,\n'
            '    (prefix/"pkg"/"keep.txt").read,\n'
            '    removed.to_s,\n'
            '    alias_link.symlink?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "cleanup.txt").read_text().splitlines() == [
        "true",
        "false",
        "false",
        "keep",
        "true",
        "false",
    ]


def test_keg_node_gyp_debris_and_tab_dep_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-keg-nodegyp",
        "1.0",
        (
            '  base = prefix/"pkg"/"node_modules"/"addon"/"build"/"Release"\n'
            '  (base/"obj.target"/"nested").mkpath\n'
            '  (base/"obj.target"/"x.o").write "obj"\n'
            '  (base/"addon.a").write "archive"\n'
            '  (base/"addon.node").write "binary"\n'
            '  keg = Keg.new(prefix)\n'
            '  keg.require_relocation!\n'
            '  keg.delete_node_gyp_debris!\n'
            '  ref = Formula["ruby-keg-nodegyp"]\n'
            '  row = Tab.formula_to_dep_hash(ref, ["ruby-keg-nodegyp"])\n'
            '  (prefix/"nodegyp.txt").write [\n'
            '    keg.require_relocation?.to_s,\n'
            '    (base/"obj.target").exist?.to_s,\n'
            '    (base/"addon.a").exist?.to_s,\n'
            '    (base/"addon.node").exist?.to_s,\n'
            '    row["full_name"].to_s,\n'
            '    row["version"].to_s,\n'
            '    row["declared_directly"].to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "nodegyp.txt").read_text().splitlines() == [
        "true",
        "false",
        "false",
        "true",
        "ruby-keg-nodegyp",
        "1.0",
        "true",
    ]


def test_formulary_factory_from_api_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-api-factory",
        "1.0",
        (
            '  struct = Struct.new(:desc, :version).new("api desc", "3.1")\n'
            '  klass = Formulary.load_formula_from_struct!("api-factory", struct)\n'
            '  fetched = Formulary.factory("api-factory", :stable, from: :api)\n'
            '  missing = Formulary.factory("not-cached", :stable, from: :api)\n'
            '  (prefix/"api-factory.txt").write [\n'
            '    fetched.equal?(klass).to_s,\n'
            '    fetched.name.to_s,\n'
            '    missing.name.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "api-factory.txt").read_text().splitlines() == [
        "true",
        "api-factory",
        "not-cached",
    ]


def test_utils_development_tools_and_system_config_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-utils-core",
        "1.0",
        (
            '  (prefix/"utils-core.txt").write [\n'
            '    Utils.name_from_full_name("user/tap/foo"),\n'
            '    Utils.tap_from_full_name("user/tap/foo"),\n'
            '    Utils.full_name?("user/tap/foo").to_s,\n'
            '    Utils.convert_to_string_or_symbol(":abc").inspect,\n'
            '    Utils.deep_stringify_symbols(:abc),\n'
            '    Utils.deep_unstringify_symbols(":abc").to_s,\n'
            '    Utils.deep_compact_blank([nil, "", 0, 1]).join(","),\n'
            '    Utils.parse_author!("A <a@b>")[:name],\n'
            '    DevelopmentTools.host_gcc_path.to_s,\n'
            '    DevelopmentTools.needs_build_formulae?.to_s,\n'
            '    SystemConfig.homebrew_repo.to_s.start_with?(ENV["HOMEBREW_PREFIX"]).to_s,\n'
            '    SystemConfig.windows_version.nil?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "utils-core.txt").read_text().splitlines() == [
        "foo",
        "user/tap",
        "true",
        ":abc",
        ":abc",
        "abc",
        "1",
        "A",
        "/usr/bin/gcc",
        "false",
        "true",
        "true",
    ]


def test_formulary_module_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-formulary-extra",
        "1.0",
        (
            '  cellar = Pathname.new(ENV["HOMEBREW_CELLAR"])\n'
            '  (cellar/"rack-formulary"/"1.0").mkpath\n'
            '  (cellar/"ruby-formulary-extra"/"1.0").mkpath\n'
            '  rack = Formulary.to_rack("rack-formulary")\n'
            '  loader = Formulary.loader_for("ruby-formulary-extra")\n'
            '  keg = Keg.new(cellar/"ruby-formulary-extra"/"1.0")\n'
            '  (prefix/"formulary-extra.txt").write [\n'
            '    rack.basename.to_s,\n'
            '    Formulary.canonical_name("user/tap/foo.rb"),\n'
            '    Formulary.platform_cache_tag.include?("_").to_s,\n'
            '    Formulary.platform_cache.is_a?(Hash).to_s,\n'
            '    loader.name.to_s,\n'
            '    Formulary.try_new("ruby-formulary-extra").name.to_s,\n'
            '    Formulary.from_keg(keg).name.to_s,\n'
            '    Formulary.tap_formula_name_type("foo", "homebrew/core").first.to_s,\n'
            '    Formulary.core_path("foo").to_s.end_with?("/Formula/foo.rb").to_s,\n'
            '    Formulary.find_formula_in_tap("foo", Pathname.new("/tmp/tap")).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "formulary-extra.txt").read_text().splitlines()
    assert lines[0] == "rack-formulary"
    assert lines[1] == "foo"
    assert lines[2] == "true"
    assert lines[3] == "true"
    assert lines[4] == "ruby-formulary-extra"
    assert lines[5] == "ruby-formulary-extra"
    assert lines[6] == "ruby-formulary-extra"
    assert lines[7] == "foo"
    assert lines[8] == "true"
    assert lines[9] == "/tmp/tap/foo.rb"


def test_formula_installer_internal_compatibility_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-installer-surface",
        "1.0",
        (
            '  ref = Formula["ruby-installer-surface"]\n'
            '  installer = FormulaInstaller.new(ref)\n'
            '  (prefix/"installer-surface.txt").write [\n'
            '    installer.downloadable.equal?(installer).to_s,\n'
            '    installer.build_args.empty?.to_s,\n'
            '    installer.selected_bottle.nil?.to_s,\n'
            '    installer.verify_bottle_attestation?.to_s,\n'
            '    installer.unbottled_dependencies.empty?.to_s,\n'
            '    installer.formula_path.nil?.to_s,\n'
            '    installer.check_installation_already_attempted.nil?.to_s,\n'
            '    installer.create_staging_path.to_s.empty?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "installer-surface.txt").read_text().splitlines() == [
        "true",
        "true",
        "true",
        "false",
        "true",
        "true",
        "true",
        "false",
    ]


def test_formulary_from_contents_and_load_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-from-contents",
        "1.0",
        (
            '  source = "class FromContents < Formula\\n'
            '  desc \\"contents desc\\"\\n'
            '  homepage \\"https://example.com/from-contents\\"\\n'
            '  url \\"https://example.com/from-contents-1.0.tar.gz\\"\\n'
            '  version \\"1.0\\"\\n'
            '  def install; nil; end\\n'
            'end\\n"\n'
            '  loaded = Formulary.from_contents(\n'
            '    "from-contents", Pathname.new("/tmp/from-contents.rb"), source\n'
            '  )\n'
            '  (prefix/"from-contents.txt").write [\n'
            '    loaded.is_a?(PybrewLoadedFormula).to_s,\n'
            '    loaded.desc.to_s,\n'
            '    loaded.version.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "from-contents.txt").read_text().splitlines() == [
        "true",
        "contents desc",
        "1.0",
    ]


def test_compiler_gcc_probing_prefers_installed_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, version in (("gcc", "13.1.0"), ("gcc-12", "12.2.0")):
        script = bindir / name
        script.write_text(f"#!/bin/sh\necho 'gcc (GCC) {version}'\n")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")

    formula = _formula(
        "ruby-gcc-probe",
        "1.0",
        (
            '  (prefix/"gcc-probe.txt").write [\n'
            '    DevelopmentTools.gcc_version("gcc-12").to_s,\n'
            '    CompilerSelector.preferred_gcc,\n'
            '    CompilerSelector.compilers.join(",")\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "gcc-probe.txt").read_text().splitlines()
    assert lines[0] == "12.2.0"
    assert lines[1].startswith("gcc")
    assert lines[2] == "clang,llvm_clang,gnu"


def test_patch_cve_and_target_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-patch-helpers",
        "1.0",
        (
            '  base = Pathname.new(Dir.pwd)\n'
            '  (base/"file").write "a\\n"\n'
            '  Patch.ensure_targets_within!(\n'
            '    "--- a/file\\n+++ b/file\\n@@ -1 +1 @@\\n-a\\n+b\\n", strip: :p1, base: base\n'
            '  )\n'
            '  bad = begin\n'
            '    Patch.ensure_targets_within!(\n'
            '      "--- a/file\\n+++ ../../etc/passwd\\n", strip: :p1, base: base\n'
            '    )\n'
            '    "none"\n'
            '  rescue => error\n'
            '    (error.message.include?("escapes") || error.message.include?("Unsupported patch format")).to_s\n'
            '  end\n'
            '  (prefix/"patch-helpers.txt").write [\n'
            '    Patch.extract_cves("CVE-2024-1234", "CVE-2023-99999 CVE-2024-1234").join(","),\n'
            '    Patch.resolves_type("CVE-2024-1234"),\n'
            '    Patch.resolves_type("GHSA-xr7r-f8xq-vfvv"),\n'
            '    Patch.resolves_type("OSV-2023-298"),\n'
            '    Patch.resolves_type("https://github.com/foo/bar/issues/1"),\n'
            '    bad\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "patch-helpers.txt").read_text().splitlines() == [
        "CVE-2024-1234,CVE-2023-99999",
        "security",
        "security",
        "security",
        "defect",
        "true",
    ]


def test_resource_download_and_metadata_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-resource-helpers",
        "1.0",
        (
            '  staged = prefix/"resource-staged"\n'
            '  staged.mkpath\n'
            '  resource = PybrewResource.new(\n'
            '    "data",\n'
            '    {"url" => "https://example.com/data.tar.gz", "sha256" => "abc",\n'
            '     "using" => :curl, "staged" => staged.to_s}\n'
            '  )\n'
            '  resource.owner = "ruby-resource-helpers"\n'
            '  (prefix/"resource-helpers.txt").write [\n'
            '    resource.download_name,\n'
            '    resource.download_queue_name,\n'
            '    resource.download_queue_type.to_s,\n'
            '    resource.files.join(","),\n'
            '    resource.determine_url_mirrors.join(","),\n'
            '    resource.using.to_s,\n'
            '    resource.downloaded_and_valid?.to_s,\n'
            '    resource.owner.to_s,\n'
            '    resource.installed_size.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "resource-helpers.txt").read_text().splitlines() == [
        "data.tar.gz",
        "data.tar.gz",
        "resource",
        "data.tar.gz",
        "https://example.com/data.tar.gz",
        "curl",
        "true",
        "ruby-resource-helpers",
        "0",
    ]


def test_macos_mdfind_and_version_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    mdfind = bindir / "mdfind"
    mdfind.write_text("#!/bin/sh\nprintf '/Applications/Example.app\\n'\n")
    mdfind.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")

    formula = _formula(
        "ruby-macos-helpers",
        "1.0",
        (
            '  MacOS.full_version = "14.1"\n'
            '  (prefix/"macos-helpers.txt").write [\n'
            '    MacOS.mdfind("query").strip,\n'
            '    MacOS.mdfind_query("com.example.app"),\n'
            '    MacOS.app_with_bundle_id("com.example.app").to_s,\n'
            '    MacOS.full_version.to_s,\n'
            '    MacOS.pkgutil_info("missing.pkg").is_a?(String).to_s,\n'
            '    MacOS.sdk_locator.equal?(MacOS).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "macos-helpers.txt").read_text().splitlines() == [
        "/Applications/Example.app",
        "kMDItemCFBundleIdentifier == 'com.example.app'",
        "/Applications/Example.app",
        "14.1",
        "true",
        "true",
    ]


def test_resource_metadata_fields_from_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-resource-fields",
        "1.0",
        (
            '  resource = PybrewResource.new(\n'
            '    "data",\n'
            '    {"directory" => "src", "path_exec_files" => ["bin/tool"],\n'
            '     "installed_size" => 12, "bottle_size" => 34,\n'
            '     "manifest_annotations" => {"license" => "MIT"},\n'
            '     "sbom_supplement" => "supplement",\n'
            '     "download_strategy" => ":curl"}\n'
            '  )\n'
            '  (prefix/"resource-fields.txt").write [\n'
            '    resource.directory.to_s,\n'
            '    resource.path_exec_files.join(","),\n'
            '    resource.installed_size.to_s,\n'
            '    resource.bottle_size.to_s,\n'
            '    resource.manifest_annotations["license"].to_s,\n'
            '    resource.sbom_supplement.to_s,\n'
            '    resource.download_strategy.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "resource-fields.txt").read_text().splitlines() == [
        "src",
        "bin/tool",
        "12",
        "34",
        "MIT",
        "supplement",
        ":curl",
    ]


def test_bottle_tag_helpers(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
(prefix/"bottle-tag.txt").write [
  Utils::Bottles::Tag.from_symbol(:arm64_sonoma).to_s,
  Utils::Bottles::Tag.from_symbol(:ventura).to_s,
  Utils::Bottles::Tag.from_symbol(:all).to_s,
  Utils::Bottles::Tag.from_symbol(:x86_64_linux).standardized_arch.to_s,
  Utils::Bottles::Tag.from_symbol(:arm64_sonoma).macos?.to_s
].join("\n")
""",
        name="ruby-bottle-tag",
    )
    assert (keg / "bottle-tag.txt").read_text().splitlines() == [
        "arm64_sonoma",
        "ventura",
        "all",
        "x86_64",
        "true",
    ]


def test_keg_optlink_action_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
keg = Keg.new(prefix)
path = keg.optlink
dry = keg.optlink(true, true, false)
linked = keg.optlink(true, false, false)
(prefix/"optlink.txt").write [
  path.to_s.include?("/opt/").to_s,
  dry.nil?.to_s,
  File.symlink?(path.to_s).to_s,
  linked.nil?.to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "optlink.txt").read_text().splitlines() == [
        "true",
        "true",
        "true",
        "true",
    ]


def test_keg_link_error_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
conflict = prefix/"conflict.txt"
conflict.write "conflict"
error = Keg::ConflictError.new(
  Keg.new(prefix), Pathname.new("source"), conflict, RuntimeError.new("cause")
)
(prefix/"link-error.txt").write [
  error.src.to_s,
  error.suggestion.to_s.include?("already exists").to_s,
  error.to_s.include?("Could not symlink").to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "link-error.txt").read_text().splitlines() == [
        "source",
        "true",
        "true",
    ]


def test_env_determination_helpers_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
(prefix/"env-determine.txt").write [
  ENV.determine_cc.to_s.empty?.to_s,
  ENV.determine_cxx.to_s.empty?.to_s,
  ENV.determine_make_jobs.to_i.positive?.to_s,
  ENV.homebrew_extra_paths.empty?.to_s,
  ENV.reset.nil?.to_s,
  ENV.refurbish_args.nil?.to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "env-determine.txt").read_text().splitlines() == [
        "false",
        "false",
        "true",
        "true",
        "true",
        "true",
    ]


def test_macos_xcode_and_sdk_helpers_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
(prefix/"xcode-sdk.txt").write [
  MacOS::Xcode.bundle_path.to_s.end_with?(".app").to_s,
  MacOS::Xcode.minimum_version.to_s.empty?.to_s,
  MacOS::Xcode.below_minimum_version?.to_s,
  MacOS::Xcode.update_instructions.include?("Xcode").to_s,
  MacOS::SDK.sdk_prefix.to_s.include?("SDKs").to_s,
  MacOS::SDK.all_sdks.is_a?(Array).to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "xcode-sdk.txt").read_text().splitlines() == [
        "true",
        "false",
        "false",
        "true",
        "true",
        "true",
    ]


def test_bottle_tag_macos_and_svn_helpers_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
(prefix/"bottle-svn.txt").write [
  Utils::Bottles::Tag.from_symbol(:sonoma).to_macos_version.to_s,
  Utils::Svn.invalid_cert_flags.empty?.to_s,
  Utils::Svn.remote_exists?("file:///definitely-not-a-repo").to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "bottle-svn.txt").read_text().splitlines() == [
        "14",
        "true",
        "false",
    ]


def test_utils_analytics_noop_helpers_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
(prefix/"analytics.txt").write [
  Utils::Analytics.format_count(3),
  Utils::Analytics.format_percent(50),
  Utils::Analytics.no_message_output?.to_s,
  Utils::Analytics.table_output.empty?.to_s,
  Utils::Analytics.with_wsl_suffix_if_needed("text"),
  Utils::Analytics.default_package_fields.empty?.to_s,
  Utils::Analytics.report_package_event("x").nil?.to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "analytics.txt").read_text().splitlines() == [
        "3",
        "50%",
        "true",
        "true",
        "text",
        "true",
        "true",
    ]


def test_utils_curl_helpers_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
(prefix/"curl.txt").write [
  Utils::Curl.curl_executable.to_s.empty?.to_s,
  Utils::Curl.curl_path.nil?.to_s,
  Utils::Curl.curl_version.to_s.empty?.to_s,
  Utils::Curl.strip_progress_bar("50.0% body"),
  Utils::Curl.http_status_ok?(200).to_s,
  Utils::Curl.http_status_ok?(404).to_s,
  Utils::Curl.parse_curl_response("HTTP/1.1 200 OK\\r\\n\\r\\nbody")[1]
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "curl.txt").read_text().splitlines() == [
        "false",
        "false",
        "false",
        "body",
        "true",
        "false",
        "body",
    ]


def test_upstream_env_tab_installer_additions_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
tab = Tab.new(
  "versions" => { "stable" => "1.0", "head" => "HEAD" },
  "used_options" => ["--with-x"],
  "runtime_dependencies" => ["dep"],
  "built_as_bottle" => true
)
installer = FormulaInstaller.new(nil)
(prefix/"upstream.txt").write [
  ENV.build_bottle?.to_s,
  ENV.debug_symbols?.to_s,
  ENV.deps.empty?.to_s,
  ENV.keg_only_deps.empty?.to_s,
  ENV.run_time_deps.empty?.to_s,
  tab.versions["stable"],
  tab.stable_version,
  tab.head_version,
  tab.built_bottle?.to_s,
  tab.any_args_or_options?.to_s,
  tab.runtime_deps_hash.empty?.to_s,
  installer.fix_dynamic_linkage?.to_s,
  installer.same_formula?(nil).to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "upstream.txt").read_text().splitlines() == [
        "false",
        "false",
        "true",
        "true",
        "true",
        "1.0",
        "1.0",
        "HEAD",
        "true",
        "true",
        "false",
        "true",
        "false",
    ]


def test_service_ruby_object_helpers_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
service = Service.new(nil)
service.name("demo")
service.run(["echo", "hi"])
service.keep_alive(true)
service.require_root(false)
service.environment_variables({ "A" => "B" })
(prefix/"service.txt").write [
  service.default_plist_name,
  service.command.join(" "),
  service.command?.to_s,
  service.keep_alive?.to_s,
  service.requires_root?.to_s,
  service.timed?.to_s,
  service.path_dirs.empty?.to_s,
  service.to_plist["Label"],
  service.to_systemd_unit.include?("ExecStart").to_s,
  service.cron_weekday_to_systemd_weekday(0).to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "service.txt").read_text().splitlines() == [
        "homebrew.mxcl.demo",
        "echo hi",
        "true",
        "true",
        "false",
        "false",
        "true",
        "homebrew.mxcl.demo",
        "true",
        "7",
    ]


def test_utils_bottles_module_helpers_raw(tmp_path: Path) -> None:
    bottle = tmp_path / "demo--1.0.arm64_sonoma.bottle.tar.gz"
    with tarfile.open(bottle, "w:gz") as archive:
        receipt = json.dumps({"tap": "homebrew/core"}).encode()
        import io as _io

        info = tarfile.TarInfo("demo/1.0/INSTALL_RECEIPT.json")
        info.size = len(receipt)
        archive.addfile(info, _io.BytesIO(receipt))
        source = b"class Demo < Formula\nend\n"
        info = tarfile.TarInfo("demo/1.0/.brew/demo.rb")
        info.size = len(source)
        archive.addfile(info, _io.BytesIO(source))
    keg = _run_raw_body(
        tmp_path,
        f"""
parts = Utils::Bottles.extname_tag_rebuild("demo--1.0.arm64_sonoma.bottle.tar.gz")
names = Utils::Bottles.resolve_formula_names("{bottle}")
(prefix/"bottles.txt").write [
  Utils::Bottles.tag(:arm64_sonoma).to_s,
  parts[1],
  parts[2],
  names[0],
  names[1],
  Utils::Bottles.resolve_version("{bottle}").to_s,
  Utils::Bottles.formula_contents("{bottle}").include?("Demo").to_s,
  Utils::Bottles.missing_all_bottle_publish_note
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "bottles.txt").read_text().splitlines() == [
        "arm64_sonoma",
        "arm64_sonoma",
        "",
        "demo",
        "homebrew/core/demo",
        "1.0",
        "true",
        "publishing without one anyway",
    ]


def test_patch_apply_real_file_raw(tmp_path: Path) -> None:
    build = tmp_path / "build"
    build.mkdir()
    (build / "hello.txt").write_text("old\n")
    patch_file = tmp_path / "change.diff"
    patch_file.write_text(
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    keg = _run_raw_body(
        tmp_path,
        f"""
patch = PybrewPatch.new
patch.url("{patch_file}")
patch.strip(:p1)
patch.apply
(prefix/"applied.txt").write File.read("hello.txt")
""",
        name="raw-body",
    )
    assert (keg / "applied.txt").read_text() == "new\n"


def test_utils_curl_additional_helpers_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
(prefix/"curl2.txt").write [
  Utils::Curl.curl_check_http_content("https://example.invalid")[0].to_s,
  Utils::Curl.curl_headers("https://example.invalid").empty?.to_s,
  Utils::Curl.curl_http_content_headers_and_checksum("x")[1].empty?.to_s,
  Utils::Curl.curl_response_last_location("x").nil?.to_s,
  Utils::Curl.insecure_redirect?.to_s,
  Utils::Curl.no_insecure_redirect_curl_args.empty?.to_s,
  Utils::Curl.url_protected_by_cloudflare?("x").to_s,
  Utils::Curl.url_protected_by_incapsula?("x").to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "curl2.txt").read_text().splitlines() == [
        "true",
        "true",
        "true",
        "true",
        "false",
        "true",
        "false",
        "false",
    ]


def test_bottle_specification_helpers_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
spec = PybrewBottleSpec.new(
  "current_tag" => "arm64_sonoma",
  "files" => {
    "arm64_sonoma" => { "sha256" => "abc", "cellar" => "/opt/homebrew/Cellar" },
    "all" => { "sha256" => "def", "cellar" => "/usr/local/Cellar" }
  }
)
spec.add("x86_64_linux", checksum: "ghi", cellar: "/home/linuxbrew/.linuxbrew/Cellar")
(prefix/"bottle-spec.txt").write [
  spec.tags.sort.join(","),
  spec.tag?("arm64_sonoma").to_s,
  spec.tag?("ventura").to_s,
  spec.find_matching_tag("ventura").to_s,
  spec.specification_for("all").sha256,
  spec.bottle_for_tag("x86_64_linux").sha256,
  spec.eql?(spec).to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "bottle-spec.txt").read_text().splitlines() == [
        "all,arm64_sonoma,x86_64_linux",
        "true",
        "true",
        "all",
        "def",
        "ghi",
        "true",
    ]


def test_bottle_specification_metadata_helpers_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
spec = PybrewBottleSpec.new(
  "root_url" => "https://ghcr.io/v2/homebrew/core",
  "current_tag" => "arm64_sonoma",
  "files" => {
    "arm64_sonoma" => { "sha256" => "abc", "cellar" => "/opt/homebrew/Cellar", "total_size" => 12 }
  }
)
(prefix/"bottle-meta.txt").write [
  spec.json["root_url"],
  spec.total_size.to_s,
  spec.built_cellar,
  spec.compatible_locations?.to_s,
  spec.extname,
  spec.tab_attributes.empty?.to_s,
  spec.to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "bottle-meta.txt").read_text().splitlines() == [
        "https://ghcr.io/v2/homebrew/core",
        "12",
        "/opt/homebrew/Cellar",
        "true",
        ".bottle.tar.gz",
        "true",
        "https://ghcr.io/v2/homebrew/core",
    ]


def test_bottle_specification_download_stage_helpers_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
spec = PybrewBottleSpec.new("current_tag" => "all", "files" => {})
(prefix/"bottle-stage.txt").write [
  spec.skip_relocation?.to_s,
  spec.fallback_on_error?.to_s,
  spec.compatible_locations_from_tab?.to_s,
  spec.stage_from_download_queue?.to_s,
  spec.staged_from_download_queue?.to_s,
  spec.with_verified_snapshot { "ok" },
  spec.with_corrupt_download_retry { "retry" }
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "bottle-stage.txt").read_text().splitlines() == [
        "false",
        "false",
        "true",
        "false",
        "false",
        "ok",
        "retry",
    ]


def test_utils_path_helpers_raw(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("x")
    keg = _run_raw_body(
        tmp_path,
        f"""
raised = begin
  Utils::Path.ensure_child_of!("/a", "/b", message: "bad")
  false
rescue StandardError => error
  error.message == "bad"
end
(prefix/"path.txt").write [
  Utils::Path.child_of?("/a", "/a/b").to_s,
  Utils::Path.child_of?("/a", "/b").to_s,
  raised.to_s,
  Utils::Path.clean_absolute_path?("/a/b").to_s,
  Utils::Path.clean_absolute_path?("a/b").to_s,
  Utils::Path.relative_path_from("/a/b/c", "/a").to_s,
  Utils::Path.formula_opt_bin("demo").to_s.end_with?("/opt/demo/bin").to_s,
  Utils::Path.loadable_package_path?("/a").to_s,
  Utils::Path.cp_path_sub("{source}", "source", "copy").exist?.to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "path.txt").read_text().splitlines() == [
        "true",
        "false",
        "true",
        "true",
        "false",
        "b/c",
        "true",
        "true",
        "true",
    ]


def test_utils_git_helpers_raw(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "file.txt").write_text("hello\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "A"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    keg = _run_raw_body(
        tmp_path,
        f"""
Dir.chdir("{repo}") do
  (prefix/"git.txt").write [
    Utils::Git.head("{repo}") == "{commit}",
    Utils::Git.last_commit("{repo}") == "{commit}",
    Utils::Git.file_at_commit("file.txt", "{commit}").include?("hello"),
    Utils::Git.last_revision_commit_of_file("{repo}", "file.txt") == "{commit}",
    Utils::Git.supports_partial_clone_sparse_checkout?.to_s,
    Utils::Git.no_global_config_env.empty?.to_s
  ].map(&:to_s).join("\\n")
end
""",
        name="raw-body",
    )
    assert (keg / "git.txt").read_text().splitlines() == [
        "true", "true", "true", "true", "true", "true"
    ]


def test_utils_spdx_helpers_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        """
(prefix/"spdx.txt").write [
  Utils::SPDX.valid_license?("MIT").to_s,
  Utils::SPDX.valid_license_exception?("Classpath-exception-2.0").to_s,
  Utils::SPDX.deprecated_license?("GPL-1.0").to_s,
  Utils::SPDX.licenses_forbid_installation?("x").to_s,
  Utils::SPDX.forbidden_licenses_include?("x").to_s,
  Utils::SPDX.parse_license_expression("MIT OR Apache-2.0"),
  Utils::SPDX.license_expression_to_string("MIT"),
  Utils::SPDX.license_index.empty?.to_s
].join("\\n")
""",
        name="raw-body",
    )
    assert (keg / "spdx.txt").read_text().splitlines() == [
        "true",
        "false",
        "false",
        "false",
        "false",
        "MIT OR Apache-2.0",
        "MIT",
        "true",
    ]


def test_bottle_spec_root_url_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        '''
spec = PybrewBottleSpec.new("root_url" => "https://example.invalid/bottles")
(prefix/"root-url.txt").write spec.root_url
''',
        name="raw-body",
    )
    assert (keg / "root-url.txt").read_text() == "https://example.invalid/bottles"


def test_formula_spec_eval_raw(tmp_path: Path) -> None:
    keg = _run_raw_body(
        tmp_path,
        '''
source = PybrewSourceSpec.new({ "url" => "https://example.invalid/a.tar.gz" }, :stable)
evaluated = spec_eval(source, :stable)
empty = spec_eval(PybrewSourceSpec.new({}, :stable), :stable)
(prefix/"spec-eval.txt").write [
  evaluated.url,
  evaluated.owner.equal?(self).to_s,
  empty.nil?.to_s
].join("\\n")
''',
        name="raw-body",
    )
    assert (keg / "spec-eval.txt").read_text().splitlines() == [
        "https://example.invalid/a.tar.gz",
        "true",
        "true",
    ]


def test_formulary_path_cache_mtime_and_stale_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-mtime-cache",
        "1.0",
        (
            '  path = prefix/"mtime-formula.rb"\n'
            '  path.write <<~RUBY\n'
            '    class MtimeFixture < Formula\n'
            '      version "1.0"\n'
            '    end\n'
            '  RUBY\n'
            '  defined_before = Formulary.formula_class_defined_from_path?(path).to_s\n'
            '  klass = Formulary.load_formula_from_path("mtime-fixture", path)\n'
            '  same_load = Formulary.load_formula_from_path("mtime-fixture", path).equal?(klass)\n'
            '  same_get = Formulary.formula_class_get_from_path(path).equal?(klass)\n'
            '  path.write <<~RUBY\n'
            '    class MtimeFixture < Formula\n'
            '      version "2.0"\n'
            '      desc "reloaded"\n'
            '    end\n'
            '  RUBY\n'
            '  File.utime(Time.now + 10, Time.now + 10, path.to_s)\n'
            '  defined_after_change = Formulary.formula_class_defined_from_path?(path).to_s\n'
            '  reloaded = Formulary.formula_class_get_from_path(path)\n'
            '  Formulary.clear_cache\n'
            '  after_clear = Formulary.formula_class_defined_from_path?(path).to_s\n'
            '  begin\n'
            '    Formulary.formula_class_get_from_path(path)\n'
            '    cleared_error = "none"\n'
            '  rescue => error\n'
            '    cleared_error = error.class.name\n'
            '  end\n'
            '  (prefix/"mtime.txt").write [\n'
            '    defined_before, same_load.to_s, same_get.to_s, klass.loaded_version,\n'
            '    defined_after_change, reloaded.loaded_version, reloaded.loaded_desc,\n'
            '    (!reloaded.equal?(klass)).to_s, after_clear, cleared_error\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "mtime.txt").read_text().splitlines() == [
        "false",
        "true",
        "true",
        "1.0",
        "true",
        "2.0",
        "reloaded",
        "true",
        "false",
        "PybrewFormulaUnavailableError",
    ]


def test_sandbox_bounded_wrapper_with_fake_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    fake_sandbox = tmp_path / "fake-sandbox"
    fake_sandbox.write_text("#!/bin/sh\nprintf 'sandbox:%s\\n' \"$*\"\n")
    fake_sandbox.chmod(0o755)
    formula = _formula(
        "ruby-sandbox-wrapper",
        "1.0",
        (
            '  default_available = Sandbox.available?\n'
            '  default_run = Sandbox.run("echo", "hello").inspect\n'
            '  ENV["PYBREW_SANDBOX"] = "1"\n'
            '  ENV["PYBREW_SANDBOX_BINARY"] = "' + str(fake_sandbox) + '"\n'
            '  enabled = Sandbox.available?\n'
            '  output = Sandbox.run("echo", "hello")\n'
            '  (prefix/"sandbox-wrapper.txt").write [\n'
            '    default_available.to_s, default_run, enabled.to_s,\n'
            '    output.to_s.strip, Sandbox.failure_reason.nil?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "sandbox-wrapper.txt").read_text().splitlines() == [
        "false",
        "nil",
        "true",
        "sandbox:echo hello",
        "true",
    ]


def test_compiler_selector_probes_fake_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    fake_bin = tmp_path / "fake-compilers"
    fake_bin.mkdir()
    (fake_bin / "clang").write_text("#!/bin/sh\necho 'clang version 50.0.0'\n")
    (fake_bin / "gcc").write_text("#!/bin/sh\necho 'gcc 50.0.0'\n")
    (fake_bin / "clang").chmod(0o755)
    (fake_bin / "gcc").chmod(0o755)
    formula = _formula(
        "ruby-compiler-probe",
        "1.0",
        (
            '  fake_bin = Pathname.new("' + str(fake_bin) + '")\n'
            '  ENV["PATH"] = "#{fake_bin}#{File::PATH_SEPARATOR}#{ENV["PATH"]}"\n'
            '  failing = Object.new\n'
            '  def failing.compiler_failures\n'
            '    [CompilerFailure.create(:clang) { version "99.0" }]\n'
            '  end\n'
            '  clean = Object.new\n'
            '  def clean.compiler_failures\n'
            '    []\n'
            '  end\n'
            '  (prefix/"compiler-probe.txt").write [\n'
            '    DevelopmentTools.clang_version,\n'
            '    DevelopmentTools.gcc_version,\n'
            '    CompilerSelector.select_for(failing).to_s,\n'
            '    CompilerSelector.select_for(clean).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "compiler-probe.txt").read_text().splitlines() == [
        "50.0.0",
        "50.0.0",
        "llvm_clang",
        "clang",
    ]


def test_formulary_factory_alias_and_oldname_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-alias-canonical",
        "1.0",
        (
            '  (prefix/"alias.txt").write [\n'
            '    Formulary.factory("ruby-alias-only").name,\n'
            '    Formulary.factory("ruby-oldname-only").name,\n'
            '    Formulary.factory("ruby-alias-canonical").name,\n'
            '    Formulary.factory("definitely-unknown").name\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.aliases = ["ruby-alias-only"]
    formula.oldname = "ruby-oldname-only"
    keg = _build(tmp_path, formula)
    assert (keg / "alias.txt").read_text().splitlines() == [
        "ruby-alias-canonical",
        "ruby-alias-canonical",
        "ruby-alias-canonical",
        "definitely-unknown",
    ]


def test_utils_github_injectable_curl_and_write_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    fake_curl = tmp_path / "fake-curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        'url=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in http*) url="$arg" ;; esac\n'
        'done\n'
        'case "$url" in\n'
        "  */repos/homebrew/foo/releases/latest) printf '%s' '{\"tag_name\":\"v1.2.3\"}' ;;\n"
        "  */repos/homebrew/foo/releases/tags/v1.2.3) printf '%s' '{\"tag_name\":\"v1.2.3\"}' ;;\n"
        "  */repos/homebrew/foo/actions/workflows/tests.yml/runs*) printf '%s' "
        "'{\"workflow_runs\":[{\"id\":42,\"artifacts_url\":\"https://api.github.com/foo/artifacts\"}]}' ;;\n"
        "  */foo/artifacts*) printf '%s' '{\"artifacts\":[{\"archive_download_url\":\"https://example.com/foo.zip\"}]}' ;;\n"
        "  */repos/homebrew/foo/license*) printf '%s' '{\"license\":{\"spdx_id\":\"MIT\",\"key\":\"mit\"}}' ;;\n"
        "  */repos/homebrew/foo/issues*) printf '%s' '[{\"number\":1,\"title\":\"issue one\"}]' ;;\n"
        "  */repos/homebrew/foo) printf '%s' '{\"full_name\":\"homebrew/foo\",\"private\":true}' ;;\n"
        "  */user) printf '%s' '{\"login\":\"tester\"}' ;;\n"
        "  */search/issues*) printf '%s' '{\"total_count\":1,\"items\":[{\"number\":2}]}' ;;\n"
        "  *) printf '%s' '{}' ;;\n"
        'esac\n'
    )
    fake_curl.chmod(0o755)
    formula = _formula(
        "ruby-github",
        "1.0",
        (
            '  ENV["PYBREW_GITHUB_CURL"] = "' + str(fake_curl) + '"\n'
            '  latest = Utils::GitHub.get_latest_release("homebrew", "foo")\n'
            '  release = Utils::GitHub.get_release("homebrew", "foo", "v1.2.3")\n'
            '  run = Utils::GitHub.get_workflow_run("homebrew", "foo", 1)\n'
            '  artifact_urls = Utils::GitHub.get_artifact_urls([run])\n'
            '  license = Utils::GitHub.get_repo_license("homebrew", "foo")\n'
            '  repo = Utils::GitHub.repository("homebrew", "foo")\n'
            '  user = Utils::GitHub.user\n'
            '  issues = Utils::GitHub.issues(repo: "homebrew/foo", state: "open")\n'
            '  search = Utils::GitHub.search_issues("foo", state: "open")\n'
            '  begin\n'
            '    Utils::GitHub.create_issue("homebrew/foo", "t", "b")\n'
            '    create_error = "none"\n'
            '  rescue NotImplementedError => error\n'
            '    create_error = error.class.name\n'
            '  end\n'
            '  begin\n'
            '    Utils::GitHub.upload_release_asset("homebrew", "foo", 1, local_file: "x")\n'
            '    upload_error = "none"\n'
            '  rescue NotImplementedError => error\n'
            '    upload_error = error.class.name\n'
            '  end\n'
            '  begin\n'
            '    Utils::GitHub.workflow_dispatch_event("homebrew", "foo", "tests.yml", "main")\n'
            '    dispatch_error = "none"\n'
            '  rescue NotImplementedError => error\n'
            '    dispatch_error = error.class.name\n'
            '  end\n'
            '  (prefix/"github.txt").write [\n'
            '    latest["tag_name"], release["tag_name"], run["id"].to_s,\n'
            '    artifact_urls.join(","), license["license"]["spdx_id"],\n'
            '    repo["private"].to_s, user["login"], issues.first["title"],\n'
            '    search["total_count"].to_s,\n'
            '    Utils::GitHub.private_repo?("homebrew/foo").to_s,\n'
            '    Utils::GitHub.url_to("repos", "homebrew", "foo"),\n'
            '    Utils::GitHub.search_query_string("foo bar", state: "open"),\n'
            '    create_error, upload_error, dispatch_error\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "github.txt").read_text().splitlines() == [
        "v1.2.3",
        "v1.2.3",
        "42",
        "https://example.com/foo.zip",
        "MIT",
        "true",
        "tester",
        "issue one",
        "1",
        "true",
        "https://api.github.com/repos/homebrew/foo",
        "q=foo+bar+state%3Aopen",
        "NotImplementedError",
        "NotImplementedError",
        "NotImplementedError",
    ]


@pytest.mark.skipif(not is_macos(), reason="macOS-only detection overrides")
def test_macos_xcode_clt_sdk_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-macos-detect",
        "1.0",
        (
            '  fake_dev = prefix/"fake-developer"\n'
            '  (fake_dev/"Platforms/MacOSX.platform/Developer/SDKs/Fake.sdk").mkpath\n'
            '  fake_sdk = prefix/"fake-sdk"\n'
            '  fake_sdk.mkpath\n'
            '  ENV["HOMEBREW_DEVELOPER_DIR"] = fake_dev.to_s\n'
            '  ENV["HOMEBREW_SDKROOT"] = fake_sdk.to_s\n'
            '  ENV["HOMEBREW_SDK_VERSION"] = "14.4"\n'
            '  ENV["HOMEBREW_XCODE_VERSION"] = "15.4"\n'
            '  ENV["HOMEBREW_CLT_VERSION"] = "15.4.0.1"\n'
            '  (prefix/"macos-detect.txt").write [\n'
            '    MacOS.active_developer_dir,\n'
            '    MacOS.sdk_path.to_s,\n'
            '    MacOS.sdk.version.to_s,\n'
            '    MacOS::Xcode.installed?.to_s,\n'
            '    MacOS::Xcode.prefix.to_s,\n'
            '    MacOS::Xcode.version.to_s,\n'
            '    MacOS::CLT.installed?.to_s,\n'
            '    MacOS::CLT.version.to_s,\n'
            '    MacOS::CLT.sdk_path.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    fake_dev = keg / "fake-developer"
    fake_sdk = keg / "fake-sdk"
    assert (keg / "macos-detect.txt").read_text().splitlines() == [
        str(fake_dev),
        str(fake_sdk),
        "14.4",
        "true",
        str(fake_dev),
        "15.4",
        "true",
        "15.4.0.1",
        str(fake_sdk),
    ]


def test_keg_ignore_interrupts_uninstall_and_noop_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-keg-uninstall",
        "1.0",
        (
            '  keg_path = Pathname.new(ENV["HOMEBREW_CELLAR"])/"ruby-keg-uninstall/1.0"\n'
            '  (keg_path/"bin").mkpath\n'
            '  (keg_path/"bin"/"tool").write "#!/bin/sh\\n"\n'
            '  keg = Keg.new(keg_path)\n'
            '  keg.link\n'
            '  (prefix/"keg-uninstall.txt").write [\n'
            '    keg.linked?.to_s,\n'
            '    File.symlink?(keg.optlink.to_s).to_s,\n'
            '    keg.prepare_debug_symbols.nil?.to_s,\n'
            '    keg.consistent_reproducible_symlink_permissions!.to_s,\n'
            '    keg.ignore_interrupts_and_uninstall!.to_s,\n'
            '    keg_path.exist?.to_s,\n'
            '    File.symlink?(keg.optlink.to_s).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "keg-uninstall.txt").read_text().splitlines() == [
        "true",
        "true",
        "true",
        "true",
        "true",
        "false",
        "false",
    ]


def test_sandbox_policy_surface_presence_and_explicit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-sandbox-policy",
        "1.0",
        (
            '  names = %w[add_rule allow_read allow_read_if_exists allow_write\n'
            '             allow_write_path deny_all_network deny_read deny_write apply!\n'
            '             capture cleanup_sandbox copy_pty_output protect_homebrew_state\n'
            '             record_log executable! ensure_sandbox_available!\n'
            '             executable_candidate_paths executable_name executable_usable?\n'
            '             apply_before_exec? sandbox_command sandbox_environment\n'
            '             run_command ruby_command for_operation operation\n'
            '             isolate_operation? tty_state terminal_ioctl_request\n'
            '             ensure_child_tty_available home_write_paths path_filter\n'
            '             expand_realpath]\n'
            '  presence = names.all? { |name| Sandbox.respond_to?(name.to_sym) }\n'
            '  errors = []\n'
            '  %w[allow_read apply! capture cleanup_sandbox protect_homebrew_state record_log].each do |name|\n'
            '    begin\n'
            '      Sandbox.public_send(name, "/tmp")\n'
            '      errors << "none"\n'
            '    rescue NotImplementedError => error\n'
            '      errors << error.class.name\n'
            '    end\n'
            '  end\n'
            '  ensure_error = begin\n'
            '    Sandbox.ensure_sandbox_available!\n'
            '    "none"\n'
            '  rescue NotImplementedError => error\n'
            '    error.class.name\n'
            '  end\n'
            '  (prefix/"sandbox-policy.txt").write [\n'
            '    presence.to_s,\n'
            '    Sandbox.apply_before_exec?.to_s,\n'
            '    Sandbox.isolate_operation?.to_s,\n'
            '    Sandbox.executable_candidate_paths.empty?.to_s,\n'
            '    Sandbox.home_write_paths.empty?.to_s,\n'
            '    Sandbox.path_filter.nil?.to_s,\n'
            '    Sandbox.for_operation { "ran" },\n'
            '    Sandbox.sandbox_command("echo", "x").join(","),\n'
            '    Sandbox.ruby_command("--version").join(","),\n'
            '    Sandbox.run_command("echo", "x").inspect,\n'
            '    errors.join(","),\n'
            '    ensure_error\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "sandbox-policy.txt").read_text().splitlines() == [
        "true",
        "false",
        "false",
        "false",
        "true",
        "true",
        "ran",
        "echo,x",
        "ruby,--version",
        "nil",
        "NotImplementedError,NotImplementedError,NotImplementedError,"
        "NotImplementedError,NotImplementedError,NotImplementedError",
        "NotImplementedError",
    ]


def test_utils_github_extended_reads_and_write_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    fake_curl = tmp_path / "fake-curl-ext"
    fake_curl.write_text(
        "#!/bin/sh\n"
        'url=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in http*) url="$arg" ;; esac\n'
        'done\n'
        'case "$url" in\n'
        "  */repos/homebrew/foo/pulls/7/commits*) printf '%s' '[{\"sha\":\"abc\"}]' ;;\n"
        "  */repos/homebrew/foo/pulls/7/labels*) printf '%s' '[{\"name\":\"bump\"}]' ;;\n"
        "  */repos/homebrew/foo/pulls/7/files*) printf '%s' '[{\"filename\":\"Formula/foo.rb\"}]' ;;\n"
        "  */repos/homebrew/foo/pulls/7/reviews*) printf '%s' "
        "'[{\"state\":\"APPROVED\",\"commit_id\":\"abc\",\"author_association\":\"MEMBER\",\"user\":{\"login\":\"rev\"}}]' ;;\n"
        "  */repos/homebrew/foo/commits/abc*) printf '%s' '{\"sha\":\"abc\"}' ;;\n"
        "  */repos/homebrew/bar*) printf '%s' '{\"full_name\":\"homebrew/bar\"}' ;;\n"
        "  */repos/homebrew/nothing*) printf '%s' '{\"message\":\"Not Found\"}' ;;\n"
        "  */orgs/homebrew/teams/core/members*) printf '%s' '[{\"login\":\"alice\"}]' ;;\n"
        "  */orgs/homebrew/repos*) printf '%s' '[{\"name\":\"foo\"}]' ;;\n"
        "  */users/alice/sponsors/tiers*) printf '%s' '[{\"monthly_price_in_cents\":500}]' ;;\n"
        "  */users/alice/sponsors*) printf '%s' '[{\"login\":\"sponsor\"}]' ;;\n"
        "  */search/issues*) printf '%s' "
        "'{\"total_count\":1,\"items\":[{\"number\":9,\"title\":\"foo: update\",\"html_url\":\"https://github.com/homebrew/foo/pull/9\",\"state\":\"open\"}]}' ;;\n"
        "  */search/repositories*) printf '%s' '{\"total_count\":2,\"items\":[]}' ;;\n"
        "  */user*) printf '%s' '{\"login\":\"tester\"}' ;;\n"
        "  *) printf '%s' '{}' ;;\n"
        'esac\n'
    )
    fake_curl.chmod(0o755)
    formula = _formula(
        "ruby-github-ext",
        "1.0",
        (
            '  ENV["PYBREW_GITHUB_CURL"] = "' + str(fake_curl) + '"\n'
            '  prs = Utils::GitHub.fetch_pull_requests("foo", "homebrew/foo", state: "open")\n'
            '  open_prs = Utils::GitHub.fetch_open_pull_requests("foo", "homebrew/foo")\n'
            '  commits = Utils::GitHub.pull_request_commits("homebrew", "foo", 7)\n'
            '  labels = Utils::GitHub.pull_request_labels("homebrew", "foo", 7)\n'
            '  files = Utils::GitHub.pull_request_changed_files("homebrew/foo", 7)\n'
            '  reviews = Utils::GitHub.repository_approved_reviews("homebrew", "foo", 7)\n'
            '  members = Utils::GitHub.members_by_team("homebrew", "core")\n'
            '  org_repos = Utils::GitHub.organisation_repositories("homebrew")\n'
            '  sponsors = Utils::GitHub.sponsorships("alice")\n'
            '  tiers = Utils::GitHub.sponsorship_tiers("alice")\n'
            '  fork_true = Utils::GitHub.fork_exists?("homebrew/bar", org: "homebrew")\n'
            '  fork_false = Utils::GitHub.fork_exists?("homebrew/nothing", org: "homebrew")\n'
            '  regex = Utils::GitHub.pull_request_title_regex("foo", "1.2.3")\n'
            '  dupes = Utils::GitHub.check_for_duplicate_pull_requests("foo", "homebrew/foo")\n'
            '  repo_search = Utils::GitHub.search("repositories", "foo bar")\n'
            '  short_commits = Utils::GitHub.multiple_short_commits_exist?("homebrew", "foo", "abc")\n'
            '  too_many = Utils::GitHub.too_many_open_prs?("homebrew/foo")\n'
            '  printed = Utils::GitHub.print_pull_requests_matching("foo")\n'
            '  write_errors = []\n'
            '  %w[create_fork upload_release_asset workflow_dispatch_event].each do |name|\n'
            '    begin\n'
            '      Utils::GitHub.public_send(name, "homebrew/foo")\n'
            '      write_errors << "none"\n'
            '    rescue NotImplementedError => error\n'
            '      write_errors << error.class.name\n'
            '    end\n'
            '  end\n'
            '  (prefix/"github-ext.txt").write [\n'
            '    prs.size.to_s, open_prs.first["number"].to_s, commits.first["sha"],\n'
            '    labels.first["name"], files.first["filename"], reviews.first["login"],\n'
            '    members.first["login"], org_repos.first["name"], sponsors.first["login"],\n'
            '    tiers.first["monthly_price_in_cents"].to_s, fork_true.to_s, fork_false.to_s,\n'
            '    regex.match?("foo: update 1.2.3").to_s, dupes.size.to_s,\n'
            '    repo_search["total_count"].to_s, short_commits.to_s, too_many.to_s,\n'
            '    printed.size.to_s, write_errors.join(",")\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "github-ext.txt").read_text().splitlines() == [
        "1",
        "9",
        "abc",
        "bump",
        "Formula/foo.rb",
        "rev",
        "alice",
        "foo",
        "sponsor",
        "500",
        "true",
        "false",
        "true",
        "1",
        "2",
        "true",
        "true",
        "1",
        "NotImplementedError,NotImplementedError,NotImplementedError",
    ]


def test_formulary_struct_json_hash_and_malformed_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-json-struct",
        "1.0",
        (
            '  json_struct = JSON.parse(%q({"name":"json-fixture",\n'
            '    "full_name":"homebrew/core/json-fixture","desc":"json desc",\n'
            '    "homepage":"https://json.example","version":"9.9",\n'
            '    "license":{"default":"MIT"},"dependencies":{"dep":"build"}}))\n'
            '  klass = Formulary.load_formula_from_struct!("json-fixture", json_struct)\n'
            '  instance = klass.new\n'
            '  defined_full = Formulary.formula_class_defined_from_api?("homebrew/core/json-fixture").to_s\n'
            '  begin\n'
            '    Formulary.load_formula_from_struct!("bad-json", {"desc" => "no version"})\n'
            '    bad_error = "none"\n'
            '  rescue => error\n'
            '    bad_error = error.class.name + ":" + error.message.include?("malformed").to_s\n'
            '  end\n'
            '  (prefix/"json-struct.txt").write [\n'
            '    klass.loaded_desc, klass.loaded_version, klass.loaded_license,\n'
            '    instance.dependencies.map(&:name).join(","),\n'
            '    instance.dependencies.first.build?.to_s,\n'
            '    defined_full, bad_error\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "json-struct.txt").read_text().splitlines() == [
        "json desc",
        "9.9",
        "MIT",
        "dep",
        "true",
        "true",
        "PybrewFormulaUnavailableError:true",
    ]


def test_formulary_api_json_loading_and_factory_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    api_path = tmp_path / "formula.jws.json"
    api_path.write_text(
        json.dumps(
            {
                "formulae": [
                    {
                        "name": "json-one",
                        "full_name": "homebrew/core/json-one",
                        "desc": "json one",
                        "homepage": "https://example.com/json-one",
                        "version": "1.5",
                        "license": "MIT",
                        "dependencies": {"dep": ["build"]},
                    },
                    {
                        "name": "json-two",
                        "desc": "json two",
                        "homepage": "https://example.com/json-two",
                        "version": "2.0",
                        "license": {"default": "Apache-2.0"},
                    },
                ]
            }
        )
    )
    monkeypatch.setenv("PYBREW_FORMULA_API_JSON", str(api_path))
    formula = _formula(
        "ruby-api-json",
        "1.0",
        (
            '  api_path = Pathname.new(ENV["PYBREW_FORMULA_API_JSON"])\n'
            '  loaded = Formulary.load_formula_from_api!\n'
            '  factory_one = Formulary.factory("json-one", from: :api)\n'
            '  one = Formulary.formula_class_get_from_api("json-one")\n'
            '  factory_full = Formulary.factory("homebrew/core/json-one", from: :api)\n'
            '  inline = Formulary.load_formula_from_api!({\n'
            '    "name" => "inline-one", "desc" => "inline desc",\n'
            '    "homepage" => "https://inline.example", "version" => "3.0"\n'
            '  })\n'
            '  inline_cached = Formulary.formula_class_get_from_api("inline-one")\n'
            '  malformed_error = begin\n'
            '    Formulary.load_formula_from_api!({"desc" => "no version"})\n'
            '    "none"\n'
            '  rescue => error\n'
            '    error.class.name + "|" + error.message.include?("malformed").to_s\n'
            '  end\n'
            '  missing_error = begin\n'
            '    Formulary.load_formula_from_api!(path: api_path.dirname/"missing.json")\n'
            '    "none"\n'
            '  rescue => error\n'
            '    error.class.name + "|" + error.message.include?("not found").to_s\n'
            '  end\n'
            '  missing_factory_error = begin\n'
            '    Formulary.factory("not-in-api", from: :api)\n'
            '    "none"\n'
            '  rescue => error\n'
            '    error.class.name + "|" + error.message.include?("API").to_s\n'
            '  end\n'
            '  api_path.write(JSON.generate({"formulae" => [{\n'
            '    "name" => "json-one", "full_name" => "homebrew/core/json-one",\n'
            '    "desc" => "json one v2", "homepage" => "https://example.com/json-one",\n'
            '    "version" => "9.9", "license" => "MIT"\n'
            '  }]}))\n'
            '  File.utime(Time.now + 10, Time.now + 10, api_path.to_s)\n'
            '  Formulary.load_formula_from_api!\n'
            '  stale_version = Formulary.formula_class_get_from_api("json-one").loaded_version\n'
            '  (prefix/"api-json.txt").write [\n'
            '    loaded.is_a?(Array).to_s, loaded.length.to_s,\n'
            '    one.loaded_desc, one.loaded_version, one.loaded_license,\n'
            '    one.new.dependencies.map(&:name).join(","),\n'
            '    factory_one.equal?(one).to_s, factory_full.equal?(one).to_s,\n'
            '    inline_cached.loaded_desc, inline_cached.loaded_version,\n'
            '    malformed_error, missing_error, missing_factory_error, stale_version\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "api-json.txt").read_text().splitlines() == [
        "true",
        "2",
        "json one",
        "1.5",
        "MIT",
        "dep",
        "true",
        "true",
        "inline desc",
        "3.0",
        "PybrewFormulaUnavailableError|true",
        "PybrewFormulaUnavailableError|true",
        "PybrewFormulaUnavailableError|true",
        "9.9",
    ]


def test_formulary_tap_alias_directory_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    tap_root = tmp_path / "taps" / "homebrew" / "testtap"
    (tap_root / "Formula").mkdir(parents=True)
    (tap_root / "Aliases").mkdir(parents=True)
    (tap_root / "Formula" / "real-formula.rb").write_text(
        'class RealFormula < Formula\n'
        '  desc "real formula"\n'
        '  version "1.0"\n'
        'end\n'
    )
    os.symlink("../Formula/real-formula.rb", tap_root / "Aliases" / "literal-alias.rb")
    os.symlink("../Formula/real-formula.rb", tap_root / "Aliases" / "qualified-alias.rb")
    monkeypatch.setenv("PYBREW_TAPS", str(tap_root))
    formula = _formula(
        "ruby-tap-alias",
        "1.0",
        (
            '  literal = Formulary.tap_alias_path("literal-alias")\n'
            '  qualified = Formulary.tap_alias_path("homebrew/testtap/qualified-alias")\n'
            '  klass = Formulary.resolve_tap_alias!("literal-alias")\n'
            '  factory_klass = Formulary.factory("qualified-alias")\n'
            '  metadata_klass = Formulary.factory("metadata-alias")\n'
            '  unknown_error = begin\n'
            '    Formulary.resolve_tap_alias!("missing-alias")\n'
            '    "none"\n'
            '  rescue => error\n'
            '    error.class.name\n'
            '  end\n'
            '  (prefix/"tap-alias.txt").write [\n'
            '    literal.basename.to_s, qualified.basename.to_s,\n'
            '    klass.loaded_name, klass.loaded_desc,\n'
            '    factory_klass.loaded_name,\n'
            '    metadata_klass.name,\n'
            '    Formulary.tap_aliases.sort.join(","), unknown_error\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.aliases = ["metadata-alias"]
    keg = _build(tmp_path, formula)
    assert (keg / "tap-alias.txt").read_text().splitlines() == [
        "literal-alias.rb",
        "qualified-alias.rb",
        "real-formula",
        "real formula",
        "real-formula",
        "ruby-tap-alias",
        "literal-alias,qualified-alias",
        "PybrewFormulaUnavailableError",
    ]


def _has_platform_sandbox() -> bool:
    return Path("/usr/bin/sandbox-exec").exists() or shutil.which("bwrap") is not None


def test_sandbox_backend_detection_fake_and_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    fake_sandbox_exec = tmp_path / "sandbox-exec"
    fake_sandbox_exec.write_text("#!/bin/sh\nprintf 'exec:%s\\n' \"$*\"\n")
    fake_sandbox_exec.chmod(0o755)
    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text("#!/bin/sh\nprintf 'bwrap:%s\\n' \"$*\"\n")
    fake_bwrap.chmod(0o755)
    missing = tmp_path / "missing-sandbox"
    formula = _formula(
        "ruby-sandbox-backend",
        "1.0",
        (
            '  ENV["PYBREW_SANDBOX"] = "1"\n'
            '  ENV["PYBREW_SANDBOX_PATH"] = "' + str(fake_sandbox_exec) + '"\n'
            '  backend_exec = Sandbox.backend.to_s\n'
            '  exec_name = Sandbox.executable_name\n'
            '  wrapped_exec = Sandbox.wrapped_command("echo", "hi")\n'
            '  output_exec = Sandbox.run("echo", "hi")\n'
            '  ENV["PYBREW_SANDBOX_PATH"] = "' + str(fake_bwrap) + '"\n'
            '  backend_bwrap = Sandbox.backend.to_s\n'
            '  wrapped_bwrap = Sandbox.wrapped_command("echo", "hi")\n'
            '  output_bwrap = Sandbox.run("echo", "hi")\n'
            '  ENV["PYBREW_SANDBOX_PATH"] = "' + str(missing) + '"\n'
            '  unavailable = Sandbox.available?.to_s\n'
            '  error = begin\n'
            '    Sandbox.run("echo", "hi")\n'
            '    "none"\n'
            '  rescue NotImplementedError => raised\n'
            '    raised.class.name + "|" + raised.message.include?("no sandbox backend").to_s\n'
            '  end\n'
            '  ENV.delete("PYBREW_SANDBOX")\n'
            '  default = Sandbox.available?.to_s + "|" + Sandbox.run("echo", "hi").inspect\n'
            '  (prefix/"sandbox-backend.txt").write [\n'
            '    backend_exec, exec_name, wrapped_exec[1], wrapped_exec[2],\n'
            '    output_exec.to_s.strip, backend_bwrap,\n'
            '    wrapped_bwrap.include?("--ro-bind").to_s, output_bwrap.to_s.strip,\n'
            '    unavailable, error, default\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "sandbox-backend.txt").read_text().splitlines() == [
        "sandbox_exec",
        "sandbox-exec",
        "-p",
        "(version 1)(allow default)",
        "exec:-p (version 1)(allow default) echo hi",
        "bwrap",
        "true",
        "bwrap:--ro-bind / / --dev /dev --proc /proc --unshare-net -- echo hi",
        "false",
        "NotImplementedError|true",
        "false|nil",
    ]


@pytest.mark.skipif(not _has_platform_sandbox(), reason="no platform sandbox backend")
def test_sandbox_real_platform_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-sandbox-real",
        "1.0",
        (
            '  ENV["PYBREW_SANDBOX"] = "1"\n'
            '  ENV.delete("PYBREW_SANDBOX_BINARY")\n'
            '  ENV.delete("PYBREW_SANDBOX_PATH")\n'
            '  available = Sandbox.available?\n'
            '  output = available ? Sandbox.run("echo", "hello") : nil\n'
            '  (prefix/"sandbox-real.txt").write [\n'
            '    available.to_s, Sandbox.backend.to_s, output.to_s.strip\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "sandbox-real.txt").read_text().splitlines()
    assert lines[0] == "true"
    assert lines[1] in ("sandbox_exec", "bwrap")
    assert lines[2] == "hello"


def test_tab_full_fields_and_json_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-tab-full",
        "1.0",
        (
            '  receipt = prefix/"INSTALL_RECEIPT.json"\n'
            '  tab = Tab.new({}, receipt)\n'
            '  tab.homebrew_version = "4.6.0"\n'
            '  tab.installed_on_request = true\n'
            '  tab.installed_as_dependency = false\n'
            '  tab.time = 1_700_000_000\n'
            '  tab.source_modified_time = 1_650_000_000\n'
            '  tab.source = {"path" => "/tmp/foo.rb", "tap" => "homebrew/core",\n'
            '                "tap_git_head" => "abc123"}\n'
            '  tab.built_on = {"os" => "macOS", "os_version" => "14.5", "arch" => "arm64"}\n'
            '  tab.sbom = {"spdxfile" => "foo.spdx.json", "purposes" => ["source"]}\n'
            '  tab.version_scheme = 2\n'
            '  tab.built_as_bottle = true\n'
            '  tab.poured_from_bottle = false\n'
            '  tab.compiler = "clang"\n'
            '  tab.changed_files = ["bin/foo", "lib/foo.dylib"]\n'
            '  tab.used_options = ["--with-x"]\n'
            '  tab.unused_options = ["--without-y"]\n'
            '  tab.runtime_dependencies = ["dep-one", "dep-two"]\n'
            '  tab.arch = :arm64\n'
            '  tab.head = false\n'
            '  written = tab.write\n'
            '  reread = Tab.for_keg(prefix)\n'
            '  round_trip = Tab.from_file_content(reread.to_json)\n'
            '  created = Tab.create(Formula["ruby-tab-full"])\n'
            '  (prefix/"tab-full.txt").write [\n'
            '    written.to_s, reread.homebrew_version,\n'
            '    reread.installed_on_request?.to_s, reread.installed_as_dependency?.to_s,\n'
            '    reread.time.to_s, reread.source_modified_time.to_s,\n'
            '    reread.source["tap"], reread.source["tap_git_head"],\n'
            '    reread.built_on["os"], reread.sbom["spdxfile"],\n'
            '    reread.version_scheme.to_s, reread.built_as_bottle?.to_s,\n'
            '    reread.poured_from_bottle?.to_s, reread.compiler,\n'
            '    reread.changed_files.join(","), reread.used_options.join(","),\n'
            '    reread.unused_options.join(","), reread.runtime_dependencies.join(","),\n'
            '    reread.arch.to_s, reread.head?.to_s,\n'
            '    round_trip.changed_files.join(","), round_trip.sbom["purposes"].join(","),\n'
            '    created.homebrew_version, created.source["tap"].to_s,\n'
            '    created.runtime_dependencies.empty?.to_s, created.time.is_a?(Integer).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.tap = "homebrew/core"
    keg = _build(tmp_path, formula)
    assert (keg / "tab-full.txt").read_text().splitlines() == [
        "true",
        "4.6.0",
        "true",
        "false",
        "1700000000",
        "1650000000",
        "homebrew/core",
        "abc123",
        "macOS",
        "foo.spdx.json",
        "2",
        "true",
        "false",
        "clang",
        "bin/foo,lib/foo.dylib",
        "--with-x",
        "--without-y",
        "dep-one,dep-two",
        "arm64",
        "false",
        "bin/foo,lib/foo.dylib",
        "source",
        "0.0.0",
        "homebrew/core",
        "true",
        "true",
    ]


def test_keg_remove_old_aliases_and_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    keg = _run_raw_body(
        tmp_path,
        """
keg_path = prefix
(keg_path/"bin").mkpath
(keg_path/"INSTALL_RECEIPT.json").write JSON.generate({"aliases" => ["keg-alias-old"], "version_scheme" => 2})
keg = Keg.new(keg_path)
prefix_path = keg.homebrew_prefix
opt = prefix_path/"opt"
linked = prefix_path/"var/homebrew/linked"
opt.mkpath
linked.mkpath
(opt/"keg-alias-old").make_symlink(keg_path) unless (opt/"keg-alias-old").symlink?
(linked/"keg-alias-old").make_symlink(keg_path) unless (linked/"keg-alias-old").symlink?
(opt/"#{keg.name}@1").make_symlink(keg_path) unless (opt/"#{keg.name}@1").symlink?
(linked/"#{keg.name}@1").make_symlink(keg_path) unless (linked/"#{keg.name}@1").symlink?
before = [(opt/"keg-alias-old").symlink?, (linked/"keg-alias-old").symlink?,
          (opt/"#{keg.name}@1").symlink?, (linked/"#{keg.name}@1").symlink?]
result = keg.remove_old_aliases
after = [(opt/"keg-alias-old").symlink?, (linked/"keg-alias-old").symlink?,
         (opt/"#{keg.name}@1").symlink?, (linked/"#{keg.name}@1").symlink?]
(prefix/"oldaliases.txt").write [
  before.map(&:to_s).join(","), result.inspect, after.map(&:to_s).join(","),
  keg.remove_alias_symlink("keg-alias-old").to_s
].join("\\n")
""",
        name="ruby-keg-oldaliases",
    )
    assert (keg / "oldaliases.txt").read_text().splitlines() == [
        "true,true,true,true",
        "nil",
        "false,false,false,false",
        "false",
    ]


def test_sandbox_rule_collection_and_profile_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    fake_sandbox_exec = tmp_path / "sandbox-exec"
    fake_sandbox_exec.write_text("#!/bin/sh\nexit 0\n")
    fake_sandbox_exec.chmod(0o755)
    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text("#!/bin/sh\nexit 0\n")
    fake_bwrap.chmod(0o755)
    formula = _formula(
        "ruby-sandbox-rules",
        "1.0",
        (
            '  ENV["PYBREW_SANDBOX"] = "1"\n'
            '  ENV["PYBREW_SANDBOX_PATH"] = "' + str(fake_sandbox_exec) + '"\n'
            '  disabled_like = begin\n'
            '    saved = ENV.delete("PYBREW_SANDBOX")\n'
            '    Sandbox.allow_read("/tmp/x")\n'
            '    "none"\n'
            '  rescue NotImplementedError\n'
            '    "default-disabled"\n'
            '  ensure\n'
            '    ENV["PYBREW_SANDBOX"] = "1"\n'
            '  end\n'
            '  Sandbox.allow_read("/tmp/allowed")\n'
            '  Sandbox.allow_write("/tmp/out")\n'
            '  Sandbox.allow_network\n'
            '  Sandbox.deny_read("/secret")\n'
            '  Sandbox.deny_write("/System")\n'
            '  Sandbox.add_rule("(allow process-exec)")\n'
            '  kinds = Sandbox.rules.map { |rule| rule[:kind].to_s }.join(",")\n'
            '  profile = Sandbox.sandbox_profile\n'
            '  applied = Sandbox.apply!\n'
            '  stored_profile = Sandbox.applied_policy["profile"].to_s\n'
            '  ENV["PYBREW_SANDBOX_PATH"] = "' + str(fake_bwrap) + '"\n'
            '  Sandbox.reset_rules!\n'
            '  Sandbox.allow_write("/tmp/out")\n'
            '  bwrap_args = Sandbox.bwrap_policy_args.join(",")\n'
            '  applied_bwrap = Sandbox.apply!\n'
            '  applied_bwrap_args = Sandbox.applied_policy["args"].join(",")\n'
            '  ENV.delete("PYBREW_SANDBOX")\n'
            '  disabled_apply = begin\n'
            '    Sandbox.apply!\n'
            '    "none"\n'
            '  rescue NotImplementedError\n'
            '    "default-disabled"\n'
            '  end\n'
            '  (prefix/"sandbox-rules.txt").write [\n'
            '    disabled_like, kinds,\n'
            '    profile.include?("(subpath \\"/tmp/allowed\\")").to_s,\n'
            '    profile.include?("(deny file-read* (subpath \\"/secret\\"))").to_s,\n'
            '    profile.include?("(allow network*)").to_s,\n'
            '    profile.include?("(allow process-exec)").to_s,\n'
            '    applied.to_s, stored_profile.include?("(version 1)").to_s,\n'
            '    bwrap_args.include?("--bind,/tmp/out,/tmp/out").to_s,\n'
            '    bwrap_args.include?("--unshare-net").to_s,\n'
            '    applied_bwrap.to_s,\n'
            '    applied_bwrap_args.include?("--bind,/tmp/out,/tmp/out").to_s,\n'
            '    disabled_apply\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "sandbox-rules.txt").read_text().splitlines() == [
        "default-disabled",
        "allow_read,allow_write,allow_network,deny_read,deny_write,raw",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "default-disabled",
    ]


def test_formulary_remote_api_json_fake_curl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    good_curl = tmp_path / "good-curl"
    good_curl.write_text(
        "#!/bin/sh\n"
        "printf '%s' '{\"formulae\":[{\"name\":\"remote-one\",\"full_name\":\"homebrew/core/remote-one\","
        "\"desc\":\"remote formula\",\"homepage\":\"https://example.com/remote-one\","
        "\"version\":\"2.0\",\"license\":\"MIT\"}]}'\n"
    )
    good_curl.chmod(0o755)
    bad_curl = tmp_path / "bad-curl"
    bad_curl.write_text("#!/bin/sh\nprintf '%s' 'not json'\n")
    bad_curl.chmod(0o755)
    failing_curl = tmp_path / "failing-curl"
    failing_curl.write_text("#!/bin/sh\nexit 1\n")
    failing_curl.chmod(0o755)
    monkeypatch.setenv("PYBREW_FORMULA_API_URL", "https://api.example.test/formula.jws.json")
    monkeypatch.setenv("PYBREW_FORMULA_API_CURL", str(good_curl))
    formula = _formula(
        "ruby-remote-api",
        "1.0",
        (
            '  good_curl = ENV["PYBREW_FORMULA_API_CURL"]\n'
            '  loaded = Formulary.load_formula_from_api!\n'
            '  loaded_class = loaded.is_a?(Array) ? loaded.first : loaded\n'
            '  factory = Formulary.factory("remote-one", from: :api)\n'
            '  cached = Formulary.formula_class_get_from_api("remote-one")\n'
            '  inline = Formulary.load_formula_from_api!({\n'
            '    "name" => "inline-remote", "desc" => "inline desc", "version" => "1.0"\n'
            '  })\n'
            '  ENV["PYBREW_FORMULA_API_CURL"] = "' + str(bad_curl) + '"\n'
            '  Formulary.clear_cache\n'
            '  malformed = begin\n'
            '    Formulary.load_formula_from_api!\n'
            '    "none"\n'
            '  rescue => error\n'
            '    error.class.name + "|" + error.message.include?("malformed API JSON from").to_s\n'
            '  end\n'
            '  ENV["PYBREW_FORMULA_API_CURL"] = "' + str(failing_curl) + '"\n'
            '  Formulary.clear_cache\n'
            '  fetch_failed = begin\n'
            '    Formulary.load_formula_from_api!\n'
            '    "none"\n'
            '  rescue => error\n'
            '    error.class.name + "|" + error.message.include?("failed to fetch").to_s\n'
            '  end\n'
            '  ENV["PYBREW_FORMULA_API_CURL"] = good_curl\n'
            '  Formulary.clear_cache\n'
            '  factory_after_clear = Formulary.factory("remote-one", from: :api)\n'
            '  (prefix/"remote-api.txt").write [\n'
            '    loaded_class.is_a?(Class).to_s, loaded_class.loaded_desc,\n'
            '    factory.equal?(cached).to_s, cached.loaded_version,\n'
            '    inline.loaded_desc, malformed, fetch_failed,\n'
            '    factory_after_clear.loaded_version\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "remote-api.txt").read_text().splitlines() == [
        "true",
        "remote formula",
        "true",
        "2.0",
        "inline desc",
        "PybrewFormulaUnavailableError|true",
        "PybrewFormulaUnavailableError|true",
        "2.0",
    ]


def test_utils_github_graphql_transport_and_mappings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    monkeypatch.setenv("PYBREW_GITHUB_TOKEN", "secret-token")
    argv_file = tmp_path / "graphql-argv.txt"
    fake_curl = tmp_path / "fake-graphql-curl"
    script = """#!/bin/sh
printf '%s\\n' "$@" > "__ARGV__"
url=""
payload=""
prev=""
for arg in "$@"; do
  case "$arg" in
    http*) url="$arg" ;;
  esac
  if [ "$prev" = "--data" ]; then payload="$arg"; fi
  prev="$arg"
done
case "$url" in
  *api.github.com/graphql*)
    case "$payload" in
      *defaultBranchRef*) printf '%s' '{"data":{"repository":{"name":"foo","description":"desc","homepageUrl":"https://foo.example","isPrivate":true,"licenseInfo":{"spdxId":"MIT"},"defaultBranchRef":{"name":"main"},"stargazerCount":5,"forkCount":2}}}' ;;
      *pullRequests*) printf '%s' '{"data":{"repository":{"pullRequests":{"nodes":[{"number":7,"title":"foo: update","url":"https://github.com/homebrew/foo/pull/7","state":"OPEN"}]}}}}' ;;
      *commits*) printf '%s' '{"data":{"repository":{"pullRequest":{"commits":{"nodes":[{"oid":"abc","messageHeadline":"fix foo"}]}}}}}' ;;
      *labels*) printf '%s' '{"data":{"repository":{"pullRequest":{"labels":{"nodes":[{"name":"bump"}]}}}}}' ;;
      *files*) printf '%s' '{"data":{"repository":{"pullRequest":{"files":{"nodes":[{"path":"Formula/foo.rb"}]}}}}}' ;;
      *reviews*) printf '%s' '{"data":{"repository":{"pullRequest":{"reviews":{"nodes":[{"state":"APPROVED","authorAssociation":"MEMBER","author":{"login":"rev"},"commit":{"oid":"abc"}}]}}}}}' ;;
      *members*) printf '%s' '{"data":{"organization":{"team":{"members":{"nodes":[{"login":"alice"}]}}}}}' ;;
      *sponsorsListing*) printf '%s' '{"data":{"user":{"sponsorsListing":{"tiers":{"nodes":[{"name":"Gold","monthlyPriceInCents":500}]}}}}}' ;;
      *) printf '%s' '{"data":{}}' ;;
    esac ;;
  *) printf '%s' '{}' ;;
esac
"""
    fake_curl.write_text(script.replace("__ARGV__", str(argv_file)))
    fake_curl.chmod(0o755)
    errors_curl = tmp_path / "errors-graphql-curl"
    errors_curl.write_text("#!/bin/sh\nprintf '%s' '{\"errors\":[{\"message\":\"boom\"}]}'\n")
    errors_curl.chmod(0o755)
    bad_curl = tmp_path / "bad-graphql-curl"
    bad_curl.write_text("#!/bin/sh\nprintf '%s' 'not json'\n")
    bad_curl.chmod(0o755)
    nodata_curl = tmp_path / "nodata-graphql-curl"
    nodata_curl.write_text("#!/bin/sh\nprintf '%s' '{\"data\":{}}'\n")
    nodata_curl.chmod(0o755)
    monkeypatch.setenv("PYBREW_GITHUB_CURL", str(fake_curl))
    formula = _formula(
        "ruby-github-graphql",
        "1.0",
        (
            '  gql_repo = Utils::GitHub.graphql_repository("homebrew", "foo")\n'
            '  gql_prs = Utils::GitHub.graphql_fetch_pull_requests("foo", "homebrew/foo", state: "open")\n'
            '  gql_commits = Utils::GitHub.graphql_pull_request_commits("homebrew", "foo", 7)\n'
            '  gql_labels = Utils::GitHub.graphql_pull_request_labels("homebrew", "foo", 7)\n'
            '  gql_files = Utils::GitHub.graphql_pull_request_changed_files("homebrew/foo", 7)\n'
            '  gql_reviews = Utils::GitHub.graphql_repository_approved_reviews("homebrew", "foo", 7, commit: "abc")\n'
            '  gql_members = Utils::GitHub.graphql_members_by_team("homebrew", "core")\n'
            '  gql_tiers = Utils::GitHub.graphql_sponsorship_tiers("alice")\n'
            '  repo = Utils::GitHub.repository("homebrew", "foo")\n'
            '  prs = Utils::GitHub.fetch_pull_requests("foo", "homebrew/foo", state: "open")\n'
            '  commits = Utils::GitHub.pull_request_commits("homebrew", "foo", 7)\n'
            '  labels = Utils::GitHub.pull_request_labels("homebrew", "foo", 7)\n'
            '  files = Utils::GitHub.pull_request_changed_files("homebrew/foo", 7)\n'
            '  reviews = Utils::GitHub.repository_approved_reviews("homebrew", "foo", 7, commit: "abc")\n'
            '  members = Utils::GitHub.members_by_team("homebrew", "core")\n'
            '  tiers = Utils::GitHub.sponsorship_tiers("alice")\n'
            '  argv = Pathname.new(ENV["PYBREW_GRAPHQL_ARGV"]).read.lines.map(&:chomp)\n'
            '  ENV["PYBREW_GITHUB_CURL"] = "' + str(errors_curl) + '"\n'
            '  error = begin\n'
            '    Utils::GitHub.graphql_data("query { viewer { login } }")\n'
            '    "none"\n'
            '  rescue => raised\n'
            '    raised.class.name + "|" + raised.message.include?("GraphQL error").to_s\n'
            '  end\n'
            '  ENV["PYBREW_GITHUB_CURL"] = "' + str(bad_curl) + '"\n'
            '  malformed = begin\n'
            '    Utils::GitHub.graphql_data("query { viewer { login } }")\n'
            '    "none"\n'
            '  rescue => raised\n'
            '    raised.class.name + "|" + raised.message.include?("malformed GraphQL JSON").to_s\n'
            '  end\n'
            '  ENV["PYBREW_GITHUB_CURL"] = "' + str(nodata_curl) + '"\n'
            '  nodata = begin\n'
            '    Utils::GitHub.graphql_data("query { viewer { login } }")\n'
            '    "none"\n'
            '  rescue => raised\n'
            '    raised.class.name + "|" + raised.message.include?("no data").to_s\n'
            '  end\n'
            '  ENV["PYBREW_GITHUB_CURL"] = "' + str(fake_curl) + '"\n'
            '  (prefix/"graphql.txt").write [\n'
            '    gql_repo["full_name"], gql_repo["description"], gql_repo["private"].to_s,\n'
            '    gql_repo["license"]["spdx_id"], gql_repo["default_branch"],\n'
            '    gql_repo["stargazers_count"].to_s, gql_repo["forks_count"].to_s,\n'
            '    gql_prs.first["number"].to_s, gql_prs.first["html_url"], gql_prs.first["state"],\n'
            '    gql_commits.first["sha"], gql_labels.first["name"], gql_files.first["filename"],\n'
            '    gql_reviews.first["login"], gql_members.first["login"],\n'
            '    gql_tiers.first["monthly_price_in_cents"].to_s,\n'
            '    repo["description"], repo["private"].to_s,\n'
            '    prs.first["state"], commits.first["sha"], labels.first["name"],\n'
            '    files.first["filename"], reviews.first["login"], members.first["login"],\n'
            '    tiers.first["monthly_price_in_cents"].to_s,\n'
            '    argv.include?("-X").to_s, argv.include?("POST").to_s,\n'
            '    argv.include?("Content-Type: application/json").to_s,\n'
            '    argv.include?("Authorization: bearer secret-token").to_s,\n'
            '    argv.include?("https://api.github.com/graphql").to_s,\n'
            '    error, malformed, nodata\n'
            '  ].join("\\n")\n'
        ),
    )
    monkeypatch.setenv("PYBREW_GRAPHQL_ARGV", str(argv_file))
    keg = _build(tmp_path, formula)
    assert (keg / "graphql.txt").read_text().splitlines() == [
        "homebrew/foo",
        "desc",
        "true",
        "MIT",
        "main",
        "5",
        "2",
        "7",
        "https://github.com/homebrew/foo/pull/7",
        "open",
        "abc",
        "bump",
        "Formula/foo.rb",
        "rev",
        "alice",
        "500",
        "desc",
        "true",
        "open",
        "abc",
        "bump",
        "Formula/foo.rb",
        "rev",
        "alice",
        "500",
        "true",
        "true",
        "true",
        "true",
        "true",
        "PybrewGitHubGraphqlError|true",
        "PybrewGitHubGraphqlError|true",
        "PybrewGitHubGraphqlError|true",
    ]


def test_formulary_remote_api_conditional_cache_and_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    argv_file = tmp_path / "conditional-argv.txt"
    fake_curl = tmp_path / "conditional-curl"
    script = """#!/bin/sh
printf '%s\\n' "$@" > "__ARGV__"
url=""
headers=""
prev=""
for arg in "$@"; do
  case "$arg" in
    http*) url="$arg" ;;
  esac
  if [ "$prev" = "-D" ]; then headers="$arg"; fi
  prev="$arg"
done
if printf '%s\\n' "$@" | grep -q 'If-None-Match'; then
  printf 'HTTP/1.1 304 Not Modified\\r\\n\\r\\n' > "$headers"
  exit 0
fi
printf 'HTTP/1.1 200 OK\\r\\nETag: "v1"\\r\\nLast-Modified: Wed, 01 Jan 2025 00:00:00 GMT\\r\\n\\r\\n' > "$headers"
printf '%s' '{"formulae":[{"name":"cond-one","desc":"conditional","version":"1.0"}]}'
"""
    fake_curl.write_text(script.replace("__ARGV__", str(argv_file)))
    fake_curl.chmod(0o755)
    failing_curl = tmp_path / "conditional-failing-curl"
    failing_curl.write_text("#!/bin/sh\nexit 1\n")
    failing_curl.chmod(0o755)
    sidecar = tmp_path / "api-sidecar.json"
    monkeypatch.setenv("PYBREW_FORMULA_API_URL", "https://api.example.test/formula.jws.json")
    monkeypatch.setenv("PYBREW_FORMULA_API_CURL", str(fake_curl))
    monkeypatch.setenv("PYBREW_FORMULA_API_TOKEN", "secret-token")
    monkeypatch.setenv("PYBREW_FORMULA_API_SIDECAR", str(sidecar))
    monkeypatch.setenv("PYBREW_CONDITIONAL_ARGV", str(argv_file))
    formula = _formula(
        "ruby-conditional-api",
        "1.0",
        (
            '  first = Formulary.remote_api_json_entries\n'
            '  sidecar_text = Pathname.new(ENV["PYBREW_FORMULA_API_SIDECAR"]).read\n'
            '  second = Formulary.remote_api_json_entries\n'
            '  argv = Pathname.new(ENV["PYBREW_CONDITIONAL_ARGV"]).read.lines.map(&:chomp)\n'
            '  sidecar = JSON.parse(sidecar_text)\n'
            '  ENV["PYBREW_FORMULA_API_CURL"] = "' + str(failing_curl) + '"\n'
            '  Formulary.clear_cache\n'
            '  sidecar_after_clear = Pathname.new(ENV["PYBREW_FORMULA_API_SIDECAR"]).file?\n'
            '  leak = begin\n'
            '    Formulary.remote_api_json_entries\n'
            '    "none"\n'
            '  rescue => raised\n'
            '    raised.message.include?("secret-token").to_s\n'
            '  end\n'
            '  (prefix/"conditional.txt").write [\n'
            '    first.length.to_s, first.first["name"],\n'
            '    second.length.to_s, second.first["name"],\n'
            '    sidecar["etag"].to_s, sidecar["last_modified"].to_s,\n'
            '    (sidecar["content_hash"].to_s.length == 64).to_s,\n'
            '    sidecar_after_clear.to_s, leak,\n'
            '    argv.include?("Authorization: Bearer secret-token").to_s,\n'
            '    argv.any? { |line| line.include?("If-None-Match: \\"v1\\"") }.to_s,\n'
            '    argv.any? { |line| line.include?("If-Modified-Since: Wed, 01 Jan 2025 00:00:00 GMT") }.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "conditional.txt").read_text().splitlines() == [
        "1",
        "cond-one",
        "1",
        "cond-one",
        '"v1"',
        "Wed, 01 Jan 2025 00:00:00 GMT",
        "true",
        "false",
        "false",
        "true",
        "true",
        "true",
    ]


def test_formulary_api_jws_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    payload_json = json.dumps(
        {
            "formulae": [
                {
                    "name": "jws-one",
                    "desc": "jws formula",
                    "homepage": "https://example.test/jws",
                    "version": "4.0",
                    "license": "MIT",
                }
            ]
        }
    ).encode()
    header = b64url(b'{"alg":"HS256","typ":"JWT"}')
    payload = b64url(payload_json)
    signature = b64url(
        hmac.new(
            b"test-jws-key",
            f"{header}.{payload}".encode(),
            hashlib.sha256,
        ).digest()
    )
    good_token = f"{header}.{payload}.{signature}"
    bad_token = f"{header}.{payload}.{'A' + signature[1:]}"
    rs256_header = b64url(b'{"alg":"RS256","typ":"JWT"}')
    rs256_token = f"{rs256_header}.{payload}.{signature}"

    scripts = {}
    for name, token in (
        ("good", good_token),
        ("bad", bad_token),
        ("rs256", rs256_token),
        ("malformed", "abc.def.ghi"),
    ):
        path = tmp_path / f"jws-curl-{name}"
        path.write_text("#!/bin/sh\nprintf '%s' '" + token + "'\n")
        path.chmod(0o755)
        scripts[name] = path
    monkeypatch.setenv("PYBREW_FORMULA_API_URL", "https://api.example.test/formula.jws.json")
    monkeypatch.setenv("PYBREW_FORMULA_API_CURL", str(scripts["good"]))
    monkeypatch.setenv("PYBREW_FORMULA_API_JWS_KEY", "test-jws-key")
    formula = _formula(
        "ruby-jws-api",
        "1.0",
        (
            '  loaded = Formulary.load_formula_from_api!\n'
            '  key_error = begin\n'
            '    ENV.delete("PYBREW_FORMULA_API_JWS_KEY")\n'
            '    Formulary.clear_cache\n'
            '    Formulary.load_formula_from_api!\n'
            '    "none"\n'
            '  rescue => raised\n'
            '    raised.class.name + "|" + raised.message.include?("PYBREW_FORMULA_API_JWS_KEY").to_s\n'
            '  end\n'
            '  ENV["PYBREW_FORMULA_API_JWS_KEY"] = "wrong-key"\n'
            '  Formulary.clear_cache\n'
            '  wrong_key = begin\n'
            '    Formulary.load_formula_from_api!\n'
            '    "none"\n'
            '  rescue => raised\n'
            '    raised.class.name + "|" + raised.message.include?("verification failed").to_s\n'
            '  end\n'
            '  ENV["PYBREW_FORMULA_API_JWS_KEY"] = "test-jws-key"\n'
            '  ENV["PYBREW_FORMULA_API_CURL"] = "' + str(scripts["rs256"]) + '"\n'
            '  Formulary.clear_cache\n'
            '  unsupported = begin\n'
            '    Formulary.load_formula_from_api!\n'
            '    "none"\n'
            '  rescue => raised\n'
            '    raised.class.name + "|" + raised.message.include?("verification failed").to_s\n'
            '  end\n'
            '  ENV["PYBREW_FORMULA_API_CURL"] = "' + str(scripts["malformed"]) + '"\n'
            '  Formulary.clear_cache\n'
            '  malformed = begin\n'
            '    Formulary.load_formula_from_api!\n'
            '    "none"\n'
            '  rescue => raised\n'
            '    raised.class.name + "|" + raised.message.include?("malformed API JWS").to_s\n'
            '  end\n'
            '  (prefix/"jws.txt").write [\n'
            '    loaded.loaded_name, loaded.loaded_desc, loaded.loaded_version,\n'
            '    key_error, wrong_key, unsupported, malformed\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "jws.txt").read_text().splitlines() == [
        "jws-one",
        "jws formula",
        "4.0",
        "PybrewFormulaUnavailableError|true",
        "PybrewFormulaUnavailableError|true",
        "PybrewFormulaUnavailableError|true",
        "PybrewFormulaUnavailableError|true",
    ]


def test_sandbox_strict_profile_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    fake_sandbox_exec = tmp_path / "sandbox-exec"
    fake_sandbox_exec.write_text("#!/bin/sh\nexit 0\n")
    fake_sandbox_exec.chmod(0o755)
    formula = _formula(
        "ruby-sandbox-strict",
        "1.0",
        (
            '  ENV["PYBREW_SANDBOX"] = "1"\n'
            '  ENV["PYBREW_SANDBOX_PATH"] = "' + str(fake_sandbox_exec) + '"\n'
            '  default_profile = Sandbox.sandbox_profile\n'
            '  default_strict = Sandbox.strict?.to_s\n'
            '  ENV["PYBREW_SANDBOX_STRICT"] = "1"\n'
            '  Sandbox.allow_write("/tmp/out")\n'
            '  strict_profile = Sandbox.sandbox_profile\n'
            '  strict_flag = Sandbox.strict?.to_s\n'
            '  applied = Sandbox.apply!\n'
            '  stored = Sandbox.applied_policy["profile"].to_s\n'
            '  (prefix/"sandbox-strict.txt").write [\n'
            '    default_strict,\n'
            '    default_profile.include?("(allow default)").to_s,\n'
            '    default_profile.include?("(deny default)").to_s,\n'
            '    strict_flag,\n'
            '    strict_profile.include?("(deny default)").to_s,\n'
            '    strict_profile.include?("(allow default)").to_s,\n'
            '    strict_profile.include?("(allow process-exec)").to_s,\n'
            '    strict_profile.include?("(subpath \\"/System\\")").to_s,\n'
            '    strict_profile.include?("(subpath \\"/usr\\")").to_s,\n'
            '    strict_profile.include?("(allow file-write* (subpath \\"/tmp/out\\"))").to_s,\n'
            '    applied.to_s, stored.include?("(deny default)").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "sandbox-strict.txt").read_text().splitlines() == [
        "false",
        "true",
        "false",
        "true",
        "true",
        "false",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
    ]


@pytest.mark.skipif(not _has_platform_sandbox(), reason="no platform sandbox backend")
def test_sandbox_strict_profile_real_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-sandbox-strict-echo",
        "1.0",
        (
            '  ENV["PYBREW_SANDBOX"] = "1"\n'
            '  ENV["PYBREW_SANDBOX_STRICT"] = "1"\n'
            '  ENV.delete("PYBREW_SANDBOX_BINARY")\n'
            '  ENV.delete("PYBREW_SANDBOX_PATH")\n'
            '  available = Sandbox.available?\n'
            '  output = available ? Sandbox.run("echo", "strict-hello") : nil\n'
            '  (prefix/"sandbox-strict-echo.txt").write [\n'
            '    available.to_s, Sandbox.backend.to_s, output.to_s.strip\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "sandbox-strict-echo.txt").read_text().splitlines()
    assert lines[0] == "true"
    assert lines[1] in ("sandbox_exec", "bwrap")
    if len(lines) < 3 or not lines[2]:
        pytest.skip("platform sandbox backend is present but cannot execute echo")
    assert lines[2] == "strict-hello"


def test_formulary_multi_channel_api_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    api_path = tmp_path / "channels.json"
    api_path.write_text(
        json.dumps(
            {
                "stable": {
                    "formulae": [
                        {
                            "name": "multi-one",
                            "desc": "stable desc",
                            "version": "1.0",
                            "homepage": "https://example.test/stable",
                        }
                    ]
                },
                "head": {
                    "formulae": [
                        {
                            "name": "multi-one",
                            "desc": "head desc",
                            "version": "2.0",
                            "homepage": "https://example.test/head",
                        },
                        {
                            "name": "head-only",
                            "desc": "head only",
                            "version": "5.0",
                            "homepage": "https://example.test/head-only",
                        },
                    ]
                },
            }
        )
    )
    monkeypatch.setenv("PYBREW_FORMULA_API_JSON", str(api_path))
    monkeypatch.setenv("PYBREW_FORMULA_API_CHANNELS", "stable,head")
    formula = _formula(
        "ruby-multi-channel",
        "1.0",
        (
            '  channels = Formulary.api_json_channels.join(",")\n'
            '  Formulary.ensure_api_json_loaded\n'
            '  multi = Formulary.formula_class_get_from_api("multi-one")\n'
            '  factory = Formulary.factory("multi-one", from: :api)\n'
            '  head_only = Formulary.formula_class_get_from_api("head-only")\n'
            '  loaded_all = Formulary.load_formula_from_api_channels!\n'
            '  (prefix/"multi-channel.txt").write [\n'
            '    channels, multi.loaded_desc, multi.loaded_version,\n'
            '    factory.equal?(multi).to_s, head_only.loaded_desc,\n'
            '    loaded_all.is_a?(Array).to_s, loaded_all.length.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "multi-channel.txt").read_text().splitlines() == [
        "stable,head",
        "head desc",
        "2.0",
        "true",
        "head only",
        "true",
        "2",
    ]


def test_formulary_tap_source_alias_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    tap_root = tmp_path / "taps" / "homebrew" / "sourcetap"
    (tap_root / "Formula").mkdir(parents=True)
    (tap_root / "Formula" / "source-canonical.rb").write_text(
        'class SourceCanonical < Formula\n'
        '  desc "source alias target"\n'
        '  version "1.0"\n'
        '  formula_alias "source-alias"\n'
        'end\n'
    )
    monkeypatch.setenv("PYBREW_TAPS", str(tap_root))
    formula = _formula(
        "ruby-source-alias",
        "1.0",
        (
            '  target = Formulary.tap_alias_target("source-alias")\n'
            '  klass = Formulary.resolve_tap_alias!("source-alias")\n'
            '  factory = Formulary.factory("source-alias")\n'
            '  aliases = Formulary.tap_aliases\n'
            '  (prefix/"source-alias.txt").write [\n'
            '    target[0], target[1].basename.to_s,\n'
            '    klass.loaded_name, klass.loaded_desc,\n'
            '    (factory.loaded_name == klass.loaded_name).to_s,\n'
            '    aliases.include?("source-alias").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "source-alias.txt").read_text().splitlines() == [
        "source-canonical",
        "source-canonical.rb",
        "source-canonical",
        "source alias target",
        "true",
        "true",
    ]


def test_keg_relocation_paths_and_placeholders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    keg = _run_raw_body(
        tmp_path,
        """
keg_path = prefix
(keg_path/"bin").mkpath
text_file = keg_path/"bin"/"tool"
text_file.write "prefix=#{HOMEBREW_PREFIX}\\nplain\\n"
binary = keg_path/"bin"/"blob.bin"
binary.binwrite "prefix=\\0#{HOMEBREW_PREFIX}"
outside = Pathname.new(Dir.tmpdir)/"pybrew-reloc-outside-#{Process.pid}.txt"
outside.write "prefix=#{HOMEBREW_PREFIX}"
link = keg_path/"bin"/"linked"
link.make_symlink(outside) unless link.symlink?
keg = Keg.new(keg_path)
mapping = { HOMEBREW_PREFIX.to_s => "@@HOMEBREW_PREFIX@@" }
report = keg.relocation_report(mapping)
dry = keg.relocate(mapping, dry_run: true)
dry_text = text_file.read
real = keg.relocate(mapping)
real_text = text_file.read
round_file = keg_path/"bin"/"round"
round_file.write "cellar=#{HOMEBREW_CELLAR}"
keg.relocate_to_placeholders(files: [round_file])
placeholder_text = round_file.read
keg.relocate_from_placeholders(files: [round_file])
restored_text = round_file.read
(prefix/"relocation.txt").write [
  report["files"].length.to_s, report["matches"].to_s,
  dry["dry_run"].to_s, dry_text.include?("@@HOMEBREW_PREFIX@@").to_s,
  real["replacements"].to_s, real_text.include?("@@HOMEBREW_PREFIX@@").to_s,
  real_text.include?("plain").to_s,
  placeholder_text.include?("@@HOMEBREW_CELLAR@@").to_s,
  restored_text.include?(HOMEBREW_CELLAR.to_s).to_s,
  outside.read.include?("@@HOMEBREW_PREFIX@@").to_s,
  outside.delete.to_s
].join("\\n")
""",
        name="ruby-keg-relocation",
    )
    assert (keg / "relocation.txt").read_text().splitlines() == [
        "1",
        "1",
        "true",
        "false",
        "1",
        "true",
        "true",
        "true",
        "true",
        "false",
        "1",
    ]


def test_utils_github_graphql_pagination_rate_limit_enterprise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    paginated_argv = tmp_path / "paginated-argv.txt"
    paginated = tmp_path / "paginated-graphql-curl"
    paginated.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"" + str(paginated_argv) + "\"\n"
        "url=\"\"\n"
        "payload=\"\"\n"
        "prev=\"\"\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in http*) url=\"$arg\" ;; esac\n"
        "  if [ \"$prev\" = \"--data\" ]; then payload=\"$arg\"; fi\n"
        "  prev=\"$arg\"\n"
        "done\n"
        "case \"$payload\" in\n"
        "  *'\"after\":\"c1\"'*) printf '%s' '{\"data\":{\"rateLimit\":{\"remaining\":42,\"resetAt\":\"2026-01-01T00:00:00Z\",\"cost\":1},\"repository\":{\"pullRequests\":{\"pageInfo\":{\"hasNextPage\":false,\"endCursor\":null},\"nodes\":[{\"number\":8,\"title\":\"foo: update two\",\"url\":\"https://github.com/homebrew/foo/pull/8\",\"state\":\"OPEN\"}]}}}}' ;;\n"
        "  *) printf '%s' '{\"data\":{\"rateLimit\":{\"remaining\":42,\"resetAt\":\"2026-01-01T00:00:00Z\",\"cost\":1},\"repository\":{\"pullRequests\":{\"pageInfo\":{\"hasNextPage\":true,\"endCursor\":\"c1\"},\"nodes\":[{\"number\":7,\"title\":\"foo: update one\",\"url\":\"https://github.com/homebrew/foo/pull/7\",\"state\":\"OPEN\"}]}}}}' ;;\n"
        "esac\n"
    )
    paginated.chmod(0o755)
    enterprise_argv = tmp_path / "enterprise-argv.txt"
    enterprise = tmp_path / "enterprise-graphql-curl"
    enterprise.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"" + str(enterprise_argv) + "\"\n"
        "printf '%s' '{\"data\":{\"viewer\":{\"login\":\"octo\"}}}'\n"
    )
    enterprise.chmod(0o755)
    endless = tmp_path / "endless-graphql-curl"
    endless.write_text(
        "#!/bin/sh\n"
        "printf '%s' '{\"data\":{\"rateLimit\":{\"remaining\":9},\"repository\":{\"pullRequests\":{\"pageInfo\":{\"hasNextPage\":true,\"endCursor\":\"same\"},\"nodes\":[{\"number\":1,\"title\":\"foo: endless\",\"url\":\"https://github.com/homebrew/foo/pull/1\",\"state\":\"OPEN\"}]}}}}'\n"
    )
    endless.chmod(0o755)
    monkeypatch.setenv("PYBREW_GITHUB_CURL", str(paginated))
    formula = _formula(
        "ruby-graphql-pagination",
        "1.0",
        (
            '  prs = Utils::GitHub.graphql_fetch_pull_requests("foo", "homebrew/foo", state: "open")\n'
            '  numbers = prs.map { |pr| pr["number"] }.join(",")\n'
            '  rate = Utils::GitHub.graphql_rate_limit_remaining.to_s\n'
            '  truncated = Utils::GitHub.graphql_pagination_truncated?.to_s\n'
            '  ENV["PYBREW_GITHUB_API_URL"] = "https://ghe.example/api/v3"\n'
            '  gql_url = Utils::GitHub.graphql_url\n'
            '  rest_url = Utils::GitHub.url_to("repos", "a", "b")\n'
            '  ENV["PYBREW_GITHUB_CURL"] = "' + str(enterprise) + '"\n'
            '  Utils::GitHub.graphql_data("query { viewer { login } }")\n'
            '  argv = Pathname.new("' + str(enterprise_argv) + '").read\n'
            '  ENV["PYBREW_GITHUB_CURL"] = "' + str(endless) + '"\n'
            '  endless_prs = Utils::GitHub.graphql_fetch_pull_requests("foo", "homebrew/foo", state: "open")\n'
            '  (prefix/"graphql-pagination.txt").write [\n'
            '    numbers, rate, truncated, gql_url, rest_url,\n'
            '    argv.include?(gql_url).to_s, endless_prs.length.to_s,\n'
            '    Utils::GitHub.graphql_pagination_truncated?.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "graphql-pagination.txt").read_text().splitlines() == [
        "7,8",
        "42",
        "false",
        "https://ghe.example/api/graphql",
        "https://ghe.example/api/v3/repos/a/b",
        "true",
        "50",
        "true",
    ]


def test_keg_binary_relocation_fake_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    tool_log = tmp_path / "binary-tool.log"
    fake_tool = tmp_path / "fake-binary-tool"
    fake_tool.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" >> "' + str(tool_log) + '"\n'
        "exit 0\n"
    )
    fake_tool.chmod(0o755)
    monkeypatch.setenv("PYBREW_INSTALL_NAME_TOOL", str(fake_tool))
    monkeypatch.setenv("PYBREW_PATCHELF", str(fake_tool))
    monkeypatch.setenv("FAKE_BINARY_TOOL_LOG", str(tool_log))
    keg = _run_raw_body(
        tmp_path,
        """
keg_path = prefix
(keg_path/"bin").mkpath
macho = keg_path/"bin"/"macho"
macho.binwrite([0xCF, 0xFA, 0xED, 0xFE].pack("C*") + "dummy")
elf = keg_path/"bin"/"elf"
elf.binwrite([0x7F, 0x45, 0x4C, 0x46].pack("C*") + "dummy")
outside = Pathname.new(Dir.tmpdir)/"pybrew-reloc-binary-#{Process.pid}"
outside.binwrite([0xCF, 0xFA, 0xED, 0xFE].pack("C*") + "outside")
link = keg_path/"bin"/"linked"
link.make_symlink(outside) unless link.symlink?
keg = Keg.new(keg_path)
mapping = { "/old/lib" => "/new/lib" }
dry = keg.relocate_binary_files(mapping, dry_run: true)
real = keg.relocate_binary_files(mapping)
log = Pathname.new(ENV["FAKE_BINARY_TOOL_LOG"]).read
outside_raw = File.binread(outside.to_s)
ENV["PYBREW_INSTALL_NAME_TOOL"] = "/nonexistent/install_name_tool"
missing = keg.relocate_binary_files(mapping)
missing_strict = begin
  keg.relocate_binary_files(mapping, strict: true)
  "none"
rescue => error
  error.class.name + "|" + error.message.include?("tool unavailable").to_s
end
(prefix/"binary-relocation.txt").write [
  dry["files"].length.to_s, dry["commands"].length.to_s,
  real["executed"].length.to_s,
  log.include?("-change").to_s, log.include?("--replace-needed").to_s,
  log.include?("linked").to_s, outside_raw.include?("outside").to_s,
  missing["skipped"].length.to_s, missing_strict,
  dry["files"].any? { |file| file.end_with?("macho") }.to_s
].join("\\n")
""",
        name="ruby-keg-binary-relocation",
    )
    assert (keg / "binary-relocation.txt").read_text().splitlines() == [
        "2",
        "2",
        "2",
        "true",
        "true",
        "false",
        "true",
        "1",
        "RuntimeError|true",
        "true",
    ]


def test_sandbox_strict_bwrap_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text("#!/bin/sh\nexit 0\n")
    fake_bwrap.chmod(0o755)
    formula = _formula(
        "ruby-bwrap-strict",
        "1.0",
        (
            '  ENV["PYBREW_SANDBOX"] = "1"\n'
            '  ENV["PYBREW_SANDBOX_STRICT"] = "1"\n'
            '  ENV["PYBREW_SANDBOX_PATH"] = "' + str(fake_bwrap) + '"\n'
            '  Sandbox.allow_write("/tmp/out")\n'
            '  args = Sandbox.bwrap_policy_args\n'
            '  joined = args.join(",")\n'
            '  network_unshared = args.include?("--unshare-net")\n'
            '  Sandbox.allow_network\n'
            '  network_args = Sandbox.bwrap_policy_args.join(",")\n'
            '  (prefix/"bwrap-strict.txt").write [\n'
            '    joined.include?("--ro-bind,/System,/System").to_s,\n'
            '    joined.include?("--ro-bind,/usr,/usr").to_s,\n'
            '    joined.include?("--ro-bind,/,/").to_s,\n'
            '    joined.include?("--bind,/tmp/out,/tmp/out").to_s,\n'
            '    network_unshared.to_s,\n'
            '    network_args.include?("--unshare-net").to_s,\n'
            '    joined.include?("--dev,/dev").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "bwrap-strict.txt").read_text().splitlines() == [
        "true",
        "true",
        "false",
        "true",
        "true",
        "false",
        "true",
    ]


def test_keg_codesign_symlink_retarget_and_elf_rpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    int_log = tmp_path / "install-name-tool.log"
    elf_log = tmp_path / "patchelf.log"
    sign_log = tmp_path / "codesign.log"

    def fake_tool(name: str, log: Path) -> Path:
        path = tmp_path / name
        path.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$@" >> "' + str(log) + '"\n'
            "exit 0\n"
        )
        path.chmod(0o755)
        return path

    install_name_tool = fake_tool("fake-install-name-tool", int_log)
    patchelf = fake_tool("fake-patchelf", elf_log)
    codesign = fake_tool("fake-codesign", sign_log)
    monkeypatch.setenv("PYBREW_INSTALL_NAME_TOOL", str(install_name_tool))
    monkeypatch.setenv("PYBREW_PATCHELF", str(patchelf))
    monkeypatch.setenv("PYBREW_CODESIGN", str(codesign))
    monkeypatch.setenv("FAKE_CODESIGN_LOG", str(sign_log))
    monkeypatch.setenv("FAKE_ELF_LOG", str(elf_log))
    keg = _run_raw_body(
        tmp_path,
        """
keg_path = prefix
(keg_path/"bin").mkpath
macho = keg_path/"bin"/"macho"
macho.binwrite([0xCF, 0xFA, 0xED, 0xFE].pack("C*") + "dummy")
elf = keg_path/"bin"/"elf"
elf.binwrite([0x7F, 0x45, 0x4C, 0x46].pack("C*") + "dummy")
link = keg_path/"bin"/"retarget"
link.make_symlink("/old/target") unless link.symlink?
keg = Keg.new(keg_path)
mapping = { "/old/lib" => "/new/lib" }
plan = keg.binary_relocation_plan(mapping, files: [macho], codesign: true)
real = keg.relocate_binary_files(mapping, files: [macho], codesign: true)
elf_run = keg.relocate_binary_files({}, files: [elf], rpath_deletions: ["@loader_path"])
dry_links = keg.retarget_symlinks(
  { "bin/retarget" => "@@HOMEBREW_PREFIX@@/lib/new" }, dry_run: true
)
before_target = link.readlink.to_s
real_links = keg.retarget_symlinks(
  { "bin/retarget" => "@@HOMEBREW_PREFIX@@/lib/new" }
)
after_target = link.readlink.to_s
strict_error = begin
  keg.retarget_symlinks({ "bin/missing" => "/tmp/x" }, strict: true)
  "none"
rescue => error
  error.class.name + "|" + error.message.include?("skipped").to_s
end
ENV["PYBREW_CODESIGN"] = "/nonexistent/codesign"
missing_plan = keg.binary_relocation_plan(mapping, files: [macho], codesign: true)
missing_strict = begin
  keg.relocate_binary_files(mapping, files: [macho], codesign: true, strict: true)
  "none"
rescue => error
  error.class.name + "|" + error.message.include?("codesign tool unavailable").to_s
end
sign_raw = Pathname.new(ENV["FAKE_CODESIGN_LOG"]).read
(prefix/"codesign-symlink.txt").write [
  plan["codesign_commands"].length.to_s,
  real["executed"].length.to_s, real["codesigned"].length.to_s,
  sign_raw.include?("--force").to_s, sign_raw.include?("--sign").to_s,
  sign_raw.include?("-").to_s,
  Pathname.new(ENV["FAKE_ELF_LOG"]).read.include?("--remove-rpath").to_s,
  dry_links["changes"].length.to_s, before_target,
  (after_target == "#{HOMEBREW_PREFIX}/lib/new").to_s,
  strict_error, missing_plan["codesign_skipped"].length.to_s, missing_strict
].join("\\n")
""",
        name="ruby-keg-codesign-symlink",
    )
    assert (keg / "codesign-symlink.txt").read_text().splitlines() == [
        "1",
        "1",
        "1",
        "true",
        "true",
        "true",
        "true",
        "1",
        "/old/target",
        "true",
        "RuntimeError|true",
        "1",
        "RuntimeError|true",
    ]


def test_sandbox_strict_bwrap_hardening_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text("#!/bin/sh\nexit 0\n")
    fake_bwrap.chmod(0o755)
    formula = _formula(
        "ruby-bwrap-hardening",
        "1.0",
        (
            '  ENV["PYBREW_SANDBOX"] = "1"\n'
            '  ENV["PYBREW_SANDBOX_STRICT"] = "1"\n'
            '  ENV["PYBREW_SANDBOX_PATH"] = "' + str(fake_bwrap) + '"\n'
            '  args = Sandbox.bwrap_policy_args\n'
            '  joined = args.join(",")\n'
            '  (prefix/"bwrap-hardening.txt").write [\n'
            '    joined.include?("--unshare-user").to_s,\n'
            '    joined.include?("--cap-drop,ALL").to_s,\n'
            '    joined.include?("--die-with-parent").to_s,\n'
            '    joined.include?("--ro-bind,/,/").to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "bwrap-hardening.txt").read_text().splitlines() == [
        "true",
        "true",
        "true",
        "false",
    ]


def test_formulary_upstream_from_path_and_pypi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    source = tmp_path / "pypi-fixture.rb"
    source.write_text(
        "class PypiFixture < Formula\n"
        '  url "https://example.com/pypi-fixture.tar.gz"\n'
        "  pypi_packages package_name: \"demo\", extra_packages: \"setuptools\"\n"
        "  def install; end\n"
        "end\n",
        encoding="utf-8",
    )
    body = (
        f'  klass = Formulary.from_path("{source}")\n'
        "  info = klass.new.pypi_packages_info\n"
        "  source_meta = klass.new.loaded_from_metadata?\n"
        "  meta = Formulary.load_formula_from_struct!(\n"
        '    "meta-fixture", {"desc" => "meta", "version" => "1.0"}\n'
        "  )\n"
        "  meta_flag = meta.new.loaded_from_metadata?\n"
        "  alias_default = klass.new.follow_installed_alias?\n"
        "  instance = klass.new\n"
        "  instance.follow_installed_alias = false\n"
        f'  missing = Formulary.try_new("{tmp_path}/does-not-exist.rb")\n'
        '  bad = begin\n'
        f'    Formulary.from_path("{tmp_path}/not-ruby.py")\n'
        '    "no-error"\n'
        "  rescue StandardError => e\n"
        "    e.message\n"
        "  end\n"
        '  (prefix/"upstream-formulary-api.txt").write [\n'
        '    klass.name, info.package_name, info.extra_packages.join(","),\n'
        '    source_meta, meta_flag, alias_default, instance.follow_installed_alias?,\n'
        '    missing.nil?, bad\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="upstream-formulary-api")
    lines = (keg / "upstream-formulary-api.txt").read_text().splitlines()
    assert lines[0] == "pypi-fixture"
    assert lines[1] == "demo"
    assert lines[2] == "setuptools"
    assert lines[3] == "false"
    assert lines[4] == "true"
    assert lines[5] == "true"
    assert lines[6] == "false"
    assert lines[7] == "true"
    assert "not a loadable .rb" in lines[8]


def test_keg_deleted_formula_and_preserve_rpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    body = (
        '  tap_root = prefix/"tap"\n'
        '  (tap_root/"Formula").mkpath\n'
        '  keg_path = prefix/"Cellar"/"deleted-fixture"/"1.0"\n'
        '  (keg_path/".brew").mkpath\n'
        '  (keg_path/"INSTALL_RECEIPT.json").write(\'{"tap":""}\')\n'
        '  ENV["PYBREW_TAPS"] = tap_root.to_s\n'
        '  before = Keg.new(keg_path).deleted_formula?\n'
        '  preserve = Keg.new(keg_path).formula_preserve_rpath?\n'
        '  (tap_root/"Formula"/"deleted-fixture.rb").write(\n'
        '    "class DeletedFixture < Formula; end\\n"\n'
        '  )\n'
        '  after = Keg.new(keg_path).deleted_formula?\n'
        '  (prefix/"keg-deleted.txt").write [before, preserve, after].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="keg-deleted")
    lines = (keg / "keg-deleted.txt").read_text().splitlines()
    assert lines[0] == "true"
    assert lines[1] == "false"
    assert lines[2] == "false"


def test_formulary_from_bottle_local_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    bottle = tmp_path / "bottle-fixture-1.0.bottle.tar.gz"
    recipe = (
        "class BottleFixture < Formula\n"
        '  url "https://example.com/bottle-fixture.tar.gz"\n'
        "  def install; end\n"
        "end\n"
    )
    with tarfile.open(bottle, "w:gz") as tar:
        data = recipe.encode()
        info = tarfile.TarInfo("bottle-fixture/1.0/.brew/bottle-fixture.rb")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    body = (
        f'  formula = Formulary.from_bottle("{bottle}")\n'
        '  (prefix/"from-bottle.txt").write [\n'
        '    formula.name, formula.local_bottle_path.to_s\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="from-bottle")
    lines = (keg / "from-bottle.txt").read_text().splitlines()
    assert lines[0] == "bottle-fixture"
    assert lines[1] == str(bottle)


def test_keg_formula_preserve_rpath_from_installed_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    body = (
        '  keg_path = prefix/"Cellar"/"preserve-fixture"/"1.0"\n'
        '  (keg_path/".brew").mkpath\n'
        '  (keg_path/"INSTALL_RECEIPT.json").write(\'{"tap":""}\')\n'
        '  (keg_path/".brew"/"preserve-fixture.rb").write(<<~RUBY)\n'
        "    class PreserveFixture < Formula\n"
        "      preserve_rpath\n"
        "      def install; end\n"
        "    end\n"
        "  RUBY\n"
        '  (prefix/"preserve-rpath.txt").write(\n'
        '    Keg.new(keg_path).formula_preserve_rpath?.to_s\n'
        '  )\n'
    )
    keg = _run_raw_body(tmp_path, body, name="preserve-rpath")
    assert (keg / "preserve-rpath.txt").read_text().strip() == "true"


# ---------------------------------------------------------------------------
# Full-parity wave: Formulary RS256/envelope JWS + channels, Sandbox rules,
# MacOS/SystemConfig host detection, GitHub rate limits, Keg codesign,
# dynamic resources/patches and resource.stage target forms.
# ---------------------------------------------------------------------------


def _ruby_rsa_jws(
    payload: dict,
    *,
    alg: str = "RS256",
    envelope: bool = False,
    b64: bool = True,
    kid: str = "homebrew-1",
    signing_key: str | None = None,
) -> tuple[str, str, str]:
    """Sign a synthetic API payload with a throwaway RSA key via host Ruby."""

    signer = r'''
require "openssl"
require "json"
require "base64"

payload_text = ENV.fetch("JWS_PAYLOAD")
alg = ENV.fetch("JWS_ALG")
key = if ENV.fetch("JWS_KEY", "").empty?
  OpenSSL::PKey::RSA.new(2048)
else
  OpenSSL::PKey::RSA.new(ENV.fetch("JWS_KEY"))
end
digest = case alg
         when "RS256", "PS256" then "SHA256"
         when "RS384", "PS384" then "SHA384"
         else "SHA512"
         end
sign = lambda do |data|
  if alg.start_with?("PS")
    key.sign_pss(digest, data, salt_length: :digest, mgf1_hash: digest)
  else
    key.sign(digest, data)
  end
end
if ENV["JWS_ENVELOPE"] == "1"
  header = { "alg" => alg, "kid" => ENV.fetch("JWS_KID"), "b64" => ENV["JWS_B64"] == "1" }
  protected = Base64.urlsafe_encode64(JSON.generate(header), padding: false)
  signing_input = if ENV["JWS_B64"] == "1"
    "#{protected}.#{Base64.urlsafe_encode64(payload_text, padding: false)}"
  else
    "#{protected}.#{payload_text}"
  end
  signature = Base64.urlsafe_encode64(sign.call(signing_input), padding: false)
  puts JSON.generate({
    "payload" => payload_text,
    "signatures" => [{ "protected" => protected, "signature" => signature,
                       "header" => { "kid" => ENV.fetch("JWS_KID") } }],
  })
else
  header = Base64.urlsafe_encode64(JSON.generate({ "alg" => alg, "typ" => "JWT" }), padding: false)
  encoded_payload = Base64.urlsafe_encode64(payload_text, padding: false)
  signature = Base64.urlsafe_encode64(sign.call("#{header}.#{encoded_payload}"), padding: false)
  puts "#{header}.#{encoded_payload}.#{signature}"
end
print key.to_pem
print key.public_key.to_pem
'''
    env = dict(os.environ)
    env.update(
        JWS_PAYLOAD=json.dumps(payload),
        JWS_ALG=alg,
        JWS_ENVELOPE="1" if envelope else "0",
        JWS_B64="1" if b64 else "0",
        JWS_KID=kid,
        JWS_KEY=signing_key or "",
    )
    env.setdefault("JWS_KEY", "")
    result = subprocess.run(
        [_RUBY, "-e", signer], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    token_line, _, pem_text = result.stdout.partition("\n")
    public_marker = "-----BEGIN PUBLIC KEY-----"
    assert public_marker in pem_text
    private_pem, _, public_tail = pem_text.partition(public_marker)
    public_pem = public_marker + public_tail
    return token_line, private_pem.strip(), public_pem


def test_formulary_rs256_envelope_jws_and_embedded_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    compact_payload = {
        "formulae": [
            {
                "name": "rsa-compact",
                "desc": "rs256 compact",
                "homepage": "https://example.test/rsa",
                "version": "5.0",
                "license": "MIT",
            }
        ]
    }
    envelope_payload = {
        "formulae": [
            {
                "name": "rsa-envelope",
                "desc": "ps512 envelope",
                "version": "6.0",
            }
        ]
    }
    compact_token, private_pem, public_pem = _ruby_rsa_jws(
        compact_payload, alg="RS256"
    )
    envelope_token, _, _ = _ruby_rsa_jws(
        envelope_payload, alg="PS512", envelope=True, b64=False,
        signing_key=private_pem,
    )
    header_part, payload_part, signature_part = compact_token.split(".")
    tampered = f"{header_part}.{payload_part}." + ("A" * len(signature_part))
    none_header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    none_token = f"{none_header}.e30.e30"

    key_path = tmp_path / "api-public.pem"
    key_path.write_text(public_pem)

    def script(name: str, text: str) -> Path:
        path = tmp_path / f"jws-{name}.sh"
        path.write_text("#!/bin/sh\nprintf '%s' '" + text + "'\n")
        path.chmod(0o755)
        return path

    compact_script = script("compact", compact_token)
    envelope_script = script("envelope", envelope_token)
    tampered_script = script("tampered", tampered)
    none_script = script("none", none_token)
    formula = _formula(
        "ruby-rsa-jws",
        "1.0",
        (
            '  ENV["PYBREW_FORMULA_API_URL"] = "https://api.example.test/formula.jws.json"\n'
            '  ENV["PYBREW_FORMULA_API_JWS_PUBLIC_KEY"] = "' + str(key_path) + '"\n'
            '  ENV.delete("PYBREW_FORMULA_API_JWS_KEY")\n'
            '  ENV["PYBREW_FORMULA_API_CURL"] = "' + str(compact_script) + '"\n'
            '  compact = Formulary.load_formula_from_api!\n'
            '  ENV["PYBREW_FORMULA_API_CURL"] = "' + str(envelope_script) + '"\n'
            '  Formulary.clear_cache\n'
            '  envelope = Formulary.load_formula_from_api!\n'
            '  ENV["PYBREW_FORMULA_API_CURL"] = "' + str(tampered_script) + '"\n'
            '  Formulary.clear_cache\n'
            '  tampered = begin\n'
            '    Formulary.load_formula_from_api!\n'
            '    "none"\n'
            '  rescue => raised\n'
            '    raised.class.name + "|" + raised.message.include?("verification failed").to_s\n'
            '  end\n'
            '  ENV["PYBREW_FORMULA_API_CURL"] = "' + str(none_script) + '"\n'
            '  Formulary.clear_cache\n'
            '  unsupported = begin\n'
            '    Formulary.load_formula_from_api!\n'
            '    "none"\n'
            '  rescue => raised\n'
            '    raised.class.name + "|" + raised.message.include?("unsupported API JWS alg").to_s\n'
            '  end\n'
            '  embedded = Formulary::JWS_PUBLIC_KEY_PEM.include?("BEGIN PUBLIC KEY")\n'
            '  (prefix/"rsa-jws.txt").write [\n'
            '    compact.loaded_name, compact.loaded_desc, compact.loaded_version,\n'
            '    envelope.loaded_name, envelope.loaded_desc, envelope.loaded_version,\n'
            '    tampered, unsupported, embedded.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "rsa-jws.txt").read_text().splitlines() == [
        "rsa-compact",
        "rs256 compact",
        "5.0",
        "rsa-envelope",
        "ps512 envelope",
        "6.0",
        "PybrewFormulaUnavailableError|true",
        "PybrewFormulaUnavailableError|true",
        "true",
    ]


def test_formulary_channel_subset_merge_and_api_alias_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    api_path = tmp_path / "channel-subset.json"
    api_path.write_text(
        json.dumps(
            {
                "stable": {
                    "formulae": [
                        {
                            "name": "multi-dyn",
                            "full_name": "homebrew/core/multi-dyn",
                            "desc": "stable desc",
                            "homepage": "https://example.test/stable",
                            "version": "1.0",
                            "aliases": ["multi-alias"],
                            "oldname": "multi-old",
                        }
                    ]
                },
                "beta": {
                    "formulae": {
                        "multi-dyn": {
                            "desc": "beta desc",
                            "version": "2.0",
                        }
                    }
                },
            }
        )
    )
    monkeypatch.setenv("PYBREW_FORMULA_API_JSON", str(api_path))
    monkeypatch.setenv("PYBREW_FORMULA_API_CHANNELS", "stable,beta")
    formula = _formula(
        "ruby-channel-subset",
        "1.0",
        (
            '  loaded_result = Formulary.load_formula_from_api_channels!\n'
            '  loaded = loaded_result.is_a?(Array) ? loaded_result.first : loaded_result\n'
            '  alias_class = Formulary.formula_class_get_from_api("multi-alias")\n'
            '  qualified = Formulary.formula_class_get_from_api("homebrew/core/multi-alias")\n'
            '  canonical = Formulary.canonical_name("multi-old")\n'
            '  factory = Formulary.factory("multi-alias")\n'
            '  (prefix/"channel-subset.txt").write [\n'
            '    Formulary.api_json_channels.join(","),\n'
            '    loaded_result.is_a?(Array).to_s, loaded_result.is_a?(Class).to_s,\n'
            '    loaded.loaded_name, loaded.loaded_desc,\n'
            '    loaded.loaded_version, loaded.loaded_homepage,\n'
            '    alias_class.loaded_version, qualified.loaded_name,\n'
            '    canonical, factory.loaded_version\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    assert (keg / "channel-subset.txt").read_text().splitlines() == [
        "stable,beta",
        "false",
        "true",
        "multi-dyn",
        "beta desc",
        "2.0",
        "https://example.test/stable",
        "2.0",
        "multi-dyn",
        "multi-dyn",
        "2.0",
    ]


def test_sandbox_filter_types_home_rules_and_hook_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    fake_sandbox_exec = tmp_path / "sandbox-exec"
    fake_sandbox_exec.write_text("#!/bin/sh\nexit 0\n")
    fake_sandbox_exec.chmod(0o755)
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("key")
    (home / ".aws").mkdir(parents=True)
    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text("#!/bin/sh\nexit 0\n")
    fake_bwrap.chmod(0o755)
    body = (
        '  ENV["PYBREW_SANDBOX"] = "1"\n'
        '  ENV["PYBREW_SANDBOX_STRICT"] = "1"\n'
        '  ENV["PYBREW_SANDBOX_PATH"] = "' + str(fake_sandbox_exec) + '"\n'
        '  ENV["HOME"] = "' + str(home) + '"\n'
        '  filter = Sandbox.path_filter("/tmp/literal", :literal)\n'
        '  Sandbox.allow_read(path: "/tmp/literal", type: :literal)\n'
        '  Sandbox.allow_read_if_exists("/tmp/does-not-exist")\n'
        '  Sandbox.allow_write("/tmp/sub")\n'
        '  Sandbox.allow_network("/tmp/socket", type: :literal)\n'
        '  Sandbox.allow_process_exec("/bin/tool")\n'
        '  Sandbox.deny_read_home(except: :git)\n'
        '  git_allow = Sandbox.rules.any? do |rule|\n'
        '    rule[:kind] == :allow_read && Sandbox.rule_paths(rule[:payload]).include?(ENV["HOME"] + "/.ssh")\n'
        '  end\n'
        '  aws_denied = Sandbox.rules.any? do |rule|\n'
        '    rule[:kind] == :deny_read && Sandbox.rule_paths(rule[:payload]).include?(ENV["HOME"] + "/.aws")\n'
        '  end\n'
        '  whole_home_denied = Sandbox.rules.any? do |rule|\n'
        '    rule[:kind] == :deny_read && Sandbox.rule_paths(rule[:payload]).include?(ENV["HOME"])\n'
        '  end\n'
        '  profile = Sandbox.sandbox_profile\n'
        '  wrapped = Sandbox.wrapped_command("echo", "hi")\n'
        '  applied = Sandbox.apply!\n'
        '  applied_policy = Sandbox.applied_policy\n'
        '  Sandbox.reset_rules!\n'
        '  Sandbox.add_install_hook_rules(network_access_allowed: false)\n'
        '  hook_disabled = Sandbox.sandbox_profile\n'
        '  hook_rules = Sandbox.rules.map { |rule| rule[:kind].to_s }.join(",")\n'
        '  Sandbox.reset_rules!\n'
        '  Sandbox.add_install_hook_rules(network_access_allowed: true)\n'
        '  hook_enabled = Sandbox.sandbox_profile\n'
        '  Sandbox.allow_read(path: "/tmp/literal", type: :literal)\n'
        '  ENV["PYBREW_SANDBOX_PATH"] = "' + str(fake_bwrap) + '"\n'
        '  bwrap = Sandbox.bwrap_policy_args.join(",")\n'
        '  (prefix/"sandbox-filters.txt").write [\n'
        '    filter[:path], filter[:type].to_s, Sandbox.path_filter.inspect,\n'
        '    profile.include?("(literal \\"/tmp/literal\\")").to_s,\n'
        '    profile.include?("(allow file-write-setugid (subpath \\"/tmp/sub\\"))").to_s,\n'
        '    profile.include?("(allow file-write-mode (subpath \\"/tmp/sub\\"))").to_s,\n'
        '    profile.include?("(allow network* (literal \\"/tmp/socket\\"))").to_s,\n'
        '    git_allow.to_s, aws_denied.to_s, whole_home_denied.to_s,\n'
        '    wrapped[2].include?("(deny default)").to_s,\n'
        '    applied.to_s, applied_policy["strict"].to_s,\n'
        '    hook_disabled.include?("(deny network*)").to_s,\n'
        '    hook_rules, hook_enabled.include?("(deny network*)").to_s,\n'
        '    bwrap.include?("--ro-bind-try,/tmp/literal,/tmp/literal").to_s\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-sandbox-filters")
    assert (keg / "sandbox-filters.txt").read_text().splitlines() == [
        "/tmp/literal",
        "literal",
        "nil",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "false",
        "true",
        "true",
        "true",
        "true",
        "allow_write,allow_write,deny_write,deny_read,deny_network",
        "false",
        "true",
    ]


def test_macos_sdk_scan_and_system_config_host_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    sdk_prefix = tmp_path / "SDKs"
    for version in ("14.5", "15.0"):
        sdk_dir = sdk_prefix / f"MacOSX{version}.sdk"
        sdk_dir.mkdir(parents=True)
        (sdk_dir / "SDKSettings.json").write_text(json.dumps({"Version": version}))
    sdk_dir = sdk_prefix / "MacOSX15.0.sdk"
    monkeypatch.setenv("HOMEBREW_SDK_PREFIX", str(sdk_prefix))
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "14.5")
    monkeypatch.setenv("HOMEBREW_XCODE_VERSION", "15.3")
    monkeypatch.setenv("HOMEBREW_CLT_VERSION", "15.3.0.1")
    monkeypatch.setenv("HOMEBREW_SDKROOT", str(sdk_dir))
    monkeypatch.setenv("HOMEBREW_SDK_VERSION", "15.0")
    body = (
        '  versions = MacOS::SDK.all_sdks.map { |sdk| sdk.version.to_s }.join(",")\n'
        '  latest = MacOS::SDK.latest_sdk.version.to_s\n'
        '  selected = MacOS::SDK.sdk_for("14").version.to_s\n'
        '  read = MacOS::SDK.read_sdk_version(MacOS::SDK.all_sdks.first.path).to_s\n'
        '  xcode = SystemConfig.describe_xcode\n'
        '  clt = SystemConfig.describe_clt\n'
        '  sdk = SystemConfig.describe_sdk\n'
        '  os = SystemConfig.describe_os\n'
        '  dump = SystemConfig.dump(StringIO.new).string\n'
        '  (prefix/"host-details.txt").write [\n'
        '    versions, latest, selected, read, MacOS.version.to_s,\n'
        '    MacOS.version_symbol.to_s, xcode, clt, sdk, os,\n'
        '    dump.include?("OS:").to_s, dump.include?("SDK:").to_s\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-host-details")
    lines = (keg / "host-details.txt").read_text().splitlines()
    assert lines[0] == "14.5,15.0"
    assert lines[1] == "15.0"
    assert lines[2] == "14.5"
    assert lines[3] == "14.5"
    assert lines[4] == "14"
    assert lines[5] == "sonoma"
    if is_macos():
        assert "15.3" in lines[6]
        assert lines[7] == "15.3.0.1"
        assert lines[8].startswith("15.0 => ")
        assert lines[9].startswith("macOS 14.5")
        assert lines[10:] == ["true", "true"]
    else:
        # Linux SystemConfig intentionally omits the macOS-only SDK section.
        assert lines[10:] == ["true", "false"]


def test_github_secondary_rate_limit_errors_and_sync_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    reset_epoch = int(time.time()) + 120
    reset_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset_epoch))
    script = tmp_path / "github-errors-curl"
    script.write_text(
        "#!/bin/sh\n"
        'headers=""\n'
        'prev=""\n'
        'url=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "-D" ]; then headers="$arg"; fi\n'
        '  case "$arg" in http*) url="$arg" ;; esac\n'
        '  prev="$arg"\n'
        'done\n'
        'case "$url" in\n'
        "  *rate403*) printf 'HTTP/1.1 403 Forbidden\\r\\nx-ratelimit-remaining: 0\\r\\n"
        "x-ratelimit-reset: __RESET__\\r\\nx-ratelimit-resource: core\\r\\n"
        "x-ratelimit-limit: 5000\\r\\n\\r\\n' > \"$headers\"; "
        "printf '%s' '{\"message\":\"API rate limit exceeded\"}' ;;\n"
        "  *secondary*) printf 'HTTP/1.1 403 Forbidden\\r\\nretry-after: 2\\r\\n\\r\\n' > \"$headers\"; "
        "printf '%s' '{\"message\":\"You have exceeded a secondary rate limit\"}' ;;\n"
        "  *rate429*) printf 'HTTP/1.1 429 Too Many Requests\\r\\nretry-after: 1\\r\\n\\r\\n' > \"$headers\"; "
        "printf '%s' '{\"message\":\"Too many requests\"}' ;;\n"
        "  *auth*) printf 'HTTP/1.1 401 Unauthorized\\r\\n\\r\\n' > \"$headers\"; "
        "printf '%s' '{\"message\":\"Bad credentials\"}' ;;\n"
        "  *notfound*) printf 'HTTP/1.1 404 Not Found\\r\\n\\r\\n' > \"$headers\"; "
        "printf '%s' '{\"message\":\"Not Found\"}' ;;\n"
        "  *validate*) printf 'HTTP/1.1 422 Unprocessable Entity\\r\\n\\r\\n' > \"$headers\"; "
        "printf '%s' '{\"message\":\"Validation Failed\",\"errors\":[{\"code\":\"missing\"}]}' ;;\n"
        "  *graphql*) printf '%s' '{\"errors\":[{\"type\":\"RATE_LIMITED\",\"message\":\"API rate limit exceeded\"}],"
        "\"data\":{\"rateLimit\":{\"resetAt\":\"__RESET_AT__\"}}}' ;;\n"
        "  *) printf 'HTTP/1.1 200 OK\\r\\n\\r\\n' > \"$headers\"; printf '%s' '{\"ok\":true}' ;;\n"
        "esac\n"
    )
    script.write_text(
        script.read_text()
        .replace("__RESET_AT__", reset_at)
        .replace("__RESET__", str(reset_epoch))
    )
    script.chmod(0o755)
    body = (
        '  ENV["PYBREW_GITHUB_CURL"] = "' + str(script) + '"\n'
        '  ENV.delete("PYBREW_GITHUB_TOKEN")\n'
        '  base = "https://api.github.com"\n'
        '  core = begin\n'
        '    Utils::GitHub.get_json(base + "/rate403")\n'
        '    "none"\n'
        '  rescue => error\n'
        '    [error.class.name, error.respond_to?(:reset).to_s, error.resource,\n'
        '     error.limit.to_s, error.message.include?("Rate limit exceeded").to_s,\n'
        '     (error.reset - Time.now.to_i).between?(60, 200).to_s].join("|")\n'
        '  end\n'
        '  secondary = begin\n'
        '    Utils::GitHub.get_json(base + "/secondary")\n'
        '    "none"\n'
        '  rescue => error\n'
        '    [error.class.name, error.resource,\n'
        '     (error.reset - Time.now.to_i).between?(1, 10).to_s].join("|")\n'
        '  end\n'
        '  too_many = begin\n'
        '    Utils::GitHub.get_json(base + "/rate429")\n'
        '    "none"\n'
        '  rescue => error\n'
        '    [error.class.name, error.resource].join("|")\n'
        '  end\n'
        '  auth = begin\n'
        '    Utils::GitHub.get_json(base + "/auth")\n'
        '    "none"\n'
        '  rescue => error\n'
        '    error.class.name\n'
        '  end\n'
        '  not_found = begin\n'
        '    Utils::GitHub.get_json(base + "/notfound")\n'
        '    "none"\n'
        '  rescue => error\n'
        '    error.class.name\n'
        '  end\n'
        '  missing = begin\n'
        '    Utils::GitHub.get_json(base + "/notfound", scopes: ["repo"])\n'
        '    "none"\n'
        '  rescue => error\n'
        '    error.class.name\n'
        '  end\n'
        '  validation = begin\n'
        '    Utils::GitHub.get_json(base + "/validate")\n'
        '    "none"\n'
        '  rescue => error\n'
        '    [error.class.name, error.errors.length.to_s].join("|")\n'
        '  end\n'
        '  ok = Utils::GitHub.get_json(base + "/ok")["ok"].to_s\n'
        '  graphql = begin\n'
        '    Utils::GitHub.graphql_data("query { viewer { login } }")\n'
        '    "none"\n'
        '  rescue => error\n'
        '    [error.class.name, error.resource].join("|")\n'
        '  end\n'
        '  constants = [defined?(Utils::GitHub::API::RateLimitExceededError).to_s,\n'
        '               defined?(GitHub::API::RateLimitExceededError).to_s,\n'
        '               Utils::GitHub.respond_to?(:sleep_for_rate_limit).to_s].join(",")\n'
        '  past = Utils::GitHub::API::RateLimitExceededError.new(\n'
        '    "past", reset: Time.now.to_i - 30, resource: "core", limit: 1\n'
        '  )\n'
        '  slept = GitHub::API.sleep_for_rate_limit(past)\n'
        '  rate_limit_check = Utils::GitHub.rate_limit_error?(past).to_s\n'
        '  (prefix/"github-errors.txt").write [\n'
        '    core, secondary, too_many, auth, not_found, missing, validation,\n'
        '    ok, graphql, constants, slept.to_s, rate_limit_check\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-github-errors")
    assert (keg / "github-errors.txt").read_text().splitlines() == [
        "Utils::GitHub::API::RateLimitExceededError|true|core|5000|true|true",
        "Utils::GitHub::API::RateLimitExceededError|secondary|true",
        "Utils::GitHub::API::RateLimitExceededError|secondary",
        "Utils::GitHub::API::AuthenticationFailedError",
        "Utils::GitHub::API::HTTPNotFoundError",
        "Utils::GitHub::API::MissingAuthenticationError",
        "Utils::GitHub::API::ValidationFailedError|1",
        "true",
        "Utils::GitHub::API::RateLimitExceededError|graphql",
        "constant,constant,true",
        "1",
        "true",
    ]


def test_keg_codesign_verification_entitlements_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    sign_log = tmp_path / "codesign-verify.log"

    def fake_codesign(name: str, verify_exit: int) -> Path:
        path = tmp_path / name
        path.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$@" >> "' + str(sign_log) + '"\n'
            'case "$1" in\n'
            '  -d) printf \'%s\' \'<plist><dict><key>com.apple.security.cs.allow-jit</key><true/></dict></plist>\' ;;\n'
            '  --verify) exit ' + str(verify_exit) + ' ;;\n'
            "esac\n"
            "exit 0\n"
        )
        path.chmod(0o755)
        return path

    good = fake_codesign("codesign-good", 0)
    bad = fake_codesign("codesign-bad", 1)
    monkeypatch.setenv("PYBREW_INSTALL_NAME_TOOL", shutil.which("true") or "/usr/bin/true")
    monkeypatch.setenv("PYBREW_CODESIGN", str(good))
    monkeypatch.setenv("PYBREW_CODESIGN_IDENTITY", "Test Identity")
    body = (
        '  (prefix/"bin").mkpath\n'
        '  macho = prefix/"bin"/"macho"\n'
        '  macho.binwrite([0xCF, 0xFA, 0xED, 0xFE].pack("C*") + "dummy")\n'
        '  keg = Keg.new(prefix)\n'
        '  result = keg.relocate_binary_files({ "/old" => "/new" }, files: [macho], codesign: true)\n'
        '  plan = keg.binary_relocation_plan({ "/old" => "/new" }, files: [macho], codesign: true)\n'
        '  single = keg.codesign_patched_binary(macho)\n'
        '  ENV["PYBREW_CODESIGN"] = "' + str(bad) + '"\n'
        '  non_strict = keg.relocate_binary_files({ "/old" => "/new" }, files: [macho], codesign: true)\n'
        '  strict = begin\n'
        '    keg.relocate_binary_files({ "/old" => "/new" }, files: [macho], codesign: true, strict: true)\n'
        '    "none"\n'
        '  rescue => error\n'
        '    error.class.name + "|" + error.message.include?("codesign verification failed").to_s\n'
        '  end\n'
        '  log = Pathname.new(ENV["FAKE_CODESIGN_VERIFY_LOG"]).read\n'
        '  count = lambda do |token|\n'
        '    log.lines.map(&:strip).count { |line| line == token }\n'
        '  end\n'
        '  (prefix/"codesign-verify.txt").write [\n'
        '    result["codesigned"].length.to_s,\n'
        '    result["codesign_verified"].first["verified"].to_s,\n'
        '    result["entitlements"].first["present"].to_s,\n'
        '    result["entitlements"].first["raw"].include?("allow-jit").to_s,\n'
        '    log.include?("--preserve-metadata=entitlements,requirements,flags,runtime").to_s,\n'
        '    log.include?("Test Identity").to_s,\n'
        '    plan["codesign_verify_commands"].length.to_s,\n'
        '    single["signed"].to_s, single["verified"].to_s, single["identity"],\n'
        '    non_strict["codesign_verified"].first["verified"].to_s,\n'
        '    strict,\n'
        '    (count.call("--verify") >= 4).to_s\n'
        '  ].join("\\n")\n'
    )
    monkeypatch.setenv("FAKE_CODESIGN_VERIFY_LOG", str(sign_log))
    keg = _run_raw_body(tmp_path, body, name="ruby-keg-codesign-verify")
    assert (keg / "codesign-verify.txt").read_text().splitlines() == [
        "1",
        "true",
        "true",
        "true",
        "true",
        "true",
        "1",
        "true",
        "true",
        "Test Identity",
        "false",
        "RuntimeError|true",
        "true",
    ]


def test_dynamic_resource_names_stage_target_and_declared_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    archive = _tar_resource(
        tmp_path / "src" / "dynamic-thing.tar.gz",
        "dynamic-thing-1.0",
        {"inner.txt": "dynamic resource\n"},
    )
    formula = _formula(
        "ruby-dynamic-resource",
        "1.0",
        (
            '  name = "dyn" + "-thing"\n'
            '  resource = resource(name)\n'
            '  target = prefix/"stage-target"\n'
            '  returned = resource.stage(target)\n'
            '  block_value = resource.stage(prefix/"stage-target-2") do |dir|\n'
            '    (prefix/"stage-block-dir.txt").write dir.to_s\n'
            '    "blocked"\n'
            '  end\n'
            '  (prefix/"dynamic-resource.txt").write [\n'
            '    resource.name,\n'
            '    File.file?((target/"dynamic-thing-1.0"/"inner.txt").to_s).to_s,\n'
            '    (returned.to_s == target.to_s).to_s,\n'
            '    block_value,\n'
            '    File.file?((prefix/"stage-target-2"/"dynamic-thing-1.0"/"inner.txt").to_s).to_s,\n'
            '    resource.stage.to_s == resource.staged_path.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.resources["dyn-thing"] = SourceSpec(
        url=archive.as_uri(), sha256=_sha256(archive)
    )
    keg = _build(tmp_path, formula, downloader=_downloader(tmp_path))
    assert (keg / "dynamic-resource.txt").read_text().splitlines() == [
        "dyn-thing",
        "true",
        "true",
        "blocked",
        "true",
        "true",
    ]

    # Class-body declarations with interpolated names are captured by the
    # Formulary source loader as real metadata objects.
    loaded_body = (
        '  path = prefix/"dynamic-meta.rb"\n'
        "  path.write <<~'RUBY'\n"
        '    class DynamicMeta < Formula\n'
        '      version "3.0"\n'
        '      resource "res-#{version}" do\n'
        '        url "https://example.test/res-#{version}.tar.gz"\n'
        '        sha256 "abc123"\n'
        '      end\n'
        '      patch "https://example.test/patch-#{version}.diff"\n'
        '      patch :DATA\n'
        '      def install; end\n'
        '    end\n'
        '  RUBY\n'
        '  klass = Formulary.load_formula_from_path("dynamic-meta", path)\n'
        '  instance = klass.new\n'
        '  resource = instance.resources.first\n'
        '  (prefix/"dynamic-meta.txt").write [\n'
        '    resource.name, resource.url, resource.sha256,\n'
        '    instance.resource("res-3.0").equal?(resource).to_s,\n'
        '    instance.patches.length.to_s, instance.patches[0].url,\n'
        '    instance.patches[1].data?.to_s\n'
        '  ].join("\\n")\n'
    )
    keg2 = _run_raw_body(tmp_path, loaded_body, name="ruby-dynamic-meta")
    assert (keg2 / "dynamic-meta.txt").read_text().splitlines() == [
        "res-3.0",
        "https://example.test/res-3.0.tar.gz",
        "abc123",
        "true",
        "2",
        "https://example.test/patch-3.0.diff",
        "true",
    ]


# ---------------------------------------------------------------------------
# Residual parity wave: channel TTL, embedded-key cache, >10-page GraphQL
# pagination + secondary backoff, broad local MacOS/CLT detection and codesign
# identity round-trips.
# ---------------------------------------------------------------------------


def test_formulary_channel_ttl_and_embedded_key_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    counter = tmp_path / "api-fetch-count.txt"
    fake_curl = tmp_path / "ttl-curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        'count_file="' + str(counter) + '"\n'
        'count=$(cat "$count_file" 2>/dev/null || echo 0)\n'
        'printf \'%s\' "$((count + 1))" > "$count_file"\n'
        "printf '%s' '{\"formulae\":[{\"name\":\"ttl-one\",\"desc\":\"ttl\",\"version\":\"1.0\"}]}'\n"
    )
    fake_curl.chmod(0o755)
    monkeypatch.setenv("PYBREW_FORMULA_API_URL_STABLE", "https://api.example.test/stable.json")
    monkeypatch.setenv("PYBREW_FORMULA_API_URL_BETA", "https://api.example.test/beta.json")
    monkeypatch.setenv("PYBREW_FORMULA_API_CHANNELS", "stable,beta")
    monkeypatch.setenv("PYBREW_FORMULA_API_CURL", str(fake_curl))
    body = (
        '  count = lambda { Pathname.new("' + str(counter) + '").read.to_i }\n'
        '  ENV["PYBREW_FORMULA_API_TTL"] = "3600"\n'
        '  Formulary.ensure_api_json_loaded\n'
        '  first = count.call\n'
        '  Formulary.ensure_api_json_loaded\n'
        '  cached = count.call\n'
        '  ENV["PYBREW_FORMULA_API_TTL"] = "0"\n'
        '  Formulary.ensure_api_json_loaded\n'
        '  expired = count.call\n'
        '  ENV["PYBREW_FORMULA_API_TTL"] = "3600"\n'
        '  Formulary.ensure_api_json_loaded\n'
        '  after_expiry = count.call\n'
        '  (prefix/"channel-ttl.txt").write [\n'
        '    first.to_s, cached.to_s, expired.to_s, after_expiry.to_s,\n'
        '    Formulary.api_json_ttl_seconds.to_s\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-channel-ttl")
    assert (keg / "channel-ttl.txt").read_text().splitlines() == [
        "2",
        "2",
        "4",
        "4",
        "3600",
    ]

    _, _, public_pem = _ruby_rsa_jws(
        {"formulae": [{"name": "cache-key", "desc": "d", "version": "1.0"}]}
    )
    key_path = tmp_path / "cache-key.pem"
    key_path.write_text(public_pem)
    key_body = (
        '  ENV["PYBREW_FORMULA_API_JWS_PUBLIC_KEY"] = "' + str(key_path) + '"\n'
        '  first = Formulary.jws_public_key\n'
        '  second = Formulary.jws_public_key\n'
        '  cached = first.equal?(second)\n'
        '  size = Formulary.jws_public_key_cache_size\n'
        '  Formulary.clear_cache\n'
        '  cleared = Formulary.jws_public_key_cache_size\n'
        '  (prefix/"key-cache.txt").write [\n'
        '    cached.to_s, size.to_s, cleared.to_s,\n'
        '    (first.is_a?(OpenSSL::PKey::RSA)).to_s\n'
        '  ].join("\\n")\n'
    )
    keg2 = _run_raw_body(tmp_path, key_body, name="ruby-key-cache")
    assert (keg2 / "key-cache.txt").read_text().splitlines() == [
        "true",
        "1",
        "0",
        "true",
    ]


def test_github_graphql_pagination_beyond_ten_pages_and_secondary_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    count_file = tmp_path / "graphql-count.txt"
    paged = tmp_path / "graphql-many-pages"
    paged.write_text(
        "#!/usr/bin/ruby\n"
        'require "json"\n'
        'args = ARGV\n'
        'data_index = args.index("--data")\n'
        'payload = data_index ? JSON.parse(args[data_index + 1]) : {}\n'
        'after = payload.dig("variables", "after").to_s\n'
        'n = after[/\\d+/].to_i\n'
        'count_file = ENV.fetch("FAKE_GRAPHQL_COUNT")\n'
        'count = File.exist?(count_file) ? File.read(count_file).to_i : 0\n'
        'File.write(count_file, (count + 1).to_s)\n'
        'has_next = n < 11\n'
        'node = { "number" => n + 1, "title" => "foo: update #{n + 1}",\n'
        '         "url" => "https://github.com/homebrew/foo/pull/#{n + 1}",\n'
        '         "state" => "OPEN" }\n'
        'puts JSON.generate({ "data" => { "rateLimit" => { "remaining" => 10 },\n'
        '  "repository" => { "pullRequests" => {\n'
        '    "pageInfo" => { "hasNextPage" => has_next,\n'
        '                    "endCursor" => has_next ? "c#{n + 1}" : nil },\n'
        '    "nodes" => [node] } } } })\n'
    )
    paged.chmod(0o755)
    monkeypatch.setenv("FAKE_GRAPHQL_COUNT", str(count_file))

    retry_count = tmp_path / "retry-count.txt"
    retrying = tmp_path / "github-retrying"
    retrying.write_text(
        "#!/bin/sh\n"
        'headers=""\n'
        'prev=""\n'
        'url=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "-D" ]; then headers="$arg"; fi\n'
        '  case "$arg" in http*) url="$arg" ;; esac\n'
        '  prev="$arg"\n'
        'done\n'
        'count_file="' + str(retry_count) + '"\n'
        'count=$(cat "$count_file" 2>/dev/null || echo 0)\n'
        'printf \'%s\' "$((count + 1))" > "$count_file"\n'
        'if [ "$count" = "0" ]; then\n'
        "  printf 'HTTP/1.1 403 Forbidden\\r\\nretry-after: 0\\r\\n\\r\\n' > \"$headers\"\n"
        "  printf '%s' '{\"message\":\"secondary rate limit\"}'\n"
        'else\n'
        "  printf 'HTTP/1.1 200 OK\\r\\n\\r\\n' > \"$headers\"\n"
        "  printf '%s' '{\"ok\":true}'\n"
        'fi\n'
    )
    retrying.chmod(0o755)

    graphql_retry_count = tmp_path / "graphql-retry-count.txt"
    graphql_retry = tmp_path / "graphql-retry"
    graphql_retry.write_text(
        "#!/bin/sh\n"
        'count_file="' + str(graphql_retry_count) + '"\n'
        'count=$(cat "$count_file" 2>/dev/null || echo 0)\n'
        'printf \'%s\' "$((count + 1))" > "$count_file"\n'
        'if [ "$count" = "0" ]; then\n'
        "  printf '%s' '{\"errors\":[{\"type\":\"RATE_LIMITED\",\"message\":\"rate limited\"}],"
        "\"data\":{\"rateLimit\":{\"resetAt\":\"2020-01-01T00:00:00Z\"}}}'\n"
        'else\n'
        "  printf '%s' '{\"data\":{\"rateLimit\":{\"remaining\":7},\"viewer\":{\"login\":\"octo\"}}}'\n"
        'fi\n'
    )
    graphql_retry.chmod(0o755)

    body = (
        '  ENV["PYBREW_GITHUB_CURL"] = "' + str(paged) + '"\n'
        '  nodes = Utils::GitHub.graphql_paginated_nodes(\n'
        '    "query($after: String) { nodes }", {}, ["repository"],\n'
        '    max_pages: 12, rate_limit_retries: 1\n'
        '  )\n'
        '  nodes = Utils::GitHub.graphql_paginated_nodes(\n'
        '    "query { repository { pullRequests } }", {}, ["repository", "pullRequests"]\n'
        '  ) if nodes.empty?\n'
        '  prs = Utils::GitHub.graphql_fetch_pull_requests("foo", "homebrew/foo", state: "open")\n'
        '  ENV["PYBREW_GITHUB_CURL"] = "' + str(retrying) + '"\n'
        '  ENV["PYBREW_GITHUB_MAX_BACKOFF"] = "0"\n'
        '  retried = Utils::GitHub.get_json("https://api.github.com/rate", retries: 1)\n'
        '  calls = Pathname.new("' + str(retry_count) + '").read.to_i\n'
        '  ENV["PYBREW_GITHUB_CURL"] = "' + str(graphql_retry) + '"\n'
        '  graphql_retried = Utils::GitHub.graphql_paginated_nodes(\n'
        '    "query { viewer { login } }", {}, ["viewer"], rate_limit_retries: 1\n'
        '  )\n'
        '  gql_calls = Pathname.new("' + str(graphql_retry_count) + '").read.to_i\n'
        '  (prefix/"graphql-residual.txt").write [\n'
        '    prs.length.to_s, prs.last["number"].to_s,\n'
        '    Utils::GitHub.graphql_pagination_truncated?.to_s,\n'
        '    retried["ok"].to_s, calls.to_s,\n'
        '    Utils::GitHub.rate_limit_backoff_seconds(\n'
        '      Utils::GitHub::API::RateLimitExceededError.new("x", reset: Time.now.to_i - 10)\n'
        '    ).to_s,\n'
        '    graphql_retried.is_a?(Array).to_s, gql_calls.to_s\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-graphql-residual")
    lines = (keg / "graphql-residual.txt").read_text().splitlines()
    assert lines[0] == "12"
    assert lines[1] == "12"
    assert lines[2] == "false"
    assert lines[3] == "true"
    assert lines[4] == "2"
    assert lines[5] == "0"
    assert lines[6] == "true"
    assert lines[7] == "2"


def test_macos_broad_local_detection_with_fake_xcrun_clt_and_system_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not is_macos():
        pytest.skip("macOS host detection is macOS-only")

    monkeypatch.setenv(ALLOW_ENV, "1")
    sdk_path = tmp_path / "MacOSX14.5.sdk"
    sdk_path.mkdir()
    (sdk_path / "SDKSettings.json").write_text(json.dumps({"Version": "14.5"}))
    developer_dir = tmp_path / "Xcode.app" / "Contents" / "Developer"
    (developer_dir / "Platforms/MacOSX.platform/Developer/SDKs").mkdir(parents=True)
    fake_xcrun = tmp_path / "xcrun"
    fake_xcrun.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *--show-sdk-path*) printf '%s\\n' \"" + str(sdk_path) + "\" ;;\n"
        "  *--show-sdk-version*) printf '14.5\\n' ;;\n"
        "esac\n"
    )
    fake_xcrun.chmod(0o755)
    fake_select = tmp_path / "xcode-select"
    fake_select.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"" + str(developer_dir) + "\"\n"
    )
    fake_select.chmod(0o755)
    fake_pkgutil = tmp_path / "pkgutil"
    fake_pkgutil.write_text(
        "#!/bin/sh\nprintf 'package-id: com.apple.pkg.CLTools_Executables\\n"
        "version: 15.3.0.1\\n'\n"
    )
    fake_pkgutil.chmod(0o755)
    monkeypatch.setenv("PYBREW_XCRUN", str(fake_xcrun))
    monkeypatch.setenv("PYBREW_XCODE_SELECT", str(fake_select))
    monkeypatch.setenv("PYBREW_PKGUTIL", str(fake_pkgutil))
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "14.5")
    monkeypatch.delenv("HOMEBREW_SDKROOT", raising=False)
    monkeypatch.delenv("HOMEBREW_SDK_PATH", raising=False)
    monkeypatch.delenv("HOMEBREW_SDK_VERSION", raising=False)
    monkeypatch.delenv("HOMEBREW_XCODE_VERSION", raising=False)
    monkeypatch.delenv("HOMEBREW_CLT_VERSION", raising=False)
    body = (
        '  sdk = MacOS.sdk\n'
        '  clt = MacOS::CLT.version\n'
        '  (prefix/"broad-macos.txt").write [\n'
        '    sdk.nil? ? "nil" : sdk.path.to_s,\n'
        '    sdk.nil? ? "nil" : sdk.version.to_s,\n'
        '    MacOS.active_developer_dir.to_s,\n'
        '    MacOS::Xcode.installed?.to_s,\n'
        '    clt.nil? ? "nil" : clt.to_s,\n'
        '    SystemConfig.describe_sdk.include?("MacOSX14.5.sdk").to_s,\n'
        '    SystemConfig.describe_clt.to_s,\n'
        '    MacOS::CLT.installed?.to_s\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-broad-macos")
    lines = (keg / "broad-macos.txt").read_text().splitlines()
    assert lines[0] == str(sdk_path)
    assert lines[1] == "14.5"
    assert lines[2] == str(developer_dir)
    assert lines[3] == "true"
    assert lines[4] == "15.3.0.1"
    assert lines[5] == "true"
    assert lines[6] == "15.3.0.1"
    assert lines[7] == "true"


# ---------------------------------------------------------------------------
# Residual wave: local MacOS/Xcode/CLT/SDK choice, GraphQL connection
# pagination/transport retry, Formulary loader ambiguity matrix and
# FormulaInstaller Tab/SBOM parity.
# ---------------------------------------------------------------------------


def test_macos_local_choice_without_version_overrides_and_rich_system_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not is_macos():
        pytest.skip("macOS Xcode/CLT detection is macOS-only")

    monkeypatch.setenv(ALLOW_ENV, "1")
    app = tmp_path / "Xcode.app"
    developer = app / "Contents" / "Developer"
    sdk = developer / "Platforms/MacOSX.platform/Developer/SDKs/MacOSX14.5.sdk"
    sdk.mkdir(parents=True)
    (sdk / "SDKSettings.json").write_text(json.dumps({"Version": "14.5"}))
    (app / "Contents" / "version.plist").write_text(
        "<?xml version=\"1.0\"?><plist><dict>"
        "<key>CFBundleShortVersionString</key><string>15.4</string>"
        "</dict></plist>"
    )
    fake_select = tmp_path / "xcode-select"
    fake_select.write_text("#!/bin/sh\nprintf '%s\\n' \"" + str(developer) + "\"\n")
    fake_select.chmod(0o755)
    fake_pkgutil = tmp_path / "pkgutil"
    fake_pkgutil.write_text(
        "#!/bin/sh\nprintf 'package-id: com.apple.pkg.CLTools_Executables\\n"
        "version: 15.3.0.1\\n'\n"
    )
    fake_pkgutil.chmod(0o755)
    fake_xcrun = tmp_path / "xcrun"
    fake_xcrun.write_text("#!/bin/sh\nexit 1\n")
    fake_xcrun.chmod(0o755)
    monkeypatch.setenv("PYBREW_XCODE_SELECT", str(fake_select))
    monkeypatch.setenv("PYBREW_PKGUTIL", str(fake_pkgutil))
    monkeypatch.setenv("PYBREW_XCRUN", str(fake_xcrun))
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "14.5")
    for name in (
        "HOMEBREW_XCODE_VERSION",
        "HOMEBREW_CLT_VERSION",
        "HOMEBREW_SDKROOT",
        "HOMEBREW_SDK_PATH",
        "HOMEBREW_SDK_VERSION",
        "PYBREW_XCODE_APP",
    ):
        monkeypatch.delenv(name, raising=False)
    body = (
        '  sdk = MacOS.sdk\n'
        '  sdks = MacOS::SDK.all_sdks.map { |entry| entry.version.to_s }.join(",")\n'
        '  (prefix/"local-macos.txt").write [\n'
        '    MacOS::Xcode.installed?.to_s,\n'
        '    MacOS::Xcode.version.to_s,\n'
        '    MacOS::Xcode.bundle_path.to_s,\n'
        '    sdks,\n'
        '    sdk.nil? ? "nil" : sdk.path.to_s,\n'
        '    MacOS::CLT.version.to_s,\n'
        '    MacOS::CLT.installed?.to_s,\n'
        '    SystemConfig.describe_xcode.include?("15.4").to_s,\n'
        '    SystemConfig.describe_clt.to_s,\n'
        '    SystemConfig.describe_sdk.include?("14.5").to_s,\n'
        '    SystemConfig.describe_kernel.empty?.to_s,\n'
        '    SystemConfig.describe_compiler.empty?.to_s,\n'
        '    SystemConfig.describe_ruby.include?(RUBY_VERSION).to_s,\n'
        '    SystemConfig.describe_arch.include?(Hardware::CPU.arch.to_s).to_s,\n'
        '    SystemConfig.config_sections.include?(:kernel_config).to_s,\n'
        '    SystemConfig.config_sections.include?(:compiler_config).to_s,\n'
        '    SystemConfig.config_sections.include?(:taps_config).to_s,\n'
        '    SystemConfig.config_sections.include?(:macos_config).to_s,\n'
        '    SystemConfig.dump(StringIO.new).string.include?("Taps:").to_s,\n'
        '    SystemConfig.dump(StringIO.new).string.include?("Kernel:").to_s\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-local-macos-choice")
    lines = (keg / "local-macos.txt").read_text().splitlines()
    assert lines[0] == "true"
    assert lines[1] == "15.4"
    assert lines[2] == str(app)
    assert "14.5" in lines[3]
    assert lines[4] == str(sdk)
    assert lines[5] == "15.3.0.1"
    assert lines[6] == "true"
    assert lines[7] == "true"
    assert lines[8] == "15.3.0.1"
    assert lines[9] == "true"
    assert lines[10:] == ["false", "false", "true", "true", "true", "true", "true", "true", "true", "true"]


def test_github_graphql_connections_and_transport_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    graphql_script = tmp_path / "graphql-connections"
    graphql_script.write_text(
        "#!" + _RUBY + "\n"
        'require "json"\n'
        'args = ARGV\n'
        'idx = args.index("--data")\n'
        'payload = idx ? JSON.parse(args[idx + 1]) : {}\n'
        'query = payload["query"].to_s\n'
        'after = (payload["variables"] || {})["after"].to_s\n'
        'def connection(nodes, has_next, cursor)\n'
        '  { "pageInfo" => { "hasNextPage" => has_next,\n'
        '                    "endCursor" => has_next ? cursor : nil }, "nodes" => nodes }\n'
        'end\n'
        'data = { "rateLimit" => { "remaining" => 100 } }\n'
        'if query.include?("reviews")\n'
        '  nodes = if after.empty?\n'
        '    [ { "state" => "APPROVED", "authorAssociation" => "MEMBER",\n'
        '        "author" => { "login" => "revA" }, "commit" => { "oid" => "c1" } },\n'
        '      { "state" => "APPROVED", "authorAssociation" => "OWNER",\n'
        '        "author" => { "login" => "revB" }, "commit" => { "oid" => "c1" } } ]\n'
        '  else\n'
        '    [ { "state" => "APPROVED", "authorAssociation" => "MEMBER",\n'
        '        "author" => { "login" => "revC" }, "commit" => { "oid" => "c1" } } ]\n'
        '  end\n'
        '  data["repository"] = { "pullRequest" => { "reviews" =>\n'
        '    connection(nodes, after.empty?, "r1") } }\n'
        'elsif query.include?("members")\n'
        '  nodes = after.empty? ? [ { "login" => "m1" }, { "login" => "m2" } ] : [ { "login" => "m3" } ]\n'
        '  data["organization"] = { "team" => { "members" => connection(nodes, after.empty?, "m1") } }\n'
        'elsif query.include?("sponsorshipsAsSponsored")\n'
        '  nodes = if after.empty?\n'
        '    [ { "sponsorEntity" => { "login" => "s1" } },\n'
        '      { "sponsorEntity" => { "login" => "s2" } } ]\n'
        '  else\n'
        '    [ { "sponsorEntity" => { "login" => "s3" } } ]\n'
        '  end\n'
        '  data["user"] = { "sponsorshipsAsSponsored" => connection(nodes, after.empty?, "s1") }\n'
        'elsif query.include?("tiers")\n'
        '  nodes = after.empty? ? [ { "name" => "t1", "monthlyPriceInCents" => 100 } ] :\n'
        '                          [ { "name" => "t2", "monthlyPriceInCents" => 200 } ]\n'
        '  data["user"] = { "sponsorsListing" => { "tiers" => connection(nodes, after.empty?, "t1") } }\n'
        'end\n'
        'puts JSON.generate({ "data" => data })\n'
    )
    graphql_script.chmod(0o755)
    monkeypatch.setenv("PYBREW_GITHUB_CURL", str(graphql_script))
    transport_count = tmp_path / "transport-count.txt"
    transport_script = tmp_path / "graphql-transport-retry"
    transport_script.write_text(
        "#!/bin/sh\n"
        'count_file="' + str(transport_count) + '"\n'
        'count=$(cat "$count_file" 2>/dev/null || echo 0)\n'
        'printf \'%s\' "$((count + 1))" > "$count_file"\n'
        'if [ "$count" = "0" ]; then exit 1; fi\n'
        "printf '%s' '{\"data\":{\"rateLimit\":{\"remaining\":5},\"viewer\":{\"login\":\"octo\"}}}'\n"
    )
    transport_script.chmod(0o755)
    rest_count = tmp_path / "rest-transport-count.txt"
    rest_script = tmp_path / "rest-transport-retry"
    rest_script.write_text(
        "#!/bin/sh\n"
        'headers=""\n'
        'prev=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "-D" ]; then headers="$arg"; fi\n'
        '  prev="$arg"\n'
        'done\n'
        'count_file="' + str(rest_count) + '"\n'
        'count=$(cat "$count_file" 2>/dev/null || echo 0)\n'
        'printf \'%s\' "$((count + 1))" > "$count_file"\n'
        'if [ "$count" = "0" ]; then\n'
        "  printf 'HTTP/1.1 500 Internal Server Error\\r\\n\\r\\n' > \"$headers\"\n"
        "  printf '%s' '{\"message\":\"server error\"}'\n"
        'else\n'
        "  printf 'HTTP/1.1 200 OK\\r\\n\\r\\n' > \"$headers\"\n"
        "  printf '%s' '{\"ok\":true}'\n"
        'fi\n'
    )
    rest_script.chmod(0o755)
    body = (
        '  ENV["PYBREW_GITHUB_CURL"] = "' + str(graphql_script) + '"\n'
        '  reviews = Utils::GitHub.repository_approved_reviews("homebrew", "foo", 7, commit: "c1")\n'
        '  members = Utils::GitHub.members_by_team("homebrew", "core")\n'
        '  sponsors = Utils::GitHub.sponsorships("alice")\n'
        '  tiers = Utils::GitHub.sponsorship_tiers("alice")\n'
        '  ENV["PYBREW_GITHUB_CURL"] = "' + str(transport_script) + '"\n'
        '  ENV["PYBREW_GITHUB_TRANSPORT_RETRIES"] = "1"\n'
        '  ENV["PYBREW_GITHUB_TRANSPORT_BACKOFF"] = "0"\n'
        '  retried = Utils::GitHub.graphql_data("query { viewer { login } }")\n'
        '  transport_calls = Pathname.new("' + str(transport_count) + '").read.to_i\n'
        '  ENV["PYBREW_GITHUB_CURL"] = "' + str(rest_script) + '"\n'
        '  rest = Utils::GitHub.get_json("https://api.github.com/rest", transport_retries: 1)\n'
        '  rest_calls = Pathname.new("' + str(rest_count) + '").read.to_i\n'
        '  (prefix/"graphql-connections.txt").write [\n'
        '    reviews.map { |review| review["login"] }.join(","),\n'
        '    members.map { |member| member["login"] }.join(","),\n'
        '    sponsors.map { |sponsor| sponsor["login"] }.join(","),\n'
        '    tiers.map { |tier| tier["name"] }.join(","),\n'
        '    retried["viewer"]["login"], transport_calls.to_s,\n'
        '    rest["ok"].to_s, rest_calls.to_s,\n'
        '    Utils::GitHub.github_transport_retries.to_s\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-github-connections")
    assert (keg / "graphql-connections.txt").read_text().splitlines() == [
        "revA,revB,revC",
        "m1,m2,m3",
        "s1,s2,s3",
        "t1,t2",
        "octo",
        "2",
        "true",
        "2",
        "1",
    ]


def test_formulary_tap_ambiguity_loader_matrix_and_channel_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    taps = tmp_path / "taps"
    alpha = taps / "user1" / "alpha"
    beta = taps / "user2" / "beta"
    for root, desc in ((alpha, "alpha desc"), (beta, "beta desc")):
        (root / "Formula").mkdir(parents=True)
        (root / "Formula" / "ambig.rb").write_text(
            "class Ambig < Formula\n"
            "  desc \"" + desc + "\"\n"
            '  version "1.0"\n'
            "  def install; end\n"
            "end\n"
        )
    (alpha / "Formula" / "solo.rb").write_text(
        'class Solo < Formula\n  desc "solo desc"\n  version "1.0"\n  def install; end\nend\n'
    )
    monkeypatch.setenv("PYBREW_TAPS", f"{alpha}{os.pathsep}{beta}")
    local_path = tmp_path / "local-only.rb"
    local_path.write_text(
        'class LocalOnly < Formula\n  desc "local desc"\n  version "1.0"\n  def install; end\nend\n'
    )
    channel_file = tmp_path / "channel-edge.json"
    channel_file.write_text(
        json.dumps(
            {
                "stable": {
                    "formulae": [
                        {
                            "name": "chan-edge",
                            "desc": "stable channel",
                            "version": "1.0",
                            "homepage": "https://example.test/stable",
                        }
                    ]
                },
                "beta": {"formulae": []},
            }
        )
    )
    monkeypatch.setenv("PYBREW_FORMULA_API_JSON", str(channel_file))
    monkeypatch.setenv("PYBREW_FORMULA_API_CHANNELS", "stable,beta")
    body = (
        '  matrix = Formulary.loader_matrix("ambig")\n'
        '  ambiguity = begin\n'
        '    Formulary.factory("ambig")\n'
        '    "none"\n'
        '  rescue => error\n'
        '    [error.class.name, error.message.include?("multiple taps").to_s,\n'
        '     error.message.include?("user1/alpha/ambig").to_s,\n'
        '     error.message.include?("user2/beta/ambig").to_s].join("|")\n'
        '  end\n'
        '  qualified = Formulary.factory("user2/beta/ambig")\n'
        '  solo = Formulary.factory("solo")\n'
        '  local = Formulary.factory("' + str(local_path) + '")\n'
        '  choice_ambig = Formulary.loader_choice("ambig").to_s\n'
        '  choice_solo = Formulary.loader_choice("solo").to_s\n'
        '  choice_local = Formulary.loader_choice("' + str(local_path) + '").to_s\n'
        '  channels = Formulary.load_formula_from_api_channels!\n'
        '  channel_list = Formulary.api_channel_for_formula("chan-edge").join(",")\n'
        '  namespace_a = Formulary.api_channel_namespace("stable")\n'
        '  namespace_b = Formulary.api_channel_namespace("beta")\n'
        '  (prefix/"formulary-matrix.txt").write [\n'
        '    matrix[:tap_ambiguous].to_s,\n'
        '    Array(matrix[:ambiguity]).sort.join(","),\n'
        '    ambiguity,\n'
        '    qualified.loaded_desc, solo.loaded_desc, local.loaded_desc,\n'
        '    choice_ambig, choice_solo, choice_local,\n'
        '    channels.loaded_desc, channels.loaded_channels.join(","),\n'
        '    channel_list, (namespace_a != namespace_b).to_s\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-formulary-matrix")
    assert (keg / "formulary-matrix.txt").read_text().splitlines() == [
        "true",
        "user1/alpha/ambig,user2/beta/ambig",
        "PybrewFormulaAmbiguityError|true|true|true",
        "beta desc",
        "solo desc",
        "local desc",
        "name",
        "tap",
        "path",
        "stable channel",
        "stable",
        "stable",
        "true",
    ]

    empty = tmp_path / "empty-channels.json"
    empty.write_text(json.dumps({"stable": {"formulae": []}, "beta": {"formulae": []}}))
    monkeypatch.setenv("PYBREW_FORMULA_API_JSON", str(empty))
    empty_body = (
        '  Formulary.clear_cache\n'
        '  result = begin\n'
        '    Formulary.load_formula_from_api_channels!\n'
        '    "none"\n'
        '  rescue => error\n'
        '    error.class.name + "|" + error.message.include?("no formula structs found").to_s\n'
        '  end\n'
        '  (prefix/"empty-channels.txt").write result\n'
    )
    empty_keg = _run_raw_body(tmp_path, empty_body, name="ruby-empty-channels")
    assert (empty_keg / "empty-channels.txt").read_text().strip() == "PybrewFormulaUnavailableError|true"


def test_formula_installer_tab_sbom_and_metadata_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-tab-sbom",
        "1.0",
        (
            '  path = prefix/"tab-sbom.rb"\n'
            '  path.write <<~RUBY\n'
            '    class TabSbom < Formula\n'
            '      version "2.5"\n'
            '      install do\n'
            '        bin.mkpath\n'
            '        (bin/"tool").write("#!/bin/sh\\necho tool\\n")\n'
            '        lib.mkpath\n'
            '        (lib/"libtool.dylib").write("library")\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  klass = Formulary.load_formula_from_path("tab-sbom", path)\n'
            '  FormulaInstaller.new(klass.new).install\n'
            '  receipt_path = HOMEBREW_PREFIX/"Cellar"/"tab-sbom"/"2.5"/"INSTALL_RECEIPT.json"\n'
            '  receipt = JSON.parse(receipt_path.read)\n'
            '  tab = Tab.for_keg(receipt_path.dirname)\n'
            '  files = receipt.dig("sbom", "files") || {}\n'
            '  tool = files["bin/tool"] || {}\n'
            '  (prefix/"tab-sbom.txt").write [\n'
            '    receipt["built_as_bottle"].to_s,\n'
            '    receipt["poured_from_bottle"].to_s,\n'
            '    receipt["homebrew_version"].to_s,\n'
            '    receipt["compiler"].to_s,\n'
            '    (receipt["arch"].to_s.empty? ? "empty" : "arch"),\n'
            '    receipt["stdlib"].to_s,\n'
            '    (receipt["time"].is_a?(Integer) && receipt["time"] > 0).to_s,\n'
            '    receipt["installed_as_dependency"].to_s,\n'
            '    receipt.dig("source", "path").to_s.end_with?("tab-sbom.rb").to_s,\n'
            '    (receipt["source_modified_time"].is_a?(Float)).to_s,\n'
            '    files.key?("bin/tool").to_s, files.key?("lib/libtool.dylib").to_s,\n'
            '    tool["sha256"].to_s.match?(/\\A[0-9a-f]{64}\\z/).to_s,\n'
            '    (tool["size"].to_i > 0).to_s,\n'
            '    tab.sbom_files.length.to_s,\n'
            '    tab.installed_as_dependency?.to_s,\n'
            '    tab.source["path"].to_s.end_with?("tab-sbom.rb").to_s,\n'
            '    tab.source_modified_time.is_a?(Float).to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "tab-sbom.txt").read_text().splitlines()
    assert lines[0:7] == ["false", "false", "0.0.0", "clang", "arch",
                          "libc++" if is_macos() else "libstdc++", "true"]
    assert lines[7:] == [
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "2",
        "true",
        "true",
        "true",
    ]


# ---------------------------------------------------------------------------
# Closure wave: MacOS/SystemConfig/Utils rows — cache/reset, SDK symbols,
# host detail sections, REST pagination + exact rate-limit edges, tap alias
# ambiguity and channel alias resolution.
# ---------------------------------------------------------------------------


def test_macos_cache_reset_sdk_symbols_and_rich_system_config_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    sdk_prefix = tmp_path / "SDKs"
    for version in ("14.5", "15.0"):
        sdk_dir = sdk_prefix / f"MacOSX{version}.sdk"
        sdk_dir.mkdir(parents=True)
        (sdk_dir / "SDKSettings.json").write_text(json.dumps({"Version": version}))
    tap_root = tmp_path / "taps" / "user" / "demotap"
    (tap_root / "Formula").mkdir(parents=True)
    for name in ("one", "two"):
        (tap_root / "Formula" / f"{name}.rb").write_text(
            f'class {name.capitalize()} < Formula\n  version "1.0"\n  def install; end\nend\n'
        )
    monkeypatch.setenv("HOMEBREW_SDK_PREFIX", str(sdk_prefix))
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "15.0")
    monkeypatch.setenv("PYBREW_TAPS", str(tap_root))
    body = (
        '  sonoma = MacOS::SDK.sdk_for(:sonoma)\n'
        '  fifteen = MacOS::SDK.sdk_for("15")\n'
        '  sonoma_path = MacOS::SDK.sdk_path(:sonoma)\n'
        '  MacOS.full_version = "14.5"\n'
        '  simulated = MacOS.version.to_s + "|" + MacOS.version_symbol.to_s\n'
        '  MacOS.reset_cache!\n'
        '  host = MacOS.full_version.to_s + "|" + MacOS.version.to_s + "|" + MacOS.version_symbol.to_s\n'
        '  config_io = StringIO.new\n'
        '  SystemConfig.homebrew_config(config_io)\n'
        '  env_io = StringIO.new\n'
        '  SystemConfig.homebrew_env_config(env_io)\n'
        '  hardware_io = StringIO.new\n'
        '  SystemConfig.hardware_config(hardware_io)\n'
        '  taps_io = StringIO.new\n'
        '  SystemConfig.taps_config(taps_io)\n'
        '  dump_tap_io = StringIO.new\n'
        '  SystemConfig.dump_tap_config(nil, dump_tap_io)\n'
        '  core_io = StringIO.new\n'
        '  SystemConfig.core_tap_config(core_io)\n'
        '  dump = SystemConfig.dump(StringIO.new).string\n'
        '  (prefix/"macos-closure.txt").write [\n'
        '    sonoma.nil? ? "nil" : sonoma.version.to_s,\n'
        '    fifteen.nil? ? "nil" : fifteen.version.to_s,\n'
        '    sonoma_path.nil? ? "nil" : sonoma_path.basename.to_s,\n'
        '    simulated, host,\n'
        '    MacOS::CLT.sdk_path.nil?.to_s,\n'
        '    config_io.string.include?("HOMEBREW_VERSION:").to_s,\n'
        '    config_io.string.include?("HOMEBREW_REPOSITORY:").to_s,\n'
        '    env_io.string.include?("Homebrew Env:").to_s,\n'
        '    env_io.string.include?("HOMEBREW_PREFIX=").to_s,\n'
        '    hardware_io.string.include?("CPU Cores:").to_s,\n'
        '    hardware_io.string.include?("CPU Features:").to_s,\n'
        '    taps_io.string.include?("user/demotap").to_s,\n'
        '    taps_io.string.include?("(2 formulae)").to_s,\n'
        '    dump_tap_io.string.include?("Tap:").to_s,\n'
        '    core_io.string.include?("Core Tap:").to_s,\n'
        '    dump.include?("Homebrew Env:").to_s,\n'
        '    dump.include?("Taps:").to_s,\n'
        '    SystemConfig.describe_tap("' + str(tap_root) + '").include?("(2 formulae)").to_s\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-macos-closure")
    lines = (keg / "macos-closure.txt").read_text().splitlines()
    assert lines[0] == "14.5"
    assert lines[1] == "15.0"
    assert lines[2] == "MacOSX14.5.sdk"
    assert lines[3] == "14|sonoma"
    assert lines[4] == "15.0|15|sequoia"
    if is_macos():
        assert lines[5] == "false"
    assert lines[6:] == [
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
    ]


def test_github_rest_pagination_and_exact_rate_limit_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    rest_script = tmp_path / "rest-pagination"
    rest_script.write_text(
        "#!" + _RUBY + "\n"
        'require "json"\n'
        'url = ARGV.find { |arg| arg.start_with?("http") }.to_s\n'
        'page = url[/(?:[?&])page=(\\d+)/, 1].to_i\n'
        'page = 1 if page.zero?\n'
        'items = if url.include?("/orgs/") && page == 1\n'
        '  (1..100).map { |index| { "name" => "repo#{index}" } }\n'
        'elsif url.include?("/orgs/") && page == 2\n'
        '  [ { "name" => "repo101" }, { "name" => "repo102" } ]\n'
        'elsif url.include?("/issues") && page == 1\n'
        '  (1..100).map { |index| { "number" => index } }\n'
        'elsif url.include?("/issues") && page == 2\n'
        '  [ { "number" => 101 } ]\n'
        'else\n'
        '  []\n'
        'end\n'
        'puts JSON.generate({ "items" => items })\n'
    )
    rest_script.chmod(0o755)
    counter = tmp_path / "rate-edge-count.txt"
    reset_epoch = int(time.time()) + 120
    rate_script = tmp_path / "rate-edges"
    rate_script.write_text(
        "#!/bin/sh\n"
        'headers=""\n'
        'prev=""\n'
        'url=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "-D" ]; then headers="$arg"; fi\n'
        '  case "$arg" in http*) url="$arg" ;; esac\n'
        '  prev="$arg"\n'
        'done\n'
        'count_file="' + str(counter) + '"\n'
        'count=$(cat "$count_file" 2>/dev/null || echo 0)\n'
        'printf \'%s\' "$((count + 1))" > "$count_file"\n'
        'case "$url" in\n'
        "  *primary*) printf 'HTTP/1.1 403 Forbidden\\r\\nx-ratelimit-remaining: 0\\r\\n"
        "x-ratelimit-reset: __RESET__\\r\\nx-ratelimit-resource: core\\r\\n"
        "retry-after: 5\\r\\n\\r\\n' > \"$headers\"; "
        "printf '%s' '{\"message\":\"API rate limit exceeded\"}' ;;\n"
        "  *secondary*) printf 'HTTP/1.1 403 Forbidden\\r\\nretry-after: 3\\r\\n\\r\\n' > \"$headers\"; "
        "printf '%s' '{\"message\":\"You have exceeded a secondary rate limit\"}' ;;\n"
        "  *too-many*) printf 'HTTP/1.1 429 Too Many Requests\\r\\nx-ratelimit-remaining: 0\\r\\n"
        "retry-after: 2\\r\\n\\r\\n' > \"$headers\"; "
        "printf '%s' '{\"message\":\"Too many requests\"}' ;;\n"
        "  *auth*) printf 'HTTP/1.1 401 Unauthorized\\r\\n\\r\\n' > \"$headers\"; "
        "printf '%s' '{\"message\":\"Bad credentials\"}' ;;\n"
        "  *validate*) printf 'HTTP/1.1 422 Unprocessable Entity\\r\\n\\r\\n' > \"$headers\"; "
        "printf '%s' '{\"message\":\"Validation Failed\"}' ;;\n"
        "esac\n"
    )
    rate_script.write_text(rate_script.read_text().replace("__RESET__", str(reset_epoch)))
    rate_script.chmod(0o755)
    graphql_edge = tmp_path / "graphql-edge"
    graphql_edge.write_text(
        "#!/bin/sh\n"
        "printf '%s' '{\"data\":{\"repository\":{\"items\":{\"pageInfo\":"
        "{\"hasNextPage\":true,\"endCursor\":null},\"nodes\":[{\"n\":1}]}}}}'\n"
    )
    graphql_edge.chmod(0o755)
    body = (
        '  ENV["PYBREW_GITHUB_CURL"] = "' + str(rest_script) + '"\n'
        '  repos = Utils::GitHub.organisation_repositories("homebrew")\n'
        '  issues = Utils::GitHub.issues(repo: "homebrew/foo", state: "open")\n'
        '  capped = Utils::GitHub.paginate_rest(\n'
        '    "https://api.github.com/orgs/homebrew/repos", per_page: 100, max_pages: 2\n'
        '  )\n'
        '  ENV["PYBREW_GITHUB_CURL"] = "' + str(rate_script) + '"\n'
        '  counter = Pathname.new("' + str(counter) + '")\n'
        '  primary = begin\n'
        '    Utils::GitHub.get_json("https://api.github.com/primary")\n'
        '    "none"\n'
        '  rescue => error\n'
        '    [error.class.name, error.resource, (error.reset - Time.now.to_i).between?(60, 200).to_s].join("|")\n'
        '  end\n'
        '  secondary = begin\n'
        '    Utils::GitHub.get_json("https://api.github.com/secondary")\n'
        '    "none"\n'
        '  rescue => error\n'
        '    [error.class.name, error.resource, (error.reset - Time.now.to_i).between?(1, 20).to_s].join("|")\n'
        '  end\n'
        '  too_many = begin\n'
        '    Utils::GitHub.get_json("https://api.github.com/too-many")\n'
        '    "none"\n'
        '  rescue => error\n'
        '    [error.class.name, error.resource].join("|")\n'
        '  end\n'
        '  before_auth = counter.read.to_i\n'
        '  auth = begin\n'
        '    Utils::GitHub.get_json("https://api.github.com/auth", transport_retries: 2)\n'
        '    "none"\n'
        '  rescue => error\n'
        '    error.class.name\n'
        '  end\n'
        '  after_auth = counter.read.to_i\n'
        '  before_validate = counter.read.to_i\n'
        '  validate = begin\n'
        '    Utils::GitHub.get_json("https://api.github.com/validate", transport_retries: 2)\n'
        '    "none"\n'
        '  rescue => error\n'
        '    error.class.name\n'
        '  end\n'
        '  after_validate = counter.read.to_i\n'
        '  ENV["PYBREW_GITHUB_CURL"] = "' + str(graphql_edge) + '"\n'
        '  cursor_nil = Utils::GitHub.graphql_paginated_nodes(\n'
        '    "query { repository { items } }", {}, ["repository", "items"]\n'
        '  )\n'
        '  cursor_nil_truncated = Utils::GitHub.graphql_pagination_truncated?.to_s\n'
        '  capped_gql = Utils::GitHub.graphql_paginated_nodes(\n'
        '    "query { repository { items } }", {}, ["repository", "items"], max_pages: 1\n'
        '  )\n'
        '  (prefix/"github-closure.txt").write [\n'
        '    repos.length.to_s, issues.length.to_s, capped.length.to_s,\n'
        '    primary, secondary, too_many, auth, (after_auth - before_auth).to_s,\n'
        '    validate, (after_validate - before_validate).to_s,\n'
        '    cursor_nil.length.to_s, cursor_nil_truncated,\n'
        '    capped_gql.length.to_s,\n'
        '    Utils::GitHub.graphql_pagination_truncated?.to_s\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-github-closure")
    assert (keg / "github-closure.txt").read_text().splitlines() == [
        "102",
        "101",
        "102",
        "Utils::GitHub::API::RateLimitExceededError|core|true",
        "Utils::GitHub::API::RateLimitExceededError|secondary|true",
        "Utils::GitHub::API::RateLimitExceededError|secondary",
        "Utils::GitHub::API::AuthenticationFailedError",
        "1",
        "Utils::GitHub::API::ValidationFailedError",
        "1",
        "1",
        "false",
        "1",
        "true",
    ]


def test_formulary_tap_alias_ambiguity_keg_loader_and_channel_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    taps = tmp_path / "taps"
    alpha = taps / "user1" / "alpha"
    beta = taps / "user2" / "beta"
    for root, desc in ((alpha, "alpha canon"), (beta, "beta canon")):
        (root / "Formula").mkdir(parents=True)
        (root / "Aliases").mkdir(parents=True)
        (root / "Formula" / "canon.rb").write_text(
            f'class Canon < Formula\n  desc "{desc}"\n  version "1.0"\n  def install; end\nend\n'
        )
        (root / "Aliases" / "dup-alias.rb").symlink_to(root / "Formula" / "canon.rb")
    (alpha / "Aliases" / "solo-alias.rb").symlink_to(alpha / "Formula" / "canon.rb")
    monkeypatch.setenv("PYBREW_TAPS", f"{alpha}{os.pathsep}{beta}")
    channel_file = tmp_path / "channel-alias.json"
    channel_file.write_text(
        json.dumps(
            {
                "stable": {
                    "formulae": [
                        {
                            "name": "chan-target",
                            "desc": "stable target",
                            "version": "1.0",
                            "homepage": "https://example.test/target",
                            "aliases": ["chan-alias"],
                            "oldname": "chan-old",
                        }
                    ]
                },
                "beta": {
                    "formulae": {
                        "chan-target": {"desc": "beta target", "version": "2.0"}
                    }
                },
            }
        )
    )
    monkeypatch.setenv("PYBREW_FORMULA_API_JSON", str(channel_file))
    monkeypatch.setenv("PYBREW_FORMULA_API_CHANNELS", "stable,beta")
    body = (
        '  (HOMEBREW_CELLAR/"installed-only"/"1.0").mkpath\n'
        '  ambiguous = begin\n'
        '    Formulary.resolve_tap_alias!("dup-alias")\n'
        '    "none"\n'
        '  rescue => error\n'
        '    [error.class.name, error.message.include?("multiple taps").to_s,\n'
        '     error.message.include?("user1/alpha/canon").to_s,\n'
        '     error.message.include?("user2/beta/canon").to_s].join("|")\n'
        '  end\n'
        '  ambiguous_soft = Formulary.resolve_tap_alias("dup-alias").inspect\n'
        '  solo = Formulary.resolve_tap_alias!("solo-alias")\n'
        '  alias_matrix = Formulary.loader_matrix("solo-alias")\n'
        '  keg_matrix = Formulary.loader_matrix("installed-only")\n'
        '  Formulary.ensure_api_json_loaded\n'
        '  alias_class = Formulary.formula_class_get_from_api("chan-alias")\n'
        '  factory = Formulary.factory("chan-alias")\n'
        '  canonical = Formulary.canonical_name("chan-old")\n'
        '  (prefix/"formulary-closure.txt").write [\n'
        '    ambiguous, ambiguous_soft,\n'
        '    solo.loaded_desc,\n'
        '    alias_matrix[:alias].to_s, Formulary.loader_choice("solo-alias").to_s,\n'
        '    keg_matrix[:keg].to_s, Formulary.loader_choice("installed-only").to_s,\n'
        '    alias_class.loaded_version, alias_class.loaded_desc,\n'
        '    alias_class.loaded_channels.join(","), factory.loaded_version,\n'
        '    canonical\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-formulary-closure")
    assert (keg / "formulary-closure.txt").read_text().splitlines() == [
        "PybrewFormulaAmbiguityError|true|true|true",
        "nil",
        "alpha canon",
        "true",
        "alias",
        "true",
        "keg",
        "2.0",
        "beta target",
        "stable,beta",
        "2.0",
        "chan-target",
    ]


# ---------------------------------------------------------------------------
# Closure wave: Keg relocation edge matrix (thin/fat Mach-O, ELF32/64,
# hardlinks, dylib id, interpreter, rpath, codesign, restore-on-failure) and
# exotic patch/resource metadata.
# ---------------------------------------------------------------------------


def test_keg_relocation_edge_matrix_fake_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    tool_log = tmp_path / "relocation-tools.log"
    sign_log = tmp_path / "relocation-codesign.log"
    failing_tool = tmp_path / "failing-patchelf"
    failing_tool.write_text(
        "#!/bin/sh\n"
        'file=""\n'
        'for arg in "$@"; do file="$arg"; done\n'
        'printf "CORRUPTED" > "$file"\n'
        "exit 1\n"
    )
    failing_tool.chmod(0o755)
    codesign = tmp_path / "relocation-codesign"
    codesign.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" >> "' + str(sign_log) + '"\n'
        'case "$1" in\n'
        '  -d) printf \'%s\' \'<dict><key>com.apple.security.cs.allow-jit</key><true/></dict>\' ;;\n'
        '  --verify) exit 0 ;;\n'
        "esac\n"
        "exit 0\n"
    )
    codesign.chmod(0o755)

    def fake_tool(name: str) -> Path:
        path = tmp_path / name
        path.write_text(
            "#!" + _RUBY + "\n"
            'log = ENV.fetch("FAKE_RELOCATION_LOG")\n'
            'File.open(log, "a") { |io| io.puts(ARGV.join("\\u0001")) }\n'
            'if ARGV[0] == "-change" || ARGV[0] == "--replace-needed"\n'
            '  old_value, new_value, file = ARGV[1], ARGV[2], ARGV[3]\n'
            '  File.binwrite(file, File.binread(file).gsub(old_value, new_value))\n'
            'elsif ARGV[0] == "--set-interpreter"\n'
            '  new_value, file = ARGV[1], ARGV[2]\n'
            '  File.binwrite(file, File.binread(file).sub("/lib/ld-old.so", new_value))\n'
            'end\n'
            "exit 0\n"
        )
        path.chmod(0o755)
        return path

    monkeypatch.setenv("PYBREW_INSTALL_NAME_TOOL", str(fake_tool("fake-install-name-tool")))
    monkeypatch.setenv("PYBREW_PATCHELF", str(fake_tool("fake-patchelf")))
    monkeypatch.setenv("PYBREW_CODESIGN", str(codesign))
    monkeypatch.setenv("PYBREW_CODESIGN_IDENTITY", "Edge Identity")
    monkeypatch.setenv("FAKE_RELOCATION_LOG", str(tool_log))
    body = r'''
old = "@@HOMEBREW_PREFIX@@/lib"
new_value = HOMEBREW_PREFIX.to_s + "/lib"
keg_path = prefix
(keg_path/"bin").mkpath
(keg_path/"lib").mkpath
macho64 = keg_path/"bin"/"macho64"
macho64.binwrite([0xCF, 0xFA, 0xED, 0xFE, 0x07, 0x00, 0x00, 0x01].pack("C*") + "dummy" + old)
fat = keg_path/"lib"/"fat.dylib"
fat.binwrite([0xCA, 0xFE, 0xBA, 0xBE, 0x00, 0x00, 0x00, 0x02].pack("C*") + "fat" + old)
elf64 = keg_path/"bin"/"elf64"
elf64.binwrite([0x7F, 0x45, 0x4C, 0x46, 0x02, 0x01, 0x01, 0x00, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x3E, 0x00].pack("C*") + "dummy" + old)
elf32 = keg_path/"bin"/"elf32"
elf32.binwrite([0x7F, 0x45, 0x4C, 0x46, 0x01, 0x01, 0x01, 0x00, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x03, 0x00].pack("C*") + "dummy" + old)
linked = keg_path/"bin"/"macho64-linked"
File.link(macho64.to_s, linked.to_s) unless linked.exist?
symlink = keg_path/"bin"/"macho-link"
symlink.make_symlink("macho64") unless symlink.symlink?
retarget = keg_path/"lib"/"retarget.dylib"
retarget.make_symlink("/old/lib/retarget.dylib") unless retarget.symlink?
keg = Keg.new(keg_path)
files = [macho64, fat, elf64, elf32, linked]
plan = keg.binary_relocation_plan(
  old => new_value, files: files, dylib_id: "libedge.dylib",
  interpreter: "/opt/homebrew/lib/ld-edge.so",
  rpath_additions: ["/opt/homebrew/lib"], codesign: true
)
result = keg.relocate_binary_files(
  old => new_value, files: files, dylib_id: "libedge.dylib",
  interpreter: "/opt/homebrew/lib/ld-edge.so",
  rpath_additions: ["/opt/homebrew/lib"], codesign: true,
  verify_placeholders: true
)
matrix = plan["matrix"].map do |entry|
  {
    "path" => entry["path"].split("/").last,
    "kind" => entry["kind"], "format" => entry["format"],
    "arch" => entry["arch"], "fat" => entry["fat"], "bits" => entry["bits"],
    "commands" => entry["commands"].map { |command| command.map(&:to_s) },
  }
end
retarget_report = keg.retarget_symlinks(
  { "lib/retarget.dylib" => "@@HOMEBREW_PREFIX@@/lib/new.dylib" }
)
original = File.binread(elf64.to_s)
ENV["PYBREW_PATCHELF"] = (Pathname.new(ENV.fetch("FAKE_RELOCATION_LOG")).dirname/"failing-patchelf").to_s
restore_error = begin
  keg.relocate_binary_files({ old => new_value }, files: [elf64])
  "none"
rescue => error
  [error.class.name, File.binread(elf64.to_s) == original].join("|")
end
ignored = keg.relocate_binary_files(
  { old => new_value }, files: [elf64], ignore_failures: true
)
placeholders = result["matrix"].select { |entry| entry.key?("placeholders_removed") }
(keg_path/"bin"/"matrix.json").write JSON.generate({
  "files" => plan["files"].length,
  "hardlink_skipped" => plan["hardlink_skipped"].length,
  "matrix" => matrix,
  "verified" => result["matrix"].all? { |entry| entry["verified_magic"] == true },
  "placeholder_count" => placeholders.length,
  "placeholders_ok" => placeholders.all? { |entry| entry["placeholders_removed"] == true },
  "codesigned" => result["codesigned"].length,
  "codesign_verified" => result["codesign_verified"].all? { |entry| entry["verified"] },
  "entitlements" => result["entitlements"].first["present"],
  "identity" => result["codesign_identity"],
  "retarget" => retarget_report["changes"].first["to"],
  "restore_error" => restore_error,
  "ignored_failed" => ignored["failed"].length,
  "ignored_restored" => ignored["failed"].first["restored"],
})
'''
    keg = _run_raw_body(tmp_path, body, name="ruby-relocation-matrix")
    data = json.loads((keg / "bin" / "matrix.json").read_text())
    assert data["files"] == 4
    assert data["hardlink_skipped"] == 1
    by_path = {entry["path"]: entry for entry in data["matrix"]}
    assert by_path["macho64"]["kind"] == "macho"
    assert by_path["macho64"]["format"] == "macho64"
    assert by_path["macho64"]["arch"] == "x86_64"
    assert by_path["macho64"]["bits"] == 64
    assert by_path["fat.dylib"]["fat"] is True
    assert by_path["fat.dylib"]["format"] == "fat32"
    assert by_path["elf64"]["kind"] == "elf"
    assert by_path["elf64"]["format"] == "elf64"
    assert by_path["elf64"]["arch"] == "x86_64"
    assert by_path["elf32"]["format"] == "elf32"
    assert by_path["elf32"]["arch"] == "i386"
    macho_args = [arg for command in by_path["macho64"]["commands"] for arg in command]
    elf_args = [arg for command in by_path["elf64"]["commands"] for arg in command]
    assert "-change" in macho_args
    assert "-id" in macho_args
    assert "-add_rpath" in macho_args
    assert "--replace-needed" in elf_args
    assert "--set-interpreter" in elf_args
    assert "--set-rpath" in elf_args
    assert data["verified"] is True
    assert data["placeholder_count"] == 4
    assert data["placeholders_ok"] is True
    assert data["codesigned"] == 2
    assert data["codesign_verified"] is True
    assert data["entitlements"] is True
    assert data["identity"] == "Edge Identity"
    assert data["retarget"] == f"{tmp_path}/prefix/lib/new.dylib"
    assert data["restore_error"] == "RuntimeError|true"
    assert data["ignored_failed"] == 1
    assert data["ignored_restored"] is True


def test_patch_exotic_forms_metadata_and_patch_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    body = r'''
path = prefix/"patch-forms.rb"
path.write <<~'RUBY'
  class PatchForms < Formula
    version "3.0"
    patch "https://example.test/dynamic-#{version}.diff"
    patch :p0, "--- a/x\n+++ b/x\n"
    patch do
      url "https://example.test/block.diff"
      sha256 "abc123"
      strip :p2
      directory "patches"
      resolves "CVE-2024-1234", "GHSA-aaaa-bbbb-cccc"
      type :backport
      apply :p0
    end
    patch do
      file "local.patch"
    end
    patch :DATA
    def install; end
  end
RUBY
klass = Formulary.load_formula_from_path("patch-forms", path)
instance = klass.new
patches = instance.patches
dynamic = patches[0]
inline = patches[1]
block = patches[2]
local = patches[3]
data_patch = patches[4]
resource = PybrewResource.new(
  "res-patch",
  { "url" => "https://example.test/res.tar.gz",
    "patches" => [{ "url" => "https://example.test/res.diff" },
                  { "file" => "res-local.diff" }] }
)
(prefix/"patch-forms.txt").write [
  patches.length.to_s,
  dynamic.url,
  inline.strip.to_s,
  inline.data.to_s.start_with?("--- a/x").to_s,
  block.url, block.sha256, block.strip.to_s, block.directory.to_s,
  block.resolves.join(","), block.type.to_s, block.apply_value.to_s,
  block.to_hash["apply"].to_s,
  local.file.to_s,
  data_patch.data?.to_s,
  instance.patch_files.join(","),
  resource.patches.length.to_s,
  resource.patch_files.join(","),
  resource.patches?.to_s,
].join("\n")
'''
    keg = _run_raw_body(tmp_path, body, name="ruby-patch-forms")
    assert (keg / "patch-forms.txt").read_text().splitlines() == [
        "5",
        "https://example.test/dynamic-3.0.diff",
        "p0",
        "true",
        "https://example.test/block.diff",
        "abc123",
        "p2",
        "patches",
        "CVE-2024-1234,GHSA-aaaa-bbbb-cccc",
        "backport",
        "p0",
        "p0",
        "local.patch",
        "true",
        "local.patch",
        "2",
        "res-local.diff",
        "true",
    ]


def test_resource_stage_edge_forms_symlinks_staging_path_and_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    archive = tmp_path / "src" / "res-edge.tar.gz"
    archive.parent.mkdir(parents=True)
    with tarfile.open(archive, "w:gz") as tar:
        directory = tarfile.TarInfo("res-edge-1.0")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        tar.addfile(directory)
        data = b"edge content\n"
        entry = tarfile.TarInfo("res-edge-1.0/inner.txt")
        entry.size = len(data)
        entry.mode = 0o644
        tar.addfile(entry, io.BytesIO(data))
        link = tarfile.TarInfo("res-edge-1.0/link.txt")
        link.type = tarfile.SYMTYPE
        link.linkname = "inner.txt"
        link.mode = 0o777
        tar.addfile(link)
    formula = _formula(
        "ruby-resource-edge",
        "1.0",
        (
            '  name = "edge" + "-res"\n'
            '  res = resource(name)\n'
            '  stale = prefix/"edge-target"/"res-edge-1.0"/"inner.txt"\n'
            '  stale.dirname.mkpath\n'
            '  stale.write("stale")\n'
            '  target = prefix/"edge-target"\n'
            '  res.stage(target)\n'
            '  overwritten = (target/"res-edge-1.0"/"inner.txt").read == "edge content\\n"\n'
            '  symlink_preserved = File.symlink?((target/"res-edge-1.0"/"link.txt").to_s)\n'
            '  staging = prefix/"edge-staging"\n'
            '  res.stage(staging_path: staging)\n'
            '  staging_ok = File.symlink?((staging/"res-edge-1.0"/"link.txt").to_s)\n'
            '  block_value = res.stage(prefix/"edge-block") do |dir|\n'
            '    File.symlink?((dir/"res-edge-1.0"/"link.txt").to_s).to_s + "|" + dir.basename.to_s\n'
            '  end\n'
            '  both_error = begin\n'
            '    res.stage(prefix/"a", staging_path: prefix/"b")\n'
            '    "none"\n'
            '  rescue ArgumentError => error\n'
            '    "ArgumentError"\n'
            '  end\n'
            '  list_hit = resources[name].equal?(list_hit).to_s rescue "false"\n'
            '  (prefix/"resource-edge.txt").write [\n'
            '    res.name, overwritten.to_s, symlink_preserved.to_s,\n'
            '    staging_ok.to_s, block_value, both_error,\n'
            '    res.unpack.to_s == res.staged_path.to_s\n'
            '  ].join("\\n")\n'
        ),
    )
    formula.resources["edge-res"] = SourceSpec(
        url=archive.as_uri(), sha256=_sha256(archive)
    )
    keg = _build(tmp_path, formula, downloader=_downloader(tmp_path))
    assert (keg / "resource-edge.txt").read_text().splitlines() == [
        "edge-res",
        "true",
        "true",
        "true",
        "true|edge-block",
        "ArgumentError",
        "true",
    ]


# ---------------------------------------------------------------------------
# Closure wave: per-URL channel cache/TTL, factory order/migration edges and
# full Tab/SBOM/bottle-pour receipt parity.
# ---------------------------------------------------------------------------


def test_formulary_per_url_channel_cache_ttl_and_sidecar_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    counter_dir = tmp_path / "counters"
    counter_dir.mkdir()
    fake_curl = tmp_path / "per-url-curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        'headers=""\n'
        'prev=""\n'
        'url=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "-D" ]; then headers="$arg"; fi\n'
        '  case "$arg" in http*) url="$arg" ;; esac\n'
        '  prev="$arg"\n'
        'done\n'
        'case "$url" in\n'
        "  *stable*) name=stable ;;\n"
        "  *) name=beta ;;\n"
        "esac\n"
        'count_file="' + str(counter_dir) + '/$name.txt"\n'
        'count=$(cat "$count_file" 2>/dev/null || echo 0)\n'
        'printf \'%s\' "$((count + 1))" > "$count_file"\n'
        'case "$*" in\n'
        '  *If-None-Match*)\n'
        '    printf \'HTTP/1.1 304 Not Modified\\r\\nETag: "%s"\\r\\n\\r\\n\' "$name" > "$headers"\n'
        '    exit 0\n'
        '    ;;\n'
        'esac\n'
        'printf \'HTTP/1.1 200 OK\\r\\nETag: "%s"\\r\\n\\r\\n\' "$name" > "$headers"\n'
        'printf \'{"formulae":[{"name":"url-cache-one","desc":"%s desc","version":"1.0"}]}\' "$name"\n'
    )
    fake_curl.chmod(0o755)
    sidecar = tmp_path / "per-url-sidecar.json"
    monkeypatch.setenv("PYBREW_FORMULA_API_URL_STABLE", "https://api.example.test/stable.json")
    monkeypatch.setenv("PYBREW_FORMULA_API_URL_BETA", "https://api.example.test/beta.json")
    monkeypatch.setenv("PYBREW_FORMULA_API_CHANNELS", "stable,beta")
    monkeypatch.setenv("PYBREW_FORMULA_API_CURL", str(fake_curl))
    monkeypatch.setenv("PYBREW_FORMULA_API_SIDECAR", str(sidecar))
    monkeypatch.setenv("PYBREW_FORMULA_API_TTL", "3600")
    monkeypatch.setenv("FAKE_CACHE_DIR", str(counter_dir))
    body = (
        '  count = lambda do |name|\n'
        '    Pathname.new(ENV["FAKE_CACHE_DIR"]).join("#{name}.txt").read.to_i\n'
        '  end\n'
        '  Formulary.ensure_api_json_loaded\n'
        '  first = [count.call("stable"), count.call("beta")]\n'
        '  Formulary.ensure_api_json_loaded\n'
        '  cached = [count.call("stable"), count.call("beta")]\n'
        '  ENV["PYBREW_FORMULA_API_TTL"] = "0"\n'
        '  Formulary.ensure_api_json_loaded\n'
        '  refreshed = [count.call("stable"), count.call("beta")]\n'
        '  stable_url = "https://api.example.test/stable.json"\n'
        '  beta_url = "https://api.example.test/beta.json"\n'
        '  sidecar = JSON.parse(Pathname.new(ENV["PYBREW_FORMULA_API_SIDECAR"]).read)\n'
        '  read_stable = Formulary.remote_sidecar_read(stable_url)\n'
        '  read_beta = Formulary.remote_sidecar_read(beta_url)\n'
        '  merged = Formulary.formula_class_get_from_api("url-cache-one")\n'
        '  Formulary.clear_cache\n'
        '  cleared = !Pathname.new(ENV["PYBREW_FORMULA_API_SIDECAR"]).file?\n'
        '  (prefix/"per-url-cache.txt").write [\n'
        '    first.join(","), cached.join(","), refreshed.join(","),\n'
        '    sidecar["urls"].keys.sort.join(","),\n'
        '    sidecar["urls"][stable_url]["etag"].to_s,\n'
        '    sidecar["urls"][beta_url]["etag"].to_s,\n'
        '    (sidecar["urls"][stable_url]["fetched_at"].to_f > 0).to_s,\n'
        '    sidecar["etag"].to_s,\n'
        '    read_stable[:entries].length.to_s, read_beta[:entries].length.to_s,\n'
        '    merged.loaded_namespace.nil?.to_s, merged.loaded_version,\n'
        '    cleared.to_s\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-per-url-cache")
    assert (keg / "per-url-cache.txt").read_text().splitlines() == [
        "1,1",
        "1,1",
        "2,2",
        "https://api.example.test/beta.json,https://api.example.test/stable.json",
        '"stable"',
        '"beta"',
        "true",
        '"beta"',
        "1",
        "1",
        "false",
        "1.0",
        "true",
    ]


def test_factory_order_trace_tap_migration_and_qualified_api_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    tap_root = tmp_path / "taps" / "user1" / "alpha"
    (tap_root / "Formula").mkdir(parents=True)
    (tap_root / "Formula" / "oldname.rb").write_text(
        'class Oldname < Formula\n  desc "old formula"\n  version "1.0"\n  def install; end\nend\n'
    )
    (tap_root / "Formula" / "newtarget.rb").write_text(
        'class Newtarget < Formula\n  desc "new target"\n  version "2.0"\n  def install; end\nend\n'
    )
    api_file = tmp_path / "factory-api.json"
    api_file.write_text(
        json.dumps(
            {
                "formulae": [
                    {
                        "name": "api-only",
                        "desc": "api only",
                        "homepage": "https://example.test/api",
                        "version": "3.0",
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("PYBREW_TAPS", str(tap_root))
    monkeypatch.setenv("PYBREW_FORMULA_API_JSON", str(api_file))
    monkeypatch.setenv(
        "PYBREW_TAP_MIGRATIONS",
        json.dumps(
            {
                "user1/alpha/oldname": "user1/alpha/newtarget",
                "oldname": "user1/alpha/newtarget",
                "short-old": "user1/alpha/newtarget",
                "loop-a": "loop-b",
                "loop-b": "loop-a",
            }
        ),
    )
    body = (
        '  default_order = Formulary.factory_loader_order("user1/alpha/newtarget")\n'
        '    .map { |entry| entry["loader"] }.join(",")\n'
        '  ENV["PYBREW_FORMULA_API_FIRST"] = "1"\n'
        '  api_first = Formulary.factory_loader_order("api-only")\n'
        '    .map { |entry| entry["loader"] }.join(",")\n'
        '  ENV.delete("PYBREW_FORMULA_API_FIRST")\n'
        '  migrated = Formulary.factory("user1/alpha/oldname")\n'
        '  migration_trace = Formulary.factory_trace["migration"].to_s\n'
        '  tap_trace = Formulary.factory_trace["choice"].to_s\n'
        '  short_migrated = Formulary.factory("short-old")\n'
        '  name_type = Formulary.tap_formula_name_type("oldname")\n'
        '  loop_result = Formulary.factory("loop-a")\n'
        '  loop_ok = !loop_result.nil?\n'
        '  api_fallback = Formulary.factory("user1/alpha/api-only")\n'
        '  Formulary.enable_factory_cache!\n'
        '  first_cached = Formulary.factory("user1/alpha/newtarget")\n'
        '  second_cached = Formulary.factory("user1/alpha/newtarget")\n'
        '  cache_hit = Formulary.factory_trace["cache_hit"].to_s\n'
        '  (prefix/"factory-edges.txt").write [\n'
        '    default_order, api_first, migrated.loaded_desc, migration_trace,\n'
        '    tap_trace, short_migrated.loaded_desc,\n'
        '    name_type[0].to_s, name_type[2].to_s,\n'
        '    loop_ok.to_s, api_fallback.loaded_desc,\n'
        '    first_cached.equal?(second_cached).to_s, cache_hit\n'
        '  ].join("\\n")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-factory-edges")
    assert (keg / "factory-edges.txt").read_text().splitlines() == [
        "bottle,uri,tap,path,api,name",
        "bottle,uri,api,tap,path,name",
        "new target",
        "user1/alpha/newtarget",
        "tap",
        "new target",
        "newtarget",
        "migration",
        "true",
        "api only",
        "true",
        "true",
    ]


def test_receipt_full_tab_sbom_and_bottle_pour_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    formula = _formula(
        "ruby-receipt-full",
        "1.0",
        (
            '  path = prefix/"receipt-full.rb"\n'
            '  path.write <<~RUBY\n'
            '    class ReceiptFull < Formula\n'
            '      version "4.0"\n'
            '      head "https://example.test/head.tar.gz"\n'
            '      bottle do\n'
            '        sha256 cellar: :any, arm64_sonoma: "bottle-sha"\n'
            '      end\n'
            '      depends_on "dep"\n'
            '      install do\n'
            '        bin.mkpath\n'
            '        (bin/"tool").write("#!/bin/sh\\necho full\\n")\n'
            '      end\n'
            '    end\n'
            '  RUBY\n'
            '  klass = Formulary.load_formula_from_path("receipt-full", path)\n'
            '  installer = FormulaInstaller.new(\n'
            '    klass.new, installed_on_request: true, used_options: ["--with-x"],\n'
            '    build_bottle: true\n'
            '  )\n'
            '  pour_before = installer.pour_bottle?.to_s\n'
            '  pour_nil = installer.pour.nil?.to_s\n'
            '  installer.install\n'
            '  receipt_path = HOMEBREW_PREFIX/"Cellar"/"receipt-full"/"4.0"/"INSTALL_RECEIPT.json"\n'
            '  receipt = JSON.parse(receipt_path.read)\n'
            '  tab = Tab.for_keg(receipt_path.dirname)\n'
            '  files = receipt.dig("sbom", "files") || {}\n'
            '  (prefix/"receipt-full.txt").write [\n'
            '    receipt["built_as_bottle"].to_s,\n'
            '    receipt["poured_from_bottle"].to_s,\n'
            '    receipt["HEAD"].to_s,\n'
            '    receipt["version_scheme"].to_s,\n'
            '    receipt["loaded_from_api"].to_s,\n'
            '    receipt["used_options"].join(","),\n'
            '    receipt["unused_options"].join(","),\n'
            '    receipt["cxxstdlib"]["compiler"],\n'
            '    receipt["cxxstdlib"]["stdlib"],\n'
            '    receipt["built_on"]["os"].to_s.empty?.to_s,\n'
            '    receipt["built_on"]["cpu_family"].to_s.empty?.to_s,\n'
            '    receipt["bottle"]["sha256"],\n'
            '    receipt["bottle"]["cellar"],\n'
            '    receipt["bottle"]["tag"],\n'
            '    files.key?("bin/tool").to_s,\n'
            '    tab.bottle["sha256"].to_s,\n'
            '    tab.cxxstdlib["compiler"].to_s,\n'
            '    tab.head?.to_s,\n'
            '    tab.used_options.join(","),\n'
            '    tab.sbom_files.key?("bin/tool").to_s,\n'
            '    tab.built_on["os"].to_s.empty?.to_s,\n'
            '    installer.install_bottle_for?(Object.new, false).to_s,\n'
            '    pour_before, pour_nil\n'
            '  ].join("\\n")\n'
        ),
    )
    keg = _build(tmp_path, formula)
    lines = (keg / "receipt-full.txt").read_text().splitlines()
    assert lines[0:7] == ["true", "false", "true", "0", "false", "--with-x", ""]
    assert lines[7] == "clang"
    assert lines[8] == ("libc++" if is_macos() else "libstdc++")
    assert lines[9:14] == ["false", "false", "bottle-sha", "any", "arm64_sonoma"]
    assert lines[14:] == [
        "true",
        "bottle-sha",
        "clang",
        "true",
        "--with-x",
        "true",
        "false",
        "false",
        "false",
        "true",
    ]


# ---------------------------------------------------------------------------
# Sandbox boundary for the arbitrary-Ruby interpreter subprocess.
# ---------------------------------------------------------------------------


def _fake_sandbox_exec(tmp_path: Path, log: Path, *, exit_code: int = 0) -> Path:
    path = tmp_path / "sandbox-exec"
    path.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" >> "' + str(log) + '"\n'
        "exit " + str(exit_code) + "\n"
    )
    path.chmod(0o755)
    return path


def test_arbitrary_ruby_subprocess_wrapped_by_fake_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    log = tmp_path / "sandbox-exec.log"
    fake = tmp_path / "sandbox-exec"
    fake.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" >> "' + str(log) + '"\n'
        "shift 2\n"
        'exec "$@"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PYBREW_SANDBOX", "1")
    monkeypatch.setenv("PYBREW_SANDBOX_PATH", str(fake))
    body = (
        '  (prefix/"sandboxed.txt").write [\n'
        '    ENV["PYBREW_SANDBOX"].to_s, "wrapped"\n'
        '  ].join("|")\n'
    )
    keg = _run_raw_body(tmp_path, body, name="ruby-sandboxed-body")
    assert (keg / "sandboxed.txt").read_text() == "1|wrapped"
    text = log.read_text()
    assert "-p" in text
    assert "(allow default)" in text
    assert "(deny network*)" in text
    assert str(tmp_path / "build") in text
    assert str(keg) in text
    assert "file-write*" in text
    assert "install.rb" in text
    assert text.count("(allow file-write* (subpath") >= 3

    log.unlink()
    monkeypatch.delenv("PYBREW_SANDBOX")
    keg2 = _run_raw_body(
        tmp_path,
        '  (prefix/"unsandboxed.txt").write "plain"\n',
        name="ruby-unsandboxed-body",
    )
    assert (keg2 / "unsandboxed.txt").read_text() == "plain"
    assert not log.exists()


def test_arbitrary_ruby_sandbox_strict_profile_and_bwrap_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    strict_log = tmp_path / "strict.log"
    strict_sandbox = tmp_path / "sandbox-exec"
    strict_sandbox.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" >> "' + str(strict_log) + '"\n'
        "shift 2\n"
        'exec "$@"\n'
    )
    strict_sandbox.chmod(0o755)
    monkeypatch.setenv("PYBREW_SANDBOX", "1")
    monkeypatch.setenv("PYBREW_SANDBOX_STRICT", "1")
    monkeypatch.setenv("PYBREW_SANDBOX_PATH", str(strict_sandbox))
    body = '  (prefix/"strict.txt").write "strict"\n'
    keg = _run_raw_body(tmp_path, body, name="ruby-sandbox-strict")
    assert (keg / "strict.txt").read_text() == "strict"
    strict_text = strict_log.read_text()
    assert "(deny default)" in strict_text
    assert "(allow process-exec)" in strict_text
    assert "(deny network*)" in strict_text

    bwrap_log = tmp_path / "bwrap.log"
    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text(
        "#!" + _RUBY + "\n"
        'File.open(ENV.fetch("FAKE_BWRAP_LOG"), "a") { |io| io.puts(ARGV.join("\\u0001")) }\n'
        'index = ARGV.index("--")\n'
        "if index\n"
        '  exec(*ARGV[(index + 1)..-1])\n'
        "end\n"
        "exit 0\n"
    )
    fake_bwrap.chmod(0o755)
    monkeypatch.delenv("PYBREW_SANDBOX_STRICT")
    monkeypatch.setenv("PYBREW_SANDBOX_PATH", str(fake_bwrap))
    monkeypatch.setenv("FAKE_BWRAP_LOG", str(bwrap_log))
    keg2 = _run_raw_body(tmp_path, body, name="ruby-sandbox-bwrap")
    assert (keg2 / "strict.txt").read_text() == "strict"
    bwrap_args = bwrap_log.read_text()
    assert "--unshare-net" in bwrap_args
    assert "--cap-drop" in bwrap_args
    assert "--bind" in bwrap_args
    assert str(tmp_path / "build") in bwrap_args
    assert "--chdir" in bwrap_args
    assert "install.rb" in bwrap_args


def test_arbitrary_ruby_sandbox_missing_backend_is_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    monkeypatch.setenv("PYBREW_SANDBOX", "1")
    monkeypatch.setenv("PYBREW_SANDBOX_PATH", str(tmp_path / "does-not-exist"))
    with pytest.raises(BuildError, match="no sandbox backend is available"):
        _run_raw_body(
            tmp_path,
            '  (prefix/"never.txt").write "never"\n',
            name="ruby-sandbox-missing",
        )
    assert not (tmp_path / "keg" / "never.txt").exists()


def test_arbitrary_ruby_sandbox_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    log = tmp_path / "failing-sandbox.log"
    fake = _fake_sandbox_exec(tmp_path, log, exit_code=7)
    monkeypatch.setenv("PYBREW_SANDBOX", "1")
    monkeypatch.setenv("PYBREW_SANDBOX_PATH", str(fake))
    with pytest.raises(BuildError, match="sandbox backend 'sandbox-exec' exited with 7"):
        _run_raw_body(
            tmp_path,
            '  (prefix/"never.txt").write "never"\n',
            name="ruby-sandbox-fail",
        )




def test_arbitrary_ruby_build_options_reflect_environment(tmp_path: Path) -> None:
    formula = _formula(
        "ruby-build-options",
        "1.0",
        r'''
  (prefix/"build-options").write [
    build.with?("ssl"),
    build.without?("readline"),
    build.with?("readline"),
    build.head?,
    build.bottle?,
    build.any_args_or_options?,
  ].join("|")
''',
    )
    formula.options = ["with-ssl", "without-readline"]
    build_root = tmp_path / "build"
    keg = tmp_path / "keg"
    build_root.mkdir()
    keg.mkdir()
    execute_install_body(
        formula,
        _config(tmp_path),
        build_root,
        keg,
        env={
            "PYBREW_BUILD_OPTIONS": "with-ssl,without-readline,build-bottle",
            "PYBREW_BUILD_HEAD": "0",
        },
    )
    assert (keg / "build-options").read_text() == "true|true|false|false|true|true"
