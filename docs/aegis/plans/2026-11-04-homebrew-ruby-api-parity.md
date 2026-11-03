# Homebrew Ruby API parity — P0/P1/P2 completion plan

## Goal

Close the remaining P0/P1/P2 gaps tracked in `docs/ruby-api-gaps.md`, push the
result to `main`, and prove the installer path with a real local install.

Acceptance is evidence-based: targeted tests for every workstream, the existing
suite staying green in CI, updated coverage/gap docs, and one real `pybrew
install` of a local formula whose keg and receipt can be inspected.

## Baseline

- Base commit: `909531a` (`docs: record ruby api surface audit`).
- Test baseline: 802 collected, 797 passed, 5 skipped.
- Core method-name audit: 0 missing for formula/keg/tab/installer/formulary/
  utils/devtools/systemconfig/service/bottle/xcode/ENV.
- Remaining gaps are listed in `docs/ruby-api-gaps.md` and the "Not
  implemented / partial" section of `docs/ruby-api-coverage.md`.
- Current checkout may show a stale `MM` index; `git diff HEAD` is authoritative.

## Architecture / current owners

- `src/pybrew/ruby_executor.py`: Ruby shim, `Formulary`, `Sandbox`,
  `CompilerSelector`, `Superenv`, Keg/Tab/Service/Patch/Resource, Utils.
- `src/pybrew/installer.py` + `src/pybrew/cellar.py`: Python bottle/source
  install, receipts, linking, relocation.
- `src/pybrew/cask.py`, `cask_ruby.py`, `cask_installer.py`: cask model,
  evaluator/parser, download/artifact installation.
- `src/pybrew/formula_test.py`: formula test runner, dependency isolation,
  local-tap metadata index.
- `src/pybrew/source_builder.py`: source fetch/build environment.
- Docs: `README.md`, `docs/ruby-api-coverage.md`, `docs/ruby-api-gaps.md`,
  `docs/verification.md`, `docs/python-port-rfc.md`,
  `docs/upstream-pr/PR_BODY.md`.

## Compatibility boundary

- Default (non-Ruby) parser/install behavior must not change.
- Ruby bridges remain opt-in.
- No network in normal CI; network helpers must be injectable/fake-testable.
- No destructive changes to user prefixes; tests use `tmp_path`.
- Do not reopen the upstream RFC PR.

## Change Necessity

The remaining items are behavioral: current functions exist but return bounded
values or skip real work. Documentation-only changes cannot satisfy them; the
minimum change is in the owning modules above, with tests.

## TDD Route

- Mode: `off`
- Decision: `skipped`
- Authority: user did not request strict TDD; written requirements exist.
- Test posture: add focused regression tests with each behavior change, then run
  targeted files; CI matrix is the full-suite authority because local full-suite
  runs are I/O-bound.

## Parallel execution

Use durable teammates with disjoint write scopes. Each teammate works directly
in the shared checkout, does not commit/push, and reports changed files, tests,
and blockers. The Lead integrates, reviews the final diff, updates docs, runs
CI, and performs the real install.

| Task | Owner scope | Files |
| --- | --- | --- |
| T1 P0 Formulary cache/API + Ruby core | `formulary-core` | `ruby_executor.py`, `tests/test_ruby_api.py` |
| T2 P0 installer bottle/linkage/topology | `installer-bottle` | `installer.py`, `cellar.py`, installer tests |
| T3 P1 cask livecheck/deprecation/scripts | `cask-p1` | `cask.py`, `cask_ruby.py`, `cask_installer.py`, cask tests |
| T4 P1 test DSL version/isolation/alias/minitest | `test-dsl-p1` | `formula_test.py`, `tests/test_formula_test.py` |
| T5 P2 Ruby utilities/macOS/Keg/Service/Patch | Lead, blocked by T1 | `ruby_executor.py`, `tests/test_ruby_api.py` |
| T6 verification/install/docs/CI/push | Lead, blocked by T1–T5 | docs, verification, real install |

T5 is blocked by T1 because both write `ruby_executor.py`; T6 is blocked by all.
T2–T4 are file-disjoint and can run concurrently with T1.

## Tasks

### T1 — Formulary cache/API and Ruby core P0

