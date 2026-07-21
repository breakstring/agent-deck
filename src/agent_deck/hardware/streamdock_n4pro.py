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
from threading import Event
from typing import Callable, Protocol

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from agent_deck.hardware.streamdock_probe import _prepend_sdk_path_from_env

StreamDockInputCallback = Callable[[object, object], None]
StreamDockN4ProSessionOutputCallback = Callable[[object, bool], str | None]
StreamDockN4ProBackgroundUpdateProvider = Callable[[], tuple[int, Image.Image | None]]
StreamDockN4ProBackgroundWaiter = Callable[[float], bool]
StreamDockN4ProKeyImageSource = Image.Image | Path
"""持久动画器可直接下发的按键图来源：内存 Pillow 图或已预渲染 PNG 路径。"""

StreamDockN4ProKeyImageUpdateProvider = Callable[
    [], tuple[int, Mapping[int, StreamDockN4ProKeyImageSource]]
]
"""返回静态按键完整映射及其 revision 的热更新 provider。"""

_MIN_BACKGROUND_UPDATE_INTERVAL_SECONDS = 0.04
"""连续输入时同会话背景热更新的最短间隔，超过此频率时只保留最新 revision。"""

_HANDSHAKE_SETTLE_SECONDS = 0.05
"""N4 Pro 接受 `HAN` 握手后、常驻 SDK 会话 open 前的最短稳定等待。"""

