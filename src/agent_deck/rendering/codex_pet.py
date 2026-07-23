"""Codex 宠物精灵动画采样与 N4 Pro 纯 Pillow 渲染。

本模块把已验证的固定 cell 图集、全局宠物活动和 monotonic 时间转换为确定性的场景
样本，并渲染 112x112 Key 或 800x136 touchbar 图像。场景坐标与逐帧动画彼此独立；
渲染始终保留完整 192x208 cell，不裁透明边、不重新对齐角色。除调用方显式请求
预渲染 PNG 外，本模块不读写文件，也不连接硬件或启动刷新线程。
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from agent_deck.adapters.codex_pet import (
    CODEX_PET_CELL_SIZE,
    CodexPetAsset,
    PetActivity,
    PetActivitySnapshot,
)

PetAnimationName = Literal[
    "idle",
    "running-right",
    "running-left",
    "waving",
    "jumping",
    "failed",
    "waiting",
    "running",
    "review",
]
"""首版硬件会理解的 Codex v1/v2 公共动作行名称。"""

PetDirection = Literal["left", "right", "none"]
"""场景空间推进使用的离散方向。"""

KEY_CANVAS_SIZE: Final[tuple[int, int]] = (112, 112)
"""N4 Pro 单键宠物图像默认尺寸。"""

PANEL_CANVAS_SIZE: Final[tuple[int, int]] = (800, 136)
"""N4 Pro 虚拟 touchbar 中 PETS 面板的默认尺寸。"""

PET_BACKGROUND: Final[tuple[int, int, int]] = (11, 15, 22)
"""宠物 Key 与面板共享的 ``#0B0F16`` 背景色。"""

PET_GROUND: Final[tuple[int, int, int]] = (27, 42, 54)
"""PETS 面板的 ``#1B2A36`` 地面线颜色。"""

PET_ANIMATION_ROWS: Final[
    Mapping[PetAnimationName, tuple[int, tuple[int, ...]]]
] = {
    "idle": (0, (280, 110, 110, 140, 140, 320)),
    "running-right": (1, (120, 120, 120, 120, 120, 120, 120, 220)),
    "running-left": (2, (120, 120, 120, 120, 120, 120, 120, 220)),
    "waving": (3, (140, 140, 140, 280)),
    "jumping": (4, (140, 140, 140, 140, 280)),
    "failed": (5, (140, 140, 140, 140, 140, 140, 140, 240)),
    "waiting": (6, (150, 150, 150, 150, 150, 260)),
    "running": (7, (120, 120, 120, 120, 120, 220)),
    "review": (8, (150, 150, 150, 150, 150, 280)),
}
"""Codex v1 公共 9 行动作及每帧毫秒时长；v2 前 9 行沿用此合同。"""

_REACTION_BY_ACTIVITY: Final[Mapping[PetActivity, PetAnimationName]] = {
    PetActivity.NEEDS_INPUT: "waiting",
    PetActivity.BLOCKED: "failed",
    PetActivity.REVIEW: "review",
    PetActivity.READY: "review",
}

_RUNNING_ROUND_TRIP_SECONDS: Final[float] = 15.0
_IDLE_CYCLE_SECONDS: Final[float] = 45.0
_IDLE_STATIONARY_SECONDS: Final[float] = 30.0
_IDLE_WALK_SECONDS: Final[float] = 15.0
_REACTION_CYCLES: Final[int] = 3
_SLOW_IDLE_RATE: Final[float] = 0.5
_APP_COMPLETION_CYCLES: Final[int] = 3
_APP_COMPLETION_HOLD_SECONDS: Final[float] = 5.0


