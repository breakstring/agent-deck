"""Agent Deck 硬件显示外观模型与调色板解析。

本模块定义跨 Key 与 virtual panel 共用的可选基础背景色，并从用户颜色推导可读的
前景、中性表面和分隔线。它不读取配置文件、不访问 Web 或真实硬件；各渲染器仍可在
未设置覆盖值时保留自己的既有默认调色板。
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator

RGBColor = tuple[int, int, int]
"""Pillow 渲染器使用的 8-bit RGB 三元组。"""

_HEX_COLOR_RE: Final[re.Pattern[str]] = re.compile(r"^#[0-9A-Fa-f]{6}$")


class DeckAppearanceSettings(BaseModel):
    """定义 Agent Deck 生成硬件画面时使用的全局显示外观。

    入参：``background_color`` 仅接受不透明 ``#RRGGBB``，None 表示保留各渲染器默认值。
    返回：冻结、禁止额外字段的配置模型。
    错误处理：简写、透明色、命名色或非法字符由 Pydantic 拒绝。
    副作用：无；模型不读写文件或硬件。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    background_color: str | None = None

    @field_validator("background_color")
    @classmethod
    def _validate_background_color(cls, value: str | None) -> str | None:
        """校验并规范化可选背景色。

        入参：用户或持久化文件提供的颜色字符串。
        返回：None 或大写 ``#RRGGBB``。
        错误处理：格式不精确时抛 ``ValueError``。
        副作用：无。
        """

        if value is None:
            return None
        normalized = value.strip()
        if not _HEX_COLOR_RE.fullmatch(normalized):
            raise ValueError("background_color must use opaque #RRGGBB format")
        return normalized.upper()


class RenderPalette(BaseModel):
    """提供一次硬件画面渲染所需的基础中性色。

    入参：所有颜色均为 0..255 RGB 三元组；``custom`` 标识是否来自用户覆盖。
    返回：冻结调色板，可安全跨缓存和渲染入口传递。
    错误处理：元组形状或数值非法由 Pydantic 拒绝。
    副作用：无。
    """

    model_config = ConfigDict(frozen=True)

    background: RGBColor
    foreground: RGBColor
    muted_foreground: RGBColor
    surface: RGBColor
    divider: RGBColor
    custom: bool = False

    @property
    def cache_key(self) -> str:
        """返回可进入派生图缓存键的稳定调色板标识。

        入参：无。
        返回：默认调色板为 ``default``；自定义调色板使用背景色十六进制。
        错误处理：无；模型构造时已校验 RGB。
        副作用：无。
        """

        if not self.custom:
            return "default"
        return "#" + "".join(f"{channel:02X}" for channel in self.background)


def resolve_render_palette(
    settings: DeckAppearanceSettings | None,
    *,
    default_background: RGBColor,
    default_foreground: RGBColor = (238, 244, 255),
    default_muted_foreground: RGBColor = (145, 160, 182),
    default_surface: RGBColor = (18, 24, 36),
    default_divider: RGBColor = (34, 44, 64),
) -> RenderPalette:
    """为一个渲染器解析默认或用户覆盖调色板。

    入参：``settings`` 是可选全局外观；其余参数是该渲染器改动前使用的既有中性色。
    返回：未设置覆盖时原样保留默认值；设置后从用户背景推导对比前景和中性层级。
    错误处理：配置模型已保证颜色合法；RGB 运算不会访问外部状态。
    副作用：无。
    """

    if settings is None or settings.background_color is None:
        return RenderPalette(
            background=default_background,
            foreground=default_foreground,
            muted_foreground=default_muted_foreground,
            surface=default_surface,
            divider=default_divider,
            custom=False,
        )
    background = rgb_from_hex(settings.background_color)
    foreground = _best_foreground(background)
    return RenderPalette(
        background=background,
        foreground=foreground,
        muted_foreground=_mix(background, foreground, 0.62),
        surface=_surface_for_background(background),
        divider=_mix(background, foreground, 0.24),
        custom=True,
    )


def rgb_from_hex(value: str) -> RGBColor:
    """把已校验或独立传入的 ``#RRGGBB`` 转为 RGB。

    入参：必须是完整不透明十六进制颜色。
    返回：三个 0..255 整数。
    错误处理：格式非法时抛 ``ValueError``。
    副作用：无。
    """

    if not _HEX_COLOR_RE.fullmatch(value):
        raise ValueError("color must use opaque #RRGGBB format")
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))


def appearance_cache_key(settings: DeckAppearanceSettings | None) -> str:
    """返回外观设置的稳定缓存身份。

    入参：可选外观设置。
    返回：未覆盖时为 ``default``，否则为规范化十六进制颜色。
    错误处理：无。
    副作用：无。
    """

    if settings is None or settings.background_color is None:
        return "default"
    return settings.background_color


def contrast_ratio(first: RGBColor, second: RGBColor) -> float:
    """计算两种 RGB 颜色的 WCAG 相对亮度对比比。

    入参：两个 8-bit RGB 三元组。
    返回：1.0..21.0 的对比比，主要供自动化测试和诊断使用。
    错误处理：调用方应传入合法 RGB；越界值会被亮度公式自然处理。
    副作用：无。
    """

    lighter = max(_relative_luminance(first), _relative_luminance(second))
    darker = min(_relative_luminance(first), _relative_luminance(second))
    return (lighter + 0.05) / (darker + 0.05)


def _best_foreground(background: RGBColor) -> RGBColor:
    """在近黑和近白前景中选择对比度更高者。

    入参：用户基础背景。
    返回：适合小屏文字的深色或浅色前景。
    错误处理：无。
    副作用：无。
    """

    dark = (13, 18, 24)
    light = (244, 248, 252)
    return (
        dark
        if contrast_ratio(background, dark) >= contrast_ratio(background, light)
        else light
    )


def _surface_for_background(background: RGBColor) -> RGBColor:
    """从背景推导可辨识但克制的卡片表面。

    入参：用户基础背景。
    返回：浅背景略向黑混合，深背景略向白混合的 RGB。
    错误处理：无。
    副作用：无。
    """

    if _relative_luminance(background) >= 0.45:
        return _mix(background, (0, 0, 0), 0.10)
    return _mix(background, (255, 255, 255), 0.10)


def _mix(base: RGBColor, overlay: RGBColor, amount: float) -> RGBColor:
    """按比例混合两种 RGB 颜色。

    入参：``amount`` 为 overlay 权重，调用方传入 0..1。
    返回：四舍五入后的 RGB 三元组。
    错误处理：权重由内部常量控制，不主动抛错。
    副作用：无。
    """

    return tuple(
        round(base[index] * (1.0 - amount) + overlay[index] * amount)
        for index in range(3)
    )


def _relative_luminance(color: RGBColor) -> float:
    """计算 sRGB 相对亮度。

    入参：8-bit RGB 三元组。
    返回：0..1 的线性相对亮度。
    错误处理：无。
    副作用：无。
    """

    channels = []
    for channel in color:
        value = channel / 255
        channels.append(
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        )
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
