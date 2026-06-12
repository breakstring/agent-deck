"""FastAPI application factory for the local Agent Deck daemon API.

This module wires the MVP in-memory runtime for normalized events, approval
decisions, layout planning, and a fake hardware surface. It deliberately does
not bind sockets, probe StreamDock devices, install hooks, read or write user
configuration, persist state, or render to real hardware; callers such as a
future CLI entry point are responsible for hosting the returned ASGI app.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from agent_deck.core.decisions import (
    DecisionBehavior,
    DecisionBroker,
    DecisionResult,
    PendingDecision,
)
from agent_deck.core.events import EventType, NormalizedEvent
from agent_deck.core.modes import DeckSelection
from agent_deck.core.state import AgentState, AgentStateStore
from agent_deck.hardware.fake import FakeHardwareSurface
from agent_deck.rendering.layout import LayoutPlan, build_layout_plan


class DecisionRequestBody(BaseModel):
    """Validate the body for creating one approval decision.

    入参：`agent_key` 标识要审批的 agent；`session_id`、`turn_id` 和 `tool_name`
    描述来源上下文；`reason` 是展示说明；`timeout_seconds` 必须为正秒数。
    返回：FastAPI/Pydantic 构造出的请求模型，供 handler 创建 broker decision。
    错误处理：缺失字段、非法类型或非正 timeout 会由 FastAPI 映射为 422。
    副作用：仅保存请求内存数据，不访问网络、硬件、文件或全局状态。
    """

    model_config = ConfigDict(frozen=True)

    agent_key: str
    session_id: str
    tool_name: str
    reason: str
    timeout_seconds: float = Field(gt=0)
    turn_id: str | None = None


class DecisionResolveBody(BaseModel):
    """Validate the body for resolving an approval decision.

    入参：`behavior` 是 allow/deny 终态；`message` 是可选展示说明，默认空字符串。
    返回：FastAPI/Pydantic 构造出的请求模型，供 handler 调用 broker resolve。
    错误处理：非法 behavior 或字段类型由 FastAPI 映射为 422。
    副作用：仅保存请求内存数据，不访问网络、硬件、文件或全局状态。
    """

    model_config = ConfigDict(frozen=True)

    behavior: DecisionBehavior
    message: str = ""


@dataclass
class _DaemonRuntime:
    """Hold all process-local daemon state used by the HTTP handlers.

    入参：`store` 是 normalized event reducer；`broker` 管理 pending approval；
    `surface` 记录 fake render 帧；`selection` 保存当前 deck 选择；两个 id 集合分别
    记录已反映到 store 的 pending decision 和已同步终态的 decision。
    返回：dataclass 实例，供 app.state 持有并在路由间共享。
    错误处理：本类不主动校验依赖类型；handler 调用时底层异常按原语义传播。
    副作用：后续方法会修改这些内存对象；不会访问真实硬件、文件或网络。
    """

    store: AgentStateStore
    broker: DecisionBroker
    surface: FakeHardwareSurface
    selection: DeckSelection
    reflected_pending_decision_ids: set[str]
    terminal_synced_decision_ids: set[str]

    def apply_event(self, event: NormalizedEvent) -> dict[str, Any]:
        """Apply an event, render the latest layout, and return response data.

        入参：`event` 是 FastAPI 已校验的 normalized event。
        返回：包含 JSON-safe `state`、`layout` 和 `render_count` 的 dict。
        错误处理：state reducer 或 layout model 校验失败会向 FastAPI 传播为 500。
        副作用：修改 store 内存状态，可能更新 selection，并在 fake surface 记录一帧。
        """

        state = self.store.apply(event)
        self._reflect_pending_decisions_for_agent(state.agent_key)
        state = self.store.get(state.agent_key) or state
        layout = self.render_current()
        return {
            "state": _dump_model(state),
            "layout": _dump_model(layout),
            "render_count": self.surface.render_count,
        }

    def create_decision(self, body: DecisionRequestBody) -> PendingDecision:
        """Create a pending decision and sync existing agent pending state.

        入参：`body` 是已通过 Pydantic 校验的 request body，timeout 为正秒数。
        返回：broker 创建的 pending `PendingDecision`。
        错误处理：broker 创建失败或 synthetic event 校验失败会按原异常传播。
        副作用：写入 broker；若对应 agent 已在 store 中，则应用一条内存 synthetic
        approval event；随后 render fake surface 一帧。
        """

        created_at = datetime.now(UTC)
        decision = self.broker.create(
            agent_key=body.agent_key,
            session_id=body.session_id,
            turn_id=body.turn_id,
            tool_name=body.tool_name,
            reason=body.reason,
            created_at=created_at,
            timeout=body.timeout_seconds,
        )
        self._reflect_pending_decision_if_agent_exists(decision, created_at)
        self.selection = self.selection.model_copy(
            update={
                "selected_agent_key": body.agent_key,
                "selected_decision_id": decision.decision_id,
            }
        )
        self.render_current()
        return decision

    def resolve_decision(
        self,
        decision_id: str,
        body: DecisionResolveBody,
    ) -> PendingDecision | None:
        """Resolve one decision and update agent pending state when known.

        入参：`decision_id` 是路径中的 decision id；`body` 包含 allow/deny 和 message。
        返回：resolved `PendingDecision`；未知 id 返回 None 供 handler 映射 404。
        错误处理：非法 behavior 已由 body 校验；broker 内部异常按原语义传播。
        副作用：首次 resolve 会完成 broker future、减少 store pending count，并 render。
        """

        if self.broker.get(decision_id) is None:
            return None
        resolved = self.broker.resolve(decision_id, body.behavior, body.message)
        self._sync_terminal_decision_once(resolved)
        if self.selection.selected_decision_id == decision_id:
            self.selection = self.selection.model_copy(
                update={"selected_decision_id": None}
            )
        self.render_current()
        return resolved

    async def wait_for_decision(
        self,
        decision_id: str,
        timeout_seconds: float,
    ) -> DecisionResult | None:
        """Wait for a decision result and sync timeout terminal state.

        入参：`decision_id` 是路径中的 decision id；`timeout_seconds` 是本次 wait 正秒数。
        返回：`DecisionResult`；未知 id 返回 None 供 handler 映射 404。
        错误处理：底层 wait 异常按原语义传播；非正 timeout 已由 FastAPI query 校验。
        副作用：若 wait 导致 pending decision 超时，会更新 broker、减少 store pending count
        并 render；读取已有终态 result 不额外修改状态。
        """

        before = self.broker.get(decision_id)
        if before is None:
            return None
        was_pending = before.result is None
        result = await self.broker.wait(decision_id, timeout=timeout_seconds)
        after = self.broker.get(decision_id)
        if was_pending and after is not None and after.result is not None:
            self._sync_terminal_decision_once(after)
            if self.selection.selected_decision_id == decision_id:
                self.selection = self.selection.model_copy(
                    update={"selected_decision_id": None}
                )
            self.render_current()
        return result

    def status(self) -> dict[str, Any]:
        """Render and return the complete daemon status snapshot.

        入参：无；读取当前 store、broker、selection 和 fake surface。
        返回：JSON-safe agents、pending decisions、layout 和 render_count。
        错误处理：layout 生成或模型序列化失败会向 FastAPI 传播为 500。
        副作用：每次调用都会 render 当前 state 到 fake surface，以刷新 stale/offline 投影。
        """

        layout = self.render_current()
        return {
            "agents": [_dump_model(state) for state in self.store.snapshot()],
            "decisions": [_dump_model(decision) for decision in self.broker.pending()],
            "layout": _dump_model(layout),
            "render_count": self.surface.render_count,
        }

    def render_current(self) -> LayoutPlan:
        """Build the current layout and record it on the fake surface.

        入参：无；从 store snapshot、broker pending 和 selection 读取当前 frame 输入。
        返回：刚刚渲染的 `LayoutPlan`。
        错误处理：layout 构造失败会按 Pydantic/Python 原异常传播。
        副作用：可能在首次出现 agent 时更新 selection，并递增 fake surface render count。
        """

        self._reflect_pending_decisions_for_known_agents()
        states = self.store.snapshot()
        self._ensure_first_agent_selected(states)
        decisions = self.broker.pending()
        layout = build_layout_plan(states, decisions, self.selection)
        self.surface.render(layout)
        return layout

    def _ensure_first_agent_selected(self, states: list[AgentState]) -> None:
        """Select the first snapshot agent when no agent has been selected.

        入参：`states` 是当前 store snapshot，已按 recency 排序。
        返回：无显式返回值。
        错误处理：本方法不主动抛业务异常；model_copy 失败按 Pydantic 语义传播。
        副作用：当 selection 缺少 selected_agent_key 且 states 非空时修改 selection。
        """

        if self.selection.selected_agent_key is None and states:
            self.selection = self.selection.model_copy(
                update={"selected_agent_key": states[0].agent_key}
            )

    def _reflect_pending_decisions_for_known_agents(self) -> None:
        """Reflect all pending broker decisions whose agents already exist.

        入参：无；读取 broker 的 pending decisions 和当前 store 中的 agent keys。
        返回：无显式返回值。
        错误处理：synthetic event 构造或 store apply 失败会向调用方传播。
        副作用：可能为尚未 reflected 的 decision 修改对应 agent pending count。
        """

        for decision in self.broker.pending():
            self._reflect_pending_decision_if_agent_exists(decision, decision.created_at)

    def _reflect_pending_decisions_for_agent(self, agent_key: str) -> None:
        """Reflect pending broker decisions for one known agent.

        入参：`agent_key` 是刚创建或更新的 store agent key。
        返回：无显式返回值。
        错误处理：synthetic event 构造或 store apply 失败会向调用方传播。
        副作用：可能为该 agent 尚未 reflected 的 decision 增加 pending count。
        """

        for decision in self.broker.pending():
            if decision.agent_key == agent_key:
                self._reflect_pending_decision_if_agent_exists(
                    decision,
                    decision.created_at,
                )

    def _reflect_pending_decision_if_agent_exists(
        self,
        decision: PendingDecision,
        created_at: datetime,
    ) -> None:
        """Apply an approval.requested event once for an existing agent.

        入参：`decision` 是 broker 中的 pending decision；`created_at` 是 synthetic event
        使用的 timezone-aware 时间。
        返回：无显式返回值。
        错误处理：若 synthetic event 构造或 store apply 失败，异常向调用方传播。
        副作用：仅当 agent 已存在且 decision id 未 reflected 时修改 store pending count。
        """

        if decision.decision_id in self.reflected_pending_decision_ids:
            return
        state = self.store.get(decision.agent_key)
        if state is None:
            return
        self.store.apply(
            NormalizedEvent.build(
                source=state.source,
                source_event_type="decision.requested",
                normalized_type=EventType.APPROVAL_REQUESTED,
                session_id=decision.session_id,
                turn_id=decision.turn_id,
                title=state.display_name,
                tool_name=decision.tool_name,
                summary=decision.reason,
                payload={
                    "decision_id": decision.decision_id,
                    "reason": decision.reason,
                },
                occurred_at=created_at,
                received_at=created_at,
            )
        )
        self.reflected_pending_decision_ids.add(decision.decision_id)

    def _sync_terminal_decision_once(self, decision: PendingDecision) -> None:
        """Reflect one terminal decision back to the store at most once.

        入参：`decision` 是 broker 返回的 resolved 或 timed-out snapshot。
        返回：无显式返回值。
        错误处理：store 更新失败会向调用方传播；非 terminal decision 会被忽略。
        副作用：若该 decision 曾 reflected 且尚未 terminal-synced，则递减 agent pending count。
        """

        if decision.result is None:
            return
        if decision.decision_id in self.terminal_synced_decision_ids:
            return
        self.terminal_synced_decision_ids.add(decision.decision_id)
        if decision.decision_id not in self.reflected_pending_decision_ids:
            return
        self.store.mark_decision_resolved(decision.agent_key)


def create_app() -> FastAPI:
    """Create the local daemon FastAPI app without opening external resources.

    入参：无；MVP foundation 暂不接收配置、端口或硬件参数。
    返回：配置好路由且持有 in-memory runtime 的 `FastAPI` ASGI app。
    错误处理：对象构造失败会直接抛出；正常调用不打开 socket 或真实硬件。
    副作用：仅分配内存对象并注册路由；不访问网络、文件、硬件或用户配置。
    """

    runtime = _DaemonRuntime(
        store=AgentStateStore(),
        broker=DecisionBroker(),
        surface=FakeHardwareSurface(),
        selection=DeckSelection(),
        reflected_pending_decision_ids=set(),
        terminal_synced_decision_ids=set(),
    )
    app = FastAPI(title="Agent Deck Daemon API")
    app.state.runtime = runtime

    @app.post("/events")
    async def post_event(event: NormalizedEvent) -> dict[str, Any]:
        """Apply a normalized event and render the current layout.

        入参：`event` 是请求体中的 `NormalizedEvent` JSON，由 FastAPI 校验。
        返回：JSON-safe dict，包含更新后的 state、layout 和 render_count。
        错误处理：请求体校验失败返回 422；内部 reducer/render 异常由 FastAPI 处理。
        副作用：修改 in-memory state store，并写入 fake hardware surface 一帧。
        """

        return runtime.apply_event(event)

    @app.get("/status")
    async def get_status() -> dict[str, Any]:
        """Return daemon status after rendering the current projection.

        入参：无。
        返回：JSON-safe agents、pending decisions、layout 和 render_count。
        错误处理：内部 render/serialization 异常由 FastAPI 处理。
        副作用：会 render fake surface 一帧，以刷新 stale/offline layout projection。
        """

        return runtime.status()

    @app.post("/decisions/request")
    async def request_decision(body: DecisionRequestBody) -> dict[str, Any]:
        """Create a pending approval decision.

        入参：`body` 是请求体，包含 agent、session、tool、reason 和正 timeout 秒数。
        返回：JSON-safe pending decision dict。
        错误处理：请求体校验失败返回 422；broker 创建异常由 FastAPI 处理。
        副作用：写入 in-memory broker，可能同步 store pending count，并 render 一帧。
        """

        return _dump_model(runtime.create_decision(body))

    @app.post("/decisions/{decision_id}/resolve")
    async def resolve_decision(
        decision_id: str,
        body: DecisionResolveBody,
    ) -> dict[str, Any]:
        """Resolve a pending approval decision.

        入参：`decision_id` 来自路径；`body` 提供 allow/deny behavior 和 message。
        返回：JSON-safe resolved decision dict。
        错误处理：未知 id 返回 404；请求体校验失败返回 422。
        副作用：更新 in-memory broker/store，完成等待中的 future，并 render 一帧。
        """

        resolved = runtime.resolve_decision(decision_id, body)
        if resolved is None:
            raise HTTPException(status_code=404, detail="unknown decision_id")
        return _dump_model(resolved)

    @app.get("/decisions/{decision_id}/wait")
    async def wait_decision(
        decision_id: str,
        timeout_seconds: float = Query(gt=0),
    ) -> dict[str, Any]:
        """Wait for a decision result with a positive timeout.

        入参：`decision_id` 来自路径；`timeout_seconds` 是 query 中的正秒数。
        返回：JSON-safe decision result dict。
        错误处理：未知 id 返回 404；非正 timeout 由 FastAPI 返回 422。
        副作用：若等待导致 timeout，会更新 broker/store 并 render；已有结果只读取。
        """

        result = await runtime.wait_for_decision(decision_id, timeout_seconds)
        if result is None:
            raise HTTPException(status_code=404, detail="unknown decision_id")
        return _dump_model(result)

    return app


def _dump_model(model: BaseModel) -> dict[str, Any]:
    """Serialize Pydantic models into JSON-compatible dictionaries.

    入参：`model` 是 Pydantic BaseModel 子类实例，通常来自 core 或 layout 模块。
    返回：`model_dump(mode="json")` 的 dict，确保 frozen payload 等结构可 JSON 序列化。
    错误处理：不可序列化字段会由 Pydantic 抛出异常。
    副作用：只复制内存数据，不修改模型或访问外部 I/O。
    """

    return model.model_dump(mode="json")
