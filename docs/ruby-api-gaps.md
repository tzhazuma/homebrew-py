# Ruby API remaining gaps

This is the durable checklist behind `docs/ruby-api-coverage.md`. It separates
**formula-facing API** from Homebrew internals so progress is measurable without
claiming full parity with every file under `Library/Homebrew`.

## Current baseline

- Full test suite: 1068 passed, 5 skipped (2026-11-04 full local suite, wave 7)
- CI: Ubuntu 3.9 / 3.12 + macOS 3.12
- Optional Ruby bridge remains opt-in; default parser behavior is unchanged.

## P0 — Ruby core

| Item | Status | Notes |
| --- | --- | --- |
| Formulary source loader | Partial | `load_formula_from_path` evaluates a metadata-only `PybrewLoadedFormula`; path+mtime class cache, stale reload, API-struct validation/cache, local API JSON, remote API JSON with ETag/Last-Modified conditional cache, bearer token and HS256 JWS verification, local tap `Aliases/` and metadata alias fallback. No multi-channel/embedded-key/RS256 or in-source alias indexing |
| Formulary class cache / reload semantics | Completed | Path+mtime class cache with flags/namespace keys, stale reload and `clear_cache` invalidation; covered by tests |
| FormulaInstaller fetch/build/deps/link/cleanup | Partial | Loaded-source flow runs fetch/build/deps/link/clean/finish; Python installer has bottle manifest pre-validation/summary, source/build metadata + keg SBOM receipt, formula-level/all-installed linkage reports (fat Mach-O/ELF, LC_RPATH stack, $LIB/$PLATFORM, DT_AUDIT diagnostics, corrupt/unsupported separation), stale-link cleanup, hardlink handling and deterministic order. Bounded dyld/ld.so approximation |
| Compiler selection / Superenv build env | Partial | PATH-based compiler probing, `CompilerFailure` selection, opt-in generated Superenv shims with flag/rpath sanitization, SDKROOT/xcrun + `-isysroot` in compile/link flags, bounded `PYBREW_ARCH`, C/C++/OBJC/OBJCXX standards, shims and wrappers. Still not full Homebrew Superenv |
| Sandbox execution | Partial | Default stays safe no-sandbox; opt-in backend auto-detects macOS `sandbox-exec`/Linux `bwrap`, collects allow/deny rules and generates bounded seatbelt profile/bwrap args via `apply!`; missing backend fails clearly. Profile is `allow default` + clauses, not a verified minimal-policy engine |

## P1 — Cask and test DSL

| Item | Status | Notes |
| --- | --- | --- |
| livecheck strategy execution | Partial | `Cask#check_livecheck`/`livecheck_status` and `pybrew livecheck --page/--online/--header/--timeout`; opt-in HTTP transport has bounded redirects, per-origin header stripping, per-host token bucket, Retry-After/jitter/retry budget, gzip/deflate and optional brotli, 5 MiB caps |
| livecheck `url_options` | Partial | Header/user-agent/etc. captured best-effort |
| Cask script shim | Partial | `sudo:` explicitly rejected; `arch`/caskroom/language delegators covered; url_options reach the stock Downloader over HTTP(S); MacOS naming remains static |
| Test DSL arbitrary formula lookup | Partial | Lightweight metadata for up to 10000 local-tap formulae; Alias/ symlinks, local API JSON and a lazy bounded source-alias scan resolve literal/qualified/dependency aliases; dynamic source-only aliases require a prior scan. Unknown aliases fail clearly |
| Test DSL dependency test isolation | Partial | Dependency tests isolate testpath/cwd/HOME/TMPDIR/PATH/PYBREW_PREFIX, pin HOMEBREW_PREFIX/HOMEBREW_CELLAR env and hide inherited `PYBREW_*` markers while restoring parent state; Ruby constants and arbitrary non-PYBREW env remain process-global |
| Test DSL version object / revision | Partial | Python `PkgVersion` and DSL `Version`/`PkgVersion` default to Homebrew null-token comparison (`1.0 == 1.0.0`), token classes, ranked pre-release, canonical hashes, revision-aware PkgVersion; explicit legacy opt-out via `PYBREW_VERSION_LEGACY=1`/`Version.legacy` |
| Test DSL MacOS/OS/Hardware helpers | Partial | sdk_path/sdk_version/release names, OS.version, CPU flags/features, Xcode/CLT/SDK helper modules exist; detection remains best-effort/local-only |
| Minitest surface | Partial | Skip-as-Exception, pattern assertion shim and assertion counter improved for offline tests; compound assertions remain approximate |

## P2 — Keg, MacOS, Utils, services

| Item | Status | Notes |
| --- | --- | --- |
| Keg internal path/link details | Partial | relative-path handling, optlink action semantics, `remove_old_aliases`, record cleanup/stale-link cleanup and all-installed linkage reports covered; relocation internals remain |
| MacOS SDK/Xcode/CLT detection | Partial | env overrides (developer dir/SDK/Xcode/CLT versions) and Xcode detection bug fix; broad host detection remains best-effort |
| Utils modules | Partial | `Utils::Path`/Git/Curl/Gzip/Tar/Zip/Cp/Popen/Bottles/Svn/Analytics/SPDX exist; GitHub has GraphQL transport plus GraphQL-first repository/PR/commits/labels/files/reviews/team/sponsorship mappings with REST fallback, bounded `first: 100`/no pagination; Sandbox rules/profile generation is bounded |
| SystemConfig host details | Partial | sections exist; host-specific output is minimal |
| Service Ruby helpers | Partial | Both literal and Ruby evaluators follow opt/keg/global paths, std_service_path_env, manual_command quoting, working/root/input/log/error paths, bounded `HOMEBREW_USER_CONFIG_HOME/services/<formula>.env` overrides, and declared vs effective env; `PYBREW_SERVICE_PATH_MODE` unifies parsers. Effective env is Homebrew-style; plist retains pybrew default PATH |
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
