# Agent Deck

Agent Deck 是一个面向本机 AI Agents 的硬件控制台项目。目标是把妙联宝 N4 Pro 等设备用作 Agent 状态面板和安全交互入口。

当前项目处于设计与骨架初始化阶段。主要文档：

- `docs/superpowers/specs/2026-06-12-agent-deck-analysis.md`
- `docs/superpowers/specs/2026-06-12-agent-deck-mvp-design.md`
- `docs/references/agent-deck-roadmap.md`
- `docs/references/stream-dock-scenes-research-2026-06-12.md`

Python 项目使用 `uv` 管理。

## 本地启动

开发和协作调试时，推荐使用 tmux 启动脚本：

```bash
scripts/agent-deckd-tmux.sh start
scripts/agent-deckd-tmux.sh status
scripts/agent-deckd-tmux.sh logs
scripts/agent-deckd-tmux.sh restart
scripts/agent-deckd-tmux.sh attach
```

tmux 脚本会在 detached session `agent-deckd` 中启动 daemon，方便随时查看日志或 attach
到进程。它最终执行的仍然是仓库根目录下的 `agent-deckd --host 127.0.0.1 --port 8765`，
因此同样会读取当前目录的 `agent-deck.toml`，并按配置接管 N4 Pro 硬件渲染。

仓库根目录也提供了普通后台启动脚本：

```bash
./run.sh
```

`run.sh` 更适合作为普通本机后台入口：它不依赖 tmux，会把 PID 和日志写到固定位置，便于
用 `./run.sh status`、`./run.sh logs` 和 `./run.sh restart` 管理。

两种启动方式都会启动同一个 `agent-deckd`：监听 `127.0.0.1:8765`，读取 `agent-deck.toml`，
并默认执行真实硬件渲染。当前配置里的默认设备 profile 是 `n4pro`，所以 Codex 状态按钮动画
和底部 quota 背景会在同一次硬件写入里共存。

## N4 Pro 配置页

打开 <http://127.0.0.1:8765/> 可以配置主按键、4 个旋钮和控制台灯光。所有编辑先只改变网页中的
硬件预览；只有点击“保存并应用”后，daemon 才会共同保存主按键和旋钮布局，并在下一次统一 N4 Pro
renderer tick 中下发。

- 每个旋钮只配置“左右旋转”用途，可重复绑定或保持暂不设定；按下语义由用途隐式决定。
- 旋转动作包括 virtual panel/内容轮换、输出/输入音量、系统显示器亮度和控制台整体亮度；连续动作
  固定每格 `2%`。
- 输出音量旋钮按下会切换输出静音，输入音量旋钮按下会切换麦克风静音；其他旋钮按下不执行动作。系统音频操作会先读取当前状态。
- 手动 panel 顺序为 `Brand -> Quota -> Usage -> Brand`；Quota 内容切换 5h/Week，Usage 内容
  切换 Day/Week/Month/All；Brand 内容切换保持安静无操作。
- N4 Pro 当前的 4 个旋钮灯圈是一个 `rotary_ring_group`，不能分别设置颜色。配置页只提供关闭或
  基础色和可选柔和呼吸；呼吸使用同一 LED group 的亮度周期，最终平滑度仍需真机确认。

旋钮配置默认保存在：

```text
~/Library/Application Support/AgentDeck/n4pro-rotary-layout.json
```

可以用 `AGENT_DECK_N4PRO_ROTARY_LAYOUT` 覆盖这个路径，便于测试或隔离不同本机配置。

真实硬件 smoke 前，可先用下面命令启动纯 GUI/fake 模式，它不会接管 N4 Pro：

```bash
scripts/agent-deckd-tmux.sh restart --disable-hardware-renderer
```

恢复真实 N4 Pro renderer：

```bash
scripts/agent-deckd-tmux.sh restart
```

`run.sh` 的 PID 和日志默认写到：

```text
~/Library/Application Support/AgentDeck/agent-deckd.pid
~/Library/Logs/AgentDeck/agent-deckd.log
```

常用命令：

```bash
./run.sh status
./run.sh logs
./run.sh stop
./run.sh restart
./run.sh --foreground
```

需要临时覆盖 daemon 参数时，可以直接把通用参数追加到脚本后面，例如：

```bash
./run.sh --disable-hardware-renderer
```

如果要前台观察启动输出，可以使用：

```bash
./run.sh --foreground --disable-hardware-renderer
```

默认硬件渲染节奏是 `render_interval_seconds = 3.0`、`fps = 10`，对应当前 30 帧的
N4 Pro Codex 动画资产；这样每次下发按钮图标时能播放一个完整 working 动画周期，而不是
只反复看到前几帧。daemon 的渲染循环会把硬件播放耗时计入这个周期，正常情况下不会在
一次完整动画结束后再额外等待一个完整 interval。

`agent-deck.toml` 还控制 Codex 权限审批 hook 的运行策略。默认：

```toml
[codex.permission_request]
mode = "passthrough"
```

这表示即使系统级 Codex managed hooks 已安装，Agent Deck 也不会替 Codex 做 allow/deny 决策，
Codex 会继续显示原生审批界面。只有改成 `mode = "handle"` 时，PermissionRequest 才会进入
Agent Deck 的 decision broker；改成 `mode = "deny"` 则会直接拒绝。
