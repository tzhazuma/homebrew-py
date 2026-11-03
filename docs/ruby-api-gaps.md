# Ruby API remaining gaps

This is the durable checklist behind `docs/ruby-api-coverage.md`. It separates
**formula-facing API** from Homebrew internals so progress is measurable without
claiming full parity with every file under `Library/Homebrew`.

## Current baseline

- Full test suite: 851 passed, 5 skipped (2026-11-04 full local suite)
- CI: Ubuntu 3.9 / 3.12 + macOS 3.12
- Optional Ruby bridge remains opt-in; default parser behavior is unchanged.

## P0 — Ruby core

| Item | Status | Notes |
| --- | --- | --- |
| Formulary source loader | Partial | `load_formula_from_path` evaluates a metadata-only `PybrewLoadedFormula`; path+mtime class cache, stale reload, API-struct validation/cache, namespace/errors and bounded alias/oldname fallback now exist; full API JSON and full tap-directory/alias resolution remain bounded |
| Formulary class cache / reload semantics | Completed | Path+mtime class cache with flags/namespace keys, stale reload and `clear_cache` invalidation; covered by tests |
| FormulaInstaller fetch/build/deps/link/cleanup | Partial | Loaded-source flow runs fetch/build/deps/link/clean/finish, recursively installs loaded dependencies, writes a receipt readable via Tab; Python installer now has bottle manifest pre-validation/summary, opt-in linkage checks and deterministic topological ordering. Full Tab/SBOM and formula-level linkage semantics remain bounded |
| Compiler selection / Superenv build env | Partial | PATH-based compiler probing/fake-binary tests and `CompilerFailure` selection; env-only `Superenv.setup_build_environment`. No real build-through compiler/sandbox engine |
| Sandbox execution | Partial | Default stays safe no-sandbox; opt-in `PYBREW_SANDBOX=1` + `PYBREW_SANDBOX_BINARY` wrapper runs explicit commands and records failures. Real policy/backend sandbox engine remain missing |

## P1 — Cask and test DSL

| Item | Status | Notes |
| --- | --- | --- |
| livecheck strategy execution | Partial | Bounded strategy block execution with injectable fetcher/page and normalized result storage; no CLI/network transport wiring |
| livecheck `url_options` | Partial | Header/user-agent/etc. captured best-effort |
| Cask script shim | Partial | `sudo:` explicitly rejected; `arch`/caskroom/language delegators covered; url_options reach the stock Downloader over HTTP(S); MacOS naming remains static |
| Test DSL arbitrary formula lookup | Partial | Lightweight metadata for up to 10000 local-tap formulae; Alias/ symlink metadata now resolves literal/qualified/dynamic aliases with clear unknown failures. Source-only aliases still require canonical loading |
| Test DSL dependency test isolation | Partial | Dependency tests isolate testpath/cwd/HOME/TMPDIR/PATH/PYBREW_PREFIX and restore parent state; HOMEBREW_PREFIX/HOMEBREW_CELLAR and arbitrary inherited env remain process-global |
| Test DSL version object / revision | Partial | String-compatible `Version`/`PkgVersion` with case-insensitive letter ordering and revision-aware formatting/comparison; still not Homebrew's full token parser |
| Test DSL MacOS/OS/Hardware helpers | Partial | sdk_path/sdk_version/release names, OS.version, CPU flags/features, Xcode/CLT/SDK helper modules exist; detection remains best-effort/local-only |
| Minitest surface | Partial | Skip-as-Exception, pattern assertion shim and assertion counter improved for offline tests; compound assertions remain approximate |

## P2 — Keg, MacOS, Utils, services

| Item | Status | Notes |
| --- | --- | --- |
| Keg internal path/link details | Partial | relative-path handling, optlink action semantics, some record helpers |
| MacOS SDK/Xcode/CLT detection | Partial | env overrides (developer dir/SDK/Xcode/CLT versions) and Xcode detection bug fix; broad host detection remains best-effort |
| Utils modules | Partial | `Utils::Path`/Git/Curl/Gzip/Tar/Zip/Cp/Popen/Bottles/Svn/Analytics/SPDX and bounded GitHub read API exist; GitHub PR/team/sponsorship write surface and real backends remain bounded |
| SystemConfig host details | Partial | sections exist; host-specific output is minimal |
| Service Ruby helpers | Partial | mirrors pybrew paths; unknown directives recorded and surfaced by CLI |
| Patch/resource edge cases | Partial | exotic patch forms and dynamic resource iteration limited |

## P3 — Engineering

| Item | Status |
| --- | --- |
| Docs/counts/verification sync | Each batch |
| Full suite + CI | Each push |
| Upstream sync | Daily 09:00 Asia/Shanghai (scheduled task) |
| Upstream PR | Closed; do not reopen/resubmit |

## Parallel work ownership

Active batches use disjoint file scopes:

- Lead: integration, docs, CI, final review
- `cask-api`: `src/pybrew/cask*.py`, cask tests
- `test-dsl`: `src/pybrew/formula_test.py`, its tests
- `ruby-core`: `src/pybrew/ruby_executor.py`, `tests/test_ruby_api.py`
- `compiler-build`: `src/pybrew/source_builder.py`, `src/pybrew/installer.py`, their tests

Write scopes are advisory; the Lead reviews the final diff and runs the full
suite before committing.
