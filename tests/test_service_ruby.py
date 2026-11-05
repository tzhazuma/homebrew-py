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
    USER_CONFIG_HOME_ENV,
    _read_user_service_env,
    evaluate_service_block,
    evaluate_service_source,
    service_ruby_enabled,
)
from pybrew.services import (
    SERVICE_PATH_MODE_ENV,
    ServiceError,
    ServiceManager,
    ServiceSpec,
    parse_service_block,
    service_path_mode,
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


@pytest.fixture
def non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the effective uid so user env files are read even in root CI."""

    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("PYBREW_SERVICE_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("PYBREW_SERVICE_BACKEND", "launchd")
    monkeypatch.delenv(USER_CONFIG_HOME_ENV, raising=False)
    monkeypatch.delenv(SERVICE_PATH_MODE_ENV, raising=False)
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


def _keg_prefix(config: Config, formula: object) -> Path:
    """The versioned keg path Homebrew's Formula#prefix points at."""

    return config.cellar / formula.name / formula.pkg_version


def _homebrew_std_service_path_env(config: Config) -> str:
    """Homebrew's Service#std_service_path_env value."""

    return ":".join(
        [
            str(config.prefix / "bin"),
            str(config.prefix / "sbin"),
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
    )


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
    assert spec.warnings == ["unsupported service directive `nice`"]


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
            root_dir var/"root"
            input_path var/"in"
            log_path var/"log/svc.log"
            error_log_path var/"log/svc.err.log"
            keep_alive network_state: true, successful_exit: false, interval: 45
            process_type :background
            run_type :cron
            name macos: "com.example.svc", linux: "custom-svc"
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
    assert spec.root_dir == str(config.prefix / "var" / "root")
    assert spec.input_path == str(config.prefix / "var" / "in")
    assert spec.log_path == str(config.prefix / "var" / "log" / "svc.log")
    assert spec.error_log_path == str(config.prefix / "var" / "log" / "svc.err.log")
    assert spec.keep_alive == {
        "network_state": True,
        "successful_exit": False,
        "interval": 45,
    }
    assert spec.process_type == "background"
    assert spec.run_type == "cron"
    assert spec.plist_name == "com.example.svc"
    assert spec.service_name == "custom-svc"
    assert spec.interval == 300
    assert spec.run_at_load is False
    assert spec.require_root is True
    assert spec.throttle_interval == 20
    assert spec.macos_legacy_timers is True
    assert spec.launch_only_once is True
    assert spec.environment_variables == {
        "PATH": _homebrew_std_service_path_env(config),
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
            run ["#{opt_bin}/svc", "--version=#{version.major_minor}",
                 "--prefix=#{prefix}", "--keg=#{bin}"]
            working_dir var/"lib/#{formula.name}"
            log_path var/"log/#{formula.name}.log"
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
    keg = _keg_prefix(config, formula)
    assert spec.run == [
        f"{opt_prefix / 'bin'}/svc",
        "--version=2.5",
        f"--prefix={keg}",
        f"--keg={keg / 'bin'}",
    ]
    assert spec.working_dir == str(config.prefix / "var" / "lib" / "svc")
    assert spec.log_path == str(config.prefix / "var" / "log" / "svc.log")
    # Homebrew Formula path semantics: bin/lib live under the versioned keg,
    # etc/var are HOMEBREW_PREFIX subdirectories, opt_* are the stable paths.
    assert spec.environment_variables == {
        "PREFIX": str(keg),
        "OPT_PREFIX": str(opt_prefix),
        "OPT_BIN": str(opt_prefix / "bin"),
        "BIN": str(keg / "bin"),
        "LIB": str(keg / "lib"),
        "ETC": str(config.prefix / "etc"),
        "VAR": str(config.prefix / "var"),
    }


@requires_ruby
def test_prefix_uses_opt_prefix_when_keg_is_optlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula(version="2.5.1")
    keg = _keg_prefix(config, formula)
    keg.mkdir(parents=True)
    opt_prefix = config.opt_path("svc")
    opt_prefix.symlink_to(keg)

    spec = evaluate_service_source(
        _body(
            '''
            run [prefix.to_s, bin.to_s, lib.to_s, opt_bin.to_s, etc.to_s]
            '''
        ),
        formula,
        config,
    )

    # Homebrew's Formula#prefix returns opt_prefix once the keg is opt-linked.
    assert spec.run == [
        str(opt_prefix),
        str(opt_prefix / "bin"),
        str(opt_prefix / "lib"),
        str(opt_prefix / "bin"),
        str(config.prefix / "etc"),
    ]


@requires_ruby
def test_path_dirs_follow_configured_service_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    spec = evaluate_service_source(
        _body(
            '''
            working_dir var/"lib/svc"
            root_dir var/"root"
            input_path var/"in"
            log_path var/"log/svc.log"
            error_log_path var/"log/svc.err.log"
            run path_dirs.map(&:to_s)
            '''
        ),
        formula,
        config,
    )

    assert spec.run == [
        str(config.prefix / "var" / "lib" / "svc"),
        str(config.prefix / "var" / "root"),
        str(config.prefix / "var"),
        str(config.prefix / "var" / "log"),
    ]
    # No configured paths (or only relative ones) yields no path dirs.
    empty = evaluate_service_source(
        'working_dir "relative/dir"\nrun path_dirs.map(&:to_s)',
        formula,
        config,
    )
    assert empty.run == []


@requires_ruby
def test_path_dir_and_path_parent_dir_match_homebrew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    spec = evaluate_service_source(
        _body(
            '''
            run [
              path_dir("relative/path").inspect,
              path_dir("").inspect,
              path_dir(nil).inspect,
              path_parent_dir("relative/path").inspect,
              path_dir("~/svc").to_s,
              path_parent_dir(var/"log/svc.log").to_s
            ]
            '''
        ),
        formula,
        config,
    )

    assert spec.run == [
        "nil",
        "nil",
        "nil",
        "nil",
        str(Path.home() / "svc"),
        str(config.prefix / "var" / "log"),
    ]


@requires_ruby
def test_manual_command_is_shell_quoted_homebrew_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    spec = evaluate_service_source(
        _body(
            '''
            run [opt_bin/"svc", "--flag", "two words"]
            environment_variables FOO: "bar", PATH: std_service_path_env
            run [manual_command]
            '''
        ),
        formula,
        config,
    )

    opt_bin = config.opt_path("svc") / "bin"
    # PATH is excluded from the manual command; the other variable is kept
    # verbatim and command arguments are shell-quoted.
    assert spec.run == [f'FOO="bar" {opt_bin}/svc --flag two\\ words']


@requires_ruby
def test_homebrew_paths_work_with_a_minimal_fake_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    # A fake config that only exposes ``prefix``: Homebrew paths are derived
    # from it and the evaluator does not require Config.opt_path/keg_path.
    class FakeConfig:
        def __init__(self, prefix: Path) -> None:
            self.prefix = prefix

    prefix = tmp_path / "fake-prefix"
    formula = _formula(version="2.5.1")
    spec = evaluate_service_source(
        _body(
            '''
            run [prefix.to_s, opt_prefix.to_s, opt_bin.to_s, bin.to_s,
                 etc.to_s, var.to_s]
            '''
        ),
        formula,
        FakeConfig(prefix),
    )

    keg = prefix / "Cellar" / "svc" / "2.5.1"
    assert spec.run == [
        str(keg),
        str(prefix / "opt" / "svc"),
        str(prefix / "opt" / "svc" / "bin"),
        str(keg / "bin"),
        str(prefix / "etc"),
        str(prefix / "var"),
    ]


@requires_ruby
def test_path_helpers_fall_back_when_config_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    # No configuration at all: the evaluator still captures non-path fields
    # and path helpers resolve to empty paths instead of failing.
    spec = evaluate_service_source(
        'run [prefix.to_s, bin.to_s, opt_bin.to_s, keep_alive?.to_s]',
        _formula(),
        None,  # type: ignore[arg-type]
    )
    assert spec.run == ["", "", "", "false"]


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
def test_unknown_directives_are_recorded_as_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    spec = evaluate_service_source(
        _body(
            '''
            run opt_bin/"svc"
            frobnicate "x"
            '''
        ),
        _formula(),
        config,
    )

    assert spec.run == [str(config.opt_path("svc") / "bin" / "svc")]
    assert spec.warnings == ["unsupported service directive `frobnicate`"]


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
    assert payload["EnvironmentVariables"]["PATH"] == _homebrew_std_service_path_env(
        config
    )


@requires_ruby
def test_working_and_log_paths_render_from_evaluated_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        '''
        run [opt_bin/"svc"]
        working_dir var/"lib/svc"
        root_dir var/"root"
        input_path var/"in"
        log_path var/"log/svc.log"
        error_log_path var/"log/svc.err.log"
        '''
    )

    manager = ServiceManager(config)
    spec = manager.spec_for(formula)

    assert spec.working_dir == str(config.prefix / "var" / "lib" / "svc")
    assert spec.root_dir == str(config.prefix / "var" / "root")
    assert spec.input_path == str(config.prefix / "var" / "in")
    assert spec.log_path == str(config.prefix / "var" / "log" / "svc.log")
    assert spec.error_log_path == str(config.prefix / "var" / "log" / "svc.err.log")

    payload = manager.plist_payload(formula, spec)
    assert payload["WorkingDirectory"] == spec.working_dir
    assert payload["StandardInPath"] == spec.input_path
    assert payload["StandardOutPath"] == spec.log_path
    assert payload["StandardErrorPath"] == spec.error_log_path

    unit = manager.unit_text(formula, spec)
    assert f"RootDirectory={spec.root_dir}" in unit
    assert f"StandardInput=file:{spec.input_path}" in unit
    assert f"WorkingDirectory={spec.working_dir}" in unit
    assert f"StandardOutput=append:{spec.log_path}" in unit
    assert f"StandardError=append:{spec.error_log_path}" in unit


# ---------------------------------------------------------------------------
# Homebrew user service environment overrides
# ---------------------------------------------------------------------------


def _write_user_env(
    home: Path, name: str, text: str, mode: int = 0o600
) -> Path:
    directory = home / "services"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.env"
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


@requires_ruby
def test_user_service_env_overrides_formula_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    env_path = _write_user_env(
        user_home,
        "svc",
        """
        # user overrides
        FOO=override
        BAR=value=with=equals

        NOT A PAIR
        SPACED =  trimmed
        """,
    )
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))

    spec = evaluate_service_source(
        _body(
            '''
            environment_variables FOO: "formula", BAZ: "keep",
                                  PATH: std_service_path_env
            frobnicate "x"
            '''
        ),
        _formula(),
        config,
    )

    # The declared environment stays exactly as the service block declared it.
    assert spec.environment_variables == {
        "FOO": "formula",
        "BAZ": "keep",
        "PATH": _homebrew_std_service_path_env(config),
    }
    assert spec.declared_environment_variables() == spec.environment_variables
    # User overrides are stored separately and only win in the effective map.
    assert spec.user_environment_variables == {
        "FOO": "override",
        "BAR": "value=with=equals",
        "SPACED": "trimmed",
    }
    assert spec.effective_environment_variables() == {
        "FOO": "override",
        "BAZ": "keep",
        "PATH": _homebrew_std_service_path_env(config),
        "BAR": "value=with=equals",
        "SPACED": "trimmed",
    }
    # Env skip warnings precede the evaluator's unknown-directive warning.
    assert spec.warnings == [
        f"Skipping invalid line in {env_path}: NOT A PAIR",
        "unsupported service directive `frobnicate`",
    ]


