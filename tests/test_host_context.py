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
