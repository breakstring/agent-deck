"""Deck mode selection contracts for Agent Deck.

This module defines hardware-independent UI mode and selection models used by
layout planning. It does not compute layouts, resolve decisions, start servers,
access StreamDock devices, read files, write files, or perform network I/O;
importing it is intentionally side-effect free.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class DeckMode(StrEnum):
    """Represent the high-level interaction mode for the local deck UI.

    入参：枚举成员值是 layout、server 和未来 renderer 共用的稳定字符串。
    返回：作为字符串枚举参与 Pydantic 校验、比较和序列化。
    错误处理：未知模式值由 Enum/Pydantic 校验为非法值并报告。
    副作用：无；定义枚举不访问网络、硬件、文件或全局运行状态。
    """

    OVERVIEW = "overview"
    AGENT_DETAIL = "agent_detail"
    DECISION = "decision"
    QUICK_PROMPT = "quick_prompt"
    SETTINGS = "settings"


class DeckSelection(BaseModel):
    """Carry the user's current deck focus without mutating agent state.

    入参：`mode` 是用户请求的 deck mode；`selected_agent_key` 是当前聚焦 agent，可空；
    `selected_decision_id` 是当前聚焦 decision，可空。
    返回：frozen Pydantic model，可通过 `model_copy(update=...)` 派生新选择态。
    错误处理：非法 mode 值由 Pydantic 校验异常报告；未知 agent/decision 不在此处校验。
    副作用：仅保存内存数据；实例化不访问网络、硬件或文件系统。
    """

    model_config = ConfigDict(frozen=True)

    mode: DeckMode = DeckMode.OVERVIEW
    selected_agent_key: str | None = None
    selected_decision_id: str | None = None
