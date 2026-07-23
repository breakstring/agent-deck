"""Remote SSH 自定义宠物受限 SFTP 镜像、缓存与 runtime 分配测试。

这些测试只使用注入的 fake SFTP 和 pytest 临时目录，不读取真实 SSH 配置、不连接远端、
不访问用户 ``.codex``，也不接触真实 N4 Pro。
"""

from __future__ import annotations

import json
import shlex
import subprocess
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from agent_deck.adapters.codex_pet import CodexPetResolver
from agent_deck.adapters.codex_remote_pet_mirror import (
    CodexRemotePetMirror,
    CodexRemotePetMirrorResolution,
    CodexRemotePetMirrorStatus,
    resolve_codex_remote_pet_cache_root,
)
from agent_deck.config import CodexPetMotion, CodexRemotePetSource
from agent_deck.core.events import AgentSource
from agent_deck.core.state import AgentState, AgentStatus
from agent_deck.server.codex_pet_runtime import CodexPetRuntime
from agent_deck.server.pets_panel_settings_store import N4ProPetsPanelSettings


def _spritesheet_payload() -> bytes:
    """创建一张符合 v1 固定几何的极小压缩透明 PNG。

    入参：无。
    返回：PNG bytes。
    错误处理：Pillow 编码异常直接使测试失败。
    副作用：只分配内存，不写文件。
    """

    image = Image.new("RGBA", (1536, 1872), (0, 0, 0, 0))
    image.putpixel((0, 0), (255, 96, 32, 255))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _manifest_payload(
    *,
    name: str = "fire-cat",
    spritesheet_path: str = "sprites/sheet.png",
) -> bytes:
    """创建远端 custom 宠物最小 manifest bytes。

    入参：name 与 spritesheet path 可覆盖安全/非法路径场景。
    返回：UTF-8 JSON bytes。
    错误处理：无。
    副作用：无。
    """

    return json.dumps(
        {
            "id": name,
            "displayName": "Fire Cat",
            "description": "synthetic remote pet",
            "spritesheetPath": spritesheet_path,
            "spriteVersionNumber": 1,
        }
    ).encode("utf-8")


class _FakeSftpRunner:
    """用内存文件表模拟只读 ``ls -ln`` 和 ``get`` batch。

    入参：``files`` 是远端相对路径到 bytes 的映射。
    返回：实例可作为 ``subprocess.run`` 兼容 callable。
    错误处理：未知路径返回非零；``fail`` 可模拟连接失败。
    副作用：get 只写 pytest staging 路径，并记录 argv/batch。
    """

    def __init__(
        self,
        files: dict[str, bytes],
        *,
        modes: dict[str, str] | None = None,
    ) -> None:
        """保存 fake 远端文件和可选文件类型。

        入参：modes 默认所有文件为 ``-rw-r--r--``，可用 symlink mode 验证拒绝逻辑。
        返回：无。
        错误处理：无。
        副作用：无。
        """

        self.files = dict(files)
        self.modes = dict(modes or {})
        self.fail = False
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        """执行一条 fake batch 并返回 CompletedProcess。

        入参：argv 与 ``input`` 由生产镜像器生成。
        返回：ls 输出标准 mode/size 行；get 成功时返回 0。
        错误处理：fail/未知命令/未知路径返回非零，不抛异常。
        副作用：get 写入 batch 指定的本地临时文件。
        """

        batch = bytes(kwargs["input"]).decode("utf-8")
        normalized_argv = tuple(argv)
        self.calls.append((normalized_argv, batch))
        if self.fail:
            return subprocess.CompletedProcess(normalized_argv, 255, b"", b"offline")
        tokens = shlex.split(batch.strip())
        if tokens[:2] == ["ls", "-ln"] and len(tokens) == 3:
            remote_path = tokens[2]
            payload = self.files.get(remote_path)
            if payload is None:
                return subprocess.CompletedProcess(normalized_argv, 1, b"", b"missing")
            mode = self.modes.get(remote_path, "-rw-r--r--")
            stdout = (
                f"{mode} 1 501 20 {len(payload)} Jan 01 00:00 file\n"
            ).encode()
            return subprocess.CompletedProcess(normalized_argv, 0, stdout, b"")
        if tokens[:1] == ["get"] and len(tokens) == 3:
            remote_path, local_path = tokens[1:]
            payload = self.files.get(remote_path)
            if payload is None:
                return subprocess.CompletedProcess(normalized_argv, 1, b"", b"missing")
            Path(local_path).write_bytes(payload)
            return subprocess.CompletedProcess(normalized_argv, 0, b"", b"")
        return subprocess.CompletedProcess(normalized_argv, 1, b"", b"unsupported")


