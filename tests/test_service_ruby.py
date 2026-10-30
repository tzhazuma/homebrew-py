"""Tests for the opt-in arbitrary-Ruby service DSL evaluator.

The evaluator is only active with ``PYBREW_ALLOW_SERVICE_RUBY=1``; the default
literal parser must keep behaving exactly as before.  Every fixture here is
synthetic and local: no network access and no writes outside ``tmp_path``.
Real-Ruby tests are skipped when no Ruby interpreter is installed; the gate,
failure and JSON-conversion tests use a fake interpreter so they always run.
"""

from __future__ import annotations

import json
import os
import shutil
import textwrap
from pathlib import Path

import pytest

from pybrew.paths import Config
from pybrew.ruby_parser import parse_formula_source
from pybrew.service_ruby import (
    ALLOW_ENV,
    RUBY_ENV,
    evaluate_service_block,
    evaluate_service_source,
    service_ruby_enabled,
)
from pybrew.services import (
    ServiceError,
    ServiceManager,
    parse_service_block,
)

_RUBY = os.environ.get(RUBY_ENV) or "/usr/bin/ruby"
requires_ruby = pytest.mark.skipif(
    shutil.which(_RUBY) is None and not Path(_RUBY).is_file(),
    reason="Ruby interpreter not installed; opt-in service evaluator tests skipped",
)

_DIGEST = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


