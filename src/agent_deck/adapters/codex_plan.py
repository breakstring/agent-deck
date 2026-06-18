"""Codex plan type 的展示映射。

本模块只把 Codex app-server 返回的 `planType` 映射成 Agent Deck 的展示元数据。
它不读取 Codex、不访问网络、不启动子进程、不连接 daemon，也不访问 StreamDock 硬件。
映射结果同时服务于 CLI/API 的完整展示名和 N4 Pro 小屏上的短标签。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


PLAN_TYPE_SOURCE_URL: Final[str] = (
    "https://raw.githubusercontent.com/openai/codex/refs/heads/main/"
    "codex-rs/app-server-protocol/schema/typescript/PlanType.ts"
)


@dataclass(frozen=True)
class CodexPlanDisplay:
    """Codex plan 的展示元数据。

    入参：`raw_type` 是 app-server 原始 plan type；`short_label` 是 N4 Pro 等小屏主标签；
    `display_name` 是 CLI/API/详情面板使用的完整展示名；`family` 是用于分组和未来视觉主题的
    稳定类别。
    返回：不可变 dataclass 实例。
    错误处理：本类不主动校验字段集合；调用方应通过 `describe_codex_plan()` 构造。
    副作用：无。
    """

    raw_type: str | None
    short_label: str
    display_name: str
    family: str


_UNKNOWN_PLAN = CodexPlanDisplay(
    raw_type=None,
    short_label="Unknown",
    display_name="Unknown",
    family="unknown",
)

_PLAN_DISPLAYS: Final[dict[str, CodexPlanDisplay]] = {
    "free": CodexPlanDisplay("free", "Free", "Free", "free"),
    "go": CodexPlanDisplay("go", "Go", "Go", "go"),
    "plus": CodexPlanDisplay("plus", "Plus", "Plus", "plus"),
    "pro": CodexPlanDisplay("pro", "Pro", "Pro", "pro"),
    "prolite": CodexPlanDisplay("prolite", "ProLite", "ProLite", "pro"),
    "team": CodexPlanDisplay("team", "Team", "Team", "team"),
    "self_serve_business_usage_based": CodexPlanDisplay(
        "self_serve_business_usage_based",
        "Biz",
        "Business",
        "business",
    ),
    "business": CodexPlanDisplay("business", "Biz", "Business", "business"),
    "enterprise_cbp_usage_based": CodexPlanDisplay(
        "enterprise_cbp_usage_based",
        "Ent",
        "Enterprise",
        "enterprise",
    ),
    "enterprise": CodexPlanDisplay("enterprise", "Ent", "Enterprise", "enterprise"),
    "edu": CodexPlanDisplay("edu", "Edu", "Education", "education"),
    "unknown": _UNKNOWN_PLAN,
}


def describe_codex_plan(plan_type: str | None) -> CodexPlanDisplay:
    """把 Codex plan type 映射为展示元数据。

    入参：`plan_type` 是 Codex app-server 返回的原始类型；允许为 `None`。
    返回：`CodexPlanDisplay`；已知类型返回规范映射，未知非空类型保留原值并归类为
    `unknown`，空值返回 `Unknown`。
    错误处理：无；所有输入都收敛为展示元数据。
    副作用：无。
    """

    if not plan_type:
        return _UNKNOWN_PLAN
    known = _PLAN_DISPLAYS.get(plan_type)
    if known is not None:
        return known
    return CodexPlanDisplay(
        raw_type=plan_type,
        short_label=plan_type,
        display_name=plan_type,
        family="unknown",
    )


def display_plan_name(plan_type: str | None) -> str:
    """返回 Codex plan 的完整展示名。

    入参：`plan_type` 是 Codex app-server 返回的原始类型；允许为 `None`。
    返回：适合 CLI/API/详情面板使用的展示名。
    错误处理：无；未知值按原值返回，空值返回 `Unknown`。
    副作用：无。
    """

    return describe_codex_plan(plan_type).display_name
