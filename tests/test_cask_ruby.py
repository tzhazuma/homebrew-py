"""Tests for the opt-in arbitrary-Ruby cask evaluator.

The evaluator is only active with ``PYBREW_ALLOW_CASK_RUBY=1``; the default
declarative parser must keep behaving exactly as before.  Every fixture here is
synthetic and local: no network access and no writes outside ``tmp_path``.
Real-Ruby tests are skipped when no Ruby interpreter is installed; the failure
and gate tests use a fake interpreter so they always run.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from pybrew.cask import CaskError, parse_cask_file, parse_cask_source
from pybrew.cask_installer import CaskLoader
from pybrew.cask_ruby import (
    ALLOW_ENV,
    RUBY_ENV,
    cask_ruby_enabled,
    evaluate_cask_file,
    evaluate_cask_source,
)
from pybrew.taps import discover_taps

FIXTURES = Path(__file__).parent / "fixtures" / "casks"

_RUBY = os.environ.get(RUBY_ENV) or "/usr/bin/ruby"
requires_ruby = pytest.mark.skipif(
    shutil.which(_RUBY) is None and not Path(_RUBY).is_file(),
    reason="Ruby interpreter not installed; opt-in cask evaluator tests skipped",
)


@pytest.fixture
def allow_ruby(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")


def _source(body: str, token: str = "tiny") -> str:
    return f'cask "{token}" do\n{body}\nend\n'


def _fake_ruby(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-ruby"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


# ---------------------------------------------------------------------------
# opt-in gate and missing Ruby (no real interpreter needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes"])
def test_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(ALLOW_ENV, raising=False)
    else:
        monkeypatch.setenv(ALLOW_ENV, value)

    assert cask_ruby_enabled() is False
    with pytest.raises(CaskError, match="PYBREW_ALLOW_CASK_RUBY=1"):
        evaluate_cask_source(_source('  version "1.0"'))


def test_only_exact_one_enables_the_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    assert cask_ruby_enabled() is True


def test_file_evaluation_checks_the_gate_before_reading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    with pytest.raises(CaskError, match="PYBREW_ALLOW_CASK_RUBY=1"):
        evaluate_cask_file(tmp_path / "missing.rb")


def test_missing_ruby_reports_cask_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allow_ruby: None
) -> None:
    monkeypatch.setenv(RUBY_ENV, str(tmp_path / "no-such-ruby"))
    with pytest.raises(CaskError, match="Ruby interpreter not found"):
        evaluate_cask_source(_source('  version "1.0"'))


# ---------------------------------------------------------------------------
# fake Ruby: interpreter override, failures and malformed output
# ---------------------------------------------------------------------------


def test_pybrew_ruby_override_and_failure_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allow_ruby: None
) -> None:
    fake = _fake_ruby(tmp_path, 'echo "fake ruby exploded" >&2\nexit 7')
    monkeypatch.setenv(RUBY_ENV, str(fake))

    with pytest.raises(CaskError) as error:
        evaluate_cask_source(_source('  version "1.0"'))

    message = str(error.value)
    assert "arbitrary Ruby cask evaluation failed" in message
    assert "exit 7" in message
    assert "fake ruby exploded" in message


def test_fake_ruby_without_result_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allow_ruby: None
) -> None:
    fake = _fake_ruby(tmp_path, 'echo "no json produced"')
    monkeypatch.setenv(RUBY_ENV, str(fake))

    with pytest.raises(CaskError, match="did not produce a result"):
        evaluate_cask_source(_source('  version "1.0"'))


def test_ruby_timeout_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allow_ruby: None
) -> None:
    fake = _fake_ruby(tmp_path, "sleep 5")
    monkeypatch.setenv(RUBY_ENV, str(fake))
    monkeypatch.setattr("pybrew.cask_ruby.RUBY_TIMEOUT", 0.5)

    with pytest.raises(CaskError, match="timed out"):
        evaluate_cask_source(_source('  version "1.0"'))


# ---------------------------------------------------------------------------
# real Ruby: DSL capture
# ---------------------------------------------------------------------------


@requires_ruby
def test_basic_fields_and_artifacts(allow_ruby: None) -> None:
    digest = "a" * 64
    source = _source(
        '  version "2.0.4"\n'
        f'  sha256 "{digest}"\n'
        '  url "https://example.com/Tiny-2.0.4.zip"\n'
        '  name "Tiny"\n'
        '  name "Tiny App"\n'
        '  desc "tiny app"\n'
        '  homepage "https://example.com/"\n'
        '  auto_updates true\n'
        '  app "Tiny.app"\n'
        '  binary "bin/tiny", target: "~/bin/tiny"\n'
        '  caveats "Read the docs."\n'
    )

    cask = evaluate_cask_source(source, tap="homebrew/cask")

    assert cask.token == "tiny"
    assert cask.tap == "homebrew/cask"
    assert cask.path is None
    assert cask.version == "2.0.4"
    assert cask.sha256 == digest
    assert cask.resolved_sha256() == digest
    assert cask.resolved_url() == "https://example.com/Tiny-2.0.4.zip"
    assert cask.name == "Tiny, Tiny App"
    assert cask.desc == "tiny app"
    assert cask.homepage == "https://example.com/"
    assert cask.auto_updates is True
    assert cask.stage_only is False
    assert cask.caveats == "Read the docs."
    assert [(item.kind, item.source, item.target) for item in cask.artifacts] == [
        ("app", "Tiny.app", None),
        ("binary", "bin/tiny", "~/bin/tiny"),
    ]
    assert cask.extra["ruby_evaluated"] is True


@requires_ruby
def test_arbitrary_ruby_variables_loops_and_conditionals(allow_ruby: None) -> None:
    source = '''
base = "https://example.com"
build = "3.4.5"
cask "loopy" do
  version build
  sha256 "b" * 64
  url "#{base}/loopy-#{version}.zip"
  %w[Loopy Loopy-Pro].each_with_index do |label, index|
    name label if index.zero?
    app "#{label}.app"
  end
  if version.start_with?("3.")
    desc "three"
  else
    desc "other"
  end
  binary "bin/tool-#{version.major}"
end
'''

    cask = evaluate_cask_source(source)

    assert cask.version == "3.4.5"
    assert cask.sha256 == "b" * 64
    assert cask.resolved_url() == "https://example.com/loopy-3.4.5.zip"
    assert cask.name == "Loopy"
    assert cask.desc == "three"
    assert [item.source for item in cask.artifacts] == [
        "Loopy.app",
        "Loopy-Pro.app",
        "bin/tool-3",
    ]


@requires_ruby
def test_platform_blocks_follow_target_os_and_arch(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(
        '  version "1.0"\n'
        '  on_macos do\n'
        '    url "https://example.com/mac.zip"\n'
        '    app "Mac.app"\n'
        '  end\n'
        '  on_linux do\n'
        '    url "https://example.com/linux.zip"\n'
        '    app "Linux.app"\n'
        '  end\n'
        '  on_arm do\n'
        '    name "ARM build"\n'
        '  end\n'
        '  on_intel do\n'
        '    name "Intel build"\n'
        '  end\n'
    )

    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("PYBREW_TARGET_ARCH", "arm64")
    mac = evaluate_cask_source(source)
    assert mac.resolved_url() == "https://example.com/mac.zip"
    assert [item.source for item in mac.artifacts] == ["Mac.app"]
    assert mac.name == "ARM build"

    monkeypatch.setenv("PYBREW_TARGET_OS", "linux")
    monkeypatch.setenv("PYBREW_TARGET_ARCH", "x86_64")
    linux = evaluate_cask_source(source)
    assert linux.resolved_url() == "https://example.com/linux.zip"
    assert [item.source for item in linux.artifacts] == ["Linux.app"]
    assert linux.name == "Intel build"


@requires_ruby
def test_macos_release_blocks(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(
        '  on_ventura do\n'
        '    version "ventura"\n'
        '  end\n'
        '  on_sonoma :or_newer do\n'
        '    version "sonoma-or-newer"\n'
        '  end\n'
        '  on_monterey :or_older do\n'
        '    version "monterey-or-older"\n'
        '  end\n'
    )

    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "14.5")
    assert evaluate_cask_source(source).version == "sonoma-or-newer"

    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "12.0")
    assert evaluate_cask_source(source).version == "monterey-or-older"

    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "15.0")
    assert evaluate_cask_source(source).version == "sonoma-or-newer"

    # Homebrew only evaluates macOS release blocks while running on macOS.
    monkeypatch.setenv("PYBREW_TARGET_OS", "linux")
    assert evaluate_cask_source(source).version is None


@requires_ruby
def test_arch_stanza_and_sha256_map(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    arm_digest = "a" * 64
    intel_digest = "b" * 64
    source = _source(
        '  arch arm: "darwin-arm64", intel: "darwin-x64"\n'
        '  version "2.0"\n'
        f'  sha256 arm: "{arm_digest}", intel: "{intel_digest}"\n'
        '  url "https://example.com/tool-#{arch}.zip"\n'
        '  app "Tool.app"\n'
    )

    monkeypatch.setenv("PYBREW_TARGET_ARCH", "arm64")
    arm = evaluate_cask_source(source)
    assert arm.arch == "arm64"
    assert arm.arch_map == {"arm": "darwin-arm64", "intel": "darwin-x64"}
    assert arm.sha256_map == {"arm": arm_digest, "intel": intel_digest}
    assert arm.sha256 == arm_digest
    assert arm.resolved_sha256() == arm_digest
    assert arm.resolved_url() == "https://example.com/tool-darwin-arm64.zip"

    monkeypatch.setenv("PYBREW_TARGET_ARCH", "x86_64")
    intel = evaluate_cask_source(source)
    assert intel.arch == "intel"
    assert intel.sha256 == intel_digest
    assert intel.resolved_sha256() == intel_digest
    assert intel.resolved_url() == "https://example.com/tool-darwin-x64.zip"


@requires_ruby
def test_arch_and_os_conditional_helpers(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = '''
cask "helpers" do
  version "1.0"
  arch_name = on_arch_conditional arm: "aarch64", intel: "x86_64"
  os_name = on_system_conditional macos: "darwin", linux: "linux"
  os macos: "mac", linux: "lin"
  url "https://example.com/#{os_name}/#{arch_name}/#{os}.zip"
  app "Tool.app"
end
'''

    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("PYBREW_TARGET_ARCH", "arm64")
    assert evaluate_cask_source(source).resolved_url() == (
        "https://example.com/darwin/aarch64/mac.zip"
    )

    monkeypatch.setenv("PYBREW_TARGET_OS", "linux")
    monkeypatch.setenv("PYBREW_TARGET_ARCH", "x86_64")
    assert evaluate_cask_source(source).resolved_url() == (
        "https://example.com/linux/x86_64/lin.zip"
    )


@requires_ruby
def test_artifact_kinds_targets_and_kwargs(allow_ruby: None) -> None:
    source = _source(
        '  version "1.0"\n'
        '  app "Foo.app", target: "~/Applications/Foo.app"\n'
        '  binary "bin/foo", target: "/usr/local/bin/foo"\n'
        '  suite "Foo Suite"\n'
        '  pkg "Foo.pkg", allow_untrusted: true\n'
        '  font "Fonts/Foo.ttf"\n'
        '  colorpicker "Plugins/Foo.colorPicker"\n'
        '  prefpane "Plugins/Foo.prefPane"\n'
        '  qlplugin "Plugins/Foo.qlgenerator"\n'
        '  mdimporter "Plugins/Foo.mdimporter"\n'
        '  screen_saver "Screen Savers/Foo.saver"\n'
        '  service "Services/Foo.service"\n'
        '  audio_unit_plugin "Components/Foo.component"\n'
        '  vst_plugin "VST/Foo.vst"\n'
        '  vst3_plugin "VST3/Foo.vst3"\n'
        '  dictionary "Dictionaries/Foo.dictionary"\n'
        '  input_method "Input Methods/Foo.app"\n'
        '  keyboard_layout "Keyboard Layouts/Foo.bundle"\n'
        '  installer script: "/usr/bin/true"\n'
        '  artifact "Generic.app", target: "~/Desktop/Generic.app"\n'
    )

    cask = evaluate_cask_source(source)

    assert [item.kind for item in cask.artifacts] == [
        "app",
        "binary",
        "suite",
        "pkg",
        "font",
        "colorpicker",
        "prefpane",
        "qlplugin",
        "mdimporter",
        "screen_saver",
        "service",
        "audio_unit_plugin",
        "vst_plugin",
        "vst3_plugin",
        "dictionary",
        "input_method",
        "keyboard_layout",
        "installer",
        "artifact",
    ]
    app = cask.artifacts[0]
    assert app.source == "Foo.app"
    assert app.target == "~/Applications/Foo.app"
    assert app.kwargs == {"target": "~/Applications/Foo.app"}
    assert cask.artifacts[1].target == "/usr/local/bin/foo"
    assert cask.artifacts[3].kwargs == {"allow_untrusted": True}
    installer = cask.artifacts[17]
    assert installer.source == ""
    assert installer.kwargs == {"script": "/usr/bin/true"}
    assert cask.artifacts[18].target == "~/Desktop/Generic.app"


@requires_ruby
def test_uninstall_zap_dependencies_and_conflicts(allow_ruby: None) -> None:
    source = _source(
        '  version "1.0"\n'
        '  uninstall quit: "com.example.foo",\n'
        '            delete: ["/Library/Foo", "~/Library/Foo"],\n'
        '            pkgutil: "com.example.foo.pkg",\n'
        '            signal: ["TERM", "com.example.foo"],\n'
        '            launchctl: "com.example.foo.agent"\n'
        '  zap trash: ["~/.foo", "~/Library/Caches/com.example.foo"]\n'
        '  depends_on formula: "openssl@3"\n'
        '  depends_on cask: "docker"\n'
        '  depends_on macos: ">= :ventura"\n'
        '  conflicts_with cask: "foo-classic"\n'
        '  conflicts_with formula: "foo"\n'
    )

    cask = evaluate_cask_source(source)

    assert cask.uninstall == {
        "quit": "com.example.foo",
        "delete": ["/Library/Foo", "~/Library/Foo"],
        "pkgutil": "com.example.foo.pkg",
        "signal": ["TERM", "com.example.foo"],
        "launchctl": "com.example.foo.agent",
    }
    assert cask.zap == ["~/.foo", "~/Library/Caches/com.example.foo"]
    assert cask.depends_on == {
        "formula": "openssl@3",
        "cask": "docker",
        "macos": ">= :ventura",
    }
    assert cask.conflicts_with == {"cask": "foo-classic", "formula": "foo"}


@requires_ruby
def test_bare_depends_on_symbol_is_preserved(allow_ruby: None) -> None:
    cask = evaluate_cask_source(
        _source('  version "1.0"\n  depends_on :macos\n')
    )
    assert cask.depends_on == {"macos": True}


@requires_ruby
def test_language_default_block_and_getter(allow_ruby: None) -> None:
    source = '''
cask "lang" do
  version "1.0"
  language "zh" do
    sha256 "z" * 64
    app "Chinese.app"
    "zh-CN"
  end
  language "en-US", default: true do
    sha256 "e" * 64
    app "English.app"
    "en-US"
  end
  archive = (language == "zh-CN") ? "chinese.zip" : "english.zip"
  url "https://example.com/#{archive}"
end
'''

    cask = evaluate_cask_source(source)

    assert cask.language == "en-US"
    assert cask.sha256 == "e" * 64
    assert [item.source for item in cask.artifacts] == ["English.app"]
    assert cask.resolved_url() == "https://example.com/english.zip"


@requires_ruby
def test_language_without_default_uses_first_block(allow_ruby: None) -> None:
    source = _source(
        '  language "fr" do\n'
        '    version "2.0"\n'
        '  end\n'
    )
    cask = evaluate_cask_source(source)
    assert cask.language == "fr"
    assert cask.version == "2.0"


@requires_ruby
def test_flags_stage_only_auto_updates_and_deprecation(allow_ruby: None) -> None:
    source = _source(
        '  version "9.9"\n'
        '  stage_only true\n'
        '  auto_updates true\n'
        '  deprecate! date: "2024-01-01", because: "old"\n'
    )
    cask = evaluate_cask_source(source)
    assert cask.stage_only is True
    assert cask.auto_updates is True
    assert cask.deprecated is True


# ---------------------------------------------------------------------------
# real Ruby: scripts, unknown stanzas and file evaluation
# ---------------------------------------------------------------------------


@requires_ruby
def test_scripts_are_captured_but_not_executed(
    allow_ruby: None, tmp_path: Path
) -> None:
    marker = tmp_path / "postflight-ran"
    source = _source(
        '  version "1.0"\n'
        '  preflight do\n'
        '    raise "preflight must not run"\n'
        '  end\n'
        '  postflight do\n'
        f'    File.write("{marker}", "ran")\n'
        '  end\n'
        '  uninstall_preflight do\n'
        '    raise "uninstall preflight must not run"\n'
        '  end\n'
        '  uninstall_postflight do\n'
        '    raise "uninstall postflight must not run"\n'
        '  end\n'
        '  app "Tiny.app"\n'
    )

    cask = evaluate_cask_source(source)

    assert [item.kind for item in cask.artifacts] == ["app"]
    assert sorted(cask.scripts) == [
        "postflight",
        "preflight",
        "uninstall_postflight",
        "uninstall_preflight",
    ]
    assert 'raise "preflight must not run"' in cask.preflight
    assert "File.write" in cask.postflight
    assert 'raise "uninstall preflight must not run"' in cask.uninstall_preflight
    assert 'raise "uninstall postflight must not run"' in cask.uninstall_postflight
    assert cask.caveats is None
    assert not marker.exists()
    # Scripts are captured as inert data, not reported as unsupported stanzas.
    assert "ruby_unsupported" not in cask.extra
    assert "ruby_warnings" not in cask.extra


@requires_ruby
def test_unknown_stanzas_are_recorded_not_fatal(allow_ruby: None) -> None:
    source = _source(
        '  version "1.0"\n'
        '  container type: :naked\n'
        '  rename "Old.app", "New.app"\n'
        '  zsh_completion "bin/_foo"\n'
        '  preflight_steps do\n'
        '    run "echo"\n'
        '  end\n'
        '  app "Foo.app"\n'
    )

    cask = evaluate_cask_source(source)

    assert cask.version == "1.0"
    assert [item.kind for item in cask.artifacts] == ["zsh_completion", "app"]
    assert cask.renames == [("Old.app", "New.app")]
    assert cask.container == "naked"
    unsupported = cask.extra["ruby_unsupported"]
    assert [entry["stanza"] for entry in unsupported] == ["preflight_steps"]
    assert unsupported[0]["block"] is True
    warnings = cask.extra["ruby_warnings"]
    assert any("preflight_steps" in warning for warning in warnings)


@requires_ruby
def test_evaluate_cask_file_sets_path_and_tap(
    allow_ruby: None, tmp_path: Path
) -> None:
    path = tmp_path / "not-the-token.rb"
    path.write_text(
        _source('  version "3.1"\n  app "Tiny.app"', token="actual-token"),
        encoding="utf-8",
    )

    cask = evaluate_cask_file(path, tap="homebrew/cask")

    assert cask.token == "actual-token"
    assert cask.path == path
    assert cask.tap == "homebrew/cask"
    assert cask.version == "3.1"
    assert [item.source for item in cask.artifacts] == ["Tiny.app"]


# ---------------------------------------------------------------------------
# real Ruby: failure reporting
# ---------------------------------------------------------------------------


@requires_ruby
def test_missing_cask_declaration_reports_error(allow_ruby: None) -> None:
    with pytest.raises(CaskError, match=r"no `cask"):
        evaluate_cask_source('puts "hello"\n')


@requires_ruby
def test_ruby_failure_reports_stderr_and_line(allow_ruby: None) -> None:
    source = 'cask "boom" do\n  version "1.0"\n  raise "boom"\nend\n'

    with pytest.raises(CaskError) as error:
        evaluate_cask_source(source)

    message = str(error.value)
    assert "arbitrary Ruby cask evaluation failed" in message
    assert "boom" in message
    assert "cask.rb line 3" in message
    assert 'raise "boom"' in message
    assert "ruby-cask-shim.rb" in message
    assert "pybrew-cask-ruby-" not in message


@requires_ruby
def test_path_is_used_for_backtraces(
    allow_ruby: None, tmp_path: Path
) -> None:
    path = tmp_path / "thing.rb"
    source = 'cask "thing" do\n  raise "nope"\nend\n'

    with pytest.raises(CaskError) as error:
        evaluate_cask_source(source, path=path)

    message = str(error.value)
    assert str(path) in message
    assert "thing.rb line 2" in message
    assert "nope" in message


@requires_ruby
def test_ruby_syntax_error_reports_stderr(allow_ruby: None) -> None:
    with pytest.raises(CaskError) as error:
        evaluate_cask_source('cask "broken" do\n  version "1.0"\n')

    message = str(error.value)
    assert "syntax error" in message
    assert "cask.rb" in message


# ---------------------------------------------------------------------------
# default parser must be unchanged
# ---------------------------------------------------------------------------


def test_default_parser_still_parses_with_opt_in_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    cask = parse_cask_file(FIXTURES / "rectangle.rb", tap="homebrew/cask")
    assert cask.token == "rectangle"
    assert cask.version == "2.0.4"
    assert [item.kind for item in cask.artifacts] == ["app"]


@requires_ruby
def test_default_parser_does_not_execute_arbitrary_ruby(allow_ruby: None) -> None:
    source = _source(
        '  names = %w[a b]\n'
        '  names.each { |entry| app "#{entry}.app" }\n'
    )

    parsed = parse_cask_source(source)
    assert parsed.artifacts == []

    evaluated = evaluate_cask_source(source)
    assert [item.source for item in evaluated.artifacts] == ["a.app", "b.app"]


# ---------------------------------------------------------------------------
# real-world fixture: platform blocks, arch map, uninstall and zap
# ---------------------------------------------------------------------------


@requires_ruby
def test_fixture_cask_with_platform_blocks_and_arch(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("PYBREW_TARGET_ARCH", "arm64")
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "14.5")

    cask = evaluate_cask_file(
        FIXTURES / "visual-studio-code.rb", tap="homebrew/cask"
    )

    assert cask.token == "visual-studio-code"
    assert cask.version == "1.143.1"
    assert cask.resolved_url() == (
        "https://update.code.visualstudio.com/1.143.1/darwin-arm64/stable"
    )
    assert cask.resolved_sha256() == (
        "65ced7020e995c296e121881b10cd7ba46638cebc5dde0cb921bc82de13cd772"
    )
    assert cask.name == "Microsoft Visual Studio Code, VS Code"
    assert cask.auto_updates is True
    assert cask.depends_on == {"macos": True}
    assert [item.kind for item in cask.artifacts] == ["app", "binary", "binary"]
    appdir = str(Path.home() / "Applications")
    assert cask.artifacts[1].source == (
        f"{appdir}/Visual Studio Code.app/Contents/Resources/app/bin/code"
    )
    assert cask.uninstall == {
        "launchctl": "com.microsoft.VSCode.ShipIt",
        "quit": "com.microsoft.VSCode",
    }
    assert "~/.vscode" in cask.zap


@requires_ruby
def test_cask_loader_uses_ruby_evaluator_when_enabled(
    tmp_path: Path, allow_ruby: None
) -> None:
    tap = tmp_path / "tap"
    path = tap / "Casks" / "r" / "ruby-loader.rb"
    path.parent.mkdir(parents=True)
    path.write_text(
        '''cask "ruby-loader" do
  version "1.0"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  url "https://example.com/#{version}.zip"
  app "Ruby.app"
  ["one", "two"].each { |name| binary "bin/#{name}" }
end
'''
    )
    loader = CaskLoader(discover_taps([tap]))
    cask = loader.load("ruby-loader")
    assert cask.version == "1.0"
    assert cask.url == "https://example.com/1.0.zip"
    assert [artifact.kind for artifact in cask.artifacts] == ["app", "binary", "binary"]
    assert [artifact.source for artifact in cask.artifacts[1:]] == ["bin/one", "bin/two"]


@requires_ruby
def test_cli_ruby_cask_flag(run_cli, sample) -> None:
    cask_path = sample.tap / "Casks" / "r" / "ruby-flag.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        '''cask "ruby-flag" do
  version "2.0"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  url "https://example.com/#{version}.zip"
  app "RubyFlag.app"
end
'''
    )
    proc = run_cli("info", "--cask", "--ruby-cask", "ruby-flag")
    assert proc.returncode == 0, proc.stderr
    assert "ruby-flag: 2.0" in proc.stdout


@requires_ruby
def _write_scripted_cask(tap: Path) -> None:
    path = tap / "Casks" / "r" / "ruby-script.rb"
    path.parent.mkdir(parents=True)
    path.write_text(
        '''cask "ruby-script" do
  version "3.0"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  url "https://example.com/#{version}.zip"
  preflight do
    puts "preflight body"
  end
  app "RubyScript.app"
end
'''
    )


@requires_ruby
def test_loader_prefers_literal_parser_script_bodies(
    tmp_path: Path, allow_ruby: None
) -> None:
    tap = tmp_path / "tap"
    _write_scripted_cask(tap)
    cask = CaskLoader(discover_taps([tap])).load("ruby-script")
    assert cask.version == "3.0"
    assert cask.preflight is not None
    assert "preflight body" in cask.preflight
    # The literal parser captured the body verbatim, so the loader uses it as
    # the authoritative copy even though the evaluator recovered one too.
    assert "preflight" in cask.extra.get("ruby_merged_scripts", [])


@requires_ruby
def test_loader_merges_scripts_from_literal_parser(
    tmp_path: Path, allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tap = tmp_path / "tap"
    _write_scripted_cask(tap)
    # Force evaluator source recovery to fail; the literal parser must still
    # provide the inert script body through the loader merge.
    monkeypatch.setattr(
        "pybrew.cask_ruby._extract_stanza_source",
        lambda *_args, **_kwargs: None,
    )
    cask = CaskLoader(discover_taps([tap])).load("ruby-script")
    assert cask.version == "3.0"
    assert cask.preflight is not None
    assert "preflight body" in cask.preflight
    assert "preflight" in cask.extra.get("ruby_merged_scripts", [])


def test_cask_ruby_os_stanza(allow_ruby: None) -> None:
    source = _source(
        '  version "1.0"\n'
        '  os macos: ">= :sonoma"\n'
        '  app "Osy.app"\n'
    )
    cask = evaluate_cask_source(source)
    assert cask.os["macos"] == ">= :sonoma"


@requires_ruby
def test_cask_metadata_helpers(allow_ruby: None) -> None:
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  sha256 "' + "a" * 64 + '"\n'
            '  url "https://example.com/tiny.zip"\n'
            '  name "Tiny"\n'
            '  desc "tiny fixture"\n'
            '  homepage "https://example.com/tiny"\n'
            '  os macos: "13"\n'
            '  depends_on formula: "foo"\n'
            '  no_autobump! because: "test"\n'
            '  on_macos { }\n'
            '  caveats "autobump=#{autobump?} nobump=#{no_autobump_defined?} os=#{on_os_blocks_exist?} deps=#{depends_on_set_in_block?} sys=#{on_system_blocks_exist?}"\n'
        )
    )
    assert cask.caveats == "autobump=false nobump=true os=true deps=true sys=true"


@requires_ruby
def test_cask_path_and_os_helpers(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prefix = tmp_path / "prefix"
    monkeypatch.setenv("HOMEBREW_PREFIX", str(prefix))
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  sha256 "' + "a" * 64 + '"\n'
            '  url "https://example.com/tiny.zip"\n'
            '  name "Tiny"\n'
            '  desc "tiny fixture"\n'
            '  homepage "https://example.com/tiny"\n'
            '  caveats "caskroom=#{caskroom_path} staged=#{staged_path} os=#{os_version} token=#{token}"\n'
        )
    )
    assert cask.caveats.startswith(
        f"caskroom={prefix}/Caskroom/tiny staged={prefix}/Caskroom/tiny/1.0 os="
    )
    assert cask.caveats.endswith(" token=tiny")


@requires_ruby
def test_cask_language_helpers(allow_ruby: None) -> None:
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  sha256 "' + "a" * 64 + '"\n'
            '  url "https://example.com/tiny.zip"\n'
            '  name "Tiny"\n'
            '  desc "tiny fixture"\n'
            '  homepage "https://example.com/tiny"\n'
            '  language "en", default: true do\n'
            '    "English"\n'
            '  end\n'
            '  language "fr" do\n'
            '    "French"\n'
            '  end\n'
            '  caveats "langs=#{languages.join(",")} groups=#{language_groups.length} default=#{default_language_group.first}"\n'
        )
    )
    assert cask.caveats == "langs=en,fr groups=2 default=en"


@requires_ruby
def test_cask_livecheck_defined_helper(allow_ruby: None) -> None:
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  sha256 "' + "a" * 64 + '"\n'
            '  url "https://example.com/tiny.zip"\n'
            '  name "Tiny"\n'
            '  desc "tiny fixture"\n'
            '  homepage "https://example.com/tiny"\n'
            '  livecheck do\n'
            '    regex(/tiny-(\\d+)/)\n'
            '  end\n'
            '  caveats "livecheck=#{livecheck_defined?}"\n'
        )
    )
    assert cask.caveats == "livecheck=true"


@requires_ruby
def test_cask_caveats_block_capture(allow_ruby: None) -> None:
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  sha256 "' + "a" * 64 + '"\n'
            '  url "https://example.com/tiny.zip"\n'
            '  name "Tiny"\n'
            '  desc "tiny fixture"\n'
            '  homepage "https://example.com/tiny"\n'
            '  caveats do\n'
            '    puts "hello"\n'
            '    puts "world"\n'
            '  end\n'
        )
    )
    assert cask.caveats == "hello\nworld\n"


# ---------------------------------------------------------------------------
# MacOS helpers (version/full_version/version_symbol/sdk/sdk_path)
# ---------------------------------------------------------------------------


@requires_ruby
def test_cask_macos_version_helpers(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sdk = tmp_path / "MacOSX14.0.sdk"
    sdk.mkdir()
    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "14.5")
    monkeypatch.setenv("PYBREW_MACOS_SDK_PATH", str(sdk))

    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  caveats "version=#{MacOS.version} full=#{MacOS.full_version} '
            'symbol=#{MacOS.version_symbol} sdk=#{MacOS.sdk_path} '
            'sdk_version=#{MacOS.sdk.version} '
            'at_least_sonoma=#{MacOS.version >= :sonoma} '
            'before_sequoia=#{MacOS.version < :sequoia}"\n'
        )
    )

    assert cask.caveats == (
        f"version=14.5 full=14.5 symbol=sonoma sdk={sdk} "
        "sdk_version=14.0 at_least_sonoma=true before_sequoia=true"
    )


@requires_ruby
def test_cask_macos_sdk_selection_from_local_roots(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PYBREW_MACOS_SDK_PATH", raising=False)
    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "15.1")
    monkeypatch.setattr("pybrew.cask_ruby._MACOS_SDK_ROOTS", (tmp_path,))
    (tmp_path / "MacOSX14.5.sdk").mkdir()
    (tmp_path / "MacOSX15.0.sdk").mkdir()

    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  caveats "default=#{MacOS.sdk.version} '
            'by_major=#{MacOS.sdk("15").version} '
            'by_symbol=#{MacOS.sdk(:sonoma).version} '
            'missing=#{MacOS.sdk("13").inspect}"\n'
        )
    )
    assert cask.caveats == (
        "default=15.0 by_major=15.0 by_symbol=14.5 missing=nil"
    )

    # SDKs are only reported for macOS targets; Linux casks see nil even when
    # an SDK path override is configured.
    monkeypatch.setenv("PYBREW_TARGET_OS", "linux")
    monkeypatch.setenv("PYBREW_MACOS_SDK_PATH", str(tmp_path / "MacOSX14.5.sdk"))
    linux = evaluate_cask_source(
        _source('  version "1.0"\n  caveats "sdk=#{MacOS.sdk_path.inspect}"\n')
    )
    assert linux.caveats == "sdk=nil"


# ---------------------------------------------------------------------------
# livecheck metadata and source capture
# ---------------------------------------------------------------------------


@requires_ruby
def test_cask_livecheck_metadata_and_source(allow_ruby: None) -> None:
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  livecheck do\n'
            '    url "https://example.com/download"\n'
            '    regex(/demo-(\\d+(?:\\.\\d+)*)\\.zip/i)\n'
            '    strategy :page_match\n'
            '    throttle 2, days: 5\n'
            '  end\n'
            '  caveats "defined=#{livecheck_defined?}"\n'
        )
    )

    assert cask.livecheck_defined is True
    assert cask.livecheck["url"] == "https://example.com/download"
    assert cask.livecheck["regex"] == r"demo-(\d+(?:\.\d+)*)\.zip"
    assert cask.livecheck["strategy"] == ":page_match"
    assert cask.livecheck["skip"] is False
    assert cask.livecheck["throttle"] == 2
    assert cask.livecheck["throttle_days"] == 5
    assert cask.caveats == "defined=true"
    assert cask.livecheck_source is not None
    assert 'url "https://example.com/download"' in cask.livecheck_source
    assert "regex(" in cask.livecheck_source
    assert "strategy :page_match" in cask.livecheck_source


@requires_ruby
def test_cask_livecheck_skip_and_reference_metadata(allow_ruby: None) -> None:
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  livecheck do\n'
            '    skip "no longer maintained"\n'
            '    formula :parent\n'
            '  end\n'
        )
    )

    assert cask.livecheck_defined is True
    assert cask.livecheck["skip"] is True
    assert cask.livecheck["skip_msg"] == "no longer maintained"
    assert cask.livecheck["formula"] == ":parent"


def test_extract_livecheck_source_handles_do_and_brace_blocks() -> None:
    from pybrew.cask_ruby import _extract_livecheck_source

    do_source = (
        'cask "tiny" do\n'
        '  livecheck do\n'
        '    regex(/tiny/)\n'
        '  end\n'
        'end\n'
    )
    do_line = do_source.splitlines().index("  livecheck do") + 1
    assert _extract_livecheck_source(do_source, ["cask.rb", do_line]) == (
        "regex(/tiny/)"
    )

    brace_source = 'cask "tiny" do\n  livecheck { regex(/tiny/) }\nend\n'
    brace_line = brace_source.splitlines().index(
        "  livecheck { regex(/tiny/) }"
    ) + 1
    assert _extract_livecheck_source(brace_source, ["cask.rb", brace_line]) == (
        "regex(/tiny/)"
    )

    assert _extract_livecheck_source(do_source, ["cask.rb", 1]) is None
    assert _extract_livecheck_source(do_source, None) is None


@requires_ruby
def test_livecheck_source_recovery_failure_is_documented(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "pybrew.cask_ruby._extract_livecheck_source",
        lambda *_args, **_kwargs: None,
    )
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  livecheck do\n'
            '    regex(/tiny/)\n'
            '  end\n'
        )
    )

    assert cask.livecheck_defined is True
    assert cask.livecheck_source is None
    warnings = cask.extra["ruby_warnings"]
    assert any(
        "livecheck" in warning and "source" in warning for warning in warnings
    )


# ---------------------------------------------------------------------------
# metadata helpers exposed on the Python Cask model
# ---------------------------------------------------------------------------


@requires_ruby
def test_evaluator_metadata_helpers_on_cask_model(allow_ruby: None) -> None:
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  on_macos { }\n'
            '  no_autobump! because: "test"\n'
        )
    )

    assert cask.on_system_blocks is True
    assert cask.on_system_blocks_exist is True
    assert cask.autobump is False
    assert cask.livecheck_defined is False
    assert cask.livecheck == {}


# ---------------------------------------------------------------------------
# sha256 Linux architecture keys
# ---------------------------------------------------------------------------


@requires_ruby
def test_cask_sha256_linux_arch_keys(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    arm_digest = "a" * 64
    intel_digest = "b" * 64
    arm_linux_digest = "c" * 64
    x64_linux_digest = "d" * 64
    source = _source(
        '  version "1.0"\n'
        f'  sha256 arm: "{arm_digest}", intel: "{intel_digest}", '
        f'arm64_linux: "{arm_linux_digest}", '
        f'x86_64_linux: "{x64_linux_digest}"\n'
    )
    expected_map = {
        "arm": arm_digest,
        "intel": intel_digest,
        "arm64_linux": arm_linux_digest,
        "x86_64_linux": x64_linux_digest,
    }

    monkeypatch.setenv("PYBREW_TARGET_OS", "linux")
    monkeypatch.setenv("PYBREW_TARGET_ARCH", "arm64")
    arm_linux = evaluate_cask_source(source)
    assert arm_linux.sha256_map == expected_map
    assert arm_linux.sha256 == arm_linux_digest
    assert arm_linux.resolved_sha256() == arm_linux_digest

    monkeypatch.setenv("PYBREW_TARGET_ARCH", "x86_64")
    x64_linux = evaluate_cask_source(source)
    assert x64_linux.sha256 == x64_linux_digest
    assert x64_linux.resolved_sha256() == x64_linux_digest

    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("PYBREW_TARGET_ARCH", "arm64")
    mac_arm = evaluate_cask_source(source)
    assert mac_arm.sha256 == arm_digest


# ---------------------------------------------------------------------------
# deprecation/disable/homepage/no_autobump metadata
# ---------------------------------------------------------------------------


@requires_ruby
def test_cask_deprecation_and_disable_metadata(allow_ruby: None) -> None:
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  homepage "https://example.com/", browsed: "2026-07-26"\n'
            '  deprecate! date: "2024-01-01", because: :unmaintained, '
            'replacement_formula: "other"\n'
            '  disable! date: "2024-02-02", because: "broken", '
            'replacement: "newtool"\n'
            '  no_autobump! because: :extract_plist\n'
        )
    )

    assert cask.homepage == "https://example.com/"
    assert cask.homepage_browsed == "2026-07-26"

    assert cask.deprecated is True
    assert cask.deprecation_date == "2024-01-01"
    assert cask.deprecation_reason == ":unmaintained"
    assert cask.deprecation_replacement_formula == "other"
    assert cask.deprecation_replacement_cask is None
    assert cask.deprecate_args == {
        "date": "2024-01-01",
        "because": ":unmaintained",
        "replacement_formula": "other",
    }

    assert cask.disabled is True
    assert cask.disable_date == "2024-02-02"
    assert cask.disable_reason == "broken"
    # The legacy ``replacement:`` keyword fills both replacement fields.
    assert cask.disable_replacement_formula == "newtool"
    assert cask.disable_replacement_cask == "newtool"
    assert cask.disable_args["replacement"] == "newtool"

    assert cask.no_autobump is True
    assert cask.no_autobump_message == ":extract_plist"


# ---------------------------------------------------------------------------
# on_system_block_min_os
# ---------------------------------------------------------------------------


@requires_ruby
def test_cask_on_system_block_min_os(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "14.5")
    sonoma = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  on_sonoma do\n'
            '    version "2.0"\n'
            '  end\n'
        )
    )
    assert sonoma.version == "2.0"
    assert sonoma.on_system_block_min_os == "14"

    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "12.0")
    older = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  on_monterey :or_older do\n'
            '    version "2.0"\n'
            '  end\n'
        )
    )
    assert older.version == "2.0"
    assert older.on_system_block_min_os == "10.15"

    monkeypatch.setenv("PYBREW_TARGET_OS", "linux")
    linux = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  on_sonoma do\n'
            '    version "2.0"\n'
            '  end\n'
        )
    )
    assert linux.on_system_block_min_os is None


# ---------------------------------------------------------------------------
# cask script body capture (source_location + lexer)
# ---------------------------------------------------------------------------


def test_extract_stanza_source_handles_script_blocks() -> None:
    from pybrew.cask_ruby import _extract_stanza_source

    source = (
        'cask "tiny" do\n'
        '  preflight do\n'
        '    puts "hi"\n'
        '  end\n'
        '  postflight { puts "bye" }\n'
        'end\n'
    )
    preflight_line = source.splitlines().index("  preflight do") + 1
    postflight_line = source.splitlines().index(
        '  postflight { puts "bye" }'
    ) + 1
    assert _extract_stanza_source(
        source, ["cask.rb", preflight_line], "preflight"
    ) == 'puts "hi"'
    assert _extract_stanza_source(
        source, ["cask.rb", postflight_line], "postflight"
    ) == 'puts "bye"'
    assert _extract_stanza_source(source, ["cask.rb", 1], "preflight") is None
    assert _extract_stanza_source(source, None, "preflight") is None


@requires_ruby
def test_script_source_recovery_failure_is_documented(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "pybrew.cask_ruby._extract_stanza_source",
        lambda *_args, **_kwargs: None,
    )
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  preflight do\n'
            '    puts "hi"\n'
            '  end\n'
        )
    )

    assert cask.preflight is None
    assert cask.scripts == {}
    warnings = cask.extra["ruby_warnings"]
    assert any("preflight" in warning and "source" in warning for warning in warnings)


@requires_ruby
def test_evaluated_scripts_stay_inert_without_run_scripts(
    tmp_path: Path, allow_ruby: None
) -> None:
    import json

    from pybrew.cask_installer import CaskInstaller
    from pybrew.paths import Config

    marker = tmp_path / "preflight-ran"
    tap = tmp_path / "tap"
    path = tap / "Casks" / "i" / "inert-ruby.rb"
    path.parent.mkdir(parents=True)
    path.write_text(
        f'''cask "inert-ruby" do
  version "1.0"
  stage_only true
  preflight do
    File.write("{marker}", "ran")
  end
end
''',
        encoding="utf-8",
    )
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[tap], cache=tmp_path / "cache"
    )
    config.ensure()
    installer = CaskInstaller(
        config,
        appdir=tmp_path / "Applications",
        library_root=tmp_path / "Library",
    )
    cask = CaskLoader(discover_taps([tap])).load("inert-ruby")
    assert cask.preflight is not None
    installer.install(cask)

    assert not marker.exists()
    receipt = json.loads(
        (tmp_path / "prefix" / "Caskroom" / "inert-ruby" / "1.0" / ".metadata"
         / "receipt.json").read_text()
    )
    assert receipt["scripts_requested"] is False
    assert receipt["script_results"] == {}


# ---------------------------------------------------------------------------
# livecheck strategy block and url kwargs
# ---------------------------------------------------------------------------


@requires_ruby
def test_cask_livecheck_strategy_block_and_url_options(allow_ruby: None) -> None:
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  livecheck do\n'
            '    url "https://example.com/download", '
            'header: ["Accept: text/html"], user_agent: :default, '
            'referer: "https://example.com"\n'
            '    strategy :page_match do |page|\n'
            '      page.scan(/lc-(\\d+)/).flatten\n'
            '    end\n'
            '  end\n'
        )
    )

    assert cask.livecheck_defined is True
    assert cask.livecheck["url"] == "https://example.com/download"
    assert cask.livecheck["url_options"] == {
        "header": ["Accept: text/html"],
        "user_agent": ":default",
        "referer": "https://example.com",
    }
    assert cask.livecheck["strategy"] == ":page_match"
    assert cask.livecheck["strategy_source"] is not None
    assert "page.scan" in cask.livecheck["strategy_source"]
    assert cask.livecheck_source is not None
    assert "strategy :page_match" in cask.livecheck_source


@requires_ruby
def test_livecheck_strategy_source_recovery_failure_is_documented(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "pybrew.cask_ruby._extract_stanza_source",
        lambda *_args, **_kwargs: None,
    )
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  livecheck do\n'
            '    strategy :page_match do |page|\n'
            '      page.scan(/lc-(\\d+)/)\n'
            '    end\n'
            '  end\n'
        )
    )

    assert cask.livecheck_defined is True
    assert cask.livecheck["strategy"] == ":page_match"
    assert "strategy_source" not in cask.livecheck
    warnings = cask.extra["ruby_warnings"]
    assert any(
        "strategy" in warning and "source" in warning for warning in warnings
    )


def test_extract_stanza_source_handles_strategy_block() -> None:
    from pybrew.cask_ruby import _extract_stanza_source

    source = (
        'cask "tiny" do\n'
        '  livecheck do\n'
        '    strategy :page_match do |page|\n'
        '      page.scan(/tiny/)\n'
        '    end\n'
        '  end\n'
        'end\n'
    )
    line = source.splitlines().index(
        "    strategy :page_match do |page|"
    ) + 1
    extracted = _extract_stanza_source(
        source, ["cask.rb", line], "strategy"
    )
    assert extracted is not None
    assert "page.scan(/tiny/)" in extracted


# ---------------------------------------------------------------------------
# MacOS release_name / pretty_name
# ---------------------------------------------------------------------------


@requires_ruby
def test_cask_macos_release_name_helpers(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "14.5")
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  caveats "release=#{MacOS.version.release_name} '
            'pretty=#{MacOS.version.pretty_name} '
            'module_release=#{MacOS.release_name} '
            'module_pretty=#{MacOS.pretty_name} '
            'catalina=#{MacOS::Version.new("10.15.7").release_name} '
            'tahoe=#{MacOS::Version.new("26.1").release_name} '
            'unknown=#{MacOS::Version.new("99").pretty_name}"\n'
        )
    )
    assert cask.caveats == (
        "release=Sonoma pretty=Sonoma module_release=Sonoma "
        "module_pretty=Sonoma catalina=Catalina tahoe=Tahoe unknown=Dunno"
    )


# ---------------------------------------------------------------------------
# livecheck url options: nested/Regexp values are JSON-friendly
# ---------------------------------------------------------------------------


@requires_ruby
def test_cask_livecheck_url_options_json_friendly(allow_ruby: None) -> None:
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  livecheck do\n'
            '    url "https://example.com/download", '
            'header: ["Accept: text/html"], '
            'post_json: { "a" => [1, 2], "pattern" => /x-(\\d+)/ }\n'
            '  end\n'
        )
    )

    assert cask.livecheck["url_options"] == {
        "header": ["Accept: text/html"],
        "post_json": {"a": [1, 2], "pattern": "x-(\\d+)"},
    }


# ---------------------------------------------------------------------------
# MacOS.version.to_sym / outdated_release? / MacOS.language (evaluator)
# ---------------------------------------------------------------------------


@requires_ruby
def test_cask_macos_version_sym_outdated_and_language(
    allow_ruby: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("HOMEBREW_MACOS_VERSION", "14.5")
    monkeypatch.setenv("PYBREW_MACOS_LANGUAGE", "zh-Hans-CN")
    cask = evaluate_cask_source(
        _source(
            '  version "1.0"\n'
            '  caveats "sym=#{MacOS.version.to_sym} '
            'unknown=#{MacOS::Version.new("99").to_sym} '
            'outdated=#{MacOS.version.outdated_release?} '
            'current=#{MacOS::Version.new("27.2").outdated_release?} '
            'lang=#{MacOS.language} langs=#{MacOS.languages.join(",")}"\n'
        )
    )
    assert cask.caveats == (
        "sym=sonoma unknown=dunno outdated=true current=false "
        "lang=zh-Hans-CN langs=zh-Hans-CN"
    )
