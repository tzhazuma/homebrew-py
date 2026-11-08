# Verification record

## Full parity batch 2026-11-05

Closed the documented partial areas across all owners; full suite grew from
1156 to **1404 passed, 6 skipped**.

- **Cask**: evaluator `zap` now captures trash/delete/rmdir/pkgutil; livecheck
  URL kwargs merge into request headers; installed caskfile falls back to JSON;
  the script shim covers caskroom/arch/language/MacOS naming and rejects sudo.
- **Test DSL**: forked dependency-test isolation, eager dynamic source aliases
  for small taps, expanded MacOS/OS/Hardware/Xcode/CLT/SDK and DevelopmentTools
  helpers, upstream-style Token subclasses/factory/predicates, and remaining
  Minitest assertion forms.
- **Superenv/build**: dependency include/lib/env paths, MAKEFLAGS and
  `deparallelize`, compiler version/target probing, opt-in Python build
  sandbox (`PYBREW_SANDBOX=1`) with sandbox-exec/bwrap plans, and patch target
  containment via dry-run verification.
- **Services/caveats/post_install**: unified launchd/systemd effective-env PATH
  policy, Homebrew-style manual command quoting, root/working/log path
  separation, interval timers, expanded caveats platform/path/formula context,
  and isolated post_install HOME/TMPDIR/HOMEBREW_TEMP context.
- **Ruby shim**: expanded sandbox rules/profile/apply, Formulary JWS/envelope
  and remote API handling, codesign verification/entitlements helpers, GitHub
  rate-limit helpers, and resource stage support.