@pytest.fixture
def allow_ruby(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("PYBREW_SERVICE_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("PYBREW_SERVICE_BACKEND", "launchd")
    config = Config.default(prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache")
    config.ensure()
    return config


def _formula(version: str = "2.5.1") -> object:
    source = f'''
class Svc < Formula
  desc "service fixture"
  homepage "https://example.com/svc"
  url "https://example.com/svc-{version}.tar.gz"
  sha256 "{_DIGEST}"
  license "MIT"
  version "{version}"
end
'''
    return parse_formula_source(source, name="svc")


def _formula_with_service(body: str, version: str = "2.5.1") -> object:
    source = f'''
class Svc < Formula
  desc "service fixture"
  homepage "https://example.com/svc"
  url "https://example.com/svc-{version}.tar.gz"
  sha256 "{_DIGEST}"
  license "MIT"
  version "{version}"

  service do
{textwrap.indent(textwrap.dedent(body).strip(), "    ")}
  end
end
'''
    return parse_formula_source(source, name="svc")


def _body(text: str) -> str:
    return textwrap.dedent(text).strip()


def _fake_ruby(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-ruby"
    script.write_text("#!/bin/sh\n" + body.strip() + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _fake_json_ruby(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: object
) -> Path:
    """A fake Ruby that writes ``payload`` where the real shim would."""

    fake = _fake_ruby(
        tmp_path,
        'printf "%s" "$FAKE_RUBY_JSON" > "$(dirname "$1")/service.json"\n'
        "exit 0",
    )
    monkeypatch.setenv("FAKE_RUBY_JSON", json.dumps(payload))
    return fake


# ---------------------------------------------------------------------------
# opt-in gate and missing Ruby (no real interpreter needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", " 1", "01"])
def test_disabled_unless_env_is_exactly_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv(ALLOW_ENV, raising=False)
    else:
        monkeypatch.setenv(ALLOW_ENV, value)

    config = _config(tmp_path, monkeypatch)
    assert service_ruby_enabled() is False
    with pytest.raises(ServiceError, match="PYBREW_ALLOW_SERVICE_RUBY=1"):
        evaluate_service_source('run opt_bin/"svc"', _formula(), config)


def test_only_exact_one_enables_the_evaluator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOW_ENV, "1")
    assert service_ruby_enabled() is True


def test_missing_ruby_reports_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    monkeypatch.setenv(RUBY_ENV, str(tmp_path / "no-such-ruby"))
    config = _config(tmp_path, monkeypatch)

    with pytest.raises(ServiceError, match="Ruby interpreter not found"):
        evaluate_service_source('run opt_bin/"svc"', _formula(), config)


def test_evaluate_service_source_rejects_non_string_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    with pytest.raises(ServiceError, match="service source must be a string"):
        evaluate_service_source(None, _formula(), config)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fake Ruby: interpreter override, argv, failures and JSON conversion
# ---------------------------------------------------------------------------


def test_pybrew_ruby_override_gets_the_generated_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    log = tmp_path / "argv.log"
    fake = _fake_ruby(
        tmp_path,
        f'''
printf '%s\\n' "$#" > "{log}"
printf '%s\\n' "$1" >> "{log}"
pwd >> "{log}"
echo "fake ruby exploded" >&2
exit 7
''',
    )
    monkeypatch.setenv(RUBY_ENV, str(fake))
    config = _config(tmp_path, monkeypatch)

    with pytest.raises(ServiceError) as error:
        evaluate_service_source('run opt_bin/"svc"', _formula(), config)

    message = str(error.value)
    assert "arbitrary Ruby service evaluation failed" in message
    assert "exit 7" in message
    assert "fake ruby exploded" in message

    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "1"
    script_path = Path(lines[1])
    assert script_path.name == "service_evaluator.rb"
    # The shim runs from its temporary directory so relative paths are safe.
    # Resolve both sides: macOS reports /var as /private/var from ``pwd``.
    assert Path(lines[2]).resolve() == script_path.parent.resolve()


def test_fake_ruby_json_is_converted_to_service_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    payload = {
        "run": ["/opt/svc/bin/svc", "--foreground"],
        "working_dir": "/opt/svc/var/lib/svc",
        "log_path": "/opt/svc/var/log/svc.log",
        "error_log_path": "/opt/svc/var/log/svc.err.log",
        "keep_alive": {"network_state": True, "interval": 45},
        "run_at_load": False,
        "interval": 300,
        "process_type": "background",
        "run_type": "cron",
        "environment_variables": {"PATH": "/opt/svc/bin", "FOO": "bar", "NUM": 7},
        "require_root": True,
        "throttle_interval": 20,
        "launch_only_once": True,
        "macos_legacy_timers": True,
        "sockets": {"http": "tcp://127.0.0.1:8080"},
        "warnings": ["unsupported service directive `nice`"],
    }
    monkeypatch.setenv(RUBY_ENV, str(_fake_json_ruby(tmp_path, monkeypatch, payload)))
    config = _config(tmp_path, monkeypatch)

    spec = evaluate_service_source('run opt_bin/"svc"', _formula(), config)

    assert spec.run == ["/opt/svc/bin/svc", "--foreground"]
    assert spec.working_dir == "/opt/svc/var/lib/svc"
    assert spec.log_path == "/opt/svc/var/log/svc.log"
    assert spec.error_log_path == "/opt/svc/var/log/svc.err.log"
    assert spec.keep_alive == {"network_state": True, "interval": 45}
    assert spec.run_at_load is False
    assert spec.interval == 300
    assert spec.process_type == "background"
    assert spec.run_type == "cron"
    assert spec.environment_variables == {
        "PATH": "/opt/svc/bin",
        "FOO": "bar",
        "NUM": "7",
    }
    assert spec.require_root is True
    assert spec.throttle_interval == 20
    assert spec.launch_only_once is True
    assert spec.macos_legacy_timers is True
    assert spec.sockets == {
        "http": {"host": "127.0.0.1", "port": "8080", "type": "tcp"}
    }


@pytest.mark.parametrize(
    "run_value,expected",
    [
        ("svc", ["svc"]),
        (["svc", "--foreground"], ["svc", "--foreground"]),
        (None, []),
        (7, ["7"]),
    ],
)
def test_run_string_array_and_scalar_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    run_value: object,
    expected: list[str],
) -> None:
    monkeypatch.setenv(
        RUBY_ENV,
        str(_fake_json_ruby(tmp_path, monkeypatch, {"run": run_value})),
    )
    config = _config(tmp_path, monkeypatch)
    spec = evaluate_service_source('run opt_bin/"svc"', _formula(), config)
    assert spec.run == expected


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"keep_alive": {"bogus": True}}, "keep_alive"),
        ({"keep_alive": {"successful_exit": "maybe"}}, "successful_exit"),
        ({"process_type": "daemon"}, "process_type"),
        ({"interval": "soon"}, "interval"),
        ({"sockets": "not-a-socket"}, "sockets"),
        ({"sockets": "tcp://example.com:80"}, "IPv4"),
        ({"run_at_load": "maybe"}, "run_at_load"),
        ({"require_root": "maybe"}, "require_root"),
        ({"environment_variables": ["A", "B"]}, "environment_variables"),
    ],
)
def test_invalid_values_from_ruby_raise_service_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    payload: dict[str, object],
    match: str,
) -> None:
    monkeypatch.setenv(RUBY_ENV, str(_fake_json_ruby(tmp_path, monkeypatch, payload)))
    config = _config(tmp_path, monkeypatch)

    with pytest.raises(ServiceError, match=match) as error:
        evaluate_service_source('run opt_bin/"svc"', _formula(), config)
    assert str(error.value).startswith("svc: ")


