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
45 passed in 1.64s
```

Breakdown:

```console
$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_real_formulas.py
17 passed in 0.02s      # 15 parametrized real formulae + 2 aggregate checks

$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_bottles.py
10 passed in 0.84s

$ PYTHONPATH=src python -m pytest -o addopts='' -q tests/test_cli.py
8 passed in 0.76s
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
- Full source builds (autotools/cmake/meson), casks, services, tap management.
- Mach-O binary relocation was not validated by running the installed dylib.

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
