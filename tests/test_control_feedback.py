"""控制台短暂反馈 HUD 的渲染与过期行为测试。

本文件只验证内存 Pillow 图像和时间判断，不访问真实 touch bar、StreamDock、音频或显示器。
"""

from __future__ import annotations

from PIL import Image

from agent_deck.rendering.control_feedback import (
    ControlFeedback,
    ControlFeedbackKind,
    feedback_is_active,
    render_control_feedback_touchscreen,
)
from agent_deck.rendering.n4pro_panel import N4PRO_LOGICAL_PANEL_VIEWPORT


def test_value_feedback_is_active_until_expiry_and_renders_inside_touch_bar_viewport() -> None:
    """连续数值反馈应在到期前存活，并只覆盖底部 touch bar 虚拟面板。

    入参：无；测试构造固定时间戳与纯色基图。
    返回：无返回值；断言通过表示 renderer 不会把 HUD 变成固定状态条。
    错误处理：过期判断或图像尺寸错误时由 pytest 报告。
    副作用：只创建内存 Pillow 图像。
    """

    feedback = ControlFeedback(
        kind=ControlFeedbackKind.VALUE,
        label="Output volume",
        value="42%",
        expires_at_monotonic=12.5,
    )
    base = Image.new("RGB", (800, 480), (12, 14, 18))

    image = render_control_feedback_touchscreen(
        feedback,
        base_image=base,
        viewport=N4PRO_LOGICAL_PANEL_VIEWPORT,
    )

    assert feedback_is_active(feedback, now_monotonic=12.49) is True
    assert feedback_is_active(feedback, now_monotonic=12.5) is False
    assert image.size == base.size
    assert image.getpixel((400, 240)) == base.getpixel((400, 240))
    assert image.getpixel((400, 408)) != base.getpixel((400, 408))


def test_mute_and_error_feedback_use_unambiguous_red_treatment() -> None:
    """静音与失败反馈应使用红色，不依赖基础灯光颜色或暗亮度表达。

    入参：无；测试分别构造静音和错误 HUD。
    返回：无返回值；断言通过表示颜色语义符合产品确认的可辨识性要求。
    错误处理：颜色或 renderer 结果错误时由 pytest 报告。
    副作用：只创建内存图像。
    """

    base = Image.new("RGB", (800, 480), (12, 14, 18))
    mute = ControlFeedback(
        kind=ControlFeedbackKind.MUTE,
        label="Output muted",
        expires_at_monotonic=1.0,
    )
    error = ControlFeedback(
        kind=ControlFeedbackKind.ERROR,
        label="Unavailable",
        expires_at_monotonic=1.0,
    )

    mute_image = render_control_feedback_touchscreen(
        mute,
        base_image=base,
        viewport=N4PRO_LOGICAL_PANEL_VIEWPORT,
    )
    error_image = render_control_feedback_touchscreen(
        error,
        base_image=base,
        viewport=N4PRO_LOGICAL_PANEL_VIEWPORT,
    )

    assert mute_image.getpixel((400, 408))[0] > mute_image.getpixel((400, 408))[2]
    assert error_image.getpixel((400, 408))[0] > error_image.getpixel((400, 408))[2]