- **CLI search**: when no local tap matches, `pybrew search` now forwards to
  `https://formulae.brew.sh/api/formula.json`; `search --cask` uses
  `cask.json`. `PYBREW_FORMULA_SEARCH_API`/`PYBREW_CASK_SEARCH_API` override the
  index (file:// fixtures work).
- Real install verification: `REAL INSTALL VERIFIED`.
- Commits `57d927e` + `54fa7c5`; CI `41973914882` green on Ubuntu 22.04 py3.9, Ubuntu latest py3.12 and macOS py3.12.
- Inventory of the remaining 0 partial areas: `docs/parity-backlog.md`.


## Upstream sync 2026-11-08

- Upstream: Homebrew/brew `650524b3b2` → `3ccfa1d071`; Homebrew/homebrew-core
  `77174ea4211d` → `0320d5a7ac3d`.
- Formula/cask-relevant changes implemented:
  - `Formula#installed_kegs`/`#installed_prefixes` now accept optional formula
    names (used for aliases/renames);
  - `Formulary.to_rack` falls back to a unique installed rack matching
    `possible_names` and the formula's tap when the canonical rack is absent.
- Cask `sha256` `should_return` guard was reviewed; pybrew's bounded Ruby cask
  shim already keeps scalar/getter forms separate and needs no change.
- Full suite: **1404 passed, 6 skipped**; real install verified.

## Upstream sync 2026-11-07

- Upstream: Homebrew/brew `970da0cd77` → `650524b3b2`; Homebrew/homebrew-core
  `0c9f6dcddcee` → `77174ea4211d`.
- Reviewed the changed Ruby files (vendor/bundle excluded). Formula/cask/service
  relevant: `cmd/info` now resolves dependents through `Formula#possible_names`
  (aliases/oldnames); `cask/upgrade` fails explicitly for named unupgradable
  casks. Implemented alias/oldname-aware `Dependents:` in `pybrew info`; cask
  upgrade already exits non-zero with an explicit error.
- Other changes were FormulaInstaller dependency-expansion/bottle-compat
  internals, diagnostic grouping, rubocops, dev commands and vendored gems;
  pybrew's bounded equivalents were reviewed and no applicable API changes
  required code changes this round.
- Full suite: **1404 passed, 6 skipped**; real install verified.
- Commit `13ebbe8`; CI `42392978728` green on Ubuntu 22.04 py3.9, Ubuntu latest py3.12 and macOS py3.12.

## CLI/cask compatibility 2026-11-07 (round 12)

- Cask scripts: `sudo:` / `sudo_as_root:` are enabled by default; set
  `PYBREW_ALLOW_CASK_SUDO=0` to fail closed. TTY remains opt-in via
  `PYBREW_SUDO_TTY=1`; default remains non-interactive (`-n`) or askpass.
- CLI: Homebrew command aliases (`ls`, `rm`/`remove`, `up`, `instal`,
  `uninstal`, `dr`, `abv`, `homepage`, `lc`, `-S`) and Homebrew-style output
  (`==> Installing/Pouring/Uninstalling/Upgrading`, `🍺` keg summary,
  `==> Formulae`/`==> Casks` search headings, `Warning:` on stderr).
- zsh: `~/.zshrc` defines `pybrew`, pins `PYBREW_ALLOW_CASK_SUDO` default 1,
  and was verified with `zsh -ic 'whence -w pybrew; pybrew --version'`.
- Full suite: **1403 passed, 6 skipped**; real install verified.
- Commit `b046b1f`; CI `42270261512` green on Ubuntu 22.04 py3.9, Ubuntu latest py3.12 and macOS py3.12.

## Upstream sync 2026-11-06 (round 11)

- Upstream: Homebrew/brew `de8798899c` → `970da0cd77` (release 7.0.12,
  2026-11-02); Homebrew/homebrew-core `ee8b2bc9` → `0c9f6dcddcee`.
- Reviewed the 51 changed Ruby files. Formula/cask/service-relevant changes
  implemented:
  - livecheck `crate` strategy now uses the crates.io sparse index
    (`index.crates.io/<prefix>/<package>`), parses NDJSON, uses `vers`, and
    skips yanked releases;
  - `autoremove` now excludes pinned formulae, matching Homebrew's safe-removal
    behavior.
- Other upstream changes were bundle-extension/CLI internals (cargo/flatpak/
  winget/vscode, untap/update-report/commands/diagnostic/unpack) outside
  pybrew's implemented command surface; noted as non-applicable rather than
  silently ignored.
- Full suite: **1400 passed, 6 skipped**; real install verified.
- Commit `67029eb`; CI `42205969095` green on Ubuntu 22.04 py3.9, Ubuntu latest py3.12 and macOS py3.12.

## Residual parity batch 2026-11-06 (round 10)

- Superenv: Python `BuildOptions` parity (with/without/head/stable/bottle,
  optional/recommended/head deps, deprecated-option remap), installer/CLI
  wiring (`--with-*`/`--without-*`, `--build-bottle`), Ruby-bridge
  `PybrewBuild` reading the same options, extended response-file/flag families,
  developer-dir/CLT/Xcode-bundle/SDK layout discovery and matrix.
- Cask script: opt-in interactive TTY sudo (`PYBREW_SUDO_TTY=1`, pty-tested),
  non-MRI Ripper.lex exact capture with Python lexer fallback.
- Full suite: **1399 passed, 6 skipped**; real install verified.
- Open backlog: **0 areas / 0 bullets**. Bounded residuals are documented in
  `docs/parity-backlog.md` and are deliberate compatibility limits.
- Commit `5ddae56`; CI `42066444820` green on Ubuntu 22.04 py3.9, Ubuntu latest py3.12 and macOS py3.12.

## Residual parity batch 2026-11-05 (round 9)

- livecheck: extract_plist now fetches/extracts zip/tar Info.plist in memory,
  header_match uses real response headers, curl/homebrew_curl transport switching
  with bounded redirects and sensitive-header stripping, verified/using options.
- Sandbox: arbitrary Ruby interpreter execution now runs inside the same
  sandbox-exec/bwrap plan (build/keg/temp writable roots), plus existing bwrap
  BPF seccomp and capability/namespace reporting.
- Full suite: **1380 passed, 6 skipped**; real install verified.
- Commit `d09ffce`; CI `42004199333` green on Ubuntu 22.04 py3.9, Ubuntu latest py3.12 and macOS py3.12.

## Residual parity batch 2026-11-05 (round 8)

- Cask/livecheck: all upstream strategy class names now dispatch with bounded
  parsers (apache/bitbucket/cpan/crate/gnome/gnu/github_releases/header_match/
  launchpad/npm/ruby_gems/sourceforge/xorg + existing); using/verified option
  validation; exact script-body capture via Ruby AST block ranges.
- Sandbox: bwrap OS-enforced seccomp via compiled BPF fd (`PYBREW_SECCOMP_BPF`,
  `--seccomp FD`, pass_fds), capability/namespace reporting; explicit
  sandbox-exec boundary.
- Superenv: remaining -Xlinker/-Xpreprocessor/-Xassembler/response-file forms,
  Xcode version matrix and SDK selection warnings.
- Full suite: **1371 passed, 6 skipped**; real install verified.
- Commits `12f03cd` + `20d0d43`; CI `41994420972` green on Ubuntu 22.04 py3.9, Ubuntu latest py3.12 and macOS py3.12.

## Residual parity batch 2026-11-05 (round 7)

- Formulary: per-URL sidecar cache/TTL namespace, factory order trace,
  API-first switch, tap migrations/renames, qualified API fallback.
- FormulaInstaller: loaded bottle-block metadata, full Tab/SBOM fields,
  cxxstdlib/built_on/used options and round-trip receipt parity.
- Test DSL: true subprocess isolation on non-fork hosts, sysctl/proc/cpuinfo
  model/cores and xcodebuild/clang host detection.
- Cask: electron_builder/extract_plist/pypi/hackage/json/xml/yaml livecheck
  strategies, using/verified option enforcement, sudo askpass/non-interactive
  contract, exotic script capture coverage.
- Superenv: remaining -Xlinker/-Xpreprocessor/-Xassembler and response-file
  families, Xcode version matrix, LSM/user-namespace capability reporting.
- Full suite: **1356 passed, 6 skipped**; real install verified.
- Commit `5407976`; CI `41991366147` green on Ubuntu 22.04 py3.9, Ubuntu latest py3.12 and macOS py3.12.

## Residual parity batch 2026-11-05 (round 6)

- Cask: opt-in sudo-aware `system_command` (fake-sudo tested), five common
  livecheck strategy classes (page_match/sparkle/github_latest/git/url) with URL
  option normalization, and modifier-aware script-body capture.
- Test DSL: upstream version comparison table (68 pairs), Minitest 5.11 assertion
  surface, NULL/HEAD/Token edges, and on-demand tap-root lookup beyond the 20k
  catalog.
- Superenv: response-file expansion, toolchain/include/linker flag families,
  SDK version matrix/cross-target checks, bwrap namespace/capability hardening,
  explicit seccomp-unsupported reporting.
- Ruby shim: Mach-O/ELF relocation matrix (thin/fat, rpath/dylib-id/interpreter,
  hardlink dedupe, restore-on-failure, codesign), exotic patch/resource metadata
  and `stage` forms, GraphQL/REST pagination and exact rate-limit classification,
  Xcode/CLT/SDK detection and richer SystemConfig.
- Completed rows: Test DSL lookup/version/Minitest, Keg relocation, MacOS SDK,
  Utils, SystemConfig, Patch/resource (plus Service earlier).
- Full suite: **1336 passed, 6 skipped**; real install verified.
- Commits `1a7955a` + `d136179`; CI `41989145325` green on Ubuntu 22.04 py3.9, Ubuntu latest py3.12 and macOS py3.12.

## Residual parity batch 2026-11-05 (round 4)

- Cask: zap `early_script`/`launchctl`/`kext`/`script` actions with bounded
  fake-tool execution and directive ordering; deferred/date-based livecheck
  skips; brace livecheck parsing; literal `url_options` propagation.
- Test DSL: paths-only lazy catalog for >10000-name taps with bounded source
  prefix scan; short alias/HEAD Version semantics and canonical token hashes;
  explicit non-fork isolation capability mode.
- Services: canonical `sh.brew.*` labels are now the default with legacy
  aliases; doctor/cleanup label recognition; negative-nice validation; service
  option hash persistence.
- Superenv: minimum macOS/arch matrix and cross-target warnings, space-safe
  dependency flags, sandbox backend capability reporting (seccomp/capability
  drop availability), stricter sandbox boundary tests.
- Ruby shim: Formulary loader ambiguity matrix/channel namespace edges, 50-page
  GraphQL reviews/team/sponsorships/tiers, local Xcode/CLT/SDK detection and
  richer SystemConfig, FormulaInstaller Tab/SBOM receipt fields.
- Python Version: short pre-release aliases (`a4`/`b2`), HEAD ordering, and
  canonical raw-token hashes.
- Full suite: **1311 passed, 6 skipped**; real install verified.
- Commits `b5e0dca` + `72ec58a`; CI `41984727966` green on Ubuntu 22.04 py3.9, Ubuntu latest py3.12 and macOS py3.12.

## Residual parity batch 2026-11-05 (round 3)

- Cask: zap `quit`/`signal`/`login_item` (fake-tool tested), balanced
  `def`/`class`/`module` script-body capture with deterministic warnings, and
  literal-parser livecheck URL kwargs/strategy/regex metadata.
- Test DSL: large-tap (>500 names) dynamic source aliases with an 8 KiB/2000
  source bound, fork process-group timeout/reap, and Minitest reflection/pattern
  edge coverage.
- Superenv: pkg-config integration for installed dependencies, compiler family
  probe matrix, joined/separated `-Wl` rpath scrubbing, and strict sandbox
  wrapper tests.
- Services: canonical `sh.brew.*` labels with legacy aliases, ServiceSpec hash
  round-trip, env-override warnings, Homebrew-style default log paths.
- Ruby shim: channel TTL/key cache, 50-page GraphQL pagination and rate-limit
  retries, broad local Xcode/CLT/SDK detection, dynamic resources/stage forms,
  custom codesign identity.
- CLI: online search index caching (`PYBREW_SEARCH_CACHE_DIR`/`TTL`), doctor
  reports installed formulae deleted from their tap.
- Full suite: **1272 passed, 6 skipped**; real install verified.
- Commits `310c7cd` + `84db07d`; CI `41976608255` green on Ubuntu 22.04 py3.9, Ubuntu latest py3.12 and macOS py3.12.

## Scheduled upstream check 2026-11-05 09:00

- Homebrew/brew `6ceeda0fc6` → `de8798899c`; homebrew-core `28bbf746` → `ee8b2bc9`.
- The runtime diff is RuboCop-only (`std_go_args`, `components_redundancy`); no
  formula/cask/service Ruby API change, so no shim change was required.
- Full suite: **1156 passed, 5 skipped**.
- Real install verification: `REAL INSTALL VERIFIED`.


## Upstream sync 2026-11-05 — Homebrew `6ceeda0fc6`

- Advanced `.upstream-state.json` from `86e082cb08` to `6ceeda0fc6`
  (Homebrew/brew `main`, release 7.0.12; core `28bbf746`).
- Implemented the actionable upstream delta:
  - `dylib_to_dep` attribution for missing `/opt` and `/Cellar` references,
    exposed on `LinkageIssue.dep`.
  - Cleanup keeps the newest installed keg when the latest formula version is
    not installed, instead of deleting it because an older keg is opt-linked.
  - `CaskLoader.from_content` now removes its temporary source directory and
    preserves the source in `.metadata/Casks/<token>.rb` for uninstall/zap.
- Full suite: **1156 passed, 5 skipped**.
- Real install verification: `REAL INSTALL VERIFIED`.
- CI run `41897391401` for `1987b92`: Ubuntu 22.04 py3.9, Ubuntu latest py3.12 and macOS py3.12 all green.


Date: 2026-10-27 (Asia/Shanghai)
Revision: pybrew 0.1.0

## Environment

| Item | Value |
| --- | --- |
| Host | macOS 27.2 (Darwin), arm64 hardware |
| Host Homebrew | 7.0.9 (`cdc1ca5032`), x86_64 via Rosetta, prefix `/usr/local` |
| Python used | CPython 3.12.7 (`/opt/homebrew/anaconda3/bin/python3`) |
| Test runner | pytest 7.4.4 |
| Repo | `homebrew-py` at the commit that contains this file |

`brew info --json=v2` was used to generate the expected metadata for the corpus
tests. The local brew runs as x86_64, therefore corpus tests set
`PYBREW_TARGET_OS=darwin` and `PYBREW_TARGET_ARCH=x86_64` to compare the same
platform-specific dependency set.

## Test suite

```console
$ PYTHONPATH=src python -m pytest -o addopts='' -q
1156 passed, 5 skipped (local full suite, upstream sync follow-up 2026-11-04)
```

Breakdown of the feature slices:

```console
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_real_formulas.py
17 passed      # 15 parametrized real formulae + 2 aggregate checks
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_bottles.py
10 passed
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_cask.py tests/test_cask_ruby.py
72 passed      # cask base + Ruby evaluator + artifacts + uninstall actions + os versions
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_cask_depth.py tests/test_cask_pkg_system.py tests/test_cask_pkg_uninstall.py tests/test_cask_rollback.py
38 passed      # depth/system install/uninstall/rollback
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_source_build.py tests/test_ruby_dsl.py tests/test_ruby_api.py
134 passed     # declarative + opt-in Ruby bridge/API + Keg/Tab/Formulary + caveats
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_patches.py tests/test_post_install.py
47 passed      # patch url/file/:DATA application + post_install
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_services.py tests/test_service_ruby.py
84 passed      # literal service DSL + opt-in arbitrary Ruby evaluator
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_formula_test.py
27 passed      # formula test DSL + CLI + expected-status shell_output
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_caveats.py
22 passed      # formula caveats DSL + CLI
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_cask_caveats.py
6 passed       # cask caveats block/literal + CLI
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_cask_scripts.py
27 passed      # cask install/uninstall script shim
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_services.py
8 passed       # launchd + systemd unit generation, orphan cleanup
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_taps.py
5 passed
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_maintenance.py
9 passed
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_bundle.py
16 passed      # install/check/list/dump, tap URL, args, mas/vscode, restart_service
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_cli.py
13 passed
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_relocation.py
9 passed       # Mach-O padding + ELF patchelf RPATH/PT_INTERP
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_analytics.py
20 passed      # local-only analytics
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_linux_real.py
4 skipped      # opt-in OrbStack/Linux verification (arm64 + x86_64)
```

## Real formula parity

For every formula in `tests/fixtures/core_formulas/`, the Python parser produced
exactly the same values as Homebrew 7.0.9's JSON for:

- `desc`, `homepage`, `license`, `versions.stable`, `revision`, `keg_only`;
- runtime, build and test dependencies;
- `uses_from_macos`;
- bottle block presence (all 15 have bottles).

Formula sources were fetched from `Homebrew/homebrew-core@main` through the
authenticated GitHub contents API. Their SHA-256 values are recorded in
`expected.json`; 14/15 also match the `ruby_source_checksum` in the local
Homebrew package index (curl's source was updated after the local index was
generated; its parsed metadata still matches `brew info`).

## Real bottle installation

`tests/test_real_bottle.py` extracts the formula embedded in the cached
`libslirp 4.9.6` Homebrew bottle, attaches the bottle's real SHA-256, and installs
it into a temporary prefix with `--ignore-dependencies` semantics.

```console
$ PYTHONPATH=src python -m pytest -q tests/test_relocation.py -v
2 passed
$ PYTHONPATH=src python -m pytest -q tests/test_real_bottle.py -v
1 passed in 0.03s
```

Verified in the resulting keg:

- `Cellar/libslirp/4.9.6/lib/libslirp.dylib`
- `Cellar/libslirp/4.9.6/include/slirp/libslirp.h`
- `INSTALL_RECEIPT.json` with `poured_from_bottle: true` and `runtime_dependencies: ["glib"]`
- text placeholders (`@@HOMEBREW_PREFIX@@`) relocated, including `lib/pkgconfig/slirp.pc`

The offline test reads the bottle from `~/Library/Caches/Homebrew/downloads`.
A separate live verification downloaded the same bottle from ghcr.io through
pybrew's anonymous OCI token flow:

```console
$ PYTHONPATH=src python -m pybrew --prefix /tmp/pybrew-ghcr \
    --tap tests/fixtures/core_formulas --cache /tmp/pybrew-ghcr-cache \
    install --ignore-dependencies libslirp
Poured from bottle: libslirp 4.9.6 (/tmp/pybrew-ghcr/Cellar/libslirp/4.9.6)
```

The cached blob SHA-256 matched the formula, the keg receipt says
`poured_from_bottle: true`, and `file` identifies the installed dylib as a
Mach-O arm64 shared library. After the `install_name_tool` fix, `otool -L`
shows the final prefix rather than `@@HOMEBREW_PREFIX@@`:

```text
/tmp/pybrew-ghcr2/Cellar/libslirp/4.9.6/lib/libslirp.0.dylib:
    /tmp/pybrew-ghcr2/opt/libslirp/lib/libslirp.0.dylib
    /tmp/pybrew-ghcr2/opt/glib/lib/libglib-2.0.0.dylib
    /usr/lib/libresolv.9.dylib
    /usr/lib/libSystem.B.dylib
```

`grep -c '@@HOMEBREW'` returns 0 and `codesign -v` succeeds.

## Real source build (GNU hello)

The real `tests/fixtures/core_formulas/hello.rb` formula was installed with
`--build-from-source` into a temporary prefix. pybrew downloaded the GNU hello
source, ran `./configure` + `make install` via the install DSL, and produced a
working binary:

```console
$ PYTHONPATH=src python -m pybrew --prefix /tmp/pybrew-hello-prefix \
    --tap tests/fixtures/core_formulas --cache /tmp/pybrew-hello-cache \
    install --build-from-source hello
Installed: hello 2.12.3 (/tmp/pybrew-hello-prefix/Cellar/hello/2.12.3)

$ /tmp/pybrew-hello-prefix/bin/hello --greeting=pybrew
pybrew
```

The receipt says `poured_from_bottle: false`, `built_as_bottle: false`.
This is a real homebrew-core formula, not a synthetic fixture.

## Cask verification

- `tests/test_cask.py` parses real cask definitions (`rectangle`, `iterm2`,
  `visual-studio-code`) and installs a synthetic `.zip` cask: app copied to
  `<prefix>/Applications`, binary symlinked into `<prefix>/bin`, receipt written,
  uninstall and `--zap` verified.
- A synthetic `.dmg` is created with `hdiutil`, mounted, and its `.app` installed.
- A synthetic `.pkg` is built with `pkgbuild`, expanded with `pkgutil --expand-full`,
  and its payload installed under an explicit `--pkg-root`; uninstall removes the
  files. Package scripts are intentionally not executed.
- Unsafe zip members (`../`) are rejected before extraction.
- formulae.brew.sh cask JSON is mapped to the same `Cask` model (`--api`),
  including app targets, binary targets, uninstall and zap; the loader can fall
  back to the API when no local cask tap exists.
- Cask `outdated` and `upgrade` are covered: upgrading replaces the app/binary
  artifacts, keeps shared paths, and removes the superseded Caskroom version.
- Cask uninstall runs `quit` through a fake osascript, `pkgutil --forget` through
  a fake pkgutil, and removes a `delete` leftover inside the temporary root;
  other actions are skipped.

## Services verification

- `parse_service_block` resolves `opt_bin`, `var` and `std_service_path_env`.
- Plist generation is tested for ProgramArguments, KeepAlive, RunAtLoad,
  WorkingDirectory, log paths and EnvironmentVariables.
- start/stop/list are tested through a fake `launchctl` that records arguments;
  the real launchd is not touched.
- CLI `services start/list/stop` is covered with `PYBREW_SERVICE_DIR` and
  `PYBREW_LAUNCHCTL` overrides.
- A systemd backend renders a unit with `ExecStart`/`Restart`/`Environment`/
  `WorkingDirectory` and drives a fake `systemctl --user` with daemon-reload,
  start and stop; `PYBREW_SERVICE_BACKEND=systemd` selects it.

## Tap verification

- A local git remote is cloned into `<prefix>/Library/Taps/tzhazuma/homebrew-demo`,
  loaded by `FormulaLoader`, updated with a second commit, listed, and untapped.
- CLI `tap --url`, `tap --list` and `untap` are covered.

## Maintenance verification

- `doctor --json` returns structured issues and detects a removed cask artifact
  in addition to the existing prefix/Cellar/link checks.
- `outdated` compares installed keg versions with the tap formula's
  `pkg_version` and supports `--json`.
- `upgrade --dry-run` resolves the newer formula and reports the plan without
  downloading or writing a keg.
- `cleanup` keeps the opt-linked keg (or newest) and removes older kegs;
  `--dry-run` is covered.
- `reinstall` uninstalls then reinstalls a formula and restores links.

## Brewfile verification

`bundle install` parses a Brewfile containing `brew "hello"` and
`cask "testapp"`, performs a dry run, installs both, and then `bundle check`
reports the dependencies as satisfied; `bundle list` prints the parsed entries.
Unsupported Brewfile lines fail with a line number.

## Bundle dump and cache cleanup

- `bundle dump` writes `tap`/`brew`/`cask` lines for installed state, refuses to
  overwrite without `--force`, and the result can be fed back to `bundle check`.
- Brewfile `tap ..., url: "file://..."` installs a local tap, and
  `brew ..., link: false` installs the keg without bin/opt links; `install
  --no-link` exposes the same behaviour directly.
- `cleanup --prune-cache --cache-age-days 30` removes old cached downloads; both
  dry-run and real runs are covered.
- `pin` prevents `outdated` reporting, `list --pinned` shows it, and `unpin`
  restores outdated detection.
- A real upgrade installs the new keg, rewrites `bin` links, and removes the
  superseded keg; the old bottle is copied to the new version name for the test.
- `autoremove` removes a dependency after its only explicit dependent is
  uninstalled; dry-run leaves it in place.
- `leaves` omits dependency kegs and `uses dep` reports `hello-dep`.

## Resource staging

A synthetic formula declares a `resource "data"` tar.gz and uses
`resource("data").stage { bin.install "data.txt" }`; the installer downloads the
resource via the regular cache, verifies sha256, stages it under the build root,
and installs the file into the keg. Zip resources use the same path with
path-traversal checks.

## Real Linux via OrbStack

OrbStack's running Ubuntu arm64 machine was used for a real Linux run (shared
workspace path, passwordless sudo). patchelf 0.18.0 was installed with apt.

