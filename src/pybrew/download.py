"""Downloading, checksum verification and the pybrew download cache."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

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


def open_url(url: str, headers: Optional[dict[str, str]] = None):
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
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
    def fetch(self, url: str, sha256: str, hint: str = "download") -> Path:
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
            self._download(url, temporary)
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

    def _download(self, url: str, destination: Path) -> None:
        try:
            with open_url(url) as response, open(destination, "wb") as handle:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
        except urllib.error.HTTPError as error:
            raise DownloadError(f"{url}: HTTP {error.code} {error.reason}") from error
        except (urllib.error.URLError, OSError) as error:
            raise DownloadError(f"{url}: {error}") from error


def copy_or_move(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    shutil.move(str(source), str(destination))
