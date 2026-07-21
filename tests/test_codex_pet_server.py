"""Codex 宠物与 daemon、logical panel 和硬件 provider 的集成测试。

本模块只在 pytest 临时目录创建合成 v1 图集和配置，通过 FastAPI TestClient 驱动内存
runtime；不会读取真实 ``~/.codex``、复制 Rick、连接 N4 Pro 或启动真实 HID 会话。
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

from fastapi.testclient import TestClient
from PIL import Image

from agent_deck.adapters.codex_pet import CodexPetResolver
from agent_deck.config import CodexPetMotion
from agent_deck.core.events import AgentSource, EventType, NormalizedEvent
from agent_deck.rendering.key_surface import (
    KeySurfaceKind,
    N4ProKeyBinding,
    N4ProKeyLayout,
    default_n4pro_key_layout,
)
from agent_deck.server.app import DaemonPollerConfig, DecisionRequestBody, create_app


def test_pets_enters_manual_rotation_only_when_enabled() -> None:
    """Pets 应只在配置启用时加入 Brand、Quota、Tokens 后的人工轮换。

    入参：无；分别创建关闭和启用宠物的纯内存 app。
    返回：无；断言通过代表 touch tap 顺序及 disabled 移除合同稳定。
    错误处理：HTTP 或顺序不符合预期时由 pytest 报告。
    副作用：只修改两个 TestClient 的内存 selection。
    """

    disabled = TestClient(create_app())
    disabled_kinds = [
        disabled.post("/logical-panel/input", json={"event": "touch.tap"}).json()[
            "selection"
        ]["active_kind"]
        for _ in range(3)
    ]
    assert disabled_kinds == ["quota", "tokens", "brand"]

    enabled_app = create_app(
        poller_config=DaemonPollerConfig(
            codex_pet_enabled=True,
            codex_pet_motion=CodexPetMotion.FULL,
            poll_on_start=False,
        )
    )
    with TestClient(enabled_app) as enabled:
        enabled_kinds = [
            enabled.post(
                "/logical-panel/input", json={"event": "touch.tap"}
            ).json()["selection"]["active_kind"]
            for _ in range(4)
        ]
        status = enabled.get("/status").json()

    assert enabled_kinds == ["quota", "tokens", "pets", "brand"]
    assert status["codex_pet"]["enabled"] is True


def test_decision_message_is_transient_and_restores_pets_selection() -> None:
    """审批 MESSAGE 应覆盖显示但不改写人工选择的 Pets。

    入参：无；测试先切到 Pets，再创建并 resolve 一个审批。
    返回：无；断言通过代表 pending 期间 effective=message，结束后自然恢复 Pets。
    错误处理：selection 被改写、覆盖来源错误或 resolve 失败时由 pytest 报告。
    副作用：只修改 TestClient 内存 store/broker/panel。
    """

    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_pet_enabled=True,
            codex_pet_motion=CodexPetMotion.FULL,
            poll_on_start=False,
        )
    )
    with TestClient(app) as client:
        for _ in range(3):
            client.post("/logical-panel/input", json={"event": "touch.tap"})
        client.post("/events", json=_session_started_event().model_dump(mode="json"))
        decision = client.post(
            "/decisions/request",
            json={
                "agent_key": "codex:pet-session",
                "session_id": "pet-session",
                "tool_name": "shell",
                "reason": "pet transient message",
                "timeout_seconds": 30,
            },
        ).json()
        pending = client.get("/status").json()
        client.post(
            f"/decisions/{decision['decision_id']}/resolve",
            json={"behavior": "deny", "message": "done"},
        )
        restored = client.get("/status").json()

    assert pending["logical_panel"]["selection"]["active_kind"] == "pets"
    assert pending["logical_panel"]["effective_kind"] == "message"
    assert pending["logical_panel"]["touchscreen_image_source"] == "decision_message"
    assert restored["logical_panel"]["selection"]["active_kind"] == "pets"
    assert restored["logical_panel"]["effective_kind"] == "pets"
    assert restored["logical_panel"]["touchscreen_image_source"] == (
        "codex_pet_diagnostic"
    )


def test_pending_message_cannot_be_overwritten_by_stale_pet_provider(
    monkeypatch: object,
) -> None:
    """并发生成中的 PETS 帧不得在审批出现后覆盖最高优先级 MESSAGE。

    入参：pytest monkeypatch；用 Event 固定“宠物 provider 已开始、审批随后出现”的交错。
    返回：无；断言 provider 返回后及下一 tick 都保持 decision_message。
    错误处理：线程未进入/退出会由有界 Event/join 断言失败，不无限等待。
    副作用：只修改测试 app 内存方法并启动一个短生命周期线程，不访问真实硬件。
    """

    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_pet_enabled=True,
            codex_pet_motion=CodexPetMotion.FULL,
            poll_on_start=False,
        )
    )
    entered = Event()
    release = Event()
    provider_results: list[tuple[int, object | None]] = []

    with TestClient(app) as client:
        for _ in range(3):
            client.post("/logical-panel/input", json={"event": "touch.tap"})
        runtime = app.state.runtime
        pet_image = Image.new("RGB", (800, 480), (11, 15, 22))

        def blocking_pet_background() -> tuple[int, Image.Image]:
            """在返回宠物帧前等待测试显式放行。

            入参：无。
            返回：固定 revision 与合成 800x480 宠物背景。
            错误处理：一秒内未放行则断言失败并结束线程。
            副作用：设置/等待线程 Event。
            """

            entered.set()
            assert release.wait(1.0)
            return 1, pet_image

        monkeypatch.setattr(
            runtime.codex_pet,
            "panel_background",
            blocking_pet_background,
        )
        provider_thread = Thread(
            target=lambda: provider_results.append(
                runtime.current_hardware_background()
            )
        )
        provider_thread.start()
        assert entered.wait(1.0)

        runtime.create_decision(
            DecisionRequestBody(
                agent_key="codex:pet-race",
                session_id="pet-race",
                tool_name="shell",
                reason="message must win",
                timeout_seconds=30,
            )
        )
        assert runtime.surface.last_touchscreen_image_source == "decision_message"

        release.set()
        provider_thread.join(timeout=1.0)
        assert not provider_thread.is_alive()
        assert provider_results
        assert runtime.surface.last_touchscreen_image_source == "decision_message"
        runtime.current_hardware_background()
        assert runtime.surface.last_touchscreen_image_source == "decision_message"


def test_loaded_pet_drives_status_dynamic_key_and_pets_background(
    tmp_path: Path,
) -> None:
    """已加载自定义宠物应同时驱动 status、预渲染 Key Path 与动态 PETS 背景 revision。

    入参：``tmp_path`` 承载合成 Codex home、帧 fallback 和 daemon 缓存。
    返回：无；断言通过代表启动解析、Key 热切帧和 panel 热更新共享同一 runtime。
    错误处理：解析、尺寸、revision 或 provider 类型不符合合同由 pytest 报告。
    副作用：只写 pytest 临时 PNG/JSON，并做两次 140ms 有界等待以跨过动画帧边界。
    """

    codex_home = _write_v1_pet_package(tmp_path)
    frame_root = tmp_path / "codex-key-frames"
    frame_root.mkdir()
    Image.new("RGB", (112, 112), (7, 9, 12)).save(frame_root / "offline.png")
    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_pet_enabled=True,
            codex_pet_motion=CodexPetMotion.FULL,
            codex_pet_refresh_interval_seconds=60,
            streamdock_n4pro_frame_root=frame_root,
        ),
        codex_pet_resolver=CodexPetResolver(
            environment={"CODEX_HOME": str(codex_home)}
        ),
        codex_pet_cache_path=tmp_path / "pet-cache",
    )
    with TestClient(app) as client:
        layout = _layout_with_pet_key()
        response = client.put(
            "/ui/key-layout",
            json=layout.model_dump(mode="json"),
        )
        assert response.status_code == 200
        client.post("/events", json=_tool_started_event().model_dump(mode="json"))
        runtime = app.state.runtime
        first_key_revision, first_keys = runtime.current_hardware_key_surface_images()
        first_key_path = first_keys[1]
        for _ in range(3):
            client.post("/logical-panel/input", json={"event": "touch.tap"})
        first_background_revision, first_background = runtime.current_hardware_background()

        time.sleep(0.14)
        second_key_revision, second_keys = runtime.current_hardware_key_surface_images()
        time.sleep(0.14)
        second_background_revision, second_background = (
            runtime.current_hardware_background()
        )
        status = client.get("/status").json()

    assert isinstance(first_key_path, Path)
    assert first_key_path.is_file()
    assert Image.open(first_key_path).size == (112, 112)
    assert second_key_revision > first_key_revision
    assert second_keys[1] != first_key_path
    assert first_background is not None and first_background.size == (800, 480)
    assert second_background is not None and second_background.size == (800, 480)
    assert second_background_revision > first_background_revision
    assert status["codex_pet"]["selected_avatar_id"] == "custom:test-pet"
    assert status["codex_pet"]["resolution_status"] == "loaded"
    assert status["codex_pet"]["display_name"] == "Test Pet"
    assert status["codex_pet"]["sprite_version"] == 1
    assert status["codex_pet"]["activity"] == "running"
    assert status["codex_pet"]["last_error"] is None


def test_removing_pet_key_publishes_a_hardware_clear_revision(tmp_path: Path) -> None:
    """宠物键改回未分配时应发布删除 revision，令同一硬件会话清掉旧帧。

    入参：``tmp_path`` 承载合成 Codex home、静态 fallback 和 daemon 缓存。
    返回：无；断言完整映射删除 Key 1、revision 递增且 pending 差异显式记录删除。
    错误处理：旧宠物帧只能从映射移除却没有硬件 revision 时由 pytest 报告。
    副作用：只写 pytest 临时素材并更新 TestClient 内存布局，不访问真实硬件。
    """

    codex_home = _write_v1_pet_package(tmp_path)
    frame_root = tmp_path / "codex-key-frames"
    frame_root.mkdir()
    Image.new("RGB", (112, 112), (7, 9, 12)).save(frame_root / "offline.png")
    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_pet_enabled=True,
            codex_pet_motion=CodexPetMotion.FULL,
            streamdock_n4pro_frame_root=frame_root,
        ),
        codex_pet_resolver=CodexPetResolver(
            environment={"CODEX_HOME": str(codex_home)}
        ),
        codex_pet_cache_path=tmp_path / "pet-cache",
    )

    with TestClient(app) as client:
        assert client.put(
            "/ui/key-layout",
            json=_layout_with_pet_key().model_dump(mode="json"),
        ).status_code == 200
        runtime = app.state.runtime
        first_revision, first_images = runtime.current_hardware_key_surface_images()
        assert 1 in first_images

        assert client.put(
            "/ui/key-layout",
            json=default_n4pro_key_layout().model_dump(mode="json"),
        ).status_code == 200
        removed_revision, removed_images = runtime.current_hardware_key_surface_images()

    assert removed_revision == first_revision + 1
    assert 1 not in removed_images
    assert runtime.hardware_key_surface_pending_images == {1: None}


def test_pet_load_error_is_exposed_without_image_payload(tmp_path: Path) -> None:
    """自定义包缺失时 `/status.codex_pet` 应给短错误且不泄露图像字段。

    入参：``tmp_path`` 承载只有选择配置、没有宠物包的 Codex home。
    返回：无；断言通过代表降级诊断稳定。
    错误处理：状态被误报 loaded 或出现原图字段时由 pytest 报告。
    副作用：只在 pytest 临时目录写一个 TOML。
    """

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[desktop]\nselected-avatar-id = "custom:missing"\n',
        encoding="utf-8",
    )
    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_pet_enabled=True,
            codex_pet_motion=CodexPetMotion.FULL,
        ),
        codex_pet_resolver=CodexPetResolver(
            environment={"CODEX_HOME": str(codex_home)}
        ),
        codex_pet_cache_path=tmp_path / "cache",
    )
    with TestClient(app) as client:
        pet = client.get("/status").json()["codex_pet"]

    assert pet["selected_avatar_id"] == "custom:missing"
    assert pet["resolution_status"] == "invalid"
    assert pet["last_error"]
    assert "spritesheet" not in pet
    assert "image" not in pet


def _write_v1_pet_package(tmp_path: Path) -> Path:
    """在 pytest 临时目录创建有明显逐帧差异的最小 v1 宠物包。

    入参：``tmp_path`` 是隔离目录。
    返回：包含 config、manifest 与合成 atlas 的 Codex home。
    错误处理：Pillow/文件写入失败按原异常传播。
    副作用：写临时 TOML、JSON 和 PNG，不触碰真实 Codex 目录。
    """

    codex_home = tmp_path / "codex-home"
    pet_dir = codex_home / "pets" / "test-pet"
    pet_dir.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        '[desktop]\nselected-avatar-id = "custom:test-pet"\n',
        encoding="utf-8",
    )
    (pet_dir / "pet.json").write_text(
        json.dumps(
            {
                "id": "test-pet",
                "displayName": "Test Pet",
                "description": "synthetic test pet",
                "spritesheetPath": "spritesheet.png",
                "spriteVersionNumber": 1,
            }
        ),
        encoding="utf-8",
    )
    atlas = Image.new("RGBA", (1536, 1872), (0, 0, 0, 0))
    for row in range(9):
        for column in range(8):
            color = ((row * 29) % 255, (column * 31) % 255, 180, 255)
            left = column * 192 + 60
            top = row * 208 + 70
            atlas.paste(color, (left, top, left + 72, top + 92))
    atlas.save(pet_dir / "spritesheet.png", format="PNG")
    return codex_home


def _layout_with_pet_key() -> N4ProKeyLayout:
    """返回 Key 1 为宠物、其余沿用默认布局的完整 N4 Pro 配置。

    入参：无。
    返回：可经 API round-trip 的 10 键布局。
    错误处理：索引不完整时由 Pydantic 抛出。
    副作用：只创建内存模型。
    """

    defaults = default_n4pro_key_layout().sorted_keys()
    return N4ProKeyLayout(
        keys=(
            N4ProKeyBinding(index=0, kind=KeySurfaceKind.CODEX_PET),
            *defaults[1:],
        )
    )


def _session_started_event() -> NormalizedEvent:
    """构造固定顶层 Codex session.started 事件。

    入参：无。
    返回：timezone-aware normalized event。
    错误处理：模型字段非法时由 Pydantic 抛出。
    副作用：无。
    """

    occurred_at = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    return NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type="session.started",
        normalized_type=EventType.SESSION_STARTED,
        session_id="pet-session",
        thread_id="pet-session",
        occurred_at=occurred_at,
        received_at=occurred_at,
    )


def _tool_started_event() -> NormalizedEvent:
    """构造触发全局 Running 宠物活动的顶层工具事件。

    入参：无。
    返回：timezone-aware normalized event。
    错误处理：模型字段非法时由 Pydantic 抛出。
    副作用：无。
    """

    occurred_at = datetime(2026, 7, 21, 12, 1, tzinfo=UTC)
    return NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type="tool.started",
        normalized_type=EventType.TOOL_STARTED,
        session_id="pet-running",
        thread_id="pet-running",
        tool_name="shell",
        occurred_at=occurred_at,
        received_at=occurred_at,
    )
