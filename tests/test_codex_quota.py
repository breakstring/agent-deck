"""Codex app-server quota adapter 的单元测试。

这些测试只验证 JSON-RPC 响应解析、plan 映射和错误收敛，不启动真实 `codex`
子进程，不访问用户账号，不连接网络，不读写硬件。测试输入使用脱敏 fixture，
唯一副作用是 pytest 创建测试进程内的模型对象。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from agent_deck.adapters.codex_quota import (
    CodexQuotaSnapshot,
    display_plan_name,
    parse_rate_limits_response,
)


def test_parse_rate_limits_response_maps_prolite_to_prolite() -> None:
    """解析 Codex rate limit 响应并映射 plan 展示名。

    入参：无；测试内构造 app-server `account/rateLimits/read` 响应。
    返回：无返回值；断言通过表示主 quota、窗口百分比、重置时间和 plan 映射正确。
    错误处理：字段缺失或类型不符合预期时由 pytest 断言或 Pydantic 校验报告。
    副作用：无；不启动 Codex，不访问账号。
    """

    snapshot = parse_rate_limits_response(
        {
            "id": 2,
            "result": {
                "rateLimits": {
                    "limitId": "codex",
                    "limitName": None,
                    "primary": {
                        "usedPercent": 28,
                        "windowDurationMins": 300,
                        "resetsAt": 1781697062,
                    },
                    "secondary": {
                        "usedPercent": 8,
                        "windowDurationMins": 10080,
                        "resetsAt": 1782280048,
                    },
                    "credits": {
                        "hasCredits": False,
                        "unlimited": False,
                        "balance": "0",
                    },
                    "individualLimit": None,
                    "planType": "prolite",
                    "rateLimitReachedType": None,
                },
                "rateLimitsByLimitId": {},
                "rateLimitResetCredits": {
                    "availableCount": 2,
                },
            },
        },
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    assert snapshot.plan_type == "prolite"
    assert snapshot.plan_short_label == "ProLite"
    assert snapshot.plan_display_name == "ProLite"
    assert snapshot.primary.used_percent == 28
    assert snapshot.primary.window_duration_mins == 300
    assert snapshot.primary.resets_at == datetime.fromtimestamp(
        1781697062,
        ZoneInfo("Asia/Shanghai"),
    )
    assert snapshot.secondary.used_percent == 8
    assert snapshot.secondary.window_duration_mins == 10080
    assert snapshot.credits_balance == "0"
    assert snapshot.reset_credits_available == 2


def test_display_plan_name_falls_back_to_plan_type() -> None:
    """未知 plan type 应保持原值，避免错误展示。

    入参：无。
    返回：无返回值；断言通过表示只对已知映射做替换。
    错误处理：映射错误时由 pytest 断言报告。
    副作用：无。
    """

    assert display_plan_name("prolite") == "ProLite"
    assert display_plan_name("enterprise") == "Enterprise"
    assert display_plan_name(None) == "Unknown"


def test_quota_snapshot_exposes_short_reset_labels() -> None:
    """snapshot 应提供触屏渲染可直接使用的短时间标签。

    入参：无；构造最小 `CodexQuotaSnapshot`。
    返回：无返回值；断言通过表示重置时间标签是本地时区的 `HH:MM`。
    错误处理：格式错误时由 pytest 断言报告。
    副作用：无。
    """

    tz = ZoneInfo("Asia/Shanghai")
    snapshot = CodexQuotaSnapshot(
        plan_type="prolite",
        plan_short_label="ProLite",
        plan_display_name="ProLite",
        primary={
            "used_percent": 28,
            "window_duration_mins": 300,
            "resets_at": datetime(2026, 6, 17, 19, 51, 2, tzinfo=tz),
        },
        secondary={
            "used_percent": 8,
            "window_duration_mins": 10080,
            "resets_at": datetime(2026, 6, 24, 13, 47, 28, tzinfo=tz),
        },
        credits_balance="0",
        raw={},
    )

    assert snapshot.primary_reset_label() == "19:51"
    assert snapshot.secondary_reset_label() == "06-24 13:47"
