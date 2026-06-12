"""In-memory fake hardware surface for Agent Deck tests.

This module provides a hardware-independent surface that records rendered
`LayoutPlan` frames and queues synthetic hardware input events. It intentionally
does not probe StreamDock devices, start servers, invoke CLI hooks, read files,
write files, perform network I/O, modify global state, or start threads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from agent_deck.rendering.layout import LayoutPlan


class HardwareInput(BaseModel):
    """Represent one synthetic hardware input event.

    入参：`kind` 是输入类别，只允许 key、knob、touch 或 swipe；`index` 是来源控件编号；
    `value` 是测试可携带的任意事件值；`occurred_at` 必须是 timezone-aware datetime。
    返回：frozen Pydantic model，可安全入队并在测试中比较。
    错误处理：未知 kind 或 naive `occurred_at` 由 Pydantic `ValidationError` 报告。
    副作用：仅保存内存数据；实例化不访问硬件、网络或文件系统。
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["key", "knob", "touch", "swipe"]
    index: int
    value: object
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _ensure_timezone_aware(cls, value: datetime) -> datetime:
        """Reject naive datetimes so input ordering never guesses local time.

        入参：`value` 是 Pydantic 已解析出的事件发生时间。
        返回：原始 timezone-aware datetime，不做时区转换。
        错误处理：当 datetime 没有 tzinfo 或 utcoffset 为 None 时抛出 ValueError。
        副作用：无；只检查内存中的 datetime 字段。
        """

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value


class FakeHardwareSurface:
    """Record rendered layout frames and expose a FIFO synthetic input queue.

    入参：构造时不需要外部依赖。
    返回：实例提供 `last_plan`、`render_count`、`render`、`emit_input` 和 `drain_inputs`。
    错误处理：本类不主动捕获 `LayoutPlan` 或 `HardwareInput` 校验错误；调用方应传入已校验模型。
    副作用：仅修改本实例内存字段和队列；不访问硬件、网络、文件系统或线程。
    """

    def __init__(self) -> None:
        """Create an empty fake surface.

        入参：无。
        返回：无显式返回值；初始化后 `last_plan` 为 None，`render_count` 为 0。
        错误处理：本方法不主动抛业务异常。
        副作用：仅初始化本实例内存状态。
        """

        self.last_plan: LayoutPlan | None = None
        self.render_count = 0
        self._inputs: list[HardwareInput] = []

    def render(self, plan: LayoutPlan) -> None:
        """Record a layout frame as if it had been rendered to hardware.

        入参：`plan` 是 renderer-neutral、已校验的 `LayoutPlan`。
        返回：无返回值；调用后 `last_plan` 指向该 plan，`render_count` 增加 1。
        错误处理：本方法不校验 plan 内容；非法对象会按普通 Python 属性赋值语义传播。
        副作用：修改本实例的 `last_plan` 和 `render_count`；不访问外部 I/O。
        """

        self.last_plan = plan
        self.render_count += 1

    def emit_input(self, event: HardwareInput) -> None:
        """Append one synthetic hardware input event to the FIFO queue.

        入参：`event` 是已校验的 `HardwareInput`。
        返回：无返回值；后续 `drain_inputs` 会按入队顺序返回该事件。
        错误处理：本方法不重新校验 event；调用方传错类型时按 Python list append 语义处理。
        副作用：修改本实例的内存输入队列；不访问外部 I/O。
        """

        self._inputs.append(event)

    def drain_inputs(self) -> list[HardwareInput]:
        """Return queued input events in FIFO order and clear the queue.

        入参：无。
        返回：当前队列中 `HardwareInput` 的新 list；没有事件时返回空 list。
        错误处理：本方法不主动抛业务异常。
        副作用：清空本实例的内存输入队列；不访问外部 I/O。
        """

        inputs = list(self._inputs)
        self._inputs.clear()
        return inputs
