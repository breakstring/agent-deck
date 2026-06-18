"""Codex quota 的 N4 Pro 按键图标渲染器。

本模块把 `CodexQuotaSnapshot` 渲染为单个 StreamDock 按键可用的 112x112 RGB 图像。
它只处理内存中的 Pillow 图像，不读取 Codex、不访问 StreamDock 硬件、不写文件、不修改
daemon 状态。当前实验图标使用两个嵌套环形进度：外环表示 5 小时限额剩余比例，内环表示
周限额剩余比例；不显示数字和文字，便于先观察硬件按钮上的视觉密度。
"""

from __future__ import annotations

import math
from typing import Final

from PIL import Image, ImageDraw

from agent_deck.adapters.codex_quota import CodexQuotaSnapshot

N4PRO_KEY_ICON_SIZE: Final[tuple[int, int]] = (112, 112)

_SCALE: Final[int] = 4
_BACKGROUND: Final[tuple[int, int, int]] = (13, 18, 28)
_TRACK: Final[tuple[int, int, int]] = (42, 52, 72)
_PRIMARY: Final[tuple[int, int, int]] = (76, 205, 255)
_SECONDARY: Final[tuple[int, int, int]] = (126, 236, 165)


def render_quota_key_icon(
    snapshot: CodexQuotaSnapshot,
    *,
    size: tuple[int, int] = N4PRO_KEY_ICON_SIZE,
) -> Image.Image:
    """把 Codex quota 快照渲染为双层环形按键图标。

    入参：`snapshot` 是 Codex quota adapter 解析出的快照；`size` 是输出图像尺寸，默认
    N4 Pro 主按键的 112x112。
    返回：RGB `Image`；外环进度是 5 小时限额剩余比例，内环进度是周限额剩余比例。
    错误处理：尺寸小于 64x64 时抛 `ValueError`；Pillow 绘制异常会向上传播。
    副作用：只创建内存图像，不访问文件、网络或硬件。
    """

    _validate_icon_size(size)
    width, height = size
    canvas = Image.new("RGB", (width * _SCALE, height * _SCALE), _BACKGROUND)
    draw = ImageDraw.Draw(canvas)

    center = (width * _SCALE // 2, height * _SCALE // 2)
    min_side = min(width, height) * _SCALE
    outer_radius = min_side // 2 - 11 * _SCALE
    inner_radius = outer_radius - 19 * _SCALE
    outer_width = 10 * _SCALE
    inner_width = 9 * _SCALE

    _draw_ring(
        draw,
        center=center,
        radius=outer_radius,
        width=outer_width,
        percent=_remaining_percent(snapshot.primary.used_percent),
        color=_PRIMARY,
    )
    _draw_ring(
        draw,
        center=center,
        radius=inner_radius,
        width=inner_width,
        percent=_remaining_percent(snapshot.secondary.used_percent),
        color=_SECONDARY,
    )
    _draw_center_disc(draw, center=center, radius=inner_radius - 9 * _SCALE)

    return canvas.resize(size, Image.Resampling.LANCZOS)


def _validate_icon_size(size: tuple[int, int]) -> None:
    """校验按键图标尺寸是否足够容纳双层环。

    入参：`size` 是输出图像尺寸。
    返回：无返回值；校验通过表示可用于当前环形布局。
    错误处理：宽或高小于 64 时抛 `ValueError`。
    副作用：无。
    """

    width, height = size
    if width < 64 or height < 64:
        raise ValueError("quota key icon size is too small")


def _remaining_percent(used_percent: int) -> int:
    """把 Codex 已用百分比转换为剩余百分比。

    入参：`used_percent` 是 app-server `usedPercent` 字段，语义为已使用比例。
    返回：0-100 之间的剩余比例。
    错误处理：非整数类型由调用方类型约束处理；越界数值会被夹紧。
    副作用：无。
    """

    used = max(0, min(100, used_percent))
    return 100 - used


def _draw_ring(
    draw: ImageDraw.ImageDraw,
    *,
    center: tuple[int, int],
    radius: int,
    width: int,
    percent: int,
    color: tuple[int, int, int],
) -> None:
    """绘制一个从 12 点方向开始的环形剩余进度。

    入参：`draw` 是绘图对象；`center` 是圆心；`radius` 是环中心线半径；`width` 是环宽；
    `percent` 是 0-100 的剩余比例；`color` 是进度色。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像。
    """

    cx, cy = center
    bounds = (cx - radius, cy - radius, cx + radius, cy + radius)
    track_points = _arc_points(center=center, radius=radius, start=-90, end=270)
    _draw_arc_path(draw, points=track_points, width=width, color=_TRACK)
    clamped = max(0, min(100, percent))
    if clamped <= 0:
        return
    if clamped >= 100:
        _draw_arc_path(draw, points=track_points, width=width, color=color)
        return
    start_angle = -90
    end_angle = start_angle + round(360 * clamped / 100)
    points = _arc_points(center=center, radius=radius, start=start_angle, end=end_angle)
    _draw_arc_path(draw, points=points, width=width, color=color)


def _draw_arc_path(
    draw: ImageDraw.ImageDraw,
    *,
    points: list[tuple[int, int]],
    width: int,
    color: tuple[int, int, int],
) -> None:
    """用同一组中心线点绘制环形线段和圆角端点。

    入参：`draw` 是绘图对象；`points` 是圆弧中心线采样点；`width` 是环宽；`color` 是颜色。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像。
    """

    draw.line(points, fill=color, width=width, joint="curve")
    _draw_ring_cap(draw, point=points[0], width=width, color=color)
    _draw_ring_cap(
        draw,
        point=points[-1],
        width=width,
        color=color,
    )


def _arc_points(
    *,
    center: tuple[int, int],
    radius: int,
    start: int,
    end: int,
) -> list[tuple[int, int]]:
    """按圆周采样出用于绘制环形进度的中心线点。

    入参：`center` 是圆心；`radius` 是环中心线半径；`start`/`end` 是角度，0 度位于
    3 点方向，负 90 度位于 12 点方向。
    返回：至少两个 `(x, y)` 点，供 `ImageDraw.line` 绘制宽线。
    错误处理：本函数不主动抛异常；调用方保证角度范围和半径有效。
    副作用：无。
    """

    steps = max(2, abs(end - start) // 3)
    cx, cy = center
    points: list[tuple[int, int]] = []
    for index in range(steps + 1):
        angle_degrees = start + (end - start) * index / steps
        angle = math.radians(angle_degrees)
        points.append(
            (
                round(cx + math.cos(angle) * radius),
                round(cy + math.sin(angle) * radius),
            )
        )
    return points


def _draw_ring_cap(
    draw: ImageDraw.ImageDraw,
    *,
    point: tuple[int, int],
    width: int,
    color: tuple[int, int, int],
) -> None:
    """绘制环形进度的圆角端点。

    入参：`draw` 是绘图对象；`point` 是弧线中心线端点；`width` 是环宽；`color` 是端点颜色。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像。
    """

    x, y = point
    cap_radius = width // 2
    draw.ellipse(
        (
            round(x - cap_radius),
            round(y - cap_radius),
            round(x + cap_radius),
            round(y + cap_radius),
        ),
        fill=color,
    )


def _draw_center_disc(
    draw: ImageDraw.ImageDraw,
    *,
    center: tuple[int, int],
    radius: int,
) -> None:
    """绘制中央留白圆盘，保持无文字实验图标的视觉焦点。

    入参：`draw` 是绘图对象；`center` 是圆心；`radius` 是圆盘半径。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像。
    """

    cx, cy = center
    bounds = (cx - radius, cy - radius, cx + radius, cy + radius)
    draw.ellipse(bounds, fill=_BACKGROUND)
