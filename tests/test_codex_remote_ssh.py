"""远端 ChatGPT/Codex App SSH 只读观察器的协议边界与状态映射测试。

这些测试不访问真实 SSH、网络或 Codex；它们直接验证 host/command 白名单、敏感字段丢弃、
稳定 ThreadStatus 映射、active->idle 完成窗口和 host-aware daemon 聚合。
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_deck.adapters.codex_remote_ssh import (
    CodexRemoteSshObserver,
    CodexRemoteSshDiscoverySnapshot,
    CodexRemoteSshEnabledHost,
    CodexRemoteSshError,
    CodexRemoteSshSnapshot,
    _RemoteThread,
    build_codex_remote_ssh_command,
    codex_remote_host_id,
    discover_enabled_codex_remote_ssh_hosts,
    validate_ssh_host_alias,
)
from agent_deck.core.state import AgentStatus
from agent_deck.server.app import DaemonPollerConfig, create_app


def test_ssh_host_validation_and_command_are_argv_only() -> None:
    """校验具体 host 白名单，并确认远端命令是固定独立 argv。

    入参：无。
    返回：无；断言合法 alias 可构造命令，option-like/shell 文本被拒绝。
    错误处理：预期 ValueError 由断言捕获。
    副作用：不启动 SSH。
    """

    command = build_codex_remote_ssh_command(
        "user@minibox.zhiyu.ts",
        connect_timeout_seconds=3.7,
    )

    assert command[-2:] == ("user@minibox.zhiyu.ts", "codex app-server proxy")
    assert command[:3] == ("ssh", "-T", "-o")
    assert "BatchMode=yes" in command
    assert "ConnectTimeout=3" in command
    assert codex_remote_host_id("minibox") != codex_remote_host_id("devbox")

    for invalid in ("", "-oProxyCommand=bad", "host;touch /tmp/x", "host name"):
        try:
            validate_ssh_host_alias(invalid)
        except ValueError:
            pass
        else:  # pragma: no cover - 失败时给出比 pytest.raises 更直接的候选值。
            raise AssertionError(f"invalid SSH host was accepted: {invalid!r}")


def test_discovery_only_accepts_managed_auto_connect_true_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证自动发现只取 Settings 已管理且明确 auto-connect=true 的 SSH 主机。

    入参：``tmp_path`` 保存隔离状态；``monkeypatch`` 记录实际读取的路径。
    返回：无；断言 false/missing、relay、历史项目和 selected host 都不会进入结果。
    错误处理：无。
    副作用：只写 pytest 临时文件，不连接 SSH。
    """

    state_path = tmp_path / ".codex-global-state.json"
    state_path.write_text(
        json.dumps(
            {
                "codex-managed-remote-connections": [
                    {
                        "hostId": "remote-ssh-discovered:minibox",
                        "alias": "minibox",
                        "displayName": "Mini Box",
                    },
                    {
                        "hostId": "remote-ssh-discovered:disabled",
                        "alias": "disabled",
                        "displayName": "Disabled Box",
                    },
                    {
                        "hostId": "remote-ssh-discovered:missing-state",
                        "alias": "missing-state",
                        "displayName": "Missing State Box",
                    },
                    {
                        "hostId": "openai-relay:cloud",
                        "alias": "relay",
                        "displayName": "Relay",
                    },
                ],
                "remote-connection-auto-connect-by-host-id": {
                    "remote-ssh-discovered:minibox": True,
                    "remote-ssh-discovered:disabled": False,
                    "openai-relay:cloud": True,
                },
                "selected-remote-host-id": "remote-ssh-discovered:disabled",
                "remote-projects": {
                    "remote-ssh-discovered:history-only": [{"cwd": "/repo"}]
                },
            }
        ),
        encoding="utf-8",
    )
    ssh_config = tmp_path / ".ssh" / "config"
    ssh_config.parent.mkdir()
    ssh_config.write_text("Host config-only\n  HostName 192.0.2.1\n", encoding="utf-8")
    original_read_text = Path.read_text
    read_paths: list[Path] = []

    def recording_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        """记录 discovery 实际读取的文件并调用标准库实现。

        入参：``path`` 是 Path 实例，其余参数原样透传。
        返回：文件文本。
        错误处理：标准库异常原样传播。
        副作用：向 ``read_paths`` 追加一次路径。
        """

        read_paths.append(path)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording_read_text)

    snapshot = discover_enabled_codex_remote_ssh_hosts(state_path=state_path)

    assert read_paths == [state_path]
    assert [host.alias for host in snapshot.enabled_hosts] == ["minibox"]
    assert snapshot.managed_ssh_count == 3
    assert snapshot.auto_connect_disabled_count == 2
    assert snapshot.ignored_non_ssh_count == 1
    assert "disabled" not in {host.alias for host in snapshot.enabled_hosts}
    assert "missing-state" not in {host.alias for host in snapshot.enabled_hosts}
    assert "config-only" not in {host.alias for host in snapshot.enabled_hosts}


