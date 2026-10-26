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
| `pybrew/doctor.py` | prefix health checks |
| `pybrew/version.py` | version detection and natural-order comparison |

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
  │    └─ else: staged source install / explicit UnsupportedInstall
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
`@@HOMEBREW_CELLAR@@`. Text files are rewritten with the configured prefix.
Mach-O/ELF files are rewritten only when the replacement is not longer than the
placeholder (using NUL padding); longer replacements are reported as skipped
rather than corrupting the binary. A future version will use `install_name_tool`
and RPATH rewriting.

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

## Safety model

- Default prefix is `~/.pybrew`; operating on `/opt/homebrew` or `/usr/local`
  requires an explicit `--prefix`.
- Tar extraction rejects absolute paths, `..` components, absolute symlinks and
  symlinks escaping the extract root.
- Downloads are verified against the formula/bottle SHA-256 before extraction.
- `uninstall` refuses to remove paths outside the Cellar.
- No Ruby, no formula-supplied shell commands in the current installer.

## Known limitations

- Only Homebrew bottles and "already-built" staged source formulae are supported.
- Binary relocation is best-effort, not Mach-O-aware.
- No cask support, no services, no tap management, no analytics.
- `brew`'s test DSL is captured but not executed.

## Testing strategy

- Unit tests for version/parser/tag logic.
- Integration tests using a synthetic tap, a real gzipped bottle, a local HTTP
  file URL and a temporary prefix.
- Corpus tests against 15 real homebrew-core formulae with expected metadata
  generated from `brew info --json=v2`.
- One real-bottle test against a cached `libslirp 4.9.6` bottle.
