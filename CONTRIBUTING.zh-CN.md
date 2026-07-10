# 贡献 Agent Deck

**[English](CONTRIBUTING.md)**

感谢你愿意改进 Agent Deck。本项目面向本机 AI Agent 硬件控制台：当前重点是 **macOS + MiraBox N4 Pro + Codex**，但核心分层必须为未来的其他硬件和 Agent 保留空间。

本指南说明如何提出 issue、运行开发环境、验证改动，以及真实硬件相关的不可突破边界。

> **许可证状态**：当前仓库尚未包含根 `LICENSE`。本指南不建立任何外部贡献的法律授权或权利转让条款。维护者应在公开接受外部贡献或发布版本前选择并添加项目许可证；在此之前，请先与维护者确认贡献安排。

## 行为准则

- 讨论聚焦于可复现的技术事实，尊重不同的设备、系统和工作流。
- 不在 issue、PR、日志或测试夹具中提交 API key、token、完整 prompt、私有应用路径或其他个人数据。
- 安全问题不要用包含可利用细节的公开 issue 披露；在项目发布正式安全联系渠道前，请先联系维护者。

## 报告问题

提交问题前，请尽可能给出：

- macOS 版本、Python 版本、`uv --version` 和 Agent Deck commit/版本。
- 设备型号、固件信息、是否运行官方 MiraBox/StreamDock 应用。
- 启动方式（tmux、`run.sh` 或直接运行 daemon）。
- 脱敏后的 `uv run agent-deckctl doctor --json` 输出和相关日志。
- 预期行为、实际行为和可复现步骤。

不要把 `doctor` 输出中可能包含的序列号、路径或账户信息原样公开；先做必要脱敏。

## 开发环境

### 依赖

