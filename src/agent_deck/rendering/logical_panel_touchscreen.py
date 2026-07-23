"""Logical panel 的 N4 Pro 背景屏渲染器。

本模块把硬件无关的 `LogicalPanelPlan` 渲染为 N4 Pro 800x480 背景图，并把内容限制在
底部 logical panel viewport 内。它不读取 Codex、不执行 ccusage、不访问 StreamDock SDK、
不写文件，也不修改 daemon 状态；真实硬件下发由调用方负责。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from agent_deck.adapters.codex_tokens import CodexTokenPeriod, CodexTokenUsageSnapshot
from agent_deck.rendering.appearance import (
    DeckAppearanceSettings,
    RenderPalette,
    resolve_render_palette,
)
from agent_deck.rendering.logical_panel import LogicalPanelPlan, PanelKind, PanelMetric
from agent_deck.rendering.n4pro_panel import (
    N4PRO_BACKGROUND_COLOR,
    N4PRO_BACKGROUND_SIZE,
    N4PRO_LOGICAL_PANEL_VIEWPORT,
    VirtualPanelViewport,
    compose_n4pro_background,
)
from agent_deck.rendering.status_key import (
    usage_period_color,
    usage_period_label,
    usage_sparkline_values,
)

_BACKGROUND: Final[tuple[int, int, int]] = N4PRO_BACKGROUND_COLOR
_PANEL: Final[tuple[int, int, int]] = (18, 24, 36)
_TEXT: Final[tuple[int, int, int]] = (238, 244, 255)
_MUTED: Final[tuple[int, int, int]] = (145, 160, 182)
_PRIMARY: Final[tuple[int, int, int]] = (76, 205, 255)
_SECONDARY: Final[tuple[int, int, int]] = (126, 236, 165)
_GOLD: Final[tuple[int, int, int]] = (255, 202, 83)
_DIVIDER: Final[tuple[int, int, int]] = (34, 44, 64)


def render_logical_panel_touchscreen(
    plan: LogicalPanelPlan,
    *,
    size: tuple[int, int] = N4PRO_BACKGROUND_SIZE,
    viewport: VirtualPanelViewport = N4PRO_LOGICAL_PANEL_VIEWPORT,
    token_period: CodexTokenPeriod | None = None,
    token_trend: tuple[float, ...] = (),
    appearance: DeckAppearanceSettings | None = None,
) -> Image.Image:
    """把 logical panel plan 渲染为 N4 Pro 背景图。

    入参：`plan` 是待展示的 logical panel；`size` 是 N4 Pro 背景尺寸；
    `viewport` 是内容所在的底部逻辑窗口；Token 面板可选传入 `token_period` 和已聚合的
    `token_trend`，让 touch bar 与状态按键使用同一套周期趋势语义；``appearance`` 可覆盖
    基础背景与中性层级。
    返回：RGB `Image`，尺寸为 `size`，内容只绘制在 `viewport` 内。
    错误处理：panel 尺寸过小时抛 ValueError；Pillow 字体加载失败会回退默认字体。
    副作用：只创建内存图像，不访问外部 I/O。
    """

    panel = render_logical_panel(
        plan,
        size=viewport.size,
        token_period=token_period,
        token_trend=token_trend,
        appearance=appearance,
    )
    return compose_n4pro_background(
        panel,
        viewport=viewport,
        background_size=size,
        appearance=appearance,
    )


def render_token_usage_touchscreen(
    snapshot: CodexTokenUsageSnapshot,
    *,
    period: CodexTokenPeriod,
    size: tuple[int, int] = N4PRO_BACKGROUND_SIZE,
    viewport: VirtualPanelViewport = N4PRO_LOGICAL_PANEL_VIEWPORT,
    appearance: DeckAppearanceSettings | None = None,
) -> Image.Image:
    """渲染带周期色趋势线的 Token/金额 touch bar 面板。

    入参：`snapshot` 是 daemon 已缓存的 ccusage 快照；`period` 是当前 Day、Week、Month 或
    All 选择；`size` 和 `viewport` 允许 fake hardware 或后续设备复用几何契约；
    ``appearance`` 可覆盖基础背景。
    返回：N4 Pro 背景图，内容只位于 logical panel viewport。
    错误处理：快照缺少周期或 Pillow 绘制失败时按原语义抛出。
    副作用：只创建内存图像，不执行 ccusage、不写真实硬件。
    """

    from agent_deck.rendering.logical_panel import tokens_panel_plan

    return render_logical_panel_touchscreen(
        tokens_panel_plan(snapshot, period=period),
        size=size,
        viewport=viewport,
        token_period=period,
        token_trend=usage_sparkline_values(snapshot, period=period),
        appearance=appearance,
    )


def render_logical_panel(
    plan: LogicalPanelPlan,
    *,
    size: tuple[int, int] = N4PRO_LOGICAL_PANEL_VIEWPORT.size,
    token_period: CodexTokenPeriod | None = None,
    token_trend: tuple[float, ...] = (),
    appearance: DeckAppearanceSettings | None = None,
) -> Image.Image:
    """把 logical panel plan 渲染为独立 panel 图像。

    入参：`plan` 是待展示内容；`size` 是 panel 自身尺寸；Token 面板可选接收已经聚合的趋势；
    ``appearance`` 可覆盖基础背景和中性色。
    返回：RGB `Image`，只包含 panel 内容，不包含 N4 Pro 整屏背景。
    错误处理：尺寸太小时抛 ValueError；文本过长会被截断加省略号。
    副作用：只创建内存图像。
    """

    _validate_panel_size(size)
    palette = _logical_panel_palette(appearance)
    image = Image.new("RGB", size, palette.background)
    draw = ImageDraw.Draw(image)
    width, height = size
    left, top, right, bottom = 0, 0, width, height
    content_left = 34
    content_right = width - 34
    content_top = 18
    content_bottom = height - 18

    if plan.kind == PanelKind.TOKENS:
        _draw_token_panel(
            draw,
            plan=plan,
            content_left=content_left,
            content_right=content_right,
            content_top=content_top,
            period=token_period,
            trend=token_trend,
            palette=palette,
        )
        return image

    title_font = _load_font(21, bold=True)
    metric_value_font = _load_font(35, bold=True)
    metric_label_font = _load_font(15, bold=True)
    line_font = _load_font(18, bold=False)

    draw.text(
        (content_left, content_top),
        plan.title,
        fill=palette.muted_foreground,
        font=title_font,
    )
    metric_left = content_left
    metric_top = content_top + 32
    _draw_metrics(
        draw,
        metrics=plan.metrics[:2],
        origin=(metric_left, metric_top),
        max_right=content_left + 290,
        value_font=metric_value_font,
        label_font=metric_label_font,
        palette=palette,
    )

    lines_left = content_left + 330
    lines_top = content_top + 18
    line_gap = max(24, (content_bottom - lines_top) // max(1, min(4, len(plan.lines))))
    for index, line in enumerate(plan.lines[:4]):
        y = lines_top + index * line_gap
        text = _fit_text(draw, line, font=line_font, max_width=content_right - lines_left)
        draw.text((lines_left, y), text, fill=palette.foreground, font=line_font)

    return image


def _draw_token_panel(
    draw: ImageDraw.ImageDraw,
    *,
    plan: LogicalPanelPlan,
    content_left: int,
    content_right: int,
    content_top: int,
    period: CodexTokenPeriod | None,
    trend: tuple[float, ...],
    palette: RenderPalette,
) -> None:
    """绘制 tokens 面板的专用小屏布局。

    入参：`draw` 是绘图对象；`plan` 是 tokens logical panel；`content_left`、
    `content_right` 和 `content_top` 是卡片内部内容边界；`period` 与 `trend` 是预先聚合的
    当前周期身份与趋势数据；``palette`` 提供中性文字和表面颜色。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播；缺少指标或辅助行时按可用内容降级绘制。
    副作用：修改内存图像，不访问外部 I/O。
    """

    main_value_font = _load_font(30, bold=True)
    main_label_font = _load_font(11, bold=True)
    detail_label_font = _load_font(10, bold=True)
    detail_value_font = _load_font(14, bold=True)
    period_font = _load_font(11, bold=True)

    metric_top = content_top + 16
    _draw_token_main_metrics(
        draw,
        metrics=plan.metrics[:2],
        origin=(content_left, metric_top),
        value_font=main_value_font,
        label_font=main_label_font,
        palette=palette,
    )
    if period is not None:
        period_label = usage_period_label(period)
        period_color = usage_period_color(period)
        period_width = _text_width(draw, period_label, font=period_font)
        draw.text(
            (content_right - period_width, content_top + 1),
            period_label,
            fill=period_color,
            font=period_font,
        )
        _draw_token_trend(
            draw,
            values=trend,
            bounds=(content_left + 358, content_top + 20, content_right, content_top + 62),
            color=period_color,
        )
    _draw_token_detail_grid(
        draw,
        lines=plan.lines[:4],
        origin=(content_left, content_top + 76),
        max_right=content_right,
        label_font=detail_label_font,
        value_font=detail_value_font,
        palette=palette,
    )


def _draw_token_main_metrics(
    draw: ImageDraw.ImageDraw,
    *,
    metrics: tuple[PanelMetric, ...],
    origin: tuple[int, int],
    value_font: ImageFont.ImageFont,
    label_font: ImageFont.ImageFont,
    palette: RenderPalette,
) -> None:
    """绘制 token 面板左侧金额和总 token 主指标。

    入参：`draw` 是绘图对象；`metrics` 是最多两个主指标，约定为 Cost 与 Total；
    `origin` 是主指标区域左上角；`value_font`/`label_font` 是字体；``palette`` 提供中性色。
    返回：无返回值。
    错误处理：文本过长会在各自 slot 内截断，避免压到右侧趋势图。
    副作用：修改内存图像。
    """

    x, y = origin
    slots = ((x, 155, _GOLD), (x + 184, 150, palette.foreground))
    for metric, (slot_x, slot_width, color) in zip(metrics, slots, strict=False):
        value = _fit_text(draw, metric.value, font=value_font, max_width=slot_width)
        label = _fit_text(draw, metric.label, font=label_font, max_width=slot_width)
        draw.text((slot_x, y), value, fill=color, font=value_font)
        draw.text(
            (slot_x + 1, y + 35),
            label,
            fill=palette.muted_foreground,
            font=label_font,
        )


def _draw_token_detail_grid(
    draw: ImageDraw.ImageDraw,
    *,
    lines: tuple[str, ...],
    origin: tuple[int, int],
    max_right: int,
    label_font: ImageFont.ImageFont,
    value_font: ImageFont.ImageFont,
    palette: RenderPalette,
) -> None:
    """把 token 辅助指标绘制成底部四列扫描行。

    入参：`draw` 是绘图对象；`lines` 是如 `Input 954K` 的格式化指标行；`origin`
    是网格左上角；`max_right` 是右边界；`label_font`/`value_font` 是字体；
    ``palette`` 提供次要文字色。
    返回：无返回值。
    错误处理：行文本拆分失败时把整行作为 label、value 留空；文本过长会在 cell 内截断。
    副作用：修改内存图像。
    """

    x, y = origin
    cell_width = max(110, (max_right - x) // 4)
    for index, line in enumerate(lines[:4]):
        cell_x = x + index * cell_width
        cell_y = y
        label, value = _split_token_detail_line(line)
        label_text = _fit_text(draw, label, font=label_font, max_width=cell_width - 12)
        value_text = _fit_text(draw, value, font=value_font, max_width=cell_width - 12)
        draw.text(
            (cell_x, cell_y),
            label_text,
            fill=palette.muted_foreground,
            font=label_font,
        )
        draw.text((cell_x, cell_y + 13), value_text, fill=_PRIMARY, font=value_font)


def _draw_token_trend(
    draw: ImageDraw.ImageDraw,
    *,
    values: tuple[float, ...],
    bounds: tuple[int, int, int, int],
    color: tuple[int, int, int],
) -> None:
    """在 Token touch bar 右侧绘制无网格的单条历史趋势线。

    入参：`values` 是按当前周期聚合的总 Token 序列；`bounds` 是趋势可用矩形；`color` 是
    当前周期的身份色。
    返回：无。
    错误处理：空趋势安静不绘制；单值趋势仅保留末点。
    副作用：修改内存图像，不访问外部 I/O。
    """

    if not values:
        return
    left, top, right, bottom = bounds
    min_value = min(values)
    max_value = max(values)
    span = max(max_value - min_value, 1.0)
    points: list[tuple[float, float]] = []
    if len(values) == 1:
        points.append((right, (top + bottom) / 2))
    else:
        for index, value in enumerate(values):
            x = left + (right - left) * index / (len(values) - 1)
            normalized = (value - min_value) / span
            y = bottom - normalized * (bottom - top)
            points.append((x, y))
    if len(points) > 1:
        draw.line(points, fill=color, width=2, joint="curve")
    last_x, last_y = points[-1]
    radius = 4
    draw.ellipse(
        (last_x - radius, last_y - radius, last_x + radius, last_y + radius),
        fill=color,
    )


def _split_token_detail_line(line: str) -> tuple[str, str]:
    """拆分 token 辅助指标的标签和值。

    入参：`line` 是 `tokens_panel_plan` 生成的展示行，例如 `Cache read 13.3B`。
    返回：`(label, value)`；无法拆出值时 value 为空字符串。
    错误处理：空字符串返回空 label 和空 value，不抛异常。
    副作用：无。
    """

    text = line.strip()
    if not text:
        return "", ""
    label, separator, value = text.rpartition(" ")
    if not separator:
        return text, ""
    return label, value


def _draw_metrics(
    draw: ImageDraw.ImageDraw,
    *,
    metrics: tuple[PanelMetric, ...],
    origin: tuple[int, int],
    max_right: int,
    value_font: ImageFont.ImageFont,
    label_font: ImageFont.ImageFont,
    palette: RenderPalette,
) -> None:
    """绘制最多两个强调指标。

    入参：`draw` 是绘图对象；`metrics` 是待绘制指标；`origin` 是指标区左上角；
    `max_right` 是指标区右边界；`value_font`/`label_font` 是字体；``palette`` 提供中性色。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改内存图像。
    """

    x, y = origin
    slot_width = max(120, (max_right - x) // max(1, len(metrics)))
    for index, metric in enumerate(metrics):
        slot_x = x + index * slot_width
        color = _metric_color(metric, palette=palette)
        value = _fit_text(draw, metric.value, font=value_font, max_width=slot_width - 10)
        label = _fit_text(draw, metric.label, font=label_font, max_width=slot_width - 10)
        draw.text((slot_x, y), value, fill=color, font=value_font)
        draw.text(
            (slot_x + 2, y + 42),
            label,
            fill=palette.muted_foreground,
            font=label_font,
        )


def _metric_color(
    metric: PanelMetric,
    *,
    palette: RenderPalette,
) -> tuple[int, int, int]:
    """根据 metric emphasis 返回展示颜色。

    入参：`metric` 是 logical panel 指标；``palette`` 提供普通文字色。
    返回：RGB 颜色。
    错误处理：未知 emphasis 按普通文本色降级。
    副作用：无。
    """

    if metric.emphasis == "primary":
        return _PRIMARY
    if metric.emphasis == "secondary":
        return _SECONDARY
    return palette.foreground


def _logical_panel_palette(
    appearance: DeckAppearanceSettings | None,
) -> RenderPalette:
    """解析 logical panel 使用的默认或自定义中性色。

    入参：可选全局显示外观。
    返回：未设置时保持既有常量，设置时返回对比度感知调色板。
    错误处理：外观模型已校验。
    副作用：无。
    """

    return resolve_render_palette(
        appearance,
        default_background=_BACKGROUND,
        default_foreground=_TEXT,
        default_muted_foreground=_MUTED,
        default_surface=_PANEL,
        default_divider=_DIVIDER,
    )


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
