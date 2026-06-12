"""Normalized event contracts for Agent Deck core state.

This module defines the in-memory Pydantic model used to carry agent lifecycle,
tool, approval, input, subagent, error, and heartbeat events through later
reducers and renderers. It does not parse vendor-specific streams, reduce state,
choose hardware layout, start servers, access devices, read files, write files,
or perform network I/O; importing it is intentionally side-effect free.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

_SENSITIVE_KEY_MARKERS = frozenset(
    {"token", "secret", "authorization", "api_key", "apikey", "password"}
)
_REDACTED_VALUE = "[REDACTED]"


class FrozenDict(Mapping[str, Any]):
    """Read-only mapping used to protect normalized event payloads.

    入参：`items` 是已经完成脱敏和递归冻结的键值映射；本类不主动重新脱敏。
    返回：实现 `Mapping[str, Any]` 的不可变视图，可用于普通只读索引和比较。
    错误处理：访问不存在的 key 会按 dict 语义抛出 KeyError；写入操作不存在并会
    由 Python 抛出 TypeError。
    副作用：构造时复制传入 mapping，之后不修改输入对象，也不访问外部 I/O。
    """

    def __init__(self, items: Mapping[str, Any] | None = None) -> None:
        """Create an immutable mapping snapshot from already-safe items.

        入参：`items` 可为空；非空时会被复制到内部 dict 中，调用方后续修改不影响本实例。
        返回：无显式返回值；初始化后的实例可作为只读 mapping 使用。
        错误处理：若 `items` 不符合 mapping 协议，底层 `dict(...)` 会抛出异常。
        副作用：仅复制内存数据，不访问网络、硬件或文件系统。
        """

        self._items = dict(items or {})

    def __getitem__(self, key: str) -> Any:
        """Return a payload value by key.

        入参：`key` 是 payload 顶层字符串键。
        返回：对应的已冻结 payload 值，嵌套 dict 为 `FrozenDict`，list 为 tuple。
        错误处理：key 不存在时抛出 KeyError。
        副作用：无；只读取内部内存快照。
        """

        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over payload keys in insertion order.

        入参：无；读取当前冻结 mapping 的键集合。
        返回：字符串 key 的迭代器，顺序与构造时复制的 dict 一致。
        错误处理：不主动抛出业务异常；迭代器错误按 Python 运行时语义传播。
        副作用：无；不修改 mapping 或外部状态。
        """

        return iter(self._items)

    def __len__(self) -> int:
        """Return the number of top-level payload keys.

        入参：无。
        返回：内部 mapping 的键数量。
        错误处理：不主动抛出业务异常。
        副作用：无；只读取内存长度。
        """

        return len(self._items)

    def __repr__(self) -> str:
        """Return a developer-readable representation.

        入参：无。
        返回：形如 `FrozenDict({...})` 的字符串，便于测试失败时定位内容。
        错误处理：若内部值的 repr 抛错则按 Python 语义传播。
        副作用：无；只格式化内存数据。
        """

        return f"{type(self).__name__}({self._items!r})"

    def __eq__(self, other: object) -> bool:
        """Compare this frozen mapping with another mapping.

        入参：`other` 可以是普通 dict、`FrozenDict` 或任意 Mapping。
        返回：内容相等时为 True；非 Mapping 类型返回 False。
        错误处理：对方 mapping 迭代失败时按 Python 语义传播。
        副作用：无；不修改任一参与比较的对象。
        """

        if not isinstance(other, Mapping):
            return False
        return _thaw_payload(self) == _thaw_payload(other)

    def __hash__(self) -> int:
        """Return a hash for use inside frozen Pydantic models.

        入参：无。
        返回：基于已冻结键值对的 hash；嵌套结构必须已由 `_freeze_payload` 转为可哈希形态。
        错误处理：若某个标量值本身不可哈希，Python 会抛出 TypeError。
        副作用：无；只读取内部键值对。
        """

        return hash(tuple(self._items.items()))


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

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

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
    payload: FrozenDict = Field(default_factory=FrozenDict)
    occurred_at: datetime
    received_at: datetime

    @field_serializer("payload")
    def _serialize_payload(self, value: FrozenDict) -> dict[str, Any]:
        """Convert frozen payload containers back to plain serializable objects.

        入参：`value` 是当前事件内部的只读 `FrozenDict` payload。
        返回：普通 dict/list 结构，供 `model_dump` 与 JSON 序列化使用。
        错误处理：若内部值不可序列化，后续 Pydantic/JSON 序列化会报告异常。
        副作用：只复制内存结构，不修改事件或原始 payload，不访问外部 I/O。
        """

        return _thaw_payload(value)

    @field_validator("occurred_at", "received_at")
    @classmethod
    def _ensure_timezone_aware(cls, value: datetime) -> datetime:
        """Reject naive datetimes so event ordering never guesses local time.

        入参：`value` 是 Pydantic 已解析出的 `occurred_at` 或 `received_at`。
        返回：原始 timezone-aware datetime，不做时区转换。
        错误处理：当 datetime 没有 tzinfo 或 utcoffset 为 None 时抛出 ValueError，
        由 Pydantic 包装为 ValidationError。
        副作用：无；只检查内存中的 datetime 字段。
        """

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime fields must be timezone-aware")
        return value

    @field_validator("payload", mode="before")
    @classmethod
    def _redact_and_freeze_payload(cls, value: Any) -> Mapping[str, Any]:
        """Normalize payload values into an immutable, redacted mapping.

        入参：`value` 是调用方传入的 payload；None 等价于空 mapping。
        返回：`FrozenDict` 顶层 mapping，内部 dict/list 递归冻结且敏感 key 已脱敏。
        错误处理：非 mapping 顶层 payload 由 ValueError 拒绝，并由 Pydantic 包装。
        副作用：不修改原始 payload，不访问网络、硬件或文件系统。
        """

        if value is None:
            return FrozenDict()
        if not isinstance(value, Mapping):
            raise ValueError("payload must be a mapping")
        return _freeze_payload(redact_payload(value))

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
            payload=payload or {},
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


def _freeze_payload(value: Any) -> Any:
    """Recursively convert JSON-like payload containers to immutable containers.

    入参：任意已脱敏 Python 值；dict 会转为 `FrozenDict`，list 会转为 tuple，
    其他标量保持原值。
    返回：递归冻结后的值，可安全挂载到 frozen `NormalizedEvent`。
    错误处理：若 dict key 无法作为字符串 key 使用或内部迭代失败，按 Python 异常传播。
    副作用：不修改输入容器，不访问网络、硬件或文件系统。
    """

    if isinstance(value, Mapping):
        return FrozenDict({key: _freeze_payload(item) for key, item in value.items()})

    if isinstance(value, list):
        return tuple(_freeze_payload(item) for item in value)

    return value


def _thaw_payload(value: Any) -> Any:
    """Recursively convert frozen payload containers to plain containers.

    入参：任意 payload 值；`FrozenDict` 会转为 dict，tuple 会转为 list，
    其他标量保持原值。
    返回：适合比较、`model_dump` 和 JSON 序列化的普通 Python 容器。
    错误处理：内部 mapping 或 tuple 迭代失败时按 Python 异常传播。
    副作用：不修改输入容器，不访问网络、硬件或文件系统。
    """

    if isinstance(value, Mapping):
        return {key: _thaw_payload(item) for key, item in value.items()}

    if isinstance(value, tuple):
        return [_thaw_payload(item) for item in value]

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