- macOS 和 Python 3.11+ 是当前已验证的开发组合。
- 使用 [uv](https://docs.astral.sh/uv/) 管理 Python 环境和依赖。
- 运行推荐 daemon 脚本需要 `tmux` 与 `lsof`。
- 开发用量趋势时需要 [Bun](https://bun.sh/docs/installation)，以便运行 `bunx ccusage`。
- 真实 N4 Pro 适配需要设备；无硬件时始终可以使用 fake hardware。

### 建立环境

```bash
git clone https://github.com/breakstring/agent-deck.git
cd agent-deck
uv sync --all-groups
uv run pytest -q
```

开始改动前，先检查工作区，避免覆盖其他人未提交的修改：

```bash
git status --short --branch
```

建议从最新 `main` 创建边界清晰的分支，例如：

```bash
git switch -c feat/describe-the-change
```

如果开发需要隔离已有未提交改动，使用项目根目录的 `.worktrees/`，并确认它处于 `.gitignore` 中。

## 架构边界

新增能力必须遵循这条数据和交互边界：

```text
Agent ingress -> NormalizedEvent -> AgentStateStore -> DeckMode/LayoutPlan
    -> HardwareSurface -> InteractionIntent/ActionExecutor
```

核心模块当前位于：

| 模块 | 职责 |
| --- | --- |
| `src/agent_deck/core/events.py` | 统一事件模型、payload 脱敏、时间校验。 |
| `src/agent_deck/core/state.py` | 事件到 Agent 状态的内存归约。 |
| `src/agent_deck/core/decisions.py` | 审批决策、超时和默认 deny。 |
| `src/agent_deck/core/modes.py` | 逻辑 deck mode 与选择状态。 |
| `src/agent_deck/rendering/layout.py` | 从状态和决策生成硬件无关布局。 |
| `src/agent_deck/hardware/fake.py` | 无真实 I/O 的 fake hardware，用于测试与本地开发。 |
| `src/agent_deck/hardware/streamdock_probe.py` | 真实 StreamDock 设备的只读诊断探针。 |
| `src/agent_deck/server/app.py` | 本地 FastAPI daemon 与 Web 配置接口。 |

必须遵守：

- 不让 hardware driver 解析 Codex hook payload，也不让 Codex adapter 直接操作设备。
- 硬件输入先转换成 `InteractionIntent`，再由 action 层执行；不得直接执行 shell、AppleScript 或向 Agent 写入文本。
- 跨模块数据优先使用明确、不可变或复制语义清晰的 Pydantic 模型。
- 所有时间必须是 timezone-aware `datetime`。
- payload、日志和错误信息必须脱敏，不能泄露 `token`、`secret`、`authorization`、`api_key`、`password` 等敏感值。
- 高风险动作默认关闭或 fail-closed，尤其是审批、文本输入和未知前台窗口注入。

## 真实硬件规则

真实 N4 Pro 是显式手动 smoke 环境，**绝不能**成为自动化测试前提。

- 保留并维护 fake adapter；测试不得访问真实 HID 设备。
- 官方 MiraBox/StreamDock 应用可能占用设备。接管前使用 `agent-deckctl doctor` 提示诊断线索，或先退出官方应用。
- `streamdock_probe.py` 只能做短暂的 open/read/close，并且不能调用 SDK 的 `device.init()`。`init()` 可能唤醒屏幕、改变亮度、清空图像或刷新设备。
- macOS 的 SDK 加载问题优先通过 `AGENT_DECK_STREAMDOCK_SDK_PATH` 指向官方 `Python-SDK` 或 `src` 解决，而不是修改诊断流程为初始化设备。
- 新增硬件型号时，先建模能力，再做特定 layout；不要把 N4 Pro 的按键/旋钮数量或坐标写进通用逻辑。

详细手动接管流程见[使用指南](docs/guides/using-agent-deck.zh-CN.md#真实硬件运行)。

## 编码与文档约定

- 保持改动聚焦，不进行与当前问题无关的重构、格式化或依赖升级。
- 新增公共模型、函数、类和协议应有说明语义、约束、返回值、错误处理和副作用的中文 docstring。
- 代码文件中的复杂 I/O、缓存、并发或设备状态转换应有简洁注释，解释约束而不是逐行复述代码。
- 更新公共行为、硬件能力模型、DeckMode、hook 安装方式或安全默认值时，同时更新对应 README、使用指南、路线图或设计文档。
- 提交信息使用 `<type>(scope): <summary>`，例如 `fix(n4pro): 修复旋钮灯光预览`；`summary` 使用中文、动词开头、不加句号。

## 验证要求

按改动范围运行最小充分验证。通常至少包括：

```bash
uv run pytest -q
git diff --check
```

按涉及范围补充：

| 改动类型 | 建议验证 |
| --- | --- |
| CLI 或配置 | `uv run agent-deckctl version`、相关子命令 `--help`、配置解析测试。 |
| Web UI | 启动 daemon 后在 `http://127.0.0.1:8765/` 手动检查关键流程；修改 JavaScript 时运行 `node --check src/agent_deck/web/app.js`。 |
| 渲染或缓存 | 运行相关 pytest，并检查生成帧/预览的尺寸、状态和缓存失效边界。 |
| Codex 集成 | 先运行只读 `uv run agent-deckctl codex-detect --enable-integration`；安装器先 dry-run，只有明确需要时才使用 `--apply`。 |
| 真实硬件 | 在自动化测试后额外做显式手动 smoke，记录设备、固件和启动方式；不可把 HID 接入 pytest。 |

如果无法运行某项验证，请在 PR 中说明原因、未覆盖的风险和可由维护者复现的步骤。

## 提交 Pull Request

PR 请保持可审查：

1. 一个 PR 解决一个清晰问题，说明用户可见行为和不在范围内的内容。
2. 描述改动触及的架构层、数据/缓存或硬件副作用。
3. 列出已经执行的验证命令及结果；如涉及真实硬件，单独标出手动验证。
4. 如修改 UI，请附截图或简短录屏；如修改协议/配置，请附最小示例。
5. 不包含无关文件、生成缓存、个人配置、密钥或设备序列号。

维护者会优先检查安全边界、硬件副作用、兼容性、测试覆盖和文档是否同步。