class PetSceneSample(BaseModel):
    """场景控制器在一个 monotonic 时刻产生的确定性视觉样本。

    入参：``activity`` 保留业务状态；``action``/``frame_index`` 定位图集 cell；``x``
    是 0..1 的水平归一化坐标；``direction`` 描述移动朝向；``reaction_active`` 标识
    三轮反应仍在播放；``sampled_at_monotonic`` 是本次绝对 monotonic 时间。
    返回：冻结模型，可被 Key/面板渲染器或 revision 计算复用。
    错误处理：越界 x、负帧号或负采样时间由 Pydantic 拒绝。
    副作用：无；只保存样本。
    """

    model_config = ConfigDict(frozen=True)

    activity: PetActivity
    action: PetAnimationName
    frame_index: int = Field(ge=0)
    x: float = Field(ge=0.0, le=1.0)
    direction: PetDirection = "none"
    reaction_active: bool = False
    sampled_at_monotonic: float = Field(ge=0.0)
    animation_elapsed_seconds: float = Field(default=0.0, ge=0.0)

    @property
    def frame_key(self) -> tuple[str, int]:
        """返回与水平坐标无关的精灵帧 revision key。

        入参：无；读取当前动作和帧号。
        返回：``(action, frame_index)`` 元组。
        错误处理：无。
        副作用：无。
        """

        return (self.action, self.frame_index)

    @property
    def key_frame_key(self) -> tuple[str, int]:
        """返回 Key 使用的非方向 running 动作与独立累计时长帧号。

        入参：无；读取当前动作和 ``animation_elapsed_seconds``。
        返回：方向步态映射为 ``running`` 后的 ``(action, frame_index)``；其他动作不变。
        错误处理：无；样本已校验累计时间非负有限。
        副作用：无。
        """

        return pet_key_frame_for_sample(self)

    def x_bucket(self, bucket_count: int) -> int:
        """把归一化坐标量化为有限 revision bucket。

        入参：``bucket_count`` 是包含左右端点的桶数量，必须至少为 2。
        返回：0 到 ``bucket_count - 1`` 的整数。
        错误处理：桶数量不足时抛 ValueError。
        副作用：无。
        """

        if bucket_count < 2:
            raise ValueError("pet x bucket count must be at least 2")
        return min(bucket_count - 1, round(self.x * (bucket_count - 1)))


