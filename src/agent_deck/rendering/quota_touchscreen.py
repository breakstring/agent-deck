"""Codex quota 的 N4 Pro 虚拟面板渲染器。

本模块把 `CodexQuotaSnapshot` 渲染为底部虚拟 panel，再通过 N4 Pro background
composer 合成到 SDK 可下发的 800x480 背景图。它不读取 Codex、不访问 StreamDock
设备、不启动 daemon、不写文件，也不修改任何运行状态。真实硬件下发由调用方负责。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from agent_deck.adapters.codex_quota import CodexQuotaSnapshot, CodexQuotaWindow
from agent_deck.rendering.n4pro_panel import (
    N4PRO_BACKGROUND_COLOR,
    N4PRO_BACKGROUND_SIZE,
    N4PRO_LOGICAL_PANEL_VIEWPORT,
    N4PRO_TOUCH_BAR_VIEWPORT,
    VirtualPanelViewport,
    compose_n4pro_background,
)
from agent_deck.rendering.reset_credit import draw_reset_credit_key_icon

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
_TERTIARY: Final[tuple[int, int, int]] = (171, 143, 255)
_QUATERNARY: Final[tuple[int, int, int]] = (255, 143, 112)
_QUOTA_ACCENTS: Final[tuple[tuple[int, int, int], ...]] = (
    _PRIMARY,
    _SECONDARY,
    _TERTIARY,
    _QUATERNARY,
)
_RESET_CREDIT: Final[tuple[int, int, int]] = (248, 213, 113)


def render_quota_touchscreen(
    snapshot: CodexQuotaSnapshot,
    *,
    window: str = "all",
    size: tuple[int, int] = N4PRO_BACKGROUND_SIZE,
    touch_bar_rect: tuple[int, int, int, int] = N4PRO_TOUCH_BAR_RECT,
) -> Image.Image:
    """把 Codex quota 快照渲染为 N4 Pro 背景图。

    入参：`snapshot` 是 Codex quota adapter 解析出的快照；`window` 控制展示指定 API 窗口或
    全部实际可用窗口；`size` 是 SDK 背景图尺寸，
    默认 N4 Pro 的 800x480；`touch_bar_rect` 是背景图中真实底部触摸条的安全绘制区域，
    格式为 `(left, top, right, bottom)`。
    返回：RGB `Image`，可保存为 JPEG 后通过 SDK `set_touchscreen_image` 下发；信息只绘制
    在 `touch_bar_rect` 内，其余区域保持背景色，避免内容透到按键窗口。
    错误处理：Pillow 字体加载失败时自动退回默认字体；非法尺寸或非法矩形会抛异常。
    副作用：只创建内存图像，不访问文件、网络或硬件。
    """

    viewport = VirtualPanelViewport(*touch_bar_rect)
    panel = render_quota_panel(snapshot, window=window, size=viewport.size)
    return compose_n4pro_background(panel, viewport=viewport, background_size=size)


def render_quota_panel(
    snapshot: CodexQuotaSnapshot,
    *,
    window: str = "all",
    size: tuple[int, int] = N4PRO_LOGICAL_PANEL_VIEWPORT.size,
) -> Image.Image:
    """把 Codex quota 快照渲染为底部虚拟 panel 图像。

    入参：`snapshot` 是 Codex quota adapter 解析出的快照；`window` 控制当前内容维度；`size`
    是 panel 自身尺寸，
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
    rows = _quota_rows(snapshot, window=window)
    row_gap = max(48, (content_bottom - content_top - 38) // max(1, len(rows)))
    first_row_y = content_top + 5
    if len(rows) == 1:
        first_row_y = content_top + max(5, (content_bottom - content_top - 38) // 2)
    for index, (label, remaining_percent, reset_label, color) in enumerate(rows):
        _draw_quota_row(
            draw,
            label=label,
            remaining_percent=remaining_percent,
            reset_label=reset_label,
            origin=(right_x, first_row_y + index * row_gap),
            max_right=content_right,
            bar_color=color,
            label_font=label_font,
            value_font=value_font,
            percent_font=percent_font,
        )
    return image


def _quota_rows(
    snapshot: CodexQuotaSnapshot,
    *,
    window: str,
) -> list[tuple[str, int, str, tuple[int, int, int]]]:
    """把当前可用 quota 槽位转换成 touch bar 的行模型。

    入参：`snapshot` 是已解析 quota；`window` 指定稳定 window_id、`auto` 或兼容用的 `all`。
    返回：指定窗口的一行，或总览模式中当前前两项窗口的行模型。
    错误处理：未知或旧 primary/secondary 配置由 snapshot 自动回退到实际窗口。
    副作用：无；不创建图像、不访问时钟之外的外部状态。
    """

    selected_windows = (
        snapshot.available_windows()[:2]
        if window == "all"
        else (snapshot.resolved_window(window),)
    )
    return [
        (
            _compact_window_label(selected),
            _remaining_percent(selected.used_percent),
            selected.display_reset_label(),
            _QUOTA_ACCENTS[index % len(_QUOTA_ACCENTS)],
        )
        for index, selected in enumerate(selected_windows)
    ]


def _compact_window_label(window: CodexQuotaWindow) -> str:
    """把 quota 窗口名称压缩为 touch bar 左侧进度条可容纳的标签。

    入参：`window` 是 adapter 的 quota window 模型；函数只依赖其 limit 名称和周期显示方法。
    返回：主 limit 为 `week:` 等周期标签；具名额外 limit 优先取名称最后一段并附带周期。
    错误处理：缺少名称或异常格式安全回退到周期标签。
    副作用：无；不访问图片或硬件。
    """

    period = window.display_period_label().upper()
    label = window.presentation_label
    if not label and window.limit_name:
        label = window.limit_name.rsplit("-", maxsplit=1)[-1].strip()
    if not label and window.limit_id == "codex":
        label = "Codex"
    return f"{label.upper()} · {period}" if label else period


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
    label = _fit_row_label(
        draw,
        label,
        font=label_font,
        max_right=max_right - 128,
        origin_x=x,
    )
    label_right = _text_right(draw, label, font=label_font, origin=(x, y + 17))
    bar_x = max(x + 116, label_right + 18)
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


def _text_right(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    font: ImageFont.ImageFont,
    origin: tuple[int, int],
) -> int:
    """计算左中对齐文本的右边界，用于给触屏进度条预留动态空间。

    入参：`draw` 是 Pillow 绘图对象；`text` 和 `font` 是待绘制文本与字体；`origin` 是 `lm`
    锚点坐标。
    返回：文本像素右边界。
    错误处理：字体或文本测量异常按 Pillow 原语义传播。
    副作用：无；只测量，不修改图像。
    """

    left, _top, right, _bottom = draw.textbbox(origin, text, font=font, anchor="lm")
    del left
    return right


def _fit_row_label(
    draw: ImageDraw.ImageDraw,
    label: str,
    *,
    font: ImageFont.ImageFont,
    max_right: int,
    origin_x: int,
) -> str:
    """裁剪 quota 行标签，保证触屏进度条保留可读的最小宽度。

    入参：`draw` 用于文本测量；`label` 是原始标签；`font` 是标签字体；`max_right` 是标签可达
    的右边界；`origin_x` 是左起点。
    返回：原标签或带省略号的短标签。
    错误处理：Pillow 测量失败按原语义传播。
    副作用：无；不修改图像。
    """

    if _text_right(draw, label, font=font, origin=(origin_x, 0)) <= max_right:
        return label
    suffix = "..."
    for length in range(len(label) - 1, 0, -1):
        candidate = f"{label[:length]}{suffix}"
        if _text_right(draw, candidate, font=font, origin=(origin_x, 0)) <= max_right:
            return candidate
    return suffix


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
    draw_reset_credit_key_icon(
        draw,
        (x, y + 3),
        icon_size,
        color=_RESET_CREDIT,
    )
    draw.text(
        (x + icon_size + 8, y + icon_size // 2),
        str(available_count),
        fill=_RESET_CREDIT,
        font=font,
        anchor="lm",
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
