from __future__ import annotations

from pathlib import Path


def _brewfile(tmp_path: Path) -> Path:
    path = tmp_path / "Brewfile"
    path.write_text(
        '# test Brewfile\n'
        'brew "hello"\n'
        'cask "testapp"\n'
    )
    return path


def test_bundle_install_check_list(run_cli, sample, tmp_path) -> None:
    brewfile = _brewfile(tmp_path)
    check = run_cli("bundle", "check", "--file", str(brewfile))
    assert check.returncode == 1
    assert "Missing:" in check.stdout

    dry = run_cli("bundle", "install", "--dry-run", "--file", str(brewfile), bottle_tag=sample.tag)
    assert dry.returncode == 0, dry.stderr
    assert "Would install: brew hello" in dry.stdout
    assert "Would install: cask testapp" in dry.stdout
    assert not (sample.prefix / "Cellar" / "hello").exists()

    installed = run_cli("bundle", "install", "--file", str(brewfile), bottle_tag=sample.tag)
    assert installed.returncode == 0, installed.stderr + installed.stdout
    assert (sample.prefix / "Cellar" / "hello" / "1.0").is_dir()
    assert (sample.prefix / "Applications" / "Test.app").is_dir()

    check = run_cli("bundle", "check", "--file", str(brewfile))
    assert check.returncode == 0, check.stdout
    assert "satisfied" in check.stdout

    listing = run_cli("bundle", "list", "--file", str(brewfile))
    assert listing.returncode == 0
    assert "brew hello" in listing.stdout
    assert "cask testapp" in listing.stdout


def test_bundle_rejects_unknown_lines(run_cli, sample, tmp_path) -> None:
    path = tmp_path / "Brewfile"
    path.write_text('not_a_brewfile_line "x"\n')
    proc = run_cli("bundle", "list", "--file", str(path))
    assert proc.returncode == 1
    assert "unsupported brewfile line" in proc.stderr.lower()


def test_bundle_dump(run_cli, sample, tmp_path) -> None:
    brewfile = _brewfile(tmp_path)
    assert run_cli("bundle", "install", "--file", str(brewfile), bottle_tag=sample.tag).returncode == 0
    dump = tmp_path / "Brewfile.dump"
    proc = run_cli("bundle", "dump", "--file", str(dump))
    assert proc.returncode == 0, proc.stderr
    text = dump.read_text()
    assert 'brew "hello"' in text
    assert 'cask "testapp"' in text
    again = run_cli("bundle", "dump", "--file", str(dump))
    assert again.returncode == 1
    forced = run_cli("bundle", "dump", "--file", str(dump), "--force")
    assert forced.returncode == 0, forced.stderr


def test_bundle_link_false(run_cli, sample, tmp_path) -> None:
    brewfile = tmp_path / "Brewfile"
    brewfile.write_text('brew "hello", link: false\n')
    proc = run_cli("bundle", "install", "--file", str(brewfile), bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (sample.prefix / "Cellar" / "hello" / "1.0").is_dir()
    assert not (sample.prefix / "bin" / "hello").exists()
    assert not (sample.prefix / "opt" / "hello").exists()


def test_bundle_tap_url(run_cli, sample, tmp_path) -> None:
    import subprocess

    remote = tmp_path / "demo-remote"
    (remote / "Formula").mkdir(parents=True)
    (remote / "Formula" / "hello.rb").write_text('class Hello < Formula\nend\n')
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(remote), *args], check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
    brewfile = tmp_path / "Brewfile"
    brewfile.write_text(f'tap "tzhazuma/demo", url: "{remote.as_uri()}"\n')
    proc = run_cli("bundle", "install", "--file", str(brewfile))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "tap tzhazuma/demo" in proc.stdout
    assert (sample.prefix / "Library" / "Taps" / "tzhazuma" / "homebrew-demo" / ".git").is_dir()


def _write_executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)
    return path


def _fake_mas(tmp_path: Path, listings: list[str], log: Path) -> Path:
    quoted = " ".join(f"'{line}'" for line in listings)
    body = (
        'if [ "$1" = "list" ]; then\n'
        f"  printf '%s\\n' {quoted}\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "install" ]; then\n'
        f'  printf "%s\\n" "$2" >> "{log}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "unexpected mas arguments: $*" >&2\n'
        "exit 2\n"
    )
    return _write_executable(tmp_path / "fake-mas", body)