@requires_ruby
def test_user_service_env_injectable_path_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "explicit-config"
    _write_user_env(user_home, "svc", "FOO=custom\n")
    assert os.environ.get(USER_CONFIG_HOME_ENV) is None

    spec = evaluate_service_block(
        _formula_with_service('environment_variables FOO: "formula"'),
        config,
        user_config_home=user_home,
    )

    assert spec.environment_variables["FOO"] == "formula"
    assert spec.user_environment_variables["FOO"] == "custom"
    assert spec.effective_environment_variables()["FOO"] == "custom"


@requires_ruby
def test_parse_service_block_passes_injectable_user_config_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "explicit-config"
    _write_user_env(user_home, "svc", "FOO=custom\n")
    assert os.environ.get(USER_CONFIG_HOME_ENV) is None
    formula = _formula_with_service('environment_variables FOO: "formula"')

    spec = parse_service_block(formula, config, user_config_home=user_home)

    assert spec.environment_variables["FOO"] == "formula"
    assert spec.user_environment_variables["FOO"] == "custom"
    assert spec.effective_environment_variables()["FOO"] == "custom"


@requires_ruby
def test_ruby_render_time_user_env_replaces_stale_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    env_path = _write_user_env(user_home, "svc", "FOO=one\n")
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\nenvironment_variables FOO: "formula"'
    )

    spec = parse_service_block(formula, config)
    assert spec.environment_variables == {"FOO": "formula"}
    assert spec.user_environment_variables == {"FOO": "one"}
    assert spec.user_config_home == str(user_home)

    env_path.write_text("FOO=two\nBAR=added\n", encoding="utf-8")
    env_path.chmod(0o600)

    manager = ServiceManager(config)
    payload = manager.plist_payload(formula, spec)
    assert payload["EnvironmentVariables"]["FOO"] == "two"
    assert payload["EnvironmentVariables"]["BAR"] == "added"
    assert 'Environment="FOO=two"' in manager.unit_text(formula, spec)
    # The evaluator's parse-time snapshot and declared env remain untouched.
    assert spec.user_environment_variables == {"FOO": "one"}
    assert spec.environment_variables == {"FOO": "formula"}


