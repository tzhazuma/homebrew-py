"""Guarded upstream PR submission helper.

This script is intentionally inert unless ``--authorized`` is passed.  It is not
run automatically; the repository owner reviews the PR body in
``docs/upstream-pr/PR_BODY.md`` and explicitly authorizes submission.

Examples:
    # Preview only (no writes):
    python scripts/open_upstream_pr.py

    # After explicit human approval, update PR #24362 and try to reopen it:
    python scripts/open_upstream_pr.py --authorized --pr 24362

    # After explicit human approval, open a new PR:
    python scripts/open_upstream_pr.py --authorized --new
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

API = "https://api.github.com"
USER_AGENT = "pybrew-upstream-pr/1.0"
REQUIRED_MARKERS = [
    "Don't delete these checkboxes",
    "I did not use AI/LLM",
    "Have you followed our",
]


def _token() -> Optional[str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    return None


def _api(
    method: str,
    path: str,
    token: str,
    payload: Optional[dict[str, Any]] = None,
    allow: tuple[int, ...] = (),
) -> tuple[int, Any]:
    url = path if path.startswith("http") else API + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        body = error.read().decode()
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"message": body}
        if error.code in allow:
            return error.code, parsed
        raise RuntimeError(f"{method} {url} -> HTTP {error.code}: {body[:1000]}") from error


def _update_fork_file(token: str, owner: str, branch: str, content: str) -> None:
    path = "docs/python-reimplementation-rfc.md"
    status, existing = _api(
        "GET",
        f"/repos/{owner}/brew/contents/{path}?ref={branch}",
        token,
        allow=(404,),
    )
    payload: dict[str, Any] = {
        "message": "docs: update Python reimplementation RFC",
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if status == 200 and isinstance(existing, dict):
        payload["sha"] = existing["sha"]
    result_status, result = _api(
        "PUT", f"/repos/{owner}/brew/contents/{path}", token, payload
    )
    print(f"fork file updated: HTTP {result_status} {result.get('commit', {}).get('sha', '')[:12]}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body", type=Path, default=Path("docs/upstream-pr/PR_BODY.md"))
    parser.add_argument("--title", type=Path, default=Path("docs/upstream-pr/PR_TITLE.txt"))
    parser.add_argument("--rfc", type=Path, default=Path("docs/python-port-rfc.md"))
    parser.add_argument("--pr", type=int, default=24362, help="existing PR to update/reopen")
    parser.add_argument("--new", action="store_true", help="open a new PR instead of updating --pr")
    parser.add_argument("--fork-owner", default="tzhazuma")
    parser.add_argument("--branch", default="python-port-rfc")
    parser.add_argument("--base", default="main")
    parser.add_argument("--repo", default="Homebrew/brew")
    parser.add_argument(
        "--authorized",
        action="store_true",
        help="required for any write; only pass after the human has approved this exact body",
    )
    args = parser.parse_args(argv)

    body = args.body.read_text()
    title = args.title.read_text().strip()
    rfc = args.rfc.read_text()
    missing = [marker for marker in REQUIRED_MARKERS if marker not in body]
    if missing:
        print(f"error: PR body is missing template markers: {missing}", file=sys.stderr)
        return 2
    if "- [ ] I did not use AI/LLM" in body:
        print(
            "warning: the AI-disclosure checkbox is still unchecked. The submitting "
            "human must review the body and tick it (or the equivalent statement) before submission.",
            file=sys.stderr,
        )

    print(f"title: {title}")
    print(f"head: {args.fork_owner}:{args.branch} -> {args.repo}:{args.base}")
    print(f"body: {args.body} ({len(body)} bytes)")
    if not args.authorized:
        print("\nPreview only. No writes performed. Re-run with --authorized after human approval.")
        return 0

    token = _token()
    if not token:
        print("error: no GitHub token (set GITHUB_TOKEN or store a git credential)", file=sys.stderr)
        return 2

    try:
        _update_fork_file(token, args.fork_owner, args.branch, rfc)
        if args.new:
            status, pr = _api(
                "POST",
                f"/repos/{args.repo}/pulls",
                token,
                {
                    "title": title,
                    "head": f"{args.fork_owner}:{args.branch}",
                    "base": args.base,
                    "body": body,
                    "draft": False,
                },
            )
            print(f"created PR: {pr.get('html_url')}")
        else:
            status, pr = _api("GET", f"/repos/{args.repo}/pulls/{args.pr}", token)
            print(f"updating PR #{args.pr}: {pr.get('html_url')} (state {pr.get('state')})")
            status, updated = _api(
                "PATCH",
                f"/repos/{args.repo}/pulls/{args.pr}",
                token,
                {"body": body, "title": title},
            )
            if updated.get("state") == "closed":
                try:
                    status, reopened = _api(
                        "PATCH",
                        f"/repos/{args.repo}/pulls/{args.pr}",
                        token,
                        {"state": "open"},
                    )
                    print(f"reopen request: {reopened.get('state')}")
                except RuntimeError as error:
                    print(f"could not reopen automatically: {error}")
                    print("Ask a maintainer or reopen from the GitHub UI.")
        time.sleep(3)
        if not args.new:
            _, final = _api("GET", f"/repos/{args.repo}/pulls/{args.pr}", token)
            print(
                f"final state: {final.get('state')} "
                f"(draft={final.get('draft')}) {final.get('html_url')}"
            )
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
