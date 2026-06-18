"""Codex 按钮视觉资产的本地预渲染器。

本模块把 `codex.gif` 和 `codex.png` 转换成 renderer 可直接播放的 PNG 帧缓存：
idle 复用 GIF 基础帧，working/needs_user/error/completed 只在边缘和角标区域绘制
状态装饰，避免整图蒙版降低 Codex 图标本体清晰度；offline 从静态 PNG 生成低亮灰度图。
它只读调用方显式传入的源资产，只写调用方显式传入的输出目录，不访问真实
StreamDock 设备、不启动 daemon、不连接网络，也不修改 Agent 状态。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageEnhance, ImageOps, ImageSequence
from pydantic import BaseModel, ConfigDict, Field

_PREVIEW_ORDER: Final[tuple[str, ...]] = (
    "idle",
    "working",
    "needs_user",
    "error",
    "completed",
    "offline",
)


@dataclass(frozen=True)
class _SampledGifFrames:
    """源 GIF 按目标播放策略采样后的内存表示。

    入参：`frames` 是已缩放的 RGBA 输出帧；`source_frame_count` 是源 GIF 帧数；
    `source_duration_ms` 是参与采样的源时间轴长度；`source_frame_durations_ms`
    是源 GIF 每帧时长；`sample_times_ms` 是输出帧采样时间点；
    `sample_source_indexes` 是每个输出帧对应的源帧序号；`frame_duration_ms`
    是推荐播放每个输出帧的时长。
    返回：内部不可变 dataclass，供 builder 写帧、预览 GIF 与 manifest。
    错误处理：本类型不做主动校验；调用方负责在构造前完成参数检查。
    副作用：无；仅保存内存图像和采样元数据。
    """

    frames: list[Image.Image]
    source_frame_count: int
    source_duration_ms: int
    source_frame_durations_ms: list[int]
    sample_times_ms: list[int]
    sample_source_indexes: list[int]
    frame_duration_ms: int


class CodexVisualAssetBuildResult(BaseModel):
    """Codex 视觉资产构建结果。

    入参：`output_dir` 是生成根目录；`preview_path` 是 contact sheet 预览图路径；
    `manifest_path` 是生成 manifest 路径；`preview_gif_paths` 是每个动态变体的预览 GIF；
    `frame_size` 是每帧宽高像素；`variant_frame_counts` 记录每个变体生成的帧数。
    返回：frozen Pydantic model，可被 CLI 序列化或测试断言。
    错误处理：字段类型非法或帧尺寸非正时由 Pydantic 报告。
    副作用：仅保存构建结果元数据，不再读写文件。
    """

    model_config = ConfigDict(frozen=True)

    output_dir: Path
    preview_path: Path
    manifest_path: Path
    preview_gif_paths: dict[str, Path] = Field(default_factory=dict)
    frame_size: tuple[int, int]
    variant_frame_counts: dict[str, int]


def build_codex_visual_assets(
    *,
    source_gif: Path,
    source_png: Path,
    output_dir: Path,
    key_size: tuple[int, int] = (112, 112),
    target_fps: int = 10,
    max_duration_ms: int = 5000,
    max_frames: int | None = None,
) -> CodexVisualAssetBuildResult:
    """从 Codex 源 GIF/PNG 生成状态变体帧和预览图。

    入参：`source_gif` 是在线/工作/提醒类状态的源 GIF；`source_png` 是 offline 源 PNG；
    `output_dir` 是生成目录；`key_size` 是输出帧宽高像素，必须为正整数；
    `target_fps` 是按妙联宝动态图标建议采用的目标帧率，必须为正整数；
    `max_duration_ms` 是最多参与采样的源动画时长，必须为正整数；
    `max_frames` 是可选的输出帧数硬上限，设置时必须为正整数，且会在完整时间轴上均匀采样。
    返回：`CodexVisualAssetBuildResult`，包含输出目录、预览图和每个变体帧数。
    错误处理：源文件不存在、格式无法读取、尺寸或帧数非法时抛出 ValueError/FileNotFoundError
    或 Pillow 底层异常；调用方负责向 CLI 用户展示错误。
    副作用：创建 `output_dir`，写入多个 PNG 帧、每个动态变体的 `preview.gif`、
    `offline.png`、`preview.png` 和 `manifest.json`；不访问硬件、不连接网络、不修改源文件。
    """

    _validate_positive_size(key_size)
    if target_fps <= 0:
        raise ValueError("target_fps must be positive")
    if max_duration_ms <= 0:
        raise ValueError("max_duration_ms must be positive")
    if max_frames is not None and max_frames <= 0:
        raise ValueError("max_frames must be positive")
    if not source_gif.is_file():
        raise FileNotFoundError(f"source GIF not found: {source_gif}")
    if not source_png.is_file():
        raise FileNotFoundError(f"source PNG not found: {source_png}")

    output_dir.mkdir(parents=True, exist_ok=True)
    sampled_gif = _load_gif_frames(
        source_gif,
        key_size,
        target_fps=target_fps,
        max_duration_ms=max_duration_ms,
        max_frames=max_frames,
    )
    base_frames = sampled_gif.frames
    variant_frames = {
        "idle": [_copy_frame(frame) for frame in base_frames],
        "working": [
            _make_working_frame(frame, index, len(base_frames))
            for index, frame in enumerate(base_frames)
        ],
        "needs_user": [
            _make_needs_user_frame(frame, index, len(base_frames))
            for index, frame in enumerate(base_frames)
        ],
        "error": [
            _make_error_frame(frame, index, len(base_frames))
            for index, frame in enumerate(base_frames)
        ],
        "completed": [
            _make_completed_frame(frame, index, len(base_frames))
            for index, frame in enumerate(base_frames)
        ],
    }
    for variant, frames in variant_frames.items():
        _write_variant_frames(output_dir, variant, frames)
    preview_gif_paths = _write_variant_preview_gifs(
        output_dir,
        variant_frames,
        duration_ms=sampled_gif.frame_duration_ms,
    )

    offline_frame = _make_offline_frame(source_png, key_size)
    offline_path = output_dir / "offline.png"
    offline_frame.save(offline_path)
    preview_path = _write_preview(output_dir, variant_frames, offline_frame, key_size)
    manifest_path = _write_manifest(
        output_dir,
        key_size=key_size,
        target_fps=target_fps,
        max_duration_ms=max_duration_ms,
        max_frames=max_frames,
        sampled_gif=sampled_gif,
        variant_frames=variant_frames,
        preview_gif_paths=preview_gif_paths,
    )

    return CodexVisualAssetBuildResult(
        output_dir=output_dir,
        preview_path=preview_path,
        manifest_path=manifest_path,
        preview_gif_paths=preview_gif_paths,
        frame_size=key_size,
        variant_frame_counts={
            **{variant: len(frames) for variant, frames in variant_frames.items()},
            "offline": 1,
        },
    )


def _validate_positive_size(size: tuple[int, int]) -> None:
    """校验输出帧尺寸为正整数。

    入参：`size` 是 `(width, height)`。
    返回：无返回值；合法尺寸直接返回。
    错误处理：宽或高小于等于 0 时抛出 ValueError。
    副作用：无；只检查内存中的整数。
    """

    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("key_size must contain positive width and height")


def _load_gif_frames(
    source_gif: Path,
    key_size: tuple[int, int],
    *,
    target_fps: int,
    max_duration_ms: int,
    max_frames: int | None,
) -> _SampledGifFrames:
    """加载源 GIF，并按目标 FPS 在完整时间轴上重采样。

    入参：`source_gif` 是 GIF 路径；`key_size` 是输出尺寸；`target_fps` 是目标播放帧率；
    `max_duration_ms` 是参与采样的最长源动画时长；`max_frames` 是可选输出帧数上限。
    返回：`_SampledGifFrames`，包含至少一帧 RGBA 图和采样元数据。
    错误处理：GIF 无帧时抛出 ValueError；文件读取或解码失败由 Pillow 异常传播。
    副作用：只读取源 GIF，不写文件、不修改图像源。
    """

    source_frames: list[Image.Image] = []
    source_durations_ms: list[int] = []
    with Image.open(source_gif) as image:
        default_duration_ms = int(image.info.get("duration") or 100)
        for frame in ImageSequence.Iterator(image):
            source_frames.append(_fit_image(frame.convert("RGBA"), key_size))
            source_durations_ms.append(int(frame.info.get("duration") or default_duration_ms))
    if not source_frames:
        raise ValueError(f"source GIF contains no frames: {source_gif}")
    source_total_duration_ms = sum(max(1, duration) for duration in source_durations_ms)
    sampled_duration_ms = min(source_total_duration_ms, max_duration_ms)
    frame_duration_ms = max(1, round(1000 / target_fps))
    frame_count = max(1, math.ceil(sampled_duration_ms / frame_duration_ms))
    if max_frames is not None:
        frame_count = min(frame_count, max_frames)

    sample_times_ms = [
        min(sampled_duration_ms - 1, round(index * sampled_duration_ms / frame_count))
        for index in range(frame_count)
    ]
    sample_source_indexes = [
        _source_frame_index_at_time(sample_time, source_durations_ms)
        for sample_time in sample_times_ms
    ]
    return _SampledGifFrames(
        frames=[source_frames[index].copy() for index in sample_source_indexes],
        source_frame_count=len(source_frames),
        source_duration_ms=sampled_duration_ms,
        source_frame_durations_ms=source_durations_ms,
        sample_times_ms=sample_times_ms,
        sample_source_indexes=sample_source_indexes,
        frame_duration_ms=frame_duration_ms,
    )


def _source_frame_index_at_time(sample_time_ms: int, durations_ms: list[int]) -> int:
    """查找指定时间点对应的源 GIF 帧序号。

    入参：`sample_time_ms` 是从动画起点开始的毫秒偏移；`durations_ms` 是源帧时长列表。
    返回：包含该时间点的源帧下标；若时间点越过尾部则返回最后一帧。
    错误处理：空时长列表会抛 ValueError。
    副作用：无；只读取内存列表。
    """

    if not durations_ms:
        raise ValueError("durations_ms must contain at least one frame")
    elapsed_ms = 0
    for index, duration_ms in enumerate(durations_ms):
        elapsed_ms += max(1, duration_ms)
        if sample_time_ms < elapsed_ms:
            return index
    return len(durations_ms) - 1


def _fit_image(image: Image.Image, key_size: tuple[int, int]) -> Image.Image:
    """把图像等比缩放并居中放入目标尺寸透明画布。

    入参：`image` 是任意尺寸 RGBA 图像；`key_size` 是输出尺寸。
    返回：目标尺寸 RGBA 图像。
    错误处理：Pillow 缩放或粘贴失败时异常传播。
    副作用：只创建新的内存图像，不读写文件。
    """

    fitted = ImageOps.contain(image, key_size)
    canvas = Image.new("RGBA", key_size, (0, 0, 0, 0))
    x = (key_size[0] - fitted.width) // 2
    y = (key_size[1] - fitted.height) // 2
    canvas.alpha_composite(fitted, (x, y))
    return canvas


def _copy_frame(frame: Image.Image) -> Image.Image:
    """复制一帧图像，避免后续 overlay 修改原始基础帧。

    入参：`frame` 是 RGBA 图像。
    返回：独立副本。
    错误处理：Pillow copy 失败时异常传播。
    副作用：只分配内存，不访问文件或硬件。
    """

    return frame.copy()


def _make_working_frame(frame: Image.Image, index: int, total_frames: int) -> Image.Image:
    """生成 working 变体帧。

    入参：`frame` 是基础 GIF 帧；`index` 是帧序号；`total_frames` 是本轮动画总帧数，
    用于让扫光跨完整动画周期慢速移动。
    返回：保留图标主体并叠加青色边框与沿边框移动的高亮圆点的 RGBA 帧。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：只创建内存图像，不修改输入帧。
    """

    result = frame.copy()
    draw = ImageDraw.Draw(result, "RGBA")
    phase = _animation_phase(index, total_frames)
    border_width = _status_border_width(result.size)
    _draw_border(draw, result.size, (0, 210, 255, 145), width=border_width)
    _draw_activity_dot(
        draw,
        result.size,
        phase=phase,
        color=(125, 240, 255, 235),
    )
    return result


def _make_needs_user_frame(frame: Image.Image, index: int, total_frames: int) -> Image.Image:
    """生成 needs_user 变体帧。

    入参：`frame` 是基础 GIF 帧；`index` 是帧序号；`total_frames` 是本轮动画总帧数，
    用于让提醒色按完整周期慢呼吸。
    返回：保留图标主体并叠加琥珀色慢呼吸边框和用户操作角标的 RGBA 帧。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：只创建内存图像，不修改输入帧。
    """

    result = frame.copy()
    draw = ImageDraw.Draw(result, "RGBA")
    border_alpha = 178 + round(38 * _slow_pulse(index, total_frames))
    _draw_border(
        draw,
        result.size,
        (255, 190, 24, border_alpha),
        width=_status_border_width(result.size),
    )
    _draw_badge(draw, result.size, "user_action", (255, 185, 0, 230))
    return result


def _make_error_frame(frame: Image.Image, index: int, total_frames: int) -> Image.Image:
    """生成 error 变体帧。

    入参：`frame` 是基础 GIF 帧；`index` 是帧序号；`total_frames` 是本轮动画总帧数。
    返回：保留图标主体并叠加红色边框和错误角标的 RGBA 帧。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：只创建内存图像，不修改输入帧。
    """

    del index, total_frames
    result = frame.copy()
    draw = ImageDraw.Draw(result, "RGBA")
    _draw_border(
        draw,
        result.size,
        (255, 64, 80, 230),
        width=_status_border_width(result.size),
    )
    _draw_badge(draw, result.size, "error", (255, 72, 88, 235))
    return result


def _make_completed_frame(frame: Image.Image, index: int, total_frames: int) -> Image.Image:
    """生成 completed 变体帧。

    入参：`frame` 是基础 GIF 帧；`index` 是帧序号；`total_frames` 是本轮动画总帧数。
    返回：保留图标主体并叠加稳定绿色边框和成功角标的 RGBA 帧。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：只创建内存图像，不修改输入帧。
    """

    del index, total_frames
    result = frame.copy()
    draw = ImageDraw.Draw(result, "RGBA")
    _draw_border(
        draw,
        result.size,
        (75, 235, 145, 180),
        width=_status_border_width(result.size),
    )
    _draw_badge(draw, result.size, "success", (56, 220, 132, 220))
    return result


def _status_border_width(size: tuple[int, int]) -> int:
    """计算所有状态共用的边框宽度。

    入参：`size` 是输出帧 `(width, height)`。
    返回：状态边框像素宽度；N4 Pro 112px 图标使用 3px，小测试图标至少 2px。
    错误处理：非正尺寸时返回 2px，由上游尺寸校验负责拒绝非法输出尺寸。
    副作用：无；只计算整数。
    """

    short_side = min(size)
    if short_side >= 64:
        return 3
    return max(2, short_side // 16)


def _activity_dot_radius(size: tuple[int, int]) -> int:
    """计算 working 状态边缘高亮圆点半径。

    入参：`size` 是输出帧 `(width, height)`。
    返回：圆点半径像素；N4 Pro 112px 图标使用约 5px，直径明显大于 3px 边框。
    错误处理：极小尺寸至少返回 3px，避免圆点退化成边框端点。
    副作用：无；只计算整数。
    """

    return max(3, min(size) // 22)


def _draw_activity_dot(
    draw: ImageDraw.ImageDraw,
    size: tuple[int, int],
    *,
    phase: float,
    color: tuple[int, int, int, int],
) -> None:
    """沿边框绘制 working 状态的高亮圆点。

    入参：`draw` 是目标图像绘图对象；`size` 是图像尺寸；`phase` 是 0 到 1 的循环相位；
    `color` 是圆点颜色。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像，不访问外部 I/O。
    """

    radius = _activity_dot_radius(size)
    cx, cy = _border_point(phase, size)
    glow_radius = radius + max(2, radius // 2)
    draw.ellipse(
        (cx - glow_radius, cy - glow_radius, cx + glow_radius, cy + glow_radius),
        fill=(color[0], color[1], color[2], 54),
    )
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=color)


def _border_point(phase: float, size: tuple[int, int]) -> tuple[int, int]:
    """按矩形边框周长相位返回一个边缘点。

    入参：`phase` 是 0 到 1 的循环相位；`size` 是图像尺寸。
    返回：位于统一边框中心线附近的 `(x, y)` 坐标。
    错误处理：极小尺寸由坐标夹取处理。
    副作用：无；只计算坐标。
    """

    width, height = size
    inset = max(2, _status_border_width(size) // 2 + 1)
    left = inset
    top = inset
    right = max(left, width - inset - 1)
    bottom = max(top, height - inset - 1)
    side_width = max(1, right - left)
    side_height = max(1, bottom - top)
    perimeter = side_width * 2 + side_height * 2
    distance = (phase % 1.0) * perimeter

    if distance < side_width:
        return (round(left + distance), top)
    distance -= side_width
    if distance < side_height:
        return (right, round(top + distance))
    distance -= side_height
    if distance < side_width:
        return (round(right - distance), bottom)
    distance -= side_width
    return (left, round(bottom - min(distance, side_height)))


def _animation_phase(index: int, total_frames: int) -> float:
    """把帧序号转换成 0 到 1 的循环相位。

    入参：`index` 是当前输出帧序号；`total_frames` 是本轮动画输出总帧数。
    返回：归一化相位，单帧动画返回 0。
    错误处理：`total_frames` 小于等于 0 时按单帧处理，避免除零。
    副作用：无；只计算浮点数。
    """

    if total_frames <= 1:
        return 0.0
    return (index % total_frames) / total_frames


def _slow_pulse(index: int, total_frames: int) -> float:
    """按完整动画周期计算柔和呼吸强度。

    入参：`index` 是当前输出帧序号；`total_frames` 是本轮动画输出总帧数。
    返回：0 到 1 的余弦缓动值，首尾接近低点，避免 GIF 循环边界突兀。
    错误处理：`total_frames` 非正时由 `_animation_phase` 退化为 0。
    副作用：无；只计算浮点数。
    """

    phase = _animation_phase(index, total_frames)
    return (1 - math.cos(phase * math.tau)) / 2


def _make_offline_frame(source_png: Path, key_size: tuple[int, int]) -> Image.Image:
    """生成 offline 静态低亮图。

    入参：`source_png` 是静态 Codex PNG 路径；`key_size` 是输出尺寸。
    返回：灰度且降低亮度的 RGBA 图像。
    错误处理：文件读取或解码失败由 Pillow 异常传播。
    副作用：只读取源 PNG，不修改源文件，不访问硬件。
    """

    with Image.open(source_png) as image:
        fitted = _fit_image(image.convert("RGBA"), key_size)
    gray_rgb = ImageOps.grayscale(fitted.convert("RGB")).convert("RGBA")
    gray_rgb.putalpha(fitted.getchannel("A"))
    return ImageEnhance.Brightness(gray_rgb).enhance(0.45)


def _tint(frame: Image.Image, color: tuple[int, int, int], alpha: int) -> Image.Image:
    """给 RGBA 帧叠加全局状态色。

    入参：`frame` 是源帧；`color` 是 RGB 状态色；`alpha` 是 0-255 透明度。
    返回：叠色后的新 RGBA 图像。
    错误处理：Pillow alpha composite 失败时异常传播。
    副作用：只创建内存图像，不修改输入帧。
    """

    result = frame.copy()
    overlay = Image.new("RGBA", result.size, (*color, alpha))
    return Image.alpha_composite(result, overlay)


def _draw_border(
    draw: ImageDraw.ImageDraw,
    size: tuple[int, int],
    color: tuple[int, int, int, int],
    *,
    width: int,
) -> None:
    """绘制状态边框。

    入参：`draw` 是目标图像绘图对象；`size` 是图像尺寸；`color` 是 RGBA 边框色；
    `width` 是边框粗细像素。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像，不访问外部 I/O。
    """

    inset = max(1, width // 2)
    draw.rounded_rectangle(
        (inset, inset, size[0] - inset - 1, size[1] - inset - 1),
        radius=max(4, size[0] // 12),
        outline=color,
        width=width,
    )


def _draw_badge(
    draw: ImageDraw.ImageDraw,
    size: tuple[int, int],
    kind: str,
    color: tuple[int, int, int, int],
) -> None:
    """绘制角标符号。

    入参：`draw` 是目标图像绘图对象；`size` 是图像尺寸；`kind` 是 `user_action`、
    `error` 或 `success`；`color` 是角标背景色。
    返回：无返回值。
    错误处理：未知 kind 会被忽略；Pillow 绘制失败时异常传播。
    副作用：修改 `draw` 绑定的内存图像，不访问外部 I/O。
    """

    width, height = size
    radius = max(7, width // 7)
    cx = width - radius - max(3, width // 28)
    cy = radius + max(3, height // 28)
    bounds = (cx - radius, cy - radius, cx + radius, cy + radius)
    draw.ellipse(bounds, fill=color)
    symbol_color = (255, 255, 255, 240)
    stroke = max(2, width // 32)
    if kind == "user_action":
        draw.line((cx, cy - radius // 2, cx, cy + radius // 4), fill=symbol_color, width=stroke)
        draw.ellipse((cx - stroke, cy + radius // 2, cx + stroke, cy + radius // 2 + stroke * 2), fill=symbol_color)
    elif kind == "error":
        delta = max(3, radius // 2)
        draw.line((cx - delta, cy - delta, cx + delta, cy + delta), fill=symbol_color, width=stroke)
        draw.line((cx + delta, cy - delta, cx - delta, cy + delta), fill=symbol_color, width=stroke)
    elif kind == "success":
        draw.line(
            (cx - radius // 2, cy, cx - radius // 6, cy + radius // 3, cx + radius // 2, cy - radius // 3),
            fill=symbol_color,
            width=stroke,
            joint="curve",
        )


def _write_variant_frames(
    output_dir: Path,
    variant: str,
    frames: list[Image.Image],
) -> None:
    """写入一个动画变体的 PNG 帧序列。

    入参：`output_dir` 是生成根目录；`variant` 是变体目录名；`frames` 是 RGBA 帧列表。
    返回：无返回值。
    错误处理：目录不可写或 PNG 编码失败时异常传播。
    副作用：创建变体目录并写入 `frame_000.png` 等文件。
    """

    variant_dir = output_dir / variant
    variant_dir.mkdir(parents=True, exist_ok=True)
    for stale_frame in variant_dir.glob("frame_*.png"):
        stale_frame.unlink()
    for index, frame in enumerate(frames):
        frame.save(variant_dir / f"frame_{index:03d}.png")


def _write_variant_preview_gifs(
    output_dir: Path,
    variant_frames: dict[str, list[Image.Image]],
    *,
    duration_ms: int,
) -> dict[str, Path]:
    """写入每个动态变体最终合成的预览 GIF。

    入参：`output_dir` 是生成根目录；`variant_frames` 是动态变体帧映射；
    `duration_ms` 是每个输出帧的播放时长，单位毫秒。
    返回：变体名到 `preview.gif` 路径的映射。
    错误处理：缺少帧、目录不可写或 GIF 编码失败时异常传播。
    副作用：在每个动态变体目录写入或覆盖 `preview.gif`。
    """

    preview_paths: dict[str, Path] = {}
    for variant, frames in variant_frames.items():
        if not frames:
            raise ValueError(f"variant contains no frames: {variant}")
        preview_path = output_dir / variant / "preview.gif"
        gif_frames = [_gif_palette_frame(frame) for frame in frames]
        gif_frames[0].save(
            preview_path,
            save_all=True,
            append_images=gif_frames[1:],
            duration=duration_ms,
            loop=0,
            disposal=2,
        )
        preview_paths[variant] = preview_path
    return preview_paths


def _gif_palette_frame(frame: Image.Image) -> Image.Image:
    """把 RGBA 帧转换成适合 Pillow 写 GIF 的调色板帧。

    入参：`frame` 是 RGBA 源帧。
    返回：P 模式 GIF 帧。
    错误处理：Pillow 转换失败时异常传播。
    副作用：只创建内存图像，不读写文件。
    """

    return frame.convert("RGBA").convert("P", palette=Image.Palette.ADAPTIVE)


def _write_preview(
    output_dir: Path,
    variant_frames: dict[str, list[Image.Image]],
    offline_frame: Image.Image,
    key_size: tuple[int, int],
) -> Path:
    """写入所有变体的 contact sheet 预览图。

    入参：`output_dir` 是生成根目录；`variant_frames` 是动画变体到帧列表的映射；
    `offline_frame` 是离线静态图；`key_size` 是单元尺寸。
    返回：`preview.png` 路径。
    错误处理：预览图编码失败时异常传播。
    副作用：写入 `preview.png`，不访问硬件或网络。
    """

    columns = len(_PREVIEW_ORDER)
    preview = Image.new("RGBA", (key_size[0] * columns, key_size[1]), (16, 16, 20, 255))
    for column, variant in enumerate(_PREVIEW_ORDER):
        if variant == "offline":
            frame = offline_frame
        else:
            frame = variant_frames[variant][0]
        preview.alpha_composite(frame, (column * key_size[0], 0))
    preview_path = output_dir / "preview.png"
    preview.save(preview_path)
    return preview_path


def _write_manifest(
    output_dir: Path,
    *,
    key_size: tuple[int, int],
    target_fps: int,
    max_duration_ms: int,
    max_frames: int | None,
    sampled_gif: _SampledGifFrames,
    variant_frames: dict[str, list[Image.Image]],
    preview_gif_paths: dict[str, Path],
) -> Path:
    """写入资产生成 manifest，供 renderer 和人工检查复用。

    入参：`output_dir` 是生成根目录；`key_size` 是输出尺寸；`target_fps` 是目标帧率；
    `max_duration_ms` 是源动画采样时长上限；`max_frames` 是可选帧数上限；
    `sampled_gif` 是源 GIF 采样结果；`variant_frames` 是动态变体帧映射；
    `preview_gif_paths` 是每个动态变体的 GIF 预览路径。
    返回：`manifest.json` 路径。
    错误处理：JSON 序列化或文件写入失败时异常传播。
    副作用：写入或覆盖 `output_dir/manifest.json`。
    """

    manifest_path = output_dir / "manifest.json"
    payload = {
        "format_version": 1,
        "frame_size": list(key_size),
        "target_fps": target_fps,
        "frame_duration_ms": sampled_gif.frame_duration_ms,
        "max_duration_ms": max_duration_ms,
        "max_frames": max_frames,
        "source": {
            "frame_count": sampled_gif.source_frame_count,
            "duration_ms": sampled_gif.source_duration_ms,
            "frame_durations_ms": sampled_gif.source_frame_durations_ms,
        },
        "sampling": {
            "sample_times_ms": sampled_gif.sample_times_ms,
            "source_frame_indexes": sampled_gif.sample_source_indexes,
        },
        "variants": {
            variant: {
                "type": "animated",
                "frame_count": len(frames),
                "frames": f"{variant}/frame_{{index:03d}}.png",
                "preview_gif": _relative_posix(preview_gif_paths[variant], output_dir),
            }
            for variant, frames in variant_frames.items()
        },
        "offline": {
            "type": "static",
            "frame_count": 1,
            "image": "offline.png",
        },
        "preview_sheet": "preview.png",
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _relative_posix(path: Path, root: Path) -> str:
    """把生成路径转换成 manifest 使用的 POSIX 相对路径。

    入参：`path` 是生成文件路径；`root` 是生成根目录。
    返回：相对 `root` 的 POSIX 风格路径字符串。
    错误处理：`path` 不在 `root` 下时由 `relative_to` 抛出 ValueError。
    副作用：无；只做路径字符串转换。
    """

    return path.relative_to(root).as_posix()
