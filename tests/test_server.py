"""Tests for the local Agent Deck daemon HTTP API.

These tests define Task 7's in-process FastAPI contract only. They do not open
real sockets, probe StreamDock hardware, install hooks, or read user files; their side
effects are limited to TestClient requests, local asyncio scheduling inside FastAPI,
pytest temp files, and pytest assertion reporting.
"""

from __future__ import annotations

import asyncio
import base64
import plistlib
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from zoneinfo import ZoneInfo

import agent_deck.server.app as server_app
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from PIL import Image

from agent_deck.actions.app_icon_cache import AppIconCache
from agent_deck.actions.apps import LocalAppActionResult, LocalAppInfo
from agent_deck.actions.focus import FocusActionResult
from agent_deck.actions.local_targets import LocalTargetActionResult
from agent_deck.actions.system_controls import InMemorySystemControlExecutor
from agent_deck.actions.url_icon_cache import UrlIconCache
from agent_deck.adapters.codex_app_state import CodexAppActiveSession
from agent_deck.adapters.codex_quota import CodexQuotaSnapshot
from agent_deck.adapters.codex_tokens import (
    CodexTokenPeriod,
    CodexTokenUsageSnapshot,
    CodexTokenUsageStats,
)
from agent_deck.core.events import AgentSource, EventType, NormalizedEvent
from agent_deck.core.modes import DeckMode, DeckSelection
from agent_deck.core.state import AgentStatus
from agent_deck.hardware.fake import HardwareInput
from agent_deck.hardware.streamdock_n4pro import StreamDockN4ProAnimationResult
from agent_deck.hardware.streamdock_touchscreen import StreamDockTouchscreenRenderResult
from agent_deck.rendering.key_surface import (
    KeySurfaceKind,
    N4ProKeyBinding,
    N4ProKeyLayout,
    default_n4pro_key_layout,
)
from agent_deck.rendering.layout import build_layout_plan
from agent_deck.rendering.logical_panel import PanelKind
from agent_deck.rendering.rotary_surface import default_n4pro_rotary_layout
from agent_deck.server.app import DaemonPollerConfig, create_app
from agent_deck.server.quota_presentation_store import (
    QuotaPresentation,
    QuotaPresentationRule,
    save_quota_presentation,
)


def test_web_index_serves_n4pro_layout_editor() -> None:
    """Verify daemon root serves the local N4 Pro layout editor shell.

    入参：无；测试内创建 TestClient 并读取 `/`。
    返回：无返回值；断言通过代表本地 GUI 入口可用且不会影响 API 路由。
    错误处理：HTTP 状态、内容类型或核心 HTML 文案缺失时由 pytest 报告。
    副作用：只读取包内静态 HTML，不访问真实硬件、用户配置或网络。
    """

    client = TestClient(create_app())

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Agent Deck · N4 Pro" in response.text
    assert 'data-device-profile="mirabox.n4pro"' in response.text
    assert "keyGrid" in response.text
    assert "knob-strip" in response.text
    assert response.text.index("surface-swap.js") < response.text.index("app.js")


def test_web_asset_route_serves_only_whitelisted_assets() -> None:
    """Verify packaged web assets are served through a narrow allowlist.

    入参：无；测试内读取允许的品牌/CSS/JS 资源和不允许的路径。
    返回：无返回值；断言通过代表 GUI 静态资源可用，任意文件名不会被暴露。
    错误处理：允许资源缺失或未知资源未返回 404 时由 pytest 报告。
    副作用：只读取包内静态资源，不访问真实硬件、用户配置或网络。
    """

    client = TestClient(create_app())

    logo_response = client.get("/assets/logo_command_core.png")
    css_response = client.get("/web/app.css")
    swap_controller_response = client.get("/web/surface-swap.js")
    missing_response = client.get("/assets/../config.py")
    missing_web_response = client.get("/web/../server/app.py")

    assert logo_response.status_code == 200
    assert logo_response.headers["content-type"] == "image/png"
    assert css_response.status_code == 200
    assert css_response.headers["content-type"].startswith("text/css")
    assert swap_controller_response.status_code == 200
    assert "createBoundedSwapController" in swap_controller_response.text
    assert missing_response.status_code == 404
    assert missing_web_response.status_code == 404


def test_local_apps_api_returns_injected_catalog(tmp_path: Path) -> None:
    """Verify GUI can fetch local App catalog from the daemon.

    入参：无；测试内注入 fake app catalog reader。
    返回：无返回值；断言通过代表 `/ui/apps` 暴露 App 名称、路径、bundle id 和图标。
    错误处理：catalog reader 未调用或 JSON shape 错误时由 pytest 报告。
    副作用：只创建 TestClient，不扫描本机应用目录。
    """

    finder_app = _fake_finder_app(tmp_path)
    app = create_app(
        app_icon_cache_path=tmp_path / "icon-cache",
        local_app_catalog_reader=lambda: (
            LocalAppInfo(
                name="Finder",
                app_path=str(finder_app),
                bundle_id="com.apple.finder",
                icon_token="FI",
            ),
        )
    )
    client = TestClient(app)

    response = client.get("/ui/apps")
    body = response.json()

    assert response.status_code == 200
    assert body["platform"] == "darwin"
    assert len(body["apps"]) == 1
    app_payload = body["apps"][0]
    assert app_payload["name"] == "Finder"
    assert app_payload["app_path"] == str(finder_app)
    assert app_payload["bundle_id"] == "com.apple.finder"
    assert app_payload["icon_token"] == "FI"
    assert app_payload["icon_url"] == "/ui/app-icons/com.apple.finder/icon-96.png"
    assert app_payload["key_icon_url"] == "/ui/app-icons/com.apple.finder/key-112.png"
    assert app_payload["icon_cache_status"] == "ready"
    icon_response = client.get(app_payload["icon_url"])
    assert icon_response.status_code == 200
    assert icon_response.headers["content-type"] == "image/png"
    refresh_response = client.post("/ui/apps/refresh-icons")
    assert refresh_response.status_code == 200
    assert refresh_response.json()["refreshed_count"] == 1


def test_url_icon_api_returns_cached_favicon(tmp_path: Path) -> None:
    """Verify GUI can resolve and fetch cached URL favicon assets.

    入参：`tmp_path` 提供隔离 URL icon cache。
    返回：无返回值；断言通过代表 `/ui/url-icons/resolve` 会生成 GUI/硬件共用缓存。
    错误处理：HTTP 状态、响应 shape 或 PNG route 不符合预期时由 pytest 报告。
    副作用：只写 pytest 临时目录，不访问互联网。
    """

    calls: list[str] = []
    client = TestClient(
        create_app(
            url_icon_cache_path=tmp_path / "url-icons",
            url_icon_fetcher=_fake_site_fetcher(calls),
        )
    )

    response = client.get("/ui/url-icons/resolve", params={"url": "https://example.com/docs"})
    body = response.json()

    assert response.status_code == 200
    assert calls == [
        "https://example.com",
        "https://example.com/assets/icon.png",
    ]
    assert body["origin"] == "https://example.com"
    assert body["host"] == "example.com"
    assert body["icon_token"] == "EX"
    assert body["icon_cache_status"] == "ready"
    assert body["icon_url"].startswith("/ui/url-icons/example.com-")
    assert body["key_icon_url"].endswith("/key-112.png")
    icon_response = client.get(body["icon_url"])
    key_icon_response = client.get(body["key_icon_url"])
    assert icon_response.status_code == 200
    assert icon_response.headers["content-type"] == "image/png"
    assert key_icon_response.status_code == 200
    assert key_icon_response.headers["content-type"] == "image/png"


def test_url_icon_lookup_does_not_fetch_missing_icon(tmp_path: Path) -> None:
    """Verify GUI can lookup cached URL icons without network access.

    入参：`tmp_path` 提供隔离 URL icon cache。
    返回：无返回值；断言通过代表 URL 输入不会自动触发网页解析。
    错误处理：fetcher 被调用或响应 shape 错误时由 pytest 报告。
    副作用：只读 pytest 临时目录，不访问互联网。
    """

    calls: list[str] = []
    client = TestClient(
        create_app(
            url_icon_cache_path=tmp_path / "url-icons",
            url_icon_fetcher=_fake_site_fetcher(calls),
        )
    )

    response = client.get("/ui/url-icons/lookup", params={"url": "https://example.com/docs"})
    body = response.json()

    assert response.status_code == 200
    assert calls == []
    assert body["origin"] == "https://example.com"
    assert body["host"] == "example.com"
    assert body["icon_cache_status"] == "missing"
    assert body["icon_url"] is None


