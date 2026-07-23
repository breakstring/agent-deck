"""N4 Pro PETS 虚拟面板设置的版本化 JSON 持久化存储。

本模块只保存远端宠物来源和巡游速度等面板级偏好，不保存宠物图像、Agent 状态或 SSH
主机列表。它不连接远端、不读取 Codex 配置、不访问真实硬件，供 daemon GUI 与启动路径复用。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from agent_deck.config import CodexPetPatrolSpeed, CodexRemotePetSource

_PETS_PANEL_SETTINGS_ENV = "AGENT_DECK_N4PRO_PETS_PANEL_SETTINGS"
_USER_PETS_PANEL_SETTINGS_PATH = (
    Path.home() / "Library/Application Support/AgentDeck/n4pro-pets-panel.json"
)
_STORE_VERSION = 1
_DEVICE_PROFILE = "mirabox.n4pro"


class N4ProPetsPanelSettings(BaseModel):
    """描述 N4 Pro PETS 虚拟面板的可持久化用户设置。

    入参：``remote_pet_source`` 选择远端角色素材来源；``patrol_speed`` 是慢中快三档。
    返回：冻结模型，可直接跨 runtime/API 传递。
    错误处理：未知枚举值由 Pydantic 拒绝。
    副作用：模型自身不读写文件、不连接远端或硬件。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    remote_pet_source: CodexRemotePetSource = CodexRemotePetSource.BUILTIN_RANDOM
    patrol_speed: CodexPetPatrolSpeed = CodexPetPatrolSpeed.MEDIUM


class PetsPanelSettingsStoreError(ValueError):
    """表示 PETS 面板设置文件无法读取、校验或写入。"""


def resolve_n4pro_pets_panel_settings_path(path: Path | None = None) -> Path:
    """解析 N4 Pro PETS 面板设置的稳定用户级路径。

    入参：``path`` 是测试或调用方显式覆盖；为空时先读专用环境变量，再回退 Application
    Support。
    返回：展开用户目录后的路径，允许尚不存在。
    错误处理：无。
    副作用：只读一个环境变量，不写文件。
    """

    if path is not None:
        return path.expanduser()
    env_value = os.environ.get(_PETS_PANEL_SETTINGS_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return _USER_PETS_PANEL_SETTINGS_PATH


def load_n4pro_pets_panel_settings(
    path: Path,
) -> N4ProPetsPanelSettings | None:
    """从版本化 JSON envelope 读取 PETS 面板设置。

    入参：``path`` 是精确设置文件。
    返回：文件不存在时返回 None；有效文件返回冻结设置。
    错误处理：I/O、JSON、版本/profile 或模型校验失败时抛专用错误。
    副作用：只读指定文件，不访问 Codex、SSH 或硬件。
    """

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PetsPanelSettingsStoreError(
            f"无法读取 PETS 面板设置文件 {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise PetsPanelSettingsStoreError(
            f"PETS 面板设置文件 {path} 不是合法 JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PetsPanelSettingsStoreError(
            f"PETS 面板设置文件 {path} 顶层必须是 object"
        )
    if data.get("version") != _STORE_VERSION:
        raise PetsPanelSettingsStoreError(
            f"PETS 面板设置文件 {path} 的 version 不支持: {data.get('version')!r}"
        )
    if data.get("device_profile") != _DEVICE_PROFILE:
        raise PetsPanelSettingsStoreError(
            f"PETS 面板设置文件 {path} 的 device_profile 不支持: "
            f"{data.get('device_profile')!r}"
        )
    settings_data = data.get("settings")
    if not isinstance(settings_data, dict):
        raise PetsPanelSettingsStoreError(
            f"PETS 面板设置文件 {path} 缺少 settings object"
        )
    try:
        return N4ProPetsPanelSettings.model_validate(settings_data)
    except ValidationError as exc:
        raise PetsPanelSettingsStoreError(
            f"PETS 面板设置文件 {path} 校验失败: {exc}"
        ) from exc


def save_n4pro_pets_panel_settings(
    settings: N4ProPetsPanelSettings,
    path: Path,
) -> None:
    """原子保存一份已校验 PETS 面板设置。

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
        raise PetsPanelSettingsStoreError(
            f"无法写入 PETS 面板设置文件 {path}: {exc}"
        ) from exc


def _build_store_envelope(settings: N4ProPetsPanelSettings) -> dict[str, Any]:
    """构造包含版本和设备 profile 的稳定磁盘 envelope。

    入参：``settings`` 是待保存设置。
    返回：JSON-safe dict。
    错误处理：无。
    副作用：无。
    """

    return {
        "version": _STORE_VERSION,
        "device_profile": _DEVICE_PROFILE,
        "settings": settings.model_dump(mode="json"),
    }
