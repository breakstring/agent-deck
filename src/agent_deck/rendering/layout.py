"""Hardware-independent layout planning for Agent Deck.

This module turns current agent states, pending approval decisions, and user
selection into a renderer-neutral plan for StreamDock-like keys, touchscreen
text, and aggregate LED color. It does not generate images, talk to hardware,
start servers, read files, write files, modify global state, or perform network
I/O; callers can pass the returned frozen models to any later renderer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_deck.actions.keyboard import KeyboardShortcutSpec, ShortcutIconSpec
from agent_deck.core.decisions import DecisionStatus, PendingDecision
from agent_deck.core.modes import DeckMode, DeckSelection
from agent_deck.core.state import AgentState, AgentStatus
from agent_deck.rendering.visuals import VisualIconSpec, resolve_visual_icon_spec

if TYPE_CHECKING:
    from agent_deck.rendering.key_surface import N4ProKeyLayout

_KEY_COUNT = 15
_AGENT_SLOT_COUNT = 10
_STATUS_SORT_PRIORITY = {
    AgentStatus.APPROVAL_NEEDED: 0,
    AgentStatus.WAITING_USER: 1,
    AgentStatus.ERROR: 2,
    AgentStatus.RUNNING_TOOL: 3,
    AgentStatus.THINKING: 4,
    AgentStatus.COMPLETED_RECENTLY: 5,
    AgentStatus.IDLE: 6,
    AgentStatus.OFFLINE: 7,
}


class KeyAmbientOverlaySpec(BaseModel):
    """描述不改变按键动作的可选环境视觉覆盖层。

    入参：首版仅接受 Codex 宠物、启动目标作用域和任务活跃可见性三个固定值。
    返回：frozen Pydantic model，可随 binding 和 renderer-neutral plan JSON 往返。
    错误处理：未知覆盖层类型、作用域或可见性由 Pydantic 拒绝。
    副作用：仅保存声明，不读取任务状态、宠物素材或硬件。
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["codex_pet"] = "codex_pet"
    scope: Literal["launch_target"] = "launch_target"
    visibility: Literal["task_active"] = "task_active"


class KeyPlan(BaseModel):
    """Describe one physical key without renderer-specific image data.

    入参：`index` 是 0-14 的物理键位；`label` 是短文本标签；`status` 是绑定 agent
    的展示状态，可空；`visual` 是 renderer 可消费的按钮视觉规格，可空；
    `agent_key`、`intent` 和 `decision_id` 描述按键行为上下文；`role`、`kind`、`action`
    与 `payload` 是 key surface 配置投影的可选诊断/执行上下文；`ambient_overlay` 只声明
    可覆盖视觉，不替代原按键动作，旧布局可留空。
    返回：frozen Pydantic model，后续 renderer 可只读消费。
    错误处理：字段类型非法由 Pydantic 校验异常报告；键位范围由调用方生成保证。
    副作用：仅保存内存数据；实例化不访问网络、硬件或文件系统。
    """

    model_config = ConfigDict(frozen=True)

    index: int
    label: str = ""
    status: AgentStatus | None = None
    visual: VisualIconSpec | None = None
    agent_key: str | None = None
    intent: str | None = None
    decision_id: str | None = None
    role: str | None = None
    kind: str | None = None
    action: str | None = None
    payload: dict[str, str] = Field(default_factory=dict)
    shortcut: KeyboardShortcutSpec | None = None
    shortcut_icon: ShortcutIconSpec | None = None
    ambient_overlay: KeyAmbientOverlaySpec | None = None


class TouchscreenPlan(BaseModel):
    """Describe touchscreen text and selected entities for a layout frame.

    入参：`title` 是主标题；`lines` 是正文行；`selected_agent_key` 和
    `selected_decision_id` 是当前触屏内容绑定的实体，可空。
    返回：frozen Pydantic model，`lines` 会按 Pydantic 规则保存为 tuple。
    错误处理：字段类型非法由 Pydantic 校验异常报告。
    副作用：仅保存内存数据；实例化不访问网络、硬件或文件系统。
    """

    model_config = ConfigDict(frozen=True)

    title: str
    lines: tuple[str, ...] = ()
    selected_agent_key: str | None = None
    selected_decision_id: str | None = None