class PetSceneController:
    """使用绝对 monotonic 时间推进宠物活动、反应和横向轨迹。

    入参：构造时设置 reduced motion、初始 x/方向和 monotonic 锚点；之后通过 ``sample``
    输入活动快照与绝对 monotonic 时间。
    返回：每次采样返回与调用频率无关的 ``PetSceneSample``，掉帧会直接跳到正确位置。
    错误处理：时间倒退、非有限时间或非法初始坐标抛 ValueError。
    副作用：维护最近触发键、轨迹锚点和位置；不访问文件、网络或硬件。
    """

    def __init__(
        self,
        *,
        reduced_motion: bool = False,
        initial_x: float = 0.0,
        initial_direction: Literal["left", "right"] = "right",
        started_at_monotonic: float | None = None,
    ) -> None:
        """创建一个尚未绑定活动快照的场景控制器。

        入参：``reduced_motion`` 固定 idle 首帧；``initial_x`` 是 0..1 坐标；
        ``initial_direction`` 决定首次散步；``started_at_monotonic`` 可固定测试时钟。
        返回：无显式返回；实例可立即采样。
        错误处理：非法坐标、方向或 monotonic 值抛 ValueError。
        副作用：未注入时间时读取 ``time.monotonic``，仅初始化内存状态。
        """

        if not 0.0 <= initial_x <= 1.0:
            raise ValueError("initial pet x must be between 0 and 1")
        if initial_direction not in {"left", "right"}:
            raise ValueError("initial pet direction must be left or right")
        started_at = (
            time.monotonic()
            if started_at_monotonic is None
            else started_at_monotonic
        )
        _validate_monotonic(started_at)
        self._reduced_motion = reduced_motion
        self._snapshot: PetActivitySnapshot | None = None
        self._anchor_monotonic = started_at
        self._anchor_x = initial_x
        self._anchor_direction: Literal["left", "right"] = initial_direction
        self._last_sample_monotonic = started_at

    @property
    def reduced_motion(self) -> bool:
        """返回当前是否固定 reduced-motion 视觉。

        入参：无。
        返回：布尔值；True 表示永远显示 idle 首帧且不移动。
        错误处理：无。
        副作用：无。
        """

        return self._reduced_motion

    def set_reduced_motion(self, enabled: bool) -> None:
        """更新 reduced-motion 开关而不改变当前水平坐标。

        入参：``enabled`` 是新的可访问性模式。
        返回：无显式返回。
        错误处理：无；按 Python truth value 保存布尔值。
        副作用：修改控制器内存开关；不会重触发活动反应。
        """

        self._reduced_motion = bool(enabled)

    def sample(
        self,
        snapshot: PetActivitySnapshot,
        *,
        monotonic_seconds: float | None = None,
    ) -> PetSceneSample:
        """在指定绝对 monotonic 时刻采样全局宠物场景。

        入参：``snapshot`` 是顶层 Codex 聚合结果；``monotonic_seconds`` 缺省读取系统
        monotonic 时钟。
        返回：当前动作、帧、位置和方向；相同 trigger key 不会重复开始三轮反应。
        错误处理：非有限或倒退的 monotonic 时间抛 ValueError。
        副作用：新触发时先采样旧轨迹保存 x，再更新锚点；普通采样只更新时间。
        """

        now = time.monotonic() if monotonic_seconds is None else monotonic_seconds
        _validate_monotonic(now)
        if now < self._last_sample_monotonic:
            raise ValueError("pet monotonic time must not move backwards")

        if self._snapshot is None:
            self._snapshot = snapshot
            self._anchor_monotonic = now
        elif snapshot.trigger_key != self._snapshot.trigger_key:
            previous = self._sample_current(now)
            self._anchor_x = previous.x
            if previous.direction in {"left", "right"}:
                self._anchor_direction = previous.direction
            self._anchor_monotonic = now
            self._snapshot = snapshot
        else:
            self._snapshot = snapshot

        result = self._sample_current(now)
        self._last_sample_monotonic = now
        return result

    def _sample_current(self, now: float) -> PetSceneSample:
        """基于当前锚点和活动生成一个不改变锚点的样本。

        入参：``now`` 已通过 monotonic 校验且不早于最近样本。
        返回：确定性的 ``PetSceneSample``。
        错误处理：首次外部 sample 前调用会退化为 idle 快照，不抛异常。
        副作用：无；仅计算内存值。
        """

        activity = (
            self._snapshot.activity
            if self._snapshot is not None
            else PetActivity.IDLE
        )
        if self._reduced_motion:
            return PetSceneSample(
                activity=activity,
                action="idle",
                frame_index=0,
                x=self._anchor_x,
                direction="none",
                sampled_at_monotonic=now,
                animation_elapsed_seconds=0.0,
            )

        elapsed = max(0.0, now - self._anchor_monotonic)
        reaction = _REACTION_BY_ACTIVITY.get(activity)
        if reaction is not None:
            reaction_duration = animation_duration_seconds(reaction) * _REACTION_CYCLES
            if elapsed < reaction_duration:
                return PetSceneSample(
                    activity=activity,
                    action=reaction,
                    frame_index=animation_frame_index(reaction, elapsed),
                    x=self._anchor_x,
                    direction="none",
                    reaction_active=True,
                    sampled_at_monotonic=now,
                    animation_elapsed_seconds=elapsed,
                )
            idle_elapsed = (elapsed - reaction_duration) * _SLOW_IDLE_RATE
            return PetSceneSample(
                activity=activity,
                action="idle",
                frame_index=animation_frame_index("idle", idle_elapsed),
                x=self._anchor_x,
                direction="none",
                sampled_at_monotonic=now,
                animation_elapsed_seconds=idle_elapsed,
            )

        if activity == PetActivity.RUNNING:
            x, direction = _reflected_running_position(
                origin=self._anchor_x,
                direction=self._anchor_direction,
                elapsed=elapsed,
            )
            action: PetAnimationName = (
                "running-right" if direction == "right" else "running-left"
            )
            return PetSceneSample(
                activity=activity,
                action=action,
                frame_index=animation_frame_index(action, elapsed),
                x=x,
                direction=direction,
                sampled_at_monotonic=now,
                animation_elapsed_seconds=elapsed,
            )

        x, direction, walking = _idle_position(
            origin=self._anchor_x,
            direction=self._anchor_direction,
            elapsed=elapsed,
        )
        if walking:
            action = "running-right" if direction == "right" else "running-left"
            frame_time = max(0.0, elapsed - _IDLE_STATIONARY_SECONDS)
        else:
            action = "idle"
            frame_time = elapsed * _SLOW_IDLE_RATE
        return PetSceneSample(
            activity=activity,
            action=action,
            frame_index=animation_frame_index(action, frame_time),
            x=x,
            direction=direction if walking else "none",
            sampled_at_monotonic=now,
            animation_elapsed_seconds=frame_time,
        )


