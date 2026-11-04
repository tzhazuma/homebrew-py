# Ruby API remaining gaps

This is the durable checklist behind `docs/ruby-api-coverage.md`. It separates
**formula-facing API** from Homebrew internals so progress is measurable without
claiming full parity with every file under `Library/Homebrew`.

## Current baseline

- Full test suite: 1152 passed, 5 skipped (2026-11-04 upstream sync + follow-up after Homebrew `86e082cb08`)
- CI: Ubuntu 3.9 / 3.12 + macOS 3.12
- Optional Ruby bridge remains opt-in; default parser behavior is unchanged.

## P0 — Ruby core

| Item | Status | Notes |
| --- | --- | --- |
| Formulary source loader | Partial | `load_formula_from_path` plus upstream `Formulary.from_path`/`resolve_from_path`/`from_tap_path`/`from_bottle` (extracts the bottle's embedded `.brew/<name>.rb` recipe); path+mtime cache, stale reload, API-struct validation, local/channeled/remote API JSON (ETag/Last-Modified, bearer token, HS256 JWS), tap `Aliases/` plus bounded in-source `formula_alias`/`alias_name` scanning. No full multi-channel remote subsetting/embedded-key/RS256 |
| Formulary class cache / reload semantics | Completed | Path+mtime class cache with flags/namespace keys, stale reload and `clear_cache` invalidation; covered by tests |
| FormulaInstaller fetch/build/deps/link/cleanup | Partial | Loaded-source flow runs fetch/build/deps/link/clean/finish; Python installer has bottle manifest pre-validation/summary, source/build metadata + keg SBOM + Tab-compatible receipt fields (time/source_modified_time/compiler/arch/changed_files), formula-level/all-installed linkage reports, stale-link cleanup, hardlink handling and deterministic order. Bounded dyld/ld.so approximation |
| Compiler selection / Superenv build env | Partial | PATH-based compiler probing, opt-in Superenv shims with flag/rpath sanitization, SDKROOT/xcrun/LDFLAGS, arch/ARCHFLAGS, source-extension dispatch, bounded `-Wa,*` allow-list + diagnostics, mixed-language split API/no forced `-x`, C/C++/OBJC/OBJCXX standards/wrappers. Still not full Homebrew Superenv |
| Sandbox execution | Partial | Default stays safe no-sandbox; opt-in backend auto-detects `sandbox-exec`/`bwrap`, collects allow/deny rules and generates `allow default`/strict deny-default profiles or stricter bwrap bind/unshare-net args via `apply!`; missing backend fails clearly. No seccomp/capability hardening; roots/ops are hand-written |

## P1 — Cask and test DSL

| Item | Status | Notes |
| --- | --- | --- |
| livecheck strategy execution | Partial | `Cask#check_livecheck`/`livecheck_status` and `pybrew livecheck --page/--online/--header/--timeout`; opt-in HTTP transport has bounded redirects, per-origin header stripping, per-host token bucket, Retry-After/jitter/retry budget, gzip/deflate and optional brotli, 5 MiB caps. Upstream `throttle` rate/days mutual exclusion is enforced for source casks |
| livecheck `url_options` | Partial | Header/user-agent/etc. captured best-effort |
| Cask script shim | Partial | `sudo:` explicitly rejected; `arch`/caskroom/language delegators covered; url_options reach the stock Downloader over HTTP(S); MacOS naming remains static. `zap` supports `trash`/`delete`/`rmdir`/`pkgutil` with bounded root checks |
| Test DSL arbitrary formula lookup | Partial | Lightweight metadata for up to 10000 local-tap formulae; Alias/ symlinks, local API JSON and a lazy bounded source-alias scan resolve literal/qualified/dependency aliases; dynamic source-only aliases require a prior scan. Unknown aliases fail clearly |
| Test DSL dependency test isolation | Partial | Dependency tests isolate testpath/cwd/HOME/TMPDIR/PATH/PYBREW_PREFIX, pin HOMEBREW_PREFIX/HOMEBREW_CELLAR env and hide inherited `PYBREW_*` markers while restoring parent state; Ruby constants and arbitrary non-PYBREW env remain process-global |
| Test DSL version object / revision | Partial | Python `PkgVersion` and DSL `Version`/`PkgVersion` match a bounded Homebrew comparison table exactly (including non-transitive zero-skip equality), Token hierarchy, canonical/legacy hashes, revision-aware PkgVersion and legacy opt-out; `pypi_packages_info`/`follow_installed_alias`/`loaded_from_metadata?` shims added |
| Test DSL MacOS/OS/Hardware helpers | Partial | sdk_path/sdk_version/release names, OS.version, CPU flags/features, Xcode/CLT/SDK helper modules exist; detection remains best-effort/local-only |
| Minitest surface | Partial | Skip-as-Exception, pattern assertion shim and assertion counter improved for offline tests; compound assertions remain approximate |

## P2 — Keg, MacOS, Utils, services

| Item | Status | Notes |
| --- | --- | --- |
| Keg internal path/link details | Partial | relative-path handling, optlink, alias/record cleanup, linkage, `deleted_formula?`/`formula_preserve_rpath?` (honors recipe `preserve_rpath`), text and Mach-O/ELF binary relocation (`install_name_tool`/`patchelf`), optional ad-hoc codesign, explicit symlink retargeting, ELF `--remove-rpath` and strict bwrap hardening. Fake-tool tests; no entitlements/custom identity/signature verification |
| MacOS SDK/Xcode/CLT detection | Partial | env overrides (developer dir/SDK/Xcode/CLT versions) and Xcode detection bug fix; broad host detection remains best-effort |
| Utils modules | Partial | `Utils::Path`/Git/Curl/Gzip/Tar/Zip/Cp/Popen/Bottles/Svn/Analytics/SPDX exist; GitHub has GraphQL transport, GraphQL-first repository/PR/commits/labels/files/reviews/team/sponsorship with cursor pagination (10 pages) and rate-limit fields, REST fallback and Enterprise base URL; Sandbox rules/profile generation is bounded |
| SystemConfig host details | Partial | sections exist; host-specific output is minimal |
| Service Ruby helpers | Partial | Both parsers follow opt/keg/global paths, std_service_path_env, manual_command quoting, working/root/input/log/error paths, user env overrides, declared vs effective env; manager and explicit standalone context re-read at render, no-context calls use snapshots; PATH policy differs by renderer |
| Patch/resource edge cases | Partial | static-name and repeated `resource(...).stage` supported with deterministic safe paths/caching; dynamic resource names, `stage { }` block form and exotic patch forms remain limited |

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
