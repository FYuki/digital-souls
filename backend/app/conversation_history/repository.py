import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from app.conversation_history._sqlite import (
    TURN_COLUMNS,
    SqliteSession,
    format_datetime,
    select_conversation,
    select_turn,
    turn_from_row,
)
from app.conversation_history.errors import (
    InvalidConversationIdError,
    InvalidUtcDatetimeError,
)
from app.conversation_history.models import (
    Conversation,
    ConversationTurn,
    PrivacySkipReason,
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
    TurnStatus,
)
from app.conversation_history.turn_state import require_turn_transition

ConnectionFactory = Callable[[Path], sqlite3.Connection]
Clock = Callable[[], datetime]
UuidFactory = Callable[[], UUID]


class ConversationHistoryRepository:
    def __init__(
        self,
        *,
        database_path: Path,
        stale_after: timedelta,
        retention: timedelta,
        clock: Clock,
        uuid_factory: UuidFactory,
        connection_factory: ConnectionFactory = sqlite3.connect,
    ) -> None:
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        self._stale_after = stale_after
        self._retention = retention
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._database = SqliteSession(database_path, connection_factory)

    def create_conversation(self, character_id: str) -> Conversation:
        _require_non_empty(character_id, "character_id")
        conversation_id = self._new_uuid4()
        now = self._now()
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO conversations "
                "(character_id, conversation_id, created_at) VALUES (?, ?, ?)",
                (character_id, str(conversation_id), format_datetime(now)),
            )
            return select_conversation(
                connection,
                character_id,
                conversation_id,
            )

    def resume_conversation(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> Conversation:
        _require_uuid4(conversation_id)
        with self._database.connection() as connection:
            return select_conversation(connection, character_id, conversation_id)

    def create_processing_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_input: ProcessingTurnInput,
    ) -> ConversationTurn:
        _require_non_empty(turn_input.sanitized_user_content, "sanitized_user_content")
        return self._create_turn(
            character_id,
            conversation_id,
            user_content=turn_input.sanitized_user_content,
            status=TurnStatus.PROCESSING,
            privacy_reason_code=None,
        )

    def create_privacy_skipped_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_input: PrivacySkippedTurnInput,
    ) -> ConversationTurn:
        return self._create_turn(
            character_id,
            conversation_id,
            user_content=None,
            status=TurnStatus.PRIVACY_SKIPPED,
            privacy_reason_code=turn_input.reason_code,
        )

    def complete_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        sanitized_assistant_content: str,
    ) -> ConversationTurn:
        _require_non_empty(
            sanitized_assistant_content,
            "sanitized_assistant_content",
        )
        now = self._now()
        with self._database.transaction() as connection:
            current = select_turn(
                connection,
                character_id,
                conversation_id,
                turn_id,
            )
            require_turn_transition(current.status, TurnStatus.COMPLETED)
            connection.execute(
                "UPDATE conversation_turns "
                "SET assistant_content = ?, status = ?, updated_at = ? "
                "WHERE character_id = ? AND conversation_id = ? AND turn_id = ?",
                (
                    sanitized_assistant_content,
                    TurnStatus.COMPLETED.value,
                    format_datetime(now),
                    character_id,
                    str(conversation_id),
                    str(turn_id),
                ),
            )
            return select_turn(
                connection,
                character_id,
                conversation_id,
                turn_id,
            )

    def fail_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> ConversationTurn:
        now = self._now()
        with self._database.transaction() as connection:
            current = select_turn(
                connection,
                character_id,
                conversation_id,
                turn_id,
            )
            require_turn_transition(current.status, TurnStatus.FAILED)
            connection.execute(
                "UPDATE conversation_turns SET status = ?, updated_at = ? "
                "WHERE character_id = ? AND conversation_id = ? AND turn_id = ?",
                (
                    TurnStatus.FAILED.value,
                    format_datetime(now),
                    character_id,
                    str(conversation_id),
                    str(turn_id),
                ),
            )
            return select_turn(
                connection,
                character_id,
                conversation_id,
                turn_id,
            )

    def recover_stale_processing(self) -> list[ConversationTurn]:
        now = self._now()
        cutoff = format_datetime(now - self._stale_after)
        updated_at = format_datetime(now)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT {TURN_COLUMNS} FROM conversation_turns "
                "WHERE status = ? AND updated_at < ? "
                "ORDER BY updated_at, turn_id",
                (TurnStatus.PROCESSING.value, cutoff),
            ).fetchall()
            turn_ids = [str(row[0]) for row in rows]
            if not turn_ids:
                return []
            placeholders = ", ".join("?" for _ in turn_ids)
            connection.execute(
                "UPDATE conversation_turns SET status = ?, updated_at = ? "
                f"WHERE status = ? AND updated_at < ? "
                f"AND turn_id IN ({placeholders})",
                (
                    TurnStatus.FAILED.value,
                    updated_at,
                    TurnStatus.PROCESSING.value,
                    cutoff,
                    *turn_ids,
                ),
            )
            return [
                select_turn(
                    connection,
                    str(row[1]),
                    UUID(str(row[2])),
                    UUID(str(row[0])),
                )
                for row in rows
            ]

    def list_turns(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> list[ConversationTurn]:
        _require_uuid4(conversation_id)
        self.recover_stale_processing()
        cutoff = format_datetime(self._now() - self._retention)
        with self._database.connection() as connection:
            select_conversation(connection, character_id, conversation_id)
            rows = connection.execute(
                f"SELECT {TURN_COLUMNS} FROM conversation_turns "
                "WHERE character_id = ? AND conversation_id = ? "
                "AND created_at >= ? ORDER BY created_at, turn_id",
                (character_id, str(conversation_id), cutoff),
            ).fetchall()
            return [turn_from_row(row) for row in rows]

    def _create_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        *,
        user_content: str | None,
        status: TurnStatus,
        privacy_reason_code: PrivacySkipReason | None,
    ) -> ConversationTurn:
        _require_uuid4(conversation_id)
        turn_id = self._new_uuid4()
        now = self._now()
        timestamp = format_datetime(now)
        with self._database.transaction() as connection:
            select_conversation(connection, character_id, conversation_id)
            connection.execute(
                "INSERT INTO conversation_turns "
                f"({TURN_COLUMNS}) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    str(turn_id),
                    character_id,
                    str(conversation_id),
                    user_content,
                    status.value,
                    None
                    if privacy_reason_code is None
                    else privacy_reason_code.value,
                    timestamp,
                    timestamp,
                ),
            )
            return select_turn(
                connection,
                character_id,
                conversation_id,
                turn_id,
            )

    def _new_uuid4(self) -> UUID:
        value = self._uuid_factory()
        _require_uuid4(value)
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidUtcDatetimeError()
        return value.astimezone(UTC)

def _require_uuid4(value: UUID) -> None:
    if value.version != 4:
        raise InvalidConversationIdError(value)


def _require_non_empty(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
