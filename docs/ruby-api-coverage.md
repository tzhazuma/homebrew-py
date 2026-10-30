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
| Utils | `Utils::Path` (resolved paths, rmdir-if-possible, installed prefixes, opt bin paths/env); `Utils::Git` (`path`/`version`/`head`/`last_commit`/`origin_url`); `Utils::Curl` (`curl_executable`/`curl_path`/`version`)/`Gzip`/`Tar`/`Zip`/`Cp`/`Popen`, `safe_system`, `quiet_system`, `popen`, `popen_read`, `pipe_output`, `deconstantize`, `demodulize`, `underscore`, `pluralize`, `safe_filename`, deep helpers, `git`, `curl`, `gzip`, `which` |
| Hardware | `Hardware::CPU.arm?`, `intel?`, `arch`, `type`, `family`, `model`, `bits`, `is_64_bit?`, `cores`, `physical_cores`, `features`, `flags`, `neon?`, `sse4?`, `avx?`, `avx2?` |
| OS / MacOS | `OS.mac?`, `OS.linux?`, `OS.version`, `OS::Mac`, `OS::Linux`, `MacOS.version`, `full_version`, `version_symbol`, `sdk`, `sdk_path`, `latest_sdk_version`, `preferred_perl_version`, `languages`, `language`, `active_developer_dir` (best-effort) |
| ENV | `ENV[...]`, `fetch`, `append_path`, `prepend_path`, `prepend_create_path`, `append`, `prepend`, `delete`, `cc`, `cxx`, `cflags`, `cppflags`, `ldflags`, `make_jobs`, `append_to_cflags`, `prepend_to_cflags`, `append_to_ldflags`, `prepend_to_ldflags`, `O0..O3`, `Os/Oz/Og`, `deparallelize`, `cxx11/cxx14/cxx17/cxx20`, `remove_from_cflags`, `remove_from_ldflags`, `enable_warnings`, `libxml2`, `perl`, `python`, `java_home`, `sdkroot`, `with_build_environment` |
| Formula / resources | `Formula["name"]` metadata/path lookup (dependency categories/requirements/options/conflicts/link_overwrite), (`desc`/`homepage`/`license`/`tap`/`keg_only?`/`linked_keg`/`spec`/`stable`/`head`/`bottle`/`deprecated?`/`disabled?`/`oldname`/`aliases`/`stable?`/`head?`/`bottle?`/`bottled?`/`bottle_tags`/`bottle_for_tag`/`bottle_url`/`versioned_formulae`/`service?`/`caveats?`/`test?`/`test` execution/`livecheck?`/`livecheck` object/`to_hash`/`to_hash_with_variations`/`urls_hash`/`bottle_hash`/`outdated_kegs`/`outdated?`/`post_install?`/`post_install`/`fetch?`/`fetch`/`stage`/`brew`/`url`/`sha256`/`mirror`/`system`/`mkdir`/`inreplace`/`buildpath`/`staged_path`/`mktemp`/`lock`/`unlock`/`run_test`/`run_post_install`/`setup_home`/`validate_attributes!`/pinning (`pinned?`/`pinnable?`/`pin`/`unpin`/`pinned_version`)/compatibility helpers (broad batch) + with_logging + source accessors + rendered `caveats` + global `HOMEBREW_PREFIX`/`HOMEBREW_CELLAR` constants + installed `Keg` objects/prefixes (sorted) + `tab` + `service` ref (all ServiceSpec fields + plist/service names) + `python3`), `name`, `to_s`/`==`/`hash`, `version`, `prefix`, `bin`, `sbin`, `lib`, `include`, `share`, `etc`, `libexec`, `opt_prefix`, `opt_bin`, `pkgshare`, `doc`/`info`/`man*`/`etc`/`var`/completion paths, `appdir`, `buildpath`, `staged_path`, `resource(name)` metadata + lazy referenced-resource url/sha256 staging (safe tar/zip extraction), `patches` metadata, `patch` url/file/:DATA application, `option`/`depends_on` warnings |
| Service DSL | literal parser + opt-in arbitrary-Ruby evaluator (`PYBREW_ALLOW_SERVICE_RUBY=1` / `--ruby-service`): run (multiple args)/keep_alive/sockets/env/interval/nice/restart/stop/root/input/cron/run_type/name (macos:/linux:)/platform blocks + systemd timer/socket + custom plist/service labels |
| DevelopmentTools | `clang`, `clang_version`, `clang_build_version`, `default_compiler`, `installed?`, `needs_clt?`, `xcode_installed?`, `sdk_path`, `locate`; `MacOS::CLT`; `MacOS::Xcode`; `ENV.compiler` |
| Sandbox | `Sandbox.available?`, `full_write_isolation?`, `nested_sandbox?`, `avoid_nested_sandboxing?`, `use_for?`, `inherited_sandbox?`, `state`, `failure_reason`, `reset_state!`, `with_preserved_brew_file`, `run_or_fork` |
| Superenv | `Superenv.shims_path`, `.bin`, `.enabled_for?` |
| SystemConfig | `clang`/`clang_build`, `describe_clang`, `describe_homebrew_ruby`, `hardware`, `kernel`, git/curl descriptions, `config_sections`, `homebrew_config`, `homebrew_env_config`, `hardware_config`, `host_software_config`, `dump_tap_config`, `core_tap_config`, `dump_verbose_config`/`dump` |
| Build helpers | Formula instance + install context: `build.head?/stable?/with?/without?`, `args`, `on_macos`/`on_linux`/`on_arm`/`on_intel`, `std_configure_args`, `std_cmake_args`, `std_meson_args`, `std_cargo_args`, `std_go_args`, `std_pip_args`, `std_npm_args`, `std_shards_args`, `std_swift_args`, `std_zig_args`, `std_cabal_v2_args`, `std_cargo_fetch_args`, `std_go_args`, `python3`, `std_service_path_env` |
| Test DSL | `shell_output` with expected-status second arg, `pipe_output` with `stderr:`/`env:`/`chdir:` and third positional expected status, `assert_output` block/wildcard/regex forms, `capture_io`/`capture_subprocess_io`, `assert_raises`/`assert_throws`/`assert_silent`, path/file/directory/symlink/executable assertions + refute variants, `assert_equal`/`assert_match`/`assert_predicate`/`assert_empty`/`assert_includes`/`assert_nil`, numeric/same/instance-of assertions, `flunk`/`pass`, dependency `Formula[...]` lookup and expanded path helpers |
| Cask scripts | opt-in preflight/postflight/uninstall_preflight/uninstall_postflight; command/env/chdir forms; no sudo |
| Cask DSL | opt-in arbitrary-Ruby evaluator (`PYBREW_ALLOW_CASK_RUBY=1` / `--ruby-cask`): fields, artifacts, platform/arch, language, uninstall/zap, os stanza (both parsers, macOS versions), depends_on (arch/macos/formula/cask enforced), conflicts_with (formula/cask), deprecate!/disable! reasons, no_autobump!, livecheck metadata/source (best-effort), MacOS version/SDK helpers, script-context `system_command` (run! semantics, `must_succeed: false`) and `Utils.popen_read`/`which`; scripts/unknown stanzas recorded, metadata helpers (`autobump?`/`no_autobump_defined?`/`on_*_blocks_exist?`/`depends_on_set_in_block?`/`caveats_object`), `caskroom_path`/`staged_path`/`os_version`/`languages`/`language_groups`/`default_language_group`/`livecheck_defined?` |
| Cask caveats | `caveats --cask` and opt-in Ruby `caveats do ... end` block capture: captured block/literal, token/version/appdir/paths, platform blocks |
| Keg / Tab / Formulary | `Keg.new(path)` with name/version/rack, path helpers, `installed?`, `linked?`/`optlinked?`, `link`/`link_dir`/`unlink` (opt link + file symlinks), `opt_record`, keepme refs, oldname opt records, record removal, `formula`, `tab`, `Keg.for`/`Keg.all`/`Keg.from_rack`, version scheme/aliases/runtime deps, installation introspection, `uninstall`; `Tab.for_keg`/`for_name`/`for_formula`/`from_file`/`from_file_content`, receipt JSON fields/flags (`used_options`/`built_as_bottle?`/request flags), `tap`/`write`, `to_hash`/`to_json`; `Formulary.factory`/`resolve`/`load_formula`/`load_formula_from_path`/`load_formula_from_struct!`/`class_s`/`from_rack`/`keg_only?`/cache no-ops, placeholders/namespace helpers (tolerant of Homebrew kwargs) |
| Caveats DSL | `pybrew caveats` and `Formula#caveats`: heredocs/strings, platform/arch blocks, paths, `ohai`/`opoo`/`puts`, ENV/Utils |
| post_install | `post_install do ... end` after install, with paths/Formula/system/Utils/FileUtils |

