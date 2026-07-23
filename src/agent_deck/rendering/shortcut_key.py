"""键盘快捷键的 N4 Pro 自动图标渲染。

本模块把强类型快捷键序列转换成 112px 静态按键图：单步居中、双步分行、多步显示前两步
与剩余数量。它不读取自定义图标资产、不访问硬件或系统键盘 API。
"""

from __future__ import annotations

import json

from PIL import Image, ImageDraw, ImageFont

from agent_deck.actions.keyboard import KeyboardModifier, KeyboardShortcutSpec
from agent_deck.rendering.appearance import (
    DeckAppearanceSettings,
    appearance_cache_key,
    resolve_render_palette,
)

N4PRO_SHORTCUT_KEY_IMAGE_SIZE = (112, 112)
"""N4 Pro 快捷键自动图标默认尺寸。"""

_MODIFIER_SYMBOLS = {
    KeyboardModifier.COMMAND: "⌘",
    KeyboardModifier.CONTROL: "⌃",
    KeyboardModifier.OPTION: "⌥",
    KeyboardModifier.SHIFT: "⇧",
}

_KEY_LABELS = {
    "Backquote": "`",
    "Minus": "−",
    "Equal": "=",
    "BracketLeft": "[",
    "BracketRight": "]",
    "Backslash": "\\",
    "Semicolon": ";",
    "Quote": "'",
    "Comma": ",",
    "Period": ".",
    "Slash": "/",
    "Enter": "↩",
    "Escape": "Esc",
    "Backspace": "⌫",
    "Tab": "⇥",
    "Space": "Space",
    "Insert": "Ins",
    "Delete": "Del",
    "Home": "Home",
    "End": "End",
    "PageUp": "PgUp",
    "PageDown": "PgDn",
    "ArrowUp": "↑",
    "ArrowDown": "↓",
    "ArrowLeft": "←",
    "ArrowRight": "→",
    "NumpadDecimal": "Num .",
    "NumpadMultiply": "Num ×",
    "NumpadAdd": "Num +",
    "NumpadDivide": "Num ÷",
    "NumpadEnter": "Num ↩",
    "NumpadSubtract": "Num −",
    "NumpadEqual": "Num =",
    "NumLock": "Clear",
}


