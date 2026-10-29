"""Local, privacy-preserving usage analytics for pybrew.

Analytics are **disabled by default**.  When explicitly enabled, pybrew appends
small event records to a single JSON file inside the configured prefix
(``var/pybrew/analytics.json``).  Nothing is ever sent over the network: this
module intentionally contains no networking code and no telemetry uploader.
The state file is written atomically and kept readable only by its owner.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .paths import Config

STATE_VERSION = 1
"""Schema version stored in the analytics JSON file."""

MAX_EVENTS = 1000
"""Upper bound on retained events; the oldest events are dropped first."""

MAX_NAME_LENGTH = 200
MAX_PROPERTY_VALUE_LENGTH = 500

ANALYTICS_RELATIVE_PATH = Path("var") / "pybrew" / "analytics.json"
"""Location of the analytics state file, relative to ``config.prefix``."""


def analytics_path(config: Config) -> Path:
    """Return the local analytics state file for ``config``."""

    return config.prefix / ANALYTICS_RELATIVE_PATH


def _default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "enabled": False, "events": []}


def _clean_value(value: Any) -> Any:
    """Coerce a property value to a bounded, JSON-safe scalar."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        # NaN/Infinity are not valid JSON; store their textual form instead.
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value[:MAX_PROPERTY_VALUE_LENGTH]
    return str(value)[:MAX_PROPERTY_VALUE_LENGTH]


def _clean_properties(properties: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if properties is None:
        return {}
    if not isinstance(properties, Mapping):
        raise TypeError("event properties must be a mapping or None")
    cleaned: dict[str, Any] = {}
    for key, value in properties.items():
        name = key if isinstance(key, str) else str(key)
        cleaned[name[:MAX_NAME_LENGTH]] = _clean_value(value)
    return cleaned


def _clean_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("event name must be a string")
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("event name must not be empty")
    return cleaned[:MAX_NAME_LENGTH]


def _normalize_event(event: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Return a valid stored event, or ``None`` for unusable data."""

    name = event.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    properties = event.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}
    timestamp = event.get("timestamp")
    return {
        "timestamp": timestamp if isinstance(timestamp, str) else "",
        "name": name.strip()[:MAX_NAME_LENGTH],
        "properties": _clean_properties(properties),
    }


def _read_state(config: Config) -> dict[str, Any]:
    """Load local state; a missing or corrupt file means "disabled"."""

    path = analytics_path(config)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return _default_state()
    try:
        data = json.loads(raw)
    except ValueError:
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()

    state = _default_state()
    state["enabled"] = data.get("enabled") is True
    events = data.get("events")
    if isinstance(events, list):
        normalized: list[dict[str, Any]] = []
        for event in events:
            if isinstance(event, dict):
                cleaned = _normalize_event(event)
                if cleaned is not None:
                    normalized.append(cleaned)
        state["events"] = normalized[-MAX_EVENTS:]
    return state


def _write_state(config: Config, state: dict[str, Any]) -> None:
    """Atomically persist ``state`` with owner-only permissions."""

    path = analytics_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, sort_keys=True, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=".analytics-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _timestamp() -> str:
    """Return the current UTC time in ISO 8601 form (no machine identity)."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_enabled(config: Config) -> bool:
    """Return whether the user has opted in to local analytics."""

    return bool(_read_state(config)["enabled"])


def set_enabled(config: Config, enabled: bool) -> bool:
    """Enable or disable analytics, preserving any recorded events.

    Returns the new enabled state.
    """

    state = _read_state(config)
    state["version"] = STATE_VERSION
    state["enabled"] = bool(enabled)
    _write_state(config, state)
    return state["enabled"]


def record_event(
    config: Config, name: str, properties: Optional[Mapping[str, Any]] = None
) -> Optional[dict[str, Any]]:
    """Record one local event and return it, or ``None`` when disabled.

    Events are stored only on this machine.  Property values are coerced to
    bounded JSON scalars; nested containers are stored as their string form.
    """

    cleaned_name = _clean_name(name)
    cleaned_properties = _clean_properties(properties)
    state = _read_state(config)
    if not state["enabled"]:
        return None

    event = {
        "timestamp": _timestamp(),
        "name": cleaned_name,
        "properties": cleaned_properties,
    }
    events = state["events"]
    events.append(event)
    if len(events) > MAX_EVENTS:
        del events[: len(events) - MAX_EVENTS]
    state["version"] = STATE_VERSION
    _write_state(config, state)
    return {
        "timestamp": event["timestamp"],
        "name": event["name"],
        "properties": dict(event["properties"]),
    }


def read_events(config: Config, limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Return recorded events in chronological order.

    ``limit`` returns only the most recent ``limit`` events; ``None`` returns
    every retained event.
    """

    events = _read_state(config)["events"]
    if limit is not None:
        if limit < 0:
            raise ValueError("limit must be zero or greater")
        events = events[-limit:] if limit else []
    return [
        {
            "timestamp": event["timestamp"],
            "name": event["name"],
            "properties": dict(event["properties"]),
        }
        for event in events
    ]


def clear(config: Config) -> int:
    """Delete recorded events while keeping the enabled setting.

    Returns the number of events that were removed.
    """

    if not analytics_path(config).exists():
        return 0
    state = _read_state(config)
    removed = len(state["events"])
    state["version"] = STATE_VERSION
    state["events"] = []
    _write_state(config, state)
    return removed
