"""Tests for reducing normalized Agent Deck events into agent state.

These tests define the Task 3 in-memory reducer contract only. They do not
start servers, touch hardware, read user files, or persist state; their only
side effects are local Python object creation and pytest assertion reporting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_deck.core.events import AgentSource, EventType, NormalizedEvent
from agent_deck.core.state import AgentStateStore, AgentStatus


def _event(
    normalized_type: EventType,
    occurred_at: datetime,
    *,
    session_id: str = "session-1",
    source_event_type: str | None = None,
    title: str | None = "Codex",
    cwd: str | None = "/repo",
    tool_name: str | None = None,
    summary: str | None = None,
) -> NormalizedEvent:
    """Build a normalized event for reducer tests.

    入参：`normalized_type` 是要测试的规范事件类型；`occurred_at` 是带时区的事件时间；
    其余关键字参数覆盖 session、原始事件类型、展示标题、工作目录、工具名和摘要。
    返回：用于 `AgentStateStore.apply` 的 `NormalizedEvent`。
    错误处理：字段非法或时间无时区时由 `NormalizedEvent.build` 抛出 Pydantic 校验异常。
    副作用：仅创建内存模型，不访问网络、硬件或文件系统。
    """

    return NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type=source_event_type or normalized_type.value,
        normalized_type=normalized_type,
        session_id=session_id,
        occurred_at=occurred_at,
        title=title,
        cwd=cwd,
        tool_name=tool_name,
        summary=summary,
    )


def test_session_started_creates_idle_state() -> None:
    """Verify a new session starts in idle state.

    入参：无；测试内构造 `SESSION_STARTED` 事件。
    返回：无返回值；断言通过代表 reducer 初始化字段和 idle 状态符合契约。
    错误处理：模块导入、事件构建或状态断言失败由 pytest 报告。
    副作用：仅修改测试内的 `AgentStateStore` 内存状态。
    """

    occurred_at = datetime(2026, 6, 12, 8, 0, tzinfo=UTC)
    store = AgentStateStore()

    state = store.apply(_event(EventType.SESSION_STARTED, occurred_at))

    assert state.agent_key == "codex:session-1"
    assert state.source == AgentSource.CODEX
    assert state.display_name == "Codex"
    assert state.cwd == "/repo"
    assert state.status == AgentStatus.IDLE
    assert state.status_since == occurred_at
    assert state.last_event_at == occurred_at


def test_turn_started_moves_agent_to_thinking() -> None:
    """Verify turn start transitions an existing agent to thinking.

    入参：无；测试内先创建 session，再应用 `TURN_STARTED`。
    返回：无返回值；断言通过代表 turn lifecycle 状态变化正确。
    错误处理：状态未更新或时间字段不匹配会由 pytest 报告。
    副作用：仅修改测试内的 `AgentStateStore` 内存状态。
    """

    store = AgentStateStore()
    store.apply(_event(EventType.SESSION_STARTED, datetime(2026, 6, 12, 8, 0, tzinfo=UTC)))
    turn_at = datetime(2026, 6, 12, 8, 1, tzinfo=UTC)

    state = store.apply(_event(EventType.TURN_STARTED, turn_at, summary="working"))

    assert state.status == AgentStatus.THINKING
    assert state.status_since == turn_at
    assert state.last_event_at == turn_at
    assert state.last_summary == "working"


def test_tool_started_sets_running_tool_and_active_tool() -> None:
    """Verify tool start marks the agent as running the named tool.

    入参：无；测试内应用 `TOOL_STARTED` 并提供 tool name。
    返回：无返回值；断言通过代表 active tool 被记录并驱动状态。
    错误处理：active tool 或状态不匹配会由 pytest 报告。
    副作用：仅修改测试内的 `AgentStateStore` 内存状态。
    """

    store = AgentStateStore()
    tool_at = datetime(2026, 6, 12, 8, 2, tzinfo=UTC)

    state = store.apply(
        _event(EventType.TOOL_STARTED, tool_at, tool_name="shell", summary="running shell")
    )

    assert state.status == AgentStatus.RUNNING_TOOL
    assert state.status_since == tool_at
    assert state.active_tool == "shell"
    assert state.last_summary == "running shell"


def test_tool_completed_without_pending_returns_to_thinking_and_clears_tool() -> None:
    """Verify tool completion clears active tool when no decision is pending.

    入参：无；测试内先启动工具，再完成同名工具。
    返回：无返回值；断言通过代表无 pending 时 reducer 回到 thinking。
    错误处理：工具未清空或状态不正确会由 pytest 报告。
    副作用：仅修改测试内的 `AgentStateStore` 内存状态。
    """

    store = AgentStateStore()
    store.apply(
        _event(
            EventType.TOOL_STARTED,
            datetime(2026, 6, 12, 8, 2, tzinfo=UTC),
            tool_name="shell",
        )
    )
    completed_at = datetime(2026, 6, 12, 8, 3, tzinfo=UTC)

    state = store.apply(
        _event(EventType.TOOL_COMPLETED, completed_at, tool_name="shell")
    )

    assert state.status == AgentStatus.THINKING
    assert state.status_since == completed_at
    assert state.active_tool is None
    assert state.pending_decision_count == 0


def test_observed_idle_does_not_interrupt_recent_live_working_state() -> None:
    """Verify passive scanner idle cannot immediately hide live working.

    入参：无；测试内先用 hook 事件把 agent 置为 running_tool，再用 5 秒后的 scanner idle
    观测刷新同一 session。
    返回：无返回值；断言通过代表短时间内 idle 观测只更新上下文，不降级 working 视觉状态。
    错误处理：若 status、active tool 或时间字段不符合 sticky working 契约，由 pytest 报告。
    副作用：仅修改测试内的 `AgentStateStore` 内存状态。
    """

    store = AgentStateStore()
    tool_at = datetime(2026, 6, 12, 8, 2, tzinfo=UTC)
    store.apply(
        _event(EventType.TOOL_STARTED, tool_at, tool_name="shell", summary="running")
    )
    observed_at = tool_at + timedelta(seconds=5)

    state = store.upsert_observed_state(
        source=AgentSource.CODEX,
        session_id="session-1",
        observed_at=observed_at,
        status=AgentStatus.IDLE,
        title="Codex renamed",
        cwd="/repo/new",
        summary="recent unarchived thread",
    )

    assert state.status == AgentStatus.RUNNING_TOOL
    assert state.status_since == tool_at
    assert state.last_event_at == observed_at
    assert state.display_name == "Codex renamed"
    assert state.cwd == "/repo/new"
    assert state.active_tool == "shell"


def test_tool_completed_with_pending_keeps_approval_needed() -> None:
    """Verify pending approvals dominate tool completion status.

    入参：无；测试内请求 approval 后完成同名工具。
    返回：无返回值；断言通过代表 pending count 保留且状态保持 approval_needed。
    错误处理：pending count 被误减或状态被误改会由 pytest 报告。
    副作用：仅修改测试内的 `AgentStateStore` 内存状态。
    """

    store = AgentStateStore()
    store.apply(
        _event(
            EventType.TOOL_STARTED,
            datetime(2026, 6, 12, 8, 2, tzinfo=UTC),
            tool_name="shell",
        )
    )
    requested_at = datetime(2026, 6, 12, 8, 3, tzinfo=UTC)
    store.apply(
        _event(EventType.APPROVAL_REQUESTED, requested_at, tool_name="shell")
    )

    state = store.apply(
        _event(
            EventType.TOOL_COMPLETED,
            datetime(2026, 6, 12, 8, 4, tzinfo=UTC),
            tool_name="shell",
        )
    )

    assert state.status == AgentStatus.APPROVAL_NEEDED
    assert state.status_since == requested_at
    assert state.pending_decision_count == 1
    assert state.active_tool is None


def test_turn_completed_keeps_running_tool_when_tools_are_still_active() -> None:
    """Verify turn completion does not hide active tool work.

    入参：无；测试内启动 shell 和 python 两个工具后应用 `TURN_COMPLETED`。
    返回：无返回值；断言通过代表 active tool 优先级高于 completed_recently。
    错误处理：状态被误置为 completed 或 active tool 丢失会由 pytest 报告。
    副作用：仅修改测试内的 `AgentStateStore` 内存状态。
    """

    store = AgentStateStore()
    store.apply(
        _event(
            EventType.TOOL_STARTED,
            datetime(2026, 6, 12, 8, 2, tzinfo=UTC),
            tool_name="shell",
        )
    )
    store.apply(
        _event(
            EventType.TOOL_STARTED,
            datetime(2026, 6, 12, 8, 3, tzinfo=UTC),
            tool_name="python",
        )
    )

    state = store.apply(
        _event(EventType.TURN_COMPLETED, datetime(2026, 6, 12, 8, 4, tzinfo=UTC))
    )

    assert state.status == AgentStatus.RUNNING_TOOL
    assert state.active_tool in {"shell", "python"}


def test_mark_decision_resolved_returns_to_thinking_when_pending_clears() -> None:
    """Verify resolving the final pending decision returns to thinking.

    入参：无；测试内创建一个 approval pending，再按 agent key 标记 resolved。
    返回：无返回值；断言通过代表 pending 归零且 status_since 使用 resolved 时间。
    错误处理：未知 agent、pending 下溢或状态迁移错误会由 pytest 报告。
    副作用：仅修改测试内的 `AgentStateStore` 内存状态。
    """

    store = AgentStateStore()
    store.apply(
        _event(
            EventType.APPROVAL_REQUESTED,
            datetime(2026, 6, 12, 8, 3, tzinfo=UTC),
            tool_name="shell",
        )
    )
    resolved_at = datetime(2026, 6, 12, 8, 5, tzinfo=UTC)

    state = store.mark_decision_resolved("codex:session-1", resolved_at=resolved_at)

    assert state is not None
    assert state.pending_decision_count == 0
    assert state.status == AgentStatus.THINKING
    assert state.status_since == resolved_at


def test_mark_decision_resolved_returns_to_running_tool_when_tool_still_active() -> None:
    """Verify resolving approval preserves still-active tool state.

    入参：无；测试内启动 shell、请求 approval，再按 agent key 标记 resolved。
    返回：无返回值；断言通过代表 pending 清空后 active tool 驱动 running_tool 状态。
    错误处理：状态误回 thinking、pending 未清空或 active tool 丢失会由 pytest 报告。
    副作用：仅修改测试内的 `AgentStateStore` 内存状态。
    """

    store = AgentStateStore()
    store.apply(
        _event(
            EventType.TOOL_STARTED,
            datetime(2026, 6, 12, 8, 2, tzinfo=UTC),
            tool_name="shell",
        )
    )
    store.apply(
        _event(
            EventType.APPROVAL_REQUESTED,
            datetime(2026, 6, 12, 8, 3, tzinfo=UTC),
            tool_name="shell",
        )
    )

    state = store.mark_decision_resolved(
        "codex:session-1", resolved_at=datetime(2026, 6, 12, 8, 4, tzinfo=UTC)
    )

    assert state is not None
    assert state.status == AgentStatus.RUNNING_TOOL
    assert state.pending_decision_count == 0
    assert state.active_tool == "shell"


def test_duplicate_tool_names_are_counted_until_each_completion_arrives() -> None:
    """Verify same-name concurrent tools require matching completion count.

    入参：无；测试内启动两次 shell，并依次完成两次 shell。
    返回：无返回值；断言通过代表 active tool 按名称计数而不是去重。
    错误处理：第一次完成后过早清空工具或第二次完成后未回 thinking 会由 pytest 报告。
    副作用：仅修改测试内的 `AgentStateStore` 内存状态。
    """

    store = AgentStateStore()
    store.apply(
        _event(
            EventType.TOOL_STARTED,
            datetime(2026, 6, 12, 8, 2, tzinfo=UTC),
            tool_name="shell",
        )
    )
    store.apply(
        _event(
            EventType.TOOL_STARTED,
            datetime(2026, 6, 12, 8, 3, tzinfo=UTC),
            tool_name="shell",
        )
    )

    first_complete = store.apply(
        _event(
            EventType.TOOL_COMPLETED,
            datetime(2026, 6, 12, 8, 4, tzinfo=UTC),
            tool_name="shell",
        )
    )
    second_complete = store.apply(
        _event(
            EventType.TOOL_COMPLETED,
            datetime(2026, 6, 12, 8, 5, tzinfo=UTC),
            tool_name="shell",
        )
    )

    assert first_complete.status == AgentStatus.RUNNING_TOOL
    assert first_complete.active_tool == "shell"
    assert second_complete.status == AgentStatus.THINKING
    assert second_complete.active_tool is None


def test_snapshot_projects_stale_non_offline_agents_without_mutating_store() -> None:
    """Verify stale snapshot projection does not permanently update stored state.

    入参：无；测试内创建 idle agent 并用晚于 idle ttl 的时间获取 snapshot。
    返回：无返回值；断言通过代表 snapshot 投影为 offline，但 `get` 仍返回原状态。
    错误处理：排序、TTL 投影或 store 持久状态错误会由 pytest 报告。
    副作用：仅读取和修改测试内的 `AgentStateStore` 内存状态。
    """

    started_at = datetime(2026, 6, 12, 8, 0, tzinfo=UTC)
    store = AgentStateStore(idle_ttl=timedelta(minutes=30))
    original = store.apply(_event(EventType.SESSION_STARTED, started_at))

    snapshot = store.snapshot(now=started_at + timedelta(minutes=31))

    assert len(snapshot) == 1
    assert snapshot[0].status == AgentStatus.OFFLINE
    assert snapshot[0].status_since == started_at + timedelta(minutes=31)
    assert store.get(original.agent_key) == original


def test_session_ended_clears_active_tool_and_pending_count() -> None:
    """Verify ending a session resets transient tool and decision state.

    入参：无；测试内先启动工具并请求 approval，再应用 `SESSION_ENDED`。
    返回：无返回值；断言通过代表 session end 转 offline 并清空 transient 字段。
    错误处理：工具、pending 或 offline 状态不匹配会由 pytest 报告。
    副作用：仅修改测试内的 `AgentStateStore` 内存状态。
    """

    store = AgentStateStore()
    store.apply(
        _event(
            EventType.TOOL_STARTED,
            datetime(2026, 6, 12, 8, 2, tzinfo=UTC),
            tool_name="shell",
        )
    )
    store.apply(
        _event(
            EventType.APPROVAL_REQUESTED,
            datetime(2026, 6, 12, 8, 3, tzinfo=UTC),
            tool_name="shell",
        )
    )
    ended_at = datetime(2026, 6, 12, 8, 6, tzinfo=UTC)

    state = store.apply(_event(EventType.SESSION_ENDED, ended_at))

    assert state.status == AgentStatus.OFFLINE
    assert state.status_since == ended_at
    assert state.active_tool is None
    assert state.pending_decision_count == 0
