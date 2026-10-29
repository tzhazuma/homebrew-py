"""Tests for the local, opt-in analytics subsystem.

Analytics are expected to be disabled by default, stored in a single JSON file
under the configured prefix, and never uploaded.  These tests use temporary
prefixes only; no test touches the real ``~/.pybrew`` tree or the network.
"""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path

import pytest

from pybrew import analytics
from pybrew.paths import Config


def _config(tmp_path: Path) -> Config:
    return Config(prefix=tmp_path / "prefix", cache=tmp_path / "cache")


def _state_file(config: Config) -> Path:
    return config.prefix / "var" / "pybrew" / "analytics.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# -- state and privacy defaults -----------------------------------------
def test_disabled_by_default_and_no_file_created(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert analytics.is_enabled(config) is False
    assert analytics.read_events(config) == []
    assert analytics.analytics_path(config) == _state_file(config)
    assert not _state_file(config).exists()


def test_record_event_is_noop_when_disabled(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert analytics.record_event(config, "install", {"formula": "hello"}) is None
    assert analytics.read_events(config) == []
    assert not _state_file(config).exists()


def test_enable_disable_roundtrip_writes_valid_json(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert analytics.set_enabled(config, True) is True
    assert analytics.is_enabled(config) is True

    path = _state_file(config)
    payload = _read_json(path)
    assert payload == {"enabled": True, "events": [], "version": 1}
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert analytics.set_enabled(config, False) is False
    assert analytics.is_enabled(config) is False
    assert _read_json(path)["enabled"] is False


def test_corrupt_state_falls_back_to_disabled(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _state_file(config)
    path.parent.mkdir(parents=True)
    path.write_text("{this is not json", encoding="utf-8")

    assert analytics.is_enabled(config) is False
    assert analytics.read_events(config) == []
    assert analytics.record_event(config, "install") is None

    assert analytics.set_enabled(config, True) is True
    payload = _read_json(path)
    assert payload["enabled"] is True
    assert payload["events"] == []


# -- recording -----------------------------------------------------------
def test_record_and_read_events(tmp_path: Path) -> None:
    config = _config(tmp_path)
    analytics.set_enabled(config, True)

    event = analytics.record_event(
        config, "install", {"formula": "hello", "success": True, "count": 2}
    )
    assert event is not None
    assert event["name"] == "install"
    assert event["properties"] == {"formula": "hello", "success": True, "count": 2}
    assert event["timestamp"]

    events = analytics.read_events(config)
    assert [stored["name"] for stored in events] == ["install"]
    assert events[0]["properties"] == {
        "formula": "hello",
        "success": True,
        "count": 2,
    }

    payload = _read_json(_state_file(config))
    assert payload["version"] == 1
    assert payload["enabled"] is True
    assert payload["events"][0]["name"] == "install"
    assert isinstance(payload["events"][0]["timestamp"], str)


def test_read_events_limit_returns_most_recent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    analytics.set_enabled(config, True)
    for name in ("one", "two", "three"):
        analytics.record_event(config, name)

    assert [event["name"] for event in analytics.read_events(config)] == [
        "one",
        "two",
        "three",
    ]
    assert [event["name"] for event in analytics.read_events(config, limit=2)] == [
        "two",
        "three",
    ]
    assert analytics.read_events(config, limit=0) == []
    with pytest.raises(ValueError):
        analytics.read_events(config, limit=-1)


def test_events_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    analytics.set_enabled(config, True)
    monkeypatch.setattr(analytics, "MAX_EVENTS", 3)
    for name in ("one", "two", "three", "four", "five"):
        analytics.record_event(config, name)

    assert [event["name"] for event in analytics.read_events(config)] == [
        "three",
        "four",
        "five",
    ]
    assert len(_read_json(_state_file(config))["events"]) == 3


def test_event_names_and_properties_are_sanitized(tmp_path: Path) -> None:
    config = _config(tmp_path)
    analytics.set_enabled(config, True)

    with pytest.raises(ValueError):
        analytics.record_event(config, "   ")
    with pytest.raises(TypeError):
        analytics.record_event(config, "install", ["not", "a", "mapping"])  # type: ignore[arg-type]

    event = analytics.record_event(
        config,
        "  install  ",
        {"nested": {"path": "/tmp/x"}, "long": "a" * (analytics.MAX_PROPERTY_VALUE_LENGTH + 10)},
    )
    assert event is not None
    assert event["name"] == "install"
    assert isinstance(event["properties"]["nested"], str)
    assert len(event["properties"]["long"]) == analytics.MAX_PROPERTY_VALUE_LENGTH


def test_clear_removes_events_but_keeps_enabled(tmp_path: Path) -> None:
    config = _config(tmp_path)
    analytics.set_enabled(config, True)
    analytics.record_event(config, "one")
    analytics.record_event(config, "two")

    assert analytics.clear(config) == 2
    assert analytics.read_events(config) == []
    assert analytics.is_enabled(config) is True
    payload = _read_json(_state_file(config))
    assert payload["events"] == []
    assert payload["enabled"] is True


def test_clear_without_state_does_not_create_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert analytics.clear(config) == 0
    assert not _state_file(config).exists()


# -- no network ----------------------------------------------------------
def test_analytics_api_never_opens_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)

    def _blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("analytics attempted network access")

    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)

    analytics.set_enabled(config, True)
    assert analytics.record_event(config, "install", {"formula": "hello"}) is not None
    assert analytics.read_events(config)
    assert analytics.clear(config) == 1


def test_analytics_module_contains_no_network_imports() -> None:
    source = Path(analytics.__file__).read_text(encoding="utf-8")
    for banned in (
        "import socket",
        "from socket",
        "import urllib",
        "from urllib",
        "import requests",
        "from requests",
        "import http",
        "from http",
    ):
        assert banned not in source


# -- CLI ----------------------------------------------------------------
def test_cli_analytics_state_default_disabled(run_cli, sample) -> None:
    proc = run_cli("analytics")
    assert proc.returncode == 0, proc.stderr
    assert "Analytics: disabled" in proc.stdout
    assert "Stored events: 0" in proc.stdout
    assert "locally" in proc.stdout
    assert not (sample.prefix / "var" / "pybrew" / "analytics.json").exists()


def test_cli_analytics_state_json(run_cli) -> None:
    proc = run_cli("analytics", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["enabled"] is False
    assert payload["event_count"] == 0
    assert payload["path"].endswith("analytics.json")


def test_cli_analytics_on_off_aliases(run_cli, sample) -> None:
    on = run_cli("analytics", "on")
    assert on.returncode == 0, on.stderr
    assert "Analytics enabled" in on.stdout
    assert "upload" in on.stdout
    assert "Analytics: enabled" in run_cli("analytics").stdout

    path = sample.prefix / "var" / "pybrew" / "analytics.json"
    assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is True

    off = run_cli("analytics", "disable")
    assert off.returncode == 0, off.stderr
    assert "Analytics disabled" in off.stdout
    assert "Analytics: disabled" in run_cli("analytics").stdout
    assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is False

    assert run_cli("analytics", "enable").returncode == 0
    assert "Analytics: enabled" in run_cli("analytics").stdout
    assert run_cli("analytics", "off").returncode == 0
    assert "Analytics: disabled" in run_cli("analytics").stdout


def test_cli_analytics_log_and_clear(run_cli, sample) -> None:
    assert run_cli("analytics", "on").returncode == 0
    config = Config(prefix=sample.prefix, cache=sample.cache)
    assert analytics.record_event(config, "install", {"formula": "hello"}) is not None
    assert analytics.record_event(config, "upgrade", {"formula": "hello"}) is not None

    log = run_cli("analytics", "log")
    assert log.returncode == 0, log.stderr
    assert "install" in log.stdout
    assert "upgrade" in log.stdout
    assert "formula=hello" in log.stdout

    limited = run_cli("analytics", "log", "--limit", "1")
    assert limited.returncode == 0, limited.stderr
    assert "upgrade" in limited.stdout
    assert "install" not in limited.stdout

    payload = json.loads(run_cli("analytics", "log", "--json").stdout)
    assert payload["count"] == 2
    assert [event["name"] for event in payload["events"]] == ["install", "upgrade"]

    cleared = run_cli("analytics", "clear")
    assert cleared.returncode == 0, cleared.stderr
    assert "Cleared 2" in cleared.stdout
    assert analytics.read_events(config) == []

    state = json.loads(run_cli("analytics", "--json").stdout)
    assert state["enabled"] is True
    assert state["event_count"] == 0
    assert json.loads(
        (sample.prefix / "var" / "pybrew" / "analytics.json").read_text(encoding="utf-8")
    )["events"] == []


def test_cli_analytics_log_empty_and_clear_empty(run_cli) -> None:
    log = run_cli("analytics", "log")
    assert log.returncode == 0, log.stderr
    assert "No analytics events recorded." in log.stdout

    cleared = run_cli("analytics", "clear")
    assert cleared.returncode == 0, cleared.stderr
    assert "Cleared 0" in cleared.stdout


def test_cli_analytics_rejects_negative_limit(run_cli) -> None:
    proc = run_cli("analytics", "log", "--limit", "-1")
    assert proc.returncode == 1
    assert "--limit" in proc.stderr


def test_cli_analytics_makes_no_network_calls(run_cli, tmp_path) -> None:
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import socket\n"
        "def _blocked(*args, **kwargs):\n"
        "    raise RuntimeError('network access attempted')\n"
        "socket.create_connection = _blocked\n"
        "socket.getaddrinfo = _blocked\n"
        "socket.socket.connect = _blocked\n",
        encoding="utf-8",
    )
    src = Path(__file__).resolve().parents[1] / "src"
    env = {"PYTHONPATH": os.pathsep.join((str(src), str(shim)))}
    for args in (
        ("analytics",),
        ("analytics", "on"),
        ("analytics", "log"),
        ("analytics", "off"),
        ("analytics", "clear"),
    ):
        proc = run_cli(*args, extra_env=env)
        assert proc.returncode == 0, (args, proc.stdout, proc.stderr)


def test_cli_install_records_local_event_when_enabled(run_cli, sample) -> None:
    assert run_cli("analytics", "on").returncode == 0
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    log = run_cli("analytics", "log", "--json")
    assert log.returncode == 0, log.stderr
    events = json.loads(log.stdout)["events"]
    assert any(event.get("name") == "install" and event.get("properties", {}).get("formula") == "hello" for event in events)