class LayoutPlan(BaseModel):
    """Carry a complete renderer-neutral deck layout frame.

    入参：`mode` 是 effective mode；`keys` 是 15 个 `KeyPlan`；`touchscreen` 是触屏
    文本计划；`led_color` 是聚合出的颜色字符串。
    返回：frozen Pydantic model，可安全传给后续硬件 renderer。
    错误处理：字段类型非法由 Pydantic 校验异常报告；本模型不主动校验 keys 数量。
    副作用：仅保存内存数据；实例化不访问网络、硬件或文件系统。
    """

    model_config = ConfigDict(frozen=True)

    mode: DeckMode
    keys: tuple[KeyPlan, ...]
    touchscreen: TouchscreenPlan
    led_color: str


def build_layout_plan(
    states: list[AgentState] | tuple[AgentState, ...],
    decisions: list[PendingDecision] | tuple[PendingDecision, ...],
    selection: DeckSelection,
    key_layout: "N4ProKeyLayout | None" = None,
) -> LayoutPlan:
    """Build a complete hardware-neutral layout from current memory snapshots.

    入参：`states` 是当前 agent 状态快照；`decisions` 是 decision broker 快照；
    `selection` 是用户选择的 mode、agent 和 decision；`key_layout` 是可选 N4 Pro 主按键
    用户布局，传入时只把 Agent 投影到配置为 Agent 状态的主按键上。
    返回：包含 15 个 key、触屏文案、effective mode 和 LED 颜色的 `LayoutPlan`。
    错误处理：输入模型若已非法会由各自 Pydantic 构造阶段拦截；本函数遇到未知选择 id
    会降级到排序后的第一个可用 agent 或 pending decision，不抛业务异常。
    副作用：无；只读取输入内存对象并创建新的 frozen plan，不访问外部 I/O。
    """

    pending_decision = _select_pending_decision(decisions, selection)
    effective_mode = DeckMode.DECISION if pending_decision is not None else selection.mode
    effective_selected_agent_key = (
        pending_decision.agent_key
        if pending_decision is not None
        else selection.selected_agent_key
    )
    visible_states = _visible_main_button_states(states)
    sorted_states = _sort_states_for_slots(visible_states, effective_selected_agent_key)
    selected_agent = _select_agent(sorted_states, effective_selected_agent_key)
    keys = _build_base_keys(sorted_states, key_layout=key_layout)

    if effective_mode == DeckMode.DECISION and pending_decision is not None:
        keys = _apply_decision_actions(keys, pending_decision)
        touchscreen = _build_decision_touchscreen(pending_decision)
    else:
        keys = _apply_overview_actions(keys, selected_agent)
        touchscreen = _build_agent_touchscreen(selected_agent)

    return LayoutPlan(
        mode=effective_mode,
        keys=tuple(keys),
        touchscreen=touchscreen,
        led_color=_aggregate_led_color(states),
    )


def _visible_main_button_states(
    states: list[AgentState] | tuple[AgentState, ...],
) -> list[AgentState]:
    """Filter agent snapshots down to sessions worth showing on main buttons.

    入参：`states` 是 state store 的完整快照，可能包含 TTL 投影出的 offline 会话。
    返回：不包含 `AgentStatus.OFFLINE` 和 child agent 的新列表；调用方仍可在其他诊断视图
    使用原始 states。
    错误处理：非法状态通常已由 `AgentState` 校验；未知对象按 Python 属性访问错误传播。
    副作用：无；只读取内存状态，不修改输入集合。
    """

    return [
        state
        for state in states
        if state.status != AgentStatus.OFFLINE and not state.is_child_agent
    ]


