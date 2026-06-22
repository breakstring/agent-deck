"""Codex quota 的 N4 Pro 虚拟面板渲染器。

本模块把 `CodexQuotaSnapshot` 渲染为底部虚拟 panel，再通过 N4 Pro background
composer 合成到 SDK 可下发的 800x480 背景图。它不读取 Codex、不访问 StreamDock
设备、不启动 daemon、不写文件，也不修改任何运行状态。真实硬件下发由调用方负责。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from agent_deck.adapters.codex_quota import CodexQuotaSnapshot
from agent_deck.rendering.n4pro_panel import (
    N4PRO_BACKGROUND_COLOR,
    N4PRO_BACKGROUND_SIZE,
    N4PRO_TOUCH_BAR_VIEWPORT,
    VirtualPanelViewport,
    compose_n4pro_background,
)

N4PRO_TOUCH_BAR_RECT: Final[tuple[int, int, int, int]] = (
    N4PRO_TOUCH_BAR_VIEWPORT.left,
    N4PRO_TOUCH_BAR_VIEWPORT.top,
    N4PRO_TOUCH_BAR_VIEWPORT.right,
    N4PRO_TOUCH_BAR_VIEWPORT.bottom,
)
N4PRO_TOUCHSCREEN_SIZE: Final[tuple[int, int]] = N4PRO_BACKGROUND_SIZE

_BACKGROUND: Final[tuple[int, int, int]] = N4PRO_BACKGROUND_COLOR
_PANEL: Final[tuple[int, int, int]] = (18, 24, 36)
_TEXT: Final[tuple[int, int, int]] = (238, 244, 255)
_MUTED: Final[tuple[int, int, int]] = (145, 160, 182)
_TRACK: Final[tuple[int, int, int]] = (44, 54, 76)
_PRIMARY: Final[tuple[int, int, int]] = (76, 205, 255)
_SECONDARY: Final[tuple[int, int, int]] = (126, 236, 165)
_RESET_CREDIT: Final[tuple[int, int, int]] = (248, 213, 113)


def render_quota_touchscreen(
    snapshot: CodexQuotaSnapshot,
    *,
    size: tuple[int, int] = N4PRO_BACKGROUND_SIZE,
    touch_bar_rect: tuple[int, int, int, int] = N4PRO_TOUCH_BAR_RECT,
) -> Image.Image:
    """把 Codex quota 快照渲染为 N4 Pro 背景图。

    入参：`snapshot` 是 Codex quota adapter 解析出的快照；`size` 是 SDK 背景图尺寸，
    默认 N4 Pro 的 800x480；`touch_bar_rect` 是背景图中真实底部触摸条的安全绘制区域，
    格式为 `(left, top, right, bottom)`。
    返回：RGB `Image`，可保存为 JPEG 后通过 SDK `set_touchscreen_image` 下发；信息只绘制
    在 `touch_bar_rect` 内，其余区域保持背景色，避免内容透到按键窗口。
    错误处理：Pillow 字体加载失败时自动退回默认字体；非法尺寸或非法矩形会抛异常。
    副作用：只创建内存图像，不访问文件、网络或硬件。
    """

    viewport = VirtualPanelViewport(*touch_bar_rect)
    panel = render_quota_panel(snapshot, size=viewport.size)
    return compose_n4pro_background(panel, viewport=viewport, background_size=size)


def render_quota_panel(
    snapshot: CodexQuotaSnapshot,
    *,
    size: tuple[int, int] = N4PRO_TOUCH_BAR_VIEWPORT.size,
) -> Image.Image:
    """把 Codex quota 快照渲染为底部虚拟 panel 图像。

    入参：`snapshot` 是 Codex quota adapter 解析出的快照；`size` 是 panel 自身尺寸，
    默认使用 N4 Pro touch-bar viewport 尺寸。
    返回：RGB `Image`，只包含 panel 内容，不包含 N4 Pro 整屏背景。
    错误处理：Pillow 字体加载失败时自动退回默认字体；尺寸过小时抛 `ValueError`。
    副作用：只创建内存图像，不访问文件、网络或硬件。
    """

    _validate_panel_size(size)
    image = Image.new("RGB", size, _BACKGROUND)
    draw = ImageDraw.Draw(image)
    width, height = size
    left, top, right, bottom = 0, 0, width, height
    bar_height = height
    inset_x = 34
    inset_y = 14
    plan_width = 222
    content_left = left + inset_x
    content_top = top + inset_y
    content_right = right - inset_x
    content_bottom = bottom - inset_y

    _draw_panel(draw, (left + 18, top + 8, right - 18, bottom - 8))
    draw.line((left + 28, top + 10, right - 28, top + 10), fill=(34, 44, 64), width=1)

    title_font = _load_font(52, bold=True)
    label_font = _load_font(22, bold=True)
    value_font = _load_font(17, bold=False)
    percent_font = _load_font(17, bold=True)
    reset_credit_font = _load_font(18, bold=True)

    title_y = top + max(18, (bar_height - 62) // 2)
    plan_label = snapshot.plan_short_label or snapshot.plan_display_name
    draw.text((content_left, title_y), plan_label, fill=_TEXT, font=title_font)
    _draw_reset_credit_marker(
        draw,
        available_count=snapshot.reset_credits_available,
        origin=(content_left + 2, title_y + 61),
        icon_size=17,
        font=reset_credit_font,
    )

    right_x = left + plan_width + 46
    row_gap = max(48, (content_bottom - content_top - 38) // 2)
    _draw_quota_row(
        draw,
        label="5hours:",
        remaining_percent=_remaining_percent(snapshot.primary.used_percent),
        reset_label=snapshot.primary_reset_label(),
        origin=(right_x, content_top + 5),
        max_right=content_right,
        bar_color=_PRIMARY,
        label_font=label_font,
        value_font=value_font,
        percent_font=percent_font,
    )
    _draw_quota_row(
        draw,
        label="weekly:",
        remaining_percent=_remaining_percent(snapshot.secondary.used_percent),
        reset_label=snapshot.secondary_reset_label(),
        origin=(right_x, content_top + 5 + row_gap),
        max_right=content_right,
        bar_color=_SECONDARY,
        label_font=label_font,
        value_font=value_font,
        percent_font=percent_font,
    )
    return image


def _remaining_percent(used_percent: int) -> int:
    """把 Codex app-server 的已用百分比转换为剩余百分比。

    入参：`used_percent` 是 app-server `usedPercent` 字段，语义为已使用 quota 百分比。
    返回：0-100 内的剩余 quota 百分比，即 `100 - used_percent` 后再夹紧边界。
    错误处理：非整数类型由调用方类型约束处理；越界数值会被夹紧。
    副作用：无。
    """

    used = max(0, min(100, used_percent))
    return 100 - used


def _validate_panel_size(size: tuple[int, int]) -> None:
    """校验 quota panel 尺寸是否足够绘制当前布局。

    入参：`size` 是 panel 图像尺寸。
    返回：无返回值；校验通过表示尺寸可用于当前 quota 布局。
    错误处理：尺寸太小时抛出 `ValueError`。
    副作用：无。
    """

    width, height = size
    if width < 600 or height < 96:
        raise ValueError("panel size is too small for the quota layout")


def _draw_panel(draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int]) -> None:
    """绘制触屏信息面板背景。

    入参：`draw` 是目标绘图对象；`bounds` 是 `(left, top, right, bottom)`。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像。
    """

    draw.rounded_rectangle(bounds, radius=24, fill=_PANEL)


def _draw_quota_row(
    draw: ImageDraw.ImageDraw,
    *,
    label: str,
    remaining_percent: int,
    reset_label: str,
    origin: tuple[int, int],
    max_right: int,
    bar_color: tuple[int, int, int],
    label_font: ImageFont.ImageFont,
    value_font: ImageFont.ImageFont,
    percent_font: ImageFont.ImageFont,
) -> None:
    """绘制一行 quota 进度信息。

    入参：`draw` 是绘图对象；`label` 是行标题；`remaining_percent` 是 0-100 剩余配额百分比；
    `reset_label` 是重置时间文本；`origin` 是行左上角；`max_right` 是本行可绘制右边界；
    `bar_color` 是进度条颜色；`label_font`/`value_font`/`percent_font` 是对应字体。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像。
    """

    x, y = origin
    bar_x = x + 116
    bar_y = y + 9
    reset_w = 92
    reset_gap = 16
    icon_size = 12
    icon_text_gap = 8
    bar_w = max(92, max_right - bar_x - reset_gap - icon_size - icon_text_gap - reset_w)
    bar_h = 16
    percent = max(0, min(100, remaining_percent))
    bar_center_y = bar_y + bar_h // 2
    icon_x = bar_x + bar_w + reset_gap
    icon_center = (icon_x + icon_size // 2, bar_center_y)
    reset_x = icon_x + icon_size + icon_text_gap

    draw.text((x, bar_center_y), label, fill=_TEXT, font=label_font, anchor="lm")
    draw.rounded_rectangle(
        (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
        radius=bar_h // 2,
        fill=_TRACK,
    )
    fill_w = round(bar_w * percent / 100)
    if fill_w > 0:
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + fill_w, bar_y + bar_h),
            radius=bar_h // 2,
            fill=bar_color,
        )
    _draw_reset_icon(draw, icon_center, icon_size)
    draw.text((bar_x, y + 29), f"{percent}%", fill=bar_color, font=percent_font)
    draw.text((reset_x, bar_center_y), reset_label, fill=_MUTED, font=value_font, anchor="lm")


def _draw_reset_icon(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    size: int,
) -> None:
    """绘制 reset 时间前的小钟表图标。

    入参：`draw` 是绘图对象；`center` 是图标中心点；`size` 是图标外径像素。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像。
    """

    cx, cy = center
    radius = size // 2
    left = cx - radius
    top = cy - radius
    right = cx + radius
    bottom = cy + radius
    draw.ellipse((left, top, right, bottom), outline=_MUTED, width=2)
    draw.line((cx, cy, cx, cy - radius + 3), fill=_MUTED, width=2)
    draw.line((cx, cy, cx + radius - 3, cy), fill=_MUTED, width=2)


def _draw_reset_credit_marker(
    draw: ImageDraw.ImageDraw,
    *,
    available_count: int | None,
    origin: tuple[int, int],
    icon_size: int,
    font: ImageFont.ImageFont,
) -> None:
    """在订阅标签下方绘制可用 reset credit 标识。

    入参：`draw` 是绘图对象；`available_count` 是可用 reset 数，None 或 0 表示不绘制；
    `origin` 是图标左上角；`icon_size` 是钥匙图标尺寸；`font` 是数字字体。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像。
    """

    if available_count is None or available_count <= 0:
        return
    x, y = origin
    _draw_key_icon(draw, (x, y + 3), icon_size)
    draw.text(
        (x + icon_size + 8, y + icon_size // 2),
        str(available_count),
        fill=_RESET_CREDIT,
        font=font,
        anchor="lm",
    )


def _draw_key_icon(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    size: int,
) -> None:
    """绘制一个小钥匙图标。

    入参：`draw` 是绘图对象；`origin` 是图标左上角；`size` 是图标尺寸。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像；不依赖系统 emoji 字体。
    """

    x, y = origin
    bow_radius = max(4, size // 4)
    bow_cx = x + bow_radius + 1
    bow_cy = y + bow_radius + 1
    shaft_y = bow_cy
    shaft_start = bow_cx + bow_radius
    shaft_end = x + size - 1
    tooth_x = shaft_end - max(4, size // 4)
    tooth_h = max(4, size // 4)

    draw.ellipse(
        (
            bow_cx - bow_radius,
            bow_cy - bow_radius,
            bow_cx + bow_radius,
            bow_cy + bow_radius,
        ),
        outline=_RESET_CREDIT,
        width=2,
    )
    draw.line((shaft_start, shaft_y, shaft_end, shaft_y), fill=_RESET_CREDIT, width=3)
    draw.line((tooth_x, shaft_y, tooth_x, shaft_y + tooth_h), fill=_RESET_CREDIT, width=3)
    draw.line(
        (shaft_end - 2, shaft_y, shaft_end - 2, shaft_y + tooth_h - 1),
        fill=_RESET_CREDIT,
        width=2,
    )


def _load_font(size: int, *, bold: bool) -> ImageFont.ImageFont:
    """加载 macOS 常见字体，失败时退回 Pillow 默认字体。

    入参：`size` 是字号；`bold` 控制是否优先加载粗体。
    返回：Pillow 字体对象。
    错误处理：所有字体加载失败时返回默认字体。
    副作用：只读取系统字体文件，不写文件、不访问网络或硬件。
    """

    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/SFNS.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()
