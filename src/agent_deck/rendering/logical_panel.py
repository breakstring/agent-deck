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
from agent_deck.adapters.codex_tokens import CodexTokenPeriod, CodexTokenUsageSnapshot


class PanelKind(StrEnum):
    """描述 logical panel 当前展示的内容类型。

    入参：枚举值是稳定字符串，用于布局、renderer 和输入路由分发。
    返回：作为 `LogicalPanelPlan.kind` 的类型约束。
    错误处理：未知字符串由 Pydantic / Enum 校验拒绝。
    副作用：无；声明枚举不访问外部资源。
    """

    BRAND = "brand"
    QUOTA = "quota"
    TOKENS = "tokens"
    PETS = "pets"
    MESSAGE = "message"


PANEL_KIND_ORDER: tuple[PanelKind, ...] = (
    PanelKind.BRAND,
    PanelKind.QUOTA,
    PanelKind.TOKENS,
    PanelKind.PETS,
    PanelKind.MESSAGE,
)
"""Agent Deck 当前已建模的 logical panel 类型稳定顺序。"""

ACTIVE_PANEL_KIND_ORDER: tuple[PanelKind, ...] = (
    PanelKind.BRAND,
    PanelKind.QUOTA,
    PanelKind.TOKENS,
)
"""手动轮换真实 logical panel 时当前开放的稳定顺序。

Brand 是正常待机与归位面板；`pets` 和 `message` 不进入手动轮换，后续只能由明确的上下文
覆盖策略展示，避免用户切到空占位或审批内容。
"""


class PanelContentDirection(StrEnum):
    """描述 virtual panel 或 panel 内内容的一次环形切换方向。

    入参：枚举值由配置驱动的旋钮 intent 转换而来。
    返回：作为 panel selection reducer 的方向约束。
    错误处理：未知字符串由 Pydantic/Enum 拒绝。
    副作用：声明枚举不执行输入或渲染。
    """

    PREVIOUS = "previous"
    NEXT = "next"


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
    KNOB_4_ROTATE_LEFT = "knob_4.rotate_left"
    KNOB_4_ROTATE_RIGHT = "knob_4.rotate_right"
    TOUCH_TAP = "touch.tap"


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
    PREVIOUS_TOKEN_PERIOD = "previous_token_period"
    NEXT_TOKEN_PERIOD = "next_token_period"


TOKEN_PERIOD_ORDER: tuple[CodexTokenPeriod, ...] = (
    CodexTokenPeriod.TODAY,
    CodexTokenPeriod.WEEK,
    CodexTokenPeriod.MONTH,
    CodexTokenPeriod.ALL,
)
"""tokens 面板内第四旋钮切换统计周期时使用的稳定顺序。"""


class PanelSelection(BaseModel):
    """描述 logical panel 当前的展示选择状态。

    入参：`active_kind` 是当前展示的 panel 类型；`token_period` 是 tokens 面板当前统计周期。
    返回：frozen Pydantic model，可由 input router 和 renderer 只读传递。
    错误处理：非法枚举值由 Pydantic 拒绝。
    副作用：无；只保存内存状态，不监听真实输入、不写硬件。
    """

    model_config = ConfigDict(frozen=True)

    active_kind: PanelKind = PanelKind.BRAND
    quota_window: str = "auto"
    token_period: CodexTokenPeriod = CodexTokenPeriod.TODAY


