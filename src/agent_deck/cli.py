"""Agent Deck daemon、控制命令和 Codex hook 的命令行入口。

本模块只负责 CLI 参数解析、JSON/stdin 处理、本地 daemon HTTP 调用、Codex 检测/安装、
Codex App 本地状态扫描命令分派、真实 N4 Pro 预览命令，以及打包后的 console scripts
启动 uvicorn。它不持久化 daemon 状态、不实现 broker 业务逻辑；默认命令不修改用户文件，
只有用户显式运行 `codex-install --apply` 时才会写 Codex 配置。网络副作用限于用户显式执行
命令时访问配置的本地 daemon URL，并使用有界 httpx timeout；daemon 默认会按本地配置启用
真实硬件渲染，用户可用 `--disable-hardware-renderer` 临时关闭。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
import uvicorn

from agent_deck import __version__
from agent_deck.config import (
    AgentDeckConfigError,
    PermissionRequestMode,
    load_agent_deck_config,
    resolve_agent_deck_config_path,
    resolve_hardware_renderer_defaults,
)
from agent_deck.adapters.codex_app_state import (
    CodexAppActiveSession,
    build_codex_app_state_events_from_report,
    scan_codex_app_state,
    select_active_codex_app_sessions,
)
from agent_deck.adapters.codex_discovery import (
    build_codex_detection_report,
    install_codex_integration,
    validate_codex_managed_system_integration,
)
from agent_deck.adapters.codex_quota import read_codex_quota
from agent_deck.core.decisions import DecisionBehavior
from agent_deck.core.events import AgentSource, EventType, NormalizedEvent
from agent_deck.hardware.streamdock_n4pro import animate_key_images_on_n4pro
from agent_deck.hosts.codex import CodexHostResolver
from agent_deck.hosts.models import AgentHostContext
from agent_deck.rendering.asset_builder import build_codex_visual_assets
from agent_deck.rendering.codex_key_frames import (
    codex_key_frame_paths_for_variants,
)
from agent_deck.rendering.quota_touchscreen import render_quota_touchscreen
from agent_deck.rendering.visuals import resolve_visual_icon_spec
from agent_deck.server.app import DaemonPollerConfig, create_app

DEFAULT_DAEMON_URL = "http://127.0.0.1:8765"
_DEFAULT_HTTP_TIMEOUT_SECONDS = 5.0

#: Typer app for the local daemon entry point. The callback starts uvicorn when
#: invoked without a subcommand; importing the app has no network or hardware
#: side effects.
daemon_app = typer.Typer(
    help="Run the local Agent Deck daemon.",
    no_args_is_help=False,
)

#: Typer app for operator control commands. Commands contact the configured
#: daemon URL only when explicitly invoked.
ctl_app = typer.Typer(
    help="Control a running Agent Deck daemon.",
    no_args_is_help=True,
)

#: Typer app for Codex hook helper commands. It reads hook payloads from stdin
#: but never installs hooks or edits user configuration.
codex_hook_app = typer.Typer(
    help="Agent Deck Codex hook helper.",
    no_args_is_help=True,
)


@daemon_app.callback(invoke_without_command=True)
def daemon_callback(
    ctx: typer.Context,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Host interface for the local daemon listener.",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            help="TCP port for the local daemon listener.",
            min=1,
            max=65535,
        ),
    ] = 8765,
    disable_codex_app_state_poller: Annotated[
        bool,
        typer.Option(
            "--disable-codex-app-state-poller",
            help="Disable daemon polling of Codex App local state/rollouts.",
        ),
    ] = False,
    codex_app_state_poll_interval_seconds: Annotated[
        float,
        typer.Option(
            "--codex-app-state-poll-interval-seconds",
            help="Seconds between Codex App local state polls.",
            min=0.1,
        ),
    ] = 5.0,
    codex_app_state_scan_limit: Annotated[
        int,
        typer.Option(
            "--codex-app-state-scan-limit",
            help="Maximum recent Codex App threads to scan per state poll.",
            min=1,
        ),
    ] = 80,
    codex_app_active_window_seconds: Annotated[
        int,
        typer.Option(
            "--codex-app-active-window-seconds",
            help="Only show unarchived Codex App threads updated within this window.",
            min=1,
        ),
    ] = 3600,
    codex_app_active_session_limit: Annotated[
        int,
        typer.Option(
            "--codex-app-active-session-limit",
            help="Maximum active Codex App sessions to keep in daemon state.",
            min=1,
            max=10,
        ),
    ] = 10,
    disable_codex_quota_poller: Annotated[
        bool,
        typer.Option(
            "--disable-codex-quota-poller",
            help="Disable daemon polling of Codex app-server quota.",
        ),
    ] = False,
    disable_codex_token_usage_poller: Annotated[
        bool,
        typer.Option(
            "--disable-codex-token-usage-poller",
            help="Disable daemon polling of Codex token usage via ccusage.",
        ),
    ] = False,
    codex_quota_poll_interval_seconds: Annotated[
        float,
        typer.Option(
            "--codex-quota-poll-interval-seconds",
            help="Seconds between Codex quota polls; default is five minutes.",
            min=1.0,
        ),
    ] = 300.0,
    codex_token_usage_poll_interval_seconds: Annotated[
        float,
        typer.Option(
            "--codex-token-usage-poll-interval-seconds",
            help="Seconds between Codex token usage polls; default is five minutes.",
            min=1.0,
        ),
    ] = 300.0,
    codex_quota_timeout_seconds: Annotated[
        float,
        typer.Option(
            "--codex-quota-timeout-seconds",
            help="Seconds to wait for each Codex app-server quota response.",
            min=1.0,
        ),
    ] = 10.0,
    disable_streamdock_quota_touchscreen: Annotated[
        bool,
        typer.Option(
            "--disable-streamdock-quota-touchscreen",
            help="Disable sending rendered quota panel images to StreamDock hardware.",
        ),
    ] = False,
    config_path: Annotated[
        Path,
        typer.Option(
            "--config",
            help="Agent Deck TOML config path for daemon defaults.",
        ),
    ] = Path("agent-deck.toml"),
    disable_hardware_renderer: Annotated[
        bool,
        typer.Option(
            "--disable-hardware-renderer",
            help="Disable real hardware rendering; daemon still updates in-memory/fake layout.",
        ),
    ] = False,
    device_profile: Annotated[
        str | None,
        typer.Option(
            "--device-profile",
            help="Override hardware device profile from config; default config uses n4pro.",
        ),
    ] = None,
    render_interval_seconds: Annotated[
        float | None,
        typer.Option(
            "--render-interval-seconds",
            help="Override real hardware render interval from config.",
            min=0.5,
        ),
    ] = None,
    renderer_fps: Annotated[
        int | None,
        typer.Option(
            "--renderer-fps",
            help="Override real hardware button animation FPS from config.",
            min=1,
            max=20,
        ),
    ] = None,
) -> None:
    """Start the local daemon when no daemon subcommand is selected.

    入参：`ctx` 是 Typer/Click 当前命令上下文，用于判断是否已有子命令；`host`
    是 uvicorn 监听地址，默认 `127.0.0.1`；`port` 是监听 TCP 端口，范围 1-65535，
    默认 `8765`；`disable_codex_app_state_poller` 和
    `disable_codex_quota_poller` 可关闭默认 Codex pollers；状态 scan limit、active window
    和 active session limit 控制 Codex App 最近有效会话筛选；两个 interval 控制状态扫描和
    quota 刷新周期；`codex_quota_timeout_seconds` 控制单次 quota app-server 读取超时；
    `disable_codex_token_usage_poller` 可关闭基于 ccusage 的 Codex token usage 读取；
    `codex_token_usage_poll_interval_seconds` 控制 token usage 刷新周期；
    `disable_streamdock_quota_touchscreen` 可关闭旧 quota-only 真实硬件触屏下发；
    `config_path` 指向 daemon 默认配置；`disable_hardware_renderer` 可关闭默认真实硬件渲染；
    `device_profile`、`render_interval_seconds` 和 `renderer_fps` 是面向临时调试的通用覆盖项，
    未传时沿用配置文件，当前默认设备 profile 为 `n4pro`。
    返回：无显式返回值；`uvicorn.run` 负责阻塞运行 ASGI app。
    错误处理：Typer 处理 CLI 参数错误，包括非法端口、poll interval 或 timeout 范围；
    `create_app` 或 `uvicorn.run` 抛出的异常会向上传播并使命令失败。
    副作用：当没有子命令时创建 FastAPI app 并启动 uvicorn；默认启用 Codex App 本地状态
    只读 poller、quota poller 和真实硬件渲染；quota poller 会周期性启动短生命周期 Codex
    app-server；默认由统一硬件 renderer 接管背景和按钮，旧 quota-only sink 不再下发；
    若需要纯内存/fake 运行，可用 `--disable-hardware-renderer`；
    不写用户配置、不安装 Codex hooks。
    """

    if ctx.invoked_subcommand is not None:
        return
    try:
        local_config = load_agent_deck_config(config_path)
        hardware_renderer = resolve_hardware_renderer_defaults(
            local_config,
            disabled=disable_hardware_renderer,
            device_profile=device_profile,
            render_interval_seconds=render_interval_seconds,
            fps=renderer_fps,
        )
    except (AgentDeckConfigError, ValueError) as exc:
        typer.echo(f"agent-deckd: {exc}", err=True)
        raise typer.Exit(2) from exc
    if hardware_renderer.device_profile != "n4pro" and hardware_renderer.enabled:
        typer.echo(
            "agent-deckd: 当前真实硬件 renderer 只支持 device_profile=n4pro",
            err=True,
        )
        raise typer.Exit(2)
    poller_config = DaemonPollerConfig(
        codex_app_state_enabled=not disable_codex_app_state_poller,
        codex_app_state_interval_seconds=codex_app_state_poll_interval_seconds,
        codex_app_state_scan_limit=codex_app_state_scan_limit,
        codex_app_active_window_seconds=codex_app_active_window_seconds,
        codex_app_active_session_limit=codex_app_active_session_limit,
        codex_quota_enabled=not disable_codex_quota_poller,
        codex_quota_interval_seconds=codex_quota_poll_interval_seconds,
        codex_quota_timeout_seconds=codex_quota_timeout_seconds,
        codex_token_usage_enabled=not disable_codex_token_usage_poller,
        codex_token_usage_interval_seconds=codex_token_usage_poll_interval_seconds,
        streamdock_quota_touchscreen_enabled=(
            not disable_streamdock_quota_touchscreen
            and not hardware_renderer.enabled
        ),
        streamdock_quota_device=hardware_renderer.device_profile,
        streamdock_n4pro_renderer_enabled=hardware_renderer.enabled,
        streamdock_n4pro_render_interval_seconds=(
            hardware_renderer.render_interval_seconds
        ),
        streamdock_n4pro_renderer_fps=hardware_renderer.fps,
        streamdock_n4pro_frame_root=hardware_renderer.frame_root,
    )
    uvicorn.run(create_app(poller_config=poller_config), host=host, port=port)


@ctl_app.callback()
def ctl_callback() -> None:
    """Provide the Agent Deck control command group.

    入参：无；子命令各自接收 daemon URL 或业务参数。
    返回：无返回值；Typer 负责帮助信息和子命令分派。
    错误处理：本 callback 不主动抛业务异常；命令行解析错误由 Typer 处理。
    副作用：无；不连接 daemon、不读写文件、不修改全局状态。
    """


@ctl_app.command()
def version() -> None:
    """Print the Agent Deck package version.

    入参：无；版本号来自 `agent_deck.__version__`，不读取环境或配置。
    返回：无返回值；版本文本通过标准输出交给 Typer/Click 处理。
    错误处理：本函数不主动抛出业务异常；标准输出失败等底层错误由运行时传播。
    副作用：仅向标准输出写入一行版本号，不访问网络、硬件或文件系统。
    """

    typer.echo(__version__)


@ctl_app.command()
def status(
    daemon_url: Annotated[
        str,
        typer.Option(
            "--daemon-url",
            help="Base URL for the local Agent Deck daemon.",
        ),
    ] = DEFAULT_DAEMON_URL,
) -> None:
    """Fetch daemon status and print formatted JSON.

    入参：`daemon_url` 是 daemon base URL，默认 `DEFAULT_DAEMON_URL`；命令会请求
    `{daemon_url}/status`。
    返回：无显式返回值；成功时将 daemon JSON 以缩进格式写入 stdout。
    错误处理：daemon 不可达、HTTP 非 2xx 或 JSON 解码失败时写 stderr 并以 exit 1 退出。
    副作用：使用 `httpx.Client(timeout=...)` 发起一次本地 HTTP GET；不访问硬件或文件。
    """

    try:
        payload = _http_get_json(_join_url(daemon_url, "status"))
    except (httpx.HTTPError, ValueError) as exc:
        _fail_http_command("status", exc)
    _echo_json(payload)


@ctl_app.command()
def simulate(
    session_id: Annotated[
        str,
        typer.Option(
            "--session-id",
            help="Session id to attach to the simulated Codex event.",
        ),
    ] = "demo",
    event_type: Annotated[
        str,
        typer.Option(
            "--event-type",
            help="Normalized event type to simulate.",
        ),
    ] = EventType.SESSION_STARTED.value,
    daemon_url: Annotated[
        str,
        typer.Option(
            "--daemon-url",
            help="Base URL for the local Agent Deck daemon.",
        ),
    ] = DEFAULT_DAEMON_URL,
) -> None:
    """Post a synthetic Codex normalized event to the daemon.

    入参：`session_id` 是 event 的 Codex session id，默认 `demo`；`event_type` 必须是
    `EventType` 支持的 normalized type 字符串；`daemon_url` 是 daemon base URL。
    返回：无显式返回值；成功时输出 `sent ...` 摘要。
    错误处理：非法 event type 或模型校验失败由 Typer 以 exit 2 报告；daemon 不可达、
    HTTP 非 2xx 或 JSON 解码失败时写 stderr 并以 exit 1 退出。
    副作用：构造内存中的 `NormalizedEvent`，并用 bounded httpx POST 到 `/events`。
    """

    normalized_type = _parse_event_type(event_type)
    event = NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type=event_type,
        normalized_type=normalized_type,
        session_id=session_id,
        occurred_at=datetime.now(UTC),
        title=session_id,
        payload={"simulated": True},
    )
    try:
        _http_post_json(
            _join_url(daemon_url, "events"),
            event.model_dump(mode="json"),
        )
    except (httpx.HTTPError, ValueError) as exc:
        _fail_http_command("simulate", exc)
    typer.echo(f"sent {normalized_type.value} for {session_id}")


@ctl_app.command()
def resolve(
    decision_id: Annotated[str, typer.Argument(help="Decision id to resolve.")],
    behavior: Annotated[
        str,
        typer.Argument(help="Decision behavior: allow or deny."),
    ],
    daemon_url: Annotated[
        str,
        typer.Option(
            "--daemon-url",
            help="Base URL for the local Agent Deck daemon.",
        ),
    ] = DEFAULT_DAEMON_URL,
) -> None:
    """Resolve a pending daemon decision and print formatted JSON.

    入参：`decision_id` 是 daemon broker 返回的 decision id；`behavior` 必须是 `allow`
    或 `deny`；`daemon_url` 是 daemon base URL。
    返回：无显式返回值；成功时将 resolve endpoint 的 JSON 响应写入 stdout。
    错误处理：非法 behavior 写 stderr 并以 exit 2 退出；daemon 不可达、HTTP 非 2xx 或
    JSON 解码失败时写 stderr 并以 exit 1 退出。
    副作用：使用 bounded httpx POST 到 `/decisions/{decision_id}/resolve`；不修改本地文件。
    """

    decision_behavior = _parse_decision_behavior(behavior)
    try:
        payload = _http_post_json(
            _join_url(daemon_url, "decisions", decision_id, "resolve"),
            {"behavior": decision_behavior.value, "message": ""},
        )
    except (httpx.HTTPError, ValueError) as exc:
        _fail_http_command("resolve", exc)
    _echo_json(payload)


@ctl_app.command("generate-codex-assets")
def generate_codex_assets(
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="Directory for generated Codex visual frames and preview.",
        ),
    ],
    source_gif: Annotated[
        Path,
        typer.Option(
            "--source-gif",
            help="Source animated Codex GIF.",
        ),
    ] = Path("assets/codex/codex.gif"),
    source_png: Annotated[
        Path,
        typer.Option(
            "--source-png",
            help="Source static Codex PNG for offline state.",
        ),
    ] = Path("assets/codex/codex.png"),
    key_width: Annotated[
        int,
        typer.Option(
            "--key-width",
            help="Generated key frame width in pixels.",
            min=1,
        ),
    ] = 112,
    key_height: Annotated[
        int,
        typer.Option(
            "--key-height",
            help="Generated key frame height in pixels.",
            min=1,
        ),
    ] = 112,
    target_fps: Annotated[
        int,
        typer.Option(
            "--target-fps",
            help="Target animation FPS for resampling source GIFs.",
            min=1,
        ),
    ] = 10,
    max_duration_ms: Annotated[
        int,
        typer.Option(
            "--max-duration-ms",
            help="Maximum source GIF duration to sample in milliseconds.",
            min=1,
        ),
    ] = 5000,
    max_frames: Annotated[
        int | None,
        typer.Option(
            "--max-frames",
            help="Optional hard cap for generated frames per animated variant.",
            min=1,
        ),
    ] = None,
) -> None:
    """Generate local Codex visual asset frames and previews.

    入参：`output_dir` 是生成目录；`source_gif` 是非 offline 状态的源 GIF；
    `source_png` 是 offline 状态的源 PNG；`key_width`/`key_height` 是输出帧尺寸；
    `target_fps` 是按时间轴重采样的目标帧率；`max_duration_ms` 是参与采样的最长源动画时长；
    `max_frames` 是可选的每个动画变体帧数硬上限。
    返回：无显式返回值；成功时以 JSON 输出生成目录、预览图路径、manifest、帧尺寸和变体帧数。
    错误处理：源资产缺失、图片解码失败、输出目录不可写或参数非法时写 stderr 并 exit 1；
    Typer 负责命令行参数类型和范围错误。
    副作用：读取源 GIF/PNG，写入输出目录下的 PNG 帧、每状态 `preview.gif`、`preview.png`
    和 `manifest.json`；不访问 daemon、不连接硬件、不修改 Codex 配置。
    """

    try:
        result = build_codex_visual_assets(
            source_gif=source_gif,
            source_png=source_png,
            output_dir=output_dir,
            key_size=(key_width, key_height),
            target_fps=target_fps,
            max_duration_ms=max_duration_ms,
            max_frames=max_frames,
        )
    except Exception as exc:
        typer.echo(f"agent-deckctl generate-codex-assets: {exc}", err=True)
        raise typer.Exit(1) from exc
    _echo_json(
        {
            "output_dir": str(result.output_dir),
            "preview_path": str(result.preview_path),
            "manifest_path": str(result.manifest_path),
            "preview_gif_paths": {
                variant: str(path) for variant, path in result.preview_gif_paths.items()
            },
            "frame_size": list(result.frame_size),
            "variant_frame_counts": result.variant_frame_counts,
        }
    )


@ctl_app.command("codex-quota")
def codex_quota(
    timeout_seconds: Annotated[
        float,
        typer.Option(
            "--timeout-seconds",
            help="Seconds to wait for Codex app-server quota responses.",
            min=1,
        ),
    ] = 10.0,
) -> None:
    """Read Codex app-server quota information and print JSON.

    入参：`timeout_seconds` 是等待 Codex app-server 初始化和 quota 响应的超时时间。
    返回：无显式返回值；成功时将 `CodexQuotaSnapshot` 以 JSON 输出。
    错误处理：Codex CLI 不存在、app-server 超时、JSON-RPC 错误或解析失败时写 stderr
    并以 exit 1 退出。
    副作用：启动短生命周期 `codex -s read-only -a untrusted app-server` 子进程；
    不访问 StreamDock 硬件、不修改 Codex 配置、不连接 daemon。
    """

    try:
        snapshot = read_codex_quota(timeout_seconds=timeout_seconds)
    except Exception as exc:
        typer.echo(f"agent-deckctl codex-quota: {exc}", err=True)
        raise typer.Exit(1) from exc
    _echo_json(snapshot.model_dump(mode="json"))


@ctl_app.command("codex-app-state")
def codex_app_state(
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help="Optional CODEX_HOME override for Codex App local state scanning.",
        ),
    ] = None,
    state_db_path: Annotated[
        Path | None,
        typer.Option(
            "--state-db-path",
            help="Optional explicit Codex App state_*.sqlite path.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help="Maximum recent Codex App threads to scan.",
            min=1,
        ),
    ] = 20,
    sync: Annotated[
        bool,
        typer.Option(
            "--sync",
            help="Post detected input.requested events to the Agent Deck daemon.",
        ),
    ] = False,
    daemon_url: Annotated[
        str,
        typer.Option(
            "--daemon-url",
            help="Base URL for the local Agent Deck daemon when --sync is used.",
        ),
    ] = DEFAULT_DAEMON_URL,
) -> None:
    """Scan Codex App local state and optionally sync pending user inputs.

    入参：`codex_home` 可覆盖 `~/.codex`；`state_db_path` 可直接指定 state SQLite；
    `limit` 控制读取最近 thread 数量；`sync` 为 False 时只输出扫描报告，为 True 时把
    待响应 `request_user_input` 转成 `input.requested` 并 POST 到 daemon；`daemon_url`
    仅在 sync 时使用。
    返回：无显式返回值；stdout 输出 report JSON，sync 时额外包含 `synced_events`。
    错误处理：扫描失败、SQLite/JSONL 读取失败、daemon 不可达或响应非法时写 stderr 并
    以 exit 1 退出；参数范围错误由 Typer 以 exit 2 处理。
    副作用：只读访问 Codex App 本地状态文件；仅在 `--sync` 时使用 bounded httpx POST
    到 `/events`，不写 Codex 配置、不操作 App UI、不修改 SQLite/JSONL。
    """

    try:
        report = scan_codex_app_state(
            codex_home=codex_home,
            state_db_path=state_db_path,
            limit=limit,
        )
        if not sync:
            _echo_json(report.model_dump(mode="json"))
            return

        events = build_codex_app_state_events_from_report(report)
        for event in events:
            _http_post_json(
                _join_url(daemon_url, "events"),
                event.model_dump(mode="json"),
            )
    except (httpx.HTTPError, ValueError, OSError, sqlite3.Error) as exc:
        typer.echo(f"agent-deckctl codex-app-state: {exc}", err=True)
        raise typer.Exit(1) from exc

    _echo_json(
        {
            "synced_events": len(events),
            "report": report.model_dump(mode="json"),
        }
    )


@ctl_app.command("codex-hosts")
def codex_hosts(
    agent_pid: Annotated[
        int | None,
        typer.Option(
            "--agent-pid",
            help="Optional Codex CLI pid to inspect.",
        ),
    ] = None,
    include_codex_app: Annotated[
        bool,
        typer.Option(
            "--include-codex-app/--no-include-codex-app",
            help="Include Codex App local state sessions.",
        ),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print machine-readable JSON.",
        ),
    ] = False,
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help="Optional CODEX_HOME override for Codex App local state scanning.",
        ),
    ] = None,
    state_db_path: Annotated[
        Path | None,
        typer.Option(
            "--state-db-path",
            help="Optional explicit Codex App state_*.sqlite path.",
        ),
    ] = None,
    scan_limit: Annotated[
        int,
        typer.Option(
            "--scan-limit",
            help="Maximum recent Codex App threads to scan before filtering.",
            min=1,
        ),
    ] = 80,
    active_window_seconds: Annotated[
        int,
        typer.Option(
            "--active-window-seconds",
            help="Only include Codex App threads updated within this window.",
            min=1,
        ),
    ] = 3600,
    active_session_limit: Annotated[
        int,
        typer.Option(
            "--active-session-limit",
            help="Maximum Codex App active sessions to include.",
            min=1,
            max=10,
        ),
    ] = 10,
) -> None:
    """Print read-only Codex CLI/App host context.

    入参：`agent_pid` 可指定一个 Codex CLI pid；`include_codex_app` 控制是否扫描 Codex App
    本地状态；`json_output` 控制 JSON 或人类可读输出；`codex_home`、`state_db_path`、
    `scan_limit`、`active_window_seconds` 和 `active_session_limit` 只影响 Codex App 只读扫描。
    返回：无显式返回值；stdout 输出 session host contexts。
    错误处理：CLI pid 解析内部降级为 unknown；Codex App 扫描失败时 JSON 包含
    `codex_app_error` 并继续输出已有 sessions。
    副作用：只读读取进程表、tmux 状态和 Codex App 本地状态；不写配置、不 attach tmux、
    不启动终端、不执行 focus。
    """

    resolver = _build_codex_host_resolver()
    sessions: list[AgentHostContext] = []
    codex_app_error: str | None = None

    if agent_pid is not None:
        sessions.append(resolver.resolve_cli(agent_pid=agent_pid))

    if include_codex_app:
        try:
            report = scan_codex_app_state(
                codex_home=codex_home,
                state_db_path=state_db_path,
                limit=scan_limit,
            )
            active_sessions = select_active_codex_app_sessions(
                report,
                active_window_seconds=active_window_seconds,
                max_sessions=active_session_limit,
            )
            sessions.extend(resolver.resolve_app_sessions(active_sessions))
        except (OSError, ValueError, sqlite3.Error) as exc:
            codex_app_error = str(exc)

    payload: dict[str, Any] = {
        "sessions": [session.model_dump(mode="json") for session in sessions],
    }
    if codex_app_error is not None:
        payload["codex_app_error"] = codex_app_error

    if json_output:
        _echo_json(payload)
        return

    for session in sessions:
        typer.echo(_format_host_context_line(session))
    if codex_app_error is not None:
        typer.echo(f"Codex App scan error: {codex_app_error}", err=True)


@ctl_app.command("codex-sessions-preview")
def codex_sessions_preview(
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help="Optional CODEX_HOME override for Codex App local state scanning.",
        ),
    ] = None,
    state_db_path: Annotated[
        Path | None,
        typer.Option(
            "--state-db-path",
            help="Optional explicit Codex App state_*.sqlite path.",
        ),
    ] = None,
    scan_limit: Annotated[
        int,
        typer.Option(
            "--scan-limit",
            help="Maximum recent Codex App threads to scan before filtering.",
            min=1,
        ),
    ] = 80,
    active_window_seconds: Annotated[
        int,
        typer.Option(
            "--active-window-seconds",
            help="Only show unarchived Codex App threads updated within this window.",
            min=1,
        ),
    ] = 3600,
    max_sessions: Annotated[
        int,
        typer.Option(
            "--max-sessions",
            help="Maximum sessions to render onto N4 Pro buttons.",
            min=1,
            max=10,
        ),
    ] = 10,
    frame_root: Annotated[
        Path,
        typer.Option(
            "--frame-root",
            help="Generated Codex N4 Pro key frame directory.",
        ),
    ] = Path("assets/codex/generated/n4pro-key-112-fps10"),
    duration_seconds: Annotated[
        float,
        typer.Option(
            "--duration-seconds",
            help="How long to play the real-device preview animation.",
            min=0.1,
        ),
    ] = 30.0,
    fps: Annotated[
        int,
        typer.Option(
            "--fps",
            help="Target N4 Pro button animation refresh rate.",
            min=1,
            max=20,
        ),
    ] = 10,
    quota_timeout_seconds: Annotated[
        float,
        typer.Option(
            "--quota-timeout-seconds",
            help="Seconds to wait for Codex app-server quota responses.",
            min=1,
        ),
    ] = 10.0,
) -> None:
    """把最近有效 Codex 会话状态和 quota 一起预览到真实 N4 Pro。

    入参：`codex_home`/`state_db_path`/`scan_limit` 控制 Codex App 本地扫描；
    `active_window_seconds` 和 `max_sessions` 控制“有效会话”过滤；`frame_root` 指向
    `generate-codex-assets` 产出的按键帧目录；`duration_seconds`/`fps` 控制真机动画时长；
    `quota_timeout_seconds` 控制 quota app-server 读取超时。
    返回：无显式返回值；成功时输出本次扫描、筛选、渲染和硬件下发结果 JSON。
    错误处理：扫描、quota、帧目录或硬件写入失败时写 stderr 并 exit 1；参数范围错误由 Typer
    以 exit 2 处理。
    副作用：只读访问 Codex App 本地状态和 rollout；启动短生命周期 Codex app-server 读取
    quota；接管真实 N4 Pro 一次 open/init，在底部背景显示 quota，并循环写主按键动画。
    """

    try:
        report = scan_codex_app_state(
            codex_home=codex_home,
            state_db_path=state_db_path,
            limit=scan_limit,
        )
        sessions = select_active_codex_app_sessions(
            report,
            active_window_seconds=active_window_seconds,
            max_sessions=max_sessions,
        )
        quota_snapshot = read_codex_quota(timeout_seconds=quota_timeout_seconds)
        background = render_quota_touchscreen(quota_snapshot)
        key_frame_paths = _codex_session_key_frame_paths(
            frame_root=frame_root,
            sessions=sessions,
        )
        render_result = animate_key_images_on_n4pro(
            background_image=background,
            key_frame_paths=key_frame_paths,
            duration_seconds=duration_seconds,
            fps=fps,
        )
    except Exception as exc:
        typer.echo(f"agent-deckctl codex-sessions-preview: {exc}", err=True)
        raise typer.Exit(1) from exc

    payload = {
        "active_window_seconds": active_window_seconds,
        "max_sessions": max_sessions,
        "sessions": [session.model_dump(mode="json") for session in sessions],
        "key_count": len(key_frame_paths),
        "quota": quota_snapshot.model_dump(mode="json"),
        "render": render_result.model_dump(mode="json"),
    }
    if not render_result.ok:
        _echo_json(payload)
        raise typer.Exit(1)
    _echo_json(payload)


def _codex_session_key_frame_paths(
    *,
    frame_root: Path,
    sessions: tuple[CodexAppActiveSession, ...],
) -> dict[int, tuple[Path, ...]]:
    """把活动 Codex 会话映射成 N4 Pro 物理按钮编号和本地动画帧路径。

    入参：`frame_root` 是生成资产根目录；`sessions` 是已过滤并排序的活动会话列表。
    返回：dict，key 为 N4 Pro 物理按钮编号 1..10，value 为该状态对应的 PNG 帧路径元组。
    错误处理：frame root、状态子目录、offline 静态图或任何帧文件缺失时抛 `FileNotFoundError`。
    副作用：只读取文件系统元数据，不打开图片、不访问硬件。
    """

    variants = (
        resolve_visual_icon_spec(session.status).variant_id
        for session in sessions[:10]
    )
    return codex_key_frame_paths_for_variants(
        frame_root=frame_root,
        variants=variants,
        start_key=1,
        max_keys=10,
    )


@ctl_app.command("codex-detect")
def codex_detect(
    enable_integration: Annotated[
        bool,
        typer.Option(
            "--enable-integration",
            help="Include manual Codex hooks/notify integration guidance.",
        ),
    ] = False,
    daemon_url: Annotated[
        str,
        typer.Option(
            "--daemon-url",
            help="Base URL that generated Codex hook helpers should call.",
        ),
    ] = DEFAULT_DAEMON_URL,
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help="Optional CODEX_HOME override for read-only config path detection.",
        ),
    ] = None,
    app_path: Annotated[
        Path | None,
        typer.Option(
            "--app-path",
            help="Optional Codex.app bundle path override.",
        ),
    ] = None,
) -> None:
    """Print a read-only Codex detection report and optional integration guide.

    入参：`enable_integration` 控制是否附加 hooks/notify 手动接入说明；`daemon_url`
    用于生成 helper 命令；`codex_home` 可覆盖 `CODEX_HOME`；`app_path` 可覆盖 macOS
    Codex.app bundle 路径。
    返回：无显式返回值；成功时输出 JSON report。
    错误处理：路径解析或 report 构造失败时写 stderr 并以 exit 1 退出。
    副作用：只读查询 PATH、环境变量和少量路径存在性；不写 `~/.codex`，不启动 Codex，
    不连接 daemon 或硬件。
    """

    try:
        report = build_codex_detection_report(
            enable_integration=enable_integration,
            daemon_url=daemon_url,
            codex_home=codex_home,
            app_path=app_path,
        )
    except Exception as exc:
        typer.echo(f"agent-deckctl codex-detect: {exc}", err=True)
        raise typer.Exit(1) from exc
    _echo_json(report.model_dump(mode="json"))


@ctl_app.command("codex-install")
def codex_install(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Write Codex config changes after backing up existing files.",
        ),
    ] = False,
    daemon_url: Annotated[
        str,
        typer.Option(
            "--daemon-url",
            help="Base URL that generated Codex hook helpers should call.",
        ),
    ] = DEFAULT_DAEMON_URL,
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help="Optional CODEX_HOME override for config install.",
        ),
    ] = None,
    app_path: Annotated[
        Path | None,
        typer.Option(
            "--app-path",
            help="Optional Codex.app bundle path override for the embedded detection report.",
        ),
    ] = None,
    managed_system: Annotated[
        bool,
        typer.Option(
            "--managed-system",
            help="Install Codex lifecycle hooks as system managed requirements.",
        ),
    ] = False,
    system_requirements_path: Annotated[
        Path | None,
        typer.Option(
            "--system-requirements-path",
            help="Optional system requirements.toml path override for managed-system.",
        ),
    ] = None,
    managed_hooks_dir: Annotated[
        Path | None,
        typer.Option(
            "--managed-hooks-dir",
            help="Optional managed hook wrapper directory override for managed-system.",
        ),
    ] = None,
    validate_only: Annotated[
        bool,
        typer.Option(
            "--validate-only",
            help="Read-only validation for managed-system Codex integration.",
        ),
    ] = False,
) -> None:
    """Dry-run or apply Codex hooks/notify integration.

    入参：`apply` 默认 False，表示只输出安装计划；显式 `--apply` 才允许写入配置；
    `daemon_url` 用于生成 helper 命令；`codex_home` 可覆盖 `CODEX_HOME`；`app_path`
    可覆盖检测报告里的 Codex.app bundle 路径；`managed_system` 改用系统 requirements
    managed hooks；`system_requirements_path` 与 `managed_hooks_dir` 用于测试或自定义系统路径；
    `validate_only` 只读验证 managed-system 当前状态。
    返回：无显式返回值；成功时输出 JSON install result 或 validation result。
    错误处理：需要人工合并、路径解析、写入失败或非法选项组合时写 stderr 并以 exit 1 退出。
    副作用：dry-run 不写文件；`--apply` 可能创建或修改用户级 config/hooks，或在
    `--managed-system` 下写系统 requirements 与 wrapper，并在修改已有文件前创建备份；
    `--validate-only` 不写文件。
    """

    if validate_only and not managed_system:
        typer.echo(
            "agent-deckctl codex-install: --validate-only requires --managed-system",
            err=True,
        )
        raise typer.Exit(1)
    if validate_only and apply:
        typer.echo(
            "agent-deckctl codex-install: --validate-only cannot be combined with --apply",
            err=True,
        )
        raise typer.Exit(1)
    try:
        if validate_only:
            validation = validate_codex_managed_system_integration(
                daemon_url=daemon_url,
                codex_home=codex_home,
                app_path=app_path,
                system_requirements_path=system_requirements_path,
                managed_hooks_dir=managed_hooks_dir,
            )
            _echo_json(validation.model_dump(mode="json"))
            return
        result = install_codex_integration(
            apply=apply,
            daemon_url=daemon_url,
            codex_home=codex_home,
            app_path=app_path,
            mode="managed-system" if managed_system else "user",
            system_requirements_path=system_requirements_path,
            managed_hooks_dir=managed_hooks_dir,
        )
    except Exception as exc:
        typer.echo(f"agent-deckctl codex-install: {exc}", err=True)
        raise typer.Exit(1) from exc
    _echo_json(result.model_dump(mode="json"))


@codex_hook_app.callback()
def codex_hook_callback() -> None:
    """Provide the Codex hook helper command group.

    入参：无；`notify` 和 `permission-request` 子命令从 stdin 读取 JSON object。
    返回：无返回值；Typer 负责帮助信息和子命令分派。
    错误处理：本 callback 不主动抛业务异常；命令行解析错误由 Typer 处理。
    副作用：无；不安装 hook、不连接 daemon、不修改用户配置。
    """


@codex_hook_app.command()
def notify(
    payload_json: Annotated[
        str | None,
        typer.Argument(
            help="Optional Codex notify JSON argument; stdin is used when omitted.",
        ),
    ] = None,
    daemon_url: Annotated[
        str,
        typer.Option(
            "--daemon-url",
            help="Base URL for the local Agent Deck daemon.",
        ),
    ] = DEFAULT_DAEMON_URL,
) -> None:
    """Forward a Codex notify payload as a best-effort turn.completed event.

    入参：`payload_json` 是 Codex 官方 notify 传入的单个 JSON 参数，可省略并从 stdin
    读取以兼容测试和手工管道；`daemon_url` 是 daemon base URL。
    返回：无显式返回值；成功时不要求输出固定内容。
    错误处理：payload 为空、非法 JSON 或非 object 时以 exit 2 退出；daemon 不可达、
    HTTP 非 2xx 或 JSON 解码失败时写 stderr 但 exit 0。
    副作用：读取 argv 或 stdin，并可能用 bounded httpx POST 到 `/events`；不修改配置或文件。
    """

    payload = _read_json_object_from_text_or_stdin(payload_json)
    event = _event_from_hook_payload(
        payload,
        normalized_type=EventType.TURN_COMPLETED,
        default_source_event_type="notify",
    )
    try:
        _http_post_json(
            _join_url(daemon_url, "events"),
            event.model_dump(mode="json"),
        )
    except (httpx.HTTPError, ValueError) as exc:
        typer.echo(f"agent-deck-codex-hook notify: {exc}", err=True)


@codex_hook_app.command()
def event(
    daemon_url: Annotated[
        str,
        typer.Option(
            "--daemon-url",
            help="Base URL for the local Agent Deck daemon.",
        ),
    ] = DEFAULT_DAEMON_URL,
    agent_pid: Annotated[
        str | None,
        typer.Option(
            "--agent-pid",
            help="Optional Codex parent process id captured by the hook command.",
        ),
    ] = None,
) -> None:
    """Forward a generic Codex lifecycle hook payload as a normalized event.

    入参：`daemon_url` 是 daemon base URL；stdin 必须是非空 JSON object，`hookEventName`
    或同义字段会映射到 Agent Deck normalized event type；`agent_pid` 可由 hook command
    传入 `$PPID`，作为宿主识别线索保存在 payload 中。
    返回：无显式返回值；成功时不要求输出固定内容。
    错误处理：stdin 为空、非法 JSON 或非 object 时以 exit 2 退出；daemon 不可达、HTTP
    非 2xx 或 JSON 解码失败时写 stderr 但 exit 0，避免阻断 Codex 普通 lifecycle hooks。
    副作用：读取 stdin，并可能用 bounded httpx POST 到 `/events`；不修改配置或文件。
    """

    payload = _read_json_object_from_stdin()
    payload = _payload_with_agent_pid(payload, agent_pid)
    normalized_type = _normalized_event_type_from_codex_hook(payload)
    event = _event_from_hook_payload(
        payload,
        normalized_type=normalized_type,
        default_source_event_type="codex-hook",
    )
    try:
        _http_post_json(
            _join_url(daemon_url, "events"),
            event.model_dump(mode="json"),
        )
    except (httpx.HTTPError, ValueError) as exc:
        typer.echo(f"agent-deck-codex-hook event: {exc}", err=True)


@codex_hook_app.command("permission-request")
def permission_request(
    daemon_url: Annotated[
        str,
        typer.Option(
            "--daemon-url",
            help="Base URL for the local Agent Deck daemon.",
        ),
    ] = DEFAULT_DAEMON_URL,
    timeout_seconds: Annotated[
        float | None,
        typer.Option(
            "--timeout-seconds",
            help="Legacy fallback seconds for Agent Deck permission decisions.",
            min=0.001,
        ),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Agent Deck TOML config path; defaults to AGENT_DECK_CONFIG, cwd, or user config.",
        ),
    ] = None,
    agent_pid: Annotated[
        str | None,
        typer.Option(
            "--agent-pid",
            help="Optional Codex parent process id captured by the hook command.",
        ),
    ] = None,
) -> None:
    """Handle a Codex permission request according to Agent Deck config.

    入参：`daemon_url` 是 daemon base URL；`timeout_seconds` 是旧安装命令可能传入的等待秒数，
    配置文件存在时以 `codex.permission_request.timeout_seconds` 为准；`config_path` 是可选
    Agent Deck TOML 配置路径；`agent_pid` 可由 hook command 传入 `$PPID` 作为宿主识别
    线索；stdin 必须是非空 JSON object。
    返回：无显式返回值；`passthrough` 模式 stdout 为空，Codex 会继续原生审批；`handle`
    和 `deny` 模式会输出 Codex hook JSON decision payload。
    错误处理：stdin 为空、非法 JSON 或非 object 时以 exit 2 退出；daemon 不可达、HTTP
    非 2xx、缺少 decision id 或 JSON 解码失败时写 stderr；handle 模式 fail-closed 输出 deny，
    passthrough 模式不输出 decision。配置读取失败时写 stderr 并 passthrough。
    副作用：读取 stdin 和可选配置文件；handle 模式可能用 bounded httpx POST
    `/decisions/request` 后 GET `/decisions/{id}/wait`；不安装 hook、不写用户配置。
    """

    payload = _read_json_object_from_stdin()
    payload = _payload_with_agent_pid(payload, agent_pid)
    try:
        local_config = load_agent_deck_config(
            resolve_agent_deck_config_path(config_path)
        )
    except AgentDeckConfigError as exc:
        typer.echo(f"agent-deck-codex-hook permission-request: {exc}", err=True)
        return
    permission_config = local_config.codex.permission_request
    if permission_config.mode == PermissionRequestMode.PASSTHROUGH:
        return
    if permission_config.mode == PermissionRequestMode.DENY:
        _echo_json(
            _codex_permission_output(
                DecisionBehavior.DENY.value,
                permission_config.deny_message,
            )
        )
        return
    effective_timeout_seconds = permission_config.timeout_seconds
    request_body = _decision_request_from_hook_payload(
        payload,
        effective_timeout_seconds,
    )
    try:
        request_payload = _http_post_json(
            _join_url(daemon_url, "decisions", "request"),
            request_body,
            timeout=max(_DEFAULT_HTTP_TIMEOUT_SECONDS, effective_timeout_seconds + 1),
        )
        decision_id = request_payload.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            raise ValueError("daemon response missing decision_id")
        result = _http_get_json(
            _join_url(daemon_url, "decisions", decision_id, "wait"),
            params={"timeout_seconds": effective_timeout_seconds},
            timeout=max(_DEFAULT_HTTP_TIMEOUT_SECONDS, effective_timeout_seconds + 1),
        )
        behavior = _decision_behavior_from_daemon(result.get("behavior"))
        message = result.get("message", "")
        if not isinstance(message, str):
            message = ""
        _echo_json(_codex_permission_output(behavior.value, message))
    except (httpx.HTTPError, ValueError) as exc:
        typer.echo(f"agent-deck-codex-hook permission-request: {exc}", err=True)
        _echo_json(
            _codex_permission_output(
                DecisionBehavior.DENY.value,
                f"Agent Deck daemon unavailable: {exc}",
            )
        )


def _http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Perform a bounded GET request and return a JSON object.

    入参：`url` 是完整请求 URL；`params` 是可选 query 参数；`timeout` 是 httpx client
    超时秒数。
    返回：响应 JSON object。
    错误处理：网络错误、超时或非 2xx 由 httpx 异常报告；响应 JSON 不是 object 时抛
    ValueError。
    副作用：通过 `httpx.Client(timeout=timeout)` 发起一次 HTTP GET。
    """

    with httpx.Client(timeout=timeout) as client:
        response = client.get(url, params=params or None)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("daemon response must be a JSON object")
    return payload