class CodexAppPetOverlaySample(BaseModel):
    """描述一个 Codex/ChatGPT App 启动键覆盖层的确定性视觉样本。

    入参：``visible`` 决定是否覆盖基础 App 图；可见时 ``action`` 与 ``frame_index`` 定位
    图集帧；``animated`` 表示调度器可按预算推进该键；``completion_feedback`` 区分完成反馈。
    返回：frozen Pydantic model，供共享帧缓存和多 Key 调度器消费。
    错误处理：负帧号或负 monotonic 时间由 Pydantic 拒绝。
    副作用：无；只保存一次采样结果。
    """

    model_config = ConfigDict(frozen=True)

    activity: PetActivity
    visible: bool
    action: PetAnimationName | None = None
    frame_index: int | None = Field(default=None, ge=0)
    animated: bool = False
    completion_feedback: bool = False
    sampled_at_monotonic: float = Field(ge=0.0)


class CodexAppPetOverlayController:
    """按任务状态控制 App 启动键上的临时宠物覆盖层。

    入参：构造时固定 reduced-motion 与 monotonic 锚点；``sample`` 接收只含 Desktop 顶层
    task 的活动快照。
    返回：运行/介入/错误/明确 review 持续可见；完成反馈三轮 waving 后保持末帧五秒；
    idle 或完成窗口结束返回不可见样本。
    错误处理：非有限或倒退 monotonic 时间抛 ValueError。
    副作用：只维护当前 trigger、锚点与最近采样时间，不访问图片、文件或硬件。
    """

    def __init__(
        self,
        *,
        reduced_motion: bool = False,
        started_at_monotonic: float | None = None,
    ) -> None:
        """创建尚未绑定任务活动的覆盖层控制器。

        入参：``reduced_motion`` 令每种状态固定在代表帧；``started_at_monotonic`` 可用于测试。
        返回：无显式返回；实例可立即采样 idle。
        错误处理：非法 monotonic 值抛 ValueError。
        副作用：未注入时间时读取一次 ``time.monotonic``。
        """

        started_at = (
            time.monotonic()
            if started_at_monotonic is None
            else started_at_monotonic
        )
        _validate_monotonic(started_at)
        self._reduced_motion = bool(reduced_motion)
        self._snapshot: PetActivitySnapshot | None = None
        self._anchor_monotonic = started_at
        self._last_sample_monotonic = started_at

    def sample(
        self,
        snapshot: PetActivitySnapshot,
        *,
        monotonic_seconds: float | None = None,
    ) -> CodexAppPetOverlaySample:
        """采样任务态覆盖并在新状态 trigger 到来时立即重置反馈锚点。

        入参：``snapshot`` 已限定为 Desktop 顶层 task；``monotonic_seconds`` 可固定绝对时钟。
        返回：与调用频率无关的覆盖层动作、帧与可见性。
        错误处理：时间倒退、非有限值抛 ValueError。
        副作用：更新最近快照、trigger 锚点和最近采样时间；不渲染图片。
        """

        now = time.monotonic() if monotonic_seconds is None else monotonic_seconds
        _validate_monotonic(now)
        if now < self._last_sample_monotonic:
            raise ValueError("codex app pet monotonic time must not move backwards")
        if self._snapshot is None or snapshot.trigger_key != self._snapshot.trigger_key:
            self._anchor_monotonic = now
        self._snapshot = snapshot
        self._last_sample_monotonic = now
        return self._sample_current(now)

    def _sample_current(self, now: float) -> CodexAppPetOverlaySample:
        """根据当前活动与锚点生成一次不修改状态的覆盖样本。

        入参：``now`` 已校验且不早于最近采样。
        返回：当前业务规则对应的可见/不可见样本。
        错误处理：动作表缺失时由 mapping KeyError 暴露为代码缺陷。
        副作用：无。
        """

        activity = self._snapshot.activity if self._snapshot else PetActivity.IDLE
        elapsed = max(0.0, now - self._anchor_monotonic)
        if activity == PetActivity.IDLE:
            return CodexAppPetOverlaySample(
                activity=activity,
                visible=False,
                sampled_at_monotonic=now,
            )
        if activity == PetActivity.READY:
            action: PetAnimationName = "waving"
            animation_seconds = (
                animation_duration_seconds(action) * _APP_COMPLETION_CYCLES
            )
            total_seconds = animation_seconds + _APP_COMPLETION_HOLD_SECONDS
            if elapsed >= total_seconds:
                return CodexAppPetOverlaySample(
                    activity=activity,
                    visible=False,
                    completion_feedback=True,
                    sampled_at_monotonic=now,
                )
            holding = self._reduced_motion or elapsed >= animation_seconds
            frame_index = (
                len(PET_ANIMATION_ROWS[action][1]) - 1
                if holding
                else animation_frame_index(action, elapsed)
            )
            return CodexAppPetOverlaySample(
                activity=activity,
                visible=True,
                action=action,
                frame_index=frame_index,
                animated=not holding,
                completion_feedback=True,
                sampled_at_monotonic=now,
            )
        action_by_activity: Mapping[PetActivity, PetAnimationName] = {
            PetActivity.RUNNING: "running",
            PetActivity.NEEDS_INPUT: "waiting",
            PetActivity.BLOCKED: "failed",
            PetActivity.REVIEW: "review",
        }
        action = action_by_activity[activity]
        frame_index = 0 if self._reduced_motion else animation_frame_index(action, elapsed)
        return CodexAppPetOverlaySample(
            activity=activity,
            visible=True,
            action=action,
            frame_index=frame_index,
            animated=not self._reduced_motion,
            sampled_at_monotonic=now,
        )


