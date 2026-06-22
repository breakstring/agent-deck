"""Codex CLI / Codex App 会话宿主解析器。

本模块把 hook 传入的 Codex pid、进程表、tmux 快照和 Codex App active sessions 合并成
`AgentHostContext`。解析过程只读，不执行聚焦、不 attach tmux、不启动终端，也不写
Codex 本地状态。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from agent_deck.adapters.codex_app_state import CodexAppActiveSession
from agent_deck.hosts.models import (
    ActivationContext,
    ActivationStrategy,
    AgentHostContext,
    Confidence,
    ExecutionHostContext,
    ExecutionHostKind,
    PresentationClientContext,
    PresentationClientKind,
    RuntimeKind,
)
from agent_deck.hosts.processes import (
    MacOSProcessTable,
    ProcessTable,
    infer_direct_pty_host,
    process_chain,
)
from agent_deck.hosts.tmux import (
    SubprocessTmuxReader,
    TmuxClient,
    TmuxPane,
    TmuxReader,
    TmuxSnapshot,
    clients_for_session,
    find_pane_for_tty,
)


class CodexHostResolver:
    """解析 Codex 会话的宿主上下文。

    入参：构造时可注入进程表、tmux reader 或固定 tmux snapshot，便于测试；未注入时
    使用只读 macOS 进程表和 tmux subprocess reader。
    返回：实例提供 CLI 和 Codex App session 解析方法。
    错误处理：生产 reader 读取失败时 resolver 降级为 unknown，不让检测失败阻断调用方。
    副作用：构造无副作用；调用解析方法时可能只读读取进程表或 tmux 状态。
    """

    def __init__(
        self,
        *,
        process_table: ProcessTable | None = None,
        tmux_reader: TmuxReader | None = None,
        tmux_snapshot: TmuxSnapshot | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """创建 Codex host resolver。

        入参：`process_table` 是可选进程表；`tmux_reader` 是可选 tmux reader；
        `tmux_snapshot` 是测试用固定 tmux 快照，优先于 reader；`now` 返回 timezone-aware
        当前时间。
        返回：无显式返回值。
        错误处理：不主动读取外部状态，因此构造阶段不抛读取错误。
        副作用：只保存依赖引用。
        """

        self._process_table = process_table
        self._tmux_reader = tmux_reader or SubprocessTmuxReader()
        self._tmux_snapshot = tmux_snapshot
        self._now = now or (lambda: datetime.now(UTC))

    def resolve_cli(self, *, agent_pid: int, cwd: str | None = None) -> AgentHostContext:
        """解析一个 Codex CLI pid 的宿主上下文。

        入参：`agent_pid` 是 hook 捕获到的 Codex 进程 pid；`cwd` 是可选工作目录。
        返回：`AgentHostContext`；pid 缺失或读取失败时返回 low-confidence unknown。
        错误处理：进程表或 tmux 读取失败时吞掉并降级为 unknown。
        副作用：可能只读读取系统进程表和 tmux 状态。
        """

        observed_at = self._observed_at()
        table = self._read_process_table()
        if table is None:
            return self._unknown_context(
                agent_pid=agent_pid,
                cwd=cwd,
                observed_at=observed_at,
                note="process table unavailable",
            )
        process = table.get(agent_pid)
        if process is None:
            return self._unknown_context(
                agent_pid=agent_pid,
                cwd=cwd,
                observed_at=observed_at,
                note="agent pid not found",
            )

        snapshot = self._read_tmux_snapshot()
        pane = find_pane_for_tty(snapshot, process.tty)
        if pane is not None:
            return self._tmux_context(
                agent_pid=agent_pid,
                cwd=cwd,
                observed_at=observed_at,
                tty=process.tty,
                pane=pane,
                clients=clients_for_session(snapshot, pane.session_name),
            )

        chain = process_chain(table, agent_pid)
        direct_host = infer_direct_pty_host(chain)
        if direct_host is not None:
            return AgentHostContext(
                runtime_kind=RuntimeKind.CODEX_CLI,
                execution_host=ExecutionHostContext(
                    kind=ExecutionHostKind.DIRECT_PTY,
                    host_app_name=direct_host.app_name,
                    host_app_path=direct_host.app_path,
                    host_app_pid=direct_host.app_pid,
                ),
                presentation_clients=(direct_host,),
                activation=ActivationContext(
                    strategy=ActivationStrategy.APP_ACTIVATE_ONLY,
                    confidence=Confidence.MEDIUM,
                    target={
                        "app_name": direct_host.app_name,
                        "app_pid": direct_host.app_pid,
                    },
                ),
                agent_pid=agent_pid,
                pid_start_time=process.started_at,
                tty=process.tty,
                cwd=cwd or process.cwd,
                observed_at=observed_at,
                confidence=Confidence.MEDIUM,
            )

        return AgentHostContext(
            runtime_kind=RuntimeKind.CODEX_CLI,
            execution_host=ExecutionHostContext(kind=ExecutionHostKind.UNKNOWN_PTY),
            activation=ActivationContext(
                strategy=ActivationStrategy.UNAVAILABLE,
                confidence=Confidence.LOW,
            ),
            agent_pid=agent_pid,
            pid_start_time=process.started_at,
            tty=process.tty,
            cwd=cwd or process.cwd,
            observed_at=observed_at,
            confidence=Confidence.LOW,
            notes=("no tmux pane or terminal app matched",),
        )

    def resolve_app_sessions(
        self, active_sessions: Iterable[CodexAppActiveSession]
    ) -> tuple[AgentHostContext, ...]:
        """把 Codex App active sessions 映射为 host context。

        入参：`active_sessions` 是现有 Codex App state scanner 输出的近期有效会话。
        返回：每个 thread 一个 `AgentHostContext`，activation 固定为 app activate only。
        错误处理：字段非法由 Pydantic 抛出；本方法不访问文件或数据库。
        副作用：只读取传入 iterable 和当前时间。
        """

        observed_at = self._observed_at()
        contexts: list[AgentHostContext] = []
        for session in active_sessions:
            contexts.append(
                AgentHostContext(
                    runtime_kind=RuntimeKind.CODEX_APP,
                    execution_host=ExecutionHostContext(
                        kind=ExecutionHostKind.CODEX_APP,
                        host_app_name="Codex",
                    ),
                    presentation_clients=(
                        PresentationClientContext(
                            kind=PresentationClientKind.CODEX_APP_WINDOW,
                            app_name="Codex",
                            confidence=Confidence.MEDIUM,
                        ),
                    ),
                    activation=ActivationContext(
                        strategy=ActivationStrategy.APP_ACTIVATE_ONLY,
                        confidence=Confidence.MEDIUM,
                        target={"app_name": "Codex"},
                    ),
                    cwd=session.cwd,
                    thread_id=session.thread_id,
                    rollout_path=session.rollout_path,
                    observed_at=observed_at,
                    confidence=Confidence.MEDIUM,
                    notes=(session.reason,),
                )
            )
        return tuple(contexts)

    def _tmux_context(
        self,
        *,
        agent_pid: int,
        cwd: str | None,
        observed_at: datetime,
        tty: str | None,
        pane: TmuxPane,
        clients: tuple[TmuxClient, ...],
    ) -> AgentHostContext:
        """构建 tmux pane 的 host context。

        入参：字段来自 Codex 进程、匹配到的 tmux pane 和同 session clients。
        返回：`AgentHostContext`，detached 使用 reattach，新 client 存在时使用 select existing。
        错误处理：字段非法时由 Pydantic 抛出。
        副作用：无；只组装内存模型。
        """

        attached = bool(clients)
        strategy = (
            ActivationStrategy.TMUX_SELECT_EXISTING_CLIENT
            if attached
            else ActivationStrategy.TMUX_REATTACH_NEW_CLIENT
        )
        presentation_clients = tuple(
            PresentationClientContext(
                kind=PresentationClientKind.TMUX_CLIENT,
                client_tty=client.client_tty,
                app_pid=client.client_pid,
                tmux_session_name=client.session_name,
                confidence=Confidence.MEDIUM,
            )
            for client in clients
        )
        return AgentHostContext(
            runtime_kind=RuntimeKind.CODEX_CLI,
            execution_host=ExecutionHostContext(
                kind=ExecutionHostKind.TMUX_PANE,
                tmux_session_name=pane.session_name,
                tmux_window_id=pane.window_id,
                tmux_window_index=pane.window_index,
                tmux_pane_id=pane.pane_id,
                tmux_pane_index=pane.pane_index,
                pane_tty=pane.pane_tty,
                pane_pid=pane.pane_pid,
                attached=attached,
            ),
            presentation_clients=presentation_clients,
            activation=ActivationContext(
                strategy=strategy,
                confidence=Confidence.HIGH,
                target={
                    "tmux_pane_id": pane.pane_id,
                    "tmux_session_name": pane.session_name,
                    "tmux_window_id": pane.window_id,
                    "tmux_window_index": pane.window_index,
                    "tmux_pane_index": pane.pane_index,
                },
                requires_terminal_launch=not attached,
            ),
            agent_pid=agent_pid,
            tty=tty,
            cwd=cwd or pane.current_path,
            observed_at=observed_at,
            confidence=Confidence.HIGH,
        )

    def _unknown_context(
        self,
        *,
        agent_pid: int,
        cwd: str | None,
        observed_at: datetime,
        note: str,
    ) -> AgentHostContext:
        """构建 unknown 降级上下文。

        入参：`agent_pid` 是请求检测的 pid；`cwd` 是可选工作目录；`observed_at` 是检测时间；
        `note` 是降级原因。
        返回：low-confidence unknown `AgentHostContext`。
        错误处理：字段非法时由 Pydantic 抛出。
        副作用：无；只组装内存模型。
        """

        return AgentHostContext(
            runtime_kind=RuntimeKind.UNKNOWN,
            execution_host=ExecutionHostContext(kind=ExecutionHostKind.UNKNOWN),
            activation=ActivationContext(
                strategy=ActivationStrategy.UNAVAILABLE,
                confidence=Confidence.LOW,
            ),
            agent_pid=agent_pid,
            cwd=cwd,
            observed_at=observed_at,
            confidence=Confidence.LOW,
            notes=(note,),
        )

    def _read_process_table(self) -> ProcessTable | None:
        """返回注入或真实读取的进程表。

        入参：无。
        返回：`ProcessTable`；读取失败时返回 None。
        错误处理：吞掉真实进程表读取异常并让调用方降级。
        副作用：未注入时只读调用 macOS `ps`。
        """

        if self._process_table is not None:
            return self._process_table
        try:
            return MacOSProcessTable.read()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return None

    def _read_tmux_snapshot(self) -> TmuxSnapshot:
        """返回注入或真实读取的 tmux 快照。

        入参：无。
        返回：`TmuxSnapshot`，读取失败时为空快照。
        错误处理：吞掉 reader 异常并返回空快照。
        副作用：未注入固定快照时可能只读调用 tmux。
        """

        if self._tmux_snapshot is not None:
            return self._tmux_snapshot
        try:
            return self._tmux_reader.read()
        except (OSError, RuntimeError, ValueError):
            return TmuxSnapshot()

    def _observed_at(self) -> datetime:
        """返回当前检测时间。

        入参：无。
        返回：timezone-aware datetime。
        错误处理：`now` 返回 naive datetime 时抛 ValueError，避免输出模糊时间。
        副作用：调用注入的时间函数或读取当前 UTC 时间。
        """

        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("resolver clock must return timezone-aware datetime")
        return value
