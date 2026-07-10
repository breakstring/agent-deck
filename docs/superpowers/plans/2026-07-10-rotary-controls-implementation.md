# Agent Deck Rotary Controls Implementation Plan

> **Execution status (2026-07-10):** All tasks below have been implemented and verified. The remaining acceptance step is explicit real-hardware interaction by the user.

**Goal:** 完成跨硬件的旋钮、控制台灯光和系统控制配置，并将 N4 Pro 的配置预览、真实输入和硬件输出接入现有 daemon。

**Architecture:** 在 hardware capability 层把旋钮控制单元、灯光区域与设备亮度分别建模；在 rendering 层保存用户草稿并生成硬件无关的面板/HUD 图像；daemon 将真实输入归一成动作并通过受限 executor 执行。本机系统控制与 N4 Pro SDK 写入均由可注入执行器承接，fake surface 保持完整测试覆盖。

**Tech Stack:** Python 3.11、Pydantic 2、FastAPI、Pillow、pytest、原生 Web JavaScript/CSS、MiraBox StreamDock Python SDK。

## Global Constraints

- 所有旋钮连续动作固定每格 `2%`，配置 GUI 不提供步长字段。
- Brand、Quota、Usage 的手动轮换顺序固定为 `Brand -> Quota -> Usage -> Brand`。
- Brand 上的内容轮换必须安静 no-op；touch bar 不出现常驻旋钮状态带。
- GUI 编辑只能更新草稿/预览；保存并应用前不得写真实硬件。
- N4 Pro 只能表示一个 `rotary_ring_group`，不能伪造四个独立可配置的 LED 灯圈。
- 呼吸效果默认不开放；必须由硬件 profile 的 smoke 验证字段显式开启。
- 系统控制不得在 input router 内执行；所有硬件输入先转换为业务 intent。
- N4 Pro `init()` 后必须重新应用已保存的控制台亮度。

---

### Task 1: 建立旋钮、灯光与持久化配置契约

**Files:**
- Modify: `src/agent_deck/hardware/capabilities.py`
- Create: `src/agent_deck/rendering/rotary_surface.py`
- Create: `src/agent_deck/server/rotary_layout_store.py`
- Modify: `src/agent_deck/server/app.py`
- Test: `tests/test_hardware_capabilities.py`
- Test: `tests/test_rotary_surface.py`
- Test: `tests/test_rotary_layout_store.py`

**Interfaces:**
- Produces `RotaryControlCapability`, `LightZoneCapability`, `DisplayBrightnessCapability`.
- Produces `N4ProRotaryLayout`, `RotaryRotateAction`, `RotaryPressAction`, `ConsoleLightingConfig`.
- Produces `load_n4pro_rotary_layout(path)` and `save_n4pro_rotary_layout(layout, path)`.

- [x] **Step 1: 写入 N4 Pro 的 per-control 与 group LED 失败测试**
- [x] **Step 2: 运行 `uv run pytest tests/test_hardware_capabilities.py -q`，确认因新模型缺失而失败**
- [x] **Step 3: 最小实现 capability 模型，并把 N4 Pro 声明为 4 个完整旋钮、一个 group LED、device-global brightness**
- [x] **Step 4: 写入 rotary layout 默认值、校验和 JSON round-trip 失败测试**
- [x] **Step 5: 实现不可变 layout 模型与原子 JSON store，并暴露 daemon 的 GET/PUT layout API**
- [x] **Step 6: 运行 `uv run pytest tests/test_hardware_capabilities.py tests/test_rotary_surface.py tests/test_rotary_layout_store.py -q`**

### Task 2: 用配置取代硬编码的 logical panel 旋钮映射

**Files:**
- Modify: `src/agent_deck/rendering/logical_panel.py`
- Create: `src/agent_deck/input/rotary.py`
- Modify: `src/agent_deck/input/logical_panel.py`
- Modify: `src/agent_deck/server/app.py`
- Test: `tests/test_logical_panel.py`
- Test: `tests/test_logical_panel_input.py`
- Test: `tests/test_rotary_input.py`

**Interfaces:**
- Produces `RotaryInputIntent` and `rotary_input_from_hardware_input(event, layout)`.
- Consumes `N4ProRotaryLayout` and `PanelSelection`.
- Produces pure panel selection functions for panel/content cycles.

- [x] **Step 1: 写失败测试，覆盖 Brand/Quota/Usage 的轮换、Quota 5h/Week 内容切换和 Brand no-op**
- [x] **Step 2: 运行相关 panel 测试，确认现有硬编码 knob 4 行为不满足新契约**
- [x] **Step 3: 实现 Brand panel kind、quota content selection、固定 panel/content cycle helpers**
- [x] **Step 4: 写失败测试，覆盖按旋钮配置将 fake 与 SDK knob event 映射为 rotate/press intent**
- [x] **Step 5: 实现 router，并从旧 `panel_event_from_*` 路径移除 N4 Pro knob 4 硬编码**
- [x] **Step 6: 运行 `uv run pytest tests/test_logical_panel.py tests/test_logical_panel_input.py tests/test_rotary_input.py -q`**