def test_fake_ruby_without_result_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    monkeypatch.setenv(
        RUBY_ENV,
        str(_fake_ruby(tmp_path, 'echo "no json produced"\nexit 0')),
    )
    config = _config(tmp_path, monkeypatch)

    with pytest.raises(ServiceError, match="did not produce a result"):
        evaluate_service_source('run opt_bin/"svc"', _formula(), config)


def test_fake_ruby_invalid_json_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    fake = _fake_ruby(
        tmp_path,
        'printf "%s" "not json" > "$(dirname "$1")/service.json"\nexit 0',
    )
    monkeypatch.setenv(RUBY_ENV, str(fake))
    config = _config(tmp_path, monkeypatch)

    with pytest.raises(ServiceError, match="invalid JSON"):
        evaluate_service_source('run opt_bin/"svc"', _formula(), config)


def test_fake_ruby_non_object_json_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    monkeypatch.setenv(
        RUBY_ENV,
        str(_fake_json_ruby(tmp_path, monkeypatch, ["not", "an", "object"])),
    )
    config = _config(tmp_path, monkeypatch)

    with pytest.raises(ServiceError, match="unexpected result"):
        evaluate_service_source('run opt_bin/"svc"', _formula(), config)


def test_ruby_timeout_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    monkeypatch.setenv(RUBY_ENV, str(_fake_ruby(tmp_path, "sleep 5")))
    monkeypatch.setattr("pybrew.service_ruby.RUBY_TIMEOUT", 0.5)
    config = _config(tmp_path, monkeypatch)

    with pytest.raises(ServiceError, match="timed out"):
        evaluate_service_source('run opt_bin/"svc"', _formula(), config)


# ---------------------------------------------------------------------------
# real Ruby: DSL capture
# ---------------------------------------------------------------------------


@requires_ruby
def test_basic_fields_and_directives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    spec = evaluate_service_source(
        _body(
            '''
            run [opt_bin/"svc", "--foreground"]
            working_dir var/"lib/svc"
            log_path var/"log/svc.log"
            error_log_path var/"log/svc.err.log"
            keep_alive network_state: true, successful_exit: false, interval: 45
            process_type :background
            run_type :cron
            interval 300
            environment_variables PATH: std_service_path_env, FOO: "bar", NUM: 7
            require_root true
            run_at_load false
            throttle_interval 20
            macos_legacy_timers true
            launch_only_once true
            sockets http: "tcp://127.0.0.1:8080"
            '''
        ),
        formula,
        config,
    )

    opt_prefix = config.opt_path("svc")
    assert spec.run == [str(opt_prefix / "bin" / "svc"), "--foreground"]
    assert spec.working_dir == str(config.prefix / "var" / "lib" / "svc")
    assert spec.log_path == str(config.prefix / "var" / "log" / "svc.log")
    assert spec.error_log_path == str(config.prefix / "var" / "log" / "svc.err.log")
    assert spec.keep_alive == {
        "network_state": True,
        "successful_exit": False,
        "interval": 45,
    }
    assert spec.process_type == "background"
    assert spec.run_type == "cron"
    assert spec.interval == 300
    assert spec.run_at_load is False
    assert spec.require_root is True
    assert spec.throttle_interval == 20
    assert spec.macos_legacy_timers is True
    assert spec.launch_only_once is True
    assert spec.environment_variables == {
        "PATH": str(config.prefix / "bin"),
        "FOO": "bar",
        "NUM": "7",
    }
    assert spec.sockets == {
        "http": {"host": "127.0.0.1", "port": "8080", "type": "tcp"}
    }


@requires_ruby
def test_run_string_and_array_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()

    single = evaluate_service_source('run opt_bin/"svc"', formula, config)
    assert single.run == [str(config.opt_path("svc") / "bin" / "svc")]

    array = evaluate_service_source(
        'run [opt_bin/"svc", "--foreground"]', formula, config
    )
    assert array.run == [
        str(config.opt_path("svc") / "bin" / "svc"),
        "--foreground",
    ]


