# 使用 Agent Deck

**[English](using-agent-deck.en.md)**

本指南面向希望在自己的 Mac 上运行 Agent Deck 的普通用户。它描述当前已验证的路径：
**macOS + MiraBox N4 Pro + Codex**。文中把 `agent-deckd` 简称为“后台服务”；开发者如需了解
设备协议、内部状态和诊断原理，请阅读[开发者 Q&A](../references/developer-q-and-a.md)。

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
| Codex | 显示 Codex 状态和订阅额度，或安装可选集成 | `uv run agent-deckctl codex-detect --enable-integration` |

`ccusage` 只影响 Token/金额统计。没有 Bun 时，配置页、应用/网址按键、订阅额度和硬件显示仍可使用。

## 2. 获取代码并安装依赖

```bash
git clone https://github.com/breakstring/agent-deck.git
cd agent-deck
uv sync --all-groups
```

确认命令行工具可运行：

```bash
uv run agent-deckctl version
uv run agent-deckctl doctor
```

`doctor` 只读取本机环境和硬件接管线索。它不会初始化、清空或刷新真实设备，因此应优先用于第一轮排查。

## 3. 第一次启动：不接管硬件的预览模式

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

### 后台启动脚本

推荐使用 `scripts/agent-deckd-tmux.sh` 管理常驻服务：

```bash
scripts/agent-deckd-tmux.sh start
scripts/agent-deckd-tmux.sh restart
scripts/agent-deckd-tmux.sh status
scripts/agent-deckd-tmux.sh logs
scripts/agent-deckd-tmux.sh attach
scripts/agent-deckd-tmux.sh stop
```

默认服务名称为 `agent-deckd`，配置页地址为 `127.0.0.1:8765`。需要同时运行多套服务时，可在
启动前设置不同名称或端口：

```bash
export AGENT_DECK_TMUX_SESSION=agent-deckd-dev
export AGENT_DECK_HOST=127.0.0.1
export AGENT_DECK_PORT=8765
```

该脚本能让服务在终端窗口关闭后继续运行。如果后台服务本身异常退出，请执行 `restart`。
正常拔插或重启 N4 Pro 时，服务会尝试自动恢复，不需要主动重启。

不使用 tmux 时，可用根目录的 `./run.sh start`，或以前台方式运行：

```bash
uv run agent-deckd --host 127.0.0.1 --port 8765
```

## 4. 真实硬件运行

### 4.1 接管前检查

当前正式支持的真实设备为 MiraBox N4 Pro。开始前：

1. 退出官方 MiraBox/StreamDock 应用，避免它占用设备。
2. 确保没有另一个 Agent Deck 后台服务正在控制同一台设备。
3. 运行只读诊断：

   ```bash
   uv run agent-deckctl doctor --json
   uv run agent-deckctl hardware n4pro status
   ```

这些诊断命令不会改变 N4 Pro 的画面，可以放心优先执行。

### 4.2 macOS SDK 兼容路径

某些 macOS 环境下，Python 包携带的 StreamDock 动态库可能不适配本机。此时下载或检出 MiraBox 官方 Device SDK，并将变量指向其 `Python-SDK` 目录或其 `src` 子目录：

```bash
export AGENT_DECK_STREAMDOCK_SDK_PATH="/absolute/path/to/StreamDock-Device-SDK/Python-SDK"
uv run agent-deckctl doctor
```

该变量只告诉 Agent Deck 从哪里加载官方 SDK。确认诊断正常后，再启动后台服务：

```bash
scripts/agent-deckd-tmux.sh start
```

若后台服务已经运行，请用 `restart` 使新的设置生效：

```bash
scripts/agent-deckd-tmux.sh restart
```

### 4.3 N4 Pro 上的当前行为

- 10 个主按键可以配置为打开或切换应用、打开网址、发送键盘快捷键、显示 Agent 状态、订阅额度、
  用量状态或 Codex 宠物，也可以暂不设定。Codex 宠物按键只负责展示，按下不会执行动作。