@requires_ruby
def test_ruby_render_time_user_env_uses_injectable_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "explicit-config"
    env_path = _write_user_env(user_home, "svc", "FOO=one\n")
    assert os.environ.get(USER_CONFIG_HOME_ENV) is None
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\nenvironment_variables FOO: "formula"'
    )

    spec = evaluate_service_block(formula, config, user_config_home=user_home)
    assert spec.user_config_home == str(user_home)
    env_path.write_text("FOO=two\n", encoding="utf-8")
    env_path.chmod(0o600)

    manager = ServiceManager(config)
    assert (
        manager.plist_payload(formula, spec)["EnvironmentVariables"]["FOO"]
        == "two"
    )

    # A manager-provided context also refreshes render-time resolution.
    manager_with_context = ServiceManager(config, user_config_home=user_home)
    env_path.write_text("FOO=three\n", encoding="utf-8")
    env_path.chmod(0o600)
    assert (
        manager_with_context.plist_payload(formula, spec)[
            "EnvironmentVariables"
        ]["FOO"]
        == "three"
    )


@requires_ruby
def test_ruby_standalone_spec_render_rereads_explicit_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    env_path = _write_user_env(user_home, "svc", "FOO=one\n")
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\nenvironment_variables FOO: "formula"'
    )
    spec = parse_service_block(formula, config)
    assert spec.formula_name == "svc"
    assert spec.user_environment_variables == {"FOO": "one"}

    # Drop the ambient context; the standalone spec can still re-read on
    # demand via an explicit user_config_home.
    monkeypatch.delenv(USER_CONFIG_HOME_ENV, raising=False)
    env_path.write_text("FOO=two\nBAR=added\n", encoding="utf-8")
    env_path.chmod(0o600)

    assert spec.effective_environment_variables(user_config_home=user_home) == {
        "FOO": "two",
        "BAR": "added",
    }
    assert spec.render_environment_variables(
        default_path=str(config.prefix / "bin"), user_config_home=user_home
    ) == {"FOO": "two", "BAR": "added", "PATH": str(config.prefix / "bin")}
    # Without an explicit context the parse-time snapshot is still used.
    assert spec.render_environment_variables() == {"FOO": "one"}
    # No stale mutation of the evaluator's snapshot or declared env.
    assert spec.user_environment_variables == {"FOO": "one"}
    assert spec.environment_variables == {"FOO": "formula"}