def test_url_icon_upload_stores_custom_image(tmp_path: Path) -> None:
    """Verify GUI can upload a local image as URL icon cache.

    入参：`tmp_path` 提供隔离 URL icon cache。
    返回：无返回值；断言通过代表本地图片可复制进程序缓存并用于 GUI。
    错误处理：HTTP 状态、响应 shape 或 PNG route 不符合预期时由 pytest 报告。
    副作用：只写 pytest 临时目录，不访问互联网。
    """

    client = TestClient(create_app(url_icon_cache_path=tmp_path / "url-icons"))
    data_url = "data:image/png;base64," + base64.b64encode(
        _png_bytes((32, 32), (220, 80, 60, 255))
    ).decode("ascii")

    response = client.post(
        "/ui/url-icons/upload",
        json={
            "url": "https://example.com/docs",
            "filename": "custom.png",
            "data_url": data_url,
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["icon_cache_status"] == "custom"
    assert body["icon_cache_source"] == "custom_upload"
    assert body["icon_url"].startswith("/ui/url-icons/example.com-")
    icon_response = client.get(body["icon_url"])
    assert icon_response.status_code == 200
    assert icon_response.headers["content-type"] == "image/png"


def test_key_layout_api_saves_runtime_layout_and_updates_status_projection() -> None:
    """Verify GUI key layout API updates daemon runtime layout projection.

    入参：无；测试内保存一份 N4 Pro 10 键布局，并创建一个 agent session。
    返回：无返回值；断言通过代表 `/status` 暴露 runtime key layout，且 Agent 只进入 Agent 槽。
    错误处理：API 状态码、布局来源或 key projection 不符合预期时由 pytest 报告。
    副作用：只修改测试 app 内存 runtime，不写用户配置文件或访问真实硬件。
    """

    client = TestClient(create_app())
    layout_body = {
        "keys": [
            {
                "index": 0,
                "kind": "app",
                "label": "Finder",
                "app_name": "Finder",
                "app_path": "/System/Library/CoreServices/Finder.app",
                "bundle_id": "com.apple.finder",
                "icon_token": "FI",
            },
            {"index": 1, "kind": "unassigned"},
            {"index": 2, "kind": "unassigned"},
            {"index": 3, "kind": "unassigned"},
            {"index": 4, "kind": "unassigned"},
            {"index": 5, "kind": "agent"},
            {"index": 6, "kind": "agent"},
            {"index": 7, "kind": "agent"},
            {"index": 8, "kind": "agent"},
            {"index": 9, "kind": "agent"},
        ]
    }

    save_response = client.put("/ui/key-layout", json=layout_body)
    client.post("/events", json=_event("session-1").model_dump(mode="json"))
    status = client.get("/status").json()

    assert save_response.status_code == 200
    assert save_response.json()["key_layout"]["source"] == "runtime"
    assert status["key_layout"]["source"] == "runtime"
    assert status["layout"]["keys"][0]["kind"] == "app"
    assert status["layout"]["keys"][0]["intent"] == "open_or_focus_app"
    assert status["layout"]["keys"][0]["payload"]["app_name"] == "Finder"
    assert status["layout"]["keys"][1]["kind"] == "unassigned"
    assert status["layout"]["keys"][1]["intent"] == "show_brand_feedback"
    assert status["layout"]["keys"][5]["kind"] == "agent"
    assert status["layout"]["keys"][5]["intent"] == "select_agent"
    assert status["layout"]["keys"][5]["agent_key"] == "codex:session-1"


def test_key_layout_api_persists_layout_when_store_path_is_configured(
    tmp_path: Path,
) -> None:
    """Verify configured key layout path survives daemon app recreation.

    入参：`tmp_path` 提供隔离 JSON 存储路径。
    返回：无返回值；断言通过代表 PUT 会写 JSON，新 app 会从同一路径读回布局。
    错误处理：未写文件、source 不正确或重建后布局丢失时由 pytest 报告。
    副作用：只写 pytest 临时目录，不访问用户配置目录或真实硬件。
    """

    layout_path = tmp_path / "n4pro-key-layout.json"
    layout_body = {
        "keys": [
            {
                "index": 0,
                "kind": "app",
                "label": "Finder",
                "app_name": "Finder",
                "app_path": "/System/Library/CoreServices/Finder.app",
                "bundle_id": "com.apple.finder",
                "icon_token": "FI",
            },
            {"index": 1, "kind": "unassigned"},
            {"index": 2, "kind": "unassigned"},
            {"index": 3, "kind": "unassigned"},
            {"index": 4, "kind": "unassigned"},
            {"index": 5, "kind": "agent"},
            {"index": 6, "kind": "agent"},
            {"index": 7, "kind": "agent"},
            {"index": 8, "kind": "agent"},
            {"index": 9, "kind": "agent"},
        ]
    }
    client = TestClient(create_app(key_layout_path=layout_path))

    save_response = client.put("/ui/key-layout", json=layout_body)
    restored = TestClient(create_app(key_layout_path=layout_path)).get(
        "/ui/key-layout"
    )

    assert save_response.status_code == 200
    assert save_response.json()["key_layout"]["source"] == "persisted"
    assert save_response.json()["key_layout"]["path"] == str(layout_path)
    assert layout_path.is_file()
    assert restored.status_code == 200
    assert restored.json()["source"] == "persisted"
    assert restored.json()["path"] == str(layout_path)
    assert restored.json()["layout"]["keys"][0]["kind"] == "app"
    assert restored.json()["layout"]["keys"][0]["app_name"] == "Finder"


def test_key_layout_store_error_falls_back_to_default_layout(tmp_path: Path) -> None:
    """Verify invalid persisted key layout does not prevent daemon startup.

    入参：`tmp_path` 提供坏 JSON 文件路径。
    返回：无返回值；断言通过代表 daemon 回退默认布局并暴露 last_error。
    错误处理：坏配置导致 app 创建失败或错误未出现在 status 时由 pytest 报告。
    副作用：只写 pytest 临时目录，不访问用户配置目录或真实硬件。
    """

    layout_path = tmp_path / "n4pro-key-layout.json"
    layout_path.write_text("{bad json", encoding="utf-8")
    client = TestClient(create_app(key_layout_path=layout_path))

    status = client.get("/status").json()

    assert status["key_layout"]["source"] == "default"
    assert status["key_layout"]["path"] == str(layout_path)
    assert "不是合法 JSON" in status["key_layout_last_error"]


def test_key_layout_api_rejects_incomplete_or_high_risk_shape() -> None:
    """Verify key layout API rejects incomplete or invalid action definitions.

    入参：无；测试内提交缺少 App 定位字段和不完整 key 集合的布局。
    返回：无返回值；断言通过代表校验在 handler 业务逻辑前 fail-closed。
    错误处理：非法布局被接受时由 pytest 报告。
    副作用：只创建测试 app，不写配置或访问硬件。
    """

    client = TestClient(create_app())

    incomplete = client.put(
        "/ui/key-layout",
        json={"keys": [{"index": 0, "kind": "unassigned"}]},
    )
    bad_app = client.put(
        "/ui/key-layout",
        json={
            "keys": [
                {"index": 0, "kind": "app", "app_name": "Finder"},
                {"index": 1, "kind": "unassigned"},
                {"index": 2, "kind": "unassigned"},
                {"index": 3, "kind": "unassigned"},
                {"index": 4, "kind": "unassigned"},
                {"index": 5, "kind": "agent"},
                {"index": 6, "kind": "agent"},
                {"index": 7, "kind": "agent"},
                {"index": 8, "kind": "agent"},
                {"index": 9, "kind": "agent"},
            ]
        },
    )

    assert incomplete.status_code == 422
    assert bad_app.status_code == 422


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
    assert status["agents"][0]["focus_target"] == "codex-app:thread-1"
    assert status["layout"]["keys"][5]["visual"]["variant_id"] == "working"


def test_rotary_layout_api_saves_group_lighting_and_exposes_profile_capabilities() -> None:
    """旋钮 GUI API 应保存完整 layout，并把 N4 Pro 灯光限制作为一个 group 暴露。

    入参：无；测试先读取默认 layout，再提交颜色灯光草稿作为已应用配置。
    返回：无返回值；断言通过表示 GUI 可以据 API 正确同步四个预览灯圈，不会推断 per-control LED。
    错误处理：HTTP body、capability 或 layout 回显错误时由 pytest 报告。
    副作用：只修改测试 daemon 的内存 layout，不访问真实 N4 Pro。
    """

    layout = default_n4pro_rotary_layout().model_dump(mode="json")
    layout["lighting"] = {"mode": "color", "color": "#35c9ff"}
    with TestClient(create_app()) as client:
        initial = client.get("/ui/rotary-layout")
        apply_response = client.put("/ui/rotary-layout", json=layout)
        capabilities = client.get("/ui/control-capabilities")

    assert initial.status_code == 200
    assert initial.json()["source"] == "default"
    assert apply_response.status_code == 200
    assert apply_response.json()["rotary_layout"]["layout"]["lighting"] == {
        "mode": "color",
        "color": "#35C9FF",
        "breathe": False,
    }
    zones = capabilities.json()["device_profile"]["light"]["zones"]
    assert zones == [
        {
            "id": "rotary_ring_group",
            "addressability": "group",
                "associated_control_ids": ["knob_1", "knob_2", "knob_3", "knob_4"],
                "supports_color": True,
                "supports_brightness": True,
                "supports_breathe": True,
        }
    ]


def test_configuration_api_applies_key_and_rotary_layouts_in_one_request() -> None:
    """保存并应用 API 应同时接受主按键和旋钮草稿，而不是让 GUI 发两次独立请求。

    入参：无；测试提交默认 key layout 和修改过的 rotary layout。
    返回：无返回值；断言通过表示 Web 只在用户按保存后共同更新两个 applied state。
    错误处理：HTTP 路由、嵌套模型或响应回显不一致时由 pytest 报告。
    副作用：只更新测试 daemon 内存，不访问真实硬件。
    """

    rotary = default_n4pro_rotary_layout().model_dump(mode="json")
    rotary["controls"][1]["rotate_action"] = "adjust_output_volume"
    with TestClient(create_app()) as client:
        response = client.put(
            "/ui/configuration",
            json={
                "key_layout": default_n4pro_key_layout().model_dump(mode="json"),
                "rotary_layout": rotary,
            },
        )

    assert response.status_code == 200
    assert response.json()["key_layout"]["layout"]["keys"][0]["index"] == 0
    assert response.json()["rotary_layout"]["layout"]["controls"][1]["rotate_action"] == (
        "adjust_output_volume"
    )


def test_rotary_input_executes_confirmed_system_control_and_renders_transient_hud() -> None:
    """已应用旋钮 binding 应通过 executor 执行并显示短暂 HUD。

    入参：无；测试把旋钮 2 绑定到输出音量，并注入内存 executor。
    返回：无返回值；断言通过表示 router 不直接写系统，只有 executor 确认成功后才显示 value HUD。
    错误处理：binding、固定 2% 步进、反馈来源或状态诊断错误时由 pytest 报告。
    副作用：只修改 fake executor 和测试 daemon 内存。
    """

    executor = InMemorySystemControlExecutor(output_volume_percent=48)
    layout = default_n4pro_rotary_layout().model_dump(mode="json")
    layout["controls"][1]["rotate_action"] = "adjust_output_volume"
    with TestClient(create_app(system_control_executor=executor)) as client:
        client.put("/ui/rotary-layout", json=layout)
        response = client.post(
            "/hardware/input",
            json=_hardware_input(
                kind="knob",
                index=2,
                value={"action": "rotate", "direction": "right"},
            ),
        )
        status = client.get("/status").json()

    assert response.status_code == 200
    assert response.json()["handled"] is True
    assert response.json()["action"]["value_percent"] == 50
    assert executor.output_volume_percent == 50
    assert status["logical_panel"]["touchscreen_image_source"] == "control_feedback:value"
    assert status["logical_panel"]["control_feedback"]["value"] == "50%"


def test_configured_streamdock_content_rotation_uses_each_detent_without_accumulation() -> None:
    """配置驱动的 StreamDock 内容旋转应每格切换一次，不再等待两格累计。

    入参：无；测试把 runtime 放入 Usage panel，并直接传入默认已配置 knob 4 右旋事件。
    返回：无返回值；断言通过表示 token 周期从 Day 立即进入 Week。
    错误处理：累计阈值仍为两格或 selection 变化错误时由 pytest 报告。
    副作用：只修改测试 daemon 内存状态。
    """

    app = create_app()
    runtime = app.state.runtime
    runtime.logical_panel_selection = runtime.logical_panel_selection.model_copy(
        update={"active_kind": PanelKind.TOKENS}
    )

    response = runtime.apply_streamdock_input_event(
        _sdk_event(
            event_type="knob_rotate",
            knob_id="knob_4",
            direction="right",
        )
    )

    assert response["handled"] is True
    assert response["selection"]["token_period"] == "week"


def test_n4pro_session_output_restores_applied_brightness_and_writes_one_group_color() -> None:
    """N4 Pro init 后应在同一会话恢复亮度，并只写一次 group LED 颜色。

    入参：无；测试注入最小 SDK-like device，并提交非默认控制台亮度和基础色。
    返回：无返回值；断言通过表示 renderer 不需要额外 open/init 才能更新灯光或亮度。
    错误处理：SDK 方法调用参数或 applied output 缓存错误时由 pytest 报告。
    副作用：只修改 fake device 和测试 runtime 内存。
    """

    class FakeDevice:
        """记录 session output SDK 写入的最小替身。

        入参：无。
        返回：可被 runtime 调用的 fake 设备。
        错误处理：无。
        副作用：保存调用记录。
        """

        def __init__(self) -> None:
            """初始化空调用记录。

            入参：无。
            返回：无显式返回值。
            错误处理：无。
            副作用：创建内存列表。
            """

            self.calls: list[tuple[str, tuple[int, ...]]] = []

        def set_brightness(self, percent: int) -> int:
            """记录控制台亮度写入。

            入参：`percent` 是整体亮度。
            返回：0 表示成功。
            错误处理：无。
            副作用：追加调用记录。
            """

            self.calls.append(("brightness", (percent,)))
            return 0

        def set_led_color(self, red: int, green: int, blue: int) -> int:
            """记录唯一旋钮灯圈组 RGB 写入。

            入参：RGB 三通道整数。
            返回：0 表示成功。
            错误处理：无。
            副作用：追加调用记录。
            """

            self.calls.append(("led", (red, green, blue)))
            return 0

        def set_led_brightness(self, percent: int) -> int:
            """记录唯一旋钮灯圈组亮度写入。

            入参：`percent` 是 0 到 100 的灯圈组亮度。
            返回：0 表示成功。
            错误处理：无。
            副作用：追加调用记录。
            """

            self.calls.append(("led_brightness", (percent,)))
            return 0

    layout = default_n4pro_rotary_layout().model_dump(mode="json")
    layout["console_brightness_percent"] = 64
    layout["lighting"] = {"mode": "color", "color": "#35c9ff"}
    app = create_app()
    runtime = app.state.runtime
    runtime.update_rotary_layout(type(default_n4pro_rotary_layout()).model_validate(layout))
    device = FakeDevice()

    assert runtime.apply_n4pro_session_outputs(device, initialized=True) is None
    assert runtime.apply_n4pro_session_outputs(device, initialized=False) is None

    assert device.calls == [
        ("brightness", (64,)),
        ("led", (53, 201, 255)),
        ("led_brightness", (100,)),
    ]


def test_n4pro_session_output_reports_unsigned_native_failures() -> None:
    """附加亮度和灯光写入应识别 ctypes 暴露的无符号 native 失败码。

    入参：无；测试 fake device 的三个输出方法均返回 `0xFFFFFFFF`。
    返回：无返回值；断言通过表示 status 能显示连接失效而非缓存错误 applied 值。
    错误处理：任一失败被误判为成功时由 pytest 报告。
    副作用：只修改测试 runtime 内存，不访问真实 N4 Pro。
    """

    class FailingDevice:
        """对所有 session output 返回无符号 native 失败码的 fake。

        入参：无。
        返回：提供 runtime 需要的三个 SDK-like 方法。
        错误处理：无。
        副作用：无。
        """

        def set_brightness(self, _percent: int) -> int:
            """返回整体亮度写失败码。

            入参：`_percent` 是被忽略的目标亮度。
            返回：固定 `0xFFFFFFFF`。
            错误处理：不抛异常。
            副作用：无。
            """

            return 0xFFFFFFFF

        def set_led_color(self, _red: int, _green: int, _blue: int) -> int:
            """返回灯圈颜色写失败码。

            入参：三个下划线参数是被忽略的 RGB 值。
            返回：固定 `0xFFFFFFFF`。
            错误处理：不抛异常。
            副作用：无。
            """

            return 0xFFFFFFFF

        def set_led_brightness(self, _percent: int) -> int:
            """返回灯圈亮度写失败码。

            入参：`_percent` 是被忽略的目标亮度。
            返回：固定 `0xFFFFFFFF`。
            错误处理：不抛异常。
            副作用：无。
            """

            return 0xFFFFFFFF

    runtime = create_app().state.runtime

    error = runtime.apply_n4pro_session_outputs(FailingDevice(), initialized=True)

    assert error == (
        "set_brightness failed: SDK returned 4294967295; "
        "set_led_color failed: SDK returned 4294967295; "
        "set_led_brightness failed: SDK returned 4294967295"
    )
    assert runtime.n4pro_last_applied_brightness_percent is None
    assert runtime.n4pro_last_applied_lighting is None
    assert runtime.n4pro_last_applied_led_brightness_percent is None


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
    assert status["codex_quota"]["snapshot"]["windows"][0]["used_percent"] == 28
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
    返回：无返回值；断言通过代表 token usage 可在 Brand -> Quota -> Usage 手动轮换后渲染。
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


def test_token_snapshot_prewarm_caches_all_usage_period_panels() -> None:
    """Token 快照更新后应预渲染四个 Usage 周期，切换时不再临时成图。

    入参：无；测试直接向 daemon runtime 写入固定快照。
    返回：无返回值；断言通过表示缓存诊断会显示四个 Token 面板已准备。
    错误处理：缓存未预热、容量错误或状态未暴露时由 pytest 报告。
    副作用：只修改测试 app 的内存 runtime，不访问 ccusage 或真实硬件。
    """

    app = create_app()
    runtime = app.state.runtime
    runtime.update_codex_token_usage(_token_snapshot(), updated_at=datetime.now(UTC))

    cache = runtime.logical_panel_image_cache.diagnostics()

    assert cache["token_entries"] == 4
    assert cache["misses"] >= 4


def test_hardware_input_endpoint_routes_touch_and_knob_to_logical_panel() -> None:
    """Verify low-level hardware input drives logical panel selection.

    入参：无；测试内先注入 token snapshot，再 POST touch/knob hardware input。
    返回：无返回值；断言通过代表 fake/真实监听器复用同一触控入口，并让旋钮读取用户 binding。
    错误处理：HTTP 状态、panel 切换或 quota 内容变化错误时由 pytest 报告。
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
    assert knob_response.json()["panel_event"] is None
    assert knob_response.json()["rotary_intent"]["rotate_action"] == "cycle_panel_content"
    assert status["logical_panel"]["selection"]["active_kind"] == "quota"
    assert status["logical_panel"]["selection"]["quota_window"] == "auto"


def test_hardware_key_selects_agent_and_reports_missing_focus_target() -> None:
    """低层 agent key input 应选择 agent，并报告缺少 focus target。

    入参：无；测试内创建两个 agent session，再按当前 layout 的 agent slot。
    返回：无返回值；断言通过代表 agent key 会选择并尝试激活对应 agent。
    错误处理：selection、intent 记录或 dry-run 状态不符合预期时由 pytest 报告。
    副作用：只修改测试 app 的内存 runtime，不访问真实窗口系统或硬件。
    """

    app = create_app()
    with TestClient(app) as client:
        client.post("/events", json=_event("session-1").model_dump(mode="json"))
        client.post("/events", json=_event("session-2").model_dump(mode="json"))
        status_before = client.get("/status").json()
        session_1_key = _key_index_for_agent(status_before, "codex:session-1")

        select_response = client.post(
            "/hardware/input",
            json=_hardware_input(
                kind="key",
                index=session_1_key,
                value={"state": 1},
            ),
        )
        status_after = client.get("/status").json()

    assert select_response.status_code == 200
    assert select_response.json()["handled"] is True
    assert select_response.json()["interaction_intent"]["intent"] == "select_agent"
    assert select_response.json()["interaction_intent"]["agent_key"] == (
        "codex:session-1"
    )
    assert select_response.json()["action"]["status"] == "missing_target"
    assert status_after["deck_selection"]["selected_agent_key"] == "codex:session-1"
    assert status_after["interaction"]["last_intent"]["intent"] == "select_agent"
    assert status_after["interaction"]["last_action"] == {
        "intent": "focus_agent",
        "agent_key": "codex:session-1",
        "decision_id": None,
        "status": "missing_target",
        "ok": False,
        "target_available": False,
        "focus_target": None,
        "message": "focus_agent ignored; missing focus target",
    }


def test_agent_key_focus_uses_state_focus_target_by_default() -> None:
    """agent key 默认应使用 state 中已有的 focus target 调用 executor。

    入参：无；测试内创建带 `focus_target` payload 的 session 并按 agent key。
    返回：无返回值；断言通过代表真实 focus executor 默认启用但可被测试注入替身。
    错误处理：executor 未调用或 focus target 未保存时由 pytest 报告。
    副作用：只调用 fake executor，不执行真实 focus。
    """

    calls: list[str] = []

    def fake_focus_executor(focus_target: str) -> FocusActionResult:
        """记录 focus target 并返回成功。

        入参：`focus_target` 是 runtime 决定执行的目标。
        返回：成功结果。
        错误处理：无。
        副作用：写入测试内存列表。
        """

        calls.append(focus_target)
        return FocusActionResult(
            ok=True,
            status="succeeded",
            focus_target=focus_target,
            message=f"activated {focus_target}",
        )

    app = create_app(focus_action_executor=fake_focus_executor)
    with TestClient(app) as client:
        client.post(
            "/events",
            json=_event(
                "session-1",
                payload={"focus_target": "app:Codex"},
            ).model_dump(mode="json"),
        )
        status_before = client.get("/status").json()
        agent_key = _key_index_for_agent(status_before, "codex:session-1")
        response = client.post(
            "/hardware/input",
            json=_hardware_input(
                kind="key",
                index=agent_key,
                value={"state": 1},
            ),
        )
        status = client.get("/status").json()

    assert calls == ["app:Codex"]
    assert response.status_code == 200
    assert response.json()["action"]["status"] == "succeeded"
    assert response.json()["action"]["focus_target"] == "app:Codex"
    assert status["agents"][0]["focus_target"] == "app:Codex"
    assert status["interaction"]["last_action"]["message"] == (
        "activated app:Codex"
    )


def test_focus_action_can_be_disabled_for_diagnostics() -> None:
    """显式关闭 focus actions 后，agent key 只记录 dry-run。

    入参：无；测试内注入 fake focus executor 并提供 app focus target。
    返回：无返回值；断言通过代表排障时仍可关闭真实本机动作。
    错误处理：executor 被调用或 action 诊断错误时由 pytest 报告。
    副作用：只修改测试 app 内存 runtime。
    """

    calls: list[str] = []

    def fake_focus_executor(focus_target: str) -> FocusActionResult:
        """记录 focus target 并返回成功。

        入参：`focus_target` 是 runtime 决定执行的目标。
        返回：成功结果。
        错误处理：无。
        副作用：写入测试内存列表。
        """

        calls.append(focus_target)
        return FocusActionResult(
            ok=True,
            status="succeeded",
            focus_target=focus_target,
            message=f"activated {focus_target}",
        )

    app = create_app(
        poller_config=DaemonPollerConfig(focus_actions_enabled=False),
        focus_action_executor=fake_focus_executor,
    )
    with TestClient(app) as client:
        client.post(
            "/events",
            json=_event(
                "session-1",
                payload={"focus_target": "app:Codex"},
            ).model_dump(mode="json"),
        )
        status_before = client.get("/status").json()
        agent_key = _key_index_for_agent(status_before, "codex:session-1")
        response = client.post(
            "/hardware/input",
            json=_hardware_input(
                kind="key",
                index=agent_key,
                value={"state": 1},
            ),
        )
        status = client.get("/status").json()

    assert calls == []
    assert response.json()["interaction_intent"]["intent"] == "select_agent"
    assert response.json()["action"]["status"] == "dry_run"
    assert response.json()["action"]["target_available"] is True
    assert response.json()["action"]["message"] == (
        "focus_agent dry-run recorded for app:Codex"
    )
    assert status["interaction"]["last_action"]["status"] == "dry_run"


def test_app_key_input_executes_local_app_action() -> None:
    """App key press should execute the structured local App action.

    入参：无；测试内保存 App key layout 并注入 fake App action executor。
    返回：无返回值；断言通过代表硬件按键会把 payload 交给 action 层。
    错误处理：payload 丢失、executor 未调用或 action 诊断错误时由 pytest 报告。
    副作用：只修改测试 app 内存 runtime，不启动真实 App。
    """

    calls: list[dict[str, str | None]] = []

    def fake_app_executor(
        *,
        app_name: str | None = None,
        app_path: str | None = None,
        bundle_id: str | None = None,
    ) -> LocalAppActionResult:
        """记录 App action payload 并返回成功。"""

        calls.append(
            {
                "app_name": app_name,
                "app_path": app_path,
                "bundle_id": bundle_id,
            }
        )
        return LocalAppActionResult(
            ok=True,
            status="succeeded",
            app_name=app_name,
            app_path=app_path,
            bundle_id=bundle_id,
            message=f"opened {app_name}",
        )

    app = create_app(local_app_action_executor=fake_app_executor)
    with TestClient(app) as client:
        client.put(
            "/ui/key-layout",
            json={
                "keys": [
                    {
                        "index": 0,
                        "kind": "app",
                        "label": "Finder",
                        "app_name": "Finder",
                        "app_path": "/System/Library/CoreServices/Finder.app",
                        "bundle_id": "com.apple.finder",
                        "icon_token": "FI",
                    },
                    {"index": 1, "kind": "unassigned"},
                    {"index": 2, "kind": "unassigned"},
                    {"index": 3, "kind": "unassigned"},
                    {"index": 4, "kind": "unassigned"},
                    {"index": 5, "kind": "agent"},
                    {"index": 6, "kind": "agent"},
                    {"index": 7, "kind": "agent"},
                    {"index": 8, "kind": "agent"},
                    {"index": 9, "kind": "agent"},
                ]
            },
        )
        response = client.post(
            "/hardware/input",
            json=_hardware_input(kind="key", index=0, value={"state": 1}),
        )
        status = client.get("/status").json()

    assert calls == [
        {
            "app_name": "Finder",
            "app_path": "/System/Library/CoreServices/Finder.app",
            "bundle_id": "com.apple.finder",
        }
    ]
    assert response.status_code == 200
    assert response.json()["interaction_intent"]["intent"] == "open_or_focus_app"
    assert response.json()["interaction_intent"]["dry_run"] is False
    assert response.json()["interaction_intent"]["payload"]["app_name"] == "Finder"
    assert response.json()["action"]["status"] == "succeeded"
    assert response.json()["action"]["bundle_id"] == "com.apple.finder"
    assert status["interaction"]["last_action"]["message"] == "opened Finder"


def test_url_key_executes_local_target_action() -> None:
    """URL key press 应执行受控本机 target action。

    入参：无。
    返回：无返回值；断言通过代表硬件按键会把 URL payload 交给 action 层。
    错误处理：executor 未调用、payload 丢失或 action 诊断错误时由 pytest 报告。
    副作用：只修改测试 app 内存 runtime，不打开浏览器。
    """

    calls: list[dict[str, str | None]] = []

    def fake_url_executor(url: str | None = None) -> LocalTargetActionResult:
        """记录 URL action payload 并返回成功。

        入参：`url` 是 runtime 解析出的 URL。
        返回：成功结果。
        错误处理：无。
        副作用：写入 `calls`。
        """

        calls.append({"kind": "url", "value": url})
        return LocalTargetActionResult(
            ok=True,
            status="succeeded",
            target_type="url",
            url=url,
            message=f"opened {url}",
        )

    app = create_app(local_url_action_executor=fake_url_executor)
    with TestClient(app) as client:
        client.put(
            "/ui/key-layout",
            json={
                "keys": [
                    {
                        "index": 0,
                        "kind": "url",
                        "url": "https://agent.deck.local",
                    },
                    {"index": 1, "kind": "unassigned"},
                    {"index": 2, "kind": "unassigned"},
                    {"index": 3, "kind": "unassigned"},
                    {"index": 4, "kind": "unassigned"},
                    {"index": 5, "kind": "agent"},
                    {"index": 6, "kind": "agent"},
                    {"index": 7, "kind": "agent"},
                    {"index": 8, "kind": "agent"},
                    {"index": 9, "kind": "agent"},
                ]
            },
        )
        url_response = client.post(
            "/hardware/input",
            json=_hardware_input(kind="key", index=0, value={"state": 1}),
        )
        status = client.get("/status").json()

    assert calls == [{"kind": "url", "value": "https://agent.deck.local"}]
    assert url_response.json()["interaction_intent"]["intent"] == "open_url"
    assert url_response.json()["action"]["status"] == "succeeded"
    assert url_response.json()["action"]["url"] == "https://agent.deck.local"
    assert status["interaction"]["last_action"]["target_type"] == "url"


def test_legacy_folder_key_is_not_executed() -> None:
    """旧配置里的 folder key 不应再执行本机目录打开。

    入参：无；测试内直接提交兼容 schema 仍接受的旧 folder binding。
    返回：无返回值；断言通过代表 `open_path` 会被识别但返回 unsupported。
    错误处理：动作被执行或状态不是 unsupported 时由 pytest 报告。
    副作用：只修改测试 app 内存 runtime，不打开 Finder。
    """

    with TestClient(create_app()) as client:
        client.put(
            "/ui/key-layout",
            json={
                "keys": [
                    {"index": 0, "kind": "folder", "path": "~/Projects"},
                    {"index": 1, "kind": "unassigned"},
                    {"index": 2, "kind": "unassigned"},
                    {"index": 3, "kind": "unassigned"},
                    {"index": 4, "kind": "unassigned"},
                    {"index": 5, "kind": "agent"},
                    {"index": 6, "kind": "agent"},
                    {"index": 7, "kind": "agent"},
                    {"index": 8, "kind": "agent"},
                    {"index": 9, "kind": "agent"},
                ]
            },
        )
        response = client.post(
            "/hardware/input",
            json=_hardware_input(kind="key", index=0, value={"state": 1}),
        )

    assert response.json()["interaction_intent"]["intent"] == "open_path"
    assert response.json()["action"]["status"] == "unsupported"
    assert response.json()["action"]["ok"] is False


def test_unassigned_key_input_shows_brand_feedback_panel() -> None:
    """未配置按键应短暂显示 Agent Deck 默认品牌面板。

    入参：无；测试内按默认布局的第一个未配置键。
    返回：无返回值；断言通过代表未配置键不会只是 dry-run，而会触发 touch bar 反馈图。
    错误处理：intent、action 或 fake 触屏图诊断不符合预期时由 pytest 报告。
    副作用：只修改测试 app 内存 runtime，不访问真实硬件。
    """

    client = TestClient(create_app())

    response = client.post(
        "/hardware/input",
        json=_hardware_input(kind="key", index=0, value={"state": 1}),
    )
    status = client.get("/status").json()

    assert response.status_code == 200
    assert response.json()["handled"] is True
    assert response.json()["interaction_intent"]["intent"] == "show_brand_feedback"
    assert response.json()["action"] == {
        "intent": "show_brand_feedback",
        "agent_key": None,
        "decision_id": None,
        "status": "shown",
        "ok": True,
        "duration_seconds": 4.0,
        "touchscreen_image_source": "agent_deck:brand_feedback",
        "touchscreen_image_size": [800, 480],
        "message": "brand feedback panel shown",
    }
    assert status["interaction"]["last_action"]["status"] == "shown"
    assert status["logical_panel"]["touchscreen_image_source"] == (
        "agent_deck:brand_feedback"
    )


def test_brand_feedback_panel_expires_back_to_current_logical_panel(
    monkeypatch: object,
) -> None:
    """品牌反馈过期后后台 renderer 应恢复当前 logical panel。

    入参：`monkeypatch` 固定 monotonic 时间；测试内先准备 quota 数据，再触发品牌反馈。
    返回：无返回值；断言通过代表 transient override 不会永久覆盖原面板。
    错误处理：反馈源或过期恢复源错误时由 pytest 报告。
    副作用：只修改测试 app 内存 runtime，不访问真实硬件。
    """

    now = 100.0
    monkeypatch.setattr(server_app.time, "monotonic", lambda: now)
    app = create_app()
    runtime = app.state.runtime
    runtime.update_codex_quota(_quota_snapshot(), updated_at=datetime.now(UTC))

    client = TestClient(app)
    client.post(
        "/hardware/input",
        json=_hardware_input(kind="key", index=0, value={"state": 1}),
    )
    feedback_image, feedback_source = runtime.build_current_logical_panel_background()

    now = 105.0
    restored_image, restored_source = runtime.build_current_logical_panel_background()

    assert getattr(feedback_image, "size") == (800, 480)
    assert feedback_source == "agent_deck:brand_feedback"
    assert getattr(restored_image, "size") == (800, 480)
    assert restored_source == "agent_deck:splash"


def test_streamdock_n4pro_key_images_include_app_bindings(tmp_path: Path) -> None:
    """N4 Pro renderer 应把 App binding 转成静态物理按键图。

    入参：`tmp_path` 提供 fake Finder `.app` bundle。
    返回：无返回值；断言通过代表 daemon renderer 会把 App key 投影到 key_images。
    错误处理：缺少 key image、尺寸错误或未使用 App 图标时由 pytest 报告。
    副作用：只写 pytest 临时目录，不访问真实 N4 Pro 或真实 App。
    """

    finder_app = _fake_finder_app(tmp_path)
    key_layout = N4ProKeyLayout(
        keys=(
            N4ProKeyBinding(
                index=0,
                kind=KeySurfaceKind.APP,
                label="Finder",
                app_name="Finder",
                app_path=str(finder_app),
                bundle_id="com.apple.finder",
                icon_token="FI",
            ),
            *default_n4pro_key_layout().sorted_keys()[1:],
        )
    )
    layout = build_layout_plan(
        [],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
        key_layout=key_layout,
    )

    app_icon_cache = AppIconCache(tmp_path / "icon-cache")

    key_images = server_app._key_images_from_layout(
        layout,
        app_icon_cache=app_icon_cache,
    )

    assert sorted(key_images) == [1]
    assert (tmp_path / "icon-cache/com.apple.finder/key-112.png").is_file()
    image = key_images[1]
    assert getattr(image, "size") == (112, 112)
    assert getattr(image, "mode") == "RGB"
    assert _near_color(image.getpixel((56, 56)), (20, 120, 220))


def test_streamdock_n4pro_key_images_include_url_bindings(tmp_path: Path) -> None:
    """N4 Pro renderer 应把 URL binding 转成静态物理按键图。

    入参：`tmp_path` 提供隔离 URL icon cache。
    返回：无返回值；断言通过代表 daemon renderer 会把 URL key 投影到 key_images。
    错误处理：缺少 key image、尺寸错误或未使用 favicon 时由 pytest 报告。
    副作用：只写 pytest 临时目录，不访问真实 N4 Pro 或互联网。
    """

    key_layout = N4ProKeyLayout(
        keys=(
            N4ProKeyBinding(
                index=0,
                kind=KeySurfaceKind.URL,
                label="Docs",
                url="https://example.com/docs",
            ),
            *default_n4pro_key_layout().sorted_keys()[1:],
        )
    )
    layout = build_layout_plan(
        [],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
        key_layout=key_layout,
    )
    url_icon_cache = UrlIconCache(
        tmp_path / "url-icon-cache",
        fetcher=_fake_site_fetcher([]),
    )
    url_icon_cache.ensure("https://example.com/docs")

    key_images = server_app._key_images_from_layout(
        layout,
        url_icon_cache=url_icon_cache,
    )

    assert sorted(key_images) == [1]
    image = key_images[1]
    assert getattr(image, "size") == (112, 112)
    assert getattr(image, "mode") == "RGB"
    assert _near_color(image.getpixel((56, 56)), (50, 120, 210))


def test_streamdock_n4pro_key_images_include_cached_status_bindings() -> None:
    """N4 Pro renderer 应把 quota/usage 状态键转成可缓存静态按键图。

    入参：无；测试内构造 quota_status 和 usage_summary 布局。
    返回：无返回值；断言通过代表状态键图片会进入 key_images，且相同输入复用缓存图像。
    错误处理：缺少图片、尺寸不对或缓存未命中时由 pytest 报告。
    副作用：只创建内存图片，不访问真实 N4 Pro 或用户配置。
    """

    key_layout = N4ProKeyLayout(
        keys=(
            N4ProKeyBinding(
                index=0,
                kind=KeySurfaceKind.QUOTA_STATUS,
                quota_window="primary",
            ),
            N4ProKeyBinding(
                index=1,
                kind=KeySurfaceKind.USAGE_SUMMARY,
                usage_period="week",
            ),
            *default_n4pro_key_layout().sorted_keys()[2:],
        )
    )
    layout = build_layout_plan(
        [],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
        key_layout=key_layout,
    )
    cache = server_app.StatusKeyImageCache()

    first = server_app._key_images_from_layout(
        layout,
        quota_snapshot=_quota_snapshot(),
        token_usage_snapshot=_token_snapshot(),
        status_key_cache=cache,
    )
    second = server_app._key_images_from_layout(
        layout,
        quota_snapshot=_quota_snapshot(),
        token_usage_snapshot=_token_snapshot(),
        status_key_cache=cache,
    )

    assert sorted(first) == [1, 2]
    assert getattr(first[1], "size") == (112, 112)
    assert getattr(first[2], "size") == (112, 112)
    assert second[1] is first[1]
    assert second[2] is first[2]


def test_runtime_publishes_only_changed_static_key_images_for_hot_update() -> None:
    """状态键切换应只向 persistent renderer 发布变更的静态键图片。

    入参：无；测试配置一个 quota status 键、写入缓存 quota 快照，再把该键切到另一窗口。
    返回：无返回值；断言通过表示输入路径只构建缓存图选择和一键差异，不等待下一次全量 renderer。
    错误处理：revision 未递增、差异包含无关键或状态键缓存未复用时由 pytest 报告。
    副作用：只修改测试 app 的进程内 runtime，不访问真实 N4 Pro 或用户配置。
    """

    key_layout = N4ProKeyLayout(
        keys=(
            N4ProKeyBinding(
                index=0,
                kind=KeySurfaceKind.QUOTA_STATUS,
                quota_window="auto",
            ),
            *default_n4pro_key_layout().sorted_keys()[1:],
        )
    )
    app = create_app()
    runtime = app.state.runtime
    runtime.key_layout = key_layout
    runtime.update_codex_quota(_quota_snapshot(), updated_at=datetime.now(UTC))

    initial_revision, initial_images = runtime.current_hardware_key_surface_images()
    assert initial_revision == 1
    assert sorted(initial_images) == [1]

    assert runtime._replace_key_binding(
        0,
        kind="quota_status",
        update={"quota_window": "secondary"},
    )
    layout = runtime.render_current()
    runtime.prewarm_status_key_images(layout)
    runtime.publish_hardware_key_surface_images(layout)
    updated_revision, updated_images = runtime.current_hardware_key_surface_images()

    assert updated_revision == initial_revision + 1
    assert sorted(updated_images) == [1]
    assert updated_images[1] is not initial_images[1]
    assert runtime.status_key_image_cache.diagnostics()["hits"] >= 1


def test_hardware_status_key_press_cycles_quota_window_and_usage_period() -> None:
    """按下状态型主键应切换当前展示窗口/周期，而不是执行外部动作。

    入参：无；测试内保存状态型按键布局并发送 fake hardware key press。
    返回：无返回值；断言通过代表 runtime key layout 被更新，后续渲染可从缓存取新图。
    错误处理：按键 intent 未处理、周期未切换或被当成 dry-run 时由 pytest 报告。
    副作用：只修改测试 app 内存状态，不写真实用户配置、不访问硬件。
    """

    client = TestClient(create_app())
    layout_body = {
        "keys": [
            {"index": 0, "kind": "quota_status", "quota_window": "primary"},
            {"index": 1, "kind": "usage_summary", "usage_period": "today"},
            {"index": 2, "kind": "unassigned"},
            {"index": 3, "kind": "unassigned"},
            {"index": 4, "kind": "unassigned"},
            {"index": 5, "kind": "agent"},
            {"index": 6, "kind": "agent"},
            {"index": 7, "kind": "agent"},
            {"index": 8, "kind": "agent"},
            {"index": 9, "kind": "agent"},
        ]
    }

    save_response = client.put("/ui/key-layout", json=layout_body)
    quota_response = client.post(
        "/hardware/input",
        json=_hardware_input(kind="key", index=0, value={"state": 1}),
    )
    usage_response = client.post(
        "/hardware/input",
        json=_hardware_input(kind="key", index=1, value={"state": 1}),
    )
    status = client.get("/status").json()

    assert save_response.status_code == 200
    assert quota_response.json()["action"]["status"] == "cycled"
    assert quota_response.json()["action"]["quota_window"] == "auto"
    assert usage_response.json()["action"]["status"] == "cycled"
    assert usage_response.json()["action"]["usage_period"] == "week"
    assert status["key_layout"]["layout"]["keys"][0]["quota_window"] == "auto"
    assert status["key_layout"]["layout"]["keys"][1]["usage_period"] == "week"


def test_status_key_cycles_all_actual_quota_window_ids() -> None:
    """多窗口 quota 快照应让状态键在真实 window_id 间循环，而非固定两个槽位。

    入参：无；启动带双窗口 fake quota poller 的 daemon，并连续按下 quota 状态键。
    返回：无返回值；断言通过代表运行时会把 stable window_id 写回暂存布局。
    错误处理：窗口漏入循环、顺序不稳定或混入 `auto` 别名时由 pytest 报告。
    副作用：仅测试内存 daemon 和 fake quota reader，不写用户配置或访问硬件。
    """

    app = create_app(
        poller_config=DaemonPollerConfig(codex_quota_enabled=True),
        codex_quota_reader=lambda **_kwargs: _quota_snapshot(),
    )
    with TestClient(app) as client:
        client.put(
            "/ui/key-layout",
            json={
                "keys": [
                    {"index": 0, "kind": "quota_status", "quota_window": "auto"},
                    *({"index": index, "kind": "unassigned"} for index in range(1, 5)),
                    *({"index": index, "kind": "agent"} for index in range(5, 10)),
                ]
            },
        )
        windows = [
            client.post(
                "/hardware/input",
                json=_hardware_input(kind="key", index=0, value={"state": 1}),
            ).json()["action"]["quota_window"]
            for _ in range(3)
        ]

    assert windows == ["codex:secondary", "codex:primary", "codex:secondary"]


def test_quota_status_key_cycle_skips_auto_alias_between_visible_windows() -> None:
    """多窗口 quota 状态键不得在真实周期之间插入无视觉变化的 `auto`。

    入参：双窗口 quota 快照，以及已显式选中的常规与 Spark 周限 window_id。
    返回：无返回值；断言通过代表每次按下都会切换到另一个实际展示窗口。
    错误处理：返回 `auto` 或没有切换到另一窗口时由 pytest 报告。
    副作用：无；只调用纯窗口选择 helper。
    """

    snapshot = _quota_snapshot()

    assert (
        server_app._next_quota_status_window("codex:primary", snapshot=snapshot)
        == "codex:secondary"
    )
    assert (
        server_app._next_quota_status_window("codex:secondary", snapshot=snapshot)
        == "codex:primary"
    )
    assert (
        server_app._next_quota_status_window("auto", snapshot=snapshot)
        == "codex:secondary"
    )


def test_quota_presentation_controls_displayed_windows_and_status_key_cycle(
    tmp_path: Path,
) -> None:
    """独立 quota 策略应重排、标记和隐藏硬件展示窗口，而原始状态保持完整。

    入参：`tmp_path` 提供隔离策略路径；测试快照含 Codex、Spark 与隐藏的未来 limit。
    返回：无返回值；断言通过代表主键轮换只消费展示集合，而 status 仍保留采集集合。
    错误处理：策略未加载、展示标签不生效或隐藏窗口进入循环时由 pytest 报告。
    副作用：仅在 pytest 临时目录写策略 JSON，并启动内存 FastAPI app。
    """

    policy_path = tmp_path / "quota-presentation.json"
    save_quota_presentation(
        QuotaPresentation(
            rules=(
                QuotaPresentationRule(limit_id="codex_spark", label="Spark", order=0),
                QuotaPresentationRule(limit_id="codex", label="Codex", order=10),
                QuotaPresentationRule(limit_id="future", visible=False),
            )
        ),
        policy_path,
    )
    snapshot = CodexQuotaSnapshot(
        plan_type="pro",
        plan_display_name="Pro",
        windows=(
            {
                "window_id": "codex:primary",
                "limit_id": "codex",
                "used_percent": 10,
                "window_duration_mins": 10080,
                "resets_at": datetime(2026, 7, 20, 10, tzinfo=UTC),
            },
            {
                "window_id": "codex_spark:primary",
                "limit_id": "codex_spark",
                "limit_name": "GPT-5.3-Codex-Spark",
                "used_percent": 20,
                "window_duration_mins": 10080,
                "resets_at": datetime(2026, 7, 20, 10, tzinfo=UTC),
            },
            {
                "window_id": "future:primary",
                "limit_id": "future",
                "used_percent": 30,
                "window_duration_mins": 43200,
                "resets_at": datetime(2026, 8, 1, 10, tzinfo=UTC),
            },
        ),
    )
    app = create_app(
        poller_config=DaemonPollerConfig(codex_quota_enabled=True),
        codex_quota_reader=lambda **_kwargs: snapshot,
        quota_presentation_path=policy_path,
    )
    with TestClient(app) as client:
        client.put(
            "/ui/key-layout",
            json={
                "keys": [
                    {"index": 0, "kind": "quota_status", "quota_window": "auto"},
                    *({"index": index, "kind": "unassigned"} for index in range(1, 5)),
                    *({"index": index, "kind": "agent"} for index in range(5, 10)),
                ]
            },
        )
        status = client.get("/status").json()
        windows = [
            client.post(
                "/hardware/input",
                json=_hardware_input(kind="key", index=0, value={"state": 1}),
            ).json()["action"]["quota_window"]
            for _ in range(3)
        ]

    assert [item["window_id"] for item in status["codex_quota"]["snapshot"]["windows"]] == [
        "codex:primary",
        "codex_spark:primary",
        "future:primary",
    ]
    assert [
        item["window_id"] for item in status["codex_quota"]["display_snapshot"]["windows"]
    ] == ["codex_spark:primary", "codex:primary"]
    assert status["codex_quota"]["display_snapshot"]["windows"][0]["presentation_label"] == "Spark"
    assert windows == ["codex:primary", "codex_spark:primary", "codex:primary"]


def test_single_quota_window_keeps_status_key_in_auto_mode() -> None:
    """单窗口订阅按下 quota 状态键不应在等价的 auto/primary 间伪切换。

    入参：无；测试构造只有 primary 的 quota 快照。
    返回：无返回值；断言通过代表服务端策略改变后状态键仍保留直观交互。
    错误处理：缺失 secondary 被加入循环或 stale 配置未被归位时由 pytest 报告。
    副作用：无；只调用纯窗口选择 helper。
    """

    snapshot = CodexQuotaSnapshot(
        plan_type="free",
        plan_display_name="Free",
        primary={
            "used_percent": 42,
            "window_duration_mins": 43200,
            "resets_at": datetime(2026, 8, 1, 8, tzinfo=UTC),
        },
        secondary=None,
    )

    assert server_app._next_quota_status_window("auto", snapshot=snapshot) == "auto"
    assert server_app._next_quota_status_window("secondary", snapshot=snapshot) == "auto"


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
    visible_splash_calls: list[object] = []

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

    def fake_visible_splash_sink(
        image: object,
    ) -> StreamDockTouchscreenRenderResult:
        """记录 dual-device 可见层 splash 调用。

        入参：`image` 是默认 splash 背景图。
        返回：固定成功结果。
        错误处理：无。
        副作用：追加调用记录，不访问硬件。
        """

        visible_splash_calls.append(image)
        return StreamDockTouchscreenRenderResult(
            ok=True,
            background_api="set_touchscreen_image",
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
        visible_splash_touchscreen_sink=fake_visible_splash_sink,
    )
    with TestClient(app) as client:
        status = client.get("/status").json()

    assert quota_touchscreen_calls == []
    assert renderer_calls
    call = renderer_calls[0]
    assert getattr(call["background_image"], "size") == (800, 480)
    assert call["key_frame_paths"] == {6: (frame_path.resolve(),)}
    assert call["key_images"] == {}
    assert call["duration_seconds"] == 0.5
    assert call["fps"] == 4
    assert len(visible_splash_calls) == 2
    assert getattr(visible_splash_calls[0], "size") == (800, 480)
    assert getattr(visible_splash_calls[-1], "size") == (800, 480)
    assert visible_splash_calls[-1].getpixel((82, 408)) != (14, 18, 28)
    assert status["streamdock_n4pro_renderer"]["last_result"] == {
        "background_result": None,
        "device_type": "FakeN4ProDevice",
        "error": None,
        "frames_rendered": 2,
        "key_count": 1,
        "ok": True,
        "path": "n4pro-path",
        "session_output_error": None,
        "timing_seconds": {},
    }
    assert status["streamdock_n4pro_renderer"]["last_error"] is None
    assert status["codex_quota"]["streamdock_touchscreen"] is None


def test_streamdock_n4pro_renderer_starts_with_placeholder_without_panel_data(
    tmp_path: Path,
) -> None:
    """Verify N4 Pro renderer starts and exits with branded splash before panel data.

    入参：`tmp_path` 提供 fake frame root。
    返回：无返回值；断言通过代表真实 renderer 会用 Agent Deck 默认图接管残留显示，
    并在 daemon 退出前再写一次默认图。
    错误处理：renderer 未被调用或背景缺失时由 pytest 报告。
    副作用：只调用 fake renderer，不访问真实 N4 Pro。
    """

    renderer_calls: list[dict[str, object]] = []
    visible_splash_calls: list[object] = []

    def fake_n4pro_renderer(**kwargs: object) -> StreamDockN4ProAnimationResult:
        """记录 renderer 调用并返回成功结果。

        入参：`kwargs` 包含背景图、按键帧、播放时长和 fps。
        返回：固定成功结果。
        错误处理：无。
        副作用：追加调用记录。
        """

        renderer_calls.append(kwargs)
        return StreamDockN4ProAnimationResult(ok=True)

    def fake_visible_splash_sink(
        image: object,
    ) -> StreamDockTouchscreenRenderResult:
        """记录 dual-device 可见层 splash 调用。

        入参：`image` 是默认 splash 背景图。
        返回：固定成功结果。
        错误处理：无。
        副作用：追加调用记录。
        """

        visible_splash_calls.append(image)
        return StreamDockTouchscreenRenderResult(
            ok=True,
            background_api="set_touchscreen_image",
        )

    app = create_app(
        poller_config=DaemonPollerConfig(
            streamdock_n4pro_renderer_enabled=True,
            streamdock_n4pro_frame_root=tmp_path,
            streamdock_n4pro_render_interval_seconds=0.5,
        ),
        streamdock_n4pro_renderer_sink=fake_n4pro_renderer,
        visible_splash_touchscreen_sink=fake_visible_splash_sink,
    )
    with TestClient(app) as client:
        status = client.get("/status").json()

    assert renderer_calls
    call = renderer_calls[0]
    background_image = call["background_image"]
    assert getattr(background_image, "size") == (800, 480)
    assert background_image.getpixel((400, 120)) == (14, 18, 28)
    assert background_image.getpixel((82, 408)) != (14, 18, 28)
    assert call["key_frame_paths"] == {}
    assert call["key_images"] == {}
    assert len(visible_splash_calls) == 2
    shutdown_background_image = visible_splash_calls[-1]
    assert getattr(shutdown_background_image, "size") == (800, 480)
    assert shutdown_background_image.getpixel((82, 408)) != (14, 18, 28)
    assert status["streamdock_n4pro_renderer"]["last_error"] is None
    assert status["streamdock_n4pro_renderer"]["last_result"]["ok"] is True


def test_shutdown_waits_for_inflight_renderer_and_pet_workers(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """daemon 退出必须等待已启动的 renderer/pet 线程后再关闭共享资源。

    入参：``monkeypatch`` 注入可观测临时目录；``tmp_path`` 承载假缓存和帧目录。
    返回：无；断言退出 splash、renderer close 与缓存 cleanup 都晚于两个同步 worker。
    错误处理：Event 等待均有界，任一生命周期阶段未发生或顺序错误由 pytest 报告。
    副作用：启动一个 TestClient 生命周期线程和两个 asyncio 默认线程池 worker；不访问真机。
    """

    order: list[str] = []
    renderer_started = Event()
    renderer_finished = Event()
    pet_started = Event()
    pet_finished = Event()
    release_workers = Event()
    client_ready = Event()
    client_body_finished = Event()
    renderer_closed = Event()
    pet_cache_cleaned = Event()
    lifecycle_errors: list[BaseException] = []

    class TrackingTemporaryDirectory:
        """记录宠物临时缓存 cleanup 的测试替身。"""

        def __init__(self, *, prefix: str) -> None:
            """创建隔离缓存目录并保留与 TemporaryDirectory 相同的 name 合同。

            入参：``prefix`` 是生产代码传入的目录前缀。
            返回：无。
            错误处理：目录创建失败按原异常传播。
            副作用：在 pytest 临时目录创建一个目录。
            """

            assert prefix == "agent-deck-codex-pet-"
            self.name = str(tmp_path / "tracked-pet-cache")
            Path(self.name).mkdir()

        def cleanup(self) -> None:
            """记录 cleanup，并要求两个共享资源使用者都已结束。

            入参：无。
            返回：无。
            错误处理：若任一 worker 尚未结束则直接断言失败。
            副作用：追加顺序记录并设置 cleanup Event；不删除 pytest 目录。
            """

            order.append("pet_cache_cleanup")
            pet_cache_cleaned.set()
            assert renderer_finished.is_set()
            assert pet_finished.is_set()

    class BlockingRenderer:
        """模拟会跨越 lifespan cancel 时刻的 persistent renderer。"""

        def __call__(self, **_: object) -> StreamDockN4ProAnimationResult:
            """阻塞到测试放行，并记录真实同步工作结束时刻。

            入参：忽略 renderer 背景、按键与帧率参数。
            返回：固定成功结果。
            错误处理：两秒内未放行时断言失败，避免测试挂死。
            副作用：设置开始/结束 Event 并追加顺序记录。
            """

            renderer_started.set()
            assert release_workers.wait(2.0)
            order.append("renderer_finished")
            renderer_finished.set()
            return StreamDockN4ProAnimationResult(ok=True)

        def close(self) -> None:
            """记录 renderer close，并要求所有 in-flight worker 已结束。

            入参：无。
            返回：无。
            错误处理：worker 未结束时断言失败。
            副作用：追加顺序记录并设置 close Event。
            """

            order.append("renderer_close")
            renderer_closed.set()
            assert renderer_finished.is_set()
            assert pet_finished.is_set()

    splash_call_count = 0

    def fake_visible_splash_sink(
        _image: object,
    ) -> StreamDockTouchscreenRenderResult:
        """区分启动和退出 splash，并记录二者调用顺序。

        入参：``_image`` 是品牌背景，本测试不读取像素。
        返回：固定成功结果。
        错误处理：无。
        副作用：递增调用计数并追加顺序记录。
        """

        nonlocal splash_call_count
        splash_call_count += 1
        order.append("startup_splash" if splash_call_count == 1 else "exit_splash")
        return StreamDockTouchscreenRenderResult(ok=True)

    monkeypatch.setattr(server_app, "TemporaryDirectory", TrackingTemporaryDirectory)
    renderer = BlockingRenderer()
    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_pet_enabled=True,
            codex_pet_refresh_interval_seconds=0.01,
            streamdock_n4pro_renderer_enabled=True,
            streamdock_n4pro_render_interval_seconds=0.01,
            streamdock_n4pro_frame_root=tmp_path,
            poll_on_start=False,
        ),
        streamdock_n4pro_renderer_sink=renderer,
        visible_splash_touchscreen_sink=fake_visible_splash_sink,
    )

    def blocking_pet_refresh(*, now: datetime) -> None:
        """模拟会写 daemon 临时缓存的慢速宠物刷新。

        入参：``now`` 是 poller 传入的 aware 时间，仅验证其类型。
        返回：无。
        错误处理：两秒内未放行时断言失败。
        副作用：设置开始/结束 Event 并追加顺序记录。
        """

        assert now.tzinfo is not None
        pet_started.set()
        assert release_workers.wait(2.0)
        order.append("pet_finished")
        pet_finished.set()

    monkeypatch.setattr(app.state.runtime.codex_pet, "refresh", blocking_pet_refresh)

    def run_client_lifespan() -> None:
        """在独立线程进入并主动退出 TestClient lifespan。

        入参：无。
        返回：无。
        错误处理：捕获所有 lifecycle 异常交由主测试线程断言。
        副作用：启动 FastAPI lifespan，并在两个 worker 开始后触发 shutdown。
        """

        try:
            with TestClient(app):
                client_ready.set()
                assert renderer_started.wait(1.0)
                assert pet_started.wait(1.0)
                client_body_finished.set()
        except BaseException as exc:  # noqa: BLE001 - 跨线程转交给 pytest 主线程。
            lifecycle_errors.append(exc)

    lifespan_thread = Thread(target=run_client_lifespan)
    lifespan_thread.start()
    assert client_ready.wait(1.0)
    assert renderer_started.wait(1.0)
    assert pet_started.wait(1.0)
    assert client_body_finished.wait(1.0)

    assert not renderer_closed.wait(0.2)
    assert not pet_cache_cleaned.is_set()

    release_workers.set()
    lifespan_thread.join(timeout=3.0)

    assert not lifespan_thread.is_alive()
    assert lifecycle_errors == []
    assert renderer_finished.is_set()
    assert pet_finished.is_set()
    assert renderer_closed.is_set()
    assert pet_cache_cleaned.is_set()
    assert splash_call_count == 2
    assert order.index("renderer_finished") < order.index("exit_splash")
    assert order.index("pet_finished") < order.index("exit_splash")
    assert order[-3:] == ["exit_splash", "renderer_close", "pet_cache_cleanup"]


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
        visible_splash_touchscreen_sink=_fake_visible_splash_sink,
    )
    with TestClient(app) as client:
        input_callback = captured["input_callback"]
        assert callable(input_callback)
        input_callback(
            object(),
            _sdk_event(event_type="touch_point", x=658, y=94),
        )
        input_callback(
            object(),
            _sdk_event(event_type="touch_point", x=657, y=91),
        )
        first_knob_response = input_callback(
            object(),
            _sdk_event(
                event_type="knob_rotate",
                knob_id="knob_4",
                direction="right",
            ),
        )
        second_knob_response = input_callback(
            object(),
            _sdk_event(
                event_type="knob_rotate",
                knob_id="knob_4",
                direction="right",
            ),
        )
        status = client.get("/status").json()

    assert first_knob_response["handled"] is True
    assert second_knob_response["handled"] is True
    assert status["logical_panel"]["selection"]["active_kind"] == "quota"
    assert status["logical_panel"]["selection"]["quota_window"] == "auto"
    assert status["streamdock_input"]["event_count"] == 4
    assert status["streamdock_input"]["last_event"] == {
        "count": 4,
        "event_type": "knob_rotate",
        "key": None,
        "knob_id": "knob_4",
        "direction": "right",
        "state": None,
        "x": None,
        "y": None,
        "panel_event": None,
        "handled": True,
        "debounced": False,
        "accumulated": False,
        "knob4_rotate_accumulator": 0,
    }


def test_default_n4pro_renderer_input_callback_routes_button_intents(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """默认 N4 Pro renderer callback 应把 SDK agent button 路由为 selection intent。

    入参：`monkeypatch` 替换 persistent animator；`tmp_path` 提供无真实访问的 frame root。
    返回：无返回值；断言通过代表真实 key callback 可以复用当前 layout 的 agent key 语义。
    错误处理：callback 未注入、button 编号或 dry-run 记录错误时由 pytest 报告。
    副作用：只修改测试 app 内存 runtime，不访问真实 N4 Pro 或 macOS 窗口系统。
    """

    captured: dict[str, object] = {}

    class FakePersistentAnimator:
        """捕获 daemon 传入的 input callback。

        入参：`input_callback` 是待测 callback。
        返回：fake renderer。
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
            streamdock_n4pro_renderer_enabled=True,
            streamdock_n4pro_frame_root=tmp_path,
        ),
        visible_splash_touchscreen_sink=_fake_visible_splash_sink,
    )
    with TestClient(app) as client:
        client.post("/events", json=_event("session-1").model_dump(mode="json"))
        client.get("/status")
        input_callback = captured["input_callback"]
        assert callable(input_callback)
        response = input_callback(
            object(),
            _sdk_event(event_type="button", key=16, state=1),
        )
        release_response = input_callback(
            object(),
            _sdk_event(event_type="button", key=16, state=0),
        )
        status = client.get("/status").json()

    assert response["handled"] is True
    assert response["interaction_intent"]["intent"] == "select_agent"
    assert response["action"]["status"] == "missing_target"
    assert release_response["handled"] is False
    assert status["interaction"]["last_intent"]["source"] == "streamdock_button"
    assert status["interaction"]["last_action"]["intent"] == "focus_agent"
    assert status["streamdock_input"]["recent_events"][-2]["key"] == 16
    assert status["streamdock_input"]["recent_events"][-2]["state"] == 1
    assert status["streamdock_input"]["recent_events"][-2]["handled"] is True
    assert status["streamdock_input"]["recent_events"][-1]["key"] == 16
    assert status["streamdock_input"]["recent_events"][-1]["state"] == 0
    assert status["streamdock_input"]["recent_events"][-1]["handled"] is False
    assert status["interaction"]["recent"][-1]["intent"]["intent"] == "select_agent"
    assert status["interaction"]["recent"][-1]["action"]["intent"] == "focus_agent"


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
    assert body["logical_panel"]["selection"]["active_kind"] == "brand"
    assert body["logical_panel"]["effective_kind"] == "message"
    assert body["logical_panel"]["touchscreen_image_source"] == "decision_message"


