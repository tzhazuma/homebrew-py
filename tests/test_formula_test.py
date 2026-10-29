"""Tests for the limited formula test DSL runner.

Every fixture is synthetic and local: no network access, no writes outside
``tmp_path`` and no arbitrary test bodies beyond benign assertions.  Tests
that execute the real Ruby interpreter are skipped when it is not installed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pybrew.errors import BuildError
from pybrew.formula_test import (
    FormulaTestError,
    FormulaTestResult,
    FormulaTestRunner,
    has_test_block,
    run_formula_test,
    run_test,
)
from pybrew.models import Formula
from pybrew.paths import Config

_REAL_RUBY = "/usr/bin/ruby"
_HAS_RUBY = os.path.isfile(_REAL_RUBY) and os.access(_REAL_RUBY, os.X_OK)
requires_ruby = pytest.mark.skipif(
    not _HAS_RUBY, reason="real Ruby interpreter (/usr/bin/ruby) not installed"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> Config:
    config = Config.default(
        prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache"
    )
    config.ensure()
    return config


def _installed_formula(
    tmp_path: Path,
    test_source: str,
    *,
    name: str = "hello-test",
    version: str = "1.0",
    installed: bool = True,
) -> tuple[Formula, Config]:
    formula = Formula(name=name, version=version)
    formula.test_source = test_source
    config = _config(tmp_path)
    if installed:
        keg = config.keg_path(name, version)
        (keg / "bin").mkdir(parents=True)
        script = keg / "bin" / name
        script.write_text("#!/bin/sh\necho hello-test\n", encoding="utf-8")
        script.chmod(0o755)
        opt = config.opt_path(name)
        if not os.path.lexists(opt):
            opt.symlink_to(keg)
    return formula, config


def _fake_ruby(tmp_path: Path, script: str) -> Path:
    path = tmp_path / "fake-ruby"
    path.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _formula_source(name: str, test_block: str | None) -> str:
    class_name = "".join(part.capitalize() for part in name.replace("-", " ").split())
    test = "" if test_block is None else "\n" + test_block + "\n"
    return f'''class {class_name} < Formula
  desc "synthetic formula test fixture"
  homepage "https://example.com/{name}"
  url "https://example.com/{name}-1.0.tar.gz"
  sha256 "{"0" * 64}"
  license "MIT"
{test}end
'''


def _write_cli_formula(
    sample,
    name: str,
    test_block: str | None,
    *,
    installed: bool = True,
) -> Path:
    path = sample.tap / "Formula" / f"{name}.rb"
    path.write_text(_formula_source(name, test_block), encoding="utf-8")
    if installed:
        keg = sample.prefix / "Cellar" / name / "1.0"
        (keg / "bin").mkdir(parents=True)
        script = keg / "bin" / name
        script.write_text("#!/bin/sh\necho hello\n", encoding="utf-8")
        script.chmod(0o755)
        opt = sample.prefix / "opt" / name
        opt.parent.mkdir(parents=True, exist_ok=True)
        if not os.path.lexists(opt):
            opt.symlink_to(keg)
    return path


# ---------------------------------------------------------------------------
# runner behaviour with the real Ruby interpreter
# ---------------------------------------------------------------------------


@requires_ruby
def test_benign_test_block_passes(tmp_path: Path) -> None:
    source = """do
  assert_equal "hello-test", shell_output("#{bin}/hello-test").strip
  assert_equal "ok", pipe_output("cat", "ok")
  assert_match(/hello/, "hello world")
  assert_predicate testpath, :directory?
  assert_empty ""
  assert_includes ["a", "b"], "b"
  assert_nil nil
  refute_equal "a", "b"
  refute_match(/z/, "abc")
  refute_predicate testpath, :file?
  refute_empty "x"
  refute_includes ["a"], "b"
  refute_nil "x"
  assert_equal testpath.to_s, ENV["PYBREW_TESTPATH"]
  assert_equal testpath.to_s, ENV["PWD"]
  assert_equal testpath.to_s, Dir.pwd
  assert_equal prefix.to_s, ENV["PYBREW_PREFIX"]
  assert_equal name, "hello-test"
  File.write("marker.txt", "marker")
  assert_equal "marker", shell_output("cat marker.txt").strip
  puts "test-output"
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_test(formula, config, ruby=_REAL_RUBY)

    assert isinstance(result, FormulaTestResult)
    assert result.success is True
    assert result.ok is True
    assert result.returncode == 0
    assert result.formula == "hello-test"
    assert result.stdout == "test-output\n"
    assert result.stderr == ""
    assert result.prefix == str(config.keg_path("hello-test", "1.0"))
    assert result.ruby == _REAL_RUBY
    assert result.command and result.command[0] == _REAL_RUBY
    assert result.duration >= 0
    # The throw-away testpath is removed once the test finishes.
    assert not Path(result.testpath).exists()