def animation_duration_seconds(action: PetAnimationName) -> float:
    """返回一个 Codex 动作行完整循环的累计秒数。

    入参：``action`` 必须是 ``PET_ANIMATION_ROWS`` 中的公共动作名。
    返回：所有逐帧毫秒时长之和除以 1000。
    错误处理：未知动作由 mapping 访问抛 KeyError。
    副作用：无。
    """

    _, durations_ms = PET_ANIMATION_ROWS[action]
    return sum(durations_ms) / 1000.0


def animation_frame_index(action: PetAnimationName, elapsed_seconds: float) -> int:
    """按累计逐帧时长选择动作行帧号。

    入参：``action`` 是动作名；``elapsed_seconds`` 是从动作锚点开始的非负绝对耗时。
    返回：0 到该行最后使用列的帧号；超过一轮时取累计时长循环。
    错误处理：负值或非有限值抛 ValueError；未知动作抛 KeyError。
    副作用：无；不保存 tick 累计状态。
    """

    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError("pet animation elapsed time must be finite and non-negative")
    _, durations_ms = PET_ANIMATION_ROWS[action]
    total_ms = float(sum(durations_ms))
    position_ms = math.fmod(elapsed_seconds * 1000.0, total_ms)
    cumulative = 0.0
    for index, duration_ms in enumerate(durations_ms):
        cumulative += duration_ms
        if position_ms < cumulative:
            return index
    return len(durations_ms) - 1


