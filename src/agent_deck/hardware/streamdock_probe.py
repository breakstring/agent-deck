"""真实 StreamDock 设备的只读诊断探针。

本模块只负责枚举官方 SDK 暴露的 StreamDock 设备，逐个执行 open，
读取固件版本与序列号，并用 `notify=False` 尽力 close。它不调用官方 SDK 的
`init()`，因为该方法会清空图标、设置亮度并刷新屏幕；也不渲染图片、不修改 LED、
不设置按键、不切换场景，不持有长期硬件连接。

关键副作用：无注入 manager 时会懒加载官方 StreamDock SDK；探针运行期间会短暂
打开并关闭 HID 设备。枚举错误保持传播，单设备 open/read 错误会写入
`ProbeResult.error`，close 错误会被吞掉以保留首要诊断信息。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

_SDK_PATH_ENV = "AGENT_DECK_STREAMDOCK_SDK_PATH"


class StreamDockDeviceLike(Protocol):
    """StreamDock 诊断探针使用的官方 SDK device 最小协议。

    入参：协议类本身不接收运行时入参；实现对象通常来自 `DeviceManager.enumerate()`。
    返回：实现对象需提供 open/init/close、路径读取，以及固件与序列号读取能力。
    错误处理：协议不捕获异常；具体方法失败时由 `probe_streamdock_devices` 捕获或传播。
    副作用：协议声明本身无副作用；实现方法可能短暂访问 HID 设备。
    """

    firmware_version: str
    serial_number: str

    def open(self) -> None:
        """打开设备以便执行后续诊断。

        入参：无。
        返回：无返回值或 truthy 值代表成功；False/None 代表官方 SDK 未能打开设备。
        错误处理：设备 busy、权限不足或传输层失败时允许抛出异常，由探针记录为 open 失败。
        副作用：可能短暂占用 HID 设备连接；不得修改屏幕、LED、按键或场景。
        """

    def init(self) -> None:
        """初始化已打开的设备连接。

        入参：无；调用前必须已经成功 `open()`。
        返回：无返回值；成功后调用方可读取固件和序列号。
        错误处理：初始化失败时允许抛出异常，由探针记录为 init 失败。
        副作用：官方 SDK 的真实实现会清屏、改亮度和刷新屏幕；安全探针默认不会调用它，
        此协议声明只保留给 fake 或后续显式接管设备流程。
        """

    def close(self) -> None:
        """关闭已打开的设备连接。

        入参：无。
        返回：无返回值。
        错误处理：关闭失败时允许抛出异常；探针会吞掉该异常，避免覆盖 open/init 诊断。
        副作用：释放 HID 设备连接；不得修改显示、LED、按键或场景配置。
        """

    def getPath(self) -> str:
        """读取 SDK 暴露的设备路径。

        入参：无。
        返回：设备路径字符串，用于诊断和后续人工识别。
        错误处理：路径读取失败时允许抛出异常；探针会 fallback 到 `path` 属性或空字符串。
        副作用：只读取设备对象元数据，不访问显示、LED、按键或场景。
        """


class StreamDockManagerLike(Protocol):
    """StreamDock 诊断探针使用的官方 SDK manager 最小协议。

    入参：协议类本身不接收运行时入参；实现对象通常是官方 `DeviceManager()`。
    返回：实现对象需提供 `enumerate()`，返回当前可见的 StreamDock 设备列表。
    错误处理：枚举异常不在本协议层处理；探针会让枚举异常向调用方传播。
    副作用：协议声明本身无副作用；具体枚举可能访问 USB/HID 设备列表。
    """

    def enumerate(self) -> list[StreamDockDeviceLike]:
        """枚举当前连接的 StreamDock 设备。

        入参：无。
        返回：当前 manager 能发现的 device 对象列表。
        错误处理：USB/HID 枚举失败时允许抛出异常，并由探针向调用方传播。
        副作用：可能访问系统 USB/HID 枚举接口，但不打开设备、不修改设备状态。
        """


class ProbeResult(BaseModel):
    """单个 StreamDock 设备的诊断结果。

    入参：`device_type` 是设备型号或类名；`path` 是可用于识别的设备路径；
        `can_open` 表示 open 是否成功；`can_init` 表示本次探针是否执行并通过 SDK init；
        安全诊断模式不会调用 init，因此成功只读探针也会返回 `can_init=False`；
        `firmware_version` 和 `serial_number` 是成功初始化后读取到的诊断元数据；
    `error` 是首个 open/init/read 失败的阶段化错误字符串。
    返回：frozen Pydantic model，可比较、可序列化，并防止调用方事后篡改诊断事实。
    错误处理：字段类型不匹配时由 Pydantic 抛出 ValidationError。
    副作用：仅保存内存数据，不访问硬件、网络、文件系统或全局状态。
    """

    model_config = ConfigDict(frozen=True)

    device_type: str
    path: str
    can_open: bool
    can_init: bool
    firmware_version: str | None = None
    serial_number: str | None = None
    error: str | None = None


def _load_default_manager() -> StreamDockManagerLike:
    """懒加载官方 StreamDock SDK manager。

    入参：无。
    返回：新建的官方 `DeviceManager` 实例，满足 `StreamDockManagerLike` 协议；当
    `AGENT_DECK_STREAMDOCK_SDK_PATH` 指向官方 SDK 的 `Python-SDK` 或 `Python-SDK/src`
    时，会优先从该路径导入。
    错误处理：SDK 未安装、动态库不可加载、env path 非目录或构造 manager 失败时异常
    原样传播给调用方；探针不会在导入本模块时触发这些错误。
    副作用：首次调用时可能把 env 指定的 SDK `src` 目录插入 `sys.path`，随后导入
    `StreamDock.DeviceManager`，并可能加载官方 SDK 的底层库。
    """

    _prepend_sdk_path_from_env()
    from StreamDock.DeviceManager import DeviceManager

    return DeviceManager()


def _prepend_sdk_path_from_env() -> None:
    """将 env 指定的官方 SDK 源码目录插入 import 搜索路径。

    入参：无；从 `AGENT_DECK_STREAMDOCK_SDK_PATH` 读取用户显式配置，可指向
    `Python-SDK` 根目录或 `Python-SDK/src`。
    返回：无显式返回值；没有设置 env 时不做任何事。
    错误处理：env 路径不存在或不是目录时抛出 FileNotFoundError；无法识别为 SDK
    源码布局时抛出 RuntimeError。
    副作用：可能修改当前 Python 进程的 `sys.path`，以便优先导入 macOS 可用的官方 SDK。
    """

    configured_path = os.environ.get(_SDK_PATH_ENV)
    if not configured_path:
        return

    sdk_src_path = _resolve_sdk_src_path(configured_path)
    sdk_src = str(sdk_src_path)
    if sdk_src not in sys.path:
        sys.path.insert(0, sdk_src)


def _resolve_sdk_src_path(configured_path: str) -> Path:
    """解析 `AGENT_DECK_STREAMDOCK_SDK_PATH` 指向的 SDK src 目录。

    入参：`configured_path` 是用户提供的文件系统路径，可为官方仓库的 `Python-SDK`
    根目录，也可直接为 `Python-SDK/src`。
    返回：包含 `StreamDock/DeviceManager.py` 的绝对 `Path`。
    错误处理：路径不是目录时抛出 FileNotFoundError；目录内找不到 SDK 包时抛出 RuntimeError。
    副作用：只解析和检查本地路径，不导入 SDK、不打开硬件、不修改外部文件。
    """

    base_path = Path(configured_path).expanduser().resolve()
    if not base_path.is_dir():
        raise FileNotFoundError(f"{_SDK_PATH_ENV} is not a directory: {base_path}")

    candidates = (base_path, base_path / "src")
    for candidate in candidates:
        if (candidate / "StreamDock" / "DeviceManager.py").is_file():
            return candidate

    raise RuntimeError(
        f"{_SDK_PATH_ENV} must point to Python-SDK or Python-SDK/src: {base_path}"
    )


def probe_streamdock_devices(
    manager: StreamDockManagerLike | None = None,
) -> list[ProbeResult]:
    """枚举并诊断 StreamDock 设备，不修改显示、LED、按键或场景。

    入参：`manager` 可注入 fake 或官方 SDK manager；为 None 时懒加载并实例化官方
    `DeviceManager`。该 manager 必须提供 `enumerate()`。
    返回：每个枚举设备对应一个 `ProbeResult`；设备级 open/read 失败会以 `error`
    字段呈现，不阻断后续设备诊断。安全模式不会调用 SDK init，因此成功结果的
    `can_init` 仍为 False。
    错误处理：manager 枚举错误会原样传播；open/read 错误被捕获到对应 result；
    每个设备都会在 finally 中尽力 close，close 错误被吞掉且不覆盖原诊断。
    副作用：可能短暂打开并关闭真实 HID 设备；绝不调用渲染、显示、LED、
    按键映射或场景配置类方法。
    """

    active_manager = manager if manager is not None else _load_default_manager()
    results: list[ProbeResult] = []

    for device in active_manager.enumerate():
        device_type = _safe_device_type(device)
        path = _safe_path(device)
        opened = False

        try:
            open_result = device.open()
        except Exception as exc:
            results.append(
                ProbeResult(
                    device_type=device_type,
                    path=path,
                    can_open=False,
                    can_init=False,
                    error=_format_error("open", exc),
                )
            )
        else:
            if open_result is False or open_result is None:
                results.append(
                    ProbeResult(
                        device_type=device_type,
                        path=path,
                        can_open=False,
                        can_init=False,
                        error="open failed: SDK returned false",
                    )
                )
            else:
                opened = True
                try:
                    firmware_version = _read_metadata(device, "firmware")
                    serial_number = _read_metadata(device, "serial")
                except Exception as exc:
                    results.append(
                        ProbeResult(
                            device_type=device_type,
                            path=path,
                            can_open=True,
                            can_init=False,
                            error=_format_error("read", exc),
                        )
                    )
                else:
                    results.append(
                        ProbeResult(
                            device_type=device_type,
                            path=path,
                            can_open=True,
                            can_init=False,
                            firmware_version=firmware_version,
                            serial_number=serial_number,
                        )
                    )
        finally:
            try:
                _close_device_quietly(device, opened=opened)
            except Exception:
                pass

    return results


def _close_device_quietly(device: StreamDockDeviceLike, *, opened: bool) -> None:
    """尽力关闭设备，并避免官方 SDK 的断开通知副作用。

    入参：`device` 是刚尝试 open 的 SDK 或 fake device；`opened` 表示 open 是否明确成功。
    `opened` 目前仅作为调用者意图的显式记录；即使 open 失败也会尝试 close。
    返回：无显式返回值。
    错误处理：close 抛出的任何异常都由本函数吞掉，避免覆盖首要诊断错误。
    副作用：可能释放 HID transport；若设备 close 支持 `notify=False`，会显式传入 False，
    避免官方 SDK 默认发送 disconnect/clear 类设备命令。open 失败时也会尝试 close，
    用于释放半打开资源。
    """

    try:
        close = device.close
        try:
            close(notify=False)  # type: ignore[call-arg]
        except TypeError:
            close()
    except Exception:
        return


def _safe_path(device: Any) -> str:
    """尽力读取设备路径，优先使用 SDK 的 `getPath()`。

    入参：`device` 是任意 SDK 或 fake device 对象。
    返回：优先返回 `device.getPath()` 的字符串化结果；若调用失败或不存在，则返回
    `device.path` 属性的字符串化结果；仍不可用时返回空字符串。
    错误处理：`getPath()` 和 `path` 属性访问异常都会被吞掉，保证路径读取不阻断诊断。
    副作用：只读取对象元数据，不打开设备、不访问外部文件或网络。
    """

    get_path = getattr(device, "getPath", None)
    if callable(get_path):
        try:
            return str(get_path() or "")
        except Exception:
            pass

    try:
        return str(getattr(device, "path") or "")
    except Exception:
        return ""


def _safe_device_type(device: Any) -> str:
    """尽力读取设备型号，失败时使用类名。

    入参：`device` 是任意 SDK 或 fake device 对象。
    返回：优先返回 `getType()` 或 `device_type`/`deck_type`/`DECK_TYPE` 属性；
    若都不可用，则返回 Python 类名。
    错误处理：各读取路径异常都会被吞掉，避免设备型号缺失阻断 open/init 诊断。
    副作用：只读取对象元数据，不访问硬件显示、LED、按键或场景配置。
    """

    get_type = getattr(device, "getType", None)
    if callable(get_type):
        try:
            value = get_type()
            if value:
                return str(value)
        except Exception:
            pass

    for attr_name in ("device_type", "deck_type", "DECK_TYPE"):
        try:
            value = getattr(device, attr_name)
        except Exception:
            continue
        if value:
            return str(value)

    return type(device).__name__


def _read_metadata(device: Any, kind: str) -> str | None:
    """读取固件版本或序列号诊断字段。

    入参：`device` 是已成功 open/init 的 SDK 或 fake device；`kind` 只能是
    `"firmware"` 或 `"serial"`，用于选择读取固件版本或序列号。
    返回：读取到的字符串化值；SDK 返回 None 或空值时返回 None。
    错误处理：未知 `kind` 抛出 ValueError；底层 getter/属性访问异常原样传播给调用方，
    由 `probe_streamdock_devices` 记录为 read 失败。
    副作用：只调用只读元数据接口或读取属性，不渲染、不改 LED、不改按键、不改场景。
    """

    if kind == "firmware":
        method_names = ("getFirmwareVersion",)
        attr_names = ("firmware_version", "firmwareVersion")
        fallback_method_names = ("get_firmware_version",)
    elif kind == "serial":
        method_names = ("getSerialNumber",)
        attr_names = ("serial_number", "serialNumber")
        fallback_method_names = ("get_serial_number",)
    else:
        raise ValueError(f"unsupported metadata kind: {kind}")

    for method_name in method_names:
        method = getattr(device, method_name, None)
        if callable(method):
            value = method()
            return str(value) if value else None

    for attr_name in attr_names:
        value = getattr(device, attr_name, None)
        if value:
            return str(value)

    for method_name in fallback_method_names:
        method = getattr(device, method_name, None)
        if callable(method):
            value = method()
            return str(value) if value else None

    return None


def _format_error(stage: str, exc: Exception) -> str:
    """格式化设备级诊断错误。

    入参：`stage` 是 open/init/read 等失败阶段；`exc` 是该阶段捕获到的异常对象。
    返回：稳定的人类可读字符串，包含阶段、异常类型和异常消息。
    错误处理：异常对象的字符串化若失败，则按 Python 异常传播；当前调用点不预期此情况。
    副作用：仅格式化内存对象，不访问硬件、网络或文件系统。
    """

    return f"{stage} failed: {type(exc).__name__}: {exc}"
