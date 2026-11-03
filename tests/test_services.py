from __future__ import annotations

import json
import plistlib
import shlex
import textwrap
from pathlib import Path

import pytest

from pybrew.errors import PybrewError
from pybrew.models import Formula
from pybrew.paths import Config
from pybrew.ruby_parser import parse_formula_source
from pybrew.services import (
    SERVICE_PATH_MODE_ENV,
    LaunchctlRunner,
    ServiceError,
    ServiceManager,
    ServiceSpec,
    SystemctlRunner,
    parse_service_block,
)

SERVICE_FORMULA = '''
class Svc < Formula
  desc "service fixture"
  homepage "https://example.com/svc"
  url "https://example.com/svc-1.0.tar.gz"
  sha256 "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  license "MIT"

  service do
    run [opt_bin/"svc", "--foreground"]
    working_dir var/"lib/svc"
    log_path var/"log/svc.log"
    error_log_path var/"log/svc.err.log"
    keep_alive true
    environment_variables PATH: std_service_path_env, FOO: "bar"
  end
end
'''


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("PYBREW_SERVICE_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("PYBREW_SERVICE_BACKEND", "launchd")
    monkeypatch.delenv("HOMEBREW_USER_CONFIG_HOME", raising=False)
    monkeypatch.delenv(SERVICE_PATH_MODE_ENV, raising=False)
    config = Config.default(prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache")
    config.ensure()
    return config


def _formula() -> Formula:
    return parse_formula_source(SERVICE_FORMULA, name="svc")


def _make_fake_launchctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    script = tmp_path / "fake-launchctl"
    log = tmp_path / "launchctl.log"
    script.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$FAKE_LAUNCHCTL_LOG"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_LAUNCHCTL_LOG", str(log))
    return script, log


def _install_fake_keg(config: Config, formula: Formula) -> None:
    keg = config.keg_path(formula.name, formula.pkg_version)
    (keg / "bin").mkdir(parents=True)
    executable = keg / "bin" / "svc"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    opt = config.opt_path(formula.name)
    opt.symlink_to(keg)


def _formula_with_service(body: str) -> Formula:
    source = f'''
class Svc < Formula
  desc "service fixture"
  homepage "https://example.com/svc"
  url "https://example.com/svc-1.0.tar.gz"
  sha256 "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  license "MIT"

  service do
{textwrap.indent(textwrap.dedent(body).strip(), "    ")}
  end
end
'''
    return parse_formula_source(source, name="svc")


def _make_fake_systemctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    script = tmp_path / "fake-systemctl"
    log = tmp_path / "systemctl.log"
    script.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$FAKE_SYSTEMCTL_LOG"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_SYSTEMCTL_LOG", str(log))
    return script, log


def _systemd_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: Config
) -> tuple[ServiceManager, Path]:
    monkeypatch.setenv("PYBREW_SERVICE_BACKEND", "systemd")
    monkeypatch.setenv("PYBREW_SERVICE_DIR", str(tmp_path / "units"))
    script, log = _make_fake_systemctl(tmp_path, monkeypatch)
    return ServiceManager(config, SystemctlRunner(str(script))), log


def test_parse_service_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    spec = parse_service_block(formula, config)
    assert spec.run == [str(config.opt_path("svc") / "bin" / "svc"), "--foreground"]
    assert spec.working_dir == str(config.prefix / "var" / "lib" / "svc")
    assert spec.log_path == str(config.prefix / "var" / "log" / "svc.log")
    assert spec.error_log_path == str(config.prefix / "var" / "log" / "svc.err.log")
    assert spec.keep_alive is True
    assert spec.environment_variables["PATH"] == str(config.prefix / "bin")
    assert spec.environment_variables["FOO"] == "bar"