def extract_pet_frame(
    asset: CodexPetAsset,
    *,
    action: PetAnimationName,
    frame_index: int,
) -> Image.Image:
    """从完整图集中裁出一个未经内容重排的固定 192x208 cell。

    入参：``asset`` 是已验证图集；``action`` 定位行；``frame_index`` 定位使用列。
    返回：新的 RGBA 192x208 图像，包含 cell 内全部透明边距和原坐标。
    错误处理：帧号越过该动作使用列时抛 ValueError；未知动作抛 KeyError。
    副作用：只复制内存像素，不修改资产。
    """

    row, durations_ms = PET_ANIMATION_ROWS[action]
    if frame_index < 0 or frame_index >= len(durations_ms):
        raise ValueError(f"pet frame index {frame_index} is invalid for {action}")
    cell_width, cell_height = CODEX_PET_CELL_SIZE
    left = frame_index * cell_width
    top = row * cell_height
    return asset.spritesheet.crop(
        (left, top, left + cell_width, top + cell_height)
    )


def pet_key_frame_for_sample(
    sample: PetSceneSample,
) -> tuple[PetAnimationName, int]:
    """把面板场景样本转换为 Key 的动作和帧号。

    入参：``sample`` 是同一控制器产生的场景；方向步态仅用于面板横移。
    返回：running-left/right 映射到非方向 ``running`` 并按该行自身累计时长采样；
    其他动作返回原动作和帧号。
    错误处理：无；动作由 ``PetSceneSample`` 类型约束。
    副作用：无。
    """

    if sample.action in {"running-left", "running-right"}:
        return (
            "running",
            animation_frame_index("running", sample.animation_elapsed_seconds),
        )
    return sample.action, sample.frame_index


def render_pet_key_image(
    asset: CodexPetAsset,
    sample: PetSceneSample,
    *,
    size: tuple[int, int] = KEY_CANVAS_SIZE,
    pet_height: int = 96,
    baseline_y: int = 104,
) -> Image.Image:
    """把一个宠物样本渲染为静止于中央的 112x112 Key 图像。

    入参：``asset`` 和 ``sample`` 定位帧；``size`` 是画布；``pet_height`` 是完整 cell
    缩放高度上限；``baseline_y`` 是缩放后 cell 的底线。running-left/right 会改用
    非方向性的 ``running`` 行，且忽略 ``sample.x``。
    返回：RGB Pillow 图像，默认严格 112x112。
    错误处理：画布/基线无法容纳完整 cell 时抛 ValueError；Pillow 错误按原异常传播。
    副作用：只创建内存图片，不写缓存或访问硬件。
    """

    _validate_render_geometry(size, pet_height=pet_height, baseline_y=baseline_y)
    action, frame_index = pet_key_frame_for_sample(sample)
    frame = extract_pet_frame(asset, action=action, frame_index=frame_index)
    fitted = _resize_full_cell(frame, pet_height=pet_height)
    x = (size[0] - fitted.width) // 2
    y = baseline_y - fitted.height
    canvas = Image.new("RGBA", size, (*PET_BACKGROUND, 255))
    canvas.alpha_composite(fitted, (x, y))
    return canvas.convert("RGB")


