"""N4 Pro 旋钮布局 JSON 存储测试。

本文件仅验证临时目录中的读取、校验和原子写入，不访问真实 StreamDock、系统控制或用户级配置。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_deck.rendering.rotary_surface import default_n4pro_rotary_layout
from agent_deck.server.rotary_layout_store import (
    RotaryLayoutStoreError,
    load_n4pro_rotary_layout,
    save_n4pro_rotary_layout,
)


def test_rotary_layout_store_round_trips_complete_layout(tmp_path: Path) -> None:
    """有效旋钮布局应通过版本化 envelope 原子保存并按原值读回。

    入参：`tmp_path` 是 pytest 提供的隔离存储目录。
    返回：无返回值；断言通过表示 layout JSON 可供 daemon 重启恢复。
    错误处理：文件或模型内容不一致时由 pytest 报告。
    副作用：在临时目录创建一个 JSON 文件。
    """

    path = tmp_path / "n4pro-rotary-layout.json"
    layout = default_n4pro_rotary_layout()

    save_n4pro_rotary_layout(layout, path)

    assert load_n4pro_rotary_layout(path) == layout
    assert json.loads(path.read_text(encoding="utf-8"))["device_profile"] == "mirabox.n4pro"


def test_rotary_layout_store_rejects_wrong_profile(tmp_path: Path) -> None:
    """其他硬件 profile 的 JSON 不得被 N4 Pro rotary store 静默接收。

    入参：`tmp_path` 是 pytest 提供的隔离目录。
    返回：无返回值；断言通过表示跨型号配置不会被误加载。
    错误处理：错误 profile 必须转换为 `RotaryLayoutStoreError`。
    副作用：在临时目录写入一个手工构造的 JSON 文件。
    """

    path = tmp_path / "wrong-profile.json"
    path.write_text(
        json.dumps({"version": 1, "device_profile": "mirabox.n3", "layout": {}}),
        encoding="utf-8",
    )

    with pytest.raises(RotaryLayoutStoreError, match="device_profile"):
        load_n4pro_rotary_layout(path)