def test_user_service_env_bounded_parse_and_security(
    tmp_path: Path,
    non_root: None,
) -> None:
    formula = _formula()
    user_home = tmp_path / "config"
    env_path = _write_user_env(user_home, "svc", "FOO=bar\n")

    assert _read_user_service_env(formula, user_home) == {"FOO": "bar"}
    # Root services never read user overrides.
    assert _read_user_service_env(formula, user_home, euid=0) == {}
    # Group-writable and world-writable files are skipped.
    env_path.chmod(0o664)
    assert _read_user_service_env(formula, user_home) == {}
    env_path.chmod(0o666)
    assert _read_user_service_env(formula, user_home) == {}
    env_path.chmod(0o600)
    assert _read_user_service_env(formula, user_home) == {"FOO": "bar"}
    # Missing directories/files and unset configuration are harmless.
    assert _read_user_service_env(formula, tmp_path / "missing") == {}
    assert _read_user_service_env(formula, None) == {}
    # Formula names are used as a single file name, never as a path.
    traversal = type("FormulaLike", (), {"name": "../escape"})()
    assert _read_user_service_env(traversal, user_home) == {}


@requires_ruby
def test_user_service_env_reaches_effective_helpers_and_to_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    _write_user_env(user_home, "svc", "FOO=override\nPATH=/custom/bin\n")
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))

    spec = evaluate_service_source(
        _body(
            '''
            environment_variables FOO: "formula", KEEP: "yes",
                                  PATH: std_service_path_env
            run [declared_environment_variables["FOO"],
                 effective_environment_variables["FOO"],
                 effective_environment_variables["KEEP"],
                 effective_environment_variables["PATH"],
                 to_plist["EnvironmentVariables"]["FOO"],
                 manual_command]
            '''
        ),
        _formula(),
        config,
    )

    assert spec.run == [
        "formula",
        "override",
        "yes",
        "/custom/bin",
        "override",
        'FOO="override" KEEP="yes"',
    ]
    # Declared values stay untouched while the effective map applies overrides.
    assert spec.environment_variables == {
        "FOO": "formula",
        "KEEP": "yes",
        "PATH": _homebrew_std_service_path_env(config),
    }
    assert spec.user_environment_variables == {
        "FOO": "override",
        "PATH": "/custom/bin",
    }
    assert spec.effective_environment_variables() == {
        "FOO": "override",
        "KEEP": "yes",
        "PATH": "/custom/bin",
    }


