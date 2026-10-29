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
| Utils | `safe_system`, `quiet_system`, `popen`, `popen_read`, `pipe_output`, `deconstantize`, `demodulize`, `underscore`, `pluralize`, `safe_filename`, deep helpers, `git`, `curl`, `gzip`, `which` |
| Hardware | `Hardware::CPU.arm?`, `intel?`, `arch`, `type`, `family`, `model`, `bits`, `is_64_bit?`, `cores`, `physical_cores`, `features`, `flags`, `neon?`, `sse4?`, `avx?`, `avx2?` |
| OS / MacOS | `OS.mac?`, `OS.linux?`, `OS.version`, `OS::Mac`, `OS::Linux`, `MacOS.version`, `full_version`, `version_symbol`, `sdk`, `sdk_path`, `latest_sdk_version`, `preferred_perl_version`, `languages`, `language`, `active_developer_dir` (best-effort) |
| ENV | `ENV[...]`, `fetch`, `append_path`, `prepend_path`, `prepend_create_path`, `append`, `prepend`, `delete`, `cc`, `cxx`, `cflags`, `cppflags`, `ldflags`, `make_jobs`, `append_to_cflags`, `prepend_to_cflags`, `append_to_ldflags`, `prepend_to_ldflags`, `O0..O3`, `deparallelize`, `cxx11`, `libxml2`, `perl`, `python`, `java_home`, `sdkroot`, `with_build_environment` |
| Formula / resources | `Formula["name"]` metadata/path lookup (`desc`/`homepage`/`license`/`tap`/`keg_only?`/`linked_keg`/`spec`), `name`, `version`, `prefix`, `bin`, `sbin`, `lib`, `include`, `share`, `etc`, `libexec`, `opt_prefix`, `opt_bin`, `pkgshare`, `appdir`, `buildpath`, `staged_path`, `resource(name)` metadata + eager url/sha256 staging (safe tar/zip extraction), `patch` url/file/:DATA application, `option`/`depends_on` warnings |
| Build helpers | `build.head?/stable?/with?/without?`, `args`, `on_macos`/`on_linux`/`on_arm`/`on_intel`, `std_configure_args`, `std_cmake_args`, `std_meson_args`, `std_cargo_args`, `std_go_args`, `std_pip_args`, `std_service_path_env` |
| Test DSL | `shell_output` with expected-status second arg, `pipe_output` with `stderr:`/`env:`/`chdir:`, `assert_output`, path/directory/symlink assertions, `assert_equal`, `assert_match`, `assert_predicate`, `assert_empty`, `assert_includes`, `assert_nil`, `refute_*` |
| Cask scripts | opt-in preflight/postflight/uninstall_preflight/uninstall_postflight; command/env/chdir forms; no sudo |
| Cask DSL | opt-in arbitrary-Ruby evaluator (`PYBREW_ALLOW_CASK_RUBY=1` / `--ruby-cask`): fields, artifacts, platform/arch, language, uninstall/zap, depends_on; scripts/unknown stanzas recorded |
| Cask caveats | `caveats --cask`: captured block/literal, token/version/appdir/paths, platform blocks |
| Caveats DSL | `pybrew caveats`: heredocs/strings, platform/arch blocks, paths, `ohai`/`opoo`/`puts`, ENV/Utils |
| post_install | `post_install do ... end` after install, with paths/Formula/system/Utils/FileUtils |

## Not implemented / partial

- Complete `Formula` class: specs, dependencies, requirements, build options,
  bottle metadata, version schemes, aliases and formula loading internals.
- Patch application supports `patch do ... end`/`patch :DATA`/relative `file`
  with `patch -pN`; some exotic forms remain unsupported. Resource staging is
  eager and limited to archives with `url`+`sha256`.
- Homebrew internals: `Formulary`, `FormulaInstaller`, `Keg`, `Tab`, `Superenv`,
  compiler selection, sandbox, GitHub/API helpers, analytics internals.
- Full `MacOS`/SDK/Xcode/CLT detection, `DevelopmentTools`, `SystemConfig`.
- Services DSL (`service do ... end`), caveats DSL, livecheck DSL.
- Full cask DSL and cask script APIs (`Utils` internals, `MacOS` cask helpers,
  `system_command` variants are partial); the arbitrary-Ruby evaluator records
  unsupported stanzas instead of applying them, and scripts remain opt-in.
  The default literal cask parser now preserves multiline keyword arguments.
- Full test DSL (Minitest block forms, `Formula["x"].test`, all path/type
  assertions and dependency lookup).
- Any Ruby gem or third-party API used by formulae.

Unknown methods fail loudly through `method_missing`/Ruby errors instead of
silently succeeding. Extending the shim is additive and must keep the default
non-Ruby parser path unchanged.
