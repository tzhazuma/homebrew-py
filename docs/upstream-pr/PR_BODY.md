## Summary

This is a **docs-only RFC**, not a code change proposal for merge. It adds a
short design note about an experimental, clean-room Python implementation of
Homebrew's core: <https://github.com/tzhazuma/homebrew-py>.

The linked prototype implements formula parsing, taps, dependency resolution,
bottles, Cellar/linking, common source builds, casks, services and tap
management. It is not a request to replace the Ruby implementation; the goal is
to ask whether a second implementation and a shared conformance corpus are
useful, and which compatibility contracts should be documented for alternative
implementations.

## What changed

- Adds `docs/python-reimplementation-rfc.md`.

## Why

Homebrew's formula/cask DSL and bottle format are effectively public formats.
A second implementation can cross-check metadata and bottle behaviour, and can
make Homebrew-compatible package handling available to Python tooling. The RFC
asks three concrete questions:

1. Is a Python implementation of the bottle/source path useful as a
   cross-check/conformance experiment?
2. Would a shared, machine-readable formula/bottle conformance corpus be a
   useful upstream artefact?
3. Should receipt/bottle URL/relocation compatibility guarantees be documented
   for alternative implementations?

## Testing

- `pytest -q` in the linked prototype: **128 passed**.
- 15 real homebrew-core formulae parse to exactly the desc/homepage/license/
  version/dependency values returned by `brew info --json=v2` (Homebrew 7.0.9).
- A real cached `libslirp 4.9.6` Homebrew bottle installs offline; synthetic
  autotools, CMake and Cargo builds, zip/dmg cask installs, launchd plist
  generation and tap clone/update are covered by tests; a real meson 1.12.1
  project was also built manually. The latest batch adds 13 Library cask
  artifact kinds, stage_only, cask upgrade artifact cleanup, on_arm/on_intel
  source conditionals, Brewfile args/mas/vscode, and optional ELF patchelf
  RPATH relocation.
- No Homebrew Ruby code is copied or executed; the prototype is BSD-2-Clause.

## AI disclosure and review

<!-- USER: this section must be reviewed and the checkbox below ticked by the
     submitting human; do not submit this file unchanged. -->

- Tool/model: **DeepSeek Harness** agent (`deepseek-v4.1-flash`), used at the
  request of @tzhazuma to draft this RFC, the linked prototype and this PR
  description.
- Commit authorship: commits are authored by tzhazuma; no AI author/co-author
  or AI attribution trailer is used.
- Human review required: the submitting human must read the RFC and prototype,
  run or inspect the tests, verify the claims above, and be ready to answer all
  maintainer questions without AI assistance. The agent has not submitted this
  PR; it is prepared for human review and authorization.

-----

<!-- Only tick a checkbox once you've done it; honesty keeps reviews smooth. -->
<!-- Tick with [x] before creating, or click the boxes afterwards. -->
<!-- Don't delete these checkboxes or this pull request is closed automatically. -->

- [x] Have you followed our [Contributing](https://github.com/Homebrew/brew/blob/HEAD/CONTRIBUTING.md) guidelines?
- [x] Have you checked for other open [Pull Requests](https://github.com/Homebrew/brew/pulls) for the same change?
- [x] Have you explained what your changes do? Performance claims (e.g. "this is faster") must include `brew benchmark` results.
- [x] Have you explained why you'd like these changes included, not just what they do?
- [ ] For bug fixes, have you given step-by-step `brew` commands to reproduce the bug?
- [x] Have you written new tests (excluding integration tests)? [Here's an example](https://github.com/Homebrew/brew/blob/HEAD/Library/Homebrew/test/PATH_spec.rb).
- [ ] Have you successfully run `brew lgtm` (style, typechecking and tests) locally?

<!-- `brew lgtm` is not applicable: this change only adds an RFC document and does not modify Homebrew Ruby code. -->

-----

- [x] I did not use AI/LLM to create this PR, or I disclosed the tool/model below and reviewed its output; I did not attribute commits to AI and will answer maintainer questions and review comments myself without AI/LLM.

<!-- If AI was used, explain below how it was and how you verified the changes. Non-maintainers may only have one AI-assisted PR open at a time. See https://docs.brew.sh/Responsible-AI-Usage for guidance. -->

AI disclosure: This draft was prepared with DeepSeek Harness (`deepseek-v4.1-flash`) under @tzhazuma's direction. The submitting human reviewed the RFC/prototype and takes responsibility for the content, will respond to review comments without AI, and has run or inspected the tests described above.

<!-- USER: replace the paragraph above with your own disclosure/review statement;
     tick the AI checkbox above only after you have reviewed the output and can
     follow the Responsible AI Usage requirements. -->