- 快捷键支持单个物理键、组合键和最多 16 步的序列；每步释放后可等待 0–2000ms，整条序列最长 10 秒。
- 应用、网址与自定义快捷键图标会缓存到本机，供配置页预览和硬件下发共用；快捷键没有自定义图标时自动显示组合键，Web 自动预览直接使用硬件 renderer 输出的同一张 PNG。
- 4 个旋钮可分别配置旋转动作；音量类动作按下时隐含切换静音/麦克静音，不单独配置按下行为。
- 旋钮灯光是独立设置：关闭，或指定基础色并可开启呼吸效果。设备支持时，音量/亮度类动作会基于基础色反映状态；静音状态使用红色或熄灭。
- 启用 Codex 宠物后，底部虚拟面板按“品牌图 -> 订阅额度 -> 用量 -> 宠物”手动轮换；关闭宠物时
  自动跳过宠物面板。用量可在日、周、月、全部之间切换。待审批 MESSAGE 会临时覆盖当前画面，
  但不修改用户选择的面板，审批结束后会自然恢复。
- N4 Pro 被拔掉、断电或自动重启后，后台服务会持续等待它重新出现，并恢复画面、灯光和输入。
  通常几秒内完成，不需要打开官方 StreamDock App。

### 4.4 键盘快捷键与 macOS 权限

在配置页把一个主按键设为“键盘快捷键”后，点击“开始录制”即可连续按下一个或多个真实组合键；
已有步骤时也可以点击“继续录制”。录制期间按钮会变成“停止并应用”，点击后停止录制，并调用与
页面顶部“保存并应用”完全相同的整机配置保存动作。停止后旁边的“应用到硬件”也是同一个动作，
不是第二套保存机制。纯修饰键步骤仍从手动选择器添加。保存这项 binding 就表示启用它，不另设
全局或逐键开关。

首次执行前，点击配置页中的“请求当前 Agent 权限”。浏览器只是配置界面，不需要辅助功能权限；
真正需要授权的是投递按键事件的 Agent 后台进程。配置页默认只显示紧凑授权状态；悬停、键盘聚焦
或点击“详情”后，才显示当前请求进程、执行文件路径、“打开辅助功能设置”和“重新检查”。daemon
启动和普通状态刷新只做权限 preflight，不会主动弹出授权请求。

当前通过 `scripts/agent-deckd-tmux.sh` 启动属于开发模式，最终执行文件是 Python 运行时。macOS 可能
按启动链把辅助功能条目显示为 Codex、Terminal 或 Python，切换启动方式后也可能需要重新授权；请以
点击请求后系统设置中新出现的执行宿主条目为准，不要给浏览器授权。正式分发计划使用签名的
`Agent Deck.app` 与用户级 Agent 服务，提供稳定、可识别的授权对象。

快捷键始终发送给序列开始时的前台 App，并在整个序列中固定这个 PID；执行结果中的
`succeeded` 只代表事件已投递，不代表目标 App 一定处理了它。同一时间只运行一个序列，执行中
再次按下会返回 `busy`，不会积压旧动作。第一版不支持 `Fn`、媒体键、Caps Lock、文本、鼠标或
shell 混合动作。

如果后台服务退出后需要覆盖设备上残留的画面，可使用以下命令显示品牌启动图：

```bash
uv run agent-deckctl hardware n4pro splash
```

这条命令会改变设备画面。执行前请确认官方 StreamDock App 和其他 Agent Deck 服务已经退出。

## 5. 配置与备份

日常配置请直接打开 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)，修改后点击“保存并应用”。
如需备份或迁移到另一台 Mac，Web 配置页保存的主要文件位于：

- 按键布局：`~/Library/Application Support/AgentDeck/n4pro-key-layout.json`
- 快捷键自定义图标：`~/Library/Application Support/AgentDeck/shortcut-icons/`
- 旋钮与灯光布局：`~/Library/Application Support/AgentDeck/n4pro-rotary-layout.json`
- Codex 订阅额度展示：`~/Library/Application Support/AgentDeck/quota-presentation.json`

仓库中的 [`agent-deck.toml`](../../agent-deck.toml) 是默认设置示例。手动指定配置文件、隔离测试
目录或调整订阅额度展示规则属于高级用法，请参阅[开发者 Q&A](../references/developer-q-and-a.md)
和 [Codex quota 说明](../references/codex-app-server-quota.md)。

## 6. Codex 集成

先获取只读检测报告和手动配置提示：

```bash
uv run agent-deckctl codex-detect --enable-integration
```

安装器默认只预览将要修改的内容，不会写用户配置：

```bash
uv run agent-deckctl codex-install
```

确认输出后，才执行写入操作：

```bash
uv run agent-deckctl codex-install --apply
```

安装器会在修改前创建备份。除非你清楚修改审批方式的影响，否则保持默认设置；默认情况下，
审批仍由 Codex 原生界面处理。

### 6.1 Codex 宠物

Agent Deck 不维护第二套宠物选择。它只读跟随 Codex 配置中的全局 `selected-avatar-id`（兼容旧版
顶层字段和当前 `[desktop]` 字段）：先使用 `CODEX_HOME`，未设置时读取 `~/.codex`。例如 Codex
选中 `custom:rick` 时，Agent Deck 优先从
`pets/rick/pet.json` 加载；旧版 `avatars/rick/avatar.json` 仍兼容，但两个目录并存时以 `pets`
为准。不会把本机宠物素材复制到仓库或通过状态接口返回。

在 [`agent-deck.toml`](../../agent-deck.toml) 中可配置展示与刷新节奏：

```toml
[codex.pet]
enabled = true
refresh_interval_seconds = 5.0
panel_fps = 8
motion = "auto" # auto | full | reduced
```

- `enabled = false` 会禁用动态宠物并从手动面板轮换中移除 PETS；它不会改写 Codex 的宠物选择。
- `refresh_interval_seconds` 控制只读检查 Codex 配置和自定义宠物包的间隔。
- `panel_fps` 是 PETS 面板的最高目标帧率；实际刷新率还受设备传输能力影响。
- `motion = "auto"` 会尽力读取 macOS 的“减弱动态效果”；读取失败时使用完整动画并在诊断中说明。
  `reduced` 固定显示 idle 首帧且不横向移动，`full` 始终播放完整动画。

自定义宠物包必须保持 Codex 图集合同。v1 为 `1536×1872` 的 8×9 图集，v2 为
`1536×2288` 的 8×11 图集，每格都为 `192×208`。首版可解析 v1/v2，但不会使用 v2 的 gaze 行。
渲染会保留完整 cell 坐标，不裁透明边或逐帧重新居中；完全透明像素中的 RGB 残留会归零并记录
warning，但不会因此拒绝 Codex 本身能够加载的素材。
`spritesheetPath` 必须留在当前宠物目录内；绝对路径、`..` 跳转和符号链接逃逸会被拒绝。未知版本
或非法几何不会猜测动作行。相同选择 ID 短暂读取失败时保留最近一次成功结果；如果选择 ID 已改变
却无法加载，则停止展示旧宠物，避免把旧素材冒充为当前选择。

Codex 内置宠物首版不解析 App 内部资源：宠物按键回退到现有静态 Codex 图标，PETS 面板显示简短
诊断。Agent Deck 不重新分发 Codex 内置素材、自定义 Rick 素材或它们的派生资产。

宠物是纯展示层，不会改变审批、任务执行或 Agent slot。全局活动只聚合顶层 Codex 任务，忽略
child agent，并按以下优先级选择最新的触发源：

| 优先级 | Codex 状态 | 展示行为 |
| --- | --- | --- |
| Needs input | `APPROVAL_NEEDED`、`WAITING_USER` | 原地播放 `waiting` 三轮，再进入慢速 idle |
| Blocked | `ERROR` | 原地播放 `failed` 三轮，再进入慢速 idle |
| Ready | `COMPLETED_RECENTLY` | 原地播放 `review` 三轮，再进入慢速 idle |
| Running | `THINKING`、`RUNNING_TOOL` | 宠物按键播放 `running`，PETS 面板持续左右巡游 |
| Idle | 无活跃任务、idle 或 offline | 保持可见，慢速 idle 并间歇散步 |

`Ready` 暂时以 `COMPLETED_RECENTLY` 近似，因为现有状态源没有可靠的“未读”字段。相同状态不会
反复触发三轮反应，只有新的状态时间戳才会重新播放。

N4 Pro 的宠物按键使用 `112×112` 深色画布，完整 cell 等比缩放且不会横向移动。PETS 使用现有
`800×136` 虚拟面板：运行中约 15 秒完成一次全宽左右往返；idle 使用确定性的 45 秒周期，约
30 秒驻留后用 15 秒走向另一端，下个周期反向。等待、失败和完成反应会冻结当前位置，切换状态
不会把宠物瞬移回起点。待审批 MESSAGE 仍具有最高显示优先级，但只是临时覆盖；它不会把人工选择
的 PETS、Quota 或其他面板改写成 MESSAGE。

运行 `uv run agent-deckctl status` 或读取 `/status` 时，可在 `codex_pet` 中查看启用状态、
`selected-avatar-id`、解析结果、名称、图集版本、全局 activity、motion 模式、更新时间、素材错误
和独立的 motion 降级诊断。
诊断不会包含原始图片或完整图集。