```console
$ orb -m ubuntu bash -lc 'cd /Users/azuma/Documents/deepseek-harness/default-workspace/homebrew-py && /usr/bin/python3 scripts/linux_verify.py --clean'
PASS: 2 real Linux bottle(s) verified (libslirp, tree)
```

- `libslirp 4.9.6`, tag `arm64_linux`, ghcr.io, SHA-256 verified
- `tree 2.3.2`, tag `arm64_linux`, ghcr.io, SHA-256 verified
- `readelf -d` / `patchelf --print-rpath` showed the final prefix with no
  `@@HOMEBREW` placeholders; the relocated `tree --version` actually ran.

An x86_64 OrbStack run (`orb start ubuntu-x64`) used the same verifier with
`x86_64_linux` bottles: libslirp 4.9.6 (sha `9ab723e5…`) and tree 2.3.2
(sha `b98eef11…`), both SHA-verified; `readelf -d`/patchelf showed native
RUNPATH and PT_INTERP, ELF `e_machine=62`, no placeholders, and `tree --version`
ran. The verifier now enforces host-arch bottle tags and native ELF machines.

This arm64 run found and fixed a real bug: ELF **PT_INTERP** still pointed at
`@@HOMEBREW_PREFIX@@/lib/ld.so`, making executables unrunnable. `_relocate_elf`
now uses `patchelf --print-interpreter/--set-interpreter` as well as RPATH.
`tests/test_linux_real.py` covers this opt-in (`PYBREW_LINUX_REAL=1`; skipped by
default), and `scripts/linux_verify.py` is the reproducible verifier.

## Meson build flow

A fake `meson` executable on PATH handles `setup`/`compile`/`install`, records the
`--prefix` from `std_meson_args`, and installs a test binary into the keg. This
verifies the install DSL, argument expansion and PATH handling without requiring
meson in CI.

A real `meson 1.12.1` was installed into a temporary target and used for an
end-to-end build:

```console
$ PATH=/tmp/pybrew-meson/bin:$PATH PYTHONPATH=src python -m pybrew \
    --prefix /tmp/pybrew-meson-prefix --tap /tmp/meson-tap \
    --cache /tmp/pybrew-meson-cache install --build-from-source meson-hello
Installed: meson-hello 1.0 (/tmp/pybrew-meson-prefix/Cellar/meson-hello/1.0)
$ /tmp/pybrew-meson-prefix/bin/hello-real-meson
hello from real meson
```

The formula used `meson setup build *std_meson_args`, `meson compile -C build`
and `meson install -C build`; the installed binary is linked via
`prefix/bin`.

## HEAD install

A synthetic formula adds a `head do url ... sha256 ... end` block; `install --HEAD`
downloads the head source, builds it with the normal install DSL, installs the
head binary into the keg, and the resulting binary contains the head marker.

## Additional maintenance checks

- `services cleanup` removes `homebrew.mxcl.*` files whose formula is no longer
  installed while keeping installed services.
- `tap --update` updates every installed tap from its git remote.
- `shellenv` prints HOMEBREW_PREFIX/HOMEBREW_CELLAR/PATH for bash/zsh/fish.

## Fetch

`fetch hello` downloads the bottle to the cache and leaves the Cellar untouched;
`fetch --cask testapp` downloads the cask zip and leaves no Caskroom entry.

## Source inspection commands

`cat hello`/`cat --cask testapp` print local formula/cask source, `home` prints
homepages, and `edit` is tested with `EDITOR=true` to avoid an interactive
editor.

## Tap update command

`update` updates every installed tap via `git pull --ff-only`; the test taps a
local remote, commits a change, and confirms the CLI updates it.

## Ruby API expansion batch

- Opt-in Ruby shim now covers a broad common subset: Pathname extensions,
  Utils (inflections/IO/git/curl/gzip), Hardware::CPU, OS::Mac/Linux, MacOS
  SDK/language fallbacks, ENV compiler helpers, `Formula[]` lookup,
  `resource` staged-path errors, `patch`/`option`/`depends_on` warnings, and
  clear `method_missing` failures.
- New `pybrew test <formula>` runs the captured `test do ... end` under Ruby
  with shell_output/pipe_output/assert_*; 16 tests.
- This is **not** the complete Homebrew Ruby API: full Formula/spec/resource
  download/patch application, Homebrew internals, superenv/compiler selection,
  services DSL and many cask APIs remain incomplete. See
  `docs/ruby-api-coverage.md`.

## Caveats / test / Formula API batch

- `pybrew caveats <formula>` executes the caveats Ruby DSL (heredoc, platform
  branches, paths) without requiring installation.
- Formula test DSL gained path/directory/symlink assertions, `shell_output`
  `stderr:`/`env:`/`chdir:` options, and a limited `Formula[...]` context.
- Formula/resource shim gained `Formula.factory`, metadata/paths/deps,
  installed predicates, resource metadata and pre-staged paths; resources still
  do not download and patches are warned no-ops.
- Full suite: 295 passed, 5 skipped (the added caveats/formula Ruby tests are
  the main reason the suite is slower).

## Resource / service batch

- The opt-in Ruby bridge downloads and safely extracts only the resources
  referenced by literal `resource("name")` calls in the install body into
  `build_root/.pybrew-resources/<name>` when a downloader is available;
  `resource(...).stage { ... }` works. Dynamic `resources.each`/variable uses
  conservatively stage all. No downloader or missing checksum stays
  metadata-only with a clear error. Extraction rejects absolute/`..` members
  and archive links.
- Service DSL gained `keep_alive` hash forms, `process_type`, `interval`,
  `environment_variables`, `sockets` (launchd), `macos_legacy_timers`,
  `launch_only_once`, and matching systemd fields where applicable.

## Patch / post_install / compiler batch

- Formula `patch do ... end` / `patch :DATA` is parsed and applied with
  `patch -pN` before install; `url`, relative `file` and inline patches are
  supported; missing checksum, downloader, formula path or patch command fails
  clearly.
- Formula `post_install do ... end` runs after install/link under Ruby; failures
  abort and receipt records `post_install_ran`.
- Opt-in Ruby ENV gained compiler helpers (`append_to_cflags`, `O0..O3`,
  `deparallelize`, `cxx11`, `libxml2`, `perl`, `python`, `java_home`, `sdkroot`)
  and build/CPU helpers.
- Note: the local full-suite run spent ~10 minutes in a pre-existing
  `hdiutil`-based DMG cask test; after the stuck child was cleared it passed.

## Cask uninstall scripts

- `--run-scripts` now also enables `uninstall_preflight`/`uninstall_postflight`
  for `uninstall` and `cask uninstall`; default remains inert.
- Preflight runs before artifact removal; postflight after room removal. A
  postflight failure reports the error after the cask is removed.

## Cask caveats

- Cask parser captures `caveats do ... end` as inert source and keeps literal
  `caveats "..."` strings.
- `pybrew caveats --cask <token>` renders cask caveats with the Ruby shim
  (token/version/appdir/paths/platform blocks); literal caveats print directly.
- This is separate from cask install/uninstall scripts and does not run them.

## Arbitrary Ruby cask DSL

- `PYBREW_ALLOW_CASK_RUBY=1` makes `CaskLoader` evaluate cask source under Ruby
  with a JSON-collecting shim instead of the literal parser.
- Supports variables/loops/conditionals/interpolation, platform/arch blocks,
  language defaults, all artifact kinds, sha256 maps, uninstall/zap,
  depends_on/conflicts, stage_only/auto_updates.
- Scripts and unknown stanzas are not executed/captured; they are recorded as
  warnings/unsupported. Default parser remains unchanged when the env is unset.
- `--ruby-cask` exposes the evaluator as a global CLI opt-in in addition to
  `PYBREW_ALLOW_CASK_RUBY=1`.
- `CaskLoader` now merges inert script bodies captured by the default parser
  into the Ruby-evaluated cask, so opting into Ruby does not silently lose
  preflight/postflight/uninstall scripts.
- `shell_output` now follows Homebrew's second positional argument
  (`expected_status`, default 0) instead of treating it as stdin.
- Fixed the default cask parser dropping keyword arguments after a
  comma/newline continuation (e.g. `sha256 arm: ..., intel: ...` and
  `uninstall launchctl: ..., quit: ...`); regression tests use the VSCode
  fixture and a synthetic multiline cask.

## Arbitrary Ruby service DSL

- `PYBREW_ALLOW_SERVICE_RUBY=1` / `--ruby-service` evaluates captured
  `service do ... end` source under Ruby with a JSON-collecting shim.
- Supports variables/conditionals/loops, platform/arch blocks, run arrays,
  keep_alive hashes, sockets, environment variables, intervals and paths.
- Default literal service parser remains unchanged when the env is unset.
- The shim intentionally mirrors pybrew's literal path helpers
  (`bin`/`lib`/`etc` under the formula opt prefix) rather than Homebrew's
  global-prefix helpers.

## Formula dependency categories

- `Formula["name"]` now exposes `build_dependencies`, `test_dependencies`,
  `recommended_dependencies`, `optional_dependencies`, `head_dependencies`,
  `uses_from_macos`, `requirements`, `conflicts` and `link_overwrite`.
- Payload carries each category separately; `deps`/`runtime_dependencies` keep
  their existing merged/runtime semantics.

## Cask shell completion artifacts

- Added `bash_completion`, `zsh_completion`, `fish_completion` artifact kinds.
- Installer copies them under `<prefix>/share/bash-completion/completions`,
  `<prefix>/share/zsh/site-functions` and
  `<prefix>/share/fish/vendor_completions.d`.
- Supported by both the literal parser and arbitrary-Ruby evaluator.

## DevelopmentTools / CLT

- Added `DevelopmentTools` (`clang`, `clang_version`, `default_compiler`,
  `installed?`, `needs_clt?`, `xcode_installed?`, `sdk_path`, `locate`).
- Added `MacOS::CLT.installed?/version/sdk_path` and `ENV.compiler`.
- All are best-effort: non-macOS/missing CLT returns false/nil rather than
  raising.

## Manpage / compiler flag batch

- Added cask `manpage` artifacts (`share/man/manN`).
- Added `ENV.cxx14/cxx17/cxx20`, `Os/Oz/Og`, `remove_from_cflags`,
  `remove_from_ldflags`, `enable_warnings`.

## Cask rename

- Added `Cask.renames` and parser/evaluator support for
  `rename "Old.app", "New.app"`.
- Installer applies renames to the staged source before artifact installation,
  with safe relative targets and clear missing/conflict errors.
