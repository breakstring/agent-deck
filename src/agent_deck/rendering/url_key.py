"""URL 快捷键图像渲染。

本模块把 URL 或 favicon 转换成 N4 Pro 主按键可下发的静态 PNG 图像。它不访问网络、
不打开浏览器、不读取用户配置文件；调用方负责获取 favicon 并传入 Pillow 图像。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont

N4PRO_URL_KEY_IMAGE_SIZE = (112, 112)
"""N4 Pro URL 主按键图片尺寸。"""

_KEY_BACKGROUND = (11, 14, 18)
_FALLBACK_FILL = (27, 72, 90)
_FALLBACK_ACCENT = (111, 213, 255)
_FALLBACK_TEXT = (238, 243, 246)


def render_url_key_image(
    *,
    url: str | None,
    favicon: Image.Image | None = None,
    icon_token: str | None = None,
    size: tuple[int, int] = N4PRO_URL_KEY_IMAGE_SIZE,
) -> Image.Image:
    """渲染 URL 快捷键静态图。

    入参：`url` 是用户配置的网址；`favicon` 是可选网站图标；`icon_token` 是可选 fallback
    短标签；`size` 默认是 N4 Pro 主键尺寸。
    返回：RGB `Image`，可直接传给 StreamDock key image sink。
    错误处理：图标缺失或坏图标会自动 fallback 到 token 图；尺寸过小时抛 ValueError。
    副作用：无；只处理内存图像。
    """

    if size[0] < 64 or size[1] < 64:
        raise ValueError("url key image size is too small")

    canvas = _base_canvas(size)
    prepared = _prepare_favicon(favicon, max_size=(104, 104))
    if prepared is not None:
        _paste_centered(canvas, prepared)
        return canvas.convert("RGB")

    _draw_token_fallback(
        canvas,
        token=icon_token or token_for_url(url),
    )
    return canvas.convert("RGB")


def token_for_url(url: str | None) -> str:
    """从 URL 推导 fallback 短标签。

    入参：`url` 是用户输入的网址，可以为空或不完整。
    返回：1-3 个大写字母或数字；无法推导时返回 `URL`。
    错误处理：无。
    副作用：无。
    """

    host = urlparse((url or "").strip()).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    label = host.split(".")[0] if host else ""
    compact = re.sub(r"[^A-Za-z0-9]+", "", label).upper()
    if not compact:
        return "URL"
    if len(compact) <= 3:
        return compact
    return compact[:2]


def _base_canvas(size: tuple[int, int]) -> Image.Image:
    """创建无装饰边框的按键背景。

    入参：`size` 是目标图尺寸。
    返回：RGBA `Image`。
    错误处理：无。
    副作用：无。
    """

    return Image.new("RGBA", size, (*_KEY_BACKGROUND, 255))


def _prepare_favicon(
    favicon: Image.Image | None,
    *,
    max_size: tuple[int, int],
) -> Image.Image | None:
    """把 favicon 转换成可居中贴到按键上的 RGBA 图。

    入参：`favicon` 是原始图像；`max_size` 是允许的最大宽高。
    返回：缩放后的 RGBA 图或 None。
    错误处理：坏图像返回 None。
    副作用：只复制内存图像。
    """

    if favicon is None:
        return None
    try:
        icon = favicon.convert("RGBA")
        icon.thumbnail(max_size, Image.Resampling.LANCZOS)
        return icon
    except Exception:
        return None


def _paste_centered(canvas: Image.Image, icon: Image.Image) -> None:
    """把 favicon 居中贴到按键背景。

    入参：`canvas` 是目标 RGBA 图；`icon` 是已缩放 RGBA 图标。
    返回：无显式返回值。
    错误处理：无。
    副作用：原地修改 `canvas`。
    """

    x = (canvas.width - icon.width) // 2
    y = (canvas.height - icon.height) // 2
    canvas.alpha_composite(icon, (x, y))


def _draw_token_fallback(canvas: Image.Image, *, token: str) -> None:
    """绘制没有真实 favicon 时的 token fallback。

    入参：`canvas` 是目标图；`token` 是 1-3 字符短标签。
    返回：无显式返回值。
    错误处理：字体不可用时使用 Pillow 默认字体。
    副作用：原地修改 `canvas`。
    """

    draw = ImageDraw.Draw(canvas)
    rect = (22, 24, canvas.width - 23, canvas.height - 25)
    draw.rounded_rectangle(rect, radius=16, fill=(*_FALLBACK_FILL, 255))
    draw.rounded_rectangle(rect, radius=16, outline=(*_FALLBACK_ACCENT, 210), width=2)

    font = _token_font(token)
    bbox = draw.textbbox((0, 0), token, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (canvas.width - text_width) / 2
    y = (canvas.height - text_height) / 2 - 2
    draw.text((x, y), token, font=font, fill=(*_FALLBACK_TEXT, 255))


def _token_font(token: str) -> ImageFont.ImageFont:
    """返回 URL token fallback 使用的字体。

    入参：`token` 用于根据字符数选择字号。
    返回：可用于 `ImageDraw.text` 的字体对象。
    错误处理：系统字体不可用时返回 Pillow 默认字体。
    副作用：可能只读系统字体文件。
    """

    size = 32 if len(token) <= 2 else 25
    for path in (
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()
