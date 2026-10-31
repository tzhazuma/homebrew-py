# Ruby API remaining gaps

This is the durable checklist behind `docs/ruby-api-coverage.md`. It separates
**formula-facing API** from Homebrew internals so progress is measurable without
claiming full parity with every file under `Library/Homebrew`.

## Current baseline

- Full test suite: 714 passed, 5 skipped (after the sixth parallel batch)
- CI: Ubuntu 3.9 / 3.12 + macOS 3.12
- Optional Ruby bridge remains opt-in; default parser behavior is unchanged.

## P0 — Ruby core

| Item | Status | Notes |
| --- | --- | --- |
| Formulary source loader | Partial | `load_formula_from_path` evaluates a metadata-only `PybrewLoadedFormula`; namespace/cache/errors, `formula_class_get_from_path`/`defined_from_path?`, local-source `factory` cache and bounded struct mapping exist; API-struct loading and full tap/alias resolution missing |
| Formulary class cache / reload semantics | Missing | No cache keyed by path/mtime |
| FormulaInstaller fetch/build/deps/link/cleanup | Partial | Loaded-source flow runs fetch/build/deps/link/clean/finish and recursively installs loaded dependencies; bottle manifest/pour, Tab/service/SBOM, linkage checks and topological ordering missing |
| Compiler selection / Superenv build env | Partial | `CompilerSelector`/`CompilerFailure` surface and env-only `Superenv.setup_build_environment`; no real compiler probing/sandbox |
| Sandbox execution | Stub | `Sandbox` compatibility methods return safe defaults; no real sandbox |

## P1 — Cask and test DSL

| Item | Status | Notes |
| --- | --- | --- |
| livecheck strategy execution | Partial | Strategy block source is captured as text; never executed |
| livecheck `url_options` | Partial | Header/user-agent/etc. captured best-effort |
| Cask script shim | Partial | No sudo; `arch` uses cask arch stanza when set; MacOS naming static; url_options only reach injectable downloaders |
| Test DSL arbitrary formula lookup | Partial | Bounded tap index (50 metadata entries/run); dynamic/uninstalled/unreferenced and alias-only lookups fail clearly |
| Test DSL dependency test isolation | Partial | Dependency tests use isolated testpath/cwd/PYBREW_PREFIX/TMPDIR; PATH/HOME/global constants remain parent/global |
| Test DSL version object / revision | Partial | String-compatible `Version`/`PkgVersion` with revision-aware `pkg_version`; not Homebrew's full token parser |
| Test DSL MacOS/OS/Hardware helpers | Partial | sdk_path/sdk_version/release names, OS.version, CPU flags/features exist; versioned SDK selection and Xcode/CLT modules missing |
| Minitest surface | Partial | skip semantics, pattern assertions, assertion counter are approximate |

## P2 — Keg, MacOS, Utils, services

| Item | Status | Notes |
| --- | --- | --- |
| Keg internal path/link details | Partial | relative-path handling, optlink action semantics, some record helpers |
| MacOS SDK/Xcode/CLT detection | Partial | best-effort/local-only |
| Utils modules | Partial | `Utils::Path`/Git/Curl/Gzip/Tar/Zip/Cp/Popen; no Bottles/Svn/analytics |
| SystemConfig host details | Partial | sections exist; host-specific output is minimal |
| Service Ruby helpers | Partial | mirrors pybrew paths; unknown directives recorded as warnings |
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
