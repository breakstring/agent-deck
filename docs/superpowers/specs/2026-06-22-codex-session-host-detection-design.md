# Codex 会话宿主检测设计

状态：草案
日期：2026-06-22
范围：Codex CLI / Codex App 活跃会话的宿主识别、tmux 分层、聚焦能力分级

## 背景

Agent Deck 需要知道当前机器上活跃的 Codex 会话“实际是什么”，才能决定硬件按钮是否只展示状态、是否可以聚焦、以及未来是否允许更高风险的文本输入动作。Codex 的运行形态不能简单归为“CLI 或 App”两类：CLI 可能直接运行在终端 PTY 中，也可能运行在 tmux pane 中；tmux pane 又可能 detached，或者被一个或多个终端客户端 attach；Codex App 则可能在单个 App 实例中维护多个 thread。

本设计把“运行宿主”和“展示客户端”拆开，避免把 tmux 当成 Terminal、iTerm2、Ghostty、Otty 这类终端 App 的同级枚举。

## 目标

1. 识别 Codex 会话是 `codex_cli`、`codex_app` 还是未知来源。
2. 对 Codex CLI，识别它的执行宿主：直接 PTY、tmux pane、未知 PTY。
3. 对 tmux pane，区分 detached 和 attached，并记录可恢复目标。
4. 对 attached tmux，尽量识别展示该会话的终端客户端。
5. 对 Codex App，识别最近有效 thread，但第一阶段只承诺激活 App，不承诺精确打开 thread。
6. 输出结构化 `AgentHostContext`，供状态展示、按钮布局和后续 `focus_agent` 使用。

## 非目标

- 不默认向 Codex 或终端输入文本。
- 不绕过 Codex 自身权限系统。
- 不写 Codex App SQLite、rollout JSONL 或终端配置。
- 不默认使用 Accessibility 自动点击 Codex App 内部 thread。
- 不把 `ps` 全局扫描当作唯一事实来源。

## 核心模型

Codex CLI 的宿主模型分为两层：

```text
Codex CLI process
  execution_host: direct_pty | tmux_pane | unknown_pty
  presentation_clients: terminal_app[] | empty
```

`execution_host` 是 Codex 进程实际运行的位置。`presentation_clients` 是当前用户可能看到这个会话的窗口、tab 或客户端。tmux 是 execution host，不是 terminal app。

### 直接 PTY

直接 PTY 表示 Codex CLI 进程运行在终端 App 创建的 shell / login / PTY 链路中，未发现 tmux pane 绑定。

示例：

```text
codex -> zsh -> login -> Otty.app
```

这类会话的激活能力通常是 `app_activate_only` 或 `window_activate`，是否能精确到 tab 取决于终端 App 是否提供稳定 AppleScript、URL scheme 或 Accessibility 目标。

### tmux pane

tmux pane 表示 Codex CLI 的 TTY 或进程链能绑定到某个 tmux pane。此时 `focus_target` 应优先保存 tmux pane，而不是终端 App。

需要保存：

- `tmux_pane_id`，例如 `%7`，优先作为内部 handle。
- `tmux_session_name`
- `tmux_window_id` 或 `tmux_window_index`
- `tmux_pane_index`
- `pane_tty`
- `pane_pid`
- `attached`

不要只用 `session:window.pane` 作为长期 ID，因为 session、window、pane 的名字或 index 都可能变化。

### tmux detached

如果 tmux pane 仍存在，但没有可关联的 attached client，则会话处于 detached presentation 状态。

展示语义：

```text
runtime: Codex CLI
execution_host: tmux pane
presentation: detached
activation: tmux_reattach_new_client
```

硬件按钮执行 `focus_agent` 时，应打开用户配置的首选终端 App，并 attach 到目标 tmux session，再选择目标 pane。它不是“激活旧终端窗口”。

### tmux attached

如果 tmux pane 所在 session 有一个或多个 attached client，则同时记录 presentation clients。

展示语义：

```text
runtime: Codex CLI
execution_host: tmux pane
presentation: Otty/Ghostty/Terminal client
activation: tmux_select_existing_client
```

