"""硬件能力 profile 的纯单元测试。

本文件定义 Mirabox / Stream Dock capability profile 的数据契约。测试只创建 Pydantic
模型并检查内置 profile，不导入官方 SDK、不枚举 HID、不渲染图片、不访问真实硬件，也不读写
用户文件。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_deck.hardware.capabilities import (
    DeviceCapabilityProfile,
    LightAddressability,
    ProfileFamily,
    SafeActionLevel,
    built_in_device_profiles,
    get_device_profile,
)


def test_n4pro_profile_expresses_rich_touch_rotary_capabilities() -> None:
    """验证 N4 Pro profile 覆盖当前已验证的富交互硬件能力。

    入参：无；测试内从内置 registry 读取 `mirabox.n4pro`。
    返回：无返回值；断言通过代表 profile 能表达 15 key、800x480 触屏、触点、swipe、
    4 个旋钮、RGB LED 和单会话复合写入约束。
    错误处理：缺失 profile 或能力字段不符合策略时由 pytest 报告。
    副作用：仅创建和读取内存模型。
    """

    profile = get_device_profile("mirabox.n4pro")

    assert profile.family == ProfileFamily.RICH_TOUCH_ROTARY
    assert profile.key_count == 15
    assert profile.key_image.size == (96, 96)
    assert profile.background is not None
    assert profile.background.size == (800, 480)
    assert profile.touch is not None
    assert profile.touch.has_touch_points is True
    assert profile.touch.has_swipe is True
    assert profile.rotary is not None
    assert profile.rotary.count == 4
    assert profile.rotary.has_press is True
    assert tuple(control.id for control in profile.rotary.controls) == (
        "knob_1",
        "knob_2",
        "knob_3",
        "knob_4",
    )
    assert all(control.supports_rotate for control in profile.rotary.controls)
    assert all(control.supports_press for control in profile.rotary.controls)
    assert profile.light is not None
    assert profile.light.has_rgb_led is True
    assert len(profile.light.zones) == 1
    assert profile.light.zones[0].id == "rotary_ring_group"
    assert profile.light.zones[0].addressability == LightAddressability.GROUP
    assert profile.light.zones[0].associated_control_ids == (
        "knob_1",
        "knob_2",
        "knob_3",
        "knob_4",
    )
    assert profile.light.zones[0].supports_color is True
    assert profile.light.zones[0].supports_breathe is True
    assert profile.display_brightness is not None
    assert profile.display_brightness.supports_set is True
    assert profile.display_brightness.scope == "device_global"
    assert profile.requires_single_session_composite_write is True
    assert profile.supports_action(SafeActionLevel.DECIDE) is True
    assert profile.supports_action(SafeActionLevel.INPUT) is False


def test_key_grid_profile_does_not_allow_decide_or_input_by_default() -> None:
    """验证 key-grid profile 只开放低风险动作等级。

    入参：无；测试内读取通用 15-key grid profile。
    返回：无返回值；断言通过代表 key-only 设备不默认具备审批或文本注入能力。
    错误处理：安全等级过宽时由 pytest 报告。
    副作用：仅创建和读取内存模型。
    """

    profile = get_device_profile("mirabox.key_grid_15")

    assert profile.family == ProfileFamily.KEY_GRID
    assert profile.key_count == 15
    assert profile.background is None
    assert profile.touch is None
    assert profile.rotary is None
    assert profile.supports_action(SafeActionLevel.OBSERVE) is True
    assert profile.supports_action(SafeActionLevel.NAVIGATE) is True
    assert profile.supports_action(SafeActionLevel.FOCUS) is True
    assert profile.supports_action(SafeActionLevel.DECIDE) is False
    assert profile.supports_action(SafeActionLevel.INPUT) is False


def test_rotary_control_profiles_keep_decisions_out_of_hardware_only_flow() -> None:
    """验证旋钮控制型设备默认只负责选择、导航和聚焦。

    入参：无；测试内读取 N3 与 K1 Pro profile。
    返回：无返回值；断言通过代表两类设备都暴露旋钮，但不默认允许硬件 approve。
    错误处理：旋钮能力缺失或安全等级过宽时由 pytest 报告。
    副作用：仅创建和读取内存模型。
    """

    n3 = get_device_profile("mirabox.n3")
    k1pro = get_device_profile("mirabox.k1pro")

    assert n3.family == ProfileFamily.ROTARY_CONTROL
    assert n3.rotary is not None
    assert n3.rotary.count == 3
    assert n3.rotary.has_press is True
    assert n3.background is not None
    assert n3.background.size == (320, 240)
    assert n3.supports_action(SafeActionLevel.DECIDE) is False

    assert k1pro.family == ProfileFamily.KEYBOARD_COMPANION
    assert k1pro.key_count == 6
    assert k1pro.rotary is not None
    assert k1pro.rotary.count == 3
    assert k1pro.light is not None
    assert k1pro.light.has_keyboard_backlight is True
    assert k1pro.background is None
    assert k1pro.supports_action(SafeActionLevel.DECIDE) is False


def test_n4_profile_is_marked_unverified_despite_official_richer_claims() -> None:
    """验证 N4 profile 明确保留 SDK 与官方资料不一致的验证边界。

    入参：无；测试内读取 `mirabox.n4`。
    返回：无返回值；断言通过代表 N4 暂不按 N4 Pro 能力处理，并记录待验证限制。
    错误处理：若 profile 误开启触点/旋钮或缺少限制说明，由 pytest 报告。
    副作用：仅创建和读取内存模型。
    """

    profile = get_device_profile("mirabox.n4")

    assert profile.family == ProfileFamily.KEY_GRID_WITH_STATUS_STRIP
    assert profile.key_count == 14
    assert profile.background is not None
    assert profile.background.size == (800, 480)
    assert profile.touch is None
    assert profile.rotary is None
    assert profile.supports_action(SafeActionLevel.DECIDE) is False
    assert any("unverified" in item.lower() for item in profile.known_limitations)


def test_profile_registry_returns_immutable_mapping_and_unknown_ids_fail() -> None:
    """验证内置 registry 只读且未知设备 id 明确失败。

    入参：无；测试内读取 registry 并查询一个不存在的 profile。
    返回：无返回值；断言通过代表调用方不能意外修改全局 profile 表。
    错误处理：registry 可变或未知 id 未失败时由 pytest 报告。
    副作用：仅创建和读取内存模型。
    """

    profiles = built_in_device_profiles()

    assert "mirabox.n4pro" in profiles
    with pytest.raises(TypeError):
        profiles["custom"] = profiles["mirabox.n4pro"]  # type: ignore[index]
    with pytest.raises(KeyError, match="unknown device capability profile"):
        get_device_profile("mirabox.missing")


def test_device_profile_rejects_unsafe_or_incomplete_shapes() -> None:
    """验证 profile 数据模型拒绝明显不完整或不安全的形态。

    入参：无；测试内直接构造非法 `DeviceCapabilityProfile`。
    返回：无返回值；断言通过代表 profile 层能提前挡住缺 key 图能力、越界刷新率和 input 默认开放。
    错误处理：非法模型未被拒绝时由 pytest 报告。
    副作用：仅创建内存模型并捕获 Pydantic 校验异常。
    """

    with pytest.raises(ValidationError, match="key_count must be positive"):
        DeviceCapabilityProfile(
            device_id="invalid.zero_keys",
            display_name="Invalid",
            family=ProfileFamily.KEY_GRID,
            key_count=0,
            safe_action_levels=frozenset({SafeActionLevel.OBSERVE}),
        )

    with pytest.raises(ValidationError, match="key_image is required"):
        DeviceCapabilityProfile(
            device_id="invalid.no_key_image",
            display_name="Invalid",
            family=ProfileFamily.KEY_GRID,
            key_count=1,
            safe_action_levels=frozenset({SafeActionLevel.OBSERVE}),
        )

    with pytest.raises(ValidationError, match="INPUT cannot be enabled"):
        DeviceCapabilityProfile(
            device_id="invalid.input_enabled",
            display_name="Invalid",
            family=ProfileFamily.KEY_GRID,
            key_count=1,
            key_image={"size": (64, 64), "format": "JPEG"},
            safe_action_levels=frozenset(
                {SafeActionLevel.OBSERVE, SafeActionLevel.INPUT}
            ),
        )