def test_hardware_approval_key_resolves_decision_and_restores_manual_panel() -> None:
    """硬件 allow key 应 resolve 当前 decision 并恢复原人工面板。

    入参：无；测试内创建 agent 和 pending decision，再按 decision mode 的 ALLOW 键。
    返回：无返回值；断言通过代表硬件审批闭环能从 layout key intent 写入 broker result。
    错误处理：decision 未 resolve、hook wait 结果错误或 panel 未回到原选择时由 pytest 报告。
    副作用：只修改测试 app 内存 broker/state，不访问真实硬件或 Codex。
    """

    client = TestClient(create_app())
    client.post("/events", json=_event("session-1").model_dump(mode="json"))
    decision_id = _request_decision(client)

    response = client.post(
        "/hardware/input",
        json=_hardware_input(
            kind="key",
            index=10,
            value={"state": 1},
        ),
    )
    wait_response = client.get(
        f"/decisions/{decision_id}/wait",
        params={"timeout_seconds": 0.001},
    )
    status = client.get("/status").json()

    assert response.status_code == 200
    assert response.json()["handled"] is True
    assert response.json()["interaction_intent"]["intent"] == "approve_request"
    assert response.json()["action"] == {
        "intent": "approve_request",
        "agent_key": "codex:session-1",
        "decision_id": decision_id,
        "status": "resolved",
        "ok": True,
        "behavior": "allow",
        "message": "Approved by Agent Deck hardware.",
    }
    assert wait_response.status_code == 200
    assert wait_response.json() == {
        "behavior": "allow",
        "message": "Approved by Agent Deck hardware.",
    }
    assert status["decisions"] == []
    assert status["agents"][0]["pending_decision_count"] == 0
    assert status["logical_panel"]["selection"]["active_kind"] == "brand"
    assert status["logical_panel"]["effective_kind"] == "brand"
    assert status["logical_panel"]["touchscreen_image_source"] == "agent_deck:splash"


