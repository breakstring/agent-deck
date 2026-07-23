"""N4 Pro 底部虚拟显示面板合成器。

N4 Pro SDK 的 `set_touchscreen_image` 接收 800x480 的整块背景图，真实可读的底部
touch bar 只是这张背景图中的一个 viewport。本模块负责把任意 panel 图像合成到
该 viewport 中，避免上层 quota、决策或详情 panel 直接依赖整块背景坐标。本模块只
处理内存中的 Pillow 图像，不读写文件、不访问 Codex、不连接 StreamDock 硬件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from PIL import Image

from agent_deck.rendering.appearance import DeckAppearanceSettings, resolve_render_palette

N4PRO_BACKGROUND_SIZE: Final[tuple[int, int]] = (800, 480)
N4PRO_BACKGROUND_COLOR: Final[tuple[int, int, int]] = (14, 18, 28)


@dataclass(frozen=True)
class VirtualPanelViewport:
    """N4 Pro 背景图中的虚拟 panel 区域。

    入参：`left`/`top`/`right`/`bottom` 是背景图坐标系中的矩形边界，右下边界不包含。
    返回：不可变 viewport 描述，可用于验证、缩放和粘贴 panel 图像。
    错误处理：本类不主动校验边界；调用方通过 `validate_inside()` 校验。
    副作用：无。
    """

    left: int
    top: int
    right: int
    bottom: int

    @property
    def size(self) -> tuple[int, int]:
        """返回 viewport 尺寸。

        入参：无。
        返回：`(width, height)`。
        错误处理：若边界本身非法可能返回非正尺寸；调用方可用 `validate_inside()` 兜底。
        副作用：无。
        """

        return (self.right - self.left, self.bottom - self.top)

    def validate_inside(self, canvas_size: tuple[int, int]) -> None:
        """校验 viewport 是否落在背景画布内。

        入参：`canvas_size` 是背景图尺寸。
        返回：无返回值；校验通过表示可安全粘贴 panel。
        错误处理：viewport 越界或非正尺寸时抛出 `ValueError`。
        副作用：无。
        """

        width, height = canvas_size
        if self.left < 0 or self.top < 0 or self.right > width or self.bottom > height:
            raise ValueError("viewport must be inside the background canvas")
        panel_width, panel_height = self.size
        if panel_width <= 0 or panel_height <= 0:
            raise ValueError("viewport must have positive size")


N4PRO_TOUCH_BAR_VIEWPORT: Final[VirtualPanelViewport] = VirtualPanelViewport(
    left=0,
    top=340,
    right=800,
    bottom=476,
)
N4PRO_LOGICAL_PANEL_VIEWPORT: Final[VirtualPanelViewport] = N4PRO_TOUCH_BAR_VIEWPORT
"""Agent Deck logical panel 在当前 N4 Pro background 上的默认投影区域。

该常量是 `N4PRO_TOUCH_BAR_VIEWPORT` 的产品语义别名：quota、tokens、pets 和 message
都应先被视为 logical panel content，再由 renderer 决定当前投影到这个底部 viewport。
别名本身不改变坐标，也不触发任何硬件写入。
"""


def compose_n4pro_background(
    panel: Image.Image,
    *,
    viewport: VirtualPanelViewport = N4PRO_TOUCH_BAR_VIEWPORT,
    background_size: tuple[int, int] = N4PRO_BACKGROUND_SIZE,
    background_color: tuple[int, int, int] = N4PRO_BACKGROUND_COLOR,
    appearance: DeckAppearanceSettings | None = None,
) -> Image.Image:
    """把 panel 图像合成到 N4 Pro 背景图的底部 viewport。

    入参：`panel` 是上层 panel renderer 输出的图像；`viewport` 是目标区域；
    `background_size` 是 SDK 背景图尺寸；`background_color` 是未覆盖时的非 panel 区域底色；
    ``appearance`` 可提供跨表面的用户背景色。
    返回：RGB `Image`，尺寸为 `background_size`，可保存后交给 `set_touchscreen_image`。
    错误处理：viewport 非法时抛 `ValueError`；Pillow 缩放或转换异常按原异常传播。
    副作用：只创建内存图像，不访问文件、网络或硬件。
    """

    viewport.validate_inside(background_size)
    palette = resolve_render_palette(
        appearance,
        default_background=background_color,
    )
    background = Image.new("RGB", background_size, palette.background)
    panel_rgb = panel.convert("RGB")
    if panel_rgb.size != viewport.size:
        panel_rgb = panel_rgb.resize(viewport.size, Image.Resampling.LANCZOS)
    background.paste(panel_rgb, (viewport.left, viewport.top))
    return background
