# Agent Deck 开发者 Q&A

本文面向维护 Agent Deck、排查真实硬件或扩展设备适配的开发者。普通安装、启动和配置请先阅读
[使用指南](../guides/using-agent-deck.zh-CN.md)；使用指南刻意不解释底层函数、协议和状态字段。

## 运行结构

### Q：daemon、renderer 和 tmux 分别是什么？

- daemon 是 `agent-deckd` 后台服务，负责接收 Agent 状态、生成布局并协调硬件输入输出。
- renderer 是 daemon 内负责把布局写到具体硬件的组件；N4 Pro 使用常驻设备会话，避免多个写入
  路径互相清屏。
- tmux 只负责让 daemon 在终端关闭后继续运行。它不是系统级进程守护器；daemon 自身退出后，
  当前脚本不会自动创建新进程。

用户指南将它们统一称为“后台服务”，只有命令或诊断需要时才保留原名。

### Q：为什么安全诊断能识别设备，却不应初始化设备？

`streamdock_probe.py` 只允许短暂 open/read/close，用来读取设备和占用线索。SDK 的 `init()` 会
唤醒屏幕、设置亮度、清空图标并刷新设备，已经属于写操作；N4 Pro 的 `HAN` 握手也会改变设备
控制模式。因此只读 `doctor`/probe 不得调用 `init()` 或握手。真实接管只能由明确的 renderer 或
硬件写入命令执行。

## N4 Pro 连接与恢复

### Q：普通拔插和设备重启如何恢复？

`StreamDockN4ProPersistentAnimator` 每轮都会重新枚举设备。只有 USB path 相同且当前 transport
没有明确失效时，才复用现有会话。设备消失、path 变化、同 path 句柄不可写，或者背景、按键、
刷新返回非零错误码时，renderer 会执行 `close(notify=False)`，丢弃旧会话。

后续轮次发现新设备时，恢复顺序是：

1. 对新枚举 path 发送 N4 Pro 连接握手。
2. 建立常驻 open/init 会话。
3. 重新注册按键、旋钮和触控回调。
4. 恢复亮度、灯光、背景和按键图。

默认渲染周期约为 3 秒，因此普通重连通常在数秒内恢复，无需重启 daemon。

### Q：为什么运行几小时后旋钮、按键和管理页面会一起变慢？

官方 SDK 的 `DeviceManager.enumerate()` 每次都会创建新的 device wrapper，而 device 构造函数会
立即启动一个 GIF 后台线程，即使该 wrapper 只用于读取 USB path、从未 open。如果 persistent
renderer 复用已有会话，却不关闭本轮新枚举出的同 path wrapper，就会按渲染周期持续泄漏线程；
累积到上千个线程后，输入回调、HTTP API 和图片渲染会一起争抢 CPU/GIL。

`StreamDockN4ProPersistentAnimator` 必须保留每轮枚举以探测拔插和原地重启，但对以下对象立即
执行 `close(notify=False)`：

- 与活动会话 path 相同、仅用于健康比较的新 wrapper。
- 枚举结果中未被选中的其他 wrapper。
- 握手或 open 失败、不会进入活动会话的 wrapper。

排查时不能只看输入事件计数；如果 `/status` 也超时、进程 CPU 异常升高，可用
`ps -M -p <pid> | wc -l` 查看线程数量。修复代码后必须重启旧 daemon，因为已经泄漏的 SDK
线程无法在原进程内统一回收。

### Q：为什么设备会只显示品牌图？

N4 Pro 收到 SDK 的断开命令或进入某些设备侧状态后，会显示品牌图。只调用 Python SDK 的
`init()`、`send_config()`、背景写入和 `refresh()`，可能全部返回成功但无法退出这个状态。

通过分析官方 StreamDock 程序并在固件 `V4.N4 Pro.02.010` 上真机验证，恢复还需要一个完整的
1025 字节输出报告：

```text
byte 0: report id 0
byte 1..3: ASCII "HAN"
remaining bytes: 0
```

vendored SDK 的 `LibUSBHIDAPI.send_handshake()` 负责短暂打开 raw HID path、完整写入报告并立即
关闭；`StreamDockN4Pro.send_handshake()` 固定 N4 Pro 参数。persistent renderer 只在首次接管
和每次重连前调用它，不会在稳定会话中重复握手。安全 probe 不调用它。

### Q：为什么拔插有时恢复，有时仍是品牌图？

普通工作状态下拔插会触发重新枚举、open/init 和完整重绘，早期实测可以恢复。但设备已经处于
断开/品牌图控制模式时，单纯拔插并不能保证解除该模式；必须在新枚举设备上发送 `HAN` 握手。
因此“设备重新出现”与“设备已接受显示控制”是两个不同条件。

### Q：必须依赖官方 StreamDock App 复位吗？

不需要。当前 renderer 已直接实现并真机验证连接握手。官方 App 只作为后备诊断工具：当握手
明确成功但设备仍不更新时，可用它确认固件和设备侧配置是否正常。官方 App 与 Agent Deck 不应
同时接管同一设备。

## macOS 权限与假成功

### Q：为什么 `open=True`、`can_write=True`、写入返回 0，屏幕却没有变化？

vendored native transport 的 `transport_create()` 会先创建 C++ 会话对象，真实 HID 句柄可能稍后
才打开。macOS 拒绝 HID 访问时，上层仍可能得到以下假成功：

- `device.open()` 返回 True。
- `transport.can_write()` 返回 True。
- `wakeup`、背景、按键和 `refresh` 返回 0。
- 真机仍只有品牌图。

此时 `transport.get_last_error()` 的实际证据是 `[HID] Device not open`，直接
`hid_open_path()` 会返回 `not permitted`。因此这些函数返回值不能单独证明真实硬件写入成功。

新的握手路径在常规 open/init 前同步打开 HID 并完成 1025 字节写入。权限不足会直接产生
`handshake failed ... not permitted`，renderer 不再继续记录假成功。

### Q：从 Codex Desktop 调试真实硬件需要什么权限？

1. 将 Codex App 的运行策略设为 **Full Access**。
2. 完整退出并重启 Codex App，让新策略作用于进程。
3. 如果仍报 `not permitted`，检查“系统设置 -> 隐私与安全性 -> 输入监控”中 Codex 或实际启动
   daemon 的终端是否获准访问，并重启相关应用。

只修改运行策略但不重启，旧进程仍可能沿用受限权限。普通用户从自己的终端启动 daemon 时，
权限主体是该终端应用，不是编辑代码所用的工具。

## 状态字段与诊断

### Q：如何从 `/status` 判断握手和重连是否发生？

关注 `streamdock_n4pro_renderer.last_result`：

| 字段 | 含义 |
| --- | --- |
| `ok` | 最近一轮完整写入是否成功；不能替代真机观察 |
| `path` | 最近使用的设备枚举 path |
| `error` / `last_error` | 握手、open/init 或显示写入错误 |
| `timing_seconds.device_handshaken` | 最近一轮是否新执行握手，1 表示是 |
| `timing_seconds.device_handshake_count` | 当前 daemon 生命周期累计成功握手次数 |
| `timing_seconds.device_reconnected` | 最近一轮是否被判定为重连 |
| `timing_seconds.device_reconnect_count` | 当前 daemon 生命周期累计重连次数 |

稳定会话的最后一轮通常显示 `device_handshaken=0`，因为握手只在首次连接执行；累计字段应保持
大于等于 1。真机验证仍必须确认屏幕和输入实际恢复。

### Q：遇到品牌图时如何区分权限、程序和硬件问题？

| 证据 | 优先判断 |
| --- | --- |
| `handshake failed` 且包含 `not permitted` | Codex/终端的 macOS 设备访问权限不足 |
| `device_handshake_count=0` | 当前 daemon 未走到握手，检查 SDK 版本、renderer 是否启用及日志 |
| 握手成功、真机恢复 | 固件/硬件基本正常，问题在连接模式或旧软件流程 |
| 握手成功但仍是品牌图 | 收集日志、path、固件、USB/Hub 拓扑，再用官方 App 做后备诊断 |
| 设备消失后始终无法重新枚举 | 检查 USB 连接、Hub、供电或设备本身 |
| 官方 App 和 Agent Deck 同时运行 | 先消除双重占用，再判断其他原因 |

## 高级配置与 Codex 集成

### Q：后台服务按什么顺序查找主配置？

配置查找顺序是：

1. `agent-deckd --config /path/to/config.toml` 显式路径。
2. `AGENT_DECK_CONFIG` 环境变量。
3. 当前工作目录中的 `agent-deck.toml`。
4. `~/Library/Application Support/AgentDeck/config.toml`。

测试或多配置场景可使用下列环境变量隔离 Web 配置文件：

```bash
export AGENT_DECK_N4PRO_KEY_LAYOUT="/path/to/key-layout.json"
export AGENT_DECK_N4PRO_ROTARY_LAYOUT="/path/to/rotary-layout.json"
export AGENT_DECK_QUOTA_PRESENTATION="/path/to/quota-presentation.json"
```

### Q：如何手动调整 Codex 额度标签、顺序和可见性？

`quota-presentation.json` 当前不由 Web 配置页编辑。规则按 app-server 的 `limit_id` 匹配，不按
`primary` / `secondary` 槽位匹配；未知的新 limit 默认保持可见。示例：

```json
{
  "version": 1,
  "presentation": {
    "unmatched_visible": true,
    "rules": [
      { "limit_id": "codex", "label": "Codex", "visible": true, "order": 0 },
      { "limit_id": "codex_bengalfox", "label": "Spark", "visible": true, "order": 10 }
    ]
  }
}
```

通过 `GET /status` 的 `codex_quota.snapshot.windows` 查当前 `limit_id`；`display_snapshot` 是策略
筛选后的硬件展示集合。修改文件后重启 daemon。数据来源、缓存与兼容边界见
[Codex quota 说明](codex-app-server-quota.md)。

### Q：Codex hook 的失败语义是什么？

- `agent-deck-codex-hook notify` 是 best-effort；daemon 不可用时可写 stderr，但应保持 exit 0，
  不能影响 Codex 正常流程。
- `permission-request` 必须 fail-closed；daemon 不可用、响应非法或等待超时时返回 deny。
- 默认 `codex.permission_request.mode = "passthrough"`，审批仍由 Codex 原生界面处理。

普通用户不需要理解或修改这些合同；安装器默认先展示 dry-run，只有显式 `--apply` 才写配置。

## 开发与验证边界

### Q：自动化测试为什么不能访问真实 N4 Pro？

真实 HID 写入会改变用户设备、清空画面并占用官方软件会话，结果也依赖本机权限和 USB 状态。
自动化测试必须注入 fake manager/device，覆盖首次握手、权限失败、path 变化、同 path stale handle、
设备消失/返回、native 非零错误码和完整重绘。真实设备只作为显式 smoke/manual 步骤。

### Q：一次完整的真机重连 smoke 应验证什么？

1. 退出官方 StreamDock App，确保只有一个 Agent Deck daemon。
2. 启动 daemon，确认实体屏幕显示 Agent Deck，而不只看 status。
3. 记录当前 path、`device_handshake_count` 和 `device_reconnect_count`。
4. 拔掉设备，等待 8–10 秒后插回原 USB 口，不重启 daemon。
5. 确认 path 重新枚举、累计重连/握手字段更新、屏幕与输入均自动恢复。

制造断开/品牌图状态属于真实硬件写操作，不应放进普通诊断命令或自动测试。只有用户明确同意
真机 smoke 时，才可使用 SDK 的 `disconnected()` 做受控复现；结束后必须确认自动握手恢复。

## 相关代码与文档

- `src/agent_deck/hardware/streamdock_n4pro.py`：persistent renderer、会话健康检查和重连诊断。
- `vendor/streamdock-python-sdk/src/StreamDock/Transport/LibUSBHIDAPI.py`：native 返回码与 raw HID 握手。
- `vendor/streamdock-python-sdk/src/StreamDock/Devices/StreamDockN4Pro.py`：N4 Pro 固定握手参数。
- `src/agent_deck/hardware/streamdock_probe.py`：安全只读探针边界。
- [MVP 设计](../superpowers/specs/2026-06-12-agent-deck-mvp-design.md)：整体分层和真实硬件所有权。
- [长期 roadmap](agent-deck-roadmap.md)：当前实现状态与后续设备扩展。