- Literal parser and arbitrary-Ruby evaluator both support it.

## Cask container parsing

- Added `Cask.container` and literal/evaluator support for
  `container type: :nupkg` (and other type values are preserved).
- Archive detection still uses suffix/magic; `nupkg` archives are zip payloads
  and already extract as zip.

## Utils submodules

- Added `Utils::Git`, `Utils::Curl`, `Utils::Gzip`, `Utils::Tar`, `Utils::Zip`,
  `Utils::Cp`, `Utils::Popen` wrappers and `ENV.superenv?`.
- These are thin wrappers over the existing `Utils` subprocess/file helpers.

## Formula patches metadata

- `Formula["name"].patches` exposes `PybrewPatchRef` entries with
  `url`/`file`/`sha256`/`strip`/`data?`.
- The payload carries patch metadata; patch application itself remains in the
  source builder before install.

## oldname / aliases

- Formula model/parser now capture `oldname "..."` and `alias "..."`.
- `Formula["name"]` exposes `oldname` and `aliases`.

## Formula requirements

- Added `Requirement` model and `Formula.requirements`.
- `depends_on :macos`, `:xcode`, `:arch`, `:linux` are captured as
  requirements instead of being silently dropped.
- `Formula["name"].requirements` returns dependency-like refs with name/tags.

## Cask appimage

- Added `appimage` artifact installation to the configured appdir.
- Copies the image, preserves contents and adds executable bits; supports
  `target` and `force` overwrite.
- Literal parser and arbitrary-Ruby evaluator both support it.

## Cask 7z/rar extraction

- `container type: :7z` uses `7z`/`7za` (or `PYBREW_7Z`) to extract.
- `container type: :rar` uses `unrar` (or `PYBREW_UNRAR`).
- Missing external tool or failed extraction raises a clear `CaskError`.
- Fixed cask symbol parsing for `:7z`-style symbols whose first character is a
  digit.

## Formula options metadata

- Added `Formula.options`, `deprecated_options`, `fails_with`,
  `preserve_rpath`, `skip_clean`.
- Parser captures `option`, `deprecated_option`, `fails_with`, `preserve_rpath`
  and `skip_clean`.
- `Formula["name"]` exposes `options`, `deprecated_options`, `fails_with`,
  `preserve_rpath?`, `skip_clean`.

## Formula state helpers

- Added `Formula#stable?`, `#head?`, `#head_only?`, `#bottle?`, `#option?`,
  `#specified_name`, `#full_specified_name`, `#specified_path`.

## Formula bottle tag helpers

- Added `PybrewBottleFile`, `PybrewBottleSpec#bottle_tags`,
  `#bottle_for_tag` and `#bottle_url`.
- Payload carries the formula name so ghcr bottle URLs resolve correctly.

## Cask uninstall launchctl/signal

- `uninstall launchctl:` now calls `launchctl bootout` (fallback `remove`).
- `uninstall signal:` sends the requested signal via `pkill`.
- Both use env-overridable executables and ignore missing tools like the other
  best-effort uninstall actions.

## Cask login_item uninstall

- `uninstall login_item:` uses `osascript`/System Events to delete the login item.
- Best-effort, env-overridable (`PYBREW_OSASCRIPT`), macOS-only like the
  other UI actions.

## Formula versioned helpers

- Added `Formula#unversioned_formula_name`, `#versioned_formulae_names` and
  `#versioned_formulae`.
- Versioned formulae are discovered from the cellar by `<base>@` directory
  prefixes.

## Cask disable!

- Added `Cask.disabled`/`disable_reason` and literal/evaluator support for
  `disable! because: "..."`.
- The installer refuses disabled casks before doing any work.
- `deprecate!` remains metadata-only (warning path unchanged).

## Formula block presence helpers

- Added `Formula#service?`, `#caveats?`, `#test?` booleans backed by whether
  the formula defines the corresponding block.

## Formula path helpers

- Added `doc`, `info`, `man`/`man1..man8`, `elisp`, `frameworks`, `kext_prefix`,
  `etc`, `pkgetc`, `var`, completion paths, `bottle_prefix`, `logs`,
  `opt_elisp`, `opt_frameworks`.
- Added `pour_bottle?`, `fetch_defined?`, `post_install_defined?`,
  `oldnames`, `possible_names`, `linked?`, `linked_version`,
  `skip_clean?`, `link_overwrite?`, `outdated?`.

## Standard build args

- Added `std_npm_args`, `std_shards_args`, `std_cabal_v2_args` and
  `python3` to the install context.

## Standard build args keywords

- `std_configure_args`, `std_cmake_args`, `std_meson_args`, `std_cargo_args`,
  `std_go_args`, `std_pip_args`, `std_cabal_v2_args` now accept Homebrew's
  keyword arguments.
- Added `std_cargo_fetch_args`, `std_swift_args`; aligned `std_shards_args`.

## std_zig_args

- Added `std_zig_args(prefix:, release_mode:, cpu:)` with Homebrew's release
  mode validation and `-Doptimize=Release<Mode>` mapping.

## Head and installed prefixes

- Added `Formula#latest_head_version`, `#latest_head_prefix` and
  `#installed_prefixes`.

## Service names and paths

- Added `plist_name`/`plist_names`, `service_name`/`service_names`,
  `launchd_service_path(s)`, `systemd_service_path(s)` and
  `systemd_timer_path(s)`.

## Cask arch dependency

- `depends_on arch:` is now enforced before install; a mismatch raises a clear
  `CaskError` with the requested and current architecture.

## Cask named dependencies/conflicts

- `depends_on formula:` / `depends_on cask:` now verify the named formula/cask is
  installed before installing.
- `conflicts_with formula:` / `conflicts_with cask:` reject installation when
  the conflicting item is installed.

## Cask macOS dependency

- `depends_on macos:` now checks the host macOS version against `>=`/`<=`/`>`/
  `<`/`=` requirements using version symbols (`:sonoma`, etc.).
- `PYBREW_MACOS_VERSION` can override detection for tests/CI.

## Formula ref identity

- Added `to_s`, `inspect`, `==`/`eql?`, `hash` and `<=>` to `PybrewFormulaRef`.

## Cask deprecation reason

- Added `Cask.deprecation_reason` and literal/evaluator capture of
  `deprecate! because: "..."`.

## Head pkg helpers

- Added `latest_head_pkg_version`, `head_version_outdated?` and
  `outdated_kegs`.

## Formula metadata compatibility helpers

- Added `tap_path`, `reloadable_ref`, alias/installed-name helpers,
  `migration_needed?`, `new_formula_available?`, `latest_formula` and
  `old_installed_formulae` no-op/metadata methods.

## Formula service ref

- `Formula#service` returns a `PybrewServiceRef` with run/working_dir/log paths,
  keep_alive, interval, environment variables, root/once/timer flags, sockets
  and the raw source.
- The service block is parsed through the existing literal service parser; an
  unparseable block leaves `service` nil but `service?` still reflects the
  declared source.

## Formula#python3

- Added `Formula#python3` returning the discovered `python3` Pathname or nil.

## Service extended fields

- Added `nice`, `restart_delay`, `stop_timeout`, `root_dir` and `input_path`
  to the literal and arbitrary-Ruby service DSLs.
- Rendered to launchd `Nice`/`ThrottleInterval`/`ExitTimeOut`/
  `WorkingDirectory`/`StandardInPath` and the corresponding systemd keys.

## Service ref extended fields

- `Formula#service` now also exposes `nice`, `restart_delay`, `stop_timeout`,
  `root_dir` and `input_path`.

## fetch_defined?

- Added `Formula.fetch_source` capture for `def fetch` and
  `Formula#fetch_defined?` backed by it.

## livecheck presence

- Added `Formula.livecheck_source` capture and `Formula#livecheck?`.

## Service cron

- Added `cron` to the literal and arbitrary-Ruby service DSLs.
- Supports `@hourly`/`@daily`/`@weekly`/`@monthly`/`@yearly` and five-field
  cron expressions.
- launchd renders `StartCalendarInterval`; `Formula#service.cron` exposes the
  parsed fields.

## systemd cron timer

- Added `timer_path`/`timer_text`/`write_timer` and wired cron services to
  start/stop/remove the `.timer` unit on systemd.
- `OnCalendar` is generated from the parsed cron fields.

## Cask livecheck source

- Added `Cask.livecheck_source` and literal-parser capture of `livecheck do ... end`.

## Cask no_autobump!

- Added `Cask.no_autobump`/`no_autobump_reason` and literal/evaluator support
  for `no_autobump! because: "..."`.

## Cask os stanza

- Added `Cask.os` and literal support for `os macos:`/`os linux:`.
- The installer rejects a cask whose declared OS does not match the host.

## Formula compatibility batch

- Added `conflicts_with`, `deprecated_option`, `version_scheme`,
  `preserve_rpath`, `tap?`, `core_formula?`, `loaded_from_api?`,
  `supports_macos?`, `supports_linux?`, `valid_platform?`,
  `network_access_allowed?`, `autobump?`, `test_defined?`,
  `livecheck_defined?`, dependency/linkage/cleanup helpers and metadata hashes.

## Cask evaluator os mapping

- The arbitrary-Ruby cask evaluator now maps `os` to `Cask.os` so the same
  platform enforcement applies in both parser modes.

## Service run argument fix

- The literal service parser now keeps all `run` arguments (`run "x", "--flag"`)
  instead of only the first.

## Formula broad compatibility batch

- Added log/sandbox/fixture/rpath/link-overwrite/completion/spec helpers and
  several no-op metadata methods used by formula code.

## Cask os macOS versions

- `os macos:` now also enforces version constraints (`>= :sequoia`, etc.) when
  running on macOS, not just the platform.

## Formula with_logging

- Added `Formula#with_logging` which yields the block.

## systemd socket units

- Added `socket_path`/`socket_text`/`write_socket` and wired socket services to
  start/stop/remove the `.socket` unit on systemd.
- TCP maps to `ListenStream`; UDP maps to `ListenDatagram`.

## Resource metadata fields

- `PybrewResource` now reads `directory`, `path_exec_files`, `installed_size`,
  `bottle_size`, `manifest_annotations`, `sbom_supplement`, and
  `download_strategy` from resource metadata instead of returning constants.

## Cask config/language/strategy helpers

- Added `Cask.all` (bounded placeholder), `config_path`, `languages`,
  `download_strategy`, and `using`.

## Service manager render helpers

- Added `ServiceManager.to_plist` and `to_systemd_unit`, reusing the real
  launchd/systemd renderers instead of duplicating payload logic.

## macOS mdfind/pkgutil helpers

- Added `MacOS.full_version=` (cache reset), `mdfind`, `mdfind_query`,
  `app_with_bundle_id`, `pkgutil_info`, `sdk_locator`, `sdk_for_formula`,
  `sdk_path_if_needed`.

## Resource download/metadata helpers

- Added `download_name`/`download_queue_name`/`download_queue_type`,
  `download_strategy`/`using`, `downloaded_and_valid?`, `files`/`file`,
  `determine_url_mirrors`, `manifest_annotations`, `owner=`, `installed_size`,
  and bounded staging/unpack no-ops.

## Patch CVE/target helpers

- Added `Patch.extract_cves`, `Patch.resolves_type`, and a bounded
  `Patch.ensure_targets_within!` that rejects absolute/`..` patch targets.

## Dynamic local formula lookup

- Local taps with at most 200 indexed formulae are preloaded into the test
  context, so dynamic `Formula[expression]` lookups resolve uninstalled
  formulae from the same tap (clear "not installed" error instead of
  "not available").

## Service object compatibility helpers

- Added service-DSL compatibility methods for command/manual command,
  keep-alive/timed predicates, default service/plist names, path dirs,
  effective env, cron parsing/weekday mapping, `from_hash`, and simple
  plist/systemd unit rendering.

## Cask API/refresh helpers

- Added `api_to_local_hash`, `to_hash_with_variations`, `dsl`,
  `populate_from_api`, `refresh`, `refresh_for_tag`, `tab`.

## Cask serialization/ref helpers

- Added `to_h`/`to_s`/`inspect`/`hash`, `reloadable_ref`, `tap_git_head`,
  `new_download_sha`, `download_sha_path`, `install_time`.

## Cask source/platform/pin helpers