def test_hardware_deny_key_resolves_decision() -> None:
    """硬件 deny key 应把当前 decision resolve 为 deny。

    入参：无；测试内创建 pending decision，再按 decision mode 的 DENY 键。
    返回：无返回值；断言通过代表 deny 路径同样通过 layout key intent 驱动 broker。
    错误处理：intent、behavior 或 wait 结果错误时由 pytest 报告。
    副作用：只修改测试 app 内存 broker/state。
    """

    client = TestClient(create_app())
    client.post("/events", json=_event("session-1").model_dump(mode="json"))
    decision_id = _request_decision(client)

    response = client.post(
        "/hardware/input",
        json=_hardware_input(
            kind="key",
            index=11,
            value={"state": 1},
        ),
    )
    wait_response = client.get(
        f"/decisions/{decision_id}/wait",
        params={"timeout_seconds": 0.001},
    )

    assert response.status_code == 200
    assert response.json()["interaction_intent"]["intent"] == "deny_request"
    assert response.json()["action"]["behavior"] == "deny"
    assert response.json()["action"]["message"] == "Denied by Agent Deck hardware."
    assert wait_response.json() == {
        "behavior": "deny",
        "message": "Denied by Agent Deck hardware.",
    }


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


def _fake_visible_splash_sink(
    _image: object,
) -> StreamDockTouchscreenRenderResult:
    """模拟 dual-device 启停画面下发，隔离测试与真实 HID 设备。

    入参：`_image` 是 daemon 生成的启动或退出画面，本 helper 不检查其内容。
    返回：固定成功的触屏渲染结果。
    错误处理：无。
    副作用：无；不会枚举、打开或写入真实 StreamDock 设备。
    """

    return StreamDockTouchscreenRenderResult(ok=True)


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


