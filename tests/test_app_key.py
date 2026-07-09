"""本机 App 按键图像渲染测试。

这些测试只使用 pytest 临时目录里的 fake Finder `.app` bundle 和内存 Pillow 图像；
不扫描用户真实应用目录、不启动 App、不访问真实 N4 Pro。
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from PIL import Image

from agent_deck.rendering.app_key import render_app_key_image


def test_render_app_key_image_uses_app_bundle_icon(tmp_path: Path) -> None:
    """App key renderer 应优先使用 `.app` bundle 内的真实图标。

    入参：`tmp_path` 提供 fake Finder bundle。
    返回：无返回值；断言通过代表输出为 N4 Pro key 图，且中心像素来自 App 图标。
    错误处理：图标未加载或尺寸不对时由 pytest 报告。
    副作用：只写 pytest 临时目录。
    """

    app_path = _fake_finder_app(tmp_path)

    image = render_app_key_image(
        app_name="Finder",
        app_path=str(app_path),
        icon_token="FI",
    )

    assert image.size == (112, 112)
    assert image.mode == "RGB"
    assert _near_color(image.getpixel((56, 56)), (20, 120, 220))


def test_render_app_key_image_falls_back_to_token() -> None:
    """App key renderer 缺少 bundle 图标时应绘制 token fallback。

    入参：无。
    返回：无返回值；断言通过代表 fallback 仍输出可下发图片。
    错误处理：fallback 未绘制或尺寸不对时由 pytest 报告。
    副作用：无。
    """

    image = render_app_key_image(
        app_name="Finder",
        app_path="/missing/Finder.app",
        icon_token="FI",
        icon_color="#345678",
    )

    assert image.size == (112, 112)
    assert image.mode == "RGB"
    assert _near_color(image.getpixel((30, 30)), (52, 86, 120))
    assert _near_color(image.getpixel((8, 8)), (11, 14, 18), tolerance=2)


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


def _near_color(
    actual: tuple[int, int, int],
    expected: tuple[int, int, int],
    *,
    tolerance: int = 24,
) -> bool:
    """判断采样像素是否接近目标 RGB。

    入参：`actual` 是采样像素；`expected` 是目标颜色；`tolerance` 是每通道容差。
    返回：三通道都在容差内时返回 True。
    错误处理：无。
    副作用：无。
    """

    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected, strict=True))