@requires_ruby
def test_user_service_env_renders_through_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    _write_user_env(user_home, "svc", "FOO=override\n")
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))
    formula = _formula_with_service(
        '''
        run [opt_bin/"svc"]
        environment_variables FOO: "formula"
        '''
    )

    manager = ServiceManager(config)
    spec = manager.spec_for(formula)
    payload = manager.plist_payload(formula, spec)

    # Declared environment is untouched; effective/render output applies the
    # user override without destructively merging it.
    assert spec.environment_variables == {"FOO": "formula"}
    assert spec.declared_environment_variables() == {"FOO": "formula"}
    assert spec.user_environment_variables == {"FOO": "override"}
    assert spec.effective_environment_variables() == {"FOO": "override"}
    assert payload["EnvironmentVariables"]["FOO"] == "override"
    assert 'Environment="FOO=override"' in manager.unit_text(formula, spec)


@requires_ruby
def test_user_env_effective_values_with_pybrew_path_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv(SERVICE_PATH_MODE_ENV, "pybrew")
    user_home = tmp_path / "user-config"
    _write_user_env(user_home, "svc", "PATH=/user/bin\n")
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))
    formula = _formula_with_service(
        '''
        run [opt_bin/"svc"]
        environment_variables FOO: "formula", PATH: std_service_path_env
        '''
    )

    manager = ServiceManager(config)
    spec = manager.spec_for(formula)

    # pybrew path mode makes the declared std_service_path_env match the
    # literal parser; the render-time effective value still lets the user win.
    assert spec.declared_environment_variables()["PATH"] == str(config.prefix / "bin")
    assert spec.user_environment_variables == {"PATH": "/user/bin"}
    assert spec.effective_environment_variables()["PATH"] == "/user/bin"
    payload = manager.plist_payload(formula, spec)
    assert payload["EnvironmentVariables"]["PATH"] == "/user/bin"