def _remote_files(
    *,
    family: str = "pets",
    name: str = "fire-cat",
    spritesheet_path: str = "sprites/sheet.png",
) -> dict[str, bytes]:
    """构造 current/legacy 远端包的两个允许文件。

    入参：family、name 和 manifest 相对图集路径。
    返回：远端路径到 bytes 的映射。
    错误处理：无。
    副作用：创建一张内存测试图集。
    """

    manifest_filename = "pet.json" if family == "pets" else "avatar.json"
    package = f".codex/{family}/{name}"
    return {
        f"{package}/{manifest_filename}": _manifest_payload(
            name=name,
            spritesheet_path=spritesheet_path,
        ),
        f"{package}/{spritesheet_path}": _spritesheet_payload(),
    }


def test_mirror_loads_only_manifest_and_declared_spritesheet(
    tmp_path: Path,
) -> None:
    """验证 current 包成功缓存，所有 SFTP 命令均为明确只读 ls/get。

    入参：pytest 临时目录。
    返回：无；断言资产、Application Support 边界和 argv/batch。
    错误处理：任何镜像失败使测试失败。
    副作用：只写 pytest cache。
    """

    runner = _FakeSftpRunner(_remote_files())
    cache_root = tmp_path / "AgentDeck" / "remote-pets"
    mirror = CodexRemotePetMirror(
        cache_root=cache_root,
        refresh_interval_seconds=300,
        runner=runner,
    )

    resolution = mirror.resolve(
        host="minibox",
        host_id="ssh-0123456789abcdef",
        selected_avatar_id="custom:fire-cat",
        now=datetime(2026, 7, 23, tzinfo=UTC),
    )

    assert resolution.status == CodexRemotePetMirrorStatus.LOADED
    assert resolution.asset is not None
    assert resolution.asset.manifest.display_name == "Fire Cat"
    assert resolution.asset.spritesheet.size == (1536, 1872)
    assert cache_root in resolution.asset.package_dir.parents
    assert ".codex" not in str(resolution.asset.package_dir)
    assert all(call[0][0] == "sftp" for call in runner.calls)
    assert all(call[0][-1] == "minibox" for call in runner.calls)
    assert all(call[1].startswith(("ls -ln ", "get ")) for call in runner.calls)
    assert not any(
        token in call[1]
        for call in runner.calls
        for token in ("put ", "rm ", "mkdir ", "!", "cd ")
    )


def test_mirror_supports_legacy_avatar_manifest(tmp_path: Path) -> None:
    """验证 current 不存在时只读回退 legacy avatars/avatar.json。

    入参：pytest 临时目录。
    返回：无；断言最终 manifest 来自 legacy 缓存结构。
    错误处理：无。
    副作用：只写 pytest cache。
    """

    runner = _FakeSftpRunner(_remote_files(family="avatars"))
    resolution = CodexRemotePetMirror(
        cache_root=tmp_path / "cache",
        refresh_interval_seconds=0,
        runner=runner,
    ).resolve(
        host="minibox",
        host_id="ssh-0123456789abcdef",
        selected_avatar_id="custom:fire-cat",
    )

    assert resolution.status == CodexRemotePetMirrorStatus.LOADED
    assert resolution.asset is not None
    assert resolution.asset.manifest_path.name == "avatar.json"
    assert "avatars" in resolution.asset.manifest_path.parts


def test_mirror_rejects_symlink_and_manifest_path_escape(tmp_path: Path) -> None:
    """验证最终 symlink 和 manifest ``..`` 路径都不会触发图集下载。

    入参：pytest 临时目录。
    返回：无；断言 invalid 且没有可用资产。
    错误处理：无。
    副作用：只写临时 staging，结束后自动清理。
    """

    files = _remote_files()
    manifest_path = ".codex/pets/fire-cat/pet.json"
    symlink_runner = _FakeSftpRunner(
        files,
        modes={manifest_path: "lrwxr-xr-x"},
    )
    symlink_result = CodexRemotePetMirror(
        cache_root=tmp_path / "symlink-cache",
        refresh_interval_seconds=0,
        runner=symlink_runner,
    ).resolve(
        host="minibox",
        host_id="ssh-0123456789abcdef",
        selected_avatar_id="custom:fire-cat",
    )

    escape_files = _remote_files(spritesheet_path="../secret.png")
    escape_runner = _FakeSftpRunner(escape_files)
    escape_result = CodexRemotePetMirror(
        cache_root=tmp_path / "escape-cache",
        refresh_interval_seconds=0,
        runner=escape_runner,
    ).resolve(
        host="minibox",
        host_id="ssh-0123456789abcdef",
        selected_avatar_id="custom:fire-cat",
    )

    assert symlink_result.status == CodexRemotePetMirrorStatus.INVALID
    assert symlink_result.asset is None
    assert symlink_result.error == "remote_file_not_regular"
    assert escape_result.status == CodexRemotePetMirrorStatus.INVALID
    assert escape_result.asset is None
    assert escape_result.error == "spritesheet_path_invalid"
    assert not any(
        call[1].startswith("get ") and "secret.png" in call[1]
        for call in escape_runner.calls
    )