def _http_post_json(
    url: str,
    body: dict[str, Any],
    *,
    timeout: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Perform a bounded POST request with JSON body and return JSON object.

    入参：`url` 是完整请求 URL；`body` 是 JSON object 请求体；`timeout` 是 httpx client
    超时秒数。
    返回：响应 JSON object。
    错误处理：网络错误、超时或非 2xx 由 httpx 异常报告；响应 JSON 不是 object 时抛
    ValueError。
    副作用：通过 `httpx.Client(timeout=timeout)` 发起一次 HTTP POST。
    """

    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, json=body)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("daemon response must be a JSON object")
    return payload


def _read_json_object_from_stdin() -> dict[str, Any]:
    """Read stdin as a non-empty JSON object.

    入参：无；从 `sys.stdin` 读取完整文本。
    返回：解析后的 dict。
    错误处理：stdin 为空、JSON 非法或顶层不是 object 时写 stderr 并以 exit 2 退出。
    副作用：消耗当前进程 stdin；不访问网络、硬件或文件。
    """

    raw = sys.stdin.read()
    if not raw.strip():
        typer.echo("stdin must contain a JSON object", err=True)
        raise typer.Exit(2)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"stdin must contain valid JSON: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not isinstance(payload, dict):
        typer.echo("stdin JSON must be an object", err=True)
        raise typer.Exit(2)
    return payload


def _read_json_object_from_text_or_stdin(raw: str | None) -> dict[str, Any]:
    """Read a JSON object from an optional string or stdin.

    入参：`raw` 是可选 JSON 字符串；为 None 时读取当前进程 stdin。
    返回：解析后的 dict。
    错误处理：内容为空、JSON 非法或顶层不是 object 时写 stderr 并以 exit 2 退出。
    副作用：当 raw 为 None 时消耗 stdin；不访问网络、硬件或文件。
    """

    if raw is None:
        return _read_json_object_from_stdin()
    if not raw.strip():
        typer.echo("payload must contain a JSON object", err=True)
        raise typer.Exit(2)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"payload must contain valid JSON: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not isinstance(payload, dict):
        typer.echo("payload JSON must be an object", err=True)
        raise typer.Exit(2)
    return payload


def _build_codex_host_resolver() -> CodexHostResolver:
    """构建默认 Codex host resolver。

    入参：无。
    返回：使用生产只读进程表和 tmux reader 的 `CodexHostResolver`。
    错误处理：构造阶段不读取外部状态，读取错误会在 resolver 方法内降级。
    副作用：无；不立即访问进程、tmux 或 Codex App 状态。
    """

    return CodexHostResolver()


def _format_host_context_line(session: AgentHostContext) -> str:
    """把 host context 格式化为单行人类可读输出。

    入参：`session` 是 resolver 输出的 host context。
    返回：包含 runtime、execution host、activation strategy 和 confidence 的字符串。
    错误处理：不主动抛业务异常。
    副作用：无；只读取内存模型。
    """

    execution = session.execution_host.kind.value
    if session.execution_host.tmux_pane_id:
        execution = f"{execution}:{session.execution_host.tmux_pane_id}"
    elif session.execution_host.host_app_name:
        execution = f"{execution}:{session.execution_host.host_app_name}"
    identifier = session.thread_id or (
        str(session.agent_pid) if session.agent_pid is not None else "unknown"
    )
    return (
        f"{session.runtime_kind.value} {identifier} "
        f"execution={execution} "
        f"activation={session.activation.strategy.value} "
        f"confidence={session.confidence.value}"
    )


def _payload_with_agent_pid(
    payload: dict[str, Any], agent_pid: str | None
) -> dict[str, Any]:
    """Return a hook payload augmented with optional host process metadata.

    入参：`payload` 是已解析的 hook JSON object；`agent_pid` 是 hook command 捕获到的
    Codex 父进程 pid，可为空。
    返回：若 `agent_pid` 非空，返回包含 `agent_pid` 字段的浅拷贝；否则返回原 payload。
    错误处理：本 helper 不校验 pid 格式，避免不同宿主实现的 pid 表达被过早拒绝。
    副作用：不修改调用方传入的 dict，不访问网络、硬件或文件。
    """

    if agent_pid is None or not agent_pid.strip():
        return payload
    enriched = dict(payload)
    enriched["agent_pid"] = agent_pid.strip()
    return enriched


def _event_from_hook_payload(
    payload: dict[str, Any],
    *,
    normalized_type: EventType,
    default_source_event_type: str,
) -> NormalizedEvent:
    """Build a Codex normalized event from a generic hook payload.

    入参：`payload` 是 Codex hook JSON object；`normalized_type` 是目标 Agent Deck event
    type；`default_source_event_type` 在 payload 未提供事件名时使用。
    返回：完成校验的 `NormalizedEvent`。
    错误处理：若派生字段不满足 `NormalizedEvent` 约束，Pydantic 异常会向调用方传播。
    副作用：读取当前 UTC 时间；不访问网络、硬件或文件。
    """

    session_id = _string_field(
        payload,
        "session_id",
        "sessionId",
        "thread-id",
        default="codex-hook",
    )
    source_event_type = _string_field(
        payload,
        "event_type",
        "eventType",
        "hookEventName",
        "hook_event_name",
        default=default_source_event_type,
    )
    return NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type=source_event_type,
        normalized_type=normalized_type,
        session_id=session_id,
        agent_id=_optional_string_field(payload, "agent_id", "agentId"),
        thread_id=_optional_string_field(payload, "thread_id", "threadId", "thread-id"),
        turn_id=_optional_string_field(payload, "turn_id", "turnId", "turn-id"),
        cwd=_optional_string_field(payload, "cwd", "workspace"),
        title=_optional_string_field(payload, "title", "summary"),
        summary=_optional_string_field(payload, "message", "summary"),
        payload=payload,
        occurred_at=datetime.now(UTC),
    )


def _normalized_event_type_from_codex_hook(payload: dict[str, Any]) -> EventType:
    """Map a Codex hook event name to an Agent Deck normalized event type.

    入参：`payload` 是 Codex hook JSON object；优先读取 `hookEventName`、`hook_event_name`
    或 event type 同义字段。
    返回：对应 `EventType`；未知 hook 收敛为 `HEARTBEAT`，避免错误推进会话状态。
    错误处理：本函数不抛业务异常；非字符串或空字符串字段会被忽略。
    副作用：无；只读取内存 payload。
    """

    hook_name = _string_field(
        payload,
        "hookEventName",
        "hook_event_name",
        "event_type",
        "eventType",
        default="codex-hook",
    )
    return {
        "SessionStart": EventType.SESSION_STARTED,
        "UserPromptSubmit": EventType.TURN_STARTED,
        "PreToolUse": EventType.TOOL_STARTED,
        "PostToolUse": EventType.TOOL_COMPLETED,
        "PermissionRequest": EventType.APPROVAL_REQUESTED,
        "Stop": EventType.TURN_COMPLETED,
        "SubagentStart": EventType.SUBAGENT_STARTED,
    }.get(hook_name, EventType.HEARTBEAT)


def _decision_request_from_hook_payload(
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Build the daemon decision request JSON from a Codex hook payload.

    入参：`payload` 是 Codex permission hook JSON object；`timeout_seconds` 是审批过期秒数。
    返回：符合 daemon `/decisions/request` 的 JSON object。
    错误处理：非正 timeout 写 stderr 并以 exit 2 退出。
    副作用：仅读取内存 payload，不访问网络、硬件或文件。
    """

    if timeout_seconds <= 0:
        typer.echo("--timeout-seconds must be positive", err=True)
        raise typer.Exit(2)
    session_id = _string_field(payload, "session_id", "sessionId", default="codex-hook")
    tool_name = _string_field(
        payload,
        "tool_name",
        "toolName",
        "tool",
        "command",
        default="codex-permission",
    )
    reason = _string_field(
        payload,
        "reason",
        "message",
        "summary",
        "prompt",
        default=f"Codex permission request for {tool_name}",
    )
    return {
        "agent_key": f"{AgentSource.CODEX.value}:{session_id}",
        "session_id": session_id,
        "turn_id": _optional_string_field(payload, "turn_id", "turnId"),
        "tool_name": tool_name,
        "reason": reason,
        "timeout_seconds": timeout_seconds,
    }


def _codex_permission_output(behavior: str, message: str) -> dict[str, Any]:
    """Build the JSON response expected by the Codex permission hook.

    入参：`behavior` 是 `allow` 或 `deny`；`message` 是展示给 Codex 的说明文本。
    返回：包含 `hookSpecificOutput.PermissionRequest.decision` 的 JSON object。
    错误处理：本 helper 不主动校验 behavior；调用方负责传入合法值。
    副作用：无；只构造内存 dict。
    """

    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": behavior,
                "message": message,
            },
        }
    }


