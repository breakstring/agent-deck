"""In-memory fake hardware surface for Agent Deck tests.

This module provides a hardware-independent surface that records rendered
`LayoutPlan` frames and queues synthetic hardware input events. It intentionally
does not probe StreamDock devices, start servers, invoke CLI hooks, read files,
write files, perform network I/O, modify global state, or start threads.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from agent_deck.rendering.layout import LayoutPlan


class FrozenDict(Mapping[Any, Any]):
    """Read-only mapping snapshot used for fake hardware input values.

    入参：`items` 是已经递归冻结后的键值映射；本类会复制顶层 mapping，避免外部引用污染。
    返回：实现 `Mapping[Any, Any]` 的不可变可读容器，可用于索引、迭代和内容比较。
    错误处理：访问不存在 key 时按 dict 语义抛出 KeyError；写入操作因无可变 mapping API
    由 Python 抛出 TypeError。
    副作用：仅复制内存数据，不访问硬件、网络、文件系统或线程。
    """

    def __init__(self, items: Mapping[Any, Any] | None = None) -> None:
        """Create an immutable top-level mapping copy.

        入参：`items` 可为空；非空时应已经由 `_freeze_value` 递归处理内部值。
        返回：无显式返回值；初始化后的实例可作为只读 mapping 使用。
        错误处理：若 `items` 不符合 mapping 协议，底层 `dict(...)` 会抛出异常。
        副作用：仅复制内存数据，不访问硬件、网络、文件系统或线程。
        """

        self._items = dict(items or {})

    def __getitem__(self, key: Any) -> Any:
        """Return a frozen value by key.

        入参：`key` 是调用方要读取的 mapping key。
        返回：对应的已冻结 value；嵌套 dict 为 `FrozenDict`，list/tuple 为 tuple。
        错误处理：key 不存在时抛出 KeyError。
        副作用：无；只读取内部内存快照。
        """

        return self._items[key]

    def __iter__(self) -> Iterator[Any]:
        """Iterate over keys in snapshot insertion order.

        入参：无。
        返回：内部 mapping key 的迭代器。
        错误处理：不主动抛业务异常；迭代错误按 Python 运行时语义传播。
        副作用：无；不修改内部或外部状态。
        """

        return iter(self._items)

    def __len__(self) -> int:
        """Return the number of keys in this snapshot.

        入参：无。
        返回：内部 mapping 的键数量。
        错误处理：不主动抛业务异常。
        副作用：无；只读取内存长度。
        """

        return len(self._items)

    def __repr__(self) -> str:
        """Return a developer-readable representation.

        入参：无。
        返回：形如 `FrozenDict({...})` 的字符串，便于测试失败时定位内容。
        错误处理：若内部值的 repr 抛错则按 Python 语义传播。
        副作用：无；只格式化内存数据。
        """

        return f"{type(self).__name__}({self._items!r})"

    def __eq__(self, other: object) -> bool:
        """Compare this snapshot with another mapping by readable content.

        入参：`other` 可以是普通 dict、`FrozenDict` 或任意 Mapping。
        返回：内容相等时为 True；非 Mapping 类型返回 False。
        错误处理：对方 mapping 迭代失败时按 Python 语义传播。
        副作用：无；不修改任一参与比较的对象。
        """

        if not isinstance(other, Mapping):
            return False
        return dict(self.items()) == dict(other.items())

    def __hash__(self) -> int:
        """Return a hash for use inside frozen Pydantic models.

        入参：无。
        返回：基于递归冻结键值对的 hash。
        错误处理：若某个 key 或标量值本身不可哈希，Python 会抛出 TypeError。
        副作用：无；只读取内部键值对。
        """

        return hash(tuple(self._items.items()))


class HardwareInput(BaseModel):
    """Represent one synthetic hardware input event.

    入参：`kind` 是输入类别，只允许 key、knob、touch 或 swipe；`index` 是来源控件编号；
    `value` 是测试可携带的事件值，常见 JSON-like dict/list 会递归冻结为不可变快照；
    `occurred_at` 必须是 timezone-aware datetime。
    返回：frozen Pydantic model，可安全入队并在测试中比较。
    错误处理：未知 kind 或 naive `occurred_at` 由 Pydantic `ValidationError` 报告。
    副作用：仅保存内存数据；实例化不访问硬件、网络或文件系统。
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["key", "knob", "touch", "swipe"]
    index: int
    value: object
    occurred_at: datetime

    @field_validator("value", mode="before")
    @classmethod
    def _freeze_input_value(cls, value: Any) -> Any:
        """Capture JSON-like input values as recursive immutable snapshots.

        入参：`value` 是调用方传入的硬件事件 payload，可为 primitive、dict、list 或 tuple。
        返回：dict/mapping 转为 `FrozenDict`，list/tuple 转为 tuple，primitive 保持原值。
        错误处理：本方法不主动拒绝非 JSON-like 标量；不可迭代对象按原值保留。
        副作用：只复制内存结构，不修改原始 payload，不访问外部 I/O。
        """

        return _freeze_value(value)

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


def _freeze_value(value: Any) -> Any:
    """Recursively freeze common JSON-like containers.

    入参：`value` 是任意硬件输入 payload 值；dict/mapping、list 和 tuple 会被识别。
    返回：mapping 的 `FrozenDict` 快照、list/tuple 的 tuple 快照，其他值保持原对象。
    错误处理：mapping 复制或迭代失败时按 Python 语义传播。
    副作用：只创建新的内存容器，不修改输入对象，不访问外部 I/O。
    """

    if isinstance(value, Mapping):
        return FrozenDict({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
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