def _fake_finder_app(tmp_path: Path) -> Path:
    """创建测试用 fake Finder `.app` bundle。

    入参：`tmp_path` 是 fake 应用根目录。
    返回：fake Finder bundle 路径。
    错误处理：文件写入失败按 pathlib/Pillow 异常传播。
    副作用：写 pytest 临时目录。
    """

    app = tmp_path / "Finder.app"
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleName": "Finder",
                "CFBundleIdentifier": "com.apple.finder",
                "CFBundleIconFile": "Finder.png",
            },
            handle,
        )
    Image.new("RGBA", (64, 64), (20, 120, 220, 255)).save(resources / "Finder.png")
    return app


def _fake_site_fetcher(calls: list[str]):
    """构造记录请求 URL 的 fake 站点 fetcher。

    入参：`calls` 是测试内存列表。
    返回：可注入 `create_app()` 或 `UrlIconCache` 的 fetcher。
    错误处理：无。
    副作用：写入 `calls`。
    """

    def fetcher(url: str) -> bytes:
        """返回 HTML 或 32x32 PNG favicon。

        入参：`url` 是 cache 访问的首页、manifest 或 icon URL。
        返回：HTML 或 PNG bytes。
        错误处理：无。
        副作用：写入 `calls`。
        """

        calls.append(url)
        if url == "https://example.com":
            return b'<html><head><link rel="icon" sizes="32x32" href="/assets/icon.png"></head></html>'
        if url == "https://example.com/assets/icon.png":
            return _png_bytes((32, 32), (50, 120, 210, 255))
        return b""

    return fetcher


