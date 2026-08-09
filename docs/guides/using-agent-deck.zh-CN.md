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
- 当 App 目标是当前 `ChatGPT.app` 或历史 `Codex.app` 时，配置页还会显示“任务活跃时显示宠物”。
  这是 App 键原图标之上的可选覆盖层：按下仍只打开或切换 App，空闲后恢复原 App 图标。
- 快捷键支持单个物理键、组合键和最多 16 步的序列；每步释放后可等待 0–2000ms，整条序列最长 10 秒。
- 应用、网址与自定义快捷键图标会缓存到本机，供配置页预览和硬件下发共用；快捷键没有自定义图标时自动显示组合键，Web 自动预览直接使用硬件 renderer 输出的同一张 PNG。
- 4 个旋钮可分别配置旋转动作；音量类动作按下时隐含切换静音/麦克静音，不单独配置按下行为。
- 旋钮灯光是独立设置：关闭，或指定基础色并可开启呼吸效果。设备支持时，音量/亮度类动作会基于基础色反映状态；静音状态使用红色或熄灭。
- 启用 Codex 宠物后，底部虚拟面板按“品牌图 -> 订阅额度 -> 用量 -> 宠物”手动轮换；关闭宠物时
  自动跳过宠物面板。用量可在日、周、月、全部之间切换。待审批 MESSAGE 会临时覆盖当前画面，
  但不修改用户选择的面板，审批结束后会自然恢复。
- PETS 面板会把本机和已启用 SSH Remote Connection 中活动的顶层 ChatGPT 任务显示为独立宠物；
  点击 Web 设备预览中的 PETS touch bar，可以设置远端宠物来源和慢/中/快三档巡游速度。
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
- PETS 面板偏好：`~/Library/Application Support/AgentDeck/n4pro-pets-panel.json`

仓库中的 [`agent-deck.toml`](../../agent-deck.toml) 是默认设置示例。手动指定配置文件、隔离测试
目录或调整订阅额度展示规则属于高级用法，请参阅[开发者 Q&A](../references/developer-q-and-a.md)
和 [Codex quota 说明](../references/codex-app-server-quota.md)。

### 5.1 日志级别与文件上限

个人常驻使用默认只记录 `warning`、`error` 和 `critical`，并关闭每次 HTTP 请求都会产生的
access log。日志文件按大小轮转：当前文件达到 5 MiB 后轮转，默认保留 2 份历史文件，因此总占用
通常不超过约 15 MiB。配置位于 `agent-deck.toml`：

```toml
[logging]
level = "warning" # critical | error | warning | info | debug | trace
access_log = false
file_enabled = true
file_path = "~/Library/Logs/AgentDeck/agent-deckd.log"
max_bytes = 5242880
backup_count = 2
```

- 日常使用建议保持 `warning` 和 `access_log = false`；普通状态轮询与成功请求不会写入日志。
- 临时排障可执行 `uv run agent-deckd --log-level info`，或在配置中开启 access log；排障后应恢复
  默认值。
- `file_enabled = false` 可完全关闭文件日志，错误仍会输出到前台或 tmux 控制台。
- `./run.sh logs` 跟随轮转日志文件；`AGENT_DECK_LOG_FILE` 仍可临时覆盖后台脚本使用的路径。
- 修改配置后需要重启 daemon 才会生效。

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

宠物系统包含三种独立展示，启用其中一种不会替换另外两种：

1. 把一个主按键设为“Codex 宠物”，常驻展示全局 Codex 活动；这是纯展示键，按下不执行动作。
2. 把一个主按键设为 ChatGPT/Codex App，并开启“任务活跃时显示宠物”；宠物只临时覆盖 App 图标，
   按键仍然打开或聚焦 App。
3. 手动把底部逻辑面板切到 PETS；本机和远端处于活动或完成反馈状态的每个顶层 ChatGPT 任务会
   成为独立巡游角色。

最快启用方式是在 Web 配置页选择一个主按键，设为“Codex 宠物”或 ChatGPT App 启动键，再点击
N4 Pro 预览中的 touch bar 打开 PETS 设置。点击“保存并应用”后，用以下命令确认素材、角色和
面板策略：

```bash
uv run agent-deckctl status
```

Agent Deck 不维护第二套本机宠物选择。它只读跟随 Codex 配置中的全局 `selected-avatar-id`（兼容
旧版顶层字段和当前 `[desktop]` 字段）：先使用 `CODEX_HOME`，未设置时读取 `~/.codex`。例如
Codex 选中 `custom:rick` 时，Agent Deck 优先从 `pets/rick/pet.json` 加载；旧版
`avatars/rick/avatar.json` 仍兼容，但两个目录并存时以 `pets` 为准。

