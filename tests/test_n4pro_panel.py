"""N4 Pro 虚拟显示面板渲染测试。

这些测试只在内存中组合 Pillow 图像，不访问真实 N4 Pro、不启动 Codex app-server、
不写文件。它们验证底部 touch-bar viewport 作为虚拟 panel 的边界契约。
"""

from __future__ import annotations

from PIL import Image

from agent_deck.rendering.n4pro_panel import (
    N4PRO_BACKGROUND_SIZE,
    N4PRO_LOGICAL_PANEL_VIEWPORT,
    N4PRO_TOUCH_BAR_VIEWPORT,
    VirtualPanelViewport,
    compose_n4pro_background,
)


def test_touch_bar_viewport_matches_calibrated_bottom_panel() -> None:
    """默认 touch-bar viewport 应匹配真机校准后的底部面板区域。

    入参：无。
    返回：无返回值；断言通过表示默认 viewport 的坐标和尺寸稳定。
    错误处理：坐标被意外改动时由 pytest 断言报告。
    副作用：无。
    """

    assert N4PRO_BACKGROUND_SIZE == (800, 480)
    assert N4PRO_TOUCH_BAR_VIEWPORT == VirtualPanelViewport(0, 340, 800, 476)
    assert N4PRO_LOGICAL_PANEL_VIEWPORT == N4PRO_TOUCH_BAR_VIEWPORT
    assert N4PRO_TOUCH_BAR_VIEWPORT.size == (800, 136)


def test_compose_n4pro_background_places_panel_only_in_viewport() -> None:
    """composer 应只把 panel 图像放进底部 viewport。

    入参：无。
    返回：无返回值；断言通过表示按钮区域保持背景色，底部 panel 区域出现内容。
    错误处理：panel 错位、铺满整屏或背景尺寸不符时由 pytest 断言报告。
    副作用：无；只创建内存图像。
    """

    panel = Image.new("RGB", N4PRO_TOUCH_BAR_VIEWPORT.size, (250, 0, 0))
    image = compose_n4pro_background(panel)

    assert image.size == N4PRO_BACKGROUND_SIZE
    assert image.getpixel((400, 120)) == (14, 18, 28)
    assert image.getpixel((400, 350)) == (250, 0, 0)


def test_compose_n4pro_background_resizes_panel_to_viewport() -> None:
    """composer 应把不同尺寸的 panel 缩放到 viewport。

    入参：无。
    返回：无返回值；断言通过表示 panel renderer 可以只关心自身逻辑尺寸。
    错误处理：缩放失败或模式转换失败时由 pytest 断言报告。
    副作用：无；只创建内存图像。
    """

    panel = Image.new("RGBA", (400, 68), (0, 250, 0, 255))
    image = compose_n4pro_background(panel)

    assert image.mode == "RGB"
    assert image.getpixel((400, 350)) == (0, 250, 0)