def _png_bytes(size: tuple[int, int], color: tuple[int, int, int, int]) -> bytes:
    """生成测试 PNG bytes。

    入参：`size` 是图片尺寸；`color` 是 RGBA 填充色。
    返回：PNG bytes。
    错误处理：无。
    副作用：只写内存 buffer。
    """

    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _near_color(
    actual: tuple[int, int, int],
    expected: tuple[int, int, int],
    *,
    tolerance: int = 24,
) -> bool:
    """判断采样像素是否接近目标 RGB。

    入参：`actual` 是采样像素；`expected` 是目标颜色；`tolerance` 是每通道容差。
    返回：三通道都在容差内时返回 True。
    错误处理：无。
    副作用：无。
    """

    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected, strict=True))


def _key_index_for_agent(status: dict[str, object], agent_key: str) -> int:
    """从 status layout 中查找指定 agent 当前所在键位。

    入参：`status` 是 `/status` JSON；`agent_key` 是目标 agent key。
    返回：匹配 key plan 的 index。
    错误处理：找不到时让测试失败。
    副作用：无。
    """

    layout = status["layout"]
    assert isinstance(layout, dict)
    keys = layout["keys"]
    assert isinstance(keys, list)
    for key in keys:
        assert isinstance(key, dict)
        if key.get("agent_key") == agent_key:
            key_index = key.get("index")
            assert isinstance(key_index, int)
            return key_index
    raise AssertionError(f"missing key for {agent_key}")


def _sdk_event(
    *,
    event_type: str,
    key: int | None = None,
    knob_id: str | None = None,
    direction: str | None = None,
    state: int | None = None,
    x: int | None = None,
    y: int | None = None,
) -> object:
    """构造 SDK-like InputEvent 测试替身。

    入参：`event_type` 是 SDK event type；`key`、`knob_id`、`direction`、`state`、`x`、`y`
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
    event.key = ValueObject(key) if key is not None else None
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
