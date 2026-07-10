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
    ) -> None:
        """初始化 fake 设备。

        入参：`path` 是测试设备路径；`background_result` 和 `key_result` 是模拟 SDK 返回值。
        返回：无返回值。
        错误处理：无。
        副作用：初始化内存调用记录。
        """

        self.path = path
        self.background_result = background_result
        self.key_result = key_result
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

    def refresh(self) -> None:
        """记录 refresh 调用。

        入参：无。
        返回：无返回值。
        错误处理：无。
        副作用：追加调用记录。
        """

        self.calls.append(("refresh", None))

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
