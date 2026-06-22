"""Logical panel 的 N4 Pro 背景屏渲染测试。

本文件只验证 logical panel plan 到 N4 Pro 800x480 背景图的纯内存渲染；不会读取 Codex、
不会执行 ccusage、不会访问 StreamDock SDK，也不会写用户配置。
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent_deck.adapters.codex_tokens import (
    CodexTokenPeriod,
    CodexTokenUsageSnapshot,
    CodexTokenUsageStats,
)
from agent_deck.rendering.logical_panel import tokens_panel_plan
from agent_deck.rendering.logical_panel_touchscreen import (
    render_logical_panel_touchscreen,
)
from agent_deck.rendering.n4pro_panel import (
    N4PRO_BACKGROUND_COLOR,
    N4PRO_LOGICAL_PANEL_VIEWPORT,
)


def test_render_tokens_logical_panel_touchscreen_uses_n4pro_background() -> None:
    """tokens logical panel 应能渲染到底部 touch-bar viewport。

    入参：无；测试内构造固定 token usage snapshot。
    返回：无返回值；断言通过代表 token 面板有可下发到 N4 Pro 的背景图。
    错误处理：尺寸、背景保护或 panel 内容缺失时由 pytest 报告。
    副作用：只创建 Pillow 内存图像。
    """

    image = render_logical_panel_touchscreen(
        tokens_panel_plan(_token_snapshot(), period=CodexTokenPeriod.TODAY)
    )

    assert image.size == (800, 480)
    assert image.getpixel((20, 20)) == N4PRO_BACKGROUND_COLOR
    probe_x = N4PRO_LOGICAL_PANEL_VIEWPORT.left + 40
    probe_y = N4PRO_LOGICAL_PANEL_VIEWPORT.top + 40
    assert image.getpixel((probe_x, probe_y)) != N4PRO_BACKGROUND_COLOR


def _token_snapshot() -> CodexTokenUsageSnapshot:
    """构造固定 token usage snapshot。

    入参：无。
    返回：用于渲染测试的 `CodexTokenUsageSnapshot`。
    错误处理：模型字段错误由 Pydantic 报告。
    副作用：无。
    """

    stats = CodexTokenUsageStats(
        input_tokens=6_465_793,
        output_tokens=436_596,
        reasoning_output_tokens=110_065,
        cache_read_tokens=111_106_560,
        total_tokens=118_008_949,
        cost_usd=100.98012500000002,
    )
    return CodexTokenUsageSnapshot(
        periods={period: stats for period in CodexTokenPeriod},
        updated_at=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
        raw={},
    )
