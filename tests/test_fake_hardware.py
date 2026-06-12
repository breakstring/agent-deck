"""Tests for the in-memory fake hardware surface.

These tests define Task 6's fake hardware contract only. They do not probe real
StreamDock devices, start servers, invoke CLI hooks, read files, write files,
create threads, or perform network I/O; their only side effects are local
Python object creation and pytest assertion reporting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_deck.core.modes import DeckMode
from agent_deck.hardware.fake import FakeHardwareSurface, HardwareInput
from agent_deck.rendering.layout import KeyPlan, LayoutPlan, TouchscreenPlan

BASE_TIME = datetime(2026, 6, 12, 8, 0, tzinfo=UTC)


def _plan(title: str, *, led_color: str = "off") -> LayoutPlan:
    """Build a layout plan for fake surface tests.

    入参：`title` 是触屏标题；`led_color` 是计划中的 LED 颜色字符串，默认 off。
    返回：包含 15 个空 key 和指定触屏标题的 frozen `LayoutPlan`。
    错误处理：字段类型或枚举非法时由 Pydantic 校验异常报告。
    副作用：仅创建内存模型，不访问网络、硬件或文件系统。
    """

    return LayoutPlan(
        mode=DeckMode.OVERVIEW,
        keys=tuple(KeyPlan(index=index) for index in range(15)),
        touchscreen=TouchscreenPlan(title=title),
        led_color=led_color,
    )


def _input(kind: str = "key", *, index: int = 0, offset: int = 0) -> HardwareInput:
    """Build a hardware input event for fake surface tests.

    入参：`kind` 是硬件输入类别；`index` 是来源控件编号；`offset` 是相对基础时间的秒数。
    返回：带 timezone-aware `occurred_at` 的 frozen `HardwareInput`。
    错误处理：非法 kind 或 naive 时间由 Pydantic `ValidationError` 报告。
    副作用：仅创建内存模型，不访问网络、硬件或文件系统。
    """

    return HardwareInput(
        kind=kind,  # type: ignore[arg-type]
        index=index,
        value={"offset": offset},
        occurred_at=BASE_TIME + timedelta(seconds=offset),
    )


def test_render_records_plan_and_increments_count() -> None:
    """Verify one render stores the plan and increments the render counter.

    入参：无；测试内创建 fake surface 和一个 layout plan。
    返回：无返回值；断言通过代表 fake surface 记录 last_plan 并把计数增至 1。
    错误处理：未记录 plan 或计数错误时由 pytest 报告。
    副作用：仅修改 fake surface 的内存状态。
    """

    surface = FakeHardwareSurface()
    plan = _plan("Overview")

    surface.render(plan)

    assert surface.last_plan == plan
    assert surface.render_count == 1


def test_multiple_renders_accumulate_count_and_keep_latest_plan() -> None:
    """Verify repeated renders count each frame and keep the newest plan.

    入参：无；测试内创建两个不同 layout plan 并连续 render。
    返回：无返回值；断言通过代表 render_count 累加且 last_plan 指向最后一次计划。
    错误处理：计数未累加或 last_plan 未更新时由 pytest 报告。
    副作用：仅修改 fake surface 的内存状态。
    """

    surface = FakeHardwareSurface()
    first = _plan("First", led_color="green")
    second = _plan("Second", led_color="red")

    surface.render(first)
    surface.render(second)

    assert surface.render_count == 2
    assert surface.last_plan == second


def test_drain_inputs_returns_fifo_events_and_clears_queue() -> None:
    """Verify input drain returns queued events in FIFO order and empties queue.

    入参：无；测试内按顺序入队多个 hardware input。
    返回：无返回值；断言通过代表第一次 drain 返回全部事件，第二次 drain 返回空列表。
    错误处理：顺序错误或队列未清空时由 pytest 报告。
    副作用：仅修改 fake surface 的内存队列。
    """

    surface = FakeHardwareSurface()
    first = _input("key", index=1, offset=1)
    second = _input("knob", index=2, offset=2)
    third = _input("swipe", index=3, offset=3)

    surface.emit_input(first)
    surface.emit_input(second)
    surface.emit_input(third)

    assert surface.drain_inputs() == [first, second, third]
    assert surface.drain_inputs() == []


def test_invalid_input_kind_is_rejected() -> None:
    """Verify unsupported hardware input kind fails Pydantic validation.

    入参：无；测试内传入不在 key/knob/touch/swipe 内的 kind。
    返回：无返回值；断言通过代表 Pydantic 拒绝非法 kind。
    错误处理：未抛出 `ValidationError` 时由 pytest 报告。
    副作用：仅触发本地模型校验。
    """

    with pytest.raises(ValidationError):
        _input("button")


def test_naive_occurred_at_is_rejected() -> None:
    """Verify hardware input timestamps must be timezone-aware.

    入参：无；测试内传入没有 timezone 的 `occurred_at`。
    返回：无返回值；断言通过代表 Pydantic 拒绝 naive datetime。
    错误处理：未抛出 `ValidationError` 时由 pytest 报告。
    副作用：仅触发本地模型校验。
    """

    with pytest.raises(ValidationError):
        HardwareInput(
            kind="touch",
            index=0,
            value="tap",
            occurred_at=datetime(2026, 6, 12, 8, 0),
        )
