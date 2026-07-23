"""N4 Pro 按键配置表面的产品模型与投影。

本模块承接 GUI 中的 10 个主按键配置，把用户心智里的“快捷动作、Agent 状态、关闭”
转换成现有 renderer-neutral `KeyPlan`。它不读取或写入用户配置文件，不访问真实硬件，
不启动进程，也不解析 macOS App 图标；调用方负责保存配置和执行具体动作。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_deck.actions.keyboard import (
    KeyboardShortcutSpec,
    ShortcutIconSpec,
)
from agent_deck.core.state import AgentState
from agent_deck.rendering.layout import KeyAmbientOverlaySpec, KeyPlan
from agent_deck.rendering.visuals import resolve_visual_icon_spec

N4PRO_MAIN_KEY_COUNT = 10
"""第一版 GUI 配置覆盖的 N4 Pro 主按键数量。"""

_CODEX_DESKTOP_APP_NAMES: Final[frozenset[str]] = frozenset({"codex", "chatgpt"})
_CODEX_DESKTOP_APP_BUNDLE_IDS: Final[frozenset[str]] = frozenset(
    {
        "com.openai.chat",
        "com.openai.chatgpt",
        "com.openai.codex",
    }
)
_CODEX_DESKTOP_APP_BASENAMES: Final[frozenset[str]] = frozenset(
    {"codex.app", "chatgpt.app"}
)


class KeySurfaceKind(StrEnum):
    """用户可在第一版 GUI 中配置的主按键用途。

    入参：枚举值来自 API/GUI JSON。
    返回：字符串枚举，可直接序列化到 HTTP 响应。
    错误处理：未知字符串由 Pydantic 校验为 422。
    副作用：无。
    """

    UNASSIGNED = "unassigned"
    APP = "app"
    URL = "url"
    FOLDER = "folder"
    KEYBOARD_SHORTCUT = "keyboard_shortcut"
    AGENT = "agent"
    CODEX_PET = "codex_pet"
    QUOTA_STATUS = "quota_status"
    USAGE_SUMMARY = "usage_summary"
    DISABLED = "disabled"


class N4ProKeyBinding(BaseModel):
    """描述 N4 Pro 一个主按键的用户配置。

    入参：`index` 是 0-9 的主按键编号；`kind` 是 GUI 暴露的用途；App/URL/Folder
    按用途携带各自参数；`icon_token` 和 `icon_color` 是当前 GUI 原型生成预览图标的轻量字段；
    `ambient_overlay` 仅允许附加在已识别的 ChatGPT/Codex App 启动目标上。
    返回：frozen Pydantic model，可作为 runtime 内存布局的一部分。
    错误处理：key 越界、必需参数缺失或 kind/参数不匹配时抛 ValidationError。
    副作用：只保存内存数据，不读取应用、文件夹、favicon 或系统图标。
    """

    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=0, lt=N4PRO_MAIN_KEY_COUNT)
    kind: KeySurfaceKind
    label: str = ""
    app_name: str | None = None
    app_path: str | None = None
    bundle_id: str | None = None
    url: str | None = None
    path: str | None = None
    quota_window: str | None = None
    usage_period: str | None = None
    icon_token: str | None = None
    icon_color: str | None = None
    shortcut: KeyboardShortcutSpec | None = None
    icon: ShortcutIconSpec | None = None
    ambient_overlay: KeyAmbientOverlaySpec | None = None

    @field_validator("label", mode="before")
    @classmethod
    def _strip_label(cls, value: object) -> object:
        """清理可选显示标签。

        入参：`value` 是 GUI 传入的 label 字段值。
        返回：字符串会 trim；空字符串保持为空标签。
        错误处理：非字符串按原值交给 Pydantic 类型校验。
        副作用：无。
        """

        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator(
        "app_name",
        "app_path",
        "bundle_id",
        "url",
        "path",
        "quota_window",
        "usage_period",
        "icon_token",
        "icon_color",
        mode="before",
    )
    @classmethod
    def _strip_optional_string(cls, value: object) -> object:
        """清理 GUI 传入的可选字符串字段。

        入参：`value` 是任意待校验字段值。
        返回：字符串会 trim；空字符串归一为 None。
        错误处理：非字符串按原值交给 Pydantic 类型校验。
        副作用：无。
        """

        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @model_validator(mode="after")
    def _validate_action_fields(self) -> N4ProKeyBinding:
        """按 key kind 校验动作参数完整性。

        入参：已初步解析出的 binding。
        返回：参数完整的 binding 本身。
        错误处理：App 缺少名称或定位字段、URL 缺少 url、Folder 缺少 path 时抛 ValueError。
        副作用：无。
        """

        if self.kind == KeySurfaceKind.APP:
            if not self.app_name:
                raise ValueError("app key requires app_name")
            if not (self.bundle_id or self.app_path):
                raise ValueError("app key requires bundle_id or app_path")
        if self.kind == KeySurfaceKind.URL and not self.url:
            raise ValueError("url key requires url")
        if self.kind == KeySurfaceKind.FOLDER and not self.path:
            raise ValueError("folder key requires path")
        if self.kind == KeySurfaceKind.KEYBOARD_SHORTCUT and self.shortcut is None:
            raise ValueError("keyboard_shortcut key requires shortcut")
        if self.kind != KeySurfaceKind.KEYBOARD_SHORTCUT and self.shortcut is not None:
            raise ValueError("shortcut is only valid for keyboard_shortcut keys")
        if self.kind != KeySurfaceKind.KEYBOARD_SHORTCUT and self.icon is not None:
            raise ValueError("shortcut icon is only valid for keyboard_shortcut keys")
        if self.ambient_overlay is not None:
            if self.kind != KeySurfaceKind.APP:
                raise ValueError("ambient overlay is only valid for app keys")
            if not is_codex_desktop_app_target(
                app_name=self.app_name,
                app_path=self.app_path,
                bundle_id=self.bundle_id,
            ):
                raise ValueError(
                    "codex pet ambient overlay requires a recognized ChatGPT or Codex app target"
                )
        if (
            self.kind == KeySurfaceKind.QUOTA_STATUS
            and self.quota_window is not None
            and not self.quota_window.strip()
        ):
            raise ValueError("quota_status key requires a non-empty quota_window")
        if self.kind == KeySurfaceKind.USAGE_SUMMARY and self.usage_period not in {
            None,
            "today",
            "week",
            "month",
            "all",
        }:
            raise ValueError("usage_summary key requires usage_period today/week/month/all")
        return self

    def display_label(self) -> str:
        """返回适合 layout/status 展示的按键标签。

        入参：无。
        返回：优先使用显式 label，其次按用途返回 App 名称、URL、路径或产品默认文案。
        错误处理：无。
        副作用：无。
        """

        if self.label:
            return self.label
        if self.kind == KeySurfaceKind.APP and self.app_name:
            return self.app_name
        if self.kind == KeySurfaceKind.URL and self.url:
            return self.url
        if self.kind == KeySurfaceKind.FOLDER and self.path:
            return self.path
        if self.kind == KeySurfaceKind.KEYBOARD_SHORTCUT:
            return "快捷键"
        if self.kind == KeySurfaceKind.CODEX_PET:
            return "Codex 宠物"
        if self.kind == KeySurfaceKind.QUOTA_STATUS:
            return "订阅 / 限额状态"
        if self.kind == KeySurfaceKind.USAGE_SUMMARY:
            return "Token / 金额用量"
        if self.kind == KeySurfaceKind.UNASSIGNED:
            return "未设置快捷动作"
        if self.kind == KeySurfaceKind.AGENT:
            return "Agent"
        if self.kind == KeySurfaceKind.DISABLED:
            return ""
        return ""


class N4ProKeyLayout(BaseModel):
    """描述第一版 N4 Pro 10 个主按键布局。

    入参：`keys` 必须覆盖 0-9 每个主按键且不重复。
    返回：frozen Pydantic model，供 GUI API 和 layout projection 使用。
    错误处理：数量不为 10、index 重复或缺失时抛 ValidationError。
    副作用：只保存内存数据，不访问文件、硬件或网络。
    """

    model_config = ConfigDict(frozen=True)

    keys: tuple[N4ProKeyBinding, ...]

    @model_validator(mode="after")
    def _validate_complete_main_keys(self) -> N4ProKeyLayout:
        """校验布局完整覆盖 N4 Pro 10 个主键。

        入参：已解析布局。
        返回：完整布局本身。
        错误处理：index 集合不等于 0-9 时抛 ValueError。
        副作用：无。
        """

        indexes = [key.index for key in self.keys]
        expected = set(range(N4PRO_MAIN_KEY_COUNT))
        if len(indexes) != N4PRO_MAIN_KEY_COUNT or set(indexes) != expected:
            raise ValueError("n4pro key layout must contain indexes 0..9 exactly once")
        return self

    def sorted_keys(self) -> tuple[N4ProKeyBinding, ...]:
        """按物理 key index 返回稳定顺序。

        入参：无。
        返回：按 `index` 升序排列的新 tuple。
        错误处理：无。
        副作用：无。
        """

        return tuple(sorted(self.keys, key=lambda key: key.index))


def default_n4pro_key_layout() -> N4ProKeyLayout:
    """返回第一版 N4 Pro GUI 推荐默认布局。

    入参：无。
    返回：Key 1-5 为未设置快捷动作，Key 6-10 为 Agent 状态槽的布局。
    错误处理：若内置定义不完整会由 `N4ProKeyLayout` 校验异常暴露。
    副作用：只创建内存模型，不读取配置或硬件。
    """

    return N4ProKeyLayout(
        keys=tuple(
            N4ProKeyBinding(
                index=index,
                kind=KeySurfaceKind.UNASSIGNED
                if index < 5
                else KeySurfaceKind.AGENT,
            )
            for index in range(N4PRO_MAIN_KEY_COUNT)
        )
    )


def project_n4pro_key_layout(
    layout: N4ProKeyLayout,
    sorted_states: list[AgentState],
) -> list[KeyPlan]:
    """把 N4 Pro key layout 投影成前 10 个 `KeyPlan`。

    入参：`layout` 是用户保存的 N4 Pro 主键布局；`sorted_states` 是已按状态优先级排序的
    visible agent 列表。
    返回：长度为 10 的 `KeyPlan` 列表；Agent 槽只消费配置为 `agent` 的键位。
    错误处理：`KeyPlan` 字段校验失败按 Pydantic 异常传播。
    副作用：无；只读取输入并创建新模型。
    """

    projected: list[KeyPlan] = []
    agent_slot_index = 0
    for binding in layout.sorted_keys():
        if binding.kind == KeySurfaceKind.AGENT:
            state = (
                sorted_states[agent_slot_index]
                if agent_slot_index < len(sorted_states)
                else None
            )
            agent_slot_index += 1
            if state is None:
                projected.append(
                    KeyPlan(
                        index=binding.index,
                        label="",
                        role="agent_slot",
                        kind=KeySurfaceKind.AGENT.value,
                    )
                )
            else:
                projected.append(
                    KeyPlan(
                        index=binding.index,
                        label=state.display_name,
                        status=state.status,
                        visual=resolve_visual_icon_spec(state.status),
                        agent_key=state.agent_key,
                        intent="select_agent",
                        role="agent_slot",
                        kind=KeySurfaceKind.AGENT.value,
                    )
                )
            continue

        projected.append(_project_static_binding(binding))
    return projected


def _project_static_binding(binding: N4ProKeyBinding) -> KeyPlan:
    """把非 Agent binding 投影成单个 key plan。

    入参：`binding` 是 unassigned/app/url/folder/status/codex_pet/disabled 按键配置。
    返回：对应的 `KeyPlan`。
    错误处理：未知 kind 降级为无 intent 空键。
    副作用：无。
    """

    if binding.kind == KeySurfaceKind.UNASSIGNED:
        return KeyPlan(
            index=binding.index,
            label=binding.display_label(),
            intent="show_brand_feedback",
            role="user_action",
            kind=binding.kind.value,
        )
    if binding.kind == KeySurfaceKind.APP:
        return KeyPlan(
            index=binding.index,
            label=binding.display_label(),
            intent="open_or_focus_app",
            role="user_action",
            kind=binding.kind.value,
            action="open_or_focus_app",
            payload=_compact_payload(
                app_name=binding.app_name,
                app_path=binding.app_path,
                bundle_id=binding.bundle_id,
                icon_token=binding.icon_token,
                icon_color=binding.icon_color,
            ),
            ambient_overlay=binding.ambient_overlay,
        )
    if binding.kind == KeySurfaceKind.URL:
        return KeyPlan(
            index=binding.index,
            label=binding.display_label(),
            intent="open_url",
            role="user_action",
            kind=binding.kind.value,
            action="open_url",
            payload=_compact_payload(url=binding.url),
        )
    if binding.kind == KeySurfaceKind.FOLDER:
        return KeyPlan(
            index=binding.index,
            label=binding.display_label(),
            intent="open_path",
            role="user_action",
            kind=binding.kind.value,
            action="open_path",
            payload=_compact_payload(path=binding.path),
        )
    if binding.kind == KeySurfaceKind.KEYBOARD_SHORTCUT:
        return KeyPlan(
            index=binding.index,
            label=binding.display_label(),
            intent="send_keyboard_shortcut",
            role="user_action",
            kind=binding.kind.value,
            action="send_keyboard_shortcut",
            shortcut=binding.shortcut,
            shortcut_icon=binding.icon or ShortcutIconSpec(),
        )
    if binding.kind == KeySurfaceKind.CODEX_PET:
        return KeyPlan(
            index=binding.index,
            label=binding.display_label(),
            role="ambient",
            kind=binding.kind.value,
        )
    if binding.kind == KeySurfaceKind.QUOTA_STATUS:
        return KeyPlan(
            index=binding.index,
            label=binding.display_label(),
            intent="cycle_quota_status_window",
            role="user_action",
            kind=binding.kind.value,
            payload={"quota_window": binding.quota_window or "auto"},
        )
    if binding.kind == KeySurfaceKind.USAGE_SUMMARY:
        return KeyPlan(
            index=binding.index,
            label=binding.display_label(),
            intent="cycle_usage_summary_period",
            role="user_action",
            kind=binding.kind.value,
            payload={"usage_period": binding.usage_period or "today"},
        )
    if binding.kind == KeySurfaceKind.DISABLED:
        return KeyPlan(
            index=binding.index,
            label="",
            role="disabled",
            kind=binding.kind.value,
        )
    return KeyPlan(index=binding.index)


def _compact_payload(**items: str | None) -> dict[str, str]:
    """移除 payload 中的空字段。

    入参：`items` 是 key/value 字符串字段。
    返回：只包含非空字符串的新 dict。
    错误处理：无。
    副作用：无。
    """

    return {key: value for key, value in items.items() if value}


def is_codex_desktop_app_target(
    *,
    app_name: str | None,
    app_path: str | None,
    bundle_id: str | None,
) -> bool:
    """识别可关联 Codex 任务状态的 OpenAI 桌面 App 启动目标。

    入参：App catalog/binding 中的显示名、bundle 路径与 bundle id；支持当前
    ``ChatGPT.app`` 以及历史 ``Codex.app`` 身份。
    返回：命中已知 OpenAI bundle id，或命中已知 App basename 且名称也是已知别名时为 True。
    错误处理：缺失或未知字段返回 False，不按显示名独立放行同名普通 App。
    副作用：只规范化字符串和解析路径 basename，不访问文件系统。
    """

    normalized_name = (app_name or "").strip().casefold()
    normalized_bundle_id = (bundle_id or "").strip().casefold()
    if normalized_bundle_id in _CODEX_DESKTOP_APP_BUNDLE_IDS:
        return True
    if not app_path or normalized_name not in _CODEX_DESKTOP_APP_NAMES:
        return False
    return Path(app_path.strip()).name.casefold() in _CODEX_DESKTOP_APP_BASENAMES
