from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.ui_settings import router
from app.ui_settings import UiSettingsRepository
from tests.conversation_history_test_support import create_repository

NOW = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)


def _client(database_path: Path) -> TestClient:
    app = FastAPI()
    app.state.ui_settings_repository = UiSettingsRepository(
        database_path=database_path,
        clock=lambda: NOW,
    )
    app.include_router(router)
    return TestClient(app)


def test_get_returns_local_user_defaults_and_ignores_untrusted_user_header(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    create_repository(database_path)

    with _client(database_path) as client:
        response = client.get(
            "/ui-settings",
            headers={"X-User-ID": "untrusted-user"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "local",
        "desktop_portrait_layout": "right",
        "desktop_history_height_percent": 75,
        "compact_history_height_percent": 75,
        "characters": [
            {
                "character_id": "miori",
                "visible": True,
                "pinned": False,
                "pin_order": None,
            }
        ],
        "thread_pins": [],
    }


def test_partial_preference_update_is_persisted(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    create_repository(database_path)

    with _client(database_path) as client:
        updated = client.patch(
            "/ui-settings",
            json={
                "desktop_portrait_layout": "background",
                "desktop_history_height_percent": 50,
            },
        )
        restored = client.get("/ui-settings")

    assert updated.status_code == 200
    assert restored.json()["desktop_portrait_layout"] == "background"
    assert restored.json()["desktop_history_height_percent"] == 50
    assert restored.json()["compact_history_height_percent"] == 75


def test_invalid_or_empty_preference_patch_is_rejected(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    create_repository(database_path)

    with _client(database_path) as client:
        empty = client.patch("/ui-settings", json={})
        invalid = client.patch(
            "/ui-settings",
            json={"compact_history_height_percent": 60},
        )
        null_value = client.patch(
            "/ui-settings",
            json={"desktop_portrait_layout": None},
        )

    assert empty.status_code == 422
    assert invalid.status_code == 422
    assert null_value.status_code == 422


def test_character_hide_and_readd_preserve_character_pin(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    create_repository(database_path)

    with _client(database_path) as client:
        client.put("/ui-settings/characters/miori/pin")
        hidden = client.put(
            "/ui-settings/characters/miori",
            json={"visible": False},
        )
        restored = client.put(
            "/ui-settings/characters/miori",
            json={"visible": True},
        )

    hidden_miori = next(
        item for item in hidden.json()["characters"]
        if item["character_id"] == "miori"
    )
    restored_miori = next(
        item for item in restored.json()["characters"]
        if item["character_id"] == "miori"
    )
    assert hidden_miori == {
        "character_id": "miori",
        "visible": False,
        "pinned": True,
        "pin_order": 1,
    }
    assert restored_miori["visible"] is True
    assert restored_miori["pin_order"] == 1


def test_visible_character_must_exist_in_catalog(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    create_repository(database_path)

    with _client(database_path) as client:
        response = client.put(
            "/ui-settings/characters/unknown",
            json={"visible": True},
        )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "character_not_found"


def test_thread_pin_endpoint_returns_canonical_snapshot(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    conversations = create_repository(database_path)
    conversation = conversations.create_conversation("miori")
    path = (
        "/ui-settings/characters/miori/conversations/"
        f"{conversation.conversation_id}/pin"
    )

    with _client(database_path) as client:
        pinned = client.put(path)
        unpinned = client.delete(path)

    assert pinned.status_code == 200
    assert pinned.json()["thread_pins"] == [
        {
            "character_id": "miori",
            "conversation_id": str(conversation.conversation_id),
        }
    ]
    assert unpinned.json()["thread_pins"] == []


def test_pin_endpoints_report_missing_character_and_thread(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    create_repository(database_path)

    with _client(database_path) as client:
        missing_character = client.put(
            "/ui-settings/characters/akira/pin"
        )
        missing_thread = client.put(
            "/ui-settings/characters/miori/conversations/"
            "e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010/pin"
        )

    assert missing_character.status_code == 404
    assert missing_character.json()["detail"]["code"] == (
        "ui_character_not_added"
    )
    assert missing_thread.status_code == 404
    assert missing_thread.json()["detail"]["code"] == "ui_thread_not_found"
