"""ChatGPT Desktop 内置宠物图集的只读 ASAR catalog。

本模块只从已安装 ChatGPT/Codex App 的 ``app.asar`` header 发现内置 spritesheet，并在
调用方实际需要某只角色时按 offset 解码对应 WebP。它不启动 App、不写 App bundle、不把
素材复制进仓库，也不扫描其他应用目录；已解码图集只保存在进程内并可按活动角色主动释放。
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from io import BytesIO
from pathlib import Path
from typing import Final

from PIL import Image

from agent_deck.adapters.codex_pet import (
    CODEX_PET_VERSION_GEOMETRY,
    CodexPetAsset,
    CodexPetLoadError,
    CodexPetManifest,
    normalize_codex_pet_spritesheet,
)

_DEFAULT_APP_ASAR_PATHS: Final[tuple[Path, ...]] = (
    Path("/Applications/ChatGPT.app/Contents/Resources/app.asar"),
    Path("/Applications/Codex.app/Contents/Resources/app.asar"),
)
"""只检查已知 OpenAI Desktop App bundle，不遍历 ``/Applications``。"""

_MAX_ASAR_HEADER_BYTES: Final[int] = 32 * 1024 * 1024
"""拒绝异常巨大的 ASAR header，避免损坏文件触发无界内存分配。"""

_SPRITESHEET_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^webview/assets/"
    r"(?P<asset_id>[a-z0-9][a-z0-9-]*)-spritesheet-v(?P<asset_revision>[0-9]+)-"
    r"[^/]+\.webp$"
)
"""当前 ChatGPT Desktop 内置宠物资源的稳定文件名形状。"""

_PREFERRED_ASSET_ORDER: Final[tuple[str, ...]] = (
    "codex",
    "dewey",
    "rocky",
    "hoots",
    "seedy",
    "stacky",
    "fireball",
    "null-signal",
    "bsod",
)
"""内置宠物的稳定展示顺序；未知新宠物追加到末尾。"""

_DISPLAY_NAMES: Final[dict[str, str]] = {
    "codex": "Codex",
    "dewey": "Dewey",
    "rocky": "Rocky",
    "hoots": "Hoots",
    "seedy": "Seedy",
    "stacky": "Stacky",
    "fireball": "Fireball",
    "null-signal": "Null Signal",
    "bsod": "BSOD",
}


class CodexBuiltinPetCatalogStatus(StrEnum):
    """描述内置宠物 catalog 的只读发现结果。"""

    LOADED = "loaded"
    APP_UNAVAILABLE = "app_unavailable"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class CodexBuiltinPetDescriptor:
    """描述一个尚未解码的 ASAR 内置宠物条目。

    ``data_offset`` 是 ASAR 文件内绝对 byte offset；``size`` 是受 header 约束的资源长度；
    ``source_fingerprint`` 绑定 App 文件 stat、内部路径和 entry 元数据。
    """

    asset_id: str
    display_name: str
    asar_path: Path
    internal_path: str
    data_offset: int
    size: int
    asset_revision: int
    source_fingerprint: str


@dataclass(frozen=True, slots=True)
class CodexBuiltinPetCatalogSnapshot:
    """一次内置宠物目录发现的不可变结果。"""

    status: CodexBuiltinPetCatalogStatus
    descriptors: tuple[CodexBuiltinPetDescriptor, ...]
    updated_at: datetime
    error: str | None = None

    @property
    def descriptor_by_id(self) -> dict[str, CodexBuiltinPetDescriptor]:
        """返回按 asset ID 索引的临时字典。"""

        return {descriptor.asset_id: descriptor for descriptor in self.descriptors}


class CodexBuiltinPetCatalog:
    """按需发现并解码 ChatGPT Desktop 内置宠物。

    构造时可注入精确 ``app.asar`` 路径用于测试；``resolve`` 仅解析 header，``load_asset``
    才读取单个 WebP。catalog 根据 App stat 指纹复用结果，并把活动外图集从内存缓存释放。
    """

    def __init__(self, *, app_asar_paths: Iterable[Path] | None = None) -> None:
        """创建未扫描 catalog；不在构造阶段访问文件。"""

        self._app_asar_paths = tuple(app_asar_paths or _DEFAULT_APP_ASAR_PATHS)
        self._source_stat_key: tuple[str, int, int] | None = None
        self._snapshot: CodexBuiltinPetCatalogSnapshot | None = None
        self._asset_cache: dict[str, CodexPetAsset] = {}

    def resolve(
        self,
        *,
        now: datetime | None = None,
    ) -> CodexBuiltinPetCatalogSnapshot:
        """只读发现第一个已安装 OpenAI Desktop App 中的内置宠物条目。

        找不到 App 返回 ``APP_UNAVAILABLE``；header/entry 非法返回 ``INVALID``。同一 App 的
        path、mtime 和 size 未变化时复用已有 snapshot，不重复解析 1MB 级 header。
        """

        updated_at = _aware_now(now)
        asar_path = next((path for path in self._app_asar_paths if path.is_file()), None)
        if asar_path is None:
            self._source_stat_key = None
            self._asset_cache.clear()
            self._snapshot = CodexBuiltinPetCatalogSnapshot(
                status=CodexBuiltinPetCatalogStatus.APP_UNAVAILABLE,
                descriptors=(),
                updated_at=updated_at,
                error="未找到已安装 ChatGPT/Codex App 的 app.asar",
            )
            return self._snapshot

        try:
            stat = asar_path.stat()
            stat_key = (str(asar_path), stat.st_mtime_ns, stat.st_size)
        except OSError as exc:
            return self._invalid_snapshot(updated_at, exc)
        if self._snapshot is not None and stat_key == self._source_stat_key:
            return CodexBuiltinPetCatalogSnapshot(
                status=self._snapshot.status,
                descriptors=self._snapshot.descriptors,
                updated_at=updated_at,
                error=self._snapshot.error,
            )

        try:
            data_base, header = _read_asar_header(asar_path)
            descriptors = _discover_descriptors(
                asar_path=asar_path,
                data_base=data_base,
                header=header,
                file_size=stat.st_size,
                stat_key=stat_key,
            )
            if not descriptors:
                raise CodexPetLoadError("ChatGPT App 中未发现兼容内置宠物图集")
        except (OSError, ValueError, json.JSONDecodeError, CodexPetLoadError) as exc:
            self._source_stat_key = stat_key
            self._asset_cache.clear()
            return self._invalid_snapshot(updated_at, exc)

        valid_fingerprints = {
            descriptor.source_fingerprint for descriptor in descriptors
        }
        self._asset_cache = {
            asset_id: asset
            for asset_id, asset in self._asset_cache.items()
            if asset.source_fingerprint in valid_fingerprints
        }
        self._source_stat_key = stat_key
        self._snapshot = CodexBuiltinPetCatalogSnapshot(
            status=CodexBuiltinPetCatalogStatus.LOADED,
            descriptors=descriptors,
            updated_at=updated_at,
        )
        return self._snapshot

    def load_asset(
        self,
        descriptor: CodexBuiltinPetDescriptor,
        *,
        loaded_at: datetime | None = None,
    ) -> CodexPetAsset:
        """按 descriptor 精确 offset 解码一只内置宠物。

        descriptor 必须来自当前 snapshot；资源范围、WebP 解码、v1/v2 几何和透明残留都会再次
        校验。成功结果缓存在内存；不会把资源解包到磁盘。
        """

        cached = self._asset_cache.get(descriptor.asset_id)
        if (
            cached is not None
            and cached.source_fingerprint == descriptor.source_fingerprint
        ):
            return cached
        current = (
            self._snapshot.descriptor_by_id.get(descriptor.asset_id)
            if self._snapshot is not None
            else None
        )
        if current != descriptor:
            raise CodexPetLoadError("内置宠物 descriptor 已过期")

        try:
            with descriptor.asar_path.open("rb") as handle:
                handle.seek(descriptor.data_offset)
                payload = handle.read(descriptor.size)
            if len(payload) != descriptor.size:
                raise CodexPetLoadError("内置宠物图集读取长度不足")
            with Image.open(BytesIO(payload)) as source:
                source.load()
                spritesheet = source.convert("RGBA")
        except CodexPetLoadError:
            raise
        except Exception as exc:
            raise CodexPetLoadError(
                f"内置宠物图集无法解码: {type(exc).__name__}"
            ) from exc

        sprite_version = _sprite_version_for_size(spritesheet.size)
        normalized, warnings = normalize_codex_pet_spritesheet(
            spritesheet,
            sprite_version_number=sprite_version,
        )
        manifest = CodexPetManifest.model_validate(
            {
                "id": descriptor.asset_id,
                "displayName": descriptor.display_name,
                "description": "ChatGPT Desktop built-in pet",
                "spritesheetPath": descriptor.internal_path,
                "spriteVersionNumber": sprite_version,
            }
        )
        asset = CodexPetAsset(
            selected_avatar_id=f"builtin:{descriptor.asset_id}",
            manifest=manifest,
            package_dir=descriptor.asar_path.parent,
            manifest_path=descriptor.asar_path,
            spritesheet_path=Path(
                f"{descriptor.asar_path}!/{descriptor.internal_path}"
            ),
            spritesheet=normalized,
            loaded_at=_aware_now(loaded_at),
            source_fingerprint=descriptor.source_fingerprint,
            warnings=warnings,
        )
        self._asset_cache[descriptor.asset_id] = asset
        return asset

    def retain_assets(self, asset_ids: Iterable[str]) -> None:
        """只保留当前活动角色使用的已解码图集，降低 daemon 常驻内存。"""

        retained = frozenset(asset_ids)
        self._asset_cache = {
            asset_id: asset
            for asset_id, asset in self._asset_cache.items()
            if asset_id in retained
        }

    def _invalid_snapshot(
        self,
        updated_at: datetime,
        error: BaseException,
    ) -> CodexBuiltinPetCatalogSnapshot:
        """把 catalog 异常转换成无路径泄露的短诊断。"""

        self._asset_cache.clear()
        self._snapshot = CodexBuiltinPetCatalogSnapshot(
            status=CodexBuiltinPetCatalogStatus.INVALID,
            descriptors=(),
            updated_at=updated_at,
            error=_short_error("读取 ChatGPT 内置宠物失败", error),
        )
        return self._snapshot


def _read_asar_header(asar_path: Path) -> tuple[int, dict[str, object]]:
    """读取并校验标准 Electron ASAR header，返回数据区起点和 JSON object。"""

    file_size = asar_path.stat().st_size
    with asar_path.open("rb") as handle:
        prefix = handle.read(16)
        if len(prefix) != 16:
            raise CodexPetLoadError("app.asar header 不完整")
        marker, header_block_size, header_string_size, header_json_size = struct.unpack(
            "<4I",
            prefix,
        )
        if marker != 4:
            raise CodexPetLoadError("app.asar marker 不兼容")
        if (
            header_json_size <= 0
            or header_json_size > _MAX_ASAR_HEADER_BYTES
            or header_json_size > header_string_size
            or header_string_size > header_block_size
        ):
            raise CodexPetLoadError("app.asar header 长度无效")
        data_base = 8 + header_block_size
        if data_base > file_size or 16 + header_json_size > data_base:
            raise CodexPetLoadError("app.asar 数据区边界无效")
        raw = handle.read(header_json_size)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise CodexPetLoadError("app.asar header 顶层不是 object")
    return data_base, parsed


def _discover_descriptors(
    *,
    asar_path: Path,
    data_base: int,
    header: dict[str, object],
    file_size: int,
    stat_key: tuple[str, int, int],
) -> tuple[CodexBuiltinPetDescriptor, ...]:
    """遍历 ASAR header，提取边界合法的内置 spritesheet descriptor。"""

    found: list[CodexBuiltinPetDescriptor] = []
    for internal_path, entry in _walk_asar_files(header):
        match = _SPRITESHEET_PATTERN.match(internal_path)
        if match is None or bool(entry.get("unpacked")):
            continue
        raw_offset = entry.get("offset")
        raw_size = entry.get("size")
        if not isinstance(raw_offset, str) or not raw_offset.isdigit():
            raise CodexPetLoadError("内置宠物 ASAR offset 无效")
        if not isinstance(raw_size, int) or raw_size <= 0:
            raise CodexPetLoadError("内置宠物 ASAR size 无效")
        data_offset = data_base + int(raw_offset)
        if data_offset < data_base or data_offset + raw_size > file_size:
            raise CodexPetLoadError("内置宠物 ASAR entry 越界")
        asset_id = match.group("asset_id")
        asset_revision = int(match.group("asset_revision"))
        fingerprint = hashlib.sha256(
            "\0".join(
                (
                    *map(str, stat_key),
                    internal_path,
                    str(data_offset),
                    str(raw_size),
                )
            ).encode("utf-8")
        ).hexdigest()
        found.append(
            CodexBuiltinPetDescriptor(
                asset_id=asset_id,
                display_name=_DISPLAY_NAMES.get(
                    asset_id,
                    asset_id.replace("-", " ").title(),
                ),
                asar_path=asar_path,
                internal_path=internal_path,
                data_offset=data_offset,
                size=raw_size,
                asset_revision=asset_revision,
                source_fingerprint=fingerprint,
            )
        )

    preferred_rank = {
        asset_id: index for index, asset_id in enumerate(_PREFERRED_ASSET_ORDER)
    }
    found.sort(
        key=lambda descriptor: (
            preferred_rank.get(descriptor.asset_id, len(preferred_rank)),
            descriptor.asset_id,
            -descriptor.asset_revision,
        )
    )
    deduplicated: dict[str, CodexBuiltinPetDescriptor] = {}
    for descriptor in found:
        deduplicated.setdefault(descriptor.asset_id, descriptor)
    return tuple(deduplicated.values())


def _walk_asar_files(
    node: dict[str, object],
    prefix: str = "",
) -> Iterable[tuple[str, dict[str, object]]]:
    """递归遍历 ASAR ``files`` object；非法节点抛明确 load error。"""

    files = node.get("files")
    if not isinstance(files, dict):
        raise CodexPetLoadError("app.asar files 节点无效")
    for name, raw_entry in files.items():
        if not isinstance(name, str) or not isinstance(raw_entry, dict):
            raise CodexPetLoadError("app.asar entry 结构无效")
        internal_path = f"{prefix}/{name}" if prefix else name
        if "files" in raw_entry:
            yield from _walk_asar_files(raw_entry, internal_path)
        else:
            yield internal_path, raw_entry


def _sprite_version_for_size(size: tuple[int, int]) -> int:
    """按官方固定几何反查 sprite contract 版本。"""

    for version, (width, height, _rows) in CODEX_PET_VERSION_GEOMETRY.items():
        if size == (width, height):
            return version
    raise CodexPetLoadError(f"内置宠物图集几何不受支持: {size[0]}x{size[1]}")


def _aware_now(value: datetime | None) -> datetime:
    """返回 timezone-aware 当前或注入时间。"""

    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("built-in pet catalog timestamp must be timezone-aware")
    return result


def _short_error(prefix: str, error: BaseException, *, limit: int = 240) -> str:
    """把 catalog 异常压缩为短单行诊断。"""

    message = " ".join(str(error).split())
    text = f"{prefix}: {type(error).__name__}: {message}"
    return text if len(text) <= limit else f"{text[: limit - 1]}…"