首版不支持 Agent Deck 单独选宠物、多宠物、按会话绑定、宠物按键交互、v2 gaze/鼠标跟踪，也不
替换现有 Agent 状态按键。

#### 真机验收清单

以下是发布前必须重新执行的验收步骤，不代表当前版本已经通过真机测试：

1. 使用 Codex 当前选择的 Rick，同时配置一个“Codex 宠物”按键并手动切到 PETS 面板。
2. 先做 60 秒状态 smoke，再做 15 分钟 soak；确认约 15 秒完成一次全宽往返，且无裁切和鬼影。
3. 测量 PETS 背景有效刷新率，目标不低于约 7 FPS；历史约 9.56–9.57 FPS 只能作为旧基线。
4. 确认单次连接期间 `open/init=1`，没有非预期重连、HID 错误、CPU 或线程数持续增长。
5. 结束时显式关闭设备会话与后台服务，确认没有遗留 `agent-deckd` 进程。

## 7. Token、金额与订阅额度

- 后台服务启动后会自动更新订阅额度和 Token/金额信息。
- Token/金额趋势需要 Bun。先运行 `bunx --version`；没有 Bun 或使用历史太少时，趋势可能为空。
- 网络异常、Codex 未登录或版本变化可能让订阅额度暂时不可用。通常等待下一次更新即可。

## 8. 常见问题

### 配置页可访问，但 N4 Pro 没有更新

先确认后台服务正在运行：

```bash
scripts/agent-deckd-tmux.sh status
uv run agent-deckctl status
```

然后退出官方 MiraBox/StreamDock 应用，确认没有重复启动 Agent Deck，再执行
`uv run agent-deckctl doctor`。如果启动命令包含 `--disable-hardware-renderer`，请重启时移除它。

### `streamdock` 在 macOS 上加载失败

按照[真实硬件运行](#真实硬件运行)中的方法设置 `AGENT_DECK_STREAMDOCK_SDK_PATH`，然后重新
执行 `uv run agent-deckctl doctor`。

### macOS 权限不足导致“写入成功但仍是品牌图”

如果从 Codex Desktop App 启动或调试，请把运行策略设为 **Full Access**，然后完整退出并重启
Codex App。若仍提示 `not permitted`，请打开“系统设置 -> 隐私与安全性 -> 输入监控”，允许
Codex 或实际启动 Agent Deck 的终端访问设备，然后重启对应应用。

### 快捷键显示“需要权限”或没有触发前台 App

先在配置页点击“请求权限”，再到“系统设置 -> 隐私与安全性 -> 辅助功能”允许实际运行 daemon
的应用。授权后完整重启该终端或应用，再刷新配置页。触发前确认目标 App 已经在前台；Agent Deck
会固定按下瞬间的前台 App，不会根据快捷键名称猜测目标窗口。可在 `uv run agent-deckctl status`
对应的 daemon 状态或 `/status` 的 `keyboard_shortcuts` 中查看 capability、active 和 recent job。

### N4 Pro 重启或重新插入后没有恢复

先查看服务状态和日志：

```bash
uv run agent-deckctl status
scripts/agent-deckd-tmux.sh logs
```

正常情况下，重新插入后等待几秒即可恢复，不需要重启服务。如果一直只有品牌图：

1. 按上一节检查 Codex 或终端的 macOS 权限。
2. 执行 `scripts/agent-deckd-tmux.sh restart`。
3. 仍未恢复时，停止 Agent Deck，再打开官方 StreamDock App，确认它能识别设备。
4. 完全退出官方 App 后重新启动 Agent Deck，避免两个程序同时控制设备。

反馈问题时请附上日志、固件版本，以及设备是直连 Mac 还是通过 USB Hub。实现原理和状态字段见
[开发者 Q&A](../references/developer-q-and-a.md)。

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

停止已知的 Agent Deck 后台服务再启动，或设置不同的 `AGENT_DECK_PORT`。

## 9. 获取帮助与报告问题

报告问题时请提供：macOS 版本、设备型号、是否打开过官方软件、启动方式、
`agent-deckctl doctor` 的输出以及相关日志。不要粘贴 API key、token、完整对话内容或其他隐私信息。

需要修改代码或补充硬件适配时，请阅读[贡献指南](../../CONTRIBUTING.zh-CN.md)。