# ---------------------------------------------------------------------------
# literal parser / Ruby evaluator path-mode reconciliation
# ---------------------------------------------------------------------------


def test_service_path_mode_aliases_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SERVICE_PATH_MODE_ENV, raising=False)
    assert service_path_mode("pybrew") == "pybrew"
    assert service_path_mode("homebrew") == "homebrew"

    for value in ("homebrew", "brew", " HomeBrew "):
        monkeypatch.setenv(SERVICE_PATH_MODE_ENV, value)
        assert service_path_mode("pybrew") == "homebrew"

    for value in ("pybrew", "literal", "legacy"):
        monkeypatch.setenv(SERVICE_PATH_MODE_ENV, value)
        assert service_path_mode("homebrew") == "pybrew"

    monkeypatch.setenv(SERVICE_PATH_MODE_ENV, "bogus")
    assert service_path_mode("pybrew") == "pybrew"
    assert service_path_mode("homebrew") == "homebrew"


def test_path_mode_homebrew_applies_to_literal_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv(SERVICE_PATH_MODE_ENV, "homebrew")
    formula = _formula_with_service(
        """
        run [opt_bin/"svc", bin/"svc", lib/"libsvc", etc/"svc.conf",
             var/"v", prefix/"p"]
        log_path "#{etc}/svc.log"
        environment_variables PATH: std_service_path_env
        """
    )

    spec = parse_service_block(formula, config)

    opt_prefix = config.opt_path("svc")
    keg = _keg_prefix(config, formula)
    assert spec.run == [
        str(opt_prefix / "bin" / "svc"),
        str(keg / "bin" / "svc"),
        str(keg / "lib" / "libsvc"),
        str(config.prefix / "etc" / "svc.conf"),
        str(config.prefix / "var" / "v"),
        str(keg / "p"),
    ]
    # Interpolation uses the same mode-selected path map.
    assert spec.log_path == str(config.prefix / "etc" / "svc.log")
    assert spec.environment_variables["PATH"] == _homebrew_std_service_path_env(
        config
    )


def test_path_mode_pybrew_leaves_literal_parser_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv(SERVICE_PATH_MODE_ENV, "pybrew")
    formula = _formula_with_service(
        """
        run [opt_bin/"svc", bin/"svc", lib/"libsvc", etc/"svc.conf"]
        environment_variables PATH: std_service_path_env
        """
    )

    spec = parse_service_block(formula, config)

    opt_prefix = config.opt_path("svc")
    assert spec.run == [
        str(opt_prefix / "bin" / "svc"),
        str(opt_prefix / "bin" / "svc"),
        str(opt_prefix / "lib" / "libsvc"),
        str(opt_prefix / "etc" / "svc.conf"),
    ]
    assert spec.environment_variables["PATH"] == str(config.prefix / "bin")


@requires_ruby
def test_path_mode_pybrew_applies_to_ruby_evaluator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv(SERVICE_PATH_MODE_ENV, "pybrew")
    spec = evaluate_service_source(
        _body(
            '''
            environment_variables BIN: bin, LIB: lib, ETC: etc, VAR: var,
                                  PREFIX: prefix, OPT_BIN: opt_bin,
                                  PATH: std_service_path_env
            '''
        ),
        _formula(),
        config,
    )

    opt_prefix = config.opt_path("svc")
    assert spec.environment_variables == {
        "BIN": str(opt_prefix / "bin"),
        "LIB": str(opt_prefix / "lib"),
        "ETC": str(opt_prefix / "etc"),
        "VAR": str(config.prefix / "var"),
        "PREFIX": str(config.prefix),
        "OPT_BIN": str(opt_prefix / "bin"),
        # pybrew mode keeps the literal parser's reduced value.
        "PATH": str(config.prefix / "bin"),
    }


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


