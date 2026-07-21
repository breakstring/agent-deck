"""Agent Deck 本地配置模型的定向测试。

本模块只在 pytest 临时目录读写 TOML，用于验证 Codex 宠物配置默认值和 round-trip；
不会读取真实 ``~/.codex``、加载宠物图集、启动 daemon 或访问硬件。
"""

from __future__ import annotations

import pytest

from agent_deck.config import (
    AgentDeckConfigError,
    CodexPetMotion,
    load_agent_deck_config,
)


def test_codex_pet_config_defaults_follow_codex_without_pet_id(tmp_path) -> None:
    """缺省配置应启用只读跟随，并且没有第二套宠物选择字段。

    入参：pytest ``tmp_path`` 提供不存在的配置路径。
    返回：无；断言通过代表默认刷新、FPS、motion 和无 ``pet_id`` 合同稳定。
    错误处理：字段缺失或默认值漂移时由 pytest 报告。
    副作用：不创建文件，只读取不存在路径并返回默认模型。
    """

    config = load_agent_deck_config(tmp_path / "missing.toml")

    assert config.codex.pet.enabled is True
    assert config.codex.pet.refresh_interval_seconds == 5.0
    assert config.codex.pet.panel_fps == 8
    assert config.codex.pet.motion == CodexPetMotion.AUTO
    assert "pet_id" not in config.codex.pet.model_dump()


def test_codex_pet_config_round_trip_and_validation(tmp_path) -> None:
    """TOML 应完整解析宠物开关、刷新率、FPS 与 reduced motion。

    入参：pytest ``tmp_path`` 用于写入隔离配置。
    返回：无；断言通过代表配置可供 CLI 映射到 daemon。
    错误处理：未知 motion 应转换为 ``AgentDeckConfigError``。
    副作用：仅在 pytest 临时目录写两个小型 TOML 文件。
    """

    config_path = tmp_path / "agent-deck.toml"
    config_path.write_text(
        "\n".join(
            (
                "[codex.pet]",
                "enabled = false",
                "refresh_interval_seconds = 2.5",
                "panel_fps = 6",
                'motion = "reduced"',
            )
        ),
        encoding="utf-8",
    )
    pet = load_agent_deck_config(config_path).codex.pet

    assert pet.enabled is False
    assert pet.refresh_interval_seconds == 2.5
    assert pet.panel_fps == 6
    assert pet.motion == CodexPetMotion.REDUCED

    config_path.write_text('[codex.pet]\nmotion = "unknown"\n', encoding="utf-8")
    with pytest.raises(AgentDeckConfigError, match="motion"):
        load_agent_deck_config(config_path)