### Task 3: 增加短暂 HUD 渲染与缓存

**Files:**
- Create: `src/agent_deck/rendering/control_feedback.py`
- Modify: `src/agent_deck/rendering/logical_panel_touchscreen.py`
- Modify: `src/agent_deck/server/app.py`
- Test: `tests/test_control_feedback.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Produces `ControlFeedback(kind, value, expires_at_monotonic)`.
- Produces `render_control_feedback_touchscreen(feedback, base_image)`.
- Runtime selects feedback image while the 1.5-second expiry is active.

- [x] **Step 1: 写失败测试，覆盖连续数值 HUD、静音红色 HUD、错误 HUD 与过期后恢复原 panel**
- [x] **Step 2: 运行 `uv run pytest tests/test_control_feedback.py -q`，确认缺少 renderer 而失败**
- [x] **Step 3: 用 Pillow 实现透明中央 HUD 合成，不增加常驻文案条**
- [x] **Step 4: 将 daemon panel image 路径改为优先显示未过期反馈并保留原始 base panel**
- [x] **Step 5: 运行 `uv run pytest tests/test_control_feedback.py tests/test_server.py -q`**

### Task 4: 接入可注入的系统与控制台动作执行器

**Files:**
- Create: `src/agent_deck/actions/system_controls.py`
- Modify: `src/agent_deck/server/app.py`
- Test: `tests/test_system_controls.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Produces `SystemControlExecutor` protocol and `SystemControlResult`.
- Provides macOS output/input volume、mute、可控屏亮度 capability scan 的保守实现；其他平台返回明确不可用状态。
- Runtime consumes `RotaryInputIntent` and produces `ControlFeedback` only after executor result confirms success.

- [x] **Step 1: 写失败测试，覆盖 2% clamp、切换静音必须读取真实状态、不可控显示器不暴露/不执行**
- [x] **Step 2: 运行 `uv run pytest tests/test_system_controls.py -q`，确认 executor 缺失而失败**
- [x] **Step 3: 实现可替换 executor；macOS 使用受限的系统 API wrapper，测试使用 fake executor**
- [x] **Step 4: 在 runtime 中执行 rotate/press actions，失败产生错误反馈且不伪报成功**
- [x] **Step 5: 运行 `uv run pytest tests/test_system_controls.py tests/test_server.py -q`**

### Task 5: 在同一 N4 Pro 会话应用亮度与 group LED

**Files:**
- Modify: `src/agent_deck/hardware/streamdock_n4pro.py`
- Modify: `src/agent_deck/server/app.py`
- Test: `tests/test_streamdock_n4pro.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Extends `StreamDockN4ProPersistentAnimator` with an optional post-init/session output callback.
- Consumes applied `N4ProRotaryLayout` and returns a structured device-output diagnostic.
- Calls `set_brightness(percent)` after every device init; only calls LED color API for the single N4 Pro group.

- [x] **Step 1: 写失败测试，覆盖初次 init 后恢复 brightness、group LED 一次写入、SDK output failure 不破坏 frame render**
- [x] **Step 2: 运行 `uv run pytest tests/test_streamdock_n4pro.py -q`，确认 callback 契约缺失而失败**
- [x] **Step 3: 实现同会话 output callback 和 SDK 兼容调用，不新增第二个 HID 会话**
- [x] **Step 4: 将 daemon 的应用状态与 renderer callback 连接，并将真实输出结果写入 `/status`**
- [x] **Step 5: 运行 `uv run pytest tests/test_streamdock_n4pro.py tests/test_server.py -q`**

### Task 6: 完成 Web 配置、浏览器回归与启动验证环境

**Files:**
- Modify: `src/agent_deck/web/index.html`
- Modify: `src/agent_deck/web/app.js`
- Modify: `src/agent_deck/web/controls.css`
- Modify: `src/agent_deck/web/device.css`
- Modify: `README.md`
- Test: `tests/test_server.py`

**Interfaces:**
- GUI consumes `/ui/rotary-layout` and `/ui/system-controls` capability response.
- GUI keeps key layout and rotary layout in one draft state, then sends both only after “保存并应用”.
- Preview reflects `light_zones` addressability and synchronizes all N4 Pro rings for the group zone.

- [x] **Step 1: 写 API/HTML behavior tests，覆盖草稿不写硬件、保存才应用、N4 Pro 灯光预览四圈同步**
- [x] **Step 2: 运行相关 server tests，确认新 API 与保存组合尚不存在**
- [x] **Step 3: 实现旋钮检查器、独立控制台灯光区和显示器目标设置；只渲染 profile 支持的字段**
- [x] **Step 4: 合并保存请求，成功后更新 applied state；失败保留 draft 并显示错误**
- [x] **Step 5: 使用本地浏览器在亮/暗主题验证选择、草稿、保存、灯光预览和窄屏布局**
- [x] **Step 6: 运行 `uv run pytest -q`、`git diff --check`，用 tmux 脚本启动 daemon，并记录真机 smoke 命令**
