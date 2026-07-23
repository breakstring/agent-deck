"""多个 ChatGPT App 活动任务的 PETS 面板场景与纯 Pillow 渲染。

每个顶层任务拥有独立的全宽位置、方向和持续变化的平滑速度。远端任务按 host key 使用
稳定低饱和细光环，本地任务不画地垫；碰撞只在间歇窗口启用，避免形成固定领地。本模块
不读取 App/Agent 状态、不写缓存、不连接硬件，素材与业务快照由 runtime 注入。
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from PIL import Image, ImageDraw, ImageFilter
from pydantic import BaseModel, ConfigDict, Field

from agent_deck.adapters.codex_pet import (
    CodexAppPetActorSnapshot,
    CodexPetAsset,
    PetActivity,
)
from agent_deck.config import CodexPetPatrolSpeed
from agent_deck.rendering.codex_pet import (
    PANEL_CANVAS_SIZE,
    PET_ANIMATION_ROWS,
    PET_BACKGROUND,
    PET_GROUND,
    PetAnimationName,
    animation_duration_seconds,
    animation_frame_index,
    extract_pet_frame,
)

_PET_HEIGHT: Final[int] = 96
_BASELINE_Y: Final[int] = 124
_PET_RENDER_WIDTH: Final[int] = round(192 * _PET_HEIGHT / 208)
_PET_HALF_WIDTH: Final[float] = _PET_RENDER_WIDTH / 2
_COLLISION_DISTANCE: Final[float] = _PET_RENDER_WIDTH * 0.72
_COLLISION_PERIOD_SECONDS: Final[float] = 18.0
_COLLISION_WINDOW_START_SECONDS: Final[float] = 6.0
_COLLISION_WINDOW_END_SECONDS: Final[float] = 12.0
_COLLISION_COOLDOWN_SECONDS: Final[float] = 1.2
_COLLISION_BOUNCE_SECONDS: Final[float] = 0.45
_COMPLETION_CYCLES: Final[int] = 3
_COMPLETION_HOLD_SECONDS: Final[float] = 5.0
_SPEED_MULTIPLIER_BY_PROFILE: Final[dict[CodexPetPatrolSpeed, float]] = {
    CodexPetPatrolSpeed.SLOW: 0.72,
    CodexPetPatrolSpeed.MEDIUM: 1.0,
    CodexPetPatrolSpeed.FAST: 1.3,
}

REMOTE_HOST_COLORS: Final[tuple[tuple[int, int, int], ...]] = (
    (72, 207, 194),
    (145, 112, 242),
    (238, 163, 86),
    (91, 157, 218),
    (204, 116, 171),
    (104, 188, 128),
    (219, 135, 108),
    (112, 135, 218),
    (179, 164, 91),
    (93, 185, 202),
    (170, 122, 206),
    (118, 174, 105),
)
"""按 host hash 选择的 12 色低饱和光环池；避免使用高亮状态红绿。"""


class PetColonyActorSample(BaseModel):
    """一帧中的单只任务宠物视觉样本。"""

    model_config = ConfigDict(frozen=True)

    agent_key: str
    activity: PetActivity
    action: PetAnimationName
    frame_index: int = Field(ge=0)
    x: float = Field(ge=0.0, le=1.0)
    direction: int = Field(ge=-1, le=1)
    speed_pixels_per_second: float = Field(ge=0.0)
    remote_color: tuple[int, int, int] | None = None
    y_offset_pixels: int = Field(default=0, ge=-8, le=0)

    @property
    def visual_key(self) -> tuple[object, ...]:
        """返回适合 panel revision 的紧凑视觉 key。"""

        return (
            self.agent_key,
            self.activity.value,
            self.action,
            self.frame_index,
            round(self.x * 799),
            self.direction,
            self.remote_color,
            self.y_offset_pixels,
        )


class PetColonySceneSample(BaseModel):
    """一个 monotonic 时刻的多宠物场景快照。"""

    model_config = ConfigDict(frozen=True)

    actors: tuple[PetColonyActorSample, ...]
    sampled_at_monotonic: float = Field(ge=0.0)
    collision_count: int = Field(ge=0)

    @property
    def visual_key(self) -> tuple[object, ...]:
        """返回包含全部角色次序和画面的稳定 revision key。"""

        return tuple(actor.visual_key for actor in self.actors)


@dataclass(slots=True)
class _ActorMotion:
    """控制器内部的可变轨迹和状态反应锚点。"""

    x_pixels: float
    direction: int
    base_speed: float
    speed_period_primary: float
    speed_period_secondary: float
    speed_phase_primary: float
    speed_phase_secondary: float
    burst_period: float
    burst_phase: float
    animation_phase: float
    trigger_key: tuple[PetActivity, object]
    trigger_started_at: float
    last_sample_at: float
    collision_bounce_until: float = 0.0


@dataclass(frozen=True, slots=True)
class PetColonyRenderActor:
    """把一个场景样本与其已解码图集绑定，供纯渲染函数使用。"""

    asset: CodexPetAsset
    sample: PetColonyActorSample


class PetColonyController:
    """维护任意数量角色的独立全宽轨迹和间歇碰撞。

    速度不是每只宠物固定常量：每个 agent key 派生不同的基础速度、两个低频正弦周期和一个
    轻微短促速度包络，最终速度平滑变化约 ±10%～18%，且不同角色不会同步。
    """

    def __init__(
        self,
        *,
        reduced_motion: bool = False,
        patrol_speed: CodexPetPatrolSpeed = CodexPetPatrolSpeed.MEDIUM,
        started_at_monotonic: float | None = None,
    ) -> None:
        """创建空场景；未注入时间时只读取一次 ``time.monotonic``。"""

        started_at = (
            time.monotonic()
            if started_at_monotonic is None
            else started_at_monotonic
        )
        _validate_monotonic(started_at)
        self._reduced_motion = bool(reduced_motion)
        self._patrol_speed = patrol_speed
        self._started_at = started_at
        self._last_sample_at = started_at
        self._motions: dict[str, _ActorMotion] = {}
        self._collision_cooldowns: dict[tuple[str, str], float] = {}
        self._collision_count = 0

    @property
    def reduced_motion(self) -> bool:
        """返回当前是否禁用位移和逐帧动画。"""

        return self._reduced_motion

    def set_reduced_motion(self, enabled: bool) -> None:
        """更新 reduced-motion 开关，不重置角色位置。"""

        self._reduced_motion = bool(enabled)

    def set_patrol_speed(self, speed: CodexPetPatrolSpeed) -> None:
        """更新全部角色的巡游速度档位，不重置位置或动态速度相位。

        入参：``speed`` 是 slow/medium/fast 枚举。
        返回：无显式返回。
        错误处理：调用方绕过枚举传入非法值时，后续查表会抛 KeyError。
        副作用：只替换控制器内存档位；现有角色从下一次采样平滑采用新倍率。
        """

        self._patrol_speed = speed

    def sample(
        self,
        actors: tuple[CodexAppPetActorSnapshot, ...],
        *,
        monotonic_seconds: float | None = None,
    ) -> PetColonySceneSample:
        """在绝对 monotonic 时刻采样全部当前角色。

        消失角色立即从场景移除；新角色按 agent key 确定初始位置、方向和速度参数。时间倒退
        会抛 ``ValueError``，避免并发调用让轨迹反向跳变。
        """

        now = time.monotonic() if monotonic_seconds is None else monotonic_seconds
        _validate_monotonic(now)
        if now < self._last_sample_at:
            raise ValueError("pet colony monotonic time must not move backwards")

        active_keys = {actor.agent_key for actor in actors}
        self._motions = {
            agent_key: motion
            for agent_key, motion in self._motions.items()
            if agent_key in active_keys
        }
        self._collision_cooldowns = {
            pair: cooldown
            for pair, cooldown in self._collision_cooldowns.items()
            if pair[0] in active_keys and pair[1] in active_keys
        }
        for actor in actors:
            motion = self._motions.get(actor.agent_key)
            if motion is None:
                self._motions[actor.agent_key] = _new_actor_motion(actor, now)
                continue
            if motion.trigger_key != actor.trigger_key:
                motion.trigger_key = actor.trigger_key
                motion.trigger_started_at = now

        if not self._reduced_motion:
            self._advance_running_actors(actors, now)
            self._apply_intermittent_collisions(actors, now)
        else:
            for motion in self._motions.values():
                motion.last_sample_at = now

        samples: list[PetColonyActorSample] = []
        for actor in actors:
            motion = self._motions[actor.agent_key]
            sample = self._sample_actor(actor, motion, now)
            if sample is not None:
                samples.append(sample)
        samples.sort(key=lambda sample: sample.agent_key)
        self._last_sample_at = now
        return PetColonySceneSample(
            actors=tuple(samples),
            sampled_at_monotonic=now,
            collision_count=self._collision_count,
        )

    def _advance_running_actors(
        self,
        actors: tuple[CodexAppPetActorSnapshot, ...],
        now: float,
    ) -> None:
        """按每只角色的平滑动态速度推进全宽反射轨迹。"""

        left_bound = _PET_HALF_WIDTH
        right_bound = PANEL_CANVAS_SIZE[0] - _PET_HALF_WIDTH
        for actor in actors:
            motion = self._motions[actor.agent_key]
            dt = max(0.0, now - motion.last_sample_at)
            if actor.activity == PetActivity.RUNNING and dt > 0:
                midpoint = motion.last_sample_at + dt / 2
                distance = (
                    _dynamic_speed(motion, midpoint - self._started_at)
                    * _SPEED_MULTIPLIER_BY_PROFILE[self._patrol_speed]
                    * dt
                )
                motion.x_pixels, motion.direction = _reflected_position(
                    motion.x_pixels,
                    direction=motion.direction,
                    distance=distance,
                    left_bound=left_bound,
                    right_bound=right_bound,
                )
            motion.last_sample_at = now

    def _apply_intermittent_collisions(
        self,
        actors: tuple[CodexAppPetActorSnapshot, ...],
        now: float,
    ) -> None:
        """仅在周期窗口内让相向 running 角色反弹，其余时间允许互相穿过。"""

        elapsed = now - self._started_at
        phase = elapsed % _COLLISION_PERIOD_SECONDS
        if not _COLLISION_WINDOW_START_SECONDS <= phase < _COLLISION_WINDOW_END_SECONDS:
            return
        running = [
            actor for actor in actors if actor.activity == PetActivity.RUNNING
        ]
        for left_index, first in enumerate(running):
            for second in running[left_index + 1 :]:
                first_motion = self._motions[first.agent_key]
                second_motion = self._motions[second.agent_key]
                delta = second_motion.x_pixels - first_motion.x_pixels
                speed_multiplier = _SPEED_MULTIPLIER_BY_PROFILE[self._patrol_speed]
                first_velocity = (
                    first_motion.direction
                    * _dynamic_speed(first_motion, elapsed)
                    * speed_multiplier
                )
                second_velocity = (
                    second_motion.direction
                    * _dynamic_speed(second_motion, elapsed)
                    * speed_multiplier
                )
                approaching = delta * (second_velocity - first_velocity) < 0
                pair = tuple(sorted((first.agent_key, second.agent_key)))
                if (
                    abs(delta) <= _COLLISION_DISTANCE
                    and approaching
                    and now >= self._collision_cooldowns.get(pair, 0.0)
                ):
                    first_motion.direction *= -1
                    second_motion.direction *= -1
                    first_motion.collision_bounce_until = now + _COLLISION_BOUNCE_SECONDS
                    second_motion.collision_bounce_until = now + _COLLISION_BOUNCE_SECONDS
                    self._collision_cooldowns[pair] = now + _COLLISION_COOLDOWN_SECONDS
                    self._collision_count += 1

    def _sample_actor(
        self,
        actor: CodexAppPetActorSnapshot,
        motion: _ActorMotion,
        now: float,
    ) -> PetColonyActorSample | None:
        """把业务活动和轨迹转换为动作行、帧、位置、速度与地垫色。"""

        trigger_elapsed = max(0.0, now - motion.trigger_started_at)
        action = _action_for_activity(actor.activity, motion.direction)
        if actor.activity == PetActivity.READY:
            waving_duration = animation_duration_seconds("waving") * _COMPLETION_CYCLES
            if trigger_elapsed >= waving_duration + _COMPLETION_HOLD_SECONDS:
                return None
            if trigger_elapsed >= waving_duration:
                frame_index = len(PET_ANIMATION_ROWS["waving"][1]) - 1
            else:
                frame_index = animation_frame_index("waving", trigger_elapsed)
        elif self._reduced_motion:
            frame_index = 0
        else:
            animation_elapsed = (
                now - self._started_at + motion.animation_phase
            )
            frame_index = animation_frame_index(action, animation_elapsed)

        direction = motion.direction if actor.activity == PetActivity.RUNNING else 0
        speed = (
            0.0
            if self._reduced_motion or actor.activity != PetActivity.RUNNING
            else (
                _dynamic_speed(motion, now - self._started_at)
                * _SPEED_MULTIPLIER_BY_PROFILE[self._patrol_speed]
            )
        )
        bounce_remaining = max(0.0, motion.collision_bounce_until - now)
        bounce_progress = (
            bounce_remaining / _COLLISION_BOUNCE_SECONDS
            if bounce_remaining > 0
            else 0.0
        )
        y_offset = -round(4 * math.sin(math.pi * bounce_progress))
        return PetColonyActorSample(
            agent_key=actor.agent_key,
            activity=actor.activity,
            action=action,
            frame_index=frame_index,
            x=(motion.x_pixels - _PET_HALF_WIDTH)
            / (PANEL_CANVAS_SIZE[0] - 2 * _PET_HALF_WIDTH),
            direction=direction,
            speed_pixels_per_second=speed,
            remote_color=(
                remote_host_color(actor.remote_host_key)
                if actor.remote_host_key is not None
                else None
            ),
            y_offset_pixels=y_offset,
        )


def render_pet_colony_panel(
    actors: tuple[PetColonyRenderActor, ...],
    *,
    size: tuple[int, int] = PANEL_CANVAS_SIZE,
) -> Image.Image:
    """把多个角色绘制到同一 800x136 PETS viewport。

    先绘制全部远端光环，再按稳定 agent key 绘制完整 cell，角色重叠时不会因 x 顺序变化闪烁。
    本地角色 ``remote_color=None``，因此没有任何地垫。
    """

    if size != PANEL_CANVAS_SIZE:
        raise ValueError("pet colony renderer currently requires an 800x136 panel")
    canvas = Image.new("RGBA", size, (*PET_BACKGROUND, 255))
    ImageDraw.Draw(canvas).line(
        (8, _BASELINE_Y, size[0] - 8, _BASELINE_Y),
        fill=(*PET_GROUND, 255),
        width=1,
    )
    positioned: list[tuple[PetColonyRenderActor, int]] = []
    travel_width = size[0] - 2 * _PET_HALF_WIDTH
    for actor in actors:
        center_x = round(_PET_HALF_WIDTH + actor.sample.x * travel_width)
        positioned.append((actor, center_x))
        if actor.sample.remote_color is not None:
            mat = _remote_mat(actor.sample.remote_color)
            canvas.alpha_composite(
                mat,
                (center_x - mat.width // 2, _BASELINE_Y - 12),
            )

    for actor, center_x in sorted(positioned, key=lambda item: item[0].sample.agent_key):
        frame = extract_pet_frame(
            actor.asset,
            action=actor.sample.action,
            frame_index=actor.sample.frame_index,
        )
        fitted = frame.resize(
            (_PET_RENDER_WIDTH, _PET_HEIGHT),
            Image.Resampling.NEAREST,
        )
        canvas.alpha_composite(
            fitted,
            (
                center_x - fitted.width // 2,
                _BASELINE_Y - _PET_HEIGHT + actor.sample.y_offset_pixels,
            ),
        )
    return canvas


def remote_host_color(host_key: str) -> tuple[int, int, int]:
    """把稳定远端 host key 映射到低饱和光环色，不读取主机配置。"""

    digest = hashlib.blake2b(host_key.encode("utf-8"), digest_size=8).digest()
    index = int.from_bytes(digest, "big") % len(REMOTE_HOST_COLORS)
    return REMOTE_HOST_COLORS[index]


def _new_actor_motion(
    actor: CodexAppPetActorSnapshot,
    now: float,
) -> _ActorMotion:
    """从 agent key 派生稳定但彼此错开的初始轨迹和速度包络。"""

    digest = hashlib.blake2b(actor.agent_key.encode("utf-8"), digest_size=32).digest()
    usable_width = PANEL_CANVAS_SIZE[0] - 2 * _PET_HALF_WIDTH
    x_fraction = int.from_bytes(digest[0:4], "big") / (2**32 - 1)
    direction = 1 if digest[4] % 2 == 0 else -1
    base_speed = 62.0 + digest[5] / 255 * 34.0
    primary_period = 6.5 + digest[6] / 255 * 6.5
    secondary_period = 13.0 + digest[7] / 255 * 9.0
    burst_period = 8.0 + digest[8] / 255 * 8.0
    return _ActorMotion(
        x_pixels=_PET_HALF_WIDTH + x_fraction * usable_width,
        direction=direction,
        base_speed=base_speed,
        speed_period_primary=primary_period,
        speed_period_secondary=secondary_period,
        speed_phase_primary=digest[9] / 255 * math.tau,
        speed_phase_secondary=digest[10] / 255 * math.tau,
        burst_period=burst_period,
        burst_phase=digest[11] / 255 * math.tau,
        animation_phase=digest[12] / 255 * 1.3,
        trigger_key=actor.trigger_key,
        trigger_started_at=now,
        last_sample_at=now,
    )


def _dynamic_speed(motion: _ActorMotion, elapsed: float) -> float:
    """返回一只角色当前的平滑动态速度幅值。

    两个错相低频波提供持续细微差异，三次幂波只在峰值附近形成轻微短时加减速；总幅度被约束在
    基础速度的 82%～118%，不会抖动或瞬间跳速。
    """

    primary = 0.105 * math.sin(
        math.tau * elapsed / motion.speed_period_primary
        + motion.speed_phase_primary
    )
    secondary = 0.045 * math.sin(
        math.tau * elapsed / motion.speed_period_secondary
        + motion.speed_phase_secondary
    )
    burst_wave = math.sin(
        math.tau * elapsed / motion.burst_period + motion.burst_phase
    )
    burst = 0.03 * burst_wave**3
    multiplier = min(1.18, max(0.82, 1.0 + primary + secondary + burst))
    return motion.base_speed * multiplier


def _reflected_position(
    x_pixels: float,
    *,
    direction: int,
    distance: float,
    left_bound: float,
    right_bound: float,
) -> tuple[float, int]:
    """推进任意距离并在完整共享边界反射，不为角色划分领地。"""

    span = right_bound - left_bound
    if span <= 0:
        raise ValueError("pet colony horizontal bounds must have positive span")
    position = min(right_bound, max(left_bound, x_pixels)) - left_bound
    phase = position if direction >= 0 else 2 * span - position
    phase = (phase + distance) % (2 * span)
    if phase <= span:
        return left_bound + phase, 1
    return right_bound - (phase - span), -1


def _action_for_activity(
    activity: PetActivity,
    direction: int,
) -> PetAnimationName:
    """把角色活动映射为公共图集动作行。"""

    if activity == PetActivity.RUNNING:
        return "running-right" if direction >= 0 else "running-left"
    if activity == PetActivity.NEEDS_INPUT:
        return "waiting"
    if activity == PetActivity.BLOCKED:
        return "failed"
    if activity == PetActivity.REVIEW:
        return "review"
    if activity == PetActivity.READY:
        return "waving"
    return "idle"


@lru_cache(maxsize=len(REMOTE_HOST_COLORS))
def _remote_mat(color: tuple[int, int, int]) -> Image.Image:
    """预渲染一个 128x20 细光环模板，供同 host 的角色复用。"""

    width, height = 128, 20
    halo = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    halo_draw = ImageDraw.Draw(halo)
    halo_draw.ellipse((0, 0, width - 1, 17), outline=(*color, 82), width=3)
    blurred = halo.filter(ImageFilter.GaussianBlur(5))
    draw = ImageDraw.Draw(blurred)
    draw.ellipse((5, 3, width - 6, 15), outline=(*color, 235), width=1)
    draw.arc((11, 5, width - 12, 13), 194, 330, fill=(*color, 175), width=1)
    draw.arc((16, 6, width - 17, 12), 12, 138, fill=(*color, 125), width=1)
    return blurred


def _validate_monotonic(value: float) -> None:
    """拒绝负数、NaN 和无穷 monotonic 时间。"""

    if not isinstance(value, (int, float)) or value < 0:
        raise ValueError("pet colony monotonic time must be non-negative")
    if not math.isfinite(value):
        raise ValueError("pet colony monotonic time must be finite")
