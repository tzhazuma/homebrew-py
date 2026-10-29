# homebrew-py / pybrew

[![CI](https://github.com/tzhazuma/homebrew-py/actions/workflows/ci.yml/badge.svg)](https://github.com/tzhazuma/homebrew-py/actions/workflows/ci.yml)

用 Python 重新实现 Homebrew 核心功能的工程原型。目标不是简单包一层 Ruby，而是把
Formula 解析、依赖解析、Cellar、bottle 安装、链接/卸载和 receipt 全部用 Python
重新实现，并用真实 homebrew-core 公式和真实 Homebrew bottle 做回归验证。

> **状态：v0.1 原型（可运行、可测试）**
>
> - 219 个测试通过（另有 5 个 opt-in 测试跳过；`pytest -q`）
> - 15 个真实 homebrew-core 公式（bash、git、curl、openssl@3、python@3.12 等）与
>   `brew info --json=v2` 的元数据逐项对比通过
> - 使用真实 `hello 2.12.3` Formula 完成 autotools 源码构建并运行成功
> - 使用本机缓存的真实 `libslirp 4.9.6` Homebrew bottle 完成安装验证；也完成了 live ghcr.io OCI 下载验证
> - 支持 tap 管理、cask（zip/dmg 的 app/binary/suite）和 macOS launchd services
> - 支持 ghcr.io OCI bottle 下载（匿名 token）、SHA-256 校验、路径重定位、receipt、
>   opt 链接和回滚

## 快速开始

```bash
git clone https://github.com/tzhazuma/homebrew-py
cd homebrew-py
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

默认 prefix 是 `~/.pybrew`（刻意不碰真实 Homebrew 的 `/opt/homebrew` 或
`/usr/local`）。测试和演示都使用临时 prefix；如果要操作一个 Homebrew 形状的目录：

```bash
# 查看公式信息（从本地 tap 读取 Ruby 公式）
pybrew --prefix /tmp/pybrew --tap /path/to/homebrew-core info hello
pybrew --prefix /tmp/pybrew --tap /path/to/homebrew-core info --json hello

# 安装 / 卸载（bottle 优先）
pybrew --prefix /tmp/pybrew --tap /path/to/tap install hello
pybrew --prefix /tmp/pybrew list --versions
pybrew --prefix /tmp/pybrew uninstall hello

# 使用本地已有的 Homebrew bottle（离线）
pybrew --prefix /tmp/pybrew --tap /path/to/tap install --bottle-file ~/Library/Caches/Homebrew/downloads/xxx.bottle.tar.gz hello

# 使用 Homebrew JSON API（没有 homebrew-core 本地 checkout 时）
pybrew --prefix /tmp/pybrew info --api hello
pybrew --prefix /tmp/pybrew install --api hello

# Tap 管理
pybrew --prefix /tmp/pybrew tap homebrew/cask
pybrew --prefix /tmp/pybrew tap --list
pybrew --prefix /tmp/pybrew untap homebrew/cask

# Cask（zip / dmg；默认安装到 <prefix>/Applications）
pybrew --prefix /tmp/pybrew tap homebrew/cask
pybrew --prefix /tmp/pybrew cask install rectangle
pybrew --prefix /tmp/pybrew cask install --api firefox   # 无需 tap homebrew-cask
pybrew --prefix /tmp/pybrew cask list
pybrew --prefix /tmp/pybrew cask uninstall --zap rectangle

# Services（macOS launchd）
pybrew --prefix /tmp/pybrew services list
pybrew --prefix /tmp/pybrew services start nginx
pybrew --prefix /tmp/pybrew services stop nginx

# 维护
pybrew --prefix /tmp/pybrew outdated
pybrew --prefix /tmp/pybrew upgrade --dry-run
pybrew --prefix /tmp/pybrew cleanup --dry-run
pybrew --prefix /tmp/pybrew reinstall hello

# 源码构建（autotools / CMake / Cargo 等常用 def install DSL）
pybrew --prefix /tmp/pybrew --tap /path/to/tap install --build-from-source hello
```

也可以作为库调用：

```python
from pybrew.paths import Config
from pybrew.taps import FormulaLoader, discover_taps
from pybrew.installer import Installer, InstallOptions

config = Config.default(prefix="/tmp/pybrew", taps=["/path/to/tap"])
config.ensure()
loader = FormulaLoader(discover_taps(config.taps))
formula = loader.load("hello")
Installer(config, loader).install(formula, InstallOptions())
```

## 已实现功能

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| Formula DSL 解析 | ✅ | 只解析字面量 DSL，不执行 Ruby；支持 `url/sha256/version`、`license any_of/all_of/with`、`bottle` 块、`depends_on` 标签、`head`/`stable`/`resource`、`on_macos`/`on_linux`/`on_arm`/`on_intel`、cask `preflight`/`postflight` 原文捕获、`def install/test/caveats` 原文捕获 |
| Tap 发现 | ✅ | `Formula/**/*.rb`、多个 tap、tap-qualified 名称 |
| 依赖解析 | ✅ | 递归依赖、拓扑排序、环检测、build/test/optional/recommended 分类、`uses_from_macos`（Linux 安装）|
| Bottle 安装 | ✅ | 标签选择与降级、ghcr.io OCI 匿名拉取、本地缓存/SHA 校验、安全解包、`@@HOMEBREW_*@@` 重定位（Mach-O `install_name_tool`、ELF `patchelf` RPATH）、receipt |
| Cellar / 链接 | ✅ | `Cellar/<name>/<version>`、`opt` 链接、bin/lib/share/... 相对符号链接、冲突检测、`--overwrite`、卸载清理 |
| Source 安装 | ✅ 常用子集 | `system`（含 `std_configure_args`/`std_cmake_args`/`std_meson_args`/`std_cargo_args`）、`inreplace`、`ENV`、`bin.install`/`prefix.install`、`resource(...).stage`、`--HEAD` 源码安装、`on_macos`/`on_linux`/`on_arm`/`on_intel`、`Hardware::CPU`/`build.with?`/`args.include?` 条件、`ENV["X"]` 读取；新增 opt-in 任意 Ruby 桥 `PYBREW_ALLOW_ARBITRARY_RUBY=1` / `install --allow-arbitrary-ruby`（Ruby shim，含 system/inreplace/Pathname/FileUtils/Utils/MacOS/OS/Shellwords 等）；autotools/cmake/cargo 有真实构建测试，meson 也已完成真实项目手工构建验证 |
| Tap 管理 | ✅ | `tap`/`untap`/`tap --list`/`tap --update`/`update`，支持 GitHub 与本地 git URL，自动发现 Formula/Casks |
| Cask | ✅ zip/dmg/pkg 子集 | app/binary/suite 安装、receipt、uninstall、`--zap`；`pkg` 可展开 payload 到 `--pkg-root`；支持 `--api`、`outdated`/`upgrade`/`reinstall`（含 `--cask` 别名）、`search --cask`、`uninstall quit/delete/pkgutil`；新增 font/colorpicker/prefpane/qlplugin/mdimporter/screen_saver/service/audio_unit/vst/vst3/dictionary/input_method/keyboard_layout 等 Library artifact、`stage_only`、`--library-root`；支持 opt-in `--system-pkg`（root + macOS installer）、系统 pkg 卸载与升级回滚；新增 opt-in `--run-scripts` 执行 cask preflight/postflight（Ruby shim，含 Utils/MacOS/OS 等）；升级会清理被移除的旧 artifact |
| Services | ✅ launchd + systemd | macOS 生成 launchd plist；Linux 生成 systemd user unit；start/stop/restart/list/info |
| Cask/Services/Tap 命令 | ✅ | 集成到 `pybrew` CLI |
| 下载/源码查看 | ✅ | `fetch` formula bottle/source 或 `fetch --cask` artifact、`cat`/`home`/`edit` formula 与 cask |
| 维护命令 | ✅ | `leaves`、`uses`、`autoremove`、`outdated`、`upgrade`（安装新版本并清理旧 keg，含 `--dry-run`）、`cleanup`（含 `--prune-cache`）、`reinstall`、`doctor --json`、`pin`/`unpin`、`services cleanup`、`tap --update`、`shellenv`；install `--no-link` |
| Brewfile | ✅ 基础 | `bundle install/check/list/dump`，支持 `tap`/`brew`/`cask`/`mas`/`vscode` 行，tap `url:`、brew `link:false`/`args:`/`restart_service:true` |
| Analytics | ✅ 本地 | 默认关闭；`analytics on/off/state/log/clear`，事件仅存本机，不联网；install/uninstall/upgrade 会记录本地事件 |
| 完整 Homebrew Ruby/测试 DSL / 部分高级 bundle 选项 / x86_64 Linux 真机验证 | ❌ | 路线图 |

## 架构

```text
                    ┌────────────────────────────┐
  CLI (cli.py) ───▶ │ FormulaLoader / Tap         │
                    │  └─ ruby_parser.py          │
                    │      Ruby subset → Formula  │
                    └────────────┬───────────────┘
                                 │
                    ┌────────────▼───────────────┐
                    │ Installer (installer.py)    │
                    │  Resolver: deps + topo sort │
                    │  BottleInstaller            │
                    │  SourceInstaller (subset)   │
                    └───────┬──────────┬──────────┘
                            │          │
              ┌─────────────▼──┐  ┌────▼────────────┐
              │ download.py    │  │ cellar.py        │
              │ cache + SHA    │  │ receipts + links │
              │ ghcr OCI auth  │  │ unlink / prune   │
              └────────────────┘  └─────────────────┘
```

关键设计：

1. **不执行 Ruby**：`ruby_parser.py` 是词法分析器 + 字面量解析器，只接受可以安全
   静态理解的数据。未知 DSL 调用会被记录到 `Formula.extra` 而不是报错，因此可以读完
   整个 homebrew-core 而不启动 Ruby。
2. **安全第一**：默认 prefix 为 `~/.pybrew`；tar 解包拒绝绝对路径、`..`、越界符号
   链接；bottle 必须校验 SHA-256；链接冲突默认失败而不是覆盖。
3. **与 Homebrew 数据兼容**：Cellar 布局、`INSTALL_RECEIPT.json`、`opt` 链接和
   `brew info --json=v2` 的字段形状尽量保持一致，方便现有工具消费。
4. **可离线验证**：真实 bottle 测试直接使用 `~/Library/Caches/Homebrew/downloads`
   中已有的 bottle，不重复下载。

## 测试与验证

```bash
pytest -q
# 219 passed, 5 skipped
```

测试覆盖：

- `test_version.py`：版本推断/排序（含 `v` 前缀、sqlite autoconf、日期版本）
- `test_parser.py`：bottle、依赖标签、license 组合、heredoc、head 块、未知 DSL
- `test_real_formulas.py`：15 个真实 homebrew-core 公式与 `brew info --json=v2` 对比
- `test_bottles.py`：bottle 安装/卸载、重定位、冲突、校验失败、keg-only、依赖顺序、
  staged source 安装、恶意 tar 拒绝
- `test_cli.py`：info/search/list/deps/doctor/doctor --json/config/parse/shellenv、cask flags、fetch、cat/home/edit
- `test_real_bottle.py`：真实 `libslirp 4.9.6` bottle 离线安装（有缓存时运行）
- `test_source_build.py`：真实 autotools/CMake/Cargo 构建、inreplace、on_arm/on_intel、条件谓词、ENV、resource、HEAD
- `test_ruby_dsl.py`：opt-in 任意 Ruby install body、扩展 shim、失败上下文（无 Ruby 时跳过）
- `test_cask.py`：真实 cask（rectangle/iterm2/VSCode）解析、zip/dmg 安装/卸载/zap、pkg payload、`install --cask`、API 映射、outdated/upgrade/reinstall、uninstall quit/delete
- `test_cask_depth.py`：13 种 Library artifact、stage_only、target 安全校验、升级 artifact 清理
- `test_cask_pkg_system.py`：opt-in 系统 pkg 安装（fake installer、root 检查、receipt）
- `test_cask_pkg_uninstall.py`：系统 pkg ID 跟踪、文件删除、pkgutil forget、非 root/越界拒绝
- `test_cask_rollback.py`：system pkg upgrade 清理/失败回滚、共享路径保护
- `test_cask_scripts.py`：preflight/postflight 解析与 opt-in Ruby 执行（fake Ruby）
- `test_relocation.py`：Mach-O padding、ELF patchelf RPATH/PT_INTERP
- `test_analytics.py`：本地 analytics 状态、事件、CLI、无网络
- `test_linux_real.py`：OrbStack Linux arm64/x86_64 真机 bottle/patchelf（opt-in，默认跳过）
- `test_services.py`：launchd plist + systemd unit、伪造 launchctl/systemctl 的 start/stop/list、orphan cleanup
- `test_taps.py`：本地 git tap 的 clone/update/untap/update-all、`update` 命令
- `test_maintenance.py`：outdated、upgrade dry-run/real upgrade + old keg cleanup、leaves/uses、autoremove、cleanup（含 cache）、reinstall、pin/unpin
- `test_bundle.py`：Brewfile install/check/list/dump、tap URL、link:false、args、mas/vscode、restart_service

详细结果见 [docs/verification.md](docs/verification.md)，与 Ruby Homebrew 的差异见
[docs/parity.md](docs/parity.md)，设计细节见 [docs/architecture.md](docs/architecture.md)。

## 跟踪上游

- 仓库内置 `.github/workflows/track-upstream.yml`，每周一 09:00（Asia/Shanghai）检查
  `Homebrew/brew` 与 `Homebrew/homebrew-core` 的最新 release/commit，变化时自动开 issue。
- `scripts/track_upstream.py` 可在本地手动运行：

```bash
python scripts/track_upstream.py --state .upstream-state.json --report .upstream-report.md
```

- 本地 DSH 会话还设置了每周提醒（周一 09:00 Asia/Shanghai），用于跟进上游变更并更新
  公式语料/解析器。

验证记录：CI 矩阵 3/3 通过（Ubuntu Python 3.9、Ubuntu Python 3.12、macOS Python
3.12）；`Track Homebrew upstream` workflow 手动触发成功，并已自动创建
[issue #1](https://github.com/tzhazuma/homebrew-py/issues/1) 记录检测到的
homebrew-core 变更。

## 上游 PR

RFC PR 已提交，Homebrew maintainer 已给出最终范围决定：

- PR：<https://github.com/Homebrew/brew/pull/24362>
- 状态：**closed**（2026-10-28，MikeMcQuaid：*"Not interested in this, sorry."*；见 PR 页面）
- 提案正文：[docs/python-port-rfc.md](docs/python-port-rfc.md)
- PR 正文：[docs/upstream-pr/PR_BODY.md](docs/upstream-pr/PR_BODY.md)

PR 曾因缺少 Homebrew 的 `PULL_REQUEST_TEMPLATE.md` 被机器人关闭；补全模板、披露 AI
使用并获提交者明确确认后，机器人重新打开。随后 maintainer 明确表示不感兴趣并关闭了
PR。这是上游的范围决定，不是技术 review；不再尝试重开或绕过该决定。

根据 Homebrew 的 Contributing/Responsible AI 规则，提交者本人负责后续上游沟通；
本项目保留 RFC、验证记录和实现作为独立原型。

## 目录结构

```text
src/pybrew/           Python 实现（parser / installer / cellar / CLI / API）
tests/                单元 + 集成 + 真实数据测试
tests/fixtures/       真实 homebrew-core 公式语料与期望元数据
scripts/              上游跟踪脚本
docs/                 架构、兼容性、验证、RFC
.github/workflows/    CI 与上游定时跟踪
```

## 路线图

1. 扩展任意 Ruby shim 覆盖、更多构建系统。
2. Cask 深度：更多脚本 API 覆盖。
3. Brewfile 其余高级选项、Cask upgrade 更多边界。
4. 更多 Mach-O 边界。
5. 与 `brew` 的测试套件对齐，逐步扩大真实公式覆盖。
6. 跟进上游 RFC PR 的 maintainer review（已提交）。

## License

BSD-2-Clause。真实公式语料来自 Homebrew/homebrew-core（BSD-2-Clause），见
[NOTICE](NOTICE)。