def _sort_states_for_slots(
    states: list[AgentState] | tuple[AgentState, ...],
    selected_agent_key: str | None,
) -> list[AgentState]:
    """Sort agents for key slots using active status, recency, then key.

    入参：`states` 是 agent 快照集合；`selected_agent_key` 当前只影响触屏选中，不改变槽位顺序。
    返回：新的 `AgentState` 列表；最多使用者由调用方截断。
    错误处理：状态值非法通常已被 `AgentState` 校验；未知状态按最低优先级处理。
    副作用：无；不修改输入集合或状态对象。
    """

    return sorted(
        states,
        key=lambda state: (
            _STATUS_SORT_PRIORITY.get(state.status, len(_STATUS_SORT_PRIORITY)),
            -state.last_event_at.timestamp(),
            state.agent_key,
        ),
    )


def _select_agent(
    sorted_states: list[AgentState],
    selected_agent_key: str | None,
) -> AgentState | None:
    """Return the selected agent or the first sorted agent as fallback.

    入参：`sorted_states` 是已经按 slot 优先级排序的状态列表；`selected_agent_key` 可空。
    返回：匹配 selected key 的 `AgentState`；未匹配时返回首个 sorted state；空列表返回 None。
    错误处理：本函数不主动抛业务异常。
    副作用：无；只读取内存列表。
    """

    if selected_agent_key is not None:
        for state in sorted_states:
            if state.agent_key == selected_agent_key:
                return state
    return sorted_states[0] if sorted_states else None


def _select_pending_decision(
    decisions: list[PendingDecision] | tuple[PendingDecision, ...],
    selection: DeckSelection,
) -> PendingDecision | None:
    """Return the pending decision that should drive decision mode.

    入参：`decisions` 是 broker 快照集合；`selection` 可指定 preferred decision id。
    返回：匹配 selected decision 的 pending decision；未匹配时返回最早创建的 pending；
    没有 pending 时返回 None。
    错误处理：本函数不主动抛业务异常；非法 decision 状态应由模型构造阶段拦截。
    副作用：无；只读取内存集合。
    """

    pending = [
        decision
        for decision in decisions
        if decision.status == DecisionStatus.PENDING
    ]
    if not pending:
        return None
    if selection.selected_decision_id is not None:
        for decision in pending:
            if decision.decision_id == selection.selected_decision_id:
                return decision
    return sorted(
        pending,
        key=lambda decision: (decision.created_at, decision.decision_id),
    )[0]


def _build_base_keys(
    sorted_states: list[AgentState],
    *,
    key_layout: "N4ProKeyLayout | None" = None,
) -> list[KeyPlan]:
    """Create 15 key plans and fill the first ten with agent slots.

    入参：`sorted_states` 是按 slot 优先级排序后的 agent 状态列表；`key_layout` 是可选
    N4 Pro 主按键布局。
    返回：长度固定为 15 的 mutable `KeyPlan` 列表，供后续 mode action 覆盖。
    错误处理：`KeyPlan` 字段校验失败会按 Pydantic 异常传播。
    副作用：无；仅创建新的内存模型列表。
    """

    keys = [KeyPlan(index=index) for index in range(_KEY_COUNT)]
    if key_layout is not None:
        from agent_deck.rendering.key_surface import project_n4pro_key_layout

        keys[:_AGENT_SLOT_COUNT] = project_n4pro_key_layout(
            key_layout,
            sorted_states,
        )
        return keys
    for index, state in enumerate(sorted_states[:_AGENT_SLOT_COUNT]):
        keys[index] = KeyPlan(
            index=index,
            label=state.display_name,
            status=state.status,
            visual=resolve_visual_icon_spec(state.status),
            agent_key=state.agent_key,
            intent="select_agent",
        )
    return keys


def _apply_decision_actions(
    keys: list[KeyPlan],
    decision: PendingDecision,
) -> list[KeyPlan]:
    """Bind decision approval actions to the action key row.

    入参：`keys` 是 base key 列表；`decision` 是当前 pending approval。
    返回：同一个长度的 `KeyPlan` 列表，其中 10-13 已替换为 decision actions。
    错误处理：列表长度不足会按 Python 索引异常传播，正常调用只传 15 键 base 列表。
    副作用：修改传入的局部 key 列表；不修改外部状态或访问外部 I/O。
    """

    for index, label, intent in (
        (10, "ALLOW", "approve_request"),
        (11, "DENY", "deny_request"),
        (12, "DETAIL", "open_details"),
        (13, "BACK", "back"),
    ):
        keys[index] = KeyPlan(
            index=index,
            label=label,
            agent_key=decision.agent_key,
            intent=intent,
            decision_id=decision.decision_id,
        )
    return keys


