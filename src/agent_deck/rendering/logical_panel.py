"""Agent Deck 逻辑面板计划模型。

本模块把 N4 Pro 下方区域抽象成硬件无关的 logical panel：quota、tokens、pets 和 message
都是 panel content，具体映射到底部 background viewport、secondary soft-key slot 或其他
设备 surface 由后续 renderer 决定。本模块只创建 Pydantic 数据模型，不渲染图像、不访问
StreamDock SDK、不读取 Codex、不执行任何硬件输入动作。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from agent_deck.adapters.codex_quota import CodexQuotaSnapshot


class PanelKind(StrEnum):
    """描述 logical panel 当前展示的内容类型。

    入参：枚举值是稳定字符串，用于布局、renderer 和输入路由分发。
    返回：作为 `LogicalPanelPlan.kind` 的类型约束。
    错误处理：未知字符串由 Pydantic / Enum 校验拒绝。
    副作用：无；声明枚举不访问外部资源。
    """

    QUOTA = "quota"
    TOKENS = "tokens"
    PETS = "pets"
    MESSAGE = "message"


class PanelInputRole(StrEnum):
    """描述 panel 推荐使用的主要输入方式。

    入参：枚举值是硬件无关的输入角色。
    返回：作为 `LogicalPanelPlan.primary_input_role` 的提示信息。
    错误处理：未知字符串由 Pydantic / Enum 校验拒绝。
    副作用：无；不绑定真实硬件。
    """

    ROTARY_NAVIGATION = "rotary_navigation"
    ROTARY_CONFIRMATION = "rotary_confirmation"


class PanelInputEvent(StrEnum):
    """描述 logical panel 可消费的抽象输入事件。

    入参：枚举值是当前 N4 Pro 旋钮输入的硬件无关命名。
    返回：作为 `PanelControlHint.event` 的事件约束。
    错误处理：未知字符串由 Pydantic / Enum 校验拒绝。
    副作用：无；不监听真实输入。
    """

    KNOB_1_ROTATE_LEFT = "knob_1.rotate_left"
    KNOB_1_ROTATE_RIGHT = "knob_1.rotate_right"
    KNOB_1_PRESS = "knob_1.press"
    KNOB_2_ROTATE_LEFT = "knob_2.rotate_left"
    KNOB_2_ROTATE_RIGHT = "knob_2.rotate_right"


class PanelInputIntent(StrEnum):
    """描述 panel 输入事件映射出的业务意图。

    入参：枚举值是业务层 intent 名称，不直接执行动作。
    返回：作为 `PanelControlHint.intent` 的意图约束。
    错误处理：未知字符串由 Pydantic / Enum 校验拒绝。
    副作用：无；不会触发 action executor。
    """

    PREVIOUS_PANEL = "previous_panel"
    NEXT_PANEL = "next_panel"
    CONFIRM = "confirm"
    SCROLL_UP = "scroll_up"
    SCROLL_DOWN = "scroll_down"


class PanelControlHint(BaseModel):
    """描述 logical panel 推荐的输入事件到业务意图映射。

    入参：`event` 是抽象输入事件；`intent` 是业务意图；`label` 是面板或帮助 UI 可显示的短标签。
    返回：frozen Pydantic model，可被 input router 或 renderer 读取。
    错误处理：空标签由校验拒绝；枚举值非法由 Pydantic 报告。
    副作用：仅保存内存数据。
    """

    model_config = ConfigDict(frozen=True)

    event: PanelInputEvent
    intent: PanelInputIntent
    label: str

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        """校验控制提示标签非空。

        入参：`value` 是调用方提供的短标签。
        返回：去除首尾空白后的标签。
        错误处理：空字符串由 ValueError 拒绝。
        副作用：无。
        """

        return _non_empty(value, field_name="control label")


class LogicalPanelPlan(BaseModel):
    """描述一块逻辑面板要展示的内容和推荐输入方式。

    入参：`kind` 是内容类型；`title` 和 `lines` 是硬件无关展示文案；`controls` 是旋钮等输入
    事件的推荐映射；`primary_input_role` 说明当前默认交互方式。
    返回：frozen Pydantic model，可由 layout plan、renderer 或 input router 只读使用。
    错误处理：标题为空、没有内容行或控制提示重复时由校验拒绝。
    副作用：仅创建内存对象，不渲染图像、不访问硬件。
    """

    model_config = ConfigDict(frozen=True)

    kind: PanelKind
    title: str
    lines: tuple[str, ...]
    controls: tuple[PanelControlHint, ...] = ()
    primary_input_role: PanelInputRole = PanelInputRole.ROTARY_NAVIGATION

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        """校验面板标题非空。

        入参：`value` 是面板标题。
        返回：去除首尾空白后的标题。
        错误处理：空标题由 ValueError 拒绝。
        副作用：无。
        """

        return _non_empty(value, field_name="title")

    @field_validator("lines")
    @classmethod
    def _validate_lines(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """校验面板至少包含一行内容且每行非空。

        入参：`value` 是面板内容行。
        返回：去除首尾空白后的内容行元组。
        错误处理：无内容行或存在空行时抛出 ValueError。
        副作用：无。
        """

        if not value:
            raise ValueError("logical panel must contain at least one line")
        return tuple(_non_empty(line, field_name="panel line") for line in value)

    @model_validator(mode="after")
    def _validate_unique_controls(self) -> LogicalPanelPlan:
        """校验同一面板中输入事件不能重复绑定。

        入参：当前模型实例。
        返回：当前实例。
        错误处理：同一个 `PanelInputEvent` 出现多次时抛出 ValueError。
        副作用：无。
        """

        events = [control.event for control in self.controls]
        if len(events) != len(set(events)):
            raise ValueError("panel controls must not contain duplicate events")
        return self


def quota_panel_plan(snapshot: CodexQuotaSnapshot) -> LogicalPanelPlan:
    """把 Codex quota 快照转换成 logical panel 计划。

    入参：`snapshot` 是 Codex quota adapter 解析出的快照。
    返回：`kind=PanelKind.QUOTA` 的 `LogicalPanelPlan`，保留计划名、5 小时和周配额剩余百分比。
    错误处理：snapshot 字段非法应在 adapter 层由 Pydantic 报告；本函数只读取字段。
    副作用：无；不渲染图像、不访问 Codex。
    """

    plan_label = snapshot.plan_short_label or snapshot.plan_display_name
    primary_remaining = _remaining_percent(snapshot.primary.used_percent)
    secondary_remaining = _remaining_percent(snapshot.secondary.used_percent)
    return LogicalPanelPlan(
        kind=PanelKind.QUOTA,
        title="Quota",
        lines=(
            plan_label,
            f"5h {primary_remaining}% resets {snapshot.primary_reset_label()}",
            f"weekly {secondary_remaining}% resets {snapshot.secondary_reset_label()}",
        ),
        controls=_default_rotary_controls(),
    )


def tokens_panel_plan(
    *,
    used_tokens: int,
    context_window: int,
    title: str = "Tokens",
) -> LogicalPanelPlan:
    """创建 token 消耗 logical panel 计划。

    入参：`used_tokens` 是当前已用 token；`context_window` 是上下文窗口大小；`title` 是面板标题。
    返回：`kind=PanelKind.TOKENS` 的 `LogicalPanelPlan`。
    错误处理：token 数为负或上下文窗口非正时抛出 ValueError。
    副作用：无；只创建内存模型。
    """

    if used_tokens < 0:
        raise ValueError("used_tokens must not be negative")
    if context_window <= 0:
        raise ValueError("context_window must be positive")
    percent = round(min(100, used_tokens * 100 / context_window))
    return LogicalPanelPlan(
        kind=PanelKind.TOKENS,
        title=title,
        lines=(f"{used_tokens} / {context_window} tokens", f"{percent}% used"),
        controls=_default_rotary_controls(),
    )


def pets_panel_plan(
    *,
    name: str,
    mood: str,
    lines: tuple[str, ...] = (),
) -> LogicalPanelPlan:
    """创建宠物系统 logical panel 计划。

    入参：`name` 是宠物或主题名；`mood` 是当前状态；`lines` 是附加展示行。
    返回：`kind=PanelKind.PETS` 的 `LogicalPanelPlan`。
    错误处理：空标题、空 mood 或空附加行由 Pydantic/本模块校验拒绝。
    副作用：无；不播放动画、不访问资源。
    """

    return LogicalPanelPlan(
        kind=PanelKind.PETS,
        title=name,
        lines=(_non_empty(mood, field_name="pet mood"), *lines),
        controls=_default_rotary_controls(),
    )


def message_panel_plan(
    *,
    title: str,
    lines: tuple[str, ...],
) -> LogicalPanelPlan:
    """创建信息提示 logical panel 计划。

    入参：`title` 是提示标题；`lines` 是需要用户看到的多行复杂文本摘要。
    返回：`kind=PanelKind.MESSAGE` 的 `LogicalPanelPlan`。
    错误处理：标题或内容为空时由模型校验拒绝。
    副作用：无；不触发审批、不执行输入动作。
    """

    return LogicalPanelPlan(
        kind=PanelKind.MESSAGE,
        title=title,
        lines=lines,
        controls=_default_rotary_controls(),
    )


def _default_rotary_controls() -> tuple[PanelControlHint, ...]:
    """返回 logical panel 默认旋钮输入提示。

    入参：无。
    返回：旋钮 1 左右切换面板、旋钮 1 按下确认、旋钮 2 上下滚动的控制提示。
    错误处理：无业务异常。
    副作用：无；每次返回新的不可变元组，内部元素为 frozen model。
    """

    return (
        PanelControlHint(
            event=PanelInputEvent.KNOB_1_ROTATE_LEFT,
            intent=PanelInputIntent.PREVIOUS_PANEL,
            label="Previous panel",
        ),
        PanelControlHint(
            event=PanelInputEvent.KNOB_1_ROTATE_RIGHT,
            intent=PanelInputIntent.NEXT_PANEL,
            label="Next panel",
        ),
        PanelControlHint(
            event=PanelInputEvent.KNOB_1_PRESS,
            intent=PanelInputIntent.CONFIRM,
            label="Confirm",
        ),
        PanelControlHint(
            event=PanelInputEvent.KNOB_2_ROTATE_LEFT,
            intent=PanelInputIntent.SCROLL_UP,
            label="Scroll up",
        ),
        PanelControlHint(
            event=PanelInputEvent.KNOB_2_ROTATE_RIGHT,
            intent=PanelInputIntent.SCROLL_DOWN,
            label="Scroll down",
        ),
    )


def _remaining_percent(used_percent: int) -> int:
    """把已用百分比转换成剩余百分比。

    入参：`used_percent` 是 0-100 附近的已用百分比。
    返回：夹紧到 0-100 后的剩余百分比。
    错误处理：非整数类型由调用方模型约束处理。
    副作用：无。
    """

    used = max(0, min(100, used_percent))
    return 100 - used


def _non_empty(value: str, *, field_name: str) -> str:
    """校验并规范化非空字符串。

    入参：`value` 是待校验字符串；`field_name` 用于错误信息。
    返回：去除首尾空白后的字符串。
    错误处理：空字符串由 ValueError 拒绝。
    副作用：无。
    """

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized
