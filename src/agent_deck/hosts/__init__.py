"""Agent Deck 会话宿主检测包。

本包提供只读宿主上下文模型、进程表读取、tmux 探测和 Codex resolver。它不执行
focus、不启动终端、不写 Codex 配置或本地状态；真实副作用只发生在调用生产 reader
读取进程表或 tmux 状态时。
"""

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
from agent_deck.hosts.codex import CodexHostResolver

__all__ = [
    "ActivationContext",
    "ActivationStrategy",
    "AgentHostContext",
    "CodexHostResolver",
    "Confidence",
    "ExecutionHostContext",
    "ExecutionHostKind",
    "PresentationClientContext",
    "PresentationClientKind",
    "RuntimeKind",
]
