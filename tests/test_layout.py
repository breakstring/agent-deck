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
from agent_deck.rendering.key_surface import (
    KeySurfaceKind,
    N4ProKeyBinding,
    N4ProKeyLayout,
    default_n4pro_key_layout,
)
from agent_deck.rendering.layout import build_layout_plan
from agent_deck.rendering.visuals import VisualAgentState

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
    parent_agent_key: str | None = None,
    is_child_agent: bool = False,
) -> AgentState:
    """Build an agent state for layout tests.

    入参：`agent_key`、`display_name` 和 `status` 描述待布局 agent；`last_event_offset`
    以秒为单位偏移基础时间，其他关键字参数覆盖来源、活跃工具、摘要和父子关系。
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
        parent_agent_key=parent_agent_key,
        is_child_agent=is_child_agent,
    )


def _decision(
    decision_id: str,
    agent_key: str,
    *,
    tool_name: str = "shell",
    reason: str = "needs local approval",
    created_offset: int = 0,
) -> PendingDecision:
    """Build a pending decision for layout tests.

    入参：`decision_id` 和 `agent_key` 绑定审批对象；`tool_name` 与 `reason` 覆盖触屏文案；
    `created_offset` 以秒为单位偏移基础创建时间，用于排序断言。
    返回：status 为 pending 的 `PendingDecision`。
    错误处理：字段非法或时间无时区时由 `PendingDecision` 校验报告。
    副作用：仅创建内存模型，不访问网络、硬件或文件系统。
    """

    created_at = BASE_TIME + timedelta(seconds=created_offset)
    return PendingDecision(
        decision_id=decision_id,
        agent_key=agent_key,
        session_id="session-1",
        tool_name=tool_name,
        reason=reason,
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=30),
        status=DecisionStatus.PENDING,
    )


def test_overview_layout_keeps_status_slot_order_and_selected_touchscreen() -> None:
    """Verify overview slots follow status while touchscreen follows selection.

    入参：无；测试内构造 selected running agent 与一个更新的 idle agent。
    返回：无返回值；断言通过代表运行中 agent 排到首槽，overview 触屏显示 selected agent。
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
    assert plan.keys[0].visual is not None
    assert plan.keys[0].visual.visual_state == VisualAgentState.WORKING
    assert plan.keys[10].visual is None
    assert plan.keys[10].label == "MUTE"
    assert plan.keys[10].agent_key == selected.agent_key
    assert plan.keys[10].intent == "mute_agent"
    assert all(key.intent != "focus_agent" for key in plan.keys)
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


def test_n4pro_key_layout_projects_agents_only_into_agent_slots() -> None:
    """Verify user key layout reserves quick-action keys ahead of agent slots.

    入参：无；测试内使用 N4 Pro 默认 key surface 布局和一个 running agent。
    返回：无返回值；断言通过代表 Key 1-5 不被 Agent 抢占，Key 6 才显示 Agent 状态。
    错误处理：key role、intent 或 agent 投影位置不符合预期时由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    agent = _state(
        "codex:active",
        "Active Codex",
        AgentStatus.RUNNING_TOOL,
        active_tool="shell",
    )

    plan = build_layout_plan(
        [agent],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
        key_layout=default_n4pro_key_layout(),
    )

    assert plan.keys[0].kind == KeySurfaceKind.UNASSIGNED.value
    assert plan.keys[0].role == "user_action"
    assert plan.keys[0].intent == "show_brand_feedback"
    assert plan.keys[0].agent_key is None
    assert plan.keys[5].kind == KeySurfaceKind.AGENT.value
    assert plan.keys[5].role == "agent_slot"
    assert plan.keys[5].intent == "select_agent"
    assert plan.keys[5].agent_key == agent.agent_key
    assert plan.keys[5].visual is not None
    assert plan.keys[5].visual.visual_state == VisualAgentState.WORKING


def test_n4pro_app_binding_projects_action_payload() -> None:
    """Verify App bindings become low-risk action keys in layout projection.

    入参：无；测试内把 Key 1 配置为 App，其余主键保留默认语义。
    返回：无返回值；断言通过代表 layout 暴露 open/focus intent 和 App payload。
    错误处理：App key 被误投影为 agent 或缺失 payload 时由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    layout = N4ProKeyLayout(
        keys=(
            N4ProKeyBinding(
                index=0,
                kind=KeySurfaceKind.APP,
                label="Finder",
                app_name="Finder",
                app_path="/System/Library/CoreServices/Finder.app",
                bundle_id="com.apple.finder",
                icon_token="FI",
            ),
            *default_n4pro_key_layout().sorted_keys()[1:],
        )
    )

    plan = build_layout_plan(
        [],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
        key_layout=layout,
    )

    assert plan.keys[0].kind == KeySurfaceKind.APP.value
    assert plan.keys[0].role == "user_action"
    assert plan.keys[0].intent == "open_or_focus_app"
    assert plan.keys[0].action == "open_or_focus_app"
    assert plan.keys[0].label == "Finder"
    assert plan.keys[0].payload["app_name"] == "Finder"
    assert plan.keys[0].payload["app_path"] == "/System/Library/CoreServices/Finder.app"
    assert plan.keys[0].payload["bundle_id"] == "com.apple.finder"
    assert plan.keys[0].payload["icon_token"] == "FI"


