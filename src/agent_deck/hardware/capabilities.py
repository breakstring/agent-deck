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


class RotaryControlCapability(BaseModel):
    """描述一个物理旋钮位置可接收的独立输入通道。

    入参：`id` 是稳定硬件位置 id；`supports_rotate` 和 `supports_press` 分别描述左右旋转和
    按下是否可用。返回 frozen Pydantic model。空 id 由校验拒绝；该模型不监听 SDK 或设备。
    """

    model_config = ConfigDict(frozen=True)

    id: str
    supports_rotate: bool = True
    supports_press: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        """校验物理控制单元 id 非空。

        入参：`value` 是调用方传入的稳定 id。
        返回：去除首尾空白后的 id。
        错误处理：空字符串抛出 ValueError。
        副作用：无。
        """

        return _normalized_non_empty_string(value, field_name="rotary control id")


class RotaryCapability(BaseModel):
    """描述设备旋钮控制单元集合。

    入参：`controls` 是按物理位置列出的旋钮能力；`notes` 记录 SDK/固件限制。`count` 与
    `has_press` 作为兼容只读属性从 controls 派生，不再用于配置或 profile 构造。
    返回：frozen Pydantic model。
    错误处理：空集合、重复 id 或没有任何输入通道时由校验拒绝。
    副作用：无；只保存内存 capability。
    """

    model_config = ConfigDict(frozen=True)

    controls: tuple[RotaryControlCapability, ...]
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_controls(self) -> RotaryCapability:
        """校验控制单元集合可被安全配置。

        入参：当前已解析的 model。
        返回：当前 model。
        错误处理：空集合、重复 id 或完全不可输入的 control 抛出 ValueError。
        副作用：无。
        """

        if not self.controls:
            raise ValueError("rotary.controls must not be empty")
        ids = [control.id for control in self.controls]
        if len(ids) != len(set(ids)):
            raise ValueError("rotary.controls must have unique ids")
        if any(
            not (control.supports_rotate or control.supports_press)
            for control in self.controls
        ):
            raise ValueError("each rotary control must support rotate or press")
        return self

    @property
    def count(self) -> int:
        """返回旋钮控制单元数量的兼容读取视图。

        入参：无。
        返回：`controls` 的长度。
        错误处理：无。
        副作用：无。
        """

        return len(self.controls)

    @property
    def has_press(self) -> bool:
        """返回任一旋钮是否支持按下的兼容读取视图。

        入参：无。
        返回：至少一个 control 支持按下时为 True。
        错误处理：无。
        副作用：无。
        """

        return any(control.supports_press for control in self.controls)


class LightAddressability(StrEnum):
    """描述一个灯光区域的实际 SDK 可寻址粒度。

    入参：枚举值来自硬件 profile。
    返回：作为 `LightZoneCapability.addressability` 的稳定限制。
    错误处理：未知值由 Enum/Pydantic 拒绝。
    副作用：无。
    """

    NONE = "none"
    GLOBAL = "global"
    GROUP = "group"
    PER_CONTROL = "per_control"


class LightZoneCapability(BaseModel):
    """描述 SDK 可以真正写入的一组 LED 或背光输出。

    入参：`id` 是稳定区域 id；`addressability` 是寻址粒度；`associated_control_ids` 只用于
    在预览中关联物理控件；其余字段描述颜色、亮度和呼吸能力。
    返回：frozen Pydantic model。
    错误处理：空 id、重复关联 id 或 none 区域声明输出能力时抛 ValueError。
    副作用：无；不控制 LED。
    """

    model_config = ConfigDict(frozen=True)

    id: str
    addressability: LightAddressability
    associated_control_ids: tuple[str, ...] = ()
    supports_color: bool = False
    supports_brightness: bool = False
    supports_breathe: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        """校验灯光区域 id 非空。

        入参：`value` 是区域 id。
        返回：清理后的 id。
        错误处理：空字符串抛出 ValueError。
        副作用：无。
        """

        return _normalized_non_empty_string(value, field_name="light zone id")

    @model_validator(mode="after")
    def _validate_zone_shape(self) -> LightZoneCapability:
        """校验区域寻址与输出能力保持一致。

        入参：当前已解析 model。
        返回：当前 model。
        错误处理：重复关联 id 或 none 区域声明输出能力时抛 ValueError。
        副作用：无。
        """

        if len(self.associated_control_ids) != len(set(self.associated_control_ids)):
            raise ValueError("light zone associated_control_ids must be unique")
        if self.addressability == LightAddressability.NONE and any(
            (self.supports_color, self.supports_brightness, self.supports_breathe)
        ):
            raise ValueError("none light zone cannot support output")
        return self