@requires_ruby
def test_service_object_compatibility_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    source = (
        'working_dir var/"lib/svc"\n'
        'run [\n'
        '  default_service_name,\n'
        '  path_dir(working_dir).basename.to_s,\n'
        '  path_dirs.length.to_s,\n'
        '  timed?.to_s,\n'
        '  parse_cron["Minute"].to_s,\n'
        '  cron_weekday_to_systemd_weekday(1),\n'
        '  to_plist["Label"],\n'
        '  self.class.from_hash({"run" => ["x"]}).command.join(",")\n'
        ']\n'
    )
    spec = evaluate_service_source(source, _formula(), config)
    assert spec.run == [
        "sh.brew.svc",
        "svc",
        "1",
        "false",
        "*",
        "Mon",
        "sh.brew.svc",
        "x",
    ]


# ---------------------------------------------------------------------------
# Homebrew parity: platform run form and manual command quoting
# ---------------------------------------------------------------------------


@requires_ruby
@pytest.mark.parametrize(
    "target,expected",
    [
        ("darwin", ["/opt/mac/bin/svc"]),
        ("linux", ["/opt/linux/bin/svc", "--foreground"]),
    ],
)
def test_run_platform_keyword_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    target: str,
    expected: list[str],
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYBREW_TARGET_OS", target)
    spec = evaluate_service_source(
        'run macos: "/opt/mac/bin/svc", '
        'linux: ["/opt/linux/bin/svc", "--foreground"]',
        _formula(),
        config,
    )
    assert spec.run == expected


@requires_ruby
@pytest.mark.parametrize("target", ["darwin", "linux"])
def test_run_platform_missing_selection_leaves_empty_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_ruby: None,
    target: str,
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYBREW_TARGET_OS", target)
    spec = evaluate_service_source(
        'run macos: "/opt/mac/bin/svc"', _formula(), config
    )
    assert spec.run == ([] if target == "linux" else ["/opt/mac/bin/svc"])


@requires_ruby
def test_manual_command_matches_python_service_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    arguments = ["/opt/x y/bin/svc", "--flag", "two words", "~/a b", "quote'x"]
    source = (
        "run " + json.dumps(arguments) + "\n"
        'environment_variables FOO: "a b", KEEP: "yes", PATH: std_service_path_env\n'
        "run [manual_command]\n"
    )
    spec = evaluate_service_source(source, _formula(), config)

    expected = ServiceSpec(
        run=arguments,
        environment_variables={
            "FOO": "a b",
            "KEEP": "yes",
            "PATH": "ignored",
        },
    ).manual_command()
    # Ruby uses Shellwords escaping while Python uses shlex quoting; both must
    # produce a command line with the same shell-visible argument vector.
    import shlex as _shlex

    ruby_tokens = _shlex.split(spec.run[0])
    python_tokens = _shlex.split(expected)
    assert ruby_tokens[:2] == ["FOO=a b", "KEEP=yes"]
    assert python_tokens[:2] == ruby_tokens[:2]
    # Tildes are expanded by both implementations before quoting.
    expanded = [str(Path.home() / "a b") if arg == "~/a b" else arg for arg in arguments]
    assert ruby_tokens[2:] == expanded
    assert python_tokens[2:] == expanded
    assert "PATH" not in spec.run[0]


@requires_ruby
def test_service_object_cron_defaults_and_timed_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    source = (
        'run [parse_cron["Minute"].to_s, timed?.to_s, effective_run_type]\n'
        'cron "@daily"\n'
        "run [parse_cron[\"Minute\"].to_s, parse_cron[\"Hour\"].to_s, "
        "parse_cron[\"Day\"].to_s, timed?.to_s, effective_run_type]\n"
    )
    spec = evaluate_service_source(source, formula, config)
    # The last `run` wins, matching Homebrew's DSL.
    assert spec.run == ["0", "0", "*", "true", "cron"]