@requires_ruby
def test_arbitrary_ruby_variables_conditionals_and_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula(version="2.5.1")
    spec = evaluate_service_source(
        _body(
            '''
            base = opt_prefix
            args = ["--foreground"]
            %w[--log-level=info --no-daemon].each { |flag| args << flag }
            args << "--v2" if version.major.to_i >= 2
            run [base/"bin/svc", *args]

            env = {}
            %w[FOO BAR].each_with_index { |name, index| env[name] = index.to_s }
            environment_variables env

            if version.minor.to_i == 5
              interval 15
            else
              interval 30
            end
            '''
        ),
        formula,
        config,
    )

    assert spec.run == [
        str(config.opt_path("svc") / "bin" / "svc"),
        "--foreground",
        "--log-level=info",
        "--no-daemon",
        "--v2",
    ]
    assert spec.environment_variables == {"FOO": "0", "BAR": "1"}
    assert spec.interval == 15


@requires_ruby
def test_path_helpers_and_interpolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula(version="2.5.1")
    spec = evaluate_service_source(
        _body(
            '''
            run ["#{opt_bin}/svc", "--version=#{version.major_minor}"]
            working_dir var/"lib/#{formula.name}"
            log_path "#{prefix}/var/log/#{formula.name}.log"
            environment_variables PREFIX: prefix,
                                  OPT_PREFIX: opt_prefix,
                                  OPT_BIN: opt_bin,
                                  BIN: bin,
                                  LIB: lib,
                                  ETC: etc,
                                  VAR: var
            '''
        ),
        formula,
        config,
    )

    opt_prefix = config.opt_path("svc")
    assert spec.run == [f"{opt_prefix / 'bin'}/svc", "--version=2.5"]
    assert spec.working_dir == str(config.prefix / "var" / "lib" / "svc")
    assert spec.log_path == str(config.prefix / "var" / "log" / "svc.log")
    assert spec.environment_variables == {
        "PREFIX": str(config.prefix),
        "OPT_PREFIX": str(opt_prefix),
        "OPT_BIN": str(opt_prefix / "bin"),
        "BIN": str(opt_prefix / "bin"),
        "LIB": str(opt_prefix / "lib"),
        "ETC": str(opt_prefix / "etc"),
        "VAR": str(config.prefix / "var"),
    }


@requires_ruby
def test_platform_blocks_follow_target_os_and_arch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    body = _body(
        '''
        on_macos do
          run opt_bin/"mac"
        end
        on_linux do
          run opt_bin/"linux"
        end
        on_arm do
          interval 10
        end
        on_intel do
          interval 20
        end
        '''
    )

    monkeypatch.setenv("PYBREW_TARGET_OS", "darwin")
    monkeypatch.setenv("PYBREW_TARGET_ARCH", "arm64")
    mac = evaluate_service_source(body, formula, config)
    assert mac.run == [str(config.opt_path("svc") / "bin" / "mac")]
    assert mac.interval == 10

    monkeypatch.setenv("PYBREW_TARGET_OS", "linux")
    monkeypatch.setenv("PYBREW_TARGET_ARCH", "x86_64")
    linux = evaluate_service_source(body, formula, config)
    assert linux.run == [str(config.opt_path("svc") / "bin" / "linux")]
    assert linux.interval == 20


@requires_ruby
def test_keep_alive_boolean_and_hash_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()

    assert (
        evaluate_service_source("keep_alive true", formula, config).keep_alive is True
    )
    assert (
        evaluate_service_source("keep_alive false", formula, config).keep_alive is False
    )
    interval_only = evaluate_service_source(
        "keep_alive interval: 30", formula, config
    )
    assert interval_only.keep_alive == {"interval": 30}


@requires_ruby
def test_keep_alive_other_job_enabled_and_path_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    spec = evaluate_service_source(
        'keep_alive path: var/"run/svc.pid", '
        'other_job_enabled: { "com.example.job" => true }',
        formula,
        config,
    )

    assert spec.keep_alive == {
        "path": str(config.prefix / "var" / "run" / "svc.pid"),
        "other_job_enabled": {"com.example.job": True},
    }


@requires_ruby
def test_sockets_string_and_hash_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()

    single = evaluate_service_source(
        'sockets "tcp://0.0.0.0:80"', formula, config
    )
    assert single.sockets == {
        "listeners": {"host": "0.0.0.0", "port": "80", "type": "tcp"}
    }

    named = evaluate_service_source(
        'sockets http: "tcp://127.0.0.1:8080", dns: "udp://[::1]:5353"',
        formula,
        config,
    )
    assert named.sockets == {
        "http": {"host": "127.0.0.1", "port": "8080", "type": "tcp"},
        "dns": {"host": "::1", "port": "5353", "type": "udp"},
    }


