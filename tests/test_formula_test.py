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
  rescue StandardError
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
  end''',
    )
    formula, config = _installed_formula(
        tmp_path,
        """do
  Formula["dep-lib"].test
  assert Formula["dep-lib"].test_defined?
  assert_equal "hello-test", Formula["hello-test"].name
  assert_path_exists testpath/"dep-marker"
  assert_equal "marker", (testpath/"dep-marker").read
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
        tmp_path, 'do\n  Formula["dep-lib"].test\nend', taps=[tap]
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
        tmp_path, 'do\n  Formula["dep-lib"].test\nend', taps=[tap]
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
  assert_path_exists testpath/"tool-y-marker"
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