# ---------------------------------------------------------------------------
# Canonical labels and option persistence in the Ruby Service object
# ---------------------------------------------------------------------------


@requires_ruby
def test_service_object_canonical_names_with_legacy_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    source = (
        'run [canonical_service_name, default_service_name, canonical_plist_name, '
        'legacy_service_name, legacy_plist_name, pybrew_legacy_service_name, '
        'plist_names.join(","), service_names.join(","), to_plist["Label"]]\n'
    )
    spec = evaluate_service_source(source, _formula(), config)

    assert spec.run == [
        "sh.brew.svc",
        "sh.brew.svc",
        "sh.brew.svc.plist",
        "homebrew.svc",
        "homebrew.mxcl.svc.plist",
        "homebrew.mxcl.svc",
        "sh.brew.svc,homebrew.mxcl.svc,homebrew.svc",
        "sh.brew.svc,homebrew.svc,homebrew.mxcl.svc",
        "sh.brew.svc",
    ]


@requires_ruby
def test_service_object_explicit_name_has_no_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    source = (
        'name macos: "com.example.svc", linux: "custom-svc"\n'
        'run [plist_names.join(","), service_names.join(",")]\n'
    )
    spec = evaluate_service_source(source, _formula(), config)

    assert spec.run == ["com.example.svc", "custom-svc"]


@requires_ruby
def test_service_object_to_hash_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    source = (
        'run ["/bin/svc", "--flag"]\n'
        'run_type :interval\n'
        'interval 30\n'
        'keep_alive successful_exit: true\n'
        'environment_variables FOO: "bar"\n'
        'working_dir var/"lib/svc"\n'
        'launch_only_once true\n'
        'run [\n'
        '  to_hash["run"].join("|"),\n'
        '  to_hash["run_type"],\n'
        '  to_hash["interval"].to_s,\n'
        '  to_hash["keep_alive"]["successful_exit"].to_s,\n'
        '  to_hash["environment_variables"]["FOO"],\n'
        '  to_hash["working_dir"],\n'
        '  to_hash["launch_only_once"].to_s,\n'
        '  self.class.from_hash(to_hash).command.join(",")\n'
        ']\n'
    )
    spec = evaluate_service_source(source, _formula(), config)

    assert spec.run == [
        "/bin/svc|--flag",
        "interval",
        "30",
        "true",
        "bar",
        str(config.prefix / "var" / "lib" / "svc"),
        "true",
        "/bin/svc,--flag",
    ]


@requires_ruby
def test_service_object_from_hash_platform_run_and_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYBREW_TARGET_OS", "linux")
    source = (
        'dsl = self.class.from_hash({"name" => {"macos" => "com.example.svc", '
        '"linux" => "custom-svc"}, "run" => {"macos" => "/opt/mac", '
        '"linux" => ["/opt/linux", "--flag"]}, "cron" => "0 6 * * 1"})\n'
        'run [dsl.command.join("|"), dsl.plist_name, dsl.service_name, '
        'dsl.parse_cron["Hour"].to_s, dsl.cron["Hour"].to_s]\n'
    )
    spec = evaluate_service_source(source, _formula(), config)

    assert spec.run == [
        "/opt/linux|--flag",
        "com.example.svc",
        "custom-svc",
        "6",
        "6",
    ]


@requires_ruby
def test_negative_nice_validation_in_ruby_evaluator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_ruby: None
) -> None:
    config = _config(tmp_path, monkeypatch)

    with pytest.raises(ServiceError, match="require_root"):
        evaluate_service_source('nice -5\nrun [opt_bin/"svc"]', _formula(), config)

    spec = evaluate_service_source(
        'require_root true\nnice -5\nrun [nice.to_s, nice_requires_root?.to_s]',
        _formula(),
        config,
    )
    assert spec.run == ["-5", "false"]
    assert spec.nice == -5
    assert spec.require_root is True

    with pytest.raises(ServiceError, match="between -20 and 19"):
        evaluate_service_source(
            'require_root true\nnice 25\nrun [opt_bin/"svc"]',
            _formula(),
            config,
        )
