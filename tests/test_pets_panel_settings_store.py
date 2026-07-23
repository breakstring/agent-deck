"""验证 N4 Pro PETS 面板设置的持久化和 API 往返。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_deck.config import CodexPetPatrolSpeed, CodexRemotePetSource
from agent_deck.server.app import create_app
from agent_deck.server.pets_panel_settings_store import (
    N4ProPetsPanelSettings,
    load_n4pro_pets_panel_settings,
    save_n4pro_pets_panel_settings,
)


def test_pets_panel_settings_json_round_trip(tmp_path: Path) -> None:
    """两个用户设置经过版本化 JSON 保存后保持枚举语义。"""

    path = tmp_path / "n4pro-pets-panel.json"
    settings = N4ProPetsPanelSettings(
        remote_pet_source=CodexRemotePetSource.REMOTE_CONFIG,
        patrol_speed=CodexPetPatrolSpeed.FAST,
    )

    save_n4pro_pets_panel_settings(settings, path)

    assert load_n4pro_pets_panel_settings(path) == settings


def test_pets_panel_settings_api_persists_and_applies(tmp_path: Path) -> None:
    """GUI API 保存后立即返回 applied 设置，重建 app 后仍能读回。"""

    path = tmp_path / "n4pro-pets-panel.json"
    payload = {
        "remote_pet_source": "follow_local",
        "patrol_speed": "slow",
    }
    with TestClient(create_app(pets_panel_settings_path=path)) as client:
        response = client.put("/ui/pets-panel-settings", json=payload)
        assert response.status_code == 200
        assert response.json()["pets_panel_settings"]["settings"] == payload

    with TestClient(create_app(pets_panel_settings_path=path)) as client:
        response = client.get("/ui/pets-panel-settings")
        assert response.status_code == 200
        assert response.json()["source"] == "persisted"
        assert response.json()["settings"] == payload
        page = client.get("/").text
        assert 'id="petsPanelControl"' in page
        assert "PETS 虚拟面板" in page
