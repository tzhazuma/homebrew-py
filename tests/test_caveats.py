"""Tests for the formula caveats DSL runner.

Every fixture is synthetic and local: no network access, no writes outside
``tmp_path`` and no arbitrary Ruby beyond benign string/heredoc rendering.
Tests that execute the real Ruby interpreter are skipped when it is not
installed; fake interpreter tests cover argv, environment and failure
handling on any host.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pybrew.caveats import (
    CaveatsError,
    CaveatsRunner,
    has_caveats,
    render_caveats,
    run_caveats,
)
from pybrew.models import Formula
from pybrew.paths import Config
from pybrew.ruby_parser import parse_formula_file

_REAL_RUBY = "/usr/bin/ruby"
_HAS_RUBY = os.path.isfile(_REAL_RUBY) and os.access(_REAL_RUBY, os.X_OK)
requires_ruby = pytest.mark.skipif(
    not _HAS_RUBY, reason="real Ruby interpreter (/usr/bin/ruby) not installed"
)

FIXTURES = Path(__file__).parent / "fixtures" / "core_formulas"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> Config:
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"
    )
    config.ensure()
    return config


def _formula(
    caveats: str,
    *,
    name: str = "hello-caveats",
    version: str = "1.0",
) -> Formula:
    formula = Formula(name=name, version=version)
    formula.caveats = caveats
    return formula


def _fake_ruby(tmp_path: Path, script: str) -> Path:
    path = tmp_path / "fake-ruby"
    path.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _formula_source(name: str, caveats_block: str | None) -> str:
    class_name = "".join(part.capitalize() for part in name.replace("-", " ").split())
    zeros = "0" * 64
    caveats = "" if caveats_block is None else "\n" + caveats_block + "\n"
    return f'''class {class_name} < Formula
  desc "synthetic caveats fixture"
  homepage "https://example.com/{name}"
  url "https://example.com/{name}-1.0.tar.gz"
  sha256 "{zeros}"
  license "MIT"
{caveats}end
'''


def _write_cli_formula(
    sample, name: str, caveats_block: str | None
) -> Path:
    path = sample.tap / "Formula" / f"{name}.rb"
    path.write_text(_formula_source(name, caveats_block), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# rendering with the real Ruby interpreter
# ---------------------------------------------------------------------------


@requires_ruby
def test_heredoc_caveats_render(tmp_path: Path) -> None:
    source = '''def caveats
  <<~EOS
    To install the documentation, run:
      brew install cmake-docs

    Or read the online manual.
  EOS
end
'''
    formula = _formula(source)
    output = run_caveats(formula, _config(tmp_path), ruby=_REAL_RUBY)

    assert output == (
        "To install the documentation, run:\n"
        "  brew install cmake-docs\n"
        "\n"
        "Or read the online manual.\n"
    )


@requires_ruby
def test_plain_string_and_concatenation(tmp_path: Path) -> None:
    source = 'def caveats\n  "Plain " + "caveat."\nend'
    output = run_caveats(_formula(source), _config(tmp_path), ruby=_REAL_RUBY)
    assert output == "Plain caveat.\n"


@requires_ruby
def test_platform_branch_selects_on_macos_or_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = '''def caveats
  message = "base"
  on_macos do
    message += " macos"
  end
  on_linux do
    message += " linux"
  end
  message
end
'''
    formula = _formula(source)
    config = _config(tmp_path)

    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    assert run_caveats(formula, config, ruby=_REAL_RUBY) == "base macos\n"

    monkeypatch.setenv("PYBREW_TARGET_OS", "linux")
    assert run_caveats(formula, config, ruby=_REAL_RUBY) == "base linux\n"


@requires_ruby
def test_architecture_branch_selects_on_arm_or_intel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = '''def caveats
  message = "base"
  on_arm { message += " arm" }
  on_intel { message += " intel" }
  message
end
'''
    formula = _formula(source)
    config = _config(tmp_path)

    monkeypatch.setenv("PYBREW_TARGET_ARCH", "arm64")
    assert run_caveats(formula, config, ruby=_REAL_RUBY) == "base arm\n"

    monkeypatch.setenv("PYBREW_TARGET_ARCH", "x86_64")
    assert run_caveats(formula, config, ruby=_REAL_RUBY) == "base intel\n"


@requires_ruby
def test_paths_name_and_version_helpers(tmp_path: Path) -> None:
    source = '''def caveats
  <<~EOS
    #{name} #{version} #{version.major_minor}
    prefix=#{prefix}
    bin=#{bin}
    opt_prefix=#{opt_prefix}
    opt_bin=#{opt_bin}
  EOS
end
'''
    formula = _formula(source, version="1.2.3")
    config = _config(tmp_path)
    output = run_caveats(formula, config, ruby=_REAL_RUBY)

    keg = config.keg_path("hello-caveats", "1.2.3")
    opt = config.opt_path("hello-caveats")
    assert output == (
        "hello-caveats 1.2.3 1.2\n"
        f"prefix={keg}\n"
        f"bin={keg / 'bin'}\n"
        f"opt_prefix={opt}\n"
        f"opt_bin={opt / 'bin'}\n"
    )


@requires_ruby
def test_installed_keg_is_used_for_prefix(tmp_path: Path) -> None:
    formula = _formula('def caveats\n  "#{prefix}"\nend')
    config = _config(tmp_path)
    keg = config.keg_path("hello-caveats", "1.0")
    (keg / "bin").mkdir(parents=True)

    output = run_caveats(formula, config, ruby=_REAL_RUBY)
    assert output == f"{keg}\n"


@requires_ruby
def test_ohai_opoo_puts_are_captured_on_stdout(tmp_path: Path) -> None:
    source = '''def caveats
  ohai "notice"
  opoo "careful"
  puts "plain"
  "returned"
end
'''
    output = run_caveats(_formula(source), _config(tmp_path), ruby=_REAL_RUBY)
    assert output == "==> notice\nWarning: careful\nplain\nreturned\n"


@requires_ruby
def test_env_and_utils_helpers(tmp_path: Path) -> None:
    source = '''def caveats
  <<~EOS
    prefix=#{ENV["HOMEBREW_PREFIX"]}
    utils=#{Utils.popen_read("/bin/echo", "utils-ok").strip}
    which=#{Utils.which("sh")}
  EOS
end
'''
    config = _config(tmp_path)
    output = run_caveats(_formula(source), config, ruby=_REAL_RUBY)

    assert f"prefix={config.prefix}\n" in output
    assert "utils=utils-ok\n" in output
    assert "which=/bin/sh\n" in output


@requires_ruby
def test_runner_wrapper_and_alias(tmp_path: Path) -> None:
    formula = _formula('def caveats\n  "wrapped"\nend')
    config = _config(tmp_path)

    assert render_caveats(formula, config, ruby=_REAL_RUBY) == "wrapped\n"
    assert CaveatsRunner(config, ruby=_REAL_RUBY).run(formula) == "wrapped\n"


@requires_ruby
def test_real_fixture_caveats_use_platform_and_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    formula = parse_formula_file(FIXTURES / "python@3.12.rb")
    config = _config(tmp_path)

    output = run_caveats(formula, config, ruby=_REAL_RUBY)
    assert "Python is installed as" in output
    assert f"{config.prefix}/bin/python3.12" in output
    # The on_linux branch must not run on the simulated macOS host.
    assert "`dbm.gnu` is available" in output
    assert "dbm.ndbm" not in output


@requires_ruby
def test_real_ruby_failure_raises_caveats_error(tmp_path: Path) -> None:
    source = '''def caveats
  ohai "before"
  raise "boom"
end
'''
    with pytest.raises(CaveatsError) as excinfo:
        run_caveats(_formula(source), _config(tmp_path), ruby=_REAL_RUBY)

    error = excinfo.value
    assert error.returncode == 1
    assert error.stdout == "==> before\n"
    assert "Caveats failed" in error.stderr
    assert "boom" in error.stderr
    assert "boom" in str(error)
    assert error.command and error.command[0] == _REAL_RUBY


# ---------------------------------------------------------------------------
# missing/empty caveats and missing Ruby
# ---------------------------------------------------------------------------


def test_has_caveats() -> None:
    formula = Formula(name="empty", version="1.0")
    assert has_caveats(formula) is False
    formula.caveats = "  \n"
    assert has_caveats(formula) is False
    formula.caveats = "def caveats\nend\n"
    assert has_caveats(formula) is True


def test_empty_caveats_needs_no_ruby(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYBREW_RUBY", str(tmp_path / "missing-ruby"))
    assert run_caveats(_formula(""), _config(tmp_path)) == ""
    assert run_caveats(_formula("   \n"), _config(tmp_path)) == ""


def test_missing_ruby_has_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYBREW_RUBY", str(tmp_path / "missing-ruby"))
    formula = _formula('def caveats\n  "text"\nend')

    with pytest.raises(CaveatsError, match="Ruby interpreter not found"):
        run_caveats(formula, _config(tmp_path))


# ---------------------------------------------------------------------------
# fake Ruby: argv/environment and failure handling (no real execution)
# ---------------------------------------------------------------------------


def test_fake_ruby_receives_argv_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_ruby(
        tmp_path,
        """