- Added `ruby_source_path`/`ruby_source_checksum`, API-origin flags,
  `valid_platform`/`supports_macos`/`supports_linux`, and cask pinning
  (`pin_path`, `pinned`, `pinned_version`, `pin`, `unpin`).

## Cask installed app bundle helpers

- Added `installed`, `installed_version`, `outdated`,
  `installed_app_info_plist`, `installed_app_info`, `bundle_version`,
  `bundle_short_version`, `bundle_long_version` (Info.plist based).

## Compiler GCC probing

- `DevelopmentTools.gcc_version(cc)` now probes the requested compiler.
- `CompilerSelector.build_compilers(:gnu)` only offers installed `gcc-N`
  versions, and `preferred_gcc` picks the highest installed one with a
  host-`gcc` fallback.

## ServiceSpec compatibility helpers

- Added `ServiceSpec` helpers for command/manual command, keep-alive/timed/
  root predicates, default service/plist names, path dirs, effective PATH
  environment, cron defaults/parsing/weekday mapping, and `from_hash`.

## Cask metadata helpers

- Added `compare_version_strings`, `rename_list`, `old_tokens`,
  `caveats_object`/`caveats_for_api`, `loaded_from_metadata`,
  `platform_supported`, `installable_artifact`,
  `to_h_with_language_variations`, `to_installed_json_hash`.

## Cask verified URL patterns

- `url ..., verified: "regex"` is now enforced before download/extraction;
  mismatch raises `CaskError` and leaves no Caskroom version.

## Caskroom inventory helpers

- Added Cask `caskroom_path`, `installed_versions`, `timestamped_versions`,
  `installed_caskfile`, `outdated_version`/`outdated_info`, `artifacts_list`,
  `single_app_artifact`, `font`, `url_specs`, `checksumable`, `full_token`.

## Formulary contents/loader methods

- Added `Formulary.from_contents` (temporary-file evaluation, returns an
  instance), `get_formula`, `klass`, and `load_file`.
- Method-name audit now reports 0 missing Formulary methods.

## FormulaInstaller method surface

- Added the remaining Homebrew FormulaInstaller method names as bounded
  compatibility methods (`downloadable`, `build_args`, `selected_bottle`,
  `audit_installed`, `check_installation_already_attempted`, staging/fetch/
  linkage/sandbox no-ops, etc.).
- Method-name audit now reports 0 missing FormulaInstaller methods; real
  bottle/pour/Tab/SBOM/linkage work remains Python-side.

## Formulary loader/rack helpers

- Added `Formulary.to_rack`, `canonical_name`, `core_path`,
  `find_formula_in_tap`, `from_keg`, `try_new`, `loader_for`,
  `loader_from_name_tap_type`, `tap_formula_name_type`, `load_from_api`,
  `factory_cache`, `platform_cache`, `platform_cache_tag`.

## Utils / DevelopmentTools / SystemConfig completion

- Utils: name/tap/full-name parsing, `name_or_token`, `fully_qualified_name`,
  symbol/string deep conversion, `deep_compact_blank`,
  `exponential_backoff_sleep`, `parse_author!`, `parallel_map`.
- DevelopmentTools: `host_gcc_path`, `clang_version_output`, `ld64_version`,
  LLVM version helpers, needs-* predicates, install instructions,
  `build_system_info`, `clear_version_cache`.
- SystemConfig: `homebrew_repo`, `homebrew_head_info`, `windows_version`.
- Method-name audit now reports 0 missing Utils/DevelopmentTools/SystemConfig.

## Formulary factory API cache

- `Formulary.factory(ref, from: :api)` now resolves classes populated by
  `load_formula_from_struct!` before falling back to source/metadata loading.
- Unknown API names fall back to the metadata reference.

## Keg/Tab method completion

- `Keg#require_relocation!`, `prepare_debug_symbols`,
  `ignore_interrupts_and_uninstall!`, `record_cask_symlink`,
  `resolve_any_conflicts`, `delete_node_gyp_debris!`,
  `strip_node_gyp_addons!`.
- `Tab.formula_to_dep_hash`; bottle specs expose `rebuild`.
- Method-name audit now reports 0 missing Keg/Tab/Formula instance methods.

## Formula/Keg inventory + cleanup

- Added `Formula.installed`, `names`, `full_names`, `aliases`,
  `core_names`/`core_aliases`, `tap_names`/`tap_aliases`, `racks`,
  `tap_files`, `installed_with_alias_path`, `alias_full_names`,
  `fuzzy_search`.
- Added `Keg#delete_pyc_files!`, `remove_alias_symlink`,
  `consistent_reproducible_symlink_permissions!`, `normalize_pod2man_outputs!`.

## Eighth parallel batch

- FormulaInstaller loaded-source installs write a minimal
  `INSTALL_RECEIPT.json` (version, installed_on_request, runtime_dependencies,
  poured_from_bottle) readable via `Tab.for_keg`; failing installs leave no
  receipt.
- Test DSL gained versioned MacOS SDK helpers: `MacOS.sdks`, `sdk`,
  `sdk_path`, `latest_sdk_version`, `SDKROOT`/`PYBREW_SDK_DIRS`/fake `xcrun`
  discovery.

## Formula spec/build accessors

- Added `Formula#active_spec=`, `#specs`, `#spec_syms`, `#spec_eval` and
  `#build=` compatibility accessors; `specs` returns stable/head and
  `spec_syms` matches.

## Formula `spec_eval` ownership

- `spec_eval` now dups a source spec, returns nil without a URL, and assigns the
  evaluating formula/context as owner; exposed on the raw install context.

## Bottle specification root_url

- Exposed `PybrewBottleSpec#root_url` explicitly (closing the `bottle.rb`
  method-name audit).

## Cask font/checksumable semantics

- `font?` now requires all artifacts to be fonts (matching Homebrew).
- `checksumable?` is false for git/svn/hg/fossil strategies even with a
  placeholder checksum.

## Cask staged path and language helpers

- Added `staged_path`, `os_version`, `language_eval`, `language_groups`, and
  `default_language_group`.

## Utils::SPDX helpers

- Added license validity/deprecation/forbidden checks, license-expression
  parsing/serialization/truncation, and bounded license/exception data
  accessors.

## Utils::Git revision helpers

- Added commit/tar revision lookup, file-at-commit, last revision of file(s),
  cherry-pick, name/email setup, no-global-config helpers, and partial-clone
  support checks.

## Utils::Path helpers

- Added path containment checks, symlink resolution, relative paths,
  copy-with-substitution, loadable-package checks, formula opt path helpers, and
  trusted package root resolution.

## Formula test lookup budget 10000

- Raised the lightweight formula-test entry budget from 2000 to 10000 and added
  a 2500-formula dynamic `Formula[expression]` lookup test.

## Bottle download/stage helpers

- Added bounded download/stage API on `PybrewBottleSpec` (`skip_relocation?`,
  `fallback_on_error?`, download-queue predicates, snapshot/retry block
  wrappers, etc.).

## Bottle specification metadata helpers

- Added `json`, `total_size`, `built_cellar`, `compatible_locations?`,
  `extname`, `url_encode`, `github_packages`, `tab_attributes`, `filename`,
  `to_str`, and `to_s` on `PybrewBottleSpec`.

## Bottle specification collection helpers

- `PybrewBottleSpec` now supports `tags`, `each_tag`, `tag?`,
  `find_matching_tag`, `specification_for`, `add`, and `eql?`.

## Complete Utils::Curl helper surface

- Added the remaining curl check/download/headers/redirect/checksum/workaround
  helpers and Cloudflare/Incapsula predicates as safe bounded values.

## Cask fossil strategy

- `Downloader.fetch_vcs` supports fossil (`clone` then `open`); cask
  `using: :fossil` installs artifacts from the checkout, covered by a fake
  fossil executable test.

## Real patch application

- `PybrewPatch#apply` now reads the local patch, validates targets through
  `Patch.ensure_targets_within!`, and runs the `patch` binary in the build root
  (raising with output on failure). Covered by a real file patch test.

## Utils::Bottles module helpers

- Added running-tag resolution, bottle filename tag/rebuild parsing, tar listing,
  receipt/formula extraction, formula name/version resolution, `built_as?`,
  `file_outdated?`, `load_tab`, and publish-note helpers.

## Cask svn/hg strategies

- `Downloader.fetch_vcs` checks out svn/hg URLs into the cache; cask
  `using: :svn`/`:hg` installs artifacts from the checkout, with fake-VCS
  tests for both strategies.

## Cask artifact article/config/sandbox helpers

- Added `english_article`, `config`, `dirmethod`, `cask_sandbox`,
  `run_cask_sandbox`, `requires_sudo_q`, and `read_script_arguments` to
  `Artifact`.

## Ruby Service object shim

- Added the `Service` Ruby class: naming/default plist, `run`/command,
  keep-alive/root/run-at-load/launch-only-once, interval/cron parsing,
  environment variables, path dirs, plist/systemd unit/timer rendering, and
  `from_hash`.
- Closed the remaining `service.rb` method-name gap in the shim.

## Upstream sync (2026-11-02)

- Upstream exploratory PR Homebrew/brew#24362 remains closed (2026-10-28);
  it was not reopened or resubmitted.

- Tracked Homebrew/brew `59604a815c` and homebrew-core `992feb273e`; formula
  DSL changes are internal plus:
  - `Formula.rack_installed?` and `Formula.installed_formula_names`
  - `Keg#aliases` unsafe-component filtering
- No new formula syntax requiring fixture changes; existing corpus remains
  parseable.

## Seventh parallel batch

- Formulary API-struct cache: `load_formula_from_struct!` populates a cache
  read by `formula_class_defined_from_api?` / `formula_class_get_from_api`;
  `clear_cache` resets it.
- Downloader now supports `headers:` for HTTP(S); cask `url_options` reach the
  stock downloader and receipts record `applied=true`.
- Service CLI: `services info` includes `warnings`; start/stop/restart/run
  print them to stderr.
- `Tab.for_name` selects the latest keg by version, not mtime; CI pins Python
  3.9 to `ubuntu-22.04` (Ubuntu 26.04 no longer has 3.9).
- Upstream sync `1f55ebf835` (2026-10-31): `determine_active_spec` prefers the
  requested head/stable spec and `Keg#aliases` rejects unsafe names.

## Sixth parallel batch

- FormulaInstaller: recursive loaded-source dependency install, cycle/skip
  tracking, bottle/pour compatibility; Keg linked-keg record semantics.
- Cask: `url_options` propagated to injected downloaders (headers/user-agent/
  referer/cookies; stock downloader unchanged), `container_nested` extraction
  with nested zip/tar/pkg dispatch.
- Test DSL: dependency tests now use isolated testpath/cwd/PYBREW_PREFIX/
  TMPDIR and restore parent state.

## Fifth parallel batch

- Formulary: `factory` local-source loading with real cache,
  `load_formula_from_struct!` bounded mapping, API placeholders tested.
- Cask: `url_options`, `container_nested`/`container_options`, expanded
  `rename` forms, hash-rocket parsing fix.
- Test DSL: String-compatible `Version`/`PkgVersion` objects, revision and
  pkg_version metadata.
- Service: unknown directives now recorded in `ServiceSpec#warnings` and
  exposed through the service ref/payload.

## Fourth parallel batch

- Ruby core: Formulary namespace/cache/error classes, FormulaInstaller loaded
  fetch/build/link/clean flow, `CompilerSelector`/`CompilerFailure`/
  `Superenv.setup_build_environment` compatibility surface.
- Cask: script `MacOS.version.to_sym`/`outdated_release?`/`MacOS.language`,
  cask-arch stanza in script env, livecheck url_options normalization.
- Test DSL: MacOS SDK/release names, `OS.version`, CPU flags/features.
- Python source build: `PYBREW_CC`/`CC`, `PYBREW_CXX`/`CXX`,
  `PYBREW_COMPILER=clang|gcc`, flags preservation and injectable build env.

## Formulary loaded install helpers

- `PybrewLoadedFormula` install blocks can use `inreplace` (string and block
  forms with gsub!/sub!/make-var helpers), `ohai` and `opoo`.

## Cask livecheck / MacOS helpers + Test DSL tap lookup

- Cask: livecheck `url_options` and `strategy` block source capture (text-only),
  script `caskroom_path`/`arch`/`language`, and MacOS `release_name`/
  `pretty_name`/`release_version`.
