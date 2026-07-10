"""Virtual panel 的短暂控制反馈 HUD 渲染器。

本模块把已经确认执行成功或失败的控制动作表示为短暂叠加层。它不判断硬件输入、不执行系统音频
或亮度、不访问 StreamDock；daemon 负责维护 expiry，调用方把当前 base panel 图像传入后得到
同尺寸的 RGB 合成图，因此 HUD 不会变成 touch bar 的常驻状态带。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, field_validator

from agent_deck.rendering.n4pro_panel import VirtualPanelViewport


class ControlFeedbackKind(StrEnum):
    """描述短暂 HUD 的视觉语义类型。

    入参：枚举值来自 daemon action result。
    返回：作为 `ControlFeedback.kind` 的稳定值。
    错误处理：未知值由 Enum/Pydantic 拒绝。
    副作用：无。
    """

    VALUE = "value"
    MUTE = "mute"
    ERROR = "error"


class ControlFeedback(BaseModel):
    """描述一条会自动过期的 virtual panel 控制反馈。

    入参：`kind` 决定颜色语义；`label` 是简短状态；`value` 是可选百分比或文本；`expires_at`
    使用 monotonic clock 的截止时刻。
    返回：frozen Pydantic model。
    错误处理：空 label 或负数 expiry 由校验拒绝。
    副作用：仅保存内存数据，不渲染或访问时钟。
    """

    model_config = ConfigDict(frozen=True)

    kind: ControlFeedbackKind
    label: str
    value: str | None = None
    expires_at_monotonic: float

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        """校验 HUD 标签非空。

        入参：`value` 是待展示状态文本。
        返回：去除首尾空白后的文本。
        错误处理：空文本抛 ValueError。
        副作用：无。
        """

        normalized = value.strip()
        if not normalized:
            raise ValueError("control feedback label must not be empty")
        return normalized

    @field_validator("expires_at_monotonic")
    @classmethod
    def _validate_expiry(cls, value: float) -> float:
        """校验 monotonic 截止值不是负数。

        入参：`value` 是截止 monotonic 秒数。
        返回：原始秒数。
        错误处理：负数抛 ValueError。
        副作用：无。
        """

        if value < 0:
            raise ValueError("control feedback expiry must not be negative")
        return value


def feedback_is_active(feedback: ControlFeedback, *, now_monotonic: float) -> bool:
    """判断反馈 HUD 在给定 monotonic 时刻是否仍应显示。

    入参：`feedback` 是待检查反馈；`now_monotonic` 是当前 monotonic 秒数。
    返回：当前时刻严格早于截止时为 True，截止瞬间及之后为 False。
    错误处理：无。
    副作用：无；调用方决定是否读取真实时钟。
    """

    return now_monotonic < feedback.expires_at_monotonic


def render_control_feedback_touchscreen(
    feedback: ControlFeedback,
    *,
    base_image: Image.Image,
    viewport: VirtualPanelViewport,
) -> Image.Image:
    """把一个居中短暂 HUD 合成到原背景中的指定 virtual panel 区域。

    入参：`feedback` 是当前显示语义；`base_image` 是 N4 Pro 800x480 RGB 基图；`viewport` 是
    HUD 可出现的真实 touch bar 投影区域。
    返回：新的 RGB 图像，保持 base 尺寸，且 HUD 不会覆盖按键或控制台空白区。
    错误处理：viewport 越界、尺寸不支持或 Pillow 绘制错误按原异常传播。
    副作用：只创建和修改内存图像。
    """

    image = base_image.convert("RGB").copy()
    draw = ImageDraw.Draw(image)
    viewport.validate_inside(image.size)
    width, height = viewport.size
    accent = _accent_color(feedback.kind)
    panel_width = min(max(260, width // 3), max(260, width - 100))
    panel_height = min(126, max(96, height - 48))
    left = viewport.left + (width - panel_width) // 2
    top = viewport.top + (height - panel_height) // 2
    right = left + panel_width
    bottom = top + panel_height

    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=22,
        fill=_panel_fill(feedback.kind),
        outline=accent,
        width=3,
    )
    draw.ellipse((left + 25, top + 33, left + 53, top + 61), fill=accent)
    label_font = _load_font(18, bold=True)
    value_font = _load_font(34, bold=True)
    draw.text((left + 72, top + 26), feedback.label, fill=(230, 238, 248), font=label_font)
    if feedback.value:
        draw.text((left + 72, top + 51), feedback.value, fill=accent, font=value_font)
    return image


def _accent_color(kind: ControlFeedbackKind) -> tuple[int, int, int]:
    """返回 HUD 类型对应的明确语义颜色。

    入参：`kind` 是反馈类型。
    返回：数值类为青色，静音和错误均为红色。
    错误处理：枚举完整覆盖，不需要降级分支。
    副作用：无。
    """

    if kind == ControlFeedbackKind.VALUE:
        return (75, 207, 255)
    return (244, 82, 94)


def _panel_fill(kind: ControlFeedbackKind) -> tuple[int, int, int]:
    """返回 HUD 背景的低饱和语义色。

    入参：`kind` 是反馈类型。
    返回：数值类保留深蓝灰，静音和错误使用可见但克制的深红色底。
    错误处理：枚举完整覆盖，不需要降级分支。
    副作用：无。
    """

    if kind == ControlFeedbackKind.VALUE:
        return (15, 20, 29)
    return (48, 19, 27)


def _load_font(size: int, *, bold: bool) -> ImageFont.ImageFont:
    """加载可用系统字体并在缺失时回退 Pillow 默认字体。

    入参：`size` 是目标字号；`bold` 选择优先粗体候选。
    返回：Pillow 字体对象。
    错误处理：候选字体缺失或不能加载时继续尝试，最终返回默认字体。
    副作用：只读取系统字体文件。
    """

    candidates = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/SFNS.ttf",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()