def _parse_event_type(value: str) -> EventType:
    """Parse a normalized event type string for CLI input.

    入参：`value` 是用户传入的 normalized event type 文本。
    返回：对应 `EventType` 枚举成员。
    错误处理：未知值写 stderr 并以 exit 2 退出。
    副作用：无；只做枚举转换。
    """

    try:
        return EventType(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in EventType)
        typer.echo(f"invalid event type: {value}; expected one of: {allowed}", err=True)
        raise typer.Exit(2) from exc


def _parse_decision_behavior(value: str) -> DecisionBehavior:
    """Parse a decision behavior string for CLI or daemon output.

    入参：`value` 是行为文本，合法值为 `allow` 或 `deny`。
    返回：对应 `DecisionBehavior` 枚举成员。
    错误处理：未知值写 stderr 并以 exit 2 退出。
    副作用：无；只做枚举转换。
    """

    try:
        return DecisionBehavior(value)
    except ValueError as exc:
        typer.echo("invalid behavior: expected allow or deny", err=True)
        raise typer.Exit(2) from exc


def _decision_behavior_from_daemon(value: Any) -> DecisionBehavior:
    """Parse daemon decision output without treating bad daemon data as CLI usage.

    入参：`value` 是 daemon `/wait` 响应中的 behavior 字段，期望为 `allow` 或 `deny`。
    返回：对应 `DecisionBehavior` 枚举成员。
    错误处理：缺失、非字符串或未知行为抛 ValueError，供 permission hook fail-closed 为 deny。
    副作用：无；只检查内存值。
    """

    if not isinstance(value, str):
        raise ValueError("daemon response missing behavior")
    try:
        return DecisionBehavior(value)
    except ValueError as exc:
        raise ValueError(f"daemon response has invalid behavior: {value}") from exc


