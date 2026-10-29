"""Tests for the expanded Homebrew formula Ruby API shim.

The shim is only reachable through the opt-in
``PYBREW_ALLOW_ARBITRARY_RUBY=1`` bridge; the default declarative parser must
keep behaving exactly as before.  Every fixture here is synthetic and local:
no network access and no writes outside ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import struct
from pathlib import Path

import pytest

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
) -> Path:
    build_root = build_root or tmp_path / "build"
    build_root.mkdir(exist_ok=True)
    keg = keg or tmp_path / "keg"
    keg.mkdir(exist_ok=True)
    build_source(formula, _config(tmp_path), build_root, keg, verbose=verbose)
    return keg


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
