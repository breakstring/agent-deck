"""Agent Deck 逻辑面板模型的单元测试。

本文件只验证 logical panel 的纯数据契约和 quota 面板计划转换；不会渲染真实硬件、
不会访问 StreamDock SDK、不会启动 Codex，也不会读写用户文件。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from agent_deck.adapters.codex_quota import CodexQuotaSnapshot
from agent_deck.rendering.logical_panel import (
    LogicalPanelPlan,
    PanelInputEvent,
    PanelInputIntent,
    PanelInputRole,
    PanelKind,
    message_panel_plan,
    pets_panel_plan,
    quota_panel_plan,
    tokens_panel_plan,
)


def test_panel_kind_matches_initial_product_categories() -> None:
    """面板类型应只包含当前产品讨论确认的四类。

    入参：无。
    返回：无返回值；断言通过代表面板类型覆盖 quota、tokens、pets、message。
    错误处理：类型缺失或多出未讨论类型时由 pytest 报告。
    副作用：无。
    """

    assert {kind.value for kind in PanelKind} == {
        "quota",
        "tokens",
        "pets",
        "message",
    }


def test_quota_panel_plan_preserves_quota_semantics_and_rotary_controls() -> None:
    """quota 快照应能转换为 logical panel plan。

    入参：无；测试内构造固定 quota snapshot。
    返回：无返回值；断言通过代表 quota 是一种 panel content，并带有旋钮切换/确认提示。
    错误处理：kind、文案、控制提示不符合契约时由 pytest 报告。
    副作用：仅创建内存模型。
    """

    plan = quota_panel_plan(_snapshot())

    assert plan.kind == PanelKind.QUOTA
    assert plan.title == "Quota"
    assert "ProLite" in plan.lines[0]
    assert any("5h 72%" in line for line in plan.lines)
    assert any("weekly 92%" in line for line in plan.lines)
    assert plan.primary_input_role == PanelInputRole.ROTARY_NAVIGATION
    assert _control(plan, PanelInputEvent.KNOB_1_ROTATE_LEFT).intent == (
        PanelInputIntent.PREVIOUS_PANEL
    )
    assert _control(plan, PanelInputEvent.KNOB_1_ROTATE_RIGHT).intent == (
        PanelInputIntent.NEXT_PANEL
    )
    assert _control(plan, PanelInputEvent.KNOB_1_PRESS).intent == (
        PanelInputIntent.CONFIRM
    )


def test_tokens_pets_and_message_plans_use_distinct_kinds() -> None:
    """tokens、pets、message 应能作为不同 logical panel 表达。

    入参：无；测试内通过各自 factory 构造面板计划。
    返回：无返回值；断言通过代表后续内容类型不会再塞进 quota 命名。
    错误处理：kind 或文案不符合契约时由 pytest 报告。
    副作用：仅创建内存模型。
    """

    tokens = tokens_panel_plan(
        used_tokens=1200,
        context_window=8000,
        title="Token usage",
    )
    pets = pets_panel_plan(name="Codex Cat", mood="focused", lines=("watching",))
    message = message_panel_plan(
        title="Permission context",
        lines=("Need access to local shell", "Review before approving"),
    )

    assert tokens.kind == PanelKind.TOKENS
    assert "1200 / 8000" in tokens.lines[0]
    assert "15%" in tokens.lines[1]
    assert pets.kind == PanelKind.PETS
    assert pets.title == "Codex Cat"
    assert pets.lines == ("focused", "watching")
    assert message.kind == PanelKind.MESSAGE
    assert message.lines == ("Need access to local shell", "Review before approving")


def test_panel_plan_is_frozen_and_rejects_empty_content() -> None:
    """logical panel plan 应不可变且拒绝空标题/空内容。

    入参：无；测试内构造非法和合法模型。
    返回：无返回值；断言通过代表 panel 计划能稳定跨模块传递。
    错误处理：非法模型未被拒绝或模型可变时由 pytest 报告。
    副作用：仅创建内存模型。
    """

    plan = message_panel_plan(title="Notice", lines=("Line",))

    with pytest.raises(ValidationError, match="frozen"):
        plan.title = "Changed"  # type: ignore[misc]

    with pytest.raises(ValidationError, match="title must not be empty"):
        LogicalPanelPlan(kind=PanelKind.MESSAGE, title="", lines=("Line",))

    with pytest.raises(ValidationError, match="at least one line"):
        LogicalPanelPlan(kind=PanelKind.MESSAGE, title="Notice", lines=())


def _control(plan: LogicalPanelPlan, event: PanelInputEvent):
    """按输入事件查找面板控制提示。

    入参：`plan` 是待检查面板计划；`event` 是目标输入事件。
    返回：匹配的控制提示。
    错误处理：缺少事件时让测试失败。
    副作用：无。
    """

    for control in plan.controls:
        if control.event == event:
            return control
    raise AssertionError(f"missing control for {event}")


def _snapshot() -> CodexQuotaSnapshot:
    """构造固定 quota snapshot。

    入参：无。
    返回：用于 logical panel 测试的 `CodexQuotaSnapshot`。
    错误处理：模型字段错误由 Pydantic 报告。
    副作用：无。
    """

    tz = ZoneInfo("Asia/Shanghai")
    return CodexQuotaSnapshot(
        plan_type="prolite",
        plan_short_label="ProLite",
        plan_display_name="ProLite",
        primary={
            "used_percent": 28,
            "window_duration_mins": 300,
            "resets_at": datetime(2026, 6, 17, 19, 51, 2, tzinfo=tz),
        },
        secondary={
            "used_percent": 8,
            "window_duration_mins": 10080,
            "resets_at": datetime(2026, 6, 24, 13, 47, 28, tzinfo=tz),
        },
        credits_balance="0",
        raw={},
    )
