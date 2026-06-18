"""Agent 来源适配器包。

本包放置 Codex、Claude Code 等具体 Agent 运行面的适配逻辑。适配器只负责把外部
Agent 的协议、遥测或本地 IPC 转换成 Agent Deck 可使用的数据模型；它不直接渲染
StreamDock 设备、不修改硬件状态，也不执行业务动作。
"""
