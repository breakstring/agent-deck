"""URL favicon 图标缓存测试。

这些测试只使用 pytest 临时目录和 fake favicon fetcher；不访问互联网、不打开浏览器、
不访问真实 N4 Pro。
"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from agent_deck.actions.url_icon_cache import (
    UrlIconCache,
    cache_key_for_url_origin,
    origin_for_url,
)


def test_url_icon_cache_writes_web_and_key_icons(tmp_path: Path) -> None:
    """URL icon cache 应生成 Web 图标、硬件 key 图和 metadata。

    入参：`tmp_path` 提供隔离缓存根目录。
    返回：无返回值；断言通过代表 GUI 和硬件 renderer 可复用同一缓存。
    错误处理：fetch URL、文件缺失、URL 错误或图片尺寸不对时由 pytest 报告。
    副作用：只写 pytest 临时目录。
    """

    calls: list[str] = []
    cache = UrlIconCache(tmp_path / "cache", fetcher=_fake_site_fetcher(calls))

    entry = cache.ensure("https://Example.com/docs/page")

    expected_key = cache_key_for_url_origin("https://example.com")
    assert calls == [
        "https://example.com",
        "https://example.com/assets/icon.png",
    ]
    assert entry.cache_key == expected_key
    assert entry.status == "ready"
    assert entry.origin == "https://example.com"
    assert entry.host == "example.com"
    assert entry.icon_token == "EX"
    assert entry.icon_url == f"/ui/url-icons/{expected_key}/icon-96.png"
    assert entry.key_icon_url == f"/ui/url-icons/{expected_key}/key-112.png"
    assert entry.updated is True
    assert (tmp_path / f"cache/{expected_key}/icon-96.png").is_file()
    assert (tmp_path / f"cache/{expected_key}/key-112.png").is_file()

    with Image.open(tmp_path / f"cache/{expected_key}/icon-96.png") as image:
        assert image.size == (32, 32)
    with Image.open(tmp_path / f"cache/{expected_key}/key-112.png") as image:
        assert image.size == (112, 112)


def test_url_icon_cache_reuses_fresh_metadata(tmp_path: Path) -> None:
    """URL icon cache metadata 新鲜时不应重复请求 favicon。

    入参：`tmp_path` 提供隔离缓存根目录。
    返回：无返回值；断言通过代表第二次 ensure 返回 `updated=False` 且没有再次 fetch。
    错误处理：metadata 复用失败时由 pytest 报告。
    副作用：只写 pytest 临时目录。
    """

    calls: list[str] = []
    cache = UrlIconCache(tmp_path / "cache", fetcher=_fake_site_fetcher(calls))

    first = cache.ensure("https://example.com/a")
    second = cache.ensure("https://example.com/b")

    assert first.updated is True
    assert second.updated is False
    assert calls == [
        "https://example.com",
        "https://example.com/assets/icon.png",
    ]
    assert cache.resolve_file(first.cache_key, "icon-96.png") == (
        tmp_path / f"cache/{first.cache_key}/icon-96.png"
    )
    assert cache.resolve_file("../bad", "icon-96.png") is None


def test_url_icon_cache_falls_back_when_favicon_missing(tmp_path: Path) -> None:
    """favicon 下载失败时应生成 token fallback 图标。

    入参：`tmp_path` 提供隔离缓存根目录。
    返回：无返回值；断言通过代表失败不会阻断 GUI 预览。
    错误处理：fallback 文件缺失或状态错误时由 pytest 报告。
    副作用：只写 pytest 临时目录。
    """

    cache = UrlIconCache(tmp_path / "cache", fetcher=lambda _: None)

    entry = cache.ensure("https://github.com/openai")

    assert entry.status == "fallback"
    assert entry.icon_token == "GI"
    assert entry.fallback_reason == "icon not found"
    with Image.open(entry.key_icon_path or "") as image:
        assert image.size == (112, 112)
        assert _near_color(image.convert("RGB").getpixel((8, 8)), (11, 14, 18), tolerance=2)


def test_url_icon_cache_decodes_data_svg_icon(tmp_path: Path) -> None:
    """data:image/svg+xml favicon 应被缓存为 PNG，而不是错误降级成缩写。

    入参：`tmp_path` 提供隔离缓存根目录。
    返回：无返回值；断言通过代表 linux.do 这类内嵌 SVG favicon 可用于 GUI 和硬件。
    错误处理：若多请求 `/favicon.ico`、状态错误或 PNG 内容不符，由 pytest 报告。
    副作用：只写 pytest 临时目录。
    """

    calls: list[str] = []
    svg_data_url = "data:image/svg+xml;base64," + base64.b64encode(
        b"""<svg width="128" height="128" viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg">