@requires_ruby
def test_ruby_failure_reports_stderr_and_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    with pytest.raises(ServiceError) as error:
        evaluate_service_source(
            'run opt_bin/"svc"\nraise "boom"', _formula(), config
        )

    message = str(error.value)
    assert "arbitrary Ruby service evaluation failed" in message
    assert "boom" in message
    assert "svc-service.rb line" in message


@requires_ruby
def test_ruby_syntax_error_reports_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    with pytest.raises(ServiceError) as error:
        evaluate_service_source('run [opt_bin/"svc"', _formula(), config)

    message = str(error.value)
    assert "syntax error" in message
    assert "svc-service.rb" in message


@requires_ruby
def test_evaluate_service_block_reads_captured_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        '''
        args = ["--foreground"]
        run [opt_bin/"svc", *args]
        keep_alive true
        '''
    )

    spec = evaluate_service_block(formula, config)

    assert spec.run == [
        str(config.opt_path("svc") / "bin" / "svc"),
        "--foreground",
    ]
    assert spec.keep_alive is True


@requires_ruby
def test_full_service_declaration_source_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    source = 'service do\n  run opt_bin/"svc"\n  keep_alive true\nend'

    spec = evaluate_service_source(source, _formula(), config)

    assert spec.run == [str(config.opt_path("svc") / "bin" / "svc")]
    assert spec.keep_alive is True


@requires_ruby
def test_evaluate_service_block_without_service_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    with pytest.raises(ServiceError, match="does not define a service"):
        evaluate_service_block(_formula(), config)


@requires_ruby
def test_service_manager_uses_evaluator_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        '''
        args = ["--foreground"]
        run [opt_bin/"svc", *args]
        keep_alive true
        environment_variables PATH: std_service_path_env
        '''
    )

    manager = ServiceManager(config)
    spec = manager.spec_for(formula)
    payload = manager.plist_payload(formula, spec)

    assert spec.run == [
        str(config.opt_path("svc") / "bin" / "svc"),
        "--foreground",
    ]
    assert payload["ProgramArguments"] == spec.run
    assert payload["KeepAlive"] is True
    assert payload["EnvironmentVariables"]["PATH"] == str(config.prefix / "bin")


# ---------------------------------------------------------------------------
# default parser must be unchanged
# ---------------------------------------------------------------------------


LITERAL_SERVICE_BODY = """
run [opt_bin/"svc", "--foreground"]
working_dir var/"lib/svc"
log_path var/"log/svc.log"
keep_alive true
environment_variables PATH: std_service_path_env, FOO: "bar"
"""


def test_default_literal_parser_unchanged_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(LITERAL_SERVICE_BODY)

    spec = parse_service_block(formula, config)

    assert spec.run == [
        str(config.opt_path("svc") / "bin" / "svc"),
        "--foreground",
    ]
    assert spec.working_dir == str(config.prefix / "var" / "lib" / "svc")
    assert spec.log_path == str(config.prefix / "var" / "log" / "svc.log")
    assert spec.keep_alive is True
    assert spec.environment_variables == {
        "PATH": str(config.prefix / "bin"),
        "FOO": "bar",
    }


@pytest.mark.parametrize("value", ["0", "true", "yes", "01", " 1"])
def test_non_exact_env_values_keep_the_literal_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(ALLOW_ENV, value)
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(LITERAL_SERVICE_BODY)

    spec = parse_service_block(formula, config)

    assert spec.keep_alive is True
    assert spec.environment_variables["FOO"] == "bar"


def test_parse_service_block_routes_only_exact_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(LITERAL_SERVICE_BODY)
    fake = _fake_ruby(tmp_path, 'echo "evaluator was used" >&2\nexit 9')

    # Exactly "1" routes through the (failing) Ruby evaluator.
    monkeypatch.setenv(ALLOW_ENV, "1")
    monkeypatch.setenv(RUBY_ENV, str(fake))
    with pytest.raises(ServiceError) as error:
        parse_service_block(formula, config)
    assert "arbitrary Ruby service evaluation failed" in str(error.value)
    assert "evaluator was used" in str(error.value)

    # Any other value keeps the default literal parser even though the Ruby
    # override points at a failing interpreter.
    monkeypatch.setenv(ALLOW_ENV, "0")
    spec = parse_service_block(formula, config)
    assert spec.keep_alive is True
    assert spec.environment_variables["FOO"] == "bar"