如果有多个 attached client，且无法判断用户最近使用的是哪个客户端，第一阶段应降级为低置信度选择：优先前台或最近活动 client；无法确认时退化为新开终端 attach，而不是盲目切换未知窗口。

### Codex App thread

Codex App thread 来自 `~/.codex/state_*.sqlite` 和 thread rollout JSONL 的只读扫描。它可以识别 thread id、cwd、rollout path 和粗略状态。

第一阶段只承诺：

- 识别最近有效 Codex App thread。
- 显示脱敏短标题、cwd、状态和等待用户输入状态。
- 激活 Codex App 本身。

第一阶段不承诺：

- 精确打开某个 Codex App thread。
- 通过 Accessibility 自动点击 thread。
- 直接向 App 内部输入 prompt。

## 数据结构

建议新增不可变 Pydantic 模型：

```python
class AgentHostContext(BaseModel):
    runtime_kind: Literal["codex_cli", "codex_app", "unknown"]
    execution_host: ExecutionHostContext
    presentation_clients: tuple[PresentationClientContext, ...] = ()
    activation: ActivationContext
    agent_pid: int | None = None
    pid_start_time: datetime | None = None
    tty: str | None = None
    cwd: str | None = None
    thread_id: str | None = None
    rollout_path: str | None = None
    observed_at: datetime
    confidence: Literal["high", "medium", "low"]
    notes: tuple[str, ...] = ()
```

具体实现时可以按现有代码风格拆成 `StrEnum`，避免直接散落字符串。

`ExecutionHostContext` 建议覆盖：

- `kind`: `direct_pty | tmux_pane | codex_app | unknown_pty | unknown`
- `host_app_name`
- `host_app_path`
- `tmux_session_name`
- `tmux_window_id`
- `tmux_window_index`
- `tmux_pane_id`
- `tmux_pane_index`
- `pane_tty`
- `pane_pid`
- `attached`

`PresentationClientContext` 建议覆盖：

- `kind`: `terminal_app | tmux_client | codex_app_window | unknown`
- `app_name`
- `app_path`
- `app_pid`
- `client_tty`
- `tmux_session_name`
- `confidence`

`ActivationContext` 建议覆盖：

- `strategy`: `unavailable | app_activate_only | terminal_activate | tmux_reattach_new_client | tmux_select_existing_client`
- `target`: 稳定的结构化目标，不拼接 shell 命令。
- `requires_accessibility`: 是否需要 Accessibility 权限。
- `requires_terminal_launch`: 是否需要启动终端 App。
- `confidence`

## 检测信号

### 主信号：Codex hook payload

已安装 Agent Deck hook 时，hook command 会传入 `--agent-pid "$PPID"`。这是 live Codex CLI 会话最可靠的起点。

后续可以在 hook command 中增加 allowlist 环境字段：

- `TTY`
- `TERM`
- `TERM_PROGRAM`
- `TMUX`
- `TMUX_PANE`
- `KITTY_WINDOW_ID`
- `WEZTERM_PANE`
- 明确无敏感内容的 Ghostty/Otty 标识变量

不要上传完整环境变量，避免泄露 token、secret、authorization、api_key、password 等敏感信息。

### 辅助信号：进程树

从 `agent_pid` 向上追踪 `ppid`，直到遇到：

- 终端 App bundle 进程。
- `/usr/bin/login` 和 shell 链路。
- tmux server / client。
- Codex App helper。
- 进程不存在或权限不足。

需要记录 `pid_start_time`，避免 PID 复用导致旧会话错绑。

### 辅助信号：TTY

Codex CLI 的 `tty` 可用于绑定普通 PTY 或 tmux pane。tmux 场景中，优先用 `tmux list-panes` 的 `pane_tty` 反查 pane。

### 辅助信号：tmux

如果本机存在 tmux，使用只读命令：

```bash
tmux list-panes -a -F "#{pane_id}\t#{pane_tty}\t#{pane_pid}\t#{session_name}\t#{window_id}\t#{window_index}\t#{pane_index}\t#{pane_current_path}"
tmux list-clients -F "#{client_tty}\t#{client_pid}\t#{session_name}\t#{client_activity}"
```

