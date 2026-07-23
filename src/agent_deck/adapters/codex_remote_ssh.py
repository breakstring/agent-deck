"""通过 SSH 只读观察远端 ChatGPT/Codex App 的顶层任务状态。

本模块启动一条独立的 ``ssh ... codex app-server proxy`` 子进程，通过代理流上的
WebSocket/JSON-RPC 连接远端共享 app-server，只调用 ``initialize``、
``thread/list(useStateDbOnly=true)``，以及显式启用时的 ``config/read``。配置响应只投影
``desktop.selected-avatar-id``，返回模型会立即丢弃其余 config、thread preview、turn 和 item，
不记录 prompt，也不会调用任何创建、恢复、执行、打断或归档方法。观察器可跨轮询复用同一
SSH 连接；传输失败时只重建自己的连接，不接触 ChatGPT App 已建立的 SSH 会话。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import selectors
import struct
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO, Final

from pydantic import BaseModel, ConfigDict, Field

from agent_deck import __version__
from agent_deck.adapters.codex_app_state import CodexAppActiveSession
from agent_deck.core.state import AgentStatus

_SSH_HOST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?!-)[A-Za-z0-9_.@-]+$")
_REMOTE_COMMAND: Final[str] = "codex app-server proxy"
_MAX_HTTP_HEADER_BYTES: Final[int] = 32_768
_MAX_FRAME_BYTES: Final[int] = 8 * 1024 * 1024
_READ_CHUNK_BYTES: Final[int] = 64 * 1024
_INTERACTIVE_SOURCE_KINDS: Final[tuple[str, ...]] = ("vscode",)
_GLOBAL_STATE_FILENAME: Final[str] = ".codex-global-state.json"
_MANAGED_CONNECTIONS_KEY: Final[str] = "codex-managed-remote-connections"
_AUTO_CONNECT_KEY: Final[str] = "remote-connection-auto-connect-by-host-id"
_REMOTE_SSH_HOST_ID_PREFIX: Final[str] = "remote-ssh-"
_READ_ONLY_METHODS: Final[frozenset[str]] = frozenset(
    {"initialize", "initialized", "thread/list", "config/read"}
)


class CodexRemoteSshError(RuntimeError):
    """表示 SSH、WebSocket 或只读 app-server 协议失败。

    入参：异常消息只应包含主机别名和短诊断，不得包含远端 thread preview 或原始响应。
    返回：作为 ``RuntimeError`` 子类由 CLI 或 daemon poller 捕获。
    错误处理：调用方可把本异常作为可重连错误；不得据此修改远端状态。
    副作用：构造异常本身不访问进程、网络或文件。
    """


class CodexRemoteSshSnapshot(BaseModel):
    """描述一次已经脱敏的远端 ChatGPT/Codex App 状态观察。

    入参：``host`` 是 ChatGPT Settings 已启用 Connection 的 SSH 别名；``host_id`` 是不可逆短摘要；
    ``sessions`` 只包含顶层 vscode/ChatGPT thread 的活动、错误或短暂完成反馈；
    计数字段只暴露状态分布，不保留 preview、turn 或 item。
    返回：冻结模型，可直接用于诊断 JSON 或 daemon 聚合。
    错误处理：字段非法时由 Pydantic 报告。
    副作用：模型自身不持有 SSH 进程或原始 app-server 响应。
    """

    model_config = ConfigDict(frozen=True)

    host: str
    host_id: str
    observed_at: datetime
    server_user_agent: str | None = None
    considered_thread_count: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    sessions: tuple[CodexAppActiveSession, ...] = ()
    selected_avatar_id: str | None = None
    pet_config_available: bool = False


class CodexRemoteSshEnabledHost(BaseModel):
    """描述 ChatGPT Settings 中已管理且允许自动连接的一个 SSH 主机。

    入参：``chatgpt_host_id`` 是 App 内部 host id；``alias`` 是 App 已保存的具体 SSH alias；
    ``display_name`` 仅用于诊断展示。调用方不得从 ``~/.ssh/config`` 补充或扩展本模型。
    返回：冻结模型，供 daemon 动态创建只读 observer。
    错误处理：空字段由 Pydantic 拒绝；alias 还会在 observer 构造时经过 argv 白名单校验。
    副作用：无；模型不连接主机、不读取文件。
    """

    model_config = ConfigDict(frozen=True)

    chatgpt_host_id: str = Field(min_length=1)
    alias: str = Field(min_length=1)
    display_name: str = Field(min_length=1)


class CodexRemoteSshDiscoverySnapshot(BaseModel):
    """描述一次仅基于 ChatGPT Settings 状态的远端 SSH 主机发现。

    入参：``enabled_hosts`` 只包含 managed connection 且 auto-connect 严格为 true 的 SSH
    主机；计数字段说明有多少 managed SSH 被启用或因 false/missing 被跳过。
    返回：冻结脱敏模型，不包含 remote projects、SSH config 内容或连接凭据。
    错误处理：字段非法由 Pydantic 报告。
    副作用：模型自身不读取文件、不连接主机。
    """

    model_config = ConfigDict(frozen=True)

    observed_at: datetime
    state_path: str
    enabled_hosts: tuple[CodexRemoteSshEnabledHost, ...] = ()
    managed_ssh_count: int = 0
    auto_connect_disabled_count: int = 0
    ignored_non_ssh_count: int = 0


class _RemoteThread(BaseModel):
    """保存从 ``thread/list`` 原始对象提取出的最小安全字段。

    入参：仅接受 thread id、name、cwd、更新时间、父 thread id 和粗粒度状态。
    返回：冻结内部模型，供状态映射与完成过渡检测使用。
    错误处理：缺少 id、cwd 或非法时间时由 Pydantic 报告并让该条记录被调用方跳过。
    副作用：不保存 ``preview``、``turns``、``items``、``path`` 或原始 source 对象。
    """

    model_config = ConfigDict(frozen=True)

    thread_id: str = Field(min_length=1)
    name: str | None = None
    cwd: str | None = None
    updated_at: int = 0
    parent_thread_id: str | None = None
    status_type: str
    active_flags: tuple[str, ...] = ()


ProcessFactory = Callable[..., subprocess.Popen[bytes]]
"""创建 SSH 子进程的可注入工厂；测试可替换，生产默认使用 ``subprocess.Popen``。"""


def resolve_chatgpt_global_state_path(path: Path | None = None) -> Path:
    """解析 ChatGPT Desktop 全局状态文件路径。

    入参：``path`` 供测试或显式诊断覆盖；为空时只使用
    ``${CODEX_HOME:-~/.codex}/.codex-global-state.json``。
    返回：展开用户目录后的路径；不检查存在性。
    错误处理：无。
    副作用：只读取 ``CODEX_HOME`` 环境变量；明确不会读取或搜索 ``~/.ssh/config``。
    """

    if path is not None:
        return path.expanduser()
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return codex_home / _GLOBAL_STATE_FILENAME


def discover_enabled_codex_remote_ssh_hosts(
    *,
    state_path: Path | None = None,
) -> CodexRemoteSshDiscoverySnapshot:
    """只读取 ChatGPT Settings 中已管理且 auto-connect=true 的 SSH 主机。

    入参：``state_path`` 可覆盖 ChatGPT global state 文件，主要用于测试；默认走
    ``resolve_chatgpt_global_state_path``。
    返回：``CodexRemoteSshDiscoverySnapshot``；仅接受 managed connections 中的 SSH 记录，
    且 ``remote-connection-auto-connect-by-host-id[hostId] is true`` 才进入 enabled_hosts。
    错误处理：文件不存在、JSON 非法或关键容器类型错误时抛 ``OSError``/``ValueError``，
    让 daemon fail-closed；单条非法记录被计入忽略，不猜测 alias。
    副作用：只读一个 ChatGPT 状态 JSON；不读取 ``~/.ssh/config``、remote-projects 或
    selected-host，不执行 ``ssh -G``，也不建立网络连接。
    """

    resolved_path = resolve_chatgpt_global_state_path(state_path)
    raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("ChatGPT global state must be a JSON object")
    managed = raw.get(_MANAGED_CONNECTIONS_KEY, [])
    auto_connect = raw.get(_AUTO_CONNECT_KEY, {})
    if not isinstance(managed, list):
        raise ValueError("ChatGPT managed remote connections must be a JSON array")
    if not isinstance(auto_connect, Mapping):
        raise ValueError("ChatGPT remote auto-connect state must be a JSON object")

    enabled_hosts: list[CodexRemoteSshEnabledHost] = []
    seen_host_ids: set[str] = set()
    managed_ssh_count = 0
    disabled_count = 0
    ignored_count = 0
    for record in managed:
        if not isinstance(record, Mapping):
            ignored_count += 1
            continue
        host_id = _optional_text(record.get("hostId"))
        if host_id is None or not host_id.startswith(_REMOTE_SSH_HOST_ID_PREFIX):
            ignored_count += 1
            continue
        managed_ssh_count += 1
        if auto_connect.get(host_id) is not True:
            disabled_count += 1
            continue
        alias = _optional_text(record.get("alias"))
        if alias is None or host_id in seen_host_ids:
            ignored_count += 1
            continue
        try:
            validated_alias = validate_ssh_host_alias(alias)
        except ValueError:
            ignored_count += 1
            continue
        display_name = _optional_text(record.get("displayName")) or validated_alias
        enabled_hosts.append(
            CodexRemoteSshEnabledHost(
                chatgpt_host_id=host_id,
                alias=validated_alias,
                display_name=display_name,
            )
        )
        seen_host_ids.add(host_id)
    enabled_hosts.sort(key=lambda host: (host.display_name.casefold(), host.alias))
    return CodexRemoteSshDiscoverySnapshot(
        observed_at=datetime.now(UTC),
        state_path=str(resolved_path),
        enabled_hosts=tuple(enabled_hosts),
        managed_ssh_count=managed_ssh_count,
        auto_connect_disabled_count=disabled_count,
        ignored_non_ssh_count=ignored_count,
    )


def validate_ssh_host_alias(host: str) -> str:
    """校验一个可安全作为 OpenSSH destination argv 的具体主机别名。

    入参：``host`` 可为 SSH config alias、hostname、IP 或 ``user@host``；不允许空白、
    控制字符、通配符、斜杠或以 ``-`` 开头的 option-like 值。
    返回：去除首尾空白后的别名。
    错误处理：不满足白名单时抛 ``ValueError``。
    副作用：无；不读取 ``~/.ssh/config``，也不尝试建立连接。
    """

    normalized = host.strip()
    if not normalized or _SSH_HOST_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            "SSH host must be a concrete alias/hostname using letters, digits, "
            "'.', '_', '-', or '@'"
        )
    return normalized


def codex_remote_host_id(host: str) -> str:
    """为 SSH 别名生成稳定且不可逆的短 host id。

    入参：``host`` 必须通过 ``validate_ssh_host_alias``。
    返回：以 ``ssh-`` 开头的 16 位 SHA-256 摘要，供 agent identity 与诊断关联。
    错误处理：非法 host 由校验函数抛 ``ValueError``。
    副作用：无；不解析 DNS 或 SSH config。
    """

    normalized = validate_ssh_host_alias(host)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"ssh-{digest}"


def build_codex_remote_ssh_command(
    host: str,
    *,
    connect_timeout_seconds: float,
) -> tuple[str, ...]:
    """构造固定远端命令且不经过本地 shell 的 OpenSSH argv。

    入参：``host`` 是已校验的 destination；``connect_timeout_seconds`` 必须为正数。
    返回：包含 batch mode、连接超时、``--`` 和固定 app-server proxy 命令的 argv。
    错误处理：host 非法或 timeout 非正时抛 ``ValueError``。
    副作用：无；只构造字符串，不启动进程。
    """

    normalized = validate_ssh_host_alias(host)
    if connect_timeout_seconds <= 0:
        raise ValueError("connect_timeout_seconds must be positive")
    timeout = max(1, int(connect_timeout_seconds))
    return (
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
        "--",
        normalized,
        _REMOTE_COMMAND,
    )


def _selected_avatar_from_config_read(result: Any) -> tuple[str | None, bool]:
    """从 config/read 响应中只投影 Desktop App 的宠物选择。

    入参：``result`` 是远端 app-server 的 config/read result。
    返回：``(selected_avatar_id, available)``；config/desktop 合法即 available=true，
    未选择宠物时 ID 为 None。
    错误处理：顶层或 desktop 类型非法时抛 ``CodexRemoteSshError``；非字符串选择按未选择
    处理，避免把任意配置对象保留进内存。
    副作用：无；不复制 origins、layers 或其他配置字段。
    """

    if not isinstance(result, Mapping):
        raise CodexRemoteSshError("远端 config/read 结果非法")
    config = result.get("config")
    if not isinstance(config, Mapping):
        raise CodexRemoteSshError("远端 config/read 缺少 config object")
    desktop = config.get("desktop")
    if desktop is None:
        return None, True
    if not isinstance(desktop, Mapping):
        raise CodexRemoteSshError("远端 config/read 的 desktop 非 object")
    raw_selected = desktop.get("selected-avatar-id")
    if not isinstance(raw_selected, str):
        return None, True
    selected = raw_selected.strip()
    if len(selected) > 256:
        return None, True
    return (selected or None), True


class CodexRemoteSshObserver:
    """复用一条独立 SSH/WebSocket 连接读取远端顶层 ChatGPT App 状态。

    入参：``host`` 是 SSH destination；``timeout_seconds`` 控制握手和单次 RPC；
    ``thread_limit`` 控制只读 thread/list 页大小；``completed_feedback_seconds`` 控制
    active->idle 后保留 ``COMPLETED_RECENTLY`` 的本地反馈时间；``process_factory`` 供测试注入。
    ``read_pet_config`` 为 true 时额外调用一次 ``config/read``，但只保留宠物选择 ID。
    返回：实例通过 ``read_snapshot`` 返回脱敏快照，并可用 ``close`` 释放自己的子进程。
    错误处理：首次连接或协议失败抛 ``CodexRemoteSshError``；读取时先自动重连一次，
    连续失败后按 1、2、4…最多 60 秒退避。
    副作用：按需启动 SSH 子进程并保持一条远端控制 socket 客户端连接；只发白名单只读 RPC。
    """

    def __init__(
        self,
        host: str,
        *,
        timeout_seconds: float = 10.0,
        thread_limit: int = 80,
        completed_feedback_seconds: float = 10.0,
        read_pet_config: bool = False,
        process_factory: ProcessFactory = subprocess.Popen,
    ) -> None:
        """保存观察器配置；实际 SSH 连接延迟到首次读取。

        入参：同类 docstring；timeout 与 limit 必须为正，完成反馈秒数不得为负。
        返回：无显式返回值。
        错误处理：非法参数抛 ``ValueError``。
        副作用：只初始化锁和内存过渡表，不启动进程。
        """

        self.host = validate_ssh_host_alias(host)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if thread_limit <= 0:
            raise ValueError("thread_limit must be positive")
        if completed_feedback_seconds < 0:
            raise ValueError("completed_feedback_seconds must not be negative")
        self.host_id = codex_remote_host_id(self.host)
        self.timeout_seconds = timeout_seconds
        self.thread_limit = thread_limit
        self.completed_feedback_seconds = completed_feedback_seconds
        self.read_pet_config = bool(read_pet_config)
        self._process_factory = process_factory
        self._lock = RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._stdin: BinaryIO | None = None
        self._stdout: BinaryIO | None = None
        self._read_buffer = bytearray()
        self._next_request_id = 1
        self._server_user_agent: str | None = None
        self._previous_statuses: dict[str, str] = {}
        self._completed_until: dict[str, float] = {}
        self._consecutive_failures = 0
        self._retry_not_before_monotonic = 0.0

    def set_read_pet_config(self, enabled: bool) -> None:
        """动态控制后续轮询是否附带只读 config/read。

        入参：``enabled`` 仅由 PETS 面板远端来源策略驱动。
        返回：无显式返回。
        错误处理：无。
        副作用：只更新本观察器布尔值；不立即发送 RPC、不重连 SSH。
        """

        with self._lock:
            self.read_pet_config = bool(enabled)

    def read_snapshot(self) -> CodexRemoteSshSnapshot:
        """读取一次远端 thread/list，并立即投影为脱敏状态快照。

        入参：无；使用构造参数中的 host、timeout 和 limit。
        返回：只包含顶层 vscode thread 的活动、错误和短暂完成反馈。
        错误处理：连接或 RPC 失败时关闭自身连接并重试一次；仍失败则抛
        ``CodexRemoteSshError``，异常不包含原始 JSON 响应。
        副作用：可能启动或重建 SSH 子进程；会更新本地完成过渡表，不修改远端。
        """

        with self._lock:
            monotonic_now = time.monotonic()
            if (
                self._process is None
                and monotonic_now < self._retry_not_before_monotonic
            ):
                raise CodexRemoteSshError(
                    f"SSH 重连退避中（{self.host}）"
                )
            last_error: Exception | None = None
            for _attempt in range(2):
                try:
                    self._ensure_connected()
                    selected_avatar_id: str | None = None
                    pet_config_available = False
                    if self.read_pet_config:
                        try:
                            config_result = self._request(
                                "config/read",
                                {"cwd": None, "includeLayers": False},
                            )
                            selected_avatar_id, pet_config_available = (
                                _selected_avatar_from_config_read(config_result)
                            )
                        except CodexRemoteSshError:
                            # 旧版远端 app-server 可能尚不支持 config/read；任务状态
                            # 观察不能因此失效，runtime 会把宠物素材降级为内置分配。
                            selected_avatar_id = None
                            pet_config_available = False
                    result = self._request(
                        "thread/list",
                        {
                            "cursor": None,
                            "limit": self.thread_limit,
                            "sortKey": "updated_at",
                            "sortDirection": "desc",
                            "sourceKinds": list(_INTERACTIVE_SOURCE_KINDS),
                            "archived": False,
                            "useStateDbOnly": True,
                        },
                    )
                    snapshot = self._snapshot_from_thread_list(
                        result,
                        selected_avatar_id=selected_avatar_id,
                        pet_config_available=pet_config_available,
                    )
                    self._consecutive_failures = 0
                    self._retry_not_before_monotonic = 0.0
                    return snapshot
                except (OSError, EOFError, TimeoutError, ValueError, CodexRemoteSshError) as exc:
                    last_error = exc
                    self._close_transport()
            self._consecutive_failures += 1
            backoff_seconds = min(60.0, float(2 ** (self._consecutive_failures - 1)))
            self._retry_not_before_monotonic = time.monotonic() + backoff_seconds
            raise CodexRemoteSshError(
                f"无法读取远端 ChatGPT 状态（{self.host}）: "
                f"{type(last_error).__name__ if last_error else 'unknown error'}"
            ) from last_error

    def close(self) -> None:
        """关闭本观察器创建的 WebSocket 和 SSH 子进程。

        入参：无。
        返回：无显式返回值；重复调用安全。
        错误处理：关闭、等待或 terminate 异常被吞掉，避免 daemon shutdown 失败。
        副作用：只终止本实例创建的子进程，不查找或影响 ChatGPT App 的其他 SSH 进程。
        """

        with self._lock:
            self._close_transport()

    def __enter__(self) -> CodexRemoteSshObserver:
        """返回当前观察器以支持 ``with`` 生命周期。

        入参：无。
        返回：当前实例。
        错误处理：不连接，因此不会产生 SSH 错误。
        副作用：无。
        """

        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        """退出 ``with`` 时释放本观察器拥有的子进程。

        入参：标准 context manager 异常三元组，仅用于协议兼容。
        返回：None，不吞掉 ``with`` 代码块中的异常。
        错误处理：close 内部已做 best-effort。
        副作用：关闭自身 SSH 连接。
        """

        del exc_type, exc_value, traceback
        self.close()

    def _ensure_connected(self) -> None:
        """确保 SSH proxy、WebSocket 和 initialize 握手均已完成。

        入参：无。
        返回：无；成功后 stdin/stdout 可用于 JSON-RPC。
        错误处理：子进程、HTTP Upgrade 或 initialize 失败时抛异常。
        副作用：必要时启动 SSH 子进程并发送 initialize/initialized。
        """

        if self._process is not None and self._process.poll() is None:
            return
        command = build_codex_remote_ssh_command(
            self.host,
            connect_timeout_seconds=self.timeout_seconds,
        )
        try:
            process = self._process_factory(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            raise CodexRemoteSshError(
                f"无法启动 SSH（{self.host}）: {type(exc).__name__}"
            ) from exc
        if process.stdin is None or process.stdout is None:
            with suppress(Exception):
                process.terminate()
            raise CodexRemoteSshError(f"SSH 未提供可用管道（{self.host}）")
        self._process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._read_buffer.clear()
        self._next_request_id = 1
        self._perform_websocket_upgrade()
        initialize_result = self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "agent_deck",
                    "title": "Agent Deck",
                    "version": __version__,
                }
            },
        )
        if isinstance(initialize_result, Mapping):
            user_agent = initialize_result.get("userAgent")
            self._server_user_agent = (
                user_agent.strip()
                if isinstance(user_agent, str) and user_agent.strip()
                else None
            )
        self._notify("initialized", {})

    def _perform_websocket_upgrade(self) -> None:
        """在 proxy 原始流上完成标准 WebSocket HTTP Upgrade。

        入参：无；使用当前 stdin/stdout。
        返回：无；成功后流进入 WebSocket frame 模式。
        错误处理：超时、非 101 或 ``Sec-WebSocket-Accept`` 不匹配时抛
        ``CodexRemoteSshError``。
        副作用：向自身 SSH proxy 写一次 HTTP 请求并读取响应头。
        """

        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Connection: Upgrade\r\n"
            "Upgrade: websocket\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self._write_bytes(request)
        header = self._read_until(
            b"\r\n\r\n",
            max_bytes=_MAX_HTTP_HEADER_BYTES,
            timeout_seconds=self.timeout_seconds,
        )
        lines = header.decode("iso-8859-1").split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            raise CodexRemoteSshError(f"远端 WebSocket Upgrade 被拒绝（{self.host}）")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1(  # noqa: S324 - WebSocket RFC 固定握手算法，不用于安全哈希。
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise CodexRemoteSshError(f"远端 WebSocket 握手校验失败（{self.host}）")

    def _request(self, method: str, params: Mapping[str, Any]) -> Any:
        """发送一个白名单 JSON-RPC request 并等待匹配 response。

        入参：``method`` 只能是只读白名单方法；``params`` 必须为内存 mapping。
        返回：response 的 ``result``；调用方必须立即做安全字段投影。
        错误处理：非白名单、超时、远端 error 或非法 response 抛异常，原始 payload 不进消息。
        副作用：写一个 masked text frame，并消费其间的通知和 ping frame。
        """

        if method not in _READ_ONLY_METHODS or method == "initialized":
            raise ValueError(f"RPC method is not allowed: {method}")
        request_id = self._next_request_id
        self._next_request_id += 1
        self._send_json({"method": method, "id": request_id, "params": dict(params)})
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            message = self._read_json_message(deadline)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise CodexRemoteSshError(
                    f"远端 app-server 拒绝只读方法 {method}（{self.host}）"
                )
            if "result" not in message:
                raise CodexRemoteSshError(
                    f"远端 app-server 返回非法响应（{self.host}）"
                )
            return message["result"]

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        """发送一个白名单 JSON-RPC notification。

        入参：当前仅允许 ``initialized``；``params`` 是协议参数。
        返回：无。
        错误处理：非白名单通知抛 ``ValueError``。
        副作用：写一个 masked text frame，不等待响应。
        """

        if method != "initialized" or method not in _READ_ONLY_METHODS:
            raise ValueError(f"RPC notification is not allowed: {method}")
        self._send_json({"method": method, "params": dict(params)})

    def _snapshot_from_thread_list(
        self,
        result: Any,
        *,
        selected_avatar_id: str | None = None,
        pet_config_available: bool = False,
    ) -> CodexRemoteSshSnapshot:
        """把 thread/list result 立即缩减为不含 prompt 的安全快照。

        入参：``result`` 是刚收到的 response result；只读取 ``data`` 中的少量 metadata；
        宠物字段已经由 config/read 安全投影。
        返回：含状态分布和活动会话的 ``CodexRemoteSshSnapshot``。
        错误处理：result 顶层非法时抛 ``CodexRemoteSshError``；单条 thread 非法时跳过。
        副作用：更新 active->idle 完成反馈的本地过渡表，原始 result 在返回后即可释放。
        """

        if not isinstance(result, Mapping) or not isinstance(result.get("data"), list):
            raise CodexRemoteSshError(f"远端 thread/list 结果非法（{self.host}）")
        threads: list[_RemoteThread] = []
        for raw_thread in result["data"]:
            safe_thread = _safe_remote_thread(raw_thread)
            if safe_thread is not None and not safe_thread.parent_thread_id:
                threads.append(safe_thread)
        observed_at = datetime.now(UTC)
        monotonic_now = time.monotonic()
        sessions = self._sessions_from_threads(threads, monotonic_now=monotonic_now)
        counts: dict[str, int] = {}
        for thread in threads:
            counts[thread.status_type] = counts.get(thread.status_type, 0) + 1
        return CodexRemoteSshSnapshot(
            host=self.host,
            host_id=self.host_id,
            observed_at=observed_at,
            server_user_agent=self._server_user_agent,
            considered_thread_count=len(threads),
            status_counts=counts,
            sessions=sessions,
            selected_avatar_id=selected_avatar_id,
            pet_config_available=pet_config_available,
        )

    def _sessions_from_threads(
        self,
        threads: list[_RemoteThread],
        *,
        monotonic_now: float,
    ) -> tuple[CodexAppActiveSession, ...]:
        """映射远端状态并维护短暂 active->idle 完成反馈。

        入参：``threads`` 已去除 child；``monotonic_now`` 用于本地反馈截止时间。
        返回：活动、等待、错误或仍在完成反馈窗口内的 session 元组。
        错误处理：未知 status 降级为忽略，不猜测 review 或工具类型。
        副作用：更新 previous status 和 completed deadline 两个内存字典。
        """

        sessions: list[CodexAppActiveSession] = []
        seen_ids: set[str] = set()
        for thread in threads:
            seen_ids.add(thread.thread_id)
            previous = self._previous_statuses.get(thread.thread_id)
            if (
                previous == "active"
                and thread.status_type == "idle"
                and self.completed_feedback_seconds > 0
            ):
                self._completed_until[thread.thread_id] = (
                    monotonic_now + self.completed_feedback_seconds
                )
            if thread.status_type == "active":
                self._completed_until.pop(thread.thread_id, None)
            status_reason = _agent_status_from_remote_thread(thread)
            if status_reason is None:
                completed_until = self._completed_until.get(thread.thread_id)
                if completed_until is not None and monotonic_now < completed_until:
                    status_reason = (
                        AgentStatus.COMPLETED_RECENTLY,
                        "remote active -> idle",
                    )
                elif completed_until is not None:
                    self._completed_until.pop(thread.thread_id, None)
            if status_reason is not None:
                status, reason = status_reason
                sessions.append(
                    CodexAppActiveSession(
                        thread_id=thread.thread_id,
                        title=thread.name or "ChatGPT 远端任务",
                        cwd=thread.cwd,
                        rollout_path=None,
                        updated_at=thread.updated_at,
                        status=status,
                        reason=reason,
                        parent_thread_id=None,
                        thread_source="vscode",
                        is_child_thread=False,
                        execution_host_id=self.host_id,
                        execution_host_label=self.host,
                        is_remote=True,
                    )
                )
            self._previous_statuses[thread.thread_id] = thread.status_type
        for thread_id in tuple(self._previous_statuses):
            if thread_id not in seen_ids:
                self._previous_statuses.pop(thread_id, None)
                self._completed_until.pop(thread_id, None)
        sessions.sort(key=lambda session: session.updated_at, reverse=True)
        return tuple(sessions)

    def _send_json(self, message: Mapping[str, Any]) -> None:
        """把 JSON object 编码成一个 masked WebSocket text frame。

        入参：``message`` 不得包含不可序列化对象。
        返回：无。
        错误处理：序列化或管道写入失败按原异常传播。
        副作用：向本实例 SSH stdin 写字节；不记录消息内容。
        """

        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._write_frame(opcode=0x1, payload=payload)

    def _read_json_message(self, deadline: float) -> Mapping[str, Any]:
        """读取下一个完整 JSON text message，自动响应 ping。

        入参：``deadline`` 是 monotonic 绝对截止时间。
        返回：解析后的 JSON object。
        错误处理：close、binary、fragment、超大 frame、非法 UTF-8/JSON 或超时抛异常。
        副作用：遇到 ping 会向远端写 pong；其他通知仅消费不保存。
        """

        fragments = bytearray()
        started = False
        while True:
            opcode, fin, payload = self._read_frame(deadline)
            if opcode == 0x8:
                raise EOFError("remote websocket closed")
            if opcode == 0x9:
                self._write_frame(opcode=0xA, payload=payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1 and not started:
                started = True
                fragments.extend(payload)
            elif opcode == 0x0 and started:
                fragments.extend(payload)
            else:
                raise CodexRemoteSshError(
                    f"远端 WebSocket 返回不支持的 frame（{self.host}）"
                )
            if len(fragments) > _MAX_FRAME_BYTES:
                raise CodexRemoteSshError(f"远端消息过大（{self.host}）")
            if not fin:
                continue
            decoded = json.loads(fragments.decode("utf-8"))
            if not isinstance(decoded, Mapping):
                raise CodexRemoteSshError(f"远端 JSON-RPC 消息不是 object（{self.host}）")
            return decoded

    def _write_frame(self, *, opcode: int, payload: bytes) -> None:
        """写一个 RFC 6455 client frame。

        入参：``opcode`` 是 text/pong/close 等 4 位操作码；``payload`` 是 frame 内容。
        返回：无。
        错误处理：超大 payload 抛 ``ValueError``；管道异常向上传播。
        副作用：使用随机 mask key 并写入自身 SSH stdin。
        """

        if len(payload) > _MAX_FRAME_BYTES:
            raise ValueError("websocket payload is too large")
        first = 0x80 | (opcode & 0x0F)
        mask_key = secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
        masked = bytes(value ^ mask_key[index % 4] for index, value in enumerate(payload))
        self._write_bytes(header + mask_key + masked)

    def _read_frame(self, deadline: float) -> tuple[int, bool, bytes]:
        """读取一个 RFC 6455 server frame。

        入参：``deadline`` 是 monotonic 绝对截止时间。
        返回：``(opcode, fin, payload)``；兼容错误实现发送的 masked server frame。
        错误处理：长度超限、RSV 位、控制帧非法、EOF 或超时抛异常。
        副作用：从自身 SSH stdout 读取字节。
        """

        head = self._read_exact(2, deadline)
        first, second = head
        fin = bool(first & 0x80)
        if first & 0x70:
            raise CodexRemoteSshError(f"远端 WebSocket RSV 位非法（{self.host}）")
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2, deadline))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8, deadline))[0]
        if length > _MAX_FRAME_BYTES:
            raise CodexRemoteSshError(f"远端 WebSocket frame 过大（{self.host}）")
        if opcode >= 0x8 and (not fin or length > 125):
            raise CodexRemoteSshError(f"远端 WebSocket 控制帧非法（{self.host}）")
        mask_key = self._read_exact(4, deadline) if masked else b""
        payload = self._read_exact(length, deadline)
        if masked:
            payload = bytes(
                value ^ mask_key[index % 4] for index, value in enumerate(payload)
            )
        return opcode, fin, payload

    def _read_until(
        self,
        marker: bytes,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        """读取到 marker 为止并保留 marker 之后的缓冲字节。

        入参：``marker`` 非空；``max_bytes`` 是安全上限；``timeout_seconds`` 必须为正。
        返回：包含 marker 的前缀字节。
        错误处理：超时、EOF 或超过上限抛异常。
        副作用：从 SSH stdout 读取并更新内部 buffer。
        """

        deadline = time.monotonic() + timeout_seconds
        while True:
            index = self._read_buffer.find(marker)
            if index >= 0:
                end = index + len(marker)
                value = bytes(self._read_buffer[:end])
                del self._read_buffer[:end]
                return value
            if len(self._read_buffer) >= max_bytes:
                raise CodexRemoteSshError(f"远端 HTTP header 过大（{self.host}）")
            self._read_more(deadline)

    def _read_exact(self, size: int, deadline: float) -> bytes:
        """在绝对截止时间前读取指定字节数。

        入参：``size`` 不得为负；``deadline`` 是 monotonic 绝对时间。
        返回：恰好 ``size`` 个字节。
        错误处理：负 size 抛 ValueError；超时或 EOF 抛对应异常。
        副作用：消费内部 buffer，必要时读取 SSH stdout。
        """

        if size < 0:
            raise ValueError("size must not be negative")
        while len(self._read_buffer) < size:
            self._read_more(deadline)
        value = bytes(self._read_buffer[:size])
        del self._read_buffer[:size]
        return value

    def _read_more(self, deadline: float) -> None:
        """等待 stdout 可读并向内部 buffer 追加一块数据。

        入参：``deadline`` 是 monotonic 绝对时间。
        返回：无。
        错误处理：未连接、超时或 EOF 抛异常。
        副作用：使用 selector 等待并从 SSH stdout fd 读取。
        """

        if self._stdout is None:
            raise EOFError("remote stdout is not connected")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("remote app-server read timed out")
        selector = selectors.DefaultSelector()
        try:
            selector.register(self._stdout, selectors.EVENT_READ)
            if not selector.select(remaining):
                raise TimeoutError("remote app-server read timed out")
        finally:
            selector.close()
        chunk = os.read(self._stdout.fileno(), _READ_CHUNK_BYTES)
        if not chunk:
            raise EOFError("remote app-server stream closed")
        self._read_buffer.extend(chunk)

    def _write_bytes(self, payload: bytes) -> None:
        """向 SSH stdin 完整写入一段 bytes 并 flush。

        入参：``payload`` 是 HTTP 或 WebSocket 编码字节。
        返回：无。
        错误处理：未连接抛 EOFError；写入错误按 OSError 传播。
        副作用：写自身 SSH 子进程 stdin。
        """

        if self._stdin is None:
            raise EOFError("remote stdin is not connected")
        self._stdin.write(payload)
        self._stdin.flush()

    def _close_transport(self) -> None:
        """best-effort 释放当前流和子进程并清空传输状态。

        入参：无。
        返回：无。
        错误处理：close、wait、terminate、kill 异常均忽略。
        副作用：只关闭本实例保存的文件描述符和子进程。
        """

        process = self._process
        stdin = self._stdin
        stdout = self._stdout
        self._process = None
        self._stdin = None
        self._stdout = None
        self._read_buffer.clear()
        if stdin is not None:
            with suppress(Exception):
                stdin.close()
        if stdout is not None:
            with suppress(Exception):
                stdout.close()
        if process is None:
            return
        with suppress(Exception):
            process.wait(timeout=0.3)
        if process.poll() is None:
            with suppress(Exception):
                process.terminate()
            with suppress(Exception):
                process.wait(timeout=0.5)
        if process.poll() is None:
            with suppress(Exception):
                process.kill()
            with suppress(Exception):
                process.wait(timeout=0.5)


def _safe_remote_thread(value: Any) -> _RemoteThread | None:
    """从原始 thread object 提取不含 prompt 的最小字段。

    入参：``value`` 可能是任意 JSON 值。
    返回：合法时返回 ``_RemoteThread``；结构未知时返回 None。
    错误处理：字段转换或 Pydantic 校验失败时返回 None。
    副作用：不复制 preview/turns/items/path/source 等高敏或无关字段。
    """

    if not isinstance(value, Mapping):
        return None
    thread_id = value.get("id")
    status = value.get("status")
    if not isinstance(thread_id, str) or not isinstance(status, Mapping):
        return None
    status_type = status.get("type")
    if not isinstance(status_type, str):
        return None
    flags_value = status.get("activeFlags")
    active_flags = (
        tuple(flag for flag in flags_value if isinstance(flag, str))
        if isinstance(flags_value, list)
        else ()
    )
    try:
        return _RemoteThread(
            thread_id=thread_id,
            name=_optional_text(value.get("name")),
            cwd=_optional_text(value.get("cwd")),
            updated_at=_safe_int(value.get("updatedAt")),
            parent_thread_id=_optional_text(value.get("parentThreadId")),
            status_type=status_type,
            active_flags=active_flags,
        )
    except ValueError:
        return None


def _agent_status_from_remote_thread(
    thread: _RemoteThread,
) -> tuple[AgentStatus, str] | None:
    """把稳定 ThreadStatus 映射为 Agent Deck 状态。

    入参：``thread`` 是已脱敏远端记录。
    返回：active/等待/错误返回 ``(status, reason)``；idle/notLoaded/未知返回 None。
    错误处理：未知 active flag 不报错；不得从普通完成推断 review。
    副作用：无。
    """

    if thread.status_type == "systemError":
        return AgentStatus.ERROR, "remote system error"
    if thread.status_type != "active":
        return None
    flags = set(thread.active_flags)
    if "waitingOnApproval" in flags:
        return AgentStatus.APPROVAL_NEEDED, "remote waiting on approval"
    if "waitingOnUserInput" in flags:
        return AgentStatus.WAITING_USER, "remote waiting on user input"
    return AgentStatus.THINKING, "remote active"


def _optional_text(value: Any) -> str | None:
    """把非空字符串规范化为可选文本。

    入参：任意 JSON 值。
    返回：去空白后的字符串或 None。
    错误处理：无。
    副作用：无。
    """

    return value.strip() if isinstance(value, str) and value.strip() else None


def _safe_int(value: Any) -> int:
    """把 JSON 数字安全转换为整数时间戳。

    入参：整数、浮点数或其他 JSON 值；bool 不视为整数。
    返回：可转换时返回整数，否则返回 0。
    错误处理：不抛业务异常。
    副作用：无。
    """

    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0
