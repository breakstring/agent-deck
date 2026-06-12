"""渲染层视觉状态解析的测试。

这些测试只验证内部 `AgentStatus` 到按钮视觉规格的纯内存映射，不生成图片、
不读取真实图标文件、不访问硬件、不启动服务，也不执行网络 I/O。测试失败代表
renderer 将无法稳定区分“需要用户处理”“正在工作”“空闲”“离线”和“错误”。
"""

from __future__ import annotations

import pytest

from agent_deck.core.state import AgentStatus
from agent_deck.rendering.visuals import (
    VisualAgentState,
    VisualAnimation,
    VisualBadge,
    resolve_visual_icon_spec,
)


@pytest.mark.parametrize(
    ("status", "expected_priority"),
    (
        (AgentStatus.APPROVAL_NEEDED, 0),
        (AgentStatus.WAITING_USER, 0),
    ),
)
def test_user_intervention_statuses_share_needs_user_visual(
    status: AgentStatus,
    expected_priority: int,
) -> None:
    """需要用户干预的状态共享黄色强提醒视觉规格。

    入参：`status` 来自参数化用例；`expected_priority` 是 renderer 排队时应使用的高优先级。
    返回：无返回值；断言通过表示 approval 和 waiting user 已统一到同一主视觉态。
    错误处理：映射错误、资产 id 错误或动画选择错误由 pytest 断言报告。
    副作用：只创建内存模型，不读取资产、不访问硬件。
    """

    spec = resolve_visual_icon_spec(status)

    assert spec.visual_state == VisualAgentState.NEEDS_USER
    assert spec.base_asset_id == "assets/codex/codex.gif"
    assert spec.asset_id == "generated/codex/needs_user"
    assert spec.variant_id == "needs_user"
    assert spec.accent_color == "amber"
    assert spec.animation == VisualAnimation.PULSE
    assert spec.badge == VisualBadge.USER_ACTION
    assert spec.priority == expected_priority


@pytest.mark.parametrize(
    "status",
    (AgentStatus.RUNNING_TOOL, AgentStatus.THINKING),
)
def test_active_work_statuses_share_working_visual(status: AgentStatus) -> None:
    """正在工具执行或模型推理的状态共享工作中视觉规格。

    入参：`status` 来自参数化用例。
    返回：无返回值；断言通过表示 running_tool 和 thinking 已统一为同一主视觉态。
    错误处理：映射错误、资产 id 错误或动画选择错误由 pytest 断言报告。
    副作用：只创建内存模型，不读取资产、不访问硬件。
    """

    spec = resolve_visual_icon_spec(status)

    assert spec.visual_state == VisualAgentState.WORKING
    assert spec.base_asset_id == "assets/codex/codex.gif"
    assert spec.asset_id == "generated/codex/working"
    assert spec.variant_id == "working"
    assert spec.accent_color == "cyan"
    assert spec.animation == VisualAnimation.SWEEP
    assert spec.badge is None
    assert spec.priority == 2


def test_idle_uses_existing_codex_gif() -> None:
    """空闲状态使用已归档的 Codex 动态图标。

    入参：无；固定解析 `AgentStatus.IDLE`。
    返回：无返回值；断言通过表示 idle 会复用 `assets/codex/codex.gif`。
    错误处理：资产路径或动画声明错误由 pytest 断言报告。
    副作用：只创建内存模型，不读取 GIF 文件、不访问硬件。
    """

    spec = resolve_visual_icon_spec(AgentStatus.IDLE)

    assert spec.visual_state == VisualAgentState.IDLE
    assert spec.base_asset_id == "assets/codex/codex.gif"
    assert spec.asset_id == "assets/codex/codex.gif"
    assert spec.variant_id == "idle"
    assert spec.animation == VisualAnimation.GIF_ASSET
    assert spec.accent_color == "green"
    assert spec.badge is None
    assert spec.priority == 4


def test_completed_recently_reuses_idle_with_success_badge() -> None:
    """刚完成状态复用 idle 基础视觉并添加短暂成功提示。

    入参：无；固定解析 `AgentStatus.COMPLETED_RECENTLY`。
    返回：无返回值；断言通过表示完成态不会扩展成新的主视觉状态。
    错误处理：主视觉态、badge 或动画选择错误由 pytest 断言报告。
    副作用：只创建内存模型，不读取资产、不访问硬件。
    """

    spec = resolve_visual_icon_spec(AgentStatus.COMPLETED_RECENTLY)

    assert spec.visual_state == VisualAgentState.IDLE
    assert spec.base_asset_id == "assets/codex/codex.gif"
    assert spec.asset_id == "assets/codex/codex.gif"
    assert spec.variant_id == "completed"
    assert spec.animation == VisualAnimation.FLASH
    assert spec.accent_color == "green"
    assert spec.badge == VisualBadge.SUCCESS
    assert spec.priority == 3


def test_offline_uses_static_codex_png_dimmed() -> None:
    """离线状态使用静态 Codex 图标并声明低亮展示。

    入参：无；固定解析 `AgentStatus.OFFLINE`。
    返回：无返回值；断言通过表示 offline 会复用 `assets/codex/codex.png`。
    错误处理：资产路径、低亮标记或动画声明错误由 pytest 断言报告。
    副作用：只创建内存模型，不读取 PNG 文件、不访问硬件。
    """

    spec = resolve_visual_icon_spec(AgentStatus.OFFLINE)

    assert spec.visual_state == VisualAgentState.OFFLINE
    assert spec.base_asset_id == "assets/codex/codex.png"
    assert spec.asset_id == "assets/codex/codex.png"
    assert spec.variant_id == "offline"
    assert spec.animation == VisualAnimation.NONE
    assert spec.accent_color == "gray"
    assert spec.dimmed is True
    assert spec.priority == 5


def test_error_has_dedicated_visual_state() -> None:
    """错误状态保留独立红色视觉态。

    入参：无；固定解析 `AgentStatus.ERROR`。
    返回：无返回值；断言通过表示 error 不会被误合并到 needs_user 或 offline。
    错误处理：主视觉态、badge 或动画选择错误由 pytest 断言报告。
    副作用：只创建内存模型，不读取资产、不访问硬件。
    """

    spec = resolve_visual_icon_spec(AgentStatus.ERROR)

    assert spec.visual_state == VisualAgentState.ERROR
    assert spec.base_asset_id == "assets/codex/codex.gif"
    assert spec.asset_id == "generated/codex/error"
    assert spec.variant_id == "error"
    assert spec.animation == VisualAnimation.PULSE
    assert spec.accent_color == "red"
    assert spec.badge == VisualBadge.ERROR
    assert spec.priority == 1
