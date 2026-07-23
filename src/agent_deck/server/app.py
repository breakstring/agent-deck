"""FastAPI application factory for the local Agent Deck daemon API.

This module wires the MVP in-memory runtime for normalized events, approval
decisions, layout planning, optional Codex pollers, a fake hardware surface, and
optional real N4 Pro render sinks. It deliberately does not bind sockets or install
hooks. 当调用方提供用户级路径时，配置路由可持久化 N4 Pro key、rotary 和 PETS 设置；
CLI entry point 负责 hosting，并决定是否启用 Codex local-state/quota polling、
ChatGPT Settings-gated read-only SSH Remote observation 与真实 StreamDock rendering。
"""

from __future__ import annotations

import asyncio
import math
import base64
import binascii
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from agent_deck.actions.app_icon_cache import (
    AppIconCache,
    resolve_app_icon_cache_root,
)
from agent_deck.actions.key_icon_store import (
    ShortcutIconStore,
    resolve_shortcut_icon_store_root,
)
from agent_deck.actions.keyboard import (
    KeyboardShortcutExecutor,
    KeyboardShortcutScheduler,
    KeyboardShortcutSpec,
)
from agent_deck.actions.macos_keyboard import (
    create_default_keyboard_shortcut_executor,
    open_macos_accessibility_settings,
)
from agent_deck.actions.apps import (
    LocalAppActionResult,
    LocalAppInfo,
    list_local_apps,
    open_or_focus_local_app,
)
from agent_deck.actions.focus import FocusActionResult, focus_agent_target
from agent_deck.actions.local_targets import (
    LocalTargetActionResult,
    open_local_url,
)
from agent_deck.actions.system_controls import (
    SystemControlExecutor,
    create_default_system_control_executor,
)
from agent_deck.actions.url_icon_cache import (
    CachedUrlIcon,
    UrlIconCache,
    UrlIconFetcher,
    origin_for_url,
    resolve_url_icon_cache_root,
)
from agent_deck.adapters.codex_app_state import (
    CodexAppActiveSession,
    build_codex_app_state_events,
    read_codex_app_active_sessions,
)
from agent_deck.adapters.codex_quota import CodexQuotaSnapshot, read_codex_quota
from agent_deck.adapters.codex_remote_ssh import (
    CodexRemoteSshObserver,
    CodexRemoteSshDiscoverySnapshot,
    CodexRemoteSshEnabledHost,
    CodexRemoteSshSnapshot,
    codex_remote_host_id,
    discover_enabled_codex_remote_ssh_hosts,
)
from agent_deck.adapters.codex_remote_pet_mirror import (
    CodexRemotePetMirror,
    CodexRemotePetMirrorResolution,
)
from agent_deck.adapters.codex_pet import CodexPetResolver
from agent_deck.adapters.codex_tokens import (
    CodexTokenPeriod,
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
from agent_deck.config import (
    CodexPetMotion,
    CodexPetPatrolSpeed,
    CodexRemotePetSource,
)
from agent_deck.hardware.fake import FakeHardwareSurface, HardwareInput
from agent_deck.hardware.capabilities import get_device_profile
from agent_deck.hardware.streamdock_n4pro import (
    StreamDockN4ProAnimationResult,
    StreamDockN4ProPersistentAnimator,
    streamdock_sdk_result_failed,
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
from agent_deck.input.rotary import (
    RotaryInputIntent,
    rotary_input_from_hardware_input,
    rotary_input_from_streamdock_input_event,
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
    PanelContentDirection,
    PanelInputEvent,
    PanelKind,
    PanelSelection,
    apply_panel_input,
    cycle_panel_content,
    cycle_virtual_panel,
    message_panel_plan,
    pets_panel_plan,
)
from agent_deck.rendering.control_feedback import (
    ControlFeedback,
    ControlFeedbackKind,
    feedback_is_active,
    render_control_feedback_touchscreen,
)
from agent_deck.rendering.n4pro_panel import N4PRO_LOGICAL_PANEL_VIEWPORT
from agent_deck.rendering.rotary_surface import (
    N4ProRotaryLayout,
    RotaryPressAction,
    RotaryRotateAction,
    default_n4pro_rotary_layout,
)
from agent_deck.rendering.logical_panel_touchscreen import (
    render_logical_panel_touchscreen,
    render_token_usage_touchscreen,
)
from agent_deck.rendering.quota_touchscreen import render_quota_touchscreen
from agent_deck.rendering.status_key import (
    QuotaStatusWindow,
    render_quota_status_key_image,
    render_usage_summary_key_image,
)
from agent_deck.rendering.shortcut_key import ShortcutKeyImageCache
from agent_deck.rendering.url_key import render_url_key_image, token_for_url
from agent_deck.server.key_layout_store import (
    KeyLayoutStoreError,
    load_n4pro_key_layout,
    save_n4pro_key_layout,
)
from agent_deck.server.rotary_layout_store import (
    RotaryLayoutStoreError,
    load_n4pro_rotary_layout,
    save_n4pro_rotary_layout,
)
from agent_deck.server.quota_presentation_store import (
    QuotaPresentation,
    QuotaPresentationStoreError,
    load_quota_presentation,
)
from agent_deck.server.codex_pet_runtime import CodexPetRuntime, ReducedMotionReader
from agent_deck.server.pets_panel_settings_store import (
    N4ProPetsPanelSettings,
    PetsPanelSettingsStoreError,
    load_n4pro_pets_panel_settings,
    save_n4pro_pets_panel_settings,
)

CodexAppStateEventReader = Callable[[], tuple[NormalizedEvent, ...]]
CodexAppActiveSessionsReader = Callable[..., tuple[CodexAppActiveSession, ...]]
CodexRemoteSshHostsReader = Callable[[], CodexRemoteSshDiscoverySnapshot]
CodexQuotaReader = Callable[..., CodexQuotaSnapshot]
CodexTokenUsageReader = Callable[[], CodexTokenUsageSnapshot]
QuotaTouchscreenSink = Callable[[Any], StreamDockTouchscreenRenderResult]
StreamDockN4ProRendererSink = Callable[..., StreamDockN4ProAnimationResult]
FocusActionExecutor = Callable[[str], FocusActionResult]
VisibleSplashTouchscreenSink = Callable[[Any], StreamDockTouchscreenRenderResult]
LocalAppCatalogReader = Callable[[], tuple[LocalAppInfo, ...]]
LocalAppActionExecutor = Callable[..., LocalAppActionResult]
LocalUrlActionExecutor = Callable[..., LocalTargetActionResult]
KeyboardAccessibilitySettingsOpener = Callable[[], None]
"""由显式 GUI 操作调用、用于打开 macOS 辅助功能设置的可注入函数。"""


class CodexRemoteSshObserverProtocol(Protocol):
    """定义 daemon 所需的远端 SSH observer 最小接口。

    入参：实现必须提供稳定 ``host``/``host_id``、同步 ``read_snapshot`` 和幂等 ``close``。
    返回：Protocol 仅用于类型检查和测试注入。
    错误处理：读取异常由 daemon poller 捕获；close 应 best-effort。
    副作用：协议自身无副作用，生产实现会维护独立 SSH 子进程。
    """

    host: str
    host_id: str

    def read_snapshot(self) -> CodexRemoteSshSnapshot:
        """读取一次脱敏远端状态快照。

        入参：无。
        返回：不含 prompt/turn/item 的 ``CodexRemoteSshSnapshot``。
        错误处理：连接或协议异常向调用 poller 传播。
        副作用：实现可以使用自己拥有的 SSH 连接，但不得修改远端 thread。
        """

        ...

    def close(self) -> None:
        """释放 observer 自己持有的连接。

        入参：无。
        返回：无。
        错误处理：实现应 best-effort，避免阻断 daemon shutdown。
        副作用：只关闭 observer 自己创建的连接和子进程。
        """

        ...


class CodexRemotePetMirrorProtocol(Protocol):
    """定义 daemon poller 所需的远端 custom 宠物镜像接口。

    入参：实现接收已启用 Connection 的 alias、observer host id 和 config/read 选择。
    返回：经过完整校验或安全降级的镜像结果。
    错误处理：生产实现收敛 I/O 错误；测试 fake 可抛异常以验证 poller 隔离。
    副作用：协议自身无副作用，生产实现可只读 SFTP 并写 Agent Deck 自有缓存。
    """

    def resolve(
        self,
        *,
        host: str,
        host_id: str,
        selected_avatar_id: str | None,
        now: datetime | None = None,
    ) -> CodexRemotePetMirrorResolution:
        """解析一个已启用 host 当前选择的 custom 宠物。

        入参：host/host_id/selection 由成功 observer snapshot 和 discovery 共同提供。
        返回：冻结镜像结果。
        错误处理：异常由远端 poller 捕获，不能终止其他主机状态读取。
        副作用：实现可执行受限只读 SFTP 和本地缓存写入。
        """

        ...


CodexRemoteSshObserverFactory = Callable[
    [CodexRemoteSshEnabledHost],
    CodexRemoteSshObserverProtocol,
]
"""按 ChatGPT 已启用 SSH connection 创建独立只读 observer 的可注入工厂。"""

_STREAMDOCK_TOUCH_TAP_DEBOUNCE_SECONDS = 0.45
"""真实 StreamDock touch bar 连续 touch_point 归并为一次 tap 的最短间隔。"""

_STREAMDOCK_KNOB4_ROTATE_THRESHOLD = 1
"""真实 StreamDock 第 4 旋钮每收到一个 rotate 事件即切换一次 token 周期。"""

_RECENT_STREAMDOCK_INPUT_LIMIT = 30
"""status 中保留多少条最近真实 StreamDock 输入事件诊断。"""

_RECENT_INTERACTION_LIMIT = 30
"""status 中保留多少条最近业务 interaction 诊断。"""

_MAX_LOGICAL_PANEL_IMAGE_CACHE_ENTRIES = 10
"""基础 logical panel 背景图的进程内缓存上限，覆盖品牌、2 个 quota 与 4 个 usage 周期。"""

_BRAND_FEEDBACK_DURATION_SECONDS = 4.0
"""未配置主按键触发默认品牌反馈面板的持续秒数。"""

_CONTROL_FEEDBACK_DURATION_SECONDS = 1.5
"""音量、亮度、静音和错误 HUD 在停止输入后保留的秒数。"""

_MAX_STATUS_KEY_IMAGE_CACHE_ENTRIES = 64
"""状态按键图片缓存最多保留多少张 Pillow image，避免长时间运行无限增长。"""

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
    最近有效会话筛选；``codex_remote_ssh_*`` 控制仅跟随 ChatGPT Settings 已启用 connection
    的独立只读 app-server proxy、轮询/超时、thread 上限、失联清理和完成反馈；
    `codex_quota_enabled` 控制是否读取 Codex app-server quota；
    `codex_quota_interval_seconds` 是 quota 间隔，默认 5 分钟；
    `codex_quota_timeout_seconds` 是单次 app-server 读取超时；
    `codex_pet_enabled`、刷新间隔、面板 FPS 与 motion 控制只读 Codex 宠物展示；
    `streamdock_quota_touchscreen_enabled` 控制是否把 quota 触屏图下发到真实硬件；
    `streamdock_quota_device` 是目标设备能力 profile，当前只支持 `n4pro`；
    `streamdock_n4pro_renderer_enabled` 控制是否启用统一 N4 Pro 背景+按钮渲染循环，启用后
    应替代 quota-only 真实触屏下发；`streamdock_n4pro_frame_root` 指向 generated 按键帧；
    `streamdock_n4pro_render_interval_seconds` 和 `streamdock_n4pro_renderer_fps` 控制真机刷新节奏；
    `focus_actions_enabled` 控制是否允许 `focus_agent` 调用真实 action executor，默认开启；
    `local_actions_enabled` 控制是否允许 App/URL/Folder 本机快捷动作调用真实 executor；
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
    codex_remote_ssh_enabled: bool = False
    codex_remote_ssh_interval_seconds: float = Field(default=5.0, gt=0)
    codex_remote_ssh_timeout_seconds: float = Field(default=10.0, gt=0)
    codex_remote_ssh_thread_limit: int = Field(default=80, gt=0, le=200)
    codex_remote_ssh_stale_after_seconds: float = Field(default=20.0, gt=0)
    codex_remote_ssh_completed_feedback_seconds: float = Field(
        default=10.0,
        ge=0,
        le=60,
    )
    codex_quota_enabled: bool = False
    codex_quota_interval_seconds: float = Field(default=300.0, gt=0)
    codex_quota_timeout_seconds: float = Field(default=10.0, gt=0)
    codex_token_usage_enabled: bool = False
    codex_token_usage_interval_seconds: float = Field(default=300.0, gt=0)
    codex_pet_enabled: bool = False
    codex_pet_refresh_interval_seconds: float = Field(default=5.0, gt=0)
    codex_pet_panel_fps: int = Field(default=8, ge=1, le=20)
    codex_pet_motion: CodexPetMotion = CodexPetMotion.AUTO
    codex_pet_remote_pet_source: CodexRemotePetSource = (
        CodexRemotePetSource.BUILTIN_RANDOM
    )
    codex_pet_patrol_speed: CodexPetPatrolSpeed = CodexPetPatrolSpeed.MEDIUM
    streamdock_quota_touchscreen_enabled: bool = False
    streamdock_quota_device: str = "n4pro"
    streamdock_n4pro_renderer_enabled: bool = False
    streamdock_n4pro_frame_root: Path = Path("assets/codex/generated/n4pro-key-112-fps10")
    streamdock_n4pro_render_interval_seconds: float = Field(default=3.0, gt=0)
    streamdock_n4pro_renderer_fps: int = Field(default=10, gt=0)
    focus_actions_enabled: bool = True
    local_actions_enabled: bool = True
    poll_on_start: bool = True


class UrlIconUploadRequest(BaseModel):
    """URL 图标本地上传请求体。

    入参：`url` 是 URL key 的目标网址；`filename` 是浏览器侧文件名；`data_url` 是浏览器
    FileReader 生成的 base64 data URL。
    返回：Pydantic model，供 `/ui/url-icons/upload` 使用。
    错误处理：缺失字段或空字符串由 Pydantic 校验为 422。
    副作用：模型自身不解析图片、不写文件。
    """

    url: str = Field(min_length=1)
    filename: str | None = None
    data_url: str = Field(min_length=1)


class ShortcutIconUploadRequest(BaseModel):
    """快捷键自定义图标上传请求体。

    入参：``filename`` 是浏览器侧文件名；``data_url`` 是 FileReader 生成的 base64 图片。
    返回：frozen 请求模型，供 ``/ui/shortcut-icons/upload`` 使用。
    错误处理：缺少 data_url 或空字符串由 Pydantic/FastAPI 校验为 422。
    副作用：模型自身不解码图片、不写资产。
    """

    model_config = ConfigDict(frozen=True)

    filename: str | None = None
    data_url: str = Field(min_length=1)


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


class RotaryLayoutResponse(BaseModel):
    """向 GUI 返回当前可编辑或已持久化的 N4 Pro 旋钮配置。

    入参：`device_profile` 标识能力来源；`source` 说明默认、runtime 或 persisted；`path` 是
    可选 JSON 路径；`layout` 是四个旋钮、灯光组和整体亮度的完整配置。
    返回：frozen Pydantic model，可直接 FastAPI 序列化。
    错误处理：字段非法由 Pydantic 报告。
    副作用：模型本身不读写文件、系统或硬件。
    """

    model_config = ConfigDict(frozen=True)

    device_profile: str
    source: str
    path: str | None = None
    layout: N4ProRotaryLayout


class ConsoleConfigurationApplyRequest(BaseModel):
    """描述一次“保存并应用”提交的主按键、旋钮与 PETS 设置草稿。

    入参：`key_layout` 是完整 10 键主表面配置；`rotary_layout` 是四旋钮、灯圈组和整体亮度；
    ``pets_panel_settings`` 可选是兼容旧客户端的完整 PETS 设置。
    返回：frozen Pydantic model，确保任一子布局非法时在写入前返回 422。
    错误处理：子模型字段非法由 FastAPI/Pydantic 报告。
    副作用：模型本身不写文件、不访问硬件。
    """

    model_config = ConfigDict(frozen=True)

    key_layout: N4ProKeyLayout
    rotary_layout: N4ProRotaryLayout
    pets_panel_settings: N4ProPetsPanelSettings | None = None


class PetsPanelSettingsResponse(BaseModel):
    """向 GUI 返回当前 PETS 面板设置及其持久化来源。

    入参：``source`` 是 default/config/runtime/persisted；``path`` 只用于本机诊断；
    ``settings`` 是完整设置；``last_error`` 是启动读取失败的可选短文本。
    返回：冻结响应模型。
    错误处理：字段非法由 Pydantic 拒绝。
    副作用：模型本身不读写文件或远端。
    """

    model_config = ConfigDict(frozen=True)

    device_profile: str = "mirabox.n4pro"
    source: str
    path: str | None = None
    settings: N4ProPetsPanelSettings
    last_error: str | None = None


class StatusKeyImageCache:
    """缓存状态型 N4 Pro 主按键图片，避免交互时重复执行渲染。

    入参：无；缓存由 quota/token 快照内容和当前展示窗口/周期组成。
    返回：普通 Python 对象，通过 `quota_image()` / `usage_image()` 返回 Pillow image。
    错误处理：渲染器异常按原样传播；未知窗口或周期会先归一到安全默认值。
    副作用：仅保存内存 image 引用，不读取 ccusage、不访问网络或硬件。
    """

    def __init__(self) -> None:
        """初始化空图片缓存。

        入参：无。
        返回：无。
        错误处理：无。
        副作用：分配进程内 dict；不会提前渲染图片。
        """

        self._images: dict[tuple[object, ...], Any] = {}
        self._hits = 0
        self._misses = 0

    def clear(self) -> None:
        """清空所有已渲染状态按键图片。

        入参：无。
        返回：无。
        错误处理：无。
        副作用：释放本对象持有的 Pillow image 引用。
        """

        self._images.clear()

    def _store(self, key: tuple[object, ...], image: Any) -> None:
        """保存一张图片并按插入顺序裁剪缓存。

        入参：`key` 是图片内容指纹；`image` 是 Pillow image。
        返回：无。
        错误处理：无。
        副作用：修改内存缓存；超过容量时移除最早条目。
        """

        self._images[key] = image
        while len(self._images) > _MAX_STATUS_KEY_IMAGE_CACHE_ENTRIES:
            self._images.pop(next(iter(self._images)))

    def quota_image(
        self,
        snapshot: CodexQuotaSnapshot,
        *,
        window: str | None,
    ) -> Any:
        """返回 quota status 按键图片，优先命中缓存。

        入参：`snapshot` 是 daemon 当前共享 quota 快照；`window` 是 key 配置中的窗口。
        返回：112x112 Pillow image。
        错误处理：渲染失败按原异常传播。
        副作用：缓存 miss 时创建一张新图片并保存到内存。
        """

        normalized_window = _normalize_quota_status_window(window)
        key = (
            "quota_status",
            normalized_window,
            snapshot.plan_type,
            snapshot.plan_short_label,
            snapshot.plan_display_name,
            _quota_windows_fingerprint(snapshot),
            snapshot.credits_balance,
            snapshot.reset_credits_available,
        )
        image = self._images.get(key)
        if image is None:
            self._misses += 1
            image = render_quota_status_key_image(
                snapshot,
                window=normalized_window,
            )
            self._store(key, image)
        else:
            self._hits += 1
        return image

    def usage_image(
        self,
        snapshot: CodexTokenUsageSnapshot,
        *,
        period: str | None,
    ) -> Any:
        """返回 token/cost usage 按键图片，优先命中缓存。

        入参：`snapshot` 是 daemon 当前共享 token usage 快照；`period` 是 key 配置周期。
        返回：112x112 Pillow image。
        错误处理：渲染失败按原异常传播。
        副作用：缓存 miss 时创建一张新图片并保存到内存。
        """

        normalized_period = _normalize_token_usage_period(period)
        key = (
            "usage_summary",
            normalized_period.value,
            snapshot.updated_at.isoformat(),
            _token_usage_period_fingerprint(snapshot),
            _token_usage_daily_fingerprint(snapshot),
        )
        image = self._images.get(key)
        if image is None:
            self._misses += 1
            image = render_usage_summary_key_image(
                snapshot,
                period=normalized_period,
            )
            self._store(key, image)
        else:
            self._hits += 1
        return image

    def diagnostics(self) -> dict[str, int]:
        """返回状态型主按键图片缓存的最小诊断快照。

        入参：无。
        返回：缓存条目总数、quota/usage 分类条目数、命中与未命中次数。
        错误处理：无。
        副作用：无；不会触发图片渲染或访问硬件。
        """

        keys = tuple(self._images)
        return {
            "entries": len(keys),
            "quota_entries": sum(key[0] == "quota_status" for key in keys),
            "usage_entries": sum(key[0] == "usage_summary" for key in keys),
            "hits": self._hits,
            "misses": self._misses,
        }


class LogicalPanelImageCache:
    """缓存 logical panel 基础背景图，避免输入路径重复聚合和绘制。

    入参：无；缓存 key 由 quota/token 的展示数据指纹和当前周期组成。
    返回：普通 Python 对象，通过品牌、quota 与 usage 方法返回已完成的 800x480 Pillow 图像。
    错误处理：渲染异常按原样传播；缓存未命中会在当前线程生成一张图。
    副作用：仅保留进程内图片引用，不访问 ccusage、网络、文件或真实硬件。
    """

    def __init__(self) -> None:
        """初始化空的基础面板图片缓存。

        入参：无。
        返回：无。
        错误处理：无。
        副作用：分配进程内字典和诊断计数器，不提前生成图像。
        """

        self._images: dict[tuple[object, ...], Any] = {}
        self._hits = 0
        self._misses = 0
        self._prewarm_count = 0

    def brand_image(self) -> Any:
        """返回 Agent Deck 品牌基础面板，优先命中缓存。

        入参：无。
        返回：800x480 品牌背景图。
        错误处理：品牌图生成异常按原样传播。
        副作用：首次调用会创建并缓存品牌图。
        """

        return self._get_or_render(
            ("brand",),
            render_agent_deck_splash_touchscreen,
        )

    def quota_image(self, snapshot: CodexQuotaSnapshot, *, window: str) -> Any:
        """返回指定 quota 窗口的基础面板，优先命中内容指纹缓存。

        入参：`snapshot` 是 daemon 已确认的 quota 快照；`window` 是 `auto` 或稳定 window_id。
        返回：800x480 quota 背景图。
        错误处理：未知窗口或渲染异常按原语义传播。
        副作用：缓存未命中时生成并保存一张新图。
        """

        key = ("quota", window, *_quota_panel_fingerprint(snapshot))
        return self._get_or_render(
            key,
            lambda: render_quota_touchscreen(snapshot, window=window),
        )

    def token_image(
        self,
        snapshot: CodexTokenUsageSnapshot,
        *,
        period: CodexTokenPeriod,
    ) -> Any:
        """返回指定 Token 周期的趋势基础面板，优先命中内容指纹缓存。

        入参：`snapshot` 是 daemon 已确认的 ccusage 快照；`period` 是展示周期。
        返回：带主指标、趋势和四项细则的 800x480 背景图。
        错误处理：缺少周期或渲染异常按原语义传播。
        副作用：缓存未命中时聚合现有 raw daily 并保存一张新图，不执行 ccusage。
        """

        key = (
            "tokens",
            period.value,
            _token_usage_period_fingerprint(snapshot),
            _token_usage_daily_fingerprint(snapshot),
        )
        return self._get_or_render(
            key,
            lambda: render_token_usage_touchscreen(snapshot, period=period),
        )

    def prewarm_quota(self, snapshot: CodexQuotaSnapshot) -> None:
        """预渲染当前实际可用 quota 窗口，消除后续内容切换的首次绘制。

        入参：`snapshot` 是刚刷新成功的 quota 快照。
        返回：无。
        错误处理：任一实际窗口渲染失败时按原样传播给 poller。
        副作用：可能为任意数量的实际窗口新增进程内背景图并递增预热计数。
        """

        for quota_window in snapshot.available_windows():
            self.quota_image(snapshot, window=quota_window.window_id)
            self._prewarm_count += 1

    def prewarm_tokens(self, snapshot: CodexTokenUsageSnapshot) -> None:
        """预渲染四个 Token 周期，令旋钮切换只做缓存图选择。

        入参：`snapshot` 是刚刷新成功的 ccusage 快照。
        返回：无。
        错误处理：任一周期缺失或渲染失败时按原样传播给 poller。
        副作用：可能新增四张进程内背景图并递增预热计数。
        """

        for period in CodexTokenPeriod:
            self.token_image(snapshot, period=period)
            self._prewarm_count += 1

    def diagnostics(self) -> dict[str, int]:
        """返回基础面板缓存的最小诊断快照。

        入参：无。
        返回：总条目、三类条目数量、命中、未命中与预热次数。
        错误处理：无。
        副作用：无；不触发渲染。
        """

        keys = tuple(self._images)
        return {
            "entries": len(keys),
            "brand_entries": sum(key[0] == "brand" for key in keys),
            "quota_entries": sum(key[0] == "quota" for key in keys),
            "token_entries": sum(key[0] == "tokens" for key in keys),
            "hits": self._hits,
            "misses": self._misses,
            "prewarm_count": self._prewarm_count,
        }

    def _get_or_render(self, key: tuple[object, ...], renderer: Callable[[], Any]) -> Any:
        """按内容 key 读取或创建基础面板图像。

        入参：`key` 是内容指纹；`renderer` 只应创建内存图片。
        返回：缓存命中或刚创建的 Pillow 图像。
        错误处理：renderer 异常按原样传播，不保存失败值。
        副作用：更新命中/未命中计数，未命中时修改缓存并按容量裁剪。
        """

        image = self._images.get(key)
        if image is not None:
            self._hits += 1
            return image
        self._misses += 1
        image = renderer()
        self._images[key] = image
        while len(self._images) > _MAX_LOGICAL_PANEL_IMAGE_CACHE_ENTRIES:
            self._images.pop(next(iter(self._images)))
        return image

@dataclass
class _DaemonRuntime:
    """Hold all process-local daemon state used by the HTTP handlers.

    入参：`store` 是 normalized event reducer；`broker` 管理 pending approval；
    `surface` 记录 fake render 帧；`selection` 保存当前 deck 选择；两个 id 集合分别
    记录已反映到 store 的 pending decision 和已同步终态的 decision；poller 字段保存
    Codex App state、quota 与真实 N4 Pro renderer 的最近一次同步状态；
    `brand_feedback_until_monotonic` 保存未配置键触发的短暂 touch bar 品牌反馈截止时间。
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
    codex_observed_agent_keys_by_scope: dict[str, set[str]]
    codex_remote_ssh_discovery_diagnostic: dict[str, Any]
    codex_remote_ssh_diagnostics: dict[str, dict[str, Any]]
    codex_quota_snapshot: CodexQuotaSnapshot | None
    codex_quota_updated_at: datetime | None
    codex_quota_last_error: str | None
    quota_presentation: QuotaPresentation
    quota_presentation_source: str
    quota_presentation_path: Path | None
    quota_presentation_last_error: str | None
    codex_token_usage_snapshot: CodexTokenUsageSnapshot | None
    codex_token_usage_updated_at: datetime | None
    codex_token_usage_last_error: str | None
    codex_pet: CodexPetRuntime
    codex_pet_panel_revision_seen: int
    codex_pet_key_indexes: set[int]
    codex_pet_key_visual_key: tuple[object, ...] | None
    logical_panel_selection: PanelSelection
    logical_panel_render_lock: RLock
    logical_panel_background_revision: int
    logical_panel_last_render_duration_ms: float | None
    hardware_background_notifier: Callable[[], None] | None
    hardware_key_surface_revision: int
    hardware_key_surface_base_images: dict[int, Any]
    hardware_key_surface_images: dict[int, Any]
    hardware_key_surface_pending_images: dict[int, Any]
    streamdock_touch_tap_last_handled_monotonic: float | None
    streamdock_knob4_rotate_accumulator: int
    streamdock_input_event_count: int
    streamdock_last_input_event: dict[str, Any] | None
    streamdock_recent_input_events: list[dict[str, Any]]
    last_interaction_intent: InteractionIntent | None
    last_interaction_action: dict[str, Any] | None
    recent_interactions: list[dict[str, Any]]
    brand_feedback_until_monotonic: float | None
    control_feedback: ControlFeedback | None
    last_rotary_input: RotaryInputIntent | None
    last_rotary_action: dict[str, Any] | None
    focus_actions_enabled: bool
    local_actions_enabled: bool
    focus_action_executor: FocusActionExecutor
    local_app_catalog_reader: LocalAppCatalogReader
    local_app_action_executor: LocalAppActionExecutor
    local_url_action_executor: LocalUrlActionExecutor
    system_control_executor: SystemControlExecutor
    keyboard_shortcut_scheduler: KeyboardShortcutScheduler
    keyboard_accessibility_settings_opener: KeyboardAccessibilitySettingsOpener
    app_icon_cache: AppIconCache
    url_icon_cache: UrlIconCache
    shortcut_icon_store: ShortcutIconStore
    shortcut_key_image_cache: ShortcutKeyImageCache
    status_key_image_cache: StatusKeyImageCache
    logical_panel_image_cache: LogicalPanelImageCache
    streamdock_quota_touchscreen_result: StreamDockTouchscreenRenderResult | None
    streamdock_n4pro_renderer_result: StreamDockN4ProAnimationResult | None
    streamdock_n4pro_renderer_updated_at: datetime | None
    streamdock_n4pro_renderer_last_error: str | None
    key_layout: N4ProKeyLayout | None
    key_layout_source: str | None
    key_layout_path: Path | None
    key_layout_last_error: str | None
    rotary_layout: N4ProRotaryLayout | None
    rotary_layout_source: str | None
    rotary_layout_path: Path | None
    rotary_layout_last_error: str | None
    pets_panel_settings: N4ProPetsPanelSettings
    pets_panel_settings_source: str
    pets_panel_settings_path: Path | None
    pets_panel_settings_last_error: str | None
    n4pro_last_applied_brightness_percent: int | None
    n4pro_last_applied_lighting: tuple[str, str | None, bool] | None
    n4pro_last_applied_led_brightness_percent: int | None
    n4pro_session_output_last_error: str | None

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
        self.prewarm_status_key_images(rendered_layout)
        self.publish_hardware_key_surface_images(rendered_layout)
        return {
            "key_layout": _dump_model(self.current_key_layout_response()),
            "layout": _dump_model(rendered_layout),
            "render_count": self.surface.render_count,
        }

    def current_rotary_layout_response(self) -> RotaryLayoutResponse:
        """返回 GUI 当前应编辑的 N4 Pro 旋钮、灯光与亮度配置。

        入参：无。
        返回：已有 runtime/persisted layout 时返回它；否则返回可改写的内置默认布局。
        错误处理：内置 layout 构造失败会按 Pydantic 语义传播。
        副作用：只读取 runtime 内存，不写文件或硬件。
        """

        if self.rotary_layout is None:
            return RotaryLayoutResponse(
                device_profile="mirabox.n4pro",
                source="default",
                path=str(self.rotary_layout_path) if self.rotary_layout_path else None,
                layout=default_n4pro_rotary_layout(),
            )
        return RotaryLayoutResponse(
            device_profile="mirabox.n4pro",
            source=self.rotary_layout_source or "runtime",
            path=str(self.rotary_layout_path) if self.rotary_layout_path else None,
            layout=self.rotary_layout,
        )

    def update_rotary_layout(self, layout: N4ProRotaryLayout) -> dict[str, Any]:
        """保存并应用一份 N4 Pro 旋钮、灯光和控制台亮度配置。

        入参：`layout` 是 FastAPI/Pydantic 已校验的完整 rotary layout。
        返回：JSON-safe rotary layout、能力快照、当前 logical panel 与预览刷新信息。
        错误处理：路径写入失败按 `RotaryLayoutStoreError` 向 API handler 传播。
        副作用：更新 runtime applied layout，必要时原子写 JSON，并重新渲染 fake panel；真实 N4
        Pro 会在下一个同会话 renderer tick 读取该 applied state。
        """

        self._store_rotary_layout(layout)
        self.render_current_logical_panel_image()
        return {
            "rotary_layout": _dump_model(self.current_rotary_layout_response()),
            "capabilities": _dump_model(get_device_profile("mirabox.n4pro")),
            "logical_panel": _dump_model(self.logical_panel_selection),
            "render_count": self.surface.render_count,
        }

    def current_pets_panel_settings_response(self) -> PetsPanelSettingsResponse:
        """返回当前已应用的 N4 Pro PETS 面板设置。

        入参：无。
        返回：包含来源、可选路径、设置和启动读取错误的冻结响应。
        错误处理：无。
        副作用：无；不重新读取磁盘或远端。
        """

        return PetsPanelSettingsResponse(
            source=self.pets_panel_settings_source,
            path=(
                str(self.pets_panel_settings_path)
                if self.pets_panel_settings_path is not None
                else None
            ),
            settings=self.pets_panel_settings,
            last_error=self.pets_panel_settings_last_error,
        )

    def update_pets_panel_settings(
        self,
        settings: N4ProPetsPanelSettings,
    ) -> dict[str, Any]:
        """保存并立即应用一份 PETS 面板设置。

        入参：``settings`` 已由 API/Pydantic 完整校验。
        返回：当前设置响应和 PETS 诊断。
        错误处理：持久化失败抛 ``PetsPanelSettingsStoreError`` 并保留旧 applied 设置。
        副作用：可选写用户级 JSON，更新宠物 runtime 并唤醒当前逻辑面板渲染。
        """

        if self.pets_panel_settings_path is not None:
            save_n4pro_pets_panel_settings(
                settings,
                self.pets_panel_settings_path,
            )
        self.pets_panel_settings = settings
        self.pets_panel_settings_source = (
            "persisted" if self.pets_panel_settings_path is not None else "runtime"
        )
        self.pets_panel_settings_last_error = None
        self.codex_pet.update_panel_settings(settings)
        self.render_current_logical_panel_image()
        return {
            "pets_panel_settings": _dump_model(
                self.current_pets_panel_settings_response()
            ),
            "codex_pet": self.codex_pet.diagnostics(),
        }

    def set_hardware_background_notifier(
        self,
        notifier: Callable[[], None] | None,
    ) -> None:
        """设置由 persistent animator 提供的同会话 surface 更新唤醒器。

        入参：`notifier` 只能通知已有 animator 读取最新 revision，不能直接访问 SDK；None 表示
        当前 fake 或外部 renderer 不支持主动唤醒。
        返回：无。
        错误处理：设置阶段不调用 notifier，不传播硬件异常。
        副作用：覆盖 runtime 内存回调；下一次背景或静态键 revision 变化时可能唤醒硬件帧循环。
        """

        self.hardware_background_notifier = notifier

    def _store_rotary_layout(self, layout: N4ProRotaryLayout) -> None:
        """把已验证 rotary layout 更新到 runtime 并按需持久化。

        入参：`layout` 是完整 N4 Pro rotary 配置。
        返回：无显式返回值。
        错误处理：配置路径写入失败时抛 `RotaryLayoutStoreError`，调用方状态保持原布局。
        副作用：成功时写用户级 JSON（若启用）并更新 runtime source/layout。
        """

        if self.rotary_layout_path is not None:
            save_n4pro_rotary_layout(layout, self.rotary_layout_path)
            source = "persisted"
        else:
            source = "runtime"
        self.rotary_layout = layout
        self.rotary_layout_source = source
        self.rotary_layout_last_error = None

    def update_console_configuration(
        self,
        request: ConsoleConfigurationApplyRequest,
    ) -> dict[str, Any]:
        """以一次 GUI 保存请求更新主按键、旋钮和可选 PETS 配置域。

        入参：`request` 的 layout 与可选 PETS 设置已在进入前完成 Pydantic 校验。
        返回：三个配置 response、当前 renderer-neutral layout 和 render 计数。
        错误处理：任一持久化写入失败时向 handler 抛出，runtime applied state 保留旧值。
        副作用：可选地写三个用户级 JSON 文件；成功后统一刷新 key/panel 预览，真机在下个
        persistent renderer tick 的同一设备会话中接收新状态。
        """

        if self.key_layout_path is not None:
            save_n4pro_key_layout(request.key_layout, self.key_layout_path)
        if self.rotary_layout_path is not None:
            save_n4pro_rotary_layout(request.rotary_layout, self.rotary_layout_path)
        if (
            request.pets_panel_settings is not None
            and self.pets_panel_settings_path is not None
        ):
            save_n4pro_pets_panel_settings(
                request.pets_panel_settings,
                self.pets_panel_settings_path,
            )
        self.key_layout = request.key_layout
        self.key_layout_source = "persisted" if self.key_layout_path is not None else "runtime"
        self.key_layout_last_error = None
        self.rotary_layout = request.rotary_layout
        self.rotary_layout_source = (
            "persisted" if self.rotary_layout_path is not None else "runtime"
        )
        self.rotary_layout_last_error = None
        if request.pets_panel_settings is not None:
            self.pets_panel_settings = request.pets_panel_settings
            self.pets_panel_settings_source = (
                "persisted"
                if self.pets_panel_settings_path is not None
                else "runtime"
            )
            self.pets_panel_settings_last_error = None
            self.codex_pet.update_panel_settings(request.pets_panel_settings)
        rendered_layout = self.render_current()
        self.prewarm_status_key_images(rendered_layout)
        self.publish_hardware_key_surface_images(rendered_layout)
        self.render_current_logical_panel_image()
        return {
            "key_layout": _dump_model(self.current_key_layout_response()),
            "rotary_layout": _dump_model(self.current_rotary_layout_response()),
            "pets_panel_settings": _dump_model(
                self.current_pets_panel_settings_response()
            ),
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
        observation_scope: str = "local",
    ) -> None:
        """应用某个本地或远端观察域的 ChatGPT/Codex App 会话。

        入参：`sessions` 是 adapter 筛选出的顶层 App 会话；`observed_at` 是本轮扫描时间；
        ``observation_scope`` 标识 local 或一个 SSH host；只有独占 namespace 的远端 scope
        会清理上轮写入但本轮消失的状态，本地不会误删 hook 写入的同 key 状态。
        返回：无显式返回值。
        错误处理：state model 校验失败会向调用方传播，由 poller 捕获记录。
        副作用：幂等更新 store；远端 scope 会移除同 scope 上轮存在但本轮消失的
        observer-owned agent；状态有变化时 render fake surface 一帧。
        """

        current_agent_keys: set[str] = set()
        changed = False
        for session in sessions:
            session_id = _qualified_codex_app_session_id(session)
            parent_session_id = (
                _qualified_codex_app_parent_session_id(session)
                if session.parent_thread_id is not None
                else None
            )
            agent_key = f"{AgentSource.CODEX.value}:{session_id}"
            current_agent_keys.add(agent_key)
            focus_target = _codex_app_focus_target(session)
            title = _codex_app_session_display_name(session)
            self.store.upsert_observed_state(
                source=AgentSource.CODEX,
                session_id=session_id,
                observed_at=observed_at,
                status=session.status,
                title=title,
                cwd=session.cwd,
                summary=session.reason,
                active_tool=_active_tool_from_reason(session.reason),
                focus_target=focus_target,
                parent_session_id=parent_session_id,
                is_child_agent=session.is_child_thread,
            )
            changed = True
        if observation_scope != "local":
            previous_agent_keys = self.codex_observed_agent_keys_by_scope.get(
                observation_scope,
                set(),
            )
            for stale_agent_key in previous_agent_keys - current_agent_keys:
                changed = self.store.remove(stale_agent_key) is not None or changed
            self.codex_observed_agent_keys_by_scope[observation_scope] = (
                current_agent_keys
            )
        if changed:
            self.render_current()

    def clear_codex_observation_scope(self, observation_scope: str) -> None:
        """清理一个持续失联观察域先前写入的所有 Agent 状态。

        入参：``observation_scope`` 必须与 ``apply_codex_active_sessions`` 使用的 scope 一致。
        返回：无。
        错误处理：未知 scope 是幂等 no-op。
        副作用：只删除该 scope 明确追踪的 observer-owned agent，并在有删除时重渲染。
        """

        agent_keys = self.codex_observed_agent_keys_by_scope.pop(observation_scope, set())
        changed = False
        for agent_key in agent_keys:
            changed = self.store.remove(agent_key) is not None or changed
        if changed:
            self.render_current()

    def mark_codex_remote_ssh_poll_success(
        self,
        snapshot: CodexRemoteSshSnapshot,
    ) -> None:
        """记录某个 SSH host 的成功观察诊断。

        入参：``snapshot`` 已在 adapter 中脱敏，不包含 prompt、turn 或 item。
        返回：无。
        错误处理：本方法只复制 JSON-safe 字段，不主动抛业务异常。
        副作用：更新 runtime 内存诊断；保留首次成功时间供失联 stale 判定。
        """

        previous = self.codex_remote_ssh_diagnostics.get(snapshot.host_id, {})
        first_success_at = previous.get("first_success_at") or snapshot.observed_at
        self.codex_remote_ssh_diagnostics[snapshot.host_id] = {
            "host": snapshot.host,
            "host_id": snapshot.host_id,
            "last_polled_at": snapshot.observed_at,
            "first_success_at": first_success_at,
            "last_success_at": snapshot.observed_at,
            "last_error": None,
            "server_user_agent": snapshot.server_user_agent,
            "considered_thread_count": snapshot.considered_thread_count,
            "status_counts": dict(snapshot.status_counts),
            "active_session_count": len(snapshot.sessions),
            "pet_config_available": snapshot.pet_config_available,
        }
        self.codex_pet.update_remote_pet_selection(
            snapshot.host_id,
            selected_avatar_id=snapshot.selected_avatar_id,
            config_available=snapshot.pet_config_available,
        )

    def mark_codex_remote_ssh_discovery_success(
        self,
        snapshot: CodexRemoteSshDiscoverySnapshot,
    ) -> None:
        """记录一次 ChatGPT Settings connection 发现成功。

        入参：``snapshot`` 只含 managed/启用/忽略计数和已启用主机模型。
        返回：无。
        错误处理：不主动抛业务异常。
        副作用：覆盖 runtime 内存诊断；不保存 global-state 路径或原始设置内容。
        """

        self.codex_remote_ssh_discovery_diagnostic = {
            "source": "chatgpt_settings",
            "last_checked_at": snapshot.observed_at,
            "last_error": None,
            "managed_ssh_count": snapshot.managed_ssh_count,
            "enabled_host_count": len(snapshot.enabled_hosts),
            "auto_connect_disabled_count": snapshot.auto_connect_disabled_count,
            "ignored_non_ssh_count": snapshot.ignored_non_ssh_count,
        }

    def mark_codex_remote_ssh_discovery_error(
        self,
        error: Exception,
        *,
        checked_at: datetime,
    ) -> None:
        """记录设置发现失败，且只暴露异常类型。

        入参：``error`` 是读取或解析 ChatGPT global state 的异常；``checked_at`` 必须带时区。
        返回：无。
        错误处理：不保存异常消息，避免路径或内容进入状态 API。
        副作用：覆盖 discovery 诊断；observer 的关闭和状态清理由 poller 负责。
        """

        self.codex_remote_ssh_discovery_diagnostic = {
            "source": "chatgpt_settings",
            "last_checked_at": checked_at,
            "last_error": type(error).__name__,
            "managed_ssh_count": 0,
            "enabled_host_count": 0,
            "auto_connect_disabled_count": 0,
            "ignored_non_ssh_count": 0,
        }

    def remove_codex_remote_ssh_host(self, host_id: str) -> None:
        """删除一个不再启用的远端主机诊断与 observer-owned 状态。

        入参：``host_id`` 是由 SSH alias 派生的不可逆短摘要。
        返回：无。
        错误处理：未知 host id 是幂等 no-op。
        副作用：删除对应诊断并清理该观察域内的 Agent 状态，必要时触发重渲染。
        """

        self.codex_remote_ssh_diagnostics.pop(host_id, None)
        self.codex_pet.remove_remote_pet_selection(host_id)
        self.clear_codex_observation_scope(f"remote-ssh:{host_id}")

    def mark_codex_remote_ssh_poll_error(
        self,
        *,
        host: str,
        host_id: str,
        error: Exception,
        polled_at: datetime,
        stale_after_seconds: float,
    ) -> None:
        """记录 SSH host 读取失败，并在超过 stale 窗口后恢复原图标。

        入参：host/host_id 标识观察域；``error`` 只用于短类型诊断；``polled_at`` 必须带时区；
        ``stale_after_seconds`` 是距最后成功多久后清理旧状态。
        返回：无。
        错误处理：未知首次失败不会抛异常；naive 时间由调用方合同禁止。
        副作用：更新诊断；最后成功不存在或已过 stale 窗口时清理该 host 的 observer 状态。
        """

        previous = self.codex_remote_ssh_diagnostics.get(host_id, {})
        last_success_at = previous.get("last_success_at")
        diagnostic = dict(previous)
        diagnostic.update(
            {
                "host": host,
                "host_id": host_id,
                "last_polled_at": polled_at,
                "last_error": type(error).__name__,
            }
        )
        self.codex_remote_ssh_diagnostics[host_id] = diagnostic
        should_clear = not isinstance(last_success_at, datetime)
        if isinstance(last_success_at, datetime):
            should_clear = (
                polled_at - last_success_at
            ).total_seconds() >= stale_after_seconds
        if should_clear:
            self.clear_codex_observation_scope(f"remote-ssh:{host_id}")

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
        displayed_snapshot = self.displayed_codex_quota_snapshot()
        if displayed_snapshot is None:
            self.logical_panel_selection = self.logical_panel_selection.model_copy(
                update={"quota_window": "auto"}
            )
        else:
            self.logical_panel_selection = _normalize_logical_panel_quota_window(
                self.logical_panel_selection,
                displayed_snapshot,
            )
            self.logical_panel_image_cache.prewarm_quota(displayed_snapshot)
        image = self.render_current_logical_panel_image()
        self.prewarm_status_key_images()
        self.publish_hardware_key_surface_images()
        return image

    def displayed_codex_quota_snapshot(self) -> CodexQuotaSnapshot | None:
        """返回当前展示策略过滤、排序并标注后的 quota 快照。

        入参：无；读取 runtime 中最新原始 quota 快照和不可变展示策略。
        返回：无原始快照或全部被隐藏时返回 None；否则返回只含可见窗口的复制快照。
        错误处理：策略和原始快照均在写入 runtime 前完成校验，不会执行外部 I/O。
        副作用：无；不修改原始快照，也不更新缓存。
        """

        if self.codex_quota_snapshot is None:
            return None
        return self.quota_presentation.present(
            self.codex_quota_snapshot
        ).display_snapshot()

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
        self.logical_panel_image_cache.prewarm_tokens(snapshot)
        self.prewarm_status_key_images()
        self.publish_hardware_key_surface_images()
        return self.render_current_logical_panel_image()

    def _available_quota_windows(self) -> tuple[str, ...] | None:
        """返回当前 quota 快照中可供 logical panel 切换的稳定窗口标识。

        入参：无。
        返回：已有 quota 快照时返回任意数量的 `window_id`；尚未成功刷新时返回 None。
        错误处理：快照仅来自 adapter；空窗口由 adapter 模型拒绝，不会进入此方法。
        副作用：无；只读取 runtime 内存快照。
        """

        snapshot = self.displayed_codex_quota_snapshot()
        if snapshot is None:
            return None
        return tuple(window.window_id for window in snapshot.available_windows())

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
            available_quota_windows=self._available_quota_windows(),
            pets_enabled=self.codex_pet.enabled,
        )
        self.render_current_logical_panel_image()
        return {
            "selection": _dump_model(self.logical_panel_selection),
            "touchscreen_image_source": self.surface.last_touchscreen_image_source,
            "touchscreen_image_size": _image_size(self.surface.last_touchscreen_image),
        }

    def apply_rotary_input(self, intent: RotaryInputIntent) -> dict[str, Any]:
        """执行一条已按用户 binding 归一的旋钮 intent 并更新即时反馈。

        入参：`intent` 是配置驱动的 rotate 或 press 输入；系统动作只会经
        `system_control_executor` 受限边界执行。
        返回：JSON-safe intent、action、logical panel selection 和当前 feedback 诊断。
        错误处理：executor 失败转换为短暂错误反馈，不让硬件 callback 抛出系统异常。
        副作用：可能更新 panel selection、系统控制、控制台亮度持久化和 fake touchscreen 图像。
        """

        self.last_rotary_input = intent
        action: dict[str, Any]
        if intent.rotate_action is not None:
            action = self._apply_rotary_rotate_action(intent)
        else:
            action = self._apply_rotary_press_action(intent)
        self.last_rotary_action = action
        return {
            "rotary_intent": _dump_model(intent),
            "action": action,
            "selection": _dump_model(self.logical_panel_selection),
            "control_feedback": _dump_optional_model(self._active_control_feedback()),
        }

    def _apply_rotary_rotate_action(self, intent: RotaryInputIntent) -> dict[str, Any]:
        """执行一条旋转通道 action，并为连续值创建短暂 HUD。

        入参：`intent` 必须带 rotate action 和方向 -1/1。
        返回：动作诊断；面板轮换成功不创建 HUD，系统动作按真实结果创建 value/error HUD。
        错误处理：缺方向或未知 action 返回明确失败诊断。
        副作用：可能更新 panel selection、调用系统 executor、保存控制台亮度并重绘面板。
        """

        action = intent.rotate_action
        direction = intent.direction
        if action is None or direction not in {-1, 1}:
            return self._record_rotary_error("rotary input direction is invalid")
        if action == RotaryRotateAction.CYCLE_VIRTUAL_PANEL:
            self.logical_panel_selection = cycle_virtual_panel(
                self.logical_panel_selection,
                direction=(
                    PanelContentDirection.NEXT
                    if direction > 0
                    else PanelContentDirection.PREVIOUS
                ),
                pets_enabled=self.codex_pet.enabled,
            )
            self.render_current_logical_panel_image()
            return {
                "status": "cycled",
                "ok": True,
                "action": action.value,
                "active_kind": self.logical_panel_selection.active_kind.value,
            }
        if action == RotaryRotateAction.CYCLE_PANEL_CONTENT:
            before = self.logical_panel_selection
            self.logical_panel_selection = cycle_panel_content(
                before,
                direction=(
                    PanelContentDirection.NEXT
                    if direction > 0
                    else PanelContentDirection.PREVIOUS
                ),
                available_quota_windows=self._available_quota_windows(),
            )
            self.render_current_logical_panel_image()
            return {
                "status": "noop" if before == self.logical_panel_selection else "cycled",
                "ok": True,
                "action": action.value,
                "selection": _dump_model(self.logical_panel_selection),
            }
        if action == RotaryRotateAction.ADJUST_OUTPUT_VOLUME:
            result = self.system_control_executor.adjust_output_volume(2 * direction)
            return self._record_system_control_result(result, label="Output volume")
        if action == RotaryRotateAction.ADJUST_INPUT_VOLUME:
            result = self.system_control_executor.adjust_input_volume(2 * direction)
            return self._record_system_control_result(result, label="Input volume")
        if action == RotaryRotateAction.ADJUST_SYSTEM_DISPLAY_BRIGHTNESS:
            result = self.system_control_executor.adjust_system_display_brightness(
                self.current_rotary_layout_response().layout.system_display_id,
                2 * direction,
            )
            return self._record_system_control_result(result, label="Display brightness")
        if action == RotaryRotateAction.ADJUST_DECK_DISPLAY_BRIGHTNESS:
            layout = self.current_rotary_layout_response().layout
            updated_value = max(0, min(100, layout.console_brightness_percent + 2 * direction))
            self._store_rotary_layout(
                layout.model_copy(update={"console_brightness_percent": updated_value})
            )
            self._set_control_feedback(
                ControlFeedbackKind.VALUE,
                label="Console brightness",
                value=f"{updated_value}%",
            )
            self.render_current_logical_panel_image()
            return {
                "status": "succeeded",
                "ok": True,
                "action": action.value,
                "value_percent": updated_value,
                "message": "控制台亮度已更新",
            }
        return self._record_rotary_error(f"unsupported rotary action: {action.value}")

    def _apply_rotary_press_action(self, intent: RotaryInputIntent) -> dict[str, Any]:
        """执行一条按下通道 action，并按确认的静音状态生成短暂 HUD。

        入参：`intent` 必须带 press action。
        返回：系统 executor 的标准诊断加上 action 标识。
        错误处理：未知或缺少 press action 返回错误反馈。
        副作用：可能修改系统静音状态并更新 fake touchscreen。
        """

        action = intent.press_action
        if action == RotaryPressAction.TOGGLE_OUTPUT_MUTE:
            result = self.system_control_executor.toggle_output_mute()
            return self._record_system_control_result(result, label="Output")
        if action == RotaryPressAction.TOGGLE_INPUT_MUTE:
            result = self.system_control_executor.toggle_input_mute()
            return self._record_system_control_result(result, label="Microphone")
        return self._record_rotary_error("unsupported rotary press action")

    def _record_system_control_result(self, result: Any, *, label: str) -> dict[str, Any]:
        """把一个确认后的系统 executor 结果转换为 HUD 与 API 诊断。

        入参：`result` 是 `SystemControlResult`；`label` 是供 HUD 展示的动作名称。
        返回：JSON-safe action dict。
        错误处理：失败结果转换为短暂 error HUD；不抛出系统操作异常。
        副作用：更新 feedback 并重绘 fake logical panel。
        """

        result_data = _dump_model(result)
        if not result.ok:
            self._set_control_feedback(ControlFeedbackKind.ERROR, label=result.message)
        elif result.value_percent is not None:
            self._set_control_feedback(
                ControlFeedbackKind.VALUE,
                label=label,
                value=f"{result.value_percent}%",
            )
        elif result.muted is not None:
            self._set_control_feedback(
                ControlFeedbackKind.MUTE,
                label=f"{label} {'muted' if result.muted else 'unmuted'}",
            )
        self.render_current_logical_panel_image()
        return result_data

    def _record_rotary_error(self, message: str) -> dict[str, Any]:
        """记录一条不执行副作用的旋钮错误反馈。

        入参：`message` 是可展示的短错误说明。
        返回：标准 `ok=False` action dict。
        错误处理：无。
        副作用：设置短暂 error HUD 并重绘 fake logical panel。
        """

        self._set_control_feedback(ControlFeedbackKind.ERROR, label=message)
        self.render_current_logical_panel_image()
        return {"status": "unsupported", "ok": False, "message": message}

    def _set_control_feedback(
        self,
        kind: ControlFeedbackKind,
        *,
        label: str,
        value: str | None = None,
    ) -> None:
        """覆盖当前短暂 HUD，并从当前时刻重新计算 1.5 秒显示期限。

        入参：`kind`、`label` 和可选 `value` 是已经确认的动作反馈。
        返回：无显式返回值。
        错误处理：`ControlFeedback` 校验错误按原语义传播。
        副作用：更新 runtime 内存 feedback，不直接写真实硬件。
        """

        self.control_feedback = ControlFeedback(
            kind=kind,
            label=label,
            value=value,
            expires_at_monotonic=time.monotonic() + _CONTROL_FEEDBACK_DURATION_SECONDS,
        )

    def _active_control_feedback(self) -> ControlFeedback | None:
        """读取未过期的 HUD，并在过期后清理 runtime 引用。

        入参：无。
        返回：仍有效的 feedback，或 None。
        错误处理：无。
        副作用：过期时将 `control_feedback` 设为 None。
        """

        feedback = self.control_feedback
        if feedback is None:
            return None
        if feedback_is_active(feedback, now_monotonic=time.monotonic()):
            return feedback
        self.control_feedback = None
        return None

    def apply_n4pro_session_outputs(self, device: object, initialized: bool) -> str | None:
        """在 N4 Pro 已打开的统一 renderer 会话中应用整体亮度和唯一灯圈组输出。

        入参：`device` 是已成功 open/init 的官方 SDK device；`initialized` 表示本轮刚 init，
        因为 SDK init 会把亮度重置为 100，需要强制重新写入持久化值。
        返回：所有失败信息拼接后的错误字符串，成功或无变更时返回 None。
        错误处理：缺少 SDK 方法、返回非零 TransportResult 或调用异常只记录错误，不影响本轮图像渲染。
        副作用：仅在值变化或刚 init 时调用 `set_brightness`/`set_led_color`，不新建 HID 会话。
        """

        if initialized:
            self.n4pro_last_applied_brightness_percent = None
            self.n4pro_last_applied_lighting = None
            self.n4pro_last_applied_led_brightness_percent = None
        layout = self.current_rotary_layout_response().layout
        errors: list[str] = []
        desired_brightness = layout.console_brightness_percent
        if self.n4pro_last_applied_brightness_percent != desired_brightness:
            error = _set_n4pro_brightness(device, desired_brightness)
            if error is None:
                self.n4pro_last_applied_brightness_percent = desired_brightness
            else:
                errors.append(error)

        desired_lighting = (
            layout.lighting.mode.value,
            layout.lighting.color,
            layout.lighting.breathe,
        )
        if self.n4pro_last_applied_lighting != desired_lighting:
            error = _set_n4pro_group_lighting(device, *desired_lighting)
            if error is None:
                self.n4pro_last_applied_lighting = desired_lighting
            else:
                errors.append(error)

        desired_led_brightness = _n4pro_led_brightness_percent(
            mode=layout.lighting.mode.value,
            breathe=layout.lighting.breathe,
        )
        if self.n4pro_last_applied_led_brightness_percent != desired_led_brightness:
            error = _set_n4pro_group_led_brightness(device, desired_led_brightness)
            if error is None:
                self.n4pro_last_applied_led_brightness_percent = desired_led_brightness
            else:
                errors.append(error)

        error_text = "; ".join(errors) or None
        self.n4pro_session_output_last_error = error_text
        return error_text

    def apply_hardware_input(self, event: HardwareInput) -> dict[str, Any]:
        """Apply one low-level hardware input event through input routers.

        入参：`event` 是已归一化的低层硬件输入，可能来自 fake surface 或真实 SDK listener。
        返回：JSON-safe dict，说明是否被 logical panel 处理以及当前 selection。
        错误处理：panel 渲染异常按原语义传播；无法映射的输入返回 handled=false。
        副作用：当输入映射到 logical panel event 时更新 selection 并可能渲染 fake touchscreen。
        """

        if event.kind == "knob":
            rotary_intent = rotary_input_from_hardware_input(
                event,
                self.current_rotary_layout_response().layout,
            )
            if rotary_intent is not None:
                return {
                    "handled": True,
                    "panel_event": None,
                    **self.apply_rotary_input(rotary_intent),
                }
            return {
                "handled": False,
                "panel_event": None,
                "selection": _dump_model(self.logical_panel_selection),
                "rotary_intent": None,
            }

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

        rotary_intent = rotary_input_from_streamdock_input_event(
            event,
            self.current_rotary_layout_response().layout,
        )
        if rotary_intent is not None:
            result = self.apply_rotary_input(rotary_intent)
            self._record_streamdock_input_event(
                event,
                panel_event=None,
                handled=True,
                debounced=False,
                accumulated=False,
            )
            return {"handled": True, "panel_event": None, **result}

        if _dump_event_field(getattr(event, "event_type", None)) in {
            "knob_rotate",
            "knob_press",
        }:
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
                "rotary_intent": None,
            }

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
            binding = next(
                (
                    candidate
                    for candidate in self.current_key_layout_response().layout.keys
                    if candidate.index == intent.key_index
                ),
                None,
            )
            if binding is not None and binding.ambient_overlay is not None:
                self.codex_pet.record_app_overlay_key_press(intent.key_index + 1)
            if self.local_actions_enabled:
                action = _execute_local_app_action(
                    intent,
                    self.local_app_action_executor,
                )
            else:
                action = _dry_run_action(intent, state=None)
        elif intent.intent == "open_url":
            if self.local_actions_enabled:
                action = _execute_local_url_action(
                    intent,
                    self.local_url_action_executor,
                )
            else:
                action = _dry_run_action(intent, state=None)
        elif intent.intent == "send_keyboard_shortcut":
            action = self.submit_keyboard_shortcut(intent)
        elif intent.intent == "open_path":
            action = _unsupported_local_action(
                intent,
                message="open_path ignored; folder quick actions are disabled",
            )
        elif intent.intent == "cycle_quota_status_window":
            action = self.cycle_quota_status_window(intent)
        elif intent.intent == "cycle_usage_summary_period":
            action = self.cycle_usage_summary_period(intent)
        elif intent.intent == "show_brand_feedback":
            action = self.show_brand_feedback_panel(intent)
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

    def submit_keyboard_shortcut(self, intent: InteractionIntent) -> dict[str, Any]:
        """把硬件快捷键意图无阻塞提交到单 worker executor。

        入参：``intent`` 必须携带强类型 ``shortcut``；来源和 key index 用于任务诊断。
        返回：JSON-safe 即时提交结果；accepted 不代表目标应用已处理，busy 时不会排队。
        错误处理：缺 shortcut 返回 invalid_intent，不抛异常；线程池提交异常按原样传播。
        副作用：accepted 时启动后台物理键序列；调用线程不等待执行完成。
        """

        if intent.shortcut is None:
            return {
                "intent": intent.intent,
                "key_index": intent.key_index,
                "status": "invalid_intent",
                "ok": False,
                "job_id": None,
                "message": "keyboard shortcut intent is missing shortcut steps",
            }
        submission = self.keyboard_shortcut_scheduler.submit(
            intent.shortcut,
            source=intent.source,
            key_index=intent.key_index,
        )
        return {
            "intent": intent.intent,
            "key_index": intent.key_index,
            "status": submission.status.value,
            "ok": submission.accepted,
            "job_id": submission.job_id,
            "message": submission.message,
        }

    def cycle_quota_status_window(self, intent: InteractionIntent) -> dict[str, Any]:
        """切换一个 quota status 主按键展示的 quota 窗口。

        入参：`intent` 是按下状态型主键产生的 interaction intent。
        返回：JSON-safe action 诊断，包含切换后的窗口。
        错误处理：若 key layout 中找不到匹配 quota key，返回 missing_key，不抛异常。
        副作用：更新 runtime 内存 key layout 并 render 当前 layout；不写用户配置文件、不刷新 quota。
        """

        updated_window = _next_quota_status_window(
            intent.payload.get("quota_window"),
            snapshot=self.displayed_codex_quota_snapshot(),
        )
        updated = self._replace_key_binding(
            intent.key_index,
            kind="quota_status",
            update={"quota_window": updated_window},
        )
        if not updated:
            return {
                "intent": intent.intent,
                "key_index": intent.key_index,
                "status": "missing_key",
                "ok": False,
                "message": "quota_status key not found in current key layout",
            }
        layout = self.render_current()
        self.prewarm_status_key_images(layout)
        self.publish_hardware_key_surface_images(layout)
        return {
            "intent": intent.intent,
            "key_index": intent.key_index,
            "status": "cycled",
            "ok": True,
            "quota_window": updated_window,
            "message": f"quota status window cycled to {updated_window}",
        }

    def cycle_usage_summary_period(self, intent: InteractionIntent) -> dict[str, Any]:
        """切换一个 usage summary 主按键展示的 token/cost 周期。

        入参：`intent` 是按下状态型主键产生的 interaction intent。
        返回：JSON-safe action 诊断，包含切换后的周期。
        错误处理：若 key layout 中找不到匹配 usage key，返回 missing_key，不抛异常。
        副作用：更新 runtime 内存 key layout 并 render 当前 layout；不写用户配置文件、不执行 ccusage。
        """

        updated_period = _next_token_usage_period(intent.payload.get("usage_period"))
        updated = self._replace_key_binding(
            intent.key_index,
            kind="usage_summary",
            update={"usage_period": updated_period},
        )
        if not updated:
            return {
                "intent": intent.intent,
                "key_index": intent.key_index,
                "status": "missing_key",
                "ok": False,
                "message": "usage_summary key not found in current key layout",
            }
        layout = self.render_current()
        self.prewarm_status_key_images(layout)
        self.publish_hardware_key_surface_images(layout)
        return {
            "intent": intent.intent,
            "key_index": intent.key_index,
            "status": "cycled",
            "ok": True,
            "usage_period": updated_period,
            "message": f"usage summary period cycled to {updated_period}",
        }

    def _replace_key_binding(
        self,
        index: int,
        *,
        kind: str,
        update: dict[str, Any],
    ) -> bool:
        """替换当前 runtime key layout 中的一个主按键配置。

        入参：`index` 是 0-based 主键编号；`kind` 是期望用途；`update` 是 Pydantic
        `model_copy` 更新字段。
        返回：找到并替换时为 True，否则为 False。
        错误处理：更新后的 layout 若不合法会按 Pydantic 异常传播。
        副作用：只更新 daemon 内存 layout；不写 key layout path，避免硬件临时切换落盘。
        """

        layout = self.current_key_layout_response().layout
        bindings = []
        replaced = False
        for binding in layout.sorted_keys():
            if binding.index == index and binding.kind.value == kind:
                bindings.append(binding.model_copy(update=update))
                replaced = True
            else:
                bindings.append(binding)
        if not replaced:
            return False
        self.key_layout = N4ProKeyLayout(keys=tuple(bindings))
        if self.key_layout_source is None:
            self.key_layout_source = "runtime"
        return True

    def prewarm_status_key_images(self, layout: LayoutPlan | None = None) -> None:
        """按当前配置预渲染状态型主按键图片到 runtime 缓存。

        入参：`layout` 是可选的当前 layout；为空时会重建并记录一帧 fake layout。
        返回：无。
        错误处理：渲染异常按原样传播给调用方或 poller，由外层记录。
        副作用：可能创建状态键 Pillow image 并写入 `status_key_image_cache`；不访问硬件、
        不执行 ccusage、不读取 quota。
        """

        quota_snapshot = self.displayed_codex_quota_snapshot()
        if quota_snapshot is None and self.codex_token_usage_snapshot is None:
            return
        resolved_layout = layout or self.render_current()
        _key_images_from_layout(
            resolved_layout,
            quota_snapshot=quota_snapshot,
            token_usage_snapshot=self.codex_token_usage_snapshot,
            status_key_cache=self.status_key_image_cache,
        )

    def publish_hardware_key_surface_images(
        self,
        layout: LayoutPlan | None = None,
        *,
        notify: bool = True,
    ) -> dict[int, Any]:
        """生成静态主键差异，并发布给已打开的 persistent renderer。

        入参：`layout` 可复用本次业务路径已生成的 renderer-neutral layout；为空时重建当前
        layout。`notify` 仅在调用方即将同步执行 renderer 首帧时设为 False，避免无意义唤醒。
        返回：当前完整静态/热更新键图映射，先保存 App/URL/状态键基础图，再叠加独立 Codex
        宠物键与 App 任务态覆盖；不含由 agent 动画帧序列控制的键。
        错误处理：图标或状态图渲染失败按原语义传播；本方法不访问 SDK。
        副作用：内容引用发生变化时替换待下发差异、递增 revision，并可唤醒持久硬件帧循环；
        多次快速调用只保留最新差异图，保证硬件侧 latest-wins。
        """

        resolved_layout = layout or self.render_current()
        base_images = _key_images_from_layout(
            resolved_layout,
            app_icon_cache=self.app_icon_cache,
            url_icon_cache=self.url_icon_cache,
            shortcut_icon_store=self.shortcut_icon_store,
            shortcut_key_cache=self.shortcut_key_image_cache,
            quota_snapshot=self.displayed_codex_quota_snapshot(),
            token_usage_snapshot=self.codex_token_usage_snapshot,
            status_key_cache=self.status_key_image_cache,
        )
        key_images = dict(base_images)
        pet_key_indexes = {
            key.index + 1 for key in resolved_layout.keys[:10] if key.kind == "codex_pet"
        }
        pet_visual_key: tuple[object, ...] | None = None
        if pet_key_indexes:
            pet_source, pet_visual_key = self.codex_pet.key_image_source()
            if pet_source is not None:
                for key_index in pet_key_indexes:
                    key_images[key_index] = pet_source
        app_overlay_indexes = {
            key.index + 1
            for key in resolved_layout.keys[:10]
            if key.ambient_overlay is not None
        }
        key_images.update(
            self.codex_pet.app_overlay_key_sources(app_overlay_indexes)
        )
        changed_images = _changed_hardware_key_image_sources(
            self.hardware_key_surface_images,
            key_images,
        )
        self.hardware_key_surface_base_images = base_images
        self.hardware_key_surface_images = key_images
        self.codex_pet_key_indexes = pet_key_indexes
        self.codex_pet_key_visual_key = pet_visual_key
        if not changed_images:
            return key_images
        self.hardware_key_surface_pending_images = changed_images
        self.hardware_key_surface_revision += 1
        if notify:
            self._notify_hardware_background_update()
        return key_images

    def current_hardware_key_surface_images(self) -> tuple[int, dict[int, Any]]:
        """返回真实 renderer 可读取的当前完整静态键图片 revision 与快照。

        入参：无；只读取 daemon 已缓存的完整静态键图与待下发差异。
        返回：单调递增 revision 和当前完整静态键图副本；空映射代表尚无首次静态键图。renderer
        在它自己的单一硬件线程中以图片引用与上次已写快照比较，确保只下发发生变化的键。
        错误处理：无；只从已预渲染宠物缓存选择 Path，不访问 SDK。
        副作用：刷新动态宠物覆盖 revision，再复制映射，避免硬件线程遍历时看到后续替换。
        """

        self._refresh_dynamic_codex_pet_key_images()
        return self.hardware_key_surface_revision, dict(self.hardware_key_surface_images)

    def _refresh_dynamic_codex_pet_key_images(self) -> None:
        """在现有硬件 tick 中把配置的宠物 Key 切到当前预渲染 Path。

        入参：无；读取当前不可变 key layout 和线程安全宠物 runtime。
        返回：无。
        错误处理：无可用自定义图集时由宠物 runtime 返回静态 fallback 或 None；不编码图片。
        副作用：视觉 key 或宠物键位置变化时更新完整 key mapping、待下发差异和单调 revision；
        不主动唤醒，因为本方法已经由 persistent animator 的 provider tick 调用。
        """

        layout = self.current_key_layout_response().layout
        pet_indexes = {
            binding.index + 1
            for binding in layout.sorted_keys()
            if binding.kind.value == "codex_pet"
        }
        app_overlay_indexes = {
            binding.index + 1
            for binding in layout.sorted_keys()
            if binding.ambient_overlay is not None
        }
        images = dict(self.hardware_key_surface_base_images)
        visual_key: tuple[object, ...] | None = None
        if pet_indexes:
            source, visual_key = self.codex_pet.key_image_source()
            if source is not None:
                for key_index in pet_indexes:
                    images[key_index] = source
        images.update(
            self.codex_pet.app_overlay_key_sources(app_overlay_indexes)
        )
        changed = _changed_hardware_key_image_sources(
            self.hardware_key_surface_images,
            images,
        )
        self.hardware_key_surface_images = images
        self.codex_pet_key_indexes = pet_indexes
        self.codex_pet_key_visual_key = visual_key
        if not changed:
            return
        self.hardware_key_surface_pending_images = changed
        self.hardware_key_surface_revision += 1

    def show_brand_feedback_panel(self, intent: InteractionIntent) -> dict[str, Any]:
        """短暂显示 Agent Deck 默认品牌面板。

        入参：`intent` 是未配置主按键产生的 `show_brand_feedback` 意图。
        返回：JSON-safe action 诊断，包含触屏图来源、尺寸和持续时间。
        错误处理：Pillow 渲染异常按原异常传播，由 FastAPI 或 renderer loop 记录。
        副作用：设置 runtime transient override，并把品牌图记录到 fake touchscreen surface；
        真实硬件会在下一轮 N4 Pro renderer loop 读取该 override。
        """

        self.brand_feedback_until_monotonic = (
            time.monotonic() + _BRAND_FEEDBACK_DURATION_SECONDS
        )
        image = self.render_current_logical_panel_image()
        return {
            "intent": intent.intent,
            "agent_key": intent.agent_key,
            "decision_id": intent.decision_id,
            "status": "shown",
            "ok": True,
            "duration_seconds": _BRAND_FEEDBACK_DURATION_SECONDS,
            "touchscreen_image_source": self.surface.last_touchscreen_image_source,
            "touchscreen_image_size": _image_size(image),
            "message": "brand feedback panel shown",
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

        入参：无；读取人工 `logical_panel_selection`、pending MESSAGE override、宠物场景和
        已缓存的 quota/token snapshot。
        返回：`(image, source)`；当前 panel 缺少真实数据时返回占位面板，保证真实 renderer
        能启动并注册硬件输入回调。
        错误处理：Pillow 渲染异常按原语义传播。
        副作用：只创建内存图像，不修改 fake surface。
        """

        decision = self._current_pending_decision()
        effective_kind = self.effective_logical_panel_kind()
        if decision is not None:
            plan = _decision_message_panel_plan(decision)
            base_image, base_source = (
                render_logical_panel_touchscreen(plan),
                "decision_message",
            )
        elif self._is_brand_feedback_active():
            base_image, base_source = (
                self.logical_panel_image_cache.brand_image(),
                "agent_deck:brand_feedback",
            )
        else:
            quota_snapshot = self.displayed_codex_quota_snapshot()
            if effective_kind == PanelKind.QUOTA and quota_snapshot is not None:
                base_image, base_source = (
                    self.logical_panel_image_cache.quota_image(
                        quota_snapshot,
                        window=self.logical_panel_selection.quota_window,
                    ),
                    "codex_quota",
                )
            elif (
                effective_kind == PanelKind.TOKENS
                and self.codex_token_usage_snapshot is not None
            ):
                base_image, base_source = (
                    self.logical_panel_image_cache.token_image(
                        self.codex_token_usage_snapshot,
                        period=self.logical_panel_selection.token_period,
                    ),
                    "codex_tokens",
                )
            elif effective_kind == PanelKind.PETS:
                _pet_revision, pet_image = self.codex_pet.panel_background()
                if pet_image is not None:
                    base_image, base_source = pet_image, "codex_pet"
                else:
                    title, mood, lines = self.codex_pet.diagnostic_panel_content()
                    base_image, base_source = (
                        render_logical_panel_touchscreen(
                            pets_panel_plan(name=title, mood=mood, lines=lines)
                        ),
                        "codex_pet_diagnostic",
                    )
            else:
                base_image, base_source = (
                    self.logical_panel_image_cache.brand_image(),
                    "agent_deck:splash",
                )

        feedback = None if decision is not None else self._active_control_feedback()
        if feedback is not None:
            return (
                render_control_feedback_touchscreen(
                    feedback,
                    base_image=base_image,
                    viewport=N4PRO_LOGICAL_PANEL_VIEWPORT,
                ),
                f"control_feedback:{feedback.kind.value}",
            )
        return base_image, base_source

    def effective_logical_panel_kind(self) -> PanelKind:
        """返回当前真实显示的 panel kind，而不改写用户的人工选择。

        入参：无；读取 pending decisions、配置开关和 ``logical_panel_selection``。
        返回：有审批时始终 MESSAGE；Pets 关闭但旧选择仍为 Pets 时安全归位 Brand；否则返回
        人工选择。
        错误处理：无。
        副作用：无；不会覆盖 selection，因此 transient MESSAGE 结束后自然恢复原面板。
        """

        if self._current_pending_decision() is not None:
            return PanelKind.MESSAGE
        active_kind = self.logical_panel_selection.active_kind
        if active_kind == PanelKind.PETS and not self.codex_pet.enabled:
            return PanelKind.BRAND
        return active_kind

    def render_current_logical_panel_image(self) -> Any | None:
        """Render and record the current logical panel image when possible.

        入参：无。
        返回：刚记录到 fake surface 的 image；缺少可渲染数据时返回 None。
        错误处理：Pillow 渲染异常按原语义传播。
        副作用：可能更新 fake surface 的 touchscreen image 和计数。
        """

        with self.logical_panel_render_lock:
            started_at = time.perf_counter()
            built = self.build_current_logical_panel_background()
            if built is None:
                return None
            image, source = built
            if source == "codex_pet":
                pet_revision, _pet_image = self.codex_pet.panel_background()
                if (
                    pet_revision == self.codex_pet_panel_revision_seen
                    and self.surface.last_touchscreen_image_source == source
                ):
                    return self.surface.last_touchscreen_image
                self.codex_pet_panel_revision_seen = pet_revision
            self.surface.render_touchscreen_image(image, source=source)
            self.logical_panel_background_revision += 1
            self.logical_panel_last_render_duration_ms = (
                time.perf_counter() - started_at
            ) * 1000
            self._notify_hardware_background_update()
            return image

    def _notify_hardware_background_update(self) -> None:
        """通知已连接 persistent animator 读取最新 logical panel revision。

        入参：无。
        返回：无。
        错误处理：通知器异常会被吞掉，避免一次硬件 wake 失败影响输入或 fake surface 状态。
        副作用：真实 N4 Pro renderer 等待下一帧时可能被唤醒；不会在当前线程写 HID。
        """

        notifier = self.hardware_background_notifier
        if notifier is None:
            return
        with suppress(Exception):
            notifier()

    def current_hardware_background(self) -> tuple[int, Any | None]:
        """返回真实 renderer 可安全读取的最新 logical panel 背景 revision。

        入参：无；读取 daemon 已缓存的 fake touchscreen image。
        返回：单调递增 revision 和当前 800x480 Pillow 图像；当短暂 HUD 已过期但缓存仍是反馈图时，
        会先重绘基础 panel，再返回新的 revision。
        错误处理：基础 panel 不可构建时返回现有缓存或 None，不直接访问 SDK。
        副作用：HUD 过期时更新 fake surface 缓存和 revision；不创建硬件会话。
        """

        with self.logical_panel_render_lock:
            source = self.surface.last_touchscreen_image_source or ""
            pending_decision = self._current_pending_decision()
            if pending_decision is not None:
                if source != "decision_message":
                    self.render_current_logical_panel_image()
                return (
                    self.logical_panel_background_revision,
                    self.surface.last_touchscreen_image,
                )
            should_sample_pet = (
                self.effective_logical_panel_kind() == PanelKind.PETS
                and not self._is_brand_feedback_active()
                and self._active_control_feedback() is None
            )

        if should_sample_pet:
            started_at = time.perf_counter()
            pet_revision, pet_image = self.codex_pet.panel_background()
            with self.logical_panel_render_lock:
                if self._current_pending_decision() is not None:
                    self.render_current_logical_panel_image()
                elif (
                    self.effective_logical_panel_kind() == PanelKind.PETS
                    and not self._is_brand_feedback_active()
                    and self._active_control_feedback() is None
                ):
                    source = self.surface.last_touchscreen_image_source or ""
                    if (
                        pet_image is not None
                        and pet_revision != self.codex_pet_panel_revision_seen
                    ):
                        self.surface.render_touchscreen_image(
                            pet_image,
                            source="codex_pet",
                        )
                        self.codex_pet_panel_revision_seen = pet_revision
                        self.logical_panel_background_revision += 1
                        self.logical_panel_last_render_duration_ms = (
                            time.perf_counter() - started_at
                        ) * 1000
                    elif pet_image is None and source != "codex_pet_diagnostic":
                        self.render_current_logical_panel_image()

        with self.logical_panel_render_lock:
            source = self.surface.last_touchscreen_image_source or ""
            if self._current_pending_decision() is not None:
                if source != "decision_message":
                    self.render_current_logical_panel_image()
                return (
                    self.logical_panel_background_revision,
                    self.surface.last_touchscreen_image,
                )
            if source == "decision_message":
                self.render_current_logical_panel_image()
                source = self.surface.last_touchscreen_image_source or ""
            if (
                source.startswith("control_feedback:")
                and self._active_control_feedback() is None
            ):
                self.render_current_logical_panel_image()
            if self.surface.last_touchscreen_image is None:
                self.render_current_logical_panel_image()
            return (
                self.logical_panel_background_revision,
                self.surface.last_touchscreen_image,
            )

    def _is_brand_feedback_active(self) -> bool:
        """判断 transient 品牌反馈面板是否仍在有效时间内。

        入参：无；读取 runtime 中的 monotonic 截止时间。
        返回：有效期内返回 True；过期或未设置返回 False，并清理过期字段。
        错误处理：无。
        副作用：过期时清空 `brand_feedback_until_monotonic`。
        """

        until = self.brand_feedback_until_monotonic
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        self.brand_feedback_until_monotonic = None
        return False

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

        with self.logical_panel_render_lock:
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
            self.render_current_logical_panel_image()
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
            "rotary_layout": _dump_model(self.current_rotary_layout_response()),
            "rotary_layout_last_error": self.rotary_layout_last_error,
            "hardware_capabilities": _dump_model(get_device_profile("mirabox.n4pro")),
            "keyboard_shortcuts": self.keyboard_shortcut_diagnostics(),
            "render_count": self.surface.render_count,
            "pollers": {
                "codex_app_state": {
                    "last_polled_at": _dump_datetime(
                        self.codex_app_state_last_polled_at
                    ),
                    "last_error": self.codex_app_state_last_error,
                },
                "codex_remote_ssh": {
                    "discovery": _dump_codex_remote_ssh_diagnostic(
                        self.codex_remote_ssh_discovery_diagnostic
                    ),
                    "hosts": [
                        _dump_codex_remote_ssh_diagnostic(diagnostic)
                        for diagnostic in sorted(
                            self.codex_remote_ssh_diagnostics.values(),
                            key=lambda item: str(item.get("host", "")),
                        )
                    ],
                    "associated_agent_count": sum(
                        len(agent_keys)
                        for scope, agent_keys in (
                            self.codex_observed_agent_keys_by_scope.items()
                        )
                        if scope.startswith("remote-ssh:")
                    ),
                },
            },
            "codex_quota": {
                "snapshot": _dump_optional_model(self.codex_quota_snapshot),
                "display_snapshot": _dump_optional_model(
                    self.displayed_codex_quota_snapshot()
                ),
                "presentation": {
                    "source": self.quota_presentation_source,
                    "path": (
                        str(self.quota_presentation_path)
                        if self.quota_presentation_path
                        else None
                    ),
                    "last_error": self.quota_presentation_last_error,
                    "rules": _dump_model(self.quota_presentation)["rules"],
                    "unmatched_visible": self.quota_presentation.unmatched_visible,
                },
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
            "codex_pet": self.codex_pet.diagnostics(),
            "logical_panel": {
                "selection": _dump_model(self.logical_panel_selection),
                "effective_kind": self.effective_logical_panel_kind().value,
                "touchscreen_render_count": self.surface.touchscreen_render_count,
                "touchscreen_image_size": _image_size(
                    self.surface.last_touchscreen_image
                ),
                "touchscreen_image_source": self.surface.last_touchscreen_image_source,
                "control_feedback": _dump_optional_model(self._active_control_feedback()),
                "image_cache": self.logical_panel_image_cache.diagnostics(),
                "background_revision": self.logical_panel_background_revision,
                "last_render_duration_ms": self.logical_panel_last_render_duration_ms,
            },
            "streamdock_n4pro_renderer": {
                "last_result": _dump_optional_model(
                    self.streamdock_n4pro_renderer_result
                ),
                "updated_at": _dump_datetime(
                    self.streamdock_n4pro_renderer_updated_at
                ),
                "last_error": self.streamdock_n4pro_renderer_last_error,
                "session_output_last_error": self.n4pro_session_output_last_error,
                "applied_console_brightness_percent": self.n4pro_last_applied_brightness_percent,
                "applied_lighting": self.n4pro_last_applied_lighting,
                "applied_led_brightness_percent": self.n4pro_last_applied_led_brightness_percent,
                "static_key_surface": {
                    "revision": self.hardware_key_surface_revision,
                    "latest_change_key_count": len(
                        self.hardware_key_surface_pending_images
                    ),
                    "image_cache": self.status_key_image_cache.diagnostics(),
                },
            },
            "streamdock_input": {
                "event_count": self.streamdock_input_event_count,
                "last_event": self.streamdock_last_input_event,
                "recent_events": list(self.streamdock_recent_input_events),
            },
            "rotary": {
                "last_intent": _dump_optional_model(self.last_rotary_input),
                "last_action": self.last_rotary_action,
                "system_display_targets": [
                    _dump_model(target)
                    for target in self.system_control_executor.list_display_targets()
                ],
            },
            "deck_selection": _dump_model(self.selection),
            "interaction": {
                "last_intent": _dump_optional_model(self.last_interaction_intent),
                "last_action": self.last_interaction_action,
                "recent": list(self.recent_interactions),
            },
        }

    def keyboard_shortcut_diagnostics(self) -> dict[str, Any]:
        """返回键盘权限、active job 和近期终态任务的 JSON-safe 诊断。

        入参：无。
        返回：capability、active 和 recent 三部分。
        错误处理：平台 capability 检查异常按原样传播给 status/API。
        副作用：只读 scheduler 与系统授权状态，不显示授权提示。
        """

        diagnostics = self.keyboard_shortcut_scheduler.diagnostics()
        active = diagnostics["active"]
        recent = diagnostics["recent"]
        return {
            "capability": _dump_model(diagnostics["capability"]),  # type: ignore[arg-type]
            "active": _dump_optional_model(active),  # type: ignore[arg-type]
            "recent": [_dump_model(item) for item in recent],  # type: ignore[union-attr]
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
        self.codex_pet.update_activity(states)
        self._ensure_first_agent_selected(states)
        decisions = self.broker.pending()
        layout = build_layout_plan(
            states,
            decisions,
            self.selection,
            key_layout=self.current_key_layout_response().layout,
        )
        self.surface.render(layout)
        if self.effective_logical_panel_kind() == PanelKind.PETS:
            self.render_current_logical_panel_image()
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


def _build_default_n4pro_renderer_sink(
    runtime: _DaemonRuntime,
) -> StreamDockN4ProRendererSink:
    """创建默认 N4 Pro persistent renderer，并在同一会话挂接控制台输出回调。

    入参：`runtime` 提供输入 callback 与已应用的亮度/灯光配置。
    返回：可作为 renderer sink 调用的 persistent animator。
    错误处理：构造不访问设备；被 monkeypatch 的旧 fake animator 若没有 setter 仍能用于现有测试。
    副作用：仅创建 renderer 对象并保存回调引用，不 open/init 真实硬件。
    """

    animator = StreamDockN4ProPersistentAnimator(
        input_callback=lambda _device, event: runtime.apply_streamdock_input_event(event)
    )
    setter = getattr(animator, "set_session_output_callback", None)
    if callable(setter):
        setter(runtime.apply_n4pro_session_outputs)
    background_setter = getattr(animator, "set_background_update_provider", None)
    if callable(background_setter):
        background_setter(runtime.current_hardware_background)
    key_image_setter = getattr(animator, "set_key_image_update_provider", None)
    if callable(key_image_setter):
        key_image_setter(runtime.current_hardware_key_surface_images)
    notifier = getattr(animator, "notify_background_update", None)
    if callable(notifier):
        runtime.set_hardware_background_notifier(notifier)
    return animator


def create_app(
    *,
    poller_config: DaemonPollerConfig | None = None,
    codex_app_state_event_reader: CodexAppStateEventReader = build_codex_app_state_events,
    codex_app_active_sessions_reader: CodexAppActiveSessionsReader = read_codex_app_active_sessions,
    codex_remote_ssh_hosts_reader: CodexRemoteSshHostsReader = (
        discover_enabled_codex_remote_ssh_hosts
    ),
    codex_remote_ssh_observer_factory: CodexRemoteSshObserverFactory | None = None,
    codex_remote_pet_mirror: CodexRemotePetMirrorProtocol | None = None,
    codex_remote_pet_cache_path: Path | None = None,
    codex_quota_reader: CodexQuotaReader = read_codex_quota,
    codex_token_usage_reader: CodexTokenUsageReader = read_codex_token_usage,
    quota_touchscreen_sink: QuotaTouchscreenSink = render_touchscreen_image_to_n4pro,
    streamdock_n4pro_renderer_sink: StreamDockN4ProRendererSink | None = None,
    visible_splash_touchscreen_sink: VisibleSplashTouchscreenSink = render_dual_device_touchscreen_image_to_n4pro,
    focus_action_executor: FocusActionExecutor = focus_agent_target,
    local_app_catalog_reader: LocalAppCatalogReader = list_local_apps,
    local_app_action_executor: LocalAppActionExecutor = open_or_focus_local_app,
    local_url_action_executor: LocalUrlActionExecutor = open_local_url,
    system_control_executor: SystemControlExecutor | None = None,
    keyboard_shortcut_executor: KeyboardShortcutExecutor | None = None,
    keyboard_accessibility_settings_opener: KeyboardAccessibilitySettingsOpener = (
        open_macos_accessibility_settings
    ),
    app_icon_cache_path: Path | None = None,
    url_icon_cache_path: Path | None = None,
    shortcut_icon_store_path: Path | None = None,
    url_icon_fetcher: UrlIconFetcher | None = None,
    codex_pet_resolver: CodexPetResolver | None = None,
    codex_pet_reduced_motion_reader: ReducedMotionReader | None = None,
    codex_pet_cache_path: Path | None = None,
    key_layout_path: Path | None = None,
    rotary_layout_path: Path | None = None,
    pets_panel_settings_path: Path | None = None,
    quota_presentation_path: Path | None = None,
) -> FastAPI:
    """Create the local daemon FastAPI app without binding sockets.

    入参：`poller_config` 控制是否启动 Codex App state 和 quota 后台 pollers；为空时不启用
    任何 poller，保持测试和嵌入调用无外部 I/O；`codex_app_state_event_reader` 和
    `codex_app_active_sessions_reader` 读取最近有效 Codex App 会话，生产默认只读扫描本机状态；
    ``codex_remote_ssh_hosts_reader`` 只读取 ChatGPT Settings 已管理且明确启用自动连接的
    SSH connections；``codex_remote_ssh_observer_factory`` 可为测试注入 observer，生产按
    每轮发现结果动态创建或关闭独立只读 SSH app-server proxy；``codex_remote_pet_mirror``
    与 cache path 控制仅在 remote_config 策略下读取 custom manifest/图集的受限 SFTP 镜像；
    `codex_quota_reader` 是可注入 reader，生产默认读取真实本机 Codex quota；
    `codex_token_usage_reader` 是可注入 reader，生产默认通过 ccusage 读取 Codex token usage；
    `quota_touchscreen_sink` 是 quota-only 真实硬件触屏下发函数，仅在配置启用时调用；
    `streamdock_n4pro_renderer_sink` 是统一背景+按钮真实硬件下发函数，测试可替换；为空时
    使用 daemon 专用 persistent sink，避免每轮渲染都 close/open N4 Pro；
    `visible_splash_touchscreen_sink` 专门写 N4 Pro dual-device 可见触屏层，用于启动/退出时
    清掉旧 quota 残留；不参与常规按键动画渲染；
    `focus_action_executor` 是 `focus_agent` 的真实动作执行器，poller config 未禁用
    `focus_actions_enabled` 且目标 agent 有 focus target 时会被调用；`local_app_catalog_reader`
    支撑 GUI App 选择；`local_app_action_executor` 和 `local_url_action_executor`
    支撑本机快捷动作执行，测试可替换；
    `keyboard_shortcut_executor` 是可注入物理键执行器，默认按平台选择 macOS/CoreGraphics 或
    fail-closed 实现；`keyboard_accessibility_settings_opener` 只在用户显式点击后打开 macOS
    辅助功能设置，测试可注入无副作用 fake；`app_icon_cache_path` 是 App 图标缓存根目录；`url_icon_cache_path` 是 URL
    favicon 缓存根目录；`shortcut_icon_store_path` 是内容寻址自定义快捷键图标目录；这些路径
    默认使用用户级 Application Support；`url_icon_fetcher` 供测试替换网络请求；
    `codex_pet_resolver`、reduced-motion reader 与 cache path 供宠物解析/可访问性/临时缓存测试
    注入，生产默认只读跟随 Codex 并使用 daemon 生命周期临时目录；
    `key_layout_path`、`rotary_layout_path` 与 ``pets_panel_settings_path`` 为 None 时相应 GUI
    设置只保存在进程内，传入路径时启动会读各自 JSON，保存会写回；
    `quota_presentation_path` 可选加载独立 quota 展示策略，
    只影响硬件显示而不改原始采集数据；`system_control_executor` 可注入 fake，默认按平台构造
    保守 executor。
    返回：配置好路由且持有 in-memory runtime 的 `FastAPI` ASGI app。
    错误处理：对象构造失败会直接抛出；poller 单次失败会记录到 status，不让 app 启动失败。
    副作用：总是分配内存对象并注册路由；只有显式启用 poller 时，lifespan startup 才会只读访问
    Codex 本地状态或启动短生命周期 Codex app-server 子进程。
    """

    resolved_poller_config = poller_config or DaemonPollerConfig()
    resolved_codex_remote_ssh_observers: dict[
        str, CodexRemoteSshObserverProtocol
    ] = {}
    resolved_codex_remote_ssh_observer_factory = (
        codex_remote_ssh_observer_factory
        if codex_remote_ssh_observer_factory is not None
        else lambda enabled_host: CodexRemoteSshObserver(
            enabled_host.alias,
            timeout_seconds=resolved_poller_config.codex_remote_ssh_timeout_seconds,
            thread_limit=resolved_poller_config.codex_remote_ssh_thread_limit,
            completed_feedback_seconds=(
                resolved_poller_config.codex_remote_ssh_completed_feedback_seconds
            ),
        )
    )
    resolved_codex_remote_pet_mirror = (
        codex_remote_pet_mirror
        if codex_remote_pet_mirror is not None
        else CodexRemotePetMirror(
            cache_root=codex_remote_pet_cache_path,
            timeout_seconds=resolved_poller_config.codex_remote_ssh_timeout_seconds,
        )
    )
    codex_pet_temp_directory: TemporaryDirectory[str] | None = None
    if resolved_poller_config.codex_pet_enabled and codex_pet_cache_path is None:
        codex_pet_temp_directory = TemporaryDirectory(
            prefix="agent-deck-codex-pet-"
        )
    resolved_codex_pet_cache_path = (
        codex_pet_cache_path
        if codex_pet_cache_path is not None
        else (
            Path(codex_pet_temp_directory.name)
            if codex_pet_temp_directory is not None
            else Path(".")
        )
    )
    initial_pets_panel_settings = N4ProPetsPanelSettings(
        remote_pet_source=resolved_poller_config.codex_pet_remote_pet_source,
        patrol_speed=resolved_poller_config.codex_pet_patrol_speed,
    )
    initial_pets_panel_settings_source = "config"
    pets_panel_settings_last_error: str | None = None
    if pets_panel_settings_path is not None:
        try:
            persisted_pets_settings = load_n4pro_pets_panel_settings(
                pets_panel_settings_path
            )
            if persisted_pets_settings is not None:
                initial_pets_panel_settings = persisted_pets_settings
                initial_pets_panel_settings_source = "persisted"
        except PetsPanelSettingsStoreError as exc:
            pets_panel_settings_last_error = str(exc)
    codex_pet_runtime = CodexPetRuntime(
        enabled=resolved_poller_config.codex_pet_enabled,
        panel_fps=resolved_poller_config.codex_pet_panel_fps,
        motion=resolved_poller_config.codex_pet_motion,
        cache_root=resolved_codex_pet_cache_path,
        fallback_key_path=(
            resolved_poller_config.streamdock_n4pro_frame_root / "offline.png"
        ),
        panel_settings=initial_pets_panel_settings,
        resolver=codex_pet_resolver,
        reduced_motion_reader=codex_pet_reduced_motion_reader,
    )
    app_icon_cache = AppIconCache(resolve_app_icon_cache_root(app_icon_cache_path))
    url_icon_cache = UrlIconCache(
        resolve_url_icon_cache_root(url_icon_cache_path),
        fetcher=url_icon_fetcher,
    )
    shortcut_icon_store = ShortcutIconStore(
        resolve_shortcut_icon_store_root(shortcut_icon_store_path)
    )
    keyboard_shortcut_scheduler = KeyboardShortcutScheduler(
        keyboard_shortcut_executor or create_default_keyboard_shortcut_executor()
    )
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
    initial_rotary_layout: N4ProRotaryLayout | None = None
    initial_rotary_layout_source: str | None = None
    rotary_layout_last_error: str | None = None
    if rotary_layout_path is not None:
        try:
            initial_rotary_layout = load_n4pro_rotary_layout(rotary_layout_path)
            if initial_rotary_layout is not None:
                initial_rotary_layout_source = "persisted"
        except RotaryLayoutStoreError as exc:
            rotary_layout_last_error = str(exc)
    initial_quota_presentation = QuotaPresentation()
    initial_quota_presentation_source = "default"
    quota_presentation_last_error: str | None = None
    if quota_presentation_path is not None:
        try:
            persisted_presentation = load_quota_presentation(quota_presentation_path)
            if persisted_presentation is not None:
                initial_quota_presentation = persisted_presentation
                initial_quota_presentation_source = "persisted"
        except QuotaPresentationStoreError as exc:
            quota_presentation_last_error = str(exc)
    runtime = _DaemonRuntime(
        store=AgentStateStore(),
        broker=DecisionBroker(),
        surface=FakeHardwareSurface(),
        selection=DeckSelection(),
        reflected_pending_decision_ids=set(),
        terminal_synced_decision_ids=set(),
        codex_app_state_last_polled_at=None,
        codex_app_state_last_error=None,
        codex_observed_agent_keys_by_scope={},
        codex_remote_ssh_discovery_diagnostic={
            "source": "chatgpt_settings",
            "last_checked_at": None,
            "last_error": None,
            "managed_ssh_count": 0,
            "enabled_host_count": 0,
            "auto_connect_disabled_count": 0,
            "ignored_non_ssh_count": 0,
        },
        codex_remote_ssh_diagnostics={},
        codex_quota_snapshot=None,
        codex_quota_updated_at=None,
        codex_quota_last_error=None,
        quota_presentation=initial_quota_presentation,
        quota_presentation_source=initial_quota_presentation_source,
        quota_presentation_path=quota_presentation_path,
        quota_presentation_last_error=quota_presentation_last_error,
        codex_token_usage_snapshot=None,
        codex_token_usage_updated_at=None,
        codex_token_usage_last_error=None,
        codex_pet=codex_pet_runtime,
        codex_pet_panel_revision_seen=0,
        codex_pet_key_indexes=set(),
        codex_pet_key_visual_key=None,
        logical_panel_selection=PanelSelection(),
        logical_panel_render_lock=RLock(),
        logical_panel_background_revision=0,
        logical_panel_last_render_duration_ms=None,
        hardware_background_notifier=None,
        hardware_key_surface_revision=0,
        hardware_key_surface_base_images={},
        hardware_key_surface_images={},
        hardware_key_surface_pending_images={},
        streamdock_touch_tap_last_handled_monotonic=None,
        streamdock_knob4_rotate_accumulator=0,
        streamdock_input_event_count=0,
        streamdock_last_input_event=None,
        streamdock_recent_input_events=[],
        last_interaction_intent=None,
        last_interaction_action=None,
        recent_interactions=[],
        brand_feedback_until_monotonic=None,
        control_feedback=None,
        last_rotary_input=None,
        last_rotary_action=None,
        focus_actions_enabled=resolved_poller_config.focus_actions_enabled,
        local_actions_enabled=resolved_poller_config.local_actions_enabled,
        focus_action_executor=focus_action_executor,
        local_app_catalog_reader=local_app_catalog_reader,
        local_app_action_executor=local_app_action_executor,
        local_url_action_executor=local_url_action_executor,
        system_control_executor=(
            system_control_executor or create_default_system_control_executor()
        ),
        keyboard_shortcut_scheduler=keyboard_shortcut_scheduler,
        keyboard_accessibility_settings_opener=keyboard_accessibility_settings_opener,
        app_icon_cache=app_icon_cache,
        url_icon_cache=url_icon_cache,
        shortcut_icon_store=shortcut_icon_store,
        shortcut_key_image_cache=ShortcutKeyImageCache(),
        status_key_image_cache=StatusKeyImageCache(),
        logical_panel_image_cache=LogicalPanelImageCache(),
        streamdock_quota_touchscreen_result=None,
        streamdock_n4pro_renderer_result=None,
        streamdock_n4pro_renderer_updated_at=None,
        streamdock_n4pro_renderer_last_error=None,
        key_layout=initial_key_layout,
        key_layout_source=initial_key_layout_source,
        key_layout_path=key_layout_path,
        key_layout_last_error=key_layout_last_error,
        rotary_layout=initial_rotary_layout,
        rotary_layout_source=initial_rotary_layout_source,
        rotary_layout_path=rotary_layout_path,
        rotary_layout_last_error=rotary_layout_last_error,
        pets_panel_settings=initial_pets_panel_settings,
        pets_panel_settings_source=initial_pets_panel_settings_source,
        pets_panel_settings_path=pets_panel_settings_path,
        pets_panel_settings_last_error=pets_panel_settings_last_error,
        n4pro_last_applied_brightness_percent=None,
        n4pro_last_applied_lighting=None,
        n4pro_last_applied_led_brightness_percent=None,
        n4pro_session_output_last_error=None,
    )
    resolved_streamdock_n4pro_renderer_sink: StreamDockN4ProRendererSink = (
        streamdock_n4pro_renderer_sink
        or _build_default_n4pro_renderer_sink(runtime)
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
                codex_remote_ssh_hosts_reader,
                resolved_codex_remote_ssh_observer_factory,
                resolved_codex_remote_ssh_observers,
                resolved_codex_remote_pet_mirror,
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
        if resolved_poller_config.codex_remote_ssh_enabled:
            tasks.append(
                asyncio.create_task(
                    _poll_codex_remote_ssh_loop(
                        runtime,
                        observers=resolved_codex_remote_ssh_observers,
                        hosts_reader=codex_remote_ssh_hosts_reader,
                        observer_factory=resolved_codex_remote_ssh_observer_factory,
                        pet_mirror=resolved_codex_remote_pet_mirror,
                        interval_seconds=(
                            resolved_poller_config.codex_remote_ssh_interval_seconds
                        ),
                        stale_after_seconds=(
                            resolved_poller_config.codex_remote_ssh_stale_after_seconds
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
        if resolved_poller_config.codex_pet_enabled:
            tasks.append(
                asyncio.create_task(
                    _poll_codex_pet_loop(
                        runtime,
                        interval_seconds=(
                            resolved_poller_config.codex_pet_refresh_interval_seconds
                        ),
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
            try:
                if resolved_poller_config.streamdock_n4pro_renderer_enabled:
                    await _render_streamdock_n4pro_visible_splash(
                        visible_splash_touchscreen_sink,
                    )
            finally:
                try:
                    runtime.keyboard_shortcut_scheduler.close()
                finally:
                    try:
                        close_renderer = getattr(
                            resolved_streamdock_n4pro_renderer_sink,
                            "close",
                            None,
                        )
                        if callable(close_renderer):
                            close_renderer()
                    finally:
                        try:
                            for observer in (
                                resolved_codex_remote_ssh_observers.values()
                            ):
                                observer.close()
                        finally:
                            if codex_pet_temp_directory is not None:
                                codex_pet_temp_directory.cleanup()

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
        `app.css`、`device.css`、`controls.css`、`surface-swap.js` 和 `app.js`。
        返回：`FileResponse`，由 FastAPI/Starlette 按扩展名设置内容类型。
        错误处理：未知文件名、路径穿越或文件缺失返回 404。
        副作用：只读取包内静态前端资源，不读取用户目录、不访问网络或硬件。
        """

        allowed_assets = {
            "app.css",
            "device.css",
            "controls.css",
            "surface-swap.js",
            "app.js",
        }
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

    @app.get("/ui/rotary-layout")
    async def get_rotary_layout() -> dict[str, Any]:
        """返回 GUI 当前可编辑的 N4 Pro 旋钮、灯光和亮度配置。

        入参：无。
        返回：默认、runtime 或 persisted rotary layout response。
        错误处理：内置默认 layout 构造失败按 500 传播。
        副作用：只读取 daemon 内存，不写配置或硬件。
        """

        return _dump_model(runtime.current_rotary_layout_response())

    @app.get("/ui/control-capabilities")
    async def get_control_capabilities() -> dict[str, Any]:
        """返回当前 N4 Pro profile 和本机已确认系统显示器能力。

        入参：无。
        返回：硬件 capability profile、可控系统显示器和当前选中显示器 id。
        错误处理：system executor 枚举异常按 FastAPI 500 处理。
        副作用：可能读取系统显示器 capability；不写系统或硬件。
        """

        rotary = runtime.current_rotary_layout_response().layout
        return {
            "device_profile": _dump_model(get_device_profile("mirabox.n4pro")),
            "keyboard_shortcuts": _dump_model(
                runtime.keyboard_shortcut_scheduler.capability()
            ),
            "system_display_targets": [
                _dump_model(target)
                for target in runtime.system_control_executor.list_display_targets()
            ],
            "selected_system_display_id": rotary.system_display_id,
        }

    @app.post("/ui/keyboard-shortcuts/request-permission")
    async def request_keyboard_shortcut_permission() -> dict[str, Any]:
        """由用户显式点击后请求 macOS 键盘事件权限。

        入参：无；路由只接受显式 POST，不在 daemon 启动或执行失败时自动调用。
        返回：请求完成后的 JSON-safe capability。
        错误处理：native 权限 API 异常返回 500。
        副作用：macOS 可能显示辅助功能授权提示或引导用户打开隐私设置。
        """

        try:
            capability = runtime.keyboard_shortcut_scheduler.request_permission()
        except Exception as exc:  # noqa: BLE001 - native 错误需映射为明确 HTTP 诊断。
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return _dump_model(capability)

    @app.post("/ui/keyboard-shortcuts/open-accessibility-settings")
    async def open_keyboard_shortcut_accessibility_settings() -> dict[str, bool]:
        """由用户显式点击后打开 macOS 辅助功能隐私设置。

        入参：无；目标页面由固定 opener 决定，不接受 URL 或 shell 参数。
        返回：成功时 ``{"opened": true}``。
        错误处理：平台不支持、系统设置无法启动或 injected opener 失败时返回 500。
        副作用：启动或聚焦系统设置；不会替用户修改授权开关。
        """

        try:
            runtime.keyboard_accessibility_settings_opener()
        except Exception as exc:  # noqa: BLE001 - 平台 opener 错误需映射成 UI 可见诊断。
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"opened": True}

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

    @app.get("/ui/url-icons/resolve")
    async def resolve_url_icon(
        url: str = Query(min_length=1),
        force: bool = False,
    ) -> dict[str, Any]:
        """Resolve and cache one URL favicon for GUI preview.

        入参：`url` 是用户配置的网址；`force` 为 True 时强制重建缓存。
        返回：包含 icon URL、key icon URL、fallback token 和缓存状态的 dict。
        错误处理：URL 非 http/https 或缺少 host 返回 422；缓存写入失败返回 500。
        副作用：缓存缺失、过期或强制刷新时可能发起一次 favicon HTTP 请求并写 PNG/metadata。
        """

        try:
            return _dump_url_icon_for_ui(url, runtime.url_icon_cache, force=force)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/ui/url-icons/lookup")
    async def lookup_url_icon(url: str = Query(min_length=1)) -> dict[str, Any]:
        """Lookup one cached URL icon without network access.

        入参：`url` 是用户配置的网址。
        返回：缓存命中时包含 icon URL、key icon URL、fallback token 和缓存状态；未命中时
        `icon_url` 为 None。
        错误处理：URL 非 http/https 或缺少 host 返回 422。
        副作用：只读 URL icon cache，不访问网络、不写文件。
        """

        try:
            return _dump_cached_url_icon_for_ui(url, runtime.url_icon_cache)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/ui/url-icons/upload")
    async def upload_url_icon(body: UrlIconUploadRequest) -> dict[str, Any]:
        """Store one user-selected local image as the URL key icon.

        入参：`body` 包含目标 URL、文件名和 base64 data URL。
        返回：缓存后的 icon URL、key icon URL 和状态。
        错误处理：URL 非法、data URL 非法或图片无法解析返回 422；缓存写入失败返回 500。
        副作用：把用户选择的本地图像转换成 PNG 并写入 URL icon cache。
        """

        try:
            image_bytes = _decode_upload_data_url(body.data_url)
            cached = runtime.url_icon_cache.store_custom_icon(
                body.url,
                image_bytes=image_bytes,
                filename=body.filename,
            )
            return _dump_cached_url_icon(cached)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (OSError, binascii.Error) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/ui/url-icons/{cache_key}/{asset_name}")
    async def get_url_icon(cache_key: str, asset_name: str) -> FileResponse:
        """Return one cached URL icon PNG.

        入参：`cache_key` 是缓存目录名；`asset_name` 必须是允许的 PNG 名称。
        返回：`FileResponse`。
        错误处理：未知 cache key、未知文件名或文件不存在返回 404。
        副作用：只读取 Agent Deck URL icon cache 文件。
        """

        icon_path = runtime.url_icon_cache.resolve_file(cache_key, asset_name)
        if icon_path is None:
            raise HTTPException(status_code=404, detail="url icon is not cached")
        return FileResponse(icon_path)

    @app.get("/ui/shortcut-icons/auto-preview.png")
    async def get_shortcut_auto_icon_preview(
        spec: str = Query(min_length=1, max_length=12_000),
    ) -> Response:
        """返回与 N4 Pro 硬件下发共用 renderer 的快捷键自动图标。

        入参：``spec`` 是 URL 编码后的 ``KeyboardShortcutSpec`` JSON。
        返回：112px RGB PNG；Web 预览可直接把本路由用作 ``img.src``。
        错误处理：JSON 或快捷键模型非法返回 422；PNG 编码异常按 500 传播。
        副作用：只读取进程内自动图标缓存，并把图片编码到内存 buffer。
        """

        try:
            shortcut = KeyboardShortcutSpec.model_validate_json(spec)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        image = runtime.shortcut_key_image_cache.image(shortcut)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return Response(
            content=buffer.getvalue(),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/ui/shortcut-icons/upload")
    async def upload_shortcut_icon(body: ShortcutIconUploadRequest) -> dict[str, Any]:
        """保存一个用户选择的快捷键自定义图标。

        入参：body 包含可选文件名和 base64 data URL。
        返回：内容寻址 asset id、原始尺寸和 Web/硬件 PNG URL。
        错误处理：MIME、base64、图片、大小或尺寸非法返回 422；写入失败返回 500。
        副作用：在快捷键图标 store 写规范化 PNG、96px 预览、112px key 图和 metadata。
        """

        try:
            image_bytes = _decode_shortcut_icon_data_url(body.data_url)
            asset = runtime.shortcut_icon_store.store(
                image_bytes,
                filename=body.filename,
            )
            return _dump_model(asset)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (OSError, binascii.Error) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/ui/shortcut-icons/{asset_id}/{asset_name}")
    async def get_shortcut_icon(asset_id: str, asset_name: str) -> FileResponse:
        """返回快捷键自定义图标的白名单派生 PNG。

        入参：64 位内容 hash 和 preview-96.png/key-112.png 文件名。
        返回：存在时返回 ``FileResponse``。
        错误处理：非法 id、未知文件名或缺失资产返回 404。
        副作用：只读图标 store 文件。
        """

        icon_path = runtime.shortcut_icon_store.resolve_file(asset_id, asset_name)
        if icon_path is None:
            raise HTTPException(status_code=404, detail="shortcut icon is not cached")
        return FileResponse(icon_path)

    @app.put("/ui/configuration")
    async def put_console_configuration(
        request: ConsoleConfigurationApplyRequest,
    ) -> dict[str, Any]:
        """保存并应用 GUI 的主按键与旋钮草稿。

        入参：`request` 同时携带完整 key/rotary layout，路由层先整体校验。
        返回：两个已应用 layout response 和当前 projection。
        错误处理：body 非法返回 422；任一 JSON 持久化失败返回 500，旧 runtime state 不被覆盖。
        副作用：成功时更新 daemon applied 配置，真实硬件等待下一个统一 renderer tick 下发。
        """

        try:
            return runtime.update_console_configuration(request)
        except (
            KeyLayoutStoreError,
            RotaryLayoutStoreError,
            PetsPanelSettingsStoreError,
        ) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/ui/pets-panel-settings")
    async def get_pets_panel_settings() -> dict[str, Any]:
        """返回 N4 Pro PETS 虚拟面板的当前设置。

        入参：无。
        返回：来源、持久化路径、完整设置和可选启动错误。
        错误处理：无。
        副作用：只读 runtime 快照，不访问磁盘、远端或硬件。
        """

        return _dump_model(runtime.current_pets_panel_settings_response())

    @app.put("/ui/pets-panel-settings")
    async def put_pets_panel_settings(
        settings: N4ProPetsPanelSettings,
    ) -> dict[str, Any]:
        """保存并应用 N4 Pro PETS 虚拟面板设置。

        入参：远端宠物来源和巡游速度完整设置。
        返回：当前设置响应与宠物诊断。
        错误处理：请求非法返回 422，持久化失败返回 500 并保留旧 applied 设置。
        副作用：成功时写可选用户级 JSON、更新 runtime 并刷新当前 PETS 面板。
        """

        try:
            return runtime.update_pets_panel_settings(settings)
        except PetsPanelSettingsStoreError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

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

    @app.put("/ui/rotary-layout")
    async def put_rotary_layout(layout: N4ProRotaryLayout) -> dict[str, Any]:
        """保存并应用一份完整 N4 Pro rotary layout。

        入参：`layout` 是完整四旋钮、灯圈组和整体亮度配置。
        返回：已应用 rotary layout、N4 Pro capability 和逻辑面板预览诊断。
        错误处理：请求体非法返回 422；JSON 持久化失败返回 500 并保留原 applied layout。
        副作用：更新 runtime 和可选用户级 layout JSON；真实硬件在下一 renderer tick 应用。
        """

        try:
            return runtime.update_rotary_layout(layout)
        except RotaryLayoutStoreError as exc:
            runtime.rotary_layout_last_error = str(exc)
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
    codex_remote_ssh_hosts_reader: CodexRemoteSshHostsReader,
    codex_remote_ssh_observer_factory: CodexRemoteSshObserverFactory,
    codex_remote_ssh_observers: dict[str, CodexRemoteSshObserverProtocol],
    codex_remote_pet_mirror: CodexRemotePetMirrorProtocol,
    codex_quota_reader: CodexQuotaReader,
    codex_token_usage_reader: CodexTokenUsageReader,
    quota_touchscreen_sink: QuotaTouchscreenSink,
    streamdock_n4pro_renderer_sink: StreamDockN4ProRendererSink,
) -> None:
    """Run each enabled poller once during app startup.

    入参：`runtime` 是 daemon 内存状态；`config` 是 poller 配置；Codex reader、SSH observer、
    远端宠物镜像、quota/token reader、触屏 sink 和统一 N4 Pro renderer sink 是可注入数据源/输出端。
    返回：无显式返回值。
    错误处理：单个 poller 的异常由 poll-once helper 记录，另一个 poller 仍会继续执行。
    副作用：可能只读访问 Codex 本地状态、启动 SSH/app-server/SFTP、写 Agent Deck 素材缓存，
    并更新 runtime 和 fake surface。
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
    if config.codex_remote_ssh_enabled:
        await _poll_codex_remote_ssh_once(
            runtime,
            observers=codex_remote_ssh_observers,
            hosts_reader=codex_remote_ssh_hosts_reader,
            observer_factory=codex_remote_ssh_observer_factory,
            pet_mirror=codex_remote_pet_mirror,
            stale_after_seconds=config.codex_remote_ssh_stale_after_seconds,
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
    if config.codex_pet_enabled:
        await _poll_codex_pet_once(runtime)
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


async def _poll_codex_remote_ssh_loop(
    runtime: _DaemonRuntime,
    *,
    observers: dict[str, CodexRemoteSshObserverProtocol],
    hosts_reader: CodexRemoteSshHostsReader,
    observer_factory: CodexRemoteSshObserverFactory,
    pet_mirror: CodexRemotePetMirrorProtocol,
    interval_seconds: float,
    stale_after_seconds: float,
) -> None:
    """周期性读取多个 SSH host 的远端 ChatGPT App 粗粒度状态。

    入参：``runtime`` 是 daemon 状态；``observers`` 按 host id 保存动态只读连接；
    ``hosts_reader`` 只读 ChatGPT Settings；``observer_factory`` 为新启用主机创建 observer；
    ``pet_mirror`` 仅按 remote_config 策略镜像 custom 素材；``interval_seconds`` 是轮询间隔；
    ``stale_after_seconds`` 控制失联清理。
    返回：不主动返回；任务取消时结束。
    错误处理：单 host 异常在 poll-once 内记录，不终止其他 host 或后台循环。
    副作用：周期性使用 observer 的 SSH 连接并更新内存 agent/store 诊断。
    """

    while True:
        await asyncio.sleep(interval_seconds)
        await _poll_codex_remote_ssh_once(
            runtime,
            observers=observers,
            hosts_reader=hosts_reader,
            observer_factory=observer_factory,
            pet_mirror=pet_mirror,
            stale_after_seconds=stale_after_seconds,
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


async def _poll_codex_pet_loop(
    runtime: _DaemonRuntime,
    *,
    interval_seconds: float,
) -> None:
    """周期性只读刷新 Codex 全局宠物选择与自定义素材。

    入参：``runtime`` 持有 resolver/controller；``interval_seconds`` 来自 ``[codex.pet]``。
    返回：不主动返回；任务取消时结束。
    错误处理：单次意外异常由 ``_poll_codex_pet_once`` 写入短诊断，不终止循环。
    副作用：周期性读取 Codex 配置/图集；素材指纹变化时重建 daemon 临时 Key PNG。
    """

    while True:
        await asyncio.sleep(interval_seconds)
        await _poll_codex_pet_once(runtime)


async def _run_tracked_sync_worker(
    function: Callable[..., Any],
    /,
    *args: object,
    **kwargs: object,
) -> Any:
    """在线程池执行一个同步 worker，并在调用任务取消后等待已启动工作真正结束。

    入参：``function`` 是可能访问 renderer 或宠物临时缓存的同步函数；其余位置和关键字参数
    原样传入。返回：同步函数的返回值。错误处理：正常执行时原样传播 worker 异常；调用任务
    被取消时先等待已启动 worker 收尾，再重新抛出 ``CancelledError``。副作用：创建一个仅跟踪
    本次已开始工作的 asyncio task；不会预排下一轮 poll，也不会让尚在 sleep 的循环启动工作。
    """

    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        with suppress(Exception):
            await worker
        raise


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
        key_images = runtime.publish_hardware_key_surface_images(
            layout,
            notify=False,
        )
        result = await _run_tracked_sync_worker(
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
    url_icon_cache: UrlIconCache | None = None,
    shortcut_icon_store: ShortcutIconStore | None = None,
    shortcut_key_cache: ShortcutKeyImageCache | None = None,
    quota_snapshot: CodexQuotaSnapshot | None = None,
    token_usage_snapshot: CodexTokenUsageSnapshot | None = None,
    status_key_cache: StatusKeyImageCache | None = None,
) -> dict[int, Any]:
    """从 layout 提取 N4 Pro 静态主键图片。

    入参：`layout` 是当前 daemon layout；`app_icon_cache` 是可选 App 图标缓存；
    `url_icon_cache` 是可选 URL favicon 缓存；`shortcut_icon_store` 只读自定义图标；
    `shortcut_key_cache` 缓存自动快捷键图；`quota_snapshot` 和 `token_usage_snapshot` 是 touchbar
    面板已经复用的状态数据；`status_key_cache` 缓存状态按键渲染结果。
    返回：物理按钮编号到 Pillow 图像的映射；包含 App、URL、快捷键和状态型主键。
    错误处理：单个图标读取失败会 fallback 成 token 图，不影响整轮渲染。
    副作用：可能只读 `.app` bundle 图标资源或访问 favicon 缓存；状态图缓存 miss 时会创建
    内存图片；不访问硬件、不启动 App、不执行 ccusage。
    """

    key_images: dict[int, Any] = {}
    resolved_status_key_cache = status_key_cache or StatusKeyImageCache()
    resolved_shortcut_key_cache = shortcut_key_cache or ShortcutKeyImageCache()
    for key in layout.keys[:10]:
        if key.kind == "app":
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
        if key.kind == "url":
            url = key.payload.get("url") or key.label
            cached_url_image = None
            if url_icon_cache is not None:
                cached_url_image = url_icon_cache.key_image_for_url(url)
            key_images[key.index + 1] = cached_url_image or render_url_key_image(
                url=url,
            )
        if key.kind == "keyboard_shortcut" and key.shortcut is not None:
            custom_image = None
            if (
                shortcut_icon_store is not None
                and key.shortcut_icon is not None
                and key.shortcut_icon.mode.value == "custom"
                and key.shortcut_icon.asset_id is not None
            ):
                custom_image = shortcut_icon_store.key_image(
                    key.shortcut_icon.asset_id
                )
            key_images[key.index + 1] = (
                custom_image or resolved_shortcut_key_cache.image(key.shortcut)
            )
        if key.kind == "quota_status" and quota_snapshot is not None:
            key_images[key.index + 1] = resolved_status_key_cache.quota_image(
                quota_snapshot,
                window=key.payload.get("quota_window"),
            )
        if key.kind == "usage_summary" and token_usage_snapshot is not None:
            key_images[key.index + 1] = resolved_status_key_cache.usage_image(
                token_usage_snapshot,
                period=key.payload.get("usage_period"),
            )
    return key_images


def _same_key_image_source(previous: Any, current: Any) -> bool:
    """比较硬件 Key 图来源是否代表同一缓存视觉。

    入参：``previous``/``current`` 可以是 Pillow image、预渲染 ``Path`` 或 None。
    返回：Path 按路径值相等、其他对象按引用相同则为 True。
    错误处理：无；未知来源仍按引用保守比较。
    副作用：无；不检查路径存在性、不打开图片。
    """

    if isinstance(previous, Path) or isinstance(current, Path):
        return isinstance(previous, Path) and isinstance(current, Path) and previous == current
    return previous is current


def _changed_hardware_key_image_sources(
    previous: dict[int, Any],
    current: dict[int, Any],
) -> dict[int, Any]:
    """比较 daemon 两轮完整静态键映射，并显式保留删除项。

    入参：``previous`` 是上轮已发布映射；``current`` 是本轮完整映射。
    返回：新增/替换项携带当前图片，删除项携带 ``None``，供 revision 与诊断计数传播；
    persistent animator 会在硬件边界把删除项转换成稳定清屏图。
    错误处理：无；Path/图片比较由 ``_same_key_image_source`` 完成。
    副作用：无；只创建差异字典，不访问文件或硬件。
    """

    changed: dict[int, Any] = {}
    for key in previous.keys() | current.keys():
        if key not in current:
            changed[key] = None
            continue
        if key not in previous or not _same_key_image_source(
            previous[key],
            current[key],
        ):
            changed[key] = current[key]
    return changed


def _normalize_quota_status_window(value: str | None) -> QuotaStatusWindow:
    """把配置或 intent 中的 quota window 归一成渲染器可接受的值。

    入参：`value` 是用户配置、layout payload 或硬件 intent 中的窗口字符串。
    返回：非空的 `auto`、稳定 window_id 或遗留槽位名；空值降级到 `auto`。
    错误处理：不抛业务异常，避免坏配置打断硬件渲染循环。
    副作用：无。
    """

    return value.strip() if value and value.strip() else "auto"


def _normalize_token_usage_period(value: str | None) -> CodexTokenPeriod:
    """把配置或 intent 中的 token usage 周期归一成枚举。

    入参：`value` 是用户配置、layout payload 或硬件 intent 中的周期字符串。
    返回：合法 `CodexTokenPeriod`；未知值降级到 `today`。
    错误处理：不抛业务异常，避免坏配置打断硬件渲染循环。
    副作用：无。
    """

    try:
        return CodexTokenPeriod(value or CodexTokenPeriod.TODAY.value)
    except ValueError:
        return CodexTokenPeriod.TODAY


def _next_quota_status_window(
    value: str | None,
    *,
    snapshot: CodexQuotaSnapshot | None = None,
) -> str:
    """返回 quota status 按键下一个展示窗口。

    入参：`value` 是当前窗口。
    返回：多窗口时在实际 `window_id` 之间循环；`auto` 和过期值先解析为默认
    实际窗口，再返回其后一个窗口。单窗口或没有快照时保持 `auto`。
    错误处理：未知值按 quota 快照的默认窗口解析；解析结果异常时回退第一个实际窗口。
    副作用：无。
    """

    if snapshot is None:
        return "auto"
    available = tuple(item.window_id for item in snapshot.available_windows())
    if len(available) <= 1:
        return "auto"
    current = _normalize_quota_status_window(value)
    if current not in available:
        resolved = snapshot.resolved_window(current).window_id
        current = resolved if resolved in available else available[0]
    return available[(available.index(current) + 1) % len(available)]


def _next_token_usage_period(value: str | None) -> str:
    """返回 usage summary 按键下一个统计周期。

    入参：`value` 是当前周期。
    返回：按 `today -> week -> month -> all -> today` 循环后的字符串。
    错误处理：未知值视为 `today`。
    副作用：无。
    """

    order = (
        CodexTokenPeriod.TODAY,
        CodexTokenPeriod.WEEK,
        CodexTokenPeriod.MONTH,
        CodexTokenPeriod.ALL,
    )
    current = _normalize_token_usage_period(value)
    return order[(order.index(current) + 1) % len(order)].value


def _token_usage_period_fingerprint(
    snapshot: CodexTokenUsageSnapshot,
) -> tuple[tuple[str, int, float], ...]:
    """生成 token usage 四周期汇总的轻量缓存指纹。

    入参：`snapshot` 是当前 token usage 快照。
    返回：周期、总 token、金额三元组序列。
    错误处理：缺失周期按当前 mapping 内容生成，渲染器后续仍会按原语义报错。
    副作用：无。
    """

    return tuple(
        (
            period.value,
            stats.total_tokens,
            round(stats.cost_usd, 6),
        )
        for period, stats in sorted(
            snapshot.periods.items(),
            key=lambda item: item[0].value,
        )
    )


def _quota_panel_fingerprint(
    snapshot: CodexQuotaSnapshot,
) -> tuple[object, ...]:
    """生成 quota touch bar 基础图的内容缓存指纹。

    入参：`snapshot` 是当前 Codex quota 快照。
    返回：覆盖计划名、实际可用窗口、重置时间和可用重置次数的不可变元组。
    错误处理：无；adapter 已保证快照字段可读。
    副作用：无；只读取内存快照。
    """

    return (
        snapshot.plan_type,
        snapshot.plan_short_label,
        snapshot.plan_display_name,
        _quota_windows_fingerprint(snapshot),
        snapshot.credits_balance,
        snapshot.reset_credits_available,
    )


def _quota_windows_fingerprint(
    snapshot: CodexQuotaSnapshot,
) -> tuple[tuple[str, str, str | None, int, int, str], ...]:
    """生成 quota 实际窗口的缓存指纹，正确区分缺失窗口与零用量窗口。

    入参：`snapshot` 是 adapter 已校验的 quota 快照。
    返回：每个实际窗口的稳定 id、所属 limit、已用比例、时长和重置时间组成的稳定元组。
    错误处理：无；至少一个窗口由快照模型保证。
    副作用：无；只读取内存字段。
    """

    return tuple(
        (
            window.window_id,
            window.limit_id,
            window.presentation_label,
            window.used_percent,
            window.window_duration_mins,
            window.resets_at.isoformat(),
        )
        for window in snapshot.available_windows()
    )


def _normalize_logical_panel_quota_window(
    selection: PanelSelection,
    snapshot: CodexQuotaSnapshot,
) -> PanelSelection:
    """在 quota 刷新后把已失效的 virtual panel 窗口选择回退到实际可用项。

    入参：`selection` 是当前面板选择；`snapshot` 是新成功的 quota 快照。
    返回：选择仍有效或为 auto 时返回原对象；否则将 quota 窗口设为第一个实际 window_id。
    错误处理：snapshot 至少包含一个窗口，故不会出现空回退。
    副作用：无；不修改用户保存的主键配置，只修正 runtime 面板选择。
    """

    available = tuple(window.window_id for window in snapshot.available_windows())
    if selection.quota_window == "auto" or selection.quota_window in available:
        return selection
    return selection.model_copy(update={"quota_window": available[0]})


def _token_usage_daily_fingerprint(
    snapshot: CodexTokenUsageSnapshot,
) -> tuple[tuple[str, int, float], ...]:
    """生成 ccusage daily raw 的趋势缓存指纹。

    入参：`snapshot` 是当前 token usage 快照。
    返回：日期、总 token、金额三元组序列；raw 不符合预期时返回空 tuple。
    错误处理：非 dict/list 结构会被忽略，避免缓存指纹构造打断状态页。
    副作用：无。
    """

    raw_daily = snapshot.raw.get("daily") if isinstance(snapshot.raw, dict) else None
    if not isinstance(raw_daily, list):
        return ()
    fingerprint: list[tuple[str, int, float]] = []
    for item in raw_daily:
        if not isinstance(item, dict):
            continue
        date_value = item.get("date")
        if not isinstance(date_value, str):
            continue
        try:
            total_tokens = int(item.get("totalTokens", item.get("total_tokens", 0)) or 0)
            cost_usd = float(item.get("totalCost", item.get("cost_usd", 0.0)) or 0.0)
        except (TypeError, ValueError):
            continue
        fingerprint.append((date_value, total_tokens, round(cost_usd, 6)))
    return tuple(fingerprint)


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


def _qualified_codex_app_session_id(session: CodexAppActiveSession) -> str:
    """生成不会让本地和不同 SSH host thread id 冲突的 session id。

    入参：``session`` 是本地或远端 App 会话。
    返回：本地保持原 thread id；远端使用 ``remote-ssh:<host-id>:<thread-id>``。
    错误处理：模型已保证字段是字符串，本函数不额外抛业务异常。
    副作用：无。
    """

    if not session.is_remote:
        return session.thread_id
    return f"remote-ssh:{session.execution_host_id}:{session.thread_id}"


def _qualified_codex_app_parent_session_id(
    session: CodexAppActiveSession,
) -> str | None:
    """把父 thread id 投影到与子会话一致的 host namespace。

    入参：``session`` 可包含 parent_thread_id。
    返回：无 parent 时 None；本地返回原 id；远端返回 host-aware id。
    错误处理：无。
    副作用：无。
    """

    if session.parent_thread_id is None:
        return None
    if not session.is_remote:
        return session.parent_thread_id
    return (
        f"remote-ssh:{session.execution_host_id}:{session.parent_thread_id}"
    )


def _codex_app_focus_target(session: CodexAppActiveSession) -> str:
    """生成仍由本机 ChatGPT App action 处理的 host-aware focus target。

    入参：``session`` 是本地或远端 App 会话。
    返回：以 ``codex-app:`` 开头的目标；远端目标带 host namespace，便于诊断和宠物筛选。
    错误处理：无。
    副作用：无；不会尝试切换 ChatGPT App 内部 thread。
    """

    if not session.is_remote:
        return f"codex-app:{session.thread_id}" if session.thread_id else "app:ChatGPT"
    return (
        f"codex-app:remote-ssh:{session.execution_host_id}:{session.thread_id}"
    )


def _codex_app_session_display_name(session: CodexAppActiveSession) -> str | None:
    """为远端会话添加主机标签，同时保持本地标题不变。

    入参：``session`` 带可选 title 和 execution_host_label。
    返回：本地原 title；远端 ``<host> · <title>``。
    错误处理：无。
    副作用：无。
    """

    if not session.is_remote:
        return session.title
    host_label = session.execution_host_label or session.execution_host_id
    title = session.title or "ChatGPT 远端任务"
    return f"{host_label} · {title}"


def _dump_codex_remote_ssh_diagnostic(
    diagnostic: Mapping[str, Any],
) -> dict[str, Any]:
    """把 runtime 内部 SSH host 诊断转换为 JSON-safe 且脱敏的 dict。

    入参：``diagnostic`` 只包含 host 别名、时间、计数、短错误类型和 server user-agent。
    返回：datetime 已转换为 ISO 字符串的新 dict。
    错误处理：未知字段被原样复制；调用方保证没有 prompt/raw response。
    副作用：无；不修改原 mapping。
    """

    dumped = dict(diagnostic)
    for key in (
        "first_success_at",
        "last_success_at",
        "last_polled_at",
        "last_checked_at",
    ):
        value = dumped.get(key)
        if isinstance(value, datetime):
            dumped[key] = _dump_datetime(value)
    return dumped


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


async def _poll_codex_remote_ssh_once(
    runtime: _DaemonRuntime,
    *,
    observers: dict[str, CodexRemoteSshObserverProtocol],
    hosts_reader: CodexRemoteSshHostsReader,
    observer_factory: CodexRemoteSshObserverFactory,
    stale_after_seconds: float,
    pet_mirror: CodexRemotePetMirrorProtocol | None = None,
) -> None:
    """发现并并行读取一次 ChatGPT Settings 当前已启用的 SSH hosts。

    入参：``runtime`` 是 daemon 内存状态；``observers`` 是可动态增删的 observer 映射；
    ``hosts_reader`` 只能返回 ChatGPT 已管理且 auto-connect=true 的 SSH connection；
    ``observer_factory`` 为新启用主机创建 observer；``pet_mirror`` 只在策略为
    remote_config 且 config/read 返回 custom ID 时读取两个素材文件；``stale_after_seconds``
    控制启用主机持续连接失败后清理旧远端 agent。
    返回：无。
    错误处理：发现失败时 fail-closed，关闭全部 observer 并清理状态；单 host 普通异常只写入
    诊断并继续；task cancellation 正常向上传播。
    副作用：设置关闭的主机会立即关闭；启用主机通过 ``asyncio.to_thread`` 使用独立 SSH
    连接，并更新 host-aware agent 状态与渲染。
    """

    checked_at = datetime.now(UTC)
    try:
        discovery = await asyncio.to_thread(hosts_reader)
    except Exception as exc:  # noqa: BLE001 - 设置不可判定时必须 fail-closed。
        runtime.mark_codex_remote_ssh_discovery_error(exc, checked_at=checked_at)
        for host_id, observer in tuple(observers.items()):
            with suppress(Exception):
                observer.close()
            observers.pop(host_id, None)
            runtime.remove_codex_remote_ssh_host(host_id)
        return

    runtime.mark_codex_remote_ssh_discovery_success(discovery)
    enabled_by_host_id = {
        codex_remote_host_id(enabled_host.alias): enabled_host
        for enabled_host in discovery.enabled_hosts
    }
    for host_id in tuple(observers):
        if host_id in enabled_by_host_id:
            continue
        observer = observers.pop(host_id)
        with suppress(Exception):
            observer.close()
        runtime.remove_codex_remote_ssh_host(host_id)
    for host_id, enabled_host in enabled_by_host_id.items():
        if host_id in observers:
            continue
        try:
            observers[host_id] = observer_factory(enabled_host)
        except Exception as exc:  # noqa: BLE001 - 单主机构造失败不影响其他已启用主机。
            runtime.mark_codex_remote_ssh_poll_error(
                host=enabled_host.display_name,
                host_id=host_id,
                error=exc,
                polled_at=discovery.observed_at,
                stale_after_seconds=stale_after_seconds,
            )

    if not observers:
        return
    read_remote_pet_config = runtime.codex_pet.reads_remote_pet_config
    for observer in observers.values():
        setter = getattr(observer, "set_read_pet_config", None)
        if callable(setter):
            setter(read_remote_pet_config)
    observer_items = tuple(observers.items())
    results = await asyncio.gather(
        *(
            asyncio.to_thread(observer.read_snapshot)
            for _host_id, observer in observer_items
        ),
        return_exceptions=True,
    )
    polled_at = datetime.now(UTC)
    for (configured_host_id, observer), result in zip(
        observer_items,
        results,
        strict=True,
    ):
        if isinstance(result, Exception):
            runtime.mark_codex_remote_ssh_poll_error(
                host=observer.host,
                host_id=configured_host_id,
                error=result,
                polled_at=polled_at,
                stale_after_seconds=stale_after_seconds,
            )
            continue
        snapshot = result
        runtime.apply_codex_active_sessions(
            snapshot.sessions,
            observed_at=snapshot.observed_at,
            observation_scope=f"remote-ssh:{snapshot.host_id}",
        )
        runtime.mark_codex_remote_ssh_poll_success(snapshot)
        should_mirror_custom = (
            pet_mirror is not None
            and read_remote_pet_config
            and snapshot.pet_config_available
            and snapshot.selected_avatar_id is not None
            and snapshot.selected_avatar_id.startswith("custom:")
        )
        if not should_mirror_custom:
            runtime.codex_pet.update_remote_custom_pet_resolution(
                snapshot.host_id,
                None,
            )
            continue
        enabled_host = enabled_by_host_id.get(configured_host_id)
        if enabled_host is None:
            runtime.codex_pet.update_remote_custom_pet_resolution(
                snapshot.host_id,
                None,
            )
            continue
        try:
            mirror_resolution = await asyncio.to_thread(
                pet_mirror.resolve,
                host=enabled_host.alias,
                host_id=snapshot.host_id,
                selected_avatar_id=snapshot.selected_avatar_id,
                now=snapshot.observed_at,
            )
        except Exception:  # noqa: BLE001 - 素材镜像失败不得影响远端任务状态。
            runtime.codex_pet.update_remote_custom_pet_resolution(
                snapshot.host_id,
                None,
            )
            continue
        runtime.codex_pet.update_remote_custom_pet_resolution(
            snapshot.host_id,
            mirror_resolution,
        )


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


async def _poll_codex_pet_once(runtime: _DaemonRuntime) -> None:
    """只读刷新一次 Codex 宠物，并发布最新 Key/PETS surface。

    入参：``runtime`` 持有线程安全宠物协调器与当前 layout。
    返回：无。
    错误处理：resolver 的已建模失败进入 ``codex_pet`` status；未建模异常也转成短诊断，
    不阻断其他 poller 或 daemon 启动。
    副作用：在线程中读取 Codex 配置/自定义图集并按需写临时 Key PNG；成功或降级后只更新
    内存 surface revision，不创建硬件会话。
    """

    polled_at = datetime.now(UTC)
    try:
        await _run_tracked_sync_worker(runtime.codex_pet.refresh, now=polled_at)
        layout = runtime.render_current()
        runtime.publish_hardware_key_surface_images(layout)
        if runtime.effective_logical_panel_kind() == PanelKind.PETS:
            runtime.render_current_logical_panel_image()
    except Exception as exc:  # noqa: BLE001 - 宠物展示不能杀 daemon。
        runtime.codex_pet.mark_refresh_error(exc, updated_at=polled_at)


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


def _dump_url_icon_for_ui(
    url: str,
    url_icon_cache: UrlIconCache,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """把 URL favicon 缓存条目序列化为 GUI 可消费的 dict。

    入参：`url` 是用户输入的网址；`url_icon_cache` 是 favicon 缓存；`force` 控制是否强制重建。
    返回：包含 origin、host、`icon_url`、`key_icon_url` 和缓存状态的 dict。
    错误处理：URL 非法时由 cache 抛 ValueError；缓存写入失败按 OSError 传播。
    副作用：缓存缺失、过期或强制刷新时可能发起 HTTP 请求并写 PNG/metadata 文件。
    """

    cached = url_icon_cache.ensure(url, force=force)
    return _dump_cached_url_icon(cached)


def _dump_cached_url_icon_for_ui(
    url: str,
    url_icon_cache: UrlIconCache,
) -> dict[str, Any]:
    """只读序列化 URL icon cache 条目。

    入参：`url` 是用户输入的网址；`url_icon_cache` 是 favicon 缓存。
    返回：缓存命中时包含 icon URL；未命中时包含 origin/host/token 和空 icon URL。
    错误处理：URL 非法时由 cache 抛 ValueError。
    副作用：只读缓存目录，不访问网络、不写文件。
    """

    cached = url_icon_cache.lookup(url)
    if cached is not None:
        return _dump_cached_url_icon(cached)
    origin = _url_icon_cache_origin(url)
    return {
        "origin": origin["origin"],
        "host": origin["host"],
        "icon_token": origin["icon_token"],
        "icon_url": None,
        "key_icon_url": None,
        "icon_cache_key": None,
        "icon_cache_status": "missing",
        "icon_cache_updated": False,
        "icon_cache_fallback_reason": None,
        "icon_cache_source": None,
    }


def _dump_cached_url_icon(cached: CachedUrlIcon) -> dict[str, Any]:
    """序列化 URL icon cache 条目。

    入参：`cached` 是 URL icon cache 返回的条目。
    返回：GUI 可消费的 dict。
    错误处理：无。
    副作用：无。
    """

    return {
        "origin": cached.origin,
        "host": cached.host,
        "icon_token": cached.icon_token,
        "icon_url": cached.icon_url,
        "key_icon_url": cached.key_icon_url,
        "icon_cache_key": cached.cache_key,
        "icon_cache_status": cached.status,
        "icon_cache_updated": cached.updated,
        "icon_cache_fallback_reason": cached.fallback_reason,
        "icon_cache_source": cached.source,
    }


def _url_icon_cache_origin(url: str) -> dict[str, str]:
    """解析 URL icon cache lookup 未命中时仍需要返回的 origin 信息。

    入参：`url` 是用户输入的网址。
    返回：包含 origin、host 和 token 的 dict。
    错误处理：URL 非法时抛 ValueError。
    副作用：无。
    """

    origin = origin_for_url(url)
    return {
        "origin": origin,
        "host": origin.split("://", 1)[-1],
        "icon_token": token_for_url(origin),
    }


def _decode_upload_data_url(data_url: str) -> bytes:
    """解码浏览器 FileReader 生成的 base64 data URL。

    入参：`data_url` 应形如 `data:image/png;base64,...`。
    返回：原始图片 bytes。
    错误处理：非 data URL、非图片 MIME 或 base64 非法时抛 ValueError。
    副作用：无。
    """

    header, separator, encoded = data_url.partition(",")
    if separator != "," or not header.startswith("data:"):
        raise ValueError("url icon upload requires data URL")
    media_type = header[5:].split(";", 1)[0].lower()
    if media_type not in {"image/png", "image/jpeg", "image/webp", "image/x-icon"}:
        raise ValueError(f"unsupported url icon media type: {media_type or '<none>'}")
    try:
        return base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValueError("invalid url icon base64 payload") from exc


def _decode_shortcut_icon_data_url(data_url: str) -> bytes:
    """解码快捷键图标上传的受限 base64 data URL。

    入参：``data_url`` 应使用 PNG/JPEG/WebP/ICO MIME 且包含 base64 标记。
    返回：原始图片 bytes；真实格式、5 MiB 和像素尺寸随后由 store 再校验。
    错误处理：结构、MIME、非 base64 或 payload 非法时抛 ValueError。
    副作用：无；只分配解码后的内存 bytes。
    """

    header, separator, encoded = data_url.partition(",")
    if separator != "," or not header.startswith("data:") or ";base64" not in header:
        raise ValueError("shortcut icon upload requires a base64 data URL")
    media_type = header[5:].split(";", 1)[0].lower()
    if media_type not in {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/x-icon",
        "image/vnd.microsoft.icon",
    }:
        raise ValueError(
            f"unsupported shortcut icon media type: {media_type or '<none>'}"
        )
    try:
        return base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValueError("invalid shortcut icon base64 payload") from exc


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


def _set_n4pro_brightness(device: object, percent: int) -> str | None:
    """调用 N4 Pro SDK 的整体亮度 API，并把兼容错误归一为诊断文本。

    入参：`device` 是已 open/init 的官方 SDK 对象；`percent` 是 0 到 100 的目标亮度。
    返回：成功时 None；缺方法、SDK 返回非零 TransportResult 或异常时返回错误说明。
    错误处理：不抛 SDK 异常，避免控制台附加输出破坏主图像刷新。
    副作用：成功时调用设备 `set_brightness(percent)`。
    """

    setter = getattr(device, "set_brightness", None)
    if not callable(setter):
        return "N4 Pro SDK does not expose set_brightness"
    try:
        result = setter(percent)
    except Exception as exc:
        return f"set_brightness failed: {type(exc).__name__}: {exc}"
    if streamdock_sdk_result_failed(result):
        return f"set_brightness failed: SDK returned {result}"
    return None


def _set_n4pro_group_lighting(
    device: object,
    mode: str,
    color: str | None,
    breathe: bool,
) -> str | None:
    """调用 N4 Pro 单一灯圈组颜色 API，不伪造每旋钮独立写入。

    入参：`device` 是已 open/init 的官方 SDK 对象；`mode` 是 off 或 color；`color` 是经模型
    校验的 `#RRGGBB` 或 None；`breathe` 只用于完整接收持久化配置，亮度周期另由 LED brightness
    API 写入。
    返回：成功时 None；缺方法、模式非法、SDK 返回非零 TransportResult 或异常时返回错误说明。
    错误处理：不抛 SDK 异常，图像刷新仍可继续。
    副作用：成功时调用一次 `set_led_color(r, g, b)`，off 发送全零。
    """

    setter = getattr(device, "set_led_color", None)
    if not callable(setter):
        return "N4 Pro SDK does not expose set_led_color"
    if mode == "off":
        red, green, blue = (0, 0, 0)
    elif mode == "color" and color is not None:
        red, green, blue = tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
    else:
        return "invalid N4 Pro lighting configuration"
    try:
        result = setter(red, green, blue)
    except Exception as exc:
        return f"set_led_color failed: {type(exc).__name__}: {exc}"
    if streamdock_sdk_result_failed(result):
        return f"set_led_color failed: SDK returned {result}"
    return None


def _n4pro_led_brightness_percent(*, mode: str, breathe: bool) -> int:
    """根据灯光模式计算当前 N4 Pro group LED 亮度。

    入参：`mode` 是已校验的 off/color；`breathe` 仅在 color 下有效。
    返回：关闭时为 0，常亮时为 100，呼吸时在 24 到 86 之间按 3.6 秒正弦周期平滑变化。
    错误处理：未知 mode 按关闭处理，避免无效配置点亮设备。
    副作用：读取 monotonic 时钟，不访问 SDK 或修改硬件。
    """

    if mode != "color":
        return 0
    if not breathe:
        return 100
    phase = (time.monotonic() % 3.6) / 3.6
    return round(24 + ((math.sin((phase * math.tau) - (math.pi / 2)) + 1) / 2) * 62)


def _set_n4pro_group_led_brightness(device: object, percent: int) -> str | None:
    """调用 N4 Pro group LED 亮度 API，供可选软件呼吸效果复用。

    入参：`device` 是已 open/init 的官方 SDK 对象；`percent` 必须是 0 到 100 的 group 灯圈亮度。
    返回：成功时 None；SDK 缺少 API、返回非零 TransportResult 或调用异常时返回诊断文本。
    错误处理：不抛 SDK 异常，避免辅助灯光效果中断主图像刷新。
    副作用：成功时调用一次 `set_led_brightness(percent)`。
    """

    setter = getattr(device, "set_led_brightness", None)
    if not callable(setter):
        return "N4 Pro SDK does not expose set_led_brightness"
    try:
        result = setter(percent)
    except Exception as exc:
        return f"set_led_brightness failed: {type(exc).__name__}: {exc}"
    if streamdock_sdk_result_failed(result):
        return f"set_led_brightness failed: SDK returned {result}"
    return None


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
    if intent.intent == "open_url":
        url = intent.payload.get("url")
        return {
            "intent": intent.intent,
            "agent_key": intent.agent_key,
            "decision_id": intent.decision_id,
            "status": "dry_run",
            "target_available": url is not None,
            "target_type": "url",
            "url": url,
            "message": (
                f"open_url dry-run recorded for {url}"
                if url
                else "open_url dry-run recorded; missing url"
            ),
        }
    return {
        "intent": intent.intent,
        "agent_key": intent.agent_key,
        "decision_id": intent.decision_id,
        "status": "dry_run",
        "message": f"{intent.intent} dry-run recorded; no external action executed",
    }


def _unsupported_local_action(
    intent: InteractionIntent,
    *,
    message: str,
) -> dict[str, Any]:
    """返回已识别但当前产品不开放的本机动作诊断。

    入参：`intent` 是硬件输入归一化后的交互意图；`message` 是稳定诊断文本。
    返回：JSON-safe action 诊断，明确不会执行外部动作。
    错误处理：无。
    副作用：无。
    """

    return {
        "intent": intent.intent,
        "agent_key": intent.agent_key,
        "decision_id": intent.decision_id,
        "status": "unsupported",
        "ok": False,
        "message": message,
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


def _execute_local_url_action(
    intent: InteractionIntent,
    local_url_action_executor: LocalUrlActionExecutor,
) -> dict[str, Any]:
    """执行 URL quick-action 并返回诊断。

    入参：`intent` 是 `open_url` 交互意图；payload 中应包含 `url`；
    `local_url_action_executor` 是受配置保护的真实动作执行器。
    返回：JSON-safe action 诊断。
    错误处理：executor 自己负责把系统异常转换为 `LocalTargetActionResult`，本函数只序列化结果。
    副作用：生产 executor 会调用 macOS `open` 打开 http/https URL。
    """

    result = local_url_action_executor(url=intent.payload.get("url"))
    return {
        "intent": intent.intent,
        "agent_key": intent.agent_key,
        "decision_id": intent.decision_id,
        "status": result.status,
        "ok": result.ok,
        "target_type": result.target_type,
        "url": result.url,
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
