"""系统音频与显示器控制 executor 的单元测试。

本文件只使用 fake executor 验证固定 2% 步进、真实状态切换和 capability 降级，不调用
AppleScript、Windows API、DDC/CI、真实音频或显示器。
"""

from __future__ import annotations

from agent_deck.actions.system_controls import (
    InMemorySystemControlExecutor,
    SystemDisplayTarget,
)


def test_system_control_executor_clamps_continuous_values_at_two_percent_steps() -> None:
    """连续值调节应固定以 2% 步进并在 0 到 100 内夹紧。

    入参：无；测试以接近边界的 fake 输出/输入音量开始。
    返回：无返回值；断言通过表示 GUI 无需配置额外步长。
    错误处理：值未夹紧或步进错误时由 pytest 报告。
    副作用：仅修改 fake executor 内存状态。
    """

    executor = InMemorySystemControlExecutor(output_volume_percent=99, input_volume_percent=1)

    assert executor.adjust_output_volume(2).value_percent == 100
    assert executor.adjust_input_volume(-2).value_percent == 0


def test_mute_toggles_read_current_state_and_input_mute_uses_zero_volume() -> None:
    """输出静音与输入静音应基于当前状态切换，输入零音量代表已静音。

    入参：无；测试连续执行两次切换。
    返回：无返回值；断言通过表示单个物理按下可往返切换而不是单向设置。
    错误处理：状态未反转或输入音量未恢复时由 pytest 报告。
    副作用：仅修改 fake executor 内存。
    """

    executor = InMemorySystemControlExecutor(input_volume_percent=37)

    assert executor.toggle_output_mute().muted is True
    assert executor.toggle_output_mute().muted is False
    assert executor.toggle_input_mute().muted is True
    assert executor.input_volume_percent == 0
    assert executor.toggle_input_mute().muted is False
    assert executor.input_volume_percent == 37


def test_system_display_actions_require_capability_confirmed_target() -> None:
    """系统显示器亮度只能操作 executor 明确暴露的目标。

    入参：无；测试先构造无目标，再构造单一可控目标。
    返回：无返回值；断言通过表示不支持的显示器不会被假装为可控制。
    错误处理：不可用状态或目标选择错误时由 pytest 报告。
    副作用：仅修改 fake executor 内存。
    """

    unavailable = InMemorySystemControlExecutor()
    assert unavailable.list_display_targets() == ()
    assert unavailable.adjust_system_display_brightness("missing", 2).ok is False

    executor = InMemorySystemControlExecutor(
        displays=(SystemDisplayTarget(id="primary", label="Built-in", brightness_percent=50),)
    )
    result = executor.adjust_system_display_brightness("primary", 2)

    assert result.ok is True
    assert result.value_percent == 52
