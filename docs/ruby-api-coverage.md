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
| ENV | `ENV[...]`, `fetch`, `append_path`, `prepend_path`, `prepend_create_path`, `append`, `prepend`, `delete`, `cc`, `cxx`, `cflags`, `cppflags`, `ldflags`, `make_jobs`, `with_build_environment` |
| Formula / resources | `Formula["name"]` path lookup, `name`, `version`, `prefix`, `bin`, `sbin`, `lib`, `include`, `share`, `etc`, `libexec`, `opt_prefix`, `opt_bin`, `pkgshare`, `appdir`, `buildpath`, `staged_path`, `resource(name)` staged-path object, `patch`/`option`/`depends_on` warnings |
| Build helpers | `build.head?/stable?/with?/without?`, `args`, `on_macos`/`on_linux`/`on_arm`/`on_intel`, `std_configure_args`, `std_cmake_args`, `std_meson_args`, `std_cargo_args`, `std_go_args`, `std_pip_args`, `std_service_path_env` |
| Test DSL | `shell_output`/`pipe_output` with `stderr:`/`env:`/`chdir:`, `assert_output`, path/directory/symlink assertions, `assert_equal`, `assert_match`, `assert_predicate`, `assert_empty`, `assert_includes`, `assert_nil`, `refute_*` |
| Caveats DSL | `pybrew caveats`: heredocs/strings, platform/arch blocks, paths, `ohai`/`opoo`/`puts`, ENV/Utils |

## Not implemented / partial

- Complete `Formula` class: specs, dependencies, requirements, build options,
  bottle metadata, version schemes, aliases and formula loading internals.
- Resource download/staging beyond pre-staged paths (`resource(...).stage`),
  patch application, `patch do ... end` execution.
- Homebrew internals: `Formulary`, `FormulaInstaller`, `Keg`, `Tab`, `Superenv`,
  compiler selection, sandbox, GitHub/API helpers, analytics internals.
- Full `MacOS`/SDK/Xcode/CLT detection, `DevelopmentTools`, `SystemConfig`.
- Services DSL (`service do ... end`), caveats DSL, livecheck DSL.
- Full cask DSL and cask script APIs (`Utils` internals, `MacOS` cask helpers,
  `system_command` variants are partial).
- Full test DSL (Minitest block forms, `Formula["x"].test`, all path/type
  assertions and dependency lookup).
- Any Ruby gem or third-party API used by formulae.

Unknown methods fail loudly through `method_missing`/Ruby errors instead of
silently succeeding. Extending the shim is additive and must keep the default
non-Ruby parser path unchanged.
