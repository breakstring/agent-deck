# Agent Deck 按键表面配置设计

状态：部分落地；N4 Pro 逐键配置与键盘快捷键扩展已实现，通用 Zone 仍为后续设计
日期：2026-06-24  
范围：产品语义与配置模型设计；第一阶段只覆盖硬件按键，不覆盖触屏、旋钮、旋钮 LED 或常驻启动。

## 2026-07-16 键盘快捷键实现补记

N4 Pro 主键现在新增 `keyboard_shortcut` binding。它不是任意自动化脚本，而是一个有界的
物理键事件模型：

```json
{
  "index": 0,
  "kind": "keyboard_shortcut",
  "label": "Command Palette",
  "shortcut": {
    "steps": [
      {
        "key": "KeyP",
        "modifiers": ["command", "shift"],
        "delay_after_ms": 0
      }
    ]
  },
  "icon": {"mode": "auto", "asset_id": null}
}
```

实现合同：

- `key` 使用 W3C `KeyboardEvent.code`；允许字母、数字、常用标点、导航键、F1–F20 和数字键盘。
  `key=null` 可表达纯修饰键步骤；第一版不支持 Fn、媒体键或 Caps Lock。
- 修饰键仅限 `command`、`control`、`option`、`shift`。一个序列 1–16 步，每步释放后等待
  0–2000ms，固定按住 20ms，整条序列最长 10 秒，末步 delay 必须为 0。
- `KeyPlan` 和 `InteractionIntent` 用强类型 `shortcut` / `shortcut_icon` 字段传递，不把嵌套
  动作编码进 `dict[str, str]` payload。
- macOS executor 在执行开始时通过 `NSWorkspace.frontmostApplication` 读取并固定 PID，随后用
  CoreGraphics 向该 PID 发送完整 down/up 序列。成功只表示事件已投递，不保证目标 App 消费。
- scheduler 是单 worker、零等待队列；已有任务运行时立即返回 `busy`。无论成功失败，executor
  都在 `finally` 中尽力释放已按下的键。
- daemon 启动和 status 只调用权限 preflight。只有配置页显式点击才调用系统权限 request。
- 浏览器只负责配置，不持有辅助功能权限。配置页常态只显示紧凑授权状态；悬停、键盘聚焦或点击
  “详情”后，才显示实际请求权限的 Agent 后台进程、执行文件路径、开发身份不稳定提示，以及请求、
  打开系统设置、重新检查动作。tmux/Python 只作为开发模式；正式分发的稳定授权身份由
  `Agent Deck.app` 内的用户级 Agent 服务提供。
- 保存 `keyboard_shortcut` binding 本身即表示启用；没有另一个全局或逐键 enabled 开关。
- 录制是一次可连续追加多步的会话；录制按钮在有新步骤后变成“停止并应用”，并复用顶部
  “保存并应用”的完整配置 API。停止后的局部“应用到硬件”也只镜像同一动作，不建立第二套保存合同。
- 自动图标单步居中、双步分行、多步显示前两步和 `+N`；Web 预览直接读取硬件 renderer 输出的
  同源 PNG。自定义图标支持 PNG/JPEG/WebP/ICO，最大 5 MiB、4096×4096，以规范化 PNG SHA-256
  内容寻址；资产缺失时硬件回退自动图标。
- 持久化 envelope 升级为 v2，v1 可直接迁移，未知未来版本 fail-closed。

该能力仍不允许文本、鼠标、shell 或跨动作类型序列，也不改变审批只能出现在明确上下文中的边界。

## 背景

Agent Deck 目前已经能把 Codex 状态投影到 N4 Pro，并能在 daemon 启动、退出或手动恢复时写入
Agent Deck 默认触屏图。这个阶段解决了“不要残留旧 quota 图”的兜底问题，但还没有解决更核心的产品问题：

- 硬件加电且没有 Agent 在跑时，上方按键是否应该全黑？
- 用户是否可以把一部分按键设成自己的常用动作？
- 当 Agent 开始运行后，Agent 状态应该占用哪些键？
- 不同硬件的按键数量不同，Agent Deck 如何不写死 N4 Pro？

本设计把 Agent Deck 从“Agent 状态显示器”重新定位为“可配置的本机 Agent 控制台”。按键的第一语义不是
固定的 Agent slot，而是用户可配置的 key surface；Agent 状态只是可以被投影到一部分按键上的动态内容。

## 产品目标

