# Agent Deck

**[English](README.en.md)**

Agent Deck 是一个本机运行的 AI Agent 硬件控制台桥接项目。它把 Agent 的状态、用量与可配置操作映射到妙联宝设备，同时保留可在浏览器中使用的本地配置界面。

当前公开版本聚焦于 **macOS + MiraBox N4 Pro + Codex**。项目已经具备可实际使用的主路径，但仍处于 `0.x` 快速迭代阶段；其他操作系统、硬件型号和 Agent 平台尚未作出兼容性承诺。

## 你可以用它做什么

- 在 N4 Pro 的 10 个 LCD 按键上配置本地应用、网址、Agent 状态和订阅/用量视图。
- 在底部虚拟面板中轮换品牌图、订阅额度与 Token/金额统计；使用趋势由本地缓存预渲染，减少切换等待。
- 配置 4 个旋钮的旋转动作，例如切换面板或周期、调节系统输入/输出音量、显示器亮度和控制台屏幕亮度。
- 配置旋钮灯光颜色和可选呼吸效果，并在 Web 配置页实时预览。
- 读取 Codex 本地状态、quota 和 `ccusage` 数据；可选择安装 Codex hook 集成。默认审批模式保持 Codex 原生审批界面，不会把审批控制权交给硬件。
- 没有连接真实硬件时，以 fake hardware 模式运行配置 UI 和核心服务。

## 当前支持范围

| 维度 | 当前状态 |
| --- | --- |
| 操作系统 | macOS 为已验证目标。Windows/Linux 目前不属于正式支持范围。 |
| 真实硬件 | MiraBox N4 Pro。其他 StreamDock/MiraBox 型号保留架构扩展空间，但尚未作为可用目标发布。 |
| Agent | Codex 的本地 App/CLI 状态与 hook 集成。 |
| Python | Python 3.11 或更高版本。 |
| 用量趋势 | 可选依赖 Bun 的 `bunx` 与 `ccusage`；缺失时，其他功能仍可运行，但 Token/金额趋势不可用。 |

## 快速开始

完整步骤、硬件接管和排障请阅读 [使用指南](docs/guides/using-agent-deck.zh-CN.md)。下面是使用 fake hardware 启动本地配置页的最短路径：

```bash
git clone https://github.com/breakstring/agent-deck.git
cd agent-deck
uv sync --all-groups

# 检查本机环境与设备线索
uv run agent-deckctl doctor

# 以不接管真实设备的方式启动
scripts/agent-deckd-tmux.sh start --disable-hardware-renderer
```

然后打开 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)。停止服务：

```bash
scripts/agent-deckd-tmux.sh stop
```

如果希望接管 N4 Pro，请先退出官方 MiraBox/StreamDock 应用，并在启动前通过 `doctor` 确认设备线索。macOS 上 SDK 动态库不兼容时，需要将 `AGENT_DECK_STREAMDOCK_SDK_PATH` 指向官方 Python SDK；具体做法见[真实硬件运行](docs/guides/using-agent-deck.zh-CN.md#真实硬件运行)。

## 文档

- [使用指南（中文）](docs/guides/using-agent-deck.zh-CN.md)：安装依赖、启动、配置、硬件、Codex 集成与排障。
- [Usage guide (English)](docs/guides/using-agent-deck.en.md)
- [贡献指南（中文）](CONTRIBUTING.zh-CN.md)
- [Contributing guide (English)](CONTRIBUTING.md)
- [项目路线图](docs/references/agent-deck-roadmap.md)：后续方向和边界。

## CLI 主要命令

项目提供了三个主要的 CLI 入口（通过 `uv run` 执行）：
- **`agent-deckd`**：核心后台 Daemon 服务，负责驱动硬件显示并接收外部 Agent 遥测。
- **`agent-deckctl`**：运维管理工具，支持环境诊断 (`doctor`)、运行状态查看和事件模拟。
- **`agent-deck-codex-hook`**：Codex 专属集成钩子，负责同步 turn 状态并安全反馈硬件端做出的审批决策。

## 运行管理

你可以使用项目内置的脚本来管理常驻后台进程：

- **Tmux 托管模式**（推荐，便于查看日志和进程 attach）：
  ```bash
  # 启动/停止/查看状态/查看日志/附加到会话
  scripts/agent-deckd-tmux.sh [start|stop|status|logs|attach|restart]
  ```

- **普通后台模式**（在没有安装 tmux 的环境）：
  ```bash
  # 使用内置的运行脚本
  ./run.sh [start|stop|status|logs|restart]
  ```

- **前台直接运行**（常用于调试）：
  ```bash
  uv run agent-deckd --host 127.0.0.1 --port 8765
  ```

## 智能体集成 (Hooks 配置)

项目通过命令行工具支持对 Codex 的一键集成配置：
1. **环境检测**：运行 `uv run agent-deckctl codex-detect --enable-integration` 获取配置建议。
2. **预览配置 (Dry-run)**：运行 `uv run agent-deckctl codex-install` 预览待写入的 hooks 设置。
3. **应用集成**：运行 `uv run agent-deckctl codex-install --apply` 自动完成本地 Codex 系统的 hooks 配置。

更多高级配置及硬件排障，请参考 [使用指南](docs/guides/using-agent-deck.zh-CN.md)。


## 授权协议

本项目的核心代码采用 **[MIT 许可证](LICENSE)** 进行授权。你可以自由地使用、修改和分发该软件。

### 第三方组件及授权

本项目的 `vendor/` 目录下包含以下第三方组件：
- **streamdock-python-sdk**：用于与妙联宝/StreamDock 控制台设备进行通信的 Python SDK。
  - **原始仓库**: [MiraboxSpace/StreamDock-Plugin-SDK](https://github.com/MiraboxSpace/StreamDock-Plugin-SDK)
  - **授权协议**: [MIT 许可证](vendor/streamdock-python-sdk/LICENSE)


## 开发与验证

```bash
uv run pytest -q
uv run agent-deckctl version
git diff --check
```

真实设备验证属于显式手动 smoke，不会纳入自动化测试。详见[贡献指南](CONTRIBUTING.zh-CN.md)。
