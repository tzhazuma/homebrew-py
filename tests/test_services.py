from __future__ import annotations

import json
import os
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
    SERVICE_DEFAULT_LOGS_ENV,
    SERVICE_NAME_STYLE_ENV,
    SERVICE_PATH_MODE_ENV,
    USER_CONFIG_HOME_ENV,
    LaunchctlRunner,
    ServiceError,
    ServiceManager,
    ServiceSpec,
    SystemctlRunner,
    parse_service_block,
    read_user_service_env,
    resolve_render_user_environment,
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
    monkeypatch.delenv(SERVICE_NAME_STYLE_ENV, raising=False)
    monkeypatch.delenv(SERVICE_DEFAULT_LOGS_ENV, raising=False)
    config = Config.default(prefix=tmp_path / "prefix", taps=[], cache=tmp_path / "cache")
    config.ensure()
    return config


@pytest.fixture
def non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the effective uid so user env files are read even in root CI."""

    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)


def _write_user_env(
    home: Path,
    name: str = "svc",
    text: str = "FOO=override\n",
    mode: int = 0o600,
) -> Path:
    directory = home / "services"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.env"
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


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


# ---------------------------------------------------------------------------
# Homebrew user service env overrides for the literal parser
# ---------------------------------------------------------------------------


def test_literal_parser_reads_user_service_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        PATH=/user/bin

        NOT A PAIR
        SPACED =  trimmed
        """,
    )
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))
    formula = _formula_with_service(
        """
        run [opt_bin/"svc"]
        environment_variables FOO: "formula", KEEP: "yes",
                              PATH: std_service_path_env
        frobnicate "x"
        """
    )

    spec = parse_service_block(formula, config)

    # Declared values stay exactly as parsed; user overrides are separate.
    assert spec.environment_variables == {
        "FOO": "formula",
        "KEEP": "yes",
        "PATH": str(config.prefix / "bin"),
    }
    assert spec.declared_environment_variables() == spec.environment_variables
    assert spec.user_environment_variables == {
        "FOO": "override",
        "BAR": "value=with=equals",
        "PATH": "/user/bin",
        "SPACED": "trimmed",
    }
    assert spec.effective_environment_variables() == {
        "FOO": "override",
        "KEEP": "yes",
        "PATH": "/user/bin",
        "BAR": "value=with=equals",
        "SPACED": "trimmed",
    }
    # Unknown directives and skipped env lines share the warning surface.
    assert spec.warnings == [
        "unsupported service directive `frobnicate`",
        f"Skipping invalid line in {env_path}: NOT A PAIR",
    ]

    manager = ServiceManager(config)
    payload = manager.plist_payload(formula, spec)
    assert payload["EnvironmentVariables"]["FOO"] == "override"
    assert payload["EnvironmentVariables"]["BAR"] == "value=with=equals"
    assert payload["EnvironmentVariables"]["PATH"] == "/user/bin"
    unit = manager.unit_text(formula, spec)
    assert 'Environment="FOO=override"' in unit
    assert 'Environment="PATH=/user/bin"' in unit


def test_literal_parser_user_env_injectable_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    manager_spec = ServiceManager(config).spec_for(
        formula, user_config_home=user_home
    )
    assert manager_spec.effective_environment_variables()["FOO"] == "custom"


def test_literal_user_env_applies_without_declared_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    _write_user_env(user_home, "svc", "FOO=only-user\n")
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))
    formula = _formula_with_service('run [opt_bin/"svc"]')

    spec = parse_service_block(formula, config)

    assert spec.environment_variables == {}
    assert spec.user_environment_variables == {"FOO": "only-user"}
    assert spec.effective_environment_variables() == {"FOO": "only-user"}

    manager = ServiceManager(config)
    payload = manager.plist_payload(formula, spec)
    # Both renderers use the effective environment only: no synthetic PATH.
    assert payload["EnvironmentVariables"] == {"FOO": "only-user"}
    assert 'Environment="FOO=only-user"' in manager.unit_text(formula, spec)


def test_literal_parser_user_env_bounded_security_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    env_path = _write_user_env(user_home, "svc", "FOO=override\n")
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))
    formula = _formula_with_service('environment_variables FOO: "formula"')

    def effective_foo() -> str:
        return parse_service_block(formula, config).effective_environment_variables()[
            "FOO"
        ]

    assert effective_foo() == "override"
    # World- and group-writable user env files are ignored.
    env_path.chmod(0o666)
    assert effective_foo() == "formula"
    env_path.chmod(0o664)
    assert effective_foo() == "formula"
    # Root services never read user overrides.
    env_path.chmod(0o600)
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    assert effective_foo() == "formula"
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    # Unset HOMEBREW_USER_CONFIG_HOME means no overrides.
    monkeypatch.delenv(USER_CONFIG_HOME_ENV, raising=False)
    assert effective_foo() == "formula"


def test_read_user_service_env_shared_bounds(tmp_path: Path) -> None:
    formula = _formula()
    user_home = tmp_path / "config"
    env_path = _write_user_env(user_home, "svc", "FOO=bar\n")

    assert read_user_service_env(formula, user_home) == {"FOO": "bar"}
    assert read_user_service_env(formula, user_home, euid=0) == {}
    env_path.chmod(0o664)
    assert read_user_service_env(formula, user_home) == {}
    env_path.chmod(0o666)
    assert read_user_service_env(formula, user_home) == {}
    env_path.chmod(0o600)
    assert read_user_service_env(formula, user_home) == {"FOO": "bar"}
    assert read_user_service_env(formula, tmp_path / "missing") == {}
    assert read_user_service_env(formula, None) == {}