1. 用户可以决定哪些按键用于自己的固定动作，哪些按键用于显示 Agent。
2. 没有 Agent 在跑时，配置过的按键仍然有用，硬件不应大面积黑屏。
3. 有 Agent 在跑时，Agent Deck 只使用用户分配给 Agent 的按键区域，不擅自抢占用户动作键。
4. 同一套按键配置模型能适配 N4 Pro、普通 15-key grid、6-key companion 等不同硬件。
5. 高风险动作必须保留安全边界，不能因为用户自定义按键而绕过确认上下文。

## 非目标

本阶段不做：

- 触屏 panel 的完整重设计。
- 旋钮和旋钮 LED 的语义设计。
- 通用跨设备 Zone 图形编辑器；当前已实现 N4 Pro 逐键 Web 配置页。
- LaunchAgent 常驻启动。
- 云同步、多用户配置或配置 marketplace。
- 允许任意 shell 命令、文本注入或无上下文审批快捷键。
- 为所有未来硬件写完整默认布局；只定义可扩展模型和少量内置 profile 默认。

## 核心产品原则

### 用户配置优先

用户配置决定“哪些键属于哪个区域”。Agent Deck 只决定“区域内部当前显示什么状态”。

例如用户配置：

```text
keys 1-5: user_action
keys 6-10: agent_slot
```

那么即使当前有 8 个 Agent，Agent Deck 也不应该把 keys 1-5 临时拿来显示 Agent。它应该只在 keys 6-10
内排序、分页或提示 overflow。

### Agent 只是按键内容类型之一

按键可以是：

- 用户动作
- Agent slot
- 当前上下文动作
- 系统状态
- 禁用键

Agent slot 不拥有全部按键，只拥有配置给它的 zone。

### 无 Agent 不等于无用

没有 Agent 在跑时：

- `user_action` 键继续显示 App、URL、文件夹或 Agent Deck 内部动作。
- `agent_slot` 键显示低亮空槽位或 Agent Deck ready 占位。
- `system_status` 键显示 daemon、Codex、quota 或硬件连接状态。
- `disabled` 键保持关闭。

这比系统自动填满无意义占位更稳定，也比全黑更像可用硬件产品。

### 高风险动作必须有上下文

用户自定义按键第一阶段只开放低风险动作。审批、文本输入、shell 命令等高风险动作不能被永久绑定成一个
无上下文快捷键。

允许的第一阶段动作：

- `launch_app`
- `focus_app`
- `open_url`
- `open_path`
- `send_keyboard_shortcut`（仅受限物理键、组合键和时序）
- `agent_deck_command`

暂不默认开放：

- `run_shell`
- `type_text`
- `paste_text`
- `approve_permission`
- `deny_permission`

审批只能作为 `context_action` 出现在明确的 pending decision 上下文中，并且必须有屏幕或桌面端说明。

## 按键角色

### `user_action`

用户固定动作键。它不依赖 Agent 是否存在。

典型用途：

- 启动 Terminal、Chrome、Cursor、Codex 等 App。
- 聚焦某个 App。
- 打开 URL。
- 打开文件夹或项目目录。
- 执行 Agent Deck 内部安全命令，例如切换 view、刷新 status。

显示规则：

- 有用户提供图标时使用用户图标。
- 没有图标时使用 App icon、favicon、路径类型图标或文字首字母 fallback。
- 状态可叠加小角标，例如 App 不存在、路径不可访问、动作被禁用。

### `agent_slot`

Agent 状态键。它显示一个 Agent 的当前状态，并提供选择或聚焦入口。

Agent slot 必须复用现有状态归约和按键视觉映射，不另起一套产品状态词表。当前 canonical 来源是：

```text
AgentStatus -> resolve_visual_icon_spec(status) -> VisualIconSpec.variant_id -> generated Codex key frames
```

当前已实现映射：

| AgentStatus | VisualIconSpec.variant_id | 视觉语义 |
| --- | --- | --- |
| `approval_needed` | `needs_user` | 需要用户处理，琥珀提醒 |
| `waiting_user` | `needs_user` | 等待用户输入，琥珀提醒 |
| `error` | `error` | 错误，红色提醒 |
| `running_tool` | `working` | 正在执行工具，工作动画 |
| `thinking` | `working` | 正在思考，工作动画 |
| `completed_recently` | `completed` | 刚完成，成功角标或短暂完成态 |
| `idle` | `idle` | 空闲但在线 |
| `offline` | `offline` | 离线或 stale 投影，低亮静态图 |