def render_pet_panel_image(
    asset: CodexPetAsset,
    sample: PetSceneSample,
    *,
    size: tuple[int, int] = PANEL_CANVAS_SIZE,
    pet_height: int = 96,
    baseline_y: int = 124,
    horizontal_margin: int = 8,
) -> Image.Image:
    """把宠物样本渲染到完整 800x136 touchbar 运动空间。

    入参：``asset``/``sample`` 定位帧与归一化 x；``size``、``pet_height``、底线和
    左右 margin 定义空间几何。
    返回：RGB Pillow 图像；角色不越界，背景仅含极简地面线，不在轨迹叠文字。
    错误处理：尺寸、边距或底线无法容纳完整 cell 时抛 ValueError。
    副作用：只创建内存图像，不写文件或访问硬件。
    """

    _validate_render_geometry(size, pet_height=pet_height, baseline_y=baseline_y)
    if horizontal_margin < 0:
        raise ValueError("pet panel horizontal margin must be non-negative")
    frame = extract_pet_frame(
        asset,
        action=sample.action,
        frame_index=sample.frame_index,
    )
    fitted = _resize_full_cell(frame, pet_height=pet_height)
    travel = size[0] - horizontal_margin * 2 - fitted.width
    if travel < 0:
        raise ValueError("pet panel is too narrow for the full sprite cell")
    x = horizontal_margin + round(travel * sample.x)
    y = baseline_y - fitted.height
    canvas = Image.new("RGBA", size, (*PET_BACKGROUND, 255))
    draw = ImageDraw.Draw(canvas)
    draw.line(
        (horizontal_margin, baseline_y, size[0] - horizontal_margin - 1, baseline_y),
        fill=(*PET_GROUND, 255),
        width=1,
    )
    canvas.alpha_composite(fitted, (x, y))
    return canvas.convert("RGB")


def pre_render_pet_key_frames(
    asset: CodexPetAsset,
    *,
    cache_dir: Path,
) -> dict[PetAnimationName, tuple[Path, ...]]:
    """一次性把全部公共动作行预渲染为 112x112 PNG 缓存。

    入参：``asset`` 是已加载图集；``cache_dir`` 必须由 daemon 指向其临时生命周期目录。
    返回：动作名到有序 PNG Path 元组的映射；所有帧均为完整 Key 输出。
    错误处理：目录创建、PNG 编码或 Pillow 渲染失败时原异常传播。
    副作用：创建缓存目录并覆盖同名派生 PNG；不修改原宠物包。
    """

    resolved_cache = cache_dir.expanduser()
    resolved_cache.mkdir(parents=True, exist_ok=True)
    rendered: dict[PetAnimationName, tuple[Path, ...]] = {}
    for action, (_, durations_ms) in PET_ANIMATION_ROWS.items():
        paths: list[Path] = []
        for frame_index in range(len(durations_ms)):
            sample = PetSceneSample(
                activity=_activity_for_action(action),
                action=action,
                frame_index=frame_index,
                x=0.0,
                direction="none",
                sampled_at_monotonic=0.0,
            )
            output_path = resolved_cache / f"{action}-{frame_index:02d}.png"
            render_pet_key_image(asset, sample).save(output_path, format="PNG")
            paths.append(output_path)
        rendered[action] = tuple(paths)
    return rendered


def _reflected_running_position(
    *,
    origin: float,
    direction: Literal["left", "right"],
    elapsed: float,
) -> tuple[float, Literal["left", "right"]]:
    """按 15 秒完整往返的三角波计算 running 位置和朝向。

    入参：``origin`` 是进入状态时位置；``direction`` 是继续方向；``elapsed`` 是绝对耗时。
    返回：0..1 位置和边界折返后的方向；15 秒后回到原位置和方向。
    错误处理：上层已校验输入，本函数不重复抛业务异常。
    副作用：无。
    """

    phase_origin = origin if direction == "right" else 2.0 - origin
    phase = math.fmod(
        phase_origin + elapsed * (2.0 / _RUNNING_ROUND_TRIP_SECONDS),
        2.0,
    )
    if phase < 1.0:
        return phase, "right"
    return 2.0 - phase, "left"


