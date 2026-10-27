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
76 passed in 38.32s
```

Breakdown of the feature slices:

```console
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_real_formulas.py
17 passed      # 15 parametrized real formulae + 2 aggregate checks
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_bottles.py
10 passed
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_cask.py
12 passed      # includes real dmg and pkg payload tests on macOS and install --cask
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_source_build.py
5 passed       # autotools, CMake, Cargo, inreplace, conditionals
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_services.py
7 passed       # launchd + systemd unit generation
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_taps.py
3 passed
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_maintenance.py
4 passed
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
$ PYTHONPATH=src python -m pytest -q tests/test_real_bottle.py -v
1 passed in 0.03s
```

Verified in the resulting keg:

- `Cellar/libslirp/4.9.6/lib/libslirp.dylib`
- `Cellar/libslirp/4.9.6/include/slirp/libslirp.h`
- `INSTALL_RECEIPT.json` with `poured_from_bottle: true` and `runtime_dependencies: ["glib"]`
- text placeholders (`@@HOMEBREW_PREFIX@@`) relocated, including `lib/pkgconfig/slirp.pc`

The bottle was read from `~/Library/Caches/Homebrew/downloads`, so the test is
offline and does not re-download the artefact.

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

- `outdated` compares installed keg versions with the tap formula's
  `pkg_version` and supports `--json`.
- `upgrade --dry-run` resolves the newer formula and reports the plan without
  downloading or writing a keg.
- `cleanup` keeps the opt-linked keg (or newest) and removes older kegs;
  `--dry-run` is covered.
- `reinstall` uninstalls then reinstalls a formula and restores links.

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

- Live download from ghcr.io was exercised by unit logic but not by the offline
  test suite; the real bottle test intentionally uses a cached artefact.
- Meson builds are implemented via `std_meson_args` but meson is not installed in
  this environment, so only helper expansion is covered.
- Cask package scripts and system-wide `.pkg` installation.
- Mach-O binary relocation was not validated by running the installed dylib.
- `brew lgtm`, `brew bundle`, `brew upgrade`, analytics.

These are tracked in the README roadmap and `docs/parity.md`.

## Upstream PR attempt

A draft RFC PR was opened against `Homebrew/brew`:

- <https://github.com/Homebrew/brew/pull/24362>

It was automatically closed after ~12 seconds by Homebrew's PR-template bot because
the body did not match `PULL_REQUEST_TEMPLATE.md`; the bot also warned that the PR
appeared AI-generated. This is an upstream process gate, not a technical review.
The fork branch remains at `tzhazuma/brew:python-port-rfc` and the proposal text is
kept in `docs/python-port-rfc.md` for a human follow-up (fill the template, disclose
AI usage, run applicable checks). No attempt was made to evade the bot by
misrepresenting authorship.

## GitHub repository, CI and upstream tracking

- Repository: <https://github.com/tzhazuma/homebrew-py>
- CI run for the feature commit `03e6ba9`:
  <https://github.com/tzhazuma/homebrew-py/actions/runs/40445584843>
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

The draft RFC PR <https://github.com/Homebrew/brew/pull/24362> was created, then
automatically closed by Homebrew's PR-template bot (missing template, possible
AI authorship warning). The fork branch `tzhazuma/brew:python-port-rfc` remains
available.

A complete re-submission template is now prepared in
`docs/upstream-pr/PR_BODY.md` (with the current Homebrew template checkboxes and
an AI disclosure section) and `docs/upstream-pr/REVIEW_AND_SUBMIT.md`. It has
**not** been resubmitted: Homebrew requires the human submitter to review all
AI-generated content and answer review comments without AI. The guarded helper
`scripts/open_upstream_pr.py` performs no writes unless `--authorized` is passed
and still requires the human to tick the AI checkbox and confirm the disclosure.