文档或配置中可以使用更友好的显示文案，但 renderer、projection 和测试必须以
`AgentStatus` 与 `VisualIconSpec.variant_id` 为准。未来如果要引入 `blocked`、`paused`
或更细的 child-agent 状态，必须先扩展状态机、视觉映射和生成帧资产，再进入 key surface 配置。

显示规则：

- 有 Agent 时显示 Agent 状态视觉。
- 无 Agent 时显示低亮空槽位，不抢视觉焦点。
- child agent 使用角标或 branch 标识，不在第一版做复杂树形导航。

交互规则：

- 单击：选择该 Agent。
- 二次确认或配置允许时：聚焦该 Agent。
- 不直接执行审批或文本输入。

### `context_action`

只在特定上下文中有效的动作键。

典型用途：

- pending decision 时显示 Allow / Deny / Details。
- Agent overflow 时显示 Next Page / Previous Page。
- 错误状态时显示 Retry / Dismiss。

显示规则：

- 没有上下文时可隐藏、低亮或回退到 zone fallback。
- 高风险动作必须显示明确文本、颜色和上下文来源。

### `system_status`

Agent Deck 系统状态键。

典型用途：

- Deck ready / degraded。
- Codex connected / offline。
- Quota ok / low / unknown。
- Hardware renderer ok / failed。

显示规则：

- 不用于高频动态动画。
- 以稳定图标和颜色表达系统状态。
- 按下后进入相关详情或刷新命令。

### `disabled`

明确禁用的键。

显示规则：

- 默认关闭或极低亮。
- 不响应输入。
- 用于避免误触或保留物理空位。

## Zone 与 Binding

### Zone

Zone 是一组连续或非连续按键的用途声明。它符合用户对硬件区域的理解，比逐个 key 配置更容易维护。

示例：

```toml
[[hardware.profiles.mirabox_n4pro.layout.zones]]
name = "quick_apps"
keys = [1, 2, 3, 4, 5]
role = "user_actions"

[[hardware.profiles.mirabox_n4pro.layout.zones]]
name = "agents"
keys = [6, 7, 8, 9, 10]
role = "agent_slots"
max_items = 5
overflow = "page"
```

Zone 字段建议：

- `name`：稳定名称，用于诊断和 UI。
- `keys`：物理 key id 列表，必须在设备 profile 的 key 范围内。
- `role`：`user_actions`、`agent_slots`、`context_actions`、`system_status` 或 `disabled`。
- `max_items`：该 zone 最多承载多少动态 item，默认等于 key 数量。
- `overflow`：动态内容超过 key 数量时的处理方式。
- `fallback`：该 zone 没有内容时显示什么。

### Binding

Binding 是具体按键或 zone item 的动作定义。

示例：

```toml
[[hardware.profiles.mirabox_n4pro.layout.bindings]]
key = 1
label = "Terminal"
role = "user_action"
action = "launch_app"
bundle_id = "com.apple.Terminal"

[[hardware.profiles.mirabox_n4pro.layout.bindings]]
key = 2
label = "Chrome"
role = "user_action"
action = "focus_app"
bundle_id = "com.google.Chrome"
```

Binding 字段建议：

- `key`：物理 key id。
- `label`：显示名。
- `role`：可选；缺省时继承 zone role。
- `action`：动作类型。
- `bundle_id`、`app_path`、`url`、`path`：动作参数。
- `icon`：可选图标路径或内置图标 id。
- `enabled`：其他 binding 可选的启用标记；当前 `keyboard_shortcut` 以 binding 是否存在表达启用，
  不额外保存 enabled 字段。

### Zone 与 Binding 的关系

推荐第一版支持：

1. Zone 定义大区域用途。
2. Binding 覆盖具体键的显示和动作。
3. Binding 不允许突破 zone 的安全等级。例如 `user_actions` zone 不能绑定 `approve_permission`。

## 默认布局策略

### 通用原则

无配置时不能全黑。默认布局应包含：

- 少量 Agent slot。
- 至少一个 Agent Deck Home / status 入口。
- 如果可发现 Codex 或 quota，则给系统状态留出位置。
- 如果设备按键数量较少，优先保留 Agent slot 和 Home，不强行提供 App 快捷键。

### N4 Pro 建议默认

N4 Pro 有 15 个逻辑按键，其中当前产品讨论先聚焦上方 10 个主按键。第一版默认可以采用：