def _idle_position(
    *,
    origin: float,
    direction: Literal["left", "right"],
    elapsed: float,
) -> tuple[float, Literal["left", "right"], bool]:
    """按 45 秒周期计算约 30 秒驻留和 15 秒单向散步。

    入参：``origin``/``direction`` 是进入 idle 时锚点；``elapsed`` 是绝对耗时。
    返回：位置、该周期目标方向和当前是否处于 walking 阶段；周期间方向交替。
    错误处理：上层保证非负有限时间。
    副作用：无。
    """

    cycle = int(elapsed // _IDLE_CYCLE_SECONDS)
    phase = elapsed - cycle * _IDLE_CYCLE_SECONDS
    first_target = 1.0 if direction == "right" else 0.0
    second_target = 1.0 - first_target
    if cycle == 0:
        cycle_origin = origin
        cycle_target = first_target
    elif cycle % 2 == 1:
        cycle_origin = first_target
        cycle_target = second_target
    else:
        cycle_origin = second_target
        cycle_target = first_target
    cycle_direction: Literal["left", "right"] = (
        "right" if cycle_target >= cycle_origin else "left"
    )
    if phase < _IDLE_STATIONARY_SECONDS:
        return cycle_origin, cycle_direction, False
    progress = min(
        1.0,
        (phase - _IDLE_STATIONARY_SECONDS) / _IDLE_WALK_SECONDS,
    )
    position = cycle_origin + (cycle_target - cycle_origin) * progress
    return position, cycle_direction, True


def _resize_full_cell(frame: Image.Image, *, pet_height: int) -> Image.Image:
    """等比缩放完整固定 cell 到目标高度，保留全部透明边距。

    入参：``frame`` 应为完整 192x208 cell；``pet_height`` 是输出高度。
    返回：RGBA 缩放副本，宽度按原 cell 比例四舍五入。
    错误处理：非正高度抛 ValueError；Pillow resize 错误按原异常传播。
    副作用：只创建内存图片。
    """

    if pet_height <= 0:
        raise ValueError("pet render height must be positive")
    width = max(1, round(frame.width * pet_height / frame.height))
    return frame.convert("RGBA").resize(
        (width, pet_height),
        Image.Resampling.LANCZOS,
    )


def _validate_render_geometry(
    size: tuple[int, int],
    *,
    pet_height: int,
    baseline_y: int,
) -> None:
    """校验画布能容纳缩放后的完整 cell 与指定底线。

    入参：``size`` 是画布；``pet_height`` 是 cell 高度；``baseline_y`` 是 cell 底部 y。
    返回：几何合法时无返回值。
    错误处理：非正尺寸、角色越过顶部或底线越界时抛 ValueError。
    副作用：无。
    """

    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("pet render canvas must be positive")
    if pet_height <= 0 or baseline_y < pet_height or baseline_y >= height:
        raise ValueError("pet render baseline cannot contain the full sprite cell")


def _activity_for_action(action: PetAnimationName) -> PetActivity:
    """为预渲染样本选择不会改变目标帧的业务活动值。

    入参：``action`` 是公共动作名。
    返回：反应动作对应业务活动，running 类对应 RUNNING，其余对应 IDLE。
    错误处理：未知动作由类型和调用方 mapping 约束。
    副作用：无。
    """

    if action == "waiting":
        return PetActivity.NEEDS_INPUT
    if action == "failed":
        return PetActivity.BLOCKED
    if action == "review":
        return PetActivity.READY
    if action.startswith("running"):
        return PetActivity.RUNNING
    return PetActivity.IDLE


def _validate_monotonic(value: float) -> None:
    """拒绝负值或非有限的 monotonic 采样时间。

    入参：``value`` 是调用方注入或系统读取的秒数。
    返回：合法时无返回值。
    错误处理：负数、NaN 或无穷大抛 ValueError。
    副作用：无。
    """

    if not math.isfinite(value) or value < 0:
        raise ValueError("pet monotonic time must be finite and non-negative")
