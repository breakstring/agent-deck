"""Tests for hardware-independent Agent Deck layout planning.

These tests define Task 5's pure layout contract only. They do not generate
images, start servers, touch hardware, read user files, write files, or perform
network I/O; their only side effects are local Python object creation and
pytest assertion reporting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_deck.core.decisions import DecisionStatus, PendingDecision
from agent_deck.core.events import AgentSource
from agent_deck.core.modes import DeckMode, DeckSelection
from agent_deck.core.state import AgentState, AgentStatus
from agent_deck.rendering.layout import build_layout_plan

BASE_TIME = datetime(2026, 6, 12, 8, 0, tzinfo=UTC)


def _state(
    agent_key: str,
    display_name: str,
    status: AgentStatus,
    *,
    last_event_offset: int = 0,
    source: AgentSource = AgentSource.CODEX,
    active_tool: str | None = None,
    last_summary: str | None = None,
) -> AgentState:
    """Build an agent state for layout tests.

    入参：`agent_key`、`display_name` 和 `status` 描述待布局 agent；`last_event_offset`
    以秒为单位偏移基础时间，其他关键字参数覆盖来源、活跃工具和摘要。
    返回：用于 `build_layout_plan` 的 frozen `AgentState`。
    错误处理：非法枚举、负 pending 数或 naive 时间由 `AgentState` 校验报告。
    副作用：仅创建内存模型，不访问网络、硬件或文件系统。
    """

    return AgentState(
        agent_key=agent_key,
        source=source,
        display_name=display_name,
        status=status,
        status_since=BASE_TIME + timedelta(seconds=last_event_offset),
        last_event_at=BASE_TIME + timedelta(seconds=last_event_offset),
        last_summary=last_summary,
        active_tool=active_tool,
    )


def _decision(
    decision_id: str,
    agent_key: str,
    *,
    tool_name: str = "shell",
    reason: str = "needs local approval",
) -> PendingDecision:
    """Build a pending decision for layout tests.

    入参：`decision_id` 和 `agent_key` 绑定审批对象；`tool_name` 与 `reason` 覆盖触屏文案。
    返回：status 为 pending 的 `PendingDecision`。
    错误处理：字段非法或时间无时区时由 `PendingDecision` 校验报告。
    副作用：仅创建内存模型，不访问网络、硬件或文件系统。
    """

    return PendingDecision(
        decision_id=decision_id,
        agent_key=agent_key,
        session_id="session-1",
        tool_name=tool_name,
        reason=reason,
        created_at=BASE_TIME,
        expires_at=BASE_TIME + timedelta(seconds=30),
        status=DecisionStatus.PENDING,
    )


def test_overview_layout_prioritizes_selected_running_agent_and_touchscreen() -> None:
    """Verify overview slots and touchscreen follow the selected agent.

    入参：无；测试内构造 selected running agent 与一个更新的 idle agent。
    返回：无返回值；断言通过代表 selected agent 被排到首槽并驱动 overview 触屏。
    错误处理：slot 排序、mode、触屏或 key count 不符合契约时由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    selected = _state(
        "codex:selected",
        "Selected Codex",
        AgentStatus.RUNNING_TOOL,
        last_event_offset=1,
        active_tool="pytest",
        last_summary="running tests",
    )
    newer_idle = _state(
        "codex:newer",
        "Newer Idle",
        AgentStatus.IDLE,
        last_event_offset=30,
    )
    selection = DeckSelection(
        mode=DeckMode.OVERVIEW,
        selected_agent_key=selected.agent_key,
    )

    plan = build_layout_plan([newer_idle, selected], [], selection)

    assert plan.mode == DeckMode.OVERVIEW
    assert len(plan.keys) == 15
    assert [key.index for key in plan.keys] == list(range(15))
    assert plan.keys[0].agent_key == selected.agent_key
    assert plan.keys[0].intent == "select_agent"
    assert plan.keys[10].label == "FOCUS"
    assert plan.keys[10].agent_key == selected.agent_key
    assert plan.touchscreen.title == selected.display_name
    assert plan.touchscreen.selected_agent_key == selected.agent_key
    assert any("pytest" in line for line in plan.touchscreen.lines)


def test_no_agents_keeps_full_key_plan_and_empty_touchscreen() -> None:
    """Verify an empty state still yields a complete neutral layout.

    入参：无；测试内不提供 agents 或 decisions。
    返回：无返回值；断言通过代表 15 键计划、默认触屏和 off LED 符合契约。
    错误处理：空输入未被稳定处理时由 pytest 报告。
    副作用：仅创建内存布局计划。
    """

    plan = build_layout_plan(
        [],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
    )

    assert len(plan.keys) == 15
    assert plan.touchscreen.title == "Agent Deck"
    assert plan.touchscreen.lines == ("No agents online",)
    assert plan.touchscreen.selected_agent_key is None
    assert plan.led_color == "off"