def test_discovery_rejects_unknown_settings_shape_without_fallback(
    tmp_path: Path,
) -> None:
    """验证关键 Settings 结构异常时 fail-closed，不从其他本地来源猜测。

    入参：``tmp_path`` 保存非法 global state。
    返回：无；断言 reader 抛 ValueError。
    错误处理：预期异常由 pytest 捕获。
    副作用：只写 pytest 临时文件。
    """

    state_path = tmp_path / ".codex-global-state.json"
    state_path.write_text(
        json.dumps(
            {
                "codex-managed-remote-connections": {},
                "remote-connection-auto-connect-by-host-id": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="managed remote connections"):
        discover_enabled_codex_remote_ssh_hosts(state_path=state_path)


def test_thread_list_projection_discards_preview_and_maps_stable_statuses() -> None:
    """验证 thread/list 只保留安全 metadata，并映射等待、运行、错误和 child 过滤。

    入参：无。
    返回：无；断言快照 JSON 不含 preview secret 且状态映射正确。
    错误处理：无。
    副作用：只修改 observer 的内存过渡表，不建立 SSH。
    """

    observer = CodexRemoteSshObserver("minibox.zhiyu.ts")
    snapshot = observer._snapshot_from_thread_list(  # noqa: SLF001 - 精确测试脱敏边界。
        {
            "data": [
                _raw_thread(
                    "approval",
                    "active",
                    active_flags=["waitingOnApproval"],
                    preview="TOP-SECRET-APPROVAL",
                ),
                _raw_thread(
                    "input",
                    "active",
                    active_flags=["waitingOnUserInput"],
                    preview="TOP-SECRET-INPUT",
                ),
                _raw_thread("running", "active", preview="TOP-SECRET-RUNNING"),
                _raw_thread("error", "systemError", preview="TOP-SECRET-ERROR"),
                _raw_thread("idle", "idle", preview="TOP-SECRET-IDLE"),
                _raw_thread(
                    "child",
                    "active",
                    parent_thread_id="running",
                    preview="TOP-SECRET-CHILD",
                ),
            ]
        }
    )

    statuses = {session.thread_id: session.status for session in snapshot.sessions}
    assert statuses == {
        "approval": AgentStatus.APPROVAL_NEEDED,
        "input": AgentStatus.WAITING_USER,
        "running": AgentStatus.THINKING,
        "error": AgentStatus.ERROR,
    }
    dumped = snapshot.model_dump_json()
    assert "TOP-SECRET" not in dumped
    assert snapshot.considered_thread_count == 5
    assert snapshot.status_counts == {"active": 3, "systemError": 1, "idle": 1}
    assert all(session.rollout_path is None for session in snapshot.sessions)
    assert all(session.is_remote for session in snapshot.sessions)


def test_active_to_idle_emits_bounded_completed_feedback() -> None:
    """验证 active->idle 只产生有限的 COMPLETED_RECENTLY，不会推断 review。

    入参：无。
    返回：无；断言完成窗口内保留，窗口后恢复为空。
    错误处理：无。
    副作用：更新 observer 的内存 previous/deadline。
    """

    observer = CodexRemoteSshObserver(
        "minibox",
        completed_feedback_seconds=10,
    )
    active = _RemoteThread(
        thread_id="thread-1",
        name="远端任务",
        cwd="/repo",
        updated_at=100,
        status_type="active",
    )
    idle = active.model_copy(update={"status_type": "idle", "updated_at": 101})

    first = observer._sessions_from_threads(  # noqa: SLF001
        [active],
        monotonic_now=20,
    )
    completed = observer._sessions_from_threads(  # noqa: SLF001
        [idle],
        monotonic_now=21,
    )
    holding = observer._sessions_from_threads(  # noqa: SLF001
        [idle],
        monotonic_now=29,
    )
    expired = observer._sessions_from_threads(  # noqa: SLF001
        [idle],
        monotonic_now=32,
    )

    assert first[0].status == AgentStatus.THINKING
    assert completed[0].status == AgentStatus.COMPLETED_RECENTLY
    assert holding[0].status == AgentStatus.COMPLETED_RECENTLY
    assert expired == ()
    assert "review" not in completed[0].reason.casefold()


def test_repeated_connection_failure_enters_bounded_backoff() -> None:
    """验证单次读取只重试一次，随后进入本地重连退避。

    入参：无。
    返回：无；断言立即第二次读取不会再次启动 SSH。
    错误处理：预期 ``CodexRemoteSshError`` 由 pytest 捕获。
    副作用：只调用一个始终抛 OSError 的 fake process factory。
    """

    attempts = 0

    def failing_process_factory(*_args: object, **_kwargs: object) -> object:
        """记录启动次数并模拟 ssh executable 失败。

        入参：忽略 Popen argv/kwargs。
        返回：不会返回。
        错误处理：总是抛 OSError。
        副作用：递增内存计数。
        """

        nonlocal attempts
        attempts += 1
        raise OSError("sensitive executable detail")

    observer = CodexRemoteSshObserver(
        "minibox",
        process_factory=failing_process_factory,  # type: ignore[arg-type]
    )

    with pytest.raises(CodexRemoteSshError, match="无法读取"):
        observer.read_snapshot()
    assert attempts == 2
    with pytest.raises(CodexRemoteSshError, match="退避"):
        observer.read_snapshot()
    assert attempts == 2


def test_daemon_poller_merges_remote_session_with_host_aware_identity() -> None:
    """验证 daemon 启动轮询把远端状态并入 store 和宠物可识别 focus target。

    入参：无。
    返回：无；断言 agent key 含 host namespace，诊断不含敏感 thread 内容。
    错误处理：无。
    副作用：只启动 TestClient lifespan 和 fake observer，不访问 SSH/硬件。
    """

    observer = _FakeObserver()
    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_remote_ssh_enabled=True,
            poll_on_start=True,
        ),
        codex_remote_ssh_hosts_reader=lambda: _discovery("minibox"),
        codex_remote_ssh_observer_factory=lambda _host: observer,
    )

    with TestClient(app) as client:
        status = client.get("/status").json()

    assert status["agents"][0]["agent_key"] == (
        f"codex:remote-ssh:{observer.host_id}:remote-thread"
    )
    assert status["agents"][0]["focus_target"] == (
        f"codex-app:remote-ssh:{observer.host_id}:remote-thread"
    )
    assert status["agents"][0]["display_name"] == "minibox · 远端任务"
    remote_status = status["pollers"]["codex_remote_ssh"]
    assert remote_status["associated_agent_count"] == 1
    assert remote_status["hosts"][0]["active_session_count"] == 1
    assert observer.closed is True
    assert "preview" not in str(remote_status).casefold()


