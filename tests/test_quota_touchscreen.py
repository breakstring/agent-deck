"""Codex quota N4 Pro 背景屏渲染测试。

这些测试只在内存中绘制 Pillow 图像，不访问真实 N4 Pro、不启动 Codex app-server、
不读写用户账号数据。测试验证 800x480 背景图、底部 touch-bar viewport 和进度条像素。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image

from agent_deck.adapters.codex_quota import CodexQuotaSnapshot
from agent_deck.rendering.quota_touchscreen import render_quota_panel, render_quota_touchscreen


def test_render_quota_touchscreen_returns_n4pro_image() -> None:
    """quota 渲染应输出 N4 Pro SDK 背景图尺寸。

    入参：无；测试内构造 quota snapshot。
    返回：无返回值；断言通过表示输出为 800x480 RGB 图像。
    错误处理：尺寸或模式不对时由 pytest 断言报告。
    副作用：无；只创建内存图像。
    """

    image = render_quota_touchscreen(_snapshot())

    assert isinstance(image, Image.Image)
    assert image.size == (800, 480)
    assert image.mode == "RGB"


def test_render_quota_touchscreen_leaves_button_area_empty() -> None:
    """quota 内容不应绘制到 N4 Pro 的按键窗口区域。

    入参：无。
    返回：无返回值；断言通过表示顶部按键区域仍保持背景色。
    错误处理：若 quota UI 回归到整屏铺满，由 pytest 断言报告。
    副作用：无；只读取内存像素。
    """

    image = render_quota_touchscreen(_snapshot())
    background = image.getpixel((8, 8))

    assert image.getpixel((400, 120)) == background
    assert image.getpixel((400, 260)) == background


def test_render_quota_touchscreen_draws_progress_bars() -> None:
    """quota 渲染应在底部 touch-bar viewport 内绘制剩余配额进度条。

    入参：无。
    返回：无返回值；断言通过表示进度条按 `100 - used_percent` 而不是 used percent 绘制。
    错误处理：进度条未绘制、颜色退化或语义回归为已用百分比时由 pytest 断言报告。
    副作用：无；只读取内存像素。
    """

    image = render_quota_touchscreen(_snapshot())

    assert image.getpixel((520, 378)) == (76, 205, 255)
    assert image.getpixel((600, 426)) == (126, 236, 165)


def test_render_quota_panel_removes_subtitle_text() -> None:
    """quota panel 左侧不应再绘制 `Codex quota` 副标题。

    入参：无。
    返回：无返回值；断言通过表示旧副标题区域回到底色。
    错误处理：副标题重新出现时由 pytest 断言报告。
    副作用：无；只读取内存像素。
    """

    panel = render_quota_panel(_snapshot())

    assert panel.getpixel((50, 103)) == (18, 24, 36)


def test_render_quota_panel_aligns_labels_with_progress_bars() -> None:
    """quota 行标题应与右侧进度条按视觉中线对齐。

    入参：无。
    返回：无返回值；断言通过表示 `5hours:` 文本下沿进入 bar 中线附近。
    错误处理：标题仍显著偏高时由 pytest 断言报告。
    副作用：无；只读取内存像素。
    """

    panel = render_quota_panel(_snapshot())

    assert panel.getpixel((274, 42)) == (238, 244, 255)


def test_render_quota_panel_draws_reset_icon() -> None:
    """reset 时间前应有小图标，避免日期时间孤立出现。

    入参：无。
    返回：无返回值；断言通过表示 reset 文本左侧 gap 内存在 muted icon 像素。
    错误处理：图标缺失或位置漂移时由 pytest 断言报告。
    副作用：无；只读取内存像素。
    """

    panel = render_quota_panel(_snapshot())

    assert panel.getpixel((660, 35)) == (145, 160, 182)


def _snapshot() -> CodexQuotaSnapshot:
    """构造固定 quota snapshot。

    入参：无。
    返回：用于触屏渲染测试的 `CodexQuotaSnapshot`。
    错误处理：模型字段错误由 Pydantic 报告。
    副作用：无。
    """

    tz = ZoneInfo("Asia/Shanghai")
    return CodexQuotaSnapshot(
        plan_type="prolite",
        plan_short_label="ProLite",
        plan_display_name="ProLite",
        primary={
            "used_percent": 28,
            "window_duration_mins": 300,
            "resets_at": datetime(2026, 6, 17, 19, 51, 2, tzinfo=tz),
        },
        secondary={
            "used_percent": 8,
            "window_duration_mins": 10080,
            "resets_at": datetime(2026, 6, 24, 13, 47, 28, tzinfo=tz),
        },
        credits_balance="0",
        raw={},
    )