def test_literal_parser_keeps_pybrew_service_path_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in Ruby evaluator diverges, but the literal parser must not.

    The Ruby evaluator aligns ``bin``/``lib``/``etc`` with Homebrew's formula
    paths; the default literal parser keeps pybrew's formula-opt-prefix
    helpers and its reduced ``std_service_path_env`` value.
    """

    monkeypatch.delenv("PYBREW_ALLOW_SERVICE_RUBY", raising=False)
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        """
        run [opt_bin/"svc", bin/"svc", lib/"libsvc", etc/"svc.conf", prefix/"README"]
        working_dir var/"lib/svc"
        environment_variables PATH: std_service_path_env, PREFIX: prefix
        """
    )

    spec = parse_service_block(formula, config)

    opt_prefix = config.opt_path("svc")
    assert spec.run == [
        str(opt_prefix / "bin" / "svc"),
        str(opt_prefix / "bin" / "svc"),
        str(opt_prefix / "lib" / "libsvc"),
        str(opt_prefix / "etc" / "svc.conf"),
        str(config.prefix / "README"),
    ]
    assert spec.environment_variables["PATH"] == str(config.prefix / "bin")
    assert spec.environment_variables["PREFIX"] == str(config.prefix)


def test_plist_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    _install_fake_keg(config, formula)
    manager = ServiceManager(config)
    spec = manager.spec_for(formula)
    path = manager.write_plist(formula, spec)
    payload = plistlib.loads(path.read_bytes())
    assert payload["Label"] == "homebrew.mxcl.svc"
    assert payload["ProgramArguments"][0].endswith("/opt/svc/bin/svc")
    assert payload["KeepAlive"] is True
    assert payload["RunAtLoad"] is True
    assert payload["EnvironmentVariables"]["FOO"] == "bar"
    assert path.name == "homebrew.mxcl.svc.plist"


def test_start_stop_with_fake_launchctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    _install_fake_keg(config, formula)
    script, log = _make_fake_launchctl(tmp_path, monkeypatch)
    manager = ServiceManager(config, LaunchctlRunner(str(script)))

    path = manager.start(formula)
    assert path.exists()
    assert "bootstrap" in log.read_text()
    assert manager.status(formula) == "started"

    manager.stop(formula)
    assert "bootout" in log.read_text()


def test_start_requires_installed_formula(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    script, _ = _make_fake_launchctl(tmp_path, monkeypatch)
    manager = ServiceManager(config, LaunchctlRunner(str(script)))
    with pytest.raises(PybrewError):
        manager.start(formula)


def test_list_includes_service_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    _install_fake_keg(config, formula)
    script, _ = _make_fake_launchctl(tmp_path, monkeypatch)
    manager = ServiceManager(config, LaunchctlRunner(str(script)))
    manager.start(formula)
    rows = manager.list([formula])
    assert rows and rows[0]["name"] == "svc"
    assert rows[0]["status"] == "started"
    assert rows[0]["run"].endswith("--foreground")


def test_cli_services_start_list_stop(run_cli, sample, tmp_path, monkeypatch) -> None:
    keg = sample.prefix / "Cellar" / "svc" / "1.0"
    (keg / "bin").mkdir(parents=True)
    executable = keg / "bin" / "svc"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    opt = sample.prefix / "opt" / "svc"
    opt.parent.mkdir(parents=True, exist_ok=True)
    opt.symlink_to(keg)

    script = sample.root / "fake-launchctl-cli"
    log = sample.root / "launchctl-cli.log"
    script.write_text('#!/bin/sh\necho "$@" >> "$FAKE_LAUNCHCTL_LOG"\nexit 0\n')
    script.chmod(0o755)
    env = {
        "PYBREW_SERVICE_DIR": str(sample.root / "agents"),
        "PYBREW_LAUNCHCTL": str(script),
        "FAKE_LAUNCHCTL_LOG": str(log),
        "PYBREW_SERVICE_BACKEND": "launchd",
    }
    started = run_cli("services", "start", "svc", extra_env=env)
    assert started.returncode == 0, started.stderr
    assert (sample.root / "agents" / "homebrew.mxcl.svc.plist").is_file()
    assert "bootstrap" in log.read_text()

    listed = run_cli("services", "list", extra_env=env)
    assert listed.returncode == 0
    assert "svc" in listed.stdout and "started" in listed.stdout

    stopped = run_cli("services", "stop", "svc", extra_env=env)
    assert stopped.returncode == 0, stopped.stderr
    assert "bootout" in log.read_text()


# ---------------------------------------------------------------------------
# CLI warning surfacing for unsupported service directives
# ---------------------------------------------------------------------------


def _install_cli_svc_keg(sample) -> None:
    keg = sample.prefix / "Cellar" / "svc" / "1.0"
    (keg / "bin").mkdir(parents=True)
    executable = keg / "bin" / "svc"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    opt = sample.prefix / "opt" / "svc"
    opt.parent.mkdir(parents=True, exist_ok=True)
    opt.symlink_to(keg)


def _cli_launchd_env(sample) -> dict[str, str]:
    script = sample.root / "fake-launchctl-warnings"
    log = sample.root / "launchctl-warnings.log"
    script.write_text('#!/bin/sh\necho "$@" >> "$FAKE_LAUNCHCTL_LOG"\nexit 0\n')
    script.chmod(0o755)
    return {
        "PYBREW_SERVICE_DIR": str(sample.root / "warning-agents"),
        "PYBREW_LAUNCHCTL": str(script),
        "FAKE_LAUNCHCTL_LOG": str(log),
        "PYBREW_SERVICE_BACKEND": "launchd",
    }


def _add_unknown_service_directive(sample) -> None:
    svc_file = sample.tap / "Formula" / "svc.rb"
    source = svc_file.read_text()
    marker = "    keep_alive true"
    assert marker in source
    svc_file.write_text(source.replace(marker, f'{marker}\n    frobnicate "x"', 1))


def test_cli_services_info_json_reports_warnings(run_cli, sample) -> None:
    _add_unknown_service_directive(sample)
    proc = run_cli("services", "info", "svc", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["name"] == "svc"
    assert payload["warnings"] == ["unsupported service directive `frobnicate`"]
    # --json output stays pure JSON: the warning lives inside the payload and
    # is never printed as text on stdout.
    assert "Warning:" not in proc.stdout
    assert proc.stderr == ""


@pytest.mark.parametrize("action", ["start", "stop", "restart", "run"])
def test_cli_services_actions_print_spec_warnings(action, run_cli, sample) -> None:
    _install_cli_svc_keg(sample)
    _add_unknown_service_directive(sample)
    env = _cli_launchd_env(sample)
    proc = run_cli("services", action, "svc", extra_env=env)
    assert proc.returncode == 0, proc.stderr
    assert "Warning: unsupported service directive `frobnicate`" in proc.stderr
    assert "Warning:" not in proc.stdout


def test_cli_services_quiet_when_service_has_no_warnings(run_cli, sample) -> None:
    _install_cli_svc_keg(sample)
    env = _cli_launchd_env(sample)

    info = run_cli("services", "info", "svc", "--json")
    assert info.returncode == 0, info.stderr
    assert json.loads(info.stdout)["warnings"] == []
    assert info.stderr == ""

    started = run_cli("services", "start", "svc", extra_env=env)
    assert started.returncode == 0, started.stderr
    assert started.stderr == ""

    stopped = run_cli("services", "stop", "svc", extra_env=env)
    assert stopped.returncode == 0, stopped.stderr
    assert stopped.stderr == ""


def test_systemd_unit_and_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYBREW_SERVICE_BACKEND", "systemd")
    monkeypatch.setenv("PYBREW_SERVICE_DIR", str(tmp_path / "units"))
    script = tmp_path / "fake-systemctl"
    log = tmp_path / "systemctl.log"
    script.write_text('#!/bin/sh\necho "$@" >> "$FAKE_SYSTEMCTL_LOG"\nexit 0\n')
    script.chmod(0o755)
    monkeypatch.setenv("PYBREW_SYSTEMCTL", str(script))
    monkeypatch.setenv("FAKE_SYSTEMCTL_LOG", str(log))

    config = Config.default(prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache")
    config.ensure()
    formula = _formula()
    _install_fake_keg(config, formula)
    manager = ServiceManager(config)

    path = manager.start(formula)
    assert path.name == "homebrew.mxcl.svc.service"
    unit = path.read_text()
    assert "[Unit]" in unit and "[Service]" in unit
    assert "ExecStart=" in unit
    assert "WorkingDirectory=" in unit
    assert 'Environment="FOO=bar"' in unit
    assert "daemon-reload" in log.read_text()
    assert "start homebrew.mxcl.svc.service" in log.read_text()
    assert manager.status(formula) == "started"

    manager.stop(formula)
    assert "stop homebrew.mxcl.svc.service" in log.read_text()


def test_cleanup_removes_orphan_service_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    _install_fake_keg(config, formula)
    manager = ServiceManager(config)
    manager.directory.mkdir(parents=True, exist_ok=True)
    installed_file = manager.directory / "homebrew.mxcl.svc.plist"
    installed_file.write_text("keep")
    orphan = manager.directory / "homebrew.mxcl.ghost.plist"
    orphan.write_text("orphan")
    removed = manager.cleanup()
    assert orphan in removed
    assert not orphan.exists()
    assert installed_file.exists()


FULL_SERVICE_BODY = """
run [opt_bin/"svc", "--foreground"]
working_dir var/"lib/svc"
log_path var/"log/svc.log"
error_log_path var/"log/svc.err.log"
keep_alive network_state: true, successful_exit: false, path: var/"run/svc.pid", interval: 45
process_type :background
run_type :interval
interval 300
environment_variables PATH: std_service_path_env, FOO: "bar", NUM: 7
require_root true
run_at_load false
"""


def test_parse_extended_service_dsl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(FULL_SERVICE_BODY)
    spec = parse_service_block(formula, config)

    assert spec.run == [str(config.opt_path("svc") / "bin" / "svc"), "--foreground"]
    assert spec.working_dir == str(config.prefix / "var" / "lib" / "svc")
    assert spec.log_path == str(config.prefix / "var" / "log" / "svc.log")
    assert spec.error_log_path == str(config.prefix / "var" / "log" / "svc.err.log")
    assert spec.keep_alive == {
        "network_state": True,
        "successful_exit": False,
        "path": str(config.prefix / "var" / "run" / "svc.pid"),
        "interval": 45,
    }
    assert spec.process_type == "background"
    assert spec.run_type == "interval"
    assert spec.interval == 300
    assert spec.run_at_load is False
    assert spec.require_root is True
    assert spec.environment_variables == {
        "PATH": str(config.prefix / "bin"),
        "FOO": "bar",
        "NUM": "7",
    }


def test_parse_keep_alive_hash_forms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, monkeypatch)
    cases = {
        "keep_alive network_state: true": {"network_state": True},
        "keep_alive successful_exit: false": {"successful_exit": False},
        "keep_alive crashed: true": {"crashed": True},
        "keep_alive always: true": {"always": True},
        "keep_alive interval: 60": {"interval": 60},
        "keep_alive network_state: true, successful_exit: true": {
            "network_state": True,
            "successful_exit": True,
        },
    }
    for directive, expected in cases.items():
        formula = _formula_with_service(f'run [opt_bin/"svc"]\n{directive}')
        spec = parse_service_block(formula, config)
        assert spec.keep_alive == expected, directive

    formula = _formula_with_service(
        'run [opt_bin/"svc"]\nkeep_alive path: var/"run/svc.pid"'
    )
    assert parse_service_block(formula, config).keep_alive == {
        "path": str(config.prefix / "var" / "run" / "svc.pid")
    }


def test_parse_keep_alive_boolean_forms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, monkeypatch)
    for directive, expected in (("keep_alive true", True), ("keep_alive false", False)):
        formula = _formula_with_service(f'run [opt_bin/"svc"]\n{directive}')
        assert parse_service_block(formula, config).keep_alive is expected


def test_parse_hash_literal_and_rocket_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        """
        run [opt_bin/"svc"]
        keep_alive({ network_state: true })
        environment_variables({ "FOO" => "bar", :BAZ => "qux" })
        """
    )
    spec = parse_service_block(formula, config)
    assert spec.keep_alive == {"network_state": True}
    assert spec.environment_variables["FOO"] == "bar"
    assert spec.environment_variables["BAZ"] == "qux"


def test_plist_renders_extended_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(FULL_SERVICE_BODY)
    manager = ServiceManager(config)
    spec = manager.spec_for(formula)
    payload = manager.plist_payload(formula, spec)

    assert payload["Label"] == "homebrew.mxcl.svc"
    assert payload["RunAtLoad"] is False
    assert payload["KeepAlive"] == {
        "NetworkState": True,
        "SuccessfulExit": False,
        "PathState": str(config.prefix / "var" / "run" / "svc.pid"),
    }
    assert payload["ThrottleInterval"] == 45
    assert payload["StartInterval"] == 300
    assert payload["ProcessType"] == "Background"
    assert payload["WorkingDirectory"] == str(config.prefix / "var" / "lib" / "svc")
    assert payload["StandardOutPath"] == str(config.prefix / "var" / "log" / "svc.log")
    assert payload["StandardErrorPath"] == str(config.prefix / "var" / "log" / "svc.err.log")
    assert payload["EnvironmentVariables"]["FOO"] == "bar"
    assert payload["EnvironmentVariables"]["NUM"] == "7"
    assert payload["EnvironmentVariables"]["PATH"] == str(config.prefix / "bin")
    # require_root is stored on the spec but never changes the plist user.
    assert "User" not in payload and "UserName" not in payload


def test_plist_keep_alive_boolean_and_interval_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    manager = ServiceManager(config)

    enabled = manager.plist_payload(
        _formula_with_service('run [opt_bin/"svc"]\nkeep_alive true'),
        manager.spec_for(_formula_with_service('run [opt_bin/"svc"]\nkeep_alive true')),
    )
    assert enabled["KeepAlive"] is True
    assert "ThrottleInterval" not in enabled

    disabled_formula = _formula_with_service('run [opt_bin/"svc"]\nkeep_alive false')
    disabled = manager.plist_payload(disabled_formula, manager.spec_for(disabled_formula))
    assert "KeepAlive" not in disabled

    interval_formula = _formula_with_service(
        'run [opt_bin/"svc"]\nkeep_alive interval: 30'
    )
    interval = manager.plist_payload(interval_formula, manager.spec_for(interval_formula))
    assert interval["KeepAlive"] is True
    assert interval["ThrottleInterval"] == 30


def test_plist_sockets_legacy_timers_and_launch_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        """
        run [opt_bin/"svc"]
        sockets http: "tcp://127.0.0.1:8080", https: "udp://[::1]:5353"
        macos_legacy_timers true
        launch_only_once true
        """
    )
    manager = ServiceManager(config)
    spec = manager.spec_for(formula)
    assert spec.sockets["http"] == {"host": "127.0.0.1", "port": "8080", "type": "tcp"}
    assert spec.sockets["https"] == {"host": "::1", "port": "5353", "type": "udp"}
    assert spec.macos_legacy_timers is True
    assert spec.launch_only_once is True

    payload = manager.plist_payload(formula, spec)
    assert payload["Sockets"]["http"] == {
        "SockNodeName": "127.0.0.1",
        "SockServiceName": "8080",
        "SockProtocol": "TCP",
    }
    assert payload["Sockets"]["https"] == {
        "SockNodeName": "::1",
        "SockServiceName": "5353",
        "SockProtocol": "UDP",
    }
    assert payload["LegacyTimers"] is True
    assert payload["LaunchOnlyOnce"] is True


def test_plist_single_string_socket_uses_listeners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\nsockets "tcp://0.0.0.0:80"'
    )
    manager = ServiceManager(config)
    payload = manager.plist_payload(formula, manager.spec_for(formula))
    assert payload["Sockets"] == {
        "listeners": {
            "SockNodeName": "0.0.0.0",
            "SockServiceName": "80",
            "SockProtocol": "TCP",
        }
    }


def test_systemd_unit_renders_extended_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        """
        run [opt_bin/"svc", "--foreground"]
        working_dir var/"lib/svc"
        log_path var/"log/svc.log"
        error_log_path var/"log/svc.err.log"
        keep_alive successful_exit: false
        throttle_interval 20
        restart_delay 7
        stop_timeout 9
        root_dir var/"root"
        input_path var/"in"
        nice 5
        launch_only_once true
        environment_variables FOO: "bar"
        """
    )
    manager, _ = _systemd_manager(tmp_path, monkeypatch, config)
    unit = manager.unit_text(formula, manager.spec_for(formula))

    assert "[Unit]" in unit and "[Service]" in unit
    assert "Type=oneshot" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=7" in unit
    assert "TimeoutStopSec=9" in unit
    assert f"RootDirectory={config.prefix / 'var' / 'root'}" in unit
    assert f"StandardInput=file:{config.prefix / 'var' / 'in'}" in unit
    assert "Nice=5" in unit
    assert "ExecStart=" in unit
    assert f"WorkingDirectory={config.prefix / 'var' / 'lib' / 'svc'}" in unit
    assert f"StandardOutput=append:{config.prefix / 'var' / 'log' / 'svc.log'}" in unit
    assert (
        f"StandardError=append:{config.prefix / 'var' / 'log' / 'svc.err.log'}" in unit
    )
    assert 'Environment="FOO=bar"' in unit


@pytest.mark.parametrize(
    "keep_alive,restart",
    [
        ("true", "always"),
        ("false", "no"),
        ("successful_exit: true", "on-success"),
        ("successful_exit: false", "on-failure"),
        ("network_state: true", "on-failure"),
        ("crashed: true", "on-failure"),
        ("interval: 30", "always"),
    ],
)
def test_systemd_keep_alive_restart_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keep_alive: str,
    restart: str,
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(f'run [opt_bin/"svc"]\nkeep_alive {keep_alive}')
    manager, _ = _systemd_manager(tmp_path, monkeypatch, config)
    unit = manager.unit_text(formula, manager.spec_for(formula))
    assert f"Restart={restart}" in unit
    if keep_alive == "interval: 30":
        assert "RestartSec=30" in unit


def test_systemd_start_stop_with_fake_runner_extended_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        """
        run [opt_bin/"svc"]
        keep_alive network_state: true
        launch_only_once true
        """
    )
    _install_fake_keg(config, formula)
    manager, log = _systemd_manager(tmp_path, monkeypatch, config)

    path = manager.start(formula)
    unit = path.read_text()
    assert path.name == "homebrew.mxcl.svc.service"
    assert "Type=oneshot" in unit
    assert "Restart=on-failure" in unit
    assert "daemon-reload" in log.read_text()
    assert "start homebrew.mxcl.svc.service" in log.read_text()
    assert manager.status(formula) == "started"

    manager.stop(formula)
    assert "stop homebrew.mxcl.svc.service" in log.read_text()


@pytest.mark.parametrize(
    "body,match",
    [
        ("keep_alive bogus: true", "keep_alive"),
        ('keep_alive successful_exit: "maybe"', "successful_exit"),
        ('sockets "not-a-socket"', "sockets"),
        ('sockets "tcp://example.com:80"', "IPv4"),
        ('sockets "icmp://127.0.0.1:1"', "protocol"),
        ("process_type :daemon", "process_type"),
        ('interval "soon"', "interval"),
        ('run_at_load "maybe"', "run_at_load"),
    ],
)
def test_invalid_service_dsl_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    match: str,
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(f'run [opt_bin/"svc"]\n{body}')
    with pytest.raises(ServiceError, match=match):
        parse_service_block(formula, config)


def test_parse_multiline_keyword_and_parenthesized_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        """
        run [opt_bin/"svc"]
        keep_alive network_state: true,
                   successful_exit: false
        environment_variables(
          PATH: std_service_path_env,
          FOO: "bar"
        )
        """
    )
    spec = parse_service_block(formula, config)
    assert spec.keep_alive == {"network_state": True, "successful_exit": False}
    assert spec.environment_variables["FOO"] == "bar"


def test_service_cron_renders_launchd_calendar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\ncron "@daily"'
    )
    manager = ServiceManager(config)
    spec = manager.spec_for(formula)
    assert spec.cron["Minute"] == 0
    assert spec.cron["Hour"] == 0
    payload = manager.plist_payload(formula, spec)
    assert payload["StartCalendarInterval"] == {
        "Minute": 0,
        "Hour": 0,
        "Day": "*",
        "Month": "*",
        "Weekday": "*",
    }


def test_systemd_cron_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\ncron "@daily"'
    )
    manager, _ = _systemd_manager(tmp_path, monkeypatch, config)
    spec = manager.spec_for(formula)
    timer = manager.timer_text(formula, spec)
    assert "OnCalendar=*-*-* 00:00:00" in timer
    assert "WantedBy=timers.target" in timer
    path = manager.write_timer(formula, spec)
    assert path == manager.timer_path(formula)
    assert path.is_file()


def test_systemd_socket_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\nsockets "tcp://127.0.0.1:8080"'
    )
    manager, _ = _systemd_manager(tmp_path, monkeypatch, config)
    spec = manager.spec_for(formula)
    text = manager.socket_text(formula, spec)
    assert "ListenStream=127.0.0.1:8080" in text
    assert "WantedBy=sockets.target" in text
    path = manager.write_socket(formula, spec)
    assert path == manager.socket_path(formula)
    assert path.is_file()


def test_service_run_type_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    spec = parse_service_block(
        _formula_with_service('run_type :cron\ncron "0 0 * * *"'), config
    )
    assert spec.run_type == "cron"

    with pytest.raises(ServiceError, match="run_type"):
        parse_service_block(_formula_with_service("run_type :bogus"), config)


def test_service_custom_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\n'
        'name macos: "com.example.svc", linux: "custom-svc"'
    )
    spec = parse_service_block(formula, config)
    assert spec.plist_name == "com.example.svc"
    assert spec.service_name == "custom-svc"

    manager = ServiceManager(config)
    assert manager.label(formula, spec) == "com.example.svc"
    assert manager.plist_path(formula, spec).name == "com.example.svc.plist"

    manager.backend = "systemd"
    assert manager.label(formula, spec) == "custom-svc"
    assert manager.unit_path(formula, spec).name == "custom-svc.service"


def test_unknown_service_directive_is_warned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service('run [opt_bin/"svc"]\nfrobnicate "x"')
    spec = parse_service_block(formula, config)
    assert spec.warnings == ["unsupported service directive `frobnicate`"]


def test_service_spec_helper_methods(tmp_path: Path) -> None:
    spec = ServiceSpec(
        run=["/opt/example/bin/svc", "--flag"],
        keep_alive={"successful_exit": True},
        cron={"minute": 5, "hour": "6", "weekday": 7},
        environment_variables={"FOO": "bar"},
        require_root=True,
        nice=5,
        interval=30,
    )
    prefix = tmp_path / "prefix"
    assert spec.command() == ["/opt/example/bin/svc", "--flag"]
    assert spec.command_p() is True
    assert spec.manual_command() == spec.command()
    assert spec.keep_alive_p() is True
    assert spec.timed_p() is True
    assert spec.requires_root_p() is True
    assert spec.nice_requires_root_p() is True
    assert spec.default_service_name("svc") == "homebrew.mxcl.svc"
    assert spec.default_plist_name("svc") == "homebrew.mxcl.svc.plist"
    assert spec.path_dir(prefix) == prefix / "bin"
    assert spec.path_dirs(prefix) == [prefix / "bin", prefix / "sbin"]
    assert spec.path_parent_dir(prefix) == tmp_path
    assert spec.declared_environment_variables() == {"FOO": "bar"}
    # Homebrew-style effective environment: the prefix argument is accepted for
    # compatibility but no longer synthesises PATH.
    env = spec.effective_environment_variables(prefix)
    assert env == {"FOO": "bar"}
    assert spec.effective_environment_variables() == {"FOO": "bar"}
    assert spec.parse_cron() == {
        "Minute": 5,
        "Hour": 6,
        "Day": 1,
        "Month": 1,
        "Weekday": 7,
    }
    assert ServiceSpec.cron_weekday_to_systemd_weekday(1) == "Mon"
    assert ServiceSpec.cron_weekday_to_systemd_weekday("Sun") == "Sun"
    restored = ServiceSpec.from_hash({"run": ["x"], "unknown": "ignored"})
    assert restored.run == ["x"]


def test_service_spec_declared_and_effective_environment_separation(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "prefix"
    spec = ServiceSpec(
        environment_variables={"FOO": "declared", "PATH": "/declared/bin"},
        user_environment_variables={"FOO": "user", "BAR": "added"},
    )

    # The declared map is untouched by user overrides.
    assert spec.declared_environment_variables() == {
        "FOO": "declared",
        "PATH": "/declared/bin",
    }
    assert spec.environment_variables == {
        "FOO": "declared",
        "PATH": "/declared/bin",
    }
    # Effective values merge overrides with user values winning; the legacy
    # prefix argument does not synthesise PATH.
    effective = spec.effective_environment_variables(prefix)
    assert effective == {
        "FOO": "user",
        "PATH": "/declared/bin",
        "BAR": "added",
    }
    assert spec.effective_environment_variables() == effective
    assert spec.user_environment == {"FOO": "user", "BAR": "added"}
    # Returned dicts are copies: mutating them cannot corrupt the spec.
    effective["NEW"] = "value"
    assert "NEW" not in spec.effective_environment_variables()

    restored = ServiceSpec.from_hash(
        {
            "environment_variables": {"A": "declared"},
            "user_environment": {"A": "user"},
        }
    )
    assert restored.declared_environment_variables() == {"A": "declared"}
    assert restored.effective_environment_variables() == {"A": "user"}

    empty = ServiceSpec()
    assert empty.declared_environment_variables() == {}
    assert empty.effective_environment_variables() == {}
    assert empty.user_environment_variables == {}


def test_service_manager_render_wrappers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    manager = ServiceManager(config)
    formula = _formula()
    spec = manager.spec_for(formula)
    payload = manager.to_plist(formula)
    assert payload["Label"] == "homebrew.mxcl.svc"
    assert payload["ProgramArguments"] == spec.run
    unit = manager.to_systemd_unit(formula)
    assert "[Service]" in unit


def test_rendering_applies_user_environment_without_mutating_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    manager = ServiceManager(config)
    formula = _formula()
    spec = ServiceSpec(
        run=["/opt/svc/bin/svc"],
        environment_variables={"FOO": "declared", "KEEP": "yes"},
        user_environment_variables={"FOO": "override", "BAR": "added"},
    )

    payload = manager.plist_payload(formula, spec)
    assert payload["EnvironmentVariables"] == {
        "FOO": "override",
        "KEEP": "yes",
        "BAR": "added",
        "PATH": str(config.prefix / "bin"),
    }

    unit = manager.unit_text(formula, spec)
    assert 'Environment="FOO=override"' in unit
    assert 'Environment="KEEP=yes"' in unit
    assert 'Environment="BAR=added"' in unit
    assert 'Environment="FOO=declared"' not in unit

    # Rendering never merges overrides into the declared environment.
    assert spec.environment_variables == {"FOO": "declared", "KEEP": "yes"}
    assert spec.declared_environment_variables() == {
        "FOO": "declared",
        "KEEP": "yes",
    }
    assert spec.user_environment_variables == {"FOO": "override", "BAR": "added"}

    # A user-provided PATH wins over pybrew's default search path.
    path_spec = ServiceSpec(
        run=["/opt/svc/bin/svc"],
        user_environment_variables={"PATH": "/user/bin"},
    )
    path_payload = manager.plist_payload(formula, path_spec)
    assert path_payload["EnvironmentVariables"]["PATH"] == "/user/bin"


# ---------------------------------------------------------------------------
# working_dir / root_dir / input_path / log rendering edges
# ---------------------------------------------------------------------------


def test_plist_and_unit_render_root_and_input_paths_with_home_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    manager = ServiceManager(config)
    formula = _formula()
    spec = ServiceSpec(
        run=["/opt/svc/bin/svc"],
        root_dir="~/root",
        input_path="~/stdin",
        log_path="~/out.log",
        error_log_path="~/err.log",
    )

    payload = manager.plist_payload(formula, spec)
    assert payload["WorkingDirectory"] == str(home / "root")
    assert payload["RootDirectory"] == str(home / "root")
    assert payload["StandardInPath"] == str(home / "stdin")
    assert payload["StandardOutPath"] == str(home / "out.log")
    assert payload["StandardErrorPath"] == str(home / "err.log")

    unit = manager.unit_text(formula, spec)
    assert f"RootDirectory={home / 'root'}" in unit
    assert f"StandardInput=file:{home / 'stdin'}" in unit
    assert f"WorkingDirectory={home / 'root'}" in unit
    assert f"StandardOutput=append:{home / 'out.log'}" in unit
    assert f"StandardError=append:{home / 'err.log'}" in unit


def test_working_dir_takes_precedence_over_root_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    manager = ServiceManager(config)
    formula = _formula()
    spec = ServiceSpec(
        run=["/opt/svc/bin/svc"],
        working_dir="~/work",
        root_dir="~/root",
    )

    payload = manager.plist_payload(formula, spec)
    assert payload["WorkingDirectory"] == str(home / "work")
    assert payload["RootDirectory"] == str(home / "root")

    unit = manager.unit_text(formula, spec)
    assert f"WorkingDirectory={home / 'work'}" in unit
    assert f"RootDirectory={home / 'root'}" in unit


def test_default_log_paths_when_service_paths_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    manager = ServiceManager(config)
    formula = _formula()
    spec = ServiceSpec(run=["/opt/svc/bin/svc"])
    default_log = str(config.prefix / "var" / "log" / "svc.log")

    payload = manager.plist_payload(formula, spec)
    assert payload["StandardOutPath"] == default_log
    assert payload["StandardErrorPath"] == default_log
    assert "WorkingDirectory" not in payload
    assert "RootDirectory" not in payload
    assert "StandardInPath" not in payload

    unit = manager.unit_text(formula, spec)
    assert f"StandardOutput=append:{default_log}" in unit
    assert f"StandardError=append:{default_log}" in unit
    assert "WorkingDirectory=" not in unit
    assert "RootDirectory=" not in unit
    assert "StandardInput=" not in unit


def test_only_one_custom_log_path_keeps_the_other_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    manager = ServiceManager(config)
    formula = _formula()
    default_log = str(config.prefix / "var" / "log" / "svc.log")
    spec = ServiceSpec(run=["/opt/svc/bin/svc"], log_path="~/custom.out")

    payload = manager.plist_payload(formula, spec)
    assert payload["StandardOutPath"] == str(home / "custom.out")
    assert payload["StandardErrorPath"] == default_log

    unit = manager.unit_text(formula, spec)
    assert f"StandardOutput=append:{home / 'custom.out'}" in unit
    assert f"StandardError=append:{default_log}" in unit
    assert f"ExecStart={shlex.join(spec.run)}" in unit
