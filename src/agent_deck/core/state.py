"""State reducer for normalized Agent Deck events.

This module owns the in-memory projection from `NormalizedEvent` streams to
per-agent status models. It does not parse vendor payloads, broker decisions,
compute hardware layouts, start servers, access StreamDock devices, read files,
write files, or perform network I/O; callers are responsible for feeding
already-normalized events and persisting snapshots if they need durability.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_deck.core.events import AgentSource, EventType, NormalizedEvent


class AgentStatus(StrEnum):
    """Represent the UI-facing lifecycle state for one agent session.

    入参：枚举成员值是 renderer、API 和测试共同使用的稳定字符串。
    返回：作为字符串枚举参与 Pydantic 校验、序列化和状态比较。
    错误处理：未知状态值由 Enum/Pydantic 校验为非法值并报告。
    副作用：无；定义枚举不访问网络、硬件、文件或全局运行状态。
    """

    OFFLINE = "offline"
    IDLE = "idle"
    THINKING = "thinking"
    RUNNING_TOOL = "running_tool"
    WAITING_USER = "waiting_user"
    APPROVAL_NEEDED = "approval_needed"
    ERROR = "error"
    COMPLETED_RECENTLY = "completed_recently"


class AgentState(BaseModel):
    """Immutable projection of the latest known state for one agent.

    入参：字段覆盖 agent identity、展示上下文、当前状态、时间戳、最新摘要、活跃工具、
    待处理决策数、布局槽位、焦点目标和静音标记；时间字段必须带 timezone。
    返回：frozen Pydantic model，可通过 `model_copy(update=...)` 派生新版本。
    错误处理：非法枚举、负 pending count 或 naive datetime 由 Pydantic 报告。
    副作用：仅保存内存数据；实例化不访问网络、硬件或文件系统。
    """

    model_config = ConfigDict(frozen=True)

    agent_key: str
    source: AgentSource
    display_name: str
    cwd: str | None = None
    status: AgentStatus
    status_since: datetime
    last_event_at: datetime
    last_summary: str | None = None
    active_tool: str | None = None
    pending_decision_count: int = Field(default=0, ge=0)
    slot_id: str | None = None
    focus_target: str | None = None
    muted: bool = False

    @field_validator("status_since", "last_event_at")
    @classmethod
    def _ensure_timezone_aware(cls, value: datetime) -> datetime:
        """Reject naive datetimes so state aging never guesses local time.

        入参：`value` 是 Pydantic 已解析出的状态时间字段。
        返回：原始 timezone-aware datetime，不做时区转换。
        错误处理：当 datetime 没有 tzinfo 或 utcoffset 为 None 时抛出 ValueError。
        副作用：无；只检查内存中的 datetime 字段。
        """

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime fields must be timezone-aware")
        return value


class AgentStateStore:
    """Reduce normalized events into mutable in-memory agent state.

    入参：构造时可配置 idle TTL；后续通过 `apply` 输入单个 normalized event。
    返回：方法返回 frozen `AgentState` 快照或状态列表。
    错误处理：事件字段非法应已由 `NormalizedEvent` 阶段拦截；未知 agent 的 decision
    resolve 返回 None；内部状态校验失败按 Pydantic 异常传播。
    副作用：`apply` 和 `mark_decision_resolved` 修改本实例内存；不访问外部 I/O。
    """

    def __init__(self, idle_ttl: timedelta = timedelta(minutes=30)) -> None:
        """Create an empty in-memory state store.

        入参：`idle_ttl` 是 snapshot 时将非 offline agent 投影为 offline 的空闲阈值。
        返回：无显式返回值；初始化后的 store 可接收事件。
        错误处理：本方法不主动校验负 TTL；调用方传入异常对象时按 Python 语义传播。
        副作用：仅初始化内存 dict，不访问网络、硬件或文件系统。
        """

        self._idle_ttl = idle_ttl
        self._states: dict[str, AgentState] = {}
        self._active_tools: dict[str, tuple[str, ...]] = {}

    def apply(self, event: NormalizedEvent) -> AgentState:
        """Apply one normalized event and return the updated agent state.

        入参：`event` 是已校验、已脱敏、payload 不可变的 normalized event。
        返回：应用 reducer 规则后的 frozen `AgentState`，并写入本 store。
        错误处理：未知事件类型按刷新上下文处理；Pydantic 校验异常会向调用方传播。
        副作用：修改本实例中该 agent 的内存状态和活跃工具集合；不访问外部 I/O。
        """

        current = self._states.get(event.agent_key)
        state = current or self._new_state(event)
        active_tools = self._active_tools.get(event.agent_key, ())
        status = state.status
        active_tool = state.active_tool
        pending_count = state.pending_decision_count
        force_status_since = False

        match event.normalized_type:
            case EventType.SESSION_STARTED:
                status = AgentStatus.IDLE
                active_tools = ()
                active_tool = None
                pending_count = 0
            case EventType.SESSION_ENDED:
                status = AgentStatus.OFFLINE
                active_tools = ()
                active_tool = None
                pending_count = 0
                force_status_since = True
            case EventType.TURN_STARTED:
                status = AgentStatus.THINKING
            case EventType.TURN_COMPLETED:
                status = (
                    AgentStatus.APPROVAL_NEEDED
                    if pending_count > 0
                    else AgentStatus.COMPLETED_RECENTLY
                )
            case EventType.TOOL_STARTED:
                tool_name = _tool_name(event)
                active_tools = _add_tool(active_tools, tool_name)
                active_tool = tool_name
                status = AgentStatus.RUNNING_TOOL
            case EventType.TOOL_COMPLETED:
                active_tools = _remove_tool(active_tools, _tool_name(event) or active_tool)
                active_tool = _first_tool(active_tools)
                if pending_count > 0:
                    status = AgentStatus.APPROVAL_NEEDED
                elif active_tool is not None:
                    status = AgentStatus.RUNNING_TOOL
                else:
                    status = AgentStatus.THINKING
            case EventType.TOOL_FAILED:
                active_tools = _remove_tool(active_tools, _tool_name(event) or active_tool)
                active_tool = _first_tool(active_tools)
                status = AgentStatus.ERROR
            case EventType.APPROVAL_REQUESTED:
                tool_name = _tool_name(event)
                pending_count += 1
                active_tool = tool_name or active_tool
                active_tools = _add_tool(active_tools, tool_name)
                status = AgentStatus.APPROVAL_NEEDED
            case EventType.INPUT_REQUESTED:
                status = AgentStatus.WAITING_USER
            case EventType.ERROR:
                status = AgentStatus.ERROR
            case EventType.HEARTBEAT:
                status = state.status
            case EventType.SUBAGENT_STARTED | EventType.SUBAGENT_COMPLETED:
                status = state.status

        updated = self._copy_state(
            state,
            event=event,
            status=status,
            active_tool=active_tool,
            pending_decision_count=pending_count,
            force_status_since=force_status_since,
        )
        self._states[event.agent_key] = updated
        self._active_tools[event.agent_key] = active_tools
        return updated

    def mark_decision_resolved(
        self, agent_key: str, resolved_at: datetime | None = None
    ) -> AgentState | None:
        """Resolve one pending decision for an agent.

        入参：`agent_key` 是 `NormalizedEvent.agent_key`；`resolved_at` 为空时使用当前 UTC。
        返回：更新后的 `AgentState`；未知 agent 返回 None。
        错误处理：`resolved_at` 若是 naive datetime，会在生成 `AgentState` 时被校验拒绝。
        副作用：修改本实例中该 agent 的 pending count 和可能的状态；不访问外部 I/O。
        """

        state = self._states.get(agent_key)
        if state is None:
            return None

        resolved_time = resolved_at or datetime.now(UTC)
        _ensure_timezone_aware_datetime(resolved_time)
        pending_count = max(0, state.pending_decision_count - 1)
        status = state.status
        status_since = state.status_since
        if state.status == AgentStatus.APPROVAL_NEEDED and pending_count == 0:
            status = AgentStatus.THINKING
            status_since = resolved_time

        updated = state.model_copy(
            update={
                "status": status,
                "status_since": status_since,
                "pending_decision_count": pending_count,
            }
        )
        self._states[agent_key] = updated
        return updated

    def get(self, agent_key: str) -> AgentState | None:
        """Return the stored state for one agent without TTL projection.

        入参：`agent_key` 是 source/session 派生出的稳定 key。
        返回：已存储的 `AgentState`；未知 agent 返回 None。
        错误处理：本方法不主动抛业务异常。
        副作用：无；只读取本实例内存状态，不访问外部 I/O。
        """

        return self._states.get(agent_key)

    def snapshot(self, now: datetime | None = None) -> list[AgentState]:
        """Return all states sorted by recency with stale agents projected offline.

        入参：`now` 是 TTL 判断时间；为空时使用当前 UTC 时间。
        返回：按 `last_event_at` 倒序排列的 `AgentState` 列表；超过 idle TTL 且非 offline
        的 agent 在返回值中投影为 offline。
        错误处理：`now` 若是 naive datetime，会在投影 `AgentState` 时被校验拒绝。
        副作用：不修改 store 内部状态；仅创建返回列表，不访问外部 I/O。
        """

        snapshot_time = now or datetime.now(UTC)
        _ensure_timezone_aware_datetime(snapshot_time)
        projected = [
            self._project_stale_offline(state, snapshot_time)
            for state in self._states.values()
        ]
        return sorted(projected, key=lambda state: state.last_event_at, reverse=True)

    def _new_state(self, event: NormalizedEvent) -> AgentState:
        """Create the default state for the first event of an agent.

        入参：`event` 提供 agent key、source、展示名、cwd 和初始时间。
        返回：初始 idle `AgentState`，具体事件规则随后由 `apply` 覆盖。
        错误处理：字段不合法由 Pydantic 校验异常报告。
        副作用：无；只创建内存模型，不访问外部 I/O。
        """

        return AgentState(
            agent_key=event.agent_key,
            source=event.source,
            display_name=_display_name(event),
            cwd=event.cwd,
            status=AgentStatus.IDLE,
            status_since=event.occurred_at,
            last_event_at=event.occurred_at,
            last_summary=event.summary,
        )

    def _copy_state(
        self,
        state: AgentState,
        *,
        event: NormalizedEvent,
        status: AgentStatus,
        active_tool: str | None,
        pending_decision_count: int,
        force_status_since: bool = False,
    ) -> AgentState:
        """Derive a new frozen state after one reducer transition.

        入参：`state` 是旧状态；`event` 提供最新上下文；`status`、`active_tool` 和
        `pending_decision_count` 是 reducer 计算后的 transient 字段；`force_status_since`
        用于 session end 等必须刷新进入时间的事件。
        返回：更新后的 `AgentState`，不会修改旧实例。
        错误处理：Pydantic 校验异常会向调用方传播。
        副作用：无；只复制内存模型，不访问外部 I/O。
        """

        status_changed = status != state.status or force_status_since
        return state.model_copy(
            update={
                "display_name": event.title or state.display_name,
                "cwd": event.cwd if event.cwd is not None else state.cwd,
                "status": status,
                "status_since": event.occurred_at
                if status_changed
                else state.status_since,
                "last_event_at": event.occurred_at,
                "last_summary": event.summary
                if event.summary is not None
                else state.last_summary,
                "active_tool": active_tool,
                "pending_decision_count": pending_decision_count,
            }
        )

    def _project_stale_offline(
        self, state: AgentState, snapshot_time: datetime
    ) -> AgentState:
        """Project a stale non-offline state to offline for snapshot output.

        入参：`state` 是 store 内保存的原始状态；`snapshot_time` 是 TTL 判断时间。
        返回：原状态或一个仅用于返回值的 offline 投影。
        错误处理：`snapshot_time` 若无时区会在 Pydantic 校验中被拒绝。
        副作用：无；不回写 store，不访问外部 I/O。
        """

        if state.status == AgentStatus.OFFLINE:
            return state
        if snapshot_time - state.last_event_at <= self._idle_ttl:
            return state
        return state.model_copy(
            update={
                "status": AgentStatus.OFFLINE,
                "status_since": snapshot_time,
                "active_tool": None,
            }
        )


def _display_name(event: NormalizedEvent) -> str:
    """Return the best available display name for an agent event.

    入参：`event` 是 normalized event；优先读取 title，其次 agent_id，最后 session_id。
    返回：非空字符串，用于 UI 和 API 展示。
    错误处理：本函数不主动抛业务异常。
    副作用：无；只读取事件字段，不访问外部 I/O。
    """

    return event.title or event.agent_id or event.session_id


def _ensure_timezone_aware_datetime(value: datetime) -> None:
    """Reject naive datetimes passed to public store methods.

    入参：`value` 是调用方传入或默认生成的 datetime。
    返回：无显式返回值；校验通过即可继续使用该时间。
    错误处理：当 datetime 没有 tzinfo 或 utcoffset 为 None 时抛出 ValueError。
    副作用：无；只检查内存中的 datetime，不访问外部 I/O。
    """

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime fields must be timezone-aware")


def _tool_name(event: NormalizedEvent) -> str | None:
    """Return the normalized tool name carried by an event.

    入参：`event` 是 normalized event；MVP 只使用顶层 `tool_name` 字段。
    返回：工具名字符串；事件未携带工具名时返回 None。
    错误处理：本函数不主动抛业务异常。
    副作用：无；只读取事件字段，不访问外部 I/O。
    """

    return event.tool_name


def _add_tool(active_tools: Iterable[str], tool_name: str | None) -> tuple[str, ...]:
    """Add a tool name to the active tool tuple while preserving order.

    入参：`active_tools` 是当前活跃工具集合的有序快照；`tool_name` 可为空。
    返回：加入工具后的 tuple；空工具名或重复工具名会返回原有集合。
    错误处理：迭代输入失败时按 Python 异常传播。
    副作用：无；只创建新的内存 tuple，不访问外部 I/O。
    """

    tools = tuple(active_tools)
    if tool_name is None or tool_name in tools:
        return tools
    return (*tools, tool_name)


def _remove_tool(
    active_tools: Iterable[str], tool_name: str | None
) -> tuple[str, ...]:
    """Remove a tool name from the active tool tuple.

    入参：`active_tools` 是当前活跃工具集合的有序快照；`tool_name` 可为空。
    返回：移除匹配工具后的 tuple；空工具名不会改变集合。
    错误处理：迭代输入失败时按 Python 异常传播。
    副作用：无；只创建新的内存 tuple，不访问外部 I/O。
    """

    tools = tuple(active_tools)
    if tool_name is None:
        return tools
    return tuple(tool for tool in tools if tool != tool_name)


def _first_tool(active_tools: Iterable[str]) -> str | None:
    """Return the first active tool name, if any.

    入参：`active_tools` 是当前活跃工具集合的有序快照。
    返回：第一个工具名；集合为空时返回 None。
    错误处理：迭代输入失败时按 Python 异常传播。
    副作用：无；只读取内存 iterable，不访问外部 I/O。
    """

    for tool in active_tools:
        return tool
    return None
