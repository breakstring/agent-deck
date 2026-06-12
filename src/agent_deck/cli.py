"""Command-line entry points for Agent Deck daemon, control, and Codex hooks.

This module owns only CLI parsing, JSON/stdin handling, local daemon HTTP calls,
and uvicorn hosting for the packaged console scripts. It does not probe
hardware, install or edit Codex configuration, persist daemon state, implement
broker logic, or mutate user files. Network side effects are limited to explicit
HTTP calls to the configured local daemon URL with bounded httpx timeouts.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
import typer
import uvicorn

from agent_deck import __version__
from agent_deck.core.decisions import DecisionBehavior
from agent_deck.core.events import AgentSource, EventType, NormalizedEvent
from agent_deck.server.app import create_app

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
        ),
    ] = 8765,
) -> None:
    """Start the local daemon when no daemon subcommand is selected.

    入参：`ctx` 是 Typer/Click 当前命令上下文，用于判断是否已有子命令；`host`
    是 uvicorn 监听地址，默认 `127.0.0.1`；`port` 是监听 TCP 端口，默认 `8765`。
    返回：无显式返回值；`uvicorn.run` 负责阻塞运行 ASGI app。
    错误处理：Typer 处理 CLI 参数错误；`create_app` 或 `uvicorn.run` 抛出的异常会向上
    传播并使命令失败。
    副作用：当没有子命令时创建 FastAPI app 并启动 uvicorn 监听指定本地地址；不探测硬件、
    不读写用户配置、不安装 Codex hooks。
    """

    if ctx.invoked_subcommand is not None:
        return
    uvicorn.run(create_app(), host=host, port=port)


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
    daemon_url: Annotated[
        str,
        typer.Option(
            "--daemon-url",
            help="Base URL for the local Agent Deck daemon.",
        ),
    ] = DEFAULT_DAEMON_URL,
) -> None:
    """Forward a Codex notify payload as a best-effort turn.completed event.

    入参：`daemon_url` 是 daemon base URL；stdin 必须是非空 JSON object，字段会尽力映射到
    Codex `TURN_COMPLETED` normalized event。
    返回：无显式返回值；成功时不要求输出固定内容。
    错误处理：stdin 为空、非法 JSON 或非 object 时以 exit 2 退出；daemon 不可达、HTTP
    非 2xx 或 JSON 解码失败时写 stderr 但 exit 0。
    副作用：读取 stdin，并可能用 bounded httpx POST 到 `/events`；不修改配置或文件。
    """

    payload = _read_json_object_from_stdin()
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
        float,
        typer.Option(
            "--timeout-seconds",
            help="Seconds to wait for an Agent Deck permission decision.",
        ),
    ] = 25,
) -> None:
    """Request and wait for a Codex permission decision with fail-closed output.

    入参：`daemon_url` 是 daemon base URL；`timeout_seconds` 是 request 与 wait 的审批等待秒数；
    stdin 必须是非空 JSON object，字段会尽力映射到 daemon decision request。
    返回：无显式返回值；stdout 始终输出 Codex hook JSON decision payload。
    错误处理：stdin 为空、非法 JSON 或非 object 时以 exit 2 退出；daemon 不可达、HTTP
    非 2xx、缺少 decision id 或 JSON 解码失败时写 stderr、输出 deny JSON，并 exit 0。
    副作用：读取 stdin，并可能用 bounded httpx POST `/decisions/request` 后 GET
    `/decisions/{id}/wait`；不安装 hook、不读写用户配置。
    """

    payload = _read_json_object_from_stdin()
    request_body = _decision_request_from_hook_payload(payload, timeout_seconds)
    try:
        request_payload = _http_post_json(
            _join_url(daemon_url, "decisions", "request"),
            request_body,
            timeout=max(_DEFAULT_HTTP_TIMEOUT_SECONDS, timeout_seconds + 1),
        )
        decision_id = request_payload.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            raise ValueError("daemon response missing decision_id")
        result = _http_get_json(
            _join_url(daemon_url, "decisions", decision_id, "wait"),
            params={"timeout_seconds": timeout_seconds},
            timeout=max(_DEFAULT_HTTP_TIMEOUT_SECONDS, timeout_seconds + 1),
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

    session_id = _string_field(payload, "session_id", "sessionId", default="codex-hook")
    source_event_type = _string_field(
        payload,
        "event_type",
        "eventType",
        "hookEventName",
        default=default_source_event_type,
    )
    return NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type=source_event_type,
        normalized_type=normalized_type,
        session_id=session_id,
        agent_id=_optional_string_field(payload, "agent_id", "agentId"),
        thread_id=_optional_string_field(payload, "thread_id", "threadId"),
        turn_id=_optional_string_field(payload, "turn_id", "turnId"),
        cwd=_optional_string_field(payload, "cwd", "workspace"),
        title=_optional_string_field(payload, "title", "summary"),
        summary=_optional_string_field(payload, "message", "summary"),
        payload=payload,
        occurred_at=datetime.now(UTC),
    )


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
