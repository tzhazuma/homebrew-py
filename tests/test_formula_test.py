"""Tests for the limited formula test DSL runner.

Every fixture is synthetic and local: no network access, no writes outside
``tmp_path`` and no arbitrary test bodies beyond benign assertions.  Tests
that execute the real Ruby interpreter are skipped when it is not installed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from pybrew import formula_test as formula_test_module
from pybrew.errors import BuildError
from pybrew.formula_test import (
    FormulaTestError,
    FormulaTestResult,
    FormulaTestRunner,
    has_test_block,
    run_formula_test,
    run_test,
)
from pybrew.models import Dependency, Formula
from pybrew.paths import Config

_REAL_RUBY = "/usr/bin/ruby"
_HAS_RUBY = os.path.isfile(_REAL_RUBY) and os.access(_REAL_RUBY, os.X_OK)
requires_ruby = pytest.mark.skipif(
    not _HAS_RUBY, reason="real Ruby interpreter (/usr/bin/ruby) not installed"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, taps: list[Path] | None = None) -> Config:
    config = Config.default(
        prefix=tmp_path / "prefix", taps=taps or [], cache=tmp_path / "cache"
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
    taps: list[Path] | None = None,
) -> tuple[Formula, Config]:
    formula = Formula(name=name, version=version)
    formula.test_source = test_source
    config = _config(tmp_path, taps=taps)
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


def _formula_source(
    name: str,
    test_block: str | None,
    *,
    depends_on: list[str] | None = None,
    aliases: list[str] | None = None,
    version: str = "1.0",
) -> str:
    class_name = "".join(part.capitalize() for part in name.replace("-", " ").split())
    deps = "".join(f'  depends_on "{dependency}"\n' for dependency in depends_on or [])
    alias_lines = "".join(f'  alias "{alias}"\n' for alias in aliases or [])
    test = "" if test_block is None else "\n" + test_block + "\n"
    return f'''class {class_name} < Formula
  desc "synthetic formula test fixture"
  homepage "https://example.com/{name}"
  url "https://example.com/{name}-{version}.tar.gz"
  sha256 "{"0" * 64}"
  license "MIT"
{deps}{alias_lines}{test}end
'''


def _write_tap_formula(
    tap: Path,
    name: str,
    test_block: str | None,
    *,
    depends_on: list[str] | None = None,
    aliases: list[str] | None = None,
    version: str = "1.0",
) -> Path:
    path = tap / "Formula" / f"{name}.rb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _formula_source(
            name,
            test_block,
            depends_on=depends_on,
            aliases=aliases,
            version=version,
        ),
        encoding="utf-8",
    )
    return path


def _install_keg(
    config: Config, name: str, *, version: str = "1.0", output: str = "ok"
) -> Path:
    keg = config.keg_path(name, version)
    (keg / "bin").mkdir(parents=True)
    script = keg / "bin" / name
    script.write_text(f"#!/bin/sh\necho {output}\n", encoding="utf-8")
    script.chmod(0o755)
    opt = config.opt_path(name)
    opt.parent.mkdir(parents=True, exist_ok=True)
    if not os.path.lexists(opt):
        opt.symlink_to(keg)
    return keg


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


# ---------------------------------------------------------------------------
# expanded formula test DSL (real Ruby)
# ---------------------------------------------------------------------------


@requires_ruby
def test_path_assertions_pass(tmp_path: Path) -> None:
    source = """do
  (testpath/"sub").mkpath
  (testpath/"data.txt").write "data"
  (testpath/"link").make_symlink("data.txt")
  assert_path_exists testpath/"data.txt"
  assert_path_exists testpath/"sub"
  refute_path_exists testpath/"missing"
  assert_directory testpath/"sub"
  refute_directory testpath/"data.txt"
  assert_file testpath/"data.txt"
  assert_symlink testpath/"link"
  assert_path_exists testpath/"link"
  assert_predicate testpath/"sub", :directory?
  refute_predicate testpath/"data.txt", :directory?
  assert_empty ""
  refute_empty "x"
  assert_includes ["a", "b"], "b"
  refute_includes ["a"], "b"
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert result.success is True
    assert result.stderr == ""