_REMOVED_KEY_IMAGE = Image.new("RGB", (112, 112), (0x0B, 0x0F, 0x16))
"""完整静态键映射删除某键时下发的稳定清屏图，避免旧图留在设备显存。"""


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
        错误处理：SDK 转换或传输失败可返回非零 TransportResult 或抛异常。
        副作用：真实实现会修改 N4 Pro 背景层；该接口实测可与按键图层同时显示。
        """

    def set_key_image(self, key: int, path: str) -> object:
        """把图片文件下发到指定按键。

        入参：`key` 是 N4 Pro 逻辑按键编号 1-15；`path` 是本地 PNG 图片路径。
        返回：SDK 返回值；常见成功值可能是 None 或 0。
        错误处理：SDK 转换或传输失败可返回非零 TransportResult 或抛异常。
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

    def send_handshake(self) -> object:
        """请求 N4 Pro 退出品牌图模式并进入 SDK 控制模式。

        入参：无；设备实现使用枚举 path 和 N4 Pro 固定报告参数。
        返回：SDK 成功值 0；旧版 SDK 可能没有此可选方法，调用方会兼容跳过。
        错误处理：权限不足、HID 打开失败或短写可抛异常。
        副作用：在常驻 `open()` 前短暂打开同一 HID path 并发送连接握手。
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
    session_output_error: str | None = None
    error: str | None = None


class StreamDockN4ProPersistentAnimator:
    """daemon 专用的 N4 Pro 长连接按键动画 sink。

    入参：`manager` 可注入 fake 或官方 DeviceManager；`temp_dir` 控制临时背景 JPEG 目录；
    `sleep` 和 `monotonic` 仅供测试替换。实例会在第一次调用时 open/init N4 Pro，并在后续
    调用中复用同一个设备会话，直到显式调用 `close()`、设备 path 变化或当前 transport 不可写。
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
        input_callback: StreamDockInputCallback | None = None,
        session_output_callback: StreamDockN4ProSessionOutputCallback | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        background_wait: StreamDockN4ProBackgroundWaiter | None = None,
    ) -> None:
        """初始化 persistent animator，但不立即访问硬件。

        入参：`manager` 是可选设备管理器；`temp_dir` 是临时背景文件目录；
        `input_callback` 是可选 SDK input 回调，会在首次 open/init 后注册到 key 和 touch bar；
        `session_output_callback` 在同一已打开会话中应用亮度/灯光等非图像输出；`sleep`/`monotonic`
        用于帧节奏和诊断；`background_wait` 允许测试替换输入唤醒等待器。
        返回：无显式返回值。
        错误处理：构造阶段不访问 SDK，不抛硬件错误。
        副作用：仅保存依赖到实例字段。
        """

        self._manager = manager
        self._temp_dir = temp_dir
        self._input_callback = input_callback
        self._session_output_callback = session_output_callback
        self._background_update_provider: StreamDockN4ProBackgroundUpdateProvider | None = None
        self._last_background_revision: int | None = None
        self._last_background_update_monotonic: float | None = None
        self._key_image_update_provider: StreamDockN4ProKeyImageUpdateProvider | None = None
        self._last_key_image_revision: int | None = None
        self._last_key_images: dict[int, StreamDockN4ProKeyImageSource] = {}
        self._animation_overwritten_keys: set[int] = set()
        self._background_update_event = Event()
        self._background_wait = background_wait or (
            self._background_update_event.wait if sleep is time.sleep else None
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._device: StreamDockN4ProDeviceLike | None = None
        self._device_type: str | None = None
        self._path: str | None = None
        self._has_opened_device = False
        self._device_reconnect_count = 0
        self._device_handshake_count = 0

    def set_session_output_callback(
        self,
        callback: StreamDockN4ProSessionOutputCallback | None,
    ) -> None:
        """设置在每轮 render 的同一 N4 Pro 会话内调用的附加输出回调。

        入参：`callback` 接收 SDK device 与本轮是否刚完成 init；返回 None 或可记录的错误文本。
        返回：无显式返回值。
        错误处理：本方法不调用 callback，不在设置阶段访问硬件。
        副作用：覆盖实例内存中的 callback 引用；下一轮 render 生效。
        """

        self._session_output_callback = callback

    def set_background_update_provider(
        self,
        provider: StreamDockN4ProBackgroundUpdateProvider | None,
    ) -> None:
        """设置可在同一帧循环中读取最新 touch bar 背景 revision 的 provider。

        入参：`provider` 返回单调递增 revision 与对应 800x480 图像；None 表示维持调用方传入的
        初始背景。provider 只能读取 daemon 已缓存的图像，不能直接访问 SDK。
        返回：无显式返回值。
        错误处理：设置阶段不调用 provider，因此不会抛 provider 内部异常。
        副作用：覆盖实例内存 provider；下一轮和当前活跃帧循环的下一帧生效。
        """

        self._background_update_provider = provider

    def set_key_image_update_provider(
        self,
        provider: StreamDockN4ProKeyImageUpdateProvider | None,
    ) -> None:
        """设置静态主按键的热更新 provider。

        入参：`provider` 返回单调递增 revision 与当前完整静态键图映射；值可以是 Pillow 图像，
        也可以是已预渲染且仍存在的 `Path`。正常映射不应包含当前由动画帧控制的 agent 键；
        若布局热切换短暂产生重叠，animator 会延迟该键的静态写入，待动画释放后恢复。
        animator 会基于上次已写图片引用或路径计算本次仅需下发的差异。
        返回：无显式返回值。
        错误处理：设置阶段不读取 provider，也不访问 SDK。
        副作用：覆盖实例内存 provider；当前活跃帧循环会在下次检查 revision 时使用新值。
        """

        self._key_image_update_provider = provider

    def notify_background_update(self) -> None:
        """通知持久 animator 有新的背景或静态键 revision 可在同会话内尽快下发。

        入参：无；调用方必须已经把新图写入 provider 可读的 daemon 内存缓存。
        返回：无。
        错误处理：不读取 provider、不访问 SDK，因此不会传播硬件或渲染异常。
        副作用：唤醒当前帧间等待；连续通知会自然合并为一次事件信号。
        """

        self._background_update_event.set()

    def __call__(
        self,
        *,
        background_image: Image.Image | None,
        key_frame_paths: Mapping[int, tuple[Path, ...]],
        duration_seconds: float,
        fps: int,
        key_images: Mapping[int, StreamDockN4ProKeyImageSource] | None = None,
    ) -> StreamDockN4ProAnimationResult:
        """播放一轮按键动画，复用已经打开的 N4 Pro 会话。

        入参：`background_image` 是可选 800x480 背景图；`key_frame_paths` 是 key 到帧 PNG
        路径的映射；`key_images` 是 key 到静态 Pillow 图或预渲染图片路径的映射；
        `duration_seconds` 是本轮播放窗口；`fps` 是目标帧率。
        返回：`StreamDockN4ProAnimationResult`，成功结果包含 timing 诊断；persistent 模式下
        `close` 阶段耗时固定为 0，真实 close 只在 `close()` 中发生。
        错误处理：非法参数、设备不可用或 SDK 返回非零 TransportResult 时返回 `ok=False`；
        异常会关闭当前会话
        并作为错误结果返回。
        副作用：可能首次 open/init 真实设备，每轮写 frame background、set key image 和 refresh。
        """

        validation_error = _validate_animation_inputs(
            duration_seconds=duration_seconds,
            fps=fps,
            key_frame_paths=key_frame_paths,
            key_images=key_images,
            require_surface=background_image is not None,
        )
        if validation_error is not None:
            return validation_error
        normalized_frames = {
            key: tuple(paths) for key, paths in key_frame_paths.items()
        }
        animated_keys = set(normalized_frames)
        normalized_key_images = dict(key_images or {})
        key_count = len(set(normalized_frames) | set(normalized_key_images))
        frame_budget = max(1, int(round(duration_seconds * fps)))
        frame_interval = 1.0 / fps
        timing: dict[str, float] = {}
        started_at = self._monotonic()
        temp_paths: list[Path] = []
        try:
            previous_device = self._device
            device = self._ensure_open_device(timing=timing, started_at=started_at)
            if isinstance(device, StreamDockN4ProAnimationResult):
                return device
            self._animation_overwritten_keys.update(animated_keys)

            session_output_error = self._apply_session_output(
                device,
                initialized=device is not previous_device,
            )

            background_result: object | None = None
            initial_background_revision = self._read_background_revision()
            if initial_background_revision is not None:
                revision, provider_image = initial_background_revision
                if provider_image is not None:
                    background_image = provider_image
                self._last_background_revision = revision
            initial_key_image_revision = self._read_key_image_revision()
            if initial_key_image_revision is not None:
                initial_key_revision, initial_key_images = initial_key_image_revision
                normalized_key_images = dict(initial_key_images)
                source_error = _key_image_sources_error(normalized_key_images)
                if source_error is not None:
                    self.close()
                    return StreamDockN4ProAnimationResult(
                        ok=False,
                        key_count=len(set(normalized_frames) | set(normalized_key_images)),
                        error=f"key image update failed: {source_error}",
                    )
                if (
                    initial_key_revision == self._last_key_image_revision
                    and not self._animation_overwritten_keys
                ):
                    initial_key_images_to_write = {}
                else:
                    initial_key_images_to_write = _changed_key_image_sources(
                        previous=self._last_key_images,
                        current=normalized_key_images,
                    )
                    for key in self._animation_overwritten_keys:
                        initial_key_images_to_write[key] = normalized_key_images.get(
                            key,
                            _REMOVED_KEY_IMAGE,
                        )
            else:
                initial_key_images_to_write = dict(normalized_key_images)
                for key in self._animation_overwritten_keys:
                    initial_key_images_to_write[key] = normalized_key_images.get(
                        key,
                        _REMOVED_KEY_IMAGE,
                    )
            for key in animated_keys:
                initial_key_images_to_write.pop(key, None)
            key_count = len(set(normalized_frames) | set(normalized_key_images))
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
                if streamdock_sdk_result_failed(background_result):
                    self.close()
                    return StreamDockN4ProAnimationResult(
                        ok=False,
                        device_type=self._device_type,
                        path=self._path,
                        background_result=str(background_result),
                        error=_sdk_failure_message(
                            "set_frame_background failed",
                            background_result,
                        ),
                    )
            else:
                timing["background"] = 0.0

            if initial_key_images_to_write:
                static_started_at = self._monotonic()
                for key, image_source in initial_key_images_to_write.items():
                    key_path = _key_image_source_path(
                        image_source,
                        temp_dir=self._temp_dir,
                        temp_paths=temp_paths,
                    )
                    key_result = device.set_key_image(key, str(key_path))
                    if streamdock_sdk_result_failed(key_result):
                        self.close()
                        return StreamDockN4ProAnimationResult(
                            ok=False,
                            device_type=self._device_type,
                            path=self._path,
                            background_result=_stringify_optional(background_result),
                            key_count=key_count,
                            error=_sdk_failure_message(
                                f"set_key_image failed for key {key}",
                                key_result,
                            ),
                        )
                timing["static_keys"] = _elapsed_seconds(
                    static_started_at,
                    self._monotonic(),
                )
                after_static = self._monotonic()
            else:
                timing["static_keys"] = 0.0
                after_static = after_background
            self._animation_overwritten_keys.difference_update(
                initial_key_images_to_write
            )
            if initial_key_image_revision is not None:
                self._last_key_image_revision = initial_key_revision
                self._last_key_images = dict(normalized_key_images)

            frames_rendered = 0
            background_hot_updates = 0
            background_hot_update_seconds = 0.0
            key_hot_updates = 0
            key_hot_update_seconds = 0.0
            key_hot_keys = 0
            playback_started_at = after_static
            next_frame_at = playback_started_at
            if (
                normalized_frames
                or self._background_update_provider is not None
                or self._key_image_update_provider is not None
            ):
                for frame_index in range(frame_budget):
                    background_update_started_at = self._monotonic()
                    background_updated, updated_background_result, background_update_error = (
                        self._write_pending_background_update(device, temp_paths=temp_paths)
                    )
                    background_update_elapsed = _elapsed_seconds(
                        background_update_started_at,
                        self._monotonic(),
                    )
                    if background_update_error is not None:
                        self.close()
                        return StreamDockN4ProAnimationResult(
                            ok=False,
                            device_type=self._device_type,
                            path=self._path,
                            background_result=_stringify_optional(background_result),
                            frames_rendered=frames_rendered,
                            key_count=key_count,
                            error=background_update_error,
                        )
                    if background_updated:
                        if updated_background_result is not None:
                            background_result = updated_background_result
                        background_hot_updates += 1
                        background_hot_update_seconds += background_update_elapsed
                    key_update_started_at = self._monotonic()
                    key_updated, updated_key_count, key_update_error = (
                        self._write_pending_key_image_update(
                            device,
                            temp_paths=temp_paths,
                            animated_keys=animated_keys,
                        )
                    )
                    key_update_elapsed = _elapsed_seconds(
                        key_update_started_at,
                        self._monotonic(),
                    )
                    if key_update_error is not None:
                        self.close()
                        return StreamDockN4ProAnimationResult(
                            ok=False,
                            device_type=self._device_type,
                            path=self._path,
                            background_result=_stringify_optional(background_result),
                            frames_rendered=frames_rendered,
                            key_count=key_count,
                            error=key_update_error,
                        )
                    if key_updated:
                        key_hot_updates += 1
                        key_hot_update_seconds += key_update_elapsed
                        key_hot_keys += updated_key_count
                    frame_session_output_error = self._apply_session_output(
                        device,
                        initialized=False,
                    )
                    if frame_session_output_error is not None:
                        session_output_error = frame_session_output_error
                    for key, paths in normalized_frames.items():
                        frame_path = paths[frame_index % len(paths)]
                        key_result = device.set_key_image(key, str(frame_path))
                        if streamdock_sdk_result_failed(key_result):
                            self.close()
                            return StreamDockN4ProAnimationResult(
                                ok=False,
                                device_type=self._device_type,
                                path=self._path,
                                background_result=_stringify_optional(background_result),
                                frames_rendered=frames_rendered,
                                key_count=key_count,
                                error=_sdk_failure_message(
                                    f"set_key_image failed for key {key}",
                                    key_result,
                                ),
                            )
                    refresh_result = device.refresh()
                    if streamdock_sdk_result_failed(refresh_result):
                        self.close()
                        return StreamDockN4ProAnimationResult(
                            ok=False,
                            device_type=self._device_type,
                            path=self._path,
                            background_result=_stringify_optional(background_result),
                            frames_rendered=frames_rendered,
                            key_count=key_count,
                            error=_sdk_failure_message("refresh failed", refresh_result),
                        )
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
                            self._wait_for_next_frame(delay)
            else:
                refresh_result = device.refresh()
                if streamdock_sdk_result_failed(refresh_result):
                    self.close()
                    return StreamDockN4ProAnimationResult(
                        ok=False,
                        device_type=self._device_type,
                        path=self._path,
                        background_result=_stringify_optional(background_result),
                        frames_rendered=frames_rendered,
                        key_count=key_count,
                        error=_sdk_failure_message("refresh failed", refresh_result),
                    )
                frames_rendered += 1
                timing["first_frame"] = _elapsed_seconds(
                    playback_started_at,
                    self._monotonic(),
                )

            playback_finished_at = self._monotonic()
            timing.setdefault("first_frame", 0.0)
            timing["playback"] = _elapsed_seconds(
                playback_started_at,
                playback_finished_at,
            )
            timing["background_hot_updates"] = float(background_hot_updates)
            timing["background_hot_update"] = background_hot_update_seconds
            timing["key_hot_updates"] = float(key_hot_updates)
            timing["key_hot_update"] = key_hot_update_seconds
            timing["key_hot_keys"] = float(key_hot_keys)
            timing["close"] = 0.0
            timing["total"] = _elapsed_seconds(started_at, self._monotonic())
            return StreamDockN4ProAnimationResult(
                ok=True,
                device_type=self._device_type,
                path=self._path,
                background_result=_stringify_optional(background_result),
                frames_rendered=frames_rendered,
                key_count=key_count,
                timing_seconds=timing,
                session_output_error=session_output_error,
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
        self._last_background_revision = None
        self._last_background_update_monotonic = None
        self._last_key_image_revision = None
        self._last_key_images = {}
        self._animation_overwritten_keys.clear()
        self._background_update_event.clear()
        if device is None:
            return
        try:
            device.close(notify=False)
        except Exception:
            pass

    def _apply_session_output(
        self,
        device: StreamDockN4ProDeviceLike,
        *,
        initialized: bool,
    ) -> str | None:
        """在当前 open/init 会话中调用可选控制台输出回调。

        入参：`device` 是已打开 N4 Pro；`initialized` 表示本轮刚完成 init，供回调恢复亮度。
        返回：callback 返回的错误文本，或捕获异常转换后的文本；没有 callback 时为 None。
        错误处理：callback 的任意异常只记录文本，不能中断背景/按键渲染。
        副作用：callback 可能写 N4 Pro 亮度或 LED，但不新建 HID 会话。
        """

        callback = self._session_output_callback
        if callback is None:
            return None
        try:
            return callback(device, initialized)
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"

    def _read_background_revision(self) -> tuple[int, Image.Image | None] | None:
        """读取可选 provider 给出的当前背景 revision，并把异常降级为本轮无热更新。

        入参：无；provider 由 daemon 注入，只应返回缓存图像。
        返回：无 provider 时为 None；provider 正常时为 `(revision, image)`。
        错误处理：provider 异常在此处吞掉，避免输入侧的缓存问题关闭真实设备会话。
        副作用：可能读取 daemon 内存图像，不访问 SDK 或文件系统。
        """

        provider = self._background_update_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:
            return None

    def _read_key_image_revision(
        self,
    ) -> tuple[int, Mapping[int, StreamDockN4ProKeyImageSource]] | None:
        """读取静态按键 provider 的最新 revision，并将异常降级为本帧无更新。

        入参：无。
        返回：无 provider 时为 None；正常时返回 revision 与当前完整静态键图来源映射。
        错误处理：provider 异常被吞掉，避免一次缓存读取失败关闭真实设备会话。
        副作用：只读取 daemon 内存中的 revision 与 Pillow 图像或路径引用，不访问 SDK。
        """

        provider = self._key_image_update_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:
            return None

    def _write_pending_background_update(
        self,
        device: StreamDockN4ProDeviceLike,
        *,
        temp_paths: list[Path],
    ) -> tuple[bool, object | None, str | None]:
        """在活跃帧循环内下发 revision 已变化的完整背景图。

        入参：`device` 是当前已 open/init 的 N4 Pro；`temp_paths` 收集本轮临时 JPEG 以便统一清理。
        返回：第一个值表示是否实际写入；未变化时为 `(False, None, None)`；成功时保留 SDK
        返回值（即使成功值是 None）；provider/SDK 异常返回错误文本。
        错误处理：provider 无图或读取异常保持当前背景；SDK 写入失败返回错误供调用方关闭会话并记录。
        副作用：revision 变化时调用一次 `set_frame_background`，不创建第二个 HID 会话。
        """

        pending = self._read_background_revision()
        if pending is None:
            return False, None, None
        revision, image = pending
        if revision == self._last_background_revision or image is None:
            return False, None, None
        last_update = self._last_background_update_monotonic
        if (
            last_update is not None
            and self._monotonic() - last_update < _MIN_BACKGROUND_UPDATE_INTERVAL_SECONDS
        ):
            return False, None, None
        try:
            background_path = _save_temp_jpeg(image, temp_dir=self._temp_dir)
            temp_paths.append(background_path)
            result = device.set_frame_background(str(background_path))
        except Exception as exc:
            return False, None, f"set_frame_background update failed: {type(exc).__name__}: {exc}"
        if streamdock_sdk_result_failed(result):
            return (
                False,
                result,
                _sdk_failure_message("set_frame_background update failed", result),
            )
        self._last_background_revision = revision
        self._last_background_update_monotonic = self._monotonic()
        return True, result, None

    def _write_pending_key_image_update(
        self,
        device: StreamDockN4ProDeviceLike,
        *,
        temp_paths: list[Path],
        animated_keys: set[int],
    ) -> tuple[bool, int, str | None]:
        """在活跃帧循环内下发 revision 已变化的静态键图。

        入参：`device` 是当前已 open/init 的 N4 Pro；`temp_paths` 收集本轮 PNG 以便统一清理；
        `animated_keys` 是本轮仍由 agent 动画帧占用的键，静态更新必须延迟到下一轮释放后。
        返回：依次为是否写入、写入键数和可选错误文本；没有新 revision 时返回 `(False, 0, None)`。
        错误处理：provider 缺失或异常降级为无更新；非法键或 SDK 写入失败返回错误，调用方关闭会话。
        副作用：revision 变化时只比较并调用本次完整映射中发生变化的键的 `set_key_image`，由同一帧
        的 `refresh` 可见化，不会新建 HID 会话。
        """

        pending = self._read_key_image_revision()
        if pending is None:
            return False, 0, None
        revision, key_images = pending
        if (
            revision == self._last_key_image_revision
            and not self._animation_overwritten_keys
        ):
            return False, 0, None
        normalized_images = dict(key_images)
        invalid_keys = sorted(key for key in normalized_images if key not in range(1, 16))
        if invalid_keys:
            return False, 0, f"key image update keys must be in range 1..15: {invalid_keys}"
        source_error = _key_image_sources_error(normalized_images)
        if source_error is not None:
            return False, 0, f"key image update failed: {source_error}"
        changed_images = _changed_key_image_sources(
            previous=self._last_key_images,
            current=normalized_images,
        )
        for key in self._animation_overwritten_keys:
            changed_images[key] = normalized_images.get(key, _REMOVED_KEY_IMAGE)
        for key in animated_keys:
            changed_images.pop(key, None)
        try:
            for key, image_source in changed_images.items():
                key_path = _key_image_source_path(
                    image_source,
                    temp_dir=self._temp_dir,
                    temp_paths=temp_paths,
                )
                result = device.set_key_image(key, str(key_path))
                if streamdock_sdk_result_failed(result):
                    return (
                        False,
                        0,
                        _sdk_failure_message(
                            f"set_key_image update failed for key {key}",
                            result,
                        ),
                    )
        except Exception as exc:
            return False, 0, f"set_key_image update failed: {type(exc).__name__}: {exc}"
        self._last_key_image_revision = revision
        self._last_key_images = normalized_images
        self._animation_overwritten_keys.difference_update(changed_images)
        return bool(changed_images), len(changed_images), None

    def _wait_for_next_frame(self, delay_seconds: float) -> None:
        """等待下一帧或被新的背景、静态键 revision 提前唤醒。

        入参：`delay_seconds` 是原本要等待的帧间隔。
        返回：无。
        错误处理：测试注入的 waiter 异常按原语义传播；默认 Event.wait 不抛业务异常。
        副作用：默认路径可能因 `notify_background_update()` 提前结束等待；事件会在读取后清除。
        """

        waiter = self._background_wait
        if waiter is None:
            self._sleep(delay_seconds)
            return
        waiter(delay_seconds)
        self._background_update_event.clear()

    def _ensure_open_device(
        self,
        *,
        timing: dict[str, float],
        started_at: float,
    ) -> StreamDockN4ProDeviceLike | StreamDockN4ProAnimationResult:
        """返回当前枚举路径对应的已打开设备；必要时重连、open 并 init。

        入参：`timing` 是本轮 timing 结果容器；`started_at` 是本轮开始 monotonic 时间。每轮会先
        只读枚举 N4 Pro，并与当前持有会话的 SDK 路径比较。
        返回：成功时返回 device；失败时返回 `ok=False` 的结果对象。
        错误处理：open false、设备消失和找不到设备转为错误结果；枚举/SDK 异常交给调用方捕获。
        副作用：路径未变时复用当前会话；路径变化时先 `close(notify=False)` 释放失效 HID 句柄，再
        open/init 新枚举的设备，并在 timing 中记录 `device_reconnected=1`。
        """

        active_manager = self._manager if self._manager is not None else _load_default_manager()
        had_opened_device = self._has_opened_device
        enumerated_devices = list(active_manager.enumerate())
        device = _first_n4pro_device(enumerated_devices)
        for enumerated_device in enumerated_devices:
            if enumerated_device is device or enumerated_device is self._device:
                continue
            _close_discarded_enumerated_device(enumerated_device)
        if self._device is not None:
            same_path = device is not None and _safe_path(device) == self._path
            if same_path and _device_is_writable(self._device) is not False:
                if device is not self._device:
                    _close_discarded_enumerated_device(device)
                timing["open_init"] = 0.0
                timing["handshake"] = 0.0
                timing["device_handshaken"] = 0.0
                timing["device_handshake_count"] = float(
                    self._device_handshake_count
                )
                timing["device_reconnected"] = 0.0
                timing["device_reconnect_count"] = float(self._device_reconnect_count)
                return self._device
            self.close()
            if device is None:
                return StreamDockN4ProAnimationResult(
                    ok=False,
                    error="N4 Pro disappeared while persistent renderer was active",
                )
            timing["device_reconnected"] = 1.0
        if device is None:
            return StreamDockN4ProAnimationResult(ok=False, error="no N4 Pro device found")
        self._device_type = type(device).__name__
        self._path = _safe_path(device)
        handshake_started_at = self._monotonic()
        handshake = getattr(device, "send_handshake", None)
        device_handshaken = False
        if callable(handshake):
            try:
                handshake_result = handshake()
            except Exception as exc:
                _close_discarded_enumerated_device(device)
                return StreamDockN4ProAnimationResult(
                    ok=False,
                    device_type=self._device_type,
                    path=self._path,
                    error=f"handshake failed: {type(exc).__name__}: {exc}",
                )
            if streamdock_sdk_result_failed(handshake_result):
                _close_discarded_enumerated_device(device)
                return StreamDockN4ProAnimationResult(
                    ok=False,
                    device_type=self._device_type,
                    path=self._path,
                    error=_sdk_failure_message("handshake failed", handshake_result),
                )
            self._device_handshake_count += 1
            device_handshaken = True
            self._sleep(_HANDSHAKE_SETTLE_SECONDS)
        timing["handshake"] = _elapsed_seconds(
            handshake_started_at,
            self._monotonic(),
        )
        timing["device_handshaken"] = 1.0 if device_handshaken else 0.0
        timing["device_handshake_count"] = float(self._device_handshake_count)
        try:
            open_result = device.open()
        except Exception:
            _close_discarded_enumerated_device(device)
            raise
        if open_result is False:
            _close_discarded_enumerated_device(device)
            return StreamDockN4ProAnimationResult(
                ok=False,
                device_type=self._device_type,
                path=self._path,
                error="open failed: SDK returned false",
            )
        self._device = device
        device.init()
        if _device_is_writable(device) is False:
            device_type = self._device_type
            path = self._path
            self.close()
            return StreamDockN4ProAnimationResult(
                ok=False,
                device_type=device_type,
                path=path,
                error="init failed: N4 Pro session is not writable",
            )
        self._register_input_callback(device)
        if had_opened_device:
            self._device_reconnect_count += 1
        self._has_opened_device = True
        timing["open_init"] = _elapsed_seconds(started_at, self._monotonic())
        timing["device_reconnected"] = 1.0 if had_opened_device else 0.0
        timing["device_reconnect_count"] = float(self._device_reconnect_count)
        return device

    def _register_input_callback(self, device: StreamDockN4ProDeviceLike) -> None:
        """把可选 input callback 注册到 N4 Pro SDK 回调入口。

        入参：`device` 是已 open/init 的 N4 Pro SDK device。
        返回：无返回值。
        错误处理：若设备没有某类 callback setter 则跳过；setter 自身异常会传播并关闭会话。
        副作用：修改 SDK device 的 key/touch callback，用于同一设备会话内接收真实输入。
        """

        if self._input_callback is None:
            return
        key_setter = getattr(device, "set_key_callback", None)
        if callable(key_setter):
            key_setter(self._input_callback)
        touch_setter = getattr(device, "set_touch_bar_callback", None)
        if not callable(touch_setter):
            touch_setter = getattr(device, "set_touchscreen_callback", None)
        if callable(touch_setter):
            touch_setter(self._input_callback)


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
    错误处理：没有任何 surface、非法 key、找不到 N4 Pro、open false、任一 SDK 返回非零结果或
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
            if streamdock_sdk_result_failed(background_result):
                return StreamDockN4ProRenderResult(
                    ok=False,
                    device_type=device_type,
                    path=path,
                    background_result=str(background_result),
                    error=_sdk_failure_message(
                        "set_frame_background failed",
                        background_result,
                    ),
                )

        key_results: dict[int, str] = {}
        for key, image in normalized_key_images.items():
            key_path = _save_temp_png(image, temp_dir=temp_dir)
            temp_paths.append(key_path)
            key_result = device.set_key_image(key, str(key_path))
            key_results[key] = str(key_result)
            if streamdock_sdk_result_failed(key_result):
                return StreamDockN4ProRenderResult(
                    ok=False,
                    device_type=device_type,
                    path=path,
                    background_result=_stringify_optional(background_result),
                    key_results=key_results,
                    error=_sdk_failure_message(
                        f"set_key_image failed for key {key}",
                        key_result,
                    ),
                )

        refresh_result = device.refresh()
        if streamdock_sdk_result_failed(refresh_result):
            return StreamDockN4ProRenderResult(
                ok=False,
                device_type=device_type,
                path=path,
                background_result=_stringify_optional(background_result),
                key_results=key_results,
                refresh_result=str(refresh_result),
                error=_sdk_failure_message("refresh failed", refresh_result),
            )
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
    key_images: Mapping[int, Image.Image] | None = None,
    manager: StreamDockN4ProManagerLike | None = None,
    temp_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> StreamDockN4ProAnimationResult:
    """在同一次 N4 Pro 设备会话里播放多个按键动画并保留背景层。

    入参：`background_image` 是可选 800x480 背景图；`key_frame_paths` 是 key 到帧 PNG
    路径元组的映射，key 必须在 1-15 且每个 key 至少一帧；`key_images` 是 key 到静态
    Pillow 图像的映射；`duration_seconds` 是播放时长；`fps` 是目标刷新帧率；`manager`
    可注入 fake 或官方 DeviceManager；`temp_dir` 是背景临时 JPEG 目录；`sleep`/`monotonic`
    仅供测试替换计时函数。
    返回：`StreamDockN4ProAnimationResult`，成功时包含刷新帧数和参与按键数。
    错误处理：非法 key、空帧、时长/FPS 非正、找不到 N4 Pro、open false、SDK 返回非零结果或
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
    validation_error = _validate_animation_inputs(
        duration_seconds=duration_seconds,
        fps=fps,
        key_frame_paths=key_frame_paths,
        key_images=key_images,
        require_surface=background_image is not None,
    )
    if validation_error is not None:
        return validation_error
    normalized_frames = {
        key: tuple(paths) for key, paths in key_frame_paths.items()
    }
    normalized_key_images = dict(key_images or {})
    key_count = len(set(normalized_frames) | set(normalized_key_images))

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
            if streamdock_sdk_result_failed(background_result):
                return StreamDockN4ProAnimationResult(
                    ok=False,
                    device_type=device_type,
                    path=path,
                    background_result=str(background_result),
                    error=_sdk_failure_message(
                        "set_frame_background failed",
                        background_result,
                    ),
                )
        else:
            timing["background"] = 0.0

        if normalized_key_images:
            static_started_at = monotonic()
            for key, image in normalized_key_images.items():
                key_path = _save_temp_png(image, temp_dir=temp_dir)
                temp_paths.append(key_path)
                key_result = device.set_key_image(key, str(key_path))
                if streamdock_sdk_result_failed(key_result):
                    return StreamDockN4ProAnimationResult(
                        ok=False,
                        device_type=device_type,
                        path=path,
                        background_result=_stringify_optional(background_result),
                        key_count=key_count,
                        error=_sdk_failure_message(
                            f"set_key_image failed for key {key}",
                            key_result,
                        ),
                    )
            timing["static_keys"] = _elapsed_seconds(static_started_at, monotonic())
            after_static = monotonic()
        else:
            timing["static_keys"] = 0.0
            after_static = after_background

        frames_rendered = 0
        playback_started_at = after_static
        next_frame_at = playback_started_at
        if normalized_frames:
            for frame_index in range(frame_budget):
                for key, paths in normalized_frames.items():
                    frame_path = paths[frame_index % len(paths)]
                    key_result = device.set_key_image(key, str(frame_path))
                    if streamdock_sdk_result_failed(key_result):
                        return StreamDockN4ProAnimationResult(
                            ok=False,
                            device_type=device_type,
                            path=path,
                            background_result=_stringify_optional(background_result),
                            frames_rendered=frames_rendered,
                            key_count=key_count,
                            error=_sdk_failure_message(
                                f"set_key_image failed for key {key}",
                                key_result,
                            ),
                        )
                refresh_result = device.refresh()
                if streamdock_sdk_result_failed(refresh_result):
                    return StreamDockN4ProAnimationResult(
                        ok=False,
                        device_type=device_type,
                        path=path,
                        background_result=_stringify_optional(background_result),
                        frames_rendered=frames_rendered,
                        key_count=key_count,
                        error=_sdk_failure_message("refresh failed", refresh_result),
                    )
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
        else:
            refresh_result = device.refresh()
            if streamdock_sdk_result_failed(refresh_result):
                return StreamDockN4ProAnimationResult(
                    ok=False,
                    device_type=device_type,
                    path=path,
                    background_result=_stringify_optional(background_result),
                    frames_rendered=frames_rendered,
                    key_count=key_count,
                    error=_sdk_failure_message("refresh failed", refresh_result),
                )
            frames_rendered += 1
            timing["first_frame"] = _elapsed_seconds(playback_started_at, monotonic())

        playback_finished_at = monotonic()
        timing.setdefault("first_frame", 0.0)
        timing["playback"] = _elapsed_seconds(playback_started_at, playback_finished_at)
        result = StreamDockN4ProAnimationResult(
            ok=True,
            device_type=device_type,
            path=path,
            background_result=_stringify_optional(background_result),
            frames_rendered=frames_rendered,
            key_count=key_count,
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
    key_images: Mapping[int, StreamDockN4ProKeyImageSource] | None,
    require_surface: bool,
) -> StreamDockN4ProAnimationResult | None:
    """校验 N4 Pro 按键动画 sink 的硬件无关参数。

    入参：`duration_seconds` 是播放窗口；`fps` 是目标帧率；`key_frame_paths` 是 key 到帧路径；
    `key_images` 是 key 到静态 Pillow 图或预渲染路径的映射；`require_surface` 表示调用方已经
    提供背景或其他非 key surface。
    返回：参数合法时返回 None；非法时返回 `ok=False` 的错误结果。
    错误处理：本函数不抛业务异常，统一把校验失败转成结果对象。
    副作用：只读检查 `Path` 是否为现存普通文件，不访问硬件也不修改文件。
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
    normalized_key_images = dict(key_images or {})
    invalid_keys = sorted(
        key
        for key in set(normalized_frames) | set(normalized_key_images)
        if key not in range(1, 16)
    )
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
    source_error = _key_image_sources_error(normalized_key_images)
    if source_error is not None:
        return StreamDockN4ProAnimationResult(ok=False, error=source_error)
    if not require_surface and not normalized_frames and not normalized_key_images:
        return StreamDockN4ProAnimationResult(
            ok=False,
            error="at least one background, key animation, or key image is required",
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


def _close_discarded_enumerated_device(device: StreamDockN4ProDeviceLike) -> None:
    """释放本轮枚举后不会被 persistent animator 采用的 SDK device wrapper。

    入参：`device` 是刚由官方 `DeviceManager.enumerate()` 构造、但不会成为当前活动会话的对象。
    返回：无。
    错误处理：SDK `close()` 异常被吞掉，避免一个无关设备阻断 N4 Pro 健康检查。
    副作用：调用 `close(notify=False)`；官方 SDK 会由此停止构造时立即启动的 GIF 后台线程，
    防止每轮重枚举泄漏一个线程。未 open 的 transport 不会收到断开通知或修改设备画面。
    """

    try:
        device.close(notify=False)
    except Exception:
        pass


def _key_image_sources_error(
    sources: Mapping[int, StreamDockN4ProKeyImageSource],
) -> str | None:
    """校验静态按键图来源是否为 Pillow 图或现存文件路径。

    入参：`sources` 是逻辑键号到静态按键图来源的映射；键号范围由上层独立校验。
    返回：全部合法时返回 None；否则返回第一条可直接进入诊断的短错误。
    错误处理：路径状态读取异常转换为错误文本，不向调用方传播。
    副作用：只调用路径的 `is_file()`，不打开、写入或删除文件。
    """

    for key, source in sources.items():
        if isinstance(source, Image.Image):
            continue
        if not isinstance(source, Path):
            return (
                f"key image source for key {key} must be Pillow Image or pathlib.Path"
            )
        try:
            is_file = source.is_file()
        except OSError as exc:
            return f"key image path for key {key} is unreadable: {type(exc).__name__}: {exc}"
        if not is_file:
            return f"key image path for key {key} must be an existing file: {source}"
    return None


def _changed_key_image_sources(
    *,
    previous: Mapping[int, StreamDockN4ProKeyImageSource],
    current: Mapping[int, StreamDockN4ProKeyImageSource],
) -> dict[int, StreamDockN4ProKeyImageSource]:
    """返回相对上一轮确实需要重新下发的静态按键图来源。

    入参：`previous` 是上次成功写入后的完整映射；`current` 是新 revision 的完整映射。
    返回：仅含新增、删除或视觉来源发生变化的键；删除项映射到稳定清屏图，Path 按路径值
    比较，Pillow 图按对象引用比较。
    错误处理：不做文件校验；调用方须先调用 `_key_image_sources_error` 或在物化时处理错误。
    副作用：无；只读取映射和值。
    """

    changed: dict[int, StreamDockN4ProKeyImageSource] = {}
    for key, source in current.items():
        old_source = previous.get(key)
        if isinstance(source, Path):
            if not isinstance(old_source, Path) or old_source != source:
                changed[key] = source
        elif old_source is not source:
            changed[key] = source
    for key in previous.keys() - current.keys():
        changed[key] = _REMOVED_KEY_IMAGE
    return changed


def _key_image_source_path(
    source: StreamDockN4ProKeyImageSource,
    *,
    temp_dir: Path | None,
    temp_paths: list[Path],
) -> Path:
    """把静态键图来源转换为可直接交给 SDK 的路径。

    入参：`source` 是 Pillow 图或预渲染 Path；`temp_dir` 是 Pillow 编码临时目录；
    `temp_paths` 收集由本函数新建且应在本轮结束时删除的 PNG。
    返回：Path 输入原样返回，Pillow 输入返回新编码的临时 PNG 路径。
    错误处理：Path 不存在或不是文件时抛 ValueError；Pillow 保存错误按原异常传播。
    副作用：仅 Pillow 来源会创建临时 PNG；预渲染 Path 不复制、不编码、也不加入清理列表。
    """

    if isinstance(source, Path):
        if not source.is_file():
            raise ValueError(f"key image path must be an existing file: {source}")
        return source
    if not isinstance(source, Image.Image):
        raise TypeError("key image source must be Pillow Image or pathlib.Path")
    key_path = _save_temp_png(source, temp_dir=temp_dir)
    temp_paths.append(key_path)
    return key_path


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


def _device_is_writable(device: StreamDockN4ProDeviceLike) -> bool | None:
    """尽量读取当前 HID 会话是否仍可写，供同 path 原地重启检测。

    入参：`device` 是 persistent animator 当前持有的 SDK device。
    返回：SDK 明确报告时返回 bool；旧版 SDK 没有健康接口时返回 None 并保留兼容复用行为。
    错误处理：健康检查抛异常时返回 False，按失效句柄处理。
    副作用：可能调用 SDK device 或 transport 的只读 `can_write()`，不写屏幕、不重置设备。
    """

    checker = getattr(device, "can_write", None)
    if not callable(checker):
        transport = getattr(device, "transport", None)
        checker = getattr(transport, "can_write", None)
    if not callable(checker):
        return None
    try:
        return bool(checker())
    except Exception:
        return False


def streamdock_sdk_result_failed(result: object | None) -> bool:
    """判断 StreamDock native 写操作是否返回失败码。

    入参：`result` 是 SDK/ctypes 返回值；旧版成功路径可能返回 None，新版 TransportResult
    以 0 表示成功，并可能把 native `-1` 暴露成无符号 `0xFFFFFFFF`。
    返回：非零整数返回 True；None、0 和布尔兼容值返回 False。
    错误处理：未知对象按旧 SDK 成功兼容处理，由异常路径和下一轮健康检查兜底。
    副作用：无；只检查内存返回值。
    """

    return isinstance(result, int) and not isinstance(result, bool) and result != 0


def _sdk_failure_message(failure: str, result: object) -> str:
    """构造保留原始 native 返回值的稳定硬件错误文本。

    入参：`failure` 是已经包含 `failed` 的操作说明；`result` 是 SDK 原始返回值。
    返回：适合 result/status 和测试断言的单行错误文本。
    错误处理：`str(result)` 异常按 Python 原语义传播；常见 SDK 标量不会触发。
    副作用：无。
    """

    return f"{failure}: SDK returned {result}"


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