在 [`agent-deck.toml`](../../agent-deck.toml) 中可配置展示与刷新节奏：

```toml
[codex.pet]
enabled = true
refresh_interval_seconds = 5.0
panel_fps = 8
motion = "auto" # auto | full | reduced
remote_pet_source = "builtin_random"
patrol_speed = "medium"
```

- `enabled = false` 会禁用动态宠物并从手动面板轮换中移除 PETS；它不会改写 Codex 的宠物选择。
- `refresh_interval_seconds` 控制只读检查 Codex 配置和自定义宠物包的间隔。
- `panel_fps` 是 PETS 面板的最高目标帧率；实际刷新率还受设备传输能力影响。
- `motion = "auto"` 会尽力读取 macOS 的“减弱动态效果”；读取失败时使用完整动画并在诊断中说明。
  `reduced` 固定显示 idle 首帧且不横向移动，`full` 始终播放完整动画。
- `remote_pet_source` 和 `patrol_speed` 是首次启动时的 PETS 面板默认值；用户在 Web 设备预览中
  保存的设置会写入 `n4pro-pets-panel.json`，以后优先于这里的默认值。

自定义宠物包必须保持 Codex 图集合同。v1 为 `1536×1872` 的 8×9 图集，v2 为
`1536×2288` 的 8×11 图集，每格都为 `192×208`。首版可解析 v1/v2，但不会使用 v2 的 gaze 行。
渲染会保留完整 cell 坐标，不裁透明边或逐帧重新居中；完全透明像素中的 RGB 残留会归零并记录
warning，但不会因此拒绝 Codex 本身能够加载的素材。
`spritesheetPath` 必须留在当前宠物目录内；绝对路径、`..` 跳转和符号链接逃逸会被拒绝。未知版本
或非法几何不会猜测动作行。相同选择 ID 短暂读取失败时保留最近一次成功结果；如果选择 ID 已改变
却无法加载，则停止展示旧宠物，避免把旧素材冒充为当前选择。

已安装 ChatGPT/Codex App 的内置宠物会从已知 App bundle 的 `app.asar` header 按需只读发现，
只有当前角色实际使用某只宠物时才按精确 offset 解码图集到内存。Agent Deck 不遍历其他 App、
不把内置素材解包到磁盘或仓库，也不重新分发内置宠物、自定义 Rick 或它们的派生资产。App 不存在、
资源合同不兼容或自定义包加载失败时，相关展示安全降级，并在状态诊断中说明原因。

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

#### ChatGPT/Codex App 启动键的任务态覆盖

App 启动键的覆盖规则独立于上面的常驻“Codex 宠物”键和 PETS 面板。配置页按 OpenAI bundle id
或明确的 `ChatGPT.app`/`Codex.app` 路径识别目标，不会仅凭显示名把普通同名 App 绑定进来。
开关保存后，App 动作、用户原图标和宠物覆盖声明一起写入 version 3 按键布局；version 1/2 布局
读入时默认不开启覆盖。

覆盖层只聚合带 `codex-app:*`（以及旧兼容 App target）的顶层 Codex Desktop task，排除 Codex CLI、
child agent 和其他 App。多个任务按 `Needs input > Error > Review > Running > Completed` 聚合，
同级选择最新状态。当前没有显式 `REVIEW_NEEDED` 信号，因此普通完成不会被推断为常驻 review。

| App 任务状态 | App Key 覆盖行为 |
| --- | --- |
| 无匹配任务、`IDLE`、`OFFLINE` | 显示用户原 App 图标 |
| `THINKING`、`RUNNING_TOOL` | 持续播放 `running` |
| `WAITING_USER`、`APPROVAL_NEEDED` | 持续播放 `waiting`，直到状态解除 |
| `ERROR` | 持续播放 `failed`，直到状态解除 |
| `COMPLETED_RECENTLY` | 播放三轮 `waving`，末帧保持 5 秒，再恢复原 App 图标 |
| 未来显式 `REVIEW_NEEDED` | 持续播放 `review`，直到状态解除 |

任意多个 ChatGPT/Codex App 键可以同时开启关联，它们共享图集解析和预渲染帧缓存。默认总写屏预算
是每秒 10 次，每个动态键至少 5 FPS，所以最多两个键同时动画；更多活跃键显示对应状态的静态帧。
同级优先最近按下的键，再按物理索引排序。按下只改变后续动态槽排序，不确认或消除任务提醒。
`motion=reduced` 时所有关联键使用对应状态的静态帧，但完成反馈仍按相同时间恢复。