- Test DSL: bounded local-tap index, cached metadata loader, arbitrary
  tap-qualified/alias/installed `Formula[...]` lookup and literal-reference
  preloading. `tests/test_formula_test.py` + CLI: 81 passed.

## Formulary loaded metadata blocks

- `PybrewLoadedFormula` records bottle/service/livecheck blocks, resources,
  patches, options, head and stable blocks, with instance helpers
  (`options`, `option?`, `head?`, `stable?`, `bottle?`, `livecheck?`).

## FormulaInstaller loaded sources

- `FormulaInstaller#install` now installs `Formulary.load_formula_from_path`
  results (`PybrewLoadedFormula` instances) by running their captured install
  block; keg/ref installs still raise the explicit Python-installer error.

## Formulary source loader

- `Formulary.load_formula_from_path` now evaluates a real formula file in an
  isolated module with a metadata-only `PybrewLoadedFormula` base class,
  returning the loaded class with desc/homepage/url/sha256/version/license/
  dependencies/test/install/caveats blocks. Missing paths still fall back to
  the metadata ref for compatibility.

## Upstream sync (2026-10-30)

- Fetched Homebrew `main` `fcdfa0476e` (2026-10-30) and compared the formula,
  formulary, keg, service and cask DSL files.
- Added `Dependency#no_linkage`/`PybrewDependencyRef#no_linkage?`,
  `Formula#local_patch_checksums`, and `to_hash["no_linkage_dependencies"]` /
  `to_hash["local_patch_checksums"]` for the new `:no_linkage` DSL.
- Other upstream changes were docs-only or internal (Keg relative-path
  handling, systemd `successful_exit` already matched, Formulary
  `from_cache` kwargs already tolerated).

## Formula#install execution

- `Formula#install` now executes the captured bare `def install` body in a
  `PybrewInstallContext`, with a re-entrancy guard so a formula calling
  `Formula["self"].install` from inside its own install block is a no-op.

## Third parallel batch

- Cask evaluator captures `preflight`/`postflight`/`uninstall_*` block sources
  best-effort, merges literal-parser bodies as authoritative and keeps default
  installs inert. `tests/test_cask*.py`: 100 passed.
- Formula test DSL can execute dependency `Formula["dep"].test` bodies loaded
  from local taps, including nested dependencies; failure/skip semantics
  propagate. `tests/test_formula_test.py` + CLI: 74 passed.

## Second parallel batch

- Cask lifecycle metadata (`deprecation_*`/`disable_*`, `homepage_browsed`,
  `on_system_block_min_os`, `no_autobump_message`), Linux sha256 keys and
  script `input:`/`timeout:`/`Utils.popen_write`.
- Formula test DSL: `refute_operator`, `assert_send`, `skip`, pattern
  assertions, assertion counter, `test_fixtures`, HOMEBREW constants and
  read-only OS/Hardware/MacOS helpers. `tests/test_formula_test.py`: 55 passed.
- `FormulaInstaller` compatibility surface with predicates and a clear
  `NotImplementedError` for `install`.

## Parallel batch: service names, cask helpers, test DSL

- Service custom `name macos:/linux:` now reaches manager labels/paths and
  `PybrewServiceRef` plist/service names.
- Cask evaluator: `MacOS.version/full_version/version_symbol/sdk/sdk_path`,
  livecheck metadata/source capture, script-context `system_command`
  `must_succeed` semantics and `Utils.popen_read`/`which`.
- Formula test DSL: Minitest block forms, more path/type assertions,
  `pipe_output` expected-status arg, dependency `Formula[...]` lookup and
  expanded path helpers. `tests/test_formula_test.py`: 43 passed.

## Cask caveats blocks

- The opt-in cask Ruby evaluator now executes `caveats do ... end` and captures
  `puts`/`print`/`ohai`/`opoo` plus a returned string; the cask script test no
  longer expects caveats to be skipped.

## Formula/Formulary surface methods

- Added no-op `Formula#depends_on`, `#option`, `#patch` and
  `Formulary.load_formula_from_path`/`load_formula_from_struct!`
  (metadata-only compatibility).

## Cask livecheck flag

- Added `livecheck_defined?` to the opt-in cask Ruby evaluator; `livecheck`
  blocks now record that they were seen.

## Service ref names

- Added `PybrewServiceRef#plist_name`, `canonical_plist_name`,
  `legacy_plist_name`, `plist_names`, `service_name`,
  `canonical_service_name`, `legacy_service_name`, `service_names` and
  `formula_name`; service metadata now carries the formula name.

## Formula sandbox env

- Implemented `Formula#common_sandbox_env(home)` and
  `#test_sandbox_env(testpath)` with HOME/TMPDIR/GIT/XDG and package-manager
  cache variables; added `HOMEBREW_TEMP`.

## Cask language helpers

- Added `languages`, `language_groups` and `default_language_group` to the
  opt-in cask Ruby evaluator.

## Cask path/os helpers

- Added `caskroom_path`, `staged_path`, `os_version` and a minimal
  `MacOS.version` to the opt-in cask Ruby evaluator.

## Cask DSL metadata helpers

- Added the Ruby evaluator helpers `autobump?`, `no_autobump_defined?`,
  `on_system_blocks_exist?`, `on_os_blocks_exist?`,
  `depends_on_set_in_block?` and `caveats_object`; `on_macos`/`on_linux`
  record that a system block was seen.

## Utils::Git / Curl

- Added `Utils::Git.path`, `.version`, `.head`, `.last_commit`, `.origin_url`
  and `Utils::Curl.curl_executable`, `.curl_path`, `.version`.
- Made the cached real-bottle test version-agnostic (the local bottle cache had
  moved from libslirp 4.9.6 to 4.9.7).

## Utils::Path

- Added `Utils::Path.rmdir_if_possible`, `resolved_path`,
  `resolved_path_exists?`, `formula_installed_prefixes`,
  `formula_opt_bin_path` and `formula_opt_bin_env`.

## Sandbox

- Added the `Sandbox` compatibility module (`available?`,
  `full_write_isolation?`, `nested_sandbox?`, `avoid_nested_sandboxing?`,
  `use_for?`, `inherited_sandbox?`, `state`, `failure_reason`,
  `reset_state!`, `with_preserved_brew_file`, `run_or_fork`).

## SystemConfig sections

- Added `SystemConfig.config_sections`, `homebrew_config`, `homebrew_env_config`,
  `hardware_config`, `host_software_config`, `dump_tap_config` and
  `core_tap_config`; `dump_verbose_config` now iterates the sections.

## Superenv

- Added the `Superenv` compatibility module with `shims_path`, `bin` and
  `enabled_for?`.

## Service run_type

- Added the `run_type` service directive (`immediate`/`interval`/`cron`) to
  the literal parser, opt-in Ruby evaluator, `ServiceSpec`, `PybrewServiceRef`
  and payload; invalid values raise `ServiceError`.

## Formula std build args

- Added `Formula#std_configure_args`, `std_cmake_args`, `std_meson_args`,
  `std_cargo_args`, `std_cargo_fetch_args`, `std_go_args`, `std_pip_args`,
  `std_npm_args`, `std_shards_args`, `std_swift_args`, `std_cabal_v2_args`
  and `std_zig_args`.

## Formulary placeholder and cache helpers

- Added `Formulary.formula_class_defined_from_path?`/`_from_api?`,
  `formula_class_get_from_path`/`_from_api`, `namespace_key`,
  `replace_placeholders` and `ensure_utf8_encoding`.

## Keg records and class helpers

- Added `Keg.keg_link_directories`, `Keg.must_exist_subdirectories`,
  `Keg.must_exist_directories`, `Keg.must_be_writable_directories`,
  `Keg#keepme_refs`, `#binary_executable_or_library_files`,
  `#codesign_patched_binary`/`binaries`, `#remove_opt_record`,
  `#remove_linked_keg_record`, `#oldname_opt_records`,
  `#remove_oldname_opt_records` and `#remove_old_aliases`.

## Formula shell/source helpers

- Added `Formula#url`, `#sha256`, `#mirror`, `#system`, `#mkdir`,
  `#inreplace` and `#install` (default no-op).

## Keg link_dir

- Added `Keg#link_dir` (links one keg directory only, with dry-run/overwrite)
  and `Keg#opt_record`.

## Formula stage/brew

- Added `Formula#stage` (yields/returns the existing buildpath) and
  `Formula#brew` (runs `fetch` then `stage`), plus
  `Formula#run_post_install_steps` and `Formula#print_tap_action`.

## MacOS::Xcode

- Added `MacOS::Xcode.installed?`, `.version`, `.prefix`, `.sdk_path` and
  `.default_developer_dir`.

## Tab class methods

- Added `Tab.from_file`, `Tab.from_file_content`, `Tab#homebrew_version`,
  `Tab#parsed_homebrew_version`, `Tab#installed_on_request_present?` and
  `Formula#on_system_blocks_exist?`.

## Formulary kwargs, Tab write and Keg uninstall

- `Formulary.factory`/`resolve`/`from_rack` tolerate Homebrew's extra
  positional/keyword arguments.
- `Tab#tap`, `#tap=`, `#write`; `Keg#uninstall`.

## Keg introspection

- Added `Keg.for`, `Keg.all`, `#version_scheme`, `#scheme_and_version`,
  `#to_formula`, `#runtime_dependencies`, `#aliases`, `#empty_installation?`,
  `#require_relocation?`, `#lock`, `#elisp_installed?`, `#apps`,
  `#plist_installed?`, `#completion_installed?`, `#functions_installed?`;
  `Tab` gained `version_scheme`/`aliases`.

## Keg file linking

- `Keg#link` now creates relative symlinks for files under `bin`, `etc`,
  `include`, `lib`, `sbin`, `share` and `var` (plus the opt link), with
  conflict detection; `Keg#unlink` removes them and prunes empty directories.

## Formula bottle accessors

- Added `Formula#bottled?`, `#bottle_tags`, `#bottle_for_tag` and
  `#bottle_url`.

## Formula misc helpers

- Added `mktemp`, `lock`/`unlock`, `run_test`, `run_post_install`,
  `post_install_steps`, `python_major_minor_version`, `xcodebuild`,
  `exec_cmd`, `validate_attributes!`, `determine_active_spec`,
  `update_head_version`, `setup_home`, `ensure_installed!`, `install_etc_var`
  and `caveats_with_placeholders`.

## Formula#fetch

- `Formula#fetch` evaluates the captured `fetch` block in an install-like
  context; added `Formula#buildpath` and `Formula#staged_path`.

## Formula#test

- `Formula#test` evaluates the captured `test do ... end` block with
  `shell_output`/`pipe_output` and the common `assert_*`/`refute_*` helpers.

## Tab receipt fields

- Added `Tab#used_options`, `#unused_options`, `#built_as_bottle?`,
  `#installed_on_request`/`#installed_as_dependency` and dependency-aware
  request flags; `Formula#to_hash` installed entries now include options and
  bottle flags.

## Formula#post_install

- `Formula#post_install` now evaluates the captured `post_install` block in an
  install-like context (paths, `system`, `inreplace`, `Utils`, `FileUtils`).

## Formula pinning

- Added `Formula#pinned?`, `#pinnable?`, `#pin`, `#unpin`, `#pinned_version`
  and `#pin_path`, backed by `HOMEBREW_PINNED_KEGS`; `to_hash["pinned"]`
  now reflects the real state.

## Outdated kegs and defined flags

- `Formula#outdated_kegs` now compares installed keg versions with
  `pkg_version`; `Formula#outdated?` reflects the result.
- Added `Formula#post_install?` and `Formula#fetch?`.

## Formula metadata hashes

- Implemented `Formula#urls_hash`, `#bottle_hash`, `#to_hash` and
  `#to_hash_with_variations` with the common metadata keys, installed-keg
  entries and service metadata; `PybrewServiceRef#to_hash` was added.

## Formula livecheck object

- Added `Formula#livecheck` returning a `PybrewLivecheck` wrapper with `url`,
  `regex` (`Regexp`), `strategy`, `skip?`, `skip_reason`, `source` and
  `to_s`; the Python payload parses literal `url`/`regex`/`strategy`/`skip`
  directives and resolves `url :stable`/`:head`.

## Formulary rack helpers

