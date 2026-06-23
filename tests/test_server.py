"""Tests for the local Agent Deck daemon HTTP API.

These tests define Task 7's in-process FastAPI contract only. They do not open
real sockets, probe StreamDock hardware, install hooks, read user files, or
persist configuration; their side effects are limited to TestClient requests,
local asyncio scheduling inside FastAPI, and pytest assertion reporting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import agent_deck.server.app as server_app
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from PIL import Image

from agent_deck.adapters.codex_app_state import CodexAppActiveSession
from agent_deck.adapters.codex_quota import CodexQuotaSnapshot
from agent_deck.adapters.codex_tokens import (
    CodexTokenPeriod,
    CodexTokenUsageSnapshot,
    CodexTokenUsageStats,
)
from agent_deck.core.events import AgentSource, EventType, NormalizedEvent
from agent_deck.core.state import AgentStatus
from agent_deck.hardware.fake import HardwareInput
from agent_deck.hardware.streamdock_n4pro import StreamDockN4ProAnimationResult
from agent_deck.hardware.streamdock_touchscreen import StreamDockTouchscreenRenderResult
from agent_deck.server.app import DaemonPollerConfig, create_app


def test_events_start_session_renders_status_and_layout() -> None:
    """Verify POST /events stores a session and GET /status renders it.

    入参：无；测试内创建 TestClient 并提交一个 session.started normalized event。
    返回：无返回值；断言通过代表 API 返回 JSON-safe state/layout，status 会再次 render。
    错误处理：HTTP 状态、agent 状态、layout 或 render count 不符合契约时由 pytest 报告。
    副作用：仅修改测试 app 的内存 runtime，并通过 fake surface 记录渲染帧。
    """

    client = TestClient(create_app())

    response = client.post("/events", json=_event("session-1").model_dump(mode="json"))
    status = client.get("/status")

    assert response.status_code == 200
    assert response.json()["state"]["agent_key"] == "codex:session-1"
    assert status.status_code == 200
    body = status.json()
    assert body["agents"][0]["agent_key"] == "codex:session-1"
    assert body["agents"][0]["status"] == "idle"
    assert body["layout"]["touchscreen"]["title"] == "session-1"
    assert body["layout"]["touchscreen"]["selected_agent_key"] == "codex:session-1"
    assert body["render_count"] > 0


def test_codex_app_state_poller_applies_input_requested_event() -> None:
    """Verify daemon startup poller syncs Codex App input requests into state.

    入参：无；测试内注入 fake Codex App state reader 和启用状态轮询的配置。
    返回：无返回值；断言通过代表 poller 能把 `input.requested` 映射为 `waiting_user`。
    错误处理：若 startup poller 未执行、状态未归约或 status 未暴露 agent，由 pytest 报告。
    副作用：仅修改测试 app 的内存 runtime，不读取真实 `~/.codex` 或连接 daemon。
    """

    def fake_codex_app_state_reader() -> tuple[NormalizedEvent, ...]:
        """返回一个待用户输入事件供 daemon poller 消费。

        入参：无。
        返回：包含单个 `EventType.INPUT_REQUESTED` 的事件 tuple。
        错误处理：事件构造失败由 Pydantic 抛出并交给 pytest。
        副作用：只创建内存事件。
        """

        occurred_at = datetime.now(UTC)
        return (
            NormalizedEvent.build(
                source=AgentSource.CODEX,
                source_event_type="codex-app.request_user_input",
                normalized_type=EventType.INPUT_REQUESTED,
                session_id="thread-1",
                thread_id="thread-1",
                title="请求用户选择选项",
                summary="请选择一个测试选项",
                payload={"call_id": "call-1"},
                occurred_at=occurred_at,
                received_at=occurred_at,
            ),
        )

    app = create_app(
        poller_config=DaemonPollerConfig(codex_app_state_enabled=True),
        codex_app_state_event_reader=fake_codex_app_state_reader,
        codex_app_active_sessions_reader=lambda **_: (),
    )
    with TestClient(app) as client:
        status = client.get("/status").json()

    assert status["agents"][0]["agent_key"] == "codex:thread-1"
    assert status["agents"][0]["status"] == "waiting_user"
    assert status["agents"][0]["last_summary"] == "请选择一个测试选项"
    assert status["pollers"]["codex_app_state"]["last_error"] is None
    assert status["pollers"]["codex_app_state"]["last_polled_at"] is not None


def test_codex_app_state_poller_applies_active_sessions() -> None:
    """Verify daemon startup poller syncs recent Codex App sessions into state.

    入参：无；测试内注入空 pending-event reader 和 fake active session reader。
    返回：无返回值；断言通过代表本地扫描出的 running_tool 会话会进入 AgentStateStore。
    错误处理：若 active session 未创建 agent 或状态/工具名不符合预期，由 pytest 报告。
    副作用：仅修改测试 app 的内存 runtime，不读取真实 `~/.codex`。
    """

    reader_calls: list[dict[str, object]] = []

    def fake_active_sessions_reader(**kwargs: object) -> tuple[CodexAppActiveSession]:
        """返回一个 running_tool 活动会话并记录筛选参数。

        入参：`kwargs` 是 daemon 传给 active session reader 的扫描/筛选参数。
        返回：包含单个 `CodexAppActiveSession` 的 tuple。
        错误处理：模型字段非法时由 Pydantic 抛出。
        副作用：记录调用参数。
        """

        reader_calls.append(kwargs)
        return (
            CodexAppActiveSession(
                thread_id="thread-1",
                title="实现 Codex 状态按钮",
                cwd="/repo",
                rollout_path="/tmp/rollout.jsonl",
                updated_at=1781773200,
                status=AgentStatus.RUNNING_TOOL,
                reason="pending tool call: shell",
            ),
        )

    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_app_state_enabled=True,
            codex_app_state_scan_limit=42,
            codex_app_active_window_seconds=1800,
            codex_app_active_session_limit=6,
        ),
        codex_app_state_event_reader=lambda: (),
        codex_app_active_sessions_reader=fake_active_sessions_reader,
    )
    with TestClient(app) as client:
        status = client.get("/status").json()

    assert reader_calls == [
        {
            "active_window_seconds": 1800,
            "max_sessions": 6,
            "scan_limit": 42,
        }
    ]
    assert status["agents"][0]["agent_key"] == "codex:thread-1"
    assert status["agents"][0]["status"] == "running_tool"
    assert status["agents"][0]["active_tool"] == "shell"
    assert status["layout"]["keys"][0]["visual"]["variant_id"] == "working"


def test_codex_quota_poller_updates_status_and_touchscreen_frame() -> None:
    """Verify daemon quota poller refreshes snapshot and virtual touch panel.

    入参：无；测试内注入 fake quota reader 和启用 quota 轮询的配置。
    返回：无返回值；断言通过代表 quota 被读取、状态暴露，并渲染了 N4 Pro 触屏背景图。
    错误处理：若 reader 未调用、状态缺失或触屏 frame 未记录，由 pytest 报告。
    副作用：只在内存中生成 Pillow 图像，不启动 Codex app-server 或访问真实 N4 Pro。
    """

    calls: list[dict[str, object]] = []

    def fake_quota_reader(**kwargs: object) -> CodexQuotaSnapshot:
        """返回固定 quota snapshot 并记录 daemon 传入的 timeout。

        入参：`kwargs` 是 daemon poller 转发给 quota adapter 的参数。
        返回：固定 `CodexQuotaSnapshot`。
        错误处理：模型字段非法时由 Pydantic 报告。
        副作用：把调用参数追加到测试内存列表。
        """

        calls.append(kwargs)
        return _quota_snapshot()

    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_quota_enabled=True,
            codex_quota_timeout_seconds=1.5,
        ),
        codex_quota_reader=fake_quota_reader,
    )
    with TestClient(app) as client:
        status = client.get("/status").json()

    assert calls == [{"timeout_seconds": 1.5}]
    assert status["codex_quota"]["snapshot"]["plan_short_label"] == "ProLite"
    assert status["codex_quota"]["snapshot"]["primary"]["used_percent"] == 28
    assert status["codex_quota"]["last_error"] is None
    assert status["codex_quota"]["updated_at"] is not None
    assert status["codex_quota"]["touchscreen_render_count"] == 1
    assert status["codex_quota"]["touchscreen_image_size"] == [800, 480]
    assert status["codex_quota"]["streamdock_touchscreen"] is None


def test_codex_quota_poller_sends_touchscreen_to_streamdock_sink() -> None:
    """Verify enabled quota poller sends the rendered image to the real-device sink.

    入参：无；测试内注入 fake quota reader 和 fake StreamDock sink。
    返回：无返回值；断言通过代表 daemon 复用 800x480 图下发，并在 status 暴露结果。
    错误处理：若 sink 未调用、图片尺寸错误或结果未记录，由 pytest 报告。
    副作用：只调用测试 fake sink，不访问真实 N4 Pro。
    """

    sink_images: list[object] = []

    def fake_quota_reader(**_: object) -> CodexQuotaSnapshot:
        """返回固定 quota snapshot。

        入参：忽略 daemon 传入的 reader 参数。
        返回：固定 `CodexQuotaSnapshot`。
        错误处理：字段非法由 Pydantic 抛出。
        副作用：无。
        """

        return _quota_snapshot()

    def fake_sink(image: object) -> StreamDockTouchscreenRenderResult:
        """记录 daemon 传入的触屏图像并模拟硬件下发成功。

        入参：`image` 是 quota renderer 输出的 Pillow 图像。
        返回：固定成功结果。
        错误处理：无。
        副作用：把 image 追加到测试内存列表。
        """

        sink_images.append(image)
        return StreamDockTouchscreenRenderResult(
            ok=True,
            device_type="FakeN4ProDevice",
            path="n4pro-path",
            sdk_result="0",
        )

    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_quota_enabled=True,
            streamdock_quota_touchscreen_enabled=True,
            streamdock_quota_device="n4pro",
        ),
        codex_quota_reader=fake_quota_reader,
        quota_touchscreen_sink=fake_sink,
    )
    with TestClient(app) as client:
        status = client.get("/status").json()

    assert len(sink_images) == 1
    assert getattr(sink_images[0], "size") == (800, 480)
    assert status["codex_quota"]["streamdock_touchscreen"] == {
        "background_api": None,
        "device_type": "FakeN4ProDevice",
        "error": None,
        "ok": True,
        "path": "n4pro-path",
        "sdk_result": "0",
    }


def test_codex_token_poller_updates_status_and_tokens_panel_frame() -> None:
    """Verify daemon token poller refreshes snapshot and touch tap shows tokens.

    入参：无；测试内注入 fake token reader，并用 logical panel input 模拟 touch bar 点击。
    返回：无返回值；断言通过代表 token usage 能进入 daemon runtime 并渲染 tokens 面板。
    错误处理：reader 未调用、status 未暴露或 panel 未切换/渲染时由 pytest 报告。
    副作用：只在内存中生成 Pillow 图像，不执行 ccusage 或访问真实 N4 Pro。
    """

    calls = 0

    def fake_token_reader() -> CodexTokenUsageSnapshot:
        """返回固定 token usage snapshot 并记录调用次数。

        入参：无。
        返回：固定 `CodexTokenUsageSnapshot`。
        错误处理：模型字段非法时由 Pydantic 报告。
        副作用：递增测试内 `calls`。
        """

        nonlocal calls
        calls += 1
        return _token_snapshot()

    app = create_app(
        poller_config=DaemonPollerConfig(codex_token_usage_enabled=True),
        codex_token_usage_reader=fake_token_reader,
    )
    with TestClient(app) as client:
        initial = client.get("/status").json()
        switch_response = client.post(
            "/logical-panel/input",
            json={"event": "touch.tap"},
        )
        status = client.get("/status").json()

    assert calls == 1
    assert initial["codex_tokens"]["snapshot"]["periods"]["today"]["total_tokens"] == (
        118_008_949
    )
    assert initial["codex_tokens"]["last_error"] is None
    assert initial["codex_tokens"]["updated_at"] is not None
    assert switch_response.status_code == 200
    assert switch_response.json()["selection"]["active_kind"] == "tokens"
    assert status["logical_panel"]["selection"]["active_kind"] == "tokens"
    assert status["logical_panel"]["selection"]["token_period"] == "today"
    assert status["logical_panel"]["touchscreen_image_source"] == "codex_tokens"
    assert status["logical_panel"]["touchscreen_image_size"] == [800, 480]


def test_hardware_input_endpoint_routes_touch_and_knob_to_logical_panel() -> None:
    """Verify low-level hardware input drives logical panel selection.

    入参：无；测试内先注入 token snapshot，再 POST touch/knob hardware input。
    返回：无返回值；断言通过代表 fake/真实监听器可复用同一低层输入入口。
    错误处理：HTTP 状态、panel 切换或 token 周期变化错误时由 pytest 报告。
    副作用：只修改测试 app 的内存 runtime，不访问真实硬件。
    """

    app = create_app(
        poller_config=DaemonPollerConfig(codex_token_usage_enabled=True),
        codex_token_usage_reader=_token_snapshot,
    )
    with TestClient(app) as client:
        touch_response = client.post(
            "/hardware/input",
            json=_hardware_input(
                kind="touch",
                index=0,
                value={"x": 120, "y": 380},
            ),
        )
        knob_response = client.post(
            "/hardware/input",
            json=_hardware_input(
                kind="knob",
                index=4,
                value={"action": "rotate", "direction": "right"},
            ),
        )
        status = client.get("/status").json()

    assert touch_response.status_code == 200
    assert touch_response.json()["handled"] is True
    assert touch_response.json()["panel_event"] == "touch.tap"
    assert knob_response.status_code == 200
    assert knob_response.json()["handled"] is True
    assert knob_response.json()["panel_event"] == "knob_4.rotate_right"
    assert status["logical_panel"]["selection"]["active_kind"] == "tokens"
    assert status["logical_panel"]["selection"]["token_period"] == "week"


def test_streamdock_n4pro_renderer_combines_quota_and_agent_keys(
    tmp_path: Path,
) -> None:
    """Verify unified N4 Pro renderer consumes quota background and key frames.

    入参：`tmp_path` 提供 generated frame root；测试内注入 fake Codex state reader、
    quota reader、legacy quota sink 和 unified N4 Pro renderer sink。
    返回：无返回值；断言通过代表 unified renderer 启用时会组合 quota 背景和按钮帧，
    且不会再调用 quota-only 真实触屏 sink。
    错误处理：若 frame 映射、互斥行为或 status 诊断不符合预期，由 pytest 报告。
    副作用：只写 pytest 临时 PNG，不访问真实 Codex 或 N4 Pro。
    """

    frame_root = tmp_path / "frames"
    working_dir = frame_root / "working"
    working_dir.mkdir(parents=True)
    frame_path = working_dir / "frame_000.png"
    Image.new("RGB", (112, 112), (3, 4, 5)).save(frame_path)
    renderer_calls: list[dict[str, object]] = []
    quota_touchscreen_calls: list[object] = []

    def fake_codex_app_state_reader() -> tuple[NormalizedEvent, ...]:
        """返回一个 running_tool 事件供 daemon layout 映射 working 帧。

        入参：无。
        返回：包含单个 `EventType.TOOL_STARTED` 的事件 tuple。
        错误处理：事件构造失败由 Pydantic 抛出并交给 pytest。
        副作用：只创建内存事件。
        """

        occurred_at = datetime.now(UTC)
        return (
            NormalizedEvent.build(
                source=AgentSource.CODEX,
                source_event_type="tool.started",
                normalized_type=EventType.TOOL_STARTED,
                session_id="thread-1",
                thread_id="thread-1",
                title="thread-1",
                cwd="/repo",
                tool_name="shell",
                occurred_at=occurred_at,
                received_at=occurred_at,
            ),
        )

    def fake_quota_reader(**_: object) -> CodexQuotaSnapshot:
        """返回固定 quota snapshot。

        入参：忽略 daemon 传入的 reader 参数。
        返回：固定 `CodexQuotaSnapshot`。
        错误处理：字段非法由 Pydantic 抛出。
        副作用：无。
        """

        return _quota_snapshot()

    def fake_quota_touchscreen_sink(
        image: object,
    ) -> StreamDockTouchscreenRenderResult:
        """记录意外 quota-only 硬件 sink 调用。

        入参：`image` 是 quota renderer 输出。
        返回：固定成功结果。
        错误处理：无。
        副作用：追加调用记录；该测试期望不会被调用。
        """

        quota_touchscreen_calls.append(image)
        return StreamDockTouchscreenRenderResult(ok=True)

    def fake_n4pro_renderer(**kwargs: object) -> StreamDockN4ProAnimationResult:
        """记录 unified renderer 参数并模拟下发成功。

        入参：`kwargs` 包含背景图、按键帧、播放时长和 fps。
        返回：固定成功结果。
        错误处理：无。
        副作用：追加调用记录，不访问硬件。
        """

        renderer_calls.append(kwargs)
        return StreamDockN4ProAnimationResult(
            ok=True,
            device_type="FakeN4ProDevice",
            path="n4pro-path",
            frames_rendered=2,
            key_count=len(kwargs["key_frame_paths"]),
        )

    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_app_state_enabled=True,
            codex_quota_enabled=True,
            streamdock_quota_touchscreen_enabled=True,
            streamdock_n4pro_renderer_enabled=True,
            streamdock_n4pro_frame_root=frame_root,
            streamdock_n4pro_render_interval_seconds=0.5,
            streamdock_n4pro_renderer_fps=4,
        ),
        codex_app_state_event_reader=fake_codex_app_state_reader,
        codex_app_active_sessions_reader=lambda **_: (),
        codex_quota_reader=fake_quota_reader,
        quota_touchscreen_sink=fake_quota_touchscreen_sink,
        streamdock_n4pro_renderer_sink=fake_n4pro_renderer,
    )
    with TestClient(app) as client:
        status = client.get("/status").json()

    assert quota_touchscreen_calls == []
    assert renderer_calls
    call = renderer_calls[-1]
    assert getattr(call["background_image"], "size") == (800, 480)
    assert call["key_frame_paths"] == {1: (frame_path.resolve(),)}
    assert call["duration_seconds"] == 0.5
    assert call["fps"] == 4
    assert status["streamdock_n4pro_renderer"]["last_result"] == {
        "background_result": None,
        "device_type": "FakeN4ProDevice",
        "error": None,
        "frames_rendered": 2,
        "key_count": 1,
        "ok": True,
        "path": "n4pro-path",
        "timing_seconds": {},
    }
    assert status["streamdock_n4pro_renderer"]["last_error"] is None
    assert status["codex_quota"]["streamdock_touchscreen"] is None


def test_streamdock_n4pro_renderer_starts_with_placeholder_without_panel_data(
    tmp_path: Path,
) -> None:
    """Verify N4 Pro renderer starts even before quota/token snapshots exist.

    入参：`tmp_path` 提供 fake frame root。
    返回：无返回值；断言通过代表真实 renderer 不依赖当前 panel 数据已加载。
    错误处理：renderer 未被调用或背景缺失时由 pytest 报告。
    副作用：只调用 fake renderer，不访问真实 N4 Pro。
    """

    renderer_calls: list[dict[str, object]] = []

    def fake_n4pro_renderer(**kwargs: object) -> StreamDockN4ProAnimationResult:
        """记录 renderer 调用并返回成功结果。

        入参：`kwargs` 包含背景图、按键帧、播放时长和 fps。
        返回：固定成功结果。
        错误处理：无。
        副作用：追加调用记录。
        """

        renderer_calls.append(kwargs)
        return StreamDockN4ProAnimationResult(ok=True)

    app = create_app(
        poller_config=DaemonPollerConfig(
            streamdock_n4pro_renderer_enabled=True,
            streamdock_n4pro_frame_root=tmp_path,
            streamdock_n4pro_render_interval_seconds=0.5,
        ),
        streamdock_n4pro_renderer_sink=fake_n4pro_renderer,
    )
    with TestClient(app) as client:
        status = client.get("/status").json()

    assert renderer_calls
    call = renderer_calls[-1]
    assert getattr(call["background_image"], "size") == (800, 480)
    assert call["key_frame_paths"] == {}
    assert status["streamdock_n4pro_renderer"]["last_error"] is None
    assert status["streamdock_n4pro_renderer"]["last_result"]["ok"] is True


def test_default_n4pro_renderer_input_callback_routes_sdk_events(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """Verify default N4 Pro renderer callback drives logical panel input.

    入参：`monkeypatch` 替换 persistent animator；`tmp_path` 提供 fake frame root。
    返回：无返回值；断言通过代表真实 SDK callback 可进入 daemon runtime。
    错误处理：callback 未注入、event 未映射或 selection 未更新时由 pytest 报告。
    副作用：只运行 fake renderer，不访问真实 N4 Pro。
    """

    captured: dict[str, object] = {}

    class FakePersistentAnimator:
        """捕获 daemon 默认构造的 persistent animator。

        入参：`input_callback` 是 daemon 注入的 SDK event callback。
        返回：callable fake renderer。
        错误处理：无。
        副作用：保存 callback 到测试 dict。
        """

        def __init__(self, *, input_callback: object | None = None) -> None:
            """保存 input callback。

            入参：`input_callback` 是 daemon 传入的 callback。
            返回：无。
            错误处理：无。
            副作用：写入测试 dict。
            """

            captured["input_callback"] = input_callback

        def __call__(self, **_: object) -> StreamDockN4ProAnimationResult:
            """模拟一次统一 renderer 成功。

            入参：忽略 renderer 参数。
            返回：固定成功结果。
            错误处理：无。
            副作用：无。
            """

            return StreamDockN4ProAnimationResult(ok=True)

        def close(self) -> None:
            """模拟 renderer close。

            入参：无。
            返回：无。
            错误处理：无。
            副作用：无。
            """

    monkeypatch.setattr(
        server_app,
        "StreamDockN4ProPersistentAnimator",
        FakePersistentAnimator,
    )
    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_token_usage_enabled=True,
            streamdock_n4pro_renderer_enabled=True,
            streamdock_n4pro_frame_root=tmp_path,
        ),
        codex_token_usage_reader=_token_snapshot,
    )
    with TestClient(app) as client:
        input_callback = captured["input_callback"]
        assert callable(input_callback)
        input_callback(
            object(),
            _sdk_event(event_type="knob_press", knob_id="knob_4", state=1),
        )
        input_callback(
            object(),
            _sdk_event(
                event_type="knob_rotate",
                knob_id="knob_4",
                direction="right",
            ),
        )
        status = client.get("/status").json()

    assert status["logical_panel"]["selection"]["active_kind"] == "tokens"
    assert status["logical_panel"]["selection"]["token_period"] == "week"


async def test_streamdock_n4pro_loop_deducts_render_time_from_interval(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """Verify N4 Pro render loop does not sleep a full interval after playback.

    入参：`monkeypatch` 替换 renderer once、sleep 和 monotonic；`tmp_path` 提供无访问的
    fake frame root。
    返回：无返回值；断言通过代表 loop 先立即渲染，再只等待扣除播放耗时后的剩余周期。
    错误处理：若 loop 先 sleep、或播放后仍 sleep 完整 interval，由 pytest 报告。
    副作用：只运行被替换的 coroutine，不访问真实 daemon、quota、文件或硬件。
    """

    events: list[tuple[str, float]] = []
    times = iter((10.0, 12.75))

    async def fake_render_once(*args: object, **kwargs: object) -> None:
        """记录一次 renderer loop 调用。

        入参：忽略 positional args，读取 `duration_seconds`。
        返回：无返回值。
        错误处理：缺失参数会由测试失败暴露。
        副作用：追加内存事件记录。
        """

        events.append(("render", float(kwargs["duration_seconds"])))

    async def fake_sleep(delay: float) -> None:
        """记录 loop sleep 并终止无限循环。

        入参：`delay` 是 loop 计算出的剩余等待时间。
        返回：不返回；总是抛 `CancelledError` 结束测试。
        错误处理：无业务错误；取消异常由测试捕获。
        副作用：追加内存事件记录。
        """

        events.append(("sleep", delay))
        raise asyncio.CancelledError

    monkeypatch.setattr(server_app, "_render_streamdock_n4pro_once", fake_render_once)

    try:
        await server_app._render_streamdock_n4pro_loop(
            object(),
            interval_seconds=3.0,
            fps=10,
            frame_root=tmp_path,
            renderer_sink=lambda **_: StreamDockN4ProAnimationResult(ok=True),
            sleep=fake_sleep,
            monotonic=lambda: next(times),
        )
    except asyncio.CancelledError:
        pass

    assert events == [("render", 3.0), ("sleep", 0.25)]


def test_decision_request_updates_pending_status_and_decision_layout() -> None:
    """Verify POST /decisions/request creates pending state and layout.

    入参：无；测试内先创建 agent，再请求一个 shell approval decision。
    返回：无返回值；断言通过代表 status decisions、decision mode 和 agent pending count 同步。
    错误处理：pending decision 未创建、layout 未进入 decision mode 或状态未同步时报错。
    副作用：仅修改测试 app 内存 runtime，并记录 fake render 帧。
    """

    client = TestClient(create_app())
    client.post("/events", json=_event("session-1").model_dump(mode="json"))

    response = client.post(
        "/decisions/request",
        json={
            "agent_key": "codex:session-1",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_name": "shell",
            "reason": "run command",
            "timeout_seconds": 30,
        },
    )
    status = client.get("/status")

    assert response.status_code == 200
    decision = response.json()
    assert decision["agent_key"] == "codex:session-1"
    assert decision["status"] == "pending"
    body = status.json()
    assert len(body["decisions"]) == 1
    assert body["decisions"][0]["decision_id"] == decision["decision_id"]
    assert body["layout"]["mode"] == "decision"
    assert body["agents"][0]["pending_decision_count"] == 1


def test_decision_resolve_deny_clears_pending_state() -> None:
    """Verify POST /decisions/{id}/resolve denies and clears pending state.

    入参：无；测试内创建 agent 和 pending decision，然后用 deny result resolve。
    返回：无返回值；断言通过代表 response result、pending list 和 agent pending count 同步。
    错误处理：resolve 未返回 deny、decision 未清空或 agent count 未回落时由 pytest 报告。
    副作用：仅修改测试 app 内存 runtime，并记录 fake render 帧。
    """

    client = TestClient(create_app())
    client.post("/events", json=_event("session-1").model_dump(mode="json"))
    decision_id = _request_decision(client)

    response = client.post(
        f"/decisions/{decision_id}/resolve",
        json={"behavior": "deny", "message": "not now"},
    )
    status = client.get("/status")

    assert response.status_code == 200
    resolved = response.json()
    assert resolved["result"]["behavior"] == "deny"
    assert resolved["result"]["message"] == "not now"
    body = status.json()
    assert body["decisions"] == []
    assert body["agents"][0]["pending_decision_count"] == 0


def test_repeated_resolve_one_of_two_pending_decisions_decrements_once() -> None:
    """Verify repeated resolve of one decision does not clear another pending.

    入参：无；测试内为同一 agent 创建两个 pending decisions，并重复 resolve 第一个。
    返回：无返回值；断言通过代表 store pending count 按 decision id 幂等同步。
    错误处理：若第二个 pending 被误清理或 count 被重复递减，会由 pytest 报告。
    副作用：仅修改测试 app 的内存 broker/store，并记录 fake render 帧。
    """

    client = TestClient(create_app())
    client.post("/events", json=_event("session-1").model_dump(mode="json"))
    first_id = _request_decision(client, tool_name="shell")
    second_id = _request_decision(client, tool_name="python")

    first_resolve = client.post(
        f"/decisions/{first_id}/resolve",
        json={"behavior": "deny", "message": "first"},
    )
    second_resolve = client.post(
        f"/decisions/{first_id}/resolve",
        json={"behavior": "allow", "message": "duplicate"},
    )
    status = client.get("/status").json()

    assert first_resolve.status_code == 200
    assert second_resolve.status_code == 200
    assert second_resolve.json()["result"]["behavior"] == "deny"
    assert [decision["decision_id"] for decision in status["decisions"]] == [
        second_id
    ]
    assert status["agents"][0]["pending_decision_count"] == 1
    assert status["layout"]["mode"] == "decision"


async def test_concurrent_wait_timeouts_for_same_decision_decrement_once() -> None:
    """Verify concurrent wait timeouts for one decision sync terminal state once.

    入参：无；测试内为同一 agent 创建两个 pending decisions，并并发等待第一个超时。
    返回：无返回值；断言通过代表两个 waiters 只让第一个 decision 终态同步一次。
    错误处理：若另一个 pending 的 count 被误清零或 decisions 列表丢失，会由 pytest 报告。
    副作用：仅修改测试 app 内存 runtime，并调度两个 ASGI wait 请求。
    """

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/events", json=_event("session-1").model_dump(mode="json"))
        first_id = await _request_decision_async(client, tool_name="shell")
        second_id = await _request_decision_async(client, tool_name="python")

        first_wait, second_wait = await asyncio.gather(
            client.get(f"/decisions/{first_id}/wait?timeout_seconds=0.001"),
            client.get(f"/decisions/{first_id}/wait?timeout_seconds=0.001"),
        )
        status = (await client.get("/status")).json()

    assert first_wait.status_code == 200
    assert second_wait.status_code == 200
    assert first_wait.json()["behavior"] == "deny"
    assert second_wait.json()["behavior"] == "deny"
    assert [decision["decision_id"] for decision in status["decisions"]] == [
        second_id
    ]
    assert status["agents"][0]["pending_decision_count"] == 1
    assert status["layout"]["mode"] == "decision"


def test_wait_returns_existing_resolved_decision_result() -> None:
    """Verify GET /decisions/{id}/wait returns a prior resolved result.

    入参：无；测试内创建 decision，先 resolve 为 allow，再调用 wait endpoint。
    返回：无返回值；断言通过代表 wait 可读取已有终态，不依赖并发 resolve。
    错误处理：HTTP 状态或返回 behavior/message 不符合契约时由 pytest 报告。
    副作用：仅修改测试 app 内存 runtime，并通过 FastAPI async handler 等待 broker。
    """

    client = TestClient(create_app())
    client.post("/events", json=_event("session-1").model_dump(mode="json"))
    decision_id = _request_decision(client)
    client.post(
        f"/decisions/{decision_id}/resolve",
        json={"behavior": "allow", "message": "approved"},
    )

    response = client.get(f"/decisions/{decision_id}/wait?timeout_seconds=1")

    assert response.status_code == 200
    assert response.json() == {"behavior": "allow", "message": "approved"}


def test_testclient_pending_wait_timeout_returns_deny_not_500() -> None:
    """Verify pending wait works across TestClient request loops.

    入参：无；测试内使用 `raise_server_exceptions=False` 的 TestClient 创建 pending decision
    后等待短 timeout。
    返回：无返回值；断言通过代表 API 不暴露 cross-loop future 500，并返回默认 deny。
    错误处理：若 wait await 到其他 loop 的 future，HTTP 500 会由断言报告。
    副作用：仅修改测试 app 内存 runtime，并让 broker timeout 一个 pending decision。
    """

    client = TestClient(create_app(), raise_server_exceptions=False)
    client.post("/events", json=_event("session-1").model_dump(mode="json"))
    decision_id = _request_decision(client)

    response = client.get(f"/decisions/{decision_id}/wait?timeout_seconds=0.001")

    assert response.status_code == 200
    assert response.json()["behavior"] == "deny"


def test_request_before_event_reconciles_pending_decisions_when_agent_arrives() -> None:
    """Verify pending broker decisions are reflected when agent state appears.

    入参：无；测试内先请求 decision，再提交同一 agent 的 session.started event。
    返回：无返回值；断言通过代表 status 中 agent pending count 与 broker pending 对齐。
    错误处理：若 agent 保持 idle/0 但 decisions/layout 有 pending，会由 pytest 报告。
    副作用：仅修改测试 app 内存 runtime，并通过 status 触发 reconciliation render。
    """

    client = TestClient(create_app())
    decision_id = _request_decision(client)

    event_response = client.post(
        "/events",
        json=_event("session-1").model_dump(mode="json"),
    )
    status = client.get("/status").json()

    assert event_response.status_code == 200
    assert [decision["decision_id"] for decision in status["decisions"]] == [
        decision_id
    ]
    assert status["agents"][0]["agent_key"] == "codex:session-1"
    assert status["agents"][0]["status"] == "approval_needed"
    assert status["agents"][0]["pending_decision_count"] == 1
    assert status["layout"]["mode"] == "decision"


async def test_wait_timeout_status_keeps_agents_decisions_and_layout_consistent() -> None:
    """Verify wait timeout clears only the timed-out decision in full status.

    入参：无；测试内为同一 agent 创建两个 pending decisions，并等待第一个超时。
    返回：无返回值；断言通过代表 agents、decisions 和 layout 同步指向剩余 pending。
    错误处理：若 agent count、broker pending 或 layout mode 不一致，会由 pytest 报告。
    副作用：修改测试 app 内存 broker/store，并通过 wait/status render fake surface。
    """

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/events", json=_event("session-1").model_dump(mode="json"))
        first_id = await _request_decision_async(client, tool_name="shell")
        second_id = await _request_decision_async(client, tool_name="python")

        wait = await client.get(f"/decisions/{first_id}/wait?timeout_seconds=0.001")
        status = (await client.get("/status")).json()

    assert wait.status_code == 200
    assert wait.json()["behavior"] == "deny"
    assert [decision["decision_id"] for decision in status["decisions"]] == [
        second_id
    ]
    assert status["agents"][0]["pending_decision_count"] == 1
    assert status["agents"][0]["status"] == "approval_needed"
    assert status["layout"]["mode"] == "decision"
    assert status["layout"]["touchscreen"]["selected_decision_id"] == second_id


def test_unknown_resolve_and_wait_return_404() -> None:
    """Verify unknown decision ids map to HTTP 404.

    入参：无；测试内对不存在的 decision id 分别调用 resolve 和 wait。
    返回：无返回值；断言通过代表 API 不把未知 decision 静默创建或默认拒绝。
    错误处理：若返回非 404 状态码，会由 pytest 报告。
    副作用：仅读取测试 app 空 broker，不访问外部 I/O。
    """

    client = TestClient(create_app())

    resolve = client.post(
        "/decisions/missing/resolve",
        json={"behavior": "allow", "message": ""},
    )
    wait = client.get("/decisions/missing/wait?timeout_seconds=1")

    assert resolve.status_code == 404
    assert wait.status_code == 404


def test_non_positive_timeouts_return_422() -> None:
    """Verify request and wait timeouts must be positive.

    入参：无；测试内分别提交 request body timeout 和 wait query timeout 的非正值。
    返回：无返回值；断言通过代表 FastAPI/Pydantic 在 handler 业务逻辑前返回 422。
    错误处理：若非正 timeout 被接受或映射成其他状态码，会由 pytest 报告。
    副作用：request case 不应创建 decision；wait case 仅读取空 broker 路由校验。
    """

    client = TestClient(create_app())

    request = client.post(
        "/decisions/request",
        json={
            "agent_key": "codex:session-1",
            "session_id": "session-1",
            "tool_name": "shell",
            "reason": "run command",
            "timeout_seconds": 0,
        },
    )
    wait = client.get("/decisions/missing/wait?timeout_seconds=0")

    assert request.status_code == 422
    assert wait.status_code == 422


def test_events_accept_nested_payload_and_return_json_safe_response() -> None:
    """Verify nested event payloads do not leak frozen containers into JSON.

    入参：无；测试内提交含 nested dict/list payload 的 session.started event。
    返回：无返回值；断言通过代表 request parsing 和 response serialization 都 JSON-safe。
    错误处理：若 FrozenDict/tuple 未正确转换导致 HTTP 序列化失败，会由 pytest 报告。
    副作用：仅修改测试 app 内存 store，并 render fake surface 一帧。
    """

    client = TestClient(create_app())
    event = _event(
        "session-nested",
        payload={"items": [{"name": "alpha", "values": [1, 2]}]},
    )

    response = client.post("/events", json=event.model_dump(mode="json"))

    assert response.status_code == 200
    body = response.json()
    assert body["state"]["agent_key"] == "codex:session-nested"
    assert body["layout"]["touchscreen"]["title"] == "session-nested"
    assert body["render_count"] > 0


def _event(
    session_id: str,
    *,
    payload: dict[str, object] | None = None,
) -> NormalizedEvent:
    """Build a JSON-serializable session.started event for server tests.

    入参：`session_id` 是测试 agent session id，同时作为默认 title 展示；`payload`
    可提供 JSON-like 嵌套结构，用于验证 HTTP parsing 和 serialization。
    返回：可通过 `model_dump(mode="json")` 发送给 `/events` 的 `NormalizedEvent`。
    错误处理：字段校验异常会由 `NormalizedEvent.build` 抛出并交给 pytest。
    副作用：仅创建内存模型；不访问网络、硬件、文件或测试 app runtime。
    """

    occurred_at = datetime.now(UTC)
    return NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type="session_started",
        normalized_type=EventType.SESSION_STARTED,
        session_id=session_id,
        title=session_id,
        payload=payload,
        occurred_at=occurred_at,
        received_at=occurred_at + timedelta(milliseconds=1),
    )


def _hardware_input(
    *,
    kind: str,
    index: int,
    value: object,
) -> dict[str, object]:
    """构造可提交给 `/hardware/input` 的 JSON-safe hardware input。

    入参：`kind`、`index` 和 `value` 描述低层硬件输入。
    返回：`HardwareInput.model_dump(mode="json")` 的 dict。
    错误处理：字段非法由 `HardwareInput` 校验并交给 pytest。
    副作用：只创建内存模型。
    """

    return {
        "kind": kind,
        "index": index,
        "value": value,
        "occurred_at": datetime(2026, 6, 22, 12, 0, tzinfo=UTC).isoformat(),
    }


def _sdk_event(
    *,
    event_type: str,
    knob_id: str | None = None,
    direction: str | None = None,
    state: int | None = None,
    x: int | None = None,
    y: int | None = None,
) -> object:
    """构造 SDK-like InputEvent 测试替身。

    入参：`event_type` 是 SDK event type；`knob_id`、`direction`、`state`、`x`、`y`
    是可选属性。
    返回：带 `.value` enum-like 属性的对象。
    错误处理：无。
    副作用：只创建内存对象。
    """

    class ValueObject:
        """带 `.value` 的 SDK enum-like 测试替身。

        入参：`value` 是字符串值。
        返回：测试对象。
        错误处理：无。
        副作用：无。
        """

        def __init__(self, value: str) -> None:
            """保存字符串值。

            入参：`value` 是字符串值。
            返回：无。
            错误处理：无。
            副作用：无。
            """

            self.value = value

    class Event:
        """最小 SDK InputEvent 替身。

        入参：无。
        返回：测试对象。
        错误处理：无。
        副作用：无。
        """

    event = Event()
    event.event_type = ValueObject(event_type)
    event.knob_id = ValueObject(knob_id) if knob_id is not None else None
    event.direction = ValueObject(direction) if direction is not None else None
    event.state = state
    event.x = x
    event.y = y
    return event


def _request_decision(
    client: TestClient,
    *,
    session_id: str = "session-1",
    tool_name: str = "shell",
) -> str:
    """Create one pending decision through the HTTP API and return its id.

    入参：`client` 是目标 TestClient；`session_id` 是要绑定的 agent session；
    `tool_name` 是本次 approval 关联工具名。
    返回：新 pending decision 的 `decision_id` 字符串。
    错误处理：非 200 响应或缺失字段会通过 assert/KeyError 由 pytest 报告。
    副作用：修改传入 client 对应 app 的内存 broker/state，并触发 fake render。
    """

    response = client.post(
        "/decisions/request",
        json={
            "agent_key": f"codex:{session_id}",
            "session_id": session_id,
            "turn_id": "turn-1",
            "tool_name": tool_name,
            "reason": "run command",
            "timeout_seconds": 30,
        },
    )
    assert response.status_code == 200
    return response.json()["decision_id"]


async def _request_decision_async(
    client: AsyncClient,
    *,
    session_id: str = "session-1",
    tool_name: str = "shell",
) -> str:
    """Create one pending decision through an async HTTP client.

    入参：`client` 是绑定 ASGI app 的 `AsyncClient`；`session_id` 和 `tool_name`
    决定 request body 中的 agent 和工具上下文。
    返回：新 pending decision 的 `decision_id` 字符串。
    错误处理：非 200 响应或缺失字段会通过 assert/KeyError 由 pytest 报告。
    副作用：修改对应 app 的 in-memory broker/state，并触发 fake render。
    """

    response = await client.post(
        "/decisions/request",
        json={
            "agent_key": f"codex:{session_id}",
            "session_id": session_id,
            "turn_id": "turn-1",
            "tool_name": tool_name,
            "reason": "run command",
            "timeout_seconds": 30,
        },
    )
    assert response.status_code == 200
    return response.json()["decision_id"]


def _quota_snapshot() -> CodexQuotaSnapshot:
    """构造固定 quota snapshot 供 daemon poller 测试使用。

    入参：无。
    返回：包含 ProLite、5 小时窗口和 weekly 窗口的 `CodexQuotaSnapshot`。
    错误处理：字段非法时由 Pydantic 抛出并交给 pytest。
    副作用：无；只创建内存模型。
    """

    tz = ZoneInfo("Asia/Shanghai")
    return CodexQuotaSnapshot(
        plan_type="prolite",
        plan_short_label="ProLite",
        plan_display_name="ProLite",
        primary={
            "used_percent": 28,
            "window_duration_mins": 300,
            "resets_at": datetime(2026, 6, 17, 19, 51, 2, tzinfo=tz),
        },
        secondary={
            "used_percent": 8,
            "window_duration_mins": 10080,
            "resets_at": datetime(2026, 6, 24, 13, 47, 28, tzinfo=tz),
        },
        credits_balance="0",
        raw={},
    )


def _token_snapshot() -> CodexTokenUsageSnapshot:
    """构造固定 token usage snapshot 供 daemon poller 测试使用。

    入参：无。
    返回：包含 today/week/month/all 四个周期的 `CodexTokenUsageSnapshot`。
    错误处理：字段非法时由 Pydantic 抛出并交给 pytest。
    副作用：无；只创建内存模型。
    """

    stats = CodexTokenUsageStats(
        input_tokens=6_465_793,
        output_tokens=436_596,
        reasoning_output_tokens=110_065,
        cache_read_tokens=111_106_560,
        total_tokens=118_008_949,
        cost_usd=100.98012500000002,
    )
    return CodexTokenUsageSnapshot(
        periods={period: stats for period in CodexTokenPeriod},
        updated_at=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
        raw={},
    )
