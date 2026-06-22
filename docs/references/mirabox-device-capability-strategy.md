# Mirabox 设备能力与 Agent Deck 产品策略

状态：草案  
日期：2026-06-22  
范围：Mirabox / Stream Dock 硬件能力分层、Agent Deck 功能映射、安全边界和设备支持优先级

## 背景

Agent Deck 当前已经在 N4 Pro 上验证了按键、背景屏和 quota virtual panel 的最短闭环。
继续向“点击某个 Agent 状态后激活对应会话”推进前，需要先回答一个更上层的问题：
不同 Mirabox 硬件适合承载哪些 Agent Deck 能力，以及哪些能力必须受显示面积、输入方式和
安全上下文约束。

术语说明：

- 物理主按键：用户肉眼可见、可独立按下的 LCD key。N4 Pro 官方资料描述为 10 个。
- SDK 逻辑 key：Python SDK 中 `ButtonKey.KEY_*` 的逻辑编号。它可能包含物理主按键，也可能包含
  secondary screen / soft-key 区域，不等同于物理按钮数量。
- secondary screen / soft-key slot：下方触控/显示区域内可被 SDK 当作 key image 或 button-like
  event 处理的逻辑槽位。它适合承载 mode、page、focus、deny/snooze、details 等短动作。
- touch display / touch panel：可渲染较大背景并读取 touch point / swipe 的区域。Agent Deck 应把
  这类区域抽象为逻辑窗口目标，而不是 quota 专用屏。
- logical panel：Agent Deck 自己的逻辑窗口内容模型。第一批内容类型是 `quota`、`tokens`、
  `pets`、`message`；它们可以按设备能力映射到底部 background viewport、secondary soft-key
  slot、外部 companion UI 或未来其他 surface。
- rotary control：旋钮旋转和旋钮按下。旋钮按下可映射成业务 intent，但不计入 SDK 逻辑 key 数。

本策略把硬件拆成 capability profile，而不是按型号写死交互。原因有三点：

1. Mirabox 产品线覆盖按键矩阵、触控条、触摸点、旋钮、RGB LED、键盘背光等不同能力。
2. 官方产品页、第三方 surface 文档和 Python Device SDK 对某些型号的能力描述并不完全一致，
   例如 N4 的旋钮和 touch strip 在官方/Companion 文档里存在，但当前 vendored Python SDK 只
   建模为按钮事件。
3. Agent Deck 的安全边界不应由“有一个可按按钮”决定。审批、聚焦、文本注入等动作需要结合
   可读上下文、目标置信度和用户显式配置。

## 资料来源

### 官方与第三方资料

- MiraBox N4 Pro 官方页：10 个自定义 LCD keys、1 个 2.8 英寸 touch panel、4 个 RGB 金属旋钮；
  页面也描述了 tap、touch、turn 和 sliding page turning 等交互。
  <https://mirabox.net/products/mirabox-stream-dock-n4-pro>
- MiraBox N4 官方页：10 个自定义 LED buttons、110x14mm LED touch bar、4 组 360 度旋钮和 USB hub。
  <https://mirabox.net/products/mbox-n4>
- MiraBox 293S 官方页：15 个 LCD keys、monitoring bar、GIF dynamic icons。
  <https://mirabox.net/products/mirabox-293s>
- MiraBox N3 官方页：6 个自定义 LCD keys、3 个 knobs，并强调 drag-and-drop、scene follow 和 GIF icons。
  <https://mirabox.net/products/mirabox-n3-stream-deck>
- MiraBox K1 Pro 官方页：87 键机械键盘，内置 6 个 LCD keys、3 个 knob、RGB 背光和 Stream Dock 能力。
  <https://mirabox.net/products/mirabox-k1-pro-mechanical-ai-keyboard-with-stream-deck>
- StreamDock Device SDK 官方仓库：包含 Python-SDK、WebSocket-SDK、CPP-SDK，是直连设备能力的主要入口。
  <https://github.com/MiraboxSpace/StreamDock-Device-SDK>
- StreamDock Plugin SDK 官方仓库：包含 JavaScript、Vue、Node.js、C++、Qt、Python 插件模板，面向官方
  Stream Dock 软件生态。
  <https://github.com/MiraboxSpace/StreamDock-Plugin-SDK>
- Bitfocus Companion Mirabox Stream Dock surface 文档：确认 293V3、N3、N4 的 Companion 映射，
  并提示使用 Companion 时官方 Mirabox creator software 不能运行。
  <https://companion.free/user-guide/v4.2/surfaces/mirabox-streamdock/>

