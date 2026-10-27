# Upstream PR review and submission

This folder holds the **draft upstream PR** for Homebrew/brew. It is intentionally
not submitted by the agent in this session. The repository owner reviews,
edits/approves it, and explicitly authorizes submission.

## Current state

- Previous PR: <https://github.com/Homebrew/brew/pull/24362> — closed ~12 seconds
  after creation by Homebrew's PR-template bot because the body lacked
  `.github/PULL_REQUEST_TEMPLATE.md`.
- Fork branch with the RFC file: `tzhazuma/brew:python-port-rfc`
- The bot comment says it will reopen the PR once the template is complete.
- Homebrew's Contributing rules require AI/LLM disclosure, human review of all
  AI-authored content, and that the submitting human answers review comments
  without AI. There may only be one open AI-assisted PR from a non-maintainer.

## Files

| File | Purpose |
| --- | --- |
| `PR_BODY.md` | Full PR body, including the current Homebrew template with checkboxes and AI disclosure section |
| `PR_TITLE.txt` | PR title |
| `scripts/open_upstream_pr.py` | Guarded helper that updates the fork branch/RFC and PR body, or opens a new PR, only with `--authorized` |

## What the human must review/decide

1. **Scope**: is a docs-only RFC the right upstream contribution? Homebrew may
   prefer a discussion or decide this is out of scope. The PR is not a request
   to merge the Python implementation.
2. **Claims**: read `docs/python-port-rfc.md` and `docs/verification.md`; verify
   the test count and feature claims against the repository.
3. **AI disclosure**: review the disclosure in `PR_BODY.md`, replace it with
   your own wording if needed, and tick the AI checkbox only if you can follow
   Homebrew's Responsible AI Usage requirements (including answering review
   comments yourself without AI).
4. **Template checkboxes**: tick every applicable box. `brew lgtm` is left
   unchecked because this is a docs-only change; run it or explain why it is not
   applicable in the PR body.
5. **Old PR**: decide whether to update/reopen #24362 or open a new PR from the
   same fork branch. The helper defaults to updating #24362 and then attempting
   to reopen it.

## After you approve

Option A — you submit through the GitHub UI:
1. Edit <https://github.com/Homebrew/brew/pull/24362> (or create a new PR from
   `tzhazuma:python-port-rfc`), paste `PR_BODY.md`, complete the human-only
   checkboxes.
2. The template bot may reopen the PR automatically.

Option B — ask the agent to submit:
1. Reply in the session that you approve and authorize submission, and state
   whether to update #24362 or create a new PR.
2. The agent will run:

```bash
python scripts/open_upstream_pr.py --authorized --pr 24362
# or, for a new PR:
python scripts/open_upstream_pr.py --authorized --new
```

Without `--authorized`, the script only prints a preview and performs no writes.

## Guardrails

- The script refuses to submit when the template checkboxes are missing.
- It never force-pushes; it writes one RFC file commit to the existing fork
  branch and updates the PR body via the API.
- It does not close/reopen other people's PRs or comment on unrelated issues.
- If GitHub rejects reopening or creating the PR, the script prints the reason
  and leaves the state untouched for the human to handle.
