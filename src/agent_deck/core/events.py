"""Normalized event contracts for Agent Deck core state.

This module defines the in-memory Pydantic model used to carry agent lifecycle,
tool, approval, input, subagent, error, and heartbeat events through later
reducers and renderers. It does not parse vendor-specific streams, reduce state,
choose hardware layout, start servers, access devices, read files, write files,
or perform network I/O; importing it is intentionally side-effect free.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, Field

_SENSITIVE_KEY_MARKERS = frozenset(
    {"token", "secret", "authorization", "api_key", "apikey", "password"}
)
_REDACTED_VALUE = "[REDACTED]"


class AgentSource(StrEnum):
    """Identify the upstream agent product that emitted an event.

    入参：枚举成员值是规范化后的 source 字符串，通常来自 adapter 或 hook 层。
    返回：作为字符串枚举参与 Pydantic 校验、序列化和 event id 构造。
    错误处理：未知 source 由 Pydantic/Enum 校验为非法值并报告。
    副作用：无；定义枚举不会访问网络、硬件、文件或全局运行状态。
    """

    CODEX = "codex"
    CLAUDE_CODE = "claude-code"
    GENERIC = "generic"


class EventType(StrEnum):
    """Represent Agent Deck's normalized event taxonomy.

    入参：枚举成员值是跨 agent adapter 共用的 normalized event type 字符串。
    返回：作为字符串枚举约束 `NormalizedEvent.normalized_type`。
    错误处理：未知事件类型由 Pydantic/Enum 校验为非法值并报告。
    副作用：无；仅声明稳定分类，不读取外部状态或触发 I/O。
    """

    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    APPROVAL_REQUESTED = "approval.requested"
    INPUT_REQUESTED = "input.requested"
    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_COMPLETED = "subagent.completed"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


class NormalizedEvent(BaseModel):
    """Carry one normalized, redacted event through the Agent Deck pipeline.

    入参：字段覆盖来源、原始事件类型、规范事件类型、agent/session/thread/turn
    关联信息、工作目录、展示标题、工具名、严重级别、摘要、payload 以及发生/接收时间；
    `event_id` 应由 `build` 生成以保持确定性。
    返回：Pydantic model 实例，可被后续 reducer 或 renderer 读取。
    错误处理：字段类型或枚举值不合法时由 Pydantic 抛出校验异常。
    副作用：仅保存内存数据；模型实例化不访问网络、硬件或文件系统。
    """

    event_id: str
    source: AgentSource
    source_event_type: str
    normalized_type: EventType
    agent_id: str | None = None
    session_id: str
    thread_id: str | None = None
    turn_id: str | None = None
    cwd: str | None = None
    title: str | None = None
    tool_name: str | None = None
    severity: str | None = None
    summary: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
    received_at: datetime

    @property
    def agent_key(self) -> str:
        """Return the stable grouping key for one agent session.

        入参：无；读取当前事件的 `source` 与 `session_id` 字段。
        返回：形如 `"{source}:{session_id}"` 的字符串，用于后续状态聚合。
        错误处理：不主动抛出业务异常；字段缺失或非法会先由模型校验阻止。
        副作用：无；只进行字符串拼接，不触发外部 I/O 或修改对象状态。
        """

        return f"{self.source.value}:{self.session_id}"

    @classmethod
    def build(
        cls,
        *,
        source: AgentSource,
        source_event_type: str,
        normalized_type: EventType,
        session_id: str,
        occurred_at: datetime,
        payload: dict[str, Any] | None = None,
        received_at: datetime | None = None,
        agent_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        cwd: str | None = None,
        title: str | None = None,
        tool_name: str | None = None,
        severity: str | None = None,
        summary: str | None = None,
    ) -> Self:
        """Construct a normalized event with stable identity and safe payload.

        入参：`source` 是上游 agent 枚举；`source_event_type` 是原始事件名；
        `normalized_type` 是跨 agent 分类；`session_id` 与 `occurred_at` 参与
        deterministic `event_id`；`payload` 可为空并会递归脱敏；其余可选字段是
        后续 UI 与状态聚合所需上下文；`received_at` 为空时使用当前 UTC 时间。
        返回：完成 Pydantic 校验的 `NormalizedEvent` 实例。
        错误处理：非法枚举、字段类型不匹配或 Pydantic 校验失败会向调用方抛出异常。
        副作用：除读取当前系统时间作为默认 `received_at` 外，不访问网络、硬件或文件。
        """

        event_id = (
            f"{source.value}:{source_event_type}:{session_id}:{occurred_at.isoformat()}"
        )

        return cls(
            event_id=event_id,
            source=source,
            source_event_type=source_event_type,
            normalized_type=normalized_type,
            agent_id=agent_id,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            cwd=cwd,
            title=title,
            tool_name=tool_name,
            severity=severity,
            summary=summary,
            payload=redact_payload(payload or {}),
            occurred_at=occurred_at,
            received_at=received_at or datetime.now(UTC),
        )


def redact_payload(value: Any) -> Any:
    """Recursively redact sensitive keys from JSON-like payload values.

    入参：任意 Python 值；dict 会按 key 判断是否敏感，list 会逐项递归处理，
    其他标量或对象保持原值返回。敏感 key 标记包含 token、secret、authorization、
    api_key、apikey、password，匹配时不区分大小写并替换对应 value。
    返回：脱敏后的新 dict/list 结构；非容器值直接返回原值。
    错误处理：本函数不主动抛业务异常；不可哈希或非字符串 key 会按其字符串形式判断。
    副作用：不修改输入容器，不访问网络、硬件或文件系统。
    """

    if isinstance(value, dict):
        return {
            key: _REDACTED_VALUE
            if _is_sensitive_key(key)
            else redact_payload(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [redact_payload(item) for item in value]

    return value


def _is_sensitive_key(key: Any) -> bool:
    """Return whether a payload key should have its value redacted.

    入参：任意 dict key；通常是字符串，但非字符串 key 会转为字符串后判断。
    返回：布尔值；任一敏感标记出现在 key 的小写形式中即为敏感。
    错误处理：字符串转换失败会由 Python 异常向上传播，正常 payload key 不应触发。
    副作用：无；只做本地字符串比较。
    """

    key_text = str(key).lower()
    return any(marker in key_text for marker in _SENSITIVE_KEY_MARKERS)
