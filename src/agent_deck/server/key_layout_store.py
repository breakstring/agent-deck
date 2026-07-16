"""N4 Pro key layout 的 JSON 持久化存储。

本模块负责把本地 GUI 保存的 10 键布局写入用户级 JSON 文件，并在 daemon 启动时读回。
它不启动 daemon、不访问真实硬件、不执行按键动作；调用方决定是否启用这个存储路径。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent_deck.rendering.key_surface import N4ProKeyLayout

_KEY_LAYOUT_ENV = "AGENT_DECK_N4PRO_KEY_LAYOUT"
_USER_KEY_LAYOUT_PATH = (
    Path.home() / "Library/Application Support/AgentDeck/n4pro-key-layout.json"
)
_STORE_VERSION = 2
_SUPPORTED_STORE_VERSIONS = frozenset({1, 2})
_DEVICE_PROFILE = "mirabox.n4pro"


class KeyLayoutStoreError(ValueError):
    """表示 N4 Pro key layout 文件无法读取、解析、校验或写入。

    入参：标准 `ValueError` 参数，通常包含面向 API/status 的错误说明。
    返回：异常实例。
    错误处理：调用方应捕获并降级默认布局或返回 HTTP 错误。
    副作用：异常本身不读写文件。
    """


def resolve_n4pro_key_layout_path(path: Path | None = None) -> Path:
    """解析 N4 Pro key layout 的持久化路径。

    入参：`path` 是调用方显式传入路径；为空时先读
    `AGENT_DECK_N4PRO_KEY_LAYOUT`，再使用用户级默认路径。
    返回：展开 `~` 后的路径；路径可以不存在。
    错误处理：本函数不读取文件，不因路径不存在抛错。
    副作用：只读取进程环境变量，不写文件、不访问硬件。
    """

    if path is not None:
        return path.expanduser()
    env_value = os.environ.get(_KEY_LAYOUT_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return _USER_KEY_LAYOUT_PATH


def load_n4pro_key_layout(path: Path) -> N4ProKeyLayout | None:
    """从 JSON 文件读取 N4 Pro key layout。

    入参：`path` 是 key layout JSON envelope 路径。
    返回：文件不存在时返回 None；文件存在且有效时返回 `N4ProKeyLayout`。
    错误处理：读取失败、JSON 非法、设备 profile 不匹配或 layout 校验失败时抛
    `KeyLayoutStoreError`。
    副作用：只读取指定文件，不写文件、不访问硬件或网络。
    """

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise KeyLayoutStoreError(f"无法读取 key layout 文件 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise KeyLayoutStoreError(f"key layout 文件 {path} 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise KeyLayoutStoreError(f"key layout 文件 {path} 顶层必须是 object")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise KeyLayoutStoreError(f"key layout 文件 {path} 缺少合法整数 version")
    if version not in _SUPPORTED_STORE_VERSIONS:
        raise KeyLayoutStoreError(
            f"key layout 文件 {path} 的 version 不支持: {version!r}"
        )
    if data.get("device_profile") != _DEVICE_PROFILE:
        raise KeyLayoutStoreError(
            f"key layout 文件 {path} 的 device_profile 不支持: "
            f"{data.get('device_profile')!r}"
        )
    layout_data = data.get("layout")
    if not isinstance(layout_data, dict):
        raise KeyLayoutStoreError(f"key layout 文件 {path} 缺少 layout object")
    try:
        return N4ProKeyLayout.model_validate(layout_data)
    except ValidationError as exc:
        raise KeyLayoutStoreError(f"key layout 文件 {path} 校验失败: {exc}") from exc


def save_n4pro_key_layout(layout: N4ProKeyLayout, path: Path) -> None:
    """把 N4 Pro key layout 原子写入 JSON 文件。

    入参：`layout` 是已校验的 10 键布局；`path` 是目标 JSON 文件路径。
    返回：无显式返回值。
    错误处理：目录创建、临时文件写入或 replace 失败时抛 `KeyLayoutStoreError`。
    副作用：创建目标父目录，写入同目录临时文件，并用 replace 更新目标文件。
    """

    envelope = _build_store_envelope(layout)
    temp_path = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)
    except OSError as exc:
        raise KeyLayoutStoreError(f"无法写入 key layout 文件 {path}: {exc}") from exc


def _build_store_envelope(layout: N4ProKeyLayout) -> dict[str, Any]:
    """构造磁盘 JSON envelope。

    入参：`layout` 是已校验的 N4 Pro key layout。
    返回：包含版本、设备 profile 和 layout JSON 的 dict。
    错误处理：无。
    副作用：无。
    """

    return {
        "version": _STORE_VERSION,
        "device_profile": _DEVICE_PROFILE,
        "layout": layout.model_dump(mode="json"),
    }
