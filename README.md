# Agent Deck

Agent Deck 是一个面向本机 AI Agents 的硬件控制台项目。目标是把妙联宝 N4 Pro 等设备用作 Agent 状态面板和安全交互入口。

当前项目处于设计与骨架初始化阶段。主要文档：

- `docs/superpowers/specs/2026-06-12-agent-deck-analysis.md`
- `docs/superpowers/specs/2026-06-12-agent-deck-mvp-design.md`
- `docs/references/agent-deck-roadmap.md`
- `docs/references/stream-dock-scenes-research-2026-06-12.md`

Python 项目使用 `uv` 管理。

## 本地启动

仓库根目录提供了默认启动脚本：

```bash
./run.sh
```

它等价于启动 `agent-deckd` 的本机默认配置：监听 `127.0.0.1:8765`，读取
`agent-deck.toml`，并默认执行真实硬件渲染。当前配置里的默认设备 profile 是 `n4pro`，
所以 Codex 状态按钮动画和底部 quota 背景会在同一次硬件写入里共存。需要临时覆盖参数时，
可以直接把通用参数追加到脚本后面，例如：

```bash
./run.sh --disable-hardware-renderer
```

默认硬件渲染节奏是 `render_interval_seconds = 3.0`、`fps = 10`，对应当前 30 帧的
N4 Pro Codex 动画资产；这样每次下发按钮图标时能播放一个完整 working 动画周期，而不是
只反复看到前几帧。

`agent-deck.toml` 还控制 Codex 权限审批 hook 的运行策略。默认：

```toml
[codex.permission_request]
mode = "passthrough"
```

这表示即使系统级 Codex managed hooks 已安装，Agent Deck 也不会替 Codex 做 allow/deny 决策，
Codex 会继续显示原生审批界面。只有改成 `mode = "handle"` 时，PermissionRequest 才会进入
Agent Deck 的 decision broker；改成 `mode = "deny"` 则会直接拒绝。
