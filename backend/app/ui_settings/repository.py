import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.characters.catalog import is_valid_character_id
from app.conversation_history._sqlite import SqliteSession, format_datetime
from app.ui_settings.errors import (
    UiCharacterNotAddedError,
    UiThreadNotFoundError,
)
from app.ui_settings.models import (
    CharacterUiState,
    PortraitLayout,
    ThreadPin,
    UiPreferences,
    UiSettingsSnapshot,
)

ConnectionFactory = Callable[[Path], sqlite3.Connection]
Clock = Callable[[], datetime]
DEFAULT_CHARACTER_ID = "miori"
ALLOWED_HISTORY_HEIGHT_PERCENTAGES = frozenset({50, 75, 100})


class UiSettingsRepository:
    def __init__(
        self,
        *,
        database_path: Path,
        clock: Clock,
        connection_factory: ConnectionFactory = sqlite3.connect,
    ) -> None:
        self._database = SqliteSession(database_path, connection_factory)
        self._clock = clock

    def get(self, user_id: str) -> UiSettingsSnapshot:
        _require_user_id(user_id)
        with self._database.transaction() as connection:
            self._ensure_user(connection, user_id)
            return self._snapshot(connection, user_id)

    def update_preferences(
        self,
        user_id: str,
        *,
        desktop_portrait_layout: PortraitLayout | None = None,
        desktop_history_height_percent: int | None = None,
        compact_history_height_percent: int | None = None,
    ) -> UiSettingsSnapshot:
        _require_user_id(user_id)
        if (
            desktop_portrait_layout is None
            and desktop_history_height_percent is None
            and compact_history_height_percent is None
        ):
            raise ValueError("at least one preference is required")
        for value in (
            desktop_history_height_percent,
            compact_history_height_percent,
        ):
            if (
                value is not None
                and value not in ALLOWED_HISTORY_HEIGHT_PERCENTAGES
            ):
                raise ValueError("history height must be 50, 75, or 100")
        updates: list[str] = []
        values: list[object] = []
        if desktop_portrait_layout is not None:
            updates.append("desktop_portrait_layout = ?")
            values.append(desktop_portrait_layout.value)
        if desktop_history_height_percent is not None:
            updates.append("desktop_history_height_percent = ?")
            values.append(desktop_history_height_percent)
        if compact_history_height_percent is not None:
            updates.append("compact_history_height_percent = ?")
            values.append(compact_history_height_percent)
        updates.append("updated_at = ?")
        values.append(format_datetime(self._now()))
        values.append(user_id)
        with self._database.transaction() as connection:
            self._ensure_user(connection, user_id)
            connection.execute(
                f"UPDATE ui_settings SET {', '.join(updates)} WHERE user_id = ?",
                values,
            )
            return self._snapshot(connection, user_id)

    def set_character_visibility(
        self,
        user_id: str,
        character_id: str,
        *,
        visible: bool,
    ) -> UiSettingsSnapshot:
        _require_user_id(user_id)
        _require_character_id(character_id)
        now = format_datetime(self._now())
        with self._database.transaction() as connection:
            self._ensure_user(connection, user_id)
            existing = connection.execute(
                "SELECT 1 FROM ui_characters "
                "WHERE user_id = ? AND character_id = ?",
                (user_id, character_id),
            ).fetchone()
            if existing is None and not visible:
                raise UiCharacterNotAddedError(character_id)
            connection.execute(
                "INSERT INTO ui_characters "
                "(user_id, character_id, is_visible, pin_order, added_at, updated_at) "
                "VALUES (?, ?, ?, NULL, ?, ?) "
                "ON CONFLICT(user_id, character_id) DO UPDATE SET "
                "is_visible = excluded.is_visible, updated_at = excluded.updated_at",
                (user_id, character_id, int(visible), now, now),
            )
            self._touch_user(connection, user_id, now)
            return self._snapshot(connection, user_id)

    def set_character_pinned(
        self,
        user_id: str,
        character_id: str,
        *,
        pinned: bool,
    ) -> UiSettingsSnapshot:
        _require_user_id(user_id)
        _require_character_id(character_id)
        now = format_datetime(self._now())
        with self._database.transaction() as connection:
            self._ensure_user(connection, user_id)
            row = connection.execute(
                "SELECT pin_order FROM ui_characters "
                "WHERE user_id = ? AND character_id = ?",
                (user_id, character_id),
            ).fetchone()
            if row is None:
                raise UiCharacterNotAddedError(character_id)
            if pinned and row[0] is None:
                next_order = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(pin_order), 0) + 1 "
                        "FROM ui_characters WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    "UPDATE ui_characters SET pin_order = ?, updated_at = ? "
                    "WHERE user_id = ? AND character_id = ?",
                    (next_order, now, user_id, character_id),
                )
            elif not pinned and row[0] is not None:
                connection.execute(
                    "UPDATE ui_characters SET pin_order = NULL, updated_at = ? "
                    "WHERE user_id = ? AND character_id = ?",
                    (now, user_id, character_id),
                )
            self._touch_user(connection, user_id, now)
            return self._snapshot(connection, user_id)

    def set_thread_pinned(
        self,
        user_id: str,
        character_id: str,
        conversation_id: UUID,
        *,
        pinned: bool,
    ) -> UiSettingsSnapshot:
        _require_user_id(user_id)
        _require_character_id(character_id)
        now = format_datetime(self._now())
        with self._database.transaction() as connection:
            self._ensure_user(connection, user_id)
            character = connection.execute(
                "SELECT 1 FROM ui_characters "
                "WHERE user_id = ? AND character_id = ?",
                (user_id, character_id),
            ).fetchone()
            if character is None:
                raise UiCharacterNotAddedError(character_id)
            conversation = connection.execute(
                "SELECT 1 FROM conversations "
                "WHERE character_id = ? AND conversation_id = ?",
                (character_id, str(conversation_id)),
            ).fetchone()
            if conversation is None:
                raise UiThreadNotFoundError(str(conversation_id))
            if pinned:
                connection.execute(
                    "INSERT INTO ui_thread_pins "
                    "(user_id, character_id, conversation_id, pinned_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(user_id, character_id, conversation_id) "
                    "DO NOTHING",
                    (user_id, character_id, str(conversation_id), now),
                )
            else:
                connection.execute(
                    "DELETE FROM ui_thread_pins WHERE user_id = ? "
                    "AND character_id = ? AND conversation_id = ?",
                    (user_id, character_id, str(conversation_id)),
                )
            self._touch_user(connection, user_id, now)
            return self._snapshot(connection, user_id)

    def _ensure_user(
        self,
        connection: sqlite3.Connection,
        user_id: str,
    ) -> None:
        now = format_datetime(self._now())
        connection.execute(
            "INSERT OR IGNORE INTO ui_settings (user_id, updated_at) VALUES (?, ?)",
            (user_id, now),
        )
        connection.execute(
            "INSERT OR IGNORE INTO ui_characters "
            "(user_id, character_id, is_visible, pin_order, added_at, updated_at) "
            "VALUES (?, ?, 1, NULL, ?, ?)",
            (user_id, DEFAULT_CHARACTER_ID, now, now),
        )

    @staticmethod
    def _touch_user(
        connection: sqlite3.Connection,
        user_id: str,
        now: str,
    ) -> None:
        connection.execute(
            "UPDATE ui_settings SET updated_at = ? WHERE user_id = ?",
            (now, user_id),
        )

    @staticmethod
    def _snapshot(
        connection: sqlite3.Connection,
        user_id: str,
    ) -> UiSettingsSnapshot:
        preference = connection.execute(
            "SELECT desktop_portrait_layout, "
            "desktop_history_height_percent, compact_history_height_percent "
            "FROM ui_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if preference is None:
            raise RuntimeError("UI settings row is missing")
        characters = tuple(
            CharacterUiState(
                character_id=str(row[0]),
                visible=bool(row[1]),
                pin_order=None if row[2] is None else int(row[2]),
            )
            for row in connection.execute(
                "SELECT character_id, is_visible, pin_order FROM ui_characters "
                "WHERE user_id = ? ORDER BY "
                "CASE WHEN pin_order IS NULL THEN 1 ELSE 0 END, "
                "pin_order, added_at, character_id",
                (user_id,),
            )
        )
        thread_pins = tuple(
            ThreadPin(character_id=str(row[0]), conversation_id=UUID(str(row[1])))
            for row in connection.execute(
                "SELECT character_id, conversation_id FROM ui_thread_pins "
                "WHERE user_id = ? ORDER BY pinned_at, character_id, conversation_id",
                (user_id,),
            )
        )
        return UiSettingsSnapshot(
            user_id=user_id,
            preferences=UiPreferences(
                desktop_portrait_layout=PortraitLayout(str(preference[0])),
                desktop_history_height_percent=int(preference[1]),
                compact_history_height_percent=int(preference[2]),
            ),
            characters=characters,
            thread_pins=thread_pins,
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return an aware datetime")
        return now.astimezone(UTC)


def _require_user_id(user_id: str) -> None:
    if not user_id or user_id != user_id.strip():
        raise ValueError("user_id must be non-empty and trimmed")


def _require_character_id(character_id: str) -> None:
    if not is_valid_character_id(character_id):
        raise ValueError("character_id must be ASCII lowercase kebab-case")
