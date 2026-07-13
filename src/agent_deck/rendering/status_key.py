"""Agent Deck 状态型主按键渲染器。

本模块把 Codex quota 与 token/cost usage 快照渲染成 N4 Pro 主按键可用的 112x112
静态图片。它只消费调用方传入的内存模型，不读取 Codex、不执行 ccusage、不访问
StreamDock 硬件、不写文件；预览或硬件下发由调用方决定。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from PIL import Image, ImageDraw, ImageFont

from agent_deck.adapters.codex_quota import (
    CodexQuotaSnapshot,
    CodexQuotaWindow,
    quota_window_period_label,
)
from agent_deck.adapters.codex_tokens import (
    CodexTokenPeriod,
    CodexTokenUsageSnapshot,
    format_token_count,
)
from agent_deck.rendering.reset_credit import draw_reset_credit_key_icon

N4PRO_STATUS_KEY_SIZE: Final[tuple[int, int]] = (112, 112)
"""N4 Pro 主按键状态图标默认尺寸。"""

QuotaStatusWindow = str
"""quota_status 按键可展示的 `auto` 或稳定 quota window_id。"""

UsageSparklineMetric = Literal["total_tokens", "cost_usd"]
"""usage_summary sparkline 可使用的趋势指标。"""

_SCALE: Final[int] = 4
_BACKGROUND: Final[tuple[int, int, int]] = (10, 14, 22)
_SURFACE: Final[tuple[int, int, int]] = (15, 21, 32)
_SURFACE_EDGE: Final[tuple[int, int, int]] = (32, 43, 62)
_TEXT: Final[tuple[int, int, int]] = (238, 244, 255)
_MUTED: Final[tuple[int, int, int]] = (142, 157, 181)
_TRACK: Final[tuple[int, int, int]] = (43, 54, 75)
_PRIMARY: Final[tuple[int, int, int]] = (76, 205, 255)
_SECONDARY: Final[tuple[int, int, int]] = (126, 236, 165)
_TERTIARY: Final[tuple[int, int, int]] = (171, 143, 255)
_QUATERNARY: Final[tuple[int, int, int]] = (255, 143, 112)
_QUOTA_ACCENTS: Final[tuple[tuple[int, int, int], ...]] = (
    _PRIMARY,
    _SECONDARY,
    _TERTIARY,
    _QUATERNARY,
)
_RESET_CREDIT: Final[tuple[int, int, int]] = (248, 213, 113)
_USAGE_LINE: Final[tuple[int, int, int]] = (91, 210, 246)
_USAGE_PERIOD_COLORS: Final[Mapping[CodexTokenPeriod, tuple[int, int, int]]] = {
    CodexTokenPeriod.TODAY: (91, 210, 246),
    CodexTokenPeriod.WEEK: (126, 236, 165),
    CodexTokenPeriod.MONTH: (171, 143, 255),
    CodexTokenPeriod.ALL: (255, 143, 112),
}
"""usage_summary 各统计周期的身份色，用于顶部标签和底部趋势线。"""


def render_quota_status_key_image(
    snapshot: CodexQuotaSnapshot,
    *,
    window: QuotaStatusWindow = "auto",
    size: tuple[int, int] = N4PRO_STATUS_KEY_SIZE,
    now: datetime | None = None,
) -> Image.Image:
    """把 Codex quota 快照渲染成单个订阅/限额状态按键。

    入参：`snapshot` 是 quota adapter 输出；`window` 控制展示最紧张窗口或指定 API 窗口；
    `size` 是输出尺寸；`now` 用于测试或预览中稳定“今天/其他日期”判断。
    返回：RGB `Image`，默认 112x112。
    错误处理：尺寸过小时抛 `ValueError`；未知窗口由类型约束或显式分支抛出。
    副作用：只创建内存图片，不访问文件、网络或硬件。
    """

    _validate_key_size(size)
    selected_window, label, accent = _select_quota_window(snapshot, window=window)
    remaining = _remaining_percent(selected_window.used_percent)
    reset_label = _quota_reset_label(selected_window, now=now)

    canvas = _new_canvas(size)
    draw = ImageDraw.Draw(canvas)
    _draw_key_surface(draw, size)
    _draw_badge(draw, (size[0] / 2, 8), label, accent)

    plan_label = _quota_identity_label(selected_window, snapshot=snapshot)
    _draw_text(
        draw,
        (size[0] / 2, 31),
        plan_label,
        size=11,
        fill=_MUTED,
        bold=True,
        anchor="mm",
    )
    _draw_fitted_text(
        draw,
        f"{remaining}%",
        bounds=(8, 38, 104, 72),
        max_size=34,
        min_size=24,
        fill=_TEXT,
        bold=True,
        anchor="mm",
    )
    _draw_progress_bar(
        draw,
        bounds=(10, 76, 102, 82),
        percent=remaining,
        fill=accent,
    )
    _draw_reset_footer(
        draw,
        available_count=snapshot.reset_credits_available,
        reset_label=reset_label,
    )
    return _downsample(canvas, size)


def render_usage_summary_key_image(
    snapshot: CodexTokenUsageSnapshot,
    *,
    period: CodexTokenPeriod = CodexTokenPeriod.TODAY,
    size: tuple[int, int] = N4PRO_STATUS_KEY_SIZE,
    sparkline_metric: UsageSparklineMetric = "total_tokens",
) -> Image.Image:
    """把 Codex token/cost 快照渲染成单个用量状态按键。

    入参：`snapshot` 是 token usage adapter 输出；`period` 是展示周期；`size` 是输出尺寸；
    `sparkline_metric` 控制底部趋势使用 token 总量还是金额。
    返回：RGB `Image`，默认 112x112。
    错误处理：缺少周期时抛 `KeyError`；尺寸过小时抛 `ValueError`。
    副作用：只创建内存图片，不执行 ccusage、不访问硬件。
    """

    _validate_key_size(size)
    stats = snapshot.periods[period]
    token_label = stats.total_tokens_label
    cost_label = _compact_cost_label(stats.cost_usd)
    values = usage_sparkline_values(snapshot, period=period, metric=sparkline_metric)
    period_color = _period_color(period)

    canvas = _new_canvas(size)
    draw = ImageDraw.Draw(canvas)
    _draw_key_surface(draw, size)
    _draw_badge(
        draw,
        (size[0] / 2, 8),
        _period_badge(period),
        period_color,
        text_fill=period_color,
    )
    _draw_fitted_text(
        draw,
        token_label,
        bounds=(8, 30, 104, 59),
        max_size=28,
        min_size=21,
        fill=_TEXT,
        bold=True,
        anchor="mm",
    )
    _draw_fitted_text(
        draw,
        cost_label,
        bounds=(8, 58, 104, 76),
        max_size=16,
        min_size=12,
        fill=_RESET_CREDIT,
        bold=True,
        anchor="mm",
    )
    _draw_sparkline(draw, values, bounds=(11, 83, 101, 101), color=period_color)
    return _downsample(canvas, size)


def usage_sparkline_values(
    snapshot: CodexTokenUsageSnapshot,
    *,
    period: CodexTokenPeriod,
    metric: UsageSparklineMetric = "total_tokens",
) -> tuple[float, ...]:
    """从 ccusage raw daily 数据提取用量按键 sparkline 序列。

    入参：`snapshot` 是 token usage 快照；`period` 是 day/week/month/all；`metric` 是趋势指标。
    返回：按日期补齐后的数值序列；数据不足时可能为空或只有一个有效值。
    错误处理：raw daily 缺失、日期非法或数值非法时会跳过对应行，避免预览/渲染失败。
    副作用：无；只读取内存中的 raw daily。
    """

    daily = _daily_metric_by_date(snapshot, metric=metric)
    if not daily:
        return ()
    reference_date = _reference_date(snapshot, daily)
    dates = _sparkline_dates(period, reference_date=reference_date)
    return tuple(float(daily.get(item, 0.0)) for item in dates)


def _validate_key_size(size: tuple[int, int]) -> None:
    """校验状态按键尺寸是否足够容纳当前布局。

    入参：`size` 是输出尺寸。
    返回：无返回值；尺寸可用时直接返回。
    错误处理：任一边小于 96 像素时抛 `ValueError`。
    副作用：无。
    """

    width, height = size
    if width < 96 or height < 96:
        raise ValueError("status key image size is too small")


def _select_quota_window(
    snapshot: CodexQuotaSnapshot,
    *,
    window: QuotaStatusWindow,
) -> tuple[CodexQuotaWindow, str, tuple[int, int, int]]:
    """选择 quota 按键当前展示的窗口、角标和强调色。

    入参：`snapshot` 是 quota 快照；`window` 是展示窗口。
    返回：窗口模型、短角标和 RGB 强调色。
    错误处理：未知窗口抛 `ValueError`。
    副作用：无。
    """

    selected = snapshot.resolved_window(window)
    selected_index = snapshot.available_windows().index(selected)
    return (
        selected,
        quota_window_period_label(selected.window_duration_mins),
        _accent_for_window(selected_index),
    )


def _quota_identity_label(
    window: CodexQuotaWindow,
    *,
    snapshot: CodexQuotaSnapshot,
) -> str:
    """返回状态键中用于区分同周期不同限额的短身份标签。

    入参：`window` 是当前状态键选中的 quota 窗口；`snapshot` 提供订阅名作为单窗口回退。
    返回：优先使用展示策略短标签，其次从 limit 名称提取末段；只有单一无名 limit 时使用订阅名。
    错误处理：名称格式异常时回退为 `LIMIT`，避免小屏渲染为空。
    副作用：无；只读取内存模型。
    """

    if window.presentation_label:
        return window.presentation_label.upper()
    if window.limit_id == "codex":
        return "CODEX"
    if window.limit_name:
        parts = tuple(
            item.strip()
            for item in window.limit_name.replace("_", "-").split("-")
            if item.strip()
        )
        return (parts[-1] if parts else "LIMIT").upper()
    if len(snapshot.available_windows()) > 1:
        return window.limit_id[:12].upper()
    return (snapshot.plan_short_label or snapshot.plan_display_name).upper()


def _accent_for_window(index: int) -> tuple[int, int, int]:
    """返回 quota 窗口在当前快照顺序中的循环强调色。

    入参：`index` 是窗口在 `snapshot.windows` 中的稳定位置。
    返回：按预设调色板循环的强调色，支持超过两个窗口。
    错误处理：负索引按 Python 取模规则仍可安全映射。
    副作用：无。
    """

    return _QUOTA_ACCENTS[index % len(_QUOTA_ACCENTS)]


def _remaining_percent(used_percent: int) -> int:
    """把已用比例转换为剩余比例。

    入参：`used_percent` 是 app-server `usedPercent`。
    返回：0-100 的剩余百分比。
    错误处理：无；越界值会夹紧。
    副作用：无。
    """

    return 100 - max(0, min(100, used_percent))


def _quota_reset_label(
    window: CodexQuotaWindow,
    *,
    now: datetime | None,
) -> str:
    """返回主按键右下角使用的重置时间标签。

    入参：`window` 是 quota 窗口；`now` 是可选当前时间。
    返回：当天重置为 `HH:MM`，非当天重置为 `MM-DD`。
    错误处理：naive `now` 会按 Python 日期比较语义处理；window 时间由 adapter 保证带时区。
    副作用：无。
    """

    reset_at = window.resets_at
    reference = now or datetime.now(reset_at.tzinfo)
    if reference.astimezone(reset_at.tzinfo).date() == reset_at.date():
        return reset_at.strftime("%H:%M")
    return reset_at.strftime("%m-%d")


def _period_badge(period: CodexTokenPeriod) -> str:
    """返回 usage 周期角标。

    入参：`period` 是 token usage 周期枚举。
    返回：适合 112x112 按键顶部显示的短标签。
    错误处理：未知枚举值按 `.value.upper()` 降级。
    副作用：无。
    """

    return usage_period_label(period)


def usage_period_label(period: CodexTokenPeriod) -> str:
    """返回 Token 用量周期的稳定短标签。

    入参：`period` 是 ccusage 支持的统计周期。
    返回：适合按键和 touch bar 共用的 `DAY`、`WEEK`、`MONTH` 或 `ALL`。
    错误处理：未知枚举值按大写原始值降级。
    副作用：无；只读取内存枚举。
    """

    labels = {
        CodexTokenPeriod.TODAY: "DAY",
        CodexTokenPeriod.WEEK: "WEEK",
        CodexTokenPeriod.MONTH: "MONTH",
        CodexTokenPeriod.ALL: "ALL",
    }
    return labels.get(period, period.value.upper())


def _period_color(period: CodexTokenPeriod) -> tuple[int, int, int]:
    """返回 usage 周期身份色。

    入参：`period` 是 token usage 周期枚举。
    返回：RGB 颜色；未知枚举值降级为 today 青色。
    错误处理：无。
    副作用：无。
    """

    return usage_period_color(period)


def usage_period_color(period: CodexTokenPeriod) -> tuple[int, int, int]:
    """返回 Token 用量周期在所有硬件表面共用的身份色。

    入参：`period` 是 ccusage 支持的统计周期。
    返回：RGB 三元组；未知周期降级为 Day 青色。
    错误处理：无。
    副作用：无；只读取模块级颜色映射。
    """

    return _USAGE_PERIOD_COLORS.get(period, _USAGE_LINE)


def _compact_cost_label(value: float) -> str:
    """把金额格式化为状态按键更短的美元标签。

    入参：`value` 是非负美元金额。
    返回：例如 `$29.56`、`$485`、`$9.06k`。
    错误处理：负数抛 `ValueError`。
    副作用：无。
    """

    if value < 0:
        raise ValueError("cost must not be negative")
    if value >= 10_000:
        return f"${value / 1000:.1f}k"
    if value >= 1000:
        return f"${value / 1000:.2f}k"
    if value >= 100:
        return f"${value:.0f}"
    if value >= 1:
        return f"${value:.2f}"
    return f"${value:.3f}".rstrip("0").rstrip(".")


def _daily_metric_by_date(
    snapshot: CodexTokenUsageSnapshot,
    *,
    metric: UsageSparklineMetric,
) -> dict[date, float]:
    """把 snapshot raw daily 转成日期到趋势指标的映射。

    入参：`snapshot` 是 token usage 快照；`metric` 是 total tokens 或 cost。
    返回：日期到数值的新 dict。
    错误处理：单行数据非法时跳过该行。
    副作用：无。
    """

    rows = snapshot.raw.get("daily") if isinstance(snapshot.raw, Mapping) else None
    if not isinstance(rows, list):
        return {}
    values: dict[date, float] = {}
    field = "totalTokens" if metric == "total_tokens" else "costUSD"
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            row_date = date.fromisoformat(str(row["date"]))
            values[row_date] = max(0.0, float(row.get(field, 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return values


def _reference_date(
    snapshot: CodexTokenUsageSnapshot,
    daily: Mapping[date, float],
) -> date:
    """返回 sparkline 使用的参考日期。

    入参：`snapshot` 是 token usage 快照；`daily` 是已解析的日期数据。
    返回：优先使用 snapshot 更新时间对应日期，否则使用 daily 最大日期。
    错误处理：无。
    副作用：无。
    """

    updated_date = snapshot.updated_at.date()
    if updated_date in daily:
        return updated_date
    return max(daily)


def _sparkline_dates(
    period: CodexTokenPeriod,
    *,
    reference_date: date,
) -> tuple[date, ...]:
    """返回指定周期的 sparkline 日期序列。

    入参：`period` 是展示周期；`reference_date` 是当前本地参考日期。
    返回：日期元组，缺失日期由调用方补 0。
    错误处理：未知周期按最近 7 天降级。
    副作用：无。
    """

    if period == CodexTokenPeriod.TODAY:
        return _date_range(reference_date - timedelta(days=6), reference_date)
    if period == CodexTokenPeriod.WEEK:
        week_start = reference_date - timedelta(days=reference_date.weekday())
        return _date_range(week_start, reference_date)
    if period == CodexTokenPeriod.MONTH:
        month_start = reference_date.replace(day=1)
        return _date_range(month_start, reference_date)
    if period == CodexTokenPeriod.ALL:
        return _date_range(reference_date - timedelta(days=29), reference_date)
    return _date_range(reference_date - timedelta(days=6), reference_date)


def _date_range(start: date, end: date) -> tuple[date, ...]:
    """返回闭区间日期序列。

    入参：`start` 和 `end` 是日期边界。
    返回：从 start 到 end 的日期元组；如果 end 早于 start，返回单点 end。
    错误处理：无。
    副作用：无。
    """

    if end < start:
        return (end,)
    days = (end - start).days
    return tuple(start + timedelta(days=offset) for offset in range(days + 1))


def _new_canvas(size: tuple[int, int]) -> Image.Image:
    """创建抗锯齿绘制用的放大画布。

    入参：`size` 是最终输出尺寸。
    返回：放大 `_SCALE` 倍的 RGB 图像。
    错误处理：Pillow 创建失败时异常传播。
    副作用：只分配内存。
    """

    width, height = size
    return Image.new("RGB", (width * _SCALE, height * _SCALE), _BACKGROUND)


def _downsample(canvas: Image.Image, size: tuple[int, int]) -> Image.Image:
    """把放大画布缩回目标尺寸。

    入参：`canvas` 是放大图像；`size` 是最终输出尺寸。
    返回：RGB `Image`。
    错误处理：Pillow resize 失败时异常传播。
    副作用：只创建内存图像。
    """

    return canvas.resize(size, Image.Resampling.LANCZOS)


def _draw_key_surface(
    draw: ImageDraw.ImageDraw,
    size: tuple[int, int],
) -> None:
    """绘制状态按键的底色和轻量边界。

    入参：`draw` 是放大画布的绘图对象；`size` 是最终输出尺寸。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改内存图像。
    """

    width, height = size
    bounds = _scaled_box((3, 3, width - 3, height - 3))
    draw.rounded_rectangle(bounds, radius=10 * _SCALE, fill=_SURFACE)
    draw.rounded_rectangle(bounds, radius=10 * _SCALE, outline=_SURFACE_EDGE, width=1 * _SCALE)


def _draw_badge(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    label: str,
    color: tuple[int, int, int],
    *,
    text_fill: tuple[int, int, int] = _TEXT,
) -> None:
    """绘制左上角周期角标。

    入参：`draw` 是绘图对象；`origin` 是最终坐标；`label` 是短角标；`color` 是强调色；
    `text_fill` 是标签文字色。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改内存图像。
    """

    center_x, y = origin
    font = _load_font(9, bold=True)
    text_box = draw.textbbox((0, 0), label, font=font)
    badge_w = max(25, (text_box[2] - text_box[0]) // _SCALE + 12)
    badge_h = 15
    x = center_x - badge_w / 2
    draw.rounded_rectangle(
        _scaled_box((x, y, x + badge_w, y + badge_h)),
        radius=5 * _SCALE,
        fill=_tinted(color, 0.18),
        outline=color,
        width=1 * _SCALE,
    )
    _draw_text(
        draw,
        (x + badge_w / 2, y + badge_h / 2 + 0.2),
        label,
        size=9,
        fill=text_fill,
        bold=True,
        anchor="mm",
    )


def _draw_progress_bar(
    draw: ImageDraw.ImageDraw,
    *,
    bounds: tuple[int, int, int, int],
    percent: int,
    fill: tuple[int, int, int],
) -> None:
    """绘制 quota 剩余比例细进度条。

    入参：`draw` 是绘图对象；`bounds` 是最终坐标；`percent` 是 0-100；`fill` 是进度色。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改内存图像。
    """

    left, top, right, bottom = bounds
    clamped = max(0, min(100, percent))
    draw.rounded_rectangle(_scaled_box(bounds), radius=3 * _SCALE, fill=_TRACK)
    if clamped <= 0:
        return
    fill_right = left + round((right - left) * clamped / 100)
    draw.rounded_rectangle(
        _scaled_box((left, top, fill_right, bottom)),
        radius=3 * _SCALE,
        fill=fill,
    )


def _draw_reset_footer(
    draw: ImageDraw.ImageDraw,
    *,
    available_count: int | None,
    reset_label: str,
) -> None:
    """绘制 quota 按键底部 reset credit 与 reset time。

    入参：`draw` 是绘图对象；`available_count` 是可用重置次数；`reset_label` 是右下角时间。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改内存图像。
    """

    count = max(0, available_count or 0)
    draw_reset_credit_key_icon(
        draw,
        (10 * _SCALE, 94 * _SCALE),
        12 * _SCALE,
        color=_RESET_CREDIT,
    )
    _draw_text(
        draw,
        (28, 98),
        str(count),
        size=12,
        fill=_RESET_CREDIT,
        bold=True,
        anchor="lm",
    )
    _draw_text(
        draw,
        (102, 98),
        reset_label,
        size=12,
        fill=_MUTED,
        bold=True,
        anchor="rm",
    )


def _draw_sparkline(
    draw: ImageDraw.ImageDraw,
    values: tuple[float, ...],
    *,
    bounds: tuple[int, int, int, int],
    color: tuple[int, int, int],
) -> None:
    """绘制 usage 按键底部趋势线，只强调最后一个点。

    入参：`draw` 是绘图对象；`values` 是趋势序列；`bounds` 是绘制区域；`color` 是线色。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改内存图像。
    """

    left, top, right, bottom = bounds
    positive_values = [value for value in values if value > 0]
    if len(positive_values) < 2 or len(values) < 2:
        return
    max_value = max(positive_values)
    min_value = min(positive_values)
    span = max(max_value - min_value, 1.0)
    step = (right - left) / max(1, len(values) - 1)
    points: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        if value <= 0:
            continue
        x = left + step * index
        normalized = (value - min_value) / span
        y = bottom - normalized * (bottom - top)
        points.append((x, y))
    if len(points) < 2:
        return
    draw.line(_scaled_points(points), fill=color, width=2 * _SCALE, joint="curve")
    last_x, last_y = points[-1]
    radius = 3.4
    draw.ellipse(
        _scaled_box((last_x - radius, last_y - radius, last_x + radius, last_y + radius)),
        fill=color,
    )


def _draw_fitted_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    bounds: tuple[int, int, int, int],
    max_size: int,
    min_size: int,
    fill: tuple[int, int, int],
    bold: bool,
    anchor: str,
) -> None:
    """在指定区域内自适应绘制单行文本。

    入参：`draw` 是绘图对象；`text` 是文本；`bounds` 是最终坐标；`max_size`/`min_size`
    是字号边界；`fill` 是颜色；`bold` 控制字体权重；`anchor` 是 Pillow text anchor。
    返回：无返回值。
    错误处理：字体加载失败会退回默认字体；Pillow 绘制失败时异常传播。
    副作用：修改内存图像。
    """

    left, top, right, bottom = bounds
    font = _fit_font(draw, text, max_width=(right - left) * _SCALE, max_size=max_size, min_size=min_size, bold=bold)
    x = (left + right) / 2 if "m" in anchor else left
    y = (top + bottom) / 2 if anchor.endswith("m") else top
    _draw_text(draw, (x, y), text, size=max_size, fill=fill, bold=bold, anchor=anchor, font=font)


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    max_width: int,
    max_size: int,
    min_size: int,
    bold: bool,
) -> ImageFont.ImageFont:
    """返回能装入目标宽度的字体。

    入参：`draw` 是绘图对象；`text` 是待测文本；`max_width` 是放大画布宽度；字号为最终尺寸。
    返回：Pillow 字体对象。
    错误处理：字体加载失败时 `_load_font` 退回默认字体。
    副作用：无。
    """

    for size in range(max_size, min_size - 1, -1):
        font = _load_font(size, bold=bold)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font
    return _load_font(min_size, bold=bold)


def _draw_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[float, float],
    text: str,
    *,
    size: int,
    fill: tuple[int, int, int],
    bold: bool,
    anchor: str | None = None,
    font: ImageFont.ImageFont | None = None,
) -> None:
    """按最终坐标在放大画布上绘制文本。

    入参：`draw` 是绘图对象；`position` 是最终坐标；`text` 是文本；`size` 是最终字号；
    `fill` 是颜色；`bold` 控制字体权重；`anchor` 是可选文本锚点；`font` 可复用已适配字体。
    返回：无返回值。
    错误处理：Pillow 绘制失败时异常传播。
    副作用：修改内存图像。
    """

    draw.text(
        (round(position[0] * _SCALE), round(position[1] * _SCALE)),
        text,
        fill=fill,
        font=font or _load_font(size, bold=bold),
        anchor=anchor,
    )


def _load_font(size: int, *, bold: bool) -> ImageFont.ImageFont:
    """加载 macOS 常见字体，失败时退回 Pillow 默认字体。

    入参：`size` 是最终输出字号；`bold` 控制是否使用粗体。
    返回：放大画布使用的字体对象。
    错误处理：字体文件不存在时自动尝试备用字体，全部失败时返回默认字体。
    副作用：只读取系统字体文件元数据。
    """

    font_size = size * _SCALE
    candidates = (
        Path("/System/Library/Fonts/SFNS.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")
        if bold
        else Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
    )
    for path in candidates:
        try:
            if path.exists():
                return ImageFont.truetype(str(path), font_size)
        except OSError:
            continue
    return ImageFont.load_default()


def _scaled_box(box: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """把最终坐标矩形转换为放大画布坐标。

    入参：`box` 是最终坐标矩形。
    返回：整数放大坐标矩形。
    错误处理：无。
    副作用：无。
    """

    return tuple(round(value * _SCALE) for value in box)  # type: ignore[return-value]


def _scaled_points(points: tuple[tuple[float, float], ...] | list[tuple[float, float]]) -> list[tuple[int, int]]:
    """把最终坐标点序列转换为放大画布坐标。

    入参：`points` 是二维点序列。
    返回：整数放大坐标列表。
    错误处理：无。
    副作用：无。
    """

    return [(round(x * _SCALE), round(y * _SCALE)) for x, y in points]


def _tinted(color: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    """把颜色与状态按键背景混合成暗色 tint。

    入参：`color` 是 RGB；`alpha` 是目标色权重。
    返回：混合后的 RGB。
    错误处理：alpha 会夹紧到 0-1。
    副作用：无。
    """

    weight = max(0.0, min(1.0, alpha))
    return tuple(
        round(_SURFACE[channel] * (1 - weight) + color[channel] * weight)
        for channel in range(3)
    )