def test_n4pro_status_bindings_project_to_stateful_key_intents() -> None:
    """状态型按键配置应投影为可切换的 quota/usage key intent。

    入参：无；测试内配置一个 quota_status 和一个 usage_summary 主键。
    返回：无返回值；断言通过代表 layout 中包含 renderer 和 input router 所需的 kind/payload。
    错误处理：kind、intent 或 payload 不符合状态键契约时由 pytest 报告。
    副作用：无；只创建内存 layout。
    """

    layout = N4ProKeyLayout(
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

    plan = build_layout_plan(
        [],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
        key_layout=layout,
    )

    assert plan.keys[0].kind == KeySurfaceKind.QUOTA_STATUS.value
    assert plan.keys[0].intent == "cycle_quota_status_window"
    assert plan.keys[0].payload["quota_window"] == "primary"
    assert plan.keys[1].kind == KeySurfaceKind.USAGE_SUMMARY.value
    assert plan.keys[1].intent == "cycle_usage_summary_period"
    assert plan.keys[1].payload["usage_period"] == "week"


def test_overview_hides_offline_agents_from_main_button_slots() -> None:
    """Verify offline agents do not occupy overview agent key slots.

    入参：无；测试内构造一个 offline agent 和一个 idle agent。
    返回：无返回值；断言通过代表主按钮区只显示可操作 agent，offline 不占槽。
    错误处理：若 offline agent 仍出现在 key slot 或被选为触屏 agent，由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    offline = _state(
        "codex:offline",
        "Offline Codex",
        AgentStatus.OFFLINE,
        last_event_offset=40,
    )
    idle = _state(
        "codex:idle",
        "Idle Codex",
        AgentStatus.IDLE,
        last_event_offset=10,
    )

    plan = build_layout_plan(
        [offline, idle],
        [],
        DeckSelection(
            mode=DeckMode.OVERVIEW,
            selected_agent_key=offline.agent_key,
        ),
    )

    assert plan.keys[0].agent_key == idle.agent_key
    assert all(key.agent_key != offline.agent_key for key in plan.keys)
    assert plan.touchscreen.selected_agent_key == idle.agent_key


def test_overview_hides_child_agents_from_main_button_slots() -> None:
    """Verify child agents do not occupy overview agent key slots.

    入参：无；测试内构造一个主 agent 和一个更新的 child agent。
    返回：无返回值；断言通过代表主按钮区只显示顶层 agent，child agent 不抢占槽位。
    错误处理：若 child agent 出现在 key slot 或影响触屏 fallback，由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    parent = _state(
        "codex:parent",
        "Parent Codex",
        AgentStatus.IDLE,
        last_event_offset=10,
    )
    child = _state(
        "codex:child",
        "Child Codex",
        AgentStatus.RUNNING_TOOL,
        active_tool="shell",
        parent_agent_key=parent.agent_key,
        is_child_agent=True,
        last_event_offset=60,
    )

    plan = build_layout_plan(
        [child, parent],
        [],
        DeckSelection(
            mode=DeckMode.OVERVIEW,
            selected_agent_key=child.agent_key,
        ),
    )

    assert [key.agent_key for key in plan.keys[:10] if key.agent_key is not None] == [
        parent.agent_key
    ]
    assert plan.touchscreen.selected_agent_key == parent.agent_key


def test_overview_treats_offline_only_as_no_visible_agents() -> None:
    """Verify offline-only snapshots leave the main overview empty.

    入参：无；测试内只提供 offline agent。
    返回：无返回值；断言通过代表 stale/offline 会话不会留下灰色主按钮。
    错误处理：若 offline agent 出现在按钮、触屏或 action row，由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    offline = _state("codex:offline", "Offline Codex", AgentStatus.OFFLINE)

    plan = build_layout_plan(
        [offline],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
    )

    assert all(key.agent_key is None for key in plan.keys)
    assert plan.touchscreen.title == "Agent Deck"
    assert plan.touchscreen.lines == ("No agents online",)
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


def test_decision_mode_focuses_slots_on_decision_agent_over_previous_selection() -> None:
    """Verify decision mode uses the current decision agent as slot focus.

    入参：无；测试内让 selection 指向 agent A，但当前 pending decision 属于 agent B。
    返回：无返回值；断言通过代表 decision mode 的触屏和首个 slot 都聚焦 decision agent。
    错误处理：若旧 selection 继续支配 slot 排序或触屏绑定，会由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    selected_agent = _state(
        "codex:agent-a",
        "Agent A",
        AgentStatus.IDLE,
        last_event_offset=40,
    )
    decision_agent = _state(
        "codex:agent-b",
        "Agent B",
        AgentStatus.THINKING,
        last_event_offset=10,
    )
    decision = _decision("decision-b", decision_agent.agent_key)

    plan = build_layout_plan(
        [selected_agent, decision_agent],
        [decision],
        DeckSelection(
            mode=DeckMode.OVERVIEW,
            selected_agent_key=selected_agent.agent_key,
        ),
    )

    assert plan.mode == DeckMode.DECISION
    assert plan.touchscreen.selected_agent_key == decision_agent.agent_key
    assert plan.keys[0].agent_key == decision_agent.agent_key


def test_selected_decision_id_binds_actions_and_touchscreen_when_multiple_pending() -> None:
    """Verify selected decision id wins among multiple pending decisions.

    入参：无；测试内构造两个 pending decisions，并让 selection 指向较晚的那个。
    返回：无返回值；断言通过代表 action keys 和触屏都绑定 selected decision。
    错误处理：若 fallback 覆盖 selected decision 或绑定错 id，会由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    agent_a = _state("codex:agent-a", "Agent A", AgentStatus.APPROVAL_NEEDED)
    agent_b = _state("codex:agent-b", "Agent B", AgentStatus.APPROVAL_NEEDED)
    earliest = _decision(
        "decision-a",
        agent_a.agent_key,
        tool_name="shell",
        reason="earliest reason",
        created_offset=0,
    )
    selected = _decision(
        "decision-b",
        agent_b.agent_key,
        tool_name="python",
        reason="selected reason",
        created_offset=10,
    )

    plan = build_layout_plan(
        [agent_a, agent_b],
        [earliest, selected],
        DeckSelection(
            mode=DeckMode.OVERVIEW,
            selected_decision_id=selected.decision_id,
        ),
    )

    assert plan.keys[10].decision_id == selected.decision_id
    assert plan.keys[11].decision_id == selected.decision_id
    assert plan.touchscreen.selected_decision_id == selected.decision_id
    assert plan.touchscreen.selected_agent_key == agent_b.agent_key
    assert plan.touchscreen.lines == ("python", "selected reason")


def test_missing_selected_decision_falls_back_to_earliest_pending_decision() -> None:
    """Verify missing selected decision id falls back by earliest created time.

    入参：无；测试内构造两个 pending decisions，并让 selection 指向不存在的 id。
    返回：无返回值；断言通过代表 fallback 稳定选择创建时间最早的 pending decision。
    错误处理：若选择不存在 id 或较晚 pending decision，会由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    agent_a = _state("codex:agent-a", "Agent A", AgentStatus.APPROVAL_NEEDED)
    agent_b = _state("codex:agent-b", "Agent B", AgentStatus.APPROVAL_NEEDED)
    earliest = _decision(
        "decision-a",
        agent_a.agent_key,
        reason="earliest reason",
        created_offset=0,
    )
    later = _decision(
        "decision-b",
        agent_b.agent_key,
        reason="later reason",
        created_offset=10,
    )

    plan = build_layout_plan(
        [agent_b, agent_a],
        [later, earliest],
        DeckSelection(
            mode=DeckMode.OVERVIEW,
            selected_decision_id="missing",
        ),
    )

    assert plan.keys[10].decision_id == earliest.decision_id
    assert plan.touchscreen.selected_decision_id == earliest.decision_id
    assert plan.touchscreen.selected_agent_key == agent_a.agent_key


def test_pending_decision_fallback_tie_breaks_by_decision_id() -> None:
    """Verify same-time pending fallback uses decision id as deterministic tie-breaker.

    入参：无；测试内构造两个 created_at 相同但 id 不同的 pending decisions。
    返回：无返回值；断言通过代表 fallback 选择较小 decision id，避免输入顺序影响布局。
    错误处理：若 fallback 仍依赖输入顺序，会由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    agent_a = _state("codex:agent-a", "Agent A", AgentStatus.APPROVAL_NEEDED)
    agent_b = _state("codex:agent-b", "Agent B", AgentStatus.APPROVAL_NEEDED)
    larger_id = _decision("decision-z", agent_b.agent_key)
    smaller_id = _decision("decision-a", agent_a.agent_key)

    plan = build_layout_plan(
        [agent_b, agent_a],
        [larger_id, smaller_id],
        DeckSelection(mode=DeckMode.OVERVIEW),
    )

    assert plan.keys[10].decision_id == smaller_id.decision_id
    assert plan.touchscreen.selected_decision_id == smaller_id.decision_id
    assert plan.touchscreen.selected_agent_key == agent_a.agent_key


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


def test_slot_sorting_prefers_active_status_then_recency_without_selected_reorder() -> None:
    """Verify agent slot ordering stays stable when selection changes.

    入参：无；测试内构造 selected idle、running、thinking、新旧 idle 多个 agent。
    返回：无返回值；断言通过代表物理槽位不因 selected agent 置顶而漂移。
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
        running_old.agent_key,
        thinking_new.agent_key,
        idle_new.agent_key,
        idle_old.agent_key,
        selected_idle.agent_key,
    ]
    assert plan.touchscreen.selected_agent_key == selected_idle.agent_key


def test_slot_sorting_places_error_before_running_and_thinking() -> None:
    """Verify error status is a higher slot priority than active execution.

    入参：无；测试内构造 error、running 和 thinking agent，且不设置 selected agent。
    返回：无返回值；断言通过代表 error 会排在 running/thinking 前面。
    错误处理：若 slot 排序与 LED 高优先级语义不一致，会由 pytest 报告。
    副作用：仅创建内存模型和布局计划。
    """

    running = _state(
        "codex:running",
        "Running",
        AgentStatus.RUNNING_TOOL,
        last_event_offset=30,
    )
    thinking = _state(
        "codex:thinking",
        "Thinking",
        AgentStatus.THINKING,
        last_event_offset=40,
    )
    error = _state(
        "codex:error",
        "Error",
        AgentStatus.ERROR,
        last_event_offset=10,
    )

    plan = build_layout_plan(
        [running, thinking, error],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
    )

    assert [key.agent_key for key in plan.keys[:3]] == [
        error.agent_key,
        running.agent_key,
        thinking.agent_key,
    ]
