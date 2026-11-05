# Homebrew parity backlog

This is the measurable inventory behind `docs/ruby-api-gaps.md`. It lists every
area still marked **Partial** and the concrete behavior that is not yet at
Homebrew parity. "Completed" means the documented API/semantics exist and are
covered by tests; it does not claim that pybrew is a line-for-line copy of
`Library/Homebrew`.

Last inventory: 2026-11-05 (upstream `de8798899c`).

## Summary

| Bucket | Partial areas |
| --- | ---: |
| Ruby core / Formulary / installer | 4 |
| Cask + test DSL | 8 |
| Keg, platform, Utils, services, patches | 6 |
| **Total** | **18** |

## P0 — Ruby core

### 1. Formulary source loader

Implemented: local path/channel/cache, `from_path`/`resolve_from_path`/
`from_tap_path`/`from_bottle`, API struct validation, ETag/Last-Modified, bearer
token, HS256 JWS, tap Aliases and bounded source aliases.

Remaining:

- multi-channel remote API subsetting (channel-specific TTL/namespace);
- embedded public-key JWKS fetch/cache and RS256 verification;
- full loader choice matrix for ambiguous tap/API/local references.

### 2. FormulaInstaller fetch/build/deps/link/cleanup

Implemented: loaded-source install flow, dependency recursion, receipt/SBOM,
bottle manifest validation, stale-link cleanup, `dylib_to_dep` attribution.

Remaining:

- full Homebrew Tab/SBOM field parity;
- exact dyld/ld.so fallback search semantics;
- Mach-O entitlements and signature verification on installed binaries.

### 3. Compiler selection / Superenv build environment

Implemented: PATH probing, opt-in generated shims, flag/rpath sanitization,
SDKROOT/xcrun/LDFLAGS, arch flags, extension dispatch, C/C++/OBJC standards.

Remaining:

- exact flag scrubbing for all compiler families and every `-Wl` form;
- complete compiler version/probe matrix (Apple clang, LLVM clang, GCC);
- MAKEFLAGS/deparallelize parity across non-Make build systems.

### 4. Sandbox execution

Implemented: opt-in `sandbox-exec`/`bwrap`, rule collection, allow-default and
strict deny-default profile generation, clear failure when no backend.

Remaining:

- verified minimal-policy engine rather than hand-written roots/ops;
- seccomp/capability/namespace hardening parity on Linux;
- complete Homebrew `Sandbox` fallback/inheritance behavior;
- Python build sandbox only wraps external `system` commands, not DSL writes.

## P1 — Cask and test DSL

### 5. livecheck strategy execution

Remaining: full upstream strategy class dispatch, deferred/date-based checks,
and strategy-specific URL option normalization.

### 6. livecheck `url_options`

Remaining: full option propagation across every strategy and redirect path.

### 7. Cask script shim

Remaining: exact `system_command`/`sudo` safety semantics; script body capture
for unbalanced lexer input; unsupported zap keys beyond
trash/delete/rmdir/pkgutil.

### 8. Test DSL arbitrary formula lookup

Remaining: dynamic source-only aliases beyond the eager/lazy scan limits;
fully lazy tap indexes for >10000 formulae.

### 9. Test DSL dependency test isolation

Remaining: same-process fallback on hosts without `fork`; parent timeout does
not clean up orphaned child processes.

### 10. Test DSL version object / revision

Remaining: exact Homebrew edge comparisons and String-compatibility nuances
(the Python hierarchy now exposes String/Composite/factory/predicates).

### 11. Test DSL MacOS/OS/Hardware helpers

Remaining: full host detection (Xcode/CLT/SDK, CPU flags) instead of bounded
local probing; pattern assertions vary with host Ruby.

### 12. Minitest surface

Remaining: exact compound assertion reflection and pattern-assertion behavior
across host Ruby versions.

## P2 — Keg, platform, Utils, services, patches

### 13. Keg internal path/link details

Remaining: entitlements/custom codesign identity/signature verification;
complete Mach-O/ELF relocation edge matrix.

### 14. MacOS SDK/Xcode/CLT detection

Remaining: broad host detection without env overrides.

### 15. Utils modules

Remaining: full GraphQL connection coverage (reviews/team/sponsorship pages)
and exact secondary-rate-limit backoff across all transports.

### 16. SystemConfig host details

Remaining: host-specific output (kernel, hardware, compiler, Ruby, taps) at
Homebrew detail level.

### 17. Service Ruby helpers

Remaining: canonical `sh.brew.*` labels vs pybrew `homebrew.mxcl.*`; explicit
service option persistence; rich service env warning surfaces.

### 18. Patch/resource edge cases

Remaining: dynamic resource names, `stage { }` block form, exotic patch forms
(brace patch blocks and target containment now implemented).

## Execution rule

Each pass must close one or more of the concrete bullets above, add tests, run
the targeted suite, and leave the full suite green. Documentation rows move to
**Completed** only when their bullets are all implemented.
