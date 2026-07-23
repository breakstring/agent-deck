"""验证 ChatGPT 内置宠物 ASAR catalog 的边界、顺序和按需解码。"""

from __future__ import annotations

import json
import struct
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from PIL import Image

from agent_deck.adapters.codex_builtin_pets import (
    CodexBuiltinPetCatalog,
    CodexBuiltinPetCatalogStatus,
)
from agent_deck.adapters.codex_pet import CodexPetResolver
from agent_deck.config import CodexPetMotion, CodexRemotePetSource
from agent_deck.core.events import AgentSource
from agent_deck.core.state import AgentState, AgentStatus
from agent_deck.server.codex_pet_runtime import CodexPetRuntime
from agent_deck.server.pets_panel_settings_store import N4ProPetsPanelSettings


def _webp_payload() -> bytes:
    """创建几何合法且体积很小的 v2 透明测试图集。"""

    image = Image.new("RGBA", (1536, 2288), (0, 0, 0, 0))
    image.putpixel((0, 0), (255, 255, 255, 255))
    buffer = BytesIO()
    image.save(buffer, format="WEBP", lossless=True)
    return buffer.getvalue()


def _write_asar(path: Path, entries: list[tuple[str, bytes]]) -> None:
    """写一个只含测试资源的最小标准 ASAR。"""

    files: dict[str, object] = {}
    offset = 0
    for internal_path, payload in entries:
        node = files
        parts = internal_path.split("/")
        for part in parts[:-1]:
            raw_child = node.setdefault(part, {"files": {}})
            assert isinstance(raw_child, dict)
            child_files = raw_child["files"]
            assert isinstance(child_files, dict)
            node = child_files
        node[parts[-1]] = {"offset": str(offset), "size": len(payload)}
        offset += len(payload)
    header = json.dumps({"files": files}, separators=(",", ":")).encode()
    header_string_size = (len(header) + 3) // 4 * 4
    header_block_size = 8 + header_string_size
    prefix = struct.pack("<4I", 4, header_block_size, header_string_size, len(header))
    path.write_bytes(
        prefix
        + header
        + b"\0" * (header_string_size - len(header))
        + b"".join(payload for _name, payload in entries)
    )


def test_catalog_discovers_and_loads_builtin_pet(tmp_path: Path) -> None:
    """catalog 只读解析 header，并可按 descriptor 解码 v2 图集。"""

    asar_path = tmp_path / "app.asar"
    payload = _webp_payload()
    _write_asar(
        asar_path,
        [
            ("webview/assets/rocky-spritesheet-v2-test.webp", payload),
            ("webview/assets/codex-spritesheet-v2-test.webp", payload),
        ],
    )

    catalog = CodexBuiltinPetCatalog(app_asar_paths=(asar_path,))
    snapshot = catalog.resolve()

    assert snapshot.status == CodexBuiltinPetCatalogStatus.LOADED
    assert [item.asset_id for item in snapshot.descriptors] == ["codex", "rocky"]
    asset = catalog.load_asset(snapshot.descriptors[0])
    assert asset.selected_avatar_id == "builtin:codex"
    assert asset.sprite_version_number == 2
    assert asset.spritesheet.size == (1536, 2288)


def test_catalog_rejects_out_of_bounds_entry(tmp_path: Path) -> None:
    """被篡改为越界的 entry 不得进入可加载 catalog。"""

    asar_path = tmp_path / "app.asar"
    _write_asar(
        asar_path,
        [("webview/assets/codex-spritesheet-v2-test.webp", b"x")],
    )
    raw = bytearray(asar_path.read_bytes())
    raw[-1:] = b""
    asar_path.write_bytes(raw)

    snapshot = CodexBuiltinPetCatalog(app_asar_paths=(asar_path,)).resolve()

    assert snapshot.status == CodexBuiltinPetCatalogStatus.INVALID
    assert snapshot.descriptors == ()


