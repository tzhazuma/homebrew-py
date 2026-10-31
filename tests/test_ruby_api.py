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
            '    ref.rpath.to_s, ref.common_sandbox_env.key?(:HOME).to_s,\n'
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
            '    CompilerSelector.preferred_gcc,\n'
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
        "gcc",
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
