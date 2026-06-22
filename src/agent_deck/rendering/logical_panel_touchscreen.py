"""Logical panel 的 N4 Pro 背景屏渲染器。

本模块把硬件无关的 `LogicalPanelPlan` 渲染为 N4 Pro 800x480 背景图，并把内容限制在
底部 logical panel viewport 内。它不读取 Codex、不执行 ccusage、不访问 StreamDock SDK、
不写文件，也不修改 daemon 状态；真实硬件下发由调用方负责。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from agent_deck.rendering.logical_panel import LogicalPanelPlan, PanelMetric
from agent_deck.rendering.n4pro_panel import (
    N4PRO_BACKGROUND_COLOR,
    N4PRO_BACKGROUND_SIZE,
    N4PRO_LOGICAL_PANEL_VIEWPORT,
    VirtualPanelViewport,
    compose_n4pro_background,
)

_BACKGROUND: Final[tuple[int, int, int]] = N4PRO_BACKGROUND_COLOR
_PANEL: Final[tuple[int, int, int]] = (18, 24, 36)
_TEXT: Final[tuple[int, int, int]] = (238, 244, 255)
_MUTED: Final[tuple[int, int, int]] = (145, 160, 182)
_PRIMARY: Final[tuple[int, int, int]] = (76, 205, 255)
_SECONDARY: Final[tuple[int, int, int]] = (126, 236, 165)
_DIVIDER: Final[tuple[int, int, int]] = (34, 44, 64)


def render_logical_panel_touchscreen(
    plan: LogicalPanelPlan,
    *,
    size: tuple[int, int] = N4PRO_BACKGROUND_SIZE,
    viewport: VirtualPanelViewport = N4PRO_LOGICAL_PANEL_VIEWPORT,
) -> Image.Image:
    """把 logical panel plan 渲染为 N4 Pro 背景图。

    入参：`plan` 是待展示的 logical panel；`size` 是 N4 Pro 背景尺寸；
    `viewport` 是内容所在的底部逻辑窗口。
    返回：RGB `Image`，尺寸为 `size`，内容只绘制在 `viewport` 内。
    错误处理：panel 尺寸过小时抛 ValueError；Pillow 字体加载失败会回退默认字体。
    副作用：只创建内存图像，不访问外部 I/O。
    """

    panel = render_logical_panel(plan, size=viewport.size)
    return compose_n4pro_background(panel, viewport=viewport, background_size=size)


def render_logical_panel(
    plan: LogicalPanelPlan,
    *,
    size: tuple[int, int] = N4PRO_LOGICAL_PANEL_VIEWPORT.size,
) -> Image.Image:
    """把 logical panel plan 渲染为独立 panel 图像。

    入参：`plan` 是待展示内容；`size` 是 panel 自身尺寸。
    返回：RGB `Image`，只包含 panel 内容，不包含 N4 Pro 整屏背景。
    错误处理：尺寸太小时抛 ValueError；文本过长会被截断加省略号。
    副作用：只创建内存图像。
    """

    _validate_panel_size(size)
    image = Image.new("RGB", size, _BACKGROUND)
    draw = ImageDraw.Draw(image)
    width, height = size
    left, top, right, bottom = 0, 0, width, height
    content_left = 34
    content_right = width - 34
    content_top = 18
    content_bottom = height - 18

    draw.rounded_rectangle(
        (left + 18, top + 8, right - 18, bottom - 8),
        radius=24,
        fill=_PANEL,
    )
    draw.line((left + 28, top + 10, right - 28, top + 10), fill=_DIVIDER, width=1)

    title_font = _load_font(21, bold=True)
    metric_value_font = _load_font(35, bold=True)
    metric_label_font = _load_font(15, bold=True)
    line_font = _load_font(18, bold=False)

    draw.text((content_left, content_top), plan.title, fill=_MUTED, font=title_font)
    metric_left = content_left
    metric_top = content_top + 32
    _draw_metrics(
        draw,
        metrics=plan.metrics[:2],
        origin=(metric_left, metric_top),
        max_right=content_left + 290,
        value_font=metric_value_font,
        label_font=metric_label_font,
    )

    lines_left = content_left + 330
    lines_top = content_top + 18
    line_gap = max(24, (content_bottom - lines_top) // max(1, min(4, len(plan.lines))))
    for index, line in enumerate(plan.lines[:4]):
        y = lines_top + index * line_gap
        text = _fit_text(draw, line, font=line_font, max_width=content_right - lines_left)
        draw.text((lines_left, y), text, fill=_TEXT, font=line_font)

    return image


def _draw_metrics(
    draw: ImageDraw.ImageDraw,
    *,
    metrics: tuple[PanelMetric, ...],
    origin: tuple[int, int],
    max_right: int,
    value_font: ImageFont.ImageFont,
    label_font: ImageFont.ImageFont,
) -> None:
    """绘制最多两个强调指标。

    入参：`draw` 是绘图对象；`metrics` 是待绘制指标；`origin` 是指标区左上角；
    `max_right` 是指标区右边界；`value_font`/`label_font` 是字体。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改内存图像。
    """

    x, y = origin
    slot_width = max(120, (max_right - x) // max(1, len(metrics)))
    for index, metric in enumerate(metrics):
        slot_x = x + index * slot_width
        color = _metric_color(metric)
        value = _fit_text(draw, metric.value, font=value_font, max_width=slot_width - 10)
        label = _fit_text(draw, metric.label, font=label_font, max_width=slot_width - 10)
        draw.text((slot_x, y), value, fill=color, font=value_font)
        draw.text((slot_x + 2, y + 42), label, fill=_MUTED, font=label_font)


def _metric_color(metric: PanelMetric) -> tuple[int, int, int]:
    """根据 metric emphasis 返回展示颜色。

    入参：`metric` 是 logical panel 指标。
    返回：RGB 颜色。
    错误处理：未知 emphasis 按普通文本色降级。
    副作用：无。
    """

    if metric.emphasis == "primary":
        return _PRIMARY
    if metric.emphasis == "secondary":
        return _SECONDARY
    return _TEXT


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    """把文本裁剪到指定像素宽度内。

    入参：`draw` 是测量上下文；`text` 是待绘制文本；`font` 是字体；`max_width` 是像素宽度。
    返回：原文本或加 `...` 的截断文本。
    错误处理：无；极小宽度下返回空字符串。
    副作用：无。
    """

    if max_width <= 0:
        return ""
    if _text_width(draw, text, font=font) <= max_width:
        return text
    suffix = "..."
    available = max_width - _text_width(draw, suffix, font=font)
    if available <= 0:
        return ""
    result = text
    while result and _text_width(draw, result, font=font) > available:
        result = result[:-1]
    return f"{result.rstrip()}{suffix}" if result else suffix


def _text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    font: ImageFont.ImageFont,
) -> int:
    """测量文本像素宽度。

    入参：`draw` 是测量上下文；`text` 是待测文本；`font` 是字体。
    返回：向上取整后的文本宽度。
    错误处理：Pillow 测量异常按原语义传播。
    副作用：无。
    """

    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def _validate_panel_size(size: tuple[int, int]) -> None:
    """校验 logical panel 尺寸足够绘制当前布局。

    入参：`size` 是 panel 图像尺寸。
    返回：无返回值。
    错误处理：尺寸太小时抛 ValueError。
    副作用：无。
    """

    width, height = size
    if width < 600 or height < 96:
        raise ValueError("logical panel size is too small")


def _load_font(size: int, *, bold: bool) -> ImageFont.ImageFont:
    """加载项目可用字体，失败时回退到 Pillow 默认字体。

    入参：`size` 是字体像素大小；`bold` 表示是否优先使用粗体。
    返回：`ImageFont` 实例。
    错误处理：字体文件不可用时返回默认字体。
    副作用：只读尝试加载系统字体文件。
    """

    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/Library/Fonts/Arial Bold.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
    )
    preferred = candidates if bold else (candidates[1], candidates[3], *candidates[:1])
    for path in preferred:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()
