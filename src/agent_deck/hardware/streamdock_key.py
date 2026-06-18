"""真实 StreamDock N4 Pro 按键图片下发适配器。

本模块把已经渲染好的 Pillow 图像保存为临时 PNG，并通过官方 StreamDock Python SDK
写入 N4 Pro 的单个按键。它不生成 quota 内容、不维护 daemon 状态、不监听按键、不修改
Codex 配置；真实副作用仅在调用下发函数时发生：枚举 HID 设备、打开并初始化 N4 Pro、
设置指定按键图标、刷新并关闭设备。

注意：官方 SDK 的 `init()` 会唤醒屏幕、设置亮度、清空图标并刷新设备。本模块用于显式
硬件实验或后续真实渲染路径，不应被只读诊断探针调用。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Protocol

from PIL import Image
from pydantic import BaseModel, ConfigDict

from agent_deck.hardware.streamdock_probe import _prepend_sdk_path_from_env


class StreamDockKeyDeviceLike(Protocol):
    """N4 Pro 按键下发所需的官方 SDK device 最小协议。

    入参：协议类本身不接收运行时参数。
    返回：实现对象需提供 open/init/set_key_image/refresh/close/getPath。
    错误处理：协议不捕获异常；调用方负责把真实 SDK 异常转换为结果对象。
    副作用：协议声明无副作用；真实实现会访问并修改硬件按键显示。
    """

    def open(self) -> object:
        """打开设备连接。

        入参：无。
        返回：SDK 返回值；False 表示打开失败，其他值按成功处理。
        错误处理：HID/权限/占用错误可抛异常。
        副作用：真实实现会占用设备连接并启动读线程/心跳。
        """

    def init(self) -> None:
        """初始化设备以便写入按键内容。

        入参：无；必须在 open 成功后调用。
        返回：无返回值。
        错误处理：SDK 初始化失败可抛异常。
        副作用：真实实现会唤醒屏幕、设置亮度、清空图标并刷新设备。
        """

    def set_key_image(self, key: int, path: str) -> object:
        """把图片文件下发到指定按键。

        入参：`key` 是 N4 Pro 逻辑按键编号 1-15；`path` 是本地图片路径。
        返回：SDK 返回值；常见成功值可能是 None 或 0。
        错误处理：SDK 转换或传输失败可返回 -1 或抛异常。
        副作用：真实实现会修改 N4 Pro 指定按键图标。
        """

    def refresh(self) -> object:
        """刷新设备显示。

        入参：无。
        返回：SDK 返回值。
        错误处理：SDK 刷新失败可抛异常。
        副作用：真实实现会刷新设备显示内容。
        """

    def close(self, notify: bool = True) -> None:
        """关闭设备连接。

        入参：`notify` 控制 SDK 是否触发断开通知。
        返回：无返回值。
        错误处理：关闭失败可抛异常；调用方会吞掉以保留首要错误。
        副作用：释放 HID 连接和后台线程。
        """

    def getPath(self) -> str:
        """读取设备路径。

        入参：无。
        返回：设备路径字符串。
        错误处理：读取失败可抛异常，调用方会降级为空字符串。
        副作用：只读取 SDK 元数据。
        """


class StreamDockKeyManagerLike(Protocol):
    """N4 Pro 按键下发所需的官方 SDK manager 最小协议。

    入参：协议类本身不接收运行时参数。
    返回：实现对象需提供 `enumerate()`。
    错误处理：枚举异常由调用方转换为失败结果或传播。
    副作用：真实实现会访问系统 HID 枚举接口。
    """

    def enumerate(self) -> list[StreamDockKeyDeviceLike]:
        """枚举当前连接的 StreamDock 设备。

        入参：无。
        返回：device 对象列表。
        错误处理：HID 枚举失败可抛异常。
        副作用：可能访问 USB/HID 设备列表，但不应直接写显示。
        """


class StreamDockKeyRenderResult(BaseModel):
    """一次 N4 Pro 按键图片下发结果。

    入参：`ok` 表示是否成功完成 SDK 调用；`key` 是目标逻辑按键；`device_type` 和 `path`
    描述选中的设备；`sdk_result` 是 `set_key_image` 返回值的字符串化结果；`error` 是失败说明。
    返回：frozen Pydantic model，可进入 daemon status 或测试断言。
    错误处理：字段类型非法由 Pydantic 报告。
    副作用：模型自身不访问硬件或文件。
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    key: int
    device_type: str | None = None
    path: str | None = None
    sdk_result: str | None = None
    error: str | None = None


