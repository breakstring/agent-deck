"""FastAPI application factory for the local Agent Deck daemon API.

This module wires the MVP in-memory runtime for normalized events, approval
decisions, layout planning, optional Codex pollers, and a fake hardware surface.
It deliberately does not bind sockets, probe StreamDock devices, install hooks,
write user configuration, persist state, or render to real hardware; callers such
as CLI entry points are responsible for hosting the returned ASGI app and
choosing whether Codex local-state/quota polling is enabled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from agent_deck.adapters.codex_app_state import build_codex_app_state_events
from agent_deck.adapters.codex_quota import CodexQuotaSnapshot, read_codex_quota
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
from agent_deck.hardware.streamdock_touchscreen import (
    StreamDockTouchscreenRenderResult,
    render_touchscreen_image_to_n4pro,
)
from agent_deck.rendering.layout import LayoutPlan, build_layout_plan
from agent_deck.rendering.quota_touchscreen import render_quota_touchscreen

CodexAppStateEventReader = Callable[[], tuple[NormalizedEvent, ...]]
CodexQuotaReader = Callable[..., CodexQuotaSnapshot]
QuotaTouchscreenSink = Callable[[Any], StreamDockTouchscreenRenderResult]


class DaemonPollerConfig(BaseModel):
    """Configure optional daemon-side Codex polling loops.

    入参：`codex_app_state_enabled` 控制是否扫描 Codex App 本地 state/rollout；
    `codex_app_state_interval_seconds` 是扫描间隔；`codex_quota_enabled` 控制是否读取
    Codex app-server quota；`codex_quota_interval_seconds` 是 quota 间隔，默认 5 分钟；
    `codex_quota_timeout_seconds` 是单次 app-server 读取超时；
    `streamdock_quota_touchscreen_enabled` 控制是否把 quota 触屏图下发到真实硬件；
    `streamdock_quota_device` 是目标设备能力 profile，当前只支持 `n4pro`；`poll_on_start`
    控制启动时是否先同步一次，便于 daemon 刚启动就有状态。
    返回：frozen Pydantic model，供 `create_app` lifespan 使用。
    错误处理：非正间隔或 timeout 由 Pydantic 校验为 422/ValidationError。
    副作用：模型自身不读取文件、不启动进程、不创建后台任务。
    """

    model_config = ConfigDict(frozen=True)

    codex_app_state_enabled: bool = False
    codex_app_state_interval_seconds: float = Field(default=5.0, gt=0)
    codex_quota_enabled: bool = False
    codex_quota_interval_seconds: float = Field(default=300.0, gt=0)
    codex_quota_timeout_seconds: float = Field(default=10.0, gt=0)
    streamdock_quota_touchscreen_enabled: bool = False
    streamdock_quota_device: str = "n4pro"
    poll_on_start: bool = True


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
    记录已反映到 store 的 pending decision 和已同步终态的 decision；poller 字段保存
    Codex App state 与 quota 的最近一次同步状态。
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
    codex_app_state_last_polled_at: datetime | None
    codex_app_state_last_error: str | None
    codex_quota_snapshot: CodexQuotaSnapshot | None
    codex_quota_updated_at: datetime | None
    codex_quota_last_error: str | None
    streamdock_quota_touchscreen_result: StreamDockTouchscreenRenderResult | None

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

    def apply_polled_events(self, events: tuple[NormalizedEvent, ...]) -> None:
        """Apply events produced by a daemon poller and render once.

        入参：`events` 是 adapter 已生成的 normalized event tuple，可能为空。
        返回：无显式返回值。
        错误处理：任一事件 reducer 或 layout 校验失败会向调用方传播，由 poller 捕获记录。
        副作用：修改 store 中对应 agent 状态；有事件时 render fake surface 一帧。
        """

        for event in events:
            state = self.store.apply(event)
            self._reflect_pending_decisions_for_agent(state.agent_key)
        if events:
            self.render_current()

    def mark_codex_app_state_poll_success(self, polled_at: datetime) -> None:
        """Record a successful Codex App state poll.

        入参：`polled_at` 是本次扫描完成时间，必须 timezone-aware。
        返回：无显式返回值。
        错误处理：本方法不主动校验 datetime；调用方负责传入 UTC aware 时间。
        副作用：更新 runtime 内存诊断字段。
        """

        self.codex_app_state_last_polled_at = polled_at
        self.codex_app_state_last_error = None

    def mark_codex_app_state_poll_error(
        self,
        error: Exception,
        *,
        polled_at: datetime,
    ) -> None:
        """Record a failed Codex App state poll without crashing the daemon.

        入参：`error` 是扫描异常；`polled_at` 是失败发生时间。
        返回：无显式返回值。
        错误处理：本方法不抛业务异常；错误文本会被截断以避免异常对象过长。
        副作用：更新 runtime 内存诊断字段，不修改 agent 状态。
        """

        self.codex_app_state_last_polled_at = polled_at
        self.codex_app_state_last_error = _short_error(error)

    def update_codex_quota(
        self,
        snapshot: CodexQuotaSnapshot,
        *,
        updated_at: datetime,
    ) -> Any:
        """Store a quota snapshot and render it into the fake touch panel.

        入参：`snapshot` 是 quota adapter 返回的最新快照；`updated_at` 是读取完成时间。
        返回：刚渲染出的 800x480 触屏背景图，供真实硬件 sink 复用。
        错误处理：Pillow 渲染失败会向调用方传播，由 poller 捕获记录为 last_error。
        副作用：更新 runtime quota 快照，渲染 N4 Pro 800x480 触屏背景图到 fake surface。
        """

        self.codex_quota_snapshot = snapshot
        self.codex_quota_updated_at = updated_at
        self.codex_quota_last_error = None
        image = render_quota_touchscreen(snapshot)
        self.surface.render_touchscreen_image(
            image,
            source="codex_quota",
        )
        return image

    def update_streamdock_quota_touchscreen_result(
        self,
        result: StreamDockTouchscreenRenderResult,
    ) -> None:
        """Record the latest real StreamDock quota touchscreen render result.

        入参：`result` 是真实硬件 sink 返回的结果。
        返回：无显式返回值。
        错误处理：本方法不主动抛异常；字段合法性由结果模型保证。
        副作用：更新 runtime 内存诊断字段，不访问硬件。
        """

        self.streamdock_quota_touchscreen_result = result

    def mark_codex_quota_poll_error(
        self,
        error: Exception,
        *,
        polled_at: datetime,
    ) -> None:
        """Record a failed quota poll without clearing the last good snapshot.

        入参：`error` 是 quota adapter 或 renderer 异常；`polled_at` 是失败发生时间。
        返回：无显式返回值。
        错误处理：本方法不抛业务异常；错误文本会被截断。
        副作用：更新 quota 诊断字段；保留 `codex_quota_snapshot` 以便 UI 继续展示旧值。
        """

        self.codex_quota_updated_at = polled_at
        self.codex_quota_last_error = _short_error(error)

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
            "pollers": {
                "codex_app_state": {
                    "last_polled_at": _dump_datetime(
                        self.codex_app_state_last_polled_at
                    ),
                    "last_error": self.codex_app_state_last_error,
                },
            },
            "codex_quota": {
                "snapshot": _dump_optional_model(self.codex_quota_snapshot),
                "updated_at": _dump_datetime(self.codex_quota_updated_at),
                "last_error": self.codex_quota_last_error,
                "touchscreen_render_count": self.surface.touchscreen_render_count,
                "touchscreen_image_size": _image_size(
                    self.surface.last_touchscreen_image
                ),
                "streamdock_touchscreen": _dump_optional_model(
                    self.streamdock_quota_touchscreen_result
                ),
            },
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


def create_app(
    *,
    poller_config: DaemonPollerConfig | None = None,
    codex_app_state_event_reader: CodexAppStateEventReader = build_codex_app_state_events,
    codex_quota_reader: CodexQuotaReader = read_codex_quota,
    quota_touchscreen_sink: QuotaTouchscreenSink = render_touchscreen_image_to_n4pro,
) -> FastAPI:
    """Create the local daemon FastAPI app without binding sockets.

    入参：`poller_config` 控制是否启动 Codex App state 和 quota 后台 pollers；为空时不启用
    任何 poller，保持测试和嵌入调用无外部 I/O；`codex_app_state_event_reader` 和
    `codex_quota_reader` 是可注入 reader，生产默认读取真实本机 Codex 状态；`quota_touchscreen_sink`
    是真实硬件触屏下发函数，仅在配置启用时调用，测试可替换。
    返回：配置好路由且持有 in-memory runtime 的 `FastAPI` ASGI app。
    错误处理：对象构造失败会直接抛出；poller 单次失败会记录到 status，不让 app 启动失败。
    副作用：总是分配内存对象并注册路由；只有显式启用 poller 时，lifespan startup 才会只读访问
    Codex 本地状态或启动短生命周期 Codex app-server 子进程。
    """

    resolved_poller_config = poller_config or DaemonPollerConfig()
    runtime = _DaemonRuntime(
        store=AgentStateStore(),
        broker=DecisionBroker(),
        surface=FakeHardwareSurface(),
        selection=DeckSelection(),
        reflected_pending_decision_ids=set(),
        terminal_synced_decision_ids=set(),
        codex_app_state_last_polled_at=None,
        codex_app_state_last_error=None,
        codex_quota_snapshot=None,
        codex_quota_updated_at=None,
        codex_quota_last_error=None,
        streamdock_quota_touchscreen_result=None,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        """Start and stop optional daemon poller tasks.

        入参：`app` 是 FastAPI 实例，按 lifespan 协议传入。
        返回：async context manager；yield 后应用开始接收请求。
        错误处理：单次 poll 异常由 poll-once helper 记录；shutdown 取消任务并吞掉取消异常。
        副作用：可能创建 asyncio background tasks；不会绑定 socket 或访问真实硬件。
        """

        tasks: list[asyncio.Task[None]] = []
        if resolved_poller_config.poll_on_start:
            await _run_enabled_pollers_once(
                runtime,
                resolved_poller_config,
                codex_app_state_event_reader,
                codex_quota_reader,
                quota_touchscreen_sink,
            )
        if resolved_poller_config.codex_app_state_enabled:
            tasks.append(
                asyncio.create_task(
                    _poll_codex_app_state_loop(
                        runtime,
                        interval_seconds=resolved_poller_config.codex_app_state_interval_seconds,
                        event_reader=codex_app_state_event_reader,
                    )
                )
            )
        if resolved_poller_config.codex_quota_enabled:
            tasks.append(
                asyncio.create_task(
                    _poll_codex_quota_loop(
                        runtime,
                        interval_seconds=resolved_poller_config.codex_quota_interval_seconds,
                        timeout_seconds=resolved_poller_config.codex_quota_timeout_seconds,
                        quota_reader=codex_quota_reader,
                        streamdock_touchscreen_enabled=resolved_poller_config.streamdock_quota_touchscreen_enabled,
                        streamdock_device=resolved_poller_config.streamdock_quota_device,
                        quota_touchscreen_sink=quota_touchscreen_sink,
                    )
                )
            )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Agent Deck Daemon API", lifespan=lifespan)
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


async def _run_enabled_pollers_once(
    runtime: _DaemonRuntime,
    config: DaemonPollerConfig,
    codex_app_state_event_reader: CodexAppStateEventReader,
    codex_quota_reader: CodexQuotaReader,
    quota_touchscreen_sink: QuotaTouchscreenSink,
) -> None:
    """Run each enabled poller once during app startup.

    入参：`runtime` 是 daemon 内存状态；`config` 是 poller 配置；两个 reader 和触屏 sink 是
    可注入数据源/输出端。
    返回：无显式返回值。
    错误处理：单个 poller 的异常由 poll-once helper 记录，另一个 poller 仍会继续执行。
    副作用：可能只读访问 Codex 本地状态、启动短生命周期 app-server、更新 runtime 和 fake surface。
    """

    if config.codex_app_state_enabled:
        await _poll_codex_app_state_once(runtime, codex_app_state_event_reader)
    if config.codex_quota_enabled:
        await _poll_codex_quota_once(
            runtime,
            timeout_seconds=config.codex_quota_timeout_seconds,
            quota_reader=codex_quota_reader,
            streamdock_touchscreen_enabled=config.streamdock_quota_touchscreen_enabled,
            streamdock_device=config.streamdock_quota_device,
            quota_touchscreen_sink=quota_touchscreen_sink,
        )


async def _poll_codex_app_state_loop(
    runtime: _DaemonRuntime,
    *,
    interval_seconds: float,
    event_reader: CodexAppStateEventReader,
) -> None:
    """Periodically scan Codex App local state and apply generated events.

    入参：`runtime` 是 daemon 内存状态；`interval_seconds` 是两次扫描间隔；`event_reader`
    是同步 reader，会通过 `asyncio.to_thread` 调用。
    返回：不主动返回；任务被取消时结束。
    错误处理：单次扫描异常被记录到 runtime，不终止循环；取消异常正常传播给 shutdown。
    副作用：周期性只读访问 Codex 本地状态并更新 in-memory store。
    """

    while True:
        await asyncio.sleep(interval_seconds)
        await _poll_codex_app_state_once(runtime, event_reader)


async def _poll_codex_quota_loop(
    runtime: _DaemonRuntime,
    *,
    interval_seconds: float,
    timeout_seconds: float,
    quota_reader: CodexQuotaReader,
    streamdock_touchscreen_enabled: bool,
    streamdock_device: str,
    quota_touchscreen_sink: QuotaTouchscreenSink,
) -> None:
    """Periodically refresh Codex quota and render the virtual touch panel.

    入参：`runtime` 是 daemon 内存状态；`interval_seconds` 默认应远大于状态扫描间隔；
    `timeout_seconds` 是单次 quota app-server 读取超时；`quota_reader` 是同步 reader；
    `streamdock_touchscreen_enabled` 控制是否真实下发；`streamdock_device` 是目标设备 profile；
    `quota_touchscreen_sink` 是真实触屏输出端。
    返回：不主动返回；任务被取消时结束。
    错误处理：单次读取或渲染异常被记录到 runtime，不终止循环。
    副作用：周期性启动短生命周期 Codex app-server 子进程，并在成功时渲染内存触屏图。
    """

    while True:
        await asyncio.sleep(interval_seconds)
        await _poll_codex_quota_once(
            runtime,
            timeout_seconds=timeout_seconds,
            quota_reader=quota_reader,
            streamdock_touchscreen_enabled=streamdock_touchscreen_enabled,
            streamdock_device=streamdock_device,
            quota_touchscreen_sink=quota_touchscreen_sink,
        )


async def _poll_codex_app_state_once(
    runtime: _DaemonRuntime,
    event_reader: CodexAppStateEventReader,
) -> None:
    """Run one Codex App state poll and update runtime diagnostics.

    入参：`runtime` 是 daemon 内存状态；`event_reader` 返回 normalized events。
    返回：无显式返回值。
    错误处理：reader 或 reducer 异常会被捕获并记录到 `codex_app_state_last_error`。
    副作用：可能更新 agent state store 和 fake layout render。
    """

    polled_at = datetime.now(UTC)
    try:
        events = await asyncio.to_thread(event_reader)
        runtime.apply_polled_events(events)
        runtime.mark_codex_app_state_poll_success(polled_at)
    except Exception as exc:  # noqa: BLE001 - poller 必须保护 daemon 主循环。
        runtime.mark_codex_app_state_poll_error(exc, polled_at=polled_at)


async def _poll_codex_quota_once(
    runtime: _DaemonRuntime,
    *,
    timeout_seconds: float,
    quota_reader: CodexQuotaReader,
    streamdock_touchscreen_enabled: bool,
    streamdock_device: str,
    quota_touchscreen_sink: QuotaTouchscreenSink,
) -> None:
    """Run one Codex quota poll and update runtime diagnostics.

    入参：`runtime` 是 daemon 内存状态；`timeout_seconds` 是 reader 超时秒数；`quota_reader`
    返回 `CodexQuotaSnapshot`；`streamdock_touchscreen_enabled` 控制是否真实下发；`streamdock_device`
    当前支持 `n4pro`；`quota_touchscreen_sink` 接收渲染图并写真实硬件。
    返回：无显式返回值。
    错误处理：reader 或触屏渲染异常会被捕获并记录到 `codex_quota_last_error`。
    副作用：可能启动 Codex app-server 子进程，并在成功时渲染 fake touchscreen image。
    """

    polled_at = datetime.now(UTC)
    try:
        snapshot = await asyncio.to_thread(
            quota_reader,
            timeout_seconds=timeout_seconds,
        )
        image = runtime.update_codex_quota(snapshot, updated_at=polled_at)
        if streamdock_touchscreen_enabled:
            result = await _send_quota_touchscreen_to_streamdock(
                image,
                streamdock_device=streamdock_device,
                quota_touchscreen_sink=quota_touchscreen_sink,
            )
            runtime.update_streamdock_quota_touchscreen_result(result)
    except Exception as exc:  # noqa: BLE001 - poller 必须保护 daemon 主循环。
        runtime.mark_codex_quota_poll_error(exc, polled_at=polled_at)


async def _send_quota_touchscreen_to_streamdock(
    image: Any,
    *,
    streamdock_device: str,
    quota_touchscreen_sink: QuotaTouchscreenSink,
) -> StreamDockTouchscreenRenderResult:
    """Send a rendered quota touchscreen image to the configured StreamDock device.

    入参：`image` 是 quota renderer 输出的 800x480 图；`streamdock_device` 是目标设备 profile；
    `quota_touchscreen_sink` 是实际输出端。
    返回：真实输出端的结果，或不支持设备 profile 的失败结果。
    错误处理：sink 异常会转换为 `ok=False` 结果，避免中断 quota poller。
    副作用：当设备为 `n4pro` 时，会在线程中调用真实 sink，可能接管硬件触屏。
    """

    if streamdock_device.lower() != "n4pro":
        return StreamDockTouchscreenRenderResult(
            ok=False,
            error=f"unsupported StreamDock quota device: {streamdock_device}",
        )
    try:
        return await asyncio.to_thread(quota_touchscreen_sink, image)
    except Exception as exc:  # noqa: BLE001 - 硬件输出失败应进入 status，而不是杀 daemon。
        return StreamDockTouchscreenRenderResult(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def _dump_model(model: BaseModel) -> dict[str, Any]:
    """Serialize Pydantic models into JSON-compatible dictionaries.

    入参：`model` 是 Pydantic BaseModel 子类实例，通常来自 core 或 layout 模块。
    返回：`model_dump(mode="json")` 的 dict，确保 frozen payload 等结构可 JSON 序列化。
    错误处理：不可序列化字段会由 Pydantic 抛出异常。
    副作用：只复制内存数据，不修改模型或访问外部 I/O。
    """

    return model.model_dump(mode="json")


def _dump_optional_model(model: BaseModel | None) -> dict[str, Any] | None:
    """Serialize an optional Pydantic model for status output.

    入参：`model` 是可选 Pydantic model。
    返回：model 为 None 时返回 None，否则返回 JSON-safe dict。
    错误处理：不可序列化字段由 Pydantic 抛出。
    副作用：只复制内存数据。
    """

    if model is None:
        return None
    return _dump_model(model)


def _dump_datetime(value: datetime | None) -> str | None:
    """Serialize an optional datetime for status output.

    入参：`value` 是可选 datetime。
    返回：ISO 8601 字符串或 None。
    错误处理：datetime 格式化异常按 Python 语义传播。
    副作用：无；只读取内存值。
    """

    return value.isoformat() if value is not None else None


def _image_size(image: Any | None) -> list[int] | None:
    """Return a JSON-safe image size for diagnostics.

    入参：`image` 通常是 Pillow `Image`，也可为空或测试替身。
    返回：`[width, height]`；缺少合法 `size` 属性时返回 None。
    错误处理：本函数不抛业务异常；无法识别尺寸时降级为 None。
    副作用：无；只读取对象属性。
    """

    size = getattr(image, "size", None)
    if (
        isinstance(size, tuple)
        and len(size) == 2
        and all(isinstance(item, int) for item in size)
    ):
        return [size[0], size[1]]
    return None


def _short_error(error: Exception) -> str:
    """Format a bounded poller error string for status output.

    入参：`error` 是 poller 捕获的异常。
    返回：包含异常类型和消息的短字符串，最多 500 个字符。
    错误处理：异常消息格式化失败时按 Python repr/str 语义传播。
    副作用：无；不记录日志、不访问外部 I/O。
    """

    return f"{type(error).__name__}: {error}"[:500]