def test_remote_failure_clears_only_its_host_after_stale_window() -> None:
    """验证持续失联只清理对应 SSH host 的 observer-owned 状态。

    入参：无。
    返回：无；断言 stale 前保留、窗口后移除并恢复无覆盖状态。
    错误处理：无。
    副作用：只操作 TestClient runtime 内存，不建立 SSH。
    """

    observer = _FakeObserver()
    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_remote_ssh_enabled=True,
            poll_on_start=True,
        ),
        codex_remote_ssh_hosts_reader=lambda: _discovery("minibox"),
        codex_remote_ssh_observer_factory=lambda _host: observer,
    )

    with TestClient(app) as client:
        first_status = client.get("/status").json()
        last_success_text = first_status["pollers"]["codex_remote_ssh"]["hosts"][0][
            "last_success_at"
        ]
        last_success = datetime.fromisoformat(last_success_text.replace("Z", "+00:00"))
        runtime = app.state.runtime
        runtime.mark_codex_remote_ssh_poll_error(
            host=observer.host,
            host_id=observer.host_id,
            error=TimeoutError("ignored detail"),
            polled_at=last_success + timedelta(seconds=5),
            stale_after_seconds=20,
        )
        assert len(client.get("/status").json()["agents"]) == 1

        runtime.mark_codex_remote_ssh_poll_error(
            host=observer.host,
            host_id=observer.host_id,
            error=TimeoutError("ignored detail"),
            polled_at=last_success + timedelta(seconds=21),
            stale_after_seconds=20,
        )
        cleared_status = client.get("/status").json()

    assert cleared_status["agents"] == []
    host_status = cleared_status["pollers"]["codex_remote_ssh"]["hosts"][0]
    assert host_status["last_error"] == "TimeoutError"
    assert "ignored detail" not in str(host_status)


