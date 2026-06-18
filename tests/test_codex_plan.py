"""Codex plan 展示映射测试。

这些测试只验证本地 plan type 到 Agent Deck 展示元数据的纯函数映射，不启动 Codex、
不访问网络、不读取账号 quota，也不触碰 StreamDock 硬件。
"""

from __future__ import annotations

from agent_deck.adapters.codex_plan import describe_codex_plan, display_plan_name


def test_prolite_uses_official_short_label() -> None:
    """`prolite` 应按官方口径展示为 ProLite。

    入参：无。
    返回：无返回值；断言通过表示短标签和完整展示名都使用 `ProLite`。
    错误处理：映射缺失或仍显示旧的 `5x Pro` 时由 pytest 断言报告。
    副作用：无。
    """

    plan = describe_codex_plan("prolite")

    assert plan.raw_type == "prolite"
    assert plan.short_label == "ProLite"
    assert plan.display_name == "ProLite"
    assert plan.family == "pro"
    assert display_plan_name("prolite") == "ProLite"


def test_business_and_enterprise_use_short_hardware_labels() -> None:
    """Business 和 Enterprise 系列应给 N4 Pro 提供短标签。

    入参：无。
    返回：无返回值；断言通过表示 usage-based 原始类型不会污染短标签。
    错误处理：短标签过长或 usage-based 泄露到展示名时由 pytest 断言报告。
    副作用：无。
    """

    business = describe_codex_plan("self_serve_business_usage_based")
    enterprise = describe_codex_plan("enterprise_cbp_usage_based")

    assert business.short_label == "Biz"
    assert business.display_name == "Business"
    assert business.family == "business"
    assert enterprise.short_label == "Ent"
    assert enterprise.display_name == "Enterprise"
    assert enterprise.family == "enterprise"


def test_unknown_plan_preserves_raw_type_for_debugging() -> None:
    """未知 plan 应保留原始 type，方便协议变化时排查。

    入参：无。
    返回：无返回值；断言通过表示未知值不被误归类。
    错误处理：未知值被吞掉或空值处理错误时由 pytest 断言报告。
    副作用：无。
    """

    custom = describe_codex_plan("future_plan")
    missing = describe_codex_plan(None)

    assert custom.short_label == "future_plan"
    assert custom.display_name == "future_plan"
    assert custom.family == "unknown"
    assert missing.short_label == "Unknown"
    assert missing.display_name == "Unknown"
    assert missing.family == "unknown"
