"""Agent Deck 品牌 splash 渲染测试。

这些测试只验证 logo 和默认 N4 Pro splash 的纯内存 Pillow 输出，不读取真实资产、
不访问 StreamDock SDK、不启动 daemon，也不写用户配置。
"""

from __future__ import annotations

from agent_deck.rendering.brand import (
    render_agent_deck_logo,
    render_agent_deck_splash_panel,
    render_agent_deck_splash_touchscreen,
)
from agent_deck.rendering.n4pro_panel import (
    N4PRO_BACKGROUND_COLOR,
    N4PRO_LOGICAL_PANEL_VIEWPORT,
)


def test_render_agent_deck_logo_returns_command_core_bitmap() -> None:
    """logo renderer 应优先输出 Command Core bitmap 资产。

    入参：无。
    返回：无返回值；断言通过表示 logo 尺寸、模式和中心像素内容稳定。
    错误处理：图像为空、尺寸错误或模式错误时由 pytest 报告。
    副作用：只创建内存图像。
    """

    image = render_agent_deck_logo(size=(128, 128))

    assert image.size == (128, 128)
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] > 0
    assert image.getpixel((64, 64))[3] > 0


def test_render_agent_deck_splash_panel_contains_branded_content() -> None:
    """默认 splash panel 应包含非背景内容和品牌状态区域。

    入参：无。
    返回：无返回值；断言通过表示 panel 尺寸稳定且关键区域不是空背景。
    错误处理：panel 退化为空背景或尺寸错误时由 pytest 报告。
    副作用：只创建内存图像。
    """

    image = render_agent_deck_splash_panel()

    assert image.size == N4PRO_LOGICAL_PANEL_VIEWPORT.size
    assert image.getpixel((60, 20)) != N4PRO_BACKGROUND_COLOR
    assert image.getpixel((82, 68)) != N4PRO_BACKGROUND_COLOR
    assert image.getpixel((650, 50)) != N4PRO_BACKGROUND_COLOR


def test_render_agent_deck_splash_touchscreen_uses_bottom_viewport() -> None:
    """默认 splash touchscreen 应只把内容放入 N4 Pro 底部 viewport。

    入参：无。
    返回：无返回值；断言通过表示背景层尺寸、上方按钮区域保护和底部内容存在。
    错误处理：内容错位或铺满整屏时由 pytest 报告。
    副作用：只创建内存图像。
    """

    image = render_agent_deck_splash_touchscreen()

    assert image.size == (800, 480)
    assert image.getpixel((400, 120)) == N4PRO_BACKGROUND_COLOR
    probe_x = N4PRO_LOGICAL_PANEL_VIEWPORT.left + 82
    probe_y = N4PRO_LOGICAL_PANEL_VIEWPORT.top + 68
    assert image.getpixel((probe_x, probe_y)) != N4PRO_BACKGROUND_COLOR