def test_mirror_uses_same_selection_lkg_but_not_another_pet(
    tmp_path: Path,
) -> None:
    """验证刷新失败只回退同 ID 缓存，选择变化不会显示旧宠物。

    入参：pytest 临时目录。
    返回：无；断言 loaded→stale，随后另一 ID 为 unavailable。
    错误处理：无。
    副作用：只写 pytest cache。
    """

    runner = _FakeSftpRunner(_remote_files())
    mirror = CodexRemotePetMirror(
        cache_root=tmp_path / "cache",
        refresh_interval_seconds=300,
        runner=runner,
    )
    first_at = datetime(2026, 7, 23, tzinfo=UTC)
    first = mirror.resolve(
        host="minibox",
        host_id="ssh-0123456789abcdef",
        selected_avatar_id="custom:fire-cat",
        now=first_at,
    )
    call_count = len(runner.calls)
    cached = mirror.resolve(
        host="minibox",
        host_id="ssh-0123456789abcdef",
        selected_avatar_id="custom:fire-cat",
        now=first_at + timedelta(seconds=30),
    )
    runner.fail = True
    stale = mirror.resolve(
        host="minibox",
        host_id="ssh-0123456789abcdef",
        selected_avatar_id="custom:fire-cat",
        now=first_at + timedelta(seconds=301),
    )
    changed = mirror.resolve(
        host="minibox",
        host_id="ssh-0123456789abcdef",
        selected_avatar_id="custom:other",
        now=first_at + timedelta(seconds=302),
    )

    assert first.status == CodexRemotePetMirrorStatus.LOADED
    assert cached is first
    assert len(runner.calls) > call_count
    assert stale.status == CodexRemotePetMirrorStatus.STALE
    assert stale.asset is first.asset
    assert changed.status == CodexRemotePetMirrorStatus.UNAVAILABLE
    assert changed.asset is None


def test_runtime_uses_matching_remote_custom_mirror(tmp_path: Path) -> None:
    """验证 remote_config 只采用 host 与选择 ID 都匹配的镜像资产。

    入参：pytest 临时目录。
    返回：无；断言角色分配标记为 custom loaded 且可渲染。
    错误处理：无。
    副作用：只创建合成缓存和内存面板，不访问硬件。
    """

    runner = _FakeSftpRunner(_remote_files())
    selected_id = "custom:fire-cat"
    mirror_resolution = CodexRemotePetMirror(
        cache_root=tmp_path / "remote-cache",
        refresh_interval_seconds=0,
        runner=runner,
    ).resolve(
        host="minibox",
        host_id="ssh-test",
        selected_avatar_id=selected_id,
    )
    runtime = CodexPetRuntime(
        enabled=True,
        panel_fps=8,
        motion=CodexPetMotion.FULL,
        cache_root=tmp_path / "render-cache",
        fallback_key_path=None,
        panel_settings=N4ProPetsPanelSettings(
            remote_pet_source=CodexRemotePetSource.REMOTE_CONFIG
        ),
        resolver=CodexPetResolver(
            environment={"CODEX_HOME": str(tmp_path / "missing-codex")},
            home_dir=tmp_path,
        ),
        started_at_monotonic=100.0,
    )
    now = datetime.now(UTC)
    runtime.refresh(now=now)
    runtime.update_remote_pet_selection(
        "ssh-test",
        selected_avatar_id=selected_id,
        config_available=True,
    )
    runtime.update_remote_custom_pet_resolution("ssh-test", mirror_resolution)
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

    diagnostics = runtime.diagnostics()["panel_colony"]
    revision, image = runtime.panel_background(monotonic_seconds=101.0)

    assert revision == 1
    assert image is not None
    assert diagnostics["renderable_actor_count"] == 1
    assert diagnostics["remote_custom_asset_count"] == 1
    assert diagnostics["assignments"]["codex:remote"] == (
        "remote_config:custom:loaded"
    )


def test_default_remote_cache_never_uses_codex_home() -> None:
    """验证生产缺省缓存固定属于 Agent Deck Application Support。

    入参：无。
    返回：无；断言目录名和父级，不要求目录已经存在。
    错误处理：无。
    副作用：无。
    """

    resolved = resolve_codex_remote_pet_cache_root()

    assert resolved.name == "remote-pets"
    assert resolved.parent.name == "AgentDeck"
    assert ".codex" not in resolved.parts
