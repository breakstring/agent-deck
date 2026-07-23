"""本机 App 快捷键图像渲染。

本模块把 key-surface 投影出的 App payload 转换成 N4 Pro 主按键可下发的 112x112
Pillow 图像。它只读 `.app` bundle 图标，失败时绘制短 token fallback；不启动 App、
不执行 shell、不访问真实硬件。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from agent_deck.actions.apps import load_local_app_icon
from agent_deck.rendering.appearance import (
    DeckAppearanceSettings,
    RenderPalette,
    resolve_render_palette,
)

N4PRO_KEY_IMAGE_SIZE = (112, 112)
"""N4 Pro 主按键图片尺寸。"""

_KEY_BACKGROUND = (11, 14, 18)
_FALLBACK_FILL = (55, 67, 82)
_FALLBACK_ACCENT = (111, 213, 255)
_FALLBACK_TEXT = (238, 243, 246)


def render_app_key_image(
    *,
    app_name: str | None = None,
    app_path: str | None = None,
    icon_token: str | None = None,
    icon_color: str | None = None,
    size: tuple[int, int] = N4PRO_KEY_IMAGE_SIZE,
    appearance: DeckAppearanceSettings | None = None,
) -> Image.Image:
    """渲染 App 快捷键静态图。

    入参：`app_name`、`app_path`、`icon_token` 和 `icon_color` 来自 `KeyPlan.payload`；
    `size` 默认是 N4 Pro 主键尺寸；``appearance`` 可覆盖 Agent Deck 拥有的基础画布。
    返回：RGB `Image`，可直接传给 StreamDock key image sink。
    错误处理：图标缺失或坏图标会自动 fallback 到 token 图；尺寸过小时抛 ValueError。
    副作用：当 `app_path` 存在时只读该 `.app` bundle 的图标资源。
    """

    if size[0] < 64 or size[1] < 64:
        raise ValueError("app key image size is too small")

    palette = resolve_render_palette(
        appearance,
        default_background=_KEY_BACKGROUND,
        default_foreground=_FALLBACK_TEXT,
        default_surface=_FALLBACK_FILL,
        default_divider=_FALLBACK_ACCENT,
    )
    canvas = _base_canvas(size, palette=palette)
    icon = load_local_app_icon(Path(app_path), max_size=(104, 104)) if app_path else None
    if icon is not None:
        _paste_centered(canvas, icon)
        return canvas.convert("RGB")

    token = _token(icon_token=icon_token, app_name=app_name)
    _draw_token_fallback(
        canvas,
        token=token,
        fill=_parse_hex_color(icon_color) or palette.surface,
        text_fill=palette.foreground,
        outline=palette.divider if palette.custom else _FALLBACK_ACCENT,
    )
    return canvas.convert("RGB")


def _base_canvas(
    size: tuple[int, int],
    *,
    palette: RenderPalette,
) -> Image.Image:
    """创建无装饰边框的按键背景。

    入参：`size` 是目标图尺寸；``palette`` 提供基础背景。
    返回：RGBA `Image`。
    错误处理：无。
    副作用：无。
    """

    return Image.new("RGBA", size, (*palette.background, 255))


def _paste_centered(canvas: Image.Image, icon: Image.Image) -> None:
    """把 App 图标居中贴到按键背景。

    入参：`canvas` 是目标 RGBA 图；`icon` 是已缩放 RGBA 图标。
    返回：无显式返回值。
    错误处理：无。
    副作用：原地修改 `canvas`。
    """

    x = (canvas.width - icon.width) // 2
    y = (canvas.height - icon.height) // 2
    canvas.alpha_composite(icon, (x, y))


def _draw_token_fallback(
    canvas: Image.Image,
    *,
    token: str,
    fill: tuple[int, int, int],
    text_fill: tuple[int, int, int],
    outline: tuple[int, int, int],
) -> None:
    """绘制没有真实图标时的 token fallback。

    入参：`canvas` 是目标图；`token` 是 1-2 字符短标签；`fill` 是卡片底色；
    ``text_fill`` 和 ``outline`` 来自当前调色板。
    返回：无显式返回值。
    错误处理：字体不可用时使用 Pillow 默认字体。
    副作用：原地修改 `canvas`。
    """

    draw = ImageDraw.Draw(canvas)
    rect = (24, 24, canvas.width - 25, canvas.height - 25)
    draw.rounded_rectangle(rect, radius=14, fill=(*fill, 255))
    draw.rounded_rectangle(rect, radius=14, outline=(*outline, 210), width=2)

    font = _token_font()
    bbox = draw.textbbox((0, 0), token, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (canvas.width - text_width) / 2
    y = (canvas.height - text_height) / 2 - 2
    draw.text((x, y), token, font=font, fill=(*text_fill, 255))


def _token_font() -> ImageFont.ImageFont:
    """返回 token fallback 使用的字体。

    入参：无。
    返回：可用于 `ImageDraw.text` 的字体对象。
    错误处理：系统字体不可用时返回 Pillow 默认字体。
    副作用：可能只读系统字体文件。
    """

    for path in (
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, 32)
        except OSError:
            continue
    return ImageFont.load_default()


def _token(*, icon_token: str | None, app_name: str | None) -> str:
    """生成 fallback 短 token。

    入参：优先使用 `icon_token`，否则从 `app_name` 推导。
    返回：1-2 个大写字符。
    错误处理：无。
    副作用：无。
    """

    raw = (icon_token or "").strip() or (app_name or "App").strip()[:2] or "A"
    return raw[:2].upper()


def _parse_hex_color(value: str | None) -> tuple[int, int, int] | None:
    """解析简单十六进制颜色。

    入参：`value` 可以是 `#RRGGBB` 或 `RRGGBB`；CSS gradient 等复杂值会返回 None。
    返回：RGB tuple 或 None。
    错误处理：非法颜色返回 None。
    副作用：无。
    """

    if not value:
        return None
    raw = value.strip().removeprefix("#")
    if len(raw) != 6:
        return None
    try:
        return tuple(int(raw[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return None
