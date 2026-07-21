"""Codex 宠物主键合同与 Web 配置静态契约测试。

本模块只构造内存布局、模拟 fake key 输入并读取仓库内 Web 脚本；不解析用户宠物素材，
不启动 daemon，不连接真实硬件，也不写入配置或外部文件。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agent_deck.core.events import AgentSource
from agent_deck.core.modes import DeckMode, DeckSelection
from agent_deck.core.state import AgentState, AgentStatus
from agent_deck.hardware.fake import HardwareInput
from agent_deck.input.interactions import interaction_intent_from_hardware_input
from agent_deck.rendering.key_surface import (
    KeySurfaceKind,
    N4ProKeyBinding,
    N4ProKeyLayout,
    default_n4pro_key_layout,
)
from agent_deck.rendering.layout import build_layout_plan


def _pet_layout() -> N4ProKeyLayout:
    """构造一个宠物键后紧跟 Agent 槽位的完整主键布局。

    入参：无。
    返回：Key 1 为 Codex 宠物、Key 2 为 Agent，其余键沿用默认用途的完整布局。
    错误处理：若索引不完整或重复，由 `N4ProKeyLayout` 校验异常报告。
    副作用：只创建内存模型。
    """

    return N4ProKeyLayout(
        keys=(
            N4ProKeyBinding(index=0, kind=KeySurfaceKind.CODEX_PET),
            N4ProKeyBinding(index=1, kind=KeySurfaceKind.AGENT),
            *default_n4pro_key_layout().sorted_keys()[2:],
        )
    )


def _running_agent() -> AgentState:
    """构造用于验证 Agent 槽位消费顺序的运行中 Codex 状态。

    入参：无。
    返回：时间字段带 UTC 时区的顶层 Codex `AgentState`。
    错误处理：字段若违反状态模型合同，由 Pydantic 校验异常报告。
    副作用：只创建内存模型。
    """

    observed_at = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
    return AgentState(
        agent_key="codex:pet-test",
        source=AgentSource.CODEX,
        display_name="Codex Pet Test",
        status=AgentStatus.RUNNING_TOOL,
        status_since=observed_at,
        last_event_at=observed_at,
    )


def test_codex_pet_binding_round_trips_without_changing_default_layout() -> None:
    """宠物用途应可 JSON 往返，且不自动占用默认键位。

    入参：无；测试内创建默认布局和包含宠物键的自定义布局。
    返回：无；断言通过代表枚举值、标签及持久化合同稳定。
    错误处理：默认布局被改写或序列化丢失字段时由 pytest 报告。
    副作用：只创建和序列化内存模型。
    """

    assert all(
        binding.kind != KeySurfaceKind.CODEX_PET
        for binding in default_n4pro_key_layout().keys
    )

    layout = _pet_layout()
    restored = N4ProKeyLayout.model_validate(layout.model_dump(mode="json"))

    assert restored == layout
    assert restored.sorted_keys()[0].kind == KeySurfaceKind.CODEX_PET
    assert restored.sorted_keys()[0].display_label() == "Codex 宠物"


def test_codex_pet_projects_as_ambient_without_consuming_agent_slot() -> None:
    """宠物键应无意图，后续 Agent 键仍消费第一个可见 Agent。

    入参：无；测试内投影一个宠物键、一个 Agent 键和一个运行中状态。
    返回：无；断言通过代表 `ambient` 和 Agent slot 消费边界正确。
    错误处理：宠物键生成 action/intent 或抢占状态时由 pytest 报告。
    副作用：只创建内存布局计划。
    """

    agent = _running_agent()
    plan = build_layout_plan(
        [agent],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
        key_layout=_pet_layout(),
    )

    pet_key = plan.keys[0]
    agent_key = plan.keys[1]
    assert pet_key.kind == KeySurfaceKind.CODEX_PET.value
    assert pet_key.role == "ambient"
    assert pet_key.intent is None
    assert pet_key.action is None
    assert pet_key.agent_key is None
    assert agent_key.kind == KeySurfaceKind.AGENT.value
    assert agent_key.agent_key == agent.agent_key
    assert agent_key.intent == "select_agent"


def test_codex_pet_key_press_is_safely_ignored() -> None:
    """宠物键按下不应产生任何硬件交互 intent。

    入参：无；测试内向含宠物键的布局发送 fake key press。
    返回：无；断言通过代表输入路由因 `intent=None` 安全忽略按键。
    错误处理：若按键生成可执行动作，由 pytest 报告。
    副作用：仅创建内存事件与布局。
    """

    plan = build_layout_plan(
        [_running_agent()],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
        key_layout=_pet_layout(),
    )
    event = HardwareInput(
        kind="key",
        index=0,
        value={"state": 1},
        occurred_at=datetime(2026, 7, 21, 8, 1, tzinfo=UTC),
    )

    assert interaction_intent_from_hardware_input(event, plan) is None


def test_web_editor_preserves_no_action_codex_pet_contract() -> None:
    """Web 编辑器应支持宠物用途的读取、保存、预览和无动作提示。

    入参：无；测试内读取打包的 `app.js` 源码。
    返回：无；断言通过代表 UI 两向映射及产品文案都被显式保留。
    错误处理：关键分支或“点击无动作”提示丢失时由 pytest 报告。
    副作用：只读取仓库内静态脚本，不执行浏览器代码。
    """

    app_js = (
        Path(__file__).parents[1] / "src" / "agent_deck" / "web" / "app.js"
    ).read_text(encoding="utf-8")

    assert 'binding.kind === "codex_pet"' in app_js
    assert 'kind: "codex_pet", label: "Codex 宠物"' in app_js
    assert 'choiceButton("codex_pet", "Codex 宠物"' in app_js
    assert 'role: "ambient", kind: "codex_pet"' in app_js
    assert "仅展示，点击无动作" in app_js
    assert 'detailRow("按下", "仅展示，不执行动作")' in app_js
