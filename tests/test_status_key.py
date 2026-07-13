"""Agent Deck 状态型主按键渲染测试。

这些测试只在内存中绘制 Pillow 图片，并用固定 quota/token 快照验证 112x112 状态按键的
基础契约。测试不访问真实 N4 Pro、不启动 Codex、不执行 ccusage，也不写用户配置。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from PIL import Image

from agent_deck.adapters.codex_quota import CodexQuotaSnapshot, CodexQuotaWindow
from agent_deck.adapters.codex_tokens import (
    CodexTokenPeriod,
    CodexTokenUsageSnapshot,
    CodexTokenUsageStats,
)
from agent_deck.rendering.status_key import (
    render_quota_status_key_image,
    render_usage_summary_key_image,
    usage_sparkline_values,
)
from agent_deck.rendering.status_key import _quota_reset_label as quota_reset_label
from agent_deck.rendering.status_key import _select_quota_window as select_quota_window

_DEFAULT_SECONDARY = object()
"""测试 helper 用于区分默认双窗口与显式缺失 secondary 的哨兵值。"""


def test_render_quota_status_key_image_uses_n4pro_key_size_and_gold_reset() -> None:
    """quota_status 按键应输出 112x112，并绘制金色 reset credit 标识。

    入参：无；测试内构造固定 quota snapshot。
    返回：无返回值；断言通过代表基础尺寸和 reset credit 色彩语义成立。
    错误处理：尺寸、模式或颜色采样不符合预期时由 pytest 报告。
    副作用：无；只创建内存图片并采样像素。
    """

    image = render_quota_status_key_image(
        _quota_snapshot(),
        window="primary",
        now=datetime(2026, 7, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert isinstance(image, Image.Image)
    assert image.size == (112, 112)
    assert image.mode == "RGB"
    assert _count_near_color(image, (248, 213, 113), region=(6, 84, 42, 108)) > 12


def test_render_usage_summary_key_image_uses_gold_cost_and_white_token() -> None:
    """usage_summary 按键应输出 112x112，并使用白色 token 与金色金额。

    入参：无；测试内构造固定 token usage snapshot。
    返回：无返回值；断言通过代表 usage 主副数值色彩语义成立。
    错误处理：尺寸、模式或颜色采样不符合预期时由 pytest 报告。
    副作用：无；只创建内存图片并采样像素。
    """

    image = render_usage_summary_key_image(
        _token_snapshot(),
        period=CodexTokenPeriod.ALL,
    )

    assert image.size == (112, 112)
    assert image.mode == "RGB"
    assert _count_near_color(image, (238, 244, 255), region=(12, 30, 100, 62)) > 20
    assert _count_near_color(image, (248, 213, 113), region=(18, 56, 96, 78)) > 10


def test_render_usage_summary_key_image_uses_period_color_for_badge_and_sparkline() -> None:
    """usage_summary 周期标签和折线应使用同一个周期身份色。

    入参：无；测试内渲染 week/month/all 三个周期。
    返回：无返回值；断言通过代表不同周期能靠颜色区分，且折线跟随周期色。
    错误处理：标签或折线采样不到周期色时由 pytest 报告。
    副作用：无；只创建内存图片并采样像素。
    """

    samples = (
        (CodexTokenPeriod.WEEK, (126, 236, 165)),
        (CodexTokenPeriod.MONTH, (171, 143, 255)),
        (CodexTokenPeriod.ALL, (255, 143, 112)),
    )

    for period, color in samples:
        image = render_usage_summary_key_image(_token_snapshot(), period=period)

        assert _count_near_color(image, color, region=(36, 7, 76, 25)) > 6
        assert _count_near_color(image, color, region=(8, 78, 104, 104)) > 8


def test_render_usage_summary_key_image_skips_missing_zero_days_in_sparkline() -> None:
    """usage_summary 折线应跳过缺失日期的 0 值，而不是把曲线压到底部。

    入参：无；测试使用 raw daily 中周二缺失的 week 数据。
    返回：无返回值；断言通过代表中间缺失日期仍能画出连续绿色趋势线。
    错误处理：折线被 0 值压到底部或未绘制时由 pytest 报告。
    副作用：无；只创建内存图片并采样像素。
    """

    image = render_usage_summary_key_image(
        _token_snapshot(),
        period=CodexTokenPeriod.WEEK,
    )

    assert _count_near_color(
        image,
        (126, 236, 165),
        region=(8, 82, 104, 98),
    ) > 16


def test_usage_sparkline_values_fill_missing_week_dates() -> None:
    """week sparkline 应按当前周补齐缺失日期。

    入参：无；测试内 raw daily 只提供周一和周三。
    返回：无返回值；断言通过代表周二按 0 补齐。
    错误处理：序列日期范围或缺失值处理错误时由 pytest 报告。
    副作用：无。
    """

    values = usage_sparkline_values(
        _token_snapshot(),
        period=CodexTokenPeriod.WEEK,
    )

    assert values == (10.0, 0.0, 30.0)


def test_usage_sparkline_values_use_recent_windows_for_today_and_all() -> None:
    """today/all sparkline 应使用最近窗口，而不是压缩全部历史。

    入参：无。
    返回：无返回值；断言通过代表 today 为最近 7 天、all 为最近 30 天。
    错误处理：窗口长度不符合约定时由 pytest 报告。
    副作用：无。
    """

    snapshot = _token_snapshot()

    assert len(
        usage_sparkline_values(snapshot, period=CodexTokenPeriod.TODAY)
    ) == 7
    assert len(usage_sparkline_values(snapshot, period=CodexTokenPeriod.ALL)) == 30


def test_quota_reset_label_uses_time_today_and_date_other_day() -> None:
    """quota reset 标签应当天显示时间，非当天显示日期。

    入参：无。
    返回：无返回值；断言通过代表 112x112 按键右下角标签遵循 spec。
    错误处理：日期判断错误时由 pytest 报告。
    副作用：无。
    """

    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 7, 9, 12, 0, tzinfo=tz)

    assert quota_reset_label(
        CodexQuotaWindow(
            used_percent=10,
            window_duration_mins=300,
            resets_at=datetime(2026, 7, 9, 15, 18, tzinfo=tz),
        ),
        now=now,
    ) == "15:18"
    assert quota_reset_label(
        CodexQuotaWindow(
            used_percent=10,
            window_duration_mins=10080,
            resets_at=datetime(2026, 7, 14, 11, 27, tzinfo=tz),
        ),
        now=now,
    ) == "07-14"


def test_quota_status_key_uses_actual_monthly_label_and_falls_back_from_missing_slot() -> None:
    """单月限额时状态键应使用 MONTH 角标，旧 secondary 配置也要安全回退。

    入参：无；测试构造只有 monthly primary 的 quota 快照。
    返回：无返回值；断言通过代表按键渲染不会继续把 primary 误写成 5H。
    错误处理：缺失 secondary 触发异常、标签仍固定为 WEEK/5H 时由 pytest 报告。
    副作用：无；不访问 Codex 或硬件。
    """

    snapshot = _quota_snapshot(secondary=None, primary_duration_mins=43200)

    selected, label, accent = select_quota_window(snapshot, window="secondary")

    assert selected.window_duration_mins == 43200
    assert label == "MONTH"
    assert accent == (76, 205, 255)


def test_status_key_rejects_tiny_size() -> None:
    """状态型按键尺寸太小时应明确失败。

    入参：无。
    返回：无返回值；断言通过代表 renderer 不会静默输出不可读小图。
    错误处理：未抛异常时由 pytest 报告。
    副作用：无。
    """

    try:
        render_usage_summary_key_image(_token_snapshot(), size=(48, 48))
    except ValueError as exc:
        assert str(exc) == "status key image size is too small"
    else:
        raise AssertionError("expected ValueError")


def _count_near_color(
    image: Image.Image,
    expected: tuple[int, int, int],
    *,
    region: tuple[int, int, int, int],
    tolerance: int = 28,
) -> int:
    """统计指定区域内接近目标颜色的像素数量。

    入参：`image` 是待采样图片；`expected` 是 RGB 目标色；`region` 是采样区域；
    `tolerance` 是每个颜色通道容差。
    返回：接近目标色的像素数量。
    错误处理：无。
    副作用：无；只读取内存像素。
    """

    left, top, right, bottom = region
    count = 0
    for y in range(top, bottom):
        for x in range(left, right):
            pixel = image.getpixel((x, y))
            if all(
                abs(channel - target) <= tolerance
                for channel, target in zip(pixel, expected, strict=True)
            ):
                count += 1
    return count


def _quota_snapshot(
    *,
    secondary: object = _DEFAULT_SECONDARY,
    primary_duration_mins: int = 300,
) -> CodexQuotaSnapshot:
    """构造固定 quota snapshot。

    入参：无。
    返回：用于状态按键测试的 `CodexQuotaSnapshot`。
    错误处理：字段非法由 Pydantic 报告。
    副作用：无。
    """

    tz = ZoneInfo("Asia/Shanghai")
    secondary_payload = (
        {
            "used_percent": 58,
            "window_duration_mins": 10080,
            "resets_at": datetime(2026, 7, 14, 11, 27, tzinfo=tz),
        }
        if secondary is _DEFAULT_SECONDARY
        else secondary
    )
    return CodexQuotaSnapshot(
        plan_type="prolite",
        plan_short_label="ProLite",
        plan_display_name="ProLite",
        primary={
            "used_percent": 49,
            "window_duration_mins": primary_duration_mins,
            "resets_at": datetime(2026, 7, 9, 15, 18, tzinfo=tz),
        },
        secondary=secondary_payload,
        reset_credits_available=2,
        raw={},
    )


def _token_snapshot() -> CodexTokenUsageSnapshot:
    """构造固定 token usage snapshot。

    入参：无。
    返回：包含 today/week/month/all 和 raw daily 的 `CodexTokenUsageSnapshot`。
    错误处理：字段非法由 Pydantic 报告。
    副作用：无。
    """

    periods = {
        CodexTokenPeriod.TODAY: _stats(total_tokens=30, cost_usd=1.5),
        CodexTokenPeriod.WEEK: _stats(total_tokens=40, cost_usd=2.5),
        CodexTokenPeriod.MONTH: _stats(total_tokens=50, cost_usd=3.5),
        CodexTokenPeriod.ALL: _stats(total_tokens=15_963_757_604, cost_usd=9063.23),
    }
    return CodexTokenUsageSnapshot(
        periods=periods,
        updated_at=datetime(2026, 7, 8, 4, 0, tzinfo=UTC),
        raw={
            "daily": [
                {"date": "2026-07-06", "totalTokens": 10, "costUSD": 1.0},
                {"date": "2026-07-08", "totalTokens": 30, "costUSD": 2.0},
            ],
        },
    )


def _stats(*, total_tokens: int, cost_usd: float) -> CodexTokenUsageStats:
    """构造测试用 token usage stats。

    入参：`total_tokens` 和 `cost_usd` 是本测试关心的字段。
    返回：其他 token 字段归零的 `CodexTokenUsageStats`。
    错误处理：字段非法由 Pydantic 报告。
    副作用：无。
    """

    return CodexTokenUsageStats(
        input_tokens=0,
        output_tokens=0,
        reasoning_output_tokens=0,
        cache_read_tokens=0,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
    )