def test_literal_user_env_with_homebrew_path_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv(SERVICE_PATH_MODE_ENV, "homebrew")
    user_home = tmp_path / "user-config"
    _write_user_env(user_home, "svc", "FOO=override\nPATH=/user/bin\n")
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))
    formula = _formula_with_service(
        """
        run [opt_bin/"svc"]
        environment_variables FOO: "formula", PATH: std_service_path_env
        """
    )

    spec = parse_service_block(formula, config)
    declared_path = ":".join(
        [
            str(config.prefix / "bin"),
            str(config.prefix / "sbin"),
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
    )
    assert spec.declared_environment_variables() == {
        "FOO": "formula",
        "PATH": declared_path,
    }
    assert spec.user_environment_variables == {
        "FOO": "override",
        "PATH": "/user/bin",
    }
    assert spec.effective_environment_variables()["PATH"] == "/user/bin"
    payload = ServiceManager(config).plist_payload(formula, spec)
    assert payload["EnvironmentVariables"]["PATH"] == "/user/bin"


# ---------------------------------------------------------------------------
# shared PATH policy across renderers
# ---------------------------------------------------------------------------


def test_path_policy_is_shared_by_launchd_and_systemd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    manager = ServiceManager(config)
    formula = _formula()
    spec = ServiceSpec(run=["/opt/svc/bin/svc"])

    # Homebrew's effective environment never synthesises PATH, and both
    # renderers now follow it.
    assert spec.effective_environment_variables() == {}
    assert spec.render_environment_variables() == {}
    payload = manager.plist_payload(formula, spec)
    assert "EnvironmentVariables" not in payload
    assert "Environment=" not in manager.unit_text(formula, spec)

    # Declared or user PATH is rendered identically by both backends.
    declared = ServiceSpec(
        run=["/opt/svc/bin/svc"], environment_variables={"PATH": "/declared/bin"}
    )
    assert (
        manager.plist_payload(formula, declared)["EnvironmentVariables"]["PATH"]
        == "/declared/bin"
    )
    assert 'Environment="PATH=/declared/bin"' in manager.unit_text(formula, declared)
    user = ServiceSpec(
        run=["/opt/svc/bin/svc"], user_environment_variables={"PATH": "/user/bin"}
    )
    assert user.effective_environment_variables() == {"PATH": "/user/bin"}
    assert (
        manager.plist_payload(formula, user)["EnvironmentVariables"]["PATH"]
        == "/user/bin"
    )
    assert 'Environment="PATH=/user/bin"' in manager.unit_text(formula, user)


# ---------------------------------------------------------------------------
# render-time user env resolution (avoid stale snapshots)
# ---------------------------------------------------------------------------


def test_render_time_user_env_replaces_stale_parse_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    assert spec.user_environment_variables == {"FOO": "one"}
    assert spec.user_config_home == str(user_home)
    assert spec.environment_variables == {"FOO": "formula"}

    # Change the file after parsing: renderers must use the fresh values.
    env_path.write_text("FOO=two\nBAR=added\n", encoding="utf-8")
    env_path.chmod(0o600)

    manager = ServiceManager(config)
    payload = manager.plist_payload(formula, spec)
    assert payload["EnvironmentVariables"]["FOO"] == "two"
    assert payload["EnvironmentVariables"]["BAR"] == "added"
    assert "PATH" not in payload["EnvironmentVariables"]

    unit = manager.unit_text(formula, spec)
    assert 'Environment="FOO=two"' in unit
    assert 'Environment="BAR=added"' in unit
    assert 'Environment="FOO=one"' not in unit

    # Render-time resolution does not mutate the spec or its declared env.
    assert spec.user_environment_variables == {"FOO": "one"}
    assert spec.environment_variables == {"FOO": "formula"}
    assert spec.effective_environment_variables() == {"FOO": "one"}


def test_render_time_user_env_removal_ignores_stale_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    assert spec.user_environment_variables == {"FOO": "one"}
    env_path.unlink()

    manager = ServiceManager(config)
    payload = manager.plist_payload(formula, spec)
    # The current (empty) file supersedes the stale snapshot.
    assert payload["EnvironmentVariables"] == {"FOO": "formula"}
    assert 'Environment="FOO=formula"' in manager.unit_text(formula, spec)
    # The standalone spec snapshot is untouched.
    assert spec.user_environment_variables == {"FOO": "one"}


def test_render_time_user_env_uses_parse_time_context_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    env_path = _write_user_env(user_home, "svc", "FOO=one\n")
    assert os.environ.get(USER_CONFIG_HOME_ENV) is None
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\nenvironment_variables FOO: "formula"'
    )

    spec = parse_service_block(formula, config, user_config_home=user_home)
    assert spec.user_config_home == str(user_home)
    env_path.write_text("FOO=two\n", encoding="utf-8")
    env_path.chmod(0o600)

    manager = ServiceManager(config)
    assert (
        manager.plist_payload(formula, spec)["EnvironmentVariables"]["FOO"]
        == "two"
    )

    # A manager configured with a context refreshes both parse and render.
    manager_with_context = ServiceManager(config, user_config_home=user_home)
    context_spec = manager_with_context.spec_for(formula)
    env_path.write_text("FOO=three\n", encoding="utf-8")
    env_path.chmod(0o600)
    assert (
        manager_with_context.plist_payload(formula, context_spec)[
            "EnvironmentVariables"
        ]["FOO"]
        == "three"
    )


def test_render_environment_variables_explicit_user_environment() -> None:
    spec = ServiceSpec(
        environment_variables={"FOO": "declared", "PATH": "/declared/bin"},
        user_environment_variables={"FOO": "snapshot"},
    )

    assert spec.effective_environment_variables() == {
        "FOO": "snapshot",
        "PATH": "/declared/bin",
    }
    assert spec.effective_environment_variables(
        user_environment={"FOO": "fresh", "BAR": "x"}
    ) == {
        "FOO": "fresh",
        "PATH": "/declared/bin",
        "BAR": "x",
    }
    # An explicit empty mapping means "no current overrides", not "snapshot".
    assert spec.effective_environment_variables(user_environment={}) == {
        "FOO": "declared",
        "PATH": "/declared/bin",
    }
    assert spec.render_environment_variables(
        user_environment={"FOO": "fresh"}
    ) == {"FOO": "fresh", "PATH": "/declared/bin"}

    no_path = ServiceSpec(
        environment_variables={"FOO": "declared"},
        user_environment_variables={"FOO": "snapshot"},
    )
    assert no_path.render_environment_variables(
        default_path="/default/bin", user_environment={}
    ) == {"FOO": "declared", "PATH": "/default/bin"}
    assert no_path.render_environment_variables(
        default_path="/default/bin", user_environment={"PATH": "/user/bin"}
    ) == {"FOO": "declared", "PATH": "/user/bin"}
    # Neither method mutates the snapshot.
    assert spec.user_environment_variables == {"FOO": "snapshot"}
    assert no_path.user_environment_variables == {"FOO": "snapshot"}