def test_two_hosts_with_same_thread_id_do_not_collide() -> None:
    """验证相同远端 thread id 在不同 SSH host 上形成两个独立 agent。

    入参：无。
    返回：无；断言两个 host-aware key 均存在。
    错误处理：无。
    副作用：仅运行两个 fake observer 的 TestClient lifespan。
    """

    first = _FakeObserver()
    second = _FakeObserver()
    second.host = "devbox"
    second.host_id = codex_remote_host_id(second.host)
    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_remote_ssh_enabled=True,
            poll_on_start=True,
        ),
        codex_remote_ssh_hosts_reader=lambda: _discovery("minibox", "devbox"),
        codex_remote_ssh_observer_factory=lambda host: {
            "minibox": first,
            "devbox": second,
        }[host.alias],
    )

    with TestClient(app) as client:
        status = client.get("/status").json()

    agent_keys = {agent["agent_key"] for agent in status["agents"]}
    assert agent_keys == {
        f"codex:remote-ssh:{first.host_id}:remote-thread",
        f"codex:remote-ssh:{second.host_id}:remote-thread",
    }
    assert status["pollers"]["codex_remote_ssh"]["associated_agent_count"] == 2


def test_daemon_closes_observer_when_chatgpt_connection_is_disabled() -> None:
    """验证 Settings 关闭 connection 后 daemon 无需重启即可停止观察并清理状态。

    入参：无。
    返回：无；断言动态发现变空后 observer 被关闭、agent 被移除且诊断显示零启用主机。
    错误处理：轮询未在限时内收敛时由断言失败。
    副作用：只启动 TestClient 后台循环与 fake observer，不访问 SSH。
    """

    observer = _FakeObserver()
    enabled = True

    def read_enabled_hosts() -> CodexRemoteSshDiscoverySnapshot:
        """返回测试控制的 ChatGPT Settings 快照。

        入参：无。
        返回：enabled 为真时包含 minibox，否则为空。
        错误处理：无。
        副作用：只读取闭包布尔值。
        """

        return _discovery("minibox") if enabled else _discovery()

    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_remote_ssh_enabled=True,
            codex_remote_ssh_interval_seconds=0.01,
            poll_on_start=True,
        ),
        codex_remote_ssh_hosts_reader=read_enabled_hosts,
        codex_remote_ssh_observer_factory=lambda _host: observer,
    )

    with TestClient(app) as client:
        assert len(client.get("/status").json()["agents"]) == 1
        enabled = False
        status = _wait_for_remote_agent_count(client, expected=0)

        assert observer.closed is True
        assert status["pollers"]["codex_remote_ssh"]["discovery"][
            "enabled_host_count"
        ] == 0
        assert status["pollers"]["codex_remote_ssh"]["hosts"] == []


def test_daemon_fails_closed_when_chatgpt_settings_cannot_be_read() -> None:
    """验证 Settings 读取失败会关闭既有 observer，而不是沿用历史主机继续连接。

    入参：无。
    返回：无；断言发现异常后 observer/agent 被清理且只暴露异常类型。
    错误处理：轮询未在限时内收敛时由断言失败。
    副作用：只启动 TestClient 后台循环与 fake observer。
    """

    observer = _FakeObserver()
    readable = True

    def read_enabled_hosts() -> CodexRemoteSshDiscoverySnapshot:
        """先返回启用主机，再模拟 global state 不可读。

        入参：无。
        返回：可读阶段返回 minibox 快照。
        错误处理：不可读阶段抛 FileNotFoundError。
        副作用：只读取闭包布尔值。
        """

        if not readable:
            raise FileNotFoundError("sensitive local path")
        return _discovery("minibox")

    app = create_app(
        poller_config=DaemonPollerConfig(
            codex_remote_ssh_enabled=True,
            codex_remote_ssh_interval_seconds=0.01,
            poll_on_start=True,
        ),
        codex_remote_ssh_hosts_reader=read_enabled_hosts,
        codex_remote_ssh_observer_factory=lambda _host: observer,
    )

    with TestClient(app) as client:
        assert len(client.get("/status").json()["agents"]) == 1
        readable = False
        status = _wait_for_remote_agent_count(client, expected=0)

        discovery = status["pollers"]["codex_remote_ssh"]["discovery"]
        assert observer.closed is True
        assert discovery["last_error"] == "FileNotFoundError"
        assert "sensitive local path" not in str(discovery)


