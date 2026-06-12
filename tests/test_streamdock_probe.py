"""StreamDock 探针的单元测试。

本文件只用 fake manager/device 定义 Task 8 的诊断契约；不会访问真实 N4 Pro、
不会导入官方 SDK、不会渲染图片、不会修改 LED 或按键配置，也不会读写外部文件。
副作用限定为创建本地 Python 对象和 pytest 断言输出。
"""

from __future__ import annotations

import pytest

from agent_deck.hardware.streamdock_probe import (
    ProbeResult,
    _resolve_sdk_src_path,
    probe_streamdock_devices,
)


class FakeDevice:
    """记录探针调用顺序的 StreamDock device fake。

    入参：构造参数描述 fake 设备的类型、路径、固件序列号以及各阶段失败开关。
    返回：实例暴露探针所需的最小方法集合，并记录所有被调用的方法名。
    错误处理：当 open/init/close 对应失败开关启用时抛出 RuntimeError。
    副作用：仅修改本实例的 `calls` 列表；不会访问硬件、网络、文件系统或全局状态。
    """

    def __init__(
        self,
        *,
        device_type: str = "N4 Pro",
        path: str = "fake-path",
        firmware_version: str = "1.2.3",
        serial_number: str = "SN123",
        open_error: Exception | None = None,
        init_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        """初始化一个可控 fake device。

        入参：`device_type`、`path`、`firmware_version`、`serial_number` 是读取接口返回值；
        `open_error`、`init_error`、`close_error` 分别控制对应阶段是否失败。
        返回：无显式返回值；初始化后的实例可被 fake manager 枚举。
        错误处理：构造阶段不主动抛业务异常。
        副作用：仅保存内存字段，不访问外部 I/O。
        """

        self.path = path
        self._device_type = device_type
        self._firmware_version = firmware_version
        self._serial_number = serial_number
        self._open_error = open_error
        self._init_error = init_error
        self._close_error = close_error
        self.calls: list[str] = []

    def getType(self) -> str:
        """返回 fake 设备类型。

        入参：无。
        返回：构造时传入的设备类型字符串。
        错误处理：不主动抛业务异常。
        副作用：记录 `getType` 调用到内存列表。
        """

        self.calls.append("getType")
        return self._device_type

    def getPath(self) -> str:
        """返回 fake 设备路径。

        入参：无。
        返回：构造时传入的路径字符串。
        错误处理：不主动抛业务异常。
        副作用：记录 `getPath` 调用到内存列表。
        """

        self.calls.append("getPath")
        return self.path

    def open(self) -> None:
        """模拟打开 StreamDock 设备。

        入参：无。
        返回：无返回值；成功时代表设备可继续初始化。
        错误处理：若构造时提供 `open_error`，原样抛出该异常。
        副作用：记录 `open` 调用到内存列表。
        """

        self.calls.append("open")
        if self._open_error is not None:
            raise self._open_error

    def init(self) -> None:
        """模拟探针允许的初始化握手。

        入参：无。
        返回：无返回值；成功时代表设备可读取诊断信息。
        错误处理：若构造时提供 `init_error`，原样抛出该异常。
        副作用：记录 `init` 调用到内存列表；不会设置显示、LED、按键或场景。
        """

        self.calls.append("init")
        if self._init_error is not None:
            raise self._init_error

    def getFirmwareVersion(self) -> str:
        """返回 fake 固件版本。

        入参：无。
        返回：构造时传入的固件版本字符串。
        错误处理：不主动抛业务异常。
        副作用：记录 `getFirmwareVersion` 调用到内存列表。
        """

        self.calls.append("getFirmwareVersion")
        return self._firmware_version

    def getSerialNumber(self) -> str:
        """返回 fake 序列号。

        入参：无。
        返回：构造时传入的序列号字符串。
        错误处理：不主动抛业务异常。
        副作用：记录 `getSerialNumber` 调用到内存列表。
        """

        self.calls.append("getSerialNumber")
        return self._serial_number

    def close(self) -> None:
        """模拟关闭 StreamDock 设备。

        入参：无。
        返回：无返回值。
        错误处理：若构造时提供 `close_error`，原样抛出该异常；探针应吞掉该异常。
        副作用：记录 `close` 调用到内存列表。
        """

        self.calls.append("close")
        if self._close_error is not None:
            raise self._close_error

    def setKeyImage(self, *_args: object, **_kwargs: object) -> None:
        """防止探针越界渲染按键图像。

        入参：任意位置和关键字参数，代表官方 SDK 的显示设置接口。
        返回：无；本方法一旦被调用就让测试失败。
        错误处理：总是抛出 AssertionError，说明探针调用了禁止的副作用接口。
        副作用：除抛出测试异常外不修改外部状态。
        """

        raise AssertionError("probe must not render key images")

    def setBrightness(self, *_args: object, **_kwargs: object) -> None:
        """防止探针越界修改亮度或 LED。

        入参：任意位置和关键字参数，代表官方 SDK 的亮度/LED 设置接口。
        返回：无；本方法一旦被调用就让测试失败。
        错误处理：总是抛出 AssertionError，说明探针调用了禁止的副作用接口。
        副作用：除抛出测试异常外不修改外部状态。
        """

        raise AssertionError("probe must not change brightness or LED state")


class PathFailingDevice(FakeDevice):
    """让 `getPath()` 失败以覆盖路径 fallback 的 fake device。

    入参：继承 `FakeDevice` 的构造参数，其中 `path` 属性用于 fallback。
    返回：调用 `getPath()` 会失败，但 `path` 属性仍可读取的 fake device。
    错误处理：`getPath()` 固定抛出 RuntimeError。
    副作用：仅记录内存调用，不访问外部 I/O。
    """

    def getPath(self) -> str:
        """模拟官方 SDK 路径读取失败。

        入参：无。
        返回：正常情况下不返回；总是抛出 RuntimeError。
        错误处理：固定抛出 RuntimeError，探针应 fallback 到 `path` 属性。
        副作用：记录 `getPath` 调用到内存列表。
        """

        self.calls.append("getPath")
        raise RuntimeError("path unavailable")


class FakeManager:
    """返回预置 fake devices 的 StreamDock manager fake。

    入参：`devices` 是本次枚举要返回的 fake device 列表。
    返回：实例提供 `enumerate()`，满足探针 manager 协议。
    错误处理：本 fake 不主动模拟枚举失败；调用方可另写 fake 覆盖该场景。
    副作用：仅记录 `enumerate` 调用次数到内存字段。
    """

    def __init__(self, devices: list[FakeDevice]) -> None:
        """初始化 fake manager。

        入参：`devices` 是要由 `enumerate()` 返回的 fake device 序列。
        返回：无显式返回值。
        错误处理：构造阶段不主动抛业务异常。
        副作用：仅保存内存引用，不访问硬件或文件系统。
        """

        self.devices = devices
        self.enumerate_count = 0

    def enumerate(self) -> list[FakeDevice]:
        """返回当前 fake device 列表。

        入参：无。
        返回：构造时传入的 fake device 列表副本。
        错误处理：不主动抛业务异常。
        副作用：递增内存中的枚举计数。
        """

        self.enumerate_count += 1
        return list(self.devices)


def test_probe_reports_openable_initialized_device() -> None:
    """验证可打开设备会完成初始化并读取固件与序列号。

    入参：无；测试内构造一个成功 fake device。
    返回：无返回值；断言通过代表 ProbeResult 字段和调用顺序符合只读诊断契约。
    错误处理：字段错误、遗漏 close 或调用禁止接口时由 pytest 报告。
    副作用：仅修改 fake device/manager 的内存调用记录。
    """

    device = FakeDevice()
    manager = FakeManager([device])

    results = probe_streamdock_devices(manager)

    assert results == [
        ProbeResult(
            device_type="N4 Pro",
            path="fake-path",
            can_open=True,
            can_init=True,
            firmware_version="1.2.3",
            serial_number="SN123",
            error=None,
        )
    ]
    assert manager.enumerate_count == 1
    assert device.calls == [
        "getType",
        "getPath",
        "open",
        "init",
        "getFirmwareVersion",
        "getSerialNumber",
        "close",
    ]


def test_probe_reports_open_failure_without_init_or_diagnostic_reads() -> None:
    """验证设备 busy/open 失败会记录错误并跳过初始化和诊断读取。

    入参：无；测试内构造一个 open 抛错的 fake device。
    返回：无返回值；断言通过代表 open 失败不会继续 init 或读取固件序列号。
    错误处理：错误字段缺失或调用顺序越界时由 pytest 报告。
    副作用：仅修改 fake device/manager 的内存调用记录。
    """

    device = FakeDevice(open_error=RuntimeError("device busy"))

    results = probe_streamdock_devices(FakeManager([device]))

    assert results == [
        ProbeResult(
            device_type="N4 Pro",
            path="fake-path",
            can_open=False,
            can_init=False,
            firmware_version=None,
            serial_number=None,
            error="open failed: RuntimeError: device busy",
        )
    ]
    assert device.calls == ["getType", "getPath", "open"]


def test_probe_reports_init_failure_and_still_closes_device() -> None:
    """验证初始化失败会记录错误且仍尽力关闭已打开设备。

    入参：无；测试内构造一个 init 抛错的 fake device。
    返回：无返回值；断言通过代表 can_open 为 True、can_init 为 False 且 close 被调用。
    错误处理：错误字段不对或未 close 时由 pytest 报告。
    副作用：仅修改 fake device/manager 的内存调用记录。
    """

    device = FakeDevice(init_error=RuntimeError("init rejected"))

    results = probe_streamdock_devices(FakeManager([device]))

    assert results == [
        ProbeResult(
            device_type="N4 Pro",
            path="fake-path",
            can_open=True,
            can_init=False,
            firmware_version=None,
            serial_number=None,
            error="init failed: RuntimeError: init rejected",
        )
    ]
    assert device.calls == ["getType", "getPath", "open", "init", "close"]


def test_probe_swallows_close_failure_without_overwriting_success() -> None:
    """验证 close 失败会被吞掉且不会覆盖 open/init 成功诊断。

    入参：无；测试内构造一个 close 抛错但其他阶段成功的 fake device。
    返回：无返回值；断言通过代表 close 异常没有污染 ProbeResult。
    错误处理：close 异常外泄或 error 字段被覆盖时由 pytest 报告。
    副作用：仅修改 fake device/manager 的内存调用记录。
    """

    device = FakeDevice(close_error=RuntimeError("close failed"))

    results = probe_streamdock_devices(FakeManager([device]))

    assert results == [
        ProbeResult(
            device_type="N4 Pro",
            path="fake-path",
            can_open=True,
            can_init=True,
            firmware_version="1.2.3",
            serial_number="SN123",
            error=None,
        )
    ]
    assert device.calls[-1] == "close"


def test_probe_falls_back_to_path_attribute_when_get_path_fails() -> None:
    """验证 getPath 失败时会 fallback 到 path 属性。

    入参：无；测试内构造一个 `getPath()` 固定失败的 fake device。
    返回：无返回值；断言通过代表 result.path 使用 `device.path`。
    错误处理：路径为空或 getPath 异常外泄时由 pytest 报告。
    副作用：仅修改 fake device/manager 的内存调用记录。
    """

    device = PathFailingDevice(path="fallback-path")

    results = probe_streamdock_devices(FakeManager([device]))

    assert results[0].path == "fallback-path"
    assert results[0].error is None


def test_probe_uses_empty_path_when_all_path_sources_fail() -> None:
    """验证没有可用路径来源时返回空字符串。

    入参：无；测试内删除 fake device 的 `path` 属性并让 `getPath()` 失败。
    返回：无返回值；断言通过代表 `_safe_path` 的最终 fallback 是空字符串。
    错误处理：路径异常外泄或返回非空脏值时由 pytest 报告。
    副作用：仅修改 fake device/manager 的内存调用记录和测试内 fake 属性。
    """

    device = PathFailingDevice(path="fallback-path")
    del device.path

    results = probe_streamdock_devices(FakeManager([device]))

    assert results[0].path == ""


def test_probe_result_is_frozen() -> None:
    """验证 ProbeResult 是 frozen Pydantic 模型。

    入参：无；测试内构造一个最小 ProbeResult。
    返回：无返回值；断言通过代表调用方不能事后篡改诊断结果。
    错误处理：字段可被重新赋值时由 pytest 报告。
    副作用：仅触发本地模型冻结校验。
    """

    result = ProbeResult(
        device_type="N4 Pro",
        path="fake-path",
        can_open=True,
        can_init=True,
        firmware_version="1.2.3",
        serial_number="SN123",
        error=None,
    )

    with pytest.raises(Exception):
        result.can_open = False


def test_resolve_sdk_src_path_accepts_python_sdk_root(tmp_path) -> None:
    """验证 SDK path env 可指向官方 Python-SDK 根目录。

    入参：`tmp_path` 是 pytest 提供的临时目录，用于构造 fake SDK layout。
    返回：无返回值；断言通过代表根目录会被解析为其 `src` 子目录。
    错误处理：路径解析错误或误判 layout 时由 pytest 报告。
    副作用：只在 pytest 临时目录内创建空文件，不导入 SDK、不访问真实硬件。
    """

    sdk_root = tmp_path / "Python-SDK"
    streamdock_dir = sdk_root / "src" / "StreamDock"
    streamdock_dir.mkdir(parents=True)
    (streamdock_dir / "DeviceManager.py").write_text("", encoding="utf-8")

    assert _resolve_sdk_src_path(str(sdk_root)) == sdk_root / "src"


def test_resolve_sdk_src_path_accepts_python_sdk_src(tmp_path) -> None:
    """验证 SDK path env 可直接指向官方 Python-SDK/src 目录。

    入参：`tmp_path` 是 pytest 提供的临时目录，用于构造 fake SDK src layout。
    返回：无返回值；断言通过代表 `src` 目录本身会原样返回。
    错误处理：路径解析错误或误判 layout 时由 pytest 报告。
    副作用：只在 pytest 临时目录内创建空文件，不导入 SDK、不访问真实硬件。
    """

    sdk_src = tmp_path / "src"
    streamdock_dir = sdk_src / "StreamDock"
    streamdock_dir.mkdir(parents=True)
    (streamdock_dir / "DeviceManager.py").write_text("", encoding="utf-8")

    assert _resolve_sdk_src_path(str(sdk_src)) == sdk_src
