"""Agent Deck 全局显示外观的版本化 JSON 持久化存储。

本模块只保存跨 Key 与 virtual panel 共用的显示外观，不保存 PETS、按键布局、灯光或
设备亮度。它不渲染图片、不访问 Web 或真实硬件，供 daemon 启动和配置 API 复用。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent_deck.rendering.appearance import DeckAppearanceSettings

_DISPLAY_APPEARANCE_ENV = "AGENT_DECK_DISPLAY_APPEARANCE"
_USER_DISPLAY_APPEARANCE_PATH = (
    Path.home() / "Library/Application Support/AgentDeck/deck-appearance.json"
)
_STORE_VERSION = 1


class DisplayAppearanceStoreError(ValueError):
    """表示显示外观文件无法读取、校验或写入。"""


def resolve_display_appearance_path(path: Path | None = None) -> Path:
    """解析显示外观的稳定用户级路径。

    入参：``path`` 是测试或调用方显式覆盖；为空时先读专用环境变量，再回退 Application
    Support。
    返回：展开用户目录后的路径，允许尚不存在。
    错误处理：无。
    副作用：只读一个环境变量，不写文件。
    """

    if path is not None:
        return path.expanduser()
    env_value = os.environ.get(_DISPLAY_APPEARANCE_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return _USER_DISPLAY_APPEARANCE_PATH


def load_display_appearance(path: Path) -> DeckAppearanceSettings | None:
    """从版本化 JSON envelope 读取显示外观。

    入参：``path`` 是精确设置文件。
    返回：文件不存在时返回 None；有效文件返回冻结设置。
    错误处理：I/O、JSON、版本或模型校验失败时抛专用错误。
    副作用：只读指定文件，不访问硬件。
    """

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DisplayAppearanceStoreError(
            f"无法读取显示外观文件 {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise DisplayAppearanceStoreError(
            f"显示外观文件 {path} 不是合法 JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise DisplayAppearanceStoreError(f"显示外观文件 {path} 顶层必须是 object")
    if data.get("version") != _STORE_VERSION:
        raise DisplayAppearanceStoreError(
            f"显示外观文件 {path} 的 version 不支持: {data.get('version')!r}"
        )
    nested_settings = data.get("settings")
    if nested_settings is not None and not isinstance(nested_settings, dict):
        raise DisplayAppearanceStoreError(f"显示外观文件 {path} 的 settings 必须是 object")
    settings_data = (
        nested_settings
        if isinstance(nested_settings, dict)
        else {"background_color": data.get("background_color")}
    )
    try:
        return DeckAppearanceSettings.model_validate(settings_data)
    except ValidationError as exc:
        raise DisplayAppearanceStoreError(
            f"显示外观文件 {path} 校验失败: {exc}"
        ) from exc


def save_display_appearance(
    settings: DeckAppearanceSettings,
    path: Path,
) -> None:
    """原子保存一份已校验显示外观。

    入参：``settings`` 是完整冻结设置；``path`` 是用户级或测试路径。
    返回：无显式返回。
    错误处理：建目录、临时写入或 replace 失败时抛专用错误。
    副作用：创建父目录并原子替换目标 JSON，不修改其他配置文件。
    """

    envelope = _build_store_envelope(settings)
    temp_path = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)
    except OSError as exc:
        raise DisplayAppearanceStoreError(
            f"无法写入显示外观文件 {path}: {exc}"
        ) from exc


def _build_store_envelope(settings: DeckAppearanceSettings) -> dict[str, Any]:
    """构造包含版本号的稳定磁盘 envelope。

    入参：``settings`` 是待保存外观。
    返回：JSON-safe dict。
    错误处理：无。
    副作用：无。
    """

    return {
        "version": _STORE_VERSION,
        **settings.model_dump(mode="json"),
    }
