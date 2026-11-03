# Ruby API remaining gaps

This is the durable checklist behind `docs/ruby-api-coverage.md`. It separates
**formula-facing API** from Homebrew internals so progress is measurable without
claiming full parity with every file under `Library/Homebrew`.

## Current baseline

- Full test suite: 967 passed, 5 skipped (2026-11-04 full local suite, wave 4)
- CI: Ubuntu 3.9 / 3.12 + macOS 3.12
- Optional Ruby bridge remains opt-in; default parser behavior is unchanged.

## P0 — Ruby core

| Item | Status | Notes |
| --- | --- | --- |
| Formulary source loader | Partial | `load_formula_from_path` evaluates a metadata-only `PybrewLoadedFormula`; path+mtime class cache, stale reload, API-struct validation/cache, local API JSON loading (`PYBREW_FORMULA_API_JSON`), local tap `Aliases/` resolution and metadata alias/oldname fallback exist. Remote/JWS API JSON and in-source alias indexing remain bounded |
| Formulary class cache / reload semantics | Completed | Path+mtime class cache with flags/namespace keys, stale reload and `clear_cache` invalidation; covered by tests |
| FormulaInstaller fetch/build/deps/link/cleanup | Partial | Loaded-source flow runs fetch/build/deps/link/clean/finish; Python installer has bottle manifest pre-validation/summary, source/build metadata + keg SBOM receipt, formula-level linkage diagnostics, stale-link cleanup, hardlink handling and deterministic topological ordering. Full Tab semantics and non-heuristic linkage remain bounded |
| Compiler selection / Superenv build env | Partial | PATH-based compiler probing/fake-binary tests, `CompilerFailure` selection, opt-in generated Superenv shims with bounded flag/rpath sanitization, macOS SDKROOT/xcrun detection, bounded C/C++ `-std` flags and shlex compiler wrappers. No full Homebrew Superenv engine |
| Sandbox execution | Partial | Default stays safe no-sandbox; opt-in backend auto-detects macOS `sandbox-exec` or Linux `bwrap` and runs explicit commands, external fake wrapper still supported, missing backend fails clearly. Granular rule/profile policy remains missing |

## P1 — Cask and test DSL

| Item | Status | Notes |
| --- | --- | --- |
| livecheck strategy execution | Partial | `Cask#check_livecheck`/`livecheck_status` plus `pybrew livecheck --page/--online`; default HTTP transport (`livecheck_http_fetcher`) is opt-in via `online`, bounded to 5 MiB http/https, strategy/regex structured status. No redirect/header/retry/content-encoding pipeline |
| livecheck `url_options` | Partial | Header/user-agent/etc. captured best-effort |
| Cask script shim | Partial | `sudo:` explicitly rejected; `arch`/caskroom/language delegators covered; url_options reach the stock Downloader over HTTP(S); MacOS naming remains static |
| Test DSL arbitrary formula lookup | Partial | Lightweight metadata for up to 10000 local-tap formulae; Alias/ symlinks, local API JSON and a lazy bounded source-alias scan resolve literal/qualified/dependency aliases; dynamic source-only aliases require a prior scan. Unknown aliases fail clearly |
| Test DSL dependency test isolation | Partial | Dependency tests isolate testpath/cwd/HOME/TMPDIR/PATH/PYBREW_PREFIX, pin HOMEBREW_PREFIX/HOMEBREW_CELLAR env and hide inherited `PYBREW_*` markers while restoring parent state; Ruby constants and arbitrary non-PYBREW env remain process-global |
| Test DSL version object / revision | Partial | Python `PkgVersion` plus DSL `Version`/`PkgVersion` with segments/major/minor/patch, ranked pre-release tokens, revision-aware formatting/comparison; documented deviations from Homebrew token classes remain |
| Test DSL MacOS/OS/Hardware helpers | Partial | sdk_path/sdk_version/release names, OS.version, CPU flags/features, Xcode/CLT/SDK helper modules exist; detection remains best-effort/local-only |
| Minitest surface | Partial | Skip-as-Exception, pattern assertion shim and assertion counter improved for offline tests; compound assertions remain approximate |

## P2 — Keg, MacOS, Utils, services

| Item | Status | Notes |
| --- | --- | --- |
| Keg internal path/link details | Partial | relative-path handling, optlink action semantics, `remove_old_aliases` (opt/linked/versioned), record cleanup/stale-link cleanup covered; relocation edge cases remain |
| MacOS SDK/Xcode/CLT detection | Partial | env overrides (developer dir/SDK/Xcode/CLT versions) and Xcode detection bug fix; broad host detection remains best-effort |
| Utils modules | Partial | `Utils::Path`/Git/Curl/Gzip/Tar/Zip/Cp/Popen/Bottles/Svn/Analytics/SPDX exist; GitHub method-name surface is complete with injectable REST reads and explicit write guards, but PR/team/sponsorship semantics remain single-page/bounded. Sandbox policy names exist but raise explicit NotImplementedError |
| SystemConfig host details | Partial | sections exist; host-specific output is minimal |
| Service Ruby helpers | Partial | Ruby evaluator now follows Homebrew opt/keg/global path semantics, std_service_path_env, manual_command quoting and working/log/input paths; literal parser intentionally keeps pybrew paths; user env override file unsupported |
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
