"""Codex 视觉资产预渲染器的测试。

这些测试在 pytest 临时目录中生成小型 GIF/PNG 源素材，并验证预渲染器输出帧序列、
离线静态图和预览图。测试不读取项目真实 `assets/`，不访问 StreamDock 硬件，
不启动 daemon，也不执行网络 I/O；唯一副作用是写入 pytest 管理的临时目录。
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageChops

from agent_deck.rendering import asset_builder
from agent_deck.rendering.asset_builder import build_codex_visual_assets


def test_build_codex_visual_assets_writes_variants_and_preview(tmp_path: Path) -> None:
    """预渲染器应输出所有 Codex 主视觉态和预览图。

    入参：`tmp_path` 是 pytest 提供的临时目录。
    返回：无返回值；断言通过表示 frame 目录、offline 图和 preview 图都已生成。
    错误处理：缺少文件、尺寸错误或结果模型错误由 pytest 断言报告。
    副作用：在临时目录写入测试 GIF、PNG 和预渲染输出，不影响仓库文件。
    """

    source_gif = tmp_path / "codex.gif"
    source_png = tmp_path / "codex.png"
    output_dir = tmp_path / "generated"
    _write_sample_gif(source_gif)
    _write_sample_png(source_png)

    result = build_codex_visual_assets(
        source_gif=source_gif,
        source_png=source_png,
        output_dir=output_dir,
        key_size=(32, 32),
        max_frames=3,
    )

    assert result.output_dir == output_dir
    assert result.frame_size == (32, 32)
    assert result.preview_path == output_dir / "preview.png"
    assert result.preview_path.is_file()
    assert result.variant_frame_counts == {
        "idle": 3,
        "working": 3,
        "needs_user": 3,
        "error": 3,
        "completed": 3,
        "offline": 1,
    }
    for variant in ("idle", "working", "needs_user", "error", "completed"):
        frame_path = output_dir / variant / "frame_000.png"
        assert frame_path.is_file()
        with Image.open(frame_path) as image:
            assert image.size == (32, 32)
            assert image.mode == "RGBA"
    with Image.open(output_dir / "offline.png") as offline:
        assert offline.size == (32, 32)
        assert offline.mode == "RGBA"


def test_build_codex_visual_assets_resamples_full_timeline_and_writes_gif_previews(
    tmp_path: Path,
) -> None:
    """预渲染器应按目标 FPS 重采样完整动画时间轴。

    入参：`tmp_path` 是 pytest 临时目录。
    返回：无返回值；断言通过表示输出帧覆盖完整源 GIF 时长、manifest 记录播放参数，
    且每个动态状态都有最终合成的 `preview.gif`。
    错误处理：帧数、manifest 或 GIF 预览缺失时由 pytest 断言报告。
    副作用：只在临时目录写入测试素材和生成结果。
    """

    source_gif = tmp_path / "codex.gif"
    source_png = tmp_path / "codex.png"
    output_dir = tmp_path / "generated"
    _write_timed_sample_gif(source_gif)
    _write_sample_png(source_png)

    result = build_codex_visual_assets(
        source_gif=source_gif,
        source_png=source_png,
        output_dir=output_dir,
        key_size=(32, 32),
        target_fps=4,
        max_duration_ms=1000,
    )

    assert result.manifest_path == output_dir / "manifest.json"
    assert result.preview_gif_paths["working"] == output_dir / "working" / "preview.gif"
    assert result.variant_frame_counts == {
        "idle": 4,
        "working": 4,
        "needs_user": 4,
        "error": 4,
        "completed": 4,
        "offline": 1,
    }
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["target_fps"] == 4
    assert manifest["frame_duration_ms"] == 250
    assert manifest["source"]["duration_ms"] == 1000
    assert manifest["variants"]["idle"]["preview_gif"] == "idle/preview.gif"
    for variant in ("idle", "working", "needs_user", "error", "completed"):
        preview_gif = output_dir / variant / "preview.gif"
        assert preview_gif.is_file()
        with Image.open(preview_gif) as image:
            assert getattr(image, "n_frames", 1) == 4


def test_colored_variants_are_not_identical_to_idle(tmp_path: Path) -> None:
    """状态 overlay 变体不能和 idle 基础帧完全相同。

    入参：`tmp_path` 是 pytest 临时目录。
    返回：无返回值；断言通过表示 working、needs_user、error、completed 都实际改变像素。
    错误处理：某个 overlay 没有产生像素差异时由 pytest 断言报告。
    副作用：只在临时目录写入和读取测试图片。
    """

    source_gif = tmp_path / "codex.gif"
    source_png = tmp_path / "codex.png"
    output_dir = tmp_path / "generated"
    _write_sample_gif(source_gif)
    _write_sample_png(source_png)

    build_codex_visual_assets(
        source_gif=source_gif,
        source_png=source_png,
        output_dir=output_dir,
        key_size=(32, 32),
        max_frames=3,
    )

    idle_frame = Image.open(output_dir / "idle" / "frame_000.png").convert("RGBA")
    for variant in ("working", "needs_user", "error", "completed"):
        variant_frame = Image.open(output_dir / variant / "frame_000.png").convert("RGBA")
        assert ImageChops.difference(idle_frame, variant_frame).getbbox() is not None


def test_status_overlays_do_not_pulse_every_single_frame() -> None:
    """状态装饰的辅助动效不应污染图标主体。

    入参：无；测试直接使用一张内存基础帧和 30 帧动画周期。
    返回：无返回值；断言通过表示相邻帧不会改变图标中心区域。
    错误处理：动效修改图标主体时由 pytest 断言报告。
    副作用：无；只创建和读取内存图像。
    """

    base_frame = Image.new("RGBA", (40, 40), (20, 24, 32, 255))
    total_frames = 30

    working_0 = asset_builder._make_working_frame(base_frame, 0, total_frames)
    working_1 = asset_builder._make_working_frame(base_frame, 1, total_frames)
    assert _max_center_pixel_delta(working_0, working_1) == 0

    for make_frame in (
        asset_builder._make_needs_user_frame,
        asset_builder._make_error_frame,
        asset_builder._make_completed_frame,
    ):
        frame_0 = make_frame(base_frame, 0, total_frames)
        frame_1 = make_frame(base_frame, 1, total_frames)
        assert _max_center_pixel_delta(frame_0, frame_1) <= 8


def test_status_decorations_preserve_icon_body_and_use_one_border_width() -> None:
    """状态装饰不能用整图蒙版污染图标主体。

    入参：无；测试直接构造一张带透明度的基础帧。
    返回：无返回值；断言通过表示四个状态中心像素不变、边框宽度统一，并且 working
    的动态变化只发生在边缘区域。
    错误处理：整图 tint、横向扫光或边框宽度分裂时由 pytest 断言报告。
    副作用：无；只创建和比较内存图像。
    """

    base_frame = Image.new("RGBA", (80, 80), (40, 45, 70, 255))
    center = (base_frame.width // 2, base_frame.height // 2)
    total_frames = 30
    makers = (
        asset_builder._make_working_frame,
        asset_builder._make_needs_user_frame,
        asset_builder._make_error_frame,
        asset_builder._make_completed_frame,
    )

    assert asset_builder._status_border_width(base_frame.size) == 3
    assert asset_builder._activity_dot_radius(base_frame.size) > (
        asset_builder._status_border_width(base_frame.size) / 2
    )
    for make_frame in makers:
        decorated = make_frame(base_frame, 0, total_frames)
        assert decorated.getpixel(center) == base_frame.getpixel(center)

    working = asset_builder._make_working_frame(base_frame, 8, total_frames)
    assert _all_changed_pixels_are_near_edge(base_frame, working, edge_margin=12)


def _all_changed_pixels_are_near_edge(
    original: Image.Image,
    decorated: Image.Image,
    *,
    edge_margin: int,
) -> bool:
    """判断两张图的差异是否只位于边缘区域。

    入参：`original` 是源图；`decorated` 是装饰后图；`edge_margin` 是允许变化的边缘宽度。
    返回：若所有不同像素都在四周边缘范围内则为 True。
    错误处理：尺寸不一致时抛出 ValueError。
    副作用：无；只读取内存像素。
    """

    if original.size != decorated.size:
        raise ValueError("images must have the same size")
    diff = ImageChops.difference(original.convert("RGBA"), decorated.convert("RGBA"))
    for x in range(diff.width):
        for y in range(diff.height):
            if diff.getpixel((x, y)) == (0, 0, 0, 0):
                continue
            if (
                x < edge_margin
                or y < edge_margin
                or x >= diff.width - edge_margin
                or y >= diff.height - edge_margin
            ):
                continue
            return False
    return True


def _brightest_column(image: Image.Image) -> int:
    """查找图像中最亮列的位置。

    入参：`image` 是 RGBA 图像。
    返回：RGB 总亮度最高的列下标。
    错误处理：空尺寸图像会由 Pillow 访问像素时报错。
    副作用：无；只读取内存像素。
    """

    rgba = image.convert("RGBA")
    scores = []
    for x in range(rgba.width):
        score = 0
        for y in range(rgba.height):
            red, green, blue, alpha = rgba.getpixel((x, y))
            score += (red + green + blue) * alpha
        scores.append(score)
    return max(range(len(scores)), key=scores.__getitem__)


def _max_center_pixel_delta(left: Image.Image, right: Image.Image) -> int:
    """计算两张图中心像素的最大通道差。

    入参：`left` 和 `right` 是相同尺寸的 RGBA 图像。
    返回：中心像素四个通道的最大绝对差。
    错误处理：尺寸不一致或空尺寸会由 Pillow 访问像素时报错。
    副作用：无；只读取内存像素。
    """

    x = left.width // 2
    y = left.height // 2
    left_pixel = left.convert("RGBA").getpixel((x, y))
    right_pixel = right.convert("RGBA").getpixel((x, y))
    return max(abs(a - b) for a, b in zip(left_pixel, right_pixel, strict=True))


def _write_sample_gif(path: Path) -> None:
    """写入一个三帧测试 GIF。

    入参：`path` 是 GIF 输出路径。
    返回：无返回值。
    错误处理：目录不可写或 Pillow 编码失败时异常传播给 pytest。
    副作用：在临时目录写入一个小型 GIF 文件。
    """

    frames = [
        Image.new("RGBA", (20, 20), color)
        for color in ((30, 30, 40, 255), (45, 45, 65, 255), (60, 60, 90, 255))
    ]
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=80,
        loop=0,
    )


def _write_timed_sample_gif(path: Path) -> None:
    """写入一个总时长一秒的四帧测试 GIF。

    入参：`path` 是 GIF 输出路径。
    返回：无返回值。
    错误处理：目录不可写或 Pillow 编码失败时异常传播给 pytest。
    副作用：在临时目录写入一个小型 GIF 文件。
    """

    frames = [
        Image.new("RGBA", (20, 20), color)
        for color in (
            (25, 30, 40, 255),
            (45, 50, 65, 255),
            (65, 70, 90, 255),
            (85, 90, 115, 255),
        )
    ]
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=[250, 250, 250, 250],
        loop=0,
    )


def _write_sample_png(path: Path) -> None:
    """写入一个测试 PNG。

    入参：`path` 是 PNG 输出路径。
    返回：无返回值。
    错误处理：目录不可写或 Pillow 编码失败时异常传播给 pytest。
    副作用：在临时目录写入一个小型 PNG 文件。
    """

    Image.new("RGBA", (20, 20), (70, 70, 95, 255)).save(path)
