"""Agent Deck reset credit 图标绘制工具。

本模块提供 touch bar 和主按键共用的小钥匙图标绘制函数，确保 quota panel 与状态型按键
使用同一套 reset credit 视觉语义。它只修改调用方传入的 Pillow 绘图对象，不读取文件、
不访问硬件，也不维护任何 daemon 状态。
"""

from __future__ import annotations

from PIL import ImageDraw


def draw_reset_credit_key_icon(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    size: int,
    *,
    color: tuple[int, int, int],
) -> None:
    """绘制 reset credit 使用的小钥匙图标。

    入参：`draw` 是目标绘图对象；`origin` 是图标左上角；`size` 是图标尺寸；`color`
    是线条颜色。
    返回：无返回值。
    错误处理：Pillow 绘图失败时异常向上传播。
    副作用：修改 `draw` 绑定的内存图像；不依赖系统 emoji 字体或外部图标文件。
    """

    x, y = origin
    bow_radius = max(4, size // 4)
    outline_width = max(2, round(size / 9))
    shaft_width = max(3, round(size / 6))
    end_width = max(2, round(size / 8))
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
        outline=color,
        width=outline_width,
    )
    draw.line((shaft_start, shaft_y, shaft_end, shaft_y), fill=color, width=shaft_width)
    draw.line(
        (tooth_x, shaft_y, tooth_x, shaft_y + tooth_h),
        fill=color,
        width=shaft_width,
    )
    draw.line(
        (shaft_end - 2, shaft_y, shaft_end - 2, shaft_y + tooth_h - 1),
        fill=color,
        width=end_width,
    )
