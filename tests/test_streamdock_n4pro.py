"""StreamDock N4 Pro 多 surface 统一 writer 的单元测试。

这些测试只使用 fake manager/device 验证调用顺序、临时图片处理和错误路径，不访问真实
N4 Pro、不加载官方 SDK、不启动 daemon，也不写用户配置。真实硬件 smoke 只能作为显式手动步骤。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from agent_deck.hardware.streamdock_n4pro import (
    StreamDockN4ProPersistentAnimator,
    animate_key_images_on_n4pro,
    render_images_to_n4pro,
)


class FakeN4ProUnifiedDevice:
    """测试用 N4 Pro unified device fake。

    入参：`path` 是设备路径；`background_result` 是 `set_frame_background` 返回值；
    `key_result` 是 `set_key_image` 返回值。
    返回：提供 unified writer 需要的最小 device 接口。
    错误处理：本 fake 默认不抛异常。
    副作用：把方法调用记录到 `calls`，不访问硬件。
    """

    def __init__(
        self,
        path: str = "n4pro-path",
        background_result: object = None,
        key_result: object = 0,
        refresh_result: object = None,
        writable: bool = True,
    ) -> None:
        """初始化 fake 设备。

        入参：`path` 是测试设备路径；`background_result`、`key_result` 和 `refresh_result`
        是模拟 SDK 返回值；`writable` 表示当前句柄是否仍可写。
        返回：无返回值。
        错误处理：无。
        副作用：初始化内存调用记录。
        """

        self.path = path
        self.background_result = background_result
        self.key_result = key_result
        self.refresh_result = refresh_result
        self.writable = writable
        self.calls: list[tuple[str, object | None]] = []
        self.paths_seen: list[Path] = []
        self.key_callback: object | None = None
        self.touch_bar_callback: object | None = None

    def open(self) -> bool:
        """记录 open 调用并模拟成功。

        入参：无。
        返回：True 表示设备已打开。
        错误处理：无。
        副作用：追加调用记录。
        """

        self.calls.append(("open", None))
        return True

    def init(self) -> None:
        """记录 init 调用。

        入参：无。
        返回：无返回值。
        错误处理：无。
        副作用：追加调用记录；真实 SDK 会接管显示，本 fake 不执行。
        """

        self.calls.append(("init", None))

    def set_frame_background(self, path: str) -> object:
        """记录 frame background 路径并模拟 SDK 下发。

        入参：`path` 是 writer 保存的临时 JPEG 路径。
        返回：构造时传入的 `background_result`。
        错误处理：无。
        副作用：记录路径并断言文件在 SDK 调用期间存在。
        """

        self.calls.append(("set_frame_background", None))
        path_obj = Path(path)
        self.paths_seen.append(path_obj)
        assert path_obj.is_file()
        return self.background_result

    def set_key_image(self, key: int, path: str) -> object:
        """记录按键图片路径并模拟 SDK 下发。

        入参：`key` 是逻辑按键编号；`path` 是 writer 保存的临时 PNG 路径。
        返回：构造时传入的 `key_result`。
        错误处理：无。
        副作用：记录 key 和路径，并断言文件在 SDK 调用期间存在。
        """

        self.calls.append(("set_key_image", key))
        path_obj = Path(path)
        self.paths_seen.append(path_obj)
        assert path_obj.is_file()
        return self.key_result

    def refresh(self) -> object:
        """记录 refresh 调用并返回可配置的 SDK 结果。

        入参：无。
        返回：构造时传入的 `refresh_result`。
        错误处理：无。
        副作用：追加调用记录。
        """

        self.calls.append(("refresh", None))
        return self.refresh_result

    def can_write(self) -> bool:
        """返回 fake HID 句柄当前是否可写。

        入参：无。
        返回：构造或测试过程设置的 `writable`。
        错误处理：无。
        副作用：无；不追加调用记录，避免改变现有顺序断言。
        """

        return self.writable

    def close(self, notify: bool = True) -> None:
        """记录 close 调用。

        入参：`notify` 是 SDK close 参数。
        返回：无返回值。
        错误处理：无。
        副作用：追加调用记录。
        """

        self.calls.append(("close", notify))

    def getPath(self) -> str:
        """返回 fake 设备路径。

        入参：无。
        返回：构造时传入的路径。
        错误处理：无。
        副作用：无。
        """

        return self.path

    def set_key_callback(self, callback: object) -> None:
        """记录 key/knob 输入回调。

        入参：`callback` 是 SDK key callback。
        返回：无返回值。
        错误处理：无。
        副作用：保存 callback 并追加调用记录。
        """

        self.key_callback = callback
        self.calls.append(("set_key_callback", None))

    def set_touch_bar_callback(self, callback: object) -> None:
        """记录 touch bar 输入回调。

        入参：`callback` 是 SDK touch bar callback。
        返回：无返回值。
        错误处理：无。
        副作用：保存 callback 并追加调用记录。
        """

        self.touch_bar_callback = callback
        self.calls.append(("set_touch_bar_callback", None))

    def set_brightness(self, percent: int) -> int:
        """记录控制台整体亮度写入。

        入参：`percent` 是 0 到 100 亮度。
        返回：0 表示 fake SDK 成功。
        错误处理：无。
        副作用：记录调用。
        """

        self.calls.append(("set_brightness", percent))
        return 0

    def set_led_color(self, red: int, green: int, blue: int) -> int:
        """记录 N4 Pro RGB 灯圈组颜色写入。

        入参：`red`、`green`、`blue` 是 0 到 255 色值。
        返回：0 表示 fake SDK 成功。
        错误处理：无。
        副作用：记录调用。
        """

        self.calls.append(("set_led_color", (red, green, blue)))
        return 0

    def set_led_brightness(self, percent: int) -> int:
        """记录 N4 Pro RGB 灯圈组亮度写入。

        入参：`percent` 是 0 到 100 的 group LED 亮度。
        返回：0 表示 fake SDK 成功。
        错误处理：无。
        副作用：记录调用。
        """

        self.calls.append(("set_led_brightness", percent))
        return 0


class FakeOtherUnifiedDevice(FakeN4ProUnifiedDevice):
    """测试用非 N4 Pro 设备 fake。

    入参：继承自 `FakeN4ProUnifiedDevice`。
    返回：类名不包含 N4Pro，因此 writer 应跳过该设备。
    错误处理：无。
    副作用：仅记录调用。
    """


class FakeHandshakeN4ProUnifiedDevice(FakeN4ProUnifiedDevice):
    """提供可配置握手结果的 N4 Pro fake。

    入参：继承 unified fake 参数，并接受 `handshake_result` 或 `handshake_error`。
    返回：可验证握手、open、init 调用顺序的 fake device。
    错误处理：配置异常时 `send_handshake()` 原样抛出。
    副作用：只追加内存调用记录，不访问真实 HID。
    """

    def __init__(
        self,
        *,
        handshake_result: object = 0,
        handshake_error: Exception | None = None,
        **kwargs: object,
    ) -> None:
        """初始化带握手能力的 fake。

        入参：`handshake_result` 是模拟 native 返回值；`handshake_error` 是可选异常；
        `kwargs` 传给基础 fake。
        返回：无返回值。
        错误处理：构造阶段不抛配置异常。
        副作用：初始化内存调用记录。
        """

        super().__init__(**kwargs)
        self.handshake_result = handshake_result
        self.handshake_error = handshake_error

    def send_handshake(self) -> object:
        """记录握手并返回配置结果或抛出配置异常。

        入参：无。
        返回：构造时配置的 `handshake_result`。
        错误处理：配置了 `handshake_error` 时原样抛出。
        副作用：追加握手调用记录。
        """

        self.calls.append(("send_handshake", None))
        if self.handshake_error is not None:
            raise self.handshake_error
        return self.handshake_result


class FakeUnifiedManager:
    """测试用 StreamDock manager fake。

    入参：`devices` 是 enumerate 返回的设备列表。
    返回：提供 `enumerate()` 接口。
    错误处理：无。
    副作用：记录枚举次数。
    """

    def __init__(self, devices: list[object]) -> None:
        """初始化 fake manager。

        入参：`devices` 是待返回设备。
        返回：无返回值。
        错误处理：无。
        副作用：初始化内存字段。
        """

        self.devices = devices
        self.enumerate_count = 0

    def enumerate(self) -> list[object]:
        """返回 fake 设备列表。

        入参：无。
        返回：构造时传入的设备列表。
        错误处理：无。
        副作用：递增枚举计数。
        """

        self.enumerate_count += 1
        return self.devices


def test_render_images_to_n4pro_writes_background_and_keys_once(
    tmp_path: Path,
) -> None:
    """验证 unified writer 在一次会话里写背景和按键。

    入参：`tmp_path` 提供临时图片目录。
    返回：无返回值；断言通过表示 open/init 只发生一次，frame background 先于 key 写入。
    错误处理：调用顺序、返回值或临时文件清理不符合预期时由 pytest 报告。
    副作用：在 pytest 临时目录短暂写入 JPEG/PNG，随后清理。
    """

    device = FakeN4ProUnifiedDevice(background_result=None, key_result=0)
    manager = FakeUnifiedManager([device])
    background = Image.new("RGB", (800, 480), (1, 2, 3))
    key_image = Image.new("RGB", (112, 112), (4, 5, 6))

    result = render_images_to_n4pro(
        background_image=background,
        key_images={1: key_image},
        manager=manager,
        temp_dir=tmp_path,
    )

    assert result.ok is True
    assert result.device_type == "FakeN4ProUnifiedDevice"
    assert result.path == "n4pro-path"
    assert result.background_result is None
    assert result.key_results == {1: "0"}
    assert result.refresh_result == "None"
    assert [name for name, _ in device.calls] == [
        "open",
        "init",
        "set_frame_background",
        "set_key_image",
        "refresh",
        "close",
    ]
    assert device.calls[3] == ("set_key_image", 1)
    assert device.calls[-1] == ("close", False)
    assert device.paths_seen
    assert all(not path.exists() for path in device.paths_seen)


def test_persistent_animator_runs_session_output_once_per_render_without_reopening(
    tmp_path: Path,
) -> None:
    """长连接 animator 应在同一设备会话内执行附加输出，并标明首轮 init。

    入参：`tmp_path` 提供临时图片目录。
    返回：无返回值；断言通过表示控制台亮度/LED 可复用背景和按键同一 HID 会话。
    错误处理：callback 次数、初始化标识或设备 reopen 错误时由 pytest 报告。
    副作用：只调用 fake device 并写 pytest 临时图片。
    """

    device = FakeN4ProUnifiedDevice()
    manager = FakeUnifiedManager([device])
    callbacks: list[bool] = []
    animator = StreamDockN4ProPersistentAnimator(manager=manager, temp_dir=tmp_path)
    animator.set_session_output_callback(
        lambda _device, initialized: callbacks.append(initialized) or None
    )
    background = Image.new("RGB", (800, 480), (1, 2, 3))

    first = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )
    second = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )
    assert first.ok is True
    assert second.ok is True
    assert callbacks == [True, False]
    assert [name for name, _ in device.calls].count("open") == 1
    assert [name for name, _ in device.calls].count("init") == 1


def test_persistent_animator_handshakes_before_first_open_and_each_reconnect(
    tmp_path: Path,
) -> None:
    """首次接管和 USB path 变化后的重连都必须在 open/init 前发送握手。

    入参：`tmp_path` 提供临时背景目录；manager 第二轮切换到新 path 的握手 fake。
    返回：无返回值；断言通过表示品牌图恢复协议覆盖首次连接和重连，但稳定会话不重复握手。
    错误处理：握手缺失、顺序错误或稳定会话重复打开时由 pytest 报告。
    副作用：只写 pytest 临时图片并操作 fake device，不访问真实 N4 Pro。
    """

    original = FakeHandshakeN4ProUnifiedDevice(path="n4pro-path-a")
    replacement = FakeHandshakeN4ProUnifiedDevice(path="n4pro-path-b")
    manager = FakeUnifiedManager([original])
    animator = StreamDockN4ProPersistentAnimator(
        manager=manager,
        temp_dir=tmp_path,
        sleep=lambda _seconds: None,
    )
    background = Image.new("RGB", (800, 480), (1, 2, 3))

    first = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )
    steady = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )
    manager.devices = [replacement]
    reconnected = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )

    assert first.ok is True
    assert steady.ok is True
    assert reconnected.ok is True
    assert [name for name, _ in original.calls[:3]] == [
        "send_handshake",
        "open",
        "init",
    ]
    assert [name for name, _ in original.calls].count("send_handshake") == 1
    assert [name for name, _ in replacement.calls[:3]] == [
        "send_handshake",
        "open",
        "init",
    ]
    assert reconnected.timing_seconds["device_reconnected"] == 1.0
    assert "handshake" in first.timing_seconds
    assert "handshake" in reconnected.timing_seconds
    assert first.timing_seconds["device_handshaken"] == 1.0
    assert first.timing_seconds["device_handshake_count"] == 1.0
    assert steady.timing_seconds["device_handshaken"] == 0.0
    assert steady.timing_seconds["device_handshake_count"] == 1.0
    assert reconnected.timing_seconds["device_handshaken"] == 1.0
    assert reconnected.timing_seconds["device_handshake_count"] == 2.0


def test_persistent_animator_reports_handshake_permission_error_without_opening(
    tmp_path: Path,
) -> None:
    """raw HID 握手无权限时必须停止接管并保留底层错误文本。

    入参：`tmp_path` 提供临时目录；fake 握手抛出 macOS `not permitted` 错误。
    返回：无返回值；断言通过表示 SDK 的 open 假成功路径不会再被执行。
    错误处理：renderer 误报成功、吞掉权限错误或继续 open 时由 pytest 报告。
    副作用：只操作 fake device，不访问真实 N4 Pro。
    """

    device = FakeHandshakeN4ProUnifiedDevice(
        handshake_error=OSError("cannot open HID path: not permitted")
    )
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _seconds: None,
    )

    result = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )

    assert result.ok is False
    assert result.path == "n4pro-path"
    assert result.error is not None
    assert "handshake failed: OSError" in result.error
    assert "not permitted" in result.error
    assert device.calls == [("send_handshake", None), ("close", False)]


def test_persistent_animator_reopens_when_n4pro_reenumerates(
    tmp_path: Path,
) -> None:
    """N4 Pro 的 USB 路径变化后，persistent animator 应释放旧句柄并重连新设备。

    入参：`tmp_path` 提供临时背景目录；fake manager 先枚举路径 A，随后模拟设备重新枚举为路径 B。
    返回：无返回值；断言通过表示旧 HID 句柄不会在重枚举后继续接收按键、背景或 LED 写入。
    错误处理：未关闭路径 A、没有打开/初始化路径 B，或 reconnect timing 缺失时由 pytest 报告。
    副作用：仅写 pytest 临时图片并操作 fake device，不访问真实 N4 Pro。
    """

    original = FakeN4ProUnifiedDevice(path="n4pro-path-a")
    replacement = FakeN4ProUnifiedDevice(path="n4pro-path-b")
    manager = FakeUnifiedManager([original])
    animator = StreamDockN4ProPersistentAnimator(
        manager=manager,
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )
    background = Image.new("RGB", (800, 480), (1, 2, 3))

    first = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )
    manager.devices = [replacement]
    second = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )
    steady = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )

    assert first.ok is True
    assert second.ok is True
    assert second.path == "n4pro-path-b"
    assert second.timing_seconds["device_reconnected"] == 1.0
    assert second.timing_seconds["device_reconnect_count"] == 1.0
    assert steady.timing_seconds["device_reconnected"] == 0.0
    assert steady.timing_seconds["device_reconnect_count"] == 1.0
    assert ("close", False) in original.calls
    assert [name for name, _ in replacement.calls].count("open") == 1
    assert [name for name, _ in replacement.calls].count("init") == 1


def test_persistent_animator_reopens_unwritable_n4pro_at_same_path(
    tmp_path: Path,
) -> None:
    """同一 HID path 的旧句柄失效后应改用本轮重新枚举的新设备对象。

    入参：`tmp_path` 提供临时背景目录；fake manager 在第二轮返回 path 相同的新设备对象，
    同时把第一轮持有句柄标为不可写。
    返回：无返回值；断言通过表示设备原地重启后不会继续复用 stale handle。
    错误处理：旧句柄未关闭、新对象未 open/init 或缺少重连诊断时由 pytest 报告。
    副作用：仅写 pytest 临时图片并操作 fake device，不访问真实 N4 Pro。
    """

    original = FakeN4ProUnifiedDevice(path="stable-n4pro-path")
    replacement = FakeN4ProUnifiedDevice(path="stable-n4pro-path")
    manager = FakeUnifiedManager([original])
    animator = StreamDockN4ProPersistentAnimator(
        manager=manager,
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )
    background = Image.new("RGB", (800, 480), (1, 2, 3))

    first = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )
    original.writable = False
    manager.devices = [replacement]
    second = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )
    steady = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )

    assert first.ok is True
    assert second.ok is True
    assert second.path == "stable-n4pro-path"
    assert second.timing_seconds["device_reconnected"] == 1.0
    assert second.timing_seconds["device_reconnect_count"] == 1.0
    assert steady.timing_seconds["device_reconnected"] == 0.0
    assert steady.timing_seconds["device_reconnect_count"] == 1.0
    assert ("close", False) in original.calls
    assert [name for name, _ in replacement.calls].count("open") == 1
    assert [name for name, _ in replacement.calls].count("init") == 1


def test_persistent_animator_recovers_after_device_disappears_and_returns(
    tmp_path: Path,
) -> None:
    """设备完整消失后以同一 path 返回时，下一轮应重新 open/init 并恢复渲染。

    入参：`tmp_path` 提供临时背景目录；fake manager 依次模拟在线、无设备、同 path 新设备。
    返回：无返回值；断言通过表示真实拔插窗口不会终止 animator 后续探测。
    错误处理：离线轮未关闭旧句柄或恢复轮未打开新设备时由 pytest 报告。
    副作用：仅写 pytest 临时图片并操作 fake device，不访问真实 N4 Pro。
    """

    original = FakeN4ProUnifiedDevice(path="stable-n4pro-path")
    replacement = FakeN4ProUnifiedDevice(path="stable-n4pro-path")
    manager = FakeUnifiedManager([original])
    animator = StreamDockN4ProPersistentAnimator(
        manager=manager,
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )
    background = Image.new("RGB", (800, 480), (1, 2, 3))

    online = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )
    manager.devices = []
    offline = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )
    manager.devices = [replacement]
    recovered = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )

    assert online.ok is True
    assert offline.ok is False
    assert offline.error == "N4 Pro disappeared while persistent renderer was active"
    assert recovered.ok is True
    assert recovered.timing_seconds["device_reconnected"] == 1.0
    assert recovered.timing_seconds["device_reconnect_count"] == 1.0
    assert ("close", False) in original.calls
    assert [name for name, _ in replacement.calls].count("open") == 1
    assert [name for name, _ in replacement.calls].count("init") == 1


def test_persistent_animator_closes_session_on_unsigned_sdk_write_failure(
    tmp_path: Path,
) -> None:
    """ctypes 无符号返回的 native -1 必须被识别为硬件写失败。

    入参：`tmp_path` 提供临时图片目录；fake key writer 返回 `0xFFFFFFFF`。
    返回：无返回值；断言通过表示失败不会被记录成 ok，也不会保留失效设备会话。
    错误处理：漏判无符号失败值或未关闭句柄时由 pytest 报告。
    副作用：仅写 pytest 临时图片并操作 fake device，不访问真实 N4 Pro。
    """

    device = FakeN4ProUnifiedDevice(key_result=0xFFFFFFFF)
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )

    result = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={},
        key_images={1: Image.new("RGB", (112, 112), (4, 5, 6))},
        duration_seconds=0.01,
        fps=1,
    )

    assert result.ok is False
    assert result.error == "set_key_image failed for key 1: SDK returned 4294967295"
    assert device.calls[-1] == ("close", False)


def test_persistent_animator_closes_session_when_refresh_fails(
    tmp_path: Path,
) -> None:
    """最终 refresh 失败时必须关闭当前设备会话并进入下一轮重连。

    入参：`tmp_path` 提供临时背景目录；fake refresh 返回 `0xFFFFFFFF`。
    返回：无返回值；断言通过表示 refresh 错误不会被误报为成功。
    错误处理：漏判 refresh 失败或保留旧句柄时由 pytest 报告。
    副作用：仅写 pytest 临时图片并操作 fake device，不访问真实 N4 Pro。
    """

    device = FakeN4ProUnifiedDevice(refresh_result=0xFFFFFFFF)
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )

    result = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )

    assert result.ok is False
    assert result.error == "refresh failed: SDK returned 4294967295"
    assert device.calls[-1] == ("close", False)


def test_persistent_animator_refreshes_session_output_for_each_animation_frame(
    tmp_path: Path,
) -> None:
    """带按键动画时，persistent animator 应在同一会话逐帧刷新附加输出。

    入参：`tmp_path` 提供一张最小按键帧；session callback 只记录调用次数。
    返回：无返回值；断言通过表示软件呼吸灯可以在现有帧循环内平滑更新亮度。
    错误处理：帧循环漏调 callback 或重新打开设备时由 pytest 报告。
    副作用：只操作 fake device 和 pytest 临时图片。
    """

    frame = tmp_path / "frame.png"
    Image.new("RGB", (96, 96), (10, 20, 30)).save(frame)
    device = FakeN4ProUnifiedDevice()
    callbacks: list[bool] = []
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )
    animator.set_session_output_callback(
        lambda _device, initialized: callbacks.append(initialized) or None
    )

    result = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={1: (frame,)},
        duration_seconds=0.2,
        fps=10,
    )

    assert result.ok is True
    assert callbacks == [True, False, False]
    assert [name for name, _ in device.calls].count("open") == 1


def test_persistent_animator_writes_a_new_background_revision_during_active_frame_loop(
    tmp_path: Path,
) -> None:
    """长连接动画循环应在不重开设备的前提下立即下发更新后的背景 revision。

    入参：`tmp_path` 提供最小按键帧；provider 先返回初始背景 revision，随后返回更新后的 revision。
    返回：无返回值；断言通过表示旋钮反馈不必等待下一次 3 秒 renderer tick。
    错误处理：背景 revision 未检测、额外 open 或 frame background 写入次数错误时由 pytest 报告。
    副作用：仅操作 fake device 和 pytest 临时图片。
    """

    frame = tmp_path / "frame.png"
    Image.new("RGB", (96, 96), (10, 20, 30)).save(frame)
    device = FakeN4ProUnifiedDevice()
    initial = Image.new("RGB", (800, 480), (1, 2, 3))
    updated = Image.new("RGB", (800, 480), (4, 5, 6))
    revisions = iter(((1, initial), (2, updated), (2, updated)))
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )
    animator.set_background_update_provider(lambda: next(revisions))

    result = animator(
        background_image=initial,
        key_frame_paths={1: (frame,)},
        duration_seconds=0.2,
        fps=10,
    )

    assert result.ok is True
    assert [name for name, _ in device.calls].count("set_frame_background") == 2
    assert [name for name, _ in device.calls].count("open") == 1
    assert result.timing_seconds["background_hot_updates"] == 1.0
    assert result.timing_seconds["background_hot_update"] >= 0.0


def test_persistent_animator_writes_static_key_diff_during_active_frame_loop(
    tmp_path: Path,
) -> None:
    """长连接动画循环应在同一会话内立即写入状态键的最新差异图。

    入参：`tmp_path` 提供最小 agent 动画帧；静态键 provider 先给出初始 revision，再给出一个
    只包含键 1 的新图片 revision。
    返回：无返回值；断言通过表示物理状态键切换不必等待下一轮 renderer tick。
    错误处理：revision 未识别、静态键被重复写入或没有与下一帧一起 refresh 时由 pytest 报告。
    副作用：仅操作 fake device 和 pytest 临时图片，不访问真实 N4 Pro。
    """

    frame = tmp_path / "frame.png"
    Image.new("RGB", (96, 96), (10, 20, 30)).save(frame)
    device = FakeN4ProUnifiedDevice()
    initial = Image.new("RGB", (112, 112), (1, 2, 3))
    updated = Image.new("RGB", (112, 112), (4, 5, 6))
    revisions = iter(((1, {1: initial}), (2, {1: updated}), (2, {1: updated})))
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )
    animator.set_key_image_update_provider(lambda: next(revisions))

    result = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={2: (frame,)},
        key_images={1: initial},
        duration_seconds=0.2,
        fps=10,
    )

    assert result.ok is True
    assert [entry for entry in device.calls if entry == ("set_key_image", 1)] == [
        ("set_key_image", 1),
        ("set_key_image", 1),
    ]
    assert [entry for entry in device.calls if entry == ("set_key_image", 2)] == [
        ("set_key_image", 2),
        ("set_key_image", 2),
    ]
    assert [name for name, _ in device.calls].count("refresh") == 2
    assert [name for name, _ in device.calls].count("open") == 1
    assert result.timing_seconds["key_hot_updates"] == 1.0
    assert result.timing_seconds["key_hot_keys"] == 1.0
    assert result.timing_seconds["key_hot_update"] >= 0.0


def test_persistent_animator_clears_a_removed_static_key_in_same_session(
    tmp_path: Path,
) -> None:
    """静态键从完整映射删除时应写一张清屏图，不重开设备或遗留旧帧。

    入参：``tmp_path`` 提供初始静态键图片；provider 从 ``{1: image}`` 切到空映射。
    返回：无；断言 Key 1 写入两次、第二次计为一次 hot update 且 open/init 仍各一次。
    错误处理：删除只改变 Python 映射却没有真实 ``set_key_image`` 时由 pytest 报告。
    副作用：只操作 fake device 和 pytest 临时图片，不访问真实 N4 Pro。
    """

    device = FakeN4ProUnifiedDevice()
    initial = Image.new("RGB", (112, 112), (1, 2, 3))
    revisions = iter(((1, {1: initial}), (2, {}), (2, {})))
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )
    animator.set_key_image_update_provider(lambda: next(revisions))

    result = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={},
        duration_seconds=0.2,
        fps=10,
    )

    assert result.ok is True
    assert [entry for entry in device.calls if entry == ("set_key_image", 1)] == [
        ("set_key_image", 1),
        ("set_key_image", 1),
    ]
    assert [name for name, _ in device.calls].count("open") == 1
    assert [name for name, _ in device.calls].count("init") == 1
    assert result.timing_seconds["key_hot_updates"] == 1.0
    assert result.timing_seconds["key_hot_keys"] == 1.0


def test_persistent_animator_reuses_prerendered_key_paths_during_hot_updates(
    tmp_path: Path,
) -> None:
    """长连接动画循环应原样复用预渲染路径，并只下发实际切换的宠物帧。

    入参：`tmp_path` 提供两个预渲染宠物键 PNG 和一个普通动画键帧；provider 在活跃循环中
    从第一帧切到第二帧，随后仅增加 revision 而保持同一路径。
    返回：无返回值；断言通过表示 Path 未被二次编码、同一路径未重复写入且仍共用一次会话。
    错误处理：路径被复制/清理、revision 空转导致重复写或 open/init 次数变化时由 pytest 报告。
    副作用：只操作 fake device 和 pytest 临时图片，不访问真实 N4 Pro。
    """

    animation_frame = tmp_path / "agent-frame.png"
    pet_frame_a = tmp_path / "pet-a.png"
    pet_frame_b = tmp_path / "pet-b.png"
    Image.new("RGB", (96, 96), (10, 20, 30)).save(animation_frame)
    Image.new("RGB", (112, 112), (1, 2, 3)).save(pet_frame_a)
    Image.new("RGB", (112, 112), (4, 5, 6)).save(pet_frame_b)
    revisions = iter(
        (
            (1, {1: pet_frame_a}),
            (1, {1: pet_frame_a}),
            (2, {1: pet_frame_b}),
            (3, {1: pet_frame_b}),
        )
    )
    device = FakeN4ProUnifiedDevice()
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )
    animator.set_key_image_update_provider(lambda: next(revisions))

    result = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={2: (animation_frame,)},
        duration_seconds=0.3,
        fps=10,
    )

    assert result.ok is True
    assert [entry for entry in device.calls if entry == ("set_key_image", 1)] == [
        ("set_key_image", 1),
        ("set_key_image", 1),
    ]
    assert device.paths_seen.count(pet_frame_a) == 1
    assert device.paths_seen.count(pet_frame_b) == 1
    assert not any(path.name.startswith("agent-deck-n4pro-key-") for path in device.paths_seen)
    assert pet_frame_a.is_file()
    assert pet_frame_b.is_file()
    assert [name for name, _ in device.calls].count("open") == 1
    assert [name for name, _ in device.calls].count("init") == 1
    assert result.timing_seconds["key_hot_updates"] == 1.0
    assert result.timing_seconds["key_hot_keys"] == 1.0


def test_persistent_animator_rewrites_static_pet_after_agent_animation_releases_key(
    tmp_path: Path,
) -> None:
    """动画键被静态宠物接管时，下一轮必须真正重写同 revision 的宠物帧。

    入参：`tmp_path` 提供一张 agent 动画帧和一张预渲染宠物帧；provider 在首轮动画中途
    从空映射切到键 1 宠物，并在第二轮保持相同 revision。
    返回：无返回值；断言第二轮释放动画键后仍写入宠物，且 persistent 会话不重新 open/init。
    错误处理：若热更新被随后 agent 帧覆盖却仍被误记为已生效，路径与调用次数断言会失败。
    副作用：只操作 fake device 和 pytest 临时图片，不访问真实 N4 Pro。
    """

    agent_frame = tmp_path / "agent-frame.png"
    pet_frame = tmp_path / "pet-frame.png"
    Image.new("RGB", (96, 96), (10, 20, 30)).save(agent_frame)
    Image.new("RGB", (112, 112), (4, 5, 6)).save(pet_frame)
    provider_calls = [0]

    def provide_key_images() -> tuple[int, dict[int, Path]]:
        """首读返回空映射，后续始终返回同一宠物 revision。

        入参：无。
        返回：第一次为 revision 1 的空映射，随后为 revision 2 的键 1 宠物路径。
        错误处理：无。
        副作用：递增内存调用计数，用来模拟首轮动画中途发生布局切换。
        """

        provider_calls[0] += 1
        if provider_calls[0] == 1:
            return 1, {}
        return 2, {1: pet_frame}

    device = FakeN4ProUnifiedDevice()
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )
    animator.set_key_image_update_provider(provide_key_images)

    first = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={1: (agent_frame,)},
        duration_seconds=0.1,
        fps=10,
    )
    writes_after_first = [
        entry for entry in device.calls if entry == ("set_key_image", 1)
    ]
    second = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={},
        duration_seconds=0.1,
        fps=10,
    )

    assert first.ok is True
    assert second.ok is True
    assert len(writes_after_first) == 1
    assert [entry for entry in device.calls if entry == ("set_key_image", 1)] == [
        ("set_key_image", 1),
        ("set_key_image", 1),
    ]
    assert device.paths_seen[-1] == pet_frame
    assert [name for name, _ in device.calls].count("open") == 1
    assert [name for name, _ in device.calls].count("init") == 1


def test_persistent_animator_does_not_repeat_same_prerendered_path_between_calls(
    tmp_path: Path,
) -> None:
    """相同 revision 或仅 revision 变化但路径未变时不应跨 renderer 轮次重写宠物键。

    入参：`tmp_path` 提供一个预渲染宠物键 PNG 和普通动画键帧；provider 状态由测试原地更新。
    返回：无返回值；断言通过表示相同路径只下发一次，且多轮调用仍复用首次 open/init 会话。
    错误处理：同帧重复写、provider revision 空转触发写入或额外重连时由 pytest 报告。
    副作用：只操作 fake device 和 pytest 临时图片，不访问真实硬件。
    """

    animation_frame = tmp_path / "agent-frame.png"
    pet_frame = tmp_path / "pet.png"
    Image.new("RGB", (96, 96), (10, 20, 30)).save(animation_frame)
    Image.new("RGB", (112, 112), (1, 2, 3)).save(pet_frame)
    current_revision = [1]
    device = FakeN4ProUnifiedDevice()
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )
    animator.set_key_image_update_provider(
        lambda: (current_revision[0], {1: pet_frame})
    )

    first = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={2: (animation_frame,)},
        duration_seconds=0.1,
        fps=10,
    )
    second = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={2: (animation_frame,)},
        duration_seconds=0.1,
        fps=10,
    )
    current_revision[0] = 2
    third = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={2: (animation_frame,)},
        duration_seconds=0.1,
        fps=10,
    )

    assert first.ok is True
    assert second.ok is True
    assert third.ok is True
    assert [entry for entry in device.calls if entry == ("set_key_image", 1)] == [
        ("set_key_image", 1)
    ]
    assert [name for name, _ in device.calls].count("open") == 1
    assert [name for name, _ in device.calls].count("init") == 1


def test_persistent_animator_rejects_missing_prerendered_path_and_closes(
    tmp_path: Path,
) -> None:
    """provider 返回不存在的预渲染路径时应失败并关闭已打开的持久会话。

    入参：`tmp_path` 提供不会实际创建的宠物键路径。
    返回：无返回值；断言通过表示错误清晰、没有调用按键写入且 open 后执行 close(False)。
    错误处理：无效路径被交给 SDK、会话泄漏或异常逸出时由 pytest 报告。
    副作用：只操作 fake device 的内存调用记录，不访问真实 N4 Pro。
    """

    missing_path = tmp_path / "missing-pet-frame.png"
    device = FakeN4ProUnifiedDevice()
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )
    animator.set_key_image_update_provider(lambda: (1, {1: missing_path}))

    result = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={},
        duration_seconds=0.1,
        fps=10,
    )

    assert result.ok is False
    assert result.error is not None
    assert "must be an existing file" in result.error
    assert ("set_key_image", 1) not in device.calls
    assert [name for name, _ in device.calls].count("open") == 1
    assert [name for name, _ in device.calls].count("init") == 1
    assert device.calls[-1] == ("close", False)


def test_persistent_animator_background_notification_wakes_frame_wait(
    tmp_path: Path,
) -> None:
    """背景 revision 通知应唤醒持久 animator，而不是固定等到下一帧。

    入参：`tmp_path` 提供最小按键帧；测试通过可注入 wait 函数模拟一条输入通知。
    返回：无返回值；断言通过表示 notifier 会让当前帧循环提前继续。
    错误处理：没有调用 wait 或 notifier 未清除时由 pytest 报告。
    副作用：只操作 fake device 和 pytest 临时图片。
    """

    frame = tmp_path / "frame.png"
    Image.new("RGB", (96, 96), (10, 20, 30)).save(frame)
    device = FakeN4ProUnifiedDevice()
    waits: list[float] = []
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _: None,
        background_wait=lambda timeout: waits.append(timeout) or True,
    )
    animator.set_background_update_provider(
        lambda: (1, Image.new("RGB", (800, 480), (1, 2, 3)))
    )

    animator.notify_background_update()
    result = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={1: (frame,)},
        duration_seconds=0.2,
        fps=10,
    )

    assert result.ok is True
    assert waits


def test_animate_key_images_on_n4pro_keeps_one_device_session(
    tmp_path: Path,
) -> None:
    """验证按键动画预览在一次设备会话里写背景并循环刷新按键。

    入参：`tmp_path` 提供临时背景和按键帧目录。
    返回：无返回值；断言通过表示 open/init 只发生一次，按键帧循环写入并 refresh。
    错误处理：调用顺序、帧数或临时文件清理不符合预期时由 pytest 报告。
    副作用：在 pytest 临时目录写入 PNG/JPEG 文件；不访问真实 N4 Pro。
    """

    frame_a = tmp_path / "frame_a.png"
    frame_b = tmp_path / "frame_b.png"
    Image.new("RGB", (112, 112), (10, 20, 30)).save(frame_a)
    Image.new("RGB", (112, 112), (30, 20, 10)).save(frame_b)
    device = FakeN4ProUnifiedDevice(background_result=0, key_result=0)
    manager = FakeUnifiedManager([device])
    background = Image.new("RGB", (800, 480), (1, 2, 3))

    result = animate_key_images_on_n4pro(
        background_image=background,
        key_frame_paths={1: (frame_a, frame_b)},
        duration_seconds=0.3,
        fps=10,
        manager=manager,
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )

    assert result.ok is True
    assert result.frames_rendered == 3
    assert result.key_count == 1
    assert [name for name, _ in device.calls] == [
        "open",
        "init",
        "set_frame_background",
        "set_key_image",
        "refresh",
        "set_key_image",
        "refresh",
        "set_key_image",
        "refresh",
        "close",
    ]
    assert [call for call in device.calls if call[0] == "set_key_image"] == [
        ("set_key_image", 1),
        ("set_key_image", 1),
        ("set_key_image", 1),
    ]
    assert device.calls[-1] == ("close", False)


def test_animate_key_images_on_n4pro_writes_static_key_images(
    tmp_path: Path,
) -> None:
    """验证动画 sink 能在同一设备会话里写入 App 等静态按键图。

    入参：`tmp_path` 提供临时背景和静态图目录。
    返回：无返回值；断言通过表示静态图下发后会 refresh，且不要求动态帧。
    错误处理：调用顺序、key_count 或临时文件清理不符合预期时由 pytest 报告。
    副作用：在 pytest 临时目录写入 PNG/JPEG 文件；不访问真实 N4 Pro。
    """

    device = FakeN4ProUnifiedDevice(background_result=0, key_result=0)
    manager = FakeUnifiedManager([device])

    result = animate_key_images_on_n4pro(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={},
        key_images={1: Image.new("RGB", (112, 112), (20, 120, 220))},
        duration_seconds=0.3,
        fps=10,
        manager=manager,
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )

    assert result.ok is True
    assert result.frames_rendered == 1
    assert result.key_count == 1
    assert [name for name, _ in device.calls] == [
        "open",
        "init",
        "set_frame_background",
        "set_key_image",
        "refresh",
        "close",
    ]
    assert device.calls[3] == ("set_key_image", 1)
    assert device.calls[-1] == ("close", False)
    assert all(not path.exists() for path in device.paths_seen)


def test_animate_key_images_on_n4pro_reports_static_key_failure(
    tmp_path: Path,
) -> None:
    """验证静态按键图下发失败时会返回明确错误。

    入参：`tmp_path` 提供临时背景和静态图目录。
    返回：无返回值；断言通过表示 App 静态图失败不会被误报为成功。
    错误处理：错误信息或关闭行为不符合预期时由 pytest 报告。
    副作用：只操作 fake device 和 pytest 临时文件，不访问真实 N4 Pro。
    """

    device = FakeN4ProUnifiedDevice(background_result=0, key_result=-1)
    manager = FakeUnifiedManager([device])

    result = animate_key_images_on_n4pro(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={},
        key_images={1: Image.new("RGB", (112, 112), (20, 120, 220))},
        duration_seconds=0.3,
        fps=10,
        manager=manager,
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )

    assert result.ok is False
    assert result.key_count == 1
    assert result.error == "set_key_image failed for key 1: SDK returned -1"
    assert device.calls[-1] == ("close", False)
    assert all(not path.exists() for path in device.paths_seen)


def test_animate_key_images_on_n4pro_reports_timing_diagnostics(
    tmp_path: Path,
) -> None:
    """验证按键动画结果会暴露关键阶段耗时。

    入参：`tmp_path` 提供临时帧文件；测试内注入 deterministic monotonic 和 no-op sleep。
    返回：无返回值；断言通过表示 status 可用于判断停顿来自 setup、播放还是 close。
    错误处理：缺少 timing 字段或字段值不符合递增时钟时由 pytest 报告。
    副作用：只写 pytest 临时 PNG 文件；不访问真实 N4 Pro。
    """

    frame = tmp_path / "frame.png"
    Image.new("RGB", (112, 112), (10, 20, 30)).save(frame)
    device = FakeN4ProUnifiedDevice(background_result=0, key_result=0)
    manager = FakeUnifiedManager([device])
    times = iter((0.0, 0.1, 0.2, 0.3, 0.9, 1.0, 1.1, 1.2))

    result = animate_key_images_on_n4pro(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={1: (frame,)},
        duration_seconds=0.5,
        fps=2,
        manager=manager,
        temp_dir=tmp_path,
        sleep=lambda _: None,
        monotonic=lambda: next(times),
    )

    assert result.ok is True
    assert result.timing_seconds == {
        "open_init": 0.1,
        "background": 0.1,
        "static_keys": 0.0,
        "first_frame": 0.1,
        "playback": 0.7,
        "close": 0.1,
        "total": 1.2,
    }


def test_persistent_animator_reuses_open_device_between_calls(
    tmp_path: Path,
) -> None:
    """验证 daemon 用 persistent animator 不会每轮 close/open 造成停顿。

    入参：`tmp_path` 提供临时帧和背景目录。
    返回：无返回值；断言通过表示两轮渲染只 open/init 一次，中间不 close。
    错误处理：重复 open/init 或自动 close 会由 pytest 报告。
    副作用：只操作 fake device 和 pytest 临时文件，不访问真实 N4 Pro。
    """

    frame = tmp_path / "frame.png"
    Image.new("RGB", (112, 112), (10, 20, 30)).save(frame)
    device = FakeN4ProUnifiedDevice(background_result=0, key_result=0)
    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )

    first = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={1: (frame,)},
        duration_seconds=0.1,
        fps=10,
    )
    second = animator(
        background_image=Image.new("RGB", (800, 480), (3, 2, 1)),
        key_frame_paths={1: (frame,)},
        duration_seconds=0.1,
        fps=10,
    )

    assert first.ok is True
    assert second.ok is True
    assert [call for call in device.calls if call[0] == "open"] == [("open", None)]
    assert [call for call in device.calls if call[0] == "init"] == [("init", None)]
    assert [call for call in device.calls if call[0] == "close"] == []

    animator.close()

    assert device.calls[-1] == ("close", False)


def test_persistent_animator_closes_same_path_enumeration_wrapper(
    tmp_path: Path,
) -> None:
    """稳定会话应释放只用于 path 比较的新 SDK wrapper，避免每轮泄漏后台线程。

    入参：`tmp_path` 提供临时背景目录；第二轮枚举返回同 path 的另一个 fake device。
    返回：无返回值；断言通过表示活动会话继续复用，临时 wrapper 立即无通知关闭。
    错误处理：重复 open 活动设备、采用临时 wrapper 或漏掉 close 时由 pytest 报告。
    副作用：只操作 fake device 与 pytest 临时图片，不启动线程、不访问真实 N4 Pro。
    """

    active = FakeN4ProUnifiedDevice(path="stable-n4pro-path")
    duplicate = FakeN4ProUnifiedDevice(path="stable-n4pro-path")
    manager = FakeUnifiedManager([active])
    animator = StreamDockN4ProPersistentAnimator(
        manager=manager,
        temp_dir=tmp_path,
        sleep=lambda _: None,
    )
    background = Image.new("RGB", (800, 480), (1, 2, 3))

    first = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )
    manager.devices = [duplicate]
    second = animator(
        background_image=background,
        key_frame_paths={},
        duration_seconds=0.01,
        fps=1,
    )

    assert first.ok is True
    assert second.ok is True
    assert [name for name, _ in active.calls].count("open") == 1
    assert ("close", False) not in active.calls
    assert duplicate.calls == [("close", False)]

    animator.close()

    assert active.calls[-1] == ("close", False)


def test_persistent_animator_registers_input_callbacks_on_open(
    tmp_path: Path,
) -> None:
    """验证 persistent animator 在同一设备会话里注册输入回调。

    入参：`tmp_path` 提供临时帧和背景目录。
    返回：无返回值；断言通过表示 key/knob 与 touch bar 回调都绑定到同一 input callback。
    错误处理：未注册、重复打开或回调对象不一致时由 pytest 报告。
    副作用：只操作 fake device 和 pytest 临时文件，不访问真实 N4 Pro。
    """

    frame = tmp_path / "frame.png"
    Image.new("RGB", (112, 112), (10, 20, 30)).save(frame)
    device = FakeN4ProUnifiedDevice(background_result=0, key_result=0)
    callbacks: list[object] = []

    def input_callback(_device: object, event: object) -> None:
        """记录 fake SDK 回调事件。

        入参：`_device` 是 fake device；`event` 是 fake SDK event。
        返回：无。
        错误处理：无。
        副作用：追加测试内存列表。
        """

        callbacks.append(event)

    animator = StreamDockN4ProPersistentAnimator(
        manager=FakeUnifiedManager([device]),
        temp_dir=tmp_path,
        input_callback=input_callback,
        sleep=lambda _: None,
    )

    result = animator(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        key_frame_paths={1: (frame,)},
        duration_seconds=0.1,
        fps=10,
    )

    assert result.ok is True
    assert device.key_callback is input_callback
    assert device.touch_bar_callback is input_callback

    device.key_callback(device, "knob-event")
    device.touch_bar_callback(device, "touch-event")

    assert callbacks == ["knob-event", "touch-event"]


def test_render_images_to_n4pro_rejects_invalid_key() -> None:
    """验证非法按键编号不会打开设备。

    入参：无。
    返回：无返回值；断言通过表示 key 校验在硬件访问前完成。
    错误处理：若错误信息不清晰或误访问设备，由 pytest 报告。
    副作用：只操作 fake manager 内存记录。
    """

    manager = FakeUnifiedManager([FakeN4ProUnifiedDevice()])
    result = render_images_to_n4pro(
        key_images={0: Image.new("RGB", (112, 112), (1, 2, 3))},
        manager=manager,
    )

    assert result.ok is False
    assert result.error == "keys must be in range 1..15: [0]"
    assert manager.enumerate_count == 0


def test_render_images_to_n4pro_requires_at_least_one_surface() -> None:
    """验证没有任何待写 surface 时明确失败。

    入参：无。
    返回：无返回值；断言通过表示 writer 不会空刷新真实设备。
    错误处理：若错误信息不清晰或误访问设备，由 pytest 报告。
    副作用：只操作 fake manager 内存记录。
    """

    manager = FakeUnifiedManager([FakeN4ProUnifiedDevice()])

    result = render_images_to_n4pro(manager=manager)

    assert result.ok is False
    assert result.error == "at least one background or key image is required"
    assert manager.enumerate_count == 0


def test_render_images_to_n4pro_skips_non_n4pro() -> None:
    """验证 unified writer 只接管 N4 Pro 设备。

    入参：无。
    返回：无返回值；断言通过表示非 N4 Pro 不会被 open/init。
    错误处理：若误接管其他设备或未返回错误说明，由 pytest 报告。
    副作用：只操作 fake 设备内存记录。
    """

    other = FakeOtherUnifiedDevice(path="other-path")
    manager = FakeUnifiedManager([other])

    result = render_images_to_n4pro(
        background_image=Image.new("RGB", (800, 480), (1, 2, 3)),
        manager=manager,
    )

    assert result.ok is False
    assert result.error == "no N4 Pro device found"
    assert other.calls == []
