"""Logical panel 硬件输入路由测试。

本文件只验证 fake hardware input / SDK-like input event 到 `PanelInputEvent` 的纯映射；
不会访问真实 StreamDock 设备、不会启动 daemon，也不会执行任何硬件动作。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from agent_deck.hardware.fake import HardwareInput
from agent_deck.input.logical_panel import (
    panel_event_from_hardware_input,
    panel_event_from_streamdock_input_event,
)
from agent_deck.rendering.logical_panel import PanelInputEvent


def test_touch_inside_n4pro_logical_panel_maps_to_touch_tap() -> None:
    """N4 Pro logical panel viewport 内的 touch point 应映射为 touch tap。

    入参：无；测试内构造 fake touch input。
    返回：无返回值；断言通过代表 touch bar 自身点击可切换 logical panel。
    错误处理：坐标判断错误时由 pytest 报告。
    副作用：只创建内存模型。
    """

    event = HardwareInput(
        kind="touch",
        index=0,
        value={"x": 120, "y": 380},
        occurred_at=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
    )

    assert panel_event_from_hardware_input(event) == PanelInputEvent.TOUCH_TAP


def test_touch_outside_n4pro_logical_panel_is_ignored() -> None:
    """logical panel viewport 外的 touch point 不应触发 panel 切换。

    入参：无；测试内构造按键区域 touch input。
    返回：无返回值；断言通过代表主按键区域不会被误路由为 panel tap。
    错误处理：坐标边界错误时由 pytest 报告。
    副作用：只创建内存模型。
    """

    event = HardwareInput(
        kind="touch",
        index=0,
        value={"x": 120, "y": 120},
        occurred_at=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
    )

    assert panel_event_from_hardware_input(event) is None


def test_knob4_fake_input_maps_to_token_period_events() -> None:
    """fake knob4 左右旋转应映射为 token 周期切换事件。

    入参：无；测试内构造 fake knob input。
    返回：无返回值；断言通过代表 fake hardware 可驱动 tokens 面板周期切换。
    错误处理：旋钮编号或方向映射错误时由 pytest 报告。
    副作用：只创建内存模型。
    """

    occurred_at = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    left = HardwareInput(
        kind="knob",
        index=4,
        value={"action": "rotate", "direction": "left"},
        occurred_at=occurred_at,
    )
    right = HardwareInput(
        kind="knob",
        index=4,
        value={"action": "rotate", "direction": "right"},
        occurred_at=occurred_at,
    )

    assert panel_event_from_hardware_input(left) == (
        PanelInputEvent.KNOB_4_ROTATE_LEFT
    )
    assert panel_event_from_hardware_input(right) == (
        PanelInputEvent.KNOB_4_ROTATE_RIGHT
    )


def test_streamdock_input_event_shape_maps_to_panel_events() -> None:
    """SDK-like InputEvent 应按 event_type/knob_id/direction 映射。

    入参：无；测试内构造最小 SDK-like 对象，不导入真实 SDK。
    返回：无返回值；断言通过代表真实 StreamDock callback 可复用同一映射。
    错误处理：枚举值读取或方向映射错误时由 pytest 报告。
    副作用：只创建内存对象。
    """

    touch = SimpleNamespace(
        event_type=_ValueObject("touch_point"),
        x=60,
        y=410,
    )
    knob_right = SimpleNamespace(
        event_type=_ValueObject("knob_rotate"),
        knob_id=_ValueObject("knob_4"),
        direction=_ValueObject("right"),
    )
    knob_press = SimpleNamespace(
        event_type=_ValueObject("knob_press"),
        knob_id=_ValueObject("knob_4"),
        state=1,
    )

    assert panel_event_from_streamdock_input_event(touch) == (
        PanelInputEvent.TOUCH_TAP
    )
    assert panel_event_from_streamdock_input_event(knob_press) == (
        PanelInputEvent.TOUCH_TAP
    )
    assert panel_event_from_streamdock_input_event(knob_right) == (
        PanelInputEvent.KNOB_4_ROTATE_RIGHT
    )


class _ValueObject:
    """构造带 `.value` 的最小 SDK enum 替身。

    入参：`value` 是枚举值字符串。
    返回：测试用对象。
    错误处理：无。
    副作用：无。
    """

    def __init__(self, value: str) -> None:
        """保存字符串值。

        入参：`value` 是字符串值。
        返回：无。
        错误处理：无。
        副作用：无。
        """

        self.value = value
