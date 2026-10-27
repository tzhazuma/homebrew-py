# Parity with Ruby Homebrew

Tested against **Homebrew 7.0.9** (`cdc1ca5032`) on macOS 27.2 (arm64 hardware,
brew running as x86_64 Rosetta), Python 3.12.7, 2026-10-27.

## Real formula corpus

The following formulae were copied from `Homebrew/homebrew-core@main` and parsed
by pybrew. Every desc/homepage/license/version/revision/keg_only/dependency
field below matched `brew info --json=v2` exactly.

| Formula | Version | Runtime deps | Build deps | `uses_from_macos` | Bottle tags |
| --- | --- | ---: | ---: | --- | ---: |
| abseil | 20260817.0 | 0 | 2 | – | 7 |
| aom | 3.15.2 | 1 | 3 | – | 5 |
| autoconf | 2.73 | 1 | 0 | perl | 9 |
| bash | 5.3.20 | 3 | 0 | – | 5 |
| cmake | 4.4.4 | 0 | 0 | ncurses | 5 |
| curl | 8.23.0 | 8 | 1 | krb5, openldap | 5 |
| git | 2.56.0 | 2 | 2 | curl, expat | 5 |
| hello | 2.12.3 | 0 | 0 | – | 6 |
| jq | 1.8.2 | 1 | 0 | – | 7 |
| libslirp | 4.9.6 | 1 | 3 | – | 5 |
| openssl@3 | 3.5.9 | 1 | 0 | – | 5 |
| python@3.12 | 3.12.16 | 4 | 1 | 6 | 5 |
| sqlite | 3.54.1 | 1 | 0 | – | 5 |
| tree | 2.3.2 | 0 | 0 | – | 7 |
| zlib | 1.3.2 | 0 | 0 | – | 7 |

Run:

```bash
PYBREW_TARGET_OS=darwin PYBREW_TARGET_ARCH=x86_64 \
  pytest tests/test_real_formulas.py -q
```

## Feature matrix

| Capability | Ruby Homebrew | pybrew 0.1 |
| --- | --- | --- |
| Formula DSL metadata | Full Ruby evaluation + DSL | Literal-only parser, tolerates unknown calls |
| Dependency resolution | Full, options/requirements | Runtime/build/test/optional/recommended + uses_from_macos |
| Bottle install | All platforms, OCI + legacy mirrors | ghcr.io OCI + plain root_url + local file; SHA-256; text relocation + Mach-O install_name_tool |
| Source install | Any build system / formula Ruby | Common `def install` subset: system/inreplace/ENV/install actions/resource staging/--HEAD; autotools/CMake/Cargo tested; meson flow tested with a fake tool; unsupported Ruby fails explicitly |
| Cellar / keg / receipts | Yes | Yes, compatible layout |
| opt / linking | Yes | Yes, conflict-aware, --overwrite |
| `info --json` | Full v2 schema | Compatible subset of v2 fields |
| Casks | Yes | zip/dmg app/binary/suite, receipts, uninstall/zap + quit/delete; pkg payload via `--pkg-root`; API; outdated/upgrade/reinstall; `--cask` flags |
| Tap management | `brew tap` | `tap`/`untap`/list/`--update`/`update` with GitHub or local git URLs |
| Services | Yes | macOS launchd + Linux systemd user units; start/stop/restart/list/info/run |
| Outdated / upgrade / auto-remove / leaves / uses / cleanup / reinstall / pin | Yes | outdated; upgrade installs/relinks/removes old kegs; autoremove; cleanup; reinstall; pin |
| Bundle | Yes | Brewfile install/check/list/dump, tap URL, link:false |
| Analytics | Yes | No |
| Formula tests | `brew test` | Not executed |
| Fetch / source inspection | `brew fetch`, `cat`, `home`, `edit` | `fetch` formula/cask; `cat`/`home`/`edit` formula/cask |
| Self-update / doctor | Yes | `doctor` checks prefix, Cellar, cask artifacts, links and orphan services; `--json`; `shellenv` |

## Verified integration cases

- `install hello` from a synthetic tap via a local bottle: cellar, opt link,
  bin/lib links, placeholder relocation, receipt, cache entry.
- `uninstall hello`: keg and all links removed.
- Link conflict: pre-existing `bin/hello` blocks install; `--force`/`--overwrite`
  replaces it.
- Corrupted bottle: SHA-256 mismatch aborts before extraction.
- Malicious tar: `../` member and absolute symlink are rejected.
- keg-only formula: only `opt/<name>` is created.
- Dependency order: `hello-dep` installs `dep` first.
- Staged source formula: `--build-from-source` installs from the source tarball.
- Real bottle: cached Homebrew `libslirp 4.9.6` bottle installs offline, including
  its real `lib/libslirp.dylib`, headers, pkg-config file and receipt; live ghcr
  install verifies Mach-O relocation (`otool -L` shows the final prefix).
- CLI: info/search/list/deps/doctor/config/parse all exercised in subprocesses.
- Tap: clone a local git tap, update it, load a formula from it, untap.
- Bundle: a Brewfile with `brew`/`cask` entries supports dry-run install, real
  install, check, list and dump; cache cleanup prunes old downloads.
- Source build: real autotools/CMake/Cargo fixture compilation, `inreplace`, host conditionals; real GNU hello 2.12.3 built manually.
- Cask: real rectangle/iterm2/VSCode definitions parse; synthetic zip, dmg and pkg installs verify app/binary/receipt/uninstall/zap, uninstall quit/delete and API mapping.
- Services: DSL parsing, launchd plist fields, start/stop/list with a fake `launchctl`; real launchctl is not invoked in tests.

## Known differences

1. Source builds use a deliberately small install-DSL subset. A formula that
   uses unsupported Ruby (loops, complex build.with? branches) fails with
   `UnsupportedInstall` instead of pretending to succeed; simple resource
   staging is supported.
2. Mach-O relocation uses `install_name_tool` and ad-hoc signing; ELF
   relocation is byte-level and skips binaries whose paths cannot fit. More
   RPATH edge cases remain future work.
3. Homebrew's `brew info --json=v2` has more fields (patches, requirements,
   deprecation dates, post-install steps). pybrew emits the core subset.
4. On this machine brew is x86_64 under Rosetta; corpus expectations therefore
   set `PYBREW_TARGET_ARCH=x86_64`, while native pybrew/pytest is arm64. The
   parser supports both.
5. Cask package scripts/system-wide installs, advanced Brewfile options and
   analytics remain roadmap items.
