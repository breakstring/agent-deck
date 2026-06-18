"""StreamDock N4 Pro 多 surface 统一渲染适配器。

本模块把 N4 Pro 的背景层和按键图像放在同一次 SDK 设备会话里写入。它不生成业务图像、
不读取 Codex、不维护 daemon 状态、不监听输入；真实副作用仅在调用 writer 时发生：
枚举 HID 设备、open/init 一次、按顺序写 frame background 和 key images、refresh 并 close。

实测 N4 Pro 上不能把触屏背景和按键图拆成两个独立的 open/init/close sink 来写，否则后一次
`init()` 或 legacy `set_touchscreen_image` 可能清掉/压住另一层显示。本模块统一使用
`set_frame_background` 写 800x480 背景层，再写按键图，作为后续真实 daemon renderer 的
推荐入口。
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from agent_deck.hardware.streamdock_probe import _prepend_sdk_path_from_env


class StreamDockN4ProDeviceLike(Protocol):
    """统一 N4 Pro writer 所需的官方 SDK device 最小协议。

    入参：协议类本身不接收运行时参数。
    返回：实现对象需提供 open/init/set_frame_background/set_key_image/refresh/close/getPath。
    错误处理：协议不捕获异常；调用方负责把真实 SDK 异常转换为结果对象。
    副作用：协议声明无副作用；真实实现会访问并修改硬件显示。
    """

    def open(self) -> object:
        """打开设备连接。

        入参：无。
        返回：SDK 返回值；False 表示打开失败，其他值按成功处理。
        错误处理：HID/权限/占用错误可抛异常。
        副作用：真实实现会占用设备连接并启动读线程/心跳。
        """

    def init(self) -> None:
        """初始化设备以便写入多个显示 surface。

        入参：无；必须在 open 成功后调用。
        返回：无返回值。
        错误处理：SDK 初始化失败可抛异常。
        副作用：真实实现会唤醒屏幕、设置亮度并刷新设备；本 writer 只调用一次。
        """

    def set_frame_background(self, path: str) -> object:
        """把图片文件下发到 N4 Pro 的临时 frame 背景层。

        入参：`path` 是本地 JPEG 图片路径。
        返回：SDK 返回值；常见成功值可能是 None 或 0。
        错误处理：SDK 转换或传输失败可返回 -1 或抛异常。
        副作用：真实实现会修改 N4 Pro 背景层；该接口实测可与按键图层同时显示。
        """

    def set_key_image(self, key: int, path: str) -> object:
        """把图片文件下发到指定按键。

        入参：`key` 是 N4 Pro 逻辑按键编号 1-15；`path` 是本地 PNG 图片路径。
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


class StreamDockN4ProManagerLike(Protocol):
    """统一 N4 Pro writer 所需的官方 SDK manager 最小协议。

    入参：协议类本身不接收运行时参数。
    返回：实现对象需提供 `enumerate()`。
    错误处理：枚举异常由调用方转换为失败结果或传播。
    副作用：真实实现会访问系统 HID 枚举接口。
    """

    def enumerate(self) -> list[StreamDockN4ProDeviceLike]:
        """枚举当前连接的 StreamDock 设备。

        入参：无。
        返回：device 对象列表。
        错误处理：HID 枚举失败可抛异常。
        副作用：可能访问 USB/HID 设备列表，但不应直接写显示。
        """