def _string_field(
    payload: dict[str, Any],
    *keys: str,
    default: str,
) -> str:
    """Return the first non-empty string field from a payload.

    入参：`payload` 是 hook JSON object；`keys` 是按优先级查找的字段名；`default` 是未找到
    可用字符串时的返回值。
    返回：非空字符串字段值或 default。
    错误处理：本 helper 不抛业务异常；非字符串或空字符串字段会被忽略。
    副作用：无；只读取内存 dict。
    """

    value = _optional_string_field(payload, *keys)
    return value if value is not None else default


def _optional_string_field(payload: dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty string field or None.

    入参：`payload` 是 hook JSON object；`keys` 是候选字段名。
    返回：首个非空字符串值；找不到时返回 None。
    错误处理：本 helper 不抛业务异常；非字符串或空字符串字段会被忽略。
    副作用：无；只读取内存 dict。
    """

    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _join_url(base_url: str, *parts: str) -> str:
    """Join a daemon base URL with path components.

    入参：`base_url` 是 daemon base URL；`parts` 是无需包含斜杠的路径片段。
    返回：去除重复边界斜杠后的完整 URL。
    错误处理：本 helper 不校验 URL 合法性；非法 URL 由 httpx 在请求阶段报告。
    副作用：无；只做字符串拼接。
    """

    suffix = "/".join(part.strip("/") for part in parts)
    return f"{base_url.rstrip('/')}/{suffix}"


def _echo_json(payload: dict[str, Any]) -> None:
    """Print a JSON object with stable formatting.

    入参：`payload` 是要输出的 JSON object。
    返回：无返回值；格式化文本写入 stdout。
    错误处理：不可 JSON 序列化的值会由 `json.dumps` 抛出异常。
    副作用：向 stdout 写入一段 UTF-8 JSON 文本。
    """

    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _fail_http_command(command_name: str, exc: Exception) -> None:
    """Report a control-command HTTP failure and exit with status 1.

    入参：`command_name` 是当前 control 子命令名称；`exc` 是捕获到的 HTTP 或 JSON 异常。
    返回：不返回；总是抛出 `typer.Exit(1)`。
    错误处理：通过 Typer exit code 1 表示 daemon 不可达或响应不可用。
    副作用：向 stderr 写入一行错误说明。
    """

    typer.echo(f"agent-deckctl {command_name}: {exc}", err=True)
    raise typer.Exit(1)
