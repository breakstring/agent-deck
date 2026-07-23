"""Codex 宠物场景时钟、空间轨迹、像素边界和 Key 缓存测试。

测试使用内存合成图集与 pytest 临时缓存，验证绝对 monotonic 采样和纯 Pillow 输出；
不读取真实 Codex 配置或宠物、不启动硬件线程，也不连接 N4 Pro。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from agent_deck.adapters.codex_pet import (
    CodexPetAsset,
    CodexPetManifest,
    PetActivity,
    PetActivitySnapshot,
)
from agent_deck.rendering.codex_pet import (
    KEY_CANVAS_SIZE,
    PANEL_CANVAS_SIZE,
    PET_ANIMATION_ROWS,
    PET_BACKGROUND,
    PetSceneController,
    PetSceneSample,
    animation_duration_seconds,
    animation_frame_index,
    pet_key_frame_for_sample,
    pre_render_pet_key_frames,
    render_pet_key_image,
    render_pet_panel_image,
)


@pytest.fixture(scope="module")
def synthetic_asset(tmp_path_factory: pytest.TempPathFactory) -> CodexPetAsset:
    """创建每个使用 cell 都有稳定身份色的合成 v1 资产。

    入参：pytest module 级临时目录工厂。
    返回：无需文件加载器即可供渲染测试裁切的 ``CodexPetAsset``。
    错误处理：Pillow 内存分配失败即测试失败。
    副作用：只创建内存图集和指向临时目录的元数据 Path。
    """

    root = tmp_path_factory.mktemp("pet-render-asset")
    atlas = Image.new("RGBA", (1536, 1872), (0, 0, 0, 0))
    cell_width, cell_height = 192, 208
    for row, (_, durations) in enumerate(PET_ANIMATION_ROWS.values()):
        for column in range(len(durations)):
            color = (
                30 + row * 20,
                40 + column * 15,
                210 - row * 10,
                255,
            )
            atlas.paste(
                color,
                (
                    column * cell_width,
                    row * cell_height,
                    (column + 1) * cell_width,
                    (row + 1) * cell_height,
                ),
            )
    manifest = CodexPetManifest.model_validate(
        {
            "id": "synthetic",
            "displayName": "Synthetic",
            "description": "render fixture",
            "spritesheetPath": "spritesheet.png",
        }
    )
    now = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
    return CodexPetAsset(
        selected_avatar_id="custom:synthetic",
        manifest=manifest,
        package_dir=root,
        manifest_path=root / "pet.json",
        spritesheet_path=root / "spritesheet.png",
        spritesheet=atlas,
        loaded_at=now,
        source_fingerprint="synthetic-fingerprint",
    )


def _activity(
    activity: PetActivity,
    *,
    offset_seconds: int = 0,
    agent_key: str = "codex:top",
) -> PetActivitySnapshot:
    """构造带稳定 trigger key 的活动快照。

    入参：业务活动、状态时间偏移和 agent key。
    返回：timezone-aware ``PetActivitySnapshot``；idle 仍保留测试触发来源。
    错误处理：非法字段由 Pydantic 传播。
    副作用：无。
    """

    base = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
    status_since = base + timedelta(seconds=offset_seconds)
    return PetActivitySnapshot(
        activity=activity,
        status_since=status_since,
        agent_key=agent_key,
        updated_at=status_since,
    )


def test_animation_frame_boundaries_use_cumulative_durations() -> None:
    """验证帧边界按官方累计时长且掉帧直接定位。

    入参：无。
    返回：无；断言 idle 行关键边界与整轮循环。
    错误处理：无。
    副作用：无。
    """

    assert animation_frame_index("idle", 0.0) == 0
    assert animation_frame_index("idle", 0.279999) == 0
    assert animation_frame_index("idle", 0.28) == 1
    assert animation_frame_index("idle", 0.499999) == 2
    assert animation_frame_index("idle", 0.5) == 3
    assert animation_frame_index("idle", animation_duration_seconds("idle")) == 0
    with pytest.raises(ValueError):
        animation_frame_index("idle", -0.001)


def test_reaction_plays_three_cycles_and_same_trigger_does_not_restart() -> None:
    """验证 waiting 恰好三轮后慢 idle，轮询更新时间不会重播。

    入参：无。
    返回：无；断言反应边界和 trigger 去重。
    错误处理：无。
    副作用：仅修改场景控制器内存。
    """

    controller = PetSceneController(started_at_monotonic=0.0)
    waiting = _activity(PetActivity.NEEDS_INPUT)
    first = controller.sample(waiting, monotonic_seconds=0.0)
    assert first.action == "waiting"
    assert first.reaction_active

    three_cycles = animation_duration_seconds("waiting") * 3
    almost_done = controller.sample(
        waiting.model_copy(update={"updated_at": waiting.updated_at + timedelta(seconds=1)}),
        monotonic_seconds=three_cycles - 0.000001,
    )
    assert almost_done.action == "waiting"
    finished = controller.sample(
        waiting.model_copy(update={"updated_at": waiting.updated_at + timedelta(seconds=2)}),
        monotonic_seconds=three_cycles + 0.000001,
    )
    assert finished.action == "idle"
    assert not finished.reaction_active

    polled_again = controller.sample(
        waiting.model_copy(update={"updated_at": waiting.updated_at + timedelta(seconds=3)}),
        monotonic_seconds=three_cycles + 0.2,
    )
    assert polled_again.action == "idle"

    same_timestamp_other_agent = controller.sample(
        waiting.model_copy(
            update={
                "agent_key": "codex:other-top",
                "updated_at": waiting.updated_at + timedelta(seconds=4),
            }
        ),
        monotonic_seconds=three_cycles + 0.25,
    )
    assert same_timestamp_other_agent.action == "idle"
    assert not same_timestamp_other_agent.reaction_active

    retriggered = controller.sample(
        _activity(PetActivity.NEEDS_INPUT, offset_seconds=10),
        monotonic_seconds=three_cycles + 0.3,
    )
    assert retriggered.action == "waiting"
    assert retriggered.frame_index == 0


def test_running_uses_absolute_fifteen_second_round_trip() -> None:
    """验证 Running 15 秒完成一次左右全往返并切换正确方向行。

    入参：无。
    返回：无；断言四分之一周期位置、边界和动作行。
    错误处理：无。
    副作用：仅采样场景控制器。
    """

    controller = PetSceneController(started_at_monotonic=0.0)
    running = _activity(PetActivity.RUNNING)
    samples = [
        controller.sample(running, monotonic_seconds=seconds)
        for seconds in (0.0, 3.75, 7.5, 11.25, 15.0)
    ]

    assert [sample.x for sample in samples] == pytest.approx([0.0, 0.5, 1.0, 0.5, 0.0])
    assert [sample.action for sample in samples] == [
        "running-right",
        "running-right",
        "running-left",
        "running-left",
        "running-right",
    ]


def test_idle_has_thirty_second_rest_and_alternating_fifteen_second_walk() -> None:
    """验证 Idle 的确定性 45 秒周期和下一周期反向散步。

    入参：无。
    返回：无；断言驻留、半程、端点和反向阶段。
    错误处理：无。
    副作用：仅采样场景控制器。
    """

    controller = PetSceneController(started_at_monotonic=0.0)
    idle = _activity(PetActivity.IDLE)
    controller.sample(idle, monotonic_seconds=0.0)
    rest = controller.sample(idle, monotonic_seconds=29.0)
    start_walk = controller.sample(idle, monotonic_seconds=30.0)
    midpoint = controller.sample(idle, monotonic_seconds=37.5)
    other_side = controller.sample(idle, monotonic_seconds=45.0)
    reverse_start = controller.sample(idle, monotonic_seconds=75.0)
    returned = controller.sample(idle, monotonic_seconds=90.0)

    assert rest.action == "idle"
    assert rest.x == pytest.approx(0.0)
    assert start_walk.action == "running-right"
    assert midpoint.x == pytest.approx(0.5)
    assert other_side.action == "idle"
    assert other_side.x == pytest.approx(1.0)
    assert reverse_start.action == "running-left"
    assert returned.x == pytest.approx(0.0)


def test_state_change_preserves_current_x_and_reduced_motion_freezes() -> None:
    """验证状态切换不瞬移，reduced 模式固定 idle 首帧和坐标。

    入参：无。
    返回：无；断言 running 中点转 waiting 后 x 不变及 reduced 行为。
    错误处理：无。
    副作用：仅修改两个场景控制器实例。
    """

    controller = PetSceneController(started_at_monotonic=0.0)
    running = _activity(PetActivity.RUNNING)
    controller.sample(running, monotonic_seconds=0.0)
    midpoint = controller.sample(running, monotonic_seconds=3.75)
    waiting = controller.sample(
        _activity(PetActivity.NEEDS_INPUT, offset_seconds=1),
        monotonic_seconds=3.75,
    )
    assert midpoint.x == pytest.approx(0.5)
    assert waiting.x == pytest.approx(midpoint.x)
    assert waiting.action == "waiting"

    reduced = PetSceneController(
        reduced_motion=True,
        initial_x=0.4,
        started_at_monotonic=0.0,
    )
    reduced_sample = reduced.sample(running, monotonic_seconds=12.0)
    assert reduced_sample.action == "idle"
    assert reduced_sample.frame_index == 0
    assert reduced_sample.x == pytest.approx(0.4)
    assert reduced_sample.direction == "none"


def test_key_uses_non_directional_running_with_own_timing() -> None:
    """验证 Key 不复用方向步态帧号，而按 running 行自身时长采样。

    入参：无。
    返回：无；断言相同累计时间得到非方向动作及正确帧。
    错误处理：无。
    副作用：无。
    """

    sample = PetSceneSample(
        activity=PetActivity.RUNNING,
        action="running-right",
        frame_index=7,
        x=0.5,
        direction="right",
        sampled_at_monotonic=1.0,
        animation_elapsed_seconds=0.5,
    )

    action, frame_index = pet_key_frame_for_sample(sample)

    assert action == "running"
    assert frame_index == animation_frame_index("running", 0.5)
    assert (action, frame_index) == sample.key_frame_key


def test_key_render_is_exact_size_centered_and_preserves_full_cell(
    synthetic_asset: CodexPetAsset,
) -> None:
    """验证 Key 输出严格 112x112、完整 cell 为 89x96 且底线 y=104。

    入参：module 级合成资产。
    返回：无；通过与纯背景差分断言未裁透明坐标空间的固定放置。
    错误处理：Pillow 错误即测试失败。
    副作用：只创建内存图像。
    """

    sample = PetSceneSample(
        activity=PetActivity.IDLE,
        action="idle",
        frame_index=0,
        x=1.0,
        sampled_at_monotonic=0.0,
    )
    image = render_pet_key_image(synthetic_asset, sample)
    background = Image.new("RGB", KEY_CANVAS_SIZE, PET_BACKGROUND)
    bbox = ImageChops.difference(image, background).getbbox()

    assert image.size == (112, 112)
    assert bbox == (11, 8, 100, 104)


def test_panel_render_stays_inside_800x136_and_uses_direction_rows(
    synthetic_asset: CodexPetAsset,
) -> None:
    """验证面板左右端点不越界且 left/right 使用各自图集行。

    入参：module 级合成资产。
    返回：无；断言尺寸、端点 pet 区域和方向行身份色不同。
    错误处理：Pillow 错误即测试失败。
    副作用：只创建内存图像。
    """

    right = PetSceneSample(
        activity=PetActivity.RUNNING,
        action="running-right",
        frame_index=0,
        x=0.0,
        direction="right",
        sampled_at_monotonic=0.0,
    )
    left = right.model_copy(
        update={"action": "running-left", "x": 1.0, "direction": "left"}
    )
    right_image = render_pet_panel_image(synthetic_asset, right)
    left_image = render_pet_panel_image(synthetic_asset, left)

    assert right_image.size == PANEL_CANVAS_SIZE
    assert left_image.size == PANEL_CANVAS_SIZE
    assert right_image.getpixel((8, 30)) != PET_BACKGROUND
    assert right_image.getpixel((97, 30)) == PET_BACKGROUND
    assert right_image.getpixel((400, 124)) == PET_BACKGROUND
    assert left_image.getpixel((702, 30)) == PET_BACKGROUND
    assert left_image.getpixel((703, 30)) != PET_BACKGROUND
    assert left_image.getpixel((791, 30)) != PET_BACKGROUND
    assert right_image.getpixel((20, 30)) != left_image.getpixel((720, 30))


def test_pre_rendered_key_cache_contains_all_public_rows(
    tmp_path: Path,
    synthetic_asset: CodexPetAsset,
) -> None:
    """验证预渲染缓存为每个公共动作生成完整有序 PNG 集合。

    入参：pytest 临时缓存目录与合成资产。
    返回：无；断言动作集合、帧数、存在性和 112x112 尺寸。
    错误处理：PNG 编码/读取错误即测试失败。
    副作用：只在 pytest 临时目录写派生 PNG。
    """

    paths = pre_render_pet_key_frames(synthetic_asset, cache_dir=tmp_path / "cache")

    assert set(paths) == set(PET_ANIMATION_ROWS)
    for action, (_, durations) in PET_ANIMATION_ROWS.items():
        assert len(paths[action]) == len(durations)
        assert all(path.is_file() for path in paths[action])
    with Image.open(paths["idle"][0]) as first:
        assert first.size == (112, 112)