这些命令失败时应降级为非 tmux 检测，不应阻断 Codex App 或普通 CLI 检测。

### 辅助信号：Codex App state

继续复用现有 Codex App state scanner，读取 `state_*.sqlite` 和 rollout JSONL。该扫描是 best-effort，不能替代 live hook。

## 激活策略

### direct PTY

如果只知道 terminal app，但不知道 tab/window，则输出：

```text
activation.strategy = app_activate_only
confidence = medium
```

如果未来针对某个终端 App 实现了稳定窗口定位，再升级为 `terminal_activate`。

### tmux detached

输出：

```text
activation.strategy = tmux_reattach_new_client
confidence = high
```

ActionExecutor 应打开首选终端并运行 attach/select 流程。命令生成必须在 action 层完成，不能由检测层拼接 shell 字符串。

### tmux attached

输出：

```text
activation.strategy = tmux_select_existing_client
confidence = high | medium
```

如果能定位到唯一 client，优先激活对应终端 App，再切换 tmux pane。如果多个 client 无法 disambiguate，则降级为 medium 或 low，并允许用户配置偏好：使用前台 client、最近活动 client，或新开终端 attach。

### Codex App

输出：

```text
activation.strategy = app_activate_only
confidence = medium
```

即使识别到 thread，也不把 thread id 伪装成可精确激活目标。

## 展示策略

硬件和 CLI 输出必须区分：

- `runtime`: Codex CLI / Codex App / Unknown。
- `execution`: direct PTY / tmux pane / app thread。
- `presentation`: Otty/Ghostty/Terminal client / detached / unknown。
- `activation`: 可执行动作和置信度。

不要直接展示 Codex App `threads.title` 的完整内容。默认展示短标题或派生标签，并保留 cwd、状态和 thread id 后缀作为诊断信息。

## 安全边界

1. host detection 默认只读。
2. 检测层不执行 shell 副作用命令，不启动终端，不 attach tmux。
3. action 层执行聚焦时必须使用结构化 target，并注入 `AGENT_DECK_INTERNAL=1`。
4. 文本输入动作仍默认关闭，且必须依赖高置信 activation target。
5. `PermissionRequest` 决策不依赖 host detection 成功；检测失败不能让审批 fail-open。

## 验证切口

第一阶段可以先实现只读 CLI：

```bash
uv run agent-deckctl codex-hosts --json
```

建议覆盖的 fixture 和测试：

1. Codex CLI 直接运行在 Otty/Ghostty/Terminal 的 fake `ps` 样本。
2. Codex CLI 在 tmux detached pane 中，`tmux list-clients` 为空。
3. Codex CLI 在 tmux attached pane 中，存在唯一 client。
4. Codex CLI 在 tmux attached pane 中，存在多个 client，输出低置信或配置化策略。
5. Codex App thread 只读扫描输出 `codex_app`，activation 为 `app_activate_only`。
6. `agent_pid` 不存在或 PID start time 不匹配时降级为 unknown。

真实 macOS smoke 验证：

1. 在普通终端直接运行 `codex`，确认 runtime 为 `codex_cli` 且 execution host 为 `direct_pty`。
2. 在 tmux pane 中运行 `codex`，detach 后确认 execution host 为 `tmux_pane` 且 activation 为 `tmux_reattach_new_client`。
3. attach 回 tmux session 后确认 presentation client 非空，activation 切换为 `tmux_select_existing_client`。
4. 打开 Codex App，确认最近 thread 仍只提供 `app_activate_only`。

## 实施顺序

1. 新增 `agent_deck.hosts` 或 `agent_deck.adapters.host_context` 模块，定义数据模型。
2. 新增进程表抽象，生产环境走 macOS `ps`，测试使用 fixture。
3. 新增 tmux 只读探测器，失败时降级。
4. 新增 Codex host resolver，把 hook payload、process table、tmux 和 App scanner 合并。
5. 新增 `agent-deckctl codex-hosts --json`。
6. 将 host context 接入 `AgentState.focus_target` 或新增 `host_context` 字段。
7. 后续再实现 `focus_agent` 的 tmux reattach / select 动作。