- Add a path+mtime class cache to `Formulary`, invalidated by `clear_cache`.
- Complete API-struct mapping for the bounded formula struct shape used by
  `load_formula_from_struct!`; reject malformed structs explicitly.
- Add alias-only/oldname lookup fallback in Formulary factory where metadata is
  available.
- Fill the real compiler-selection gaps: probe compiler versions and honor
  `fails_with`; keep a fake-compiler test.
- Replace `Sandbox` safe stubs with a bounded executable wrapper that runs a
  command through an available sandbox binary when explicitly enabled, and
  falls back to a clearly documented no-sandbox mode otherwise. Tests use a
  fake sandbox binary.
- Extend Superenv shim path/env setup with tests; no real compiler invocation in
  CI.
- Tests: targeted `tests/test_ruby_api.py`; report exact commands.

### T2 — FormulaInstaller bottle manifest/pour/linkage/topology

- Implement a bottle manifest readback for local bottle files (files, symlinks,
  sizes, receipt) and expose it for verification.
- Ensure pour validates manifest/checksum before extraction and records the
  manifest summary in the receipt.
- Add linkage sanity checks for bottle/source kegs (dangling dylib/ELF
  references) with a clear diagnostic; tests use synthetic files.
- Make dependency ordering deterministic/topological and fail cycles clearly.
- Tests: `tests/test_installer.py`, `tests/test_real_bottle.py` (skip when no
  bottle), new synthetic bottle test.

### T3 — Cask P1

- Execute livecheck strategy blocks in a bounded evaluator (no network by
  default; injectable HTTP fetcher for tests) and store normalized results.
- Implement date-based deprecation/disable deferral semantics; tests with fixed
  dates.
- Complete script shim delegators (`arch`, caskroom/language) and document
  no-sudo behavior.
- Tests: `tests/test_cask*.py`; no network.

### T4 — Test DSL P1

- Add a fuller `Version`/`PkgVersion` token parser: numeric segments, letters,
  revisions, comparison, `major/minor/patch`, `to_s`, `pkg_version`.
- Strengthen dependency test isolation: isolated HOME/PATH/constants and
  dependency-specific testpath; tests prove a dependency cannot see the
  parent's test state.
- Add alias-only local lookup resolution where the tap index exposes aliases.
- Align Minitest skip/pattern/assertion-counter behavior with upstream tests
  that can run offline.
- Tests: `tests/test_formula_test.py`, `tests/test_version.py`.

### T5 — P2 Ruby utilities/macOS/Keg/Service/Patch

- Fill `Utils::GitHub` read API surface with an injectable curl/JSON transport
  and fake-curl tests; write operations fail explicitly (no silent success).
- Improve macOS/Xcode/CLT/SDK detection fallbacks and SystemConfig host output.
- Tighten Keg relocation/link edge cases, Service evaluator path semantics, and
  exotic Patch/Resource forms documented as bounded.
- Tests: `tests/test_ruby_api.py`, related service/patch tests.

### T6 — Verification, install, docs, CI, push

- Run targeted suites for each task, then the CI matrix.
- Create a temporary local tap/formula and run a real `pybrew install`; assert
  keg files, receipt, linkage, and `pybrew uninstall`.
- Update README/coverage/gaps/verification/PR body counts and evidence.
- Commit with plumbing if normal git index is slow; push `main`; poll CI.
- Leave goal active if any P0/P1/P2 item remains; otherwise complete.

## Verification commands

```bash
PYTHONPATH=src /opt/homebrew/anaconda3/bin/python3 -m pytest -o addopts='' -q \
  tests/test_ruby_api.py tests/test_installer.py tests/test_cask.py \
  tests/test_formula_test.py tests/test_version.py
PYTHONPATH=src /opt/homebrew/anaconda3/bin/python3 -m pybrew install ...
```

## Risks and retirement

- Same-file contention: T1 and T5 are serialized.
- Real sandbox/compiler behavior is host-dependent; tests must inject fake
  binaries and skip unsupported hosts.
- Bottle manifest/linkage heuristics must not break real cached-bottle tests.
- Retire only bounded stubs whose replacement is covered by tests; retain
  documented fallbacks where host support is unavailable.