宠物全局关闭、未选择可解析的自定义宠物、图集加载失败或缓存缺帧时，覆盖层完全退出并保留原 App
图标，App 按键动作仍可执行。`/status.codex_pet.app_overlay` 会报告关联、可见、动态、静态降级键数，
有效目标 FPS 以及 10 次/秒预算。Codex CLI 启动不在本能力范围内；终端宿主与展示客户端会单独设计。

#### PETS 多任务巡游

PETS 使用现有 `800×136` 虚拟面板，但不会把所有任务压成一个全局状态。每个本机或远端顶层
`codex-app:*` 任务只在活动或完成反馈期间成为独立角色，拥有由 agent identity 稳定派生的位置、
方向、动画相位和基础速度。角色共享完整横向空间，速度会做轻微、错相的周期变化；碰撞只在短窗口
内反弹，其余时间允许穿过，避免宠物长期挤在固定领地。本机角色不画地垫，远端角色按 observer host
使用稳定、低饱和光环；光环只表示执行主机，不表示成功或失败。

点击 Web 配置页 N4 Pro 预览中的 PETS touch bar，可以保存：

- **远端宠物来源**：`follow_local` 跟随本机选择；`remote_config` 只读远端 Codex 选择；
  `builtin_random` 按任务稳定分配本机已安装 ChatGPT/Codex App 中的内置宠物，且不读取远端配置。
- **巡游速度**：`slow`、`medium`、`fast`。切换速度不会把现有角色瞬移回起点。

本机角色优先使用当前 Codex 选择。`remote_config` 只对 ChatGPT Settings 中 managed 且
auto-connect 为 `true` 的 SSH connection 增加一次最小 `config/read` 投影，并只保留
`selected-avatar-id`。远端选择是本机可识别内置宠物时，复用本机 App 里的同名资源；远端
`custom:<name>` 只通过短生命周期系统 SFTP 镜像 manifest 和其中声明的单张图集到 Agent Deck
自己的内容寻址缓存。它不会读取完整远端配置、执行宠物代码、复制整个目录、写入远端或改动本机
Codex 目录。未知、过大、符号链接、越界路径或校验失败的包会尝试回退到稳定内置宠物，不会冒充
旧选择。

N4 Pro 的独立宠物按键仍使用 `112×112` 画布，完整 cell 等比缩放且不会横向移动。待审批 MESSAGE
在 PETS 上具有最高显示优先级，但只是临时覆盖；它不会把人工选择的 PETS、Quota 或其他面板改写
成 MESSAGE。

运行 `uv run agent-deckctl status` 或读取 `/status` 时，可在 `codex_pet` 中查看启用状态、
`selected-avatar-id`、解析结果、名称、图集版本、全局 activity、motion 模式、更新时间、素材错误
和独立的 motion 降级诊断；`app_overlay` 提供 App Key 调度诊断，`panel_colony` 提供角色数、
远端角色数、素材分配、来源策略、速度、碰撞计数、内置 catalog 和远端 custom 缓存状态。诊断不会
包含原始图片、完整图集、完整远端配置或 prompt。

当前不支持 Agent Deck 上传独立宠物包、让用户逐任务手选宠物、宠物按键交互、hover/jump、
v2 gaze 或鼠标跟踪，也不替换现有 Agent 状态按键。Codex CLI 任务不会进入 ChatGPT App 的
PETS 角色；终端宿主与展示客户端需要单独建模。

### 6.2 远端 ChatGPT App 任务状态（SSH）