def test_pending_decision_overrides_mode_and_binds_approval_keys() -> None:
    """Verify pending decisions force decision mode and approval actions.

    入参：无；测试内构造一个 pending decision 和非 decision 选择态。
    返回：无返回值；断言通过代表 mode 覆盖、approve/deny key 和审批触屏符合契约。
    错误处理：decision 未覆盖 mode 或 key 未绑定 decision id 时由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    state = _state(
        "codex:session-1",
        "Approval Codex",
        AgentStatus.APPROVAL_NEEDED,
    )
    decision = _decision(
        "decision-1",
        state.agent_key,
        tool_name="shell",
        reason="run deploy command",
    )

    plan = build_layout_plan(
        [state],
        [decision],
        DeckSelection(mode=DeckMode.SETTINGS, selected_agent_key=state.agent_key),
    )

    assert plan.mode == DeckMode.DECISION
    assert plan.keys[10].label == "ALLOW"
    assert plan.keys[10].intent == "approve_request"
    assert plan.keys[10].decision_id == decision.decision_id
    assert plan.keys[11].label == "DENY"
    assert plan.keys[11].intent == "deny_request"
    assert plan.keys[11].decision_id == decision.decision_id
    assert plan.keys[12].label == "DETAIL"
    assert plan.keys[12].intent == "open_details"
    assert plan.keys[13].label == "BACK"
    assert plan.keys[13].intent == "back"
    assert plan.touchscreen.title == "Approval needed"
    assert plan.touchscreen.selected_decision_id == decision.decision_id
    assert "shell" in plan.touchscreen.lines
    assert "run deploy command" in plan.touchscreen.lines


def test_led_priority_across_agent_statuses() -> None:
    """Verify LED color aggregates highest-priority visible status.

    入参：无；测试内依次构造 error、approval、running、idle 和 offline-only 场景。
    返回：无返回值；断言通过代表 LED 优先级红黄蓝绿灭符合契约。
    错误处理：任一状态聚合颜色错误时由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    assert (
        build_layout_plan(
            [
                _state("codex:error", "Error", AgentStatus.ERROR),
                _state("codex:running", "Running", AgentStatus.RUNNING_TOOL),
            ],
            [],
            DeckSelection(mode=DeckMode.OVERVIEW),
        ).led_color
        == "red"
    )
    assert (
        build_layout_plan(
            [_state("codex:approval", "Approval", AgentStatus.APPROVAL_NEEDED)],
            [],
            DeckSelection(mode=DeckMode.OVERVIEW),
        ).led_color
        == "yellow"
    )
    assert (
        build_layout_plan(
            [
                _state("codex:thinking", "Thinking", AgentStatus.THINKING),
                _state("codex:offline", "Offline", AgentStatus.OFFLINE),
            ],
            [],
            DeckSelection(mode=DeckMode.OVERVIEW),
        ).led_color
        == "blue"
    )
    assert (
        build_layout_plan(
            [_state("codex:idle", "Idle", AgentStatus.IDLE)],
            [],
            DeckSelection(mode=DeckMode.OVERVIEW),
        ).led_color
        == "green"
    )
    assert (
        build_layout_plan(
            [
                _state("codex:offline", "Offline", AgentStatus.OFFLINE),
                _state("codex:offline-2", "Offline 2", AgentStatus.OFFLINE),
            ],
            [],
            DeckSelection(mode=DeckMode.OVERVIEW),
        ).led_color
        == "off"
    )


def test_slot_sorting_prefers_selected_then_active_status_then_recency() -> None:
    """Verify agent slot ordering follows selected, status priority, and recency.

    入参：无；测试内构造 selected idle、running、thinking、新旧 idle 多个 agent。
    返回：无返回值；断言通过代表 selected 优先，非 selected 再按状态和时间排序。
    错误处理：slot 排序偏离契约时由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    selected_idle = _state(
        "codex:selected",
        "Selected Idle",
        AgentStatus.IDLE,
        last_event_offset=0,
    )
    running_old = _state(
        "codex:running",
        "Running Old",
        AgentStatus.RUNNING_TOOL,
        last_event_offset=10,
    )
    thinking_new = _state(
        "codex:thinking",
        "Thinking New",
        AgentStatus.THINKING,
        last_event_offset=30,
    )
    idle_new = _state(
        "codex:idle-new",
        "Idle New",
        AgentStatus.IDLE,
        last_event_offset=40,
    )
    idle_old = _state(
        "codex:idle-old",
        "Idle Old",
        AgentStatus.IDLE,
        last_event_offset=20,
    )

    plan = build_layout_plan(
        [idle_new, thinking_new, selected_idle, running_old, idle_old],
        [],
        DeckSelection(
            mode=DeckMode.OVERVIEW,
            selected_agent_key=selected_idle.agent_key,
        ),
    )

    assert [key.agent_key for key in plan.keys[:5]] == [
        selected_idle.agent_key,
        running_old.agent_key,
        thinking_new.agent_key,
        idle_new.agent_key,
        idle_old.agent_key,
    ]
