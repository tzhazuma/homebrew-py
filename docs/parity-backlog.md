# Homebrew parity backlog

This is the measurable inventory behind `docs/ruby-api-gaps.md`. It lists every
area still marked **Partial** and the concrete behavior that is not yet at
Homebrew parity. Completed areas remain listed at the bottom for traceability.

Last inventory: 2026-11-05 (upstream `de8798899c`).

## Summary

| Bucket | Partial areas |
| --- | ---: |
| Superenv / Cask script | 2 |
| **Total** | **2 open areas / 5 concrete bullets** |

## Remaining partial areas

### 1. Compiler selection / Superenv build environment

- complete compiler/linker flag family policy and response-file semantics;
- full Xcode/SDK layout/version matrix;
- formula option/build semantics not covered by the bounded shim.

### 2. Cask script shim

- exact upstream interactive TTY/privileged sudo semantics;
- exact script-body capture fallback on non-MRI Ruby (AST path requires MRI).

## Completed during this effort

- **Service Ruby helpers** — canonical labels/aliases, doctor/cleanup coverage,
  negative-nice validation, PATH/env warnings, hash persistence (round 4).
- **Test DSL arbitrary formula lookup** — paths-only catalog + on-demand tap
  root fallback (round 6).
- **Test DSL version object / revision** — upstream comparison table, NULL/HEAD,
  short aliases, PkgVersion revisions/hashes (round 6).
- **Minitest surface** — Minitest 5.11 core assertions and host-version branches
  (round 6).
- **Keg internal path/link details** — relocation matrix, hardlink dedupe,
  restore-on-failure, codesign/entitlements (round 6).
- **MacOS SDK/Xcode/CLT detection** — bundle/version.plist/CLT SDK root/cache
  reset (round 6).
- **Utils modules** — GraphQL/REST pagination, exact rate-limit/transport retry
  classification (round 6).
- **SystemConfig host details** — kernel/compiler/ruby/taps/SDK sections (round 6).
- **Patch/resource edge cases** — dynamic names, stage forms, exotic patch
  metadata, safe archive links (round 6).
- **Formulary source loader** — per-URL cache/TTL, factory order trace,
  API-first switch, tap migrations (round 7).
- **FormulaInstaller** — bottle metadata, full Tab/SBOM fields and receipt
  round-trip (round 7).
- **Test DSL dependency isolation** — fork default + subprocess boundary on
  non-fork hosts (round 7).
- **Test DSL MacOS/OS/Hardware helpers** — sysctl/proc/cpuinfo/xcodebuild/clang
  host detection (round 7).
- **Sandbox execution** — bwrap BPF seccomp, capability/namespace reporting, and
  arbitrary Ruby interpreter execution inside the shared sandbox plan (round 9).
- **livecheck strategy execution** — all upstream strategy class names dispatch,
  extract_plist archive extraction, real header_match headers (round 9).
- **livecheck url_options** — header/user-agent/referer/cookies/using/verified
  propagation and safe redirect header stripping (round 9).

## Execution rule

Each pass must close one or more of the concrete bullets above, add tests, run
the targeted suite, and leave the full suite green. Documentation rows move to
**Completed** only when their bullets are all implemented.
