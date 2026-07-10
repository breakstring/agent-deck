"""受限的本机音频与显示器控制执行器。

本模块是 Agent Deck rotary action 的唯一系统副作用边界：输入 router 只能产生 intent，daemon
再调用这里的 executor。默认 macOS 实现仅用 AppleScript 操作当前默认音频设备；系统显示器亮度
只在平台后端明确枚举到可控 target 时开放，当前没有安全 detector 的平台返回空集合而不是伪造能力。
测试可使用纯内存 executor，避免访问本机设置。
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

_CONTROL_STEP_PERCENT = 2


class SystemDisplayTarget(BaseModel):
    """描述 capability scan 确认可调节亮度的一块系统显示器。

    入参：`id` 是持久化目标 id；`label` 是 GUI 展示名；`brightness_percent` 是最近确认值。
    返回：frozen Pydantic model。
    错误处理：空 id/label 或越界亮度由校验拒绝。
    副作用：仅保存内存数据。
    """

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    brightness_percent: int = Field(ge=0, le=100)

    @field_validator("id", "label")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        """校验显示器标识和文案非空。

        入参：`value` 是待清理字符串。
        返回：去除首尾空白后的字符串。
        错误处理：空字符串抛 ValueError。
        副作用：无。
        """

        normalized = value.strip()
        if not normalized:
            raise ValueError("system display target text must not be empty")
        return normalized


class SystemControlResult(BaseModel):
    """描述一次受限系统控制调用的确认结果。

    入参：`ok` 表示执行器已确认成功；`status` 是稳定诊断；`value_percent` 或 `muted` 是可选
    已确认状态；`message` 适合短暂错误/状态反馈。
    返回：frozen Pydantic model。
    错误处理：空 action/status/message 或越界百分比由校验拒绝。
    副作用：仅保存结果快照。
    """

    model_config = ConfigDict(frozen=True)

    action: str
    ok: bool
    status: str
    message: str
    value_percent: int | None = Field(default=None, ge=0, le=100)
    muted: bool | None = None


class SystemControlExecutor(Protocol):
    """定义 runtime 调用的受限本机控制边界。

    入参：协议方法只接受固定 2% 步进或已 capability-confirmed display id。
    返回：每次调用必须返回 `SystemControlResult`，避免 runtime 以缓存假装成功。
    错误处理：实现不得抛出未分类的系统命令错误；应转为 `ok=False` 结果。
    副作用：真实实现会修改系统设置，fake 实现只修改内存。
    """

    def adjust_output_volume(self, delta_percent: int) -> SystemControlResult:
        """按固定步进调节默认输出音量。"""

    def toggle_output_mute(self) -> SystemControlResult:
        """读取真实输出静音状态后切换。"""

    def adjust_input_volume(self, delta_percent: int) -> SystemControlResult:
        """按固定步进调节默认输入音量。"""

    def toggle_input_mute(self) -> SystemControlResult:
        """按输入音量零值语义切换麦克风静音。"""

    def list_display_targets(self) -> tuple[SystemDisplayTarget, ...]:
        """返回平台已确认可读写亮度的显示器集合。"""

    def adjust_system_display_brightness(
        self,
        target_id: str | None,
        delta_percent: int,
    ) -> SystemControlResult:
        """只对 capability-confirmed 系统显示器调节亮度。"""


class InMemorySystemControlExecutor:
    """用于测试与 fake hardware 的确定性系统控制替身。

    入参：可选输出/输入音量、输出静音和已确认系统显示器集合。
    返回：提供与真实 executor 相同的接口。
    错误处理：未知显示器返回 `ok=False`，不抛系统异常。
    副作用：仅修改该实例的内存状态。
    """

    def __init__(
        self,
        *,
        output_volume_percent: int = 50,
        input_volume_percent: int = 50,
        output_muted: bool = False,
        displays: tuple[SystemDisplayTarget, ...] = (),
    ) -> None:
        """初始化可预测的 fake 系统状态。

        入参：各连续数值会在 0 到 100 内夹紧；`displays` 仅包含可控目标。
        返回：无显式返回值。
        错误处理：无业务异常。
        副作用：初始化内存字段。
        """

        self.output_volume_percent = _clamp_percent(output_volume_percent)
        self.input_volume_percent = _clamp_percent(input_volume_percent)
        self.output_muted = output_muted
        self._input_restore_percent = max(1, self.input_volume_percent)
        self._displays = {display.id: display for display in displays}

    def adjust_output_volume(self, delta_percent: int) -> SystemControlResult:
        """用固定调用方步进改变 fake 输出音量。

        入参：`delta_percent` 是正负百分比步进。
        返回：成功且含夹紧后百分比的结果。
        错误处理：无。
        副作用：修改 fake 输出音量。
        """

        self.output_volume_percent = _clamp_percent(
            self.output_volume_percent + delta_percent
        )
        return _value_result("adjust_output_volume", self.output_volume_percent)

    def toggle_output_mute(self) -> SystemControlResult:
        """翻转 fake 输出静音状态。

        入参：无。
        返回：成功且包含新 muted 状态的结果。
        错误处理：无。
        副作用：修改 fake 输出静音字段。
        """

        self.output_muted = not self.output_muted
        return _mute_result("toggle_output_mute", self.output_muted)

    def adjust_input_volume(self, delta_percent: int) -> SystemControlResult:
        """用固定调用方步进改变 fake 输入音量。

        入参：`delta_percent` 是正负百分比步进。
        返回：成功且含夹紧后百分比的结果。
        错误处理：无。
        副作用：修改 fake 输入音量和非零恢复值。
        """

        self.input_volume_percent = _clamp_percent(self.input_volume_percent + delta_percent)
        if self.input_volume_percent > 0:
            self._input_restore_percent = self.input_volume_percent
        return _value_result("adjust_input_volume", self.input_volume_percent)

    def toggle_input_mute(self) -> SystemControlResult:
        """在 fake 中以输入音量为零表示麦克风静音并保存恢复值。

        入参：无。
        返回：成功且包含新 muted 状态的结果。
        错误处理：无。
        副作用：修改 fake 输入音量或恢复值。
        """

        if self.input_volume_percent > 0:
            self._input_restore_percent = self.input_volume_percent
            self.input_volume_percent = 0
            return _mute_result("toggle_input_mute", True)
        self.input_volume_percent = self._input_restore_percent
        return _mute_result("toggle_input_mute", False)

    def list_display_targets(self) -> tuple[SystemDisplayTarget, ...]:
        """返回 fake capability-confirmed 显示器快照。

        入参：无。
        返回：按 id 排序的不可变目标元组。
        错误处理：无。
        副作用：无。
        """

        return tuple(self._displays[key] for key in sorted(self._displays))

    def adjust_system_display_brightness(
        self,
        target_id: str | None,
        delta_percent: int,
    ) -> SystemControlResult:
        """调节一个 fake capability-confirmed 显示器亮度。

        入参：`target_id` 是显示器 id；`delta_percent` 是正负步进。
        返回：未知 id 返回 unavailable；成功时返回夹紧后亮度。
        错误处理：无系统异常。
        副作用：更新 fake 显示器快照。
        """

        target = self._displays.get(target_id or "")
        if target is None:
            return _unavailable_result(
                "adjust_system_display_brightness",
                "没有可控制的系统显示器",
            )
        updated = target.model_copy(
            update={"brightness_percent": _clamp_percent(target.brightness_percent + delta_percent)}
        )
        self._displays[updated.id] = updated
        return _value_result("adjust_system_display_brightness", updated.brightness_percent)


class MacOSSystemControlExecutor:
    """基于 AppleScript 的 macOS 默认音频控制实现。

    入参：`script_runner` 可替换以便测试；默认 runner 调用 `/usr/bin/osascript`。macOS 内建 API
    不提供稳定的第三方显示器 brightness target scan，因此 `list_display_targets` 保守返回空元组；
    input mute 使用输入音量为零并保存最后确认的非零值。
    返回：实现 `SystemControlExecutor` 的对象。
    错误处理：所有 AppleScript 失败转换成 `ok=False` 结果。
    副作用：成功调用会修改当前默认音频设备设置。
    """

    def __init__(self, script_runner: Callable[[str], str] | None = None) -> None:
        """初始化 macOS executor。

        入参：`script_runner` 为 None 时使用真实 osascript runner。
        返回：无显式返回值。
        错误处理：构造不执行系统命令。
        副作用：仅初始化内存 runner 和输入恢复值。
        """

        self._script_runner = script_runner or _run_osascript
        self._input_restore_percent = 50

    def adjust_output_volume(self, delta_percent: int) -> SystemControlResult:
        """读取当前输出音量、按步进夹紧并写回 macOS 默认输出设备。

        入参：`delta_percent` 是正负百分比步进。
        返回：成功时含确认百分比，脚本失败时返回错误结果。
        错误处理：AppleScript/解析错误转为 `ok=False`。
        副作用：成功时写 macOS 输出音量。
        """

        current = self._read_int("output volume of (get volume settings)")
        if current is None:
            return _unavailable_result("adjust_output_volume", "无法读取系统输出音量")
        updated = _clamp_percent(current + delta_percent)
        return self._write_int("adjust_output_volume", f"set volume output volume {updated}", updated)

    def toggle_output_mute(self) -> SystemControlResult:
        """读取当前输出 muted 状态后在 macOS 中翻转。

        入参：无。
        返回：成功时含翻转后的 muted 状态，失败时返回错误结果。
        错误处理：AppleScript/布尔解析失败转为 `ok=False`。
        副作用：成功时写 macOS 输出静音。
        """

        current = self._read_bool("output muted of (get volume settings)")
        if current is None:
            return _unavailable_result("toggle_output_mute", "无法读取系统输出静音状态")
        updated = not current
        try:
            self._script_runner(f"set volume output muted {'true' if updated else 'false'}")
        except OSError as exc:
            return _unavailable_result("toggle_output_mute", str(exc))
        return _mute_result("toggle_output_mute", updated)

    def adjust_input_volume(self, delta_percent: int) -> SystemControlResult:
        """读取当前输入音量、按步进夹紧并写回 macOS 默认输入设备。

        入参：`delta_percent` 是正负百分比步进。
        返回：成功时含确认百分比，失败时返回错误结果。
        错误处理：AppleScript/解析失败转为 `ok=False`。
        副作用：成功时写 macOS 输入音量。
        """

        current = self._read_int("input volume of (get volume settings)")
        if current is None:
            return _unavailable_result("adjust_input_volume", "无法读取系统输入音量")
        updated = _clamp_percent(current + delta_percent)
        result = self._write_int("adjust_input_volume", f"set volume input volume {updated}", updated)
        if result.ok and updated > 0:
            self._input_restore_percent = updated
        return result

    def toggle_input_mute(self) -> SystemControlResult:
        """以输入音量为零的可观察状态实现 macOS 麦克风静音切换。

        入参：无。
        返回：输入大于零时置零并返回 muted；为零时恢复最后确认的非零值。
        错误处理：AppleScript/解析失败转为 `ok=False`。
        副作用：成功时写 macOS 输入音量。
        """

        current = self._read_int("input volume of (get volume settings)")
        if current is None:
            return _unavailable_result("toggle_input_mute", "无法读取系统输入音量")
        if current > 0:
            self._input_restore_percent = current
            result = self._write_int("toggle_input_mute", "set volume input volume 0", 0)
            return result.model_copy(update={"muted": True})
        restore = max(1, self._input_restore_percent)
        result = self._write_int(
            "toggle_input_mute",
            f"set volume input volume {restore}",
            restore,
        )
        return result.model_copy(update={"muted": False})

    def list_display_targets(self) -> tuple[SystemDisplayTarget, ...]:
        """返回当前 macOS 后端已确认可控制的显示器集合。

        入参：无。
        返回：空元组；不调用私有 CoreDisplay API，也不假定任意外接屏支持 DDC/CI。
        错误处理：无。
        副作用：无。
        """

        return ()

    def adjust_system_display_brightness(
        self,
        target_id: str | None,
        delta_percent: int,
    ) -> SystemControlResult:
        """拒绝当前 macOS 后端无法 capability-confirm 的显示器亮度写入。

        入参：`target_id` 和 `delta_percent` 仅为协议兼容保留。
        返回：明确 unavailable 结果。
        错误处理：无。
        副作用：无；不调用私有或猜测性 API。
        """

        del target_id, delta_percent
        return _unavailable_result(
            "adjust_system_display_brightness",
            "当前 macOS 后端未发现可控制的显示器亮度目标",
        )

    def _read_int(self, expression: str) -> int | None:
        """执行 AppleScript 表达式并保守解析百分比整数。

        入参：`expression` 是只读取 volume settings 的 AppleScript 表达式。
        返回：0 到 100 内整数，失败时 None。
        错误处理：runner 异常或返回非整数时吞掉并返回 None。
        副作用：真实 runner 会读取 macOS 音频设置。
        """

        try:
            return _clamp_percent(int(self._script_runner(f"get {expression}").strip()))
        except (OSError, ValueError):
            return None

    def _read_bool(self, expression: str) -> bool | None:
        """执行 AppleScript 表达式并保守解析布尔值。

        入参：`expression` 是只读取 volume settings 的 AppleScript 表达式。
        返回：true/false 对应 bool，其他值或异常返回 None。
        错误处理：runner 异常转为 None。
        副作用：真实 runner 会读取 macOS 音频设置。
        """

        try:
            value = self._script_runner(f"get {expression}").strip().lower()
        except OSError:
            return None
        if value == "true":
            return True
        if value == "false":
            return False
        return None

    def _write_int(
        self,
        action: str,
        script: str,
        expected_value: int,
    ) -> SystemControlResult:
        """执行一个已构造的音量写入并返回标准结果。

        入参：`action` 是稳定动作 id；`script` 是固定格式 AppleScript；`expected_value` 是写入值。
        返回：成功或 unavailable 结果。
        错误处理：runner OSError 转为 `ok=False`。
        副作用：成功时修改 macOS 音频设置。
        """

        try:
            self._script_runner(script)
        except OSError as exc:
            return _unavailable_result(action, str(exc))
        return _value_result(action, expected_value)


def create_default_system_control_executor() -> SystemControlExecutor:
    """按当前平台创建保守的系统控制 executor。

    入参：无。
    返回：macOS 使用 AppleScript executor；其他平台使用无 capability 的内存 executor。
    错误处理：构造阶段不执行系统命令。
    副作用：只创建内存对象。
    """

    if sys.platform == "darwin":
        return MacOSSystemControlExecutor()
    return InMemorySystemControlExecutor()


def _run_osascript(script: str) -> str:
    """执行一条固定来源的 AppleScript 并返回标准输出。

    入参：`script` 由本模块内部固定模板构造，不接受用户输入拼接。
    返回：去除末尾换行前的 stdout。
    错误处理：非零退出、找不到可执行文件或超时转为 OSError。
    副作用：会启动 `osascript` 子进程，脚本可能读写系统音频设置。
    """

    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"osascript failed: {exc}") from exc
    return result.stdout


def _clamp_percent(value: int) -> int:
    """把一个连续控制值限制到 0 至 100 的有效范围。

    入参：`value` 是待夹紧整数。
    返回：0 到 100 的整数。
    错误处理：无。
    副作用：无。
    """

    return max(0, min(100, value))


def _value_result(action: str, value_percent: int) -> SystemControlResult:
    """创建连续数值控制成功结果。

    入参：`action` 是稳定动作 id；`value_percent` 是已确认值。
    返回：`ok=True` 的标准结果。
    错误处理：无。
    副作用：无。
    """

    return SystemControlResult(
        action=action,
        ok=True,
        status="succeeded",
        message="系统控制已更新",
        value_percent=_clamp_percent(value_percent),
    )


def _mute_result(action: str, muted: bool) -> SystemControlResult:
    """创建静音切换成功结果。

    入参：`action` 是稳定动作 id；`muted` 是切换后的确认状态。
    返回：`ok=True` 的标准结果。
    错误处理：无。
    副作用：无。
    """

    return SystemControlResult(
        action=action,
        ok=True,
        status="succeeded",
        message="已静音" if muted else "已取消静音",
        muted=muted,
    )


def _unavailable_result(action: str, message: str) -> SystemControlResult:
    """创建不能安全执行时的标准失败结果。

    入参：`action` 是稳定动作 id；`message` 是可展示诊断。
    返回：`ok=False`、`status=unavailable` 的结果。
    错误处理：无。
    副作用：无。
    """

    return SystemControlResult(
        action=action,
        ok=False,
        status="unavailable",
        message=message,
    )
