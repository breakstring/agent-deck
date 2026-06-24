"""Agent Deck 品牌图形与默认 N4 Pro splash 渲染器。

本模块把 Agent Deck 的 Command Core logo mark 和无数据默认 splash panel 渲染为 Pillow 图像。
它只处理内存图像，不读取文件、不访问 Codex、不连接 StreamDock 硬件，也不修改 daemon
状态；调用方可把输出交给 N4 Pro background composer 或保存为预览资产。
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from agent_deck.rendering.n4pro_panel import (
    N4PRO_BACKGROUND_COLOR,
    N4PRO_BACKGROUND_SIZE,
    N4PRO_LOGICAL_PANEL_VIEWPORT,
    VirtualPanelViewport,
    compose_n4pro_background,
)

_ASSET_PACKAGE: Final[str] = "agent_deck.assets"
_LOGO_ASSET_NAME: Final[str] = "logo_command_core.png"
_SPLASH_PANEL_ASSET_NAME: Final[str] = "n4pro_splash_command_core.png"
_BACKGROUND: Final[tuple[int, int, int]] = N4PRO_BACKGROUND_COLOR
_PANEL: Final[tuple[int, int, int]] = (17, 22, 30)
_PANEL_INNER: Final[tuple[int, int, int]] = (28, 32, 38)
_METAL: Final[tuple[int, int, int]] = (66, 70, 76)
_METAL_LIGHT: Final[tuple[int, int, int]] = (96, 102, 110)
_SLOT: Final[tuple[int, int, int]] = (7, 11, 16)
_TEXT: Final[tuple[int, int, int]] = (238, 244, 255)
_MUTED: Final[tuple[int, int, int]] = (145, 160, 182)
_CYAN: Final[tuple[int, int, int]] = (0, 209, 255)
_CYAN_DIM: Final[tuple[int, int, int]] = (18, 98, 124)
_GREEN: Final[tuple[int, int, int]] = (34, 197, 94)
_LINE: Final[tuple[int, int, int]] = (48, 57, 72)


def render_agent_deck_logo(
    *,
    size: tuple[int, int] = (256, 256),
    transparent: bool = True,
) -> Image.Image:
    """渲染 Agent Deck 的独立 logo mark。

    入参：`size` 是输出图像宽高，建议使用正方形；`transparent` 控制兜底图和外部留白背景，
    随包 bitmap 资产自身可能保持不透明。
    返回：RGBA `Image`，包含 Command Core 硬件控制键、中央控制槽和青色状态通道。
    错误处理：尺寸太小时抛出 `ValueError`；Pillow 绘制异常按原异常传播。
    副作用：只创建内存图像，不读写文件或访问硬件。
    """

    _validate_logo_size(size)
    asset = _load_packaged_image(_LOGO_ASSET_NAME, mode="RGBA")
    if asset is not None:
        return _fit_bitmap_asset(asset, size=size, transparent=transparent)

    return _render_agent_deck_logo_fallback(size=size, transparent=transparent)


def _render_agent_deck_logo_fallback(
    *,
    size: tuple[int, int],
    transparent: bool,
) -> Image.Image:
    """在 bitmap logo 资产不可用时渲染程序化兜底 logo。

    入参：`size` 是输出尺寸；`transparent` 控制背景是否透明。
    返回：RGBA `Image`，视觉语义接近 Command Core 资产。
    错误处理：Pillow 绘制异常按原异常传播。
    副作用：只创建内存图像。
    """

    background = (0, 0, 0, 0) if transparent else (*_BACKGROUND, 255)
    image = Image.new("RGBA", size, background)
    draw = ImageDraw.Draw(image)
    width, height = size
    scale = min(width, height) / 256
    mark_size = int(204 * scale)
    left = (width - mark_size) // 2
    top = (height - mark_size) // 2
    _draw_agent_deck_mark(draw, bounds=(left, top, left + mark_size, top + mark_size))
    return image


def render_agent_deck_splash_touchscreen(
    *,
    size: tuple[int, int] = N4PRO_BACKGROUND_SIZE,
    viewport: VirtualPanelViewport = N4PRO_LOGICAL_PANEL_VIEWPORT,
) -> Image.Image:
    """渲染可直接下发到 N4 Pro 背景层的 Agent Deck 默认 splash。

    入参：`size` 是 N4 Pro SDK 背景图尺寸；`viewport` 是底部 logical panel 投影区域。
    返回：RGB `Image`，尺寸为 `size`，品牌 splash 只绘制在 `viewport` 内。
    错误处理：viewport 越界或 panel 尺寸过小时抛出 `ValueError`。
    副作用：只创建内存图像，不访问真实硬件。
    """

    panel = render_agent_deck_splash_panel(size=viewport.size)
    return compose_n4pro_background(panel, viewport=viewport, background_size=size)


def render_agent_deck_splash_panel(
    *,
    size: tuple[int, int] = N4PRO_LOGICAL_PANEL_VIEWPORT.size,
) -> Image.Image:
    """渲染底部 logical panel 尺寸的 Agent Deck 默认 splash。

    入参：`size` 是独立 panel 尺寸，默认匹配 N4 Pro 底部 touch bar viewport。
    返回：RGB `Image`，包含 logo、产品名、默认状态和短状态文案。
    错误处理：尺寸不足时抛出 `ValueError`；字体加载失败会回退到 Pillow 默认字体。
    副作用：只创建内存图像。
    """

    _validate_splash_panel_size(size)
    asset = _load_packaged_image(_SPLASH_PANEL_ASSET_NAME, mode="RGB")
    if asset is not None:
        return asset.resize(size, Image.Resampling.LANCZOS)

    return _render_agent_deck_splash_panel_fallback(size=size)


def _render_agent_deck_splash_panel_fallback(
    *,
    size: tuple[int, int],
) -> Image.Image:
    """在 bitmap splash 资产不可用时渲染程序化默认 panel。

    入参：`size` 是独立 panel 尺寸。
    返回：RGB `Image`，包含 Command Core 风格的基础品牌信息。
    错误处理：Pillow 字体或绘制异常按原异常传播。
    副作用：只创建内存图像。
    """

    image = Image.new("RGB", size, _BACKGROUND)
    draw = ImageDraw.Draw(image)
    width, height = size

    panel_bounds = (18, 8, width - 18, height - 8)
    draw.rounded_rectangle(panel_bounds, radius=24, fill=_PANEL)
    draw.rounded_rectangle(
        (
            panel_bounds[0] + 2,
            panel_bounds[1] + 2,
            panel_bounds[2] - 2,
            panel_bounds[3] - 2,
        ),
        radius=22,
        outline=_LINE,
        width=1,
    )

    logo_size = min(92, height - 38)
    logo_left = 42
    logo_top = (height - logo_size) // 2
    _draw_agent_deck_mark(
        draw,
        bounds=(logo_left, logo_top, logo_left + logo_size, logo_top + logo_size),
    )

    title_font = _load_font(38, bold=True)
    subtitle_font = _load_font(17, bold=True)
    status_font = _load_font(15, bold=True)
    small_font = _load_font(14, bold=False)

    text_left = logo_left + logo_size + 30
    title_y = 28
    draw.text((text_left, title_y), "AGENT DECK", fill=_TEXT, font=title_font)
    draw.text(
        (text_left + 2, title_y + 47),
        "LOCAL AI CONTROL SURFACE",
        fill=_MUTED,
        font=subtitle_font,
    )

    status_text = "LOCAL / READY"
    status_bounds = (width - 232, 34, width - 58, 68)
    draw.rounded_rectangle(
        status_bounds,
        radius=17,
        fill=(20, 45, 48),
        outline=_GREEN,
        width=1,
    )
    _draw_status_dot(draw, center=(status_bounds[0] + 22, status_bounds[1] + 17))
    draw.text(
        (status_bounds[0] + 40, status_bounds[1] + 9),
        status_text,
        fill=_GREEN,
        font=status_font,
    )

    trace_left = text_left
    trace_right = width - 86
    trace_y = height - 35
    draw.line((trace_left, trace_y, trace_right - 96, trace_y), fill=_CYAN_DIM, width=1)
    _draw_status_trace(
        draw,
        points=(
            (trace_right - 96, trace_y),
            (trace_right - 70, trace_y - 14),
            (trace_right - 32, trace_y - 14),
            (trace_right - 12, trace_y - 22),
        ),
        width=2,
    )
    draw.ellipse(
        (trace_right - 15, trace_y - 25, trace_right - 9, trace_y - 19),
        fill=_CYAN,
    )
    draw.text(
        (text_left, height - 23),
        "Command surface standing by",
        fill=_MUTED,
        font=small_font,
    )
    return image


def _load_packaged_image(name: str, *, mode: str) -> Image.Image | None:
    """从包资源中读取一个品牌 bitmap 资产。

    入参：`name` 是 `agent_deck.assets` 内的文件名；`mode` 是期望输出模式。
    返回：转换为目标模式的 Pillow `Image`；资源缺失或解码失败时返回 None。
    错误处理：资产加载异常被吞掉，让调用方使用程序化兜底。
    副作用：只读访问随包资源，不写文件、不访问硬件。
    """

    try:
        asset_path = resources.files(_ASSET_PACKAGE).joinpath(name)
        with asset_path.open("rb") as handle:
            return Image.open(handle).convert(mode)
    except Exception:
        return None


def _fit_bitmap_asset(
    image: Image.Image,
    *,
    size: tuple[int, int],
    transparent: bool,
) -> Image.Image:
    """把随包 bitmap logo 适配到请求尺寸。

    入参：`image` 是已加载的 logo bitmap；`size` 是目标尺寸；`transparent` 控制目标画布背景，
    不会自动抠除 bitmap 资产自身的暗色背景。
    返回：RGBA `Image`，按比例缩放并居中。
    错误处理：Pillow resize/paste 异常按原异常传播。
    副作用：只创建内存图像。
    """

    background = (0, 0, 0, 0) if transparent else (*_BACKGROUND, 255)
    canvas = Image.new("RGBA", size, background)
    fitted = image.copy()
    fitted.thumbnail(size, Image.Resampling.LANCZOS)
    x = (size[0] - fitted.width) // 2
    y = (size[1] - fitted.height) // 2
    canvas.paste(fitted, (x, y), fitted)
    return canvas


def _validate_logo_size(size: tuple[int, int]) -> None:
    """校验 logo 输出尺寸。

    入参：`size` 是调用方请求的图像尺寸。
    返回：无返回值；合法尺寸直接返回。
    错误处理：任一边小于 64 像素时抛出 `ValueError`。
    副作用：无。
    """

    width, height = size
    if width < 64 or height < 64:
        raise ValueError("logo size must be at least 64x64")


def _validate_splash_panel_size(size: tuple[int, int]) -> None:
    """校验默认 splash panel 尺寸。

    入参：`size` 是 panel 宽高。
    返回：无返回值；合法尺寸直接返回。
    错误处理：宽小于 560 或高小于 110 时抛出 `ValueError`。
    副作用：无。
    """

    width, height = size
    if width < 560 or height < 110:
        raise ValueError("splash panel size is too small")


def _draw_agent_deck_mark(
    draw: ImageDraw.ImageDraw,
    *,
    bounds: tuple[int, int, int, int],
) -> None:
    """绘制 Command Core 风格的 Agent Deck logo mark。

    入参：`draw` 是 Pillow 绘图对象；`bounds` 是 mark 的外接矩形。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像。
    """

    left, top, right, bottom = bounds
    unit = min(right - left, bottom - top)
    radius = max(9, round(unit * 0.20))
    layer_offset = max(3, round(unit * 0.045))
    face = (
        left + round(unit * 0.08),
        top + round(unit * 0.06),
        left + round(unit * 0.92),
        top + round(unit * 0.88),
    )

    draw.rounded_rectangle(
        (
            face[0] + layer_offset,
            face[1] + layer_offset * 3,
            face[2] - layer_offset,
            face[3] + layer_offset * 2,
        ),
        radius=radius,
        fill=(10, 13, 18),
    )
    draw.rounded_rectangle(
        (
            face[0] + layer_offset // 2,
            face[1] + layer_offset,
            face[2] - layer_offset // 2,
            face[3] + layer_offset,
        ),
        radius=radius,
        fill=(26, 29, 34),
    )
    draw.rounded_rectangle(
        face,
        radius=radius,
        fill=_METAL,
        outline=_METAL_LIGHT,
        width=max(1, unit // 46),
    )

    slot = (
        left + round(unit * 0.30),
        top + round(unit * 0.30),
        left + round(unit * 0.68),
        top + round(unit * 0.62),
    )
    draw.rounded_rectangle(
        slot,
        radius=max(6, round(unit * 0.08)),
        fill=_SLOT,
        outline=(17, 176, 208),
        width=max(1, unit // 34),
    )

    trace_width = max(2, unit // 28)
    _draw_status_trace(
        draw,
        points=(
            (left + round(unit * 0.33), top + round(unit * 0.72)),
            (left + round(unit * 0.33), top + round(unit * 0.62)),
            (left + round(unit * 0.43), top + round(unit * 0.56)),
            (left + round(unit * 0.58), top + round(unit * 0.56)),
            (left + round(unit * 0.69), top + round(unit * 0.48)),
            (left + round(unit * 0.84), top + round(unit * 0.48)),
        ),
        width=trace_width,
    )
    dot = (left + round(unit * 0.84), top + round(unit * 0.48))
    dot_radius = max(3, unit // 22)
    draw.ellipse(
        (
            dot[0] - dot_radius,
            dot[1] - dot_radius,
            dot[0] + dot_radius,
            dot[1] + dot_radius,
        ),
        fill=_CYAN,
    )


def _draw_status_trace(
    draw: ImageDraw.ImageDraw,
    *,
    points: tuple[tuple[int, int], ...],
    width: int,
) -> None:
    """绘制 Command Core 的青色状态通道。

    入参：`draw` 是绘图对象；`points` 是折线路径点；`width` 是线宽。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改内存图像。
    """

    if len(points) < 2:
        return
    draw.line(points, fill=_CYAN_DIM, width=width + 2, joint="curve")
    draw.line(points, fill=_CYAN, width=width, joint="curve")


def _draw_status_dot(draw: ImageDraw.ImageDraw, *, center: tuple[int, int]) -> None:
    """绘制 READY 状态前的小型状态点。

    入参：`draw` 是绘图对象；`center` 是状态点中心。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改内存图像。
    """

    cx, cy = center
    draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=_GREEN)
    draw.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), outline=(52, 104, 90), width=2)


def _load_font(size: int, *, bold: bool) -> ImageFont.ImageFont:
    """加载适合小屏渲染的 TrueType 字体。

    入参：`size` 是字号；`bold` 控制是否优先使用粗体。
    返回：Pillow font 对象。
    错误处理：系统字体不可用时回退到默认字体。
    副作用：可能只读访问常见系统字体路径。
    """

    candidates = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    preferred = candidates if bold else candidates[1:] + candidates[:1]
    for path in preferred:
        font_path = Path(path)
        if font_path.exists():
            try:
                return ImageFont.truetype(str(font_path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()