- Added `Keg.from_rack` (linked/optlinked/latest-version selection),
  `Formulary.from_rack`, `Formulary.keg_only?`, cache no-ops, and the exact
  Homebrew `Formulary.class_s` naming rules (`FooBar`, `FooAT12`, `Cxx`).

## SystemConfig

- Added `SystemConfig`: clang/build versions, `describe_clang`,
  `describe_homebrew_ruby`, hardware/kernel, git/curl descriptions and
  `dump_verbose_config`; added `DevelopmentTools.clang_build_version`.

## Installed Keg objects

- `Formula#installed_kegs` now returns `Keg` objects (matching Homebrew),
  deterministically sorted by version, and `installed_prefixes` returns their
  `Pathname` prefixes.
- Added `Keg#basename`/`to_path` and `Formula#tab`.

## Formula caveats rendering

- Added `Formula#caveats`: evaluates the captured block with `puts`/`ohai`/
  `opoo`, platform/arch guards, path helpers, and the global
  `HOMEBREW_PREFIX`/`HOMEBREW_CELLAR` constants.

## Keg, Tab and Formulary

- Added the `Keg` class: path derivation, path helpers, `installed?`, `linked?`,
  opt-link creation/removal, `formula`, and `tab`.
- Added `Tab`: `for_keg`/`for_name`/`for_formula`, receipt loading from
  `INSTALL_RECEIPT.json`, request/bottle/HEAD flags, runtime/build
  dependencies, source/time/compiler/changed files, and JSON round-tripping.
- Added `Formulary.factory`/`resolve`/`load_formula`/`class_s` delegating to the
  existing formula reference lookup.

## Formula source accessors

- Added `caveats_source`, `test_source`, `livecheck_source`,
  `post_install_source` and `fetch_source` for read-only introspection of the
  captured block bodies.

## Final parallel batch

- Cask preflight/postflight scripts are parsed inertly and executed only with
  `--run-scripts`; the Ruby shim covers system_command/system/Pathname/FileUtils
  and records `scripts_requested`/`script_results`. No scripts run by default.
- `--system-pkg` records installed pkg IDs via `pkgutil --pkgs`; uninstall
  requires root, validates `pkgutil --files` paths under allowed roots, removes
  files/empty dirs, and calls `pkgutil --forget`. No real system writes in tests.
- Local analytics (`analytics on/off/state/log/clear`) stores events only under
  `var/pybrew/analytics.json`; disabled by default, no network. Install,
  uninstall and upgrade record events when enabled.
- Opt-in arbitrary Ruby install bodies run under `/usr/bin/ruby` with a shim
  (`PYBREW_ALLOW_ARBITRARY_RUBY=1` or `install --allow-arbitrary-ruby`);
  default declarative path unchanged. Tests skip if Ruby is absent.
- Real Linux verification via OrbStack Ubuntu arm64: patchelf 0.18.0 installed;
  real `libslirp 4.9.6` and `tree 2.3.2` arm64_linux bottles downloaded from
  ghcr.io, SHA-verified, installed, and inspected. This found and fixed a real
  PT_INTERP relocation bug (`@@HOMEBREW_PREFIX@@/lib/ld.so`); after the fix
  `tree --version` runs. See `scripts/linux_verify.py` and
  `tests/test_linux_real.py` (opt-in).


- `--system-pkg` performs an explicit root-only macOS `installer -pkg ... -target /`
  install; tests use a fake installer and never run as root. The default
  `--pkg-root` payload expansion remains unchanged. System pkg uninstall/rollback
  is still not implemented.
- Brewfile `brew "svc", restart_service: true` starts the installed service after
  a successful non-dry-run install; missing service/start failure becomes a
  bundle error.

## Parallel feature batch

- 13 Cask Library artifact kinds (`font`, `colorpicker`, `prefpane`, `qlplugin`,
  `mdimporter`, `screen_saver`, `service`, `audio_unit_plugin`, `vst_plugin`,
  `vst3_plugin`, `dictionary`, `input_method`, `keyboard_layout`) install under
  `--library-root` (default `<prefix>/Library`), with target safety checks,
  receipt tracking and uninstall support.
- `stage_only` casks install a receipt without artifacts; upgrade diffs old and
  new receipts and removes dropped artifacts while preserving shared paths.
- Source DSL supports `on_arm`/`on_intel`, `Hardware::CPU` predicates,
  `build.with?`/`without?`, `args.include?`, and `ENV[...]` reads.
- Brewfile supports `brew ..., args: ["--HEAD", "--build-from-source"]`,
  `mas ... id:` and `vscode ...` via fake/injectable executables.
- ELF bottles can use `patchelf` to rewrite placeholder RPATHs when the target
  path is longer than the placeholder; without patchelf the safe skip path is
  preserved.

## Cask reinstall

`cask reinstall testapp` and `reinstall --cask testapp` remove and reinstall the
cask, recreating the app and receipt.

## Cask CLI aliases

`search --cask`, `outdated --cask` and `upgrade --cask` are covered end-to-end:
search finds the local cask, outdated reports 1.0 < 2.0 (including JSON), and
upgrade installs the new Caskroom version.

## CLI smoke checks

```console
$ PYTHONPATH=src python -m pybrew --prefix /tmp/pybrew-demo \
    --tap tests/fixtures/core_formulas info hello
hello: stable 2.12.3
Program providing model for GNU coding standards and practices
https://www.gnu.org/software/hello/
License: GPL-3.0-or-later
Not installed
Bottles: 6 tag(s) (arm64_golden_gate, arm64_linux, arm64_sequoia, arm64_sonoma)

$ PYTHONPATH=src python -m pybrew --version
pybrew 0.1.0
```

The integration tests additionally exercise: bottle install/uninstall, placeholder
relocation, cache writes, link conflict + `--force`, SHA-256 mismatch abort,
malicious tar rejection, keg-only behaviour, dependency ordering, staged source
install, `doctor`, `list`, `deps`, `search`, `config`, and `parse`.

## Not covered yet

- Live download from ghcr.io is not part of the offline suite; the real bottle
  test intentionally uses a cached artefact. Live download is recorded manually
  above.
- Cask login items and complete script API coverage; exotic cask script APIs
  outside the shim still fail explicitly.
- `brew lgtm`, remaining advanced Brewfile options.
- Full Homebrew Ruby API coverage; the bridge/test/caveats runners remain partial shims.
- Resource staging is literal-call-scoped (dynamic uses fall back to all) and
  patches are now implemented separately; systemd has no timer unit for
  standalone `interval`.

These are tracked in the README roadmap and `docs/parity.md`.

## Upstream PR attempt

RFC PR <https://github.com/Homebrew/brew/pull/24362> was initially closed by
Homebrew's PR-template bot because the body did not match
`PULL_REQUEST_TEMPLATE.md`. After the human reviewed and explicitly authorized
the submission, the body was updated with the template and AI disclosure, the
AI-disclosure checkbox was checked, and the template bot reopened the PR:

- reopened by `github-actions[bot]` on 2026-10-28
- **closed by maintainer MikeMcQuaid on 2026-10-28** with the comment
  "Not interested in this, sorry." (see the PR page)
- `brew lgtm` remains unchecked with an explanatory note because the PR only
  adds an RFC document and does not change Homebrew Ruby code
- no attempt was made to misrepresent authorship or bypass the template bot

The maintainer's close is a final scope decision; no further reopen attempts
were made. The submitting human owns any future upstream communication and
must not use AI to answer review comments.

## Upstream sync follow-up — 2026-11-04

- Closed the remaining actionable upstream-audit gaps:
  - `Formulary.from_bottle` now extracts and loads the embedded
    `.brew/<name>.rb` recipe and records `local_bottle_path`.
  - `CaskLoader` token traversal hardening plus explicit
    `from_path`/`from_tap_path`/`from_content` and installed `.rb`/`.json`
    caskfile fallback.
  - Installer copies the cask source into `.metadata/.../Casks/<token>.rb`;
    uninstall falls back to it when the tap has disappeared.
  - Cask `zap` supports `delete`, `rmdir` and `pkgutil` in addition to
    `trash`, with the existing allowed-root checks.
  - `livecheck` `throttle` rate/days mutual exclusion.
  - `Keg#formula_preserve_rpath?` now honors `preserve_rpath` in an installed `.brew` recipe.
- Full local suite: **1156 passed, 5 skipped**.
- Real install verification: `scripts/verify_real_install.py` passes.

## Upstream API sync — 2026-11-04

- Upstream checkout: `/usr/local/Homebrew`, `origin/main` = `86e082cb08`
  (previous state `57cac9d0f8`; 52 commits).
- Implemented upstream API deltas:
  - `Formulary.from_path` / `resolve_from_path` / `from_tap_path` /
    `from_bottle`, path-only source loading.
  - `Keg#deleted_formula?`, `Keg#formula_preserve_rpath?`.
  - `Formula.loaded_from_metadata?`, `pypi_packages` DSL /
    `pypi_packages_info`, `follow_installed_alias` shims.
  - Livecheck `throttle` rate/days mutual exclusion (source casks).
  - CaskLoader token traversal hardening, `from_path`/`from_tap_path`/
    `from_content`, installed `.rb`/`.json` caskfile fallback, and installer
    metadata source copy.
  - Shared `HOMEBREW_PACKAGE_NAME_REGEX` constant.
- Full local suite: **1149 passed, 5 skipped** in ~122s.
- Real install verification: `scripts/verify_real_install.py` passes.

## Wave 11 completion batch (2026-11-04)

- Full local suite: **1143 passed, 5 skipped** in ~134s.
- Targeted slices:
  - Ruby shim: 192 passed
  - Superenv/source build: 53 passed
  - CLI: 19 passed
- Real install verification: `scripts/verify_real_install.py` passes with
  Tab-compatible receipt fields + SBOM.
- Wave 11 changes:
  - Mach-O ad-hoc codesign after binary relocation, explicit symlink
    retargeting, ELF `--remove-rpath`, stricter bwrap user/capability flags.
  - Superenv mixed-language split API and bounded `-Wa,*` allow-list with
    dropped-flag diagnostics.
  - `doctor --linkage` and structured `linkage --json` diagnostics.

## Wave 10 completion batch (2026-11-04)

- Full local suite: **1137 passed, 5 skipped** in ~135s.
- Targeted slices:
  - Ruby shim: 190 passed
  - Superenv/source build: 50 passed
  - Cask: 327 passed
  - Test DSL/version: 131 passed
  - Service: 144 passed
- Real install verification: `scripts/verify_real_install.py` passes with
  Tab-compatible receipt fields + SBOM.
- Wave 10 changes:
  - Keg Mach-O/ELF binary relocation via `install_name_tool`/`patchelf`
    (dry-run/report, fake-tool tests); strict bwrap roots/write binds/netns.
  - Superenv sanitized `-Wa,*`, `.S` vs `.s`, mixed-language no forced `-x`.
  - Cask optional tracked chmod, move rollback, copy/link rollback edges.
  - Version bounded Homebrew comparison table exactness and hash consistency.
  - Service standalone explicit user-env re-read context.
  - CLI `linkage --json` now includes checked/skipped/issues/unsupported/
    corrupt/audit structures.

## Wave 9 completion batch (2026-11-04)

- Full local suite: **1115 passed, 5 skipped** in ~107s.
- Targeted slices:
  - Ruby shim: 188 passed
  - Superenv/source build: 48 passed
  - Cask: 318 passed
  - Test DSL/version: 128 passed
  - Installer: 52 passed
- Real install verification: `scripts/verify_real_install.py` passes and now
  asserts the Tab-compatible receipt fields (`time`, `source_modified_time`,
  `arch`) alongside build metadata + SBOM.
- Wave 9 changes:
  - Keg text relocation + placeholder round-trip; GraphQL cursor pagination
    (10 pages), rate-limit fields and Enterprise URL.
  - Superenv `.s/.S/.asm` dispatch, mixed-language first-source behavior,
    resource staging static names/caching/safe paths.
  - Cask directory copy, chmod/hardlink/symlink edges, rollback diagnostics,
    livecheck default stability.
  - Version Token edge semantics (Null/0, revision 0, letters vs p/post,
    preview normalization, hash consistency).
  - Keg receipt Tab-compatible fields and real-install assertions.

## Wave 8 completion batch (2026-11-04)

