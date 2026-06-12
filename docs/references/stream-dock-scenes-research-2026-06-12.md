# Stream Dock 场景配置研究摘记

日期：2026-06-12
用途：归档关于妙联宝官方“场景配置”和 Agent Deck 内部运行场景的设计判断。

## 结论

Agent Deck 应该区分两类“场景”：

1. **官方 Stream Dock 场景配置**
   这是妙联宝软件里的用户配置单位，适合用户手动管理、导入导出、应用智能跟随和静态快捷键布局。

2. **Agent Deck 内部运行场景，也称 DeckMode**
   这是 Agent Deck daemon 在运行时使用的 UI/交互模式，适合实时状态、临时审批、触屏详情、旋钮选择和快捷 prompt。

第一版不应依赖官方场景配置作为动态状态展示的底座。更合理的方式是：建议用户在官方软件里创建一个 `Agent Deck` 场景作为入口；真正动态控制由 `agent-deckd` 的 HID 直连和内部 DeckMode 完成。

## 官方场景配置

官方 Creator 文档把场景配置定义为面向特定应用、任务、工具或软件的一套 Stream Dock 按键布局。它适合 Photoshop、Premiere、Teams、Zoom、浏览器、编程快捷操作、音频控制、直播等稳定任务。

官方文档同时列出限制：

- 跨平台或跨软件版本时兼容性可能不稳定。
- 场景配置使用的插件不会随场景一起传输，用户要额外安装插件。
- 音频等本地文件路径不会包含在场景内。
- 不同 Stream Dock 型号按键数量不同，跨硬件使用会有功能缺口或布局问题。
- 用户环境不可预测，防火墙、系统权限、其他软件都会影响场景运行。
- 场景配置主要面向定义明确的静态操作，缺乏基于实时数据和复杂逻辑动态调整内容或动作的能力。

这些限制与 Agent Deck 的核心需求直接相关。Agent Deck 要处理实时 Agent 状态、pending decision、临时重绑定、超时、硬件断连、触屏详情和旋钮输入，不能完全交给官方场景配置。

## 切换方式

当前公开资料能确认或强烈指向以下切换方式：

- 用户可以在 Stream Dock 软件中手动编辑、选择和导出场景配置。
- 官方教程列表包含“预设的场景如何用键盘进行切换”“预设应用场景快捷切换”“如何设置场景智能跟随”等内容，说明官方软件存在快捷切换和应用跟随能力。
- Plugin SDK 支持 `applicationDidLaunch` / `applicationDidTerminate`，插件可在 manifest 中声明要监控的应用。macOS 使用 bundle id，Windows 使用 exe 文件名。

但当前文档中没有看到 Python SDK 或 Plugin SDK 暴露 `switchScene` / `switchProfile` 之类的主动切换 API。Plugin SDK 可发送的动态事件包括 `setTitle`、`setImage`、`showAlert`、`showOk`、`setState`、settings/globalSettings 等，未发现官方场景切换事件。

因此第一版不能假设 Agent Deck 能用 SDK 主动切换官方场景。

## 设计判断

### 官方场景的角色

官方场景应作为 Agent Deck 的入口和用户熟悉的外部配置层：

- 创建 `Agent Deck` 官方场景。
- 放置启动/停止 daemon 的静态按钮。
- 放置打开本地管理页、日志、配置目录的静态按钮。
- 放置回到用户默认场景的静态按钮。
- 后续如果做官方插件，可以在该场景里放 Agent Deck 插件 action。

### DeckMode 的角色

DeckMode 是 Agent Deck 内部 runtime 模式，应由 `agent-deckd` 控制：

- `overview`：总览所有 Agent。
- `agent_detail`：当前选中 Agent 详情。
- `decision`：审批、选择、等待输入的临时模式。
- `quick_prompt`：快捷 prompt 模板模式。
- `settings`：亮度、静音、显示模式等。

核心渲染链路应为：

```text
AgentState + PendingDecision + SelectedAgent + DeckMode
  -> LayoutPlan
  -> HardwareRenderer
```

而不是：

```text
AgentState -> N4ProLayout
```

这样未来接其他设备、官方场景、插件模式或宠物机制时，状态机不用重写。

## 第一版影响

第一版建议：

- 在文档中指导用户手动创建官方 `Agent Deck` 场景，但不自动创建。
- `agent-deckd` 使用内部 DeckMode 控制 N4 Pro 动态布局。
- 明确设备控制模式：如果官方软件和 Python HID 直连抢占设备，第一版采用 Agent Deck 独占控制模式。
- 将官方场景集成放入 roadmap，作为 P4/P5 后的可选能力。

## 参考链接

- 什么是场景配置？
  https://creator.key123.vip/stream-dock/scene/scene.html
- 场景配置导出
  https://creator.key123.vip/en/stream-dock/scene/exporting.html
- 场景使用案例
  https://creator.key123.vip/en/stream-dock/scene/cases.html
- Space Plugin SDK 概述
  https://sdk.key123.vip/guide/overview.html
- 插件可接收事件
  https://sdk.key123.vip/guide/events-received.html
- 插件可发送事件
  https://sdk.key123.vip/guide/events-sent.html
