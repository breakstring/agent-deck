"""Codex quota 按键环形图标渲染测试。

这些测试只在内存中绘制 Pillow 图像，不访问真实 N4 Pro、不启动 Codex app-server、
不读写用户账号数据。测试验证 112x112 图标尺寸，以及外环 5 小时限额、内环周限额都使用
剩余百分比语义。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image

from agent_deck.adapters.codex_quota import CodexQuotaSnapshot
from agent_deck.rendering.quota_key import render_quota_key_icon


def test_render_quota_key_icon_returns_n4pro_key_image() -> None:
    """quota 按键图标应输出 N4 Pro 主按键尺寸。

    入参：无；测试内构造 quota snapshot。
    返回：无返回值；断言通过表示输出为 112x112 RGB 图像。
    错误处理：尺寸或模式不对时由 pytest 断言报告。
    副作用：无；只创建内存图像。
    """

    image = render_quota_key_icon(_snapshot())

    assert isinstance(image, Image.Image)
    assert image.size == (112, 112)
    assert image.mode == "RGB"


def test_render_quota_key_icon_draws_nested_remaining_rings() -> None:
    """quota 按键图标应绘制双层剩余配额环形图。

    入参：无。
    返回：无返回值；断言通过表示外环使用 5 小时剩余比例，内环使用周限额剩余比例。
    错误处理：若环形图缺失或进度语义退回为已用比例，由 pytest 断言报告。
    副作用：无；只读取内存像素。
    """

    image = render_quota_key_icon(_snapshot())

    assert _near_color(image.getpixel((56, 10)), (76, 205, 255))
    assert _near_color(image.getpixel((11, 56)), (76, 205, 255))
    assert _near_color(image.getpixel((56, 30)), (126, 236, 165))
    assert not _near_color(image.getpixel((56, 83)), (126, 236, 165))


def test_render_quota_key_icon_rejects_tiny_size() -> None:
    """quota 按键图标尺寸过小时应明确失败。

    入参：无。
    返回：无返回值；断言通过表示 renderer 没有静默产出不可读图标。
    错误处理：未抛出或错误类型不对时由 pytest 报告。
    副作用：无。
    """

    try:
        render_quota_key_icon(_snapshot(), size=(48, 48))
    except ValueError as exc:
        assert str(exc) == "quota key icon size is too small"
    else:
        raise AssertionError("expected ValueError")


def _near_color(
    actual: tuple[int, int, int],
    expected: tuple[int, int, int],
    *,
    tolerance: int = 22,
) -> bool:
    """判断抗锯齿后的像素是否接近目标颜色。

    入参：`actual` 是采样像素；`expected` 是目标 RGB；`tolerance` 是每通道允许误差。
    返回：三通道差值都在容差内时返回 True。
    错误处理：无。
    副作用：无。
    """

    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected, strict=True))


def _snapshot() -> CodexQuotaSnapshot:
    """构造固定 quota snapshot。

    入参：无。
    返回：用于按键渲染测试的 `CodexQuotaSnapshot`。
    错误处理：模型字段错误由 Pydantic 报告。
    副作用：无。
    """

    tz = ZoneInfo("Asia/Shanghai")
    return CodexQuotaSnapshot(
        plan_type="prolite",
        plan_short_label="ProLite",
        plan_display_name="ProLite",
        primary={
            "used_percent": 25,
            "window_duration_mins": 300,
            "resets_at": datetime(2026, 6, 18, 13, 0, 0, tzinfo=tz),
        },
        secondary={
            "used_percent": 60,
            "window_duration_mins": 10080,
            "resets_at": datetime(2026, 6, 24, 13, 0, 0, tzinfo=tz),
        },
        credits_balance="0",
        raw={},
    )
