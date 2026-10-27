from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from pybrew.errors import PybrewError
from pybrew.models import Formula
from pybrew.paths import Config
from pybrew.ruby_parser import parse_formula_source
from pybrew.services import LaunchctlRunner, ServiceManager, parse_service_block

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
