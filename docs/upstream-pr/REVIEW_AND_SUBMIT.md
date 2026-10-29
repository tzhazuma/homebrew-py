# Upstream PR review and submission

This folder holds the upstream PR material for Homebrew/brew. The repository
owner reviewed and explicitly authorized submission; the PR was submitted,
reopened by the template bot, and then closed by a Homebrew maintainer as out
of scope.

## Current state

- PR: <https://github.com/Homebrew/brew/pull/24362> — **closed** on 2026-10-28
  by maintainer MikeMcQuaid ("Not interested in this, sorry.").
- The PR had been reopened by Homebrew's PR-template bot after the authorized
  template/AI-disclosure update.
- Fork branch with the RFC file: `tzhazuma/brew:python-port-rfc`
- Body includes the Homebrew template checkboxes and AI disclosure; the AI
  checkbox was checked only after the human confirmed review/authorization.
- `brew lgtm` is unchecked with a docs-only note.
- Do **not** reopen or resubmit against the maintainer's decision. The human
  submitter owns any future upstream communication and must not use AI to answer
  review comments.

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

## Submission history

The human authorized updating and reopening #24362. The agent ran:

```bash
python scripts/open_upstream_pr.py --authorized --pr 24362
```

The PR body was updated and the template bot reopened the PR. The agent then
marked the PR ready for review (GraphQL `markPullRequestReadyForReview`) only
after the human explicitly confirmed they had reviewed the body and authorized
checking the AI disclosure.

Without `--authorized`, the script only prints a preview and performs no writes.

## Guardrails

- The script refuses to submit when the template checkboxes are missing.
- It never force-pushes; it writes one RFC file commit to the existing fork
  branch and updates the PR body via the API.
- It does not close/reopen other people's PRs or comment on unrelated issues.
- If GitHub rejects reopening or creating the PR, the script prints the reason
  and leaves the state untouched for the human to handle.
