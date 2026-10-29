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
from pybrew.models import Dependency, Formula, SourceSpec, is_linux, is_macos
from pybrew.paths import Config
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