- Full local suite: **1097 passed, 5 skipped** in ~119s.
- Targeted slices:
  - Ruby shim: 186 passed
  - Superenv/source build: 44 passed
  - Cask: 307 passed
  - Test DSL/version: 128 passed
  - Service: 139 passed
- Real install verification: `scripts/verify_real_install.py` still passes
  (build metadata + keg SBOM + install/uninstall).
- Wave 8 changes:
  - Strict deny-default Sandbox profile (`PYBREW_SANDBOX_STRICT=1`) with
    bounded system/runtime read roots; default allow-default unchanged.
  - Formulary multi-channel API JSON and lazy in-source tap alias scanning.
  - Superenv extension-based shim dispatch, ARCHFLAGS/ARCH propagation,
    SDK sysroot duplicate rules, OBJC/OBJCXX standards fallback.
  - Cask steps move/chmod/link/hardlink, rate-limiter reset, safer zap roots.
  - Version bounded Token hierarchy (Numeric/Alpha/Beta/Pre/Rc/P/Post/Letter/
    Null) in Python and DSL Ruby.
  - Service render-time user env re-read when formula context exists.

## Wave 7 completion batch (2026-11-04)

- Full local suite: **1068 passed, 5 skipped** in ~149s.
- Targeted slices:
  - Ruby shim: 182 passed
  - Superenv/source build: 41 passed
  - Cask: 293 passed
  - Test DSL/version: 127 passed
  - Service: 132 passed
- Real install verification: `scripts/verify_real_install.py` still passes
  (build metadata + keg SBOM + install/uninstall).
- Wave 7 changes:
  - Formulary remote ETag/Last-Modified conditional cache, bearer token,
    HS256 JWS verification with explicit failures.
  - Superenv LDFLAGS SDK, `PYBREW_ARCH`, OBJC/OBJCXX shims/standards,
    syslibroot-aware sanitizer.
  - Cask generic `steps:` (`mkdir`/`copy`/`symlink`), per-host livecheck
    token bucket, connection Retry-After, configurable redirect headers;
    CLI `--header/--timeout`.
  - Version default switched to Homebrew null-token semantics with legacy
    opt-out.
  - Literal service parser reads bounded user env overrides; declared vs
    effective environment and renderer PATH behavior documented/tested.

## Wave 6 completion batch (2026-11-04)

- Full local suite: **1035 passed, 5 skipped** in ~115s.
- Targeted slices:
  - Ruby shim: 180 passed (GitHub GraphQL)
  - Installer/linkage: 60 passed
  - Cask: 279 passed
  - Test DSL/version: 126 passed
  - Service: 124 passed
- Real install verification: `scripts/verify_real_install.py` still passes
  (build metadata + keg SBOM + install/uninstall).
- Wave 6 changes:
  - GitHub GraphQL transport + GraphQL-first PR/repo/team/sponsorship mappings.
  - dyld/ld.so `$LIB`/`$PLATFORM`, LC_RPATH stack, DYLD/LD env paths,
    DT_RUNPATH precedence and DT_AUDIT diagnostics.
  - Cask optional brotli, Retry-After/jitter/retry budget, cross-origin
    redirect header stripping; generic artifact `steps:` explicit limit.
  - Version opt-in Homebrew null-token mode and hash alignment.
  - Service declared vs effective environment separation with user overrides.

## Wave 5 completion batch (2026-11-04)

- Full local suite: **1007 passed, 5 skipped** in ~109s.
- Targeted slices:
  - Ruby shim: 179 passed
  - Installer/linkage: 54 passed (`tests/test_installer.py tests/test_relocation.py`)
  - Cask: 266 passed
  - Test DSL/version: 121 passed
  - Service: 121 passed
  - CLI: 15 passed
- Real install verification: `scripts/verify_real_install.py` still passes
  (build metadata + keg SBOM + install/uninstall).
- Wave 5 changes:
  - Sandbox allow/deny rule collection + generated seatbelt profile/bwrap args.
  - Formulary remote single-URL API JSON with injectable curl transport.
  - Non-heuristic Mach-O/ELF linkage parsing, RPATH/RUNPATH, corrupt/unsupported
    separation, `all_formulae` reports and `pybrew linkage --all`.
  - Cask generic artifact dirmethod/base_dir and livecheck HTTP redirects,
    headers, retries, gzip/deflate/charset.
  - Version token classes plus opt-in Homebrew null-token comparison.
  - Service user env override file and `PYBREW_SERVICE_PATH_MODE` unifier.

## Wave 4 completion batch (2026-11-04)

- Full local suite: **967 passed, 5 skipped** in ~100s.
- Targeted slices:
  - Ruby shim: 177 passed
  - Cask: 253 passed (`tests/test_cask*.py`)
  - Service: 108 passed (`tests/test_services.py tests/test_service_ruby.py`)
  - Source build/Superenv: 34 passed
  - CLI: 15 passed
- Real install verification: `scripts/verify_real_install.py` still passes
  (build metadata + keg SBOM + install/uninstall).
- Wave 4 changes:
  - Real bounded Sandbox backends (`sandbox-exec`/`bwrap` or fake wrapper),
    clear failure when enabled without a backend.
  - Tab full field/round-trip semantics; Keg `remove_old_aliases`.
  - Superenv macOS SDKROOT/xcrun, bounded `-std` flags, shlex compiler wrappers.
  - Cask generic artifact (bounded appdir/install roots), livecheck default
    HTTP transport (opt-in online), suite/zap symlink edges.
  - Service Ruby evaluator aligned to Homebrew opt/keg/global paths and
    working/log/input semantics while literal parser stays pybrew-compatible.
  - CLI livecheck now delegates to the Cask online transport.

## Wave 3 completion batch (2026-11-04)

- Full local suite: **937 passed, 5 skipped** in ~107s.
- Targeted slices:
  - Ruby shim: 173 passed (`tests/test_ruby_api.py`)
  - Installer: 36 passed, 1 skipped (`tests/test_installer.py tests/test_real_bottle.py`)
  - Cask: 240 passed (`tests/test_cask*.py`)
  - Test DSL/version: 119 passed (`tests/test_formula_test.py tests/test_version.py`)
  - Source build/Superenv: 29 passed (`tests/test_source_build.py`)
  - CLI/ruby/cask smoke: 280 passed
- Real install verification: `scripts/verify_real_install.py` still passes and
  asserts build metadata + keg SBOM + install/uninstall.
- Wave 3 changes:
  - Formulary local API JSON loading/cache and local tap `Aliases/` resolution.
  - Superenv shim-side bounded flag/rpath sanitization (opt-in).
  - Cask `livecheck_status` + `pybrew livecheck --page/--online`; suite target
    semantics, generic artifact explicit limitation, receipt/zap safety.
  - Test DSL ranked pre-release/PkgVersion tokens and dependency HOMEBREW_*
    constant/env restoration.
  - CLI `pybrew linkage` diagnostic wired to installer linkage checks.

## Wave 2 completion batch (2026-11-04)

- Full local suite: **903 passed, 5 skipped** in ~99s.
- Targeted slices:
  - Ruby shim: 171 passed (`tests/test_ruby_api.py`)
  - Installer: 36 passed, 1 skipped (`tests/test_installer.py tests/test_real_bottle.py`)
  - Cask: 219 passed (`tests/test_cask*.py`)
  - Test DSL/version: 115 passed (`tests/test_formula_test.py tests/test_version.py`)
  - Source build/Superenv: 24 passed (`tests/test_source_build.py`)
  - CLI regression: 13 passed (`tests/test_cli.py`)
- Real install verification: `scripts/verify_real_install.py` now additionally
  asserts the bottle/source `build` receipt, keg SBOM file_count and
  `bin/hello.sh` entry; install + uninstall still pass.
- Wave 2 changes:
  - Utils::GitHub full method-name surface with injectable REST reads and
    explicit write guards; Sandbox policy names present but explicitly fail
    rather than pretending isolation.
  - Cask `check_livecheck` API, stricter app/appimage/binary target safety,
    completion/zap/upgrade cleanup tests, lifecycle-date edge tests.
  - Test DSL bounded source-only alias scan, global-constant env isolation,
    Python PkgVersion + DSL version improvements, Minitest compound counters.
  - Installer Tab/SBOM receipt, source/build metadata, formula-level linkage
    diagnostics, stale-link cleanup, hardlink manifest edge cases.
  - Opt-in `PYBREW_SUPERENV=1` compiler shims with fake-compiler tests.

## P0/P1/P2 completion batch (2026-11-04)

- Full local suite: **851 passed, 5 skipped** in ~122s.
- Targeted slices:
  - Ruby shim: 168 passed (`tests/test_ruby_api.py`)
  - Installer: 22 passed, 1 skipped (`tests/test_installer.py tests/test_real_bottle.py`)
  - Cask: 193 passed (`tests/test_cask*.py`)
  - Test DSL/version: 107 passed (`tests/test_formula_test.py tests/test_version.py`)
  - CLI integration: 13 passed (`tests/test_cli.py`)
- Real install verification (`scripts/verify_real_install.py`): local tap/formula
  source install, keg/bin/opt/receipt assertions, executable output, and
  uninstall all passed.
- Ruby-bridge real install also verified with `--allow-arbitrary-ruby`: the
  install body wrote `hello-ruby.txt` and installed `bin/payload.txt`.
- P0/P1/P2 changes:
  - Formulary path+mtime cache/stale reload, API-struct validation/cache,
    bounded alias/oldname fallback.
  - Installer bottle manifest pre-validation/summary, opt-in linkage check,
    deterministic topological dependency order.
  - Bounded Sandbox wrapper, compiler probing tests, Superenv env coverage.
  - Cask livecheck strategy execution API, date-deferred deprecation/disable,
    script delegators/no-sudo coverage.
  - Test DSL alias index, dependency HOME/PATH isolation, Version/PkgVersion
    ordering and Minitest skip/pattern improvements.
  - Utils::GitHub injectable read API + explicit write guards; macOS/Xcode/CLT
    env overrides and Xcode detection bug fix.

## API surface audit (2026-11-03)

Method-name audit against Homebrew `origin/main` (`57cac9d`) reports **0
missing** for:

`formula.rb`, `keg.rb`, `tab/tab.rb`, `formula_installer.rb`, `formulary.rb`,
`utils.rb`, `development_tools.rb`, `system_config.rb`, `service.rb`,
`bottle.rb`, `os/mac/xcode.rb`, `extend/ENV/super.rb`, `extend/ENV/shared.rb`.

Utility surfaces additionally cover `Utils::Path/Git/Curl/Bottles/Svn/
Analytics/SPDX`. Remaining gaps are semantic depth (`Sandbox`/`Superenv`
execution, real bottle pour, network `Utils::GitHub`, exotic cask/livecheck
edge cases), not missing method names in the audited core files.

## GitHub repository, CI and upstream tracking

- Repository: <https://github.com/tzhazuma/homebrew-py>
- Latest feature commit: `19d7959` (upstream follow-up: CaskLoader/from_bottle/zap)
- CI run for `19d7959`:
  <https://github.com/tzhazuma/homebrew-py/actions/runs/41782789878>


  - `test (ubuntu-22.04, py3.9)` — success
  - `test (ubuntu-latest, py3.12)` — success
  - `test (macos-latest, py3.12)` — success
- Upstream tracking workflow dispatch:
  <https://github.com/tzhazuma/homebrew-py/actions/runs/40382724086> — success.
  It detected a new homebrew-core HEAD and opened
  <https://github.com/tzhazuma/homebrew-py/issues/1>.
- Weekly local reminder: Monday 09:00 Asia/Shanghai (DSH schedule
  `schedule-d10d92c4-71a3-4de1-8f49-5016c0c51d3a`).

## Upstream RFC PR outcome

The RFC PR <https://github.com/Homebrew/brew/pull/24362> was reopened by the
template bot after the human-authorized template/AI-disclosure update, then
**closed by Homebrew maintainer MikeMcQuaid** on 2026-10-28 with
"Not interested in this, sorry." This is an upstream scope decision; the PR was
not reopened again. The fork branch `tzhazuma/brew:python-port-rfc` and the RFC
document remain for reference.

The guarded helper `scripts/open_upstream_pr.py` still performs no writes without
`--authorized`; it is not a tool for overriding a maintainer's close. The human
submitter owns any future upstream communication and must not use AI to answer
review comments.