class DisplayBrightnessCapability(BaseModel):
    """描述设备显示亮度可写入的范围与粒度。

    入参：`supports_set` 表示是否可以写亮度；`scope` 表示整体设备或可独立 surface。
    返回：frozen Pydantic model。
    错误处理：scope 不是两个稳定值时由 Pydantic 拒绝。
    副作用：无；不调用 SDK。
    """

    model_config = ConfigDict(frozen=True)

    supports_set: bool = False
    scope: str = "device_global"

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        """校验亮度控制的硬件作用域。

        入参：`value` 是 profile 定义的作用域。
        返回：原值。
        错误处理：非 `device_global` 或 `per_surface` 时抛出 ValueError。
        副作用：无。
        """

        if value not in {"device_global", "per_surface"}:
            raise ValueError("display brightness scope must be device_global or per_surface")
        return value


class LightCapability(BaseModel):
    """描述 LED、背光或环境状态提示能力。

    入参：遗留字段保留兼容旧 profile；`zones` 是 UI 和动作层必须使用的真实寻址输出单位。
    返回：frozen Pydantic model。
    错误处理：LED 数量非法或 zone id 重复时由校验拒绝。
    副作用：仅保存内存数据，不控制灯光。
    """

    model_config = ConfigDict(frozen=True)

    has_rgb_led: bool = False
    led_count: int | None = None
    has_keyboard_backlight: bool = False
    zones: tuple[LightZoneCapability, ...] = ()
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

    @model_validator(mode="after")
    def _validate_zones(self) -> LightCapability:
        """校验灯光区域 id 唯一。

        入参：当前已解析 model。
        返回：当前 model。
        错误处理：重复 zone id 抛出 ValueError。
        副作用：无。
        """

        ids = [zone.id for zone in self.zones]
        if len(ids) != len(set(ids)):
            raise ValueError("light zones must have unique ids")
        return self


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
    display_brightness: DisplayBrightnessCapability | None = None
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
            rotary=RotaryCapability(
                controls=tuple(
                    RotaryControlCapability(
                        id=f"knob_{index}",
                        supports_rotate=True,
                        supports_press=True,
                    )
                    for index in range(1, 5)
                )
            ),
            light=LightCapability(
                has_rgb_led=True,
                led_count=4,
                zones=(
                    LightZoneCapability(
                        id="rotary_ring_group",
                        addressability=LightAddressability.GROUP,
                        associated_control_ids=(
                            "knob_1",
                            "knob_2",
                            "knob_3",
                            "knob_4",
                        ),
                        supports_color=True,
                        supports_brightness=True,
                        supports_breathe=True,
                    ),
                ),
            ),
            display_brightness=DisplayBrightnessCapability(
                supports_set=True,
                scope="device_global",
            ),
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
            rotary=RotaryCapability(
                controls=tuple(
                    RotaryControlCapability(
                        id=f"knob_{index}",
                        supports_rotate=True,
                        supports_press=True,
                    )
                    for index in range(1, 4)
                )
            ),
            safe_action_levels=_LOW_RISK_ACTIONS,
        ),
        "mirabox.k1pro": _profile(
            device_id="mirabox.k1pro",
            display_name="MiraBox K1 Pro",
            family=ProfileFamily.KEYBOARD_COMPANION,
            key_count=6,
            key_image=KeyImageCapability(size=(64, 64), format="JPEG"),
            rotary=RotaryCapability(
                controls=tuple(
                    RotaryControlCapability(
                        id=f"knob_{index}",
                        supports_rotate=True,
                        supports_press=True,
                    )
                    for index in range(1, 4)
                )
            ),
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
                controls=tuple(
                    RotaryControlCapability(
                        id=f"knob_{index}",
                        supports_rotate=True,
                        supports_press=False,
                    )
                    for index in range(1, 3)
                ),
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
