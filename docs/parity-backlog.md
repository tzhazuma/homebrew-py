# Homebrew parity backlog

This is the measurable inventory behind `docs/ruby-api-gaps.md`. It lists every
area still marked **Partial** and the concrete behavior that is not yet at
Homebrew parity. Completed areas remain listed below for traceability.

Last inventory: 2026-11-06 (upstream `de8798899c`).

## Summary

| Bucket | Partial areas |
| --- | ---: |
| Superenv / Sandbox / Cask / livecheck / installer / test DSL | 0 |
| **Total** | **0 open areas / 0 concrete bullets** |

## Remaining partial areas

None. Every area previously listed here has been implemented and verified;
the bounded residuals that remain are deliberate compatibility limits, not
unstarted checklist items. They are recorded below for transparency.

### Documented bounded residuals (not open checklist items)

- **Superenv** — the opt-in shim is not a byte-for-byte Homebrew C shim; exotic
  compiler/linker flags outside the documented families are preserved or warned
  rather than rewritten, and Xcode/SDK selection is layout/version based rather
  than a full `xcodebuild -showsdks` matrix.
- **Sandbox** — bwrap seccomp uses a caller-supplied compiled BPF; network is
  deny-all without per-host allow-lists; `sandbox-exec` has no seccomp/capability
  drop; Python declarative DSL writes use the in-process boundary.
- **Cask script** — TTY sudo is opt-in and has no password lifecycle; exact
  capture needs MRI AST or Ripper, with Python lexer fallback for exotic bodies.
- **livecheck** — listing/API parsers are heuristic; skip labels use `skipped`;
  curl transport is bounded (no full TLS/proxy/retry parity).
- **FormulaInstaller / Formulary** — deep Homebrew loader internals and full
  dyld/ld.so simulation remain bounded; bottle pouring stays in the Python
  bottle installer.
- **Test DSL** — non-fork isolation uses a subprocess boundary; host Ruby
  pattern-assertion behavior may vary.

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
- **Compiler selection / Superenv** — Python and Ruby `BuildOptions`, full
  flag/response-file families, SDK/Xcode layout matrix, installer/CLI wiring
  (round 10).
- **Cask script shim** — TTY/askpass/non-interactive sudo, AST/Ripper exact
  capture, zap directives (round 10).

## Execution rule

Each pass must close one or more concrete behaviors, add tests, run the targeted
suite, and leave the full suite green. Documentation rows move to **Completed**
only when their bullets are all implemented.
