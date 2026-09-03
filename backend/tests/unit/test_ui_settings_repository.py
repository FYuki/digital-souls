import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.ui_settings import PortraitLayout, UiSettingsRepository
from tests.conversation_history_test_support import create_repository

NOW = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
LOCAL_USER = "local"


def _settings(database_path: Path) -> UiSettingsRepository:
    return UiSettingsRepository(database_path=database_path, clock=lambda: NOW)


def test_default_settings_are_created_for_an_explicit_local_user(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    create_repository(database_path)

    snapshot = _settings(database_path).get(LOCAL_USER)

    assert snapshot.user_id == LOCAL_USER
    assert snapshot.preferences.desktop_portrait_layout is PortraitLayout.RIGHT
    assert snapshot.preferences.desktop_history_height_percent == 75
    assert snapshot.preferences.compact_history_height_percent == 75
    assert [(item.character_id, item.visible, item.pin_order) for item in snapshot.characters] == [
        ("miori", True, None)
    ]
    assert snapshot.thread_pins == ()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT user_id FROM ui_settings"
        ).fetchall() == [(LOCAL_USER,)]


def test_pc_and_compact_history_heights_are_updated_independently(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    create_repository(database_path)
    settings = _settings(database_path)

    settings.update_preferences(
        LOCAL_USER,
        desktop_portrait_layout=PortraitLayout.BACKGROUND,
        desktop_history_height_percent=50,
    )
    snapshot = settings.update_preferences(
        LOCAL_USER,
        compact_history_height_percent=100,
    )

    assert snapshot.preferences.desktop_portrait_layout is PortraitLayout.BACKGROUND
    assert snapshot.preferences.desktop_history_height_percent == 50
    assert snapshot.preferences.compact_history_height_percent == 100


def test_last_preference_write_wins(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    create_repository(database_path)
    settings = _settings(database_path)

    settings.update_preferences(
        LOCAL_USER,
        desktop_history_height_percent=50,
    )
    snapshot = settings.update_preferences(
        LOCAL_USER,
        desktop_history_height_percent=100,
    )

    assert snapshot.preferences.desktop_history_height_percent == 100


def test_hidden_character_restores_character_and_thread_pins_when_readded(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    conversations = create_repository(database_path)
    conversation = conversations.create_conversation("akira")
    settings = _settings(database_path)
    settings.set_character_visibility(LOCAL_USER, "akira", visible=True)
    settings.set_character_pinned(LOCAL_USER, "akira", pinned=True)
    settings.set_thread_pinned(
        LOCAL_USER,
        "akira",
        conversation.conversation_id,
        pinned=True,
    )

    hidden = settings.set_character_visibility(
        LOCAL_USER,
        "akira",
        visible=False,
    )
    restored = settings.set_character_visibility(
        LOCAL_USER,
        "akira",
        visible=True,
    )

    hidden_akira = next(
        item for item in hidden.characters if item.character_id == "akira"
    )
    restored_akira = next(
        item for item in restored.characters if item.character_id == "akira"
    )
    assert hidden_akira.visible is False
    assert hidden_akira.pin_order == 1
    assert restored_akira.visible is True
    assert restored_akira.pin_order == 1
    assert [(pin.character_id, pin.conversation_id) for pin in restored.thread_pins] == [
        ("akira", conversation.conversation_id)
    ]


def test_character_pin_order_follows_pin_actions(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    create_repository(database_path)
    settings = _settings(database_path)
    settings.set_character_visibility(LOCAL_USER, "akira", visible=True)

    settings.set_character_pinned(LOCAL_USER, "miori", pinned=True)
    pinned = settings.set_character_pinned(LOCAL_USER, "akira", pinned=True)
    settings.set_character_pinned(LOCAL_USER, "miori", pinned=False)
    repinned = settings.set_character_pinned(LOCAL_USER, "miori", pinned=True)

    assert [(item.character_id, item.pin_order) for item in pinned.characters] == [
        ("miori", 1),
        ("akira", 2),
    ]
    assert [(item.character_id, item.pin_order) for item in repinned.characters] == [
        ("akira", 2),
        ("miori", 3),
    ]


def test_archive_preserves_thread_pin_and_hard_delete_removes_it(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    conversations = create_repository(database_path)
    conversation = conversations.create_conversation("miori")
    settings = _settings(database_path)
    settings.set_thread_pinned(
        LOCAL_USER,
        "miori",
        conversation.conversation_id,
        pinned=True,
    )

    conversations.archive_conversation("miori", conversation.conversation_id)
    assert settings.get(LOCAL_USER).thread_pins[0].conversation_id == (
        conversation.conversation_id
    )
    conversations.unarchive_conversation("miori", conversation.conversation_id)
    assert settings.get(LOCAL_USER).thread_pins[0].conversation_id == (
        conversation.conversation_id
    )

    conversations.hard_delete_conversation("miori", conversation.conversation_id)

    assert settings.get(LOCAL_USER).thread_pins == ()


def test_repeated_thread_pin_preserves_original_order(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    conversations = create_repository(database_path)
    first = conversations.create_conversation("miori")
    second = conversations.create_conversation("miori")
    current = [NOW]
    settings = UiSettingsRepository(
        database_path=database_path,
        clock=lambda: current[0],
    )

    settings.set_thread_pinned(
        LOCAL_USER,
        "miori",
        first.conversation_id,
        pinned=True,
    )
    current[0] += timedelta(seconds=1)
    settings.set_thread_pinned(
        LOCAL_USER,
        "miori",
        second.conversation_id,
        pinned=True,
    )
    current[0] += timedelta(seconds=1)
    snapshot = settings.set_thread_pinned(
        LOCAL_USER,
        "miori",
        first.conversation_id,
        pinned=True,
    )

    assert [pin.conversation_id for pin in snapshot.thread_pins] == [
        first.conversation_id,
        second.conversation_id,
    ]


def test_different_user_ids_do_not_share_settings_or_pins(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    conversations = create_repository(database_path)
    conversation = conversations.create_conversation("miori")
    settings = _settings(database_path)
    settings.update_preferences(
        LOCAL_USER,
        desktop_portrait_layout=PortraitLayout.BACKGROUND,
    )
    settings.set_thread_pinned(
        LOCAL_USER,
        "miori",
        conversation.conversation_id,
        pinned=True,
    )

    other = settings.get("sns-user-1")

    assert other.preferences.desktop_portrait_layout is PortraitLayout.RIGHT
    assert other.thread_pins == ()
    assert settings.get(LOCAL_USER).thread_pins != ()
