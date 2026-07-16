"""键盘快捷键模型、macOS 投递、调度、图标与 daemon 闭环测试。

本文件使用 fake native bridge 和临时图标目录，不向真实前台应用发送按键、不触发 macOS
权限弹窗、不访问真实 N4 Pro 或网络。
"""

from __future__ import annotations

import base64
import json
import time
from io import BytesIO
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
import agent_deck.server.app as server_app
import agent_deck.actions.macos_keyboard as macos_keyboard
import agent_deck.rendering.shortcut_key as shortcut_key
from fastapi.testclient import TestClient
from PIL import Image, ImageFont
from pydantic import ValidationError

from agent_deck.actions.key_icon_store import ShortcutIconStore
from agent_deck.actions.keyboard import (
    KeyboardShortcutCapability,
    KeyboardShortcutRunResult,
    KeyboardShortcutRunStatus,
    KeyboardShortcutScheduler,
    KeyboardShortcutSpec,
    ShortcutIconSpec,
)
from agent_deck.actions.macos_keyboard import MacOSKeyboardShortcutExecutor
from agent_deck.core.modes import DeckMode, DeckSelection
from agent_deck.hardware.fake import HardwareInput
from agent_deck.input.interactions import interaction_intent_from_hardware_input
from agent_deck.rendering.key_surface import (
    KeySurfaceKind,
    N4ProKeyBinding,
    N4ProKeyLayout,
    default_n4pro_key_layout,
)
from agent_deck.rendering.layout import build_layout_plan
from agent_deck.rendering.shortcut_key import (
    ShortcutKeyImageCache,
    render_shortcut_key_image,
)
from agent_deck.server.app import create_app
from agent_deck.server.key_layout_store import (
    KeyLayoutStoreError,
    load_n4pro_key_layout,
    save_n4pro_key_layout,
)


def _shortcut(*steps: dict[str, object]) -> KeyboardShortcutSpec:
    """从紧凑测试字典构造强类型快捷键规格。

    入参：一个或多个步骤字典。
    返回：``KeyboardShortcutSpec``。
    错误处理：非法步骤由 Pydantic 交给 pytest。
    副作用：无。
    """

    return KeyboardShortcutSpec(steps=steps)


def test_shortcut_model_normalizes_modifiers_and_rejects_unsafe_shapes() -> None:
    """模型应规范修饰键，并拒绝未知键、末步延迟和超时序列。

    入参：无；测试内构造合法和非法 JSON-like 数据。
    返回：无返回值；断言通过代表 API/persistence 共用校验边界稳定。
    错误处理：非法数据未被拒绝时由 pytest 报告。
    副作用：无。
    """

    shortcut = _shortcut(
        {
            "key": "KeyP",
            "modifiers": ["shift", "command"],
            "delay_after_ms": 0,
        }
    )

    assert shortcut.model_dump(mode="json") == {
        "steps": [
            {
                "key": "KeyP",
                "modifiers": ["command", "shift"],
                "delay_after_ms": 0,
            }
        ]
    }
    with pytest.raises(ValidationError, match="unsupported keyboard code"):
        _shortcut({"key": "MediaPlayPause"})
    with pytest.raises(ValidationError, match="last keyboard shortcut step"):
        _shortcut({"key": "KeyA", "delay_after_ms": 10})
    with pytest.raises(ValidationError, match="duration exceeds"):
        _shortcut(
            {"key": "KeyA", "delay_after_ms": 2_000},
            {"key": "KeyB", "delay_after_ms": 2_000},
            {"key": "KeyC", "delay_after_ms": 2_000},
            {"key": "KeyD", "delay_after_ms": 2_000},
            {"key": "KeyE", "delay_after_ms": 2_000},
            {"key": "KeyF", "delay_after_ms": 0},
        )
    with pytest.raises(ValidationError, match="requires a key or modifier"):
        _shortcut({"key": None, "modifiers": []})


