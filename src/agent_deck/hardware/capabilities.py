"""Mirabox / StreamDock 硬件能力 profile 数据模型。

本模块只描述设备 capability，不导入官方 StreamDock SDK、不枚举 HID、不渲染图片、不读写配置、
不执行任何硬件操作。它为后续 layout、input intent 和 action safety gate 提供稳定的纯数据
入口，避免把 N4 Pro、N4、N3、K1 Pro 等型号能力写死在渲染或动作执行代码里。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProfileFamily(StrEnum):
    """描述 Agent Deck 如何使用某类硬件能力。

    入参：枚举值是稳定的 profile family 字符串。
    返回：作为 `DeviceCapabilityProfile.family` 的分类约束。
    错误处理：未知字符串由 Pydantic / Enum 校验拒绝。
    副作用：无；声明枚举不会访问外部状态。
    """

    KEY_GRID = "key_grid"
    KEY_GRID_WITH_STATUS_STRIP = "key_grid_with_status_strip"
    ROTARY_CONTROL = "rotary_control"
    RICH_TOUCH_ROTARY = "rich_touch_rotary"
    KEYBOARD_COMPANION = "keyboard_companion"
    DASHBOARD_GRID = "dashboard_grid"


class SafeActionLevel(StrEnum):
    """描述硬件 profile 默认允许承载的最高动作等级。

    入参：枚举值按产品安全策略命名，不直接等价于真实 action executor 权限。
    返回：可放入 `DeviceCapabilityProfile.safe_action_levels` 中供策略层查询。
    错误处理：未知动作等级由 Pydantic / Enum 校验拒绝。
    副作用：无；不触发任何动作。
    """

    OBSERVE = "observe"
    NAVIGATE = "navigate"
    LAUNCH = "launch"
    FOCUS = "focus"
    DECIDE = "decide"
    INPUT = "input"


class KeyImageCapability(BaseModel):
    """描述可绘制按键图像能力。

    入参：`size` 是渲染目标尺寸；`format` 是 SDK 下发前推荐编码格式；`supports_animation`
    表示该 profile 是否允许 key 动画路径；`notes` 记录已知限制。
    返回：frozen Pydantic model，可嵌入 `DeviceCapabilityProfile`。
    错误处理：非正尺寸或空格式由校验拒绝。
    副作用：仅保存内存数据。
    """

    model_config = ConfigDict(frozen=True)

    size: tuple[int, int]
    format: str
    supports_animation: bool = False
    notes: tuple[str, ...] = ()

    @field_validator("size")
    @classmethod
    def _validate_size(cls, value: tuple[int, int]) -> tuple[int, int]:
        """校验按键图尺寸必须为正数。

        入参：`value` 是 Pydantic 解析后的 `(width, height)`。
        返回：原始尺寸元组。
        错误处理：任一维度小于等于 0 时抛出 ValueError。
        副作用：无。
        """

        _ensure_positive_size(value, field_name="key_image.size")
        return value

    @field_validator("format")
    @classmethod
    def _validate_format(cls, value: str) -> str:
        """校验图像格式字符串非空。

        入参：`value` 是调用方传入的格式名。
        返回：去除首尾空白后的格式名。
        错误处理：空字符串由 ValueError 拒绝。
        副作用：无。
        """

        return _normalized_non_empty_string(value, field_name="key_image.format")


class BackgroundSurfaceCapability(BaseModel):
    """描述设备背景屏或大画布渲染能力。

    入参：`size` 是背景 surface 尺寸；`format` 是推荐编码格式；`supports_animation` 表示
    是否适合走 background GIF/MP4 或逐帧动画；`max_recommended_fps` 是保守刷新建议。
    返回：frozen Pydantic model。
    错误处理：尺寸、格式或 fps 非法时由校验拒绝。
    副作用：仅保存内存数据。
    """

    model_config = ConfigDict(frozen=True)

    size: tuple[int, int]
    format: str
    supports_animation: bool = False
    max_recommended_fps: int | None = None
    notes: tuple[str, ...] = ()

    @field_validator("size")
    @classmethod
    def _validate_size(cls, value: tuple[int, int]) -> tuple[int, int]:
        """校验背景 surface 尺寸必须为正数。

        入参：`value` 是 `(width, height)`。
        返回：原始尺寸元组。
        错误处理：任一维度小于等于 0 时抛出 ValueError。
        副作用：无。
        """

        _ensure_positive_size(value, field_name="background.size")
        return value

    @field_validator("format")
    @classmethod
    def _validate_format(cls, value: str) -> str:
        """校验背景图格式字符串非空。

        入参：`value` 是调用方传入的格式名。
        返回：去除首尾空白后的格式名。
        错误处理：空字符串由 ValueError 拒绝。
        副作用：无。
        """

        return _normalized_non_empty_string(value, field_name="background.format")

    @field_validator("max_recommended_fps")
    @classmethod
    def _validate_fps(cls, value: int | None) -> int | None:
        """校验推荐刷新率必须为正数。

        入参：`value` 是可选 fps。
        返回：原始 fps 或 None。
        错误处理：非正 fps 由 ValueError 拒绝。
        副作用：无。
        """

        if value is not None and value <= 0:
            raise ValueError("background.max_recommended_fps must be positive")
        return value


class TouchCapability(BaseModel):
    """描述触控输入能力。

    入参：`has_touch_points` 表示是否能读取坐标点；`has_swipe` 表示是否能读取滑动手势；
    `notes` 记录固件或 SDK 差异。
    返回：frozen Pydantic model。
    错误处理：字段类型非法由 Pydantic 报告。
    副作用：仅保存内存数据。
    """

    model_config = ConfigDict(frozen=True)

    has_touch_points: bool = False
    has_swipe: bool = False
    notes: tuple[str, ...] = ()


class RotaryCapability(BaseModel):
    """描述旋钮输入能力。

    入参：`count` 是旋钮数量；`has_press` 表示是否能读取旋钮按下；`notes` 记录映射限制。
    返回：frozen Pydantic model。
    错误处理：旋钮数量小于等于 0 时由校验拒绝。
    副作用：仅保存内存数据。
    """

    model_config = ConfigDict(frozen=True)

    count: int
    has_press: bool = False
    notes: tuple[str, ...] = ()

    @field_validator("count")
    @classmethod
    def _validate_count(cls, value: int) -> int:
        """校验旋钮数量必须为正数。

        入参：`value` 是旋钮数量。
        返回：原始数量。
        错误处理：小于等于 0 时抛出 ValueError。
        副作用：无。
        """

        if value <= 0:
            raise ValueError("rotary.count must be positive")
        return value


class LightCapability(BaseModel):
    """描述 LED、背光或环境状态提示能力。

    入参：`has_rgb_led`、`led_count`、`has_keyboard_backlight` 和 `notes` 描述可用灯光能力。
    返回：frozen Pydantic model。
    错误处理：`led_count` 为非正数时由校验拒绝。
    副作用：仅保存内存数据，不控制灯光。
    """

    model_config = ConfigDict(frozen=True)

    has_rgb_led: bool = False
    led_count: int | None = None
    has_keyboard_backlight: bool = False
    notes: tuple[str, ...] = ()

    @field_validator("led_count")
    @classmethod
    def _validate_led_count(cls, value: int | None) -> int | None:
        """校验 LED 数量字段。

        入参：`value` 是可选 LED 数量。
        返回：原始数量或 None。
        错误处理：非正数量由 ValueError 拒绝。
        副作用：无。
        """

        if value is not None and value <= 0:
            raise ValueError("light.led_count must be positive")
        return value


class DeviceCapabilityProfile(BaseModel):
    """描述一个硬件 profile 可被 Agent Deck 安全使用的能力边界。

    入参：字段覆盖设备 id、展示名、profile family、可绘制 key 数、图像 surface、触控、
    旋钮、灯光、安全动作等级、复合写入要求和已知限制。
    返回：frozen Pydantic model，可被 layout、input router 和安全策略层只读引用。
    错误处理：字段缺失、尺寸非法、默认开启 input 或 key_count/key_image 不匹配时由校验拒绝。
    副作用：仅创建内存对象，不导入 SDK 或访问硬件。
    """

    model_config = ConfigDict(frozen=True)

    device_id: str
    display_name: str
    family: ProfileFamily
    key_count: int
    key_image: KeyImageCapability | None = None
    secondary_key_count: int = 0
    background: BackgroundSurfaceCapability | None = None
    touch: TouchCapability | None = None
    rotary: RotaryCapability | None = None
    light: LightCapability | None = None
    safe_action_levels: frozenset[SafeActionLevel] = Field(
        default_factory=lambda: frozenset({SafeActionLevel.OBSERVE})
    )
    requires_single_session_composite_write: bool = False
    known_limitations: tuple[str, ...] = ()

    @field_validator("device_id", "display_name")
    @classmethod
    def _validate_non_empty_strings(cls, value: str) -> str:
        """校验 profile 基础字符串字段非空。

        入参：`value` 是设备 id 或展示名。
        返回：去除首尾空白后的字符串。
        错误处理：空字符串由 ValueError 拒绝。
        副作用：无。
        """

        return _normalized_non_empty_string(value, field_name="profile string")

    @field_validator("key_count")
    @classmethod
    def _validate_key_count(cls, value: int) -> int:
        """校验可绘制 key 数量必须为正数。

        入参：`value` 是 profile 的 key 数。
        返回：原始 key 数。
        错误处理：小于等于 0 时抛出 ValueError。
        副作用：无。
        """

        if value <= 0:
            raise ValueError("key_count must be positive")
        return value

    @field_validator("secondary_key_count")
    @classmethod
    def _validate_secondary_key_count(cls, value: int) -> int:
        """校验 secondary key 数量不能为负。

        入参：`value` 是 secondary key 数量。
        返回：原始数量。
        错误处理：负数由 ValueError 拒绝。
        副作用：无。
        """

        if value < 0:
            raise ValueError("secondary_key_count must not be negative")
        return value

    @model_validator(mode="after")
    def _validate_shape(self) -> DeviceCapabilityProfile:
        """校验跨字段 profile 约束。

        入参：当前已解析模型实例。
        返回：当前实例。
        错误处理：有 key 却缺少 key_image、默认开放 input、touch 缺少 background 时抛出
        ValueError，由 Pydantic 包装为 ValidationError。
        副作用：无。
        """

        if self.key_count > 0 and self.key_image is None:
            raise ValueError("key_image is required when key_count is positive")
        if SafeActionLevel.INPUT in self.safe_action_levels:
            raise ValueError("INPUT cannot be enabled in a hardware profile by default")
        if self.touch is not None and self.background is None:
            raise ValueError("touch capability requires a background surface")
        return self

    def supports_action(self, level: SafeActionLevel) -> bool:
        """判断该 profile 默认是否允许承载某个动作等级。

        入参：`level` 是要查询的安全动作等级。
        返回：当 `level` 出现在 `safe_action_levels` 中时为 True。
        错误处理：非法枚举值在调用前应由类型检查或 Enum 构造拒绝；本方法不额外转换字符串。
        副作用：无；只读取当前模型字段。
        """

        return level in self.safe_action_levels


def built_in_device_profiles() -> Mapping[str, DeviceCapabilityProfile]:
    """返回内置设备能力 profile 的只读 registry。

    入参：无。
    返回：mapping proxy，key 是稳定 profile id，value 是 frozen `DeviceCapabilityProfile`。
    错误处理：无业务异常；调用方尝试修改返回 mapping 时 Python 会抛出 TypeError。
    副作用：无；只返回模块初始化时构造好的内存对象。
    """

    return _BUILT_IN_DEVICE_PROFILES


def get_device_profile(device_id: str) -> DeviceCapabilityProfile:
    """按 id 读取一个内置硬件能力 profile。

    入参：`device_id` 是稳定 profile id，例如 `mirabox.n4pro`。
    返回：对应的 frozen `DeviceCapabilityProfile`。
    错误处理：未知 id 抛出 KeyError，错误信息包含 `unknown device capability profile`。
    副作用：无；不访问硬件或文件系统。
    """

    try:
        return _BUILT_IN_DEVICE_PROFILES[device_id]
    except KeyError as exc:
        raise KeyError(f"unknown device capability profile: {device_id}") from exc


def _profile(**kwargs: Any) -> DeviceCapabilityProfile:
    """构造内置 profile 的小型 helper。

    入参：`kwargs` 直接透传给 `DeviceCapabilityProfile`。
    返回：校验后的 frozen profile。
    错误处理：字段不合法时由 Pydantic 抛出 ValidationError。
    副作用：无；仅减少 registry 字面量重复。
    """

    return DeviceCapabilityProfile(**kwargs)


def _normalized_non_empty_string(value: str, *, field_name: str) -> str:
    """校验并规范化非空字符串。

    入参：`value` 是待校验字符串；`field_name` 用于错误信息。
    返回：去除首尾空白后的字符串。
    错误处理：空字符串由 ValueError 拒绝。
    副作用：无。
    """

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _ensure_positive_size(value: tuple[int, int], *, field_name: str) -> None:
    """校验尺寸元组两个维度均为正数。

    入参：`value` 是 `(width, height)`；`field_name` 用于错误信息。
    返回：无。
    错误处理：宽或高小于等于 0 时抛出 ValueError。
    副作用：无。
    """

    width, height = value
    if width <= 0 or height <= 0:
        raise ValueError(f"{field_name} must contain positive dimensions")


_LOW_RISK_ACTIONS = frozenset(
    {
        SafeActionLevel.OBSERVE,
        SafeActionLevel.NAVIGATE,
        SafeActionLevel.LAUNCH,
        SafeActionLevel.FOCUS,
    }
)

_RICH_CONTEXT_ACTIONS = frozenset(
    {
        SafeActionLevel.OBSERVE,
        SafeActionLevel.NAVIGATE,
        SafeActionLevel.LAUNCH,
        SafeActionLevel.FOCUS,
        SafeActionLevel.DECIDE,
    }
)

_BUILT_IN_DEVICE_PROFILES = MappingProxyType(
    {
        "mirabox.n4pro": _profile(
            device_id="mirabox.n4pro",
            display_name="MiraBox Stream Dock N4 Pro",
            family=ProfileFamily.RICH_TOUCH_ROTARY,
            key_count=15,
            key_image=KeyImageCapability(
                size=(96, 96),
                format="PNG",
                supports_animation=True,
                notes=("SDK maps 15 logical keys across main and secondary surfaces.",),
            ),
            secondary_key_count=5,
            background=BackgroundSurfaceCapability(
                size=(800, 480),
                format="JPEG",
                supports_animation=True,
                max_recommended_fps=12,
                notes=("Use set_frame_background for composite key/background writes.",),
            ),
            touch=TouchCapability(
                has_touch_points=True,
                has_swipe=True,
                notes=("Touch coordinates are decoded from N4 Pro touch-bar packets.",),
            ),
            rotary=RotaryCapability(count=4, has_press=True),
            light=LightCapability(has_rgb_led=True),
            safe_action_levels=_RICH_CONTEXT_ACTIONS,
            requires_single_session_composite_write=True,
            known_limitations=(
                "set_touchscreen_image may cover or clear key layers; prefer frame background.",
            ),
        ),
        "mirabox.n4": _profile(
            device_id="mirabox.n4",
            display_name="MiraBox Stream Dock N4",
            family=ProfileFamily.KEY_GRID_WITH_STATUS_STRIP,
            key_count=14,
            key_image=KeyImageCapability(size=(112, 112), format="JPEG"),
            secondary_key_count=4,
            background=BackgroundSurfaceCapability(size=(800, 480), format="JPEG"),
            safe_action_levels=_LOW_RISK_ACTIONS,
            known_limitations=(
                "Official and Companion docs mention knobs/touch strip/swipe, but current vendored Python SDK exposure is unverified.",
            ),
        ),
        "mirabox.n3": _profile(
            device_id="mirabox.n3",
            display_name="MiraBox Stream Dock N3",
            family=ProfileFamily.ROTARY_CONTROL,
            key_count=9,
            key_image=KeyImageCapability(size=(64, 64), format="JPEG"),
            background=BackgroundSurfaceCapability(
                size=(320, 240),
                format="JPEG",
                notes=("Background surface exists, but touch-point input is not modeled.",),
            ),
            rotary=RotaryCapability(count=3, has_press=True),
            safe_action_levels=_LOW_RISK_ACTIONS,
        ),
        "mirabox.k1pro": _profile(
            device_id="mirabox.k1pro",
            display_name="MiraBox K1 Pro",
            family=ProfileFamily.KEYBOARD_COMPANION,
            key_count=6,
            key_image=KeyImageCapability(size=(64, 64), format="JPEG"),
            rotary=RotaryCapability(count=3, has_press=True),
            light=LightCapability(has_keyboard_backlight=True),
            safe_action_levels=_LOW_RISK_ACTIONS,
            known_limitations=(
                "Treat as keyboard companion; do not inject text into unknown foreground apps.",
            ),
        ),
        "mirabox.key_grid_15": _profile(
            device_id="mirabox.key_grid_15",
            display_name="Generic MiraBox 15-Key Grid",
            family=ProfileFamily.KEY_GRID,
            key_count=15,
            key_image=KeyImageCapability(
                size=(100, 100),
                format="JPEG",
                supports_animation=True,
            ),
            safe_action_levels=_LOW_RISK_ACTIONS,
            known_limitations=(
                "Key-only profile must use desktop details for rich permission context.",
            ),
        ),
        "mirabox.xl": _profile(
            device_id="mirabox.xl",
            display_name="MiraBox Stream Dock XL",
            family=ProfileFamily.DASHBOARD_GRID,
            key_count=32,
            key_image=KeyImageCapability(
                size=(80, 80),
                format="PNG",
                supports_animation=True,
            ),
            background=BackgroundSurfaceCapability(
                size=(1024, 600),
                format="JPEG",
                supports_animation=True,
                max_recommended_fps=10,
            ),
            rotary=RotaryCapability(
                count=2,
                has_press=False,
                notes=("Vendored SDK exposes rotation events only.",),
            ),
            light=LightCapability(has_rgb_led=True, led_count=6),
            safe_action_levels=_LOW_RISK_ACTIONS,
            known_limitations=(
                "Large dashboard profile should benchmark refresh rate before enabling dense animation.",
            ),
        ),
    }
)
