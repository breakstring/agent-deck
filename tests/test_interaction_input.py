"""Deck 交互输入路由测试。

本文件只验证低层 key/button 事件如何结合当前 `LayoutPlan` 映射成硬件无关
`InteractionIntent`；不访问真实 StreamDock、不启动 daemon、不执行 focus 或审批动作。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from agent_deck.core.modes import DeckMode
from agent_deck.hardware.fake import HardwareInput
from agent_deck.input.interactions import (
    interaction_intent_from_hardware_input,
    interaction_intent_from_streamdock_input_event,
)
from agent_deck.rendering.layout import KeyPlan, LayoutPlan, TouchscreenPlan


def test_hardware_key_press_maps_current_layout_key_to_interaction_intent() -> None:
    """fake agent key press 应按当前 layout key 语义映射为 select intent。

    入参：无；测试内构造带 agent slot 的 layout。
    返回：无返回值；断言通过代表 fake hardware 可驱动 deck selection/action 薄闭环。
    错误处理：字段映射或 release 过滤错误时由 pytest 报告。
    副作用：只创建内存模型。
    """

    layout = _layout()
    event = HardwareInput(
        kind="key",
        index=0,
        value={"state": 1},
        occurred_at=datetime(2026, 6, 23, 12, 0, tzinfo=UTC),
    )

    intent = interaction_intent_from_hardware_input(event, layout)

    assert intent is not None
    assert intent.intent == "select_agent"
    assert intent.source == "hardware_key"
    assert intent.key_index == 0
    assert intent.agent_key == "codex:session-1"
    assert intent.decision_id is None
    assert intent.dry_run is False


def test_key_release_and_empty_layout_key_are_ignored() -> None:
    """key release 或空 key plan 不应产生 interaction intent。

    入参：无；测试内构造 release 事件和空键位 press 事件。
    返回：无返回值；断言通过代表不会按下/释放各触发一次，也不会触发空键。
    错误处理：过滤条件错误时由 pytest 报告。
    副作用：只创建内存模型。
    """

    occurred_at = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
    release = HardwareInput(
        kind="key",
        index=0,
        value={"state": 0},
        occurred_at=occurred_at,
    )
    empty = HardwareInput(
        kind="key",
        index=4,
        value={"state": 1},
        occurred_at=occurred_at,
    )

    assert interaction_intent_from_hardware_input(release, _layout()) is None
    assert interaction_intent_from_hardware_input(empty, _layout()) is None


def test_streamdock_button_press_uses_n4pro_main_key_values() -> None:
    """SDK button press 应把 N4 Pro ButtonKey 11-20 映射到 layout 的 0-9。

    入参：无；测试内构造最小 SDK-like button event。
    返回：无返回值；断言通过代表真实 N4 Pro key callback 可复用同一 layout key 语义。
    错误处理：key 编号、state 或 enum-like 字段读取错误时由 pytest 报告。
    副作用：只创建内存对象。
    """

    event = SimpleNamespace(
        event_type=_ValueObject("button"),
        key=_ValueObject(11),
        state=1,
    )

    intent = interaction_intent_from_streamdock_input_event(event, _layout())

    assert intent is not None
    assert intent.intent == "select_agent"
    assert intent.source == "streamdock_button"
    assert intent.key_index == 0
    assert intent.agent_key == "codex:session-1"


def test_streamdock_second_main_button_maps_to_second_agent_slot() -> None:
    """N4 Pro 物理第二个主按键应映射到 layout index 1。

    入参：无；测试内构造带两个 agent slot 的 layout 和 `key=12` 的 SDK-like event。
    返回：无返回值；断言通过代表真实 N4 Pro 主按键不会误映射到 action row。
    错误处理：编号偏移错误时由 pytest 报告。
    副作用：只创建内存对象。
    """

    event = SimpleNamespace(
        event_type=_ValueObject("button"),
        key=_ValueObject(12),
        state=1,
    )
    layout = _layout(
        second_key=KeyPlan(
            index=1,
            label="session-2",
            agent_key="codex:session-2",
            intent="select_agent",
        )
    )

    intent = interaction_intent_from_streamdock_input_event(event, layout)

    assert intent is not None
    assert intent.intent == "select_agent"
    assert intent.key_index == 1
    assert intent.agent_key == "codex:session-2"


def test_app_key_press_carries_action_payload() -> None:
    """App key press 应透传 layout 中的结构化 action payload。

    入参：无；测试内构造 App key plan 和 fake key press。
    返回：无返回值；断言通过代表 action 层能收到 App 名称、路径和 bundle id。
    错误处理：payload 或 dry-run 状态错误时由 pytest 报告。
    副作用：只创建内存模型。
    """

    layout = _layout(
        second_key=KeyPlan(
            index=1,
            label="Finder",
            intent="open_or_focus_app",
            action="open_or_focus_app",
            payload={
                "app_name": "Finder",
                "app_path": "/System/Library/CoreServices/Finder.app",
                "bundle_id": "com.apple.finder",
            },
        )
    )
    event = HardwareInput(
        kind="key",
        index=1,
        value={"state": 1},
        occurred_at=datetime(2026, 6, 23, 12, 0, tzinfo=UTC),
    )

    intent = interaction_intent_from_hardware_input(event, layout)

    assert intent is not None
    assert intent.intent == "open_or_focus_app"
    assert intent.action == "open_or_focus_app"
    assert intent.payload["app_name"] == "Finder"
    assert intent.payload["app_path"] == "/System/Library/CoreServices/Finder.app"
    assert intent.payload["bundle_id"] == "com.apple.finder"
    assert intent.dry_run is False


def _layout(second_key: KeyPlan | None = None) -> LayoutPlan:
    """构造包含 agent slot 和空键位的测试 layout。

    入参：`second_key` 可覆盖第二个键位，用于测试 N4 Pro 第二物理键。
    返回：固定 `LayoutPlan`。
    错误处理：模型字段非法时由 Pydantic 交给 pytest。
    副作用：无。
    """

    keys = [KeyPlan(index=index) for index in range(15)]
    keys[0] = KeyPlan(
        index=0,
        label="session-1",
        agent_key="codex:session-1",
        intent="select_agent",
    )
    if second_key is not None:
        keys[1] = second_key
    return LayoutPlan(
        mode=DeckMode.OVERVIEW,
        keys=tuple(keys),
        touchscreen=TouchscreenPlan(title="session-1"),
        led_color="green",
    )


class _ValueObject:
    """构造带 `.value` 的最小 SDK enum 替身。

    入参：`value` 是枚举值。
    返回：测试用对象。
    错误处理：无。
    副作用：无。
    """

    def __init__(self, value: object) -> None:
        """保存枚举值。

        入参：`value` 是任意枚举值。
        返回：无。
        错误处理：无。
        副作用：无。
        """

        self.value = value
