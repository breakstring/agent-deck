"""ChatGPT/Codex App 启动键任务态宠物覆盖层的模型、时序、预算与恢复测试。

本模块只构造内存状态、临时 Path 和 FastAPI 测试 runtime；不读取用户 Codex 配置、
不启动 ChatGPT/Codex App、不连接 N4 Pro，也不访问真实 HID。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_deck.adapters.codex_pet import (
    PetActivity,
    PetActivitySnapshot,
    derive_codex_app_pet_activity,
)
from agent_deck.config import CodexPetMotion
from agent_deck.core.events import AgentSource
from agent_deck.core.modes import DeckMode, DeckSelection
from agent_deck.core.state import AgentState, AgentStatus
from agent_deck.rendering.codex_pet import CodexAppPetOverlayController
from agent_deck.rendering.key_surface import (
    KeySurfaceKind,
    N4ProKeyBinding,
    N4ProKeyLayout,
    default_n4pro_key_layout,
    is_codex_desktop_app_target,
)
from agent_deck.rendering.layout import KeyAmbientOverlaySpec, build_layout_plan
from agent_deck.server.app import DaemonPollerConfig, create_app
from agent_deck.server.codex_pet_runtime import CodexPetRuntime

_BASE_TIME = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)


def _state(
    key: str,
    status: AgentStatus,
    *,
    seconds: int = 0,
    focus_target: str | None = "codex-app:thread-1",
    source: AgentSource = AgentSource.CODEX,
    child: bool = False,
) -> AgentState:
    """构造 App 活动聚合测试所需的最小 AgentState。

    入参：稳定 key、状态、相对时间、focus target、来源与 child 标志。
    返回：合法且时间带 UTC 的 frozen AgentState。
    错误处理：字段不满足核心模型时由 Pydantic 抛出。
    副作用：无；只创建内存模型。
    """

    observed_at = _BASE_TIME + timedelta(seconds=seconds)
    return AgentState(
        agent_key=key,
        source=source,
        display_name=key,
        status=status,
        status_since=observed_at,
        last_event_at=observed_at,
        focus_target=focus_target,
        parent_agent_key="codex:parent" if child else None,
        is_child_agent=child,
    )


def _chatgpt_binding(index: int, *, overlay: bool = True) -> N4ProKeyBinding:
    """构造当前 ChatGPT.app 身份的启动键配置。

    入参：0-based 键位和是否启用任务态宠物。
    返回：保留 open/focus App 参数的 binding。
    错误处理：模型合同变化时由 Pydantic 抛出。
    副作用：无。
    """

    return N4ProKeyBinding(
        index=index,
        kind=KeySurfaceKind.APP,
        label="ChatGPT",
        app_name="ChatGPT",
        app_path="/Applications/ChatGPT.app",
        bundle_id="com.openai.chat",
        icon_token="CG",
        ambient_overlay=KeyAmbientOverlaySpec() if overlay else None,
    )


def _layout_with_chatgpt_keys(count: int) -> N4ProKeyLayout:
    """把默认布局前若干键替换为关联宠物的 ChatGPT 启动键。

    入参：``count`` 必须是 1..10。
    返回：完整覆盖 0..9 的 N4 Pro layout。
    错误处理：越界 count 抛 ValueError。
    副作用：无。
    """

    if not 1 <= count <= 10:
        raise ValueError("chatgpt key count must be between 1 and 10")
    keys = list(default_n4pro_key_layout().sorted_keys())
    for index in range(count):
        keys[index] = _chatgpt_binding(index)
    return N4ProKeyLayout(keys=tuple(keys))


def _seed_key_frames(runtime: CodexPetRuntime, root: Path) -> None:
    """给测试 runtime 注入无需实际解码的预渲染动作 Path。

    入参：目标 runtime 与 pytest 临时目录。
    返回：无显式返回。
    错误处理：仅测试私有缓存赋值，不产生 I/O 错误。
    副作用：替换 runtime 的内存帧表；不会创建文件。
    """

    runtime._key_frames = {  # noqa: SLF001 - 定向验证共享调度器，不重复构造大图集。
        action: tuple(root / f"{action}-{index}.png" for index in range(frame_count))
        for action, frame_count in {
            "running": 6,
            "waiting": 6,
            "failed": 8,
            "review": 6,
            "waving": 4,
        }.items()
    }


def test_chatgpt_and_legacy_codex_targets_are_recognized_without_name_only_match() -> None:
    """当前/历史 OpenAI App 身份应识别，普通同名或 Finder 不应误绑定。

    入参：无；直接调用纯 target 识别函数。
    返回：无；断言通过代表 UI/后端可共享的身份边界明确。
    错误处理：无。
    副作用：无。
    """

    assert is_codex_desktop_app_target(
        app_name="ChatGPT",
        app_path="/Applications/ChatGPT.app",
        bundle_id="com.openai.chat",
    )
    assert is_codex_desktop_app_target(
        app_name="Codex",
        app_path="/Applications/Codex.app",
        bundle_id="com.openai.codex",
    )
    assert not is_codex_desktop_app_target(
        app_name="ChatGPT",
        app_path="/Applications/Unrelated.app",
        bundle_id="example.chatgpt",
    )
    assert not is_codex_desktop_app_target(
        app_name="Finder",
        app_path="/System/Library/CoreServices/Finder.app",
        bundle_id="com.apple.finder",
    )


def test_overlay_round_trip_preserves_app_action_and_backend_rejects_other_apps() -> None:
    """覆盖层应随 App binding/plan 往返，但不能改变动作或绑定普通 App。

    入参：无；构造 ChatGPT 与 Finder binding。
    返回：无；断言 open/focus payload 和原图标字段仍在，非法绑定被拒绝。
    错误处理：Finder 覆盖层必须产生 ValidationError。
    副作用：无。
    """

    layout = _layout_with_chatgpt_keys(1)
    restored = N4ProKeyLayout.model_validate(layout.model_dump(mode="json"))
    plan = build_layout_plan(
        [],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
        key_layout=restored,
    )

    assert restored.sorted_keys()[0].ambient_overlay == KeyAmbientOverlaySpec()
    assert plan.keys[0].intent == "open_or_focus_app"
    assert plan.keys[0].action == "open_or_focus_app"
    assert plan.keys[0].payload["app_name"] == "ChatGPT"
    assert plan.keys[0].payload["icon_token"] == "CG"
    assert plan.keys[0].ambient_overlay == KeyAmbientOverlaySpec()

    with pytest.raises(ValidationError, match="recognized ChatGPT or Codex"):
        N4ProKeyBinding(
            index=0,
            kind=KeySurfaceKind.APP,
            app_name="Finder",
            app_path="/System/Library/CoreServices/Finder.app",
            bundle_id="com.apple.finder",
            ambient_overlay=KeyAmbientOverlaySpec(),
        )


def test_app_activity_filters_cli_children_and_prioritizes_running_over_completed() -> None:
    """App 聚合只消费 Desktop 顶层 task，并按产品优先级选择最新同级状态。

    入参：无；混合 App、CLI、child、其他来源与多个状态。
    返回：无；断言 waiting 胜出，移除后 running 胜 completed。
    错误处理：无。
    副作用：无。
    """

    completed = _state("codex:completed", AgentStatus.COMPLETED_RECENTLY, seconds=8)
    running = _state("codex:running", AgentStatus.RUNNING_TOOL, seconds=2)
    waiting = _state("codex:waiting", AgentStatus.WAITING_USER, seconds=1)
    ignored = (
        _state("codex:cli", AgentStatus.WAITING_USER, focus_target="terminal:cli"),
        _state("codex:child", AgentStatus.WAITING_USER, child=True),
        _state(
            "claude:app",
            AgentStatus.WAITING_USER,
            source=AgentSource.CLAUDE_CODE,
        ),
        _state("codex:idle", AgentStatus.IDLE),
    )

    winner = derive_codex_app_pet_activity(
        (completed, running, waiting, *ignored),
        updated_at=_BASE_TIME,
    )
    without_attention = derive_codex_app_pet_activity(
        (completed, running, *ignored),
        updated_at=_BASE_TIME,
    )

    assert winner.activity == PetActivity.NEEDS_INPUT
    assert winner.agent_key == waiting.agent_key
    assert without_attention.activity == PetActivity.RUNNING
    assert without_attention.agent_key == running.agent_key


def test_overlay_controller_persists_attention_and_restores_after_completion_window() -> None:
    """waiting/error 应持续动画，完成反馈应三轮挥手加五秒后消失且可被运行态打断。

    入参：无；使用固定 monotonic 时钟采样控制器。
    返回：无；断言动作、持久性、末帧停留、恢复与打断规则。
    错误处理：无。
    副作用：只更新控制器内存锚点。
    """

    controller = CodexAppPetOverlayController(started_at_monotonic=0.0)
    waiting = PetActivitySnapshot(
        activity=PetActivity.NEEDS_INPUT,
        status_since=_BASE_TIME,
        updated_at=_BASE_TIME,
    )
    failed = waiting.model_copy(
        update={"activity": PetActivity.BLOCKED, "status_since": _BASE_TIME + timedelta(seconds=1)}
    )
    completed = waiting.model_copy(
        update={"activity": PetActivity.READY, "status_since": _BASE_TIME + timedelta(seconds=2)}
    )
    running = waiting.model_copy(
        update={"activity": PetActivity.RUNNING, "status_since": _BASE_TIME + timedelta(seconds=3)}
    )
    review = waiting.model_copy(
        update={"activity": PetActivity.REVIEW, "status_since": _BASE_TIME + timedelta(seconds=4)}
    )

    assert controller.sample(waiting, monotonic_seconds=0.0).action == "waiting"
    long_wait = controller.sample(waiting, monotonic_seconds=120.0)
    assert long_wait.visible and long_wait.animated and long_wait.action == "waiting"
    long_error = controller.sample(failed, monotonic_seconds=121.0)
    assert long_error.visible and long_error.animated and long_error.action == "failed"
    first_completed = controller.sample(completed, monotonic_seconds=122.0)
    holding = controller.sample(completed, monotonic_seconds=124.2)
    restored = controller.sample(completed, monotonic_seconds=129.2)
    interrupted = controller.sample(running, monotonic_seconds=129.3)
    persistent_review = controller.sample(review, monotonic_seconds=300.0)
    later_review = controller.sample(review, monotonic_seconds=600.0)

    assert first_completed.action == "waving" and first_completed.animated
    assert holding.visible and not holding.animated and holding.frame_index == 3
    assert not restored.visible
    assert interrupted.visible and interrupted.action == "running"
    assert persistent_review.visible and persistent_review.action == "review"
    assert later_review.visible and later_review.animated


def test_reduced_motion_uses_static_state_frames_with_same_completion_timeout() -> None:
    """reduced motion 应固定代表帧，但不改变完成反馈的可见窗口。

    入参：无；固定时钟采样 reduced controller。
    返回：无；断言 running 静态、完成末帧静态并在 7.1 秒后消失。
    错误处理：无。
    副作用：只更新控制器内存状态。
    """

    controller = CodexAppPetOverlayController(
        reduced_motion=True,
        started_at_monotonic=0.0,
    )
    running = PetActivitySnapshot(
        activity=PetActivity.RUNNING,
        status_since=_BASE_TIME,
        updated_at=_BASE_TIME,
    )
    completed = running.model_copy(
        update={"activity": PetActivity.READY, "status_since": _BASE_TIME + timedelta(seconds=1)}
    )

    static_running = controller.sample(running, monotonic_seconds=0.0)
    static_completed = controller.sample(completed, monotonic_seconds=1.0)
    expired = controller.sample(completed, monotonic_seconds=8.2)

    assert static_running.visible and not static_running.animated
    assert static_running.action == "running" and static_running.frame_index == 0
    assert static_completed.visible and not static_completed.animated
    assert static_completed.action == "waving" and static_completed.frame_index == 3
    assert not expired.visible


def test_multi_key_budget_limits_animation_and_recent_press_changes_priority(
    tmp_path: Path,
) -> None:
    """1/2/3 个关联键应得到 10/5/5 FPS，第三个静态，最近按下可抢占动态槽。

    入参：pytest 临时目录只承载虚拟帧 Path。
    返回：无；断言计数、有效 FPS、静态降级与最近按下排序。
    错误处理：无。
    副作用：只更新 runtime 内存帧缓存与调度诊断。
    """

    runtime = CodexPetRuntime(
        enabled=True,
        panel_fps=8,
        motion=CodexPetMotion.FULL,
        cache_root=tmp_path,
        fallback_key_path=None,
        started_at_monotonic=0.0,
    )
    _seed_key_frames(runtime, tmp_path)
    runtime.update_activity([_state("codex:running", AgentStatus.RUNNING_TOOL)])

    runtime.app_overlay_key_sources({1}, monotonic_seconds=0.0)
    assert runtime.diagnostics()["app_overlay"]["effective_fps"] == 10.0
    runtime.app_overlay_key_sources({1, 2}, monotonic_seconds=0.1)
    assert runtime.diagnostics()["app_overlay"]["effective_fps"] == 5.0
    before_press = runtime.app_overlay_key_sources({1, 2, 3}, monotonic_seconds=0.3)
    before = runtime.diagnostics()["app_overlay"]
    runtime.record_app_overlay_key_press(3, monotonic_seconds=0.31)
    after_press = runtime.app_overlay_key_sources({1, 2, 3}, monotonic_seconds=0.5)
    after = runtime.diagnostics()["app_overlay"]

    assert before["animated_key_count"] == 2
    assert before["static_fallback_key_count"] == 1
    assert before["write_budget_per_second"] == 10
    assert before_press[1] == before_press[2]
    assert before_press[3].name == "running-0.png"
    assert after["animated_key_count"] == 2
    assert after_press[3] != after_press[2]
    assert after_press[2].name == "running-0.png"


def test_multi_key_sources_never_exceed_ten_changed_paths_per_second(
    tmp_path: Path,
) -> None:
    """三个关联键在 10 Hz 采样下每秒最多产生十次动态 Path 变化。

    入参：pytest 临时目录只承载虚拟帧 Path。
    返回：无；断言两个动态键受 5 FPS 绝对时间 bucket 约束，第三个静态键始终不变。
    错误处理：无。
    副作用：只推进测试 runtime 的 monotonic 控制器和帧缓存。
    """

    runtime = CodexPetRuntime(
        enabled=True,
        panel_fps=8,
        motion=CodexPetMotion.FULL,
        cache_root=tmp_path,
        fallback_key_path=None,
        started_at_monotonic=0.0,
    )
    _seed_key_frames(runtime, tmp_path)
    runtime.update_activity([_state("codex:running", AgentStatus.RUNNING_TOOL)])
    previous = runtime.app_overlay_key_sources({1, 2, 3}, monotonic_seconds=0.0)
    changed_paths = 0
    static_sources = [previous[3]]
    for tick in range(1, 10):
        current = runtime.app_overlay_key_sources(
            {1, 2, 3},
            monotonic_seconds=tick / 10,
        )
        changed_paths += sum(
            current[index] != previous[index]
            for index in current
        )
        static_sources.append(current[3])
        previous = current

    assert changed_paths <= 10
    assert len(set(static_sources)) == 1


def test_reduced_motion_and_disabled_runtime_never_allocate_dynamic_keys(
    tmp_path: Path,
) -> None:
    """reduced motion 全部静态；全局关闭即使有帧缓存也必须返回原图标路径。

    入参：pytest 临时目录只承载虚拟 Path。
    返回：无；断言动态数为零、静态数为三且 disabled 返回空覆盖。
    错误处理：无。
    副作用：只更新两个测试 runtime 的内存诊断。
    """

    reduced = CodexPetRuntime(
        enabled=True,
        panel_fps=8,
        motion=CodexPetMotion.REDUCED,
        cache_root=tmp_path,
        fallback_key_path=None,
        started_at_monotonic=0.0,
    )
    disabled = CodexPetRuntime(
        enabled=False,
        panel_fps=8,
        motion=CodexPetMotion.FULL,
        cache_root=tmp_path,
        fallback_key_path=None,
        started_at_monotonic=0.0,
    )
    for runtime in (reduced, disabled):
        _seed_key_frames(runtime, tmp_path)
        runtime.update_activity([_state("codex:running", AgentStatus.RUNNING_TOOL)])

    reduced_sources = reduced.app_overlay_key_sources({1, 2, 3}, monotonic_seconds=0.0)
    disabled_sources = disabled.app_overlay_key_sources({1, 2, 3}, monotonic_seconds=0.0)

    reduced_diagnostics = reduced.diagnostics()["app_overlay"]
    assert len(reduced_sources) == 3
    assert reduced_diagnostics["animated_key_count"] == 0
    assert reduced_diagnostics["static_fallback_key_count"] == 3
    assert disabled_sources == {}


def test_daemon_overlay_restores_exact_cached_app_icon_object(tmp_path: Path) -> None:
    """任务覆盖退出时应恢复同一个基础 App 图对象，不清空或重绘 fallback。

    入参：pytest 临时目录承载 daemon 缓存和虚拟宠物帧 Path。
    返回：无；断言 running 使用 Path，idle 后恢复原 Pillow 对象且 revision 只含脏键。
    错误处理：FastAPI 或模型异常由测试失败暴露。
    副作用：读取当前 UTC 时间并只修改 TestClient runtime 内存，不启动 App 或硬件。
    """

    observed_at = datetime.now(UTC)
    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_pet_enabled=True,
            codex_pet_motion=CodexPetMotion.FULL,
            poll_on_start=False,
        ),
        app_icon_cache_path=tmp_path / "app-icon-cache",
        codex_pet_cache_path=tmp_path / "pet-cache",
    )
    with TestClient(app) as client:
        runtime = app.state.runtime
        _seed_key_frames(runtime.codex_pet, tmp_path)
        response = client.put(
            "/ui/key-layout",
            json=_layout_with_chatgpt_keys(1).model_dump(mode="json"),
        )
        assert response.status_code == 200
        runtime.store.upsert_observed_state(
            source=AgentSource.CODEX,
            session_id="running",
            observed_at=observed_at,
            status=AgentStatus.RUNNING_TOOL,
            focus_target="codex-app:thread-1",
        )
        runtime.publish_hardware_key_surface_images(runtime.render_current())
        running_revision, running_images = runtime.current_hardware_key_surface_images()
        base_icon = runtime.hardware_key_surface_base_images[1]

        runtime.store.upsert_observed_state(
            source=AgentSource.CODEX,
            session_id="running",
            observed_at=observed_at + timedelta(seconds=1),
            status=AgentStatus.OFFLINE,
            focus_target="codex-app:thread-1",
        )
        runtime.render_current()
        restored_revision, restored_images = runtime.current_hardware_key_surface_images()

    assert isinstance(running_images[1], Path)
    assert restored_images[1] is base_icon
    assert restored_revision == running_revision + 1
    assert runtime.hardware_key_surface_pending_images == {1: base_icon}


def test_web_editor_exposes_overlay_only_for_recognized_app_and_serializes_spec() -> None:
    """Web 配置页应显式识别 ChatGPT/Codex，展示开关并保留完整 overlay spec。

    入参：无；只读打包 app.js。
    返回：无；断言身份 helper、开关文案、双向字段和切换 App 清理逻辑存在。
    错误处理：文件缺失或关键契约被删除时由 pytest 报告。
    副作用：只读取仓库静态资源。
    """

    app_js = (
        Path(__file__).parents[1] / "src" / "agent_deck" / "web" / "app.js"
    ).read_text(encoding="utf-8")

    assert "function isCodexDesktopAppTarget(app)" in app_js
    assert '"com.openai.chat"' in app_js
    assert '"com.openai.codex"' in app_js
    assert "任务活跃时显示宠物" in app_js
    assert "binding.ambient_overlay?.kind" in app_js
    assert 'kind: "codex_pet", scope: "launch_target", visibility: "task_active"' in app_js
    assert "key.ambientOverlayEnabled = isCodexDesktopAppTarget(app)" in app_js
