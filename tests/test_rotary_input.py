"""配置驱动的旋钮输入归一测试。

本文件只验证 fake/SDK-like 输入到硬件无关旋钮 intent 的映射，不执行系统音量、亮度、LED 或
真实 StreamDock I/O。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from agent_deck.hardware.fake import HardwareInput
from agent_deck.input.rotary import (
    rotary_input_from_hardware_input,
    rotary_input_from_streamdock_input_event,
)
from agent_deck.rendering.rotary_surface import (
    RotaryPressAction,
    RotaryRotateAction,
    default_n4pro_rotary_layout,
)


def test_fake_rotary_rotate_uses_binding_for_its_own_physical_control() -> None:
    """fake knob rotate 应读取对应位置 binding，而不是沿用硬编码 knob 4 行为。

    入参：无；测试把 knob 2 改为切换 virtual panel。
    返回：无返回值；断言通过表示任意位置都可承载同一动作。
    错误处理：控制位置、方向或 action 映射错误时由 pytest 报告。
    副作用：仅创建内存事件和配置模型。
    """

    body = default_n4pro_rotary_layout().model_dump(mode="json")
    body["controls"][1]["rotate_action"] = RotaryRotateAction.CYCLE_VIRTUAL_PANEL.value
    layout = type(default_n4pro_rotary_layout()).model_validate(body)
    event = HardwareInput(
        kind="knob",
        index=2,
        value={"action": "rotate", "direction": "left"},
        occurred_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
    )

    intent = rotary_input_from_hardware_input(event, layout)

    assert intent is not None
    assert intent.control_id == "knob_2"
    assert intent.rotate_action == RotaryRotateAction.CYCLE_VIRTUAL_PANEL
    assert intent.direction == -1


def test_sdk_rotary_press_derives_mute_action_from_volume_rotation() -> None:
    """SDK knob press 应由对应的音量旋转动作派生静音语义。

    入参：无；测试把 knob 3 的旋转动作配置为输出音量调节。
    返回：无返回值；断言通过表示用户不需要也不能独立配置按下通道。
    错误处理：SDK enum 读取、state 过滤或 press action 映射错误时由 pytest 报告。
    副作用：仅创建最小 SDK-like 对象。
    """

    body = default_n4pro_rotary_layout().model_dump(mode="json")
    body["controls"][2]["rotate_action"] = RotaryRotateAction.ADJUST_OUTPUT_VOLUME.value
    layout = type(default_n4pro_rotary_layout()).model_validate(body)
    event = SimpleNamespace(
        event_type=_Value("knob_press"),
        knob_id=_Value("knob_3"),
        state=1,
    )

    intent = rotary_input_from_streamdock_input_event(event, layout)

    assert intent is not None
    assert intent.control_id == "knob_3"
    assert intent.press_action == RotaryPressAction.TOGGLE_OUTPUT_MUTE
    assert intent.direction is None


def test_unassigned_rotary_channel_produces_no_action_intent() -> None:
    """暂不设定的旋转或按下通道不得制造可执行 intent。

    入参：无；默认 knob 2 保持暂不设定。
    返回：无返回值；断言通过表示无配置旋钮可由 daemon 触发品牌反馈而不执行副作用。
    错误处理：unassigned 被错误映射为动作时由 pytest 报告。
    副作用：仅创建内存事件。
    """

    event = HardwareInput(
        kind="knob",
        index=2,
        value={"action": "rotate", "direction": "right"},
        occurred_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
    )

    assert rotary_input_from_hardware_input(event, default_n4pro_rotary_layout()) is None


class _Value:
    """带 `.value` 的最小 SDK enum 替身。

    入参：`value` 是输入类型、旋钮 id 或方向字符串。
    返回：测试对象。
    错误处理：无。
    副作用：仅保存内存字符串。
    """

    def __init__(self, value: str) -> None:
        """保存 enum-like 字符串值。

        入参：`value` 是稳定字符串。
        返回：无显式返回值。
        错误处理：无。
        副作用：写入实例内存字段。
        """

        self.value = value
