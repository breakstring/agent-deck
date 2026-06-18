"""StreamDock N4 Pro 按键真实下发适配器的单元测试。

这些测试只使用 fake manager/device 验证调用顺序和临时图片处理，不访问真实 N4 Pro、
不加载官方 SDK、不启动 daemon，也不写用户配置。真实硬件 smoke 只能作为显式手动步骤。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from agent_deck.hardware.streamdock_key import render_key_image_to_n4pro


class FakeN4ProKeyDevice:
    """测试用 N4 Pro 按键 device fake。

    入参：`path` 是设备路径；`set_result` 是 `set_key_image` 的返回值。
    返回：提供真实按键 sink 需要的最小 device 接口。
    错误处理：本 fake 默认不抛异常。
    副作用：把方法调用记录到 `calls`，不访问硬件。
    """

    def __init__(self, path: str = "n4pro-path", set_result: object = 0) -> None:
        """初始化 fake 设备。

        入参：`path` 是测试设备路径；`set_result` 是按键下发返回值。
        返回：无返回值。
        错误处理：无。
        副作用：初始化内存调用记录。
        """

        self.path = path
        self.calls: list[tuple[str, object | None]] = []
        self.set_result = set_result
        self.image_path_seen: Path | None = None

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

    def set_key_image(self, key: int, path: str) -> object:
        """记录按键图片路径并模拟 SDK 下发。

        入参：`key` 是逻辑按键编号；`path` 是 sink 保存的临时 PNG 路径。
        返回：构造时传入的 `set_result`。
        错误处理：无。
        副作用：记录 key 和路径，并断言文件在 SDK 调用期间存在。
        """

        self.calls.append(("set_key_image", key))
        self.image_path_seen = Path(path)
        assert self.image_path_seen.is_file()
        return self.set_result

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


class FakeOtherKeyDevice(FakeN4ProKeyDevice):
    """测试用非 N4 Pro 设备 fake。

    入参：继承自 `FakeN4ProKeyDevice`。
    返回：类名不包含 N4Pro，因此 sink 应跳过该设备。
    错误处理：无。
    副作用：仅记录调用。
    """


class FakeKeyManager:
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


def test_render_key_image_to_n4pro_calls_sdk_sequence(tmp_path: Path) -> None:
    """验证 N4 Pro 按键 sink 会按 SDK 所需顺序下发图片。

    入参：`tmp_path` 提供临时图片目录。
    返回：无返回值；断言通过表示 open/init/set/refresh/close 顺序正确。
    错误处理：调用顺序、返回值或临时文件清理不符合预期时由 pytest 报告。
    副作用：在 pytest 临时目录短暂写入 PNG，随后清理。
    """

    device = FakeN4ProKeyDevice(set_result=0)
    manager = FakeKeyManager([device])
    image = Image.new("RGB", (112, 112), (1, 2, 3))

    result = render_key_image_to_n4pro(
        image,
        key=1,
        manager=manager,
        temp_dir=tmp_path,
    )

    assert result.ok is True
    assert result.key == 1
    assert result.device_type == "FakeN4ProKeyDevice"
    assert result.path == "n4pro-path"
    assert result.sdk_result == "0"
    assert [name for name, _ in device.calls] == [
        "open",
        "init",
        "set_key_image",
        "refresh",
        "close",
    ]
    assert device.calls[2] == ("set_key_image", 1)
    assert device.calls[-1] == ("close", False)
    assert device.image_path_seen is not None
    assert not device.image_path_seen.exists()


def test_render_key_image_to_n4pro_rejects_invalid_key() -> None:
    """验证非法按键编号不会打开设备。

    入参：无。
    返回：无返回值；断言通过表示 key 校验在硬件访问前完成。
    错误处理：若错误信息不清晰或误访问设备，由 pytest 报告。
    副作用：只操作 fake manager 内存记录。
    """

    manager = FakeKeyManager([FakeN4ProKeyDevice()])
    image = Image.new("RGB", (112, 112), (1, 2, 3))

    result = render_key_image_to_n4pro(image, key=0, manager=manager)

    assert result.ok is False
    assert result.error == "key must be in range 1..15"
    assert manager.enumerate_count == 0


def test_render_key_image_to_n4pro_skips_non_n4pro() -> None:
    """验证按键 sink 只接管 N4 Pro 设备。

    入参：无。
    返回：无返回值；断言通过表示非 N4 Pro 不会被 open/init。
    错误处理：若误接管其他设备或未返回错误说明，由 pytest 报告。
    副作用：只操作 fake 设备内存记录。
    """

    other = FakeOtherKeyDevice(path="other-path")
    manager = FakeKeyManager([other])
    image = Image.new("RGB", (112, 112), (1, 2, 3))

    result = render_key_image_to_n4pro(image, key=1, manager=manager)

    assert result.ok is False
    assert result.error == "no N4 Pro device found"
    assert other.calls == []
