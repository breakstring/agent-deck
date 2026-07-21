"""Codex 宠物选择、资产安全边界与全局活动聚合测试。

这些测试只在 pytest 临时目录创建合成 v1/v2 图集和配置，不读取用户真实 Codex 宠物、
不复制 Rick 或内置资源、不启动 daemon，也不访问 N4 Pro 硬件。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from agent_deck.adapters.codex_pet import (
    CODEX_PET_VERSION_GEOMETRY,
    CodexPetResolutionStatus,
    CodexPetResolver,
    PetActivity,
    derive_pet_activity,
    load_custom_codex_pet,
    resolve_codex_home,
)
from agent_deck.core.events import AgentSource
from agent_deck.core.state import AgentState, AgentStatus


def _write_config(codex_home: Path, selected_id: str | None) -> None:
    """在测试目录写入最小 Codex 顶层宠物选择配置。

    入参：``codex_home`` 是 pytest 临时目录；``selected_id`` 为空时写空 TOML。
    返回：无显式返回。
    错误处理：测试目录不可写时让 pathlib 异常传播。
    副作用：仅写入临时 ``config.toml``。
    """

    codex_home.mkdir(parents=True, exist_ok=True)
    content = (
        f'selected-avatar-id = {json.dumps(selected_id)}\n'
        if selected_id is not None
        else ""
    )
    (codex_home / "config.toml").write_text(content, encoding="utf-8")


def _write_pet(
    codex_home: Path,
    *,
    name: str,
    version: int = 1,
    family: str = "pets",
    display_name: str | None = None,
    image_size: tuple[int, int] | None = None,
    spritesheet_path: str = "spritesheet.png",
    transparent_residue: bool = False,
) -> Path:
    """创建一个只供解析测试使用的合成自定义宠物包。

    入参：可切换版本、current/legacy 目录、几何、图集路径和透明 RGB 残留。
    返回：manifest 路径。
    错误处理：未知测试版本若未显式给 size 会回退 v1 尺寸，供 unknown 流程测试。
    副作用：只在 pytest 临时目录写 JSON 和 PNG。
    """

    package_dir = codex_home / family / name
    package_dir.mkdir(parents=True, exist_ok=True)
    filename = "pet.json" if family == "pets" else "avatar.json"
    manifest = {
        "id": name,
        "displayName": display_name or name.title(),
        "description": "synthetic test pet",
        "spritesheetPath": spritesheet_path,
        "spriteVersionNumber": version,
        "kind": "object",
    }
    manifest_path = package_dir / filename
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    if ".." not in Path(spritesheet_path).parts and not Path(spritesheet_path).is_absolute():
        resolved_size = image_size or CODEX_PET_VERSION_GEOMETRY.get(
            version, CODEX_PET_VERSION_GEOMETRY[1]
        )[:2]
        image = Image.new("RGBA", resolved_size, (0, 0, 0, 0))
        image.putpixel((0, 0), (42, 180, 220, 255))
        if transparent_residue:
            image.putpixel((1, 0), (123, 45, 67, 0))
        image_path = package_dir / spritesheet_path
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(image_path, format="PNG")
    return manifest_path


def _state(
    *,
    key: str,
    status: AgentStatus,
    status_since: datetime,
    source: AgentSource = AgentSource.CODEX,
    child: bool = False,
) -> AgentState:
    """构造活动优先级测试所需的最小 frozen AgentState。

    入参：指定稳定 key、状态、进入时间、来源和 child 标志。
    返回：合法 timezone-aware ``AgentState``。
    错误处理：非法字段由 Pydantic 直接报告给测试。
    副作用：无。
    """

    return AgentState(
        agent_key=key,
        source=source,
        display_name=key,
        status=status,
        status_since=status_since,
        last_event_at=status_since,
        parent_agent_key="codex:parent" if child else None,
        is_child_agent=child,
    )


@pytest.mark.parametrize("version", [1, 2])
def test_loads_v1_and_v2_geometry(tmp_path: Path, version: int) -> None:
    """验证 v1 8x9 与 v2 8x11 都按 manifest 版本加载。

    入参：pytest 临时目录和参数化版本。
    返回：无；通过断言验证几何、行数和指纹。
    错误处理：加载失败即测试失败。
    副作用：创建合成图集。
    """

    _write_pet(tmp_path, name="orbit", version=version)
    asset = load_custom_codex_pet(
        codex_home=tmp_path,
        selected_avatar_id="custom:orbit",
    )

    assert asset.sprite_version_number == version
    assert asset.row_count == CODEX_PET_VERSION_GEOMETRY[version][2]
    assert asset.spritesheet.size == CODEX_PET_VERSION_GEOMETRY[version][:2]
    assert len(asset.source_fingerprint) == 64


def test_current_pets_directory_wins_over_legacy_avatars(tmp_path: Path) -> None:
    """验证 current 与 legacy 包并存时优先使用 pets/pet.json。

    入参：pytest 临时目录。
    返回：无；断言展示名和 manifest 路径来自 current 包。
    错误处理：加载失败即测试失败。
    副作用：创建两个同名合成包。
    """

    _write_pet(tmp_path, name="orbit", family="avatars", display_name="Legacy")
    _write_pet(tmp_path, name="orbit", family="pets", display_name="Current")

    asset = load_custom_codex_pet(
        codex_home=tmp_path,
        selected_avatar_id="custom:orbit",
    )

    assert asset.manifest.display_name == "Current"
    assert asset.manifest_path == (tmp_path / "pets/orbit/pet.json").resolve()


@pytest.mark.parametrize(
    ("spritesheet_path", "image_size"),
    [
        ("../outside.png", None),
        ("/tmp/outside.png", None),
        ("spritesheet.png", (1535, 1872)),
    ],
)
def test_rejects_path_escape_and_invalid_geometry(
    tmp_path: Path,
    spritesheet_path: str,
    image_size: tuple[int, int] | None,
) -> None:
    """验证绝对/父目录路径和错误图集几何均明确降级。

    入参：pytest 临时目录、危险路径或错误尺寸。
    返回：无；断言 resolver 返回 INVALID 且无资产。
    错误处理：异常由 resolver 转换，不应逃出测试。
    副作用：创建临时配置和 manifest。
    """

    _write_config(tmp_path, "custom:orbit")
    _write_pet(
        tmp_path,
        name="orbit",
        spritesheet_path=spritesheet_path,
        image_size=image_size,
    )

    resolution = CodexPetResolver().resolve(codex_home=tmp_path)

    assert resolution.status == CodexPetResolutionStatus.INVALID
    assert resolution.asset is None


def test_rejects_spritesheet_symlink_escape(tmp_path: Path) -> None:
    """验证指向宠物目录外部的图集 symlink 不会被解码。

    入参：pytest 临时目录。
    返回：无；断言 INVALID 且无资产。
    错误处理：平台不支持 symlink 时跳过测试。
    副作用：仅在临时目录创建 PNG 和 symlink。
    """

    _write_config(tmp_path, "custom:orbit")
    manifest_path = _write_pet(tmp_path, name="orbit")
    outside = tmp_path / "outside.png"
    Image.new("RGBA", (1536, 1872), (0, 0, 0, 0)).save(outside)
    sheet = manifest_path.parent / "spritesheet.png"
    sheet.unlink()
    try:
        sheet.symlink_to(outside)
    except OSError:
        pytest.skip("当前平台不允许创建 symlink")

    resolution = CodexPetResolver().resolve(codex_home=tmp_path)

    assert resolution.status == CodexPetResolutionStatus.INVALID
    assert resolution.asset is None


def test_normalizes_transparent_rgb_without_changing_cell_geometry(tmp_path: Path) -> None:
    """验证透明 RGB 残留被清零、warning 被记录且图集坐标不变。

    入参：pytest 临时目录。
    返回：无；断言残留像素变为全零而相邻不透明像素仍在原坐标。
    错误处理：加载失败即测试失败。
    副作用：创建一张带合成残留的图集。
    """

    _write_pet(tmp_path, name="orbit", transparent_residue=True)

    asset = load_custom_codex_pet(
        codex_home=tmp_path,
        selected_avatar_id="custom:orbit",
    )

    assert asset.spritesheet.getpixel((0, 0)) == (42, 180, 220, 255)
    assert asset.spritesheet.getpixel((1, 0)) == (0, 0, 0, 0)
    assert asset.warnings == ("已将完全透明像素中的非零 RGB 残留归零",)


def test_source_fingerprint_is_stable_and_changes_with_manifest(tmp_path: Path) -> None:
    """验证来源未变化时指纹稳定，manifest 元数据变化后指纹改变。

    入参：pytest 临时目录。
    返回：无；断言重复加载相等、文件 size/mtime 变化后不等。
    错误处理：加载失败即测试失败。
    副作用：创建并重写临时 manifest。
    """

    manifest_path = _write_pet(tmp_path, name="orbit")
    first = load_custom_codex_pet(
        codex_home=tmp_path,
        selected_avatar_id="custom:orbit",
    )
    second = load_custom_codex_pet(
        codex_home=tmp_path,
        selected_avatar_id="custom:orbit",
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["description"] += " changed"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    changed = load_custom_codex_pet(
        codex_home=tmp_path,
        selected_avatar_id="custom:orbit",
    )

    assert first.source_fingerprint == second.source_fingerprint
    assert changed.source_fingerprint != first.source_fingerprint


def test_resolver_reuses_unchanged_decoded_asset_and_reloads_changed_source(
    tmp_path: Path,
) -> None:
    """验证轮询未变化来源时复用内存图集，来源变化后重新安全解码。

    入参：pytest 临时目录。
    返回：无；断言未变化时对象 identity 稳定，manifest 变化后产生新资产与新指纹。
    错误处理：解析或重载失败即测试失败。
    副作用：创建合成宠物包并重写临时 manifest。
    """

    _write_config(tmp_path, "custom:orbit")
    manifest_path = _write_pet(tmp_path, name="orbit")
    resolver = CodexPetResolver()

    first = resolver.resolve(codex_home=tmp_path)
    unchanged = resolver.resolve(codex_home=tmp_path)
    assert first.asset is not None
    assert unchanged.asset is first.asset

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["description"] += " changed"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    changed = resolver.resolve(codex_home=tmp_path)

    assert changed.asset is not None
    assert changed.asset is not first.asset
    assert changed.asset.source_fingerprint != first.asset.source_fingerprint


def test_same_selection_uses_lkg_but_changed_selection_does_not(tmp_path: Path) -> None:
    """验证同 ID 短暂失败保留 LKG，而选择变化后绝不冒充旧宠物。

    入参：pytest 临时目录。
    返回：无；依次断言 LOADED、STALE 和新 ID INVALID 无资产。
    错误处理：resolver 应吸收包错误。
    副作用：创建并损坏临时图集、重写临时配置。
    """

    _write_config(tmp_path, "custom:orbit")
    manifest_path = _write_pet(tmp_path, name="orbit")
    resolver = CodexPetResolver()
    loaded = resolver.resolve(codex_home=tmp_path)
    assert loaded.status == CodexPetResolutionStatus.LOADED
    assert loaded.asset is not None

    (manifest_path.parent / "spritesheet.png").write_bytes(b"not an image")
    stale = resolver.resolve(codex_home=tmp_path)
    assert stale.status == CodexPetResolutionStatus.STALE
    assert stale.asset is loaded.asset

    _write_config(tmp_path, "custom:new-pet")
    changed = resolver.resolve(codex_home=tmp_path)
    assert changed.status == CodexPetResolutionStatus.INVALID
    assert changed.asset is None


def test_unknown_version_never_uses_same_id_lkg(tmp_path: Path) -> None:
    """验证同 ID manifest 变为未知版本后明确降级而不展示旧行语义。

    入参：pytest 临时目录。
    返回：无；断言 UNKNOWN_VERSION 且 asset 为空。
    错误处理：resolver 应转换 unknown version 异常。
    副作用：创建 v1 包后重写临时 manifest。
    """

    _write_config(tmp_path, "custom:orbit")
    manifest_path = _write_pet(tmp_path, name="orbit")
    resolver = CodexPetResolver()
    assert resolver.resolve(codex_home=tmp_path).is_available
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["spriteVersionNumber"] = 99
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    resolution = resolver.resolve(codex_home=tmp_path)

    assert resolution.status == CodexPetResolutionStatus.UNKNOWN_VERSION
    assert resolution.asset is None


def test_config_failure_and_builtin_selection_have_no_asset(tmp_path: Path) -> None:
    """验证配置解析失败与内置宠物都进入无旧资产的诊断结果。

    入参：pytest 临时目录。
    返回：无；断言 CONFIG_ERROR 和 BUILTIN_UNSUPPORTED。
    错误处理：TOML 错误应由 resolver 吸收。
    副作用：写入临时无效配置并随后替换。
    """

    (tmp_path / "config.toml").write_text("not = [valid", encoding="utf-8")
    resolver = CodexPetResolver()
    broken = resolver.resolve(codex_home=tmp_path)
    assert broken.status == CodexPetResolutionStatus.CONFIG_ERROR
    assert broken.asset is None

    _write_config(tmp_path, "builtin:codex")
    builtin = resolver.resolve(codex_home=tmp_path)
    assert builtin.status == CodexPetResolutionStatus.BUILTIN_UNSUPPORTED
    assert builtin.asset is None


def test_reads_current_desktop_selected_avatar_id(tmp_path: Path) -> None:
    """验证当前 Codex 配置把全局选择存放在 ``[desktop]`` 时仍可解析。

    入参：pytest 临时目录。
    返回：无；断言自定义宠物加载成功。
    错误处理：解析失败即测试失败。
    副作用：写入临时配置和合成宠物包。
    """

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.toml").write_text(
        '[desktop]\nselected-avatar-id = "custom:orbit"\n',
        encoding="utf-8",
    )
    _write_pet(tmp_path, name="orbit")

    resolution = CodexPetResolver().resolve(codex_home=tmp_path)

    assert resolution.status == CodexPetResolutionStatus.LOADED
    assert resolution.selected_avatar_id == "custom:orbit"


def test_resolve_codex_home_prefers_environment_then_home(tmp_path: Path) -> None:
    """验证 CODEX_HOME 优先且缺省落到指定 home 的 ``.codex``。

    入参：pytest 临时目录。
    返回：无；断言两种查找路径。
    错误处理：无。
    副作用：无；只构造 Path。
    """

    configured = tmp_path / "custom-home"
    assert resolve_codex_home(
        environment={"CODEX_HOME": str(configured)}, home_dir=tmp_path
    ) == configured
    assert resolve_codex_home(environment={}, home_dir=tmp_path) == tmp_path / ".codex"


def test_global_activity_priority_recency_and_child_exclusion() -> None:
    """验证官方优先级、同级最新时间、非 Codex 与 child 过滤。

    入参：无；使用固定 UTC 时间构造状态。
    返回：无；断言 waiting 顶层状态获胜且触发源是同级最新者。
    错误处理：模型或聚合异常即测试失败。
    副作用：无。
    """

    base = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
    states = [
        _state(key="codex:running", status=AgentStatus.THINKING, status_since=base),
        _state(
            key="codex:ready",
            status=AgentStatus.COMPLETED_RECENTLY,
            status_since=base + timedelta(seconds=1),
        ),
        _state(
            key="codex:block",
            status=AgentStatus.ERROR,
            status_since=base + timedelta(seconds=2),
        ),
        _state(
            key="codex:child-wait",
            status=AgentStatus.WAITING_USER,
            status_since=base + timedelta(seconds=10),
            child=True,
        ),
        _state(
            key="claude:wait",
            status=AgentStatus.WAITING_USER,
            status_since=base + timedelta(seconds=11),
            source=AgentSource.CLAUDE_CODE,
        ),
        _state(
            key="codex:older-wait",
            status=AgentStatus.APPROVAL_NEEDED,
            status_since=base + timedelta(seconds=3),
        ),
        _state(
            key="codex:newer-wait",
            status=AgentStatus.WAITING_USER,
            status_since=base + timedelta(seconds=4),
        ),
    ]

    snapshot = derive_pet_activity(states, updated_at=base + timedelta(seconds=20))

    assert snapshot.activity == PetActivity.NEEDS_INPUT
    assert snapshot.agent_key == "codex:newer-wait"
    assert snapshot.status_since == base + timedelta(seconds=4)


def test_global_activity_is_idle_without_active_top_level_codex() -> None:
    """验证只有 idle/offline/child/non-Codex 时宠物仍保持 Idle。

    入参：无。
    返回：无；断言没有触发来源。
    错误处理：模型异常即测试失败。
    副作用：无。
    """

    now = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
    snapshot = derive_pet_activity(
        [
            _state(key="codex:idle", status=AgentStatus.IDLE, status_since=now),
            _state(
                key="codex:child-error",
                status=AgentStatus.ERROR,
                status_since=now,
                child=True,
            ),
        ],
        updated_at=now,
    )

    assert snapshot.activity == PetActivity.IDLE
    assert snapshot.agent_key is None
    assert snapshot.status_since is None
