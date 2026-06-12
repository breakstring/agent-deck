"""Codex 按钮视觉资产的本地预渲染器。

本模块把 `codex.gif` 和 `codex.png` 转换成 renderer 可直接播放的 PNG 帧缓存：
idle 复用 GIF 基础帧，working/needs_user/error/completed 在 GIF 帧上叠加状态色和角标，
offline 从静态 PNG 生成低亮灰度图。它只读调用方显式传入的源资产，只写调用方显式
传入的输出目录，不访问真实 StreamDock 设备、不启动 daemon、不连接网络，也不修改
Agent 状态。
"""

from __future__ import annotations

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


class CodexVisualAssetBuildResult(BaseModel):
    """Codex 视觉资产构建结果。

    入参：`output_dir` 是生成根目录；`preview_path` 是 contact sheet 预览图路径；
    `frame_size` 是每帧宽高像素；`variant_frame_counts` 记录每个变体生成的帧数。
    返回：frozen Pydantic model，可被 CLI 序列化或测试断言。
    错误处理：字段类型非法或帧尺寸非正时由 Pydantic 报告。
    副作用：仅保存构建结果元数据，不再读写文件。
    """

    model_config = ConfigDict(frozen=True)

    output_dir: Path
    preview_path: Path
    frame_size: tuple[int, int]
    variant_frame_counts: dict[str, int]


def build_codex_visual_assets(
    *,
    source_gif: Path,
    source_png: Path,
    output_dir: Path,
    key_size: tuple[int, int] = (112, 112),
    max_frames: int = 12,
) -> CodexVisualAssetBuildResult:
    """从 Codex 源 GIF/PNG 生成状态变体帧和预览图。

    入参：`source_gif` 是在线/工作/提醒类状态的源 GIF；`source_png` 是 offline 源 PNG；
    `output_dir` 是生成目录；`key_size` 是输出帧宽高像素，必须为正整数；
    `max_frames` 限制每个动画变体最多输出多少帧，必须为正整数。
    返回：`CodexVisualAssetBuildResult`，包含输出目录、预览图和每个变体帧数。
    错误处理：源文件不存在、格式无法读取、尺寸或帧数非法时抛出 ValueError/FileNotFoundError
    或 Pillow 底层异常；调用方负责向 CLI 用户展示错误。
    副作用：创建 `output_dir`，写入多个 PNG 帧、`offline.png` 和 `preview.png`；
    不访问硬件、不连接网络、不修改源文件。
    """

    _validate_positive_size(key_size)
    if max_frames <= 0:
        raise ValueError("max_frames must be positive")
    if not source_gif.is_file():
        raise FileNotFoundError(f"source GIF not found: {source_gif}")
    if not source_png.is_file():
        raise FileNotFoundError(f"source PNG not found: {source_png}")

    output_dir.mkdir(parents=True, exist_ok=True)
    base_frames = _load_gif_frames(source_gif, key_size, max_frames)
    variant_frames = {
        "idle": [_copy_frame(frame) for frame in base_frames],
        "working": [_make_working_frame(frame, index) for index, frame in enumerate(base_frames)],
        "needs_user": [
            _make_needs_user_frame(frame, index) for index, frame in enumerate(base_frames)
        ],
        "error": [_make_error_frame(frame, index) for index, frame in enumerate(base_frames)],
        "completed": [
            _make_completed_frame(frame, index) for index, frame in enumerate(base_frames)
        ],
    }
    for variant, frames in variant_frames.items():
        _write_variant_frames(output_dir, variant, frames)

    offline_frame = _make_offline_frame(source_png, key_size)
    offline_path = output_dir / "offline.png"
    offline_frame.save(offline_path)
    preview_path = _write_preview(output_dir, variant_frames, offline_frame, key_size)

    return CodexVisualAssetBuildResult(
        output_dir=output_dir,
        preview_path=preview_path,
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
    max_frames: int,
) -> list[Image.Image]:
    """加载源 GIF 并转换成目标尺寸的 RGBA 帧。

    入参：`source_gif` 是 GIF 路径；`key_size` 是输出尺寸；`max_frames` 是帧数上限。
    返回：至少一帧 RGBA `Image` 列表。
    错误处理：GIF 无帧时抛出 ValueError；文件读取或解码失败由 Pillow 异常传播。
    副作用：只读取源 GIF，不写文件、不修改图像源。
    """

    frames: list[Image.Image] = []
    with Image.open(source_gif) as image:
        for index, frame in enumerate(ImageSequence.Iterator(image)):
            if index >= max_frames:
                break
            frames.append(_fit_image(frame.convert("RGBA"), key_size))
    if not frames:
        raise ValueError(f"source GIF contains no frames: {source_gif}")
    return frames


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


def _make_working_frame(frame: Image.Image, index: int) -> Image.Image:
    """生成 working 变体帧。

    入参：`frame` 是基础 GIF 帧；`index` 是帧序号，用于扫光位置。
    返回：叠加青色弱 overlay 和扫光条的 RGBA 帧。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：只创建内存图像，不修改输入帧。
    """

    result = _tint(frame, (0, 190, 255), 42)
    draw = ImageDraw.Draw(result, "RGBA")
    width, height = result.size
    sweep_width = max(4, width // 5)
    x = int((index % 6) / 5 * (width + sweep_width)) - sweep_width
    draw.rectangle((x, 0, x + sweep_width, height), fill=(120, 235, 255, 70))
    _draw_border(draw, result.size, (0, 210, 255, 150), width=max(2, width // 24))
    return result


def _make_needs_user_frame(frame: Image.Image, index: int) -> Image.Image:
    """生成 needs_user 变体帧。

    入参：`frame` 是基础 GIF 帧；`index` 是帧序号，用于脉冲强度。
    返回：叠加琥珀色脉冲边框和用户操作角标的 RGBA 帧。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：只创建内存图像，不修改输入帧。
    """

    alpha = 46 + (index % 3) * 22
    result = _tint(frame, (255, 176, 0), alpha)
    draw = ImageDraw.Draw(result, "RGBA")
    _draw_border(draw, result.size, (255, 190, 24, 210), width=max(3, result.width // 18))
    _draw_badge(draw, result.size, "user_action", (255, 185, 0, 230))
    return result


def _make_error_frame(frame: Image.Image, index: int) -> Image.Image:
    """生成 error 变体帧。

    入参：`frame` 是基础 GIF 帧；`index` 是帧序号，用于轻微闪烁强度。
    返回：叠加红色 overlay、边框和错误角标的 RGBA 帧。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：只创建内存图像，不修改输入帧。
    """

    alpha = 54 + (index % 2) * 18
    result = _tint(frame, (240, 48, 64), alpha)
    draw = ImageDraw.Draw(result, "RGBA")
    _draw_border(draw, result.size, (255, 64, 80, 230), width=max(3, result.width // 18))
    _draw_badge(draw, result.size, "error", (255, 72, 88, 235))
    return result


def _make_completed_frame(frame: Image.Image, index: int) -> Image.Image:
    """生成 completed 变体帧。

    入参：`frame` 是基础 GIF 帧；`index` 是帧序号，用于成功 flash 衰减。
    返回：叠加绿色短闪和成功角标的 RGBA 帧。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：只创建内存图像，不修改输入帧。
    """

    alpha = max(28, 92 - index * 18)
    result = _tint(frame, (56, 220, 132), alpha)
    draw = ImageDraw.Draw(result, "RGBA")
    _draw_border(draw, result.size, (75, 235, 145, 160), width=max(2, result.width // 24))
    _draw_badge(draw, result.size, "success", (56, 220, 132, 220))
    return result


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
