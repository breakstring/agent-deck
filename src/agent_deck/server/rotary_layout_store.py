"""N4 Pro 旋钮、灯圈组和控制台亮度布局的 JSON 持久化存储。

本模块只负责读取与原子写入用户级 N4 Pro rotary layout JSON envelope，不启动 daemon、不访问
真实 StreamDock、不执行系统控制。它与 key layout store 分离，使两类表面配置可以独立演进，
而 API 层可以在同一次“保存并应用”请求中协调它们。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent_deck.rendering.rotary_surface import N4ProRotaryLayout

_ROTARY_LAYOUT_ENV = "AGENT_DECK_N4PRO_ROTARY_LAYOUT"
_USER_ROTARY_LAYOUT_PATH = (
    Path.home() / "Library/Application Support/AgentDeck/n4pro-rotary-layout.json"
)
_STORE_VERSION = 1
_DEVICE_PROFILE = "mirabox.n4pro"


class RotaryLayoutStoreError(ValueError):
    """表示 N4 Pro rotary layout 文件无法读取、校验或写入。

    入参：标准 `ValueError` 参数，通常是 API/status 可展示的中文错误说明。
    返回：异常实例。
    错误处理：调用方应捕获并保留 daemon 中已有的 applied layout。
    副作用：异常对象不访问文件或硬件。
    """


def resolve_n4pro_rotary_layout_path(path: Path | None = None) -> Path:
    """解析 N4 Pro rotary layout 的稳定持久化路径。

    入参：`path` 是调用方显式覆盖；为空时先读取环境变量，再回退用户级 Application Support。
    返回：展开用户目录后的路径，路径本身可以不存在。
    错误处理：不因路径不存在抛错。
    副作用：只读取环境变量，不写文件、不访问硬件。
    """

    if path is not None:
        return path.expanduser()
    env_value = os.environ.get(_ROTARY_LAYOUT_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return _USER_ROTARY_LAYOUT_PATH


def load_n4pro_rotary_layout(path: Path) -> N4ProRotaryLayout | None:
    """从版本化 JSON envelope 读取一个完整 N4 Pro rotary layout。

    入参：`path` 是 envelope JSON 路径。
    返回：文件不存在时返回 None；有效文件时返回不可变 layout。
    错误处理：文件读取、JSON、device profile 或 Pydantic layout 校验错误都会抛
    `RotaryLayoutStoreError`。
    副作用：只读取指定文件，不写文件、不访问真实硬件。
    """

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RotaryLayoutStoreError(f"无法读取 rotary layout 文件 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RotaryLayoutStoreError(
            f"rotary layout 文件 {path} 不是合法 JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise RotaryLayoutStoreError(f"rotary layout 文件 {path} 顶层必须是 object")
    if data.get("device_profile") != _DEVICE_PROFILE:
        raise RotaryLayoutStoreError(
            f"rotary layout 文件 {path} 的 device_profile 不支持: "
            f"{data.get('device_profile')!r}"
        )
    layout_data = data.get("layout")
    if not isinstance(layout_data, dict):
        raise RotaryLayoutStoreError(f"rotary layout 文件 {path} 缺少 layout object")
    try:
        return N4ProRotaryLayout.model_validate(layout_data)
    except ValidationError as exc:
        raise RotaryLayoutStoreError(f"rotary layout 文件 {path} 校验失败: {exc}") from exc


def save_n4pro_rotary_layout(layout: N4ProRotaryLayout, path: Path) -> None:
    """把完整 N4 Pro rotary layout 原子写入目标 JSON 文件。

    入参：`layout` 是已校验 layout；`path` 是要写入的用户级或测试文件路径。
    返回：无显式返回值。
    错误处理：目录创建、临时写入或 replace 失败时抛 `RotaryLayoutStoreError`。
    副作用：创建父目录，写入同目录临时文件，并用原子 replace 更新目标。
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
        raise RotaryLayoutStoreError(f"无法写入 rotary layout 文件 {path}: {exc}") from exc


def _build_store_envelope(layout: N4ProRotaryLayout) -> dict[str, Any]:
    """构造稳定版本和 profile 标识包裹的磁盘 JSON 数据。

    入参：`layout` 是待持久化的不可变 N4 Pro rotary layout。
    返回：可直接 JSON 序列化的 envelope dict。
    错误处理：无。
    副作用：无。
    """

    return {
        "version": _STORE_VERSION,
        "device_profile": _DEVICE_PROFILE,
        "layout": layout.model_dump(mode="json"),
    }