```text
keys 1-5: agent_slots
keys 6-8: user_actions fallback / unassigned quick actions
key 9: system_status quota
key 10: system_status deck_home
keys 11-15: context_actions
```

也可以在首次配置向导中提供布局模板：

```text
Agent-first:    1-8 agent slots, 9 quota, 10 home
Personal-first: 1-5 user actions, 6-10 agent slots
Ops-first:      1-4 user actions, 5-8 agent slots, 9 quota, 10 home
```

推荐默认模板：`Personal-first`。

理由：

- 它符合用户提出的“上面一部分是我的快捷键，下面一部分是 Agent 状态”的使用心智。
- 没有 Agent 时仍有可用按键。
- 有 Agent 时不会抢占用户固定动作键。

### 15-key grid 建议默认

```text
keys 1-5: user_actions
keys 6-13: agent_slots
key 14: quota/system_status
key 15: deck_home
```

### 6-key companion 建议默认

```text
keys 1-3: user_actions
keys 4-5: agent_slots
key 6: deck_home / context_action
```

按键少的设备不默认开放硬件审批。审批应退回桌面端或明确上下文 UI。

## Runtime Projection

运行时不直接从 AgentState 生成按键，而是经过用户布局：

```text
DeviceProfile + UserKeyLayout + AgentState + SystemStatus
  -> KeySurfaceProjection
  -> HardwareSurface renderer
```

### Projection 输入

- `DeviceProfile`：设备能力和 key 数量。
- `UserKeyLayout`：用户 zone 和 binding。
- `AgentStateStore`：当前 Agent 快照。
- `DecisionBroker`：pending decisions。
- `SystemStatus`：Codex scanner、quota、hardware renderer、daemon health。
- `SelectionState`：当前选中的 Agent、页码和面板。

### Projection 输出

每个 key 输出一个 `ProjectedKey`：

- `key`
- `role`
- `label`
- `icon`
- `variant`
- `intent`
- `agent_key`
- `decision_id`
- `enabled`
- `priority`
- `diagnostics`

Renderer 只负责把 `ProjectedKey` 画成图和绑定输入，不应该重新解释配置规则。

## Agent Slot 排序与 Overflow

默认 Agent 排序：

1. `approval_needed`
2. `waiting_user`
3. `error`
4. `running_tool`
5. `thinking`
6. `completed_recently`
7. `idle`
8. `offline`

该顺序应与现有 layout 层的 `AgentStatus` 优先级保持一致。`offline` 默认不占用主 Agent slot；
只有诊断视图或显式配置允许时才展示。若未来引入 `blocked` 等新状态，必须同时更新状态优先级、
视觉映射和 key-frame asset 生成规则。

Overflow 策略：

- `priority_only`：只显示最高优先级 Agent。
- `page`：允许分页，默认推荐。
- `rotate`：自动轮换，第一版不推荐。
- `expand_when_unassigned`：只有未配置的空键可临时扩展，第一版可延后。

第一版建议实现：

```text
overflow = "page"
```

没有分页控制硬件时，退化为 `priority_only`，并在 system/touch context 中提示 `+N more`。

## 状态优先级

在同一个 key 或 zone 内，显示优先级为：

1. 明确禁用。
2. 高风险上下文动作，例如 pending approval。
3. 用户固定动作。
4. Agent 动态状态。
5. 系统状态。
6. 空槽位 fallback。

但跨 zone 不抢占。也就是说，pending approval 可以改变 `context_actions` zone 和对应 Agent slot 的视觉，
但不应覆盖 `user_actions` zone。

## 无 Agent 场景

当没有 Agent 在跑时：

- `user_action` zone 正常显示并响应。
- `agent_slots` zone 显示低亮空槽位。
- `system_status` zone 显示 Deck/Codex/Quota 状态。
- 若设备无任何用户配置，则显示默认 Home、Codex、Agent empty 和 Settings/Help。

示例：

```text
Key 1 Terminal    Key 2 Chrome     Key 3 Cursor     Key 4 Finder     Key 5 Notes
Key 6 Agent Slot  Key 7 Agent Slot Key 8 Agent Slot Key 9 Quota      Key 10 Deck
```

## 有 Agent 场景

当有 Agent 在跑时：

- Agent 只进入 `agent_slots` zone。
- `user_action` zone 保持原样。
- `system_status` zone 可显示聚合状态。
- selected Agent 的详情由后续 touch/desktop surface 解释；按键本身只保留简洁状态。

示例：

