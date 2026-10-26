#!/usr/bin/env python3
"""Track Homebrew upstream releases/commits and report or open an issue.

This script is intentionally dependency-free (stdlib only) so the scheduled
GitHub Action and a local checkout behave identically.

State file example:

    {
      "checked_at": "2026-10-27T00:00:00+00:00",
      "brew": {"repo": "Homebrew/brew", "default_branch": "master",
                "release": "7.0.9", "head_sha": "...", ...},
      "core": {...}
    }

Exit codes:
    0  no change (or report written)
    1  --check requested and upstream changed
    2  network/API failure
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

API = "https://api.github.com"
USER_AGENT = "pybrew-upstream-tracker/1.0"
TRACKED = {
    "brew": "Homebrew/brew",
    "core": "Homebrew/homebrew-core",
}


def _request(
    url: str,
    token: Optional[str] = None,
    method: str = "GET",
    payload: Optional[dict[str, Any]] = None,
) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read()
            return json.loads(body.decode("utf-8")) if body else None
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub API {error.code} for {url}: {error.reason}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError(f"GitHub API request failed for {url}: {error}") from error


def _latest_release(repo: str, token: Optional[str]) -> Optional[dict[str, Any]]:
    try:
        release = _request(f"{API}/repos/{repo}/releases/latest", token)
    except RuntimeError:
        return None
    if not isinstance(release, dict):
        return None
    return {
        "tag": release.get("tag_name"),
        "name": release.get("name"),
        "published_at": release.get("published_at"),
        "url": release.get("html_url"),
    }


def snapshot(repo: str, token: Optional[str]) -> dict[str, Any]:
    info = _request(f"{API}/repos/{repo}", token)
    if not isinstance(info, dict):
        raise RuntimeError(f"unexpected repo response for {repo}")
    branch = info.get("default_branch") or "main"
    commit = _request(f"{API}/repos/{repo}/commits/{urllib.parse.quote(branch)}", token)
    if not isinstance(commit, dict):
        raise RuntimeError(f"unexpected commit response for {repo}@{branch}")
    commit_info = commit.get("commit") or {}
    author = (commit_info.get("author") or {}).get("date")
    message = (commit_info.get("message") or "").splitlines()[0] if commit_info.get("message") else ""
    return {
        "repo": repo,
        "default_branch": branch,
        "release": _latest_release(repo, token),
        "head_sha": commit.get("sha"),
        "head_date": author,
        "head_message": message,
        "head_url": commit.get("html_url"),
    }


def collect(token: Optional[str]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    for key, repo in TRACKED.items():
        state[key] = snapshot(repo, token)
    return state


def _short(value: Optional[str], length: int = 12) -> str:
    return (value or "?")[:length]


def changes_between(
    previous: dict[str, Any], current: dict[str, Any]
) -> list[str]:
    changes: list[str] = []
    if not previous:
        return changes
    for key in TRACKED:
        old = previous.get(key) or {}
        new = current.get(key) or {}
        repo = new.get("repo") or TRACKED[key]
        if old.get("head_sha") != new.get("head_sha"):
            changes.append(
                f"{repo} `{new.get('default_branch')}` moved "
                f"{_short(old.get('head_sha'))} → {_short(new.get('head_sha'))}"
            )
        old_release = (old.get("release") or {}).get("tag")
        new_release = (new.get("release") or {}).get("tag")
        if old_release != new_release and new_release:
            changes.append(f"{repo} latest release {old_release or '–'} → {new_release}")
    return changes


def render_report(state: dict[str, Any], changes: list[str]) -> str:
    lines = [
        "# Homebrew upstream report",
        "",
        f"Checked at: `{state.get('checked_at')}`",
        "",
    ]
    if changes:
        lines += ["## Changes since last check", ""]
        lines += [f"- {change}" for change in changes]
    else:
        lines += ["No upstream changes detected since the last check.", ""]
    for key, repo in TRACKED.items():
        item = state.get(key) or {}
        release = item.get("release") or {}
        lines += [
            "",
            f"## {repo}",
            "",
            f"- default branch: `{item.get('default_branch')}`",
            f"- latest release: `{release.get('tag') or 'n/a'}`"
            + (f" ({release.get('published_at')})" if release.get("published_at") else ""),
            f"- HEAD: `{item.get('head_sha')}` ({item.get('head_date')})",
            f"- HEAD message: {item.get('head_message')}",
            f"- {item.get('head_url')}",
        ]
    lines += [
        "",
        "## Suggested actions",
        "",
        "1. Re-run `pytest -q`.",
        "2. Refresh `tests/fixtures/core_formulas/` and `expected.json` if formula DSL changes.",
        "3. Check bottle/OCI/tag changes before the next release.",
        "",
    ]
    return "\n".join(lines)


def _find_open_issue(repo: str, token: str, title: str) -> Optional[dict[str, Any]]:
    url = f"{API}/repos/{repo}/issues?state=open&per_page=100"
    issues = _request(url, token)
    if not isinstance(issues, list):
        return None
    for issue in issues:
        if issue.get("title") == title and not issue.get("pull_request"):
            return issue
    return None


def open_or_update_issue(
    repo: str, token: str, report: str, changes: list[str]
) -> Optional[str]:
    title = "Upstream Homebrew changes detected"
    body = report + "\n\n" + "\n".join(f"- {change}" for change in changes) + "\n"
    existing = _find_open_issue(repo, token, title)
    if existing:
        _request(
            f"{API}/repos/{repo}/issues/{existing['number']}/comments",
            token,
            method="POST",
            payload={"body": body},
        )
        return existing.get("html_url")
    issue = _request(
        f"{API}/repos/{repo}/issues",
        token,
        method="POST",
        payload={"title": title, "body": body},
    )
    return issue.get("html_url") if isinstance(issue, dict) else None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=".upstream-state.json", type=Path)
    parser.add_argument("--report", default="upstream-report.md", type=Path)
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    parser.add_argument("--check", action="store_true", help="exit 1 when upstream changed")
    parser.add_argument("--open-issue", action="store_true", help="open/update a GitHub issue")
    parser.add_argument("--repo", help="owner/repo in which to open the issue")
    args = parser.parse_args(argv)

    previous: dict[str, Any] = {}
    if args.state.is_file():
        try:
            previous = json.loads(args.state.read_text())
        except (OSError, ValueError):
            previous = {}

    try:
        current = collect(args.token)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    changes = changes_between(previous, current)
    report = render_report(current, changes)
    args.report.write_text(report)
    args.state.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    print(report)

    if changes and args.open_issue:
        if not args.repo or not args.token:
            print("warning: --open-issue requires --repo and a token", file=sys.stderr)
        else:
            try:
                url = open_or_update_issue(args.repo, args.token, report, changes)
                print(f"issue: {url}")
            except RuntimeError as error:
                print(f"warning: could not update issue: {error}", file=sys.stderr)

    if args.check and changes:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