def test_runtime_renders_remote_actor_from_builtin_catalog(tmp_path: Path) -> None:
    """远端活动任务可按策略分配内置宠物并生成完整 N4 Pro 背景。"""

    asar_path = tmp_path / "app.asar"
    payload = _webp_payload()
    _write_asar(
        asar_path,
        [
            ("webview/assets/codex-spritesheet-v2-test.webp", payload),
            ("webview/assets/hoots-spritesheet-v2-test.webp", payload),
        ],
    )
    codex_home = tmp_path / "codex-home"
    runtime = CodexPetRuntime(
        enabled=True,
        panel_fps=8,
        motion=CodexPetMotion.FULL,
        cache_root=tmp_path / "cache",
        fallback_key_path=None,
        panel_settings=N4ProPetsPanelSettings(
            remote_pet_source=CodexRemotePetSource.REMOTE_CONFIG
        ),
        resolver=CodexPetResolver(
            environment={"CODEX_HOME": str(codex_home)},
            home_dir=tmp_path,
        ),
        builtin_catalog=CodexBuiltinPetCatalog(app_asar_paths=(asar_path,)),
        started_at_monotonic=100.0,
    )
    now = datetime.now(UTC)
    runtime.refresh(now=now)
    runtime.update_remote_pet_selection(
        "ssh-test",
        selected_avatar_id="builtin:hoots",
        config_available=True,
    )
    runtime.update_activity(
        (
            AgentState(
                agent_key="codex:remote",
                source=AgentSource.CODEX,
                display_name="remote",
                status=AgentStatus.THINKING,
                status_since=now,
                last_event_at=now,
                focus_target="codex-app:remote-ssh:ssh-test:thread-1",
            ),
        ),
        updated_at=now,
    )

    revision, image = runtime.panel_background(monotonic_seconds=101.0)
    diagnostics = runtime.diagnostics()["panel_colony"]

    assert revision == 1
    assert image is not None
    assert image.size == (800, 480)
    assert diagnostics["actor_count"] == 1
    assert diagnostics["renderable_actor_count"] == 1
    assert diagnostics["assignments"]["codex:remote"].endswith("builtin:hoots")


def test_remote_config_maps_fireball_name_id_without_hash_fallback(
    tmp_path: Path,
) -> None:
    """远端 config/read 的 ``fireball`` 名字型 ID 必须精确命中 Fireball。

    入参：pytest 临时目录。
    返回：无；断言 assignment 不进入稳定随机或 unavailable fallback。
    错误处理：无。
    副作用：只读写 pytest 合成 ASAR 和内存 runtime。
    """

    asar_path = tmp_path / "app.asar"
    _write_asar(
        asar_path,
        [("webview/assets/fireball-spritesheet-v2-test.webp", _webp_payload())],
    )
    runtime = CodexPetRuntime(
        enabled=True,
        panel_fps=8,
        motion=CodexPetMotion.FULL,
        cache_root=tmp_path / "cache",
        fallback_key_path=None,
        panel_settings=N4ProPetsPanelSettings(
            remote_pet_source=CodexRemotePetSource.REMOTE_CONFIG
        ),
        resolver=CodexPetResolver(
            environment={"CODEX_HOME": str(tmp_path / "missing-codex")},
            home_dir=tmp_path,
        ),
        builtin_catalog=CodexBuiltinPetCatalog(app_asar_paths=(asar_path,)),
        started_at_monotonic=100.0,
    )
    now = datetime.now(UTC)
    runtime.refresh(now=now)
    runtime.update_remote_pet_selection(
        "ssh-test",
        selected_avatar_id="fireball",
        config_available=True,
    )
    runtime.update_activity(
        (
            AgentState(
                agent_key="codex:remote-fireball",
                source=AgentSource.CODEX,
                display_name="remote",
                status=AgentStatus.THINKING,
                status_since=now,
                last_event_at=now,
                focus_target="codex-app:remote-ssh:ssh-test:thread-1",
            ),
        ),
        updated_at=now,
    )

    assignment = runtime.diagnostics()["panel_colony"]["assignments"][
        "codex:remote-fireball"
    ]

    assert assignment == "remote_config:builtin:fireball"