ChatGPT App 的 [Remote Connections](https://learn.chatgpt.com/docs/remote-connections.md)
可以通过 SSH 在另一台电脑运行 Codex。Agent Deck 默认启用这一类连接的只读观察：
它为每个 host 创建自己的 SSH 子进程，执行固定的 `codex app-server proxy`，再按
[Codex app-server](https://learn.chatgpt.com/docs/app-server.md) 合同只调用
`initialize`、`initialized` 和 `thread/list(useStateDbOnly=true)`；只有 PETS 选择
`remote_config` 时才增加只读 `config/read`，且立即只投影宠物选择 ID。这条连接和 ChatGPT App
自己的连接彼此独立；Agent Deck 不读取或复用 App 的 socket 文件描述符，也不调用
`thread/resume`、`thread/start`、`turn/start`、interrupt、archive 等改变远端状态的方法。

先用诊断命令确认 SSH alias、远端 Codex 路径和共享 app-server 可达：

```bash
uv run agent-deckctl codex-remote-state \
  --host minibox.example \
  --timeout-seconds 10 \
  --limit 80
```

输出只含 host 摘要、cwd、可选 thread name、更新时间和粗粒度状态计数。app-server 返回的
`preview`（通常来自首条 prompt）、turn、item、rollout path 和原始响应会在适配器内立即丢弃，
不会进入 daemon 状态、日志或 `/status`。

确认诊断成功后，只需在 ChatGPT 的 **Settings → Connections** 中添加并启用需要观察的
SSH Connection。Agent Deck 的远端观察总开关默认启用；如需调整轮询参数，可在本机
`agent-deck.toml` 显式配置：

```toml
[codex.remote_ssh]
enabled = true
poll_interval_seconds = 5.0
timeout_seconds = 10.0
thread_limit = 80
stale_after_seconds = 20.0
completed_feedback_seconds = 10.0
```

Agent Deck 每轮只读取 ChatGPT 自己保存的 managed connections，并且仅接受
`remote-connection-auto-connect-by-host-id` 中值**严格为 `true`** 的 SSH 项。它不会读取
`~/.ssh/config` 来发现或扩展主机，不会把历史 `remote-projects`、当前 selected host 或
auto-connect 为 `false`/缺失的记录当作授权。用户在 Settings 中关闭 Connection 后，daemon
会动态关闭自己的 observer 并清理旧状态；设置文件缺失或结构无法确认时同样 fail-closed。

如需完全关闭 Agent Deck 的远端观察，可设置 `enabled = false` 并重启后台服务。多个已启用
host 会各自复用一条独立连接并并行轮询；本地和远端
thread 即使 ID 相同也使用不同的 host-aware agent identity。只消费
`sourceKinds=["vscode"]` 的顶层 thread，
因此 Codex CLI、exec 和 child/subagent 不会触发 App Key 宠物覆盖。状态映射如下：

| 远端 `ThreadStatus` | Agent Deck |
| --- | --- |
| `active + waitingOnApproval` | `APPROVAL_NEEDED` |
| `active + waitingOnUserInput` | `WAITING_USER` |
| `active`（无等待 flag） | `THINKING`（宠物显示 running） |
| `systemError` | `ERROR` |
| `active -> idle` | 本机短暂 `COMPLETED_RECENTLY`，窗口后恢复原图标 |
| 冷启动 `idle`、`notLoaded` | 不覆盖原图标 |

这些远端顶层任务同时也是 PETS 面板的独立角色。切换 PETS 的远端宠物来源只改变角色素材如何
分配，不改变 SSH host 授权、任务状态轮询、App Key 覆盖或远端任务本身。

app-server 当前粗粒度状态不能区分 thinking 与 running tool，也没有显式 review/未读信号，因此
Agent Deck 不猜测 `RUNNING_TOOL` 或常驻 review。连接持续失败超过 `stale_after_seconds` 后会清理
该 host 的旧活动状态并恢复 App Key 原图标；按键原有的“打开或聚焦 ChatGPT App”动作不受影响。
`/status.pollers.codex_remote_ssh` 会给出 Settings 发现计数、各 host 的成功时间、短错误类型、
状态计数和关联 agent 数。

OpenAI 中转型 Remote Connections 目前没有供第三方读取远端 thread/status 的公开接口，本版本不
逆向或接入该私有链路；这里只支持用户已经能从本机 `ssh <host>` 访问的 SSH Remote。

#### 真机验收清单

以下是发布前必须重新执行的验收步骤，不代表当前版本已经通过真机测试：

1. 使用 Codex 当前选择的 Rick，同时配置一个“Codex 宠物”按键并手动切到 PETS 面板。
2. 同时启动多个本机任务和至少一个已授权 SSH Remote 任务，确认角色独立、远端光环稳定，
   三种远端来源策略和慢/中/快三档均能保存并应用。
3. 先做 60 秒状态 smoke，再做 15 分钟 soak；确认角色持续巡游，且无裁切、鬼影或固定领地拥挤。
4. 测量 PETS 背景有效刷新率，目标不低于约 7 FPS；单宠物首版 901 秒 soak 的约 7.88 FPS 只作
   旧基线，不能替代当前多角色实现的实测。
5. 确认单次连接期间 `open/init=1`，没有非预期重连、HID 错误、CPU 或线程数持续增长。
6. 结束时显式关闭设备会话与后台服务，确认没有遗留 `agent-deckd` 进程。
7. 另配置 1、2、3 个 ChatGPT/Codex App 关联键，分别测量动态键有效 FPS、静态降级、按键响应、
   状态退出后的原图标恢复与 HID 错误；自动化测试通过不等于这项真机验收已完成。

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