printf 'argc=%s\\n' "$#"
printf 'cwd=%s\\n' "$(basename "$(pwd -P)")"
printf 'prefix=%s\\n' "$HOMEBREW_PREFIX"
printf 'cellar=%s\\n' "$HOMEBREW_CELLAR"
printf 'name=%s\\n' "$PYBREW_FORMULA_NAME"
exit 0
""",
    )
    monkeypatch.setenv("PYBREW_RUBY", str(fake))
    formula = _formula('def caveats\n  "unused"\nend')
    config = _config(tmp_path)

    output = run_caveats(formula, config)

    assert "argc=3" in output
    assert "cwd=pybrew-caveats-" in output
    assert f"prefix={config.prefix}" in output
    assert f"cellar={config.cellar}" in output
    assert "name=hello-caveats" in output


def test_fake_ruby_failure_preserves_stdout_and_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_ruby(
        tmp_path, 'printf "ruby stdout\\n"\nprintf "ruby stderr\\n" >&2\nexit 3'
    )
    monkeypatch.setenv("PYBREW_RUBY", str(fake))
    formula = _formula('def caveats\n  "unused"\nend')

    with pytest.raises(CaveatsError) as excinfo:
        run_caveats(formula, _config(tmp_path))

    error = excinfo.value
    assert error.returncode == 3
    assert error.stdout == "ruby stdout\n"
    assert error.stderr == "ruby stderr\n"
    assert "ruby stderr" in str(error)
    assert error.command and error.command[0] == str(fake)


def test_timeout_raises_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_ruby(tmp_path, "exec sleep 5")
    monkeypatch.setenv("PYBREW_RUBY", str(fake))
    formula = _formula('def caveats\n  "unused"\nend')

    with pytest.raises(CaveatsError, match="timed out"):
        run_caveats(formula, _config(tmp_path), timeout=0.2)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


@requires_ruby
def test_cli_caveats_prints_output_without_install(run_cli, sample) -> None:
    _write_cli_formula(
        sample,
        "hello-caveats",
        '''  def caveats
    <<~EOS
      Hello caveats.
    EOS
  end''',
    )
    proc = run_cli(
        "caveats", "hello-caveats", extra_env={"PYBREW_RUBY": _REAL_RUBY}
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == "Hello caveats.\n"
    # Caveats are shown before the formula is installed.
    assert not (sample.prefix / "Cellar" / "hello-caveats").exists()


@requires_ruby
def test_cli_caveats_failure_returns_one(run_cli, sample) -> None:
    _write_cli_formula(
        sample, "hello-caveats-fail", '  def caveats\n    raise "boom"\n  end'
    )
    proc = run_cli(
        "caveats", "hello-caveats-fail", extra_env={"PYBREW_RUBY": _REAL_RUBY}
    )

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "boom" in proc.stderr


def test_cli_caveats_with_fake_ruby_prints_stdout(
    run_cli, sample, tmp_path: Path
) -> None:
    fake = _fake_ruby(tmp_path, 'printf "from fake ruby\\n"')
    _write_cli_formula(
        sample, "hello-fake-caveats", '  def caveats\n    "ignored"\n  end'
    )
    proc = run_cli(
        "caveats", "hello-fake-caveats", extra_env={"PYBREW_RUBY": str(fake)}
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == "from fake ruby\n"


def test_cli_caveats_without_block_prints_nothing(run_cli, sample) -> None:
    _write_cli_formula(sample, "hello-no-caveats", None)
    proc = run_cli("caveats", "hello-no-caveats")

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_cli_caveats_missing_formula(run_cli) -> None:
    proc = run_cli("caveats", "definitely-not-a-formula-xyz")

    assert proc.returncode == 1
    assert "no formula named" in proc.stderr.lower()
