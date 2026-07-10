"""旋钮、控制台灯光与显示亮度的用户配置模型。

本模块定义 GUI 草稿、持久化 JSON 和 daemon input router 共享的硬件中立配置契约。它不读取或
写入配置文件、不访问 StreamDock SDK、不执行系统音频/亮度动作，也不渲染页面；真实模型能力由
`agent_deck.hardware.capabilities` 另行声明，调用方必须按 profile 过滤本模块的可选动作。
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

N4PRO_ROTARY_CONTROL_IDS: tuple[str, ...] = (
    "knob_1",
    "knob_2",
    "knob_3",
    "knob_4",
)
"""N4 Pro 当前已验证的四个物理旋钮位置稳定 id。"""


class RotaryRotateAction(StrEnum):
    """描述用户可绑定到旋钮左右旋转的第一阶段动作。

    入参：枚举值来自 Web API/JSON 配置。
    返回：作为 `RotaryControlBinding.rotate_action` 的稳定动作标识。
    错误处理：未知动作由 Pydantic/Enum 拒绝。
    副作用：声明枚举不执行系统或硬件动作。
    """

    UNASSIGNED = "unassigned"
    CYCLE_VIRTUAL_PANEL = "cycle_virtual_panel"
    CYCLE_PANEL_CONTENT = "cycle_panel_content"
    ADJUST_OUTPUT_VOLUME = "adjust_output_volume"
    ADJUST_INPUT_VOLUME = "adjust_input_volume"
    ADJUST_SYSTEM_DISPLAY_BRIGHTNESS = "adjust_system_display_brightness"
    ADJUST_DECK_DISPLAY_BRIGHTNESS = "adjust_deck_display_brightness"


class RotaryPressAction(StrEnum):
    """描述由旋钮旋转动作隐式派生的按下动作。

    入参：枚举值由 `press_action_for_rotate_action` 产生，不接受 GUI/JSON 独立配置。
    返回：作为 `RotaryInputIntent.press_action` 的稳定动作标识。
    错误处理：未知动作由 Pydantic/Enum 拒绝。
    副作用：声明枚举不执行系统或硬件动作。
    """

    UNASSIGNED = "unassigned"
    TOGGLE_OUTPUT_MUTE = "toggle_output_mute"
    TOGGLE_INPUT_MUTE = "toggle_input_mute"


class ConsoleLightingMode(StrEnum):
    """描述控制台灯光的两个已确认常态。

    入参：枚举值来自 GUI 草稿。
    返回：作为 `ConsoleLightingConfig.mode` 的稳定值。
    错误处理：未知值由 Pydantic/Enum 拒绝。
    副作用：声明枚举不控制 LED。
    """

    OFF = "off"
    COLOR = "color"


class ConsoleLightingConfig(BaseModel):
    """描述一个可寻址控制台灯光区域的基础视觉配置。

    入参：`mode` 只能是关闭或基础色；`color` 在基础色模式必须是六位十六进制；`breathe` 仅在
    基础色模式可用，表示使用设备 group LED 的亮度周期营造柔和呼吸。
    返回：frozen Pydantic model。
    错误处理：关闭模式携带颜色、基础色缺颜色或颜色格式非法时抛 ValueError。
    副作用：仅保存内存配置，不控制硬件。
    """

    model_config = ConfigDict(frozen=True)

    mode: ConsoleLightingMode = ConsoleLightingMode.OFF
    color: str | None = None
    breathe: bool = False

    @field_validator("color")
    @classmethod
    def _normalize_color(cls, value: str | None) -> str | None:
        """规范化并校验基础色的 HTML 十六进制表示。

        入参：`value` 是可选颜色字符串。
        返回：大写的 `#RRGGBB` 或 None。
        错误处理：非六位十六进制颜色抛 ValueError。
        副作用：无。
        """

        if value is None:
            return None
        normalized = value.strip().upper()
        if not re.fullmatch(r"#[0-9A-F]{6}", normalized):
            raise ValueError("lighting color must be a six-digit hex value")
        return normalized

    @model_validator(mode="after")
    def _validate_mode(self) -> ConsoleLightingConfig:
        """校验灯光模式与颜色字段的组合。

        入参：当前已解析配置。
        返回：当前配置。
        错误处理：不匹配组合抛 ValueError。
        副作用：无。
        """

        if self.mode == ConsoleLightingMode.OFF and self.color is not None:
            raise ValueError("off lighting must not include color")
        if self.mode == ConsoleLightingMode.OFF and self.breathe:
            raise ValueError("off lighting must not enable breathing")
        if self.mode == ConsoleLightingMode.COLOR and self.color is None:
            raise ValueError("color lighting requires color")
        return self


class RotaryControlBinding(BaseModel):
    """描述一个物理旋钮位置的用户可配置旋转动作。

    入参：`control_id` 是硬件能力模型中声明的位置 id；`rotate_action` 是唯一可配置动作，按下
    语义由该动作固定派生。旧持久化 JSON 中的 `press_action` 按 Pydantic 默认 extra-ignore
    策略忽略，保证已有配置可无感迁移。
    返回：frozen Pydantic model。
    错误处理：空 id 或未知动作由 Pydantic/Enum 拒绝。
    副作用：仅保存配置，不触发实际动作。
    """

    model_config = ConfigDict(frozen=True)

    control_id: str
    rotate_action: RotaryRotateAction = RotaryRotateAction.UNASSIGNED

    @field_validator("control_id")
    @classmethod
    def _validate_control_id(cls, value: str) -> str:
        """校验旋钮位置 id 非空。

        入参：`value` 是待保存的控制单元 id。
        返回：去除首尾空白后的 id。
        错误处理：空字符串抛 ValueError。
        副作用：无。
        """

        normalized = value.strip()
        if not normalized:
            raise ValueError("rotary control id must not be empty")
        return normalized


class N4ProRotaryLayout(BaseModel):
    """描述 N4 Pro 四个旋钮、灯圈组与整体显示亮度的完整用户布局。

    入参：`controls` 必须完整覆盖 N4 Pro 四个稳定 id；`lighting` 是唯一 group 灯光设置；
    `console_brightness_percent` 是持久化的整体设备亮度；`system_display_id` 是可选全局目标。
    返回：frozen Pydantic model，可用于 GUI 草稿、JSON store 和 daemon applied state。
    错误处理：缺漏/重复 control id、非法亮度或空显示器 id 由校验拒绝。
    副作用：仅创建内存数据，不访问硬件或操作系统。
    """

    model_config = ConfigDict(frozen=True)

    controls: tuple[RotaryControlBinding, ...]
    lighting: ConsoleLightingConfig = Field(default_factory=ConsoleLightingConfig)
    console_brightness_percent: int = Field(default=100, ge=0, le=100)
    system_display_id: str | None = None

    @field_validator("system_display_id")
    @classmethod
    def _normalize_display_id(cls, value: str | None) -> str | None:
        """清理可选系统显示器目标 id。

        入参：`value` 是 GUI 或 capability scan 提供的可选 id。
        返回：None 或去除首尾空白后的 id。
        错误处理：空字符串抛 ValueError。
        副作用：无。
        """

        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("system display id must not be empty")
        return normalized

    @model_validator(mode="after")
    def _validate_controls(self) -> N4ProRotaryLayout:
        """确保 layout 完整覆盖 N4 Pro 的四个物理旋钮。

        入参：当前已解析 layout。
        返回：当前 layout。
        错误处理：control id 集合不是既定四个 id 时抛 ValueError。
        副作用：无。
        """

        ids = tuple(binding.control_id for binding in self.controls)
        if len(ids) != len(N4PRO_ROTARY_CONTROL_IDS) or set(ids) != set(
            N4PRO_ROTARY_CONTROL_IDS
        ):
            raise ValueError("n4pro rotary layout must contain knob_1..knob_4 exactly once")
        return self

    def binding_for(self, control_id: str) -> RotaryControlBinding:
        """按物理旋钮 id 读取一个绑定。

        入参：`control_id` 是 N4 Pro 稳定旋钮 id。
        返回：匹配的 `RotaryControlBinding`。
        错误处理：未知 id 抛出 KeyError。
        副作用：无。
        """

        for binding in self.controls:
            if binding.control_id == control_id:
                return binding
        raise KeyError(f"unknown N4 Pro rotary control: {control_id}")


def default_n4pro_rotary_layout() -> N4ProRotaryLayout:
    """返回可立即理解且仍可完全改写的 N4 Pro 推荐旋钮布局。

    入参：无。
    返回：旋钮 1 轮换 virtual panel，旋钮 4 轮换当前内容，其余输入暂不设定；灯圈默认关闭，
    控制台亮度为 100%。
    错误处理：内置值若不符合 N4 Pro layout 校验会按 Pydantic 语义抛出。
    副作用：只创建内存模型。
    """

    return N4ProRotaryLayout(
        controls=(
            RotaryControlBinding(
                control_id="knob_1",
                rotate_action=RotaryRotateAction.CYCLE_VIRTUAL_PANEL,
            ),
            RotaryControlBinding(control_id="knob_2"),
            RotaryControlBinding(control_id="knob_3"),
            RotaryControlBinding(
                control_id="knob_4",
                rotate_action=RotaryRotateAction.CYCLE_PANEL_CONTENT,
            ),
        )
    )


def press_action_for_rotate_action(action: RotaryRotateAction) -> RotaryPressAction:
    """返回一个旋转动作在按下时应隐式执行的固定语义。

    入参：`action` 是某个物理旋钮已保存的旋转动作。
    返回：输出音量对应输出静音、输入音量对应麦克风静音，其余动作返回未设定。
    错误处理：枚举完整覆盖，不抛出额外业务异常。
    副作用：无；只进行纯映射，不访问配置、系统或硬件。
    """

    if action == RotaryRotateAction.ADJUST_OUTPUT_VOLUME:
        return RotaryPressAction.TOGGLE_OUTPUT_MUTE
    if action == RotaryRotateAction.ADJUST_INPUT_VOLUME:
        return RotaryPressAction.TOGGLE_INPUT_MUTE
    return RotaryPressAction.UNASSIGNED