def test_shortcut_binding_projects_typed_intent_without_string_payload() -> None:
    """快捷键配置应以强类型字段贯穿 layout 和 hardware intent。

    入参：无；Key 1 配置为 Command+Shift+P。
    返回：无返回值；断言通过代表快捷键没有被塞入通用字符串 payload。
    错误处理：投影或路由丢字段时由 pytest 报告。
    副作用：只创建内存布局和 fake input。
    """

    shortcut = _shortcut({"key": "KeyP", "modifiers": ["command", "shift"]})
    layout = N4ProKeyLayout(
        keys=(
            N4ProKeyBinding(
                index=0,
                kind=KeySurfaceKind.KEYBOARD_SHORTCUT,
                label="命令面板",
                shortcut=shortcut,
                icon=ShortcutIconSpec(),
            ),
            *default_n4pro_key_layout().sorted_keys()[1:],
        )
    )
    plan = build_layout_plan(
        [],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
        key_layout=layout,
    )
    event = HardwareInput.model_validate(
        {
            "kind": "key",
            "index": 0,
            "value": {"state": 1},
            "occurred_at": "2026-07-16T10:00:00+08:00",
        }
    )

    intent = interaction_intent_from_hardware_input(event, plan)

    assert plan.keys[0].intent == "send_keyboard_shortcut"
    assert plan.keys[0].payload == {}
    assert plan.keys[0].shortcut == shortcut
    assert intent is not None
    assert intent.dry_run is False
    assert intent.shortcut == shortcut
    assert intent.payload == {}


class _FakeMacBridge:
    """记录 macOS executor 投递顺序的 fake native bridge。

    入参：权限、固定 PID 和可选首次失败的 key code。
    返回：实现 native bridge protocol 的测试对象。
    错误处理：命中 fail key 时只抛一次 RuntimeError。
    副作用：把事件和调用计数写入内存列表。
    """

    def __init__(
        self,
        *,
        permission: bool = True,
        pid: int | None = 321,
        fail_key_code: int | None = None,
    ) -> None:
        """初始化 fake bridge 状态。

        入参：preflight 结果、frontmost PID 和失败 key code。
        返回：无显式返回值。
        错误处理：无。
        副作用：无。
        """

        self.permission = permission
        self.pid = pid
        self.fail_key_code = fail_key_code
        self.failed_once = False
        self.frontmost_calls = 0
        self.request_calls = 0
        self.events: list[tuple[int, int, bool, int]] = []

    def preflight_event_access(self) -> bool:
        """返回配置的权限状态。"""

        return self.permission

    def request_event_access(self) -> bool:
        """记录显式权限请求并把 fake 权限设为允许。"""

        self.request_calls += 1
        self.permission = True
        return True

    def frontmost_application_pid(self) -> int | None:
        """记录查询次数并返回配置 PID。"""

        self.frontmost_calls += 1
        return self.pid

    def post_key_event(
        self,
        *,
        pid: int,
        key_code: int,
        is_down: bool,
        flags: int,
    ) -> None:
        """记录事件；可在指定 key 首次 key-down 时模拟 native 失败。"""

        if (
            is_down
            and key_code == self.fail_key_code
            and not self.failed_once
        ):
            self.failed_once = True
            raise RuntimeError("synthetic post failure")
        self.events.append((pid, key_code, is_down, flags))


def test_macos_executor_pins_pid_and_posts_exact_chord_order() -> None:
    """macOS executor 应只取一次前台 PID，并按 down/up 顺序投递组合键。

    入参：无；fake bridge 执行 Command+Shift+P 后再执行 A。
    返回：无返回值；断言通过代表整条序列固定同一 PID，且修饰键正确释放。
    错误处理：事件顺序或 flags 错误时由 pytest 报告。
    副作用：只写 fake bridge 事件列表，不向系统投递。
    """

    bridge = _FakeMacBridge()
    sleeps: list[float] = []
    executor = MacOSKeyboardShortcutExecutor(bridge, sleep=sleeps.append)
    shortcut = _shortcut(
        {
            "key": "KeyP",
            "modifiers": ["command", "shift"],
            "delay_after_ms": 100,
        },
        {"key": "KeyA", "delay_after_ms": 0},
    )

    result = executor.execute(shortcut)

    assert result.status == KeyboardShortcutRunStatus.SUCCEEDED
    assert result.target_pid == 321
    assert bridge.frontmost_calls == 1
    assert bridge.events[:6] == [
        (321, 55, True, 0x0010_0000),
        (321, 56, True, 0x0012_0000),
        (321, 35, True, 0x0012_0000),
        (321, 35, False, 0x0012_0000),
        (321, 56, False, 0x0010_0000),
        (321, 55, False, 0),
    ]
    assert bridge.events[6:] == [
        (321, 0, True, 0),
        (321, 0, False, 0),
    ]
    assert sleeps == [0.02, 0.1, 0.02]


