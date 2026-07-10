# 使用 Agent Deck

**[English](using-agent-deck.en.md)**

本指南面向希望在自己的 Mac 上运行 Agent Deck 的用户。它描述当前已验证的路径：**macOS + MiraBox N4 Pro + Codex**。未连接 N4 Pro 时，也可以用 fake hardware 模式使用本地配置 UI 和 daemon。

## 1. 安装前准备

### 必需依赖

| 项目 | 用途 | 检查命令 |
| --- | --- | --- |
| macOS | 当前已验证的运行平台 | `sw_vers` |
| Python 3.11+ | Agent Deck 运行时 | `python3 --version` |
| [uv](https://docs.astral.sh/uv/) | Python 环境与依赖管理 | `uv --version` |
| Git | 获取源代码 | `git --version` |

### 按需依赖

| 项目 | 何时需要 | 检查命令 |
| --- | --- | --- |
| `tmux` 与 `lsof` | 使用推荐的常驻启动脚本 | `tmux -V`、`lsof -v` |
| [Bun](https://bun.sh/docs/installation) | 显示 Token/金额历史趋势；项目通过 `bunx ccusage` 读取数据 | `bunx --version` |
| MiraBox N4 Pro | 在真实硬件上显示和响应按键/旋钮 | `uv run agent-deckctl doctor` |
| 官方 StreamDock Python SDK | macOS 上内置 SDK 无法加载时的兼容路径 | 见[真实硬件运行](#真实硬件运行) |
| Codex | 显示 Codex 状态、quota，或安装可选 hook 集成 | `uv run agent-deckctl codex-detect --enable-integration` |

`ccusage` 只影响 Token/金额统计。没有 Bun 时，配置页、应用/网址按键、quota 和硬件主路径仍可使用；daemon 状态中会记录 Token 轮询失败原因。

## 2. 获取代码并安装依赖

```bash
git clone https://github.com/breakstring/agent-deck.git
cd agent-deck
uv sync --all-groups
```

确认 CLI 可运行：

```bash
uv run agent-deckctl version
uv run agent-deckctl doctor
```

`doctor` 只读取本机环境和硬件接管线索。它不会初始化、清空或刷新真实设备，因此应优先用于第一轮排查。

## 3. 第一次启动：fake hardware 模式

首次建议不要接管真实硬件，先确认本地服务和配置页可用：

```bash
scripts/agent-deckd-tmux.sh start --disable-hardware-renderer
scripts/agent-deckd-tmux.sh status
```

在浏览器中打开 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)。你可以在此修改按键、旋钮和灯光设置；“保存并应用”才会下发配置，编辑中的改动只更新预览。

停止和查看日志：

```bash
scripts/agent-deckd-tmux.sh logs
scripts/agent-deckd-tmux.sh stop
```

### tmux 启动脚本

推荐使用 `scripts/agent-deckd-tmux.sh` 管理常驻服务：

```bash
scripts/agent-deckd-tmux.sh start
scripts/agent-deckd-tmux.sh restart
scripts/agent-deckd-tmux.sh status
scripts/agent-deckd-tmux.sh logs
scripts/agent-deckd-tmux.sh attach
scripts/agent-deckd-tmux.sh stop
```

默认 tmux session 名称为 `agent-deckd`，监听地址为 `127.0.0.1:8765`。可在启动前设置：

```bash
export AGENT_DECK_TMUX_SESSION=agent-deckd-dev
export AGENT_DECK_HOST=127.0.0.1
export AGENT_DECK_PORT=8765
```

不使用 tmux 时，可用根目录的 `./run.sh start`，或以前台方式运行：

```bash
uv run agent-deckd --host 127.0.0.1 --port 8765
```

## 4. 真实硬件运行

### 4.1 接管前检查

当前正式支持的真实设备为 MiraBox N4 Pro。开始前：

1. 退出官方 MiraBox/StreamDock 应用，避免它占用设备。
2. 确保没有另一个 Agent Deck daemon 正在接管同一台设备。
3. 运行只读诊断：

   ```bash
   uv run agent-deckctl doctor --json
   uv run agent-deckctl hardware n4pro status
   ```

`doctor` 的探针会短暂 open/read/close 设备，但刻意不会调用 SDK 的 `init()`。不要把 `init()` 加到诊断脚本里；真实 SDK 的初始化可能唤醒屏幕、改变亮度、清空按键图像或刷新设备。

### 4.2 macOS SDK 兼容路径

某些 macOS 环境下，Python 包携带的 StreamDock 动态库可能不适配本机。此时下载或检出 MiraBox 官方 Device SDK，并将变量指向其 `Python-SDK` 目录或其 `src` 子目录：

```bash
export AGENT_DECK_STREAMDOCK_SDK_PATH="/absolute/path/to/StreamDock-Device-SDK/Python-SDK"
uv run agent-deckctl doctor
```

该变量只告诉 Agent Deck 从哪里导入官方 Python SDK，不会替你安装或初始化设备。确认诊断正常后，再启动真实 renderer：

```bash
scripts/agent-deckd-tmux.sh start
```

若已存在 tmux session，请用 `restart` 使新的环境变量生效：

```bash
scripts/agent-deckd-tmux.sh restart
```

### 4.3 N4 Pro 上的当前行为

- 10 个主按键可以配置为打开或切换应用、打开网址、显示 Agent 状态、订阅额度或用量状态，也可以暂不设定。
- 应用与网址图标会缓存到本机，供配置页预览和硬件下发共用。
- 4 个旋钮可分别配置旋转动作；音量类动作按下时隐含切换静音/麦克静音，不单独配置按下行为。
- 旋钮灯光是独立设置：关闭，或指定基础色并可开启呼吸效果。设备支持时，音量/亮度类动作会基于基础色反映状态；静音状态使用红色或熄灭。
- 底部虚拟面板会轮换品牌图、quota 和 usage；usage 可在 Day、Week、Month、All 之间切换。

如果 daemon 退出后需要有意覆盖设备上残留的画面，可使用以下写入操作显示品牌启动图：

```bash
uv run agent-deckctl hardware n4pro splash
```

这不是只读诊断命令。执行前请确认它不会覆盖另一个正在控制设备的 daemon。

## 5. 配置位置与持久化

默认配置查找顺序为：

1. `agent-deckd --config /path/to/config.toml` 显式指定的路径。
2. `AGENT_DECK_CONFIG` 环境变量。
3. 当前工作目录的 `agent-deck.toml`。
4. `~/Library/Application Support/AgentDeck/config.toml`。

仓库中的 [`agent-deck.toml`](../../agent-deck.toml) 是可直接阅读的默认示例。当前默认 `codex.permission_request.mode = "passthrough"`，即审批仍由 Codex 原生界面处理。

用户在 Web 配置页保存的硬件布局位于：

- 按键布局：`~/Library/Application Support/AgentDeck/n4pro-key-layout.json`
- 旋钮与灯光布局：`~/Library/Application Support/AgentDeck/n4pro-rotary-layout.json`

测试或多配置场景可用下列环境变量隔离路径：

```bash
export AGENT_DECK_N4PRO_KEY_LAYOUT="/path/to/key-layout.json"
export AGENT_DECK_N4PRO_ROTARY_LAYOUT="/path/to/rotary-layout.json"
```

## 6. Codex 集成

先获取只读检测报告和手动配置提示：

```bash
uv run agent-deckctl codex-detect --enable-integration
```

安装器默认只 dry-run，不写用户配置：

```bash
uv run agent-deckctl codex-install
```

确认输出后，才执行写入操作：

```bash
uv run agent-deckctl codex-install --apply
```

安装器会在修改前创建备份。请先阅读它展示的目标文件与 hook 内容。Agent Deck 的 `notify` hook 是 best-effort，不应影响 Codex 正常工作；`permission-request` 在 daemon 不可用、超时或响应无效时会 fail-closed。除非你清楚审批流的安全影响，否则保持 `passthrough` 默认值。

## 7. Token、金额与 quota 数据

- daemon 启动时会主动刷新 quota 和 Token/金额缓存，之后按本地刷新策略更新。
- Token/金额趋势使用 `bunx ccusage`。请先检查 `bunx --version`；缺失 Bun 或 `ccusage` 数据不足时，趋势可能为空或只有最新点。
- quota 数据来自 Codex app-server 相关路径。网络、登录状态或 Codex 版本变化都可能造成暂时不可用；使用 `uv run agent-deckctl status` 检查 daemon 的数据状态。
- 面板和按键图片使用同一套缓存。旋钮/按键切换优先下发现有渲染帧，后台再刷新过期快照，避免用户交互被网络或统计计算阻塞。

## 8. 常见问题

### 配置页可访问，但 N4 Pro 没有更新

先确认 daemon 是否以真实 renderer 启动：

```bash
scripts/agent-deckd-tmux.sh status
uv run agent-deckctl status
```

然后退出官方 MiraBox/StreamDock 应用，确认不存在第二个 daemon，并重新执行 `uv run agent-deckctl doctor`。`--disable-hardware-renderer` 模式故意不接管设备。

### `streamdock` 在 macOS 上加载失败

设置 `AGENT_DECK_STREAMDOCK_SDK_PATH` 到官方 SDK 的 `Python-SDK` 或 `src`，然后通过 `doctor` 验证。不要为了绕过加载问题在探针里调用 `device.init()`。

### Token/金额面板没有趋势线

运行：

```bash
bunx --version
uv run agent-deckctl status
```

确认 Bun 可用，并等待足够的 `ccusage` 历史数据。没有历史数据时，单个最新点是预期降级行为。

### 端口被占用或 tmux 启动失败

检查服务和端口：

```bash
scripts/agent-deckd-tmux.sh status
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

停止已知的 Agent Deck session 后再启动，或设置不同的 `AGENT_DECK_PORT`。

## 9. 获取帮助与报告问题

报告问题时请提供：macOS 版本、Python/uv 版本、设备型号、是否连接官方软件、启动方式、`agent-deckctl doctor` 的脱敏输出以及相关 daemon 日志。不要在 issue 或日志中粘贴 API key、token、完整 prompt 或私有应用路径。

需要修改代码或补充硬件适配时，请阅读[贡献指南](../../CONTRIBUTING.zh-CN.md)。
