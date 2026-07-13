"""Codex app-server quota adapter 的单元测试。

这些测试只验证 JSON-RPC 响应解析、plan 映射和错误收敛，不启动真实 `codex`
子进程，不访问用户账号，不连接网络，不读写硬件。测试输入使用脱敏 fixture，
唯一副作用是 pytest 创建测试进程内的模型对象。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

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
    primary, secondary = snapshot.windows
    assert primary.window_id == "codex:primary"
    assert primary.used_percent == 28
    assert primary.window_duration_mins == 300
    assert primary.resets_at == datetime.fromtimestamp(
        1781697062,
        ZoneInfo("Asia/Shanghai"),
    )
    assert secondary.window_id == "codex:secondary"
    assert secondary.used_percent == 8
    assert secondary.window_duration_mins == 10080
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

    primary, secondary = snapshot.windows

    assert primary.reset_label(include_date=False) == "19:51"
    assert secondary.reset_label(include_date=True) == "06-24 13:47"


def test_parse_rate_limits_response_accepts_a_single_monthly_window() -> None:
    """单窗口订阅不应因 secondary 为 null 被判定为 quota 读取失败。

    入参：无；测试构造只有 30 天 primary 窗口的 app-server 响应。
    返回：无返回值；断言通过代表免费或策略调整后的单月限额可安全进入渲染路径。
    错误处理：缺失窗口、周期推导或回退选择错误时由 pytest 报告。
    副作用：无；不启动 Codex、不访问用户账号。
    """

    snapshot = parse_rate_limits_response(
        {
            "id": 2,
            "result": {
                "rateLimits": {
                    "primary": {
                        "usedPercent": 42,
                        "windowDurationMins": 43200,
                        "resetsAt": 1784513812,
                    },
                    "secondary": None,
                    "planType": "free",
                }
            },
        },
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    assert tuple(item.window_id for item in snapshot.available_windows()) == ("codex:primary",)
    assert snapshot.windows[0].display_period_label() == "MONTH"
    assert snapshot.resolved_window("secondary").window_id == "codex:primary"


def test_parse_rate_limits_response_collects_additional_limit_windows() -> None:
    """额外模型或产品限额应进入同一个窗口集合，而不是被 primary/secondary 丢弃。

    入参：无；测试构造主 Codex 限额及一个具名模型专属限额。
    返回：无返回值；断言通过代表未来多重订阅限制可由同一按键和面板交互消费。
    错误处理：重复主 limit、额外 limit 漏解析或 window_id 不稳定时由 pytest 报告。
    副作用：无；不启动 Codex、不访问用户账号。
    """

    snapshot = parse_rate_limits_response(
        {
            "id": 2,
            "result": {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 42,
                        "windowDurationMins": 10080,
                        "resetsAt": 1784513812,
                    },
                },
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "primary": {
                            "usedPercent": 42,
                            "windowDurationMins": 10080,
                            "resetsAt": 1784513812,
                        },
                    },
                    "codex_spark": {
                        "limitId": "codex_spark",
                        "limitName": "GPT-5 Spark",
                        "primary": {
                            "usedPercent": 73,
                            "windowDurationMins": 43200,
                            "resetsAt": 1787000000,
                        },
                        "tertiary": {
                            "usedPercent": 16,
                            "windowDurationMins": 300,
                            "resetsAt": 1785000000,
                        },
                    },
                },
            },
        },
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    assert [item.window_id for item in snapshot.windows] == [
        "codex:primary",
        "codex_spark:primary",
        "codex_spark:tertiary",
    ]
    assert snapshot.resolved_window("auto").window_id == "codex_spark:primary"
    assert snapshot.resolved_window("codex_spark:tertiary").display_period_label() == "5H"


def test_parse_rate_limits_response_rejects_when_no_window_exists() -> None:
    """服务端同时缺失 primary 和 secondary 时应给出明确结构错误。

    入参：无；测试构造两个窗口都为 null 的响应。
    返回：无返回值；断言通过代表 daemon 会保留上次成功缓存并记录可诊断错误。
    错误处理：没有可用窗口时由 Pydantic ValueError 报告。
    副作用：无。
    """

    with pytest.raises(ValueError, match="usable quota window"):
        parse_rate_limits_response(
            {
                "id": 2,
                "result": {
                    "rateLimits": {
                        "primary": None,
                        "secondary": None,
                    }
                },
            }
        )
