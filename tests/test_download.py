"""Downloader tests: request headers for cask ``url_options`` (no network)."""

from __future__ import annotations

import hashlib
import io
import urllib.request
from pathlib import Path
from typing import Optional

import pytest

from pybrew import download as download_module
from pybrew.download import USER_AGENT, Downloader


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._stream = io.BytesIO(payload)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.closed = True
        return False


class _RecordingOpener:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: Optional[int] = None):
        self.requests.append(request)
        return _FakeResponse(self.payload)


def _install_fake_transport(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> _RecordingOpener:
    opener = _RecordingOpener(payload)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    return opener


def _request_headers(request: urllib.request.Request) -> dict[str, str]:
    # urllib normalises header names (``X-Test`` -> ``X-test``); compare
    # case-insensitively, like an HTTP server would.
    return {name.lower(): value for name, value in request.header_items()}


def test_fetch_forwards_headers_to_http_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"cask archive bytes"
    digest = hashlib.sha256(payload).hexdigest()
    opener = _install_fake_transport(monkeypatch, payload)

    archive = Downloader(tmp_path / "cache").fetch(
        "https://example.com/Tool-1.0.zip",
        digest,
        headers={
            "Authorization": "Bearer token",
            "X-Test": "yes",
            "User-Agent": "custom-agent",
        },
    )

    assert archive.read_bytes() == payload
    assert len(opener.requests) == 1
    headers = _request_headers(opener.requests[0])
    assert headers["authorization"] == "Bearer token"
    assert headers["x-test"] == "yes"
    # A caller-supplied User-Agent overrides the pybrew default.
    assert headers["user-agent"] == "custom-agent"


def test_fetch_without_headers_keeps_default_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"plain archive bytes"
    digest = hashlib.sha256(payload).hexdigest()
    opener = _install_fake_transport(monkeypatch, payload)

    archive = Downloader(tmp_path / "cache").fetch(
        "https://example.com/Plain-1.0.zip", digest
    )

    assert archive.read_bytes() == payload
    assert _request_headers(opener.requests[0]) == {"user-agent": USER_AGENT}


def test_file_url_fetch_ignores_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"local archive bytes"
    source = tmp_path / "Local.zip"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    real_build_opener = urllib.request.build_opener
    captured: list[urllib.request.Request] = []

    def recording_build_opener(*handlers):
        opener = real_build_opener(*handlers)
        real_open = opener.open

        def recording_open(request, *args, **kwargs):
            captured.append(request)
            return real_open(request, *args, **kwargs)

        opener.open = recording_open  # type: ignore[method-assign]
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", recording_build_opener)

    archive = Downloader(tmp_path / "cache").fetch(
        source.as_uri(),
        digest,
        headers={"Authorization": "Bearer secret", "X-Test": "yes"},
    )

    assert archive.read_bytes() == payload
    assert len(captured) == 1
    # Local files never carry HTTP request headers.
    assert _request_headers(captured[0]) == {"user-agent": USER_AGENT}


def test_file_url_does_not_forward_headers_to_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"local archive bytes"
    source = tmp_path / "Local.zip"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    captured: list[Optional[dict[str, str]]] = []
    original_open_url = download_module.open_url

    def recording_open_url(url, headers=None):
        captured.append(dict(headers) if headers is not None else None)
        return original_open_url(url, headers)

    monkeypatch.setattr(download_module, "open_url", recording_open_url)

    archive = Downloader(tmp_path / "cache").fetch(
        source.as_uri(), digest, headers={"X-Test": "yes"}
    )

    assert archive.read_bytes() == payload
    # ``fetch`` accepts headers, but a file:// URL never forwards them.
    assert captured == [None]