class StreamDockN4ProRenderResult(BaseModel):
    """一次 N4 Pro 多 surface 写入结果。

    入参：`ok` 表示所有请求的 surface 是否写入成功；`device_type` 和 `path` 描述选中的
    设备；`background_result` 是 frame background 返回值；`key_results` 记录每个 key 的
    SDK 返回值；`refresh_result` 是 refresh 返回值；`error` 是失败说明。
    返回：frozen Pydantic model，可进入 daemon status 或测试断言。
    错误处理：字段类型非法由 Pydantic 报告。
    副作用：模型自身不访问硬件或文件。
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    device_type: str | None = None
    path: str | None = None
    background_result: str | None = None
    key_results: dict[int, str] = Field(default_factory=dict)
    refresh_result: str | None = None
    error: str | None = None


def render_images_to_n4pro(
    *,
    background_image: Image.Image | None = None,
    key_images: Mapping[int, Image.Image] | None = None,
    manager: StreamDockN4ProManagerLike | None = None,
    temp_dir: Path | None = None,
) -> StreamDockN4ProRenderResult:
    """在同一次 N4 Pro 设备会话里写背景层和按键图。

    入参：`background_image` 是可选 800x480 背景图，写入 `set_frame_background`；`key_images`
    是逻辑 key 到 Pillow 图像的映射，key 必须在 1-15；`manager` 可注入 fake 或官方
    DeviceManager；`temp_dir` 是临时文件目录，测试可传入 pytest tmp_path。
    返回：`StreamDockN4ProRenderResult`，成功时包含背景和各 key 的 SDK 返回值。
    错误处理：没有任何 surface、非法 key、找不到 N4 Pro、open false、任一 SDK 返回 -1 或
    抛异常都会返回 `ok=False`；临时文件会尽力清理。
    副作用：无 manager 时懒加载官方 SDK 并枚举真实设备；成功选中 N4 Pro 后会 open/init、
    set frame background、set key images、refresh 并 close(notify=False)。
    """

    normalized_key_images = dict(key_images or {})
    invalid_keys = sorted(key for key in normalized_key_images if key not in range(1, 16))
    if invalid_keys:
        return StreamDockN4ProRenderResult(
            ok=False,
            error=f"keys must be in range 1..15: {invalid_keys}",
        )
    if background_image is None and not normalized_key_images:
        return StreamDockN4ProRenderResult(
            ok=False,
            error="at least one background or key image is required",
        )

    active_manager = manager if manager is not None else _load_default_manager()
    device = _first_n4pro_device(active_manager.enumerate())
    if device is None:
        return StreamDockN4ProRenderResult(
            ok=False,
            error="no N4 Pro device found",
        )

    device_type = type(device).__name__
    path = _safe_path(device)
    opened = False
    temp_paths: list[Path] = []
    try:
        open_result = device.open()
        if open_result is False:
            return StreamDockN4ProRenderResult(
                ok=False,
                device_type=device_type,
                path=path,
                error="open failed: SDK returned false",
            )
        opened = True
        device.init()

        background_result: object | None = None
        if background_image is not None:
            background_path = _save_temp_jpeg(background_image, temp_dir=temp_dir)
            temp_paths.append(background_path)
            background_result = device.set_frame_background(str(background_path))
            if background_result == -1:
                return StreamDockN4ProRenderResult(
                    ok=False,
                    device_type=device_type,
                    path=path,
                    background_result=str(background_result),
                    error="set_frame_background failed: SDK returned -1",
                )

        key_results: dict[int, str] = {}
        for key, image in normalized_key_images.items():
            key_path = _save_temp_png(image, temp_dir=temp_dir)
            temp_paths.append(key_path)
            key_result = device.set_key_image(key, str(key_path))
            key_results[key] = str(key_result)
            if key_result == -1:
                return StreamDockN4ProRenderResult(
                    ok=False,
                    device_type=device_type,
                    path=path,
                    background_result=_stringify_optional(background_result),
                    key_results=key_results,
                    error=f"set_key_image failed for key {key}: SDK returned -1",
                )

        refresh_result = device.refresh()
        return StreamDockN4ProRenderResult(
            ok=True,
            device_type=device_type,
            path=path,
            background_result=_stringify_optional(background_result),
            key_results=key_results,
            refresh_result=str(refresh_result),
        )
    except Exception as exc:
        return StreamDockN4ProRenderResult(
            ok=False,
            device_type=device_type,
            path=path,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        for temp_path in temp_paths:
            temp_path.unlink(missing_ok=True)
        if opened:
            try:
                device.close(notify=False)
            except Exception:
                pass


def _load_default_manager() -> StreamDockN4ProManagerLike:
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
    devices: list[StreamDockN4ProDeviceLike],
) -> StreamDockN4ProDeviceLike | None:
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


def _save_temp_jpeg(image: Image.Image, *, temp_dir: Path | None) -> Path:
    """把 Pillow 图像保存为 SDK 可读取的临时 JPEG。

    入参：`image` 是待下发背景图；`temp_dir` 是可选临时目录。
    返回：已写入的 JPEG 路径。
    错误处理：目录不可写或 Pillow 保存失败时异常传播。
    副作用：在临时目录创建一个 `.jpg` 文件；调用方负责删除。
    """

    directory = Path(temp_dir) if temp_dir is not None else None
    handle = tempfile.NamedTemporaryFile(
        prefix="agent-deck-n4pro-frame-",
        suffix=".jpg",
        dir=directory,
        delete=False,
    )
    path = Path(handle.name)
    handle.close()
    try:
        image.convert("RGB").save(path, format="JPEG", quality=90)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _save_temp_png(image: Image.Image, *, temp_dir: Path | None) -> Path:
    """把 Pillow 图像保存为 SDK 可读取的临时 PNG。

    入参：`image` 是待下发按键图；`temp_dir` 是可选临时目录。
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


def _safe_path(device: StreamDockN4ProDeviceLike) -> str:
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


def _stringify_optional(value: object | None) -> str | None:
    """把可空 SDK 返回值转为可序列化字符串。

    入参：`value` 是 SDK 返回值，可为 None。
    返回：None 保持 None，其余值转为 `str(value)`。
    错误处理：无。
    副作用：无。
    """

    return None if value is None else str(value)