<clipPath id="a"><circle cx="60" cy="60" r="47"/></clipPath>
<circle fill="#f0f0f0" cx="60" cy="60" r="50"/>
<rect fill="#1c1c1e" clip-path="url(#a)" x="10" y="10" width="100" height="30"/>
<rect fill="#f0f0f0" clip-path="url(#a)" x="10" y="40" width="100" height="40"/>
<rect fill="#ffb003" clip-path="url(#a)" x="10" y="80" width="100" height="30"/>
</svg>"""
    ).decode("ascii")

    def fetcher(url: str) -> bytes:
        """返回包含 data SVG favicon 的测试首页。

        入参：`url` 是 cache 请求的 URL。
        返回：首页 HTML bytes。
        错误处理：无。
        副作用：记录请求 URL。
        """

        calls.append(url)
        if url == "https://linux.do":
            return f'<html><head><link rel="icon" href="{svg_data_url}"></head></html>'.encode()
        return b""

    cache = UrlIconCache(tmp_path / "cache", fetcher=fetcher)

    entry = cache.ensure("https://linux.do")

    assert calls == ["https://linux.do"]
    assert entry.status == "ready"
    assert entry.fallback_reason is None
    with Image.open(entry.icon_path or "") as image:
        assert image.mode == "RGBA"
        assert _near_color(image.convert("RGBA").getpixel((48, 22)), (28, 28, 30))
    with Image.open(entry.key_icon_path or "") as image:
        assert image.size == (112, 112)


def test_url_icon_cache_lookup_does_not_fetch_when_missing(tmp_path: Path) -> None:
    """只读 lookup 未命中时不应访问网络或写缓存。

    入参：`tmp_path` 提供隔离缓存根目录。
    返回：无返回值；断言通过代表 URL 输入框可查询旧缓存但不会自动解析网页。
    错误处理：fetcher 被调用或缓存被写入时由 pytest 报告。
    副作用：只读 pytest 临时目录。
    """

    calls: list[str] = []
    cache = UrlIconCache(tmp_path / "cache", fetcher=_fake_site_fetcher(calls))

    assert cache.lookup("https://example.com/docs") is None
    assert calls == []
    assert not (tmp_path / "cache").exists()


def test_url_icon_cache_stores_custom_uploaded_icon(tmp_path: Path) -> None:
    """本地上传图片应覆盖为 custom_upload 缓存。

    入参：`tmp_path` 提供隔离缓存根目录。
    返回：无返回值；断言通过代表用户选择图片后可复用到 GUI 和硬件图。
    错误处理：状态、metadata 或图片尺寸错误时由 pytest 报告。
    副作用：只写 pytest 临时目录。
    """

    cache = UrlIconCache(tmp_path / "cache", fetcher=lambda _: None)

    entry = cache.store_custom_icon(
        "https://example.com/docs",
        image_bytes=_png_bytes((32, 64), (200, 80, 60, 255)),
        filename="custom.png",
    )
    lookup = cache.lookup("https://example.com/other")

    assert entry.status == "custom"
    assert entry.source == "custom_upload"
    assert lookup is not None
    assert lookup.source == "custom_upload"
    with Image.open(entry.icon_path or "") as image:
        assert image.size == (32, 64)
    with Image.open(entry.key_icon_path or "") as image:
        assert image.size == (112, 112)


def test_origin_for_url_rejects_non_http_url() -> None:
    """URL icon cache 只接受 http/https 且必须包含 host。

    入参：无。
    返回：无返回值；断言通过代表危险或不完整 URL 不会触发 favicon 请求。
    错误处理：非法 URL 未拒绝时由 pytest 报告。
    副作用：无。
    """

    with pytest.raises(ValueError):
        origin_for_url("javascript:alert(1)")
    with pytest.raises(ValueError):
        origin_for_url("https:///missing-host")


def _fake_site_fetcher(calls: list[str]):
    """构造记录请求 URL 的 fake 站点 fetcher。

    入参：`calls` 是测试内存列表。
    返回：可注入 `UrlIconCache` 的 fetcher。
    错误处理：无。
    副作用：写入 `calls`。
    """

    def fetcher(url: str) -> bytes:
        """返回 HTML 或 32x32 PNG favicon。

        入参：`url` 是 cache 生成的 favicon URL。
        返回：HTML 或 PNG bytes。
        错误处理：无。
        副作用：写入 `calls`。
        """

        calls.append(url)
        if url == "https://example.com":
            return b'<html><head><link rel="icon" sizes="32x32" href="/assets/icon.png"></head></html>'
        if url == "https://example.com/assets/icon.png":
            return _png_bytes((32, 32), (50, 120, 210, 255))
        return b""

    return fetcher


def _png_bytes(size: tuple[int, int], color: tuple[int, int, int, int]) -> bytes:
    """生成测试 PNG bytes。

    入参：`size` 是图片尺寸；`color` 是 RGBA 填充色。
    返回：PNG bytes。
    错误处理：无。
    副作用：只写内存 buffer。
    """

    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _near_color(
    actual: tuple[int, int, int] | tuple[int, int, int, int],
    expected: tuple[int, int, int],
    *,
    tolerance: int = 8,
) -> bool:
    """判断实际颜色是否接近期望 RGB。

    入参：`actual` 是 Pillow pixel；`expected` 是 RGB；`tolerance` 是单通道容差。
    返回：所有 RGB 通道都在容差内时为 True。
    错误处理：无。
    副作用：无。
    """

    return all(abs(int(actual[index]) - expected[index]) <= tolerance for index in range(3))
