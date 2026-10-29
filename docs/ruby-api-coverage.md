# Ruby API coverage

pybrew is a Python reimplementation. The optional Ruby bridge is deliberately
opt-in: it runs a formula's `def install` body (or `test do ... end`) under the
host Ruby with a compatibility shim. It is **not** a vendored copy of Homebrew's
Ruby implementation and does not aim to reproduce every internal class.

The local Homebrew checkout used for comparison contains roughly 768 Ruby files
under `Library/Homebrew`; the shim covers the common formula-facing subset below.

## Implemented in the opt-in shim

| Area | Covered |
| --- | --- |
| Kernel / shell | `system`, `safe_system`, `quiet_system`, `popen`, `popen_read`, `pipe_output`, `which`, `cd` |
| Pathname | `install`, `install_symlink`, `install_p`, `install_symlink_p`, `write`, `atomic_write`, `append_lines`, `mkdir`, `mkdir_p`, `cp`, `mv`, `rm_rf`, `chmod`, `touch`, `sha256`, `verify_checksum`, `rmdir_if_possible`, `subdirs`, `text_executable?`, `binary_executable?`, `dylib?`, `mach_o_bundle?`, `make_relative_symlink`, `ensure_writable`, `install_info`, `uninstall_info`, `write_env_script`, `write_exec_script`, `resolved_path`, `resolved_path_exists?` |
| Utils | `Utils::Git`/`Curl`/`Gzip`/`Tar`/`Zip`/`Cp`/`Popen`, `safe_system`, `quiet_system`, `popen`, `popen_read`, `pipe_output`, `deconstantize`, `demodulize`, `underscore`, `pluralize`, `safe_filename`, deep helpers, `git`, `curl`, `gzip`, `which` |
| Hardware | `Hardware::CPU.arm?`, `intel?`, `arch`, `type`, `family`, `model`, `bits`, `is_64_bit?`, `cores`, `physical_cores`, `features`, `flags`, `neon?`, `sse4?`, `avx?`, `avx2?` |
| OS / MacOS | `OS.mac?`, `OS.linux?`, `OS.version`, `OS::Mac`, `OS::Linux`, `MacOS.version`, `full_version`, `version_symbol`, `sdk`, `sdk_path`, `latest_sdk_version`, `preferred_perl_version`, `languages`, `language`, `active_developer_dir` (best-effort) |
| ENV | `ENV[...]`, `fetch`, `append_path`, `prepend_path`, `prepend_create_path`, `append`, `prepend`, `delete`, `cc`, `cxx`, `cflags`, `cppflags`, `ldflags`, `make_jobs`, `append_to_cflags`, `prepend_to_cflags`, `append_to_ldflags`, `prepend_to_ldflags`, `O0..O3`, `Os/Oz/Og`, `deparallelize`, `cxx11/cxx14/cxx17/cxx20`, `remove_from_cflags`, `remove_from_ldflags`, `enable_warnings`, `libxml2`, `perl`, `python`, `java_home`, `sdkroot`, `with_build_environment` |
| Formula / resources | `Formula["name"]` metadata/path lookup (dependency categories/requirements/options/conflicts/link_overwrite), (`desc`/`homepage`/`license`/`tap`/`keg_only?`/`linked_keg`/`spec`/`stable`/`head`/`bottle`/`deprecated?`/`disabled?`/`oldname`/`aliases`/`stable?`/`head?`/`bottle?`/`bottle_tags`/`versioned_formulae`/`service?`/`caveats?`/`test?`), `name`, `version`, `prefix`, `bin`, `sbin`, `lib`, `include`, `share`, `etc`, `libexec`, `opt_prefix`, `opt_bin`, `pkgshare`, `doc`/`info`/`man*`/`etc`/`var`/completion paths, `appdir`, `buildpath`, `staged_path`, `resource(name)` metadata + lazy referenced-resource url/sha256 staging (safe tar/zip extraction), `patches` metadata, `patch` url/file/:DATA application, `option`/`depends_on` warnings |
| Service DSL | literal parser + opt-in arbitrary-Ruby evaluator (`PYBREW_ALLOW_SERVICE_RUBY=1` / `--ruby-service`): run/keep_alive/sockets/env/interval/platform blocks |
| DevelopmentTools | `clang`, `clang_version`, `default_compiler`, `installed?`, `needs_clt?`, `xcode_installed?`, `sdk_path`, `locate`; `MacOS::CLT`; `ENV.compiler` |
| Build helpers | `build.head?/stable?/with?/without?`, `args`, `on_macos`/`on_linux`/`on_arm`/`on_intel`, `std_configure_args`, `std_cmake_args`, `std_meson_args`, `std_cargo_args`, `std_go_args`, `std_pip_args`, `std_npm_args`, `std_shards_args`, `std_swift_args`, `std_zig_args`, `std_cabal_v2_args`, `std_cargo_fetch_args`, `std_go_args`, `python3`, `std_service_path_env` |
| Test DSL | `shell_output` with expected-status second arg, `pipe_output` with `stderr:`/`env:`/`chdir:`, `assert_output`, path/directory/symlink assertions, `assert_equal`, `assert_match`, `assert_predicate`, `assert_empty`, `assert_includes`, `assert_nil`, `refute_*` |
| Cask scripts | opt-in preflight/postflight/uninstall_preflight/uninstall_postflight; command/env/chdir forms; no sudo |
| Cask DSL | opt-in arbitrary-Ruby evaluator (`PYBREW_ALLOW_CASK_RUBY=1` / `--ruby-cask`): fields, artifacts, platform/arch, language, uninstall/zap, depends_on (arch/macos/formula/cask enforced), conflicts_with (formula/cask), deprecate!/disable!; scripts/unknown stanzas recorded |
| Cask caveats | `caveats --cask`: captured block/literal, token/version/appdir/paths, platform blocks |
| Caveats DSL | `pybrew caveats`: heredocs/strings, platform/arch blocks, paths, `ohai`/`opoo`/`puts`, ENV/Utils |
| post_install | `post_install do ... end` after install, with paths/Formula/system/Utils/FileUtils |

