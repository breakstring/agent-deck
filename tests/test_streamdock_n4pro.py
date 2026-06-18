"""StreamDock N4 Pro 多 surface 统一 writer 的单元测试。

这些测试只使用 fake manager/device 验证调用顺序、临时图片处理和错误路径，不访问真实
N4 Pro、不加载官方 SDK、不启动 daemon，也不写用户配置。真实硬件 smoke 只能作为显式手动步骤。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from agent_deck.hardware.streamdock_n4pro import (
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