def test_resolve_render_user_environment_context_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, non_root: None
) -> None:
    formula = _formula()
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    home_c = tmp_path / "home-c"
    _write_user_env(home_a, "svc", "SOURCE=a\n")
    _write_user_env(home_b, "svc", "SOURCE=b\n")
    _write_user_env(home_c, "svc", "SOURCE=c\n")
    monkeypatch.delenv(USER_CONFIG_HOME_ENV, raising=False)
    spec = ServiceSpec(
        user_config_home=str(home_b),
        user_environment_variables={"SOURCE": "snapshot"},
    )

    # Explicit argument wins over env and the spec context.
    assert resolve_render_user_environment(
        formula, spec, user_config_home=home_c
    ) == {"SOURCE": "c"}
    # Current env wins over the parse-time spec context.
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(home_a))
    assert resolve_render_user_environment(formula, spec) == {"SOURCE": "a"}
    # Spec context is used when no env/argument context is configured.
    monkeypatch.delenv(USER_CONFIG_HOME_ENV, raising=False)
    assert resolve_render_user_environment(formula, spec) == {"SOURCE": "b"}
    # No context at all: renderers keep using the standalone spec snapshot.
    assert resolve_render_user_environment(formula, ServiceSpec()) is None


# ---------------------------------------------------------------------------
# standalone ServiceSpec explicit render context
# ---------------------------------------------------------------------------


def test_standalone_spec_render_rereads_user_env_with_config_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_root: None,
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    env_path = _write_user_env(user_home, "svc", "FOO=one\nPATH=/from-file\n")
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\nenvironment_variables FOO: "formula"'
    )
    spec = parse_service_block(formula, config)
    spec.warnings = ["kept warning"]
    assert spec.formula_name == "svc"
    assert spec.user_config_home == str(user_home)

    # The manager context disappears, but the standalone spec can still
    # explicitly re-read from the passed context directory.
    monkeypatch.delenv(USER_CONFIG_HOME_ENV, raising=False)
    env_path.write_text("FOO=two\nBAR=added\n", encoding="utf-8")
    env_path.chmod(0o600)

    effective = spec.effective_environment_variables(user_config_home=user_home)
    assert effective == {"FOO": "two", "BAR": "added"}
    # Explicit context ignores the stale parse snapshot; no context uses it.
    assert spec.render_environment_variables(
        default_path=str(config.prefix / "bin"),
        user_config_home=user_home,
    ) == {"FOO": "two", "BAR": "added", "PATH": str(config.prefix / "bin")}
    assert spec.render_environment_variables() == {"FOO": "one", "PATH": "/from-file"}

    # The standalone spec itself is never mutated.
    assert spec.environment_variables == {"FOO": "formula"}
    assert spec.user_environment_variables == {"FOO": "one", "PATH": "/from-file"}
    assert spec.warnings == ["kept warning"]


def test_standalone_spec_render_explicit_user_environment_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_root: None,
) -> None:
    user_home = tmp_path / "user-config"
    _write_user_env(user_home, "svc", "FOO=from-file\n")
    spec = ServiceSpec(
        environment_variables={"FOO": "declared", "KEEP": "yes"},
        user_environment_variables={"FOO": "snapshot"},
        formula_name="svc",
    )

    # Explicit user_environment wins over both the file context and snapshot.
    assert spec.render_environment_variables(
        user_environment={"FOO": "explicit"},
        user_config_home=user_home,
    ) == {"FOO": "explicit", "KEEP": "yes"}
    # Explicit empty mapping means no current overrides, not the snapshot.
    assert spec.render_environment_variables(user_environment={}) == {
        "FOO": "declared",
        "KEEP": "yes",
    }
    # user_environment={} plus a context still wins (no file read).
    assert spec.effective_environment_variables(
        user_environment={}, user_config_home=user_home
    ) == {"FOO": "declared", "KEEP": "yes"}
    assert spec.user_environment_variables == {"FOO": "snapshot"}


def test_standalone_spec_render_removal_does_not_use_stale_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_root: None,
) -> None:
    user_home = tmp_path / "user-config"
    env_path = _write_user_env(user_home, "svc", "FOO=one\n")
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))
    spec = parse_service_block(
        _formula_with_service('environment_variables FOO: "formula"'),
        config,
    )
    assert spec.user_environment_variables == {"FOO": "one"}
    monkeypatch.delenv(USER_CONFIG_HOME_ENV, raising=False)
    env_path.unlink()

    # A passed context with no file supersedes the stale snapshot.
    assert spec.effective_environment_variables(user_config_home=user_home) == {
        "FOO": "formula"
    }
    assert spec.render_environment_variables(
        default_path="/default/bin", user_config_home=user_home
    ) == {"FOO": "formula", "PATH": "/default/bin"}


def test_standalone_spec_render_context_needs_formula_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_root: None,
) -> None:
    user_home = tmp_path / "user-config"
    _write_user_env(user_home, "svc", "FOO=fresh\n")
    formula = _formula()
    bare = ServiceSpec(environment_variables={"FOO": "declared"})

    with pytest.raises(ServiceError, match="requires a formula name"):
        bare.render_environment_variables(user_config_home=user_home)

    # formula_name or a Formula-like object supplies the missing name.
    named = ServiceSpec(
        environment_variables={"FOO": "declared"}, formula_name="svc"
    )
    assert named.render_environment_variables(
        user_config_home=user_home
    ) == {"FOO": "fresh"}
    object_based = ServiceSpec(
        environment_variables={"FOO": "declared"},
        user_environment_variables={"FOO": "snapshot"},
    )
    assert object_based.render_environment_variables(
        formula=formula, user_config_home=user_home
    ) == {"FOO": "fresh"}
    assert bare.render_environment_variables(
        formula="svc", user_config_home=user_home
    ) == {"FOO": "fresh"}

    # from_hash round-trips the captured name for standalone specs.
    restored = ServiceSpec.from_hash(
        {
            "environment_variables": {"FOO": "declared"},
            "formula_name": "svc",
        }
    )
    assert restored.render_environment_variables(
        user_config_home=user_home
    ) == {"FOO": "fresh"}