@requires_ruby
def test_def_test_source_is_supported(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, "def test\n  assert_equal 2, 1 + 1\nend\n"
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True
    assert result.returncode == 0


@requires_ruby
def test_runner_wrapper_uses_config(tmp_path: Path) -> None:
    formula, config = _installed_formula(tmp_path, "do\n  assert_equal 1, 1\nend")
    result = FormulaTestRunner(config, ruby=_REAL_RUBY).run(formula)
    assert result.success is True


@requires_ruby
def test_failing_assertion_raises_formula_test_error(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, 'do\n  assert_equal "expected", "actual"\nend'
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    error = excinfo.value
    assert isinstance(error, BuildError)
    assert error.returncode == 1
    assert "expected" in str(error).lower()
    assert "actual" in str(error).lower()
    assert "Formula test failed" in error.stderr
    assert error.stdout == ""


@requires_ruby
def test_shell_output_failure_includes_status(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, 'do\n  shell_output("exit 7")\nend'
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "exit status 7" in str(excinfo.value)
    assert excinfo.value.returncode == 1


def test_missing_test_block_raises(tmp_path: Path) -> None:
    formula, config = _installed_formula(tmp_path, "")
    assert has_test_block(formula) is False
    with pytest.raises(FormulaTestError, match="no test block"):
        run_formula_test(formula, config)


def test_not_installed_formula_raises(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, "do\n  assert_equal 1, 1\nend", installed=False
    )
    with pytest.raises(FormulaTestError, match="not installed"):
        run_formula_test(formula, config)


def test_missing_ruby_has_clear_error(tmp_path: Path, monkeypatch) -> None:
    formula, config = _installed_formula(tmp_path, "do\n  assert_equal 1, 1\nend")
    monkeypatch.setenv("PYBREW_RUBY", str(tmp_path / "missing-ruby"))
    with pytest.raises(FormulaTestError, match="Ruby interpreter not found"):
        run_formula_test(formula, config)


def test_has_test_block() -> None:
    formula = Formula(name="empty", version="1.0")
    assert has_test_block(formula) is False
    formula.test_source = "  \n"
    assert has_test_block(formula) is False
    formula.test_source = "do\nend\n"
    assert has_test_block(formula) is True


# ---------------------------------------------------------------------------
# PYBREW_RUBY override, cwd and environment (fake Ruby; no real execution)
# ---------------------------------------------------------------------------


def test_ruby_override_runs_with_testpath_cwd_and_env(tmp_path: Path, monkeypatch) -> None:
    fake = _fake_ruby(
        tmp_path,
        """
printf 'argc=%s\\n' "$#"
printf 'cwd=%s\\n' "$(pwd -P)"
printf 'testpath=%s\\n' "$PYBREW_TESTPATH"
printf 'prefix=%s\\n' "$PYBREW_PREFIX"
printf 'extra=%s\\n' "$PYBREW_FAKE_EXTRA"
printf 'fake stderr\\n' >&2
exit 0
""",
    )
    monkeypatch.setenv("PYBREW_RUBY", str(fake))
    formula, config = _installed_formula(tmp_path, "do\n  assert_equal 1, 1\nend")

    result = run_formula_test(formula, config, env={"PYBREW_FAKE_EXTRA": "yes"})

    assert result.returncode == 0
    assert result.ruby == str(fake)
    assert "argc=3" in result.stdout
    assert "extra=yes" in result.stdout
    assert "fake stderr\n" == result.stderr
    assert f"testpath={result.testpath}" in result.stdout
    assert f"prefix={config.keg_path('hello-test', '1.0')}" in result.stdout
    cwd_line = next(line for line in result.stdout.splitlines() if line.startswith("cwd="))
    assert os.path.realpath(cwd_line.split("=", 1)[1]) == os.path.realpath(result.testpath)


def test_fake_ruby_failure_preserves_stdout_and_stderr(tmp_path: Path, monkeypatch) -> None:
    fake = _fake_ruby(tmp_path, 'printf "ruby stdout\\n"\nprintf "ruby stderr\\n" >&2\nexit 3')
    monkeypatch.setenv("PYBREW_RUBY", str(fake))
    formula, config = _installed_formula(tmp_path, "do\n  assert_equal 1, 1\nend")

    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config)

    error = excinfo.value
    assert error.returncode == 3
    assert error.stdout == "ruby stdout\n"
    assert error.stderr == "ruby stderr\n"
    assert "ruby stderr" in str(error)
    assert error.command and error.command[0] == str(fake)


def test_timeout_raises_clear_error(tmp_path: Path, monkeypatch) -> None:
    fake = _fake_ruby(tmp_path, "exec sleep 5")
    monkeypatch.setenv("PYBREW_RUBY", str(fake))
    formula, config = _installed_formula(tmp_path, "do\n  assert_equal 1, 1\nend")

    with pytest.raises(FormulaTestError, match="timed out"):
        run_formula_test(formula, config, timeout=0.2)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


@requires_ruby
def test_cli_test_prints_pass(run_cli, sample) -> None:
    _write_cli_formula(
        sample,
        "hello-test",
        '  test do\n    assert_equal "hello", shell_output("printf hello")\n  end',
    )
    proc = run_cli("test", "hello-test", extra_env={"PYBREW_RUBY": _REAL_RUBY})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS: hello-test" in proc.stdout


@requires_ruby
def test_cli_test_prints_fail(run_cli, sample) -> None:
    _write_cli_formula(
        sample,
        "hello-fail",
        '  test do\n    assert_equal "a", "b"\n  end',
    )
    proc = run_cli("test", "hello-fail", extra_env={"PYBREW_RUBY": _REAL_RUBY})
    assert proc.returncode == 1
    assert "FAIL: hello-fail" in proc.stdout
    assert "Expected" in proc.stderr


def test_cli_test_requires_installed_formula(run_cli, sample) -> None:
    _write_cli_formula(
        sample,
        "hello-missing",
        "  test do\n    assert_equal 1, 1\n  end",
        installed=False,
    )
    proc = run_cli("test", "hello-missing")
    assert proc.returncode == 1
    assert "FAIL: hello-missing" in proc.stdout
    assert "not installed" in proc.stderr


def test_cli_test_requires_test_block(run_cli, sample) -> None:
    _write_cli_formula(sample, "hello-notest", None)
    proc = run_cli("test", "hello-notest")
    assert proc.returncode == 1
    assert "FAIL: hello-notest" in proc.stdout
    assert "no test block" in proc.stderr
