"""URL favicon 图标缓存。

本模块把 URL 的 favicon 拉取或降级渲染成 Agent Deck 自己可复用的 PNG 缓存。Web GUI
通过 URL 读取 `icon-96.png`，N4 Pro renderer 通过同一缓存读取 `key-112.png`。缓存可删除、
可重建，不作为用户配置的唯一真相；用户配置仍只保存 URL。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import ParseResult, unquote_to_bytes, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from PIL import Image, ImageChops, ImageColor, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from agent_deck.rendering.url_key import render_url_key_image, token_for_url

UrlIconFetcher = Callable[[str], bytes | None]
"""URL favicon 下载函数签名；测试可注入 fake fetcher。"""

_URL_ICON_CACHE_ENV = "AGENT_DECK_URL_ICON_CACHE_DIR"
_DEFAULT_URL_ICON_CACHE_ROOT = (
    Path.home() / "Library/Application Support/AgentDeck/icon-cache/urls"
)
_CACHE_VERSION = 2
_ICON_96 = "icon-96.png"
_KEY_112 = "key-112.png"
_METADATA = "metadata.json"
_MAX_FAVICON_BYTES = 1024 * 1024
_FETCH_TIMEOUT_SECONDS = 4.0
_USER_AGENT = "AgentDeck/0.1 favicon-cache"


class CachedUrlIcon(BaseModel):
    """描述一个 URL 图标缓存条目。

    入参：字段来自缓存目录和刷新结果。
    返回：frozen Pydantic model，可进入 `/ui/url-icons/resolve` 响应。
    错误处理：字段类型非法由 Pydantic 报告。
    副作用：模型自身不读写文件。
    """

    model_config = ConfigDict(frozen=True)

    cache_key: str = Field(min_length=1)
    status: str
    origin: str
    host: str
    icon_token: str
    icon_url: str | None = None
    key_icon_url: str | None = None
    icon_path: str | None = None
    key_icon_path: str | None = None
    updated: bool = False
    fallback_reason: str | None = None
    source: str = "discovered"


class UrlIconCache:
    """管理 URL favicon 缓存目录。

    入参：`root` 是缓存根目录；`url_prefix` 是 Web 路由前缀；`fetcher` 是 favicon 下载函数。
    返回：实例方法可确保缓存存在、读取 key 图、解析 Web 文件路径。
    错误处理：单个 URL favicon 失败会降级 token fallback；目录写入失败向调用方抛 OSError。
    副作用：`ensure()` 可能访问网络、创建目录并写入 PNG/JSON 缓存文件。
    """

    def __init__(
        self,
        root: Path,
        *,
        url_prefix: str = "/ui/url-icons",
        fetcher: UrlIconFetcher | None = None,
    ) -> None:
        """初始化 URL icon cache。

        入参：`root` 是缓存根目录；`url_prefix` 是对外 URL 前缀；`fetcher` 为空时使用
        受超时和大小限制的默认 HTTP fetcher。
        返回：无。
        错误处理：无；目录会在首次写入时创建。
        副作用：保存路径和 fetcher 配置。
        """

        self.root = root.expanduser()
        self.url_prefix = url_prefix.rstrip("/")
        self.fetcher = fetcher or fetch_favicon_bytes

    def ensure(self, url: str, *, force: bool = False) -> CachedUrlIcon:
        """显式解析网页并确保 URL origin 已有 favicon 或 fallback 图标缓存。

        入参：`url` 是用户配置的网址；`force` 为 True 时无视 metadata 重建。
        返回：缓存条目；favicon 下载或解析失败时仍提供 token fallback 缓存。
        错误处理：URL 非 http/https 或缺少 host 时抛 ValueError；目录写入失败按 OSError 传播。
        副作用：可能访问网络并写入缓存 PNG 和 metadata。
        """

        origin = origin_for_url(url)
        cache_key = cache_key_for_url_origin(origin)
        cache_dir = self.root / cache_key
        icon_path = cache_dir / _ICON_96
        key_icon_path = cache_dir / _KEY_112
        metadata_path = cache_dir / _METADATA
        token = token_for_url(origin)
        if (
            not force
            and icon_path.is_file()
            and key_icon_path.is_file()
            and _metadata_matches(metadata_path, origin)
        ):
            return self._entry(
                cache_key,
                origin=origin,
                status=_metadata_status(metadata_path),
                icon_token=token,
                updated=False,
                fallback_reason=_metadata_fallback_reason(metadata_path),
                source=_metadata_source(metadata_path),
            )

        cache_dir.mkdir(parents=True, exist_ok=True)
        favicon, fallback_reason = _discover_url_icon_image(
            origin,
            fetcher=self.fetcher,
        )
        status = "ready" if favicon is not None else "fallback"
        self._write_images_and_metadata(
            cache_dir=cache_dir,
            icon_image=_web_icon_image(origin, favicon=favicon, icon_token=token),
            key_image=render_url_key_image(
                url=origin,
                favicon=favicon,
                icon_token=token,
            ),
            metadata={
                "version": _CACHE_VERSION,
                "cache_key": cache_key,
                "origin": origin,
                "host": urlparse(origin).hostname,
                "icon_token": token,
                "status": status,
                "source": "discovered",
                "fallback_reason": fallback_reason,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        return self._entry(
            cache_key,
            origin=origin,
            status=status,
            icon_token=token,
            updated=True,
            fallback_reason=fallback_reason,
            source="discovered",
        )

    def lookup(self, url: str) -> CachedUrlIcon | None:
        """只读查找 URL origin 已缓存的图标。

        入参：`url` 是用户配置的网址。
        返回：缓存存在且 metadata 匹配时返回条目；否则返回 None。
        错误处理：URL 非法时抛 ValueError；坏 metadata 当作未命中。
        副作用：只读缓存目录，不访问网络、不写文件。
        """

        origin = origin_for_url(url)
        cache_key = cache_key_for_url_origin(origin)
        metadata_path = self.root / cache_key / _METADATA
        icon_path = self.root / cache_key / _ICON_96
        key_icon_path = self.root / cache_key / _KEY_112
        if (
            not icon_path.is_file()
            or not key_icon_path.is_file()
            or not _metadata_matches(metadata_path, origin)
        ):
            return None
        return self._entry(
            cache_key,
            origin=origin,
            status=_metadata_status(metadata_path),
            icon_token=token_for_url(origin),
            updated=False,
            fallback_reason=_metadata_fallback_reason(metadata_path),
            source=_metadata_source(metadata_path),
        )

    def store_custom_icon(
        self,
        url: str,
        *,
        image_bytes: bytes,
        filename: str | None = None,
    ) -> CachedUrlIcon:
        """把用户选择的本地图像复制进 URL icon cache。

        入参：`url` 是配置网址；`image_bytes` 是浏览器上传的原始图像 bytes；`filename`
        用于 metadata 诊断。
        返回：缓存条目，状态为 `custom`。
        错误处理：URL 非法、图片无法解析或目录写入失败会抛异常给 API 层。
        副作用：写入 Web 图标、硬件 key 图和 metadata；不访问网络。
        """

        origin = origin_for_url(url)
        cache_key = cache_key_for_url_origin(origin)
        cache_dir = self.root / cache_key
        token = token_for_url(origin)
        try:
            with Image.open(BytesIO(image_bytes)) as image:
                image.load()
                favicon = image.convert("RGBA")
        except Exception as exc:  # noqa: BLE001 - 上传图片坏掉时应返回可读 422。
            raise ValueError("uploaded url icon image cannot be decoded") from exc
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._write_images_and_metadata(
            cache_dir=cache_dir,
            icon_image=_web_icon_image(origin, favicon=favicon, icon_token=token),
            key_image=render_url_key_image(
                url=origin,
                favicon=favicon,
                icon_token=token,
            ),
            metadata={
                "version": _CACHE_VERSION,
                "cache_key": cache_key,
                "origin": origin,
                "host": urlparse(origin).hostname,
                "icon_token": token,
                "status": "custom",
                "source": "custom_upload",
                "filename": filename,
                "fallback_reason": None,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        return self._entry(
            cache_key,
            origin=origin,
            status="custom",
            icon_token=token,
            updated=True,
            fallback_reason=None,
            source="custom_upload",
        )

    def key_image_for_url(self, url: str) -> Image.Image | None:
        """只读读取适合硬件下发的缓存 URL key 图。

        入参：`url` 来自 `KeyPlan.payload`。
        返回：RGB `Image`；URL 非法、缓存或读取失败时返回 None，让调用方 fallback。
        错误处理：坏缓存图片返回 None。
        副作用：只读缓存目录，不访问网络、不写文件。
        """

        try:
            entry = self.lookup(url)
        except ValueError:
            return None
        if entry is None:
            return None
        if entry.key_icon_path is None:
            return None
        try:
            with Image.open(entry.key_icon_path) as image:
                image.load()
                return image.convert("RGB")
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
        origin: str,
        status: str,
        icon_token: str,
        updated: bool,
        fallback_reason: str | None,
        source: str,
    ) -> CachedUrlIcon:
        """构造缓存条目模型。

        入参：`cache_key` 是目录名；`origin` 是归一化 URL origin；`status` 是缓存状态；
        `icon_token` 是 fallback 文本；`updated` 表示本次是否重建。
        返回：`CachedUrlIcon`。
        错误处理：字段非法由 Pydantic 抛出。
        副作用：无。
        """

        icon_path = self.root / cache_key / _ICON_96
        key_icon_path = self.root / cache_key / _KEY_112
        host = urlparse(origin).hostname or ""
        return CachedUrlIcon(
            cache_key=cache_key,
            status=status,
            origin=origin,
            host=host,
            icon_token=icon_token,
            icon_url=f"{self.url_prefix}/{cache_key}/{_ICON_96}",
            key_icon_url=f"{self.url_prefix}/{cache_key}/{_KEY_112}",
            icon_path=str(icon_path),
            key_icon_path=str(key_icon_path),
            updated=updated,
            fallback_reason=fallback_reason,
            source=source,
        )

    def _write_images_and_metadata(
        self,
        *,
        cache_dir: Path,
        icon_image: Image.Image,
        key_image: Image.Image,
        metadata: dict[str, Any],
    ) -> None:
        """写入 URL icon cache 的图片和 metadata。

        入参：`cache_dir` 是单个 origin 目录；`icon_image` 是 Web 预览图；`key_image`
        是硬件按键图；`metadata` 是 JSON-safe 元数据。
        返回：无。
        错误处理：文件写入失败按 OSError/Pillow 异常传播。
        副作用：写入 PNG 和 metadata 文件。
        """

        _save_png(icon_image, cache_dir / _ICON_96)
        _save_png(key_image, cache_dir / _KEY_112)
        _write_metadata(cache_dir / _METADATA, metadata)


_SAFE_CACHE_KEY_RE = re.compile(r"[A-Za-z0-9._-]{1,160}")


def resolve_url_icon_cache_root(path: Path | None = None) -> Path:
    """解析 URL icon cache 根目录。

    入参：`path` 是调用方显式路径；为空时先读环境变量，再使用用户级默认路径。
    返回：展开后的缓存根目录。
    错误处理：无；本函数不创建目录。
    副作用：只读取环境变量。
    """

    if path is not None:
        return path.expanduser()
    env_value = os.environ.get(_URL_ICON_CACHE_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return _DEFAULT_URL_ICON_CACHE_ROOT


def origin_for_url(url: str) -> str:
    """把用户 URL 归一化为 favicon 所属 origin。

    入参：`url` 必须是包含 host 的 http/https URL。
    返回：小写 scheme/host 的 origin，保留显式端口。
    错误处理：非 http/https 或缺少 host 时抛 ValueError。
    副作用：无。
    """

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url icon requires http/https url")
    if not parsed.hostname:
        raise ValueError("url icon requires host")
    netloc = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("url icon requires valid port") from exc
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunparse((parsed.scheme.lower(), netloc, "", "", "", ""))


def cache_key_for_url_origin(origin: str) -> str:
    """生成稳定 URL icon 缓存目录名。

    入参：`origin` 是 `origin_for_url()` 的输出。
    返回：只包含安全字符的缓存 key。
    错误处理：无。
    副作用：无。
    """

    parsed = urlparse(origin)
    host = re.sub(r"[^A-Za-z0-9._-]+", "-", parsed.netloc).strip(".-") or "url"
    digest = sha256(origin.encode("utf-8")).hexdigest()[:12]
    return f"{host}-{digest}"[:160]


def fetch_favicon_bytes(favicon_url: str) -> bytes | None:
    """下载 favicon bytes。

    入参：`favicon_url` 是完整 `/favicon.ico` URL。
    返回：最多 1 MiB 的 bytes；HTTP 错误页有 body 时也返回 body 供 HTML icon 解析。
    错误处理：网络、HTTP、超时等错误被吞掉并返回 None。
    副作用：发起一次 HTTP(S) GET 请求。
    """

    request = Request(favicon_url, headers={"User-Agent": _USER_AGENT})
    try:
        with urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
            return response.read(_MAX_FAVICON_BYTES + 1)[:_MAX_FAVICON_BYTES]
    except HTTPError as exc:
        try:
            return exc.read(_MAX_FAVICON_BYTES + 1)[:_MAX_FAVICON_BYTES]
        except OSError:
            return None
    except (OSError, URLError, TimeoutError, ValueError):
        return None


class _IconLinkParser(HTMLParser):
    """从 HTML head 中提取 icon 和 manifest link。

    入参：通过 `feed()` 传入 HTML 文本。
    返回：实例的 `icons` 与 `manifests` 字段保存候选链接。
    错误处理：HTMLParser 自行容忍坏 HTML。
    副作用：只修改实例内存列表。
    """

    def __init__(self) -> None:
        """初始化 parser 状态。

        入参：无。
        返回：无。
        错误处理：无。
        副作用：初始化候选列表。
        """

        super().__init__()
        self.icons: list[dict[str, str]] = []
        self.manifests: list[str] = []
        self._in_head = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        """处理 HTML start tag 并收集 link 候选。

        入参：`tag` 是标签名；`attrs` 是 HTMLParser 解析出的属性列表。
        返回：无。
        错误处理：缺失 href 或 rel 时忽略。
        副作用：可能追加到 `icons` 或 `manifests`。
        """

        if tag.lower() == "head":
            self._in_head = True
            return
        if not self._in_head or tag.lower() != "link":
            return
        normalized = {name.lower(): value or "" for name, value in attrs}
        rel = normalized.get("rel", "").lower()
        href = normalized.get("href", "").strip()
        if not href:
            return
        rel_tokens = set(rel.split())
        if "manifest" in rel_tokens:
            self.manifests.append(href)
            return
        if "icon" in rel_tokens or "apple-touch-icon" in rel_tokens:
            self.icons.append(
                {
                    "href": href,
                    "rel": rel,
                    "sizes": normalized.get("sizes", ""),
                    "type": normalized.get("type", ""),
                }
            )

    def handle_endtag(self, tag: str) -> None:
        """处理 HTML end tag。

        入参：`tag` 是标签名。
        返回：无。
        错误处理：无。
        副作用：遇到 `</head>` 后停止收集候选。
        """

        if tag.lower() == "head":
            self._in_head = False


def _discover_url_icon_image(
    origin: str,
    *,
    fetcher: UrlIconFetcher,
) -> tuple[Image.Image | None, str | None]:
    """解析网页 header、manifest 和默认 favicon 并返回可用图标。

    入参：`origin` 是 URL origin；`fetcher` 是下载函数。
    返回：成功时返回 Pillow 图和 None；失败时返回 None 和 fallback reason。
    错误处理：fetcher 抛异常或图片解析失败都会转换为 fallback reason。
    副作用：可能通过 fetcher 访问网络。
    """

    candidates: list[str] = []
    html, html_error = _fetch_text(origin, fetcher=fetcher)
    if html is not None:
        parser = _IconLinkParser()
        parser.feed(html)
        candidates.extend(_icon_candidate_urls(origin, parser.icons))
        candidates.extend(_manifest_icon_candidate_urls(origin, parser.manifests, fetcher))
    candidates.append(_favicon_url(origin))
    seen: set[str] = set()
    last_error = html_error or "favicon not found"
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        image, error = _load_icon_candidate(candidate, fetcher=fetcher)
        if image is not None:
            return image, None
        last_error = error or last_error
    return None, last_error


def _load_icon_candidate(
    icon_url: str,
    *,
    fetcher: UrlIconFetcher,
) -> tuple[Image.Image | None, str | None]:
    """下载并解析一个 icon 候选。

    入参：`icon_url` 是完整图标 URL；`fetcher` 是下载函数。
    返回：成功时返回图像；失败时返回 None 和原因。
    错误处理：fetcher 异常或 Pillow 解码失败都会转换为原因字符串。
    副作用：可能通过 fetcher 访问网络。
    """

    if icon_url.startswith("data:"):
        return _load_data_url_icon_candidate(icon_url)
    try:
        raw = fetcher(icon_url)
    except Exception as exc:  # noqa: BLE001 - 图标解析失败必须降级而不是中断 GUI。
        return None, f"fetch failed: {type(exc).__name__}"
    if not raw:
        return None, "icon not found"
    svg_image, svg_error = _rasterize_svg_icon(raw)
    if svg_image is not None:
        return svg_image, None
    try:
        with Image.open(BytesIO(raw)) as image:
            image.load()
            return image.convert("RGBA"), None
    except Exception:
        return None, svg_error or "icon decode failed"


def _load_data_url_icon_candidate(icon_url: str) -> tuple[Image.Image | None, str | None]:
    """解析 data URL 图标候选。

    入参：`icon_url` 是 HTML link 中的 data URL。
    返回：成功时返回 RGBA 图像；不支持或解码失败时返回 None 和原因。
    错误处理：坏 base64、坏 URL 编码或坏图片都转换为 fallback reason。
    副作用：无网络和文件 I/O。
    """

    try:
        header, payload = icon_url[5:].split(",", 1)
    except ValueError:
        return None, "data icon malformed"
    media_type = header.split(";", 1)[0].lower()
    is_base64 = any(part.lower() == "base64" for part in header.split(";")[1:])
    try:
        raw = base64.b64decode(payload, validate=True) if is_base64 else unquote_to_bytes(payload)
    except (binascii.Error, ValueError):
        return None, "data icon decode failed"
    if len(raw) > _MAX_FAVICON_BYTES:
        raw = raw[:_MAX_FAVICON_BYTES]
    if media_type in {"image/svg+xml", "image/svg"} or raw.lstrip().startswith(b"<svg"):
        return _rasterize_svg_icon(raw)
    try:
        with Image.open(BytesIO(raw)) as image:
            image.load()
            return image.convert("RGBA"), None
    except Exception:
        return None, "data icon image decode failed"


def _rasterize_svg_icon(raw: bytes) -> tuple[Image.Image | None, str | None]:
    """把简单 SVG favicon rasterize 成 RGBA 图。

    入参：`raw` 是 SVG XML bytes。
    返回：支持的基础 SVG 返回 RGBA 图片；复杂 SVG 返回 None 和原因。
    错误处理：XML、尺寸、颜色或形状解析失败时安全降级。
    副作用：只在内存中绘制。
    """

    text = raw.decode("utf-8", errors="replace").lstrip()
    if not text.startswith("<svg") and "<svg" not in text[:256]:
        return None, "icon decode failed"
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None, "svg icon parse failed"
    view_box = _svg_view_box(root)
    if view_box is None:
        return None, "svg icon missing viewport"
    view_x, view_y, view_width, view_height = view_box
    width = _svg_dimension(root.get("width")) or view_width
    height = _svg_dimension(root.get("height")) or view_height
    if width <= 0 or height <= 0 or view_width <= 0 or view_height <= 0:
        return None, "svg icon invalid viewport"
    output_width = max(16, min(512, round(width)))
    output_height = max(16, min(512, round(height)))
    draw_scale = 4
    canvas = Image.new("RGBA", (output_width * draw_scale, output_height * draw_scale), (0, 0, 0, 0))
    mapper = _SvgCoordinateMapper(
        view_x=view_x,
        view_y=view_y,
        view_width=view_width,
        view_height=view_height,
        output_width=canvas.width,
        output_height=canvas.height,
    )
    clip_masks = _svg_clip_masks(root, canvas.size, mapper)
    drew_shape = _draw_svg_children(root, canvas, mapper, clip_masks)
    if not drew_shape:
        return None, "svg icon unsupported"
    return canvas.resize((output_width, output_height), Image.Resampling.LANCZOS), None


class _SvgCoordinateMapper:
    """把 SVG viewBox 坐标映射到 raster canvas 像素坐标。

    入参：viewBox 和输出尺寸。
    返回：实例方法提供 x/y/length 映射。
    错误处理：调用方需保证尺寸大于 0。
    副作用：无。
    """

    def __init__(
        self,
        *,
        view_x: float,
        view_y: float,
        view_width: float,
        view_height: float,
        output_width: int,
        output_height: int,
    ) -> None:
        """初始化 SVG 坐标映射器。

        入参：`view_*` 来自 viewBox；`output_*` 是目标画布像素尺寸。
        返回：无。
        错误处理：无。
        副作用：保存映射参数。
        """

        self.view_x = view_x
        self.view_y = view_y
        self.scale_x = output_width / view_width
        self.scale_y = output_height / view_height

    def x(self, value: str | None) -> float:
        """映射 SVG x 坐标。

        入参：`value` 是 SVG 数字属性。
        返回：画布 x 像素坐标。
        错误处理：坏值按 0 处理。
        副作用：无。
        """

        return (_svg_number(value) - self.view_x) * self.scale_x

    def y(self, value: str | None) -> float:
        """映射 SVG y 坐标。

        入参：`value` 是 SVG 数字属性。
        返回：画布 y 像素坐标。
        错误处理：坏值按 0 处理。
        副作用：无。
        """

        return (_svg_number(value) - self.view_y) * self.scale_y

    def w(self, value: str | None) -> float:
        """映射 SVG 横向长度。

        入参：`value` 是 SVG 数字属性。
        返回：画布宽度像素。
        错误处理：坏值按 0 处理。
        副作用：无。
        """

        return _svg_number(value) * self.scale_x

    def h(self, value: str | None) -> float:
        """映射 SVG 纵向长度。

        入参：`value` 是 SVG 数字属性。
        返回：画布高度像素。
        错误处理：坏值按 0 处理。
        副作用：无。
        """

        return _svg_number(value) * self.scale_y


def _svg_view_box(root: ElementTree.Element) -> tuple[float, float, float, float] | None:
    """读取 SVG viewBox 或由 width/height 推导 viewport。

    入参：`root` 是 SVG 根节点。
    返回：`(x, y, width, height)`；缺失或非法时返回 None。
    错误处理：坏数字返回 None。
    副作用：无。
    """

    raw = root.get("viewBox") or root.get("viewbox")
    if raw:
        parts = [_svg_number(part) for part in re.split(r"[,\s]+", raw.strip()) if part]
        if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
            return (parts[0], parts[1], parts[2], parts[3])
    width = _svg_dimension(root.get("width"))
    height = _svg_dimension(root.get("height"))
    if width and height:
        return (0.0, 0.0, width, height)
    return None


def _svg_clip_masks(
    root: ElementTree.Element,
    canvas_size: tuple[int, int],
    mapper: _SvgCoordinateMapper,
) -> dict[str, Image.Image]:
    """生成当前支持的 SVG clipPath mask。

    入参：`root` 是 SVG 根节点；`canvas_size` 是目标像素尺寸；`mapper` 负责坐标转换。
    返回：clipPath id 到 L 模式 mask 的映射。
    错误处理：不支持的 clipPath 被忽略。
    副作用：只创建内存图片。
    """

    masks: dict[str, Image.Image] = {}
    for element in root.iter():
        if _svg_tag(element) != "clipPath":
            continue
        clip_id = element.get("id")
        if not clip_id:
            continue
        mask = Image.new("L", canvas_size, 0)
        drew = False
        for child in list(element):
            drew = _draw_svg_shape(child, mask, mapper, fill=255) or drew
        if drew:
            masks[clip_id] = mask
    return masks


def _draw_svg_children(
    element: ElementTree.Element,
    canvas: Image.Image,
    mapper: _SvgCoordinateMapper,
    clip_masks: dict[str, Image.Image],
) -> bool:
    """按文档顺序绘制当前支持的 SVG 子节点。

    入参：`element` 是 SVG 或 group 节点；`canvas` 是 RGBA 画布；`mapper` 负责坐标转换；
    `clip_masks` 是已解析的 clipPath。
    返回：至少绘制一个形状时返回 True。
    错误处理：未知节点和无效颜色被跳过。
    副作用：修改 `canvas`。
    """

    drew_any = False
    for child in list(element):
        tag = _svg_tag(child)
        if tag in {"defs", "clipPath", "title", "desc"}:
            continue
        if tag in {"svg", "g"}:
            drew_any = _draw_svg_children(child, canvas, mapper, clip_masks) or drew_any
            continue
        fill = _svg_fill(child)
        if fill is None:
            continue
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        if not _draw_svg_shape(child, layer, mapper, fill=fill):
            continue
        clip_id = _svg_clip_id(child.get("clip-path"))
        if clip_id and clip_id in clip_masks:
            alpha = ImageChops.multiply(layer.getchannel("A"), clip_masks[clip_id])
            layer.putalpha(alpha)
        canvas.alpha_composite(layer)
        drew_any = True
    return drew_any


def _draw_svg_shape(
    element: ElementTree.Element,
    image: Image.Image,
    mapper: _SvgCoordinateMapper,
    *,
    fill: int | tuple[int, int, int, int],
) -> bool:
    """绘制一个受支持的 SVG 基础形状。

    入参：`element` 是 circle 或 rect；`image` 是 RGBA 或 L 模式图像；`mapper` 负责坐标；
    `fill` 是 Pillow 可接受的颜色。
    返回：形状被绘制时返回 True。
    错误处理：未知形状返回 False。
    副作用：修改 `image`。
    """

    draw = ImageDraw.Draw(image, "RGBA" if image.mode == "RGBA" else None)
    tag = _svg_tag(element)
    if tag == "circle":
        cx = mapper.x(element.get("cx"))
        cy = mapper.y(element.get("cy"))
        rx = mapper.w(element.get("r"))
        ry = mapper.h(element.get("r"))
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=fill)
        return True
    if tag == "rect":
        x = mapper.x(element.get("x"))
        y = mapper.y(element.get("y"))
        width = mapper.w(element.get("width"))
        height = mapper.h(element.get("height"))
        if width <= 0 or height <= 0:
            return False
        radius = max(mapper.w(element.get("rx")), mapper.h(element.get("ry")))
        if radius > 0 and image.mode == "RGBA":
            draw.rounded_rectangle((x, y, x + width, y + height), radius=radius, fill=fill)
        else:
            draw.rectangle((x, y, x + width, y + height), fill=fill)
        return True
    return False


def _svg_tag(element: ElementTree.Element) -> str:
    """返回不带 XML namespace 的 SVG 标签名。

    入参：`element` 是 XML 节点。
    返回：局部标签名。
    错误处理：无。
    副作用：无。
    """

    return element.tag.rsplit("}", 1)[-1]


def _svg_fill(element: ElementTree.Element) -> tuple[int, int, int, int] | None:
    """解析 SVG fill 颜色。

    入参：`element` 是 SVG 节点。
    返回：RGBA 颜色；`none` 或无法解析时返回 None。
    错误处理：坏颜色返回 None。
    副作用：无。
    """

    value = element.get("fill") or _svg_style_value(element.get("style"), "fill") or "#000000"
    if value.lower() == "none":
        return None
    raw_opacity = element.get("fill-opacity") or _svg_style_value(element.get("style"), "fill-opacity")
    opacity = _svg_number(raw_opacity) if raw_opacity is not None else 1.0
    if opacity <= 0:
        return None
    opacity = 1.0 if opacity > 1 else opacity
    try:
        red, green, blue, alpha = ImageColor.getcolor(value, "RGBA")
    except ValueError:
        return None
    return (red, green, blue, round(alpha * opacity))


def _svg_style_value(style: str | None, name: str) -> str | None:
    """从 SVG style 字符串读取指定属性。

    入参：`style` 是 CSS 声明串；`name` 是属性名。
    返回：属性值或 None。
    错误处理：坏声明被忽略。
    副作用：无。
    """

    if not style:
        return None
    for declaration in style.split(";"):
        key, separator, value = declaration.partition(":")
        if separator and key.strip().lower() == name:
            return value.strip()
    return None


def _svg_clip_id(value: str | None) -> str | None:
    """从 `clip-path: url(#id)` 中提取 id。

    入参：`value` 是 SVG clip-path 属性。
    返回：clip id 或 None。
    错误处理：不匹配时返回 None。
    副作用：无。
    """

    if not value:
        return None
    match = re.fullmatch(r"\s*url\(#([A-Za-z0-9_.:-]+)\)\s*", value)
    return match.group(1) if match else None


def _svg_dimension(value: str | None) -> float:
    """解析 SVG width/height 长度。

    入参：`value` 可能包含 px 或其他单位。
    返回：数值部分；无法解析时返回 0。
    错误处理：无。
    副作用：无。
    """

    return _svg_number(value)


def _svg_number(value: str | None) -> float:
    """解析 SVG 数字属性的前导数值。

    入参：`value` 是 SVG 属性字符串。
    返回：float；缺失或非法时返回 0。
    错误处理：无。
    副作用：无。
    """

    if value is None:
        return 0.0
    match = re.match(r"\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))", str(value))
    return float(match.group(1)) if match else 0.0


def _fetch_text(
    url: str,
    *,
    fetcher: UrlIconFetcher,
) -> tuple[str | None, str | None]:
    """下载并解码 HTML 文本。

    入参：`url` 是首页 URL；`fetcher` 是下载函数。
    返回：成功时返回文本和 None；失败时返回 None 和原因。
    错误处理：fetcher 异常转换为原因字符串。
    副作用：可能通过 fetcher 访问网络。
    """

    try:
        raw = fetcher(url)
    except Exception as exc:  # noqa: BLE001 - HTML 解析失败可以回退到 /favicon.ico。
        return None, f"html fetch failed: {type(exc).__name__}"
    if not raw:
        return None, "html not found"
    return raw.decode("utf-8", errors="replace"), None


def _icon_candidate_urls(origin: str, links: list[dict[str, str]]) -> list[str]:
    """把 HTML link icon 候选排序并转成绝对 URL。

    入参：`origin` 是 URL origin；`links` 来自 `_IconLinkParser.icons`。
    返回：按优先级排序的绝对 URL 列表。
    错误处理：无。
    副作用：无。
    """

    def score(link: dict[str, str]) -> tuple[int, int]:
        rel = link.get("rel", "")
        type_value = link.get("type", "")
        sizes = link.get("sizes", "")
        rel_score = 0 if "icon" in rel and "apple" not in rel else 1
        type_score = 1 if "svg" in type_value else 0
        return (rel_score, type_score, -_largest_declared_size(sizes))

    return [urljoin(origin, link["href"]) for link in sorted(links, key=score)]


def _manifest_icon_candidate_urls(
    origin: str,
    manifests: list[str],
    fetcher: UrlIconFetcher,
) -> list[str]:
    """解析 Web manifest 中的 icon 候选。

    入参：`origin` 是 URL origin；`manifests` 是 HTML 中的 manifest href 列表；`fetcher`
    是下载函数。
    返回：按尺寸从大到小排序的绝对 URL 列表。
    错误处理：manifest 下载或 JSON 解析失败时忽略该 manifest。
    副作用：可能通过 fetcher 访问网络。
    """

    candidates: list[tuple[int, str]] = []
    for manifest_href in manifests[:3]:
        manifest_url = urljoin(origin, manifest_href)
        try:
            raw = fetcher(manifest_url)
        except Exception:
            continue
        if not raw:
            continue
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        icons = data.get("icons") if isinstance(data, dict) else None
        if not isinstance(icons, list):
            continue
        for icon in icons:
            if not isinstance(icon, dict):
                continue
            src = icon.get("src")
            if not isinstance(src, str) or not src:
                continue
            candidates.append(
                (
                    _largest_declared_size(str(icon.get("sizes", ""))),
                    urljoin(manifest_url, src),
                )
            )
    return [url for _size, url in sorted(candidates, reverse=True)]


def _largest_declared_size(value: str) -> int:
    """从 sizes 属性中提取最大边长。

    入参：`value` 可能形如 `16x16 32x32` 或 `any`。
    返回：可解析到的最大边长，无法解析时返回 0。
    错误处理：无。
    副作用：无。
    """

    sizes = re.findall(r"(\d+)x(\d+)", value)
    return max((max(int(width), int(height)) for width, height in sizes), default=0)


def _web_icon_image(
    origin: str,
    *,
    favicon: Image.Image | None,
    icon_token: str,
) -> Image.Image:
    """生成 Web GUI 预览使用的 96px 图标。

    入参：`origin` 是 URL origin；`favicon` 是可选真实图标；`icon_token` 是 fallback 文本。
    返回：RGBA 图像；有 favicon 时只缩放图标本体，缺失时生成 token fallback。
    错误处理：坏 favicon 自动 fallback。
    副作用：只复制内存图像。
    """

    if favicon is not None:
        try:
            icon = favicon.convert("RGBA")
            icon.thumbnail((96, 96), Image.Resampling.LANCZOS)
            return icon
        except Exception:
            pass
    return render_url_key_image(
        url=origin,
        favicon=None,
        icon_token=icon_token,
        size=(96, 96),
    ).convert("RGBA")


def _favicon_url(origin: str) -> str:
    """返回 origin 默认 favicon URL。

    入参：`origin` 是 URL origin。
    返回：`<origin>/favicon.ico`。
    错误处理：无。
    副作用：无。
    """

    parsed: ParseResult = urlparse(origin)
    return urlunparse((parsed.scheme, parsed.netloc, "/favicon.ico", "", "", ""))


def _metadata_matches(path: Path, origin: str) -> bool:
    """判断 metadata 是否仍匹配当前 URL origin。

    入参：`path` 是 metadata 文件；`origin` 是当前 URL origin。
    返回：版本和 origin 都匹配时返回 True。
    错误处理：文件缺失、坏 JSON 或结构错误返回 False。
    副作用：只读 metadata 文件。
    """

    data = _read_metadata(path)
    return data.get("version") == _CACHE_VERSION and data.get("origin") == origin


def _metadata_status(path: Path) -> str:
    """读取 metadata 中的缓存状态。

    入参：`path` 是 metadata 文件。
    返回：`ready` 或 `fallback`，坏 metadata 默认返回 `ready`。
    错误处理：读取失败返回默认值。
    副作用：只读 metadata 文件。
    """

    status = _read_metadata(path).get("status")
    return status if status in {"ready", "fallback"} else "ready"


def _metadata_fallback_reason(path: Path) -> str | None:
    """读取 metadata 中的 fallback 诊断。

    入参：`path` 是 metadata 文件。
    返回：诊断字符串或 None。
    错误处理：读取失败返回 None。
    副作用：只读 metadata 文件。
    """

    value = _read_metadata(path).get("fallback_reason")
    return value if isinstance(value, str) and value else None


def _metadata_source(path: Path) -> str:
    """读取 metadata 中的图标来源。

    入参：`path` 是 metadata 文件。
    返回：来源字符串；缺失时返回 `discovered`。
    错误处理：读取失败返回默认值。
    副作用：只读 metadata 文件。
    """

    value = _read_metadata(path).get("source")
    return value if isinstance(value, str) and value else "discovered"


def _read_metadata(path: Path) -> dict[str, Any]:
    """读取 metadata JSON。

    入参：`path` 是 metadata 文件。
    返回：dict；缺失、坏 JSON 或非 dict 时返回空 dict。
    错误处理：读取失败返回空 dict。
    副作用：只读 metadata 文件。
    """

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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
