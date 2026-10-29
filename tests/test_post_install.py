"""Tests for formula ``post_install`` execution.

Real-Ruby tests use the system interpreter when it exists and are skipped
otherwise; everything else drives a fake Ruby shell script so the suite never
depends on executing arbitrary Ruby from a fixture.  The installer end-to-end
tests use the local bottle tap from ``tests/conftest.py`` (no network).
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from pybrew.models import Formula
from pybrew.paths import Config
from pybrew.post_install import (
    PostInstallError,
    PostInstallRunner,
    has_post_install,
    run_post_install,
)
from pybrew.ruby_parser import parse_formula_source

REAL_RUBY = "/usr/bin/ruby"

requires_ruby = pytest.mark.skipif(
    not (os.path.isfile(REAL_RUBY) and os.access(REAL_RUBY, os.X_OK)),
    reason=f"system Ruby not available at {REAL_RUBY}",
)


# -- helpers -------------------------------------------------------------
def make_config(tmp_path: Path) -> Config:
    return Config(prefix=tmp_path / "prefix", cache=tmp_path / "cache")


def make_keg(config: Config, name: str = "posty", version: str = "1.0") -> Path:
    keg = config.keg_path(name, version)
    (keg / "bin").mkdir(parents=True, exist_ok=True)
    return keg


def make_formula(
    source: str,
    name: str = "posty",
    version: str = "1.0",
) -> Formula:
    formula = Formula(name=name, version=version)
    formula.post_install_source = source
    return formula


def make_fake_ruby(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a fake Ruby that records argv/cwd/env and copies its inputs."""

    ruby = tmp_path / "fake-ruby"
    record = tmp_path / "fake-ruby-record.txt"
    copies = tmp_path / "fake-ruby-inputs"
    copies.mkdir()
    ruby.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            record="${PYBREW_FAKE_RECORD:-}"
            if [ -n "$record" ]; then
              {
                printf 'cwd=%s\\n' "$(pwd)"
                printf 'argc=%s\\n' "$#"
                i=0
                for arg in "$@"; do
                  printf 'arg%s=%s\\n' "$i" "$arg"
                  i=$((i + 1))
                done
                printf 'HOMEBREW_PREFIX=%s\\n' "${HOMEBREW_PREFIX:-}"
                printf 'HOMEBREW_CELLAR=%s\\n' "${HOMEBREW_CELLAR:-}"
                printf 'HOMEBREW_REPOSITORY=%s\\n' "${HOMEBREW_REPOSITORY:-}"
                printf 'PYBREW_FORMULA_NAME=%s\\n' "${PYBREW_FORMULA_NAME:-}"
                printf 'PYBREW_FORMULA_VERSION=%s\\n' "${PYBREW_FORMULA_VERSION:-}"
              } > "$record"
            fi
            copies="${PYBREW_FAKE_COPY_DIR:-}"
            if [ -n "$copies" ]; then
              cp "$1" "$copies/runner.rb" 2>/dev/null || true
              cp "$2" "$copies/config.json" 2>/dev/null || true
              cp "$3" "$copies/body.rb" 2>/dev/null || true
            fi
            printf 'fake stdout\\n'
            if [ -n "${PYBREW_FAKE_STDERR:-}" ]; then
              printf '%s\\n' "$PYBREW_FAKE_STDERR" >&2
            fi
            exit "${PYBREW_FAKE_EXIT:-0}"
            """
        ),
        encoding="utf-8",
    )
    ruby.chmod(0o755)
    return ruby, record, copies


def read_record(record: Path) -> dict:
    values: dict = {}
    args: list[tuple[int, str]] = []
    for line in record.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.startswith("arg") and key[3:].isdigit():
            args.append((int(key[3:]), value))
        else:
            values[key] = value
    values["args"] = [value for _, value in sorted(args)]
    return values


def add_post_install(formula_path: Path, body: str) -> None:
    """Append a canonical ``def post_install ... end`` to a fixture formula."""

    text = formula_path.read_text(encoding="utf-8")
    stripped = text.rstrip()
    assert stripped.endswith("end"), text
    block_lines = ["  def post_install"]
    block_lines.extend(f"    {line}" for line in textwrap.dedent(body).strip().splitlines())
    block_lines.append("  end")
    formula_path.write_text(
        stripped[: -len("end")].rstrip() + "\n" + "\n".join(block_lines) + "\nend\n",
        encoding="utf-8",
    )


def read_receipt(keg: Path) -> dict:
    return json.loads((keg / "INSTALL_RECEIPT.json").read_text(encoding="utf-8"))


# -- unit tests: no-op / parsing -----------------------------------------
def test_formula_without_post_install_is_a_noop(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    formula = Formula(name="posty", version="1.0")

    assert has_post_install(formula) is False
    # A missing Ruby must not matter when there is nothing to execute.
    result = run_post_install(formula, config, ruby=str(tmp_path / "missing-ruby"))
    assert result.ran is False
    assert result.command == []


def test_whitespace_only_post_install_is_a_noop(tmp_path: Path) -> None:
    formula = make_formula("  \n\t\n")
    assert has_post_install(formula) is False


def test_parser_captures_canonical_def_post_install() -> None:
    source = textwrap.dedent(
        """\
        class Posty < Formula
          desc "post install fixture"
          homepage "https://example.com/posty"
          url "https://example.com/posty-1.0.tar.gz"
          sha256 "0000000000000000000000000000000000000000000000000000000000000000"

          def post_install
            (prefix/"log").mkpath
            File.write(prefix/"marker.txt", "hi")
          end
        end
        """
    )
    formula = parse_formula_source(source)

    assert has_post_install(formula)
    assert "def post_install" in formula.post_install_source
    assert "marker.txt" in formula.post_install_source


# -- unit tests: real Ruby ------------------------------------------------
@requires_ruby
def test_real_ruby_runs_post_install_in_the_keg(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    keg = make_keg(config)
    formula = make_formula(
        textwrap.dedent(
            """\
            def post_install
              File.write(prefix/"post-install.txt", "ran by #{name}")
              File.write(prefix/"homebrew-prefix.txt", HOMEBREW_PREFIX)
              File.write(prefix/"homebrew-cellar.txt", HOMEBREW_CELLAR)
              (share/"posty").mkpath
              File.write(share/"posty"/"data.txt", "data")
              ohai "post install complete"
              opoo "a warning"
              puts "plain output"
            end
            """
        )
    )

    result = run_post_install(formula, config, keg=keg, ruby=REAL_RUBY, timeout=30)

    assert result.ran is True
    assert result.returncode == 0
    assert result.keg == keg
    assert (keg / "post-install.txt").read_text(encoding="utf-8") == "ran by posty"
    assert (keg / "homebrew-prefix.txt").read_text(encoding="utf-8") == str(config.prefix)
    assert (keg / "homebrew-cellar.txt").read_text(encoding="utf-8") == str(config.cellar)
    assert (keg / "share" / "posty" / "data.txt").read_text(encoding="utf-8") == "data"
    assert "==> post install complete" in result.stdout
    assert "Warning: a warning" in result.stdout
    assert "plain output" in result.stdout


@requires_ruby
def test_real_ruby_exposes_formula_lookup_paths(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    dep_keg = config.keg_path("dep", "2.0")
    (dep_keg / "bin").mkdir(parents=True)
    keg = make_keg(config)
    formula = make_formula(
        textwrap.dedent(
            """\
            def post_install
              File.write(prefix/"dep-prefix.txt", Formula["dep"].prefix.to_s)
              File.write(prefix/"dep-bin.txt", Formula["dep"].bin.to_s)
              File.write(prefix/"dep-opt.txt", Formula["dep"].opt_prefix.to_s)
              File.write(prefix/"ghost-opt.txt", Formula["ghost"].opt_prefix.to_s)
              File.write(prefix/"own-prefix.txt", Formula["posty"].prefix.to_s)
            end
            """
        )
    )

    result = run_post_install(formula, config, keg=keg, ruby=REAL_RUBY, timeout=30)

    assert result.ran is True
    assert (keg / "dep-prefix.txt").read_text(encoding="utf-8") == str(dep_keg)
    assert (keg / "dep-bin.txt").read_text(encoding="utf-8") == str(dep_keg / "bin")
    assert (keg / "dep-opt.txt").read_text(encoding="utf-8") == str(config.opt_path("dep"))
    assert (keg / "ghost-opt.txt").read_text(encoding="utf-8") == str(
        config.opt_path("ghost")
    )
    assert (keg / "own-prefix.txt").read_text(encoding="utf-8") == str(keg)


@requires_ruby
def test_real_ruby_exposes_fileutils_and_system(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    keg = make_keg(config)
    formula = make_formula(
        textwrap.dedent(
            """\
            def post_install
              FileUtils.mkdir_p(prefix/"log")
              system("/bin/sh", "-c", "printf system-ran > " + (prefix/"system.txt").to_s)
              Utils.safe_system("/bin/sh", "-c", "printf utils-ran > " + (prefix/"utils.txt").to_s)
            end
            """
        )
    )

    result = run_post_install(formula, config, keg=keg, ruby=REAL_RUBY, timeout=30)

    assert result.ran is True
    assert (keg / "log").is_dir()
    assert (keg / "system.txt").read_text(encoding="utf-8") == "system-ran"
    assert (keg / "utils.txt").read_text(encoding="utf-8") == "utils-ran"


@requires_ruby
def test_real_ruby_system_failure_aborts(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    keg = make_keg(config)
    formula = make_formula(
        'def post_install\n  system("/bin/sh", "-c", "exit 3")\nend'
    )

    with pytest.raises(PostInstallError) as excinfo:
        run_post_install(formula, config, keg=keg, ruby=REAL_RUBY, timeout=30)

    assert "command failed" in str(excinfo.value)
    assert excinfo.value.returncode not in (None, 0)


@requires_ruby
def test_real_ruby_failure_reports_stderr(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    keg = make_keg(config)
    formula = make_formula(
        'def post_install\n  raise "boom from post_install"\nend'
    )

    with pytest.raises(PostInstallError) as excinfo:
        run_post_install(formula, config, keg=keg, ruby=REAL_RUBY, timeout=30)

    error = excinfo.value
    assert error.returncode not in (None, 0)
    assert "boom from post_install" in error.stderr
    assert "post_install" in str(error)
    assert error.command[0] == REAL_RUBY


@requires_ruby
def test_real_ruby_bare_body_and_do_block_sources(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    keg = make_keg(config)

    bare = make_formula('File.write(prefix/"bare.txt", "bare")')
    bare_result = run_post_install(bare, config, keg=keg, ruby=REAL_RUBY, timeout=30)
    assert bare_result.ran is True
    assert (keg / "bare.txt").read_text(encoding="utf-8") == "bare"

    block = make_formula(
        'do\n  File.write(prefix/"block.txt", "block")\nend'
    )
    block_result = run_post_install(block, config, keg=keg, ruby=REAL_RUBY, timeout=30)
    assert block_result.ran is True
    assert (keg / "block.txt").read_text(encoding="utf-8") == "block"


# -- unit tests: failures / fake Ruby ------------------------------------
def test_missing_ruby_raises_clear_error(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    keg = make_keg(config)
    formula = make_formula("def post_install\nend")

    with pytest.raises(PostInstallError) as excinfo:
        run_post_install(
            formula, config, keg=keg, ruby=str(tmp_path / "no-such-ruby")
        )

    message = str(excinfo.value)
    assert "Ruby interpreter not found" in message
    assert "PYBREW_RUBY" in message


def test_missing_keg_raises_clear_error(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    formula = make_formula("def post_install\nend")

    with pytest.raises(PostInstallError, match="keg does not exist"):
        run_post_install(
            formula,
            config,
            keg=config.keg_path("posty", "1.0"),
            ruby=str(tmp_path / "fake-ruby"),
        )


def test_fake_ruby_receives_argv_cwd_env_and_payload(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    keg = make_keg(config)
    source = 'def post_install\n  File.write(prefix/"marker", "hi")\nend'
    formula = make_formula(source)
    fake_ruby, record, copies = make_fake_ruby(tmp_path)

    result = run_post_install(
        formula,
        config,
        keg=keg,
        ruby=str(fake_ruby),
        timeout=30,
        env={
            "PYBREW_FAKE_RECORD": str(record),
            "PYBREW_FAKE_COPY_DIR": str(copies),
        },
    )

    assert result.ran is True
    assert result.returncode == 0
    assert result.stdout == "fake stdout\n"
    recorded = read_record(record)
    assert recorded["cwd"] == str(keg)
    assert recorded["HOMEBREW_PREFIX"] == str(config.prefix)
    assert recorded["HOMEBREW_CELLAR"] == str(config.cellar)
    assert recorded["HOMEBREW_REPOSITORY"] == str(config.prefix)
    assert recorded["PYBREW_FORMULA_NAME"] == "posty"
    assert recorded["PYBREW_FORMULA_VERSION"] == "1.0"
    assert len(recorded["args"]) == 3
    assert recorded["args"][0].endswith("post_install_runner.rb")
    assert recorded["args"][1].endswith("config.json")
    assert recorded["args"][2].endswith("post_install_body.rb")

    payload = json.loads((copies / "config.json").read_text(encoding="utf-8"))
    assert payload["name"] == "posty"
    assert payload["version"] == "1.0"
    assert payload["paths"]["prefix"] == str(keg)
    assert payload["paths"]["bin"] == str(keg / "bin")
    assert payload["paths"]["opt_prefix"] == str(config.opt_path("posty"))
    assert payload["homebrew_prefix"] == str(config.prefix)
    assert payload["cellar"] == str(config.cellar)
    assert payload["formulas"]["posty"]["prefix"] == str(keg)

    body = (copies / "body.rb").read_text(encoding="utf-8")
    assert body == source

    runner = (copies / "runner.rb").read_text(encoding="utf-8")
    assert 'require "fileutils"' in runner
    assert 'require "pathname"' in runner
    assert "include FileUtils" in runner
    assert "Formula = PybrewFormula" in runner
    assert "module Utils" in runner
    assert "def system" in runner
    assert "def ohai" in runner
    assert "def opoo" in runner
    assert "def puts" in runner
    assert "HOMEBREW_PREFIX" in runner


def test_pybrew_ruby_env_override_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    keg = make_keg(config)
    formula = make_formula("def post_install\nend")
    fake_ruby, record, _copies = make_fake_ruby(tmp_path)
    monkeypatch.setenv("PYBREW_RUBY", str(fake_ruby))

    result = run_post_install(
        formula,
        config,
        keg=keg,
        timeout=30,
        env={"PYBREW_FAKE_RECORD": str(record)},
    )

    assert result.ran is True
    assert result.command[0] == str(fake_ruby)
    assert read_record(record)["cwd"] == str(keg)


def test_fake_ruby_failure_surfaces_stderr(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    keg = make_keg(config)
    formula = make_formula("def post_install\nend")
    fake_ruby, _record, _copies = make_fake_ruby(tmp_path)

    with pytest.raises(PostInstallError) as excinfo:
        run_post_install(
            formula,
            config,
            keg=keg,
            ruby=str(fake_ruby),
            timeout=30,
            env={
                "PYBREW_FAKE_EXIT": "7",
                "PYBREW_FAKE_STDERR": "fake ruby exploded",
            },
        )

    error = excinfo.value
    assert error.returncode == 7
    assert "fake ruby exploded" in error.stderr
    assert "failed" in str(error)
    assert error.command[0] == str(fake_ruby)


def test_fake_ruby_timeout_raises(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    keg = make_keg(config)
    formula = make_formula("def post_install\nend")
    slow_ruby = tmp_path / "slow-ruby"
    slow_ruby.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    slow_ruby.chmod(0o755)

    with pytest.raises(PostInstallError, match="timed out"):
        run_post_install(formula, config, keg=keg, ruby=str(slow_ruby), timeout=0.2)


def test_runner_wrapper_delegates(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    keg = make_keg(config)
    formula = make_formula("def post_install\nend")
    fake_ruby, record, _copies = make_fake_ruby(tmp_path)
    runner = PostInstallRunner(config, ruby=str(fake_ruby), timeout=30)

    result = runner.run(
        formula, keg, env={"PYBREW_FAKE_RECORD": str(record)}
    )

    assert result.ran is True
    assert read_record(record)["cwd"] == str(keg)


# -- installer integration ----------------------------------------------
def test_install_without_post_install_does_not_need_ruby(run_cli, sample) -> None:
    proc = run_cli(
        "install",
        "hello",
        bottle_tag=sample.tag,
        extra_env={"PYBREW_RUBY": str(sample.root / "missing-ruby")},
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    keg = sample.prefix / "Cellar" / "hello" / "1.0"
    receipt = read_receipt(keg)
    assert "post_install_ran" not in receipt


def test_installer_runs_post_install_with_fake_ruby(
    run_cli, sample, tmp_path: Path
) -> None:
    fake_ruby, record, copies = make_fake_ruby(tmp_path)
    add_post_install(sample.formula, 'File.write(prefix/"marker", "hi")')

    proc = run_cli(
        "install",
        "hello",
        bottle_tag=sample.tag,
        extra_env={
            "PYBREW_RUBY": str(fake_ruby),
            "PYBREW_FAKE_RECORD": str(record),
            "PYBREW_FAKE_COPY_DIR": str(copies),
        },
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    keg = sample.prefix / "Cellar" / "hello" / "1.0"
    recorded = read_record(record)
    assert recorded["cwd"] == str(keg)
    assert recorded["PYBREW_FORMULA_NAME"] == "hello"
    receipt = read_receipt(keg)
    assert receipt["post_install_ran"] is True
    # The marker is added without losing the normal receipt fields.
    assert receipt["poured_from_bottle"] is True
    assert (sample.prefix / "bin" / "hello").is_symlink()


@requires_ruby
def test_installer_runs_post_install_end_to_end(run_cli, sample) -> None:
    add_post_install(
        sample.formula,
        """
        File.write(prefix/"post-install-marker", "ran")
        (share/"posty").mkpath
        """,
    )

    proc = run_cli(
        "install",
        "hello",
        bottle_tag=sample.tag,
        extra_env={"PYBREW_RUBY": REAL_RUBY},
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    keg = sample.prefix / "Cellar" / "hello" / "1.0"
    assert (keg / "post-install-marker").read_text(encoding="utf-8") == "ran"
    assert (keg / "share" / "posty").is_dir()
    assert read_receipt(keg)["post_install_ran"] is True


@requires_ruby
def test_installer_aborts_when_post_install_fails(run_cli, sample) -> None:
    add_post_install(sample.formula, 'raise "kaboom"')

    proc = run_cli(
        "install",
        "hello",
        bottle_tag=sample.tag,
        extra_env={"PYBREW_RUBY": REAL_RUBY},
    )

    assert proc.returncode == 1
    combined = proc.stderr + proc.stdout
    assert "post_install" in combined.lower()
    assert "kaboom" in combined
    keg = sample.prefix / "Cellar" / "hello" / "1.0"
    assert "post_install_ran" not in read_receipt(keg)


def test_installer_aborts_when_ruby_is_missing(run_cli, sample) -> None:
    add_post_install(sample.formula, 'File.write(prefix/"marker", "hi")')

    proc = run_cli(
        "install",
        "hello",
        bottle_tag=sample.tag,
        extra_env={"PYBREW_RUBY": str(sample.root / "missing-ruby")},
    )

    assert proc.returncode == 1
    assert "Ruby interpreter not found" in proc.stderr + proc.stdout


def test_dry_run_does_not_run_post_install(run_cli, sample, tmp_path: Path) -> None:
    fake_ruby, record, _copies = make_fake_ruby(tmp_path)
    add_post_install(sample.formula, 'File.write(prefix/"marker", "hi")')

    proc = run_cli(
        "install",
        "--dry-run",
        "hello",
        bottle_tag=sample.tag,
        extra_env={
            "PYBREW_RUBY": str(fake_ruby),
            "PYBREW_FAKE_RECORD": str(record),
        },
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert not record.exists()


def test_no_link_install_still_runs_post_install(
    run_cli, sample, tmp_path: Path
) -> None:
    fake_ruby, record, _copies = make_fake_ruby(tmp_path)
    add_post_install(sample.formula, 'File.write(prefix/"marker", "hi")')

    proc = run_cli(
        "install",
        "--no-link",
        "hello",
        bottle_tag=sample.tag,
        extra_env={
            "PYBREW_RUBY": str(fake_ruby),
            "PYBREW_FAKE_RECORD": str(record),
        },
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    keg = sample.prefix / "Cellar" / "hello" / "1.0"
    assert read_record(record)["cwd"] == str(keg)
    assert read_receipt(keg)["post_install_ran"] is True
    assert not (sample.prefix / "bin" / "hello").exists()


def test_installer_skips_post_install_when_keg_already_exists(
    run_cli, sample, tmp_path: Path
) -> None:
    fake_ruby, record, _copies = make_fake_ruby(tmp_path)
    add_post_install(sample.formula, 'File.write(prefix/"marker", "hi")')
    env = {
        "PYBREW_RUBY": str(fake_ruby),
        "PYBREW_FAKE_RECORD": str(record),
    }

    first = run_cli("install", "hello", bottle_tag=sample.tag, extra_env=env)
    assert first.returncode == 0, first.stderr + first.stdout
    record.unlink()

    second = run_cli("install", "hello", bottle_tag=sample.tag, extra_env=env)
    assert second.returncode == 0, second.stderr + second.stdout
    assert "Already installed" in second.stdout
    assert not record.exists()
