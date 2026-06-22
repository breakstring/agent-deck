"""Codex 会话宿主上下文的数据模型。

本模块只定义宿主检测输出的不可变 Pydantic 模型和枚举，不读取进程表、tmux、
Codex App 状态或硬件。模型用于在检测层、状态层、CLI 和后续 action 层之间传递
结构化 host context，并显式区分 execution host 与 presentation client。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RuntimeKind(StrEnum):
    """描述 Codex 会话运行形态。

    入参：枚举值是稳定 JSON 字符串，用于 CLI 输出和后续状态序列化。
    返回：字符串枚举成员，可由 Pydantic 校验与序列化。
    错误处理：未知值由 Pydantic/Enum 校验失败报告。
    副作用：无；仅定义常量。
    """

    CODEX_CLI = "codex_cli"
    CODEX_APP = "codex_app"
    UNKNOWN = "unknown"


class ExecutionHostKind(StrEnum):
    """描述 Codex 进程实际运行的宿主。

    入参：枚举值表示 direct PTY、tmux pane、Codex App 或未知宿主。
    返回：字符串枚举成员。
    错误处理：未知值由 Pydantic/Enum 校验失败报告。
    副作用：无；仅定义常量。
    """

    DIRECT_PTY = "direct_pty"
    TMUX_PANE = "tmux_pane"
    CODEX_APP = "codex_app"
    UNKNOWN_PTY = "unknown_pty"
    UNKNOWN = "unknown"


class PresentationClientKind(StrEnum):
    """描述展示 execution host 的客户端类型。

    入参：枚举值表示终端 App、tmux client、Codex App window 或未知展示客户端。
    返回：字符串枚举成员。
    错误处理：未知值由 Pydantic/Enum 校验失败报告。
    副作用：无；仅定义常量。
    """

    TERMINAL_APP = "terminal_app"
    TMUX_CLIENT = "tmux_client"
    CODEX_APP_WINDOW = "codex_app_window"
    UNKNOWN = "unknown"


class ActivationStrategy(StrEnum):
    """描述后续 `focus_agent` 可使用的激活策略。

    入参：枚举值只表达策略语义，不包含 shell 命令。
    返回：字符串枚举成员。
    错误处理：未知值由 Pydantic/Enum 校验失败报告。
    副作用：无；检测层不会执行任何激活动作。
    """

    UNAVAILABLE = "unavailable"
    APP_ACTIVATE_ONLY = "app_activate_only"
    TERMINAL_ACTIVATE = "terminal_activate"
    TMUX_REATTACH_NEW_CLIENT = "tmux_reattach_new_client"
    TMUX_SELECT_EXISTING_CLIENT = "tmux_select_existing_client"


class Confidence(StrEnum):
    """描述宿主检测或激活目标的置信度。

    入参：枚举值是 high、medium、low 三档。
    返回：字符串枚举成员。
    错误处理：未知值由 Pydantic/Enum 校验失败报告。
    副作用：无；仅定义常量。
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ExecutionHostContext(BaseModel):
    """描述 Codex 会话的 execution host。

    入参：字段覆盖 direct PTY host app、tmux pane 坐标、pane TTY/PID、attached 状态等；
    未适用于当前 host kind 的字段保持 None。
    返回：不可变模型实例，可被 resolver、状态层或 CLI 读取。
    错误处理：字段类型非法时由 Pydantic 抛出 ValidationError。
    副作用：只保存内存字段，不访问进程、tmux、文件或网络。
    """

    model_config = ConfigDict(frozen=True)

    kind: ExecutionHostKind
    host_app_name: str | None = None
    host_app_path: str | None = None
    host_app_pid: int | None = None
    tmux_session_name: str | None = None
    tmux_window_id: str | None = None
    tmux_window_index: int | None = None
    tmux_pane_id: str | None = None
    tmux_pane_index: int | None = None
    pane_tty: str | None = None
    pane_pid: int | None = None
    attached: bool | None = None


class PresentationClientContext(BaseModel):
    """描述展示 execution host 的客户端。

    入参：字段覆盖终端 App 名称/路径/PID、tmux client TTY/PID/session 和置信度。
    返回：不可变模型实例，可用于 CLI 输出或后续 focus action 判断。
    错误处理：字段类型非法时由 Pydantic 抛出 ValidationError。
    副作用：只保存内存字段，不访问系统 UI 或进程表。
    """

    model_config = ConfigDict(frozen=True)

    kind: PresentationClientKind
    app_name: str | None = None
    app_path: str | None = None
    app_pid: int | None = None
    client_tty: str | None = None
    tmux_session_name: str | None = None
    confidence: Confidence = Confidence.LOW


class ActivationContext(BaseModel):
    """描述可执行的聚焦策略及其结构化目标。

    入参：`strategy` 表示后续 action 层可采取的激活方式；`target` 是结构化目标，
    不包含拼接好的 shell 命令；两个布尔字段说明是否需要 Accessibility 或启动终端。
    返回：不可变模型实例。
    错误处理：字段类型非法时由 Pydantic 抛出 ValidationError。
    副作用：只保存内存字段，不执行激活动作。
    """

    model_config = ConfigDict(frozen=True)

    strategy: ActivationStrategy
    confidence: Confidence
    target: dict[str, Any] = Field(default_factory=dict)
    requires_accessibility: bool = False
    requires_terminal_launch: bool = False


class AgentHostContext(BaseModel):
    """描述一个 Codex 会话完整的宿主检测结果。

    入参：字段覆盖 runtime kind、execution host、presentation clients、activation、
    可选 pid/TTY/cwd/thread/rollout 信息和检测时间；时间字段必须带时区。
    返回：不可变模型实例，可被 CLI JSON 输出或状态层消费。
    错误处理：字段类型非法、时间字段为 naive datetime 时由 Pydantic 抛出 ValidationError。
    副作用：只保存内存字段，不读取或写入外部状态。
    """

    model_config = ConfigDict(frozen=True)

    runtime_kind: RuntimeKind
    execution_host: ExecutionHostContext
    presentation_clients: tuple[PresentationClientContext, ...] = ()
    activation: ActivationContext
    agent_pid: int | None = None
    pid_start_time: datetime | None = None
    tty: str | None = None
    cwd: str | None = None
    thread_id: str | None = None
    rollout_path: str | None = None
    observed_at: datetime
    confidence: Confidence
    notes: tuple[str, ...] = ()

    @field_validator("observed_at", "pid_start_time")
    @classmethod
    def _ensure_timezone_aware(cls, value: datetime | None) -> datetime | None:
        """拒绝 naive datetime，避免跨时区或 PID 复用判断依赖猜测。

        入参：`value` 是 `observed_at` 或可选的 `pid_start_time`。
        返回：原始 timezone-aware datetime 或 None。
        错误处理：datetime 缺少 tzinfo 或 utcoffset 时抛 ValueError。
        副作用：无；只检查内存字段。
        """

        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("host context datetime fields must be timezone-aware")
        return value
