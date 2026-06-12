"""Agent 状态到按钮视觉规格的渲染层映射。

本模块只负责把内部 `AgentStatus` 压缩成硬件按钮可消费的主视觉态、资产 id、
强调色、动画语义和角标信息。它不生成图片、不读取 `assets/` 文件、不访问真实
StreamDock 设备、不启动服务，也不修改状态机；后续具体 renderer 可以根据
`VisualIconSpec` 决定如何加载 GIF、生成帧或绘制 overlay。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agent_deck.core.state import AgentStatus


class VisualAgentState(StrEnum):
    """描述按钮主视觉态，而不是内部 Agent 生命周期状态。

    入参：枚举值是 renderer、API 和测试可依赖的稳定字符串。
    返回：作为字符串枚举参与 `VisualIconSpec` 校验和 JSON 序列化。
    错误处理：未知视觉态由 Enum/Pydantic 校验为非法值。
    副作用：无；声明枚举不读取资产、不访问硬件或全局状态。
    """

    NEEDS_USER = "needs_user"
    WORKING = "working"
    IDLE = "idle"
    OFFLINE = "offline"
    ERROR = "error"


class VisualAnimation(StrEnum):
    """描述 renderer 应使用的动画语义。

    入参：枚举值是抽象动画名称，不绑定某个具体 GIF、帧率或硬件刷新策略。
    返回：作为字符串枚举参与 `VisualIconSpec` 校验和 JSON 序列化。
    错误处理：未知动画由 Enum/Pydantic 校验为非法值。
    副作用：无；声明枚举不生成帧、不读取文件、不访问硬件。
    """

    NONE = "none"
    GIF_ASSET = "gif_asset"
    PULSE = "pulse"
    SWEEP = "sweep"
    FLASH = "flash"


class VisualBadge(StrEnum):
    """描述主图标上的小型语义角标。

    入参：枚举值是 renderer 可选择绘制的 overlay 名称。
    返回：作为字符串枚举参与 `VisualIconSpec` 校验和 JSON 序列化。
    错误处理：未知角标由 Enum/Pydantic 校验为非法值。
    副作用：无；声明枚举不绘制图形、不访问文件或硬件。
    """

    USER_ACTION = "user_action"
    ERROR = "error"
    SUCCESS = "success"


class VisualIconSpec(BaseModel):
    """描述一个 Agent 按钮槽位的视觉规格。

    入参：`visual_state` 是压缩后的主视觉态；`asset_id` 可以是实际资产路径或逻辑
    资产名称；`accent_color` 是 renderer 可映射到具体色值的语义颜色；
    `animation` 是动画语义；`badge` 可为空；`dimmed` 表示低亮展示；
    `priority` 表示视觉刷新/提醒优先级，数字越小越需要用户注意。
    返回：frozen Pydantic model，可被 layout、API 和 renderer 只读消费。
    错误处理：字段类型、枚举值或负优先级非法时由 Pydantic 报告。
    副作用：仅保存内存数据，不读取图片、不访问网络、硬件或文件系统。
    """

    model_config = ConfigDict(frozen=True)

    visual_state: VisualAgentState
    asset_id: str
    accent_color: str
    animation: VisualAnimation
    badge: VisualBadge | None = None
    dimmed: bool = False
    priority: int = Field(ge=0)


def resolve_visual_icon_spec(status: AgentStatus) -> VisualIconSpec:
    """把内部 Agent 状态解析成按钮主视觉规格。

    入参：`status` 是状态归约层输出的 `AgentStatus`；本函数只接受已知枚举值。
    返回：对应的 `VisualIconSpec`。当前映射保留 error 独立视觉态，将
    approval/waiting 合并为 `needs_user`，running/thinking 合并为 `working`，
    idle 复用 `assets/codex/codex.gif`，offline 复用 `assets/codex/codex.png`。
    错误处理：如果未来传入非 `AgentStatus` 值，匹配不到时抛出 ValueError，
    由调用方决定是否降级。
    副作用：无；只创建内存模型，不读取资产、不访问硬件、不修改状态机。
    """

    match status:
        case AgentStatus.APPROVAL_NEEDED | AgentStatus.WAITING_USER:
            return VisualIconSpec(
                visual_state=VisualAgentState.NEEDS_USER,
                asset_id="codex-needs-user",
                accent_color="amber",
                animation=VisualAnimation.PULSE,
                badge=VisualBadge.USER_ACTION,
                priority=0,
            )
        case AgentStatus.ERROR:
            return VisualIconSpec(
                visual_state=VisualAgentState.ERROR,
                asset_id="codex-error",
                accent_color="red",
                animation=VisualAnimation.PULSE,
                badge=VisualBadge.ERROR,
                priority=1,
            )
        case AgentStatus.RUNNING_TOOL | AgentStatus.THINKING:
            return VisualIconSpec(
                visual_state=VisualAgentState.WORKING,
                asset_id="codex-working",
                accent_color="cyan",
                animation=VisualAnimation.SWEEP,
                priority=2,
            )
        case AgentStatus.COMPLETED_RECENTLY:
            return VisualIconSpec(
                visual_state=VisualAgentState.IDLE,
                asset_id="assets/codex/codex.gif",
                accent_color="green",
                animation=VisualAnimation.FLASH,
                badge=VisualBadge.SUCCESS,
                priority=3,
            )
        case AgentStatus.IDLE:
            return VisualIconSpec(
                visual_state=VisualAgentState.IDLE,
                asset_id="assets/codex/codex.gif",
                accent_color="green",
                animation=VisualAnimation.GIF_ASSET,
                priority=4,
            )
        case AgentStatus.OFFLINE:
            return VisualIconSpec(
                visual_state=VisualAgentState.OFFLINE,
                asset_id="assets/codex/codex.png",
                accent_color="gray",
                animation=VisualAnimation.NONE,
                dimmed=True,
                priority=5,
            )

    raise ValueError(f"unsupported agent status for visual icon: {status!r}")