def _apply_overview_actions(
    keys: list[KeyPlan],
    selected_agent: AgentState | None,
) -> list[KeyPlan]:
    """Bind default overview actions to the action key row.

    入参：`keys` 是 base key 列表；`selected_agent` 是当前可操作 agent，可空。
    返回：同一个长度的 `KeyPlan` 列表，其中 10-14 已替换为 overview actions。
    错误处理：列表长度不足会按 Python 索引异常传播，正常调用只传 15 键 base 列表。
    副作用：修改传入的局部 key 列表；不修改外部状态或访问外部 I/O。
    """

    agent_key = selected_agent.agent_key if selected_agent is not None else None
    status = selected_agent.status if selected_agent is not None else None
    for index, label, intent in (
        (10, "MUTE", "mute_agent"),
        (11, "PROMPT", "open_quick_prompt"),
        (12, "DETAIL", "open_details"),
        (13, "MODE", "cycle_mode"),
    ):
        keys[index] = KeyPlan(
            index=index,
            label=label,
            status=status,
            agent_key=agent_key,
            intent=intent,
        )
    return keys


def _build_agent_touchscreen(selected_agent: AgentState | None) -> TouchscreenPlan:
    """Build touchscreen text for overview-like modes.

    入参：`selected_agent` 是当前聚焦 agent；为空时表示没有 online/known agent。
    返回：展示 agent 概要或空状态文案的 `TouchscreenPlan`。
    错误处理：字段校验失败会按 Pydantic 异常传播。
    副作用：无；只读取内存状态并创建新模型。
    """

    if selected_agent is None:
        return TouchscreenPlan(title="Agent Deck", lines=("No agents online",))

    lines = [f"Status: {selected_agent.status.value}"]
    if selected_agent.active_tool:
        lines.append(f"Tool: {selected_agent.active_tool}")
    if selected_agent.last_summary:
        lines.append(selected_agent.last_summary)
    if selected_agent.cwd:
        lines.append(selected_agent.cwd)

    return TouchscreenPlan(
        title=selected_agent.display_name,
        lines=tuple(lines),
        selected_agent_key=selected_agent.agent_key,
    )


def _build_decision_touchscreen(decision: PendingDecision) -> TouchscreenPlan:
    """Build touchscreen text for a pending approval decision.

    入参：`decision` 是当前 pending approval。
    返回：title 固定为 `Approval needed` 且正文包含 tool name 和 reason 的计划。
    错误处理：字段校验失败会按 Pydantic 异常传播。
    副作用：无；只读取 decision 内存快照并创建新模型。
    """

    return TouchscreenPlan(
        title="Approval needed",
        lines=(decision.tool_name, decision.reason),
        selected_agent_key=decision.agent_key,
        selected_decision_id=decision.decision_id,
    )


def _aggregate_led_color(
    states: list[AgentState] | tuple[AgentState, ...],
) -> str:
    """Aggregate visible agent statuses into one LED color.

    入参：`states` 是当前 agent 状态快照集合。
    返回：按 red、yellow、blue、green、off 优先级聚合出的颜色字符串。
    错误处理：未知状态不会抛业务异常，只会被忽略为 off。
    副作用：无；只读取内存状态集合。
    """

    statuses = {state.status for state in states}
    if AgentStatus.ERROR in statuses:
        return "red"
    if statuses & {AgentStatus.APPROVAL_NEEDED, AgentStatus.WAITING_USER}:
        return "yellow"
    if statuses & {AgentStatus.RUNNING_TOOL, AgentStatus.THINKING}:
        return "blue"
    if statuses & {AgentStatus.IDLE, AgentStatus.COMPLETED_RECENTLY}:
        return "green"
    return "off"
