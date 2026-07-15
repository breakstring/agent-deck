"""StreamDock vendored transport 返回码透传测试。

这些测试以 fake native library 验证 legacy N4 Pro 调用链会保留 TransportResult，避免底层
HID 写失败只打印日志却被 Agent Deck 误判为成功；不会枚举、打开或写入真实硬件。
"""

from __future__ import annotations

import importlib
import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest import MonkeyPatch
from StreamDock.Transport.LibUSBHIDAPI import LibUSBHIDAPI


def test_legacy_n4pro_transport_methods_preserve_native_failure_codes(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """背景、刷新、亮度和心跳 legacy 方法应返回 native 失败码。

    入参：`monkeypatch` 替换 vendored module 的 native library；`tmp_path` 提供最小背景文件。
    返回：无返回值；断言通过表示 `0xFFFFFFFF` 能抵达 Agent Deck renderer。
    错误处理：任一 wrapper 吞掉返回值时由 pytest 报告。
    副作用：只写 pytest 临时文件并修改当前测试进程内 module 引用，不访问真实 HID。
    """

    module = importlib.import_module("StreamDock.Transport.LibUSBHIDAPI")
    failure = 0xFFFFFFFF
    native = SimpleNamespace(
        transport_refresh=lambda _handle: failure,
        transport_set_background_frame_stream=lambda *_args: failure,
        transport_set_key_brightness=lambda *_args: failure,
        transport_heartbeat=lambda _handle: failure,
    )
    monkeypatch.setattr(module, "_transport_lib", native)
    transport = LibUSBHIDAPI()
    transport._handle = 1
    background = tmp_path / "background.jpg"
    background.write_bytes(b"fake-jpeg")

    try:
        assert transport.setBackgroundImgFrame(background, 800, 480) == failure
        assert transport.refresh() == failure
        assert transport.setBrightness(50) == failure
        assert transport.heartbeat() == failure
    finally:
        transport._handle = None


def test_send_handshake_writes_full_han_report_and_closes_handle(
    monkeypatch: MonkeyPatch,
) -> None:
    """握手必须写入 report id、HAN 和零填充，并在成功后关闭临时句柄。

    入参：`monkeypatch` 替换 raw HID API；不需要临时文件。
    返回：无返回值；断言通过表示 N4 Pro 握手包和资源释放符合协议。
    错误处理：包长度、内容或 close 顺序错误时由 pytest 报告。
    副作用：只修改当前测试进程内 module 引用，不访问真实 HID。
    """

    module = importlib.import_module("StreamDock.Transport.LibUSBHIDAPI")
    calls: list[tuple[str, object]] = []

    def write(_handle: int, packet: object, size: int) -> int:
        payload = ctypes.string_at(packet, size)
        calls.append(("write", payload))
        return size

    native = SimpleNamespace(
        hid_open_path=lambda path: calls.append(("open", path)) or 123,
        hid_write=write,
        hid_error=lambda _handle: "Success",
        hid_close=lambda handle: calls.append(("close", handle)),
    )
    monkeypatch.setattr(module, "_transport_lib", native)
    monkeypatch.setattr(module, "_RAW_HID_WRITE_AVAILABLE", True)

    result = LibUSBHIDAPI.send_handshake("DevSrvsID:test")

    assert result == 0
    assert calls[0] == ("open", b"DevSrvsID:test")
    assert calls[1][0] == "write"
    payload = calls[1][1]
    assert isinstance(payload, bytes)
    assert len(payload) == 1025
    assert payload[:4] == b"\x00HAN"
    assert payload[4:] == bytes(1021)
    assert calls[2] == ("close", 123)


def test_send_handshake_surfaces_hid_permission_error(
    monkeypatch: MonkeyPatch,
) -> None:
    """raw HID 无权限时握手必须失败，不能继续伪报 transport open 成功。

    入参：`monkeypatch` 模拟 macOS `not permitted` 打开失败。
    返回：无返回值；断言通过表示底层权限错误抵达 renderer。
    错误处理：没有抛出 OSError 或错误文本丢失时由 pytest 报告。
    副作用：只修改当前测试进程内 module 引用，不访问真实 HID。
    """

    module = importlib.import_module("StreamDock.Transport.LibUSBHIDAPI")
    native = SimpleNamespace(
        hid_open_path=lambda _path: None,
        hid_error=lambda _handle: "not permitted",
    )
    monkeypatch.setattr(module, "_transport_lib", native)
    monkeypatch.setattr(module, "_RAW_HID_WRITE_AVAILABLE", True)

    with pytest.raises(OSError, match="not permitted"):
        LibUSBHIDAPI.send_handshake("DevSrvsID:test")