## Not implemented / partial

- Complete `Formula` class: specs, dependencies, requirements, build options,
  bottle metadata, version schemes, aliases and formula loading internals.
- Patch application supports `patch do ... end`/`patch :DATA`/relative `file`
  with `patch -pN`; some exotic forms remain unsupported. Resource staging is
  limited to archives with `url`+`sha256` and literal `resource("name")` calls
  (dynamic resource iteration stages all).
- Homebrew internals: `Formulary`, `FormulaInstaller`, `Keg`, `Tab`, `Superenv`,
  compiler selection, sandbox, GitHub/API helpers, analytics internals.
- Full `MacOS`/SDK/Xcode/CLT detection, `DevelopmentTools`, `SystemConfig`.
- Services DSL (`service do ... end`), caveats DSL, livecheck DSL.
- Full cask DSL and cask script APIs (`Utils` internals, `MacOS` cask helpers,
  `system_command` variants are partial); the arbitrary-Ruby evaluator records
  unsupported stanzas instead of applying them, while `CaskLoader` merges
  inert script bodies captured by the default parser.
- Service Ruby evaluator mirrors pybrew's literal path helpers, not Homebrew's
  global-prefix helpers, and unknown directives are ignored like the literal
  parser.
  The default literal cask parser now preserves multiline keyword arguments.
- Full test DSL (Minitest block forms, `Formula["x"].test`, all path/type
  assertions and dependency lookup).
- Any Ruby gem or third-party API used by formulae.

Unknown methods fail loudly through `method_missing`/Ruby errors instead of
silently succeeding. Extending the shim is additive and must keep the default
non-Ruby parser path unchanged.
