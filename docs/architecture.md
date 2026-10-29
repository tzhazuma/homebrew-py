# Architecture

pybrew is a clean-room Python implementation of Homebrew's core concepts. It
reads the same formula language, uses the same Cellar layout and installs the
same bottles. It deliberately does **not** execute Ruby and does **not** try to
be a drop-in replacement for every Homebrew feature yet.

## Modules

| Module | Responsibility |
| --- | --- |
| `pybrew/cli.py` | argparse CLI, commands, JSON output, error handling |
| `pybrew/ruby_parser.py` | lexer + literal-only parser for the formula Ruby DSL |
| `pybrew/models.py` | `Formula`, `Dependency`, `BottleFile`, bottle-tag selection, brew-v2 JSON |
| `pybrew/taps.py` | tap discovery, formula lookup, search |
| `pybrew/api.py` | optional `formulae.brew.sh` JSON API client |
| `pybrew/download.py` | HTTP(S)/file downloads, ghcr OCI token, SHA-256 cache |
| `pybrew/installer.py` | dependency resolution, bottle/source installation, uninstall |
| `pybrew/cellar.py` | Cellar/keg mutation, receipts, link/unlink, opt links |
| `pybrew/paths.py` | prefix/cache/tap configuration and layout |
| `pybrew/doctor.py` | prefix, Cellar, cask and service health checks (`--json`) |
| `pybrew/version.py` | version detection and natural-order comparison |
| `pybrew/source_builder.py` | executes the supported subset of `def install` (system/inreplace/ENV/install actions) |
| `pybrew/cask.py` / `cask_installer.py` | cask parser and zip/dmg installer |
| `pybrew/services.py` | service DSL parser, launchd plists, start/stop/list |
| `pybrew/tap_manager.py` | tap clone/update/remove |
| `pybrew/bundle.py` | minimal Brewfile parsing and install/check/list |

## Formula parsing

Homebrew formulae are Ruby files. A full Ruby runtime would be a large dependency
and a security boundary. pybrew instead implements:

1. a small lexer for strings, symbols, hashes, arrays, heredocs and comments;
2. a statement parser that only evaluates literal arguments;
3. handlers for the declarative DSL (`desc`, `url`, `sha256`, `bottle`,
   `depends_on`, ...) and raw capture of `def install/test/caveats`;
4. a platform guard for `on_macos`/`on_linux`/`on_arm`/`on_intel` blocks, with
   `PYBREW_TARGET_OS` / `PYBREW_TARGET_ARCH` overrides for reproducible corpus
   tests.

Unknown calls are stored in `Formula.extra` instead of aborting. This is what
lets a 15-formula corpus (and in principle all 8,600+ homebrew-core formulae)
parse without executing any Ruby. `install` bodies are only interpreted by the
small staged-source installer described below.

### Parsing invariants

- No code execution, no `eval`, no shelling out to Ruby.
- Strings are decoded only for common escapes; interpolation is preserved.
- Malformed DSL raises `FormulaParseError` with a line number; unknown method
  calls never abort parsing.
- The parser preserves duplicate dependency names with different tag sets
  (e.g. `gettext` can be both a build and runtime dependency).

## Installation pipeline

```text
install name
  ├─ FormulaLoader.load(name)            (tap → Ruby parser)
  ├─ Installer.resolve(formula)          (recursive deps + topo sort + cycle check)
  ├─ for each formula:
  │    ├─ already installed? skip
  │    ├─ bottle available for host tag?
  │    │    ├─ fetch from cache or ghcr/root_url
  │    │    ├─ verify SHA-256
  │    │    ├─ safe tar extraction (no absolute/.. /escaping symlinks)
  │    │    ├─ relocate @@HOMEBREW_*@@ placeholders
  │    │    ├─ move keg into Cellar/<name>/<pkg_version>
  │    │    ├─ write INSTALL_RECEIPT.json
  │    │    └─ link (or only opt for keg-only)
  │    └─ else: source build via SourceBuilder (autotools/CMake/Cargo/...)
  │         or explicit UnsupportedInstall
  └─ print result
```

### Bottle selection

`candidate_bottle_tags()` maps the running macOS to Homebrew bottle tags
(`golden_gate` → `tahoe` → `sequoia` → ...), handles Apple Silicon vs Intel,
Linux tags, and allows `PYBREW_BOTTLE_TAG` / `--bottle-tag` overrides. For
ghcr.io URLs, `download.py` obtains an anonymous pull token from
`ghcr.io/token`, requests the OCI blob, and follows redirects without leaking
the Authorization header to the object store.

