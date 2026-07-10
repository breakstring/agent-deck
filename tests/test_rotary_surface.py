"""旋钮与控制台灯光配置模型的单元测试。

本文件只验证 Pydantic 配置契约、默认值和草稿结构，不访问真实硬件、系统音频、显示器、
文件系统或网络。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_deck.rendering.rotary_surface import (
    ConsoleLightingConfig,
    ConsoleLightingMode,
    N4ProRotaryLayout,
    RotaryPressAction,
    RotaryRotateAction,
    default_n4pro_rotary_layout,
    press_action_for_rotate_action,
)


def test_default_n4pro_rotary_layout_keeps_all_controls_independently_configurable() -> None:
    """默认 N4 Pro layout 应覆盖四个物理旋钮并只保存旋转 binding。

    入参：无。
    返回：无返回值；断言通过表示默认配置不会把旋钮角色写死到 capability profile。
    错误处理：配置字段或默认 control 数量不符合契约时由 pytest 报告。
    副作用：仅创建内存 Pydantic 模型。
    """

    layout = default_n4pro_rotary_layout()

    assert tuple(binding.control_id for binding in layout.controls) == (
        "knob_1",
        "knob_2",
        "knob_3",
        "knob_4",
    )
    assert layout.controls[0].rotate_action == RotaryRotateAction.CYCLE_VIRTUAL_PANEL
    assert layout.controls[3].rotate_action == RotaryRotateAction.CYCLE_PANEL_CONTENT
    assert all(not hasattr(binding, "press_action") for binding in layout.controls)
    assert layout.lighting.mode == ConsoleLightingMode.OFF
    assert layout.console_brightness_percent == 100


def test_rotary_layout_allows_duplicate_actions_and_derives_press_semantics() -> None:
    """多个旋钮可以复用同一动作，按下语义必须由旋转动作派生。

    入参：无；测试基于默认 layout 局部替换两个 binding。
    返回：无返回值；断言通过表示产品不强迫每个旋钮承担唯一角色。
    错误处理：Pydantic 未保留配置或错误拒绝重复动作时由 pytest 报告。
    副作用：仅创建内存模型。
    """

    base = default_n4pro_rotary_layout().model_dump(mode="json")
    base["controls"][1]["rotate_action"] = RotaryRotateAction.CYCLE_VIRTUAL_PANEL.value
    base["controls"][2]["rotate_action"] = RotaryRotateAction.ADJUST_INPUT_VOLUME.value
    base["controls"][2]["press_action"] = RotaryPressAction.TOGGLE_OUTPUT_MUTE.value

    layout = N4ProRotaryLayout.model_validate(base)

    assert layout.controls[0].rotate_action == RotaryRotateAction.CYCLE_VIRTUAL_PANEL
    assert layout.controls[1].rotate_action == RotaryRotateAction.CYCLE_VIRTUAL_PANEL
    assert not hasattr(layout.controls[2], "press_action")
    assert press_action_for_rotate_action(layout.controls[2].rotate_action) == RotaryPressAction.TOGGLE_INPUT_MUTE
    assert press_action_for_rotate_action(RotaryRotateAction.CYCLE_VIRTUAL_PANEL) == RotaryPressAction.UNASSIGNED


def test_console_lighting_rejects_color_when_off_and_invalid_hex() -> None:
    """灯光关闭时不能携带颜色，基础色必须是标准六位十六进制。

    入参：无；测试构造两种非法配置。
    返回：无返回值；断言通过表示 API 不会接受不可解释的灯光草稿。
    错误处理：非法配置由 Pydantic `ValidationError` 报告。
    副作用：仅创建内存模型。
    """

    with pytest.raises(ValidationError, match="off lighting must not include color"):
        ConsoleLightingConfig(mode=ConsoleLightingMode.OFF, color="#35C9FF")
    with pytest.raises(ValidationError, match="six-digit hex"):
        ConsoleLightingConfig(mode=ConsoleLightingMode.COLOR, color="#abc")
    with pytest.raises(ValidationError, match="off lighting must not enable breathing"):
        ConsoleLightingConfig(mode=ConsoleLightingMode.OFF, breathe=True)


def test_console_lighting_allows_optional_breathing_only_for_a_base_color() -> None:
    """指定基础色时可开启柔和呼吸，且该字段会保留在 JSON 模型中。

    入参：无；构造包含基础色和呼吸标志的灯光配置。
    返回：无返回值；断言通过表示 GUI 可保存用户已验证的呼吸效果开关。
    错误处理：模型字段缺失或错误校验由 pytest 报告。
    副作用：仅创建内存模型。
    """

    lighting = ConsoleLightingConfig(
        mode=ConsoleLightingMode.COLOR,
        color="#35c9ff",
        breathe=True,
    )

    assert lighting.color == "#35C9FF"
    assert lighting.breathe is True
