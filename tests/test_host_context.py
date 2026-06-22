"""Codex 会话宿主上下文模型测试。

这些测试只构造内存模型，不读取真实进程、tmux、Codex App 状态或硬件。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agent_deck.hosts.models import (
    ActivationContext,
    ActivationStrategy,
    AgentHostContext,
    Confidence,
    ExecutionHostContext,
    ExecutionHostKind,
    RuntimeKind,
)
from agent_deck.hosts.codex import CodexHostResolver
from agent_deck.hosts.processes import (
    ProcessInfo,
    StaticProcessTable,
    infer_direct_pty_host,
    process_chain,
)
from agent_deck.hosts.tmux import (
    TmuxClient,
    TmuxPane,
    TmuxSnapshot,
    clients_for_session,
    find_pane_for_tty,
)


def test_agent_host_context_accepts_tmux_detached_target() -> None:
    """验证 tmux detached 会话能表达 reattach 激活策略。

    入参：无；测试内构造固定 timezone-aware 时间与 tmux pane 字段。
    返回：无返回值；断言通过代表模型字段和枚举值可序列化。
    错误处理：字段缺失或枚举非法时由 pytest 报告。
    副作用：只创建内存 Pydantic model。
    """

    observed_at = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)
    context = AgentHostContext(
        runtime_kind=RuntimeKind.CODEX_CLI,
        execution_host=ExecutionHostContext(
            kind=ExecutionHostKind.TMUX_PANE,
            tmux_session_name="agent",
            tmux_window_id="@1",
            tmux_window_index=0,
            tmux_pane_id="%7",
            tmux_pane_index=1,
            pane_tty="/dev/ttys006",
            pane_pid=90077,
            attached=False,
        ),
        activation=ActivationContext(
            strategy=ActivationStrategy.TMUX_REATTACH_NEW_CLIENT,
            confidence=Confidence.HIGH,
            target={"tmux_pane_id": "%7", "tmux_session_name": "agent"},
            requires_terminal_launch=True,
        ),
        agent_pid=73879,
        tty="ttys006",
        observed_at=observed_at,
        confidence=Confidence.HIGH,
    )

    payload = context.model_dump(mode="json")
    assert payload["runtime_kind"] == "codex_cli"
    assert payload["execution_host"]["kind"] == "tmux_pane"
    assert payload["activation"]["strategy"] == "tmux_reattach_new_client"


def test_agent_host_context_rejects_naive_observed_at() -> None:
    """验证宿主检测时间必须带时区。

    入参：无；测试内传入 naive datetime。
    返回：无返回值；断言通过代表模型拒绝模糊时间。
    错误处理：未抛 ValidationError 时由 pytest 报告。
    副作用：只创建内存 Pydantic model。
    """

    with pytest.raises(ValidationError):
        AgentHostContext(
            runtime_kind=RuntimeKind.UNKNOWN,
            execution_host=ExecutionHostContext(kind=ExecutionHostKind.UNKNOWN),
            activation=ActivationContext(
                strategy=ActivationStrategy.UNAVAILABLE,
                confidence=Confidence.LOW,
            ),
            observed_at=datetime(2026, 6, 22, 8, 0),
            confidence=Confidence.LOW,
        )


def test_process_chain_finds_otty_direct_pty_host() -> None:
    """验证 Codex CLI 进程可通过父进程链归因到 Otty direct PTY。

    入参：无；测试内构造 codex -> zsh -> login -> Otty 的静态进程表。
    返回：无返回值；断言通过代表直接 PTY 宿主可被识别。
    错误处理：链路顺序或 host 推断不符合预期时由 pytest 报告。
    副作用：只读取测试内存进程表。
    """

    table = StaticProcessTable(
        {
            15010: ProcessInfo(
                pid=15010,
                ppid=11910,
                command="codex",
                args=("codex", "resume"),
                tty="ttys003",
            ),
            11910: ProcessInfo(
                pid=11910,
                ppid=11904,
                command="-zsh",
                args=("-zsh",),
                tty="ttys003",
            ),
            11904: ProcessInfo(
                pid=11904,
                ppid=16260,
                command="/usr/bin/login",
                args=("/usr/bin/login",),
                tty="ttys003",
            ),
            16260: ProcessInfo(
                pid=16260,
                ppid=1,
                command="/Applications/Otty.app/Contents/MacOS/Otty",
                args=("/Applications/Otty.app/Contents/MacOS/Otty",),
                tty=None,
            ),
        }
    )

    chain = process_chain(table, 15010)
    host = infer_direct_pty_host(chain)

    assert [item.pid for item in chain] == [15010, 11910, 11904, 16260]
    assert host is not None
    assert host.app_name == "Otty"
    assert host.app_pid == 16260
    assert host.confidence == Confidence.MEDIUM


def test_tmux_snapshot_detects_detached_pane_by_tty() -> None:
    """验证 tmux pane 可通过 pane_tty 绑定 Codex CLI TTY，并识别 detached。

    入参：无；测试内构造没有 client 的 tmux snapshot。
    返回：无返回值；断言通过代表 detached pane 可被匹配。
    错误处理：pane 匹配失败时由 pytest 报告。
    副作用：只读取测试内存 snapshot。
    """

    snapshot = TmuxSnapshot(
        panes=(
            TmuxPane(
                pane_id="%7",
                pane_tty="/dev/ttys006",
                pane_pid=90077,
                session_name="agent",
                window_id="@1",
                window_index=0,
                pane_index=1,
                current_path="/Users/kenn/Projects/agent-deck",
            ),
        ),
        clients=(),
    )

    pane = find_pane_for_tty(snapshot, "ttys006")

    assert pane is not None
    assert pane.pane_id == "%7"
    assert clients_for_session(snapshot, "agent") == ()


def test_tmux_snapshot_lists_attached_clients_for_session() -> None:
    """验证 attached tmux session 能列出 presentation clients。

    入参：无；测试内构造一个 pane 和一个 client。
    返回：无返回值；断言通过代表 client 可按 session 归组。
    错误处理：client 过滤错误时由 pytest 报告。
    副作用：只读取测试内存 snapshot。
    """

    snapshot = TmuxSnapshot(
        panes=(
            TmuxPane(
                pane_id="%7",
                pane_tty="/dev/ttys006",
                pane_pid=90077,
                session_name="agent",
                window_id="@1",
                window_index=0,
                pane_index=1,
                current_path="/Users/kenn/Projects/agent-deck",
            ),
        ),
        clients=(
            TmuxClient(
                client_tty="/dev/ttys010",
                client_pid=16260,
                session_name="agent",
                client_activity=1782111632,
            ),
        ),
    )

    clients = clients_for_session(snapshot, "agent")

    assert len(clients) == 1
    assert clients[0].client_pid == 16260


def test_codex_resolver_prefers_tmux_pane_over_terminal_app() -> None:
    """验证 Codex CLI 在 tmux pane 中时 focus target 优先使用 tmux。

    入参：无；测试内构造 Codex 进程链和 tmux snapshot。
    返回：无返回值；断言通过代表 resolver 不把 Otty 当成唯一宿主事实。
    错误处理：runtime、execution host 或 activation 策略错误时由 pytest 报告。
    副作用：只读取测试 fake process table 和 fake tmux snapshot。
    """

    table = StaticProcessTable(
        {
            73879: ProcessInfo(
                pid=73879,
                ppid=90077,
                command="codex",
                args=("codex", "resume"),
                tty="ttys006",
            ),
            90077: ProcessInfo(
                pid=90077,
                ppid=90072,
                command="-zsh",
                args=("-zsh",),
                tty="ttys006",
            ),
            90072: ProcessInfo(
                pid=90072,
                ppid=16260,
                command="/usr/bin/login",
                args=("/usr/bin/login",),
                tty="ttys006",
            ),
            16260: ProcessInfo(
                pid=16260,
                ppid=1,
                command="/Applications/Otty.app/Contents/MacOS/Otty",
                args=("/Applications/Otty.app/Contents/MacOS/Otty",),
                tty=None,
            ),
        }
    )
    snapshot = TmuxSnapshot(
        panes=(
            TmuxPane(
                pane_id="%7",
                pane_tty="/dev/ttys006",
                pane_pid=90077,
                session_name="agent",
                window_id="@1",
                window_index=0,
                pane_index=1,
                current_path="/repo",
            ),
        ),
        clients=(),
    )

    context = CodexHostResolver(
        process_table=table, tmux_snapshot=snapshot
    ).resolve_cli(agent_pid=73879, cwd="/repo")

    assert context.runtime_kind == RuntimeKind.CODEX_CLI
    assert context.execution_host.kind == ExecutionHostKind.TMUX_PANE
    assert context.execution_host.tmux_pane_id == "%7"
    assert context.activation.strategy == ActivationStrategy.TMUX_REATTACH_NEW_CLIENT
    assert context.presentation_clients == ()


def test_codex_resolver_marks_missing_pid_unknown() -> None:
    """验证缺失 agent_pid 时 resolver 降级为 unknown。

    入参：无；测试内构造空进程表。
    返回：无返回值；断言通过代表检测失败不会伪造 focus target。
    错误处理：降级字段不符合预期时由 pytest 报告。
    副作用：只读取测试 fake process table。
    """

    context = CodexHostResolver(
        process_table=StaticProcessTable({}), tmux_snapshot=TmuxSnapshot()
    ).resolve_cli(agent_pid=999999)

    assert context.runtime_kind == RuntimeKind.UNKNOWN
    assert context.execution_host.kind == ExecutionHostKind.UNKNOWN
    assert context.activation.strategy == ActivationStrategy.UNAVAILABLE
    assert context.confidence == Confidence.LOW
