"""FastAPI application factory for the local Agent Deck daemon API.

This module wires the MVP in-memory runtime for normalized events, approval
decisions, layout planning, optional Codex pollers, a fake hardware surface, and
optional real N4 Pro render sinks. It deliberately does not bind sockets, install
hooks, write user configuration, or persist state; callers such as CLI entry
points are responsible for hosting the returned ASGI app and choosing whether
Codex local-state/quota polling or real StreamDock rendering is enabled.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from agent_deck.actions.app_icon_cache import (
    AppIconCache,
    resolve_app_icon_cache_root,
)
from agent_deck.actions.apps import (
    LocalAppActionResult,
    LocalAppInfo,
    list_local_apps,
    open_or_focus_local_app,
)
from agent_deck.actions.focus import FocusActionResult, focus_agent_target
from agent_deck.adapters.codex_app_state import (
    CodexAppActiveSession,
    build_codex_app_state_events,
    read_codex_app_active_sessions,
)
from agent_deck.adapters.codex_quota import CodexQuotaSnapshot, read_codex_quota
from agent_deck.adapters.codex_tokens import (
    CodexTokenUsageSnapshot,
    read_codex_token_usage,
)
from agent_deck.core.decisions import (
    DecisionBehavior,
    DecisionBroker,
    DecisionResult,
    PendingDecision,
)
from agent_deck.core.events import AgentSource, EventType, NormalizedEvent
from agent_deck.core.modes import DeckSelection
from agent_deck.core.state import AgentState, AgentStateStore
from agent_deck.hardware.fake import FakeHardwareSurface, HardwareInput
from agent_deck.hardware.streamdock_n4pro import (
    StreamDockN4ProAnimationResult,
    StreamDockN4ProPersistentAnimator,
)
from agent_deck.hardware.streamdock_touchscreen import (
    StreamDockTouchscreenRenderResult,
    render_dual_device_touchscreen_image_to_n4pro,
    render_touchscreen_image_to_n4pro,
)
from agent_deck.input.logical_panel import (
    panel_event_from_hardware_input,
    panel_event_from_streamdock_input_event,
)
from agent_deck.input.interactions import (
    InteractionIntent,
    interaction_intent_from_hardware_input,
    interaction_intent_from_streamdock_input_event,
)
from agent_deck.rendering.brand import render_agent_deck_splash_touchscreen
from agent_deck.rendering.app_key import render_app_key_image
from agent_deck.rendering.codex_key_frames import codex_key_frame_paths_for_key_variants
from agent_deck.rendering.key_surface import (
    N4ProKeyLayout,
    default_n4pro_key_layout,
)
from agent_deck.rendering.layout import LayoutPlan, build_layout_plan
from agent_deck.rendering.logical_panel import (
    LogicalPanelPlan,
    PanelInputEvent,
    PanelKind,
    PanelSelection,
    apply_panel_input,
    message_panel_plan,
    tokens_panel_plan,
)
from agent_deck.rendering.logical_panel_touchscreen import (
    render_logical_panel_touchscreen,
)
from agent_deck.rendering.quota_touchscreen import render_quota_touchscreen
from agent_deck.server.key_layout_store import (
    KeyLayoutStoreError,
    load_n4pro_key_layout,
    save_n4pro_key_layout,
)

CodexAppStateEventReader = Callable[[], tuple[NormalizedEvent, ...]]
CodexAppActiveSessionsReader = Callable[..., tuple[CodexAppActiveSession, ...]]
CodexQuotaReader = Callable[..., CodexQuotaSnapshot]
CodexTokenUsageReader = Callable[[], CodexTokenUsageSnapshot]
QuotaTouchscreenSink = Callable[[Any], StreamDockTouchscreenRenderResult]
StreamDockN4ProRendererSink = Callable[..., StreamDockN4ProAnimationResult]
FocusActionExecutor = Callable[[str], FocusActionResult]
VisibleSplashTouchscreenSink = Callable[[Any], StreamDockTouchscreenRenderResult]
LocalAppCatalogReader = Callable[[], tuple[LocalAppInfo, ...]]
LocalAppActionExecutor = Callable[..., LocalAppActionResult]

_STREAMDOCK_TOUCH_TAP_DEBOUNCE_SECONDS = 0.45
"""真实 StreamDock touch bar 连续 touch_point 归并为一次 tap 的最短间隔。"""

_STREAMDOCK_KNOB4_ROTATE_THRESHOLD = 2
"""真实 StreamDock 第 4 旋钮累计多少个 rotate 事件后切换一次 token 周期。"""

_RECENT_STREAMDOCK_INPUT_LIMIT = 30
"""status 中保留多少条最近真实 StreamDock 输入事件诊断。"""

_RECENT_INTERACTION_LIMIT = 30
"""status 中保留多少条最近业务 interaction 诊断。"""

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
"""agent_deck 包根目录，用于定位随包分发的 Web GUI 和品牌资源。"""

_WEB_INDEX_PATH = _PACKAGE_ROOT / "web" / "index.html"
"""本地 daemon 根路径返回的 N4 Pro 配置 GUI 原型文件。"""

_WEB_ASSET_ROOT = _PACKAGE_ROOT / "web"
"""本地 daemon GUI 的随包 CSS/JS 资源目录。"""

_ASSET_ROOT = _PACKAGE_ROOT / "assets"
"""随包分发的 Agent Deck 品牌图和 N4 Pro splash 资源目录。"""


class DaemonPollerConfig(BaseModel):
    """Configure optional daemon-side Codex polling loops.

    入参：`codex_app_state_enabled` 控制是否扫描 Codex App 本地 state/rollout；
    `codex_app_state_interval_seconds` 是扫描间隔；`codex_app_state_scan_limit`、
    `codex_app_active_window_seconds` 和 `codex_app_active_session_limit` 控制 Codex App
    最近有效会话筛选；`codex_quota_enabled` 控制是否读取 Codex app-server quota；
    `codex_quota_interval_seconds` 是 quota 间隔，默认 5 分钟；
    `codex_quota_timeout_seconds` 是单次 app-server 读取超时；
    `streamdock_quota_touchscreen_enabled` 控制是否把 quota 触屏图下发到真实硬件；
    `streamdock_quota_device` 是目标设备能力 profile，当前只支持 `n4pro`；
    `streamdock_n4pro_renderer_enabled` 控制是否启用统一 N4 Pro 背景+按钮渲染循环，启用后
    应替代 quota-only 真实触屏下发；`streamdock_n4pro_frame_root` 指向 generated 按键帧；
    `streamdock_n4pro_render_interval_seconds` 和 `streamdock_n4pro_renderer_fps` 控制真机刷新节奏；
    `focus_actions_enabled` 控制是否允许 `focus_agent` 调用真实 action executor，默认开启；
    `poll_on_start` 控制启动时是否先同步一次，便于 daemon 刚启动就有状态。
    返回：frozen Pydantic model，供 `create_app` lifespan 使用。
    错误处理：非正间隔或 timeout 由 Pydantic 校验为 422/ValidationError。
    副作用：模型自身不读取文件、不启动进程、不创建后台任务。
    """

    model_config = ConfigDict(frozen=True)

    codex_app_state_enabled: bool = False
    codex_app_state_interval_seconds: float = Field(default=5.0, gt=0)
    codex_app_state_scan_limit: int = Field(default=80, gt=0)
    codex_app_active_window_seconds: int = Field(default=3600, gt=0)
    codex_app_active_session_limit: int = Field(default=10, gt=0)
    codex_quota_enabled: bool = False
    codex_quota_interval_seconds: float = Field(default=300.0, gt=0)
    codex_quota_timeout_seconds: float = Field(default=10.0, gt=0)
    codex_token_usage_enabled: bool = False
    codex_token_usage_interval_seconds: float = Field(default=300.0, gt=0)
    streamdock_quota_touchscreen_enabled: bool = False
    streamdock_quota_device: str = "n4pro"
    streamdock_n4pro_renderer_enabled: bool = False
    streamdock_n4pro_frame_root: Path = Path("assets/codex/generated/n4pro-key-112-fps10")
    streamdock_n4pro_render_interval_seconds: float = Field(default=3.0, gt=0)
    streamdock_n4pro_renderer_fps: int = Field(default=10, gt=0)
    focus_actions_enabled: bool = True
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


class LogicalPanelInputBody(BaseModel):
    """Validate one logical panel input event request.

    入参：`event` 是已经从硬件层归一化后的 logical panel 输入事件。
    返回：FastAPI/Pydantic 构造出的请求模型，供 handler 归约 panel selection。
    错误处理：非法事件字符串由 FastAPI 映射为 422。
    副作用：仅保存请求内存数据，不访问硬件、文件或网络。
    """

    model_config = ConfigDict(frozen=True)

    event: PanelInputEvent


class KeyLayoutResponse(BaseModel):
    """Return the current or default N4 Pro key layout to the GUI.

    入参：`device_profile` 是设备 profile 标识；`source` 描述布局来自 runtime、persisted
    还是内置默认；`path` 是可选持久化文件路径；`layout` 是完整 10 键布局。
    返回：frozen Pydantic model，可由 FastAPI 序列化。
    错误处理：字段非法由 Pydantic 报告。
    副作用：模型自身不读取文件、硬件或网络。
    """

    model_config = ConfigDict(frozen=True)

    device_profile: str
    source: str
    path: str | None = None
    layout: N4ProKeyLayout


@dataclass
class _DaemonRuntime:
    """Hold all process-local daemon state used by the HTTP handlers.

    入参：`store` 是 normalized event reducer；`broker` 管理 pending approval；
    `surface` 记录 fake render 帧；`selection` 保存当前 deck 选择；两个 id 集合分别
    记录已反映到 store 的 pending decision 和已同步终态的 decision；poller 字段保存
    Codex App state、quota 与真实 N4 Pro renderer 的最近一次同步状态。
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
    codex_token_usage_snapshot: CodexTokenUsageSnapshot | None
    codex_token_usage_updated_at: datetime | None
    codex_token_usage_last_error: str | None
    logical_panel_selection: PanelSelection
    streamdock_touch_tap_last_handled_monotonic: float | None
    streamdock_knob4_rotate_accumulator: int
    streamdock_input_event_count: int
    streamdock_last_input_event: dict[str, Any] | None
    streamdock_recent_input_events: list[dict[str, Any]]
    last_interaction_intent: InteractionIntent | None
    last_interaction_action: dict[str, Any] | None
    recent_interactions: list[dict[str, Any]]
    focus_actions_enabled: bool
    focus_action_executor: FocusActionExecutor
    local_app_catalog_reader: LocalAppCatalogReader
    local_app_action_executor: LocalAppActionExecutor
    app_icon_cache: AppIconCache
    streamdock_quota_touchscreen_result: StreamDockTouchscreenRenderResult | None
    streamdock_n4pro_renderer_result: StreamDockN4ProAnimationResult | None
    streamdock_n4pro_renderer_updated_at: datetime | None
    streamdock_n4pro_renderer_last_error: str | None
    key_layout: N4ProKeyLayout | None
    key_layout_source: str | None
    key_layout_path: Path | None
    key_layout_last_error: str | None

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
            "key_layout": _dump_model(self.current_key_layout_response()),
            "render_count": self.surface.render_count,
        }

    def current_key_layout_response(self) -> KeyLayoutResponse:
        """返回 GUI 当前应编辑的 N4 Pro 主键布局。

        入参：无。
        返回：若 runtime 已保存用户布局则返回该布局和 `runtime` source，否则返回内置默认布局和
        `default` source。
        错误处理：内置默认布局构造失败会按 Pydantic 异常传播。
        副作用：只读取 runtime 内存，不写配置、不访问硬件。
        """

        if self.key_layout is None:
            return KeyLayoutResponse(
                device_profile="mirabox.n4pro",
                source="default",
                path=str(self.key_layout_path) if self.key_layout_path else None,
                layout=default_n4pro_key_layout(),
            )
        return KeyLayoutResponse(
            device_profile="mirabox.n4pro",
            source=self.key_layout_source or "runtime",
            path=str(self.key_layout_path) if self.key_layout_path else None,
            layout=self.key_layout,
        )

    def update_key_layout(self, layout: N4ProKeyLayout) -> dict[str, Any]:
        """保存一份 daemon 进程内 N4 Pro 主键布局并重算当前投影。

        入参：`layout` 是 FastAPI/Pydantic 已校验的 10 键布局。
        返回：JSON-safe key layout response 和当前 layout projection。
        错误处理：layout 校验失败在 handler 业务逻辑前返回 422；投影异常按原语义传播。
        副作用：更新 runtime 内存布局并 render fake surface；若启用了 key layout path，会原子写入
        用户级 JSON 配置。
        """

        if self.key_layout_path is not None:
            save_n4pro_key_layout(layout, self.key_layout_path)
            self.key_layout_source = "persisted"
            self.key_layout_last_error = None
        else:
            self.key_layout_source = "runtime"
        self.key_layout = layout
        rendered_layout = self.render_current()
        return {
            "key_layout": _dump_model(self.current_key_layout_response()),
            "layout": _dump_model(rendered_layout),
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

    def apply_codex_active_sessions(
        self,
        sessions: tuple[CodexAppActiveSession, ...],
        *,
        observed_at: datetime,
    ) -> None:
        """Apply active Codex App sessions observed by the local state scanner.

        入参：`sessions` 是 adapter 按最近有效窗口筛选出的 Codex App 会话；`observed_at`
        是本轮扫描时间。
        返回：无显式返回值。
        错误处理：state model 校验失败会向调用方传播，由 poller 捕获记录。
        副作用：幂等更新 store 中对应 Codex agent 状态；有会话时 render fake surface 一帧。
        """

        for session in sessions:
            focus_target = (
                f"codex-app:{session.thread_id}"
                if session.thread_id
                else "app:Codex"
            )
            self.store.upsert_observed_state(
                source=AgentSource.CODEX,
                session_id=session.thread_id,
                observed_at=observed_at,
                status=session.status,
                title=session.title,
                cwd=session.cwd,
                summary=session.reason,
                active_tool=_active_tool_from_reason(session.reason),
                focus_target=focus_target,
                parent_session_id=session.parent_thread_id,
                is_child_agent=session.is_child_thread,
            )
        if sessions:
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
        image = self.render_current_logical_panel_image()
        return image

    def update_codex_token_usage(
        self,
        snapshot: CodexTokenUsageSnapshot,
        *,
        updated_at: datetime,
    ) -> Any | None:
        """Store a token usage snapshot and render it when tokens panel is active.

        入参：`snapshot` 是 token usage adapter 返回的最新快照；`updated_at` 是读取完成时间。
        返回：若当前 active logical panel 可渲染，则返回刚渲染出的 800x480 背景图，否则返回 None。
        错误处理：Pillow 渲染失败会向调用方传播，由 poller 捕获记录为 last_error。
        副作用：更新 runtime token usage 快照；当 active panel 为 tokens 时渲染 fake touchscreen。
        """

        self.codex_token_usage_snapshot = snapshot
        self.codex_token_usage_updated_at = updated_at
        self.codex_token_usage_last_error = None
        return self.render_current_logical_panel_image()

    def mark_codex_token_usage_poll_error(
        self,
        error: Exception,
        *,
        polled_at: datetime,
    ) -> None:
        """Record a failed token usage poll without clearing the last good snapshot.

        入参：`error` 是 token adapter 或 renderer 异常；`polled_at` 是失败发生时间。
        返回：无显式返回值。
        错误处理：本方法不抛业务异常；错误文本会被截断。
        副作用：更新 token usage 诊断字段；保留旧 snapshot 以便 UI 继续展示。
        """

        self.codex_token_usage_updated_at = polled_at
        self.codex_token_usage_last_error = _short_error(error)

    def apply_logical_panel_input(
        self,
        body: LogicalPanelInputBody,
    ) -> dict[str, Any]:
        """Apply one logical panel input event and render the selected panel.

        入参：`body` 是已校验的 panel input request。
        返回：JSON-safe selection 和触屏图诊断。
        错误处理：panel selection 或渲染异常按原语义传播，由 FastAPI 处理。
        副作用：更新 logical panel selection，并在可渲染时记录 fake touchscreen 图像。
        """

        self.logical_panel_selection = apply_panel_input(
            self.logical_panel_selection,
            body.event,
        )
        self.render_current_logical_panel_image()
        return {
            "selection": _dump_model(self.logical_panel_selection),
            "touchscreen_image_source": self.surface.last_touchscreen_image_source,
            "touchscreen_image_size": _image_size(self.surface.last_touchscreen_image),
        }

    def apply_hardware_input(self, event: HardwareInput) -> dict[str, Any]:
        """Apply one low-level hardware input event through input routers.

        入参：`event` 是已归一化的低层硬件输入，可能来自 fake surface 或真实 SDK listener。
        返回：JSON-safe dict，说明是否被 logical panel 处理以及当前 selection。
        错误处理：panel 渲染异常按原语义传播；无法映射的输入返回 handled=false。
        副作用：当输入映射到 logical panel event 时更新 selection 并可能渲染 fake touchscreen。
        """

        panel_event = panel_event_from_hardware_input(event)
        if panel_event is None:
            layout = self.render_current()
            interaction_intent = interaction_intent_from_hardware_input(event, layout)
            if interaction_intent is not None:
                interaction_result = self.apply_interaction_intent(interaction_intent)
                return {
                    "handled": True,
                    "panel_event": None,
                    **interaction_result,
                }
            return {
                "handled": False,
                "panel_event": None,
                "selection": _dump_model(self.logical_panel_selection),
                "interaction_intent": None,
            }
        result = self.apply_logical_panel_input(LogicalPanelInputBody(event=panel_event))
        return {
            "handled": True,
            "panel_event": panel_event.value,
            **result,
        }

    def apply_streamdock_input_event(self, event: object) -> dict[str, Any]:
        """Apply one SDK-like StreamDock input event through input routers.

        入参：`event` 是官方 SDK `InputEvent` 或具备同名属性的对象。
        返回：JSON-safe dict，说明是否被 logical panel 处理以及当前 selection。
        错误处理：panel 渲染异常按原语义传播；无法映射的输入返回 handled=false。
        副作用：当输入映射到 logical panel event 时更新 selection 并可能渲染 fake touchscreen。
        """

        panel_event = panel_event_from_streamdock_input_event(event)
        if panel_event is None:
            layout = self.render_current()
            interaction_intent = interaction_intent_from_streamdock_input_event(
                event,
                layout,
            )
            if interaction_intent is not None:
                interaction_result = self.apply_interaction_intent(interaction_intent)
                self._record_streamdock_input_event(
                    event,
                    panel_event=None,
                    handled=True,
                    debounced=False,
                    accumulated=False,
                )
                return {
                    "handled": True,
                    "panel_event": None,
                    **interaction_result,
                }
            self._record_streamdock_input_event(
                event,
                panel_event=None,
                handled=False,
                debounced=False,
                accumulated=False,
            )
            return {
                "handled": False,
                "panel_event": None,
                "selection": _dump_model(self.logical_panel_selection),
                "interaction_intent": None,
            }
        if self._should_debounce_streamdock_panel_event(panel_event):
            self._record_streamdock_input_event(
                event,
                panel_event=panel_event,
                handled=False,
                debounced=True,
                accumulated=False,
            )
            return {
                "handled": False,
                "panel_event": panel_event.value,
                "selection": _dump_model(self.logical_panel_selection),
                "debounced": True,
            }
        if self._should_accumulate_streamdock_knob_event(panel_event):
            self._record_streamdock_input_event(
                event,
                panel_event=panel_event,
                handled=False,
                debounced=False,
                accumulated=True,
            )
            return {
                "handled": False,
                "panel_event": panel_event.value,
                "selection": _dump_model(self.logical_panel_selection),
                "accumulated": True,
            }
        result = self.apply_logical_panel_input(LogicalPanelInputBody(event=panel_event))
        self._record_streamdock_input_event(
            event,
            panel_event=panel_event,
            handled=True,
            debounced=False,
            accumulated=False,
        )
        return {
            "handled": True,
            "panel_event": panel_event.value,
            **result,
        }

    def apply_interaction_intent(self, intent: InteractionIntent) -> dict[str, Any]:
        """应用一条 deck interaction intent。

        入参：`intent` 是硬件输入结合当前 layout 得出的交互意图。
        返回：JSON-safe dict，包含 intent、当前 deck selection 和可选 action 诊断。
        错误处理：未知 intent 只记录 dry-run；未知 decision id 返回 missing_decision 诊断。
        副作用：`select_agent` 会更新 deck selection；approval intent 会 resolve broker；
        显式启用真实 focus 时可能调用本机 focus executor。
        """

        self.last_interaction_intent = intent
        action: dict[str, Any] | None = None
        if intent.intent == "select_agent" and intent.agent_key is not None:
            self.selection = self.selection.model_copy(
                update={"selected_agent_key": intent.agent_key}
            )
            self.render_current()
            state = self.store.get(intent.agent_key)
            focus_intent = intent.model_copy(
                update={
                    "intent": "focus_agent",
                    "dry_run": True,
                }
            )
            if self.focus_actions_enabled:
                action = _execute_focus_action(
                    focus_intent,
                    state,
                    self.focus_action_executor,
                )
            else:
                action = _dry_run_action(focus_intent, state)
        elif intent.intent in {"approve_request", "deny_request"}:
            action = self._apply_decision_intent(intent)
        elif intent.intent == "open_or_focus_app":
            if self.focus_actions_enabled:
                action = _execute_local_app_action(
                    intent,
                    self.local_app_action_executor,
                )
            else:
                action = _dry_run_action(intent, state=None)
        else:
            state = self.store.get(intent.agent_key) if intent.agent_key else None
            if intent.intent == "focus_agent" and self.focus_actions_enabled:
                action = _execute_focus_action(
                    intent,
                    state,
                    self.focus_action_executor,
                )
            else:
                action = _dry_run_action(intent, state)
        self.last_interaction_action = action
        _append_bounded(
            self.recent_interactions,
            {
                "intent": _dump_model(intent),
                "action": action,
            },
            limit=_RECENT_INTERACTION_LIMIT,
        )
        return {
            "interaction_intent": _dump_model(intent),
            "deck_selection": _dump_model(self.selection),
            "action": action,
        }

    def _apply_decision_intent(self, intent: InteractionIntent) -> dict[str, Any]:
        """把硬件 approval intent 应用到 decision broker。

        入参：`intent` 是 `approve_request` 或 `deny_request`，应携带 decision id。
        返回：JSON-safe action 诊断，说明是否 resolve 成功。
        错误处理：缺少或未知 decision id 时不抛异常，返回 `missing_decision`。
        副作用：成功时会 resolve broker、同步 agent pending 状态，并 render 当前 layout/panel。
        """

        behavior = (
            DecisionBehavior.ALLOW
            if intent.intent == "approve_request"
            else DecisionBehavior.DENY
        )
        if intent.decision_id is None:
            return _missing_decision_action(intent, behavior)
        resolved = self.resolve_decision(
            intent.decision_id,
            DecisionResolveBody(
                behavior=behavior,
                message=_hardware_decision_message(behavior),
            ),
        )
        if resolved is None or resolved.result is None:
            return _missing_decision_action(intent, behavior)
        return {
            "intent": intent.intent,
            "agent_key": intent.agent_key,
            "decision_id": intent.decision_id,
            "status": "resolved",
            "ok": True,
            "behavior": resolved.result.behavior.value,
            "message": resolved.result.message,
        }

    def _record_streamdock_input_event(
        self,
        event: object,
        *,
        panel_event: PanelInputEvent | None,
        handled: bool,
        debounced: bool,
        accumulated: bool,
    ) -> None:
        """记录最近一次真实 StreamDock 输入诊断。

        入参：`event` 是 SDK-like 输入事件；`panel_event` 是 logical panel 映射结果；
        `handled` 表示本次是否改变 runtime 状态；`debounced` 表示是否因防抖被丢弃；
        `accumulated` 表示是否仍在等待旋钮累计阈值。
        返回：无。
        错误处理：缺失属性按 None 记录，不抛业务异常。
        副作用：更新 runtime 内存中的输入计数和最近事件摘要。
        """

        self.streamdock_input_event_count += 1
        snapshot = {
            "count": self.streamdock_input_event_count,
            "event_type": _dump_event_field(getattr(event, "event_type", None)),
            "key": _dump_event_key(event),
            "knob_id": _dump_event_field(getattr(event, "knob_id", None)),
            "direction": _dump_event_field(getattr(event, "direction", None)),
            "state": getattr(event, "state", None),
            "x": getattr(event, "x", None),
            "y": getattr(event, "y", None),
            "panel_event": panel_event.value if panel_event is not None else None,
            "handled": handled,
            "debounced": debounced,
            "accumulated": accumulated,
            "knob4_rotate_accumulator": self.streamdock_knob4_rotate_accumulator,
        }
        self.streamdock_last_input_event = snapshot
        _append_bounded(
            self.streamdock_recent_input_events,
            snapshot,
            limit=_RECENT_STREAMDOCK_INPUT_LIMIT,
        )

    def _should_accumulate_streamdock_knob_event(self, event: PanelInputEvent) -> bool:
        """判断真实 StreamDock 旋钮事件是否尚未达到周期切换阈值。

        入参：`event` 是 SDK 事件映射后的 logical panel 输入。
        返回：需要继续累计、暂不切换 token 周期时为 True；达到阈值时清零并返回 False。
        错误处理：无。
        副作用：更新第 4 旋钮的有符号累计步数。
        """

        if event == PanelInputEvent.KNOB_4_ROTATE_RIGHT:
            step = 1
        elif event == PanelInputEvent.KNOB_4_ROTATE_LEFT:
            step = -1
        else:
            return False

        if self.logical_panel_selection.active_kind != PanelKind.TOKENS:
            self.streamdock_knob4_rotate_accumulator = 0
            return False

        self.streamdock_knob4_rotate_accumulator += step
        if abs(self.streamdock_knob4_rotate_accumulator) < (
            _STREAMDOCK_KNOB4_ROTATE_THRESHOLD
        ):
            return True
        self.streamdock_knob4_rotate_accumulator = 0
        return False

    def _should_debounce_streamdock_panel_event(self, event: PanelInputEvent) -> bool:
        """判断真实 StreamDock panel 输入是否应因连续触发被忽略。

        入参：`event` 是 SDK 事件映射后的 logical panel 输入。
        返回：需要忽略本次事件时为 True；否则更新 touch tap 时间并返回 False。
        错误处理：无。
        副作用：当事件是未被忽略的 `TOUCH_TAP` 时更新 runtime 内存时间戳。
        """

        if event != PanelInputEvent.TOUCH_TAP:
            return False
        now = time.monotonic()
        last = self.streamdock_touch_tap_last_handled_monotonic
        if (
            last is not None
            and now - last < _STREAMDOCK_TOUCH_TAP_DEBOUNCE_SECONDS
        ):
            return True
        self.streamdock_touch_tap_last_handled_monotonic = now
        return False

    def build_current_logical_panel_background(self) -> tuple[Any, str] | None:
        """Build the current logical panel background image without recording it.

        入参：无；读取当前 `logical_panel_selection` 和已缓存的 quota/token snapshot。
        返回：`(image, source)`；当前 panel 缺少真实数据时返回占位面板，保证真实 renderer
        能启动并注册硬件输入回调。
        错误处理：Pillow 渲染异常按原语义传播。
        副作用：只创建内存图像，不修改 fake surface。
        """

        active_kind = self.logical_panel_selection.active_kind
        if active_kind == PanelKind.QUOTA and self.codex_quota_snapshot is not None:
            return render_quota_touchscreen(self.codex_quota_snapshot), "codex_quota"
        if (
            active_kind == PanelKind.TOKENS
            and self.codex_token_usage_snapshot is not None
        ):
            plan = tokens_panel_plan(
                self.codex_token_usage_snapshot,
                period=self.logical_panel_selection.token_period,
            )
            return render_logical_panel_touchscreen(plan), "codex_tokens"
        if active_kind == PanelKind.MESSAGE:
            decision = self._current_pending_decision()
            if decision is not None:
                plan = _decision_message_panel_plan(decision)
                return render_logical_panel_touchscreen(plan), "decision_message"
        return render_agent_deck_splash_touchscreen(), "agent_deck:splash"

    def render_current_logical_panel_image(self) -> Any | None:
        """Render and record the current logical panel image when possible.

        入参：无。
        返回：刚记录到 fake surface 的 image；缺少可渲染数据时返回 None。
        错误处理：Pillow 渲染异常按原语义传播。
        副作用：可能更新 fake surface 的 touchscreen image 和计数。
        """

        built = self.build_current_logical_panel_background()
        if built is None:
            return None
        image, source = built
        self.surface.render_touchscreen_image(image, source=source)
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

    def update_streamdock_n4pro_renderer_result(
        self,
        result: StreamDockN4ProAnimationResult,
        *,
        rendered_at: datetime,
    ) -> None:
        """Record the latest unified N4 Pro real-device render result.

        入参：`result` 是统一背景+按键 renderer sink 返回的结果；`rendered_at` 是本次
        下发完成时间。
        返回：无显式返回值。
        错误处理：本方法不主动抛异常；字段合法性由结果模型保证。
        副作用：更新 runtime 内存诊断字段，不直接访问硬件。
        """

        self.streamdock_n4pro_renderer_result = result
        self.streamdock_n4pro_renderer_updated_at = rendered_at
        self.streamdock_n4pro_renderer_last_error = None if result.ok else result.error

    def mark_streamdock_n4pro_renderer_error(
        self,
        error: Exception,
        *,
        rendered_at: datetime,
    ) -> None:
        """Record a unified N4 Pro renderer failure without stopping daemon.

        入参：`error` 是 frame 构建或硬件 sink 异常；`rendered_at` 是失败发生时间。
        返回：无显式返回值。
        错误处理：本方法不抛业务异常；错误文本会被截断。
        副作用：更新 renderer 诊断字段；保留上一次成功结果以便排障对比。
        """

        self.streamdock_n4pro_renderer_updated_at = rendered_at
        self.streamdock_n4pro_renderer_last_error = _short_error(error)

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
        self.logical_panel_selection = self.logical_panel_selection.model_copy(
            update={"active_kind": PanelKind.MESSAGE}
        )
        self.render_current_logical_panel_image()
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
        if not self.broker.pending():
            self.logical_panel_selection = self.logical_panel_selection.model_copy(
                update={"active_kind": PanelKind.QUOTA}
            )
        self.render_current_logical_panel_image()
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
            "key_layout": _dump_model(self.current_key_layout_response()),
            "key_layout_last_error": self.key_layout_last_error,
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
            "codex_tokens": {
                "snapshot": _dump_optional_model(self.codex_token_usage_snapshot),
                "updated_at": _dump_datetime(self.codex_token_usage_updated_at),
                "last_error": self.codex_token_usage_last_error,
            },
            "logical_panel": {
                "selection": _dump_model(self.logical_panel_selection),
                "touchscreen_render_count": self.surface.touchscreen_render_count,
                "touchscreen_image_size": _image_size(
                    self.surface.last_touchscreen_image
                ),
                "touchscreen_image_source": self.surface.last_touchscreen_image_source,
            },
            "streamdock_n4pro_renderer": {
                "last_result": _dump_optional_model(
                    self.streamdock_n4pro_renderer_result
                ),
                "updated_at": _dump_datetime(
                    self.streamdock_n4pro_renderer_updated_at
                ),
                "last_error": self.streamdock_n4pro_renderer_last_error,
            },
            "streamdock_input": {
                "event_count": self.streamdock_input_event_count,
                "last_event": self.streamdock_last_input_event,
                "recent_events": list(self.streamdock_recent_input_events),
            },
            "deck_selection": _dump_model(self.selection),
            "interaction": {
                "last_intent": _dump_optional_model(self.last_interaction_intent),
                "last_action": self.last_interaction_action,
                "recent": list(self.recent_interactions),
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
        layout = build_layout_plan(
            states,
            decisions,
            self.selection,
            key_layout=self.key_layout,
        )
        self.surface.render(layout)
        return layout

    def _current_pending_decision(self) -> PendingDecision | None:
        """读取当前 message panel 应展示的 pending decision。

        入参：无；读取 broker pending 快照和当前 selection。
        返回：优先返回 selection 指向的 pending decision，否则返回最早 pending decision。
        错误处理：无 pending 时返回 None。
        副作用：无；只读取内存 broker。
        """

        decisions = self.broker.pending()
        if not decisions:
            return None
        if self.selection.selected_decision_id is not None:
            for decision in decisions:
                if decision.decision_id == self.selection.selected_decision_id:
                    return decision
        return decisions[0]

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
    codex_app_active_sessions_reader: CodexAppActiveSessionsReader = read_codex_app_active_sessions,
    codex_quota_reader: CodexQuotaReader = read_codex_quota,
    codex_token_usage_reader: CodexTokenUsageReader = read_codex_token_usage,
    quota_touchscreen_sink: QuotaTouchscreenSink = render_touchscreen_image_to_n4pro,
    streamdock_n4pro_renderer_sink: StreamDockN4ProRendererSink | None = None,
    visible_splash_touchscreen_sink: VisibleSplashTouchscreenSink = render_dual_device_touchscreen_image_to_n4pro,
    focus_action_executor: FocusActionExecutor = focus_agent_target,
    local_app_catalog_reader: LocalAppCatalogReader = list_local_apps,
    local_app_action_executor: LocalAppActionExecutor = open_or_focus_local_app,
    app_icon_cache_path: Path | None = None,
    key_layout_path: Path | None = None,
) -> FastAPI:
    """Create the local daemon FastAPI app without binding sockets.

    入参：`poller_config` 控制是否启动 Codex App state 和 quota 后台 pollers；为空时不启用
    任何 poller，保持测试和嵌入调用无外部 I/O；`codex_app_state_event_reader` 和
    `codex_app_active_sessions_reader` 读取最近有效 Codex App 会话，生产默认只读扫描本机状态；
    `codex_quota_reader` 是可注入 reader，生产默认读取真实本机 Codex quota；
    `codex_token_usage_reader` 是可注入 reader，生产默认通过 ccusage 读取 Codex token usage；
    `quota_touchscreen_sink` 是 quota-only 真实硬件触屏下发函数，仅在配置启用时调用；
    `streamdock_n4pro_renderer_sink` 是统一背景+按钮真实硬件下发函数，测试可替换；为空时
    使用 daemon 专用 persistent sink，避免每轮渲染都 close/open N4 Pro；
    `visible_splash_touchscreen_sink` 专门写 N4 Pro dual-device 可见触屏层，用于启动/退出时
    清掉旧 quota 残留；不参与常规按键动画渲染；
    `focus_action_executor` 是 `focus_agent` 的真实动作执行器，poller config 未禁用
    `focus_actions_enabled` 且目标 agent 有 focus target 时会被调用；`local_app_catalog_reader`
    和 `local_app_action_executor` 支撑 GUI App 选择和 App key 执行，测试可替换；
    `app_icon_cache_path` 是 App 图标缓存根目录，默认使用用户级 Application Support；
    `key_layout_path` 为 None 时 GUI 布局只保存在进程内，传入路径时启动会读该 JSON，
    保存会写回该 JSON。
    返回：配置好路由且持有 in-memory runtime 的 `FastAPI` ASGI app。
    错误处理：对象构造失败会直接抛出；poller 单次失败会记录到 status，不让 app 启动失败。
    副作用：总是分配内存对象并注册路由；只有显式启用 poller 时，lifespan startup 才会只读访问
    Codex 本地状态或启动短生命周期 Codex app-server 子进程。
    """

    resolved_poller_config = poller_config or DaemonPollerConfig()
    app_icon_cache = AppIconCache(resolve_app_icon_cache_root(app_icon_cache_path))
    initial_key_layout: N4ProKeyLayout | None = None
    initial_key_layout_source: str | None = None
    key_layout_last_error: str | None = None
    if key_layout_path is not None:
        try:
            initial_key_layout = load_n4pro_key_layout(key_layout_path)
            if initial_key_layout is not None:
                initial_key_layout_source = "persisted"
        except KeyLayoutStoreError as exc:
            key_layout_last_error = str(exc)
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
        codex_token_usage_snapshot=None,
        codex_token_usage_updated_at=None,
        codex_token_usage_last_error=None,
        logical_panel_selection=PanelSelection(),
        streamdock_touch_tap_last_handled_monotonic=None,
        streamdock_knob4_rotate_accumulator=0,
        streamdock_input_event_count=0,
        streamdock_last_input_event=None,
        streamdock_recent_input_events=[],
        last_interaction_intent=None,
        last_interaction_action=None,
        recent_interactions=[],
        focus_actions_enabled=resolved_poller_config.focus_actions_enabled,
        focus_action_executor=focus_action_executor,
        local_app_catalog_reader=local_app_catalog_reader,
        local_app_action_executor=local_app_action_executor,
        app_icon_cache=app_icon_cache,
        streamdock_quota_touchscreen_result=None,
        streamdock_n4pro_renderer_result=None,
        streamdock_n4pro_renderer_updated_at=None,
        streamdock_n4pro_renderer_last_error=None,
        key_layout=initial_key_layout,
        key_layout_source=initial_key_layout_source,
        key_layout_path=key_layout_path,
        key_layout_last_error=key_layout_last_error,
    )
    resolved_streamdock_n4pro_renderer_sink: StreamDockN4ProRendererSink = (
        streamdock_n4pro_renderer_sink
        or StreamDockN4ProPersistentAnimator(
            input_callback=lambda _device, event: runtime.apply_streamdock_input_event(
                event
            )
        )
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
        if resolved_poller_config.streamdock_n4pro_renderer_enabled:
            await _render_streamdock_n4pro_visible_splash(
                visible_splash_touchscreen_sink,
            )
        if resolved_poller_config.poll_on_start:
            await _run_enabled_pollers_once(
                runtime,
                resolved_poller_config,
                codex_app_state_event_reader,
                codex_app_active_sessions_reader,
                codex_quota_reader,
                codex_token_usage_reader,
                quota_touchscreen_sink,
                resolved_streamdock_n4pro_renderer_sink,
            )
        if resolved_poller_config.codex_app_state_enabled:
            tasks.append(
                asyncio.create_task(
                    _poll_codex_app_state_loop(
                        runtime,
                        interval_seconds=resolved_poller_config.codex_app_state_interval_seconds,
                        event_reader=codex_app_state_event_reader,
                        active_sessions_reader=codex_app_active_sessions_reader,
                        scan_limit=resolved_poller_config.codex_app_state_scan_limit,
                        active_window_seconds=(
                            resolved_poller_config.codex_app_active_window_seconds
                        ),
                        active_session_limit=(
                            resolved_poller_config.codex_app_active_session_limit
                        ),
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
                        streamdock_touchscreen_enabled=(
                            resolved_poller_config.streamdock_quota_touchscreen_enabled
                            and not resolved_poller_config.streamdock_n4pro_renderer_enabled
                        ),
                        streamdock_device=resolved_poller_config.streamdock_quota_device,
                        quota_touchscreen_sink=quota_touchscreen_sink,
                    )
                )
            )
        if resolved_poller_config.codex_token_usage_enabled:
            tasks.append(
                asyncio.create_task(
                    _poll_codex_token_usage_loop(
                        runtime,
                        interval_seconds=(
                            resolved_poller_config.codex_token_usage_interval_seconds
                        ),
                        token_usage_reader=codex_token_usage_reader,
                    )
                )
            )
        if resolved_poller_config.streamdock_n4pro_renderer_enabled:
            tasks.append(
                asyncio.create_task(
                    _render_streamdock_n4pro_loop(
                        runtime,
                        interval_seconds=(
                            resolved_poller_config.streamdock_n4pro_render_interval_seconds
                        ),
                        fps=resolved_poller_config.streamdock_n4pro_renderer_fps,
                        frame_root=resolved_poller_config.streamdock_n4pro_frame_root,
                        renderer_sink=resolved_streamdock_n4pro_renderer_sink,
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
            if resolved_poller_config.streamdock_n4pro_renderer_enabled:
                await _render_streamdock_n4pro_visible_splash(
                    visible_splash_touchscreen_sink,
                )
            close_renderer = getattr(
                resolved_streamdock_n4pro_renderer_sink,
                "close",
                None,
            )
            if callable(close_renderer):
                close_renderer()

    app = FastAPI(title="Agent Deck Daemon API", lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/", response_class=HTMLResponse)
    async def get_web_index() -> HTMLResponse:
        """Return the local N4 Pro configuration GUI shell.

        入参：无。
        返回：随包分发的单页 HTML，作为本地 daemon 的默认浏览器入口。
        错误处理：若打包缺失 HTML 文件则返回 404，避免暴露文件系统细节。
        副作用：只读取包内静态 HTML，不访问真实硬件、用户配置或网络。
        """

        if not _WEB_INDEX_PATH.is_file():
            raise HTTPException(status_code=404, detail="web UI is not packaged")
        return HTMLResponse(_WEB_INDEX_PATH.read_text(encoding="utf-8"))

    @app.get("/assets/{asset_name}")
    async def get_packaged_asset(asset_name: str) -> FileResponse:
        """Return a whitelisted Agent Deck web asset.

        入参：`asset_name` 是 URL path 中的资源名；只允许本地 GUI 需要的品牌 PNG。
        返回：`FileResponse`，由 FastAPI/Starlette 流式读取包内文件。
        错误处理：未知文件名、路径穿越或文件缺失返回 404。
        副作用：只读取包内静态图片，不读取用户目录、不访问网络或硬件。
        """

        allowed_assets = {
            "logo_command_core.png",
            "n4pro_splash_command_core.png",
        }
        if asset_name not in allowed_assets:
            raise HTTPException(status_code=404, detail="unknown asset")
        asset_path = _ASSET_ROOT / asset_name
        if not asset_path.is_file():
            raise HTTPException(status_code=404, detail="asset is not packaged")
        return FileResponse(asset_path)

    @app.get("/web/{asset_name}")
    async def get_web_asset(asset_name: str) -> FileResponse:
        """Return a whitelisted CSS/JS asset for the local GUI.

        入参：`asset_name` 是 URL path 中的资源名；只允许 `index.html` 引用的
        `app.css`、`device.css`、`controls.css` 和 `app.js`。
        返回：`FileResponse`，由 FastAPI/Starlette 按扩展名设置内容类型。
        错误处理：未知文件名、路径穿越或文件缺失返回 404。
        副作用：只读取包内静态前端资源，不读取用户目录、不访问网络或硬件。
        """

        allowed_assets = {"app.css", "device.css", "controls.css", "app.js"}
        if asset_name not in allowed_assets:
            raise HTTPException(status_code=404, detail="unknown web asset")
        asset_path = _WEB_ASSET_ROOT / asset_name
        if not asset_path.is_file():
            raise HTTPException(status_code=404, detail="web asset is not packaged")
        return FileResponse(asset_path)

    @app.get("/ui/key-layout")
    async def get_key_layout() -> dict[str, Any]:
        """Return the N4 Pro key layout currently edited by the local GUI.

        入参：无。
        返回：JSON-safe `KeyLayoutResponse`，包含默认或 runtime 布局及来源。
        错误处理：默认布局构造异常按 500 暴露；正常路径不访问外部资源。
        副作用：只读取 daemon 内存，不写配置文件、不访问硬件。
        """

        return _dump_model(runtime.current_key_layout_response())

    @app.get("/ui/apps")
    async def get_local_apps() -> dict[str, Any]:
        """Return local apps that can be assigned to quick-action keys.

        入参：无。
        返回：JSON-safe app catalog，包含 App 名称、路径、bundle id 和缓存图标 URL。
        错误处理：catalog reader 异常返回 500；单个坏 App 应由 reader 自行跳过。
        副作用：调用注入的只读 app catalog reader；图标缓存缺失或过期时会写入缓存 PNG。
        """

        try:
            apps = runtime.local_app_catalog_reader()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "platform": "darwin",
            "apps": [_dump_app_for_ui(app, runtime.app_icon_cache) for app in apps],
        }

    @app.post("/ui/apps/refresh-icons")
    async def refresh_local_app_icons() -> dict[str, Any]:
        """Refresh cached icons for all currently discoverable local Apps.

        入参：无。
        返回：刷新数量和每个 App 的缓存状态。
        错误处理：catalog reader 异常返回 500；单个图标失败记录为 error 状态。
        副作用：强制重建 App icon cache PNG 和 metadata。
        """

        try:
            apps = runtime.local_app_catalog_reader()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        results = [
            _dump_app_for_ui(app, runtime.app_icon_cache, force_icon_refresh=True)
            for app in apps
        ]
        return {
            "platform": "darwin",
            "refreshed_count": sum(
                1 for app in results if app.get("icon_cache_updated") is True
            ),
            "apps": results,
        }

    @app.get("/ui/app-icons/{cache_key}/{asset_name}")
    async def get_app_icon(cache_key: str, asset_name: str) -> FileResponse:
        """Return one cached App icon PNG.

        入参：`cache_key` 是缓存目录名；`asset_name` 必须是允许的 PNG 名称。
        返回：`FileResponse`。
        错误处理：未知 cache key、未知文件名或文件不存在返回 404。
        副作用：只读取 Agent Deck icon cache 文件。
        """

        icon_path = runtime.app_icon_cache.resolve_file(cache_key, asset_name)
        if icon_path is None:
            raise HTTPException(status_code=404, detail="app icon is not cached")
        return FileResponse(icon_path)

    @app.put("/ui/key-layout")
    async def put_key_layout(layout: N4ProKeyLayout) -> dict[str, Any]:
        """Save the N4 Pro key layout in the current daemon runtime.

        入参：`layout` 是请求体中的完整 10 键布局，由 Pydantic 校验。
        返回：JSON-safe key layout response、当前 layout projection 和 render_count。
        错误处理：请求体校验失败返回 422；持久化写入失败返回 500；render 异常由 FastAPI 处理。
        副作用：更新 daemon 内存布局并重新 render fake surface；启用持久化路径时会写 JSON 文件。
        """

        try:
            return runtime.update_key_layout(layout)
        except KeyLayoutStoreError as exc:
            runtime.key_layout_last_error = str(exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

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

    @app.post("/logical-panel/input")
    async def post_logical_panel_input(
        body: LogicalPanelInputBody,
    ) -> dict[str, Any]:
        """Apply one logical panel input event.

        入参：`body` 是请求体，包含一个已归一化的 logical panel 输入事件。
        返回：JSON-safe selection 和触屏图诊断。
        错误处理：请求体校验失败返回 422；内部渲染异常由 FastAPI 处理。
        副作用：修改 runtime logical panel selection，并在可渲染时更新 fake touchscreen。
        """

        return runtime.apply_logical_panel_input(body)

    @app.post("/hardware/input")
    async def post_hardware_input(event: HardwareInput) -> dict[str, Any]:
        """Apply one low-level hardware input event.

        入参：`event` 是请求体中的 `HardwareInput` JSON，由 FastAPI 校验。
        返回：JSON-safe dict，说明输入是否被 logical panel router 处理。
        错误处理：请求体校验失败返回 422；内部渲染异常由 FastAPI 处理。
        副作用：可能更新 runtime logical panel selection，并在可渲染时更新 fake touchscreen。
        """

        return runtime.apply_hardware_input(event)

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
    codex_app_active_sessions_reader: CodexAppActiveSessionsReader,
    codex_quota_reader: CodexQuotaReader,
    codex_token_usage_reader: CodexTokenUsageReader,
    quota_touchscreen_sink: QuotaTouchscreenSink,
    streamdock_n4pro_renderer_sink: StreamDockN4ProRendererSink,
) -> None:
    """Run each enabled poller once during app startup.

    入参：`runtime` 是 daemon 内存状态；`config` 是 poller 配置；Codex reader、quota reader、
    token usage reader、触屏 sink 和统一 N4 Pro renderer sink 是可注入数据源/输出端。
    返回：无显式返回值。
    错误处理：单个 poller 的异常由 poll-once helper 记录，另一个 poller 仍会继续执行。
    副作用：可能只读访问 Codex 本地状态、启动短生命周期 app-server、更新 runtime 和 fake surface。
    """

    if config.codex_app_state_enabled:
        await _poll_codex_app_state_once(
            runtime,
            codex_app_state_event_reader,
            active_sessions_reader=codex_app_active_sessions_reader,
            scan_limit=config.codex_app_state_scan_limit,
            active_window_seconds=config.codex_app_active_window_seconds,
            active_session_limit=config.codex_app_active_session_limit,
        )
    if config.codex_quota_enabled:
        await _poll_codex_quota_once(
            runtime,
            timeout_seconds=config.codex_quota_timeout_seconds,
            quota_reader=codex_quota_reader,
            streamdock_touchscreen_enabled=(
                config.streamdock_quota_touchscreen_enabled
                and not config.streamdock_n4pro_renderer_enabled
            ),
            streamdock_device=config.streamdock_quota_device,
            quota_touchscreen_sink=quota_touchscreen_sink,
        )
    if config.codex_token_usage_enabled:
        await _poll_codex_token_usage_once(
            runtime,
            token_usage_reader=codex_token_usage_reader,
        )
    if config.streamdock_n4pro_renderer_enabled:
        await _render_streamdock_n4pro_once(
            runtime,
            duration_seconds=config.streamdock_n4pro_render_interval_seconds,
            fps=config.streamdock_n4pro_renderer_fps,
            frame_root=config.streamdock_n4pro_frame_root,
            renderer_sink=streamdock_n4pro_renderer_sink,
        )


async def _poll_codex_app_state_loop(
    runtime: _DaemonRuntime,
    *,
    interval_seconds: float,
    event_reader: CodexAppStateEventReader,
    active_sessions_reader: CodexAppActiveSessionsReader,
    scan_limit: int,
    active_window_seconds: int,
    active_session_limit: int,
) -> None:
    """Periodically scan Codex App local state and apply generated events.

    入参：`runtime` 是 daemon 内存状态；`interval_seconds` 是两次扫描间隔；`event_reader`
    和 `active_sessions_reader` 是同步 reader，会通过 `asyncio.to_thread` 调用；其余参数
    控制最近有效会话筛选。
    返回：不主动返回；任务被取消时结束。
    错误处理：单次扫描异常被记录到 runtime，不终止循环；取消异常正常传播给 shutdown。
    副作用：周期性只读访问 Codex 本地状态并更新 in-memory store。
    """

    while True:
        await asyncio.sleep(interval_seconds)
        await _poll_codex_app_state_once(
            runtime,
            event_reader,
            active_sessions_reader=active_sessions_reader,
            scan_limit=scan_limit,
            active_window_seconds=active_window_seconds,
            active_session_limit=active_session_limit,
        )


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


async def _poll_codex_token_usage_loop(
    runtime: _DaemonRuntime,
    *,
    interval_seconds: float,
    token_usage_reader: CodexTokenUsageReader,
) -> None:
    """Periodically refresh Codex token usage and update the active panel.

    入参：`runtime` 是 daemon 内存状态；`interval_seconds` 是两次刷新间隔；
    `token_usage_reader` 是同步 reader，生产默认会通过 ccusage 读取 Codex token usage。
    返回：不主动返回；任务被取消时结束。
    错误处理：单次读取或渲染异常被记录到 runtime，不终止循环。
    副作用：周期性执行 token usage reader，并在 tokens panel active 时渲染内存触屏图。
    """

    while True:
        await asyncio.sleep(interval_seconds)
        await _poll_codex_token_usage_once(
            runtime,
            token_usage_reader=token_usage_reader,
        )


async def _render_streamdock_n4pro_loop(
    runtime: _DaemonRuntime,
    *,
    interval_seconds: float,
    fps: int,
    frame_root: Path,
    renderer_sink: StreamDockN4ProRendererSink,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Periodically render current layout and quota together to N4 Pro.

    入参：`runtime` 是 daemon 内存状态；`interval_seconds` 是每次硬件播放窗口时长；
    `fps` 是按钮动画刷新率；`frame_root` 是 generated Codex key frames 根目录；
    `renderer_sink` 是真实或测试替换的统一硬件 sink；`sleep`/`monotonic` 仅用于测试调度。
    返回：不主动返回；任务被取消时结束。
    错误处理：单次 frame 构建或硬件 sink 异常被记录到 runtime，不终止循环。
    副作用：周期性 render 当前 layout，并在线程中调用真实硬件 sink。
    """

    while True:
        started_at = monotonic()
        await _render_streamdock_n4pro_once(
            runtime,
            duration_seconds=interval_seconds,
            fps=fps,
            frame_root=frame_root,
            renderer_sink=renderer_sink,
        )
        elapsed = monotonic() - started_at
        await sleep(max(0.0, interval_seconds - elapsed))


async def _render_streamdock_n4pro_once(
    runtime: _DaemonRuntime,
    *,
    duration_seconds: float,
    fps: int,
    frame_root: Path,
    renderer_sink: StreamDockN4ProRendererSink,
) -> None:
    """Render one bounded N4 Pro hardware frame window from current runtime.

    入参：`runtime` 是 daemon 内存状态；`duration_seconds` 是本次硬件播放时长；
    `fps` 是按钮动画刷新率；`frame_root` 是 generated frame 根目录；`renderer_sink`
    是统一背景+按键下发函数。
    返回：无显式返回值。
    错误处理：缺少当前 logical panel 所需 snapshot 时跳过不报错；其他构建或 sink 异常记录为 renderer error。
    副作用：会 render 当前 layout 到 fake surface，并可能在线程中接管真实 N4 Pro。
    """

    rendered_at = datetime.now(UTC)
    try:
        panel_background = runtime.build_current_logical_panel_background()
        if panel_background is None:
            return
        background, _source = panel_background
        layout = runtime.render_current()
        key_frame_paths = _key_frame_paths_from_layout(
            layout,
            frame_root=frame_root,
        )
        key_images = _key_images_from_layout(
            layout,
            app_icon_cache=runtime.app_icon_cache,
        )
        result = await asyncio.to_thread(
            renderer_sink,
            background_image=background,
            key_frame_paths=key_frame_paths,
            key_images=key_images,
            duration_seconds=duration_seconds,
            fps=fps,
        )
        runtime.update_streamdock_n4pro_renderer_result(
            result,
            rendered_at=datetime.now(UTC),
        )
    except Exception as exc:  # noqa: BLE001 - 硬件渲染失败应进入 status，不杀 daemon。
        runtime.mark_streamdock_n4pro_renderer_error(exc, rendered_at=rendered_at)


async def _render_streamdock_n4pro_visible_splash(
    touchscreen_sink: VisibleSplashTouchscreenSink,
) -> None:
    """把 N4 Pro 可见 touch layer 尽量改成 Agent Deck 默认图。

    入参：`touchscreen_sink` 是 dual-device 可见触屏层 sink。
    返回：无显式返回值。
    错误处理：启动/退出阶段不应阻塞 daemon，渲染或硬件异常会被吞掉。
    副作用：可能在线程中调用真实硬件 sink，覆盖旧 quota 等 dual-device 残留层。
    """

    with suppress(Exception):
        await asyncio.to_thread(
            touchscreen_sink,
            render_agent_deck_splash_touchscreen(),
        )


def _decision_message_panel_plan(decision: PendingDecision) -> LogicalPanelPlan:
    """把 pending decision 转换成 message logical panel。

    入参：`decision` 是当前待用户审批的 broker 快照。
    返回：`kind=message` 的 logical panel plan，包含工具名、来源 agent 和审批原因。
    错误处理：模型字段非法时由 Pydantic 报告。
    副作用：无；只创建内存展示计划。
    """

    return message_panel_plan(
        title="Approval needed",
        lines=(
            f"Tool: {decision.tool_name}",
            f"Agent: {decision.agent_key}",
            decision.reason,
        ),
    )


def _key_frame_paths_from_layout(
    layout: LayoutPlan,
    *,
    frame_root: Path,
) -> dict[int, tuple[Path, ...]]:
    """从 renderer-neutral layout 提取 N4 Pro 物理按钮动画帧路径。

    入参：`layout` 是当前 daemon layout；`frame_root` 是 generated Codex key frame 根目录。
    返回：物理按钮编号到 PNG 帧路径元组的映射；只包含带 visual 的前 10 个 agent slot。
    错误处理：帧目录缺失、按钮编号非法或变体缺帧时抛出异常，由调用方记录。
    副作用：只读取文件系统元数据；不打开图片、不访问硬件。
    """

    key_variants = {
        key.index + 1: key.visual.variant_id
        for key in layout.keys[:10]
        if key.visual is not None
    }
    return codex_key_frame_paths_for_key_variants(
        frame_root=frame_root,
        key_variants=key_variants,
    )


def _key_images_from_layout(
    layout: LayoutPlan,
    *,
    app_icon_cache: AppIconCache | None = None,
) -> dict[int, Any]:
    """从 layout 提取 N4 Pro 静态主键图片。

    入参：`layout` 是当前 daemon layout；`app_icon_cache` 是可选 App 图标缓存。
    返回：物理按钮编号到 Pillow 图像的映射；当前只包含 App quick-action 主键。
    错误处理：单个 App 图标读取失败会 fallback 成 token 图，不影响整轮渲染。
    副作用：可能只读 `.app` bundle 图标资源；不访问硬件、不启动 App。
    """

    key_images: dict[int, Any] = {}
    for key in layout.keys[:10]:
        if key.kind != "app":
            continue
        app_name = key.payload.get("app_name") or key.label
        app_path = key.payload.get("app_path")
        bundle_id = key.payload.get("bundle_id")
        icon_token = key.payload.get("icon_token")
        icon_color = key.payload.get("icon_color")
        cached_image = None
        if app_icon_cache is not None:
            cached_image = app_icon_cache.key_image_for_binding(
                app_name=app_name,
                app_path=app_path,
                bundle_id=bundle_id,
                icon_token=icon_token,
                icon_color=icon_color,
            )
        key_images[key.index + 1] = cached_image or render_app_key_image(
            app_name=app_name,
            app_path=app_path,
            icon_token=icon_token,
            icon_color=icon_color,
        )
    return key_images


def _active_tool_from_reason(reason: str) -> str | None:
    """从 Codex active session reason 中提取活跃工具名。

    入参：`reason` 是 adapter 生成的诊断字符串，当前可能形如 `pending tool call: shell`。
    返回：工具名；不是该格式或工具名为空时返回 None。
    错误处理：本函数不抛业务异常。
    副作用：无；只处理内存字符串。
    """

    prefix = "pending tool call: "
    if not reason.startswith(prefix):
        return None
    tool_name = reason[len(prefix) :].strip()
    return tool_name or None


async def _poll_codex_app_state_once(
    runtime: _DaemonRuntime,
    event_reader: CodexAppStateEventReader,
    *,
    active_sessions_reader: CodexAppActiveSessionsReader,
    scan_limit: int,
    active_window_seconds: int,
    active_session_limit: int,
) -> None:
    """Run one Codex App state poll and update runtime diagnostics.

    入参：`runtime` 是 daemon 内存状态；`event_reader` 返回 pending-input normalized events；
    `active_sessions_reader` 返回最近有效 Codex App 会话；其余参数控制扫描和筛选。
    返回：无显式返回值。
    错误处理：reader 或 reducer 异常会被捕获并记录到 `codex_app_state_last_error`。
    副作用：可能更新 agent state store 和 fake layout render。
    """

    polled_at = datetime.now(UTC)
    try:
        events = await asyncio.to_thread(event_reader)
        runtime.apply_polled_events(events)
        sessions = await asyncio.to_thread(
            active_sessions_reader,
            scan_limit=scan_limit,
            active_window_seconds=active_window_seconds,
            max_sessions=active_session_limit,
        )
        runtime.apply_codex_active_sessions(sessions, observed_at=polled_at)
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


async def _poll_codex_token_usage_once(
    runtime: _DaemonRuntime,
    *,
    token_usage_reader: CodexTokenUsageReader,
) -> None:
    """Run one Codex token usage poll and update runtime diagnostics.

    入参：`runtime` 是 daemon 内存状态；`token_usage_reader` 返回 `CodexTokenUsageSnapshot`。
    返回：无显式返回值。
    错误处理：reader 或触屏渲染异常会被捕获并记录到 `codex_token_usage_last_error`。
    副作用：可能执行 ccusage，并在成功时更新 token usage snapshot 和 active logical panel 图。
    """

    polled_at = datetime.now(UTC)
    try:
        snapshot = await asyncio.to_thread(token_usage_reader)
        runtime.update_codex_token_usage(snapshot, updated_at=polled_at)
    except Exception as exc:  # noqa: BLE001 - poller 必须保护 daemon 主循环。
        runtime.mark_codex_token_usage_poll_error(exc, polled_at=polled_at)


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


def _dump_app_for_ui(
    app: LocalAppInfo,
    app_icon_cache: AppIconCache,
    *,
    force_icon_refresh: bool = False,
) -> dict[str, Any]:
    """把 App catalog 条目序列化为 GUI 可消费的 dict。

    入参：`app` 是本机 App metadata；`app_icon_cache` 是图标缓存；`force_icon_refresh`
    控制是否强制重建缓存。
    返回：包含原始 App 字段、`icon_url`、`key_icon_url` 和缓存状态的 dict。
    错误处理：图标缓存写入失败时返回 error 状态，不让整个 App catalog 失败。
    副作用：缓存缺失、过期或强制刷新时可能写 PNG/metadata 文件。
    """

    payload = _dump_model(app)
    try:
        cached = app_icon_cache.ensure_for_app(app, force=force_icon_refresh)
    except OSError as exc:
        payload.update(
            {
                "icon_url": app.icon_data_url,
                "key_icon_url": None,
                "icon_cache_key": None,
                "icon_cache_status": "error",
                "icon_cache_error": str(exc),
                "icon_cache_updated": False,
            }
        )
        return payload
    payload.update(
        {
            "icon_url": cached.icon_url,
            "key_icon_url": cached.key_icon_url,
            "icon_cache_key": cached.cache_key,
            "icon_cache_status": cached.status,
            "icon_cache_updated": cached.updated,
        }
    )
    return payload


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


def _dry_run_action(
    intent: InteractionIntent,
    state: AgentState | None,
) -> dict[str, Any]:
    """返回第一阶段 action dry-run 诊断。

    入参：`intent` 是待执行交互意图；`state` 是目标 agent 当前状态，可空。
    返回：包含 action 状态、目标和说明的 JSON-safe dict。
    错误处理：未知 intent 使用通用 dry-run 文案。
    副作用：无。
    """

    if intent.intent == "focus_agent":
        focus_target = state.focus_target if state is not None else None
        target_available = focus_target is not None
        message = (
            f"focus_agent dry-run recorded for {focus_target}"
            if target_available
            else "focus_agent dry-run recorded; missing focus target"
        )
        return {
            "intent": intent.intent,
            "agent_key": intent.agent_key,
            "decision_id": intent.decision_id,
            "status": "dry_run",
            "target_available": target_available,
            "focus_target": focus_target,
            "message": message,
        }
    if intent.intent == "open_or_focus_app":
        app_target = (
            intent.payload.get("bundle_id")
            or intent.payload.get("app_path")
            or intent.payload.get("app_name")
        )
        return {
            "intent": intent.intent,
            "agent_key": intent.agent_key,
            "decision_id": intent.decision_id,
            "status": "dry_run",
            "target_available": app_target is not None,
            "app_name": intent.payload.get("app_name"),
            "app_path": intent.payload.get("app_path"),
            "bundle_id": intent.payload.get("bundle_id"),
            "message": (
                f"open_or_focus_app dry-run recorded for {app_target}"
                if app_target
                else "open_or_focus_app dry-run recorded; missing app target"
            ),
        }
    return {
        "intent": intent.intent,
        "agent_key": intent.agent_key,
        "decision_id": intent.decision_id,
        "status": "dry_run",
        "message": f"{intent.intent} dry-run recorded; no external action executed",
    }


def _missing_decision_action(
    intent: InteractionIntent,
    behavior: DecisionBehavior,
) -> dict[str, Any]:
    """返回 approval intent 找不到 decision 时的诊断。

    入参：`intent` 是 approval 交互意图；`behavior` 是它想要写入的 allow/deny。
    返回：JSON-safe action 诊断。
    错误处理：无。
    副作用：无。
    """

    return {
        "intent": intent.intent,
        "agent_key": intent.agent_key,
        "decision_id": intent.decision_id,
        "status": "missing_decision",
        "ok": False,
        "behavior": behavior.value,
        "message": "approval intent ignored; missing pending decision",
    }


def _missing_focus_target_action(intent: InteractionIntent) -> dict[str, Any]:
    """返回 focus intent 找不到目标时的诊断。

    入参：`intent` 是待执行的 focus intent。
    返回：JSON-safe action 诊断，说明当前 agent 尚无可执行 focus target。
    错误处理：无。
    副作用：无。
    """

    return {
        "intent": "focus_agent",
        "agent_key": intent.agent_key,
        "decision_id": intent.decision_id,
        "status": "missing_target",
        "ok": False,
        "target_available": False,
        "focus_target": None,
        "message": "focus_agent ignored; missing focus target",
    }


def _hardware_decision_message(behavior: DecisionBehavior) -> str:
    """返回硬件审批写入 broker result 的可读说明。

    入参：`behavior` 是用户通过硬件选择的 allow/deny。
    返回：稳定英文说明，供 Codex hook 或 status 诊断展示。
    错误处理：无。
    副作用：无。
    """

    if behavior == DecisionBehavior.ALLOW:
        return "Approved by Agent Deck hardware."
    return "Denied by Agent Deck hardware."


def _execute_focus_action(
    intent: InteractionIntent,
    state: AgentState | None,
    focus_action_executor: FocusActionExecutor,
) -> dict[str, Any]:
    """执行显式启用后的 `focus_agent` 动作并返回诊断。

    入参：`intent` 是硬件输入归一化后的 focus intent；`state` 是目标 agent 当前状态；
    `focus_action_executor` 是受配置保护的真实动作执行器。
    返回：JSON-safe action 诊断；缺少 focus target 时退回 dry-run 缺目标诊断。
    错误处理：executor 自己负责把系统异常转换为 `FocusActionResult`，本函数只序列化结果。
    副作用：当存在 focus target 时会调用 executor，可能激活本机窗口。
    """

    focus_target = state.focus_target if state is not None else None
    if focus_target is None:
        return _missing_focus_target_action(intent)
    result = focus_action_executor(focus_target)
    return {
        "intent": intent.intent,
        "agent_key": intent.agent_key,
        "decision_id": intent.decision_id,
        "status": result.status,
        "ok": result.ok,
        "target_available": True,
        "focus_target": result.focus_target,
        "message": result.message,
    }


def _execute_local_app_action(
    intent: InteractionIntent,
    local_app_action_executor: LocalAppActionExecutor,
) -> dict[str, Any]:
    """执行 App quick-action 并返回诊断。

    入参：`intent` 是 `open_or_focus_app` 交互意图；payload 中可包含 `app_name`、
    `app_path` 和 `bundle_id`；`local_app_action_executor` 是受配置保护的真实动作执行器。
    返回：JSON-safe action 诊断。
    错误处理：executor 自己负责把系统异常转换为 `LocalAppActionResult`，本函数只序列化结果。
    副作用：当 executor 为生产实现时会调用 macOS `open` 打开或切换 App。
    """

    result = local_app_action_executor(
        app_name=intent.payload.get("app_name"),
        app_path=intent.payload.get("app_path"),
        bundle_id=intent.payload.get("bundle_id"),
    )
    return {
        "intent": intent.intent,
        "agent_key": intent.agent_key,
        "decision_id": intent.decision_id,
        "status": result.status,
        "ok": result.ok,
        "app_name": result.app_name,
        "app_path": result.app_path,
        "bundle_id": result.bundle_id,
        "message": result.message,
    }


def _dump_event_field(value: object) -> object:
    """把 SDK enum-like 字段转换成 status 可读值。

    入参：`value` 是 SDK enum、字符串、数字或 None。
    返回：优先返回 `.value` 字符串，其次返回原始 JSON 基础值，其他对象退化为字符串。
    错误处理：无。
    副作用：无；只读取对象属性。
    """

    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _dump_event_key(event: object) -> object:
    """从 SDK-like event 中读取可序列化的物理 button key。

    入参：`event` 是官方 SDK 输入事件或测试替身，`key` 可能是 enum-like 对象。
    返回：优先返回 `event.key.value`，否则返回 `event.key` 的 JSON-safe 表示；缺失时返回 None。
    错误处理：无。
    副作用：无。
    """

    key = getattr(event, "key", None)
    return _dump_event_field(getattr(key, "value", key))


def _append_bounded(
    items: list[Any],
    item: Any,
    *,
    limit: int,
) -> None:
    """向内存诊断列表追加一项并保留最新的有限条数。

    入参：`items` 是 runtime 持有的可变列表；`item` 是新诊断；`limit` 是保留上限。
    返回：无。
    错误处理：非正 limit 会清空历史，只保留空列表语义。
    副作用：原地修改 `items`。
    """

    items.append(item)
    if limit <= 0:
        items.clear()
        return
    extra = len(items) - limit
    if extra > 0:
        del items[:extra]


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
