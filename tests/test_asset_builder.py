"""Codex 视觉资产预渲染器的测试。

这些测试在 pytest 临时目录中生成小型 GIF/PNG 源素材，并验证预渲染器输出帧序列、
离线静态图和预览图。测试不读取项目真实 `assets/`，不访问 StreamDock 硬件，
不启动 daemon，也不执行网络 I/O；唯一副作用是写入 pytest 管理的临时目录。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops

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


def _write_sample_png(path: Path) -> None:
    """写入一个测试 PNG。

    入参：`path` 是 PNG 输出路径。
    返回：无返回值。
    错误处理：目录不可写或 Pillow 编码失败时异常传播给 pytest。
    副作用：在临时目录写入一个小型 PNG 文件。
    """

    Image.new("RGBA", (20, 20), (70, 70, 95, 255)).save(path)
