"""本机 App 图标缓存。

本模块把 macOS `.app` bundle 图标提取成 Agent Deck 自己可复用的 PNG 缓存。Web GUI
通过 URL 读取 `icon-96.png`，N4 Pro renderer 通过同一缓存读取 `key-112.png`。缓存可删除、
可重建，不作为用户配置的唯一真相；用户配置仍保存 App name/path/bundle id。
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from agent_deck.actions.apps import (
    LocalAppInfo,
    load_local_app_icon,
    resolve_local_app_icon_path,
)
from agent_deck.rendering.app_key import render_app_key_image

_APP_ICON_CACHE_ENV = "AGENT_DECK_APP_ICON_CACHE_DIR"
_DEFAULT_APP_ICON_CACHE_ROOT = (
    Path.home() / "Library/Application Support/AgentDeck/icon-cache/apps"
)
_CACHE_VERSION = 2
_ICON_96 = "icon-96.png"
_KEY_112 = "key-112.png"
_METADATA = "metadata.json"


class CachedAppIcon(BaseModel):
    """描述一个 App 图标缓存条目。

    入参：字段来自缓存目录和刷新结果。
    返回：frozen Pydantic model，可进入 `/ui/apps` 响应。
    错误处理：字段类型非法由 Pydantic 报告。
    副作用：模型自身不读写文件。
    """

    model_config = ConfigDict(frozen=True)

    cache_key: str = Field(min_length=1)
    status: str
    icon_url: str | None = None
    key_icon_url: str | None = None
    icon_path: str | None = None
    key_icon_path: str | None = None
    updated: bool = False


class AppIconCache:
    """管理 App 图标缓存目录。

    入参：`root` 是缓存根目录；`url_prefix` 是 Web 路由前缀。
    返回：实例方法可确保缓存存在、读取 key 图、解析 Web 文件路径。
    错误处理：单个 App 图标失败会降级 token fallback；目录写入失败向调用方抛 OSError。
    副作用：`ensure_app_icon()` 可能创建目录并写入 PNG/JSON 缓存文件。
    """

    def __init__(
        self,
        root: Path,
        *,
        url_prefix: str = "/ui/app-icons",
    ) -> None:
        """初始化 App icon cache。

        入参：`root` 是缓存根目录；`url_prefix` 是对外 URL 前缀。
        返回：无。
        错误处理：无；目录会在首次写入时创建。
        副作用：保存路径配置。
        """

        self.root = root.expanduser()
        self.url_prefix = url_prefix.rstrip("/")
        self._key_images: dict[str, Image.Image] = {}

    def ensure_for_app(
        self,
        app: LocalAppInfo,
        *,
        force: bool = False,
    ) -> CachedAppIcon:
        """确保 catalog 中的 App 已有图标缓存。

        入参：`app` 是 catalog scanner 返回的 App metadata；`force` 为 True 时强制重建。
        返回：缓存条目，包含 Web URL 和硬件 key 图路径。
        错误处理：图标解析失败自动使用 token fallback；目录写入失败按 OSError 传播。
        副作用：可能写入缓存 PNG 和 metadata。
        """

        return self.ensure(
            app_name=app.name,
            app_path=app.app_path,
            bundle_id=app.bundle_id,
            icon_token=app.icon_token,
            force=force,
        )

    def ensure(
        self,
        *,
        app_name: str | None,
        app_path: str | None,
        bundle_id: str | None,
        icon_token: str | None,
        icon_color: str | None = None,
        force: bool = False,
    ) -> CachedAppIcon:
        """确保指定 App identity 已有图标缓存。

        入参：App identity 来自 catalog 或 key binding；`force` 为 True 时无视 metadata 重建。
        返回：缓存条目；生成失败时仍尽量提供 token fallback 缓存。
        错误处理：目录写入失败按 OSError 传播。
        副作用：可能读取 `.app` bundle 并写入缓存文件。
        """

        cache_key = cache_key_for_app(bundle_id=bundle_id, app_path=app_path)
        cache_dir = self.root / cache_key
        icon_path = cache_dir / _ICON_96
        key_icon_path = cache_dir / _KEY_112
        metadata_path = cache_dir / _METADATA
        fingerprint = _fingerprint(app_path)
        if (
            not force
            and icon_path.is_file()
            and key_icon_path.is_file()
            and _metadata_matches(metadata_path, fingerprint)
        ):
            return self._entry(cache_key, status="ready", updated=False)

        cache_dir.mkdir(parents=True, exist_ok=True)
        icon_image = (
            load_local_app_icon(app_path, max_size=(96, 96)) if app_path else None
        )
        if icon_image is None:
            icon_image = render_app_key_image(
                app_name=app_name,
                app_path=None,
                icon_token=icon_token,
                icon_color=icon_color,
                size=(96, 96),
            ).convert("RGBA")
        _save_png(icon_image, icon_path)
        key_image = render_app_key_image(
            app_name=app_name,
            app_path=app_path,
            icon_token=icon_token,
            icon_color=icon_color,
        )
        _save_png(key_image, key_icon_path)
        _write_metadata(
            metadata_path,
            {
                "version": _CACHE_VERSION,
                "cache_key": cache_key,
                "bundle_id": bundle_id,
                "app_name": app_name,
                "app_path": app_path,
                "icon_token": icon_token,
                "fingerprint": fingerprint,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        self._key_images.pop(cache_key, None)
        return self._entry(cache_key, status="ready", updated=True)

    def key_image_for_binding(
        self,
        *,
        app_name: str | None,
        app_path: str | None,
        bundle_id: str | None,
        icon_token: str | None,
        icon_color: str | None = None,
    ) -> Image.Image | None:
        """读取适合硬件下发的缓存 key 图。

        入参：App binding identity 来自 `KeyPlan.payload`。
        返回：RGB `Image`；同一缓存版本会复用同一个进程内图片对象，缓存或读取失败时返回 None，
        让调用方 fallback。
        错误处理：坏缓存图片返回 None；写缓存失败向上传播。
        副作用：缓存缺失或过期时可能重建；首次读取一个缓存版本时把 Pillow 图保存在进程内，图标
        重建后会自动失效该引用。
        """

        entry = self.ensure(
            app_name=app_name,
            app_path=app_path,
            bundle_id=bundle_id,
            icon_token=icon_token,
            icon_color=icon_color,
        )
        if entry.key_icon_path is None:
            return None
        cached = self._key_images.get(entry.cache_key)
        if cached is not None:
            return cached
        try:
            with Image.open(entry.key_icon_path) as image:
                image.load()
                key_image = image.convert("RGB")
                self._key_images[entry.cache_key] = key_image
                return key_image
        except Exception:
            return None

    def resolve_file(self, cache_key: str, asset_name: str) -> Path | None:
        """把 Web icon URL 参数解析为缓存文件路径。

        入参：`cache_key` 来自 URL；`asset_name` 必须是允许的 PNG 文件名。
        返回：存在的缓存文件路径；非法或不存在时返回 None。
        错误处理：无。
        副作用：只检查路径和文件存在性。
        """

        if not _SAFE_CACHE_KEY_RE.fullmatch(cache_key):
            return None
        if asset_name not in {_ICON_96, _KEY_112}:
            return None
        path = self.root / cache_key / asset_name
        if not path.is_file():
            return None
        return path

    def _entry(
        self,
        cache_key: str,
        *,
        status: str,
        updated: bool,
    ) -> CachedAppIcon:
        """构造缓存条目模型。

        入参：`cache_key` 是目录名；`status` 是缓存状态；`updated` 表示本次是否重建。
        返回：`CachedAppIcon`。
        错误处理：字段非法由 Pydantic 抛出。
        副作用：无。
        """

        icon_path = self.root / cache_key / _ICON_96
        key_icon_path = self.root / cache_key / _KEY_112
        return CachedAppIcon(
            cache_key=cache_key,
            status=status,
            icon_url=f"{self.url_prefix}/{cache_key}/{_ICON_96}",
            key_icon_url=f"{self.url_prefix}/{cache_key}/{_KEY_112}",
            icon_path=str(icon_path),
            key_icon_path=str(key_icon_path),
            updated=updated,
        )


_SAFE_CACHE_KEY_RE = re.compile(r"[A-Za-z0-9._-]{1,160}")


def resolve_app_icon_cache_root(path: Path | None = None) -> Path:
    """解析 App icon cache 根目录。

    入参：`path` 是调用方显式路径；为空时先读环境变量，再使用用户级默认路径。
    返回：展开后的缓存根目录。
    错误处理：无；本函数不创建目录。
    副作用：只读取环境变量。
    """

    if path is not None:
        return path.expanduser()
    env_value = os.environ.get(_APP_ICON_CACHE_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return _DEFAULT_APP_ICON_CACHE_ROOT


def cache_key_for_app(*, bundle_id: str | None, app_path: str | None) -> str:
    """生成稳定缓存目录名。

    入参：优先使用 `bundle_id`；缺少 bundle id 时使用 app path hash。
    返回：只包含安全字符的缓存 key。
    错误处理：无。
    副作用：无。
    """

    raw = bundle_id or f"path-{sha256((app_path or 'app').encode('utf-8')).hexdigest()[:16]}"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")
    return safe[:160] or "app"


def _fingerprint(app_path: str | None) -> dict[str, Any]:
    """生成用于判断缓存是否过期的轻量指纹。

    入参：`app_path` 是 `.app` bundle 路径。
    返回：包含 app/icon 路径和 mtime 的 JSON-safe dict。
    错误处理：缺失路径或 stat 失败会记录 None。
    副作用：只读文件元数据。
    """

    app = Path(app_path).expanduser() if app_path else None
    icon_path = resolve_local_app_icon_path(app) if app is not None else None
    return {
        "app_path": str(app) if app is not None else None,
        "app_mtime_ns": _mtime_ns(app),
        "icon_path": str(icon_path) if icon_path is not None else None,
        "icon_mtime_ns": _mtime_ns(icon_path),
    }


def _metadata_matches(path: Path, fingerprint: dict[str, Any]) -> bool:
    """判断 metadata 是否仍匹配当前 App 图标来源。

    入参：`path` 是 metadata 文件；`fingerprint` 是当前指纹。
    返回：版本和指纹都匹配时返回 True。
    错误处理：文件缺失、坏 JSON 或结构错误返回 False。
    副作用：只读 metadata 文件。
    """

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return data.get("version") == _CACHE_VERSION and data.get("fingerprint") == fingerprint


def _write_metadata(path: Path, data: dict[str, Any]) -> None:
    """原子写入 metadata JSON。

    入参：`path` 是目标 metadata 文件；`data` 是 JSON-safe dict。
    返回：无。
    错误处理：写入失败按 OSError 传播。
    副作用：写同目录临时文件并 replace 目标文件。
    """

    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _save_png(image: Image.Image, path: Path) -> None:
    """保存 PNG 图像。

    入参：`image` 是 Pillow 图像；`path` 是目标路径。
    返回：无。
    错误处理：Pillow 或文件写入失败按异常传播。
    副作用：写 PNG 文件。
    """

    image.save(path, format="PNG")


def _mtime_ns(path: Path | None) -> int | None:
    """读取文件或目录 mtime。

    入参：`path` 是可选路径。
    返回：mtime ns 或 None。
    错误处理：stat 失败返回 None。
    副作用：只读文件元数据。
    """

    if path is None:
        return None
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None
