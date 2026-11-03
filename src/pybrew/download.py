"""Downloading, checksum verification and the pybrew download cache."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .errors import ChecksumMismatch, DownloadError

USER_AGENT = "pybrew/0.1 (+https://github.com/tzhazuma/homebrew-py)"
DEFAULT_TIMEOUT = 60
CHUNK = 256 * 1024


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop Authorization when a redirect leaves ghcr.io."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(newurl).hostname != urllib.parse.urlparse(
            req.full_url
        ).hostname:
            new.remove_header("Authorization")
        return new


def _ghcr_token(repo: str) -> Optional[str]:
    url = "https://ghcr.io/token?" + urllib.parse.urlencode(
        {"service": "ghcr.io", "scope": f"repository:{repo}:pull"}
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            import json

            payload = json.loads(response.read().decode("utf-8"))
            token = payload.get("token") or payload.get("access_token")
            return str(token) if token else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _ghcr_repo(url: str) -> Optional[str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname != "ghcr.io":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    # /v2/<org>/<repo>/<image...>/blobs/sha256:...
    if not parts or parts[0] != "v2":
        return None
    try:
        blob_index = parts.index("blobs")
    except ValueError:
        try:
            blob_index = parts.index("manifests")
        except ValueError:
            return None
    repo_parts = parts[1:blob_index]
    if len(repo_parts) < 2:
        return None
    return "/".join(repo_parts)


def open_url(url: str, headers: Optional[Mapping[str, str]] = None):
    parsed = urllib.parse.urlparse(url)
    request_headers = {"User-Agent": USER_AGENT}
    # Local files have no HTTP request to attach headers to; ignore cask
    # ``url_options`` headers instead of putting them on the Request.
    if parsed.scheme != "file" and headers:
        request_headers.update(headers)
    repo = _ghcr_repo(url)
    if repo:
        token = _ghcr_token(repo)
        if token:
            request_headers.setdefault("Authorization", f"Bearer {token}")
    request = urllib.request.Request(url, headers=request_headers)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    return opener.open(request, timeout=DEFAULT_TIMEOUT)


class Downloader:
    """Cache-aware downloader with SHA-256 verification."""

    def __init__(
        self,
        cache_dir: Path,
        bottle_dirs: Iterable[Path] = (),
        verbose: bool = False,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.extra_dirs = [Path(path) for path in bottle_dirs]
        self.verbose = verbose

    # -- cache lookup ----------------------------------------------------
    def find_cached(self, sha256: str) -> Optional[Path]:
        if not sha256:
            return None
        for directory in [self.cache_dir, *self.extra_dirs]:
            if not directory.is_dir():
                continue
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_file() and sha256 in entry.name:
                    return entry
        return None

    # -- fetching --------------------------------------------------------
    def fetch(
        self,
        url: str,
        sha256: str,
        hint: str = "download",
        headers: Optional[Mapping[str, str]] = None,
        using: Optional[str] = None,
    ) -> Path:
        cached = self.find_cached(sha256)
        if cached is not None:
            if sha256_file(cached) == sha256:
                return cached
        safe_hint = re.sub(r"[^A-Za-z0-9._@+-]+", "-", hint).strip("-") or "download"
        destination = self.cache_dir / f"{sha256}--{safe_hint}"
        if destination.exists() and sha256_file(destination) == sha256:
            return destination
        temporary = Path(tempfile.mkstemp(prefix=".pybrew-", dir=self.cache_dir)[1])
        try:
            self._download(url, temporary, headers=headers, using=using)
            actual = sha256_file(temporary)
            if actual != sha256:
                raise ChecksumMismatch(
                    f"SHA-256 mismatch for {url}:\n  expected {sha256}\n  actual   {actual}"
                )
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination

    def fetch_unverified(self, url: str, hint: str = "head") -> Path:
        """Fetch a HEAD source URL without a checksum (Homebrew ``head`` specs)."""

        safe_hint = re.sub(r"[^A-Za-z0-9._@+-]+", "-", hint).strip("-") or "head"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        destination = self.cache_dir / f"url-{digest}--{safe_hint}"
        if destination.is_file():
            return destination
        temporary = Path(tempfile.mkstemp(prefix=".pybrew-", dir=self.cache_dir)[1])
        try:
            self._download(url, temporary)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination

    def _download(
        self,
        url: str,
        destination: Path,
        headers: Optional[Mapping[str, str]] = None,
        using: Optional[str] = None,
    ) -> None:
        strategy = str(using or "").lstrip(":").lower()
        if strategy in ("curl", "homebrew_curl"):
            self._curl_download(url, destination, headers)
            return
        if headers and urllib.parse.urlparse(url).scheme == "file":
            # A local file has no HTTP request; cask headers do not apply.
            headers = None
        try:
            # Keep the plain one-argument call for callers that replace
            # ``open_url`` while forwarding cask request headers when present.
            response = open_url(url, headers=headers) if headers else open_url(url)
            with response as source, open(destination, "wb") as handle:
                while True:
                    chunk = source.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
        except urllib.error.HTTPError as error:
            raise DownloadError(f"{url}: HTTP {error.code} {error.reason}") from error
        except (urllib.error.URLError, OSError) as error:
            raise DownloadError(f"{url}: {error}") from error

    def _curl_download(
        self,
        url: str,
        destination: Path,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        curl = shutil.which("curl")
        if curl is None:
            raise DownloadError(f"{url}: curl download strategy requested but curl is not installed")
        command = [curl, "--fail", "--location", "--silent", "--show-error", "--max-time", str(DEFAULT_TIMEOUT), "-o", str(destination)]
        for name, value in (headers or {}).items():
            command.extend(["-H", f"{name}: {value}"])
        command.append(url)
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except OSError as error:
            raise DownloadError(f"{url}: {error}") from error
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            raise DownloadError(f"{url}: curl exited {result.returncode}: {message}")


def copy_or_move(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    shutil.move(str(source), str(destination))