def render_key_image_to_n4pro(
    image: Image.Image,
    *,
    key: int,
    manager: StreamDockKeyManagerLike | None = None,
    temp_dir: Path | None = None,
) -> StreamDockKeyRenderResult:
    """把一张按键图标下发到首个 N4 Pro 设备的指定按键。

    入参：`image` 是调用方已经渲染好的 Pillow 图像；`key` 是 N4 Pro 逻辑按键编号 1-15；
    `manager` 可注入 fake 或官方 DeviceManager；`temp_dir` 是临时 PNG 目录，测试可传入
    pytest tmp_path。
    返回：`StreamDockKeyRenderResult`，成功时包含设备类型、路径、按键编号和 SDK 返回值。
    错误处理：非法 key、未发现 N4 Pro、open 返回 False、SDK 返回 -1 或任一 SDK 异常都会
    返回 `ok=False` 的结果；临时文件会尽力清理。
    副作用：无 manager 时懒加载官方 SDK 并枚举真实设备；成功选中 N4 Pro 后会 open/init、
    set key image、refresh 并 close(notify=False)。
    """

    if key not in range(1, 16):
        return StreamDockKeyRenderResult(
            ok=False,
            key=key,
            error="key must be in range 1..15",
        )

    active_manager = manager if manager is not None else _load_default_manager()
    device = _first_n4pro_device(active_manager.enumerate())
    if device is None:
        return StreamDockKeyRenderResult(
            ok=False,
            key=key,
            error="no N4 Pro device found",
        )

    device_type = type(device).__name__
    path = _safe_path(device)
    opened = False
    try:
        open_result = device.open()
        if open_result is False:
            return StreamDockKeyRenderResult(
                ok=False,
                key=key,
                device_type=device_type,
                path=path,
                error="open failed: SDK returned false",
            )
        opened = True
        device.init()
        image_path = _save_temp_png(image, temp_dir=temp_dir)
        try:
            sdk_result = device.set_key_image(key, str(image_path))
        finally:
            image_path.unlink(missing_ok=True)
        if sdk_result == -1:
            return StreamDockKeyRenderResult(
                ok=False,
                key=key,
                device_type=device_type,
                path=path,
                sdk_result=str(sdk_result),
                error="set_key_image failed: SDK returned -1",
            )
        device.refresh()
        return StreamDockKeyRenderResult(
            ok=True,
            key=key,
            device_type=device_type,
            path=path,
            sdk_result=str(sdk_result),
        )
    except Exception as exc:
        return StreamDockKeyRenderResult(
            ok=False,
            key=key,
            device_type=device_type,
            path=path,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if opened:
            try:
                device.close(notify=False)
            except Exception:
                pass


def _load_default_manager() -> StreamDockKeyManagerLike:
    """懒加载官方 StreamDock DeviceManager。

    入参：无。
    返回：新建的官方 `DeviceManager`。
    错误处理：SDK 不可导入、底层动态库不可加载或构造失败时异常传播给调用方。
    副作用：可能修改 `sys.path` 以支持 `AGENT_DECK_STREAMDOCK_SDK_PATH`，并加载官方 SDK。
    """

    _prepend_sdk_path_from_env()
    from StreamDock.DeviceManager import DeviceManager

    return DeviceManager()


def _first_n4pro_device(
    devices: list[StreamDockKeyDeviceLike],
) -> StreamDockKeyDeviceLike | None:
    """从枚举结果里选择第一个 N4 Pro 设备。

    入参：`devices` 是 SDK manager 返回的 device 列表。
    返回：类名包含 `N4Pro` 的首个设备；没有时返回 None。
    错误处理：本函数不主动抛异常。
    副作用：无；只读取内存对象类型名。
    """

    for device in devices:
        if "N4Pro" in type(device).__name__:
            return device
    return None


def _save_temp_png(image: Image.Image, *, temp_dir: Path | None) -> Path:
    """把 Pillow 图像保存为 SDK 可读取的临时 PNG。

    入参：`image` 是待下发图像；`temp_dir` 是可选临时目录。
    返回：已写入的 PNG 路径。
    错误处理：目录不可写或 Pillow 保存失败时异常传播。
    副作用：在临时目录创建一个 `.png` 文件；调用方负责删除。
    """

    directory = Path(temp_dir) if temp_dir is not None else None
    handle = tempfile.NamedTemporaryFile(
        prefix="agent-deck-n4pro-key-",
        suffix=".png",
        dir=directory,
        delete=False,
    )
    path = Path(handle.name)
    handle.close()
    try:
        image.convert("RGB").save(path, format="PNG")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _safe_path(device: StreamDockKeyDeviceLike) -> str:
    """读取设备路径，失败时降级为空字符串。

    入参：`device` 是 SDK device。
    返回：设备路径字符串或空字符串。
    错误处理：`getPath()` 异常被吞掉，避免覆盖主流程错误。
    副作用：只读取 SDK 元数据。
    """

    try:
        return device.getPath()
    except Exception:
        value = getattr(device, "path", "")
        return value if isinstance(value, str) else ""