## Not implemented / partial

- Complete `Formula` class: specs, dependencies, requirements, build options,
  bottle metadata, version schemes, aliases and formula loading internals.
- Patch application supports `patch do ... end`/`patch :DATA`/relative `file`
  with `patch -pN`; some exotic forms remain unsupported. Resource staging is
  limited to archives with `url`+`sha256` and literal `resource("name")` calls
  (dynamic resource iteration stages all).
- Homebrew internals: full `FormulaInstaller`, compiler selection, actual
  sandbox execution and GitHub/API/analytics internals. `Superenv`/`Sandbox`
  are compatibility stubs, not build engines.
- Full `MacOS`/SDK/Xcode/CLT detection, `DevelopmentTools`, `SystemConfig`
  host details.
- Services: Ruby evaluator mirrors pybrew's literal path helpers rather than
  Homebrew's global-prefix helpers; unknown directives are ignored.
- Cask: evaluator `sha256` map records only `arm:`/`intel:` (not
  `arm64_linux:`/`x86_64_linux:`); evaluator does not capture script bodies;
  livecheck `strategy do ... end`/url kwargs are partial; deprecation/disable
  dates/replacements and `homepage_browsed` are not captured; script shim has
  no sudo, `input:`/`timeout:`, `Utils.popen_write`, or caskroom/language/arch
  delegators.
- Formula test DSL: dependency test bodies cannot be loaded, arbitrary/
  uninstalled/aliased/tap-qualified Formula lookup is unavailable, version is
  a String, revision is always 0, HOMEBREW constants and OS/Hardware/MacOS
  modules are missing, and some Minitest surface (`refute_operator`,
  `assert_send`, `skip`, pattern assertions) remains.
- Any Ruby gem or third-party API used by formulae.

Unknown methods fail loudly through `method_missing`/Ruby errors instead of
silently succeeding. Extending the shim is additive and must keep the default
non-Ruby parser path unchanged.
