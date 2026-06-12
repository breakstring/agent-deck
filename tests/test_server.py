"""Tests for the local Agent Deck daemon HTTP API.

These tests define Task 7's in-process FastAPI contract only. They do not open
real sockets, probe StreamDock hardware, install hooks, read user files, or
persist configuration; their side effects are limited to TestClient requests,
local asyncio scheduling inside FastAPI, and pytest assertion reporting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

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
