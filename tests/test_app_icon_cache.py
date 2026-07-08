"""App 图标缓存测试。

这些测试只使用 pytest 临时目录里的 fake Finder `.app` bundle 和缓存目录；不读取用户真实
应用目录、不启动 App、不访问真实 N4 Pro。
"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

from PIL import Image

from agent_deck.actions.app_icon_cache import AppIconCache, cache_key_for_app
from agent_deck.actions.apps import LocalAppInfo


def test_app_icon_cache_writes_web_and_key_icons(tmp_path: Path) -> None:
    """App icon cache 应生成 Web 图标、硬件 key 图和 metadata。

    入参：`tmp_path` 提供 fake Finder bundle 和缓存根目录。
    返回：无返回值；断言通过代表 GUI 和硬件 renderer 可复用同一缓存。
    错误处理：文件缺失、URL 错误或图片尺寸不对时由 pytest 报告。
    副作用：只写 pytest 临时目录。
    """

    app_path = _fake_finder_app(tmp_path)
    cache = AppIconCache(tmp_path / "cache")

    entry = cache.ensure_for_app(_app_info(app_path))

    assert entry.cache_key == "com.apple.finder"
    assert entry.icon_url == "/ui/app-icons/com.apple.finder/icon-96.png"
    assert entry.key_icon_url == "/ui/app-icons/com.apple.finder/key-112.png"
    assert entry.updated is True
    assert (tmp_path / "cache/com.apple.finder/icon-96.png").is_file()
    assert (tmp_path / "cache/com.apple.finder/key-112.png").is_file()
    metadata = json.loads(
        (tmp_path / "cache/com.apple.finder/metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["bundle_id"] == "com.apple.finder"
    assert metadata["fingerprint"]["icon_path"].endswith("Finder.png")

    with Image.open(tmp_path / "cache/com.apple.finder/icon-96.png") as image:
        assert image.size == (64, 64)
    with Image.open(tmp_path / "cache/com.apple.finder/key-112.png") as image:
        assert image.size == (112, 112)


def test_app_icon_cache_reuses_fresh_metadata(tmp_path: Path) -> None:
    """App icon cache metadata 新鲜时不应重复生成图标。

    入参：`tmp_path` 提供 fake Finder bundle 和缓存根目录。
    返回：无返回值；断言通过代表第二次 ensure 返回 `updated=False`。
    错误处理：metadata 复用失败时由 pytest 报告。
    副作用：只写 pytest 临时目录。
    """

    app_path = _fake_finder_app(tmp_path)
    cache = AppIconCache(tmp_path / "cache")

    first = cache.ensure_for_app(_app_info(app_path))
    second = cache.ensure_for_app(_app_info(app_path))

    assert first.updated is True
    assert second.updated is False
    assert cache.resolve_file("com.apple.finder", "icon-96.png") == (
        tmp_path / "cache/com.apple.finder/icon-96.png"
    )
    assert cache.resolve_file("../bad", "icon-96.png") is None


def test_cache_key_for_app_falls_back_to_path_hash() -> None:
    """缺少 bundle id 时 cache key 应使用稳定 path hash。

    入参：无。
    返回：无返回值；断言通过代表无 bundle id 的用户 App 也能缓存图标。
    错误处理：cache key 不稳定时由 pytest 报告。
    副作用：无。
    """

    first = cache_key_for_app(bundle_id=None, app_path="/Applications/Foo.app")
    second = cache_key_for_app(bundle_id=None, app_path="/Applications/Foo.app")

    assert first == second
    assert first.startswith("path-")


def _app_info(app_path: Path) -> LocalAppInfo:
    """构造 fake Finder 的 `LocalAppInfo`。

    入参：`app_path` 是 fake Finder bundle。
    返回：测试用 App info。
    错误处理：字段非法由 Pydantic 抛出。
    副作用：无。
    """

    return LocalAppInfo(
        name="Finder",
        app_path=str(app_path),
        bundle_id="com.apple.finder",
        icon_token="FI",
    )


def _fake_finder_app(tmp_path: Path) -> Path:
    """创建测试用 fake Finder `.app` bundle。

    入参：`tmp_path` 是 fake 应用根目录。
    返回：fake Finder bundle 路径。
    错误处理：文件写入失败按 pathlib/Pillow 异常传播。
    副作用：写 pytest 临时目录。
    """

    app = tmp_path / "Finder.app"
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleName": "Finder",
                "CFBundleIdentifier": "com.apple.finder",
                "CFBundleIconFile": "Finder.png",
            },
            handle,
        )
    Image.new("RGBA", (64, 64), (20, 120, 220, 255)).save(resources / "Finder.png")
    return app
