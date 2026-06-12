"""Tests for the local Agent Deck daemon HTTP API.

These tests define Task 7's in-process FastAPI contract only. They do not open
real sockets, probe StreamDock hardware, install hooks, read user files, or
persist configuration; their side effects are limited to TestClient requests,
local asyncio scheduling inside FastAPI, and pytest assertion reporting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from agent_deck.core.events import AgentSource, EventType, NormalizedEvent
from agent_deck.server.app import create_app


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


def _event(session_id: str) -> NormalizedEvent:
    """Build a JSON-serializable session.started event for server tests.

    入参：`session_id` 是测试 agent session id，同时作为默认 title 展示。
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
        occurred_at=occurred_at,
        received_at=occurred_at + timedelta(milliseconds=1),
    )


def _request_decision(client: TestClient) -> str:
    """Create one pending decision through the HTTP API and return its id.

    入参：`client` 是已创建且通常已有 `codex:session-1` agent 的 TestClient。
    返回：新 pending decision 的 `decision_id` 字符串。
    错误处理：非 200 响应或缺失字段会通过 assert/KeyError 由 pytest 报告。
    副作用：修改传入 client 对应 app 的内存 broker/state，并触发 fake render。
    """

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
    assert response.status_code == 200
    return response.json()["decision_id"]
