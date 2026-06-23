"""Logical panel 的硬件输入归一路由。

本模块把 fake `HardwareInput` 和 StreamDock SDK `InputEvent` 形态的底层事件转换成
`PanelInputEvent`。它只做坐标、旋钮编号和方向的纯映射，不监听真实硬件、不调用 daemon、
不执行 action，也不修改任何运行状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_deck.hardware.fake import HardwareInput
from agent_deck.rendering.logical_panel import PanelInputEvent
from agent_deck.rendering.n4pro_panel import N4PRO_LOGICAL_PANEL_VIEWPORT


def panel_event_from_hardware_input(event: HardwareInput) -> PanelInputEvent | None:
    """把 fake hardware input 映射成 logical panel 输入事件。

    入参：`event` 是 fake surface 队列中的 `HardwareInput`。
    返回：匹配的 `PanelInputEvent`；与 logical panel 无关的输入返回 None。
    错误处理：缺少坐标、方向或 value 不是 mapping 时按无关事件处理，不抛业务异常。
    副作用：无；只读取内存对象。
    """

    if event.kind == "touch":
        value = _mapping_value(event.value)
        if value is None:
            return None
        x = _int_value(value.get("x"))
        y = _int_value(value.get("y"))
        if x is not None and y is not None and _is_inside_logical_panel(x, y):
            return PanelInputEvent.TOUCH_TAP
        return None

    if event.kind == "knob":
        value = _mapping_value(event.value)
        if value is None:
            return None
        if event.index != 4 or _string_value(value.get("action")) != "rotate":
            return None
        return _knob4_panel_event(_string_value(value.get("direction")))

    return None


def panel_event_from_streamdock_input_event(event: object) -> PanelInputEvent | None:
    """把 StreamDock SDK-like input event 映射成 logical panel 输入事件。

    入参：`event` 是官方 SDK `InputEvent` 或具备同名属性的测试替身。
    返回：匹配的 `PanelInputEvent`；与 logical panel 无关的事件返回 None。
    错误处理：缺少属性时按无关事件处理，不导入或依赖真实 SDK。
    副作用：无；只读取对象属性。
    """

    event_type = _enum_or_string(getattr(event, "event_type", None))
    if event_type == "touch_point":
        x = _int_value(getattr(event, "x", None))
        y = _int_value(getattr(event, "y", None))
        if x is not None and y is not None and _is_inside_logical_panel(x, y):
            return PanelInputEvent.TOUCH_TAP
        return None

    if event_type == "knob_press":
        knob_id = _enum_or_string(getattr(event, "knob_id", None))
        state = _int_value(getattr(event, "state", None))
        if knob_id == "knob_4" and state == 1:
            return PanelInputEvent.TOUCH_TAP
        return None

    if event_type == "knob_rotate":
        knob_id = _enum_or_string(getattr(event, "knob_id", None))
        if knob_id != "knob_4":
            return None
        return _knob4_panel_event(_enum_or_string(getattr(event, "direction", None)))

    return None


def _knob4_panel_event(direction: str | None) -> PanelInputEvent | None:
    """把第 4 旋钮方向映射为 token 周期切换事件。

    入参：`direction` 是 `left` 或 `right`。
    返回：对应 `PanelInputEvent`；未知方向返回 None。
    错误处理：无。
    副作用：无。
    """

    if direction == "left":
        return PanelInputEvent.KNOB_4_ROTATE_LEFT
    if direction == "right":
        return PanelInputEvent.KNOB_4_ROTATE_RIGHT
    return None


def _is_inside_logical_panel(x: int, y: int) -> bool:
    """判断坐标是否落在 N4 Pro logical panel viewport 内。

    入参：`x` 和 `y` 是 N4 Pro 背景坐标。
    返回：坐标在 viewport 边界内时为 True。
    错误处理：无。
    副作用：无。
    """

    viewport = N4PRO_LOGICAL_PANEL_VIEWPORT
    return viewport.left <= x < viewport.right and viewport.top <= y < viewport.bottom


def _mapping_value(value: object) -> Mapping[Any, Any] | None:
    """把可能的 mapping payload 规范成可读 mapping。

    入参：`value` 是硬件输入携带的 payload。
    返回：若 value 是 mapping 则返回它，否则返回 None。
    错误处理：无。
    副作用：无。
    """

    if isinstance(value, Mapping):
        return value
    return None


def _int_value(value: object) -> int | None:
    """从 payload 字段读取 int。

    入参：`value` 是任意字段值。
    返回：可转换为 int 时返回 int，否则返回 None。
    错误处理：转换失败时吞掉异常并返回 None。
    副作用：无。
    """

    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _string_value(value: object) -> str | None:
    """从 payload 字段读取字符串。

    入参：`value` 是任意字段值。
    返回：字符串值；空字符串或非字符串返回 None。
    错误处理：无。
    副作用：无。
    """

    if isinstance(value, str) and value:
        return value
    return None


def _enum_or_string(value: object) -> str | None:
    """从 SDK enum 或普通字符串中读取稳定字符串值。

    入参：`value` 可以是字符串、带 `.value` 的 enum-like 对象或 None。
    返回：字符串值；不可识别时返回 None。
    错误处理：无。
    副作用：无。
    """

    if isinstance(value, str):
        return value
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    return None