class PanelMetric(BaseModel):
    """描述 logical panel 上需要突出或普通展示的一项指标。

    入参：`label` 是短标签；`value` 是已经格式化好的显示值；`emphasis` 是渲染提示，
    当前允许 `primary`、`secondary`、`normal`。
    返回：frozen Pydantic model。
    错误处理：空标签、空值或非法 emphasis 由校验拒绝。
    副作用：仅保存内存数据。
    """

    model_config = ConfigDict(frozen=True)

    label: str
    value: str
    emphasis: str = "normal"

    @field_validator("label", "value")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        """校验指标标签和值非空。

        入参：`value` 是待校验字符串。
        返回：去除首尾空白后的字符串。
        错误处理：空字符串由 ValueError 拒绝。
        副作用：无。
        """

        return _non_empty(value, field_name="metric field")

    @field_validator("emphasis")
    @classmethod
    def _validate_emphasis(cls, value: str) -> str:
        """校验指标强调等级。

        入参：`value` 是强调等级字符串。
        返回：原始等级。
        错误处理：不在允许集合中时抛出 ValueError。
        副作用：无。
        """

        if value not in {"primary", "secondary", "normal"}:
            raise ValueError("metric emphasis must be primary, secondary, or normal")
        return value


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
    metrics: tuple[PanelMetric, ...] = ()
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
    返回：`kind=PanelKind.QUOTA` 的 `LogicalPanelPlan`，保留计划名与当前实际可用窗口的配额。
    错误处理：snapshot 字段非法应在 adapter 层由 Pydantic 报告；本函数只读取字段。
    副作用：无；不渲染图像、不访问 Codex。
    """

    plan_label = snapshot.plan_short_label or snapshot.plan_display_name
    quota_lines = tuple(
        f"{window.display_period_label().lower()} "
        f"{_remaining_percent(window.used_percent)}% resets {window.display_reset_label()}"
        for window in snapshot.available_windows()
    )
    return LogicalPanelPlan(
        kind=PanelKind.QUOTA,
        title="Quota",
        lines=(plan_label, *quota_lines),
        controls=_default_rotary_controls(),
    )


def tokens_panel_plan(
    snapshot: CodexTokenUsageSnapshot,
    *,
    period: CodexTokenPeriod = CodexTokenPeriod.TODAY,
) -> LogicalPanelPlan:
    """创建 token 消耗 logical panel 计划。

    入参：`snapshot` 是 ccusage 适配器输出的四周期 token usage；`period` 是当前展示周期。
    返回：`kind=PanelKind.TOKENS` 的 `LogicalPanelPlan`，重点指标为金额和总 token。
    错误处理：缺少周期时抛 KeyError。
    副作用：无；只创建内存模型，不执行 ccusage。
    """

    stats = snapshot.periods[period]
    return LogicalPanelPlan(
        kind=PanelKind.TOKENS,
        title=f"Tokens · {period.value}",
        metrics=(
            PanelMetric(label="Cost", value=stats.cost_label, emphasis="primary"),
            PanelMetric(label="Total", value=stats.total_tokens_label, emphasis="primary"),
        ),
        lines=(
            f"Input {stats.input_tokens_label}",
            f"Output {stats.output_tokens_label}",
            f"Reasoning {stats.reasoning_output_tokens_label}",
            f"Cache read {stats.cache_read_tokens_label}",
        ),
        controls=_token_panel_controls(),
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


def apply_panel_input(
    selection: PanelSelection,
    event: PanelInputEvent,
    *,
    available_quota_windows: tuple[str, ...] | None = None,
) -> PanelSelection:
    """把 logical panel 输入事件归约成新的选择状态。

    入参：`selection` 是当前选择状态；`event` 是已经由硬件层归一化的 panel 输入事件；
    `available_quota_windows` 可由实时 quota 快照传入，以跳过当前订阅不存在的窗口。
    返回：新的 `PanelSelection`；无状态变化时返回原状态值。
    错误处理：非法事件由枚举/Pydantic 调用方约束处理；未知业务事件按无状态变化处理。
    副作用：无；不会执行 action、不会触发硬件输出。
    """

    if event == PanelInputEvent.TOUCH_TAP:
        return cycle_virtual_panel(selection, direction=PanelContentDirection.NEXT)

    if event == PanelInputEvent.KNOB_4_ROTATE_RIGHT:
        return cycle_panel_content(
            selection,
            direction=PanelContentDirection.NEXT,
            available_quota_windows=available_quota_windows,
        )
    if event == PanelInputEvent.KNOB_4_ROTATE_LEFT:
        return cycle_panel_content(
            selection,
            direction=PanelContentDirection.PREVIOUS,
            available_quota_windows=available_quota_windows,
        )

    return selection


def cycle_virtual_panel(
    selection: PanelSelection,
    *,
    direction: PanelContentDirection,
) -> PanelSelection:
    """按固定 Brand、Quota、Usage 顺序切换手动 virtual panel。

    入参：`selection` 是当前不可变选择状态；`direction` 指定上一个或下一个面板。
    返回：active kind 更新后的新 selection；非手动 panel 会回到 Brand 再移动。
    错误处理：无；未知当前 kind 按安全归位规则处理。
    副作用：无；不渲染、不访问硬件。
    """

    step = 1 if direction == PanelContentDirection.NEXT else -1
    current = selection.active_kind
    if current not in ACTIVE_PANEL_KIND_ORDER:
        current = PanelKind.BRAND
    return selection.model_copy(
        update={"active_kind": _next_cyclic_value(ACTIVE_PANEL_KIND_ORDER, current, step=step)}
    )


def cycle_panel_content(
    selection: PanelSelection,
    *,
    direction: PanelContentDirection,
    available_quota_windows: tuple[str, ...] | None = None,
) -> PanelSelection:
    """切换当前面板内部的内容维度，不跨越到其他 virtual panel。

    入参：`selection` 是当前选择状态；`direction` 指定向前或向后环形移动。
    返回：Quota 在实际可用窗口之间切换、Usage 更新统计周期；当 Quota 当前为
    `auto` 或过期窗口时，先按第一个实际窗口定位后继续向指定方向切换；Brand 和其他
    panel 原样返回。
    错误处理：无；内容缺省值已由 Pydantic 枚举保证。
    副作用：无；不执行系统或硬件动作。
    """

    step = 1 if direction == PanelContentDirection.NEXT else -1
    if selection.active_kind == PanelKind.QUOTA:
        order = available_quota_windows or ()
        if not order:
            return selection
        current = selection.quota_window
        if current not in order:
            current = order[0]
        return selection.model_copy(
            update={
                "quota_window": _next_cyclic_value(
                    order,
                    current,
                    step=step,
                )
            }
        )
    if selection.active_kind == PanelKind.TOKENS:
        return selection.model_copy(
            update={
                "token_period": _next_cyclic_value(
                    TOKEN_PERIOD_ORDER,
                    selection.token_period,
                    step=step,
                )
            }
        )
    return selection


def _default_rotary_controls() -> tuple[PanelControlHint, ...]:
    """返回 logical panel 默认旋钮输入提示。

    入参：无。
    返回：touch tap 切换面板、旋钮 1 按下确认、旋钮 2 上下滚动的控制提示。
    错误处理：无业务异常。
    副作用：无；每次返回新的不可变元组，内部元素为 frozen model。
    """

    return (
        PanelControlHint(
            event=PanelInputEvent.TOUCH_TAP,
            intent=PanelInputIntent.NEXT_PANEL,
            label="Tap panel",
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


def _token_panel_controls() -> tuple[PanelControlHint, ...]:
    """返回 tokens 面板默认输入提示。

    入参：无。
    返回：touch tap 切换 logical panel，旋钮 4 左右切换 token 统计周期，旋钮 1 按下确认。
    错误处理：无业务异常。
    副作用：无。
    """

    return (
        *_default_rotary_controls(),
        PanelControlHint(
            event=PanelInputEvent.KNOB_4_ROTATE_LEFT,
            intent=PanelInputIntent.PREVIOUS_TOKEN_PERIOD,
            label="Previous token period",
        ),
        PanelControlHint(
            event=PanelInputEvent.KNOB_4_ROTATE_RIGHT,
            intent=PanelInputIntent.NEXT_TOKEN_PERIOD,
            label="Next token period",
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


def _next_cyclic_value[T](values: tuple[T, ...], current: T, *, step: int) -> T:
    """按固定顺序环形移动到下一个值。

    入参：`values` 是非空顺序表；`current` 是当前值；`step` 是正负移动步数。
    返回：环形移动后的值。
    错误处理：空顺序表或当前值不在顺序表内时抛出 ValueError。
    副作用：无。
    """

    if not values:
        raise ValueError("cyclic values must not be empty")
    try:
        current_index = values.index(current)
    except ValueError as exc:
        raise ValueError("current value must exist in cyclic values") from exc
    next_index = (current_index + step) % len(values)
    return values[next_index]


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