def _fake_code(tmp_path: Path, extensions: list[str], log: Path) -> Path:
    quoted = " ".join(f"'{extension}'" for extension in extensions)
    body = (
        'if [ "$1" = "--list-extensions" ]; then\n'
        f"  printf '%s\\n' {quoted}\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "--install-extension" ]; then\n'
        f'  printf "%s\\n" "$2" >> "{log}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "unexpected code arguments: $*" >&2\n'
        "exit 2\n"
    )
    return _write_executable(tmp_path / "fake-code", body)


def test_bundle_parse_brew_args_and_external_options(tmp_path) -> None:
    from pybrew.bundle import parse_brewfile

    brewfile = tmp_path / "Brewfile"
    brewfile.write_text(
        'mas "Example App", id: 123\n'
        'vscode "publisher.extension"\n'
        'brew "hello", link: false, args: ["--HEAD", "--build-from-source"]\n'
    )
    entries = parse_brewfile(brewfile)

    assert entries[0].kind == "mas"
    assert entries[0].name == "Example App"
    assert entries[0].options == {"id": "123"}
    assert entries[1].kind == "vscode"
    assert entries[1].options == {}
    assert entries[2].kind == "brew"
    assert entries[2].options["link"] == "false"
    assert "--HEAD" in entries[2].options["args"]
    assert "--build-from-source" in entries[2].options["args"]


def test_bundle_brew_args_apply_install_options(monkeypatch, sample, tmp_path) -> None:
    from pybrew import bundle as bundle_module
    from pybrew.bundle import bundle_install, parse_brewfile
    from pybrew.paths import Config

    brewfile = tmp_path / "Brewfile"
    brewfile.write_text(
        'brew "hello", args: ["--HEAD", "--build-from-source", "--unknown-flag"]\n'
    )
    entries = parse_brewfile(brewfile)
    captured = []

    class RecordingInstaller:
        def __init__(self, config, loader) -> None:
            self.config = config
            self.loader = loader

        def install(self, formula, options):
            captured.append((formula.name, options))
            return []

    monkeypatch.setattr(bundle_module, "Installer", RecordingInstaller)
    config = Config.default(prefix=sample.prefix, taps=[sample.tap], cache=sample.cache)
    installed, errors = bundle_install(config, entries)

    assert errors == []
    assert installed == ["brew hello"]
    assert len(captured) == 1
    name, options = captured[0]
    assert name == "hello"
    assert options.head is True
    assert options.build_from_source is True
    assert options.link is True


def test_bundle_mas_and_vscode_check(run_cli, sample, tmp_path) -> None:
    env = {
        "PYBREW_MAS": str(_fake_mas(tmp_path, ["123  Example App 1.0"], tmp_path / "mas.log")),
        "PYBREW_CODE": str(
            _fake_code(tmp_path, ["publisher.extension"], tmp_path / "code.log")
        ),
    }
    present = tmp_path / "present.Brewfile"
    present.write_text('mas "Example App", id: 123\nvscode "publisher.extension"\n')

    check = run_cli("bundle", "check", "--file", str(present), extra_env=env)
    assert check.returncode == 0, check.stderr + check.stdout
    assert "satisfied" in check.stdout

    missing = tmp_path / "missing.Brewfile"
    missing.write_text('mas "Other App", id: 999\nvscode "other.extension"\n')
    check = run_cli("bundle", "check", "--file", str(missing), extra_env=env)
    assert check.returncode == 1
    assert "Missing: mas Other App" in check.stdout
    assert "Missing: vscode other.extension" in check.stdout

    listing = run_cli("bundle", "list", "--file", str(present), extra_env=env)
    assert listing.returncode == 0
    assert "mas Example App" in listing.stdout
    assert "vscode publisher.extension" in listing.stdout


def test_bundle_mas_and_vscode_install(run_cli, sample, tmp_path) -> None:
    mas_log = tmp_path / "mas.log"
    code_log = tmp_path / "code.log"
    env = {
        "PYBREW_MAS": str(_fake_mas(tmp_path, ["123  Example App 1.0"], mas_log)),
        "PYBREW_CODE": str(_fake_code(tmp_path, ["publisher.extension"], code_log)),
    }
    brewfile = tmp_path / "Brewfile"
    brewfile.write_text('mas "Example App", id: 123\nvscode "publisher.extension"\n')

    dry = run_cli("bundle", "install", "--dry-run", "--file", str(brewfile), extra_env=env)
    assert dry.returncode == 0, dry.stderr + dry.stdout
    assert "Would install: mas Example App" in dry.stdout
    assert "Would install: vscode publisher.extension" in dry.stdout
    assert not mas_log.exists()
    assert not code_log.exists()

    proc = run_cli("bundle", "install", "--file", str(brewfile), extra_env=env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert mas_log.read_text().splitlines() == ["123"]
    assert code_log.read_text().splitlines() == ["publisher.extension"]
    assert "Installed: mas Example App" in proc.stdout
    assert "Installed: vscode publisher.extension" in proc.stdout


def test_bundle_external_tool_unavailable_is_error(run_cli, sample, tmp_path) -> None:
    brewfile = tmp_path / "Brewfile"
    brewfile.write_text('mas "Example App", id: 123\nvscode "publisher.extension"\n')
    env = {
        "PYBREW_MAS": str(tmp_path / "missing-mas"),
        "PYBREW_CODE": str(tmp_path / "missing-code"),
    }

    check = run_cli("bundle", "check", "--file", str(brewfile), extra_env=env)
    assert check.returncode == 1
    assert "mas executable not found" in check.stderr
    assert "PYBREW_MAS" in check.stderr

    install = run_cli("bundle", "install", "--file", str(brewfile), extra_env=env)
    assert install.returncode == 1
    assert "Error: Example App:" in install.stderr
    assert "mas executable not found" in install.stderr
    assert "Error: publisher.extension:" in install.stderr
    assert "code executable not found" in install.stderr


def test_bundle_mas_install_requires_id(run_cli, sample, tmp_path) -> None:
    mas = _fake_mas(tmp_path, ["123  Example App 1.0"], tmp_path / "mas.log")
    brewfile = tmp_path / "Brewfile"
    brewfile.write_text('mas "Example App"\n')
    proc = run_cli(
        "bundle", "install", "--file", str(brewfile), extra_env={"PYBREW_MAS": str(mas)}
    )
    assert proc.returncode == 1
    assert "requires id" in proc.stderr


def test_bundle_formula_from_newly_installed_tap(monkeypatch, sample, tmp_path) -> None:
    import subprocess

    from pybrew import bundle as bundle_module
    from pybrew.bundle import bundle_install, parse_brewfile
    from pybrew.paths import Config

    remote = tmp_path / "refresh-remote"
    (remote / "Formula").mkdir(parents=True)
    (remote / "Formula" / "fresh.rb").write_text(
        'class Fresh < Formula\n'
        '  desc "fresh fixture"\n'
        '  homepage "https://example.com/fresh"\n'
        '  url "https://example.com/fresh-1.0.tar.gz"\n'
        '  sha256 "0000000000000000000000000000000000000000000000000000000000000000"\n'
        'end\n'
    )

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(remote), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")

    brewfile = tmp_path / "Brewfile"
    brewfile.write_text(f'tap "tzhazuma/refresh", url: "{remote.as_uri()}"\nbrew "fresh"\n')
    entries = parse_brewfile(brewfile)
    captured = []

    class RecordingInstaller:
        def __init__(self, config, loader) -> None:
            self.config = config
            self.loader = loader

        def install(self, formula, options):
            captured.append(formula.name)
            return []

    monkeypatch.setattr(bundle_module, "Installer", RecordingInstaller)
    config = Config.default(prefix=sample.prefix, taps=[sample.tap], cache=sample.cache)
    installed, errors = bundle_install(config, entries)

    assert errors == []
    assert installed == ["tap tzhazuma/refresh", "brew fresh"]
    assert captured == ["fresh"]


def _fake_launchctl(tmp_path: Path, monkeypatch, *, exit_code: int = 0) -> Path:
    script = tmp_path / "fake-launchctl"
    log = tmp_path / "launchctl.log"
    script.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$FAKE_LAUNCHCTL_LOG"\n'
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_LAUNCHCTL_LOG", str(log))
    return script


def _service_env(tmp_path: Path, launchctl: Path) -> dict[str, str]:
    return {
        "PYBREW_SERVICE_DIR": str(tmp_path / "agents"),
        "PYBREW_SERVICE_BACKEND": "launchd",
        "PYBREW_LAUNCHCTL": str(launchctl),
    }


def _stage_installed_service_keg(config, formula) -> None:
    """Simulate a completed install for the service fixture formula."""

    keg = config.keg_path(formula.name, formula.pkg_version)
    (keg / "bin").mkdir(parents=True, exist_ok=True)
    executable = keg / "bin" / formula.name
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    opt = config.opt_path(formula.name)
    opt.parent.mkdir(parents=True, exist_ok=True)
    opt.symlink_to(keg)


def test_bundle_restart_service_starts_service(monkeypatch, sample, tmp_path) -> None:
    from pybrew import bundle as bundle_module
    from pybrew.bundle import bundle_install, parse_brewfile
    from pybrew.paths import Config

    brewfile = tmp_path / "Brewfile"
    brewfile.write_text('brew "svc", restart_service: true\n')
    launchctl = _fake_launchctl(tmp_path, monkeypatch)
    for name, value in _service_env(tmp_path, launchctl).items():
        monkeypatch.setenv(name, value)

    class InstallingInstaller:
        def __init__(self, config, loader) -> None:
            self.config = config
            self.loader = loader

        def install(self, formula, options):
            _stage_installed_service_keg(self.config, formula)
            return []

    monkeypatch.setattr(bundle_module, "Installer", InstallingInstaller)
    config = Config.default(prefix=sample.prefix, taps=[sample.tap], cache=sample.cache)

    installed, errors = bundle_install(config, parse_brewfile(brewfile))

    assert errors == []
    assert installed == ["brew svc"]
    log = tmp_path / "launchctl.log"
    assert log.is_file(), "launchctl was not invoked for restart_service"
    assert "bootstrap" in log.read_text()
    assert (tmp_path / "agents" / "homebrew.mxcl.svc.plist").is_file()


def test_bundle_restart_service_dry_run_does_not_start(monkeypatch, sample, tmp_path) -> None:
    from pybrew import bundle as bundle_module
    from pybrew.bundle import bundle_install, parse_brewfile
    from pybrew.paths import Config

    brewfile = tmp_path / "Brewfile"
    brewfile.write_text('brew "svc", restart_service: true\n')
    launchctl = _fake_launchctl(tmp_path, monkeypatch)
    for name, value in _service_env(tmp_path, launchctl).items():
        monkeypatch.setenv(name, value)

    captured = []

    class RecordingInstaller:
        def __init__(self, config, loader) -> None:
            self.config = config
            self.loader = loader

        def install(self, formula, options):
            captured.append(options.dry_run)
            return []

    monkeypatch.setattr(bundle_module, "Installer", RecordingInstaller)
    config = Config.default(prefix=sample.prefix, taps=[sample.tap], cache=sample.cache)

    installed, errors = bundle_install(config, parse_brewfile(brewfile), dry_run=True)

    assert errors == []
    assert installed == ["brew svc"]
    assert captured == [True]
    assert not (tmp_path / "launchctl.log").exists()
    assert not (tmp_path / "agents").exists()


def test_bundle_restart_service_without_service_is_error(run_cli, sample, tmp_path) -> None:
    brewfile = tmp_path / "Brewfile"
    brewfile.write_text('brew "hello", restart_service: true\n')

    proc = run_cli("bundle", "install", "--file", str(brewfile), bottle_tag=sample.tag)

    assert proc.returncode == 1
    assert "restart_service requested" in proc.stderr
    assert "does not define a service" in proc.stderr
    # The formula itself still installed successfully.
    assert (sample.prefix / "Cellar" / "hello" / "1.0").is_dir()
    assert "Installed: brew hello" in proc.stdout


def test_bundle_restart_service_start_failure_is_error(monkeypatch, sample, tmp_path) -> None:
    from pybrew import bundle as bundle_module
    from pybrew.bundle import bundle_install, parse_brewfile
    from pybrew.paths import Config

    brewfile = tmp_path / "Brewfile"
    brewfile.write_text('brew "svc", restart_service: true\n')
    launchctl = _fake_launchctl(tmp_path, monkeypatch, exit_code=1)
    for name, value in _service_env(tmp_path, launchctl).items():
        monkeypatch.setenv(name, value)

    class InstallingInstaller:
        def __init__(self, config, loader) -> None:
            self.config = config
            self.loader = loader

        def install(self, formula, options):
            _stage_installed_service_keg(self.config, formula)
            return []

    monkeypatch.setattr(bundle_module, "Installer", InstallingInstaller)
    config = Config.default(prefix=sample.prefix, taps=[sample.tap], cache=sample.cache)

    installed, errors = bundle_install(config, parse_brewfile(brewfile))

    assert installed == ["brew svc"]
    assert len(errors) == 1
    assert errors[0].startswith("svc: failed to start service:")
    assert "launchctl bootstrap failed" in errors[0]
