"""Codex token usage adapter 的单元测试。

本文件只验证 ccusage JSON 解析、周期聚合和展示格式化；不会实际执行 `bunx`、
不会读取 `CODEX_HOME`、不会访问网络或真实 Codex 日志。
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_deck.adapters.codex_tokens import (
    CodexTokenUsageCache,
    CodexTokenUsageSnapshot,
    CodexTokenPeriod,
    CodexTokenUsageStats,
    format_cost_usd,
    format_token_count,
    parse_ccusage_codex_daily_json,
    read_codex_token_usage,
)


def test_parse_ccusage_daily_json_aggregates_today_week_month_and_all() -> None:
    """ccusage daily JSON 应聚合成 today/week/month/all 四个周期。

    入参：无；测试内使用 ccusage codex daily compact JSON 形态。
    返回：无返回值；断言通过代表 Codex token usage 可以被稳定转换为面板数据。
    错误处理：字段缺失、聚合周期错误或数值漂移时由 pytest 报告。
    副作用：只创建内存模型。
    """

    snapshot = parse_ccusage_codex_daily_json(
        {
            "daily": [
                _row("2026-06-01", total=100, cost=1.0),
                _row("2026-06-21", total=200, cost=2.0),
                _row("2026-06-22", total=300, cost=3.0),
            ],
            "totals": _totals(total=600, cost=6.0),
        },
        reference_date=date(2026, 6, 22),
    )

    assert snapshot.periods[CodexTokenPeriod.TODAY].total_tokens == 300
    assert snapshot.periods[CodexTokenPeriod.WEEK].total_tokens == 300
    assert snapshot.periods[CodexTokenPeriod.MONTH].total_tokens == 600
    assert snapshot.periods[CodexTokenPeriod.ALL].total_tokens == 600
    assert snapshot.periods[CodexTokenPeriod.ALL].cost_usd == pytest.approx(6.0)
    assert snapshot.periods[CodexTokenPeriod.TODAY].input_tokens == 30
    assert snapshot.periods[CodexTokenPeriod.TODAY].output_tokens == 15
    assert snapshot.periods[CodexTokenPeriod.TODAY].reasoning_output_tokens == 6
    assert snapshot.periods[CodexTokenPeriod.TODAY].cache_read_tokens == 60


def test_parse_ccusage_daily_json_accepts_user_example_shape() -> None:
    """解析器应接受用户提供的 ccusage codex daily --json 示例结构。

    入参：无；测试内放入真实字段名，包括 `costUSD` 和 `reasoningOutputTokens`。
    返回：无返回值；断言通过代表字段映射符合 ccusage JSON 输出。
    错误处理：字段名映射错误时由 pytest 报告。
    副作用：只创建内存模型。
    """

    snapshot = parse_ccusage_codex_daily_json(
        json.loads(_USER_EXAMPLE_JSON),
        reference_date=date(2026, 6, 22),
    )
    today = snapshot.periods[CodexTokenPeriod.TODAY]

    assert today.input_tokens == 6_465_793
    assert today.output_tokens == 436_596
    assert today.reasoning_output_tokens == 110_065
    assert today.cache_read_tokens == 111_106_560
    assert today.total_tokens == 118_008_949
    assert today.cost_usd == pytest.approx(100.98012500000002)
    assert today.total_tokens_label == "118M"
    assert today.cost_label == "$100.98"


def test_format_token_count_and_cost_use_compact_units() -> None:
    """token 和 cost 展示值应做合理单位换算和小数取舍。

    入参：无。
    返回：无返回值；断言通过代表面板不会直接展示难读的大整数和长小数。
    错误处理：格式化规则回归时由 pytest 报告。
    副作用：无。
    """

    assert format_token_count(999) == "999"
    assert format_token_count(1_200) == "1.2K"
    assert format_token_count(436_596) == "437K"
    assert format_token_count(6_465_793) == "6.47M"
    assert format_token_count(118_008_949) == "118M"
    assert format_token_count(1_234_567_890) == "1.23B"
    assert format_cost_usd(100.98012500000002) == "$100.98"
    assert format_cost_usd(3.5) == "$3.50"
    assert format_cost_usd(0.12567) == "$0.126"
    assert format_cost_usd(0.0042) == "$0.0042"


def test_read_codex_token_usage_uses_ccusage_json_command_with_injected_runner() -> None:
    """读取函数应通过可注入 runner 调用 ccusage JSON 命令。

    入参：无；测试内注入 fake runner，避免实际执行 `bunx`。
    返回：无返回值；断言通过代表生产路径可使用外部 ccusage，测试路径仍纯内存。
    错误处理：命令参数或 JSON 解码错误时由 pytest 报告。
    副作用：仅记录 fake runner 参数。
    """

    calls: list[tuple[tuple[str, ...], float]] = []

    def fake_runner(command: tuple[str, ...], timeout_seconds: float) -> str:
        """返回固定 ccusage JSON 并记录命令。

        入参：`command` 是读取函数传入的命令；`timeout_seconds` 是超时。
        返回：ccusage JSON 字符串。
        错误处理：无。
        副作用：写入本地 `calls` 列表。
        """

        calls.append((command, timeout_seconds))
        return json.dumps({"daily": [_row("2026-06-22")], "totals": _totals()})

    snapshot = read_codex_token_usage(
        reference_date=date(2026, 6, 22),
        runner=fake_runner,
        timeout_seconds=4.5,
    )

    assert calls == [
        (
            ("bunx", "ccusage", "codex", "daily", "--compact", "--json"),
            4.5,
        )
    ]
    assert snapshot.periods[CodexTokenPeriod.TODAY].total_tokens == 100


def test_read_codex_token_usage_wraps_subprocess_failures() -> None:
    """读取函数应把外部命令失败转换成 ValueError。

    入参：无；测试内注入会抛 `CalledProcessError` 的 runner。
    返回：无返回值；断言通过代表 daemon 后续可以记录短错误。
    错误处理：未包装外部异常时由 pytest 报告。
    副作用：无。
    """

    def failing_runner(_command: tuple[str, ...], _timeout_seconds: float) -> str:
        """模拟 `bunx ccusage` 失败。

        入参：忽略命令和超时。
        返回：正常情况下不返回。
        错误处理：固定抛出 `CalledProcessError`。
        副作用：无。
        """

        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=("bunx", "ccusage"),
            stderr="ccusage failed",
        )

    with pytest.raises(ValueError, match="ccusage command failed"):
        read_codex_token_usage(runner=failing_runner)


def test_token_usage_cache_reuses_snapshot_until_ttl_or_force_refresh() -> None:
    """token usage cache 应在 TTL 内复用快照，并支持强制刷新。

    入参：无；测试内注入 reader 和 clock。
    返回：无返回值；断言通过代表几分钟级刷新策略可以不频繁执行 ccusage。
    错误处理：缓存命中/过期/强制刷新行为错误时由 pytest 报告。
    副作用：只修改测试内列表和时间变量。
    """

    current_time = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    calls = 0

    def clock() -> datetime:
        """返回测试控制的当前时间。

        入参：无。
        返回：timezone-aware datetime。
        错误处理：无。
        副作用：无。
        """

        return current_time

    def reader() -> CodexTokenUsageSnapshot:
        """返回总 token 随调用次数递增的快照。

        入参：无。
        返回：固定结构的 token usage snapshot。
        错误处理：无。
        副作用：递增测试内 `calls` 计数。
        """

        nonlocal calls
        calls += 1
        return _token_snapshot(total=100 * calls, updated_at=current_time)

    cache = CodexTokenUsageCache(reader=reader, ttl_seconds=300, clock=clock)

    first = cache.get()
    second = cache.get()
    assert first is second
    assert calls == 1

    current_time += timedelta(seconds=299)
    assert cache.get() is first
    assert calls == 1

    current_time += timedelta(seconds=1)
    expired = cache.get()
    assert expired.periods[CodexTokenPeriod.ALL].total_tokens == 200
    assert calls == 2

    forced = cache.get(force_refresh=True)
    assert forced.periods[CodexTokenPeriod.ALL].total_tokens == 300
    assert calls == 3


def test_token_usage_snapshot_rejects_naive_updated_at() -> None:
    """token usage snapshot 应拒绝没有时区的更新时间。

    入参：无；测试内构造 naive datetime。
    返回：无返回值；断言通过代表时间字段符合项目 timezone-aware 约束。
    错误处理：模型未拒绝 naive datetime 时由 pytest 报告。
    副作用：无。
    """

    stats = CodexTokenUsageStats(
        input_tokens=1,
        output_tokens=1,
        reasoning_output_tokens=0,
        cache_read_tokens=0,
        total_tokens=2,
        cost_usd=0.01,
    )
    with pytest.raises(ValidationError, match="timezone-aware"):
        CodexTokenUsageSnapshot(
            periods={period: stats for period in CodexTokenPeriod},
            updated_at=datetime(2026, 6, 22, 12, 0),
        )


def _row(
    day: str,
    *,
    total: int = 100,
    cost: float = 1.0,
) -> dict[str, object]:
    """构造 ccusage daily row。

    入参：`day` 是日期字符串；`total` 和 `cost` 控制总量。
    返回：字段名匹配 ccusage JSON 的 dict。
    错误处理：无。
    副作用：无。
    """

    return {
        "cacheCreationTokens": 0,
        "cacheReadTokens": total // 5,
        "costUSD": cost,
        "date": day,
        "inputTokens": total // 10,
        "outputTokens": total // 20,
        "reasoningOutputTokens": total // 50,
        "totalTokens": total,
    }


def _totals(total: int = 100, cost: float = 1.0) -> dict[str, object]:
    """构造 ccusage totals object。

    入参：`total` 和 `cost` 控制总量。
    返回：字段名匹配 ccusage JSON 的 dict。
    错误处理：无。
    副作用：无。
    """

    return {
        "cacheCreationTokens": 0,
        "cacheReadTokens": total // 5,
        "costUSD": cost,
        "inputTokens": total // 10,
        "outputTokens": total // 20,
        "reasoningOutputTokens": total // 50,
        "totalTokens": total,
    }


def _token_snapshot(
    *,
    total: int,
    updated_at: datetime,
) -> CodexTokenUsageSnapshot:
    """构造 token usage snapshot。

    入参：`total` 是所有周期的 total token；`updated_at` 是快照时间。
    返回：四个周期都有同一统计值的快照。
    错误处理：模型字段错误由 Pydantic 报告。
    副作用：无。
    """

    stats = CodexTokenUsageStats(
        input_tokens=total // 10,
        output_tokens=total // 20,
        reasoning_output_tokens=total // 50,
        cache_read_tokens=total // 5,
        total_tokens=total,
        cost_usd=total / 100,
    )
    return CodexTokenUsageSnapshot(
        periods={period: stats for period in CodexTokenPeriod},
        updated_at=updated_at,
    )


_USER_EXAMPLE_JSON = """
{
  "daily": [
    {
      "cacheCreationTokens": 0,
      "cacheReadTokens": 111106560,
      "costUSD": 100.98012500000002,
      "date": "2026-06-22",
      "inputTokens": 6465793,
      "outputTokens": 436596,
      "reasoningOutputTokens": 110065,
      "totalTokens": 118008949
    }
  ],
  "totals": {
    "cacheCreationTokens": 0,
    "cacheReadTokens": 111106560,
    "costUSD": 100.98012500000002,
    "inputTokens": 6465793,
    "outputTokens": 436596,
    "reasoningOutputTokens": 110065,
    "totalTokens": 118008949
  }
}
"""