### 本仓库 / vendored SDK 证据

- `vendor/streamdock-python-sdk/src/StreamDock/InputTypes.py` 定义统一事件类型：
  `BUTTON`、`KNOB_ROTATE`、`KNOB_PRESS`、`SWIPE`、`TOUCH_POINT`。
- `vendor/streamdock-python-sdk/src/StreamDock/Devices/StreamDockN4Pro.py`：
  `KEY_COUNT = 15` 是 SDK 逻辑 key 数，不是 15 个物理主按钮。映射里 `KEY_1` 到 `KEY_10`
  对应 10 个主按键；`KEY_11` 到 `KEY_15` 对应 secondary screen / soft-key slot。4 个旋钮、
  旋钮按下、左右 swipe、touch point 解码、800x480 background、key image 和 frame background
  是独立能力。
- `vendor/streamdock-python-sdk/src/StreamDock/Devices/StreamDockN4.py`：
  当前 SDK 只建模 14 个 key 和 800x480 background，未在 `decode_input_event()` 中暴露 N4 旋钮或 swipe。
- `vendor/streamdock-python-sdk/src/StreamDock/Devices/StreamDockN3.py`：
  6 个可绘制主 key、3 个底部按钮、3 个旋钮旋转/按下，背景规格 320x240。
- `vendor/streamdock-python-sdk/src/StreamDock/Devices/StreamDockN1.py`：
  15 个主 key、2 个 secondary screen key、1 个旋钮、480x854 背景；背景能力受固件版本判断影响。
- `vendor/streamdock-python-sdk/src/StreamDock/Devices/StreamDockM3.py`：
  15 个 key、3 个旋钮、854x480 background、background GIF 支持。
- `vendor/streamdock-python-sdk/src/StreamDock/Devices/StreamDockM18.py`：
  18 路输入映射、480x272 background、RGB LED。
- `vendor/streamdock-python-sdk/src/StreamDock/Devices/StreamDockXL.py`：
  32 个 key、2 个旋转输入、1024x600 background、RGB LED、background GIF 支持。
- `vendor/streamdock-python-sdk/src/StreamDock/Devices/K1Pro.py`：
  6 个 key、3 个旋钮、键盘背光亮度、灯效速度、RGB 背光和 OS mode 切换。
- `src/agent_deck/hardware/streamdock_n4pro.py` 和
  `src/agent_deck/hardware/streamdock_touchscreen.py` 已记录 N4 Pro 实测约束：
  同一次设备会话内用 `set_frame_background` 写背景，再写按键图；不要把触屏背景和按键拆成
  两个独立 open/init/close sink。
- N4 Pro SDK 同时暴露三类图像格式：主按键 `112x112`，secondary screen `176x112`，
  touchscreen/background `800x480`。当前 Agent Deck 的 quota virtual panel 走 800x480
  background 的底部 viewport，这是一种已验证的 composite 写法；后续 profile 层应继续把
  “逻辑窗口”与“具体下发 surface”分开，以便选择 secondary screen slot、touch display viewport
  或其他设备 surface。

## 设备能力 profile

### `key_grid`

能力：

- 多个可绘制 LCD key。
- key press/release 或兼容形式的 key event。
- 可显示 icon、状态色、短标签和简单动画。

典型设备：

- 293V3
- 293S
- M18 的按钮部分
- 部分 15-key 设备

适合 Agent Deck：

- 每个 key 显示一个 Agent / session / slot。
- 状态动画：idle、running、blocked、needs approval、error、offline。
- 低风险动作：选择 Agent、打开详情、聚焦桌面 companion、启动预配置 Agent。
- 多页切换：当 Agent 数超过 key 数时使用 page key 或长按切换。

不适合：

- 直接展示完整 permission request。
- 复杂 quota、token 曲线、diff、命令摘要。
- 需要明确上下文的 approve 操作。

### `key_grid_with_status_strip`

能力：

- `key_grid` 全部能力。
- 一条窄状态栏或 secondary screen / soft-key slot，可显示全局状态、局部摘要或短动作。

典型设备：

- 293S
- N4 / N4 Pro 的 touch bar 部分
- N1 / 293S 的 secondary screen key

适合 Agent Deck：

- 全局状态：活跃 Agent 数、待审批数、quota 警告、daemon 健康状态。
- 当前选中 Agent 的短摘要：模型、workspace、host confidence、最近状态。
- 页码、过滤器、模式标签。
- `mode`、`page`、`focus`、`deny/snooze`、`details` 这类可用短标签表达的动作。

不适合：

- 把状态条当成完整审批 UI。
- 显示长 prompt 或长命令。

### `rotary_control`

能力：

- 一个或多个旋转输入。
- 可选旋钮按下。
- 可选 RGB LED 或背光。

典型设备：

- N3
- N4 / N4 Pro
- M3
- XL
- K1 Pro
- N1

适合 Agent Deck：

- 旋转切换 Agent、session page、log/detail scroll。
- 旋钮按下表示 select/back/open detail。
- 在非触屏设备上用旋钮做“选择器”，避免把多个 navigation key 固定占掉。
- RGB LED 显示 aggregate status：pending、running、error、quota low、daemon disconnected。

不适合：

- 默认把旋钮旋转映射成 approve/deny。
- 用 LED 作为唯一审批上下文。
- 高风险动作无确认触发。

### `touch_display`

能力：

- 可渲染较大背景或面板。
- 可显示多行文本、状态图、列表、quota、pending request 摘要。
- 可选 touch point、swipe 或 soft key 事件。
- 可作为 Agent Deck 的 logical panel 显示目标：第一批内容包括 quota、tokens、pets、message。
  `message` 用来承载需要用户看到的复杂文字信息，例如审批上下文、host context 或系统提示。

典型设备：

- N4 Pro
- N4
- M3 / XL 的 background surface
- N1 的纵向背景屏

适合 Agent Deck：

- Agent 详情面板。
- quota / token / rate limit 面板。
- pending permission request 摘要与候选动作。
- touch/swipe 切换页、选择选项、展开详情。
- 宠物 / ambient feedback，但不能影响审批状态机。

注意：

- “能写 background”不等于“能读取触点”。M3/XL 等设备在 SDK 中有 background surface，
  但未必有 touch point callback。
- N4 Pro 的 background 写入应继续使用已验证的 `set_frame_background` 路径。
- N4 Pro 下方区域应在产品语义上叫 logical panel / touch bar viewport，而不是 quota panel。
  当前 quota 画到 800x480 background 的底部 viewport，是为了与主按键图层共存并规避多次
  SDK `init()` 清屏；这不妨碍后续把同一逻辑窗口映射到 secondary screen soft-key slot 或
  touch display 的局部 viewport。
- N4 Pro 上 logical panel 的跨面板切换默认由 touch bar 自身的 tap/click 事件承载，避免占用下方
  旋钮。旋钮 1 可用于确认，旋钮 2 可用于面板内滚动，tokens 面板中旋钮 4 可用于切换统计周期；
  具体事件仍应先进入 intent，不直接执行动作。

### `keyboard_companion`

能力：

- 常规键盘输入。
- 少量 LCD key。
- 旋钮和 RGB 背光。

典型设备：

- K1 Pro

适合 Agent Deck：

- 作为 Agent 快捷控制层，而不是完整 dashboard。
- 绑定常用 Agent 启动、当前 Agent 聚焦、模式切换。
- RGB 背光做环境状态提示。

不适合：

- 在键盘本体上承载完整权限审批。
- 默认向未知前台窗口注入文本。

## Agent Deck 功能等级

### Level 0：Observe

说明：

- 只显示状态，不执行动作。
- 包括 Agent 运行状态、最近活动、错误、quota、daemon/hardware 连接状态。

最低硬件：

- `key_grid` 或任意可见状态面。

安全策略：

- 默认允许。
- 文案和 payload 必须脱敏。

### Level 1：Navigate

说明：

- 选择 Agent、切页、过滤、展开详情、返回首页。

最低硬件：

- key press。
- 有 rotary 时优先把滚动/切换交给 rotary。

安全策略：

- 默认允许。
- 只改变 Agent Deck 内部选择状态，不触发系统级操作。

### Level 2：Launch

说明：

- 启动预配置 Agent 应用或命令，例如 Codex CLI、Codex App、Claude Code。

最低硬件：

- key press。

安全策略：

- 仅允许用户配置中的命令。
- 不从硬件事件动态拼 shell。
- 启动命令应有 dry-run / preview。

### Level 3：Focus

说明：

- 把某个已检测到的 Agent 会话带到前台。
- 例如激活 Codex App 窗口、聚焦 terminal tab/window、attach tmux session 或显示可复制命令。

最低硬件：

- key press。
- 更好的体验需要 `touch_display` 或 companion UI 显示目标 host context。

安全策略：

- 必须依赖 host detection 置信度。
- 高置信度可执行 focus；中置信度应打开候选列表；低置信度只能打开 Agent Deck 详情。
- 不得在 focus 失败后向当前前台窗口继续输入。

### Level 4：Decide

说明：

- 对 permission request 做 approve、deny 或选择分支。

最低硬件：

- 推荐 `touch_display`。
- key-only 设备只能做 deny、snooze、open details，不默认做 approve。

安全策略：

- 必须显示足够上下文：Agent、workspace、request type、风险摘要、timeout、默认动作。
- 默认 fail-closed。
- 硬件上 approve 必须是显式配置项，不应因为有按钮就自动开启。
- 对高风险请求可要求双击、长按或先在触屏展开详情。

### Level 5：Input

说明：

- 向 Agent 或前台应用注入文本、发送 prompt、触发 shell-like 动作。

最低硬件：

- 不由硬件能力决定，需要高置信 focus 和明确目标通道。

安全策略：

- 默认关闭。
- 只允许模板化、可预览、可撤销或低风险文本。
- 不向未知前台窗口盲目输入。
- 不允许把硬件事件直接拼接成 shell 命令。

## 推荐产品形态

### N4 Pro：主验证设备

N4 Pro 应继续作为第一主线，因为它同时覆盖：

- 10 个物理主 LCD key。
- 15 个 SDK 逻辑 key slot：10 个主 key + 5 个 secondary screen / soft-key slot。
- 800x480 背景层。
- touch point。
- swipe。
- 4 个旋钮和旋钮按下。
- RGB LED / device config。

推荐默认布局：

- 10 个主 key：Agent / session slots。
- 5 个 secondary soft-key slot：mode、page、focus、deny/snooze、details。
- 逻辑面板 / touch bar viewport：quota、tokens、pets、message。
  当前实现把它合成到 800x480 background 的底部区域；后续 profile-driven renderer 可以按设备能力
  改映射到 secondary screen slot、touch display viewport 或外部 companion UI。
- swipe：切页或切 mode。
- 旋钮：选择 Agent、滚动详情、切换 filter、调节面板视图。
- LED：pending/error/quota/disconnected aggregate status。

### N4：高优先级但先验证

官方和 Companion 文档都表明 N4 有 10 个 LCD keys、4 个 rotary encoders、LCD strip / touch bar
和 swipe。但当前 vendored Python SDK 对 N4 只暴露 14 个 key，没有旋钮或 swipe 解码。

支持策略：

- 不直接复用 N4 Pro profile。
- 先做只读 probe 和 raw input capture。
- 确认当前官方 Device SDK 或 WebSocket SDK 是否能提供完整 N4 事件。
- 若只能拿到 key event，则先降级到 `key_grid_with_status_strip`。

### N3 / K1 Pro：控制型设备

N3 和 K1 Pro 都有少量 LCD key + 旋钮，适合验证非触屏操作模型。

推荐默认布局：

- LCD keys：固定 Agent slots 或 mode actions。
- 旋钮：Agent 选择、滚动、切页。
- 旋钮按下：select / focus / back。
- 背光或 LED：aggregate status。

限制：

- 不默认显示 permission approve。
- 审批请求只显示 pending 并提供 open details、deny、snooze。

### 293S / 293V3：低成本状态面板

推荐默认布局：

- 15 个 key 显示 Agent slots。
- 监控条显示全局状态。
- 一个 key 固定为 page/mode。
- 一个 key 固定为 open details。

限制：

- 不承载复杂决策。
- 刷新频率需要保守，roadmap 已记录旧 293/293s 刷新图像时可能影响按键响应。

### XL / M18 / M3：扩展 dashboard

XL 适合做多 Agent dashboard，因为 key 数多、背景面大；M18/M3 适合在 profile 抽象稳定后作为扩展。

支持策略：

- 先落 `DeviceCapabilityProfile`，再实现型号 profile。
- 对 background GIF / animation 设置明确 fps、脏区域和刷新节流。
- LED 只作为状态提示，不作为唯一决策依据。

## 官方软件与 Agent Deck 的边界

Mirabox 官方 Stream Dock 软件提供场景、拖拽配置、插件市场和应用跟随。Plugin SDK 适合进入官方
软件生态，Device SDK / Python SDK / WebSocket SDK 适合 daemon 直连设备。

Agent Deck 的边界建议：

1. 官方 scene/profile 是入口和用户熟悉的配置层，不是 Agent Deck 的动态状态机。
2. Agent Deck daemon 负责实时 Agent 状态、布局计划、安全决策和硬件输入路由。
3. 如果官方软件运行时会占用 HID 设备，直连 transport 必须提示用户退出官方软件或切换到
   WebSocket / Plugin 路径。
4. 未来可以提供官方 `Agent Deck` scene 模板，但不要依赖 scene switch API 作为核心路径。

## 实现建议

### 1. 引入 `DeviceCapabilityProfile`

建议字段：

```text
device_id
display_name
physical_key_count
logical_key_count
main_key_count
main_key_image_size
main_key_image_format
has_secondary_keys
secondary_key_count
secondary_key_image_size
secondary_key_image_format
background_size
background_format
has_touch_points
has_swipe
rotary_count
has_rotary_press
has_rgb_led
has_keyboard_backlight
supports_key_animation
supports_background_animation
max_recommended_fps
requires_single_session_composite_write
known_limitations
safe_action_levels
```

`safe_action_levels` 不应只由设备决定，还要叠加全局配置和目标 Agent adapter 的安全能力。

现有 `src/agent_deck/hardware/capabilities.py` 的第一版字段仍使用 `key_count`，它应被理解为
SDK logical key count。下一轮代码重构应把它拆成上面的 `physical_key_count`、`logical_key_count`
和 `secondary_key_count`，避免把 N4 Pro 的 10 个物理主按键误读成 15 个物理按钮。

### 2. 让 `LayoutPlan` 按能力降级

同一个 Agent 状态应能生成不同 layout：

- rich touch layout：keys + detail panel + approval panel。
- rotary control layout：keys + rotary navigation + desktop details。
- key grid layout：status slots + open details。
- ambient layout：LED/backlight only。

logical panel 应作为独立于设备 surface 的内容模型进入 layout。第一批 `PanelKind` 固定为：

- `quota`：当前 Codex quota 内容。
- `tokens`：Codex token 消耗情况；当前通过 `ccusage codex daily --compact --json` 读取结构化
  数据，聚合成 today/week/month/all，并以分钟级 TTL cache 避免频繁执行外部命令。
- `pets`：宠物或 ambient 角色呈现。
- `message`：需要用户看到的复杂文字信息，例如审批详情、host context 或系统提示。

### 3. 把输入先归一成 intent

硬件事件不能直接执行动作，应先变成：

```text
SelectAgent
NextAgent
PreviousAgent
OpenDetails
RequestFocus
LaunchConfiguredAgent
DenyRequest
SnoozeRequest
ApproveRequestCandidate
```

`ApproveRequestCandidate` 需要经过 decision policy 再决定是否允许转成真实 approve。

### 4. 单独实现安全 gate

建议最少 gate：

- `display_context_sufficient`
- `host_confidence_sufficient`
- `action_enabled_by_user`
- `request_risk_allowed`
- `freshness_within_timeout`
- `target_still_current`

任何一项失败都应降级为 open details、deny 或 no-op。

## 待验证问题

1. N4 在当前最新 Device SDK / WebSocket SDK 中是否能稳定读到旋钮、touch bar soft key 和 swipe。
2. N4 Pro touch point 坐标在不同固件/官方软件版本下是否稳定。
3. 官方 Stream Dock 软件运行时，Python HID、WebSocket SDK、Plugin SDK 三条路径的设备占用边界。
4. 293/293S 刷新图像时对按键响应的影响和推荐 fps。
5. XL/M3 background GIF/MP4 与 Agent Deck 自己逐帧渲染之间的取舍。
6. RGB LED 在 N4 Pro/M18/XL/K1 Pro 上的真实灯位数量、延迟和可见性。
7. key-only 设备是否需要一个桌面 companion UI 来承载审批详情。

## 推荐路线

1. 保持 N4 Pro 为第一主线，但把代码和文档都命名为 capability-driven，而不是继续扩大
   `n4pro` 特例。
2. 下一步先实现 `DeviceCapabilityProfile` 和 profile-driven layout selection。
3. 先支持三类 profile：`rich_touch_rotary`、`rotary_control`、`key_grid`。
4. 把审批 approve 从 key-only 设备默认能力中移除，只保留 deny/snooze/open details。
5. 做 N4 raw input capture，再决定 N4 是 rich profile 还是降级 profile。
6. 官方 scene/profile 集成继续放在 P4.5，不进入核心运行时依赖。