```text
Key 1 Terminal    Key 2 Chrome     Key 3 Cursor     Key 4 Finder     Key 5 Notes
Key 6 Codex A     Key 7 Worker B   Key 8 Agent Slot Key 9 Quota      Key 10 Deck
```

## Pending Approval 场景

审批是特殊高优先级上下文，但不能破坏用户固定 zone。

规则：

- 对应 Agent slot 使用 `approval_needed -> needs_user` 视觉态。
- `context_actions` zone 显示 Allow / Deny / Details。
- 如果设备没有 `context_actions` zone，则不在按键上直接显示 Allow/Deny，改为提示在桌面端或 touch surface 处理。
- 永久 `user_action` 键不能绑定 `approve_permission` 或 `deny_permission`。

## 配置文件位置与合并策略

建议把用户布局纳入 Agent Deck 配置：

```text
~/.config/agent-deck/config.toml
```

项目本地可支持 override：

```text
.agent-deck/config.toml
```

合并顺序：

1. 内置 device profile 默认布局。
2. 用户全局配置。
3. 项目本地配置。
4. 临时 CLI 参数或测试注入。

项目本地配置不能默认启用高风险动作。若未来支持 project-specific actions，必须显式标记 trust scope。

## 配置校验

配置加载时必须校验：

- key id 在设备 key 范围内。
- zone 不重叠，除非显式 override。
- binding key 属于已声明 zone 或允许自动创建 per-key zone。
- action 参数完整，例如 `launch_app` 必须有 `bundle_id` 或 `app_path`。
- action 安全等级不超过 zone 和 device profile 允许的等级。
- 高风险 action 默认拒绝。

错误处理：

- 配置非法时不应导致 daemon 崩溃。
- daemon 应回退到内置默认布局，并在 status 中暴露配置错误。
- CLI doctor/status 应能显示当前生效布局来源和校验错误。

## 安全边界

第一版安全策略：

- `launch_app`、`focus_app`、`open_url`、`open_path` 属于低风险动作。
- `run_shell`、`type_text`、`paste_text`、`approve_permission` 属于高风险动作。
- 高风险动作不进入第一版用户 binding。
- 硬件输入永远先转换为 intent，再由 action executor 执行。
- action executor 必须知道动作来源、设备、key 和配置来源，便于审计。
- 配置中不得记录 token、password、authorization 等敏感信息。

## 与现有架构的关系

当前核心链路是：

```text
Agent ingress -> NormalizedEvent -> AgentStateStore -> DeckMode/LayoutPlan -> HardwareSurface
```

按键配置引入后，布局层应演进为：

```text
AgentStateStore + DecisionBroker + SystemStatus + UserKeyLayout
  -> KeySurfaceProjection
  -> LayoutPlan
  -> HardwareSurface
```

`LayoutPlan` 不应直接假设前 10 个 key 都是 Agent。它应该接收 projection 后的 key roles 和 intents。

## 第一版实现切片建议

本 spec 不是实现计划，但建议第一版实现切片如下：

1. 数据模型
   - `KeyRole`
   - `KeyZone`
   - `KeyBinding`
   - `UserKeyLayout`
   - `ProjectedKey`

2. 默认布局
   - N4 Pro `Personal-first`
   - Generic 15-key
   - 6-key companion

3. Projection
   - 无 Agent
   - 有 Agent
   - Agent overflow
   - pending approval 不抢占 user_action zone

4. 低风险 action
   - `launch_app`
   - `focus_app`
   - `open_url`
   - `open_path`

5. 测试
   - 配置校验
   - 默认布局
   - no-agent projection
   - with-agent projection
   - overflow projection
   - high-risk action rejection
   - fake hardware input intent

## 设计验收

完成本设计后，应能回答：

- 一个只有按键的硬件如何显示用户动作和 Agent 状态？
- 没有 Agent 时为什么不全黑？
- 用户如何把一部分键保留给自己的 App？
- Agent 为什么不能抢占用户动作键？
- 审批为什么不能被绑定成永久快捷键？
- 新硬件接入时需要定义哪些 profile 和默认布局？

## 待后续设计的问题

以下问题不在本 spec 内解决：

- N4 Pro touch bar 如何解释 selected key 或 selected Agent。
- 4 个旋钮和 LED 的 lane 语义。
- 图形化布局编辑器。
- 配置迁移和版本化。
- App icon、favicon、文件夹图标的采集和缓存策略。
- 跨设备布局同步。
- 用户自定义图标素材管理。
- 高风险动作的二次确认机制。
