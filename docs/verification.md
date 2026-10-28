# Verification record

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
104 passed in 25.70s
```

Breakdown of the feature slices:

```console
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_real_formulas.py
17 passed      # 15 parametrized real formulae + 2 aggregate checks
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_bottles.py
10 passed
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_cask.py
17 passed      # real dmg/pkg payload, install --cask, API, outdated/upgrade, uninstall actions
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_source_build.py
8 passed       # autotools, CMake, Cargo, meson(fake), inreplace, conditionals, resources, HEAD
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_services.py
8 passed       # launchd + systemd unit generation, orphan cleanup
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_taps.py
5 passed
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_maintenance.py
9 passed
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_bundle.py
5 passed
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_cli.py
8 passed
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
- Cask package scripts and system-wide `.pkg` installation.
- `brew lgtm`, advanced Brewfile options, analytics.

These are tracked in the README roadmap and `docs/parity.md`.

## Upstream PR attempt

RFC PR <https://github.com/Homebrew/brew/pull/24362> was initially closed by
Homebrew's PR-template bot because the body did not match
`PULL_REQUEST_TEMPLATE.md`. After the human reviewed and explicitly authorized
the submission, the body was updated with the template and AI disclosure, the
AI-disclosure checkbox was checked, and the template bot reopened the PR:

- state: open, ready for review (draft=false)
- reopened by `github-actions[bot]` on 2026-10-28
- `brew lgtm` remains unchecked with an explanatory note because the PR only
  adds an RFC document and does not change Homebrew Ruby code
- no attempt was made to misrepresent authorship or bypass the bot

The submitting human is responsible for answering maintainer review comments
without AI. PR state should be re-checked on GitHub; the bot may re-evaluate
the template after edits.

## GitHub repository, CI and upstream tracking

- Repository: <https://github.com/tzhazuma/homebrew-py>
- Latest feature commit: `7774a60` (cask `pkgutil` uninstall action)
- CI run for the pkgutil-uninstall feature commit `7774a60`:
  <https://github.com/tzhazuma/homebrew-py/actions/runs/40557380180>
  - `test (ubuntu-latest, py3.9)` — success
  - `test (ubuntu-latest, py3.12)` — success
  - `test (macos-latest, py3.12)` — success
- Upstream tracking workflow dispatch:
  <https://github.com/tzhazuma/homebrew-py/actions/runs/40382724086> — success.
  It detected a new homebrew-core HEAD and opened
  <https://github.com/tzhazuma/homebrew-py/issues/1>.
- Weekly local reminder: Monday 09:00 Asia/Shanghai (DSH schedule
  `schedule-d10d92c4-71a3-4de1-8f49-5016c0c51d3a`).

## Upstream RFC PR outcome

The RFC PR <https://github.com/Homebrew/brew/pull/24362> is now **open and ready
for review** after the template bot reopened it. The body includes the current
Homebrew template checkboxes and the AI disclosure; the AI checkbox was checked
only after the submitting human explicitly confirmed that they had reviewed the
body and authorized submission. The fork branch
`tzhazuma/brew:python-port-rfc` contains the RFC document.

The guarded helper `scripts/open_upstream_pr.py` still performs no writes without
`--authorized`. The human submitter must answer maintainer review comments
without AI and remains responsible for the upstream submission.
