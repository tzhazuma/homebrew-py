"""Tests for opt-in cask preflight/postflight scripts.

No test executes a captured cask script body and no test evaluates untrusted
cask source: every install that opts in points ``PYBREW_RUBY`` at a throwaway
POSIX shell script which records its argv and exits with a configurable status.
One compile-only test (``ruby -c``) checks that the generated shim parses; it
never runs a script body.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pybrew.cask import CaskError, parse_cask_source
from pybrew.cask_installer import CaskInstaller, CaskLoader
from pybrew.paths import Config
from pybrew.taps import discover_taps

# Fake Ruby interpreter.  The real shim is argv[1]; argv[2] is the script name,
# argv[3..8] are token/version/appdir/staged_path/bin/prefix and argv[9] is the
# captured body file.  It only records that contract, never interprets it.
FAKE_RUBY = """#!/bin/sh
name="$2"
if [ -d "$5/Scripty.app" ]; then app_state=present; else app_state=absent; fi
{
  printf 'name=%s\\n' "$name"
  printf 'token=%s\\n' "$3"
  printf 'version=%s\\n' "$4"
  printf 'appdir=%s\\n' "$5"
  printf 'staged=%s\\n' "$6"
  printf 'bin=%s\\n' "$7"
  printf 'prefix=%s\\n' "$8"
  printf 'cwd=%s\\n' "$PWD"
  printf 'app_state=%s\\n' "$app_state"
  printf 'body=%s\\n' "$(tr '\\n' ' ' < "$9")"
  printf '%s\\n' '---'
} >> "$FAKE_RUBY_LOG"
status=0
case "$name" in
  preflight) status="${FAKE_PREFLIGHT_EXIT:-0}" ;;
  postflight) status="${FAKE_POSTFLIGHT_EXIT:-0}" ;;
  uninstall_preflight) status="${FAKE_UNINSTALL_PREFLIGHT_EXIT:-0}" ;;
  uninstall_postflight) status="${FAKE_UNINSTALL_POSTFLIGHT_EXIT:-0}" ;;
esac
if [ "$status" -ne 0 ]; then
  echo "pybrew-script-failure $name" >&2
