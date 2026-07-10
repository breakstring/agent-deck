"""配置驱动的旋钮输入归一层。

本模块把 fake hardware 或 StreamDock SDK-like 旋钮事件按当前用户 layout 映射成硬件无关
`RotaryInputIntent`。它不执行系统音频、显示器亮度、控制台亮度或 LED 写入；daemon action
层必须根据 intent 再调用受限 executor，保证不同硬件型号可以共享输入语义。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_deck.hardware.fake import HardwareInput
from agent_deck.rendering.rotary_surface import (
    N4ProRotaryLayout,
    RotaryPressAction,
    RotaryRotateAction,
    press_action_for_rotate_action,
)


class RotaryInputIntent(BaseModel):
    """描述配置匹配后可由 daemon 执行的一次旋钮输入。

    入参：`source` 是 fake 或 StreamDock 来源；`control_id` 是稳定物理位置；rotate 与 press
    action 二选一；旋转 intent 必须携带 `direction` 的 -1 或 1。
    返回：frozen Pydantic model。
    错误处理：不完整 action 或非法方向组合由模型校验拒绝。
    副作用：仅保存内存数据，不执行动作。
    """

    model_config = ConfigDict(frozen=True)

    source: str
    control_id: str
    rotate_action: RotaryRotateAction | None = None
    press_action: RotaryPressAction | None = None
    direction: int | None = None

    def is_rotate(self) -> bool:
        """判断该 intent 是否来自旋转通道。

        入参：无。
        返回：存在 rotate action 时为 True。
        错误处理：无。
        副作用：无。
        """

        return self.rotate_action is not None


def rotary_input_from_hardware_input(
    event: HardwareInput,
    layout: N4ProRotaryLayout,
) -> RotaryInputIntent | None:
    """把 fake hardware knob 事件映射为配置驱动的旋钮 intent。

    入参：`event` 必须是 kind 为 `knob` 的低层输入；`layout` 是当前 applied rotary layout。
    返回：已配置通道时返回 intent；未设定、无效编号或非旋钮输入返回 None。
    错误处理：缺少 action/direction 的 payload 按无关输入处理，不抛业务异常。
    副作用：无。
    """

    if event.kind != "knob":
        return None
    control_id = _fake_control_id(event.index)
    value = _mapping_value(event.value)
    if control_id is None or value is None:
        return None
    action = _string_value(value.get("action"))
    if action == "rotate":
        return _rotate_intent(
            source="hardware_knob",
            control_id=control_id,
            direction=_direction_value(_string_value(value.get("direction"))),
            layout=layout,
        )
    if action == "press" or _int_value(value.get("state")) == 1:
        return _press_intent(
            source="hardware_knob",
            control_id=control_id,
            layout=layout,
        )
    return None


def rotary_input_from_streamdock_input_event(
    event: object,
    layout: N4ProRotaryLayout,
) -> RotaryInputIntent | None:
    """把 StreamDock SDK-like knob event 映射为配置驱动 intent。

    入参：`event` 是带 `event_type`、`knob_id`、可选 `direction/state` 的 SDK 或测试对象；
    `layout` 是当前 applied rotary layout。
    返回：已配置通道时返回 intent，否则返回 None。
    错误处理：缺失或未知 enum 字段按无关输入处理。
    副作用：无；不导入官方 SDK。
    """

    event_type = _enum_or_string(getattr(event, "event_type", None))
    control_id = _enum_or_string(getattr(event, "knob_id", None))
    if control_id is None:
        return None
    if event_type == "knob_rotate":
        return _rotate_intent(
            source="streamdock_knob",
            control_id=control_id,
            direction=_direction_value(_enum_or_string(getattr(event, "direction", None))),
            layout=layout,
        )
    if event_type == "knob_press" and _int_value(getattr(event, "state", None)) == 1:
        return _press_intent(
            source="streamdock_knob",
            control_id=control_id,
            layout=layout,
        )
    return None


def _rotate_intent(
    *,
    source: str,
    control_id: str,
    direction: int | None,
    layout: N4ProRotaryLayout,
) -> RotaryInputIntent | None:
    """根据一个旋钮绑定创建旋转 intent。

    入参：`source`、`control_id` 和 `direction` 描述低层输入；`layout` 提供用户 binding。
    返回：绑定非 unassigned 且方向有效时返回 intent，否则 None。
    错误处理：未知 control id 或 unassigned 通道按无动作处理。
    副作用：无。
    """

    if direction is None:
        return None
    try:
        binding = layout.binding_for(control_id)
    except KeyError:
        return None
    if binding.rotate_action == RotaryRotateAction.UNASSIGNED:
        return None
    return RotaryInputIntent(
        source=source,
        control_id=control_id,
        rotate_action=binding.rotate_action,
        direction=direction,
    )


def _press_intent(
    *,
    source: str,
    control_id: str,
    layout: N4ProRotaryLayout,
) -> RotaryInputIntent | None:
    """根据一个旋钮绑定创建按下 intent。

    入参：`source` 与 `control_id` 描述低层输入；`layout` 提供用户 binding。
    返回：绑定非 unassigned 时返回 intent，否则 None。
    错误处理：未知 control id 或 unassigned 通道按无动作处理。
    副作用：无。
    """

    try:
        binding = layout.binding_for(control_id)
    except KeyError:
        return None
    press_action = press_action_for_rotate_action(binding.rotate_action)
    if press_action == RotaryPressAction.UNASSIGNED:
        return None
    return RotaryInputIntent(
        source=source,
        control_id=control_id,
        press_action=press_action,
    )


def _fake_control_id(index: int) -> str | None:
    """把 fake hardware 的一基旋钮编号转换为 N4 Pro 控制 id。

    入参：`index` 是 fake event 中的一基位置编号。
    返回：`knob_1` 到 `knob_4`，超出范围返回 None。
    错误处理：无。
    副作用：无。
    """

    if 1 <= index <= 4:
        return f"knob_{index}"
    return None


def _direction_value(value: str | None) -> int | None:
    """把 SDK 或 fake 方向字符串转换为固定正负步进。

    入参：`value` 是 `left` 或 `right`。
    返回：left 为 -1、right 为 1，其他值为 None。
    错误处理：无。
    副作用：无。
    """

    if value == "left":
        return -1
    if value == "right":
        return 1
    return None


def _mapping_value(value: object) -> Mapping[Any, Any] | None:
    """读取 fake event 的 mapping payload。

    入参：`value` 是任意输入 payload。
    返回：mapping 时原样返回，否则 None。
    错误处理：无。
    副作用：无。
    """

    return value if isinstance(value, Mapping) else None


def _int_value(value: object) -> int | None:
    """保守读取一个整数 state。

    入参：`value` 是 SDK 或 fake payload 字段。
    返回：可转换 int 时返回该值，否则 None。
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
    """读取非空字符串 payload 字段。

    入参：`value` 是任意 payload 字段。
    返回：非空字符串或 None。
    错误处理：无。
    副作用：无。
    """

    return value if isinstance(value, str) and value else None


def _enum_or_string(value: object) -> str | None:
    """读取 SDK enum-like 或普通字符串的稳定值。

    入参：`value` 是字符串、带 `.value` 的 enum-like 对象或 None。
    返回：非空字符串值或 None。
    错误处理：无。
    副作用：无。
    """

    direct = _string_value(value)
    if direct is not None:
        return direct
    return _string_value(getattr(value, "value", None))