def test_plist_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    _install_fake_keg(config, formula)
    manager = ServiceManager(config)
    spec = manager.spec_for(formula)
    path = manager.write_plist(formula, spec)
    payload = plistlib.loads(path.read_bytes())
    assert payload["Label"] == "sh.brew.svc"
    assert payload["ProgramArguments"][0].endswith("/opt/svc/bin/svc")
    assert payload["KeepAlive"] is True
    assert payload["RunAtLoad"] is True
    assert payload["EnvironmentVariables"]["FOO"] == "bar"
    assert path.name == "sh.brew.svc.plist"


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
    assert (sample.root / "agents" / "sh.brew.svc.plist").is_file()
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
    assert path.name == "sh.brew.svc.service"
    unit = path.read_text()
    assert "[Unit]" in unit and "[Service]" in unit
    assert "ExecStart=" in unit
    assert "WorkingDirectory=" in unit
    assert 'Environment="FOO=bar"' in unit
    assert "daemon-reload" in log.read_text()
    assert "start sh.brew.svc.service" in log.read_text()
    assert manager.status(formula) == "started"

    manager.stop(formula)
    assert "stop sh.brew.svc.service" in log.read_text()


def test_cleanup_removes_orphan_service_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula()
    _install_fake_keg(config, formula)
    manager = ServiceManager(config)
    manager.directory.mkdir(parents=True, exist_ok=True)
    installed_file = manager.directory / "sh.brew.svc.plist"
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

    assert payload["Label"] == "sh.brew.svc"
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
    # The formula declares PATH via std_service_path_env; it is preserved.
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
    assert path.name == "sh.brew.svc.service"
    assert "Type=oneshot" in unit
    assert "Restart=on-failure" in unit
    assert "daemon-reload" in log.read_text()
    assert "start sh.brew.svc.service" in log.read_text()
    assert manager.status(formula) == "started"

    manager.stop(formula)
    assert "stop sh.brew.svc.service" in log.read_text()


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
    # launchd does not accept "*" in StartCalendarInterval, so wildcard fields
    # are omitted (matching Homebrew's Service#to_plist).
    assert payload["StartCalendarInterval"] == {"Minute": 0, "Hour": 0}
    assert set(payload["StartCalendarInterval"]) == {"Minute", "Hour"}


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
    assert spec.manual_command() == 'FOO="bar" /opt/example/bin/svc --flag'
    assert spec.keep_alive_p() is True
    assert spec.timed_p() is True
    assert spec.requires_root_p() is True
    assert spec.nice_requires_root_p() is False
    assert spec.default_service_name("svc") == "sh.brew.svc"
    assert spec.default_plist_name("svc") == "sh.brew.svc.plist"
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
        "Day": "*",
        "Month": "*",
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
    assert payload["Label"] == "sh.brew.svc"
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
    # root_dir is the chroot key, not a working-directory fallback.
    assert "WorkingDirectory" not in payload
    assert payload["RootDirectory"] == str(home / "root")
    assert payload["StandardInPath"] == str(home / "stdin")
    assert payload["StandardOutPath"] == str(home / "out.log")
    assert payload["StandardErrorPath"] == str(home / "err.log")

    unit = manager.unit_text(formula, spec)
    assert f"RootDirectory={home / 'root'}" in unit
    assert f"StandardInput=file:{home / 'stdin'}" in unit
    assert "WorkingDirectory=" not in unit
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


