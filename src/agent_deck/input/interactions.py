"""Deck 交互输入归一路由。

本模块把低层 key/button 输入结合当前 `LayoutPlan` 映射成硬件无关
`InteractionIntent`。它不执行 focus、审批、AppleScript 或终端输入；只读取当前 layout
里已经声明的 key intent，避免输入路由和渲染语义出现两套映射。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_deck.hardware.fake import HardwareInput
from agent_deck.rendering.layout import KeyPlan, LayoutPlan


class InteractionIntent(BaseModel):
    """描述一次硬件输入映射出的 deck 交互意图。

    入参：`source` 是输入来源；`key_index` 是 0-based layout key index；`intent` 是 layout
    key plan 上声明的业务意图；`agent_key` 和 `decision_id` 透传 key plan 上下文；
    `dry_run` 表示该 intent 当前是否只能记录、不执行外部副作用。
    返回：frozen Pydantic model，可进入 runtime status 或 action executor。
    错误处理：字段类型非法由 Pydantic 报告。
    副作用：仅保存内存数据，不执行动作。
    """

    model_config = ConfigDict(frozen=True)

    source: str
    key_index: int
    intent: str
    agent_key: str | None = None
    decision_id: str | None = None
    dry_run: bool = True


def interaction_intent_from_hardware_input(
    event: HardwareInput,
    layout: LayoutPlan,
) -> InteractionIntent | None:
    """把 fake/归一化 key input 映射成 deck interaction intent。

    入参：`event` 是低层硬件输入；`layout` 是当前渲染帧的 key 计划。
    返回：匹配 layout key 的 `InteractionIntent`；非 key、release、空键位或越界返回 None。
    错误处理：payload 缺少 state/action 时按 release/无关事件处理。
    副作用：无。
    """

    if event.kind != "key":
        return None
    value = _mapping_value(event.value)
    if value is None or not _is_press(value):
        return None
    return _intent_from_layout_key(
        layout,
        key_index=event.index,
        source="hardware_key",
    )


def interaction_intent_from_streamdock_input_event(
    event: object,
    layout: LayoutPlan,
) -> InteractionIntent | None:
    """把 StreamDock SDK button event 映射成 deck interaction intent。

    入参：`event` 是官方 SDK `InputEvent` 或测试替身；`layout` 是当前渲染帧。
    返回：匹配 layout key 的 `InteractionIntent`；非 button、release、缺 key 或越界返回 None。
    错误处理：缺少属性时按无关事件处理。
    副作用：无。
    """

    if _enum_or_string(getattr(event, "event_type", None)) != "button":
        return None
    if _int_value(getattr(event, "state", None)) != 1:
        return None
    key_value = _int_value(getattr(getattr(event, "key", None), "value", None))
    if key_value is None:
        key_value = _int_value(getattr(event, "key", None))
    if key_value is None:
        return None
    key_index = _streamdock_key_value_to_layout_index(key_value)
    if key_index is None:
        return None
    return _intent_from_layout_key(
        layout,
        key_index=key_index,
        source="streamdock_button",
    )


def _streamdock_key_value_to_layout_index(key_value: int) -> int | None:
    """把 StreamDock SDK button key 编号转换成 Agent Deck layout index。

    入参：`key_value` 是 SDK event.key.value。N4 Pro 真实主按键上报 11-20，对应物理
    10 个主键；部分测试替身或其他型号可能使用 1-10。
    返回：0-based layout key index；未知编号返回 None。
    错误处理：无。
    副作用：无。
    """

    if 11 <= key_value <= 20:
        return key_value - 11
    if 1 <= key_value <= 10:
        return key_value - 1
    return None


def _intent_from_layout_key(
    layout: LayoutPlan,
    *,
    key_index: int,
    source: str,
) -> InteractionIntent | None:
    """从 layout key plan 创建 interaction intent。

    入参：`layout` 是当前 layout；`key_index` 是 0-based 键位；`source` 是输入来源标签。
    返回：key 有 intent 时返回 `InteractionIntent`，否则返回 None。
    错误处理：key index 越界返回 None。
    副作用：无。
    """

    key = _key_at(layout, key_index)
    if key is None or key.intent is None:
        return None
    return InteractionIntent(
        source=source,
        key_index=key.index,
        intent=key.intent,
        agent_key=key.agent_key,
        decision_id=key.decision_id,
        dry_run=key.intent != "select_agent",
    )


def _key_at(layout: LayoutPlan, key_index: int) -> KeyPlan | None:
    """读取指定 layout key。

    入参：`layout` 是当前 layout；`key_index` 是 0-based 键位。
    返回：匹配 index 的 `KeyPlan`；越界或缺失返回 None。
    错误处理：无。
    副作用：无。
    """

    if key_index < 0:
        return None
    for key in layout.keys:
        if key.index == key_index:
            return key
    return None


def _is_press(value: Mapping[Any, Any]) -> bool:
    """判断 fake key payload 是否表示按下。

    入参：`value` 是 key input payload。
    返回：`state == 1` 或 `action == "press"` 时为 True。
    错误处理：缺少字段或字段不可转换时返回 False。
    副作用：无。
    """

    if _int_value(value.get("state")) == 1:
        return True
    return _string_value(value.get("action")) == "press"


def _mapping_value(value: object) -> Mapping[Any, Any] | None:
    """把可能的 mapping payload 规范成可读 mapping。

    入参：`value` 是低层硬件输入携带的 payload。
    返回：mapping payload 或 None。
    错误处理：无。
    副作用：无。
    """

    if isinstance(value, Mapping):
        return value
    return None


def _int_value(value: object) -> int | None:
    """从任意值读取 int。

    入参：`value` 是 payload 或 SDK 字段值。
    返回：可转换为 int 时返回 int，否则返回 None。
    错误处理：转换失败返回 None。
    副作用：无。
    """

    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _string_value(value: object) -> str | None:
    """从任意值读取非空字符串。

    入参：`value` 是 payload 字段值。
    返回：非空字符串或 None。
    错误处理：无。
    副作用：无。
    """

    if isinstance(value, str) and value:
        return value
    return None


def _enum_or_string(value: object) -> str | None:
    """从 SDK enum-like 或字符串读取稳定字符串。

    入参：`value` 可以是字符串、带 `.value` 的 enum-like 对象或 None。
    返回：字符串值或 None。
    错误处理：无。
    副作用：无。
    """

    if isinstance(value, str):
        return value
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    return None