### Relocation

Bottles contain text and binary placeholders such as `@@HOMEBREW_PREFIX@@` and
`@@HOMEBREW_CELLAR@@`. Text files are rewritten with the configured prefix. On
macOS, Mach-O load commands and LC_ID_DYLIB entries are rewritten with
`install_name_tool` (which can grow load commands) and ad-hoc signed with
`codesign -s -`. On ELF, `patchelf --print-rpath/--set-rpath` rewrites placeholder
RPATH entries and `--print-interpreter/--set-interpreter` rewrites PT_INTERP
when the tool is available (restoring pristine bytes on failure);
otherwise byte rewriting is used only when the replacement fits, else the binary
is reported as skipped rather than corrupted.

### Linking

Homebrew does not symlink whole keg directories. pybrew walks `bin`, `sbin`,
`lib`, `include`, `share`, `etc`, `libexec` and `Frameworks` inside the keg and
creates relative symlinks at the corresponding prefix paths. `opt/<name>` always
points at the current keg. Conflicts are detected before any link is created and
raise `LinkConflict` unless `--overwrite` or `--force` is used. `unlink` removes
only links whose resolved target is inside the keg, then prunes empty
directories.

### Receipts

Each keg contains `INSTALL_RECEIPT.json` with the pybrew version, source path,
tap, package version, runtime dependency names and bottle/source flags. `list`,
`info --json`, `doctor` and uninstall read it when the tap formula is no longer
available.

## Source builds

`source_builder.py` is a small, literal-oriented executor for the `def install`
method. It understands `system` calls (including `*std_*_args` helpers),
`inreplace`, `ENV` mutation, `bin.install`/`prefix.install`-style actions,
`cd` blocks and simple host conditionals. It runs commands in the extracted
source tree with a Homebrew-like environment (`HOMEBREW_PREFIX`, `PATH`,
`PKG_CONFIG_PATH`, compiler flags). Autotools, CMake and Cargo fixtures compile
real binaries in the test suite, and the real GNU `hello` formula has been built
end-to-end. Unsupported Ruby constructs fail with the line number.

## Casks

`cask.py` parses the cask DSL, including `arch`/`sha256 arm,intel`,
`on_<macos>` conditionals and default `language` blocks. When no local
homebrew-cask tap exists, `--api` loads the same cask metadata from
formulae.brew.sh. `cask_installer.py` downloads the URL, verifies SHA-256, then
handles zip, tar, dmg and pkg containers:
`.app` bundles are copied to `<prefix>/Applications`, `binary` artifacts are
symlinked into `<prefix>/bin`, and receipts live in
`<prefix>/Caskroom/<token>/<version>/.metadata/receipt.json`. Uninstall removes
receipt-tracked paths and executes the safe subset of cask uninstall actions
(`quit` via osascript, `pkgutil --forget`, `delete` for leftovers); `launchctl`,
login items and scripts are deliberately not executed. `--zap` removes the
cask's declared trash paths after checking they stay inside the user
home/prefix/temp roots. Library artifact kinds (`font`, `qlplugin`, VST plugins,
etc.) copy into `<prefix>/Library/...` by default or `--library-root`; `stage_only`
writes a receipt without artifacts. `.pkg` payloads are expanded under
`--pkg-root`; `--system-pkg` opts into a root-only macOS `installer` call,
tracks package IDs for root-only uninstall, and cleans/rolls back IDs during
upgrade. `--run-scripts` opts into preflight/postflight Ruby scripts (inert by
default) with a shim covering common Utils/MacOS/OS/Shellwords helpers. Exotic
script APIs remain out of scope.

## Services

`services.py` parses `service do ... end` blocks (run, working_dir, log paths,
keep_alive, environment_variables, interval, process_type). On macOS it renders
a launchd plist and drives `launchctl`; on Linux it renders a systemd user unit
and drives `systemctl --user`. Both runners are injectable so tests use fake
binaries. `services list/start/stop/restart/info/cleanup/run` are available in the CLI;
`cleanup` removes service files for uninstalled formulae. `shellenv` prints the
prefix/cellar/PATH exports for bash/zsh/fish.

## Taps

`tap_manager.py` manages `<prefix>/Library/Taps/<user>/homebrew-<repo>` with
`git clone --depth 1`, `git pull --ff-only`, `--update` for all taps, and safe
removal. Taps become visible to formula/cask loaders on the next invocation
because loaders discover them from the prefix.

## Brewfiles

