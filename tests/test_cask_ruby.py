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
def test_scripts_are_not_executed_and_are_reported(
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
        '  caveats do\n'
        '    raise "caveats must not run"\n'
        '  end\n'
        '  app "Tiny.app"\n'
    )

    cask = evaluate_cask_source(source)

    assert [item.kind for item in cask.artifacts] == ["app"]
    assert cask.scripts == {}
    assert cask.caveats is None
    assert not marker.exists()
    warnings = cask.extra["ruby_warnings"]
    for stanza in (
        "preflight",
        "postflight",
        "uninstall_preflight",
        "uninstall_postflight",
        "caveats",
    ):
        assert any(stanza in warning for warning in warnings)
    assert [entry["stanza"] for entry in cask.extra["ruby_unsupported"]] == [
        "preflight",
        "postflight",
        "uninstall_preflight",
        "uninstall_postflight",
        "caveats",
    ]


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
def test_loader_merges_scripts_from_literal_parser(
    tmp_path: Path, allow_ruby: None
) -> None:
    tap = tmp_path / "tap"
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
    loader = CaskLoader(discover_taps([tap]))
    cask = loader.load("ruby-script")
    assert cask.version == "3.0"
    assert cask.preflight is not None
    assert "preflight body" in cask.preflight
    assert "preflight" in cask.extra.get("ruby_merged_scripts", [])
