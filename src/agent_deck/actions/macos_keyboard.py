"""macOS 前台应用键盘快捷键执行器。

本模块通过公开 AppKit/CoreGraphics API 把已校验的物理键步骤投递给执行开始时固定的
前台应用 PID。它不生成文本、不执行任意 shell、不自动申请辅助功能权限，也不把序列任务
排队；只有显式 UI 操作会用固定系统命令打开辅助功能设置。线程调度和 busy 策略由
``KeyboardShortcutScheduler`` 负责。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from agent_deck.actions.keyboard import (
    KEY_HOLD_MILLISECONDS,
    KeyboardModifier,
    KeyboardShortcutCapability,
    KeyboardShortcutExecutor,
    KeyboardShortcutPermissionRequester,
    KeyboardShortcutPermissionRequesterKind,
    KeyboardShortcutRunResult,
    KeyboardShortcutRunStatus,
    KeyboardShortcutSpec,
)

_KEY_CODES: dict[str, int] = {
    "KeyA": 0,
    "KeyS": 1,
    "KeyD": 2,
    "KeyF": 3,
    "KeyH": 4,
    "KeyG": 5,
    "KeyZ": 6,
    "KeyX": 7,
    "KeyC": 8,
    "KeyV": 9,
    "KeyB": 11,
    "KeyQ": 12,
    "KeyW": 13,
    "KeyE": 14,
    "KeyR": 15,
    "KeyY": 16,
    "KeyT": 17,
    "Digit1": 18,
    "Digit2": 19,
    "Digit3": 20,
    "Digit4": 21,
    "Digit6": 22,
    "Digit5": 23,
    "Equal": 24,
    "Digit9": 25,
    "Digit7": 26,
    "Minus": 27,
    "Digit8": 28,
    "Digit0": 29,
    "BracketRight": 30,
    "KeyO": 31,
    "KeyU": 32,
    "BracketLeft": 33,
    "KeyI": 34,
    "KeyP": 35,
    "Enter": 36,
    "KeyL": 37,
    "KeyJ": 38,
    "Quote": 39,
    "KeyK": 40,
    "Semicolon": 41,
    "Backslash": 42,
    "Comma": 43,
    "Slash": 44,
    "KeyN": 45,
    "KeyM": 46,
    "Period": 47,
    "Tab": 48,
    "Space": 49,
    "Backquote": 50,
    "Backspace": 51,
    "Escape": 53,
    "F17": 64,
    "NumpadDecimal": 65,
    "NumpadMultiply": 67,
    "NumpadAdd": 69,
    "NumLock": 71,
    "NumpadDivide": 75,
    "NumpadEnter": 76,
    "NumpadSubtract": 78,
    "F18": 79,
    "F19": 80,
    "NumpadEqual": 81,
    "Numpad0": 82,
    "Numpad1": 83,
    "Numpad2": 84,
    "Numpad3": 85,
    "Numpad4": 86,
    "Numpad5": 87,
    "Numpad6": 88,
    "Numpad7": 89,
    "F20": 90,
    "Numpad8": 91,
    "Numpad9": 92,
    "F5": 96,
    "F6": 97,
    "F7": 98,
    "F3": 99,
    "F8": 100,
    "F9": 101,
    "F11": 103,
    "F13": 105,
    "F16": 106,
    "F14": 107,
    "F10": 109,
    "F12": 111,
    "F15": 113,
    "Insert": 114,
    "Home": 115,
    "PageUp": 116,
    "Delete": 117,
    "F4": 118,
    "End": 119,
    "F2": 120,
    "PageDown": 121,
    "F1": 122,
    "ArrowLeft": 123,
    "ArrowRight": 124,
    "ArrowDown": 125,
    "ArrowUp": 126,
}

_MODIFIER_KEY_CODES = {
    KeyboardModifier.COMMAND: 55,
    KeyboardModifier.SHIFT: 56,
    KeyboardModifier.OPTION: 58,
    KeyboardModifier.CONTROL: 59,
}

_MODIFIER_FLAGS = {
    KeyboardModifier.SHIFT: 0x0002_0000,
    KeyboardModifier.CONTROL: 0x0004_0000,
    KeyboardModifier.OPTION: 0x0008_0000,
    KeyboardModifier.COMMAND: 0x0010_0000,
}

_NUMERIC_PAD_FLAG = 0x0020_0000

_ACCESSIBILITY_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)


class MacOSKeyboardNativeBridgeProtocol(Protocol):
    """定义 executor 依赖的最小 macOS native bridge。

    入参：实现提供权限检查、前台 PID 查询和单个键事件投递。
    返回：各方法返回布尔权限、PID 或无返回值。
    错误处理：native 调用失败可抛异常，executor 会返回 failed。
    副作用：``post_key_event`` 会向指定进程投递 CoreGraphics 事件。
    """

    def preflight_event_access(self) -> bool:
        """只读检查当前进程是否可投递键盘事件。"""

    def request_event_access(self) -> bool:
        """显式请求系统允许当前进程投递键盘事件。"""

    def frontmost_application_pid(self) -> int | None:
        """返回调用瞬间 AppKit 观察到的前台应用 PID。"""

    def post_key_event(
        self,
        *,
        pid: int,
        key_code: int,
        is_down: bool,
        flags: int,
    ) -> None:
        """向固定 PID 投递一个 key-down 或 key-up 事件。"""


class MacOSKeyboardNativeBridge:
    """使用 ctypes 封装 AppKit/CoreGraphics 所需公开 API。

    入参：构造时无参数，仅支持 Darwin。
    返回：可供 ``MacOSKeyboardShortcutExecutor`` 使用的 native bridge。
    错误处理：非 macOS、framework/symbol 缺失或 CGEvent 创建失败时抛 RuntimeError。
    副作用：加载系统 framework；权限请求方法可能显示系统提示；post 方法投递键盘事件。
    """

    def __init__(self) -> None:
        """加载系统 framework 并绑定函数签名。

        入参：无。
        返回：无显式返回值。
        错误处理：平台或动态库不支持时抛 RuntimeError。
        副作用：把 AppKit/CoreGraphics/CoreFoundation/libobjc 加载进当前进程。
        """

        if sys.platform != "darwin":
            raise RuntimeError("macOS keyboard bridge is only available on Darwin")
        try:
            ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
            self._core_graphics = ctypes.CDLL(
                "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
            )
            self._core_foundation = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
            objc_path = ctypes.util.find_library("objc")
            if not objc_path:
                raise RuntimeError("libobjc was not found")
            self._objc = ctypes.CDLL(objc_path)
            self._bind_functions()
        except OSError as exc:
            raise RuntimeError(f"failed to load macOS keyboard frameworks: {exc}") from exc

    def _bind_functions(self) -> None:
        """为 ctypes 函数设置严格参数和返回类型。

        入参：无；使用构造阶段加载的动态库。
        返回：无显式返回值。
        错误处理：必要 symbol 缺失时由 ctypes AttributeError 向上抛出。
        副作用：修改当前 ctypes function objects 的签名元数据。
        """

        self._core_graphics.CGPreflightPostEventAccess.argtypes = []
        self._core_graphics.CGPreflightPostEventAccess.restype = ctypes.c_bool
        self._core_graphics.CGRequestPostEventAccess.argtypes = []
        self._core_graphics.CGRequestPostEventAccess.restype = ctypes.c_bool
        self._core_graphics.CGEventCreateKeyboardEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_bool,
        ]
        self._core_graphics.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        self._core_graphics.CGEventSetFlags.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
        ]
        self._core_graphics.CGEventSetFlags.restype = None
        self._core_graphics.CGEventPostToPid.argtypes = [ctypes.c_int, ctypes.c_void_p]
        self._core_graphics.CGEventPostToPid.restype = None
        self._core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        self._core_foundation.CFRelease.restype = None

        self._objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self._objc.objc_getClass.restype = ctypes.c_void_p
        self._objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self._objc.sel_registerName.restype = ctypes.c_void_p
        message_address = ctypes.cast(
            self._objc.objc_msgSend,
            ctypes.c_void_p,
        ).value
        if message_address is None:
            raise RuntimeError("objc_msgSend address is unavailable")
        self._send_pointer = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(message_address)
        self._send_pid = ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(message_address)

    def preflight_event_access(self) -> bool:
        """只读检查辅助功能键盘事件投递权限。

        入参：无。
        返回：CoreGraphics preflight 结果。
        错误处理：native 调用异常按原样传播。
        副作用：不显示授权提示。
        """

        return bool(self._core_graphics.CGPreflightPostEventAccess())

    def request_event_access(self) -> bool:
        """显式请求辅助功能键盘事件投递权限。

        入参：无。
        返回：CoreGraphics request 调用结束时的授权结果。
        错误处理：native 调用异常按原样传播。
        副作用：macOS 可能显示系统授权提示或打开隐私设置。
        """

        return bool(self._core_graphics.CGRequestPostEventAccess())

    def frontmost_application_pid(self) -> int | None:
        """通过 ``NSWorkspace.frontmostApplication`` 获取前台 PID。

        入参：无。
        返回：正 PID；无法获取时返回 None。
        错误处理：Objective-C runtime 异常按原样传播。
        副作用：只读 AppKit 当前 workspace 状态。
        """

        workspace_class = self._objc.objc_getClass(b"NSWorkspace")
        if not workspace_class:
            return None
        shared_workspace = self._send_pointer(
            workspace_class,
            self._objc.sel_registerName(b"sharedWorkspace"),
        )
        if not shared_workspace:
            return None
        application = self._send_pointer(
            shared_workspace,
            self._objc.sel_registerName(b"frontmostApplication"),
        )
        if not application:
            return None
        pid = int(
            self._send_pid(
                application,
                self._objc.sel_registerName(b"processIdentifier"),
            )
        )
        return pid if pid > 0 else None

    def post_key_event(
        self,
        *,
        pid: int,
        key_code: int,
        is_down: bool,
        flags: int,
    ) -> None:
        """构造并向固定 PID 投递一个 CoreGraphics 键盘事件。

        入参：目标 PID、macOS virtual key code、down/up 状态与 CGEventFlags。
        返回：无显式返回值。
        错误处理：CGEvent 创建失败时抛 RuntimeError。
        副作用：向目标进程投递事件，并释放本次创建的 CF 对象。
        """

        event = self._core_graphics.CGEventCreateKeyboardEvent(
            None,
            key_code,
            is_down,
        )
        if not event:
            raise RuntimeError(f"CGEventCreateKeyboardEvent failed for key code {key_code}")
        try:
            self._core_graphics.CGEventSetFlags(event, flags)
            self._core_graphics.CGEventPostToPid(pid, event)
        finally:
            self._core_foundation.CFRelease(event)


class MacOSKeyboardShortcutExecutor:
    """把快捷键规格同步投递给执行开始时固定的 macOS 前台应用。

    入参：可注入 native bridge 和 sleep 函数，便于无真实系统事件的单元测试。
    返回：实现 ``KeyboardShortcutExecutor`` 的平台执行器。
    错误处理：缺权限、无目标和 native 失败分别返回明确终态；finally 尽力释放已按下键。
    副作用：成功路径会向一个固定 PID 投递 key-down/key-up，并按规格短暂 sleep。
    """

    def __init__(
        self,
        bridge: MacOSKeyboardNativeBridgeProtocol | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """初始化 macOS 快捷键执行器。

        入参：``bridge`` 为空时创建真实 ctypes bridge；``sleep`` 默认 ``time.sleep``。
        返回：无显式返回值。
        错误处理：真实 bridge 构造失败时按原样传播。
        副作用：默认加载 macOS framework，但不申请权限、不投递事件。
        """

        self._bridge = bridge or MacOSKeyboardNativeBridge()
        self._sleep = sleep

    def capability(self) -> KeyboardShortcutCapability:
        """只读返回 macOS 键盘投递能力和当前权限。

        入参：无。
        返回：supported=True 的 capability，permission 取 preflight 结果。
        错误处理：preflight 异常按原样传播。
        副作用：不显示系统权限提示。
        """

        granted = self._bridge.preflight_event_access()
        return KeyboardShortcutCapability(
            platform="darwin",
            supported=True,
            permission_granted=granted,
            can_request_permission=True,
            can_open_system_settings=True,
            permission_requester=_current_permission_requester(),
            message=(
                "可向执行开始时的前台应用投递快捷键"
                if granted
                else "需要在 macOS 辅助功能中允许当前 agent-deckd 执行宿主控制键盘"
            ),
        )

    def request_permission(self) -> KeyboardShortcutCapability:
        """从显式 UI 动作请求 macOS 键盘事件权限。

        入参：无。
        返回：请求完成后的 capability；不假设用户已在弹窗中立刻授权。
        错误处理：native 请求异常按原样传播。
        副作用：可能显示 macOS 辅助功能授权提示。
        """

        self._bridge.request_event_access()
        return self.capability()

    def execute(self, shortcut: KeyboardShortcutSpec) -> KeyboardShortcutRunResult:
        """向固定前台应用 PID 投递完整快捷键序列。

        入参：已通过跨平台模型校验的快捷键规格。
        返回：succeeded 表示所有事件已投递；不代表目标应用实际处理了动作。
        错误处理：缺权限/目标返回专用状态；其他异常返回 failed，并尽力释放所有已按下键。
        副作用：投递 CoreGraphics 键盘事件并按固定 hold、配置 delay 等待。
        """

        if not self._bridge.preflight_event_access():
            return KeyboardShortcutRunResult(
                status=KeyboardShortcutRunStatus.PERMISSION_REQUIRED,
                message="macOS keyboard event access is not granted",
            )
        target_pid = self._bridge.frontmost_application_pid()
        if target_pid is None:
            return KeyboardShortcutRunResult(
                status=KeyboardShortcutRunStatus.TARGET_UNAVAILABLE,
                message="frontmost application PID is unavailable",
            )

        active_modifiers: set[KeyboardModifier] = set()
        pressed: list[tuple[int, KeyboardModifier | None, bool]] = []
        try:
            for step in shortcut.steps:
                for modifier in step.modifiers:
                    active_modifiers.add(modifier)
                    key_code = _MODIFIER_KEY_CODES[modifier]
                    self._bridge.post_key_event(
                        pid=target_pid,
                        key_code=key_code,
                        is_down=True,
                        flags=_flags_for(active_modifiers),
                    )
                    pressed.append((key_code, modifier, False))

                if step.key is not None:
                    key_code = _KEY_CODES[step.key]
                    is_numpad = step.key.startswith("Numpad") or step.key == "NumLock"
                    self._bridge.post_key_event(
                        pid=target_pid,
                        key_code=key_code,
                        is_down=True,
                        flags=_flags_for(active_modifiers, numeric_pad=is_numpad),
                    )
                    pressed.append((key_code, None, is_numpad))

                self._sleep(KEY_HOLD_MILLISECONDS / 1_000)

                if step.key is not None:
                    key_code, _modifier, is_numpad = pressed[-1]
                    self._bridge.post_key_event(
                        pid=target_pid,
                        key_code=key_code,
                        is_down=False,
                        flags=_flags_for(active_modifiers, numeric_pad=is_numpad),
                    )
                    pressed.pop()

                for modifier in reversed(step.modifiers):
                    active_modifiers.discard(modifier)
                    key_code = _MODIFIER_KEY_CODES[modifier]
                    self._bridge.post_key_event(
                        pid=target_pid,
                        key_code=key_code,
                        is_down=False,
                        flags=_flags_for(active_modifiers),
                    )
                    _remove_last_pressed_modifier(pressed, modifier)

                if step.delay_after_ms:
                    self._sleep(step.delay_after_ms / 1_000)
        except Exception as exc:  # noqa: BLE001 - native 失败要转成动作状态并进入清理。
            return KeyboardShortcutRunResult(
                status=KeyboardShortcutRunStatus.FAILED,
                target_pid=target_pid,
                message=f"failed while posting keyboard events: {exc}",
            )
        finally:
            _release_pressed_keys(
                self._bridge,
                pid=target_pid,
                pressed=pressed,
                active_modifiers=active_modifiers,
            )

        return KeyboardShortcutRunResult(
            status=KeyboardShortcutRunStatus.SUCCEEDED,
            target_pid=target_pid,
            message="keyboard events were posted to the pinned frontmost application",
        )


class UnsupportedKeyboardShortcutExecutor:
    """在非 macOS 平台提供 fail-closed 快捷键执行器。

    入参：可选平台名称，默认当前 ``sys.platform``。
    返回：始终 unsupported 的 executor。
    错误处理：不抛业务异常。
    副作用：无；不会投递系统事件或请求权限。
    """

    def __init__(self, platform: str | None = None) -> None:
        """保存不支持的平台名称。

        入参：可选平台名称。
        返回：无显式返回值。
        错误处理：无。
        副作用：无。
        """

        self._platform = platform or sys.platform

    def capability(self) -> KeyboardShortcutCapability:
        """返回 unsupported capability 且不请求权限。

        入参：无。
        返回：supported=False 的快照。
        错误处理：无。
        副作用：无。
        """

        return KeyboardShortcutCapability(
            platform=self._platform,
            supported=False,
            permission_granted=False,
            can_request_permission=False,
            message="keyboard shortcuts are currently implemented only for macOS",
        )

    def request_permission(self) -> KeyboardShortcutCapability:
        """返回 unchanged unsupported capability。

        入参：无。
        返回：``capability()``。
        错误处理：无。
        副作用：无。
        """

        return self.capability()

    def execute(self, shortcut: KeyboardShortcutSpec) -> KeyboardShortcutRunResult:
        """拒绝执行非 macOS 快捷键。

        入参：合法 shortcut，仅用于符合 executor 协议。
        返回：unsupported run result。
        错误处理：无。
        副作用：无。
        """

        del shortcut
        return KeyboardShortcutRunResult(
            status=KeyboardShortcutRunStatus.UNSUPPORTED,
            message="keyboard shortcuts are currently implemented only for macOS",
        )


def create_default_keyboard_shortcut_executor() -> KeyboardShortcutExecutor:
    """按当前平台创建默认快捷键执行器。

    入参：无。
    返回：macOS 使用真实 CoreGraphics executor，其他平台使用 fail-closed 实现。
    错误处理：macOS framework 加载失败时返回 unsupported executor 并把平台标为 darwin-error。
    副作用：macOS 路径加载系统 framework，但不申请权限、不投递事件。
    """

    if sys.platform != "darwin":
        return UnsupportedKeyboardShortcutExecutor()
    try:
        return MacOSKeyboardShortcutExecutor()
    except Exception:  # noqa: BLE001 - daemon 应启动并在 capability 中 fail-closed。
        return UnsupportedKeyboardShortcutExecutor(platform="darwin-error")


def open_macos_accessibility_settings() -> None:
    """由显式 UI 动作打开 macOS 辅助功能隐私设置。

    入参：无；目标 URL 是模块内固定常量，不接受用户输入。
    返回：成功时无显式返回值。
    错误处理：非 macOS、``open`` 缺失、超时或非零退出时抛 RuntimeError。
    副作用：启动或聚焦系统设置的“隐私与安全性 -> 辅助功能”页面；不修改任何授权开关。
    """

    if sys.platform != "darwin":
        raise RuntimeError("macOS accessibility settings are unavailable on this platform")
    try:
        subprocess.run(
            ["/usr/bin/open", _ACCESSIBILITY_SETTINGS_URL],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"failed to open macOS accessibility settings: {exc}") from exc


def _current_permission_requester() -> KeyboardShortcutPermissionRequester:
    """识别当前 Python 进程向 macOS 发起权限请求时使用的运行身份。

    入参：无；读取 ``sys.executable`` 的真实路径。
    返回：App bundle 或开发运行时请求者说明。
    错误处理：路径解析失败时仍以原始 executable 名称返回开发运行时说明。
    副作用：只解析进程内字符串和本地路径，不读取或修改 TCC 数据库。
    """

    raw_executable = sys.executable or "python"
    try:
        executable = Path(raw_executable).resolve()
    except OSError:
        executable = Path(raw_executable)
    bundle_path = _containing_app_bundle(executable)
    if bundle_path is not None:
        return KeyboardShortcutPermissionRequester(
            kind=KeyboardShortcutPermissionRequesterKind.APP_BUNDLE,
            display_name=bundle_path.stem,
            executable_path=str(executable),
            stable_identity=True,
            note="打包 App 执行器；浏览器只负责配置，无需辅助功能权限",
        )
    return KeyboardShortcutPermissionRequester(
        kind=KeyboardShortcutPermissionRequesterKind.DEVELOPMENT_RUNTIME,
        display_name=executable.name or "python",
        executable_path=str(executable),
        stable_identity=False,
        note=(
            "开发运行时；macOS 可能按启动链显示为 Codex、Terminal 或 Python，"
            "切换启动方式后可能需要重新授权"
        ),
    )


def _containing_app_bundle(executable: Path) -> Path | None:
    """查找执行文件路径中最近的 macOS ``.app`` bundle。

    入参：当前真实 executable 路径。
    返回：包含 executable 的最内层 ``.app`` 路径；开发运行时返回 None。
    错误处理：纯路径运算不抛业务异常。
    副作用：无；不访问文件内容。
    """

    parts = executable.parts
    candidates = [index for index, part in enumerate(parts) if part.endswith(".app")]
    if not candidates:
        return None
    return Path(*parts[: candidates[-1] + 1])


def _flags_for(
    modifiers: set[KeyboardModifier],
    *,
    numeric_pad: bool = False,
) -> int:
    """把 active 修饰键转换成 CoreGraphics flags。

    入参：当前 active 修饰键集合；``numeric_pad`` 表示主键来自数字键盘。
    返回：按位组合后的整数 flags。
    错误处理：未知枚举不会出现；若出现按字典 KeyError 暴露。
    副作用：无。
    """

    flags = 0
    for modifier in modifiers:
        flags |= _MODIFIER_FLAGS[modifier]
    if numeric_pad:
        flags |= _NUMERIC_PAD_FLAG
    return flags


def _remove_last_pressed_modifier(
    pressed: list[tuple[int, KeyboardModifier | None, bool]],
    modifier: KeyboardModifier,
) -> None:
    """从 pressed 栈移除刚成功释放的指定修饰键。

    入参：pressed 栈和目标修饰键。
    返回：无显式返回值。
    错误处理：未找到时安全无操作，finally 仍会处理其他键。
    副作用：原地修改 pressed 列表。
    """

    for index in range(len(pressed) - 1, -1, -1):
        if pressed[index][1] == modifier:
            del pressed[index]
            return


def _release_pressed_keys(
    bridge: MacOSKeyboardNativeBridgeProtocol,
    *,
    pid: int,
    pressed: list[tuple[int, KeyboardModifier | None, bool]],
    active_modifiers: set[KeyboardModifier],
) -> None:
    """finally 中尽力释放所有可能仍处于 down 状态的键。

    入参：native bridge、固定 PID、pressed 栈和 active 修饰集合。
    返回：无显式返回值。
    错误处理：单个 key-up 失败被吞掉，继续释放其余键，避免掩盖原始执行异常。
    副作用：可能向目标 PID 投递补偿性 key-up，并清空两个内存集合。
    """

    while pressed:
        key_code, modifier, is_numpad = pressed.pop()
        if modifier is not None:
            active_modifiers.discard(modifier)
        try:
            bridge.post_key_event(
                pid=pid,
                key_code=key_code,
                is_down=False,
                flags=_flags_for(
                    active_modifiers,
                    numeric_pad=is_numpad and modifier is None,
                ),
            )
        except Exception:  # noqa: BLE001 - cleanup 必须 best-effort 继续释放。
            continue
    active_modifiers.clear()
