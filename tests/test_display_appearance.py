"""跨 Key 与 Touch bar 显示外观的模型、渲染、缓存和 API 合同测试。

测试只使用内存 Pillow 图、pytest 临时目录与进程内 FastAPI TestClient；不会连接真实 N4
Pro、不读取用户配置，也不会修改仓库内预生成资产。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops
from pydantic import ValidationError

from agent_deck.rendering.appearance import (
    DeckAppearanceSettings,
    contrast_ratio,
    resolve_render_palette,
)
from agent_deck.rendering.app_key import render_app_key_image
from agent_deck.rendering.brand import render_agent_deck_splash_touchscreen
from agent_deck.rendering.codex_key_frame_compositor import (
    composite_codex_key_frame_paths,
)
from agent_deck.rendering.n4pro_panel import compose_n4pro_background
from agent_deck.server.app import create_app
from agent_deck.server.display_appearance_store import (
    DisplayAppearanceStoreError,
    load_display_appearance,
    save_display_appearance,
)


def test_display_appearance_accepts_only_optional_opaque_hex() -> None:
    """模型应区分“不设定”和完整不透明十六进制覆盖。

    入参：无；直接构造合法与非法模型。
    返回：无；断言规范化、null 语义和严格格式。
    错误处理：非法格式应抛 Pydantic ValidationError。
    副作用：无。
    """

    assert DeckAppearanceSettings().background_color is None
    assert (
        DeckAppearanceSettings(background_color="#2a405f").background_color
        == "#2A405F"
    )
    for invalid in ("black", "#123", "#11223344", "112233"):
        with pytest.raises(ValidationError):
            DeckAppearanceSettings(background_color=invalid)


def test_custom_palette_keeps_readable_foreground_on_light_and_dark_colors() -> None:
    """自动前景应在极亮和极暗用户背景上保持可读对比度。

    入参：两个极端背景色。
    返回：无；断言主前景对比度至少 7:1。
    错误处理：无。
    副作用：无。
    """

    for color in ("#05080C", "#F5F1E8"):
        palette = resolve_render_palette(
            DeckAppearanceSettings(background_color=color),
            default_background=(11, 14, 18),
        )
        assert palette.custom is True
        assert contrast_ratio(palette.background, palette.foreground) >= 7.0


def test_default_mode_preserves_existing_app_key_pixels() -> None:
    """显式默认外观必须与未传外观的旧 App fallback 图逐像素一致。

    入参：固定缺失 App 路径与 token。
    返回：无；断言两图完全相同。
    错误处理：渲染异常按测试失败报告。
    副作用：仅创建内存图片。
    """

    legacy = render_app_key_image(
        app_name="Test",
        app_path="/missing/Test.app",
        icon_token="T",
    )
    explicit_default = render_app_key_image(
        app_name="Test",
        app_path="/missing/Test.app",
        icon_token="T",
        appearance=DeckAppearanceSettings(),
    )

    assert ImageChops.difference(legacy, explicit_default).getbbox() is None


def test_custom_background_reaches_app_key_and_full_touch_surface() -> None:
    """同一用户背景应同时成为 App Key 与完整 Touch bar 的基础色。

    入参：固定自定义背景。
    返回：无；断言两个表面边角像素使用同一 RGB。
    错误处理：渲染异常按测试失败报告。
    副作用：仅创建内存图片。
    """

    appearance = DeckAppearanceSettings(background_color="#28405A")
    key_image = render_app_key_image(
        app_name="Test",
        app_path="/missing/Test.app",
        icon_token="T",
        appearance=appearance,
    )
    panel = compose_n4pro_background(
        render_agent_deck_splash_touchscreen(appearance=appearance).crop(
            (0, 172, 800, 308)
        ),
        appearance=appearance,
    )

    assert key_image.getpixel((0, 0)) == (40, 64, 90)
    assert panel.convert("RGB").getpixel((0, 0)) == (40, 64, 90)


def test_agent_rgba_frames_are_composited_without_overwriting_source(
    tmp_path: Path,
) -> None:
    """预生成 Agent 帧应派生 RGB 缓存，透明区使用用户背景且源图保持 RGBA。

    入参：pytest 临时目录。
    返回：无；断言缓存模式、透明区和不透明内容。
    错误处理：I/O 或 Pillow 异常按测试失败报告。
    副作用：只写临时源图与派生缓存。
    """

    source = tmp_path / "working" / "frame_000.png"
    source.parent.mkdir()
    image = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    image.putpixel((2, 2), (230, 40, 20, 255))
    image.save(source)

    mapping = composite_codex_key_frame_paths(
        {6: (source,)},
        background_color="#123456",
        cache_root=tmp_path / "cache",
    )

    with Image.open(mapping[6][0]) as derived:
        assert derived.mode == "RGB"
        assert derived.getpixel((0, 0)) == (18, 52, 86)
        assert derived.getpixel((2, 2)) == (230, 40, 20)
    with Image.open(source) as original:
        assert original.mode == "RGBA"
        assert original.getpixel((0, 0))[3] == 0


def test_display_appearance_store_round_trips_versioned_envelope(
    tmp_path: Path,
) -> None:
    """独立存储应版本化往返，并拒绝未知版本。

    入参：pytest 临时目录。
    返回：无；断言保存内容、读取模型和错误边界。
    错误处理：未知版本应抛专用 store error。
    副作用：只写临时 JSON。
    """

    path = tmp_path / "deck-appearance.json"
    settings = DeckAppearanceSettings(background_color="#345678")
    save_display_appearance(settings, path)

    assert load_display_appearance(path) == settings
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    assert persisted["background_color"] == "#345678"
    path.write_text('{"version": 99, "settings": {}}', encoding="utf-8")
    with pytest.raises(DisplayAppearanceStoreError):
        load_display_appearance(path)


def test_display_appearance_api_persists_and_increments_revision(
    tmp_path: Path,
) -> None:
    """独立 API 应持久化设置、统一应用并把结果暴露到 status。

    入参：pytest 临时目录。
    返回：无；断言 GET/PUT、revision、磁盘与 status 一致。
    错误处理：HTTP 或模型异常按测试失败报告。
    副作用：只写临时 JSON，并创建/清理 daemon 生命周期临时缓存。
    """

    path = tmp_path / "deck-appearance.json"
    with TestClient(create_app(display_appearance_path=path)) as client:
        initial = client.get("/ui/display-appearance")
        updated = client.put(
            "/ui/display-appearance",
            json={"background_color": "#102A43"},
        )
        status = client.get("/status")

    assert initial.status_code == 200
    assert initial.json()["settings"] == {"background_color": None}
    assert updated.status_code == 200
    assert updated.json()["display_appearance"]["revision"] == 1
    assert status.json()["display_appearance"]["settings"] == {
        "background_color": "#102A43"
    }
    assert load_display_appearance(path) == DeckAppearanceSettings(
        background_color="#102A43"
    )


def test_web_shell_exposes_separate_display_appearance_control() -> None:
    """Web 设置入口应独立于 Touch bar/PETS，并描述跨表面作用范围。

    入参：无；读取打包静态页面与脚本。
    返回：无；断言独立入口、双预览和 API 接线存在。
    错误处理：静态资源缺失会导致 HTTP 断言失败。
    副作用：只读包内资源。
    """

    client = TestClient(create_app())
    html = client.get("/").text
    script = client.get("/web/app.js").text

    assert 'id="appearanceControl"' in html
    assert 'id="petsPanelControl"' in html
    assert "显示外观" in html
    assert "appearance-preview-pair" in script
    assert 'fetch("/ui/display-appearance"' in script
    assert "display_appearance: state.displayAppearance" in script
