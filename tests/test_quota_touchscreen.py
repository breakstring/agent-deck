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

_DEFAULT_SECONDARY = object()
"""测试 helper 用于区分默认双窗口与显式缺失 secondary 的哨兵值。"""


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


def test_render_quota_panel_draws_reset_credit_marker() -> None:
    """有可用 reset credit 时，订阅标签下方应绘制小钥匙标识。

    入参：无。
    返回：无返回值；断言通过表示 reset credit 数量被渲染到左侧订阅信息区域。
    错误处理：图标缺失或位置漂移时由 pytest 断言报告。
    副作用：无；只读取内存像素。
    """

    panel = render_quota_panel(_snapshot(reset_credits_available=1))

    icon_bounds = _color_bounds(panel, (248, 213, 113), x_range=range(30, 58))
    digit_bounds = _color_bounds(panel, (248, 213, 113), x_range=range(58, 85))

    assert icon_bounds is not None
    assert digit_bounds is not None
    assert abs(_bounds_center_y(icon_bounds) - _bounds_center_y(digit_bounds)) <= 1


def test_render_quota_panel_supports_a_single_monthly_window() -> None:
    """只有月限的订阅应只绘制实际存在的一行 quota，不访问 secondary。

    入参：无；测试构造 30 天 primary 窗口和 null secondary。
    返回：无返回值；断言通过代表 touch bar 不会因缺少旧 weekly 槽位而报错。
    错误处理：渲染器仍访问 secondary 或没有绘制主窗口进度条时由 pytest 报告。
    副作用：无；只创建内存图片。
    """

    panel = render_quota_panel(_snapshot(secondary=None, primary_duration_mins=43200))

    assert panel.size == (800, 136)
    assert any(
        panel.getpixel((x, y)) == (76, 205, 255)
        for y in range(45, 78)
        for x in range(350, 640)
    )


def _color_bounds(
    image: Image.Image,
    color: tuple[int, int, int],
    *,
    x_range: range,
    y_range: range = range(85, 120),
) -> tuple[int, int, int, int] | None:
    """返回指定颜色在局部区域内的像素包围盒。

    入参：`image` 是待检查图像；`color` 是 RGB 颜色；`x_range`/`y_range` 是扫描范围。
    返回：存在匹配像素时返回 `(left, top, right, bottom)`，否则返回 None。
    错误处理：像素读取失败时由 Pillow 异常传播。
    副作用：无；只读取内存像素。
    """

    points = [
        (x, y)
        for y in y_range
        for x in x_range
        if image.getpixel((x, y)) == color
    ]
    if not points:
        return None
    return (
        min(x for x, _ in points),
        min(y for _, y in points),
        max(x for x, _ in points),
        max(y for _, y in points),
    )


def _bounds_center_y(bounds: tuple[int, int, int, int]) -> float:
    """计算像素包围盒的垂直中心。

    入参：`bounds` 是 `(left, top, right, bottom)`。
    返回：上下边界的算术中心。
    错误处理：无。
    副作用：无。
    """

    return (bounds[1] + bounds[3]) / 2


def _snapshot(
    *,
    reset_credits_available: int | None = None,
    secondary: object = _DEFAULT_SECONDARY,
    primary_duration_mins: int = 300,
) -> CodexQuotaSnapshot:
    """构造固定 quota snapshot。

    入参：`reset_credits_available` 是可选 reset credit 数量；`secondary` 可显式设为 None
    模拟单窗口账户；`primary_duration_mins` 控制 primary 周期。
    返回：用于触屏渲染测试的 `CodexQuotaSnapshot`。
    错误处理：模型字段错误由 Pydantic 报告。
    副作用：无。
    """

    tz = ZoneInfo("Asia/Shanghai")
    secondary_payload = (
        {
            "used_percent": 8,
            "window_duration_mins": 10080,
            "resets_at": datetime(2026, 6, 24, 13, 47, 28, tzinfo=tz),
        }
        if secondary is _DEFAULT_SECONDARY
        else secondary
    )
    return CodexQuotaSnapshot(
        plan_type="prolite",
        plan_short_label="ProLite",
        plan_display_name="ProLite",
        primary={
            "used_percent": 28,
            "window_duration_mins": primary_duration_mins,
            "resets_at": datetime(2026, 6, 17, 19, 51, 2, tzinfo=tz),
        },
        secondary=secondary_payload,
        credits_balance="0",
        reset_credits_available=reset_credits_available,
        raw={},
    )