def test_no_synthetic_log_paths_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity decision: only declared log/error paths are rendered.

    Homebrew does not write ``var/log/<name>.log`` defaults, so pybrew does not
    either unless ``PYBREW_SERVICE_DEFAULT_LOGS=1`` restores the old behaviour.
    """

    config = _config(tmp_path, monkeypatch)
    monkeypatch.delenv("PYBREW_SERVICE_DEFAULT_LOGS", raising=False)
    manager = ServiceManager(config)
    formula = _formula()
    spec = ServiceSpec(run=["/opt/svc/bin/svc"])

    payload = manager.plist_payload(formula, spec)
    assert "StandardOutPath" not in payload
    assert "StandardErrorPath" not in payload
    assert "WorkingDirectory" not in payload
    assert "RootDirectory" not in payload
    assert "StandardInPath" not in payload

    unit = manager.unit_text(formula, spec)
    assert "StandardOutput=" not in unit
    assert "StandardError=" not in unit
    assert "WorkingDirectory=" not in unit
    assert "RootDirectory=" not in unit
    assert "StandardInput=" not in unit


def test_legacy_default_log_paths_are_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYBREW_SERVICE_DEFAULT_LOGS", "1")
    manager = ServiceManager(config)
    formula = _formula()
    spec = ServiceSpec(run=["/opt/svc/bin/svc"])
    default_log = str(config.prefix / "var" / "log" / "svc.log")

    payload = manager.plist_payload(formula, spec)
    assert payload["StandardOutPath"] == default_log
    assert payload["StandardErrorPath"] == default_log

    unit = manager.unit_text(formula, spec)
    assert f"StandardOutput=append:{default_log}" in unit
    assert f"StandardError=append:{default_log}" in unit


def test_only_declared_log_path_is_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.delenv("PYBREW_SERVICE_DEFAULT_LOGS", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    manager = ServiceManager(config)
    formula = _formula()
    spec = ServiceSpec(run=["/opt/svc/bin/svc"], log_path="~/custom.out")

    payload = manager.plist_payload(formula, spec)
    assert payload["StandardOutPath"] == str(home / "custom.out")
    assert "StandardErrorPath" not in payload

    unit = manager.unit_text(formula, spec)
    assert f"StandardOutput=append:{home / 'custom.out'}" in unit
    assert "StandardError=" not in unit
    assert f"ExecStart={shlex.join(spec.run)}" in unit


# ---------------------------------------------------------------------------
# Homebrew parity: manual command quoting and launchd scheduling/paths
# ---------------------------------------------------------------------------


def test_manual_command_is_shell_quoted_like_homebrew() -> None:
    spec = ServiceSpec(
        run=["/opt/x y/bin/svc", "--flag", "two words", "quote'x"],
        environment_variables={"FOO": "a b", "PATH": "/declared/bin", "KEEP": "yes"},
        user_environment_variables={"FOO": "user value"},
    )

    command = spec.manual_command()

    assert command == (
        'FOO="user value" KEEP="yes" '
        "'/opt/x y/bin/svc' --flag 'two words' 'quote'\"'\"'x'"
    )
    # PATH is intentionally excluded, matching Homebrew's Service#manual_command.
    assert "PATH=" not in command


def test_manual_command_expands_tilde_like_homebrew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    spec = ServiceSpec(run=["~/bin/svc", "~/a b"])

    # Homebrew's Service#command expands leading tildes before quoting.
    assert spec.command() == [str(home / "bin/svc"), str(home / "a b")]
    assert spec.manual_command() == f"{home}/bin/svc '{home}/a b'"


def test_manual_command_keeps_declared_values_and_empty_arguments() -> None:
    spec = ServiceSpec(run=["/opt/svc/bin/svc", ""], environment_variables={"A": "1"})
    assert spec.manual_command() == 'A="1" /opt/svc/bin/svc \'\''


def test_launchd_restart_delay_and_throttle_have_distinct_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    manager = ServiceManager(config)
    formula = _formula()
    spec = ServiceSpec(
        run=["/opt/svc/bin/svc"],
        restart_delay=7,
        throttle_interval=20,
        interval=300,
        run_type="interval",
    )

    payload = manager.plist_payload(formula, spec)
    assert payload["TimeOut"] == 7
    assert payload["ThrottleInterval"] == 20
    assert payload["StartInterval"] == 300


def test_launchd_schedule_fields_follow_run_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    manager = ServiceManager(config)
    formula = _formula()

    immediate = ServiceSpec(
        run=["/opt/svc/bin/svc"],
        run_type="immediate",
        run_type_explicit=True,
        interval=30,
        cron={"minute": 5},
    )
    payload = manager.plist_payload(formula, immediate)
    assert "StartInterval" not in payload
    assert "StartCalendarInterval" not in payload

    cron = ServiceSpec(
        run=["/opt/svc/bin/svc"],
        run_type="cron",
        interval=30,
        cron={"minute": 5, "hour": 6},
    )
    payload = manager.plist_payload(formula, cron)
    assert "StartInterval" not in payload
    assert payload["StartCalendarInterval"] == {"Minute": 5, "Hour": 6}


def test_plist_limits_launchd_to_all_session_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    manager = ServiceManager(config)
    payload = manager.plist_payload(_formula(), ServiceSpec(run=["/opt/svc/bin/svc"]))
    assert payload["LimitLoadToSessionType"] == [
        "Aqua",
        "Background",
        "LoginWindow",
        "StandardIO",
        "System",
    ]


@pytest.mark.parametrize(
    "target,expected",
    [
        ("darwin", ["/opt/mac/bin/svc"]),
        ("linux", ["/opt/linux/bin/svc", "--foreground"]),
    ],
)
def test_run_platform_keyword_selects_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    expected: list[str],
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYBREW_TARGET_OS", target)
    formula = _formula_with_service(
        'run macos: "/opt/mac/bin/svc", linux: ["/opt/linux/bin/svc", "--foreground"]'
    )
    assert parse_service_block(formula, config).run == expected


@pytest.mark.parametrize(
    "target,expected",
    [
        ("darwin", []),
        ("linux", ["/opt/linux/bin/svc"]),
    ],
)
def test_run_platform_keyword_missing_target_leaves_command_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    expected: list[str],
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYBREW_TARGET_OS", target)
    formula = _formula_with_service('run linux: "/opt/linux/bin/svc"')
    assert parse_service_block(formula, config).run == expected


def test_systemd_interval_timer_and_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\nrun_type :interval\ninterval 300'
    )
    _install_fake_keg(config, formula)
    manager, log = _systemd_manager(tmp_path, monkeypatch, config)
    spec = manager.spec_for(formula)

    timer = manager.timer_text(formula, spec)
    assert "OnUnitActiveSec=300" in timer
    assert "OnCalendar=" not in timer
    assert "Persistent=true" not in timer
    assert "Unit=sh.brew.svc.service" in timer

    manager.start(formula)
    assert manager.timer_path(formula).is_file()
    assert "start sh.brew.svc.timer" in log.read_text()


# ---------------------------------------------------------------------------
# Canonical sh.brew.* labels with legacy homebrew.mxcl.* aliases
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """Minimal ServiceManager runner that records calls and fake statuses."""

    def __init__(self, started: tuple[str, ...] = ()) -> None:
        self.started = set(started)
        self.calls: list[tuple[str, ...]] = []

    def status(self, label: str) -> str:
        self.calls.append(("status", label))
        return "started" if label in self.started else "stopped"

    def start(self, *args: object) -> None:
        self.calls.append(("start", *[str(arg) for arg in args]))

    def stop(self, *args: object) -> None:
        self.calls.append(("stop", *[str(arg) for arg in args]))

    def daemon_reload(self) -> None:
        self.calls.append(("daemon-reload",))


def test_default_name_style_is_canonical_sh_brew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.delenv(SERVICE_NAME_STYLE_ENV, raising=False)
    manager = ServiceManager(config)
    formula = _formula()
    spec = manager.spec_for(formula)

    assert manager.label(formula, spec) == "sh.brew.svc"
    assert manager.plist_path(formula, spec).name == "sh.brew.svc.plist"
    assert manager.labels(formula, spec) == [
        "sh.brew.svc",
        "homebrew.mxcl.svc",
        "homebrew.svc",
    ]


def test_legacy_name_style_is_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv(SERVICE_NAME_STYLE_ENV, "pybrew")
    manager = ServiceManager(config)
    formula = _formula()
    spec = manager.spec_for(formula)

    assert manager.label(formula, spec) == "homebrew.mxcl.svc"
    assert manager.plist_path(formula, spec).name == "homebrew.mxcl.svc.plist"
    assert manager.labels(formula, spec)[0] == "homebrew.mxcl.svc"
    assert "sh.brew.svc" in manager.labels(formula, spec)


@pytest.mark.parametrize("style_env", ["canonical", "brew", "homebrew", "sh.brew"])
def test_canonical_name_style_writes_sh_brew_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, style_env: str
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv(SERVICE_NAME_STYLE_ENV, style_env)
    runner = _RecordingRunner()
    manager = ServiceManager(config, runner=runner)
    formula = _formula()
    spec = manager.spec_for(formula)

    assert manager.label(formula, spec) == "sh.brew.svc"
    assert manager.plist_path(formula, spec).name == "sh.brew.svc.plist"
    assert manager.labels(formula, spec)[0] == "sh.brew.svc"
    assert "homebrew.mxcl.svc" in manager.labels(formula, spec)

    payload = manager.plist_payload(formula, spec)
    assert payload["Label"] == "sh.brew.svc"


def test_explicit_names_disable_alias_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv(SERVICE_NAME_STYLE_ENV, "canonical")
    manager = ServiceManager(config)
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\n'
        'name macos: "com.example.svc", linux: "custom-svc"'
    )
    spec = manager.spec_for(formula)

    assert manager.label(formula, spec) == "com.example.svc"
    assert manager.labels(formula, spec) == ["com.example.svc"]
    manager.backend = "systemd"
    assert manager.label(formula, spec) == "custom-svc"
    assert manager.labels(formula, spec) == ["custom-svc"]


def test_status_resolves_running_legacy_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv(SERVICE_NAME_STYLE_ENV, "canonical")
    manager = ServiceManager(config)
    formula = _formula()
    spec = manager.spec_for(formula)
    legacy = manager.directory / "homebrew.mxcl.svc.plist"
    manager.directory.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy plist")
    runner = _RecordingRunner(started=("homebrew.mxcl.svc",))
    manager.runner = runner

    assert manager.status(formula) == "started"
    assert ("status", "sh.brew.svc") in runner.calls
    assert ("status", "homebrew.mxcl.svc") in runner.calls


def test_status_none_when_no_alias_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    manager = ServiceManager(config)
    runner = _RecordingRunner()
    manager.runner = runner

    assert manager.status(_formula()) == "none"
    assert runner.calls == []


def test_stop_and_remove_handle_legacy_alias_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv(SERVICE_NAME_STYLE_ENV, "canonical")
    manager = ServiceManager(config)
    formula = _formula()
    spec = manager.spec_for(formula)
    legacy = manager.directory / "homebrew.mxcl.svc.plist"
    manager.directory.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy plist")
    runner = _RecordingRunner()
    manager.runner = runner

    stopped = manager.stop(formula, spec)
    assert stopped == legacy
    assert ("stop", "homebrew.mxcl.svc", str(legacy)) in runner.calls

    manager.remove(formula)
    assert not legacy.exists()


def test_remove_cleans_all_label_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    manager = ServiceManager(config)
    formula = _formula()
    manager.directory.mkdir(parents=True, exist_ok=True)
    paths = [
        manager.directory / "homebrew.mxcl.svc.plist",
        manager.directory / "sh.brew.svc.plist",
    ]
    for path in paths:
        path.write_text("plist")
    runner = _RecordingRunner()
    manager.runner = runner

    manager.remove(formula)

    assert all(not path.exists() for path in paths)
    # Explicit custom labels are still never touched by remove() for others.
    assert {call[0] for call in runner.calls} == {"stop"}


def test_cleanup_removes_canonical_and_legacy_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    manager = ServiceManager(config)
    formula = _formula()
    _install_fake_keg(config, formula)
    manager.directory.mkdir(parents=True, exist_ok=True)
    stale = [
        manager.directory / "sh.brew.ghost.plist",
        manager.directory / "homebrew.mxcl.ghost.plist",
        manager.directory / "sh.brew.ghost.service",
    ]
    kept = [
        manager.directory / "homebrew.mxcl.svc.plist",
        manager.directory / "sh.brew.svc.plist",
    ]
    for path in [*stale, *kept]:
        path.write_text("service")

    removed = manager.cleanup()

    assert set(removed) == set(stale)
    assert all(path.exists() for path in kept)


def test_systemd_canonical_label_and_legacy_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYBREW_SERVICE_BACKEND", "systemd")
    runner = _RecordingRunner(started=("sh.brew.svc.service",))
    manager = ServiceManager(config, runner=runner, name_style="canonical")
    formula = _formula()
    spec = manager.spec_for(formula)
    manager.directory.mkdir(parents=True, exist_ok=True)
    (manager.directory / "sh.brew.svc.service").write_text("legacy unit")

    assert manager.label(formula, spec) == "sh.brew.svc"
    assert manager.unit_path(formula, spec).name == "sh.brew.svc.service"
    assert manager.status(formula) == "started"
    assert ("status", "sh.brew.svc.service") in runner.calls


# ---------------------------------------------------------------------------
# Service option persistence (to_hash/from_hash round-trip)
# ---------------------------------------------------------------------------


def test_service_spec_to_hash_round_trip_preserves_options() -> None:
    spec = ServiceSpec(
        run=["/opt/svc/bin/svc", "--flag"],
        working_dir="/var/lib/svc",
        log_path="/var/log/svc.log",
        error_log_path="/var/log/svc.err",
        keep_alive={"successful_exit": True},
        run_at_load=False,
        interval=300,
        process_type="background",
        run_type="interval",
        plist_name="com.example.svc",
        service_name="custom-svc",
        environment_variables={"FOO": "bar"},
        require_root=True,
        throttle_interval=5,
        launch_only_once=True,
        macos_legacy_timers=True,
        nice=5,
        restart_delay=7,
        stop_timeout=9,
        root_dir="/root",
        input_path="/tmp/in",
        cron={
            "Minute": 0,
            "Hour": 0,
            "Day": "*",
            "Month": "*",
            "Weekday": "*",
        },
        sockets={"http": {"host": "127.0.0.1", "port": "8080", "type": "tcp"}},
        run_type_explicit=True,
    )

    data = spec.to_hash()
    restored = ServiceSpec.from_hash(data)

    assert restored.run == spec.run
    assert restored.working_dir == spec.working_dir
    assert restored.log_path == spec.log_path
    assert restored.error_log_path == spec.error_log_path
    assert restored.keep_alive == spec.keep_alive
    assert restored.run_at_load is False
    assert restored.run_type == "interval"
    assert restored.run_type_explicit is True
    assert restored.interval == 300
    assert restored.process_type == "background"
    assert restored.plist_name == "com.example.svc"
    assert restored.service_name == "custom-svc"
    assert restored.environment_variables == {"FOO": "bar"}
    assert restored.require_root is True
    assert restored.throttle_interval == 5
    assert restored.launch_only_once is True
    assert restored.macos_legacy_timers is True
    assert restored.nice == 5
    assert restored.restart_delay == 7
    assert restored.stop_timeout == 9
    assert restored.root_dir == "/root"
    assert restored.input_path == "/tmp/in"
    assert restored.parse_cron() == spec.parse_cron()
    assert restored.sockets == spec.sockets


def test_to_hash_excludes_local_parse_context() -> None:
    spec = ServiceSpec(
        run=["/opt/svc/bin/svc"],
        user_environment_variables={"FOO": "user"},
        user_config_home="/home/user/.config/homebrew",
        formula_name="svc",
        warnings=["unsupported service directive `x`"],
    )

    data = spec.to_hash()

    assert "user_environment_variables" not in data
    assert "user_environment" not in data
    assert "user_config_home" not in data
    assert "formula_name" not in data
    assert "warnings" not in data


def test_from_hash_accepts_homebrew_service_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYBREW_TARGET_OS", "linux")
    data = {
        "name": {"macos": "com.example.svc", "linux": "custom-svc"},
        "run": {"macos": "/opt/mac/bin/svc", "linux": ["/opt/linux/bin/svc", "--f"]},
        "run_type": "cron",
        "cron": "0 6 * * 1",
        "keep_alive": True,
        "environment_variables": {"FOO": 1, "NONE": None},
        "sockets": "tcp://127.0.0.1:8080",
        "working_dir": "/var/lib/svc",
        "launch_only_once": True,
        "unknown": "ignored",
    }

    spec = ServiceSpec.from_hash(data)

    assert spec.run == ["/opt/linux/bin/svc", "--f"]
    assert spec.run_type == "cron"
    assert spec.run_type_explicit is True
    assert spec.parse_cron() == {
        "Minute": 0,
        "Hour": 6,
        "Day": "*",
        "Month": "*",
        "Weekday": 1,
    }
    assert spec.keep_alive is True
    assert spec.environment_variables == {"FOO": "1", "NONE": ""}
    assert spec.sockets == {
        "listeners": {"host": "127.0.0.1", "port": "8080", "type": "tcp"}
    }
    assert spec.working_dir == "/var/lib/svc"
    assert spec.launch_only_once is True
    assert spec.plist_name == "com.example.svc"
    assert spec.service_name == "custom-svc"


def test_from_hash_rejects_bad_shapes() -> None:
    with pytest.raises(ServiceError, match="mapping"):
        ServiceSpec.from_hash(["not", "a", "mapping"])  # type: ignore[arg-type]
    with pytest.raises(ServiceError, match="run"):
        ServiceSpec.from_hash({"run": 42})


# ---------------------------------------------------------------------------
# User env skip warnings
# ---------------------------------------------------------------------------


def _env_formula() -> Formula:
    return _formula_with_service('run [opt_bin/"svc"]\nenvironment_variables FOO: "formula"')


def test_user_env_world_writable_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, non_root: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    env_path = _write_user_env(user_home, "svc", "FOO=override\n", mode=0o666)
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))

    spec = parse_service_block(_env_formula(), config)

    assert spec.user_environment_variables == {}
    assert spec.effective_environment_variables()["FOO"] == "formula"
    assert spec.warnings == [f"Skipping {env_path}: file is world-writable."]


def test_user_env_group_writable_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, non_root: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    env_path = _write_user_env(user_home, "svc", "FOO=override\n", mode=0o664)
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))

    spec = parse_service_block(_env_formula(), config)

    assert spec.warnings == [f"Skipping {env_path}: file is group-writable."]


def test_user_env_root_service_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    env_path = _write_user_env(user_home, "svc", "FOO=override\n")
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    spec = parse_service_block(_env_formula(), config)

    assert spec.user_environment_variables == {}
    assert spec.warnings == [
        f"Skipping {env_path}: user env overrides are not supported for root services."
    ]


def test_user_env_warnings_are_deduplicated_on_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, non_root: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    _write_user_env(user_home, "svc", "FOO=override\n", mode=0o666)
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))
    formula = _env_formula()
    spec = parse_service_block(formula, config)
    manager = ServiceManager(config)

    manager.plist_payload(formula, spec)
    manager.unit_text(formula, spec)

    assert len(spec.warnings) == 1
    assert "world-writable" in spec.warnings[0]


def test_user_env_warnings_are_json_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, non_root: None
) -> None:
    config = _config(tmp_path, monkeypatch)
    user_home = tmp_path / "user-config"
    _write_user_env(user_home, "svc", "FOO=override\nNOT A PAIR\n", mode=0o600)
    monkeypatch.setenv(USER_CONFIG_HOME_ENV, str(user_home))

    spec = parse_service_block(_env_formula(), config)

    assert json.loads(json.dumps({"warnings": spec.warnings})) == {
        "warnings": spec.warnings
    }
    assert all(isinstance(warning, str) for warning in spec.warnings)


# ---------------------------------------------------------------------------
# Negative nice / require_root validation parity
# ---------------------------------------------------------------------------


def test_negative_nice_requires_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    with pytest.raises(ServiceError, match="require_root"):
        parse_service_block(
            _formula_with_service('run [opt_bin/"svc"]\nnice -5'), config
        )

    # Directive order does not matter: the final spec is validated.
    spec = parse_service_block(
        _formula_with_service(
            'run [opt_bin/"svc"]\nnice -5\nrequire_root true'
        ),
        config,
    )
    assert spec.nice == -5
    assert spec.nice_requires_root_p() is False
    assert spec.requires_root_p() is True


def test_nice_range_is_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    for value in (-21, 20):
        with pytest.raises(ServiceError, match="between -20 and 19"):
            parse_service_block(
                _formula_with_service(
                    f'run [opt_bin/"svc"]\nnice {value}\nrequire_root true'
                ),
                config,
            )

    spec = parse_service_block(
        _formula_with_service('run [opt_bin/"svc"]\nnice 19'), config
    )
    assert spec.nice == 19
    assert spec.nice_requires_root_p() is False


def test_from_hash_validates_nice_constraints() -> None:
    with pytest.raises(ServiceError, match="require_root"):
        ServiceSpec.from_hash({"nice": -1})
    with pytest.raises(ServiceError, match="between -20 and 19"):
        ServiceSpec.from_hash({"nice": 30, "require_root": True})

    spec = ServiceSpec.from_hash({"nice": -1, "require_root": True})
    assert spec.nice == -1
    assert spec.nice_requires_root_p() is False


# ---------------------------------------------------------------------------
# Doctor and cleanup label coverage (canonical/legacy/custom)
# ---------------------------------------------------------------------------


class _FakeLoader:
    def __init__(self, formulae: dict[str, Formula]) -> None:
        self._formulae = formulae

    def load(self, name: str) -> Formula:
        return self._formulae[name]


def test_doctor_recognises_all_service_label_forms_and_custom_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pybrew.doctor import diagnose_issues

    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\nname macos: "com.example.svc", linux: "custom-svc"'
    )
    _install_fake_keg(config, formula)
    manager = ServiceManager(config)
    manager.directory.mkdir(parents=True, exist_ok=True)
    for name in (
        "sh.brew.svc.plist",
        "homebrew.mxcl.svc.plist",
        "homebrew.svc.service",
        "com.example.svc.plist",
        "custom-svc.service",
        "custom-svc.timer",
    ):
        (manager.directory / name).write_text("service")

    issues = diagnose_issues(
        config, loader=_FakeLoader({formula.name: formula})
    )
    assert [i for i in issues if i["category"] == "services"] == []

    orphan = manager.directory / "sh.brew.ghost.plist"
    orphan.write_text("service")
    issues = diagnose_issues(
        config, loader=_FakeLoader({formula.name: formula})
    )
    messages = [i["message"] for i in issues if i["category"] == "services"]
    assert any(str(orphan) in message for message in messages)


def test_cleanup_handles_custom_labels_with_installed_formulae(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    formula = _formula_with_service(
        'run [opt_bin/"svc"]\nname macos: "com.example.svc", linux: "custom-svc"'
    )
    _install_fake_keg(config, formula)
    manager = ServiceManager(config)
    manager.directory.mkdir(parents=True, exist_ok=True)
    kept = manager.directory / "com.example.svc.plist"
    kept.write_text("service")
    stale = [
        manager.directory / "com.example.ghost.plist",
        manager.directory / "homebrew.ghost.service",
        manager.directory / "homebrew.mxcl.ghost.plist",
    ]
    for path in stale:
        path.write_text("service")

    removed = manager.cleanup([formula])

    assert set(removed) == set(stale)
    assert kept.exists()


# ---------------------------------------------------------------------------
# Persistence edge cases
# ---------------------------------------------------------------------------


def test_to_hash_round_trip_preserves_inferred_and_explicit_run_type() -> None:
    inferred = ServiceSpec(
        run=["/opt/svc/bin/svc"],
        interval=30,
        cron={
            "Minute": 0,
            "Hour": 0,
            "Day": "*",
            "Month": "*",
            "Weekday": "*",
        },
    )
    restored = ServiceSpec.from_hash(inferred.to_hash())
    assert restored.effective_run_type() == "cron"
    assert restored.run_type_explicit is False

    explicit_immediate = ServiceSpec(
        run=["/opt/svc/bin/svc"],
        run_type="immediate",
        run_type_explicit=True,
        cron={"Minute": 0, "Hour": 0},
        interval=30,
    )
    restored = ServiceSpec.from_hash(explicit_immediate.to_hash())
    assert restored.effective_run_type() == "immediate"
    assert restored.run_type_explicit is True


def test_to_hash_round_trip_preserves_false_run_at_load_and_empty_spec() -> None:
    spec = ServiceSpec(run=["/opt/svc/bin/svc"], run_at_load=False)
    restored = ServiceSpec.from_hash(spec.to_hash())
    assert restored.run_at_load is False

    empty = ServiceSpec.from_hash(ServiceSpec().to_hash())
    assert empty.run == []
    assert empty.run_type == "immediate"
    assert empty.run_type_explicit is False
    assert empty.run_at_load is True
    assert empty.cron == {}
    assert empty.sockets == {}


def test_from_hash_edge_shapes_and_unknown_fields() -> None:
    spec = ServiceSpec.from_hash(
        {
            "run": "single-command",
            "sockets": {"http": "tcp://127.0.0.1:8080"},
            "environment_variables": {"A": 1, "B": None},
            "unknown": {"ignored": True},
            "interval": "60",
        }
    )
    assert spec.run == ["single-command"]
    assert spec.sockets == {
        "http": {"host": "127.0.0.1", "port": "8080", "type": "tcp"}
    }
    assert spec.environment_variables == {"A": "1", "B": ""}
    assert spec.interval == 60
    assert not hasattr(spec, "unknown")


def test_direct_service_spec_construction_validates_nice() -> None:
    with pytest.raises(ServiceError, match="require_root"):
        ServiceSpec(nice=-1)
    with pytest.raises(ServiceError, match="between -20 and 19"):
        ServiceSpec(nice=20)
    spec = ServiceSpec(nice=-1, require_root=True)
    assert spec.nice_requires_root_p() is False
