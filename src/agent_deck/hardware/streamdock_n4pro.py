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
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, Protocol

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


class StreamDockN4ProAnimationResult(BaseModel):
    """一次 N4 Pro 长连接按键动画预览结果。

    入参：`ok` 表示动画循环是否完整执行；`device_type`/`path` 描述选中的设备；
    `background_result` 是启动时写入 frame background 的 SDK 返回值；`frames_rendered`
    是已刷新帧数；`key_count` 是参与动画的按键数；`timing_seconds` 是成功路径的阶段耗时；
    `error` 是失败说明。
    返回：frozen Pydantic model，可由 CLI 输出 JSON 或测试断言。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：模型自身不访问硬件或文件。
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    device_type: str | None = None
    path: str | None = None
    background_result: str | None = None
    frames_rendered: int = 0
    key_count: int = 0
    timing_seconds: dict[str, float] = Field(default_factory=dict)
    error: str | None = None


class StreamDockN4ProPersistentAnimator:
    """daemon 专用的 N4 Pro 长连接按键动画 sink。

    入参：`manager` 可注入 fake 或官方 DeviceManager；`temp_dir` 控制临时背景 JPEG 目录；
    `sleep` 和 `monotonic` 仅供测试替换。实例会在第一次调用时 open/init N4 Pro，并在后续
    调用中复用同一个设备会话，直到显式调用 `close()`。
    返回：实例本身是 callable，签名兼容 `animate_key_images_on_n4pro`。
    错误处理：设备枚举/open/init 或 SDK 下发失败时返回 `ok=False`；显式 close 会吞掉 SDK
    close 异常，避免 daemon shutdown 失败。
    副作用：调用时可能打开并长期持有真实 N4 Pro；`close()` 会释放该会话。
    """

    def __init__(
        self,
        *,
        manager: StreamDockN4ProManagerLike | None = None,
        temp_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化 persistent animator，但不立即访问硬件。

        入参：`manager` 是可选设备管理器；`temp_dir` 是临时背景文件目录；
        `sleep`/`monotonic` 用于帧节奏和诊断。
        返回：无显式返回值。
        错误处理：构造阶段不访问 SDK，不抛硬件错误。
        副作用：仅保存依赖到实例字段。
        """

        self._manager = manager
        self._temp_dir = temp_dir
        self._sleep = sleep
        self._monotonic = monotonic
        self._device: StreamDockN4ProDeviceLike | None = None
        self._device_type: str | None = None
        self._path: str | None = None

    def __call__(
        self,
        *,
        background_image: Image.Image | None,
        key_frame_paths: Mapping[int, tuple[Path, ...]],
        duration_seconds: float,
        fps: int,
    ) -> StreamDockN4ProAnimationResult:
        """播放一轮按键动画，复用已经打开的 N4 Pro 会话。

        入参：`background_image` 是可选 800x480 背景图；`key_frame_paths` 是 key 到帧 PNG
        路径的映射；`duration_seconds` 是本轮播放窗口；`fps` 是目标帧率。
        返回：`StreamDockN4ProAnimationResult`，成功结果包含 timing 诊断；persistent 模式下
        `close` 阶段耗时固定为 0，真实 close 只在 `close()` 中发生。
        错误处理：非法参数、设备不可用或 SDK 返回 -1 时返回 `ok=False`；异常会关闭当前会话
        并作为错误结果返回。
        副作用：可能首次 open/init 真实设备，每轮写 frame background、set key image 和 refresh。
        """

        validation_error = _validate_animation_inputs(
            duration_seconds=duration_seconds,
            fps=fps,
            key_frame_paths=key_frame_paths,
            require_surface=background_image is not None,
        )
        if validation_error is not None:
            return validation_error
        normalized_frames = {
            key: tuple(paths) for key, paths in key_frame_paths.items()
        }
        frame_budget = max(1, int(round(duration_seconds * fps)))
        frame_interval = 1.0 / fps
        timing: dict[str, float] = {}
        started_at = self._monotonic()
        temp_paths: list[Path] = []
        try:
            device = self._ensure_open_device(timing=timing, started_at=started_at)
            if isinstance(device, StreamDockN4ProAnimationResult):
                return device

            background_result: object | None = None
            after_open_init = self._monotonic()
            after_background = after_open_init
            if background_image is not None:
                background_path = _save_temp_jpeg(background_image, temp_dir=self._temp_dir)
                temp_paths.append(background_path)
                background_result = device.set_frame_background(str(background_path))
                after_background = self._monotonic()
                timing["background"] = _elapsed_seconds(
                    after_open_init,
                    after_background,
                )
                if background_result == -1:
                    self.close()
                    return StreamDockN4ProAnimationResult(
                        ok=False,
                        device_type=self._device_type,
                        path=self._path,
                        background_result=str(background_result),
                        error="set_frame_background failed: SDK returned -1",
                    )
            else:
                timing["background"] = 0.0

            frames_rendered = 0
            playback_started_at = after_background
            next_frame_at = playback_started_at
            for frame_index in range(frame_budget):
                for key, paths in normalized_frames.items():
                    frame_path = paths[frame_index % len(paths)]
                    key_result = device.set_key_image(key, str(frame_path))
                    if key_result == -1:
                        self.close()
                        return StreamDockN4ProAnimationResult(
                            ok=False,
                            device_type=self._device_type,
                            path=self._path,
                            background_result=_stringify_optional(background_result),
                            frames_rendered=frames_rendered,
                            key_count=len(normalized_frames),
                            error=f"set_key_image failed for key {key}: SDK returned -1",
                        )
                device.refresh()
                frames_rendered += 1
                if frames_rendered == 1:
                    timing["first_frame"] = _elapsed_seconds(
                        playback_started_at,
                        self._monotonic(),
                    )
                next_frame_at += frame_interval
                if frame_index + 1 < frame_budget:
                    delay = next_frame_at - self._monotonic()
                    if delay > 0:
                        self._sleep(delay)

            playback_finished_at = self._monotonic()
            timing.setdefault("first_frame", 0.0)
            timing["playback"] = _elapsed_seconds(
                playback_started_at,
                playback_finished_at,
            )
            timing["close"] = 0.0
            timing["total"] = _elapsed_seconds(started_at, self._monotonic())
            return StreamDockN4ProAnimationResult(
                ok=True,
                device_type=self._device_type,
                path=self._path,
                background_result=_stringify_optional(background_result),
                frames_rendered=frames_rendered,
                key_count=len(normalized_frames),
                timing_seconds=timing,
            )
        except Exception as exc:
            self.close()
            return StreamDockN4ProAnimationResult(
                ok=False,
                device_type=self._device_type,
                path=self._path,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            for temp_path in temp_paths:
                temp_path.unlink(missing_ok=True)

    def close(self) -> None:
        """关闭当前持有的 N4 Pro 设备会话。

        入参：无。
        返回：无显式返回值。
        错误处理：SDK close 异常被吞掉；调用方通常在 daemon shutdown 或错误恢复时调用。
        副作用：如果设备已打开，会调用 `close(notify=False)` 并清空缓存设备。
        """

        device = self._device
        self._device = None
        self._device_type = None
        self._path = None
        if device is None:
            return
        try:
            device.close(notify=False)
        except Exception:
            pass

    def _ensure_open_device(
        self,
        *,
        timing: dict[str, float],
        started_at: float,
    ) -> StreamDockN4ProDeviceLike | StreamDockN4ProAnimationResult:
        """返回已打开设备；必要时首次枚举、open 并 init。

        入参：`timing` 是本轮 timing 结果容器；`started_at` 是本轮开始 monotonic 时间。
        返回：成功时返回 device；失败时返回 `ok=False` 的结果对象。
        错误处理：open false 和找不到设备转为错误结果；其他异常交给调用方捕获。
        副作用：可能加载 SDK、枚举设备并 open/init N4 Pro。
        """

        if self._device is not None:
            timing["open_init"] = 0.0
            return self._device
        active_manager = self._manager if self._manager is not None else _load_default_manager()
        device = _first_n4pro_device(active_manager.enumerate())
        if device is None:
            return StreamDockN4ProAnimationResult(ok=False, error="no N4 Pro device found")
        self._device_type = type(device).__name__
        self._path = _safe_path(device)
        open_result = device.open()
        if open_result is False:
            return StreamDockN4ProAnimationResult(
                ok=False,
                device_type=self._device_type,
                path=self._path,
                error="open failed: SDK returned false",
            )
        device.init()
        self._device = device
        timing["open_init"] = _elapsed_seconds(started_at, self._monotonic())
        return device


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


def animate_key_images_on_n4pro(
    *,
    background_image: Image.Image | None,
    key_frame_paths: Mapping[int, tuple[Path, ...]],
    duration_seconds: float,
    fps: int,
    manager: StreamDockN4ProManagerLike | None = None,
    temp_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> StreamDockN4ProAnimationResult:
    """在同一次 N4 Pro 设备会话里播放多个按键动画并保留背景层。

    入参：`background_image` 是可选 800x480 背景图；`key_frame_paths` 是 key 到帧 PNG
    路径元组的映射，key 必须在 1-15 且每个 key 至少一帧；`duration_seconds` 是播放时长；
    `fps` 是目标刷新帧率；`manager` 可注入 fake 或官方 DeviceManager；`temp_dir` 是背景
    临时 JPEG 目录；`sleep`/`monotonic` 仅供测试替换计时函数。
    返回：`StreamDockN4ProAnimationResult`，成功时包含刷新帧数和参与按键数。
    错误处理：非法 key、空帧、时长/FPS 非正、找不到 N4 Pro、open false、SDK 返回 -1 或
    抛异常都会返回 `ok=False`；临时背景文件会尽力清理。
    副作用：无 manager 时懒加载官方 SDK 并枚举真实设备；成功选中 N4 Pro 后会 open/init、
    可选写 frame background，然后按帧循环 set_key_image/refresh，最后 close(notify=False)。
    """

    if duration_seconds <= 0:
        return StreamDockN4ProAnimationResult(
            ok=False,
            error="duration_seconds must be positive",
        )
    if fps <= 0:
        return StreamDockN4ProAnimationResult(ok=False, error="fps must be positive")
    normalized_frames = {
        key: tuple(paths) for key, paths in key_frame_paths.items()
    }
    invalid_keys = sorted(key for key in normalized_frames if key not in range(1, 16))
    if invalid_keys:
        return StreamDockN4ProAnimationResult(
            ok=False,
            error=f"keys must be in range 1..15: {invalid_keys}",
        )
    empty_keys = sorted(key for key, paths in normalized_frames.items() if not paths)
    if empty_keys:
        return StreamDockN4ProAnimationResult(
            ok=False,
            error=f"keys must have at least one frame: {empty_keys}",
        )
    if background_image is None and not normalized_frames:
        return StreamDockN4ProAnimationResult(
            ok=False,
            error="at least one background or key animation is required",
        )

    active_manager = manager if manager is not None else _load_default_manager()
    device = _first_n4pro_device(active_manager.enumerate())
    if device is None:
        return StreamDockN4ProAnimationResult(ok=False, error="no N4 Pro device found")

    device_type = type(device).__name__
    path = _safe_path(device)
    opened = False
    temp_paths: list[Path] = []
    frame_budget = max(1, int(round(duration_seconds * fps)))
    frame_interval = 1.0 / fps
    timing: dict[str, float] = {}
    started_at = monotonic()
    result: StreamDockN4ProAnimationResult | None = None
    try:
        open_result = device.open()
        if open_result is False:
            return StreamDockN4ProAnimationResult(
                ok=False,
                device_type=device_type,
                path=path,
                error="open failed: SDK returned false",
            )
        opened = True
        device.init()
        after_open_init = monotonic()
        timing["open_init"] = _elapsed_seconds(started_at, after_open_init)

        background_result: object | None = None
        after_background = after_open_init
        if background_image is not None:
            background_path = _save_temp_jpeg(background_image, temp_dir=temp_dir)
            temp_paths.append(background_path)
            background_result = device.set_frame_background(str(background_path))
            after_background = monotonic()
            timing["background"] = _elapsed_seconds(after_open_init, after_background)
            if background_result == -1:
                return StreamDockN4ProAnimationResult(
                    ok=False,
                    device_type=device_type,
                    path=path,
                    background_result=str(background_result),
                    error="set_frame_background failed: SDK returned -1",
                )
        else:
            timing["background"] = 0.0

        frames_rendered = 0
        playback_started_at = after_background
        next_frame_at = playback_started_at
        for frame_index in range(frame_budget):
            for key, paths in normalized_frames.items():
                frame_path = paths[frame_index % len(paths)]
                key_result = device.set_key_image(key, str(frame_path))
                if key_result == -1:
                    return StreamDockN4ProAnimationResult(
                        ok=False,
                        device_type=device_type,
                        path=path,
                        background_result=_stringify_optional(background_result),
                        frames_rendered=frames_rendered,
                        key_count=len(normalized_frames),
                        error=f"set_key_image failed for key {key}: SDK returned -1",
                    )
            device.refresh()
            frames_rendered += 1
            if frames_rendered == 1:
                timing["first_frame"] = _elapsed_seconds(
                    playback_started_at,
                    monotonic(),
                )
            next_frame_at += frame_interval
            if frame_index + 1 < frame_budget:
                delay = next_frame_at - monotonic()
                if delay > 0:
                    sleep(delay)

        playback_finished_at = monotonic()
        timing.setdefault("first_frame", 0.0)
        timing["playback"] = _elapsed_seconds(playback_started_at, playback_finished_at)
        result = StreamDockN4ProAnimationResult(
            ok=True,
            device_type=device_type,
            path=path,
            background_result=_stringify_optional(background_result),
            frames_rendered=frames_rendered,
            key_count=len(normalized_frames),
            timing_seconds=timing,
        )
    except Exception as exc:
        return StreamDockN4ProAnimationResult(
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
                close_started_at = monotonic()
                device.close(notify=False)
                timing["close"] = _elapsed_seconds(close_started_at, monotonic())
            except Exception:
                pass
    if result is None:
        return StreamDockN4ProAnimationResult(
            ok=False,
            device_type=device_type,
            path=path,
            error="animation did not produce a result",
        )
    timing["total"] = _elapsed_seconds(started_at, monotonic())
    return result.model_copy(update={"timing_seconds": timing})


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


def _validate_animation_inputs(
    *,
    duration_seconds: float,
    fps: int,
    key_frame_paths: Mapping[int, tuple[Path, ...]],
    require_surface: bool,
) -> StreamDockN4ProAnimationResult | None:
    """校验 N4 Pro 按键动画 sink 的硬件无关参数。

    入参：`duration_seconds` 是播放窗口；`fps` 是目标帧率；`key_frame_paths` 是 key 到帧路径；
    `require_surface` 表示调用方已经提供背景或其他非 key surface。
    返回：参数合法时返回 None；非法时返回 `ok=False` 的错误结果。
    错误处理：本函数不抛业务异常，统一把校验失败转成结果对象。
    副作用：无；只读取内存参数，不访问文件或硬件。
    """

    if duration_seconds <= 0:
        return StreamDockN4ProAnimationResult(
            ok=False,
            error="duration_seconds must be positive",
        )
    if fps <= 0:
        return StreamDockN4ProAnimationResult(ok=False, error="fps must be positive")
    normalized_frames = {
        key: tuple(paths) for key, paths in key_frame_paths.items()
    }
    invalid_keys = sorted(key for key in normalized_frames if key not in range(1, 16))
    if invalid_keys:
        return StreamDockN4ProAnimationResult(
            ok=False,
            error=f"keys must be in range 1..15: {invalid_keys}",
        )
    empty_keys = sorted(key for key, paths in normalized_frames.items() if not paths)
    if empty_keys:
        return StreamDockN4ProAnimationResult(
            ok=False,
            error=f"keys must have at least one frame: {empty_keys}",
        )
    if not require_surface and not normalized_frames:
        return StreamDockN4ProAnimationResult(
            ok=False,
            error="at least one background or key animation is required",
        )
    return None


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


def _elapsed_seconds(started_at: float, finished_at: float) -> float:
    """把 monotonic 时间差转换成稳定的秒数诊断值。

    入参：`started_at` 和 `finished_at` 是同一 monotonic 时钟来源的秒值。
    返回：非负秒数，保留 6 位小数，供 `/status` 和测试输出。
    错误处理：若时钟倒退，返回 0.0 而不是负值。
    副作用：无；只处理内存浮点数。
    """

    return round(max(0.0, finished_at - started_at), 6)
