# homebrew-py / pybrew

用 Python 重新实现 Homebrew 核心功能的工程原型。目标不是简单包一层 Ruby，而是把
Formula 解析、依赖解析、Cellar、bottle 安装、链接/卸载和 receipt 全部用 Python
重新实现，并用真实 homebrew-core 公式和真实 Homebrew bottle 做回归验证。

> **状态：v0.1 原型（可运行、可测试）**
>
> - 45 个测试全部通过（`pytest -q`）
> - 15 个真实 homebrew-core 公式（bash、git、curl、openssl@3、python@3.12 等）与
>   `brew info --json=v2` 的元数据逐项对比通过
> - 使用本机缓存的真实 `libslirp 4.9.6` Homebrew bottle 完成安装验证
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
| Formula DSL 解析 | ✅ | 只解析字面量 DSL，不执行 Ruby；支持 `url/sha256/version`、`license any_of/all_of/with`、`bottle` 块、`depends_on` 标签、`head`/`stable`/`resource`、`on_macos`/`on_linux`/`on_intel`、`def install/test/caveats` 原文捕获 |
| Tap 发现 | ✅ | `Formula/**/*.rb`、多个 tap、tap-qualified 名称 |
| 依赖解析 | ✅ | 递归依赖、拓扑排序、环检测、build/test/optional/recommended 分类、`uses_from_macos`（Linux 安装）|
| Bottle 安装 | ✅ | 标签选择与降级、ghcr.io OCI 匿名拉取、本地缓存/SHA 校验、安全解包、`@@HOMEBREW_*@@` 重定位、receipt |
| Cellar / 链接 | ✅ | `Cellar/<name>/<version>`、`opt` 链接、bin/lib/share/... 相对符号链接、冲突检测、`--overwrite`、卸载清理 |
| Source 安装 | ⚠️ 部分 | 仅支持“已构建二进制”公式的声明式 `bin.install`/`prefix.install` 等；`system "make install"` 会明确报 `UnsupportedInstall`，不会静默损坏环境 |
| Cask | ❌ | 未实现 |
| 完整构建系统（autotools/cmake/meson 等） | ❌ | 路线图 |
| Services / analytics / tap 管理 | ❌ | 路线图 |

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
# 45 passed
```

测试覆盖：

- `test_version.py`：版本推断/排序（含 `v` 前缀、sqlite autoconf、日期版本）
- `test_parser.py`：bottle、依赖标签、license 组合、heredoc、head 块、未知 DSL
- `test_real_formulas.py`：15 个真实 homebrew-core 公式与 `brew info --json=v2` 对比
- `test_bottles.py`：bottle 安装/卸载、重定位、冲突、校验失败、keg-only、依赖顺序、
  staged source 安装、恶意 tar 拒绝
- `test_cli.py`：info/search/list/deps/doctor/config/parse
- `test_real_bottle.py`：真实 `libslirp 4.9.6` bottle 离线安装（有缓存时运行）

详细结果见 [docs/verification.md](docs/verification.md)，与 Ruby Homebrew 的差异见
[docs/parity.md](docs/parity.md)，设计细节见 [docs/architecture.md](docs/architecture.md)。

## 跟踪上游

- 仓库内置 `.github/workflows/track-upstream.yml`，每周一 09:00（Asia/Shanghai）检查
  `Homebrew/brew` 与 `Homebrew/homebrew-core` 的最新 release/commit，变化时自动开 issue。
- `scripts/track_upstream.py` 可在本地手动运行：

```bash
python scripts/track_upstream.py --state .upstream-state.json --report .upstream-report.md
```

- 本地 DSH 会话还设置了每周提醒，用于跟进上游变更并更新公式语料/解析器。

## 上游 PR

已向上游提交一个 **draft RFC PR**（不是要求合并的代码 PR），说明 Python 原型、验证
结果、差异和需要维护者反馈的问题：

- PR：<https://github.com/Homebrew/brew/pull/24362> （draft RFC，可能被上游关闭）
- 提案正文：[docs/python-port-rfc.md](docs/python-port-rfc.md)

Homebrew 维护者是否会接受这种方向，最终由上游决定；该 PR 明确标注为 RFC，可随时关闭。

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

1. 补全 source build：autotools / cmake / meson / cargo 等构建系统与 `system` DSL。
2. 增加 cask 支持（下载、app bundle、pkg、zap）。
3. Tap 管理命令（`tap`/`untap`）和 `brew bundle` 兼容。
4. 更完整的 bottle 重定位（Mach-O `install_name_tool`、Linux RPATH）。
5. 与 `brew` 的测试套件对齐，逐步扩大真实公式覆盖。

## License

BSD-2-Clause。真实公式语料来自 Homebrew/homebrew-core（BSD-2-Clause），见
[NOTICE](NOTICE)。
