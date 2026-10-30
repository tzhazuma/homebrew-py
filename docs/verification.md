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
552 passed, 5 skipped in 60.45s
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
123 passed     # declarative + opt-in Ruby bridge/API + Keg/Tab/Formulary + caveats
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

## GitHub repository, CI and upstream tracking

- Repository: <https://github.com/tzhazuma/homebrew-py>
- Latest feature commit: `ba77bc2` (Formula bottle accessors)
- CI run for `ba77bc2`:
  <https://github.com/tzhazuma/homebrew-py/actions/runs/41001484693>
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
