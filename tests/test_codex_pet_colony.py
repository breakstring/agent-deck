"""验证多任务 PETS 场景的动态速度、全宽轨迹和远端视觉分组。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_deck.adapters.codex_pet import CodexAppPetActorSnapshot, PetActivity
from agent_deck.config import CodexPetPatrolSpeed
from agent_deck.rendering.codex_pet_colony import (
    PetColonyController,
    _remote_mat,
    _reflected_position,
    render_pet_colony_panel,
    remote_host_color,
)
from agent_deck.rendering.codex_pet import PET_BACKGROUND


def _actor(
    key: str,
    *,
    remote_host: str | None = None,
    activity: PetActivity = PetActivity.RUNNING,
) -> CodexAppPetActorSnapshot:
    """构造一个带时区的确定性角色快照。"""

    return CodexAppPetActorSnapshot(
        agent_key=key,
        activity=activity,
        status_since=datetime(2026, 7, 23, tzinfo=UTC),
        is_remote=remote_host is not None,
        remote_host_key=remote_host,
    )


def test_running_pet_speed_varies_smoothly_over_time() -> None:
    """同一角色的速度不会在整段巡游中保持常量。"""

    controller = PetColonyController(started_at_monotonic=100.0)
    actor = _actor("codex:local")

    speeds = [
        controller.sample((actor,), monotonic_seconds=moment).actors[0].speed_pixels_per_second
        for moment in (100.0, 102.0, 104.0, 106.0)
    ]

    assert max(speeds) - min(speeds) > 1.0
    assert all(45.0 < speed < 120.0 for speed in speeds)


def test_speed_profile_changes_base_pace_without_resetting_position() -> None:
    """慢中快三档使用同一轨迹，只改变受控基础倍率。"""

    controller = PetColonyController(
        patrol_speed=CodexPetPatrolSpeed.SLOW,
        started_at_monotonic=10.0,
    )
    actor = _actor("codex:remote")
    slow = controller.sample((actor,), monotonic_seconds=10.0).actors[0]
    controller.set_patrol_speed(CodexPetPatrolSpeed.FAST)
    fast = controller.sample((actor,), monotonic_seconds=10.0).actors[0]

    assert fast.x == slow.x
    assert fast.speed_pixels_per_second == pytest.approx(
        slow.speed_pixels_per_second * (1.3 / 0.72)
    )


def test_remote_host_color_is_stable_and_host_specific() -> None:
    """同一主机始终同色，多主机样本会分布到不止一种颜色。"""

    assert remote_host_color("ssh-a") == remote_host_color("ssh-a")
    colors = {remote_host_color(f"ssh-{index}") for index in range(8)}
    assert len(colors) > 1


def test_pet_colony_has_no_horizon_and_uses_compact_remote_halo() -> None:
    """群体面板不应绘制地平线，远端光圈应只略宽于宠物本体。

    入参：空群体和一个稳定远端颜色。
    返回：无；断言基线像素仍为背景且光圈模板缩为 96x16。
    错误处理：渲染或模板尺寸回归时由 pytest 报告。
    副作用：只创建并缓存内存图片。
    """

    panel = render_pet_colony_panel(())
    halo = _remote_mat(remote_host_color("ssh-a"))

    assert panel.getpixel((400, 124)) == (*PET_BACKGROUND, 255)
    assert halo.size == (96, 16)


def test_reflected_position_preserves_direction_before_boundary() -> None:
    """角色未碰到全宽边界时不得被错误改成向右移动。"""

    x, direction = _reflected_position(
        400.0,
        direction=-1,
        distance=20.0,
        left_bound=50.0,
        right_bound=750.0,
    )

    assert x == pytest.approx(380.0)
    assert direction == -1


def test_completed_actor_disappears_after_waving_and_hold() -> None:
    """完成角色播放三轮 waving 并保持末帧后应从群体场景移除。"""

    controller = PetColonyController(started_at_monotonic=0.0)
    actor = _actor("codex:done", activity=PetActivity.READY)

    assert controller.sample((actor,), monotonic_seconds=0.0).actors
    assert controller.sample((actor,), monotonic_seconds=30.0).actors == ()
