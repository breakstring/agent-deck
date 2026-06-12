"""Tests for normalized Agent Deck event models.

These tests define the Task 2 event contract only. They do not start network
listeners, open hardware devices, read user files, or mutate persistent state;
their only side effect is normal pytest assertion reporting.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agent_deck.core.events import AgentSource, EventType, NormalizedEvent


def test_build_creates_deterministic_id_and_agent_key() -> None:
    """Verify normalized event IDs and agent keys are stable.

    入参：无；测试内构造固定 source、source event type、session id 与 UTC 时间。
    返回：无返回值；断言通过代表 `build` 的确定性 ID 与 `agent_key` 契约成立。
    错误处理：导入失败、模型构建异常或字段不匹配会由 pytest 报告。
    副作用：仅创建内存中的 Pydantic model，不访问网络、硬件或文件系统。
    """

    event = NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type="SessionStart",
        normalized_type=EventType.SESSION_STARTED,
        session_id="session-1",
        occurred_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
    )

    assert event.event_id == "codex:SessionStart:session-1:2026-06-12T08:00:00+00:00"
    assert event.agent_key == "codex:session-1"
    assert event.payload == {}


def test_build_redacts_sensitive_payload_keys_recursively() -> None:
    """Verify sensitive payload keys are redacted at every nesting level.

    入参：无；测试内传入包含 dict/list 嵌套结构的 payload。
    返回：无返回值；断言通过代表敏感 key 被递归脱敏且非敏感字段保留。
    错误处理：模型构建异常或脱敏结果不匹配会由 pytest 报告。
    副作用：仅处理内存对象，不修改传入 payload，也不触发外部 I/O。
    """

    raw_payload = {
        "user": "kenn",
        "token": "top-secret-token",
        "nested": {
            "Authorization": "Bearer hidden",
            "count": 3,
            "items": [
                {"api_key": "secret-api-key", "name": "tool-a"},
                {"password": "secret-password", "enabled": True},
            ],
        },
    }

    event = NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type="ToolCall",
        normalized_type=EventType.TOOL_STARTED,
        session_id="session-1",
        occurred_at=datetime(2026, 6, 12, 8, 1, tzinfo=UTC),
        payload=raw_payload,
    )

    assert event.payload == {
        "user": "kenn",
        "token": "[REDACTED]",
        "nested": {
            "Authorization": "[REDACTED]",
            "count": 3,
            "items": [
                {"api_key": "[REDACTED]", "name": "tool-a"},
                {"password": "[REDACTED]", "enabled": True},
            ],
        },
    }
    assert raw_payload["token"] == "top-secret-token"
    assert raw_payload["nested"]["Authorization"] == "Bearer hidden"
    assert raw_payload["nested"]["items"][0]["api_key"] == "secret-api-key"


def test_built_event_rejects_field_and_payload_mutation() -> None:
    """Verify normalized events cannot be mutated after construction.

    入参：无；测试内构造包含顶层 dict、嵌套 dict 与 list 内 dict 的 payload。
    返回：无返回值；断言通过代表模型字段和内部 payload 都保持只读。
    错误处理：未抛出 TypeError/ValidationError 或原始 payload 被修改会由 pytest 报告。
    副作用：仅尝试修改内存对象，不访问网络、硬件或文件系统。
    """

    raw_payload = {
        "token": "top-secret-token",
        "nested": {
            "Authorization": "Bearer hidden",
            "items": [{"api_key": "secret-api-key", "name": "tool-a"}],
        },
    }

    event = NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type="ToolCall",
        normalized_type=EventType.TOOL_STARTED,
        session_id="session-1",
        occurred_at=datetime(2026, 6, 12, 8, 1, tzinfo=UTC),
        payload=raw_payload,
    )

    with pytest.raises(ValidationError):
        event.session_id = "changed"

    with pytest.raises(TypeError):
        event.payload["token"] = "secret"

    with pytest.raises(TypeError):
        event.payload["nested"]["Authorization"] = "secret"

    with pytest.raises(TypeError):
        event.payload["nested"]["items"][0]["api_key"] = "secret"

    assert raw_payload == {
        "token": "top-secret-token",
        "nested": {
            "Authorization": "Bearer hidden",
            "items": [{"api_key": "secret-api-key", "name": "tool-a"}],
        },
    }


def test_build_rejects_naive_occurred_at() -> None:
    """Verify naive occurrence timestamps are rejected explicitly.

    入参：无；测试内传入没有 timezone 的 `occurred_at`。
    返回：无返回值；断言通过代表调用方必须提供明确时区。
    错误处理：若构建成功或抛出非 Pydantic ValidationError，会由 pytest 报告。
    副作用：仅构造内存对象，不访问网络、硬件或文件系统。
    """

    with pytest.raises(ValidationError):
        NormalizedEvent.build(
            source=AgentSource.CODEX,
            source_event_type="SessionStart",
            normalized_type=EventType.SESSION_STARTED,
            session_id="session-1",
            occurred_at=datetime(2026, 6, 12, 8, 0),
        )


def test_build_rejects_naive_received_at() -> None:
    """Verify naive receive timestamps are rejected explicitly.

    入参：无；测试内传入没有 timezone 的 `received_at`。
    返回：无返回值；断言通过代表调用方不能让本地时区被静默推断。
    错误处理：若构建成功或抛出非 Pydantic ValidationError，会由 pytest 报告。
    副作用：仅构造内存对象，不访问网络、硬件或文件系统。
    """

    with pytest.raises(ValidationError):
        NormalizedEvent.build(
            source=AgentSource.CODEX,
            source_event_type="SessionStart",
            normalized_type=EventType.SESSION_STARTED,
            session_id="session-1",
            occurred_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            received_at=datetime(2026, 6, 12, 8, 0),
        )


def test_build_defaults_received_at_to_timezone_aware_utc() -> None:
    """Verify `received_at` defaults to a timezone-aware UTC timestamp.

    入参：无；测试内不显式传入 `received_at`。
    返回：无返回值；断言通过代表默认接收时间可安全跨时区比较。
    错误处理：模型构建异常或时间字段不符合 UTC aware 约束会由 pytest 报告。
    副作用：仅读取当前系统时间生成字段，不访问网络、硬件或文件系统。
    """

    event = NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type="Heartbeat",
        normalized_type=EventType.HEARTBEAT,
        session_id="session-1",
        occurred_at=datetime(2026, 6, 12, 8, 2, tzinfo=UTC),
    )

    assert event.received_at.tzinfo is not None
    assert event.received_at.utcoffset() == UTC.utcoffset(event.received_at)