fi
exit "$status"
"""

SCRIPTY_SOURCE = '''cask "scripty" do
  version "1.0"
  sha256 "{digest}"
  url "{url}"

  preflight do
    system_command "/bin/echo", args: ["preflight"]
  end

  postflight do
    system "true"
  end

  app "Scripty.app"
end
'''


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
    body: str = "",
    *,
    stage_only: bool = False,
) -> Path:
    lines = [f'cask "{token}" do', f'  version "{version}"']
    if stage_only:
        lines.append("  stage_only true")
    else:
        lines.append(f'  sha256 "{digest}"')
        lines.append(f'  url "{archive.as_uri()}"')
    if body:
        lines.append(body.rstrip("\n"))
    if not stage_only:
        lines.append('  app "Scripty.app"')
    lines.append("end")
    path = tap / "Casks" / "s" / f"{token}.rb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def script_env(tmp_path: Path) -> SimpleNamespace:
    tap = tmp_path / "tap"
    archive = tmp_path / "Scripty-1.0.zip"
    digest = _make_zip(
        archive,
        {
            "Scripty.app/Contents/Info.plist": b"<plist><dict/></plist>",
            "Scripty.app/Contents/MacOS/Scripty": b"#!/bin/sh\necho scripty\n",
        },
    )
    cask_path = tap / "Casks" / "s" / "scripty.rb"
    cask_path.parent.mkdir(parents=True, exist_ok=True)
    cask_path.write_text(
        SCRIPTY_SOURCE.format(digest=digest, url=archive.as_uri()),
        encoding="utf-8",
    )
    config = Config.default(
        prefix=tmp_path / "prefix",
        taps=[tap],
        cache=tmp_path / "cache",
    )
    config.ensure()
    return SimpleNamespace(
        tmp_path=tmp_path,
        tap=tap,
        archive=archive,
        digest=digest,
        config=config,
        loader=CaskLoader(discover_taps([tap])),
    )


def _fake_ruby(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    script = tmp_path / "fake-ruby"
    log = tmp_path / "fake-ruby.log"
    script.write_text(FAKE_RUBY, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PYBREW_RUBY", str(script))
    monkeypatch.setenv("FAKE_RUBY_LOG", str(log))
    return script, log


def _read_log(path: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "---":
            entries.append(current)
            current = {}
        elif "=" in line:
            key, _, value = line.partition("=")
            current[key] = value
    if current:
        entries.append(current)
    return entries


def _receipt(room: Path) -> dict:
    return json.loads((room / ".metadata" / "receipt.json").read_text(encoding="utf-8"))


# -- parser -------------------------------------------------------------


def test_parser_captures_script_bodies_without_changing_artifacts() -> None:
    source = '''cask "scripty" do
  version "2.0"
  sha256 "deadbeef"
  url "https://example.com/Scripty-2.0.zip"

  preflight do
    system_command "/usr/bin/xattr", args: ["-dr", "com.apple.quarantine", "#{appdir}/Scripty.app"]
  end

  app "Scripty.app"

  postflight do
    system "true"
  end

  uninstall_preflight do
    FileUtils.rm_rf "#{staged_path}/tmp"
  end

  uninstall_postflight do
    puts "done"
  end
end
'''
    cask = parse_cask_source(source)

    assert cask.preflight is not None
    assert "system_command" in cask.preflight
    assert "#{appdir}" in cask.preflight
    assert cask.postflight is not None and 'system "true"' in cask.postflight
    assert cask.uninstall_preflight is not None
    assert "FileUtils.rm_rf" in cask.uninstall_preflight
    assert cask.uninstall_postflight is not None
    assert 'puts "done"' in cask.uninstall_postflight
    # The script blocks must not swallow the stanzas around them.
    assert [artifact.kind for artifact in cask.artifacts] == ["app"]
    assert cask.artifacts[0].source == "Scripty.app"
    assert set(cask.scripts) == {
        "preflight",
        "postflight",
        "uninstall_preflight",
        "uninstall_postflight",
    }


def test_parser_ignores_scripts_for_casks_without_them() -> None:
    cask = parse_cask_source(
        'cask "plain" do\n  version "1.0"\n  app "Plain.app"\nend\n'
    )
    assert cask.scripts == {}
    assert cask.preflight is None
    assert cask.postflight is None


def test_parser_captures_empty_and_nested_script_bodies() -> None:
    cask = parse_cask_source(
        'cask "nested" do\n'
        '  version "1.0"\n'
        "  preflight do\n"
        "    if ENV[\"PYBREW_TEST\"]\n"
        "      puts \"nested\"\n"
        "    end\n"
        "  end\n"
        "  postflight do\n"
        "  end\n"
        "  app \"Nested.app\"\n"
        "end\n"
    )
    assert cask.preflight is not None and "nested" in cask.preflight
    assert not cask.postflight.strip()
    assert [artifact.source for artifact in cask.artifacts] == ["Nested.app"]


# -- install: default no-op --------------------------------------------


def test_default_install_does_not_run_scripts(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = script_env.tmp_path / "ruby-called"
    fake = script_env.tmp_path / "fake-ruby"
    fake.write_text(f"#!/bin/sh\ntouch \"{marker}\"\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PYBREW_RUBY", str(fake))

    installer = CaskInstaller(script_env.config)
    result = installer.install(script_env.loader.load("scripty"))

    assert result.skipped is False
    assert (script_env.config.applications / "Scripty.app").is_dir()
    assert not marker.exists()
    receipt = _receipt(result.room)
    assert receipt["scripts_requested"] is False
    assert receipt["script_results"] == {}


def test_default_install_does_not_run_expanded_script_api(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_cask(
        script_env.tap,
        "expanded",
        "1.0",
        script_env.digest,
        script_env.archive,
        "  preflight do\n"
        '    Utils.safe_system "/bin/true"\n'
        '    puts MacOS.version_symbol\n'
        "  end\n"
        "  postflight do\n"
        '    Utils.quiet_system "/bin/false"\n'
        "    puts OS.version\n"
        "    puts Hardware::CPU.cores\n"
        "  end",
    )
    marker = script_env.tmp_path / "ruby-called"
    fake = script_env.tmp_path / "fake-ruby"
    fake.write_text(f"#!/bin/sh\ntouch \"{marker}\"\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PYBREW_RUBY", str(fake))

    installer = CaskInstaller(script_env.config)
    result = installer.install(script_env.loader.load("expanded"))

    assert not marker.exists()
    assert _receipt(result.room)["scripts_requested"] is False


def test_run_scripts_enabled_without_scripts_never_needs_ruby(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYBREW_RUBY", str(script_env.tmp_path / "missing-ruby"))
    plain = script_env.tap / "Casks" / "s" / "plain.rb"
    plain.write_text(
        'cask "plain" do\n  version "1.0"\n  stage_only true\nend\n',
        encoding="utf-8",
    )

    installer = CaskInstaller(script_env.config, run_scripts=True)
    result = installer.install(script_env.loader.load("plain"))

    assert result.skipped is False
    receipt = _receipt(result.room)
    assert receipt["scripts_requested"] is True
    assert receipt["script_results"] == {}
    assert receipt["installed"] == []


# -- install: opt-in execution -----------------------------------------


def test_preflight_and_postflight_run_in_order_with_expected_argv(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, log = _fake_ruby(script_env.tmp_path, monkeypatch)

    installer = CaskInstaller(script_env.config, run_scripts=True)
    result = installer.install(script_env.loader.load("scripty"))

    entries = _read_log(log)
    assert [entry["name"] for entry in entries] == ["preflight", "postflight"]
    preflight, postflight = entries
    # Preflight runs before the app artifact exists; postflight after it does.
    assert preflight["app_state"] == "absent"
    assert postflight["app_state"] == "present"

    assert preflight["token"] == "scripty"
    assert preflight["version"] == "1.0"
    assert preflight["appdir"] == str(script_env.config.applications)
    assert preflight["staged"] == str(result.room)
    assert preflight["bin"] == str(script_env.config.bin)
    assert preflight["prefix"] == str(script_env.config.prefix)
    # Scripts run in the extracted archive root (both phases, same root).
    assert preflight["cwd"] == postflight["cwd"]
    assert Path(preflight["cwd"]).name.startswith("pybrew-cask-")
    assert "system_command" in preflight["body"]
    assert "preflight" in preflight["body"]
    assert 'system "true"' in postflight["body"]

    receipt = _receipt(result.room)
    assert receipt["scripts_requested"] is True
    assert receipt["script_results"] == {
        "preflight": {"status": "ok", "exit_code": 0},
        "postflight": {"status": "ok", "exit_code": 0},
    }
    assert (script_env.config.applications / "Scripty.app").is_dir()


def test_stage_only_bookkeeping_cask_runs_scripts(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_cask(
        script_env.tap,
        "bare",
        "1.0",
        "",
        script_env.archive,
        "  preflight do\n    puts \"pre\"\n  end\n"
        "  postflight do\n    puts \"post\"\n  end",
        stage_only=True,
    )
    _, log = _fake_ruby(script_env.tmp_path, monkeypatch)

    installer = CaskInstaller(script_env.config, run_scripts=True)
    result = installer.install(script_env.loader.load("bare"))

    entries = _read_log(log)
    assert [entry["name"] for entry in entries] == ["preflight", "postflight"]
    # No archive is extracted for a bookkeeping-only cask: the room is cwd.
    assert entries[0]["cwd"] == str(result.room)
    receipt = _receipt(result.room)
    assert receipt["stage_only"] is True
    assert receipt["installed"] == []
    assert receipt["scripts_requested"] is True
    assert set(receipt["script_results"]) == {"preflight", "postflight"}


# -- install: failures -------------------------------------------------


def test_preflight_failure_aborts_before_artifacts(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, log = _fake_ruby(script_env.tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_PREFLIGHT_EXIT", "7")

    installer = CaskInstaller(script_env.config, run_scripts=True)
    with pytest.raises(CaskError) as excinfo:
        installer.install(script_env.loader.load("scripty"))

    message = str(excinfo.value)
    assert "preflight" in message
    assert "exit 7" in message
    assert "pybrew-script-failure" in message  # marker printed by the fake ruby
    assert [entry["name"] for entry in _read_log(log)] == ["preflight"]
    assert not (script_env.config.applications / "Scripty.app").exists()
    room = script_env.config.caskroom / "scripty" / "1.0"
    assert not (room / ".metadata" / "receipt.json").exists()
    # The empty room is removed so a retry is not reported as installed.
    assert not room.exists()


def test_preflight_failure_can_be_retried(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, log = _fake_ruby(script_env.tmp_path, monkeypatch)
    installer = CaskInstaller(script_env.config, run_scripts=True)
    cask = script_env.loader.load("scripty")

    monkeypatch.setenv("FAKE_PREFLIGHT_EXIT", "1")
    with pytest.raises(CaskError):
        installer.install(cask)

    monkeypatch.setenv("FAKE_PREFLIGHT_EXIT", "0")
    result = installer.install(script_env.loader.load("scripty"))
    assert result.skipped is False
    assert [entry["name"] for entry in _read_log(log)] == [
        "preflight",
        "preflight",
        "postflight",
    ]
    assert (script_env.config.applications / "Scripty.app").is_dir()


def test_postflight_failure_aborts_install(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, log = _fake_ruby(script_env.tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_POSTFLIGHT_EXIT", "3")

    installer = CaskInstaller(script_env.config, run_scripts=True)
    with pytest.raises(CaskError) as excinfo:
        installer.install(script_env.loader.load("scripty"))

    message = str(excinfo.value)
    assert "postflight" in message
    assert "exit 3" in message
    assert [entry["name"] for entry in _read_log(log)] == ["preflight", "postflight"]
    # Artifacts were installed before postflight ran, but the install failed
    # and no receipt was written.
    assert (script_env.config.applications / "Scripty.app").is_dir()
    room = script_env.config.caskroom / "scripty" / "1.0"
    assert not (room / ".metadata" / "receipt.json").exists()


def test_postflight_not_run_when_artifact_install_fails(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = script_env.tmp_path / "Broken-1.0.zip"
    digest = _make_zip(broken, {"Other.app/Contents/Info.plist": b"<plist/>"})
    _write_cask(
        script_env.tap,
        "broken",
        "1.0",
        digest,
        broken,
        "  preflight do\n    puts \"pre\"\n  end\n"
        "  postflight do\n    puts \"post\"\n  end",
    )
    _, log = _fake_ruby(script_env.tmp_path, monkeypatch)

    installer = CaskInstaller(script_env.config, run_scripts=True)
    with pytest.raises(CaskError, match="not found"):
        installer.install(script_env.loader.load("broken"))

    # A failed artifact install must not reach postflight.
    assert [entry["name"] for entry in _read_log(log)] == ["preflight"]
    assert not (script_env.config.applications / "Scripty.app").exists()


def test_missing_ruby_raises_clear_error(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = script_env.tmp_path / "missing-ruby"
    monkeypatch.setenv("PYBREW_RUBY", str(missing))

    installer = CaskInstaller(script_env.config, run_scripts=True)
    with pytest.raises(CaskError) as excinfo:
        installer.install(script_env.loader.load("scripty"))

    message = str(excinfo.value)
    assert "preflight" in message
    assert "Ruby" in message
    assert "PYBREW_RUBY" in message
    assert not (script_env.config.applications / "Scripty.app").exists()


# -- shim contents ------------------------------------------------------


def test_ruby_shim_exposes_common_cask_script_api() -> None:
    from pybrew.cask_installer import _CASK_SCRIPT_SHIM

    for method in (
        "system_command",
        "system_command!",
        "system",
        "shell_command",
        "ohai",
        "opoo",
    ):
        assert f"def {method}" in _CASK_SCRIPT_SHIM
    for library in ("fileutils", "pathname", "open3"):
        assert f'require "{library}"' in _CASK_SCRIPT_SHIM
    for accessor in ("token", "version", "appdir", "staged_path", "bin", "prefix"):
        assert accessor in _CASK_SCRIPT_SHIM
    # The captured body is read from a file, never interpolated into the shim.
    assert "instance_eval(File.read(body_path)" in _CASK_SCRIPT_SHIM


def test_ruby_shim_exposes_expanded_platform_and_utils_api() -> None:
    from pybrew.cask_installer import _CASK_SCRIPT_SHIM

    for snippet in (
        "module Utils",
        "def safe_system",
        "def quiet_system",
        "module MacOS",
        "def self.version",
        "def self.version_symbol",
        "def self.full_version",
        "module OS",
        "def self.version",
        "module Hardware",
        "module CPU",
        "def self.cores",
        'require "shellwords"',
        'require "fileutils"',
        'require "pathname"',
    ):
        assert snippet in _CASK_SCRIPT_SHIM
    # The required libraries remain reachable from the evaluated body.
    for constant in ("Shellwords", "FileUtils", "Pathname"):
        assert constant in _CASK_SCRIPT_SHIM
    # Non-interactive only: no sudo helper is part of the shim API.
    assert "def sudo" not in _CASK_SCRIPT_SHIM


def test_generated_shim_file_contains_expanded_api(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = script_env.tmp_path / "captured-shim.rb"
    fake = script_env.tmp_path / "capture-ruby"
    fake.write_text(
        '#!/bin/sh\ncp "$1" "$FAKE_SHIM_CAPTURE"\nexit 0\n', encoding="utf-8"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PYBREW_RUBY", str(fake))
    monkeypatch.setenv("FAKE_SHIM_CAPTURE", str(captured))

    installer = CaskInstaller(script_env.config, run_scripts=True)
    installer.install(script_env.loader.load("scripty"))

    shim = captured.read_text(encoding="utf-8")
    for snippet in (
        "module Utils",
        "module MacOS",
        "module OS",
        "module Hardware",
        "def self.cores",
        "def self.version_symbol",
        "def self.full_version",
    ):
        assert snippet in shim
    assert "def sudo" not in shim


# -- shim: command helpers and Pathname accessors ----------------------


def test_ruby_shim_command_helpers_support_env_chdir_and_positional_forms() -> None:
    from pybrew.cask_installer import _CASK_SCRIPT_SHIM

    for snippet in (
        "module PybrewCaskSystem",
        "def stringify_environment",
        'def normalize(args, options = {}, label = "system")',
        'def run(args, options = {}, label = "system")',
        "environment = arguments.shift if arguments.first.is_a?(Hash)",
        "options.delete(:env)",
        "options.delete(:chdir)",
        "options[:chdir] = chdir.to_s unless chdir.nil?",
        "def system_command(command = nil, *positional, **options)",
        "args_option = options.delete(:args)",
        "arguments = [command, *Array(args_option), *positional]",
        "Open3.capture3(environment, *argv, **spawn_options)",
        "def system_command!(*args, **options)",
        "def system(*args, **options)",
        'PybrewCaskSystem.run(args, options, "system")',
        "Kernel.system(environment, *command, **spawn_options)",
    ):
        assert snippet in _CASK_SCRIPT_SHIM
    # Utils uses the same normalisation, so it accepts env:/chdir: as well.
    assert "PybrewCaskSystem.run(args, options, \"Utils.safe_system\")" in _CASK_SCRIPT_SHIM
    assert '"Utils.quiet_system"' in _CASK_SCRIPT_SHIM


def test_ruby_shim_exposes_pathname_cask_accessors() -> None:
    from pybrew.cask_installer import _CASK_SCRIPT_SHIM

    assert "attr_reader :token, :version, :bin, :prefix" in _CASK_SCRIPT_SHIM
    for accessor in ("appdir", "staged_path"):
        assert f"def {accessor}" in _CASK_SCRIPT_SHIM
    # Both accessors are Pathname objects, matching the Homebrew cask DSL.
    assert "@appdir = Pathname(appdir)" in _CASK_SCRIPT_SHIM
    assert "@staged_path = Pathname(staged_path)" in _CASK_SCRIPT_SHIM
    assert "@bin = Pathname(bin)" in _CASK_SCRIPT_SHIM
    assert "@prefix = Pathname(prefix)" in _CASK_SCRIPT_SHIM


def test_ruby_shim_has_no_sudo_execution_path() -> None:
    from pybrew.cask_installer import _CASK_SCRIPT_SHIM

    # No sudo helper and no code path that rewrites an argv to run sudo.
    assert "def sudo" not in _CASK_SCRIPT_SHIM
    assert "argv.unshift" not in _CASK_SCRIPT_SHIM
    assert "Process.uid" not in _CASK_SCRIPT_SHIM
    # Requesting sudo fails loudly rather than silently dropping the option.
    assert "sudo is not supported" in _CASK_SCRIPT_SHIM


def test_generated_shim_is_valid_ruby_syntax_without_running_a_body() -> None:
    """Compile-only check: ``ruby -c`` parses the shim, nothing is evaluated."""

    ruby = shutil.which("ruby")
    if ruby is None:
        pytest.skip("Ruby is not available for a compile-only shim check")
    from pybrew.cask_installer import _CASK_SCRIPT_SHIM

    with tempfile.TemporaryDirectory(prefix="pybrew-shim-syntax-") as temporary:
        shim = Path(temporary) / "cask_script_shim.rb"
        shim.write_text(_CASK_SCRIPT_SHIM, encoding="utf-8")
        proc = subprocess.run(
            [ruby, "-c", str(shim)],
            text=True,
            capture_output=True,
        )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "Syntax OK" in proc.stdout


# -- install: expanded API default no-op and exact argv ----------------

# Records every argument position and copies the generated shim plus the
# captured body, proving they are separate files.  It never interprets Ruby.
EXACT_ARGV_RUBY = """#!/bin/sh
mkdir -p "$FAKE_BODY_CAPTURE_DIR"
{
  printf 'argc=%s\\n' "$#"
  index=1
  for argument in "$@"; do
    printf 'arg%s=%s\\n' "$index" "$argument"
    index=$((index + 1))
  done
  printf 'cwd=%s\\n' "$PWD"
  printf '%s\\n' '---'
} >> "$FAKE_RUBY_LOG"
cp "$1" "$FAKE_SHIM_CAPTURE"
cp "$9" "$FAKE_BODY_CAPTURE_DIR/$2.rb"
exit 0
"""


def test_default_install_does_not_run_env_chdir_script_api(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_cask(
        script_env.tap,
        "envchdir",
        "1.0",
        script_env.digest,
        script_env.archive,
        "  preflight do\n"
        '    system_command "/usr/bin/true", "positional", args: ["keyword"],\n'
        '                   env: { "PYBREW_TEST" => "1" }, chdir: staged_path.to_s\n'
        "  end\n"
        "  postflight do\n"
        '    system "/usr/bin/true", env: { "PYBREW_TEST" => "1" }, chdir: appdir.to_s\n'
        "  end",
    )
    marker = script_env.tmp_path / "ruby-called"
    fake = script_env.tmp_path / "fake-ruby"
    fake.write_text(f"#!/bin/sh\ntouch \"{marker}\"\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PYBREW_RUBY", str(fake))

    installer = CaskInstaller(script_env.config)
    result = installer.install(script_env.loader.load("envchdir"))

    assert result.skipped is False
    assert not marker.exists()
    assert (script_env.config.applications / "Scripty.app").is_dir()
    receipt = _receipt(result.room)
    assert receipt["scripts_requested"] is False
    assert receipt["script_results"] == {}


def test_run_scripts_enabled_executes_env_chdir_script_api(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_cask(
        script_env.tap,
        "envchdirrun",
        "1.0",
        script_env.digest,
        script_env.archive,
        "  preflight do\n"
        '    system_command "/usr/bin/true", "positional", args: ["keyword"],\n'
        '                   env: { "PYBREW_TEST" => "1" }, chdir: staged_path.to_s\n'
        "  end\n"
        "  postflight do\n"
        '    system "/usr/bin/true", env: { "PYBREW_TEST" => "1" }, chdir: appdir.to_s\n'
        "  end",
    )
    _, log = _fake_ruby(script_env.tmp_path, monkeypatch)

    installer = CaskInstaller(script_env.config, run_scripts=True)
    result = installer.install(script_env.loader.load("envchdirrun"))

    entries = _read_log(log)
    assert [entry["name"] for entry in entries] == ["preflight", "postflight"]
    assert entries[0]["app_state"] == "absent"
    assert entries[1]["app_state"] == "present"
    assert entries[0]["token"] == "envchdirrun"
    assert _receipt(result.room)["script_results"] == {
        "preflight": {"status": "ok", "exit_code": 0},
        "postflight": {"status": "ok", "exit_code": 0},
    }


def test_fake_ruby_receives_shim_and_body_as_separate_files(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pybrew.cask_installer import _CASK_SCRIPT_SHIM

    _write_cask(
        script_env.tap,
        "argvy",
        "2.0",
        script_env.digest,
        script_env.archive,
        '  preflight do\n    puts "preflight-body"\n  end\n'
        '  postflight do\n    puts "postflight-body"\n  end',
    )
    cask = script_env.loader.load("argvy")

    script = script_env.tmp_path / "argv-ruby"
    log = script_env.tmp_path / "argv-ruby.log"
    shim_capture = script_env.tmp_path / "captured-shim.rb"
    body_dir = script_env.tmp_path / "captured-bodies"
    body_dir.mkdir()
    script.write_text(EXACT_ARGV_RUBY, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PYBREW_RUBY", str(script))
    monkeypatch.setenv("FAKE_RUBY_LOG", str(log))
    monkeypatch.setenv("FAKE_SHIM_CAPTURE", str(shim_capture))
    monkeypatch.setenv("FAKE_BODY_CAPTURE_DIR", str(body_dir))

    installer = CaskInstaller(script_env.config, run_scripts=True)
    result = installer.install(cask)

    entries = _read_log(log)
    assert [entry["arg2"] for entry in entries] == ["preflight", "postflight"]
    preflight, postflight = entries
    # Nine arguments after the interpreter: shim path, script name, token,
    # version, appdir, staged_path, bin, prefix and the captured body file.
    for entry in (preflight, postflight):
        assert entry["argc"] == "9"
        assert "arg10" not in entry
        assert Path(entry["arg1"]).name == "cask_script_shim.rb"
        assert Path(entry["arg9"]).name == f"{entry['arg2']}.rb"
    assert preflight["arg3"] == "argvy"
    assert preflight["arg4"] == "2.0"
    assert preflight["arg5"] == str(script_env.config.applications)
    assert preflight["arg6"] == str(result.room)
    assert preflight["arg7"] == str(script_env.config.bin)
    assert preflight["arg8"] == str(script_env.config.prefix)
    assert preflight["cwd"] == postflight["cwd"]
    assert Path(preflight["cwd"]).name.startswith("pybrew-cask-")

    # The shim and the body are separate files: the body is never interpolated
    # into the shim source, and the body file is exactly the captured block.
    shim_text = shim_capture.read_text(encoding="utf-8")
    assert shim_text == _CASK_SCRIPT_SHIM
    assert "preflight-body" not in shim_text
    preflight_body = (body_dir / "preflight.rb").read_text(encoding="utf-8")
    postflight_body = (body_dir / "postflight.rb").read_text(encoding="utf-8")
    assert preflight_body == cask.preflight + "\n"
    assert "preflight-body" in preflight_body
    assert "postflight-body" in postflight_body
    assert _receipt(result.room)["script_results"] == {
        "preflight": {"status": "ok", "exit_code": 0},
        "postflight": {"status": "ok", "exit_code": 0},
    }


# -- uninstall scripts ---------------------------------------------------


UNINSTALL_SCRIPTY_SOURCE = '''cask "scripty" do
  version "1.0"
  sha256 "{digest}"
  url "{url}"

  uninstall_preflight do
    puts "uninstall-preflight"
  end

  uninstall_postflight do
    puts "uninstall-postflight"
  end

  app "Scripty.app"
end
'''


def _install_uninstall_fixture(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> tuple[SimpleNamespace, Path]:
    script, log = _fake_ruby(script_env.tmp_path, monkeypatch)
    cask_path = script_env.tap / "Casks" / "s" / "scripty.rb"
    cask_path.write_text(
        UNINSTALL_SCRIPTY_SOURCE.format(
            digest=script_env.digest, url=script_env.archive.as_uri()
        ),
        encoding="utf-8",
    )
    installer = CaskInstaller(script_env.config)
    cask = script_env.loader.load("scripty")
    installer.install(cask)
    return installer, log


def test_uninstall_default_does_not_run_scripts(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, log = _install_uninstall_fixture(script_env, monkeypatch)
    cask = script_env.loader.load("scripty")

    installer.uninstall("scripty", cask=cask)
    assert not log.exists()


def test_uninstall_scripts_are_ordered(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, log = _install_uninstall_fixture(script_env, monkeypatch)
    cask = script_env.loader.load("scripty")

    installer.uninstall("scripty", cask=cask, run_scripts=True)
    entries = _read_log(log)
    assert [entry["name"] for entry in entries] == [
        "uninstall_preflight",
        "uninstall_postflight",
    ]


def test_uninstall_preflight_failure_aborts_before_removal(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, log = _install_uninstall_fixture(script_env, monkeypatch)
    monkeypatch.setenv("FAKE_UNINSTALL_PREFLIGHT_EXIT", "9")
    cask = script_env.loader.load("scripty")

    with pytest.raises(CaskError, match="uninstall_preflight"):
        installer.uninstall("scripty", cask=cask, run_scripts=True)

    assert (script_env.config.caskroom / "scripty" / "1.0").is_dir()
    assert [entry["name"] for entry in _read_log(log)] == ["uninstall_preflight"]


def test_uninstall_postflight_failure_reports_after_removal(
    script_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, log = _install_uninstall_fixture(script_env, monkeypatch)
    monkeypatch.setenv("FAKE_UNINSTALL_POSTFLIGHT_EXIT", "8")
    cask = script_env.loader.load("scripty")

    with pytest.raises(CaskError, match="uninstall_postflight"):
        installer.uninstall("scripty", cask=cask, run_scripts=True)

    assert not (script_env.config.caskroom / "scripty" / "1.0").is_dir()
    assert [entry["name"] for entry in _read_log(log)] == [
        "uninstall_preflight",
        "uninstall_postflight",
    ]