class ShortcutKeyImageCache:
    """按快捷键内容缓存自动生成的静态按键图。

    入参：可选最大条目数，默认 64。
    返回：通过 ``image`` 复用同一 Pillow 对象的进程内缓存。
    错误处理：非正容量抛 ValueError；渲染错误按原样传播且不缓存。
    副作用：cache miss 时创建内存图片；达到容量后移除最早插入项。
    """

    def __init__(self, *, max_entries: int = 64) -> None:
        """初始化有界自动图标缓存。

        入参：``max_entries`` 是最大条目数。
        返回：无显式返回值。
        错误处理：非正值抛 ValueError。
        副作用：只分配空内存 dict。
        """

        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._images: dict[tuple[str, str], Image.Image] = {}
        self._hits = 0
        self._misses = 0

    def image(
        self,
        shortcut: KeyboardShortcutSpec,
        *,
        appearance: DeckAppearanceSettings | None = None,
    ) -> Image.Image:
        """读取或渲染一个快捷键自动图标。

        入参：已校验 shortcut；``appearance`` 是可选全局显示外观。
        返回：缓存命中或新创建的 RGB image。
        错误处理：渲染异常按原样传播。
        副作用：更新命中计数；miss 时写入缓存并按容量裁剪。
        """

        shortcut_key = json.dumps(
            shortcut.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        key = (shortcut_key, appearance_cache_key(appearance))
        cached = self._images.get(key)
        if cached is not None:
            self._hits += 1
            return cached
        self._misses += 1
        image = render_shortcut_key_image(shortcut, appearance=appearance)
        self._images[key] = image
        while len(self._images) > self._max_entries:
            self._images.pop(next(iter(self._images)))
        return image

    def diagnostics(self) -> dict[str, int]:
        """返回自动图标缓存的有界诊断。

        入参：无。
        返回：entries/max_entries/hits/misses 计数。
        错误处理：无。
        副作用：无。
        """

        return {
            "entries": len(self._images),
            "max_entries": self._max_entries,
            "hits": self._hits,
            "misses": self._misses,
        }


def shortcut_step_label(
    shortcut: KeyboardShortcutSpec,
    step_index: int,
) -> str:
    """返回一个快捷键步骤的紧凑平台标签。

    入参：完整 shortcut 和 0-based step index。
    返回：例如 ``⌘⇧P``、``⇧`` 或 ``Num 1``。
    错误处理：step index 越界按 Python IndexError 传播。
    副作用：无。
    """

    step = shortcut.steps[step_index]
    modifiers = "".join(_MODIFIER_SYMBOLS[modifier] for modifier in step.modifiers)
    return f"{modifiers}{_key_label(step.key)}"


def render_shortcut_key_image(
    shortcut: KeyboardShortcutSpec,
    *,
    size: tuple[int, int] = N4PRO_SHORTCUT_KEY_IMAGE_SIZE,
    appearance: DeckAppearanceSettings | None = None,
) -> Image.Image:
    """为快捷键规格绘制无内层底板的默认 N4 Pro 图标。

    入参：已校验 shortcut、至少 64x64 的目标尺寸和可选显示外观。
    返回：RGB Pillow image；快捷键标签直接绘制在 Key 基础背景上。
    错误处理：尺寸太小抛 ValueError；字体缺失时使用 Pillow 默认字体。
    副作用：只创建内存图片，字体读取是只读文件访问。
    """

    if size[0] < 64 or size[1] < 64:
        raise ValueError("shortcut key image size is too small")
    palette = resolve_render_palette(
        appearance,
        default_background=(11, 14, 18),
        default_foreground=(239, 244, 247),
    )
    canvas = Image.new("RGB", size, palette.background)
    draw = ImageDraw.Draw(canvas)

    labels = [shortcut_step_label(shortcut, index) for index in range(len(shortcut.steps))]
    if len(labels) == 1:
        _draw_centered_text(
            draw,
            canvas,
            labels[0],
            center_y=size[1] / 2,
            max_size=34,
            fill=palette.foreground,
        )
        return canvas
    if len(labels) == 2:
        _draw_centered_text(
            draw, canvas, labels[0], center_y=39, max_size=25, fill=palette.foreground
        )
        _draw_centered_text(
            draw, canvas, labels[1], center_y=75, max_size=25, fill=palette.foreground
        )
        return canvas

    _draw_centered_text(
        draw, canvas, labels[0], center_y=30, max_size=21, fill=palette.foreground
    )
    _draw_centered_text(
        draw, canvas, labels[1], center_y=58, max_size=21, fill=palette.foreground
    )
    _draw_centered_text(
        draw,
        canvas,
        f"+{len(labels) - 2}",
        center_y=86,
        max_size=18,
        fill=(111, 213, 255),
    )
    return canvas


def _key_label(key: str | None) -> str:
    """把 W3C key code 转成短显示标签。

    入参：可空 key code。
    返回：空 key 返回空串；字母、数字、F 键、数字键盘和特殊键返回紧凑文案。
    错误处理：未知键码返回原字符串，正常情况下会被模型白名单拦截。
    副作用：无。
    """

    if key is None:
        return ""
    if key.startswith("Key") and len(key) == 4:
        return key[-1]
    if key.startswith("Digit") and len(key) == 6:
        return key[-1]
    if key.startswith("Numpad") and key[-1:].isdigit():
        return f"Num {key[-1]}"
    return _KEY_LABELS.get(key, key)


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    canvas: Image.Image,
    text: str,
    *,
    center_y: float,
    max_size: int,
    fill: tuple[int, int, int] = (239, 244, 247),
) -> None:
    """按可用宽度缩小并居中绘制一行文字。

    入参：draw/canvas、文本、目标中心 y、最大字号和颜色。
    返回：无显式返回值。
    错误处理：字体加载失败由 ``_font`` fallback；极长文本最小缩到 11px。
    副作用：原地修改 canvas。
    """

    font_size = max_size
    font = _font(font_size)
    bbox = draw.textbbox((0, 0), text, font=font)
    while bbox[2] - bbox[0] > canvas.width - 24 and font_size > 11:
        font_size -= 1
        font = _font(font_size)
        bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    draw.text(
        ((canvas.width - width) / 2 - bbox[0], center_y - height / 2 - bbox[1]),
        text,
        font=font,
        fill=fill,
    )


def _font(size: int) -> ImageFont.ImageFont:
    """返回能清晰绘制快捷键符号与 hooked-return 的跨平台字体。

    入参：字号。
    返回：系统 TrueType 字体或 Pillow 默认字体。
    错误处理：逐个字体路径失败后 fallback，不抛文件缺失错误。
    副作用：可能只读系统字体文件。
    """

    # Pillow 加载 SFNS 时会把 ``↩`` 画成多条横线；Unicode 字体必须排在它前面。
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Apple Symbols.ttf",
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()