@requires_ruby
def test_path_assertion_failure_names_the_path(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, 'do\n  assert_path_exists testpath/"missing.txt"\nend'
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert excinfo.value.returncode == 1
    assert "missing.txt" in excinfo.value.stderr
    assert "to exist" in excinfo.value.stderr


@requires_ruby
def test_output_assertions_pass(tmp_path: Path) -> None:
    source = """do
  assert_output "hello", "hello"
  refute_output "hello", "world"
  assert_output "piped\\n", pipe_output("cat", "piped\\n")
  assert_output "", shell_output("true")
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert result.success is True


@requires_ruby
def test_output_assertion_failure_is_reported(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, 'do\n  assert_output "expected-output", "actual-output"\nend'
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    stderr = excinfo.value.stderr
    assert "Expected output" in stderr
    assert "expected-output" in stderr
    assert "actual-output" in stderr


@requires_ruby
def test_shell_output_and_pipe_output_options(tmp_path: Path) -> None:
    source = """do
  assert_equal "out", shell_output("printf out; printf err >&2")
  assert_equal "err", shell_output("printf out; printf err >&2", stderr: true)
  assert_equal "custom", shell_output('printf %s "$PYBREW_TEST_EXTRA"',
                                     env: {"PYBREW_TEST_EXTRA" => "custom"})
  (testpath/"work").mkpath
  (testpath/"work"/"marker").write "marker"
  assert_equal "marker", shell_output("cat marker", chdir: testpath/"work")
  assert_equal "pipe-err", pipe_output("printf out; printf pipe-err >&2", nil, stderr: true)
  assert_equal "pipe-env", pipe_output('printf %s "$PYBREW_PIPE_EXTRA"', nil,
                                      env: {"PYBREW_PIPE_EXTRA" => "pipe-env"})
  assert_equal "marker", pipe_output("cat marker", nil, chdir: testpath/"work")
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert result.success is True


@requires_ruby
def test_shell_output_failure_with_stderr_option_still_raises(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, 'do\n  shell_output("printf boom >&2; exit 4", stderr: true)\nend'
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "exit status 4" in str(excinfo.value)
    assert "boom" in excinfo.value.stderr


@requires_ruby
def test_formula_reference_exposes_paths(tmp_path: Path) -> None:
    source = """do
  ref = Formula["hello-test"]
  assert_equal "hello-test", ref.name
  assert_equal prefix.to_s, ref.prefix.to_s
  assert_equal opt_prefix.to_s, ref.opt_prefix.to_s
  assert_equal bin.to_s, ref.bin.to_s
  assert_equal testpath.to_s, ref.testpath.to_s
  assert_predicate ref.testpath, :directory?
  assert_predicate ref.bin, :directory?
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert result.success is True


@requires_ruby
def test_formula_reference_unknown_name_fails_clearly(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, 'do\n  Formula["not-this-formula"].prefix\nend'
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "not-this-formula" in excinfo.value.stderr
    assert "not available" in excinfo.value.stderr


@requires_ruby
def test_testpath_is_under_configured_prefix(tmp_path: Path) -> None:
    formula, config = _installed_formula(tmp_path, "do\n  assert_equal 1, 1\nend")
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)

    testpath = os.path.realpath(result.testpath)
    prefix = os.path.realpath(config.prefix)
    assert os.path.commonpath([testpath, prefix]) == prefix
    assert not Path(result.testpath).exists()


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


@requires_ruby
def test_extended_assertion_helpers(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        '''do
  assert_operator 2, :>, 1
  assert_kind_of String, "value"
  assert_instance_of String, "value"
  assert_respond_to "value", :upcase
  assert_not_equal 1, 2
  assert_path_not_exists prefix/"missing"
  assert_output("hello\\n") { puts "hello" }
  assert_silent { }
  assert_raises(RuntimeError) { raise "boom" }
end''',
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_shell_output_expected_status(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        '''do
  assert_equal "", shell_output("exit 7", 7)
  assert_equal "out", shell_output("printf out; exit 3", 3)
end''',
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


# ---------------------------------------------------------------------------
# dependency / Formula lookup in the test context (real Ruby)
# ---------------------------------------------------------------------------


@requires_ruby
def test_formula_lookup_resolves_declared_dependencies(tmp_path: Path) -> None:
    source = """do
  dep = Formula["dep-lib"]
  assert_equal "dep-lib", dep.name
  assert_equal "dep-lib", dep.full_name
  assert_equal "2.0", dep.version
  assert_equal (cellar/"dep-lib"/"2.0").to_s, dep.prefix.to_s
  assert_equal (homebrew_prefix/"opt"/"dep-lib").to_s, dep.opt_prefix.to_s
  assert_equal (dep.prefix/"bin").to_s, dep.bin.to_s
  assert_equal (dep.prefix/"lib").to_s, dep.lib.to_s
  assert dep.installed?
  assert dep.any_version_installed?
  assert dep.latest_version_installed?
  assert_equal ["dep-lib"], Formula["hello-test"].deps.map(&:name)
  assert_equal "dep-lib", Formula.factory("dep-lib").name
  assert_equal Formula["dep-lib"], dep
  assert Formula["hello-test"].test_defined?
  refute Formula["dep-lib"].test_defined?
end
"""
    formula, config = _installed_formula(tmp_path, source)
    formula.dependencies = [Dependency(name="dep-lib")]
    dependency_keg = config.keg_path("dep-lib", "2.0")
    (dependency_keg / "bin").mkdir(parents=True)
    (dependency_keg / "lib").mkdir()

    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_formula_lookup_for_uninstalled_dependency_is_explicit(tmp_path: Path) -> None:
    source = """do
  dep = Formula["dep-missing"]
  refute dep.installed?
  assert_equal (homebrew_prefix/"opt"/"dep-missing").to_s, dep.opt_prefix.to_s
  error = begin
    dep.prefix
  rescue StandardError => error
    error
  end
  assert_match(/not installed/, error.message)
end
"""
    formula, config = _installed_formula(tmp_path, source)
    formula.dependencies = [Dependency(name="dep-missing")]

    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_formula_test_entry_rejects_reentrant_call(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, 'do\n  Formula["hello-test"].test\nend'
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "cannot be called from inside its own test block" in excinfo.value.stderr


@requires_ruby
def test_dependency_test_requires_installation(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "dep-lib", "  test do\n    assert true\n  end")
    formula, config = _installed_formula(
        tmp_path, 'do\n  Formula["dep-lib"].test\nend', taps=[tap]
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "the formula is not installed" in excinfo.value.stderr


@requires_ruby
def test_extra_formula_paths_are_available(tmp_path: Path) -> None:
    source = """do
  assert_equal (homebrew_prefix/"etc"/"hello-test").to_s, pkgetc.to_s
  assert_equal (prefix/"share"/"man"/"man1").to_s, man1.to_s
  assert_equal (prefix/"share"/"doc"/"hello-test").to_s, doc.to_s
  assert_equal (prefix/"share"/"info").to_s, info.to_s
  assert_equal (opt_prefix/"lib").to_s, opt_lib.to_s
  ref = Formula["hello-test"]
  assert_equal pkgetc.to_s, ref.pkgetc.to_s
  assert_equal doc.to_s, ref.doc.to_s
  assert_equal man1.to_s, ref.man1.to_s
  assert_equal opt_lib.to_s, ref.opt_lib.to_s
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


# ---------------------------------------------------------------------------
# path/type assertions (real Ruby)
# ---------------------------------------------------------------------------


@requires_ruby
def test_extended_path_type_assertions_pass(tmp_path: Path) -> None:
    source = """do
  (testpath/"data.txt").write "data"
  (testpath/"empty.txt").write ""
  (testpath/"sub").mkpath
  (testpath/"link").make_symlink("data.txt")
  (testpath/"tool").write "#!/bin/sh\\nexit 0\\n"
  File.chmod(0755, (testpath/"tool").to_s)
  assert_file testpath/"data.txt"
  refute_file testpath/"sub"
  assert_directory testpath/"sub"
  refute_directory testpath/"data.txt"
  assert_symlink testpath/"link"
  refute_symlink testpath/"data.txt"
  assert_executable testpath/"tool"
  refute_executable testpath/"data.txt"
  assert_path_exists testpath/"empty.txt"
  assert_empty File.read(testpath/"empty.txt")
  refute_empty File.read(testpath/"data.txt")
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_refute_file_failure_names_the_path(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        'do\n  (testpath/"plain").write "x"\n  refute_file testpath/"plain"\nend',
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "plain" in excinfo.value.stderr
    assert "to not be a file" in excinfo.value.stderr


@requires_ruby
def test_assert_executable_failure_names_the_path(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        'do\n  (testpath/"plain").write "x"\n  assert_executable testpath/"plain"\nend',
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "plain" in excinfo.value.stderr
    assert "to be executable" in excinfo.value.stderr


# ---------------------------------------------------------------------------
# shell_output / pipe_output edge behaviour (real Ruby)
# ---------------------------------------------------------------------------


@requires_ruby
def test_pipe_output_expected_status_and_ignored_status(tmp_path: Path) -> None:
    source = """do
  assert_equal "ok", pipe_output("cat", "ok", 0)
  assert_equal "out", pipe_output("printf out; exit 3", nil, 3)
  assert_equal "out", pipe_output("printf out; exit 3", nil)
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_pipe_output_expected_status_failure_is_reported(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, 'do\n  pipe_output("printf boom >&2; exit 5", nil, 0)\nend'
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "pipe_output failed with exit status 5" in excinfo.value.stderr
    assert "boom" in excinfo.value.stderr


@requires_ruby
def test_shell_helpers_accept_pathname_commands(tmp_path: Path) -> None:
    source = """do
  tool = testpath/"tool"
  tool.write "#!/bin/sh\\nprintf pathname-ok\\n"
  File.chmod(0755, tool.to_s)
  assert_equal "pathname-ok", shell_output(tool)
  assert_equal "pathname-ok", pipe_output(tool, nil, 0)
  missing = testpath/"missing-tool"
  error = begin
    shell_output(missing)
  rescue StandardError => error
    error
  end
  assert_match(/does not exist/, error.message)
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


# ---------------------------------------------------------------------------
# Minitest block forms and extra assertions (real Ruby)
# ---------------------------------------------------------------------------


@requires_ruby
def test_assert_output_block_forms_and_capture_io(tmp_path: Path) -> None:
    source = """do
  assert_output(/hel+o/) { puts "hello" }
  assert_output("out\\n", "err\\n") { puts "out"; $stderr.puts "err" }
  assert_output(nil, /warn/) { $stderr.puts "warning" }
  assert_output("") { }
  assert_output(nil) { puts "ignored" }
  refute_output("other") { puts "actual" }
  out, err = capture_io do
    puts "captured"
    $stderr.puts "captured-err"
  end
  assert_equal "captured\\n", out
  assert_equal "captured-err\\n", err
  sub_out, sub_err = capture_subprocess_io do
    system("printf child-out")
    system("printf child-err >&2")
  end
  assert_equal "child-out", sub_out
  assert_equal "child-err", sub_err
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_assert_output_block_failure_reports_stdout(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, 'do\n  assert_output("expected") { puts "actual" }\nend'
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    stderr = excinfo.value.stderr
    assert "Expected stdout" in stderr
    assert "expected" in stderr
    assert "actual" in stderr


@requires_ruby
def test_minitest_block_and_numeric_assertions(tmp_path: Path) -> None:
    source = '''do
  assert_in_delta 1.0, 1.05, 0.1
  refute_in_delta 1.0, 1.5, 0.1
  assert_in_epsilon 100.0, 100.05
  refute_in_epsilon 100.0, 120.0
  value = "same"
  other = "same"
  assert_same value, value
  refute_same value, other
  refute_instance_of Integer, "text"
  refute_kind_of Integer, "text"
  refute_respond_to "text", :not_a_method
  assert_throws(:done) { throw :done }
  assert pass
  error = assert_raises { raise "default error" }
  assert_equal RuntimeError, error.class
  error2 = assert_raises(RuntimeError, "custom context") { raise "boom" }
  assert_equal "boom", error2.message
end'''
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_flunk_failure_is_reported(tmp_path: Path) -> None:
    formula, config = _installed_formula(tmp_path, 'do\n  flunk "nope"\nend')
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "nope" in excinfo.value.stderr


@requires_ruby
def test_assert_raises_reports_unexpected_exception(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        'do\n  assert_raises(ArgumentError) { raise RuntimeError, "wrong" }\nend',
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    stderr = excinfo.value.stderr
    assert "Expected [ArgumentError] to be raised" in stderr
    assert "RuntimeError" in stderr
    assert "wrong" in stderr


# ---------------------------------------------------------------------------
# remaining Minitest surface (real Ruby)
# ---------------------------------------------------------------------------


@requires_ruby
def test_refute_operator_and_assert_send(tmp_path: Path) -> None:
    source = """do
  assert_operator 2, :>, 1
  assert_operator 2, :positive?
  refute_operator 1, :>, 2
  refute_operator 2, :negative?
  assert_send [2, :+, 2]
  assert_send ["text", :start_with?, "te"]
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_refute_operator_failure_is_reported(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, "do\n  refute_operator 1, :<, 2\nend"
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "to be falsey" in excinfo.value.stderr


@requires_ruby
def test_assert_operator_predicate_failure_is_reported(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, "do\n  assert_operator 2, :negative?\nend"
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "to satisfy" in excinfo.value.stderr


@requires_ruby
def test_assert_send_failure_is_reported(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, "do\n  assert_send [1, :zero?]\nend"
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "to return true" in excinfo.value.stderr


@requires_ruby
def test_skip_marks_the_test_as_skipped(tmp_path: Path) -> None:
    source = """do
  refute skipped?
  begin
    skip "not applicable"
  rescue FormulaTestSkip
    assert skipped?
    raise
  end
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert result.success is True
    assert result.returncode == 0
    assert "Formula test skipped: not applicable" in result.stdout


@requires_ruby
def test_assertions_counter_tracks_assertions(tmp_path: Path) -> None:
    source = """do
  assert_equal 0, assertions
  assert_equal 1, 1
  refute false
  begin
    assert_nil "not nil"
  rescue StandardError
  end
  assert_equal 4, assertions
  self.assertions = 7
  assert_equal 7, assertions
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_assert_pattern_fallback_behaviour(tmp_path: Path) -> None:
    source = """do
  assert_pattern { 1 + 1 }
  no_block = begin
    assert_pattern
  rescue StandardError => error
    error
  end
  assert_match(/requires a block/, no_block.message)
  error = begin
    refute_pattern { }
  rescue StandardError => error
    error
  end
  assert_match(/NoMatchingPatternError expected/, error.message)
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


# ---------------------------------------------------------------------------
# test_fixtures helper (real Ruby)
# ---------------------------------------------------------------------------


@requires_ruby
def test_test_fixtures_helper_resolves_configured_directory(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "sample.txt").write_text("fixture-data", encoding="utf-8")
    source = """do
  fixture = test_fixtures("sample.txt")
  assert_path_exists fixture
  assert_equal "fixture-data", fixture.read
  assert_equal fixture.to_s, Formula["hello-test"].test_fixtures("sample.txt").to_s
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={"PYBREW_TEST_FIXTURES": str(fixtures)},
    )
    assert result.success is True


@requires_ruby
def test_test_fixtures_without_configuration_fails_clearly(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, 'do\n  test_fixtures("sample.txt")\nend'
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(
            formula,
            config,
            ruby=_REAL_RUBY,
            env={"PYBREW_TEST_FIXTURES": ""},
        )

    assert "test_fixtures is not configured" in excinfo.value.stderr


@requires_ruby
def test_test_fixtures_missing_file_fails_clearly(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    formula, config = _installed_formula(
        tmp_path, 'do\n  test_fixtures("missing.txt")\nend'
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(
            formula,
            config,
            ruby=_REAL_RUBY,
            env={"PYBREW_TEST_FIXTURES": str(fixtures)},
        )

    assert "does not exist" in excinfo.value.stderr


# ---------------------------------------------------------------------------
# platform constants and read-only helpers (real Ruby)
# ---------------------------------------------------------------------------


@requires_ruby
def test_homebrew_constants_and_platform_modules(tmp_path: Path) -> None:
    source = """do
  assert_equal homebrew_prefix.to_s, HOMEBREW_PREFIX.to_s
  assert_equal cellar.to_s, HOMEBREW_CELLAR.to_s
  assert HOMEBREW_PREFIX.is_a?(Pathname)
  assert HOMEBREW_CELLAR.is_a?(Pathname)
  assert_equal OS.mac?, !OS.linux?
  assert_includes [:arm64, :x86_64], Hardware::CPU.arch
  assert_equal Hardware::CPU.arm?, !Hardware::CPU.intel?
  assert_kind_of Integer, Hardware::CPU.cores
  assert Hardware::CPU.cores >= 1
  assert_kind_of Integer, Hardware::CPU.physical_cores
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_macos_version_module(tmp_path: Path) -> None:
    source = """do
  if OS.mac?
    assert_equal "14", MacOS.version.to_s
    assert_equal "14.5.1", MacOS.full_version.to_s
    assert MacOS.version >= :ventura
    assert MacOS.version < :sequoia
    assert_equal :sonoma, MacOS.version.to_sym
    assert_equal MacOS.version.to_s, OS::Mac.version.to_s
  end
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={"PYBREW_MACOS_VERSION": "14.5.1"},
    )
    assert result.success is True


# ---------------------------------------------------------------------------
# dependency test bodies (best-effort, local taps only)
# ---------------------------------------------------------------------------


@requires_ruby
def test_dependency_test_body_executes(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap,
        "dep-lib",
        '''  test do
    assert_equal "dep-lib", name
    assert_equal "dep-ok", shell_output("#{bin}/dep-lib").strip
    (testpath/"dep-marker").write "marker"
    assert_path_exists testpath/"dep-marker"
  end''',
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  Formula["dep-lib"].test
  assert Formula["dep-lib"].test_defined?
  assert_equal "hello-test", Formula["hello-test"].name
  refute_path_exists testpath/"dep-marker"
end
""",
        taps=[tap],
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib", output="dep-ok")

    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_dependency_without_test_block_fails_clearly(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "dep-notest", None)
    formula, config = _installed_formula(
        tmp_path, 'do\n  Formula["dep-notest"].test\nend', taps=[tap]
    )
    formula.dependencies = [Dependency(name="dep-notest")]
    _install_keg(config, "dep-notest")

    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "has no test block" in excinfo.value.stderr


@requires_ruby
def test_dependency_test_source_unavailable_fails_clearly(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path, 'do\n  Formula["dep-lib"].test\nend', taps=[]
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib")

    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "could not load its test block" in excinfo.value.stderr
    assert "no formula named" in excinfo.value.stderr


@requires_ruby
def test_dependency_test_failure_propagates(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap, "dep-lib", '  test do\n    assert_equal "expected", "actual"\n  end'
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  parent_testpath = testpath.to_s
  (testpath/"parent-marker").write "parent"
  begin
    Formula["dep-lib"].test
  ensure
    assert_equal parent_testpath, Dir.pwd
    assert_path_exists testpath/"parent-marker"
    assert_equal parent_testpath, ENV["PYBREW_TESTPATH"]
    assert_equal "hello-test", name
  end
end
""",
        taps=[tap],
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib")

    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "Expected" in excinfo.value.stderr
    assert "expected" in excinfo.value.stderr


@requires_ruby
def test_dependency_test_skip_propagates(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap, "dep-lib", '  test do\n    skip "dep not applicable"\n  end'
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  parent_testpath = testpath.to_s
  parent_prefix = ENV["PYBREW_PREFIX"]
  parent_tmpdir = ENV["TMPDIR"]
  begin
    Formula["dep-lib"].test
  ensure
    assert_equal parent_testpath, Dir.pwd
    assert_equal parent_prefix, ENV["PYBREW_PREFIX"]
    assert_equal parent_testpath, ENV["PYBREW_TESTPATH"]
    assert_equal parent_tmpdir, ENV["TMPDIR"]
    assert_equal "hello-test", name
  end
end
""",
        taps=[tap],
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib")

    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True
    assert "Formula test skipped: dep not applicable" in result.stdout


@requires_ruby
def test_nested_dependency_test_body_executes(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap, "grandchild", '  test do\n    assert_equal "grandchild", name\n  end'
    )
    _write_tap_formula(
        tap,
        "dep-lib",
        '  test do\n    Formula["grandchild"].test\n    assert_equal "dep-lib", name\n  end',
        depends_on=["grandchild"],
    )
    formula, config = _installed_formula(
        tmp_path, 'do\n  Formula["dep-lib"].test\nend', taps=[tap]
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib")
    _install_keg(config, "grandchild")

    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


# ---------------------------------------------------------------------------
# arbitrary local tap formula lookup (bounded, local files only)
# ---------------------------------------------------------------------------


@requires_ruby
def test_arbitrary_tap_formula_metadata_lookup(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "tool-x", "  test do\n    assert true\n  end")
    formula, config = _installed_formula(
        tmp_path,
        """do
  ref = Formula["tool-x"]
  assert_equal "tool-x", ref.name
  assert_equal "sample-tap/tool-x", ref.full_name
  assert_equal "1.0", ref.version
  refute ref.installed?
  assert_equal (homebrew_prefix/"opt"/"tool-x").to_s, ref.opt_prefix.to_s
  assert ref.test_defined?
  assert_equal "tool-x", Formula.factory("tool-x").name
end
""",
        taps=[tap],
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_tap_qualified_formula_lookup(tmp_path: Path) -> None:
    tap = tmp_path / "Taps" / "homebrew-sample"
    _write_tap_formula(tap, "tool-x", "  test do\n    assert true\n  end")
    formula, config = _installed_formula(
        tmp_path,
        """do
  qualified = Formula["homebrew/sample/tool-x"]
  assert_equal "tool-x", qualified.name
  assert_equal "homebrew/sample/tool-x", qualified.full_name
  assert_equal Formula["tool-x"], qualified
end
""",
        taps=[tap],
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_tap_formula_alias_lookup(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap,
        "tool-x",
        "  test do\n    assert true\n  end",
        aliases=["tool-alias"],
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  assert_equal "tool-x", Formula["tool-x"].name
  assert_equal "tool-x", Formula["tool-alias"].name
  assert_equal Formula["tool-x"], Formula["tool-alias"]
end
""",
        taps=[tap],
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_installed_tap_formula_lookup_without_literal_reference(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap,
        "tool-y",
        """  test do
    assert_equal "tool-y", name
    (testpath/"tool-y-marker").write "marker"
    assert_path_exists testpath/"tool-y-marker"
  end""",
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  ref = Formula[["tool", "y"].join("-")]
  assert_equal "tool-y", ref.name
  assert ref.installed?
  assert_equal "1.0", ref.version
  assert_equal "tool-y-ok", shell_output("#{ref.bin}/tool-y").strip
  ref.test
  refute_path_exists testpath/"tool-y-marker"
end
""",
        taps=[tap],
    )
    _install_keg(config, "tool-y", output="tool-y-ok")
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_unknown_formula_with_taps_fails_clearly(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "tool-x", None)
    formula, config = _installed_formula(
        tmp_path, 'do\n  Formula["missing-formula"].name\nend', taps=[tap]
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "missing-formula" in excinfo.value.stderr
    assert "not available" in excinfo.value.stderr


@requires_ruby
def test_uninstalled_arbitrary_formula_test_fails_clearly(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "tool-x", "  test do\n    assert true\n  end")
    formula, config = _installed_formula(
        tmp_path, 'do\n  Formula["tool-x"].test\nend', taps=[tap]
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "the formula is not installed" in excinfo.value.stderr


@requires_ruby
def test_tap_formula_metadata_cache_refreshes(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "tool-x", None, version="1.0")
    formula, config = _installed_formula(
        tmp_path,
        'do\n  assert_equal "1.0", Formula["tool-x"].version\nend',
        taps=[tap],
    )
    first = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert first.success is True

    _write_tap_formula(tap, "tool-x", None, version="2.0.1")
    formula.test_source = 'do\n  assert_equal "2.0.1", Formula["tool-x"].version\nend'
    second = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert second.success is True


# ---------------------------------------------------------------------------
# read-only platform helpers (SDK, OS version, CPU features)
# ---------------------------------------------------------------------------


@requires_ruby
def test_macos_sdk_helpers_use_configured_sdk(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk" / "MacOSX14.5.sdk"
    sdk.mkdir(parents=True)
    (sdk / "SDKSettings.json").write_text('{"Version": "14.5"}', encoding="utf-8")
    formula, config = _installed_formula(
        tmp_path,
        """do
  path = MacOS.sdk_path
  assert path.is_a?(Pathname)
  assert_path_exists path
  assert_equal "MacOSX14.5.sdk", path.basename.to_s
  version = MacOS.sdk_version
  assert_kind_of OS::Mac::Version, version
  assert_equal "14.5", version.to_s
  assert_equal "14.5", OS::Mac.sdk_version.to_s
end
""",
    )
    result = run_formula_test(
        formula, config, ruby=_REAL_RUBY, env={"PYBREW_SDK_PATH": str(sdk)}
    )
    assert result.success is True


@requires_ruby
def test_macos_sdk_version_falls_back_to_directory_name(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk" / "MacOSX13.3.sdk"
    sdk.mkdir(parents=True)
    # A malformed SDKSettings.json must fall back to the directory name.
    (sdk / "SDKSettings.json").write_text("{not-json", encoding="utf-8")
    formula, config = _installed_formula(
        tmp_path,
        """do
  assert_equal "13.3", MacOS.sdk_version.to_s
end
""",
    )
    result = run_formula_test(
        formula, config, ruby=_REAL_RUBY, env={"PYBREW_SDK_PATH": str(sdk)}
    )
    assert result.success is True


@requires_ruby
def test_macos_sdk_helpers_fall_back_when_missing(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        """do
  assert_nil MacOS.sdk_path
  assert_nil MacOS.sdk_version
end
""",
    )
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={"PYBREW_SDK_PATH": str(tmp_path / "missing-sdk")},
    )
    assert result.success is True


@requires_ruby
def test_macos_release_names(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        """do
  assert_equal :sonoma, MacOS.version.to_sym
  assert_equal "Sonoma", MacOS.release_name
  assert_equal "Sonoma", MacOS.pretty_name
  assert_equal "Sonoma", MacOS.version.release_name
  assert_equal "Sonoma", MacOS.version.pretty_name
end
""",
    )
    result = run_formula_test(
        formula, config, ruby=_REAL_RUBY, env={"PYBREW_MACOS_VERSION": "14.5.1"}
    )
    assert result.success is True


@requires_ruby
def test_macos_release_names_for_unknown_version(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        """do
  assert_nil MacOS.release_name
  assert_equal "Dunno", MacOS.pretty_name
  assert_equal :dunno, MacOS.version.to_sym
end
""",
    )
    result = run_formula_test(
        formula, config, ruby=_REAL_RUBY, env={"PYBREW_MACOS_VERSION": "99.1"}
    )
    assert result.success is True


@requires_ruby
def test_os_version_helper(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        """do
  if OS.mac?
    assert_equal MacOS.version.to_s, OS.version.to_s
  else
    assert_kind_of String, OS.version
    refute_empty OS.version
  end
  assert_kind_of String, OS.kernel_version
  refute_empty OS.kernel_version
end
""",
    )
    result = run_formula_test(
        formula, config, ruby=_REAL_RUBY, env={"PYBREW_MACOS_VERSION": "14.5.1"}
    )
    assert result.success is True


@requires_ruby
def test_hardware_cpu_features_and_flags(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        """do
  flags = Hardware::CPU.flags
  features = Hardware::CPU.features
  assert_kind_of Array, flags
  assert_kind_of Array, features
  assert flags.all? { |flag| flag.is_a?(String) }
  assert features.all? { |feature| feature.is_a?(Symbol) }
  assert_equal flags.map(&:to_sym), features
  assert_equal features.include?(:sse4_2), Hardware::CPU.feature?(:sse4_2)
  assert_includes [true, false], Hardware::CPU.sse4_2?
  assert_includes [true, false], Hardware::CPU.avx512?
  if flags.include?("sse4_2") || flags.include?("sse4.2")
    assert Hardware::CPU.sse4_2?
  end
  assert Hardware::CPU.avx512? if flags.include?("avx512f")
end
""",
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


# ---------------------------------------------------------------------------
# Version / revision objects (String-compatible, best-effort)
# ---------------------------------------------------------------------------


@requires_ruby
def test_formula_version_object_semantics(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        """do
  ref = Formula["hello-test"]
  version = ref.version
  assert version.is_a?(String)
  assert_kind_of Version, version
  assert_equal "1.2.3", version
  assert_equal "1.2.3", version.to_s
  assert_equal "1.2.3", version.to_str
  assert_equal "v1.2.3", "v" + version
  assert_equal "1.2.3", "#{version}"
  assert_equal 1, version.major
  assert_equal 2, version.minor
  assert_equal 3, version.patch
  assert_equal [1, 2, 3], version.segments
  assert_equal "1.2", version.major_minor.to_s
  assert_equal "1.2.3", version.major_minor_patch.to_s
  assert version > "1.2"
  assert version < "1.10"
  assert version > 1
  assert_equal 0, version <=> "1.2.3"
  assert_equal 0, version <=> "1.2.3.0"
  assert version == "1.2.3.0"
  assert_equal 0, version <=> Version.new("1.2.3")
end
""",
        version="1.2.3",
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_formula_pkg_version_and_revision(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        """do
  ref = Formula["hello-test"]
  assert_equal 2, ref.revision
  assert_equal "1.2.3", ref.version.to_s
  pkg = ref.pkg_version
  assert pkg.is_a?(String)
  assert_kind_of Version, pkg
  assert_equal "1.2.3_2", pkg
  assert_equal "1.2.3_2", pkg.to_s
  assert_equal "1.2.3_2", pkg.to_str
  assert_equal 2, pkg.revision
  assert_equal "1.2.3", pkg.version.to_s
  assert_equal 1, pkg.major
  assert_equal 2, pkg.minor
  assert_equal 3, pkg.patch
  assert_equal [1, 2, 3], pkg.segments
  assert pkg > "1.2.3_1"
  assert_equal pkg, PkgVersion.parse("1.2.3_2")
end
""",
        version="1.2.3",
    )
    formula.revision = 2
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_dependency_version_and_revision_from_installed_keg(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "dep-lib", None, version="1.0")
    formula, config = _installed_formula(
        tmp_path,
        """do
  dep = Formula["dep-lib"]
  assert_equal "1.0", dep.version.to_s
  assert_equal 1, dep.revision
  pkg = dep.pkg_version
  assert_equal "1.0_1", pkg.to_s
  assert_equal 1, pkg.revision
  assert_equal "1.0", pkg.version.to_s
end
""",
        taps=[tap],
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib", version="1.0_1")
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_formula_version_falls_back_to_pkg_version(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        """do
  ref = Formula["hello-test"]
  assert_equal "HEAD", ref.version.to_s
  assert_equal "HEAD", ref.pkg_version.to_s
  assert_equal 0, ref.revision
end
""",
    )
    formula.version = None
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_context_version_is_string_compatible(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        """do
  assert version.is_a?(String)
  assert_equal "1.2.3", version
  assert_equal "1.2.3", version.to_s
  assert_equal 1, version.major
  assert_equal 2, version.minor
  assert_equal 3, version.patch
end
""",
        version="1.2.3",
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_dependency_test_uses_isolated_testpath_and_environment(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap,
        "dep-lib",
        '''  test do
    assert_equal "dep-lib", name
    assert_equal prefix.to_s, ENV["PYBREW_PREFIX"]
    assert_equal prefix.to_s, ENV["PYBREW_FORMULA_PREFIX"]
    assert_equal testpath.to_s, ENV["PYBREW_TESTPATH"]
    assert_equal testpath.to_s, ENV["PWD"]
    assert_equal testpath.to_s, ENV["TMPDIR"]
    assert_equal testpath.to_s, Dir.pwd
    assert_nil ENV["PYBREW_PARENT_SECRET"]
    refute_equal ENV["PARENT_TESTPATH"], testpath.to_s
    (testpath/"dep-only").write "dep"
    File.write("relative-dep", "dep")
    assert_path_exists testpath/"dep-only"
    assert_path_exists testpath/"relative-dep"
    File.write(File.join(ENV["PARENT_TESTPATH"], "dep-testpath.txt"), testpath.to_s)
  end''',
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  parent_testpath = testpath.to_s
  parent_prefix = ENV["PYBREW_PREFIX"]
  parent_tmpdir = ENV["TMPDIR"]
  (testpath/"parent-marker").write "parent"
  ENV["PARENT_TESTPATH"] = parent_testpath
  ENV["PYBREW_PARENT_SECRET"] = "hidden"
  Formula["dep-lib"].test
  assert_path_exists testpath/"parent-marker"
  assert_equal "hidden", ENV["PYBREW_PARENT_SECRET"]
  assert_equal parent_testpath, ENV["PARENT_TESTPATH"]
  refute_path_exists testpath/"dep-only"
  refute_path_exists testpath/"relative-dep"
  assert_equal parent_testpath, Dir.pwd
  assert_equal parent_prefix, ENV["PYBREW_PREFIX"]
  assert_equal parent_testpath, ENV["PYBREW_TESTPATH"]
  assert_equal parent_tmpdir, ENV["TMPDIR"]
  assert_equal "hello-test", name
  dep_testpath = Pathname.new((testpath/"dep-testpath.txt").read)
  refute_equal parent_testpath, dep_testpath.to_s
  assert_equal Pathname.new(parent_testpath).parent.to_s, dep_testpath.parent.to_s
  refute_path_exists dep_testpath
end
""",
        taps=[tap],
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib", output="dep-ok")
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


# ---------------------------------------------------------------------------
# versioned macOS SDK helpers (versioned dirs, fake xcrun, SDKROOT)
# ---------------------------------------------------------------------------


def _fake_executable(base: Path, name: str, script: str) -> Path:
    path = base / name
    path.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_sdk_dir(root: Path, version: str) -> Path:
    sdk = root / f"MacOSX{version}.sdk"
    sdk.mkdir(parents=True)
    (sdk / "SDKSettings.json").write_text(f'{{"Version": "{version}"}}', encoding="utf-8")
    return sdk


@requires_ruby
def test_macos_sdk_selection_by_version(tmp_path: Path) -> None:
    sdk_root = tmp_path / "sdks"
    _make_sdk_dir(sdk_root, "13.3")
    _make_sdk_dir(sdk_root, "14.5")
    formula, config = _installed_formula(
        tmp_path,
        """do
  assert_equal ["13.3", "14.5"], MacOS.sdks.map { |sdk| sdk.version.to_s }
  assert_equal "14.5", MacOS.latest_sdk_version.to_s
  assert_equal "14.5", MacOS.sdk.version.to_s
  assert_equal "MacOSX14.5.sdk", MacOS.sdk_path.basename.to_s
  assert_equal "13.3", MacOS.sdk("13.3").version.to_s
  assert_equal "MacOSX13.3.sdk", MacOS.sdk_path("13.3").basename.to_s
  assert_equal "14.5", MacOS.sdk(:sonoma).version.to_s
  assert_nil MacOS.sdk("99.0")
  assert_nil MacOS.sdk_path("99.0")
  assert_equal "14.5", MacOS.sdk_version.to_s
end
""",
    )
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={
            "PYBREW_SDK_DIRS": str(sdk_root),
            "PYBREW_MACOS_VERSION": "14.5.1",
            "PYBREW_SDK_PATH": "",
            "PYBREW_SDK_VERSION": "",
            "PYBREW_XCRUN": "",
            "SDKROOT": "",
        },
    )
    assert result.success is True


@requires_ruby
def test_macos_sdk_uses_fake_xcrun_from_path(tmp_path: Path) -> None:
    sdk = tmp_path / "FakeSDK"
    sdk.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_executable(
        bin_dir,
        "xcrun",
        "case \"$1\" in\n"
        f"  --show-sdk-path) printf '%s' \"{sdk}\" ;;\n"
        "  --show-sdk-version) printf '%s' \"14.2\" ;;\n"
        "  *) exit 64 ;;\n"
        "esac",
    )
    empty = tmp_path / "empty-sdks"
    empty.mkdir()
    formula, config = _installed_formula(
        tmp_path,
        """do
  sdk = MacOS.sdk
  assert_kind_of OS::Mac::SDK, sdk
  assert_equal "14.2", sdk.version.to_s
  assert_equal "FakeSDK", sdk.path.basename.to_s
  assert_equal sdk.path.to_s, MacOS.sdk_path.to_s
  assert_equal "14.2", MacOS.sdk_version.to_s
  assert_equal "14.2", MacOS.latest_sdk_version.to_s
end
""",
    )
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYBREW_SDK_DIRS": str(empty),
            "PYBREW_MACOS_VERSION": "14.2",
            "PYBREW_SDK_PATH": "",
            "PYBREW_SDK_VERSION": "",
            "PYBREW_XCRUN": "",
            "SDKROOT": "",
        },
    )
    assert result.success is True


@requires_ruby
def test_macos_sdk_returns_nil_without_versioned_sdk(tmp_path: Path) -> None:
    versionless = tmp_path / "VersionlessSDK"
    versionless.mkdir()
    empty = tmp_path / "empty-sdks"
    (empty / "MacOSX.sdk").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_executable(
        bin_dir,
        "xcrun",
        "case \"$1\" in\n"
        f"  --show-sdk-path) printf '%s' \"{versionless}\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac",
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  assert_nil MacOS.sdk
  assert_nil MacOS.sdk_path
  assert_nil MacOS.sdk_version
  assert_nil MacOS.sdk("14.5")
  assert_equal MacOS.version.to_s, MacOS.latest_sdk_version.to_s
end
""",
    )
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYBREW_SDK_DIRS": str(empty),
            "PYBREW_MACOS_VERSION": "14.5.1",
            "PYBREW_SDK_PATH": "",
            "PYBREW_SDK_VERSION": "",
            "PYBREW_XCRUN": "",
            "SDKROOT": "",
        },
    )
    assert result.success is True


@requires_ruby
def test_macos_sdk_honours_sdkroot(tmp_path: Path) -> None:
    sdk = tmp_path / "CustomSDK"
    sdk.mkdir()
    (sdk / "SDKSettings.json").write_text('{"Version": "13.1"}', encoding="utf-8")
    empty = tmp_path / "empty-sdks"
    empty.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_executable(bin_dir, "xcrun", "exit 1")
    formula, config = _installed_formula(
        tmp_path,
        """do
  sdk = MacOS.sdk
  assert_kind_of OS::Mac::SDK, sdk
  assert_equal "13.1", sdk.version.to_s
  assert_equal "CustomSDK", sdk.path.basename.to_s
  assert_equal :sdkroot, sdk.source
  assert_equal sdk.path.to_s, MacOS.sdk_path.to_s
  assert_equal "13.1", MacOS.latest_sdk_version.to_s
end
""",
    )
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYBREW_SDK_DIRS": str(empty),
            "SDKROOT": str(sdk),
            "PYBREW_MACOS_VERSION": "14.5.1",
            "PYBREW_SDK_PATH": "",
            "PYBREW_SDK_VERSION": "",
            "PYBREW_XCRUN": "",
        },
    )
    assert result.success is True


@requires_ruby
def test_dynamic_formula_lookup_resolves_local_uninstalled_formula(
    tmp_path: Path,
) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "dynamic-dep", "  test do\n    assert true\n  end")
    source = """do
  name = "dynamic-dep"
  dep = Formula[name]
  refute dep.installed?
  assert_equal "dynamic-dep", dep.name
end
"""
    formula, config = _installed_formula(tmp_path, source, taps=[tap])
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_dynamic_formula_lookup_across_larger_local_tap(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    for index in range(60):
        _write_tap_formula(tap, f"bulk-{index:02d}", None)
    source = """do
  name = "bulk-59"
  dep = Formula[name]
  refute dep.installed?
  assert_equal "bulk-59", dep.name
end
"""
    formula, config = _installed_formula(tmp_path, source, taps=[tap])
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_dynamic_formula_lookup_across_three_hundred_formulae(
    tmp_path: Path,
) -> None:
    tap = tmp_path / "sample-tap"
    for index in range(300):
        _write_tap_formula(tap, f"bulk3-{index:03d}", None)
    source = """do
  name = "bulk3-299"
  dep = Formula[name]
  refute dep.installed?
  assert_equal "bulk3-299", dep.name
end
"""
    formula, config = _installed_formula(tmp_path, source, taps=[tap])
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_dynamic_formula_lookup_across_two_thousand_formulae(
    tmp_path: Path,
) -> None:
    tap = tmp_path / "sample-tap"
    for index in range(2500):
        _write_tap_formula(tap, f"bulk4-{index:04d}", None)
    source = """do
  name = "bulk4-2499"
  dep = Formula[name]
  refute dep.installed?
  assert_equal "bulk4-2499", dep.name
end
"""
    formula, config = _installed_formula(tmp_path, source, taps=[tap])
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


# ---------------------------------------------------------------------------
# T4: stronger Version/PkgVersion DSL behavior
# ---------------------------------------------------------------------------


@requires_ruby
def test_version_letter_segments_and_pkg_ordering(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        """do
  beta = Version.new("1.2.3-beta")
  upper = Version.new("1.2.3-BETA")
  release = Version.new("1.2.3")
  assert_equal [1, 2, 3, "beta"], beta.segments
  assert_equal [1, 2, 3, "beta"], upper.segments
  assert_equal 3, beta.patch
  assert_equal 0, beta <=> upper
  assert beta < release
  assert release > beta
  assert Version.new("1.2.3a") > release
  assert_equal "1.2.3-beta", beta.to_s
  pkg = PkgVersion.parse("1.2.3_2")
  assert_equal "1.2.3_2", pkg.to_s
  assert_equal "1.2.3_2", pkg.to_str
  assert_equal 2, pkg.revision
  assert_equal "1.2.3", pkg.version.to_s
  assert_equal [1, 2, 3], pkg.segments
  assert_equal 1, pkg.major
  assert_equal 2, pkg.minor
  assert_equal 3, pkg.patch
  assert pkg > PkgVersion.parse("1.2.3_1")
  assert pkg > "1.2.3_1"
  assert pkg < "1.2.4"
  assert_equal "1.2.3", PkgVersion.parse("1.2.3").to_s
end
""",
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


# ---------------------------------------------------------------------------
# T4: dependency isolation for HOME/PATH and global constants
# ---------------------------------------------------------------------------


@requires_ruby
def test_dependency_test_isolates_home_path_and_globals(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "sample.txt").write_text("fixture-data", encoding="utf-8")
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap,
        "dep-lib",
        '''  test do
    assert_equal "dep-lib", name
    assert_equal testpath.to_s, ENV["HOME"]
    assert_equal testpath.to_s, Dir.home.to_s
    assert_equal testpath.to_s, ENV["TMPDIR"]
    assert_equal testpath.to_s, ENV["TEMP"]
    assert_equal testpath.to_s, ENV["TMP"]
    assert_equal testpath.to_s, Dir.pwd
    assert_equal prefix.to_s, ENV["PYBREW_PREFIX"]
    refute_equal ENV["PARENT_HOME"], ENV["HOME"]
    assert_nil ENV["PYBREW_PARENT_HOME"]
    assert_nil ENV["PYBREW_PARENT_TESTPATH"]
    path_entries = ENV["PATH"].split(File::PATH_SEPARATOR)
    assert_equal (prefix/"bin").to_s, path_entries.first
    refute_includes path_entries, File.join(ENV["PARENT_PREFIX"], "bin")
    assert_equal "dep-ok", shell_output("dep-lib").strip
    assert_empty `command -v parent-only-tool 2>/dev/null`.strip
    assert_equal ENV["HOMEBREW_PREFIX"], HOMEBREW_PREFIX.to_s
    assert_equal ENV["HOMEBREW_CELLAR"], HOMEBREW_CELLAR.to_s
    assert_nil ENV["PYBREW_PARENT_PREFIX"]
    assert_nil ENV["PYBREW_PARENT_SECRET"]
    assert_equal "13.5", ENV["PYBREW_MACOS_VERSION"]
    assert_equal "13", MacOS.version.to_s
    assert_equal "fixture-data", test_fixtures("sample.txt").read
    ENV["HOMEBREW_PREFIX"] = "/mutated/dep-env-prefix"
    ENV["HOMEBREW_CELLAR"] = "/mutated/dep-env-cellar"
    Object.send(:remove_const, :HOMEBREW_PREFIX)
    Object.const_set(:HOMEBREW_PREFIX, "/mutated/dep-const-prefix")
    Object.send(:remove_const, :HOMEBREW_CELLAR)
    Object.const_set(:HOMEBREW_CELLAR, "/mutated/dep-const-cellar")
  end''',
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  parent_testpath = testpath.to_s
  parent_home = ENV["HOME"].to_s
  parent_path = ENV["PATH"].to_s
  parent_prefix = ENV["PYBREW_PREFIX"].to_s
  parent_tmpdir = ENV["TMPDIR"].to_s
  (testpath/"parent-marker").write "parent"
  (bin/"parent-only-tool").write "#!/bin/sh\\necho parent-tool\\n"
  File.chmod(0755, (bin/"parent-only-tool").to_s)
  global_prefix = HOMEBREW_PREFIX.to_s
  global_cellar = HOMEBREW_CELLAR.to_s
  ENV["PARENT_TESTPATH"] = parent_testpath
  ENV["PARENT_HOME"] = parent_home
  ENV["PARENT_PREFIX"] = parent_prefix
  ENV["PYBREW_PARENT_TESTPATH"] = parent_testpath
  ENV["PYBREW_PARENT_HOME"] = parent_home
  ENV["PYBREW_PARENT_PREFIX"] = parent_prefix
  ENV["PYBREW_PARENT_SECRET"] = "hidden"
  ENV["HOMEBREW_PREFIX"] = "parent-junk-prefix"
  ENV["HOMEBREW_CELLAR"] = "parent-junk-cellar"
  Formula["dep-lib"].test
  assert_equal parent_testpath, Dir.pwd
  assert_equal parent_home, ENV["HOME"].to_s
  assert_equal parent_path, ENV["PATH"].to_s
  assert_equal parent_tmpdir, ENV["TMPDIR"].to_s
  assert_equal parent_prefix, ENV["PYBREW_PREFIX"].to_s
  assert_path_exists testpath/"parent-marker"
  assert_equal "parent-tool", shell_output("#{bin}/parent-only-tool").strip
  assert_equal "parent-junk-prefix", ENV["HOMEBREW_PREFIX"]
  assert_equal "parent-junk-cellar", ENV["HOMEBREW_CELLAR"]
  assert_equal "hidden", ENV["PYBREW_PARENT_SECRET"]
  assert_equal global_prefix, HOMEBREW_PREFIX.to_s
  assert_equal global_cellar, HOMEBREW_CELLAR.to_s
end
""",
        taps=[tap],
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib", output="dep-ok")
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={
            "PYBREW_MACOS_VERSION": "13.5",
            "PYBREW_TEST_FIXTURES": str(fixtures),
        },
    )
    assert result.success is True


# ---------------------------------------------------------------------------
# T4: alias-only local tap lookup (Aliases metadata)
# ---------------------------------------------------------------------------


def _write_tap_alias(tap: Path, alias: str, formula_name: str) -> Path:
    aliases = tap / "Aliases"
    aliases.mkdir(parents=True, exist_ok=True)
    link = aliases / alias
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(f"../Formula/{formula_name}.rb")
    return link


@requires_ruby
def test_alias_only_tap_lookup_resolves_from_metadata(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "tool-x", "  test do\n    assert true\n  end")
    _write_tap_alias(tap, "tool-alias", "tool-x")
    formula, config = _installed_formula(
        tmp_path,
        """do
  ref = Formula["tool-alias"]
  assert_equal "tool-x", ref.name
  assert_equal "sample-tap/tool-x", ref.full_name
  assert_equal "1.0", ref.version.to_s
  assert_equal Formula["tool-x"], ref
  assert_equal "tool-x", Formula["sample-tap/tool-alias"].name
end
""",
        taps=[tap],
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_dynamic_tap_alias_lookup_uses_index_metadata(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "tool-x", None)
    _write_tap_alias(tap, "tool-alias", "tool-x")
    formula, config = _installed_formula(
        tmp_path,
        """do
  ref = Formula[["tool", "alias"].join("-")]
  assert_equal "tool-x", ref.name
  refute ref.installed?
end
""",
        taps=[tap],
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_unknown_alias_fails_clearly(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "tool-x", None)
    _write_tap_alias(tap, "tool-alias", "tool-x")
    formula, config = _installed_formula(
        tmp_path, 'do\n  Formula["no-such-alias"].name\nend', taps=[tap]
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "no-such-alias" in excinfo.value.stderr
    assert "not available" in excinfo.value.stderr


# ---------------------------------------------------------------------------
# T4: Minitest fidelity (skip/pattern/counter)
# ---------------------------------------------------------------------------


@requires_ruby
def test_skip_is_not_a_standard_error(tmp_path: Path) -> None:
    source = """do
  begin
    skip "not caught"
  rescue StandardError
    flunk "skip must not be a StandardError"
  end
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert result.success is True
    assert "Formula test skipped: not caught" in result.stdout


@requires_ruby
def test_skip_default_message_is_minitest_compatible(tmp_path: Path) -> None:
    formula, config = _installed_formula(tmp_path, "do\n  skip\nend")
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert result.success is True
    assert "Formula test skipped: Skipped, no message given" in result.stdout


@requires_ruby
def test_pattern_assertions_simulate_no_matching_pattern_error(tmp_path: Path) -> None:
    source = """do
  assert_pattern { 1 + 1 }
  refute_pattern { raise NoMatchingPatternError, "length mismatch" }
  mismatch = begin
    assert_pattern { raise NoMatchingPatternError, "length mismatch" }
  rescue StandardError => error
    error
  end
  assert_match(/length mismatch/, mismatch.message)
  unmatched = begin
    refute_pattern { 1 + 1 }
  rescue StandardError => error
    error
  end
  assert_match(/NoMatchingPatternError expected/, unmatched.message)
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_assertion_counter_counts_pass_and_flunk(tmp_path: Path) -> None:
    source = """do
  assert_equal 0, assertions
  pass
  assert_equal 2, assertions
  begin
    flunk "counted failure"
  rescue FormulaTestFailure
  end
  assert_equal 4, assertions
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_skip_does_not_increment_assertion_counter(tmp_path: Path) -> None:
    source = """do
  before = assertions
  begin
    skip "uncounted"
  rescue FormulaTestSkip
    raise "skip counted as an assertion" unless assertions == before
    raise
  end
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert result.success is True
    assert "Formula test skipped: uncounted" in result.stdout


@requires_ruby
def test_dynamic_formula_lookup_is_lightweight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tap = tmp_path / "sample-tap"
    for index in range(5):
        _write_tap_formula(tap, f"tool-{index}", None)
    loaded: list[str] = []
    original = formula_test_module._load_tap_formula

    def counting_load(path: Path, tap_name: str) -> Formula:
        loaded.append(path.stem)
        return original(path, tap_name)

    monkeypatch.setattr(formula_test_module, "_load_tap_formula", counting_load)
    formula, config = _installed_formula(
        tmp_path,
        """do
  ref = Formula[["tool", "3"].join("-")]
  assert_equal "tool-3", ref.name
  refute ref.installed?
end
""",
        taps=[tap],
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True
    assert loaded == []


@requires_ruby
def test_skip_inside_assert_raises_propagates(tmp_path: Path) -> None:
    source = """do
  before = assertions
  begin
    assert_raises(StandardError) { skip "not matched" }
  rescue FormulaTestSkip
    raise "skip counted as an assertion" unless assertions == before
    raise
  end
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert result.success is True
    assert "Formula test skipped: not matched" in result.stdout


# ---------------------------------------------------------------------------
# T9: source-only aliases, compound counters, version token parity
# ---------------------------------------------------------------------------


@requires_ruby
def test_source_only_alias_resolves_without_aliases_directory(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap,
        "tool-source",
        "  test do\n    assert true\n  end",
        aliases=["source-alias"],
    )
    assert not (tap / "Aliases").exists()
    formula, config = _installed_formula(
        tmp_path,
        """do
  ref = Formula["source-alias"]
  assert_equal "tool-source", ref.name
  assert_equal "1.0", ref.version.to_s
  assert_equal Formula["tool-source"], ref
end
""",
        taps=[tap],
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_source_alias_scan_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "a-first", None)
    _write_tap_formula(tap, "b-last", None, aliases=["late-alias"])
    monkeypatch.setattr(formula_test_module, "MAX_SOURCE_ALIAS_SCAN", 1)
    formula, config = _installed_formula(
        tmp_path, 'do\n  Formula["late-alias"].name\nend', taps=[tap]
    )
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(formula, config, ruby=_REAL_RUBY)

    assert "late-alias" in excinfo.value.stderr
    assert "not available" in excinfo.value.stderr


@requires_ruby
def test_source_alias_scan_respects_budget(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "a-first", None)
    _write_tap_formula(tap, "b-last", None, aliases=["late-alias"])
    formula, config = _installed_formula(
        tmp_path, 'do\n  Formula["late-alias"].name\nend', taps=[tap]
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_compound_assertion_counter_matches_minitest(tmp_path: Path) -> None:
    source = """do
  assert_equal 0, assertions
  assert_includes [1], 1
  assert_equal 3, assertions
  assert_match(/x/, "x")
  assert_equal 6, assertions
  assert_output("out\\n") { print "out\\n" }
  assert_equal 8, assertions
  assert_silent { }
  assert_equal 11, assertions
  assert_raises(RuntimeError) { raise "boom" }
  assert_equal 13, assertions
  begin
    assert_raises(RuntimeError) { 1 }
  rescue FormulaTestFailure
  end
  assert_equal 15, assertions
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_assert_match_escapes_string_matchers(tmp_path: Path) -> None:
    source = """do
  assert_match("1.0", "1.0")
  refute_match("1.0", "1x0")
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True



@requires_ruby
def test_assert_output_regex_counter_matches_minitest(tmp_path: Path) -> None:
    source = """do
  assert_equal 0, assertions
  assert_output(/out/) { print "out" }
  assert_equal 3, assertions
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


# ---------------------------------------------------------------------------
# T16: Python/Ruby version token alignment and Minitest edge counters
# ---------------------------------------------------------------------------


@requires_ruby
def test_dsl_version_matches_python_token_semantics(tmp_path: Path) -> None:
    source = """do
  alpha = Version.new("1.0-alpha")
  beta = Version.new("1.0-beta")
  pre = Version.new("1.0-pre")
  rc = Version.new("1.0-rc1")
  patch = Version.new("1.0-p1")
  assert alpha.pre_release?
  assert_equal ["alpha"], alpha.pre_release_tokens
  refute Version.new("1.0").pre_release?
  assert_equal [1, 0, "alpha"], alpha.segments
  assert_equal [1, 0, "alpha"], alpha.segments
  assert alpha.tokens[2].is_a?(Version::AlphaToken)
  assert_equal "alpha", alpha.tokens[2].value
  assert_equal 0, alpha.tokens[2].revision
  assert Version::NumericToken.new(1) > Version::AlphaToken.new("alpha")
  assert Version::AlphaToken.new("alpha") < Version::BetaToken.new("beta")
  assert Version::BetaToken.new("beta") < Version::PreToken.new("pre")
  assert Version::PreToken.new("pre") < Version::RcToken.new("rc1")
  assert Version::RcToken.new("rc1") < Version::PToken.new("p1")
  assert Version::PostToken.new("post1") < Version::PToken.new("p1")
  assert Version::PostToken.new("post1") < Version::LetterToken.new("foo")
  assert Version::PToken.new("p1") > Version::LetterToken.new("foo")
  assert Version::PToken.new("p1") < Version::LetterToken.new("zoo")
  assert Version::PreToken.new("pre") > Version::LetterToken.new("foo")
  assert Version::AlphaToken.new("alpha") < Version::LetterToken.new("foo")
  assert Version::RcToken.new("rc1") > Version::LetterToken.new("foo")
  assert Version.new("1.0-preview") == Version.new("1.0-pre")
  assert_equal ["numeric", "numeric", "pre"], Version.new("1.0-preview").token_classes
  assert Version::AlphaToken.new("alpha", 0) == Version::AlphaToken.new("alpha")
  assert_equal Version::AlphaToken.new("alpha", 0).hash,
               Version::AlphaToken.new("alpha").hash
  assert Version::PToken.new("p", 0) == Version::LetterToken.new("p")
  assert_equal Version::PToken.new("p", 0).hash, Version::LetterToken.new("p").hash
  assert Version::NullToken.new == Version::NumericToken.new(0)
  assert Version::NullToken.new < Version::NumericToken.new(1)
  assert Version::NullToken.new > Version::AlphaToken.new("alpha")
  assert Version::NullToken.new < Version::LetterToken.new("foo")
  refute_equal PkgVersion.parse("1.0_2").hash, PkgVersion.parse("1.0_0").hash
  assert_equal 1, alpha.major
  assert_equal 0, alpha.minor
  assert_equal 0, alpha.patch
  assert alpha < beta
  assert beta < pre
  assert pre < rc
  assert rc < patch
  assert Version.new("1.0") < patch
  assert Version.new("1.0") < Version.new("1.0-foo")
  assert Version.new("1.0.0") == Version.new("1.0")
  assert_equal 0, alpha.revision
  assert_equal 2, PkgVersion.parse("1.0-alpha_2").revision
  assert PkgVersion.parse("1.0-alpha_2").pre_release?
  assert Version.new("1.0-beta2") < Version.new("1.0-beta10")
  assert Version.new("1.0-post1") < Version.new("1.0-p1")
  assert_equal 0, Version.new("1.0") <=> PkgVersion.parse("1.0_0")
  refute_equal Version.new("1.0"), PkgVersion.parse("1.0_2")
  assert_equal ["numeric", "numeric", "alpha"], Version.new("1.0-alpha2").token_classes
  assert_equal ["numeric", "numeric", "p"], Version.new("1.0-p1").token_classes
  assert_equal ["numeric", "numeric", "letter"], Version.new("1.0-foo").token_classes
  assert_equal ["numeric", "numeric", "pre"], Version.new("1.0-preview").token_classes
  assert_equal ["numeric", "numeric", "alpha"],
               PkgVersion.parse("1.0-alpha_2").token_classes
  assert_equal 0, Version.new("1.0").homebrew_compare("1.0.0")
  assert Version.new("1.0").homebrew_compare("1.0.1") < 0
  assert Version.new("1.0").homebrew_compare("1.0-alpha") > 0
  assert Version.new("1.0").homebrew_compare("1.0-p1") < 0
  assert Version.new("1.0").homebrew_compare("1.0-foo") < 0
  assert Version.new("1.0").homebrew_compare(PkgVersion.parse("1.0_2")) < 0
  assert PkgVersion.parse("1.0_2").homebrew_compare("1.0") > 0
  assert_equal 0, PkgVersion.parse("1.0_2").homebrew_compare("1.0_2")
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_minitest_compound_counter_edge_cases(tmp_path: Path) -> None:
    source = """do
  start = assertions
  assert_output(nil, nil) { puts "ignored" }
  assert_equal start, assertions
  before = assertions
  begin
    assert_includes nil, 1
  rescue FormulaTestFailure
  end
  assert_equal before + 1, assertions
  assert_pattern { 1 }
  refute_pattern { raise NoMatchingPatternError, "no match" }
  assert_equal before + 4, assertions
  begin
    assert_pattern { raise NoMatchingPatternError, "no match" }
  rescue FormulaTestFailure
  end
  assert_equal before + 6, assertions
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_dependency_constant_mutation_restored_after_failure(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap,
        "dep-lib",
        '''  test do
    ENV["HOMEBREW_PREFIX"] = "/mutated/failure-env"
    Object.send(:remove_const, :HOMEBREW_PREFIX)
    Object.const_set(:HOMEBREW_PREFIX, "/mutated/failure-const")
    raise "dependency failed after mutating globals"
  end''',
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  global_prefix = HOMEBREW_PREFIX.to_s
  original_env = ENV["HOMEBREW_PREFIX"]
  begin
    Formula["dep-lib"].test
  rescue StandardError
  end
  assert_equal global_prefix, HOMEBREW_PREFIX.to_s
  assert_equal original_env, ENV["HOMEBREW_PREFIX"]
  assert_equal "hello-test", name
end
""",
        taps=[tap],
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib", output="dep-ok")
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_dsl_homebrew_mode_env_null_token(tmp_path: Path) -> None:
    source = """do
  assert_equal 0, Version.new("1.0") <=> Version.new("1.0.0")
  assert Version.new("1.0") == Version.new("1.0.0")
  assert Version.new("1.0") == "1.0.0"
  assert_equal Version.new("1.0").hash, Version.new("1.0.0").hash
  refute_equal Version.new("1.0"), Version.new("1.0.1")
  assert Version.new("1.0") > Version.new("1.0-alpha")
  assert Version.new("1.0") < Version.new("1.0-p1")
  assert Version.new("1.0") < Version.new("1.0-foo")
  assert PkgVersion.parse("1.0_0") == Version.new("1.0.0")
  assert_equal PkgVersion.parse("1.0_0").hash, Version.new("1.0.0").hash
  refute_equal Version.new("1.0"), PkgVersion.parse("1.0_2")
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={"PYBREW_VERSION_HOMEBREW": "1"},
    )
    assert result.success is True


@requires_ruby
def test_dsl_homebrew_mode_class_flag(tmp_path: Path) -> None:
    source = """do
  begin
    assert Version.new("1.0") == Version.new("1.0.0")
    assert_equal Version.new("1.0").hash, Version.new("1.0.0").hash
    Version.legacy = true
    refute Version.new("1.0") == Version.new("1.0.0")
    Version.with_legacy(false) do
      assert Version.new("1.0") == Version.new("1.0.0")
    end
    Version.homebrew = false
    refute Version.new("1.0") == Version.new("1.0.0")
  ensure
    Version.legacy = false
    Version.homebrew = nil
  end
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={"PYBREW_VERSION_HOMEBREW": ""},
    )
    assert result.success is True


@requires_ruby
def test_dsl_legacy_mode_env_restores_extra_numeric_behavior(tmp_path: Path) -> None:
    source = """do
  assert Version.new("1.0.0") > Version.new("1.0")
  refute Version.new("1.0") == Version.new("1.0.0")
  assert Version.new("1.0-p1") < Version.new("1.0")
  assert Version.new("1.0-foo") < Version.new("1.0")
  assert Version.new("1.0") == Version.new("01.0")
  assert_equal Version.new("1.0").hash, Version.new("01.0").hash
  assert_equal PkgVersion.parse("1.0_0").hash, Version.new("1.0").hash
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={"PYBREW_VERSION_LEGACY": "1"},
    )
    assert result.success is True


@requires_ruby
def test_dsl_homebrew_bounded_table_and_hash_consistency(tmp_path: Path) -> None:
    source = """do
  versions = ["1.0", "1.0.0", "1.0-alpha", "1.0-p1", "1.0-post1", "1.0-foo", "1.0.1"]
  expected = [
    [0, 0, 1, -1, -1, -1, -1],
    [0, 0, 0, 0, 0, 0, -1],
    [-1, -1, 0, -1, -1, -1, -1],
    [1, 1, 1, 0, 1, 1, -1],
    [1, 1, 1, -1, 0, -1, -1],
    [1, 1, 1, -1, 1, 0, -1],
    [1, 1, 1, 1, 1, 1, 0],
  ]
  objects = versions.map { |value| Version.new(value) }
  versions.each_index do |left_index|
    versions.each_index do |right_index|
      comparison = expected[left_index][right_index]
      left = objects[left_index]
      right = objects[right_index]
      assert_equal comparison, left <=> right
      assert_equal(comparison.zero?, left == right)
      assert_equal(comparison.negative?, left < right)
      assert_equal(comparison.positive?, left > right)
      assert_equal left.hash, right.hash if comparison.zero?
    end
  end

  Version.with_legacy do
    assert Version.new("1.0.0") > Version.new("1.0")
    assert Version.new("1.0-p1") < Version.new("1.0")
    assert Version.new("1.0") == Version.new("01.0")
    assert_equal Version.new("1.0").hash, Version.new("01.0").hash
    assert_equal PkgVersion.parse("1.0_0").hash, Version.new("1.0").hash
  end
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={"PYBREW_VERSION_LEGACY": ""},
    )
    assert result.success is True


@requires_ruby
def test_dynamic_source_alias_lookup_without_prior_literal(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap,
        "tool-source",
        "  test do\n    assert true\n  end",
        aliases=["source-alias"],
    )
    assert not (tap / "Aliases").exists()
    formula, config = _installed_formula(
        tmp_path,
        """do
  ref = Formula[["source", "alias"].join("-")]
  assert_equal "tool-source", ref.name
  refute ref.installed?
  qualified = Formula["sample-tap/" + ["source", "alias"].join("-")]
  assert_equal "tool-source", qualified.name
end
""",
        taps=[tap],
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_dependency_subprocess_isolates_arbitrary_env_and_constants(
    tmp_path: Path,
) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap,
        "dep-lib",
        '''  test do
    ENV["PYBREW_DEP_ENV_LEAK"] = "leaked"
    Object.const_set(:PYBREW_DEP_CONST_LEAK, "leaked")
    assert_equal "leaked", ENV["PYBREW_DEP_ENV_LEAK"]
    assert_equal "leaked", PYBREW_DEP_CONST_LEAK
  end''',
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  ENV["PARENT_MARKER"] = "parent"
  Formula["dep-lib"].test
  assert_nil ENV["PYBREW_DEP_ENV_LEAK"]
  refute Object.const_defined?(:PYBREW_DEP_CONST_LEAK)
  assert_equal "parent", ENV["PARENT_MARKER"]
end
""",
        taps=[tap],
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib", output="dep-ok")
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_hardware_cpu_expanded_helpers(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        """do
  assert_equal :arm64, Hardware::CPU.arch
  assert_equal :arm, Hardware::CPU.type
  assert_equal :arm, Hardware::CPU.family
  assert_equal "Apple M-test", Hardware::CPU.model
  assert_equal 64, Hardware::CPU.bits
  assert Hardware::CPU.is_64_bit?
  assert_equal 8, Hardware::CPU.cores
  assert_equal 4, Hardware::CPU.physical_cores
  assert Hardware::CPU.neon?
  assert Hardware::CPU.sse3?
  assert Hardware::CPU.ssse3?
  assert Hardware::CPU.sse4?
  assert Hardware::CPU.sse4_1?
  assert Hardware::CPU.sse4_2?
  assert Hardware::CPU.avx?
  assert Hardware::CPU.avx2?
  assert Hardware::CPU.avx512?
  assert Hardware::CPU.feature?(:avx2)
end
""",
    )
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={
            "PYBREW_CPU_ARCH": "arm64",
            "PYBREW_CPU_MODEL": "Apple M-test",
            "PYBREW_CPU_BITS": "64",
            "PYBREW_CPU_CORES": "8",
            "PYBREW_CPU_PHYSICAL_CORES": "4",
            "PYBREW_CPU_FLAGS": "neon asimd sse3 pni ssse3 sse4_1 sse4_2 avx avx2 avx512f",
        },
    )
    assert result.success is True


@requires_ruby
def test_macos_xcode_clt_development_tools_helpers(tmp_path: Path) -> None:
    formula, config = _installed_formula(
        tmp_path,
        """do
  assert MacOS::CLT.installed?
  assert_equal "16.4.0", MacOS::CLT.version
  assert_equal "/tmp/fake-clang", MacOS::CLT.clang
  assert MacOS::Xcode.installed?
  assert_equal "16.4", MacOS::Xcode.version
  assert_equal "/Applications/Xcode.app", MacOS::Xcode.path
  assert_equal :clang, DevelopmentTools.default_compiler
  assert_equal :clang, DevelopmentTools.preferred_compiler
  assert_equal "16.0.0", DevelopmentTools.clang_version
  assert_equal "1600.0.26.6", DevelopmentTools.clang_build_version
  assert DevelopmentTools.installed?
  refute DevelopmentTools.needs_clt?
  assert DevelopmentTools.xcode_installed?
  assert_equal "/Applications/Xcode-beta.app/Contents/Developer",
               MacOS.active_developer_dir
  assert_equal "5.34", MacOS.preferred_perl_version
  assert_equal ["en", "de"], MacOS.languages
  assert_equal "en", MacOS.language
  assert_equal "26", MacOS::Version.from_symbol(:tahoe).to_s
  assert_equal "27", MacOS::Version.from_symbol(:golden_gate).to_s
  assert_nil MacOS::Version.from_symbol(:not_a_release)
end
""",
    )
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={
            "PYBREW_CLT_INSTALLED": "1",
            "PYBREW_CLT_VERSION": "16.4.0",
            "PYBREW_CLANG": "/tmp/fake-clang",
            "PYBREW_XCODE_INSTALLED": "1",
            "PYBREW_XCODE_VERSION": "16.4",
            "PYBREW_CLANG_VERSION": "16.0.0",
            "PYBREW_CLANG_BUILD_VERSION": "1600.0.26.6",
            "PYBREW_DEVELOPER_DIR": "/Applications/Xcode-beta.app/Contents/Developer",
            "PYBREW_PERL_VERSION": "5.34",
            "PYBREW_LANGUAGES": "en,de",
        },
    )
    assert result.success is True


@requires_ruby
def test_dsl_token_upstream_subclasses_and_factory(tmp_path: Path) -> None:
    source = """do
  assert Version::AlphaToken < Version::PreReleaseToken
  assert Version::PreReleaseToken < Version::CompositeToken
  assert Version::CompositeToken < Version::StringToken
  assert Version::StringToken < Version::Token
  assert Version::RCToken < Version::PreReleaseToken
  assert Version::PatchToken < Version::CompositeToken
  assert Version::PostToken < Version::CompositeToken
  assert Version::NumericToken < Version::Token
  assert Version::NullToken < Version::Token
  assert Version::LetterToken.equal?(Version::StringToken)
  assert Version::RcToken.equal?(Version::RCToken)
  assert Version::PToken.equal?(Version::PatchToken)
  assert Version::MissingToken.equal?(Version::NullToken)

  numeric = Version::Token.create("1")
  assert numeric.is_a?(Version::NumericToken)
  assert numeric.numeric?
  assert_equal 1, numeric.to_i
  assert_in_delta 1.0, numeric.to_f, 0.001

  alpha = Version::Token.create("alpha2")
  assert alpha.is_a?(Version::AlphaToken)
  assert alpha.is_a?(Version::CompositeToken)
  assert_equal 2, alpha.rev
  assert_equal 2, alpha.revision
  refute alpha.numeric?

  patch = Version::Token.create("p1")
  assert patch.is_a?(Version::PatchToken)
  assert_equal 1, patch.rev
  post = Version::Token.create("post1")
  assert post.is_a?(Version::PostToken)
  assert_equal 1, post.rev
  assert Version::Token.create("foo").is_a?(Version::StringToken)
  assert Version::Token.from(nil).null?
  assert Version::NULL_TOKEN.null?
  assert_equal 0, Version::NULL_TOKEN.to_i
  assert_equal "", Version::NULL_TOKEN.to_s
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_minitest_remaining_assertion_surface(tmp_path: Path) -> None:
    source = """do
  assert_operator 1, :positive?
  refute_operator 0, :positive?
  assert_send [[1, 2], :include?, 1]
  assert_throws(:done) { throw :done }
  error = assert_raises(RuntimeError, "custom context") { raise "boom" }
  assert_equal "boom", error.message
  assert_silent { }
  assert_predicate 1, :positive?
  refute_predicate 0, :positive?
  assert_kind_of Integer, 1
  assert_instance_of Integer, 1
  refute_respond_to Object.new, :no_such_method
  assert_nil nil
  refute_nil 0
  assert_in_delta 1.0, 1.0, 0.01
  refute_in_delta 1.0, 2.0, 0.01
  assert_in_epsilon 1.0, 1.0, 0.01
  object = Object.new
  assert_same object, object
  refute_same Object.new, Object.new
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_dependency_source_alias_lookup_is_resolved(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "dep-lib", None, aliases=["dep-alias"])
    formula, config = _installed_formula(
        tmp_path,
        """do
  dep = Formula["dep-alias"]
  assert_equal "dep-lib", dep.name
  assert dep.installed?
end
""",
        taps=[tap],
    )
    formula.dependencies = [Dependency(name="dep-alias")]
    _install_keg(config, "dep-lib", output="dep-ok")
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_large_tap_dynamic_source_alias_without_literal(tmp_path: Path) -> None:
    tap = tmp_path / "large-tap"
    for index in range(501):
        _write_tap_formula(tap, f"tool-{index:03d}", None)
    _write_tap_formula(tap, "zz-alias-target", None, aliases=["late-dynamic-alias"])
    formula, config = _installed_formula(
        tmp_path,
        """do
  ref = Formula[["late", "dynamic", "alias"].join("-")]
  assert_equal "zz-alias-target", ref.name
end
""",
        taps=[tap],
    )
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_dependency_fork_timeout_kills_child(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(tap, "dep-lib", "  test do\n    sleep 5\n  end")
    formula, config = _installed_formula(
        tmp_path, 'do\n  Formula["dep-lib"].test\nend', taps=[tap]
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib", output="dep-ok")
    started = time.monotonic()
    with pytest.raises(FormulaTestError) as excinfo:
        run_formula_test(
            formula,
            config,
            ruby=_REAL_RUBY,
            env={"PYBREW_DEPENDENCY_TIMEOUT": "0.3"},
        )
    elapsed = time.monotonic() - started
    assert "timed out" in excinfo.value.stderr
    assert elapsed < 3


@requires_ruby
def test_dependency_non_fork_fallback_runs_in_process(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap,
        "dep-lib",
        '''  test do
    File.write(File.join(ENV["PARENT_TESTPATH"], "fallback-marker"), "ok")
  end''',
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  ENV["PARENT_TESTPATH"] = testpath.to_s
  Formula["dep-lib"].test
  assert_path_exists testpath/"fallback-marker"
end
""",
        taps=[tap],
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib", output="dep-ok")
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={"PYBREW_TEST_ISOLATE_DEPENDENCIES": "0"},
    )
    assert result.success is True


@requires_ruby
def test_minitest_reflection_and_pattern_edge_forms(tmp_path: Path) -> None:
    source = """do
  assert_respond_to 1, :positive?
  refute_respond_to 1, :no_such_assertion_method
  assert_kind_of Comparable, 1
  assert_kind_of Numeric, 1
  assert_instance_of Integer, 1
  refute_instance_of Numeric, 1
  refute_kind_of String, 1
  assert_operator 1, :>, 0
  assert_operator 1, :positive?
  refute_operator 0, :positive?
  assert_send [[1, 2], :include?, 1]

  no_block = begin
    assert_pattern
  rescue StandardError => error
    error
  end
  assert_match(/requires a block/, no_block.message)
  no_block_refute = begin
    refute_pattern
  rescue StandardError => error
    error
  end
  assert_match(/requires a block/, no_block_refute.message)

  other_error = begin
    assert_pattern { raise RuntimeError, "not a pattern error" }
  rescue RuntimeError => error
    error
  end
  assert_equal "not a pattern error", other_error.message

  other_refute_error = begin
    refute_pattern { raise RuntimeError, "not a pattern error" }
  rescue RuntimeError => error
    error
  end
  assert_equal "not a pattern error", other_refute_error.message
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(formula, config, ruby=_REAL_RUBY)
    assert result.success is True


@requires_ruby
def test_macos_version_and_hardware_edge_behavior(tmp_path: Path) -> None:
    source = """do
  assert_equal :x86_64, Hardware::CPU.arch
  assert_equal :intel, Hardware::CPU.type
  assert_equal :intel, Hardware::CPU.family
  assert_equal "Intel Test", Hardware::CPU.model
  assert_equal 32, Hardware::CPU.bits
  refute Hardware::CPU.is_64_bit?
  assert Hardware::CPU.sse3?
  assert Hardware::CPU.ssse3?
  assert Hardware::CPU.sse4?
  assert Hardware::CPU.sse4_1?
  assert Hardware::CPU.sse4_2?
  assert Hardware::CPU.avx?
  assert Hardware::CPU.avx2?
  refute Hardware::CPU.avx512?
  refute Hardware::CPU.neon?
  assert_equal 2, Hardware::CPU.cores
  assert_equal 2, Hardware::CPU.physical_cores

  sequoia = MacOS::Version.new("15.7.1")
  assert_equal "15.7.1", sequoia.to_s
  assert_equal "15", sequoia.strip_patch.to_s
  assert_equal "Sequoia", sequoia.release_name
  assert_equal "Sequoia", sequoia.pretty_name
  assert_equal :sequoia, sequoia.to_sym
  catalina = MacOS::Version.new("10.15.7")
  assert_equal "10.15", catalina.strip_patch.to_s
  assert_equal "Catalina", catalina.release_name
  unknown = MacOS::Version.new("99.1")
  assert_nil unknown.release_name
  assert_equal "Dunno", unknown.pretty_name
  assert_equal :dunno, unknown.to_sym
  assert_equal "99", unknown.release_version
  assert_nil(MacOS::Version.new("15") <=> :not_a_release)
  assert(OS.mac? || OS.linux?)
  refute(OS.mac? && OS.linux?)
  refute_empty OS.kernel_version
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={
            "PYBREW_CPU_ARCH": "x86_64",
            "PYBREW_CPU_MODEL": "Intel Test",
            "PYBREW_CPU_BITS": "32",
            "PYBREW_CPU_CORES": "2",
            "PYBREW_CPU_PHYSICAL_CORES": "2",
            "PYBREW_CPU_FLAGS": "sse3 pni ssse3 sse4_1 sse4_2 avx avx2",
        },
    )
    assert result.success is True


@requires_ruby
def test_xcode_clt_not_installed_edges(tmp_path: Path) -> None:
    source = """do
  refute MacOS::CLT.installed?
  refute MacOS::Xcode.installed?
  assert_nil MacOS::CLT.version
  assert_nil MacOS::Xcode.version
  refute DevelopmentTools.xcode_installed?
  assert DevelopmentTools.needs_clt?
  assert_equal "/tmp/fallback-clang", DevelopmentTools.clang
end
"""
    formula, config = _installed_formula(tmp_path, source)
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={
            "PYBREW_CLT_INSTALLED": "0",
            "PYBREW_XCODE_INSTALLED": "0",
            "PYBREW_CLANG": "/tmp/fallback-clang",
        },
    )
    assert result.success is True


@requires_ruby
def test_dependency_fork_timeout_kills_spawned_descendants(tmp_path: Path) -> None:
    tap = tmp_path / "sample-tap"
    _write_tap_formula(
        tap,
        "dep-lib",
        '''  test do
    Process.spawn("sh", "-c", "sleep 1; touch #{ENV["ORPHAN_MARKER"]}")
    sleep 5
  end''',
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  marker = testpath/"orphan-marker"
  ENV["ORPHAN_MARKER"] = marker.to_s
  begin
    Formula["dep-lib"].test
  rescue FormulaTestFailure => error
    assert_match(/timed out/, error.message)
  end
  sleep 1.5
  refute_path_exists marker
end
""",
        taps=[tap],
    )
    formula.dependencies = [Dependency(name="dep-lib")]
    _install_keg(config, "dep-lib", output="dep-ok")
    result = run_formula_test(
        formula,
        config,
        ruby=_REAL_RUBY,
        env={"PYBREW_DEPENDENCY_TIMEOUT": "0.3"},
    )
    assert result.success is True