def test_macos_capability_identifies_development_permission_requester(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS capability 应解释当前权限请求进程，而不是暗示浏览器需要授权。

    入参：``monkeypatch`` 把 executable 固定为开发态 Python 路径。
    返回：无返回值；断言通过代表 UI 能区分开发运行时和未来稳定 App bundle。
    错误处理：请求进程缺失或被误标为稳定身份时由 pytest 报告。
    副作用：只在测试期间替换模块读取到的 ``sys.executable``。
    """

    monkeypatch.setattr(
        macos_keyboard.sys,
        "executable",
        "/opt/agent-deck-runtime/bin/python3.12",
    )

    capability = MacOSKeyboardShortcutExecutor(
        _FakeMacBridge(),
        sleep=lambda _seconds: None,
    ).capability()

    assert capability.can_open_system_settings is True
    assert capability.permission_requester is not None
    assert capability.permission_requester.display_name == "python3.12"
    assert capability.permission_requester.stable_identity is False
    assert "Codex、Terminal 或 Python" in capability.permission_requester.note


def test_macos_executor_fails_closed_and_releases_pressed_modifiers() -> None:
    """缺权限不得查询目标；中途失败必须 best-effort 释放已按下修饰键。

    入参：无；分别用未授权 bridge 和主键 down 失败 bridge。
    返回：无返回值；断言通过代表权限 fail-closed 和 finally 清理成立。
    错误处理：仍投递事件或遗漏 key-up 时由 pytest 报告。
    副作用：只使用 fake bridge。
    """

    denied = _FakeMacBridge(permission=False)
    denied_result = MacOSKeyboardShortcutExecutor(
        denied,
        sleep=lambda _seconds: None,
    ).execute(_shortcut({"key": "KeyA"}))
    assert denied_result.status == KeyboardShortcutRunStatus.PERMISSION_REQUIRED
    assert denied.frontmost_calls == 0
    assert denied.events == []

    failing = _FakeMacBridge(fail_key_code=35)
    failed_result = MacOSKeyboardShortcutExecutor(
        failing,
        sleep=lambda _seconds: None,
    ).execute(
        _shortcut({"key": "KeyP", "modifiers": ["command", "shift"]})
    )
    assert failed_result.status == KeyboardShortcutRunStatus.FAILED
    assert failing.events == [
        (321, 55, True, 0x0010_0000),
        (321, 56, True, 0x0012_0000),
        (321, 56, False, 0x0010_0000),
        (321, 55, False, 0),
    ]


class _BlockingExecutor:
    """让第一个 scheduler job 保持运行的 fake executor。

    入参：无；通过 ``release`` Event 控制完成。
    返回：实现跨平台 executor 协议的测试对象。
    错误处理：等待超过 2 秒仍返回 failed，避免测试永久挂起。
    副作用：设置 started Event 并等待 release。
    """

    def __init__(self) -> None:
        """创建 started/release 同步事件。"""

        self.started = Event()
        self.release = Event()

    def capability(self) -> KeyboardShortcutCapability:
        """返回已授权 fake capability。"""

        return KeyboardShortcutCapability(
            platform="test",
            supported=True,
            permission_granted=True,
            can_request_permission=True,
            message="ready",
        )

    def request_permission(self) -> KeyboardShortcutCapability:
        """返回 unchanged fake capability。"""

        return self.capability()

    def execute(self, shortcut: KeyboardShortcutSpec) -> KeyboardShortcutRunResult:
        """阻塞直到测试释放，并返回投递成功或等待超时。"""

        del shortcut
        self.started.set()
        if not self.release.wait(timeout=2):
            return KeyboardShortcutRunResult(
                status=KeyboardShortcutRunStatus.FAILED,
                message="test release timed out",
            )
        return KeyboardShortcutRunResult(
            status=KeyboardShortcutRunStatus.SUCCEEDED,
            target_pid=99,
            message="posted",
        )


def test_scheduler_has_one_worker_and_no_waiting_queue() -> None:
    """已有快捷键执行中时，第二次按键应立即 busy 而不排队。

    入参：无；第一个 fake job 被 Event 阻塞。
    返回：无返回值；断言通过代表零队列策略和 recent 终态诊断有效。
    错误处理：第二个任务被接受或第一个未归档时由 pytest 报告。
    副作用：短暂启动一个测试 worker 线程并在 finally 关闭。
    """

    executor = _BlockingExecutor()
    scheduler = KeyboardShortcutScheduler(executor)
    try:
        first = scheduler.submit(
            _shortcut({"key": "KeyA"}),
            source="test",
            key_index=0,
        )
        assert first.accepted is True
        assert executor.started.wait(timeout=1)

        second = scheduler.submit(
            _shortcut({"key": "KeyB"}),
            source="test",
            key_index=1,
        )
        assert second.accepted is False
        assert second.status.value == "busy"

        executor.release.set()
        deadline = time.monotonic() + 1
        diagnostics = scheduler.diagnostics()
        while diagnostics["active"] is not None and time.monotonic() < deadline:
            time.sleep(0.01)
            diagnostics = scheduler.diagnostics()
        assert diagnostics["active"] is None
        assert len(diagnostics["recent"]) == 1
        assert diagnostics["recent"][0].status.value == "succeeded"  # type: ignore[index,union-attr]
    finally:
        executor.release.set()
        scheduler.close()


def _png_bytes(size: tuple[int, int] = (48, 32)) -> bytes:
    """创建测试上传使用的内存 PNG。

    入参：图片尺寸。
    返回：PNG bytes。
    错误处理：Pillow 编码错误交给 pytest。
    副作用：只写内存 buffer。
    """

    buffer = BytesIO()
    Image.new("RGBA", size, (38, 130, 215, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_shortcut_icon_store_is_content_addressed_and_renderer_has_auto_fallback(
    tmp_path: Path,
) -> None:
    """上传图标应按规范化内容去重，并同时支持自定义和自动 112px 图。

    入参：``tmp_path`` 提供隔离资产目录。
    返回：无返回值；断言通过代表 hash、派生文件、内存缓存和自动 renderer 可用。
    错误处理：资产缺失、尺寸错误或坏图未拒绝时由 pytest 报告。
    副作用：只写 pytest 临时目录和内存图片。
    """

    store = ShortcutIconStore(tmp_path / "shortcut-icons")
    first = store.store(_png_bytes(), filename="first.png")
    second = store.store(_png_bytes(), filename="second.png")

    assert first.asset_id == second.asset_id
    assert first.preview_url.endswith("/preview-96.png")
    assert (store.root / first.asset_id / "preview-96.png").is_file()
    assert (store.root / first.asset_id / "key-112.png").is_file()
    custom = store.key_image(first.asset_id)
    assert custom is not None
    assert custom.size == (112, 112)
    assert store.key_image(first.asset_id) is custom

    shortcut = _shortcut({"key": "KeyP", "modifiers": ["command", "shift"]})
    auto = render_shortcut_key_image(shortcut)
    cache = ShortcutKeyImageCache()
    assert auto.size == (112, 112)
    assert auto.mode == "RGB"
    assert cache.image(shortcut) is cache.image(shortcut)
    with pytest.raises(ValueError, match="cannot be decoded"):
        store.store(b"not-an-image")

    missing_custom_layout = N4ProKeyLayout(
        keys=(
            N4ProKeyBinding(
                index=0,
                kind=KeySurfaceKind.KEYBOARD_SHORTCUT,
                shortcut=shortcut,
                icon=ShortcutIconSpec(mode="custom", asset_id="0" * 64),
            ),
            *default_n4pro_key_layout().sorted_keys()[1:],
        )
    )
    projected = build_layout_plan(
        [],
        [],
        DeckSelection(mode=DeckMode.OVERVIEW),
        key_layout=missing_custom_layout,
    )
    fallback_images = server_app._key_images_from_layout(
        projected,
        shortcut_icon_store=store,
        shortcut_key_cache=cache,
    )
    assert fallback_images[1] is cache.image(shortcut)


def test_shortcut_renderer_prefers_font_with_readable_return_glyph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动图标应优先使用能正确绘制 hooked-return 的 Unicode 字体。

    入参：``monkeypatch`` 记录 renderer 尝试加载的首个系统字体。
    返回：无返回值；断言通过代表不会再次优先采用把 ``↩`` 画成横线的 SFNS。
    错误处理：字体优先级回退时由 pytest 报告。
    副作用：只在测试期间替换 Pillow 的字体加载函数。
    """

    loaded_paths: list[str] = []
    fallback_font = ImageFont.load_default()

    def load_font(path: str, _size: int) -> ImageFont.ImageFont:
        """记录首个候选路径并返回不访问系统文件的测试字体。"""

        loaded_paths.append(path)
        return fallback_font

    monkeypatch.setattr(shortcut_key.ImageFont, "truetype", load_font)

    assert shortcut_key._font(24) is fallback_font
    assert loaded_paths == [
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
    ]


class _RecordingExecutor:
    """供 daemon API 测试使用的立即完成快捷键 executor。

    入参：初始权限状态。
    返回：记录 shortcut 和权限请求次数的 fake executor。
    错误处理：无业务异常。
    副作用：只写内存列表和计数。
    """

    def __init__(self, *, permission: bool) -> None:
        """初始化权限和诊断容器。"""

        self.permission = permission
        self.request_calls = 0
        self.shortcuts: list[KeyboardShortcutSpec] = []

    def capability(self) -> KeyboardShortcutCapability:
        """返回当前 fake 权限状态。"""

        return KeyboardShortcutCapability(
            platform="test",
            supported=True,
            permission_granted=self.permission,
            can_request_permission=True,
            message="ready" if self.permission else "permission required",
        )

    def request_permission(self) -> KeyboardShortcutCapability:
        """记录显式请求并授予 fake 权限。"""

        self.request_calls += 1
        self.permission = True
        return self.capability()

    def execute(self, shortcut: KeyboardShortcutSpec) -> KeyboardShortcutRunResult:
        """记录规格并立即返回已投递。"""

        self.shortcuts.append(shortcut)
        return KeyboardShortcutRunResult(
            status=KeyboardShortcutRunStatus.SUCCEEDED,
            target_pid=777,
            message="posted by test executor",
        )


def _shortcut_layout_body() -> dict[str, object]:
    """构造 Key 1 为 Command+Shift+P 的完整 API layout body。

    入参：无。
    返回：JSON-safe 完整 10 键布局。
    错误处理：内置默认布局异常交给 pytest。
    副作用：无。
    """

    body = default_n4pro_key_layout().model_dump(mode="json")
    body["keys"][0] = {  # type: ignore[index]
        "index": 0,
        "kind": "keyboard_shortcut",
        "label": "命令面板",
        "shortcut": {
            "steps": [
                {
                    "key": "KeyP",
                    "modifiers": ["command", "shift"],
                    "delay_after_ms": 0,
                }
            ]
        },
        "icon": {"mode": "auto", "asset_id": None},
    }
    return body


def test_daemon_api_exposes_permission_and_runs_hardware_shortcut(
    tmp_path: Path,
) -> None:
    """daemon 应显式请求权限，并把物理按键无阻塞提交到 executor。

    入参：``tmp_path`` 隔离图标存储。
    返回：无返回值；断言通过代表 capability、保存、input、job status 全链路成立。
    错误处理：HTTP 状态或后台任务未完成时由 pytest 报告。
    副作用：只启动 TestClient lifespan 和一个短生命周期 worker，不发送系统按键。
    """

    executor = _RecordingExecutor(permission=False)
    settings_opener = Mock()
    app = create_app(
        keyboard_shortcut_executor=executor,
        keyboard_accessibility_settings_opener=settings_opener,
        shortcut_icon_store_path=tmp_path / "icons",
    )
    with TestClient(app) as client:
        capabilities = client.get("/ui/control-capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["keyboard_shortcuts"]["permission_granted"] is False

        requested = client.post("/ui/keyboard-shortcuts/request-permission")
        assert requested.status_code == 200
        assert requested.json()["permission_granted"] is True
        assert executor.request_calls == 1

        opened = client.post(
            "/ui/keyboard-shortcuts/open-accessibility-settings"
        )
        assert opened.status_code == 200
        assert opened.json() == {"opened": True}
        settings_opener.assert_called_once_with()

        saved = client.put("/ui/key-layout", json=_shortcut_layout_body())
        assert saved.status_code == 200
        assert saved.json()["layout"]["keys"][0]["shortcut"]["steps"][0]["key"] == "KeyP"

        pressed = client.post(
            "/hardware/input",
            json={
                "kind": "key",
                "index": 0,
                "value": {"state": 1},
                "occurred_at": "2026-07-16T10:00:00+08:00",
            },
        )
        assert pressed.status_code == 200
        assert pressed.json()["action"]["status"] == "accepted"

        deadline = time.monotonic() + 1
        status = client.get("/status").json()
        while not status["keyboard_shortcuts"]["recent"] and time.monotonic() < deadline:
            time.sleep(0.01)
            status = client.get("/status").json()
        assert len(executor.shortcuts) == 1
        assert status["keyboard_shortcuts"]["recent"][0]["status"] == "succeeded"
        assert status["keyboard_shortcuts"]["recent"][0]["target_pid"] == 777


def test_shortcut_icon_upload_api_returns_content_addressed_assets(
    tmp_path: Path,
) -> None:
    """快捷键图标 API 应验证、持久化并只返回白名单 PNG。

    入参：``tmp_path`` 隔离 store。
    返回：无返回值；断言通过代表上传响应和文件路由可直接供 GUI 使用。
    错误处理：状态、asset id 或内容类型不符时由 pytest 报告。
    副作用：只写 pytest 临时目录。
    """

    data_url = "data:image/png;base64," + base64.b64encode(_png_bytes()).decode()
    with TestClient(
        create_app(shortcut_icon_store_path=tmp_path / "icons")
    ) as client:
        response = client.post(
            "/ui/shortcut-icons/upload",
            json={"filename": "shortcut.png", "data_url": data_url},
        )
        assert response.status_code == 200
        asset = response.json()
        assert len(asset["asset_id"]) == 64
        preview = client.get(asset["preview_url"])
        key_image = client.get(asset["key_icon_url"])
        assert preview.status_code == 200
        assert key_image.status_code == 200
        assert preview.headers["content-type"] == "image/png"
        assert client.get(
            f"/ui/shortcut-icons/{asset['asset_id']}/normalized.png"
        ).status_code == 404


def test_shortcut_auto_preview_api_uses_hardware_renderer(
    tmp_path: Path,
) -> None:
    """Web 自动预览应直接返回与硬件下发相同 renderer 的 PNG。

    入参：``tmp_path`` 隔离快捷键资产目录。
    返回：无返回值；断言通过代表预览像素与 N4 Pro 自动图标像素完全一致。
    错误处理：非法或空序列必须返回 422，不能生成误导预览。
    副作用：只启动 TestClient lifespan，不访问真实硬件。
    """

    shortcut = _shortcut({"key": "Enter", "modifiers": ["shift"]})
    with TestClient(
        create_app(shortcut_icon_store_path=tmp_path / "icons")
    ) as client:
        response = client.get(
            "/ui/shortcut-icons/auto-preview.png",
            params={"spec": json.dumps(shortcut.model_dump(mode="json"))},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        with Image.open(BytesIO(response.content)) as preview:
            assert preview.convert("RGB").tobytes() == render_shortcut_key_image(
                shortcut
            ).tobytes()

        invalid = client.get(
            "/ui/shortcut-icons/auto-preview.png",
            params={"spec": json.dumps({"steps": []})},
        )
        assert invalid.status_code == 422


def test_key_layout_store_migrates_v1_and_rejects_unknown_future_version(
    tmp_path: Path,
) -> None:
    """v1 布局应原样读入，保存升级 v2，未知未来版本必须 fail-closed。

    入参：``tmp_path`` 提供隔离 JSON 文件。
    返回：无返回值；断言通过代表兼容旧配置且不会误读未来 schema。
    错误处理：版本边界不符合预期时由 pytest 报告。
    副作用：只写 pytest 临时 JSON。
    """

    path = tmp_path / "n4pro-key-layout.json"
    default_layout = default_n4pro_key_layout()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "device_profile": "mirabox.n4pro",
                "layout": default_layout.model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )
    assert load_n4pro_key_layout(path) == default_layout

    save_n4pro_key_layout(default_layout, path)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2

    future = json.loads(path.read_text(encoding="utf-8"))
    future["version"] = 3
    path.write_text(json.dumps(future), encoding="utf-8")
    with pytest.raises(KeyLayoutStoreError, match="version 不支持"):
        load_n4pro_key_layout(path)