def _discovery(*aliases: str) -> CodexRemoteSshDiscoverySnapshot:
    """构造只含明确启用 SSH aliases 的 ChatGPT Settings 快照。

    入参：``aliases`` 是测试希望视为 managed 且 auto-connect=true 的主机。
    返回：带 UTC 时间和对应 enabled host 模型的发现快照。
    错误处理：非法 alias 由 Pydantic 或生产 observer factory 测试覆盖。
    副作用：无。
    """

    return CodexRemoteSshDiscoverySnapshot(
        observed_at=datetime.now(UTC),
        state_path="/test/.codex-global-state.json",
        enabled_hosts=tuple(
            CodexRemoteSshEnabledHost(
                chatgpt_host_id=f"remote-ssh-discovered:{alias}",
                alias=alias,
                display_name=alias,
            )
            for alias in aliases
        ),
        managed_ssh_count=len(aliases),
    )


def _wait_for_remote_agent_count(
    client: TestClient,
    *,
    expected: int,
) -> dict[str, Any]:
    """等待短周期 fake daemon 收敛到指定远端 agent 数。

    入参：``client`` 是运行中的 TestClient；``expected`` 是预期 agent 数。
    返回：最后一次满足条件的 status JSON。
    错误处理：1 秒内未收敛时抛 AssertionError 并附最后状态。
    副作用：最多读取 100 次本地 TestClient status，并短暂 sleep。
    """

    status: dict[str, Any] = {}
    for _attempt in range(100):
        status = client.get("/status").json()
        if len(status["agents"]) == expected:
            return status
        time.sleep(0.01)
    raise AssertionError(f"remote agent count did not reach {expected}: {status}")


def _raw_thread(
    thread_id: str,
    status_type: str,
    *,
    active_flags: list[str] | None = None,
    parent_thread_id: str | None = None,
    preview: str,
) -> dict[str, Any]:
    """构造包含故意敏感 preview 的 app-server thread fixture。

    入参：thread id、状态、可选 flags/parent 和必须显式给出的 preview。
    返回：接近 thread/list wire shape 的 dict。
    错误处理：无。
    副作用：无。
    """

    return {
        "id": thread_id,
        "name": f"name-{thread_id}",
        "preview": preview,
        "cwd": "/repo",
        "updatedAt": 100,
        "parentThreadId": parent_thread_id,
        "status": {
            "type": status_type,
            "activeFlags": active_flags or [],
        },
        "turns": [{"items": [{"text": "TOP-SECRET-TURN"}]}],
    }


class _FakeObserver:
    """为 daemon 测试提供一个无 I/O 的远端 observer。

    入参：无。
    返回：read_snapshot 始终返回单个 waiting session。
    错误处理：无。
    副作用：close 只设置布尔标记。
    """

    host = "minibox"
    host_id = codex_remote_host_id(host)

    def __init__(self) -> None:
        """初始化 close 诊断标记。

        入参：无。
        返回：无。
        错误处理：无。
        副作用：无。
        """

        self.closed = False

    def read_snapshot(self) -> CodexRemoteSshSnapshot:
        """返回一个已脱敏的远端 waiting session。

        入参：无。
        返回：固定 ``CodexRemoteSshSnapshot``。
        错误处理：无。
        副作用：无。
        """

        from agent_deck.adapters.codex_app_state import CodexAppActiveSession

        observed_at = datetime.now(UTC)
        return CodexRemoteSshSnapshot(
            host=self.host,
            host_id=self.host_id,
            observed_at=observed_at,
            considered_thread_count=1,
            status_counts={"active": 1},
            sessions=(
                CodexAppActiveSession(
                    thread_id="remote-thread",
                    title="远端任务",
                    cwd="/repo",
                    rollout_path=None,
                    updated_at=int(observed_at.timestamp()),
                    status=AgentStatus.WAITING_USER,
                    reason="remote waiting on user input",
                    thread_source="vscode",
                    execution_host_id=self.host_id,
                    execution_host_label=self.host,
                    is_remote=True,
                ),
            ),
        )

    def close(self) -> None:
        """记录 daemon shutdown 已释放 observer。

        入参：无。
        返回：无。
        错误处理：无。
        副作用：设置 ``closed=True``。
        """

        self.closed = True
