# Homebrew parity backlog

This is the measurable inventory behind `docs/ruby-api-gaps.md`. It lists every
area still marked **Partial** and the concrete behavior that is not yet at
Homebrew parity. Completed areas remain listed at the bottom for traceability.

Last inventory: 2026-11-05 (upstream `de8798899c`).

## Summary

| Bucket | Partial areas |
| --- | ---: |
| Ruby core / Formulary / installer | 4 |
| Cask + test DSL | 5 |
| Keg, platform, Utils, patches | 0 |
| **Total** | **9 open areas / 22 concrete bullets** |

## P0 — Ruby core

### 1. Formulary source loader

- channel-specific HTTP TTL/namespace edge cases;
- factory order divergence (pybrew source-first vs Homebrew API-before-tap) and
  tap migrations.

### 2. FormulaInstaller fetch/build/deps/link/cleanup

- full Homebrew Tab/SBOM field semantics;
- exact dyld/ld.so fallback search;
- bottle-pour receipt parity.

### 3. Compiler selection / Superenv build environment

- complete compiler/linker flag family policy and response-file semantics;
- full Xcode/SDK version matrix;
- formula option/build semantics not covered by the bounded shim.

### 4. Sandbox execution

- OS-enforced seccomp/LSM policy (not just capability reporting);
- sandbox-exec capability/namespace coverage;
- arbitrary Ruby/in-process DSL writes outside the Python boundary.

## P1 — Cask and test DSL

### 5. livecheck strategy execution

- remaining upstream strategy classes (extract_plist/electron_builder/pypi/hackage/etc.);
- upstream `deprecated`/`disabled` skip statuses and SkipConditions;
- strategy-specific URL option normalization.

### 6. livecheck `url_options`

- exhaustive propagation across every strategy and redirect path;
- `using`/`verified` metadata applied to strategy fetches.

### 7. Cask script shim

- exact upstream privileged/interactive sudo semantics (current opt-in wrapper
  has no askpass/TTY handling);
- exotic Ruby script-body capture beyond modifier/method balancing.

### 8. Test DSL dependency test isolation

- true isolation on non-fork hosts (current fallback is explicit same-process);
- orphan cleanup beyond the process group.

### 9. Test DSL MacOS/OS/Hardware helpers

- full host detection (Xcode/CLT/SDK, CPU flags) instead of bounded local probing;
- pattern assertions vary with host Ruby.

## Completed during this effort

- **Service Ruby helpers** — canonical `sh.brew.*` default + aliases,
  doctor/cleanup label coverage, negative-nice validation, PATH/env warnings,
  hash persistence (round 4).
- **Test DSL arbitrary formula lookup** — paths-only catalog + on-demand tap
  root fallback; unqualified/qualified aliases beyond 20k (round 6).
- **Test DSL version object / revision** — upstream comparison table, NULL/HEAD,
  short aliases, PkgVersion revisions/hashes in Python and DSL Ruby (round 6).
- **Minitest surface** — Minitest 5.11 core assertions, pattern host-version
  branches, output/compound edges (round 6).
- **Keg internal path/link details** — relocation matrix (thin/fat Mach-O,
  ELF32/64, hardlinks, rpath/dylib-id/interpreter/codesign) with restore on
  failure (round 6).
- **MacOS SDK/Xcode/CLT detection** — Xcode bundle/version.plist, CLT SDK root,
  SDK scanning/cache reset (round 6).
- **Utils modules** — GraphQL connection pagination, REST paging, exact
  rate-limit/transport retry classification (round 6).
- **SystemConfig host details** — kernel/compiler/ruby/taps/SDK sections (round 6).
- **Patch/resource edge cases** — dynamic names, `stage` forms, exotic patch
  metadata, safe archive links (round 6).

## Execution rule

Each pass must close one or more of the concrete bullets above, add tests, run
the targeted suite, and leave the full suite green. Documentation rows move to
**Completed** only when their bullets are all implemented.
