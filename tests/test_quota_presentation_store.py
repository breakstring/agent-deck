"""Codex quota 展示策略持久化与归约测试。

这些测试只读写 pytest 临时目录并构造内存快照；不启动 Codex app-server、不访问真实 N4 Pro，
验证不同 limit 的短标签、顺序和隐藏规则不会改变原始 quota 快照。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_deck.adapters.codex_quota import CodexQuotaSnapshot
from agent_deck.server.quota_presentation_store import (
    QuotaPresentation,
    QuotaPresentationRule,
    QuotaPresentationStoreError,
    load_quota_presentation,
    save_quota_presentation,
)


def test_presentation_orders_labels_and_hides_by_limit_id() -> None:
    """展示策略应按 limit_id 处理任意数量窗口，且不改动原始快照。

    入参：无；测试构造常规 Codex、Spark 和一个未来附加额度。
    返回：无返回值；断言通过代表规则不依赖 primary/secondary 槽位。
    错误处理：排序、隐藏或展示标签错误时由 pytest 报告。
    副作用：无；只创建内存模型。
    """

    snapshot = _snapshot()
    presentation = QuotaPresentation(
        rules=(
            QuotaPresentationRule(limit_id="codex", label="Codex", order=20),
            QuotaPresentationRule(limit_id="codex_spark", label="Spark", order=10),
            QuotaPresentationRule(limit_id="future", visible=False),
        )
    )

    result = presentation.present(snapshot)
    displayed = result.display_snapshot()

    assert [item.window_id for item in result.windows] == [
        "codex_spark:primary",
        "codex:primary",
    ]
    assert [item.presentation_label for item in result.windows] == ["Spark", "Codex"]
    assert displayed is not None
    assert [item.window_id for item in displayed.windows] == [
        "codex_spark:primary",
        "codex:primary",
    ]
    assert [item.window_id for item in snapshot.windows] == [
        "codex:primary",
        "codex_spark:primary",
        "future:primary",
    ]


def test_presentation_keeps_unmatched_limits_visible_by_default() -> None:
    """没有规则的未来 limit 默认必须继续进入展示集合。

    入参：无；测试仅配置主 Codex limit。
    返回：无返回值；断言通过代表升级后的未知限额不会被静默隐藏。
    错误处理：未匹配窗口被丢弃时由 pytest 报告。
    副作用：无。
    """

    result = QuotaPresentation(
        rules=(QuotaPresentationRule(limit_id="codex", label="Codex"),)
    ).present(_snapshot())

    assert [item.window_id for item in result.windows] == [
        "codex:primary",
        "codex_spark:primary",
        "future:primary",
    ]
    assert result.windows[1].presentation_label == "Spark"


def test_presentation_store_round_trips_and_rejects_duplicate_limit_rules(
    tmp_path: Path,
) -> None:
    """策略 JSON 应原子往返，并拒绝具有歧义的重复 limit_id 规则。

    入参：`tmp_path` 是 pytest 的隔离目录。
    返回：无返回值；断言通过代表 daemon 重启可恢复策略且配置歧义可见。
    错误处理：重复规则应在模型校验阶段抛 ValueError。
    副作用：在临时目录写入一个 JSON 文件。
    """

    presentation = QuotaPresentation(
        rules=(QuotaPresentationRule(limit_id="codex_spark", label="Spark", order=5),),
        unmatched_visible=False,
    )
    path = tmp_path / "quota-presentation.json"

    save_quota_presentation(presentation, path)

    assert load_quota_presentation(path) == presentation
    with pytest.raises(ValueError, match="不能重复"):
        QuotaPresentation(
            rules=(
                QuotaPresentationRule(limit_id="codex"),
                QuotaPresentationRule(limit_id="codex"),
            )
        )
    path.write_text('{"version": 2, "presentation": {}}', encoding="utf-8")
    with pytest.raises(QuotaPresentationStoreError, match="version"):
        load_quota_presentation(path)


def _snapshot() -> CodexQuotaSnapshot:
    """构造含三个独立 limit 的 quota 快照。

    入参：无。
    返回：固定的多 limit 快照。
    错误处理：字段非法时 Pydantic 抛异常。
    副作用：无。
    """

    reset_at = datetime(2026, 7, 20, 10, tzinfo=UTC)
    return CodexQuotaSnapshot(
        plan_type="pro",
        plan_display_name="Pro",
        windows=(
            {
                "window_id": "codex:primary",
                "limit_id": "codex",
                "used_percent": 20,
                "window_duration_mins": 10080,
                "resets_at": reset_at,
            },
            {
                "window_id": "codex_spark:primary",
                "limit_id": "codex_spark",
                "limit_name": "GPT-5.3-Codex-Spark",
                "used_percent": 40,
                "window_duration_mins": 10080,
                "resets_at": reset_at,
            },
            {
                "window_id": "future:primary",
                "limit_id": "future",
                "limit_name": "Future Limit",
                "used_percent": 50,
                "window_duration_mins": 43200,
                "resets_at": reset_at,
            },
        ),
    )
