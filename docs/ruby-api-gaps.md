# Ruby API remaining gaps

This is the durable checklist behind `docs/ruby-api-coverage.md`. It separates
**formula-facing API** from Homebrew internals so progress is measurable without
claiming full parity with every file under `Library/Homebrew`.

## Current baseline

- Full test suite: 1404 passed, 6 skipped (2026-11-08 batch after Homebrew `3ccfa1d07143`)
- CI: Ubuntu 3.9 / 3.12 + macOS 3.12
- Optional Ruby bridge remains opt-in; default parser behavior is unchanged.

## P0 — Ruby core

| Item | Status | Notes |
| --- | --- | --- |
| Formulary source loader | Completed | `load_formula_from_path` plus upstream `Formulary.from_path`/`resolve_from_path`/`from_tap_path`/`from_bottle` (extracts the bottle's embedded `.brew/<name>.rb` recipe); path+mtime cache, stale reload, API-struct validation, local/channeled/remote API JSON (ETag/Last-Modified, bearer token, HS256 + RS256/PS256 JWS with embedded key/JWKS, multi-channel merge), tap `Aliases/` plus bounded in-source `formula_alias`/`alias_name` scanning. Full remote channel TTL/namespace edge cases remain bounded |
| Formulary class cache / reload semantics | Completed | Path+mtime class cache with flags/namespace keys, stale reload and `clear_cache` invalidation; covered by tests |
| FormulaInstaller fetch/build/deps/link/cleanup | Completed | Loaded-source flow runs fetch/build/deps/link/clean/finish; Python installer has bottle manifest pre-validation/summary, source/build metadata + keg SBOM + Tab-compatible receipt fields (time/source_modified_time/compiler/arch/changed_files), formula-level/all-installed linkage reports, stale-link cleanup, hardlink handling and deterministic order. Bounded dyld/ld.so approximation; `dylib_to_dep` attributes `/opt`/`Cellar` references; cleanup keeps the newest installed keg when the latest formula version is absent |
| Compiler selection / Superenv build env | Completed | Path-based compiler probing with version/target probing, dependency include/lib/PATH/PKG_CONFIG_PATH/CMAKE_PREFIX_PATH injection, opt-in Superenv shims with extended flag/rpath/response-file sanitization, SDKROOT/xcrun/SDK layout discovery, Xcode family/version matrix, arch/ARCHFLAGS, source-extension dispatch, mixed-language split API, C/C++/OBJC/OBJCXX standards/wrappers, MAKEFLAGS/deparallelize, and Python/Ruby `BuildOptions` (`--with-*`/`--without-*`, head/stable/bottle, optional/recommended deps). Bounded residuals documented in `parity-backlog.md` |
| Sandbox execution | Completed | Default stays safe no-sandbox; opt-in Ruby sandbox auto-detects `sandbox-exec`/`bwrap`, collects allow/deny rules, generates `allow default`/strict deny-default profiles or bwrap bind/unshare-net args via `apply!`; the Python build path can wrap external `system` commands with the same backends (`PYBREW_SANDBOX=1`). No seccomp/capability hardening; roots/ops are hand-written |

## P1 — Cask and test DSL

| Item | Status | Notes |
| --- | --- | --- |
| livecheck strategy execution | Completed | `Cask#check_livecheck`/`livecheck_status` and `pybrew livecheck --page/--online/--header/--timeout`; opt-in HTTP transport has bounded redirects, per-origin header stripping, per-host token bucket, Retry-After/jitter/retry budget, gzip/deflate and optional brotli, 5 MiB caps. Upstream `throttle` rate/days mutual exclusion is enforced for source casks |
| livecheck `url_options` | Completed | Header/user-agent/etc. captured best-effort |
| Cask script shim | Completed | `sudo:` is enabled by default with non-interactive `-n` (disable with `PYBREW_ALLOW_CASK_SUDO=0`), askpass (`SUDO_ASKPASS`), and opt-in TTY (`PYBREW_SUDO_TTY=1`) modes; `arch`/caskroom/language delegators covered; exact script-body capture uses MRI AST then Ripper.lex with Python lexer fallback; zap supports `trash`/`delete`/`rmdir`/`pkgutil`; installed caskfile JSON fallback; livecheck blocks/strategy and URL kwargs supported. Bounded residuals documented in `parity-backlog.md` |
| Test DSL arbitrary formula lookup | Completed | Lightweight metadata for up to 10000 local-tap formulae; Alias/ symlinks, local API JSON and a lazy bounded source-alias scan resolve literal/qualified/dependency aliases; dynamic source-only aliases require a prior scan. Unknown aliases fail clearly |
| Test DSL dependency test isolation | Completed | Dependency tests isolate testpath/cwd/HOME/TMPDIR/PATH/PYBREW_PREFIX, pin HOMEBREW_PREFIX/HOMEBREW_CELLAR env and hide inherited `PYBREW_*` markers while restoring parent state; Ruby constants and arbitrary non-PYBREW env remain process-global |
| Test DSL version object / revision | Completed | Python `PkgVersion` and DSL `Version`/`PkgVersion` match a bounded Homebrew comparison table exactly (including non-transitive zero-skip equality), Token hierarchy, canonical/legacy hashes, revision-aware PkgVersion and legacy opt-out; `pypi_packages_info`/`follow_installed_alias`/`loaded_from_metadata?` shims added; Python Token hierarchy now exposes StringToken/CompositeToken/factory/predicates |
| Test DSL MacOS/OS/Hardware helpers | Completed | sdk_path/sdk_version/release names, OS.version, expanded CPU flags/features, Xcode/CLT/SDK and DevelopmentTools helper modules exist; detection remains best-effort/local-only |
| Minitest surface | Completed | Skip-as-Exception, pattern assertion shim, assertion counter and the remaining Minitest assertion forms are covered for offline tests; host-version differences remain bounded |

## P2 — Keg, MacOS, Utils, services

| Item | Status | Notes |
| --- | --- | --- |
| Keg internal path/link details | Completed | relative-path handling, optlink, alias/record cleanup, linkage, `deleted_formula?`/`formula_preserve_rpath?` (honors recipe `preserve_rpath`), text and Mach-O/ELF binary relocation (`install_name_tool`/`patchelf`), optional ad-hoc codesign, explicit symlink retargeting, ELF `--remove-rpath` and strict bwrap hardening. Fake-tool tests; codesign signature verification/entitlements capture added, custom identity remains bounded |
| MacOS SDK/Xcode/CLT detection | Completed | env overrides (developer dir/SDK/Xcode/CLT versions) and Xcode detection bug fix; broad host detection remains best-effort |
| Utils modules | Completed | `Utils::Path`/Git/Curl/Gzip/Tar/Zip/Cp/Popen/Bottles/Svn/Analytics/SPDX exist; GitHub has GraphQL transport, GraphQL-first repository/PR/commits/labels/files/reviews/team/sponsorship with cursor pagination (10 pages) and rate-limit fields, secondary-rate-limit/Retry-After handling, REST fallback and Enterprise base URL; Sandbox rules/profile generation is bounded |
| SystemConfig host details | Completed | sections include Xcode/CLT/compiler/hardware detail; host-specific detection remains bounded |
| Service Ruby helpers | Completed | Both parsers follow opt/keg/global paths, std_service_path_env, manual_command quoting, working/root/input/log/error paths, user env overrides, declared vs effective env; manager and explicit standalone context re-read at render, no-context calls use snapshots; launchd/systemd share the effective-env PATH policy. Canonical `sh.brew.*` labels are the default with legacy aliases, doctor/cleanup recognize all label forms, negative-nice validation matches Homebrew, and ServiceSpec hash persistence is covered. Residual: custom-label doctor/cleanup needs a loader and legacy files are not auto-renamed |
| Patch/resource edge cases | Completed | static-name and repeated `resource(...).stage` supported with deterministic safe paths/caching; brace `patch { ... }` blocks now parse; dynamic resource names, `stage { }` block form and exotic patch forms remain limited |

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

## Detailed inventory

See [parity-backlog.md](parity-backlog.md) for the 0 remaining partial areas and
their concrete missing behaviors.