`bundle.py` parses the common Homebrew Brewfile subset: `tap`, `brew`, `cask`,
`mas` and `vscode` lines. `bundle install` installs taps first, then formulae and casks, reusing the
normal installers; `bundle check` reports missing entries; `bundle list` prints
the parsed entries; `bundle dump` writes taps/formulae/casks back to a Brewfile.
`tap ..., url: "..."`, `brew ..., link: false`, `brew ..., args: [...]`,
`brew ..., restart_service: true`, `mas ... id:` and `vscode ...` are supported;
other advanced options are ignored. `cleanup --prune-cache` removes
downloads older than `--cache-age-days` (default 120) in addition to stale kegs.

## Pinning

`pin` records a formula under `var/pybrew/pinned`; `outdated` and all-formula
upgrades skip pinned formulae until `unpin`. `list --pinned` shows the set.

## Source inspection

`cat`, `home` and `edit` resolve local formula/cask files; `cat` prints the source,
`home` prints the homepage and `edit` opens `$EDITOR`/`$VISUAL` on the file.

## Fetch

`fetch` resolves a formula's bottle (or source) and downloads it through the
normal cache/SHA-256 path without installing anything; `fetch --cask` does the
same for a local cask artifact.

## Safety model

- Default prefix is `~/.pybrew`; operating on `/opt/homebrew` or `/usr/local`
  requires an explicit `--prefix`.
- Tar extraction rejects absolute paths, `..` components, absolute symlinks and
  symlinks escaping the extract root.
- Downloads are verified against the formula/bottle SHA-256 before extraction.
- `uninstall` refuses to remove paths outside the Cellar.
- No Ruby, no formula-supplied shell commands in the current installer.

## Analytics

`analytics.py` stores an opt-in local event log under
`var/pybrew/analytics.json` (default disabled, mode 0600, no network). The CLI
exposes `analytics state/on/off/log/clear`; successful install/uninstall/upgrade
commands append local events when enabled.

## Formula tests

`formula_test.py` runs a formula's captured `test do ... end` under Ruby with a
limited assertion/`shell_output` shim; `pybrew test <formula>` exposes it. The
runner is opt-in and requires an installed keg.

## Caveats

`caveats.py` runs a formula's captured caveats body under Ruby with a small
platform/path shim; `pybrew caveats <formula>` does not require installation.

## Resource staging

When the opt-in Ruby bridge has a downloader, formula resources referenced by
literal `resource("name")` calls in the install body and declaring `url`+`sha256`
are fetched and safely extracted into `build_root/.pybrew-resources/<name>`
before the install body runs. Dynamic `resources.each`/variable uses stage all
conservatively. Patches are applied by the source builder before install.

## Patches and post_install

The parser captures `patch do ... end`/`patch :DATA` into `Formula.patches`;
the source builder applies them with `patch -pN` before install. Captured
`post_install` source runs under Ruby after a successful install/link and is
recorded in the receipt.

## Known limitations

- Source builds support a common literal `def install` subset (on_arm/on_intel,
  predicates, resources and HEAD included); an opt-in Ruby bridge covers
  arbitrary install bodies with a broader Utils/MacOS/OS/Shellwords shim, but not
  every Homebrew Ruby API; see `docs/ruby-api-coverage.md`.
- ELF relocation needs `patchelf` for long RPATH replacements; Mach-O uses
  `install_name_tool`; further edge cases remain.
- Cask `.pkg` payloads are expanded under `--pkg-root`; `--system-pkg` delegates
  to macOS `installer` when root, but package scripts and system pkg
  uninstall/rollback are not implemented. Library artifacts are prefix-scoped
  unless `--library-root` is supplied.
- Services are best-effort: launchd/systemd are invoked, but log rotation,
  process supervision edge cases and root services are not implemented.
- Analytics is local-only and opt-in; some advanced Brewfile options and full
  cask options remain incomplete.
- `brew`'s test DSL is captured but not executed.

## Testing strategy

- Unit tests for version/parser/tag logic.
- Integration tests using a synthetic tap, a real gzipped bottle, a local HTTP
  file URL and a temporary prefix.
- Corpus tests against 15 real homebrew-core formulae with expected metadata
  generated from `brew info --json=v2`.
- One real-bottle test against a cached `libslirp 4.9.6` bottle.
- Real source-build fixtures for autotools, CMake and Cargo; real GNU `hello`
  source build verified manually.
- Cask tests with real cask definitions plus a synthetic zip and dmg.
- Service tests with a fake `launchctl`, and tap tests against a local git remote.
