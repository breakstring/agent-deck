"""快捷键自定义图标的内容寻址本地资产存储。

本模块接收有限大小的用户图片，校验真实格式和尺寸后统一转为 PNG，并生成 Web 预览图与
N4 Pro 112px 按键图。资产以规范化 PNG 的 SHA-256 命名；模块不删除孤立资产、不访问网络，
也不读取快捷键布局。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

MAX_SHORTCUT_ICON_BYTES = 5 * 1024 * 1024
"""单个快捷键自定义图标允许的最大上传字节数。"""

MAX_SHORTCUT_ICON_DIMENSION = 4096
"""快捷键自定义图标允许的最大单边像素数。"""

_ASSET_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "ICO"})
_NORMALIZED_IMAGE = "normalized.png"
_PREVIEW_IMAGE = "preview-96.png"
_KEY_IMAGE = "key-112.png"
_METADATA = "metadata.json"
_CACHE_VERSION = 1
_USER_SHORTCUT_ICON_ROOT = (
    Path.home() / "Library/Application Support/AgentDeck/shortcut-icons"
)


class ShortcutIconAsset(BaseModel):
    """描述一个已规范化的快捷键图标资产。

    入参：内容 hash、原始宽高、预览 URL、硬件 key URL 和可选文件名。
    返回：frozen JSON-safe 模型，供 GUI 上传响应和只读 lookup 使用。
    错误处理：asset id 或尺寸非法由 Pydantic 报告。
    副作用：模型自身无副作用。
    """

    model_config = ConfigDict(frozen=True)

    asset_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0, le=MAX_SHORTCUT_ICON_DIMENSION)
    height: int = Field(gt=0, le=MAX_SHORTCUT_ICON_DIMENSION)
    preview_url: str
    key_icon_url: str
    filename: str | None = None
    created_at: datetime


def resolve_shortcut_icon_store_root(path: Path | None = None) -> Path:
    """解析快捷键自定义图标存储根目录。

    入参：调用方可选显式路径；为空时读 ``AGENT_DECK_SHORTCUT_ICON_ROOT``，再用用户目录。
    返回：展开 ``~`` 后的路径，不要求已存在。
    错误处理：无；不读取目录内容。
    副作用：只读进程环境变量。
    """

    if path is not None:
        return path.expanduser()
    env_value = os.environ.get("AGENT_DECK_SHORTCUT_ICON_ROOT")
    if env_value:
        return Path(env_value).expanduser()
    return _USER_SHORTCUT_ICON_ROOT


class ShortcutIconStore:
    """保存并读取内容寻址的快捷键图标资产。

    入参：资产根目录。
    返回：可用于 API 上传、文件解析和硬件图加载的 store。
    错误处理：图片/尺寸/大小非法抛 ValueError；文件系统失败抛 OSError。
    副作用：上传时创建资产目录并写 PNG/metadata；lookup 和 key image 只读文件。
    """

    def __init__(self, root: Path) -> None:
        """保存资产根目录但不主动创建。

        入参：``root`` 是内容寻址目录的根。
        返回：无显式返回值。
        错误处理：无。
        副作用：无；目录在首次上传时创建。
        """

        self.root = root
        self._key_images: dict[str, Image.Image] = {}

    def store(
        self,
        image_bytes: bytes,
        *,
        filename: str | None = None,
    ) -> ShortcutIconAsset:
        """校验上传图片并写入内容寻址资产目录。

        入参：原始图片 bytes 和仅用于诊断的浏览器文件名。
        返回：新建或已存在的 ``ShortcutIconAsset``。
        错误处理：超过 5 MiB、非 PNG/JPEG/WebP/ICO、解码失败或单边超过 4096 时抛 ValueError；
        写入失败抛 OSError。
        副作用：规范化图片，并写 normalized/preview/key/metadata 四个文件；不删除旧资产。
        """

        if not image_bytes:
            raise ValueError("shortcut icon upload is empty")
        if len(image_bytes) > MAX_SHORTCUT_ICON_BYTES:
            raise ValueError("shortcut icon exceeds 5 MiB")
        try:
            with Image.open(BytesIO(image_bytes)) as opened:
                image_format = (opened.format or "").upper()
                if image_format not in _ALLOWED_FORMATS:
                    raise ValueError(
                        "shortcut icon must be PNG, JPEG, WebP, or ICO"
                    )
                width, height = opened.size
                if (
                    width <= 0
                    or height <= 0
                    or width > MAX_SHORTCUT_ICON_DIMENSION
                    or height > MAX_SHORTCUT_ICON_DIMENSION
                ):
                    raise ValueError("shortcut icon dimensions must be within 4096x4096")
                opened.load()
                normalized = ImageOps.exif_transpose(opened).convert("RGBA")
        except ValueError:
            raise
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise ValueError("shortcut icon image cannot be decoded") from exc

        normalized_bytes = _png_bytes(normalized)
        asset_id = hashlib.sha256(normalized_bytes).hexdigest()
        existing = self.lookup(asset_id)
        if existing is not None:
            return existing

        created_at = datetime.now(UTC)
        asset_dir = self.root / asset_id
        asset_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(asset_dir / _NORMALIZED_IMAGE, normalized_bytes)
        _atomic_write_bytes(
            asset_dir / _PREVIEW_IMAGE,
            _png_bytes(_fit_custom_icon(normalized, size=(96, 96))),
        )
        _atomic_write_bytes(
            asset_dir / _KEY_IMAGE,
            _png_bytes(_fit_custom_icon(normalized, size=(112, 112)).convert("RGB")),
        )
        metadata = {
            "version": _CACHE_VERSION,
            "asset_id": asset_id,
            "width": normalized.width,
            "height": normalized.height,
            "filename": filename,
            "created_at": created_at.isoformat(),
        }
        _atomic_write_bytes(
            asset_dir / _METADATA,
            (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        return ShortcutIconAsset(
            asset_id=asset_id,
            width=normalized.width,
            height=normalized.height,
            preview_url=f"/ui/shortcut-icons/{asset_id}/{_PREVIEW_IMAGE}",
            key_icon_url=f"/ui/shortcut-icons/{asset_id}/{_KEY_IMAGE}",
            filename=filename,
            created_at=created_at,
        )

    def lookup(self, asset_id: str) -> ShortcutIconAsset | None:
        """只读查找一个完整的快捷键图标资产。

        入参：64 位小写十六进制 asset id。
        返回：metadata 和两张派生图片完整时返回资产，否则 None。
        错误处理：非法 id 直接返回 None；坏 metadata 当作未命中。
        副作用：只读文件系统。
        """

        if not _ASSET_ID_RE.fullmatch(asset_id):
            return None
        asset_dir = self.root / asset_id
        if not (asset_dir / _PREVIEW_IMAGE).is_file() or not (
            asset_dir / _KEY_IMAGE
        ).is_file():
            return None
        metadata = _read_metadata(asset_dir / _METADATA)
        if metadata is None or metadata.get("asset_id") != asset_id:
            return None
        try:
            return ShortcutIconAsset(
                asset_id=asset_id,
                width=int(metadata["width"]),
                height=int(metadata["height"]),
                preview_url=f"/ui/shortcut-icons/{asset_id}/{_PREVIEW_IMAGE}",
                key_icon_url=f"/ui/shortcut-icons/{asset_id}/{_KEY_IMAGE}",
                filename=(
                    str(metadata["filename"])
                    if metadata.get("filename") is not None
                    else None
                ),
                created_at=datetime.fromisoformat(str(metadata["created_at"])),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def resolve_file(self, asset_id: str, asset_name: str) -> Path | None:
        """解析 API 允许返回的资产文件。

        入参：内容 hash id 和文件名；只允许 preview-96.png/key-112.png。
        返回：存在且位于对应资产目录的路径，否则 None。
        错误处理：路径穿越和未知文件名按 None 处理。
        副作用：只读文件元数据。
        """

        if not _ASSET_ID_RE.fullmatch(asset_id):
            return None
        if asset_name not in {_PREVIEW_IMAGE, _KEY_IMAGE}:
            return None
        path = self.root / asset_id / asset_name
        return path if path.is_file() else None

    def key_image(self, asset_id: str) -> Image.Image | None:
        """读取一个资产的 N4 Pro 112px RGB 图像。

        入参：内容 hash id。
        返回：独立加载的 RGB Pillow image；缺失或坏文件返回 None。
        错误处理：解码失败降级 None，便于渲染器使用自动图标 fallback。
        副作用：只读 key PNG 文件。
        """

        cached = self._key_images.get(asset_id)
        if cached is not None:
            return cached
        path = self.resolve_file(asset_id, _KEY_IMAGE)
        if path is None:
            return None
        try:
            with Image.open(path) as image:
                image.load()
                loaded = image.convert("RGB")
                self._key_images[asset_id] = loaded
                return loaded
        except (OSError, UnidentifiedImageError):
            return None


def _fit_custom_icon(image: Image.Image, *, size: tuple[int, int]) -> Image.Image:
    """把用户图片等比居中到深色正方形画布。

    入参：已规范化 RGBA 图片和目标尺寸。
    返回：RGBA 画布。
    错误处理：非法尺寸由 Pillow 抛出。
    副作用：只处理内存图片。
    """

    canvas = Image.new("RGBA", size, (11, 14, 18, 255))
    contained = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
    x = (size[0] - contained.width) // 2
    y = (size[1] - contained.height) // 2
    canvas.alpha_composite(contained, (x, y))
    return canvas


def _png_bytes(image: Image.Image) -> bytes:
    """把 Pillow image 稳定编码成无 metadata PNG。

    入参：任意 Pillow image。
    返回：PNG bytes。
    错误处理：Pillow 编码失败按原异常传播。
    副作用：只写内存 buffer。
    """

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=9)
    return buffer.getvalue()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """用同目录临时文件原子替换目标文件。

    入参：目标路径和完整 bytes。
    返回：无显式返回值。
    错误处理：写入或 replace 失败抛 OSError。
    副作用：写临时文件并替换目标；成功后不保留临时文件。
    """

    temp = path.with_name(f".{path.name}.tmp")
    temp.write_bytes(data)
    temp.replace(path)


def _read_metadata(path: Path) -> dict[str, Any] | None:
    """读取并验证 metadata 顶层 object。

    入参：metadata JSON 路径。
    返回：dict 或 None。
    错误处理：文件缺失、I/O 或 JSON 错误统一返回 None。
    副作用：只读文件。
    """

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
