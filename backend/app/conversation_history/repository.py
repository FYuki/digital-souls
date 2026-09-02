import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from app.conversation_history._sqlite import (
    TURN_COLUMNS,
    SqliteSession,
    conversation_not_found_error,
    conversation_from_row,
    format_datetime,
    parse_datetime,
    select_conversation,
    select_turn,
    turn_from_row,
)
from app.conversation_history.errors import (
    ConversationNotFoundError,
    InvalidConversationIdError,
    InvalidUtcDatetimeError,
)
from app.conversation_history.models import (
    Conversation,
    ConversationTurn,
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
    TurnStatus,
)
from app.conversation_history.turn_state import require_turn_transition
from app.conversation_history.titles import (
    DEFAULT_CONVERSATION_TITLE,
    generate_conversation_title,
    normalize_manual_conversation_title,
)
from app.conversation_history.wal_cleanup import ConversationWalCleanup
from app.privacy.contracts import HistoryDecisionReasonCode

ConnectionFactory = Callable[[Path], sqlite3.Connection]
Clock = Callable[[], datetime]
UuidFactory = Callable[[], UUID]
MIN_PROMPT_PAGE_SIZE = 1
MAX_PROMPT_PAGE_SIZE = 100
CONVERSATION_SELECT_COLUMNS = (
    "c.character_id, c.conversation_id, c.created_at, "
    "COALESCE(MAX(t.updated_at), c.created_at), c.archived_at, "
    "c.title, c.title_is_manual"
)


@dataclass(frozen=True)
class PromptHistoryCursor:
    created_at: datetime
    turn_id: UUID
    retention_cutoff: datetime


@dataclass(frozen=True)
class PromptHistoryPage:
    turns: tuple[ConversationTurn, ...]
    next_cursor: PromptHistoryCursor | None


class ConversationHistoryRepository:
    def __init__(
        self,
        *,
        database_path: Path,
        stale_after: timedelta,
        retention: timedelta,
        clock: Clock,
        uuid_factory: UuidFactory,
        wal_cleanup: ConversationWalCleanup,
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
        self._wal_cleanup = wal_cleanup
        self._database = SqliteSession(database_path, connection_factory)

    def create_conversation(self, character_id: str) -> Conversation:
        _require_non_empty(character_id, "character_id")
        conversation_id = self._new_uuid4()
        now = self._now()
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO conversations "
                "(character_id, conversation_id, created_at, title, title_is_manual) "
                "VALUES (?, ?, ?, ?, 0)",
                (
                    character_id,
                    str(conversation_id),
                    format_datetime(now),
                    DEFAULT_CONVERSATION_TITLE,
                ),
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

    def list_active_conversations(self, character_id: str) -> list[Conversation]:
        _require_non_empty(character_id, "character_id")
        return self._select_conversations(
            character_id,
            archived_clause="IS NULL",
        )

    def list_archived_conversations(self, character_id: str) -> list[Conversation]:
        _require_non_empty(character_id, "character_id")
        return self._select_conversations(
            character_id,
            archived_clause="IS NOT NULL",
        )

    def rename_conversation(
        self,
        character_id: str,
        conversation_id: UUID,
        title: str,
    ) -> Conversation:
        _require_uuid4(conversation_id)
        normalized_title = normalize_manual_conversation_title(title)
        with self._database.transaction() as connection:
            self._select_conversation_for_lifecycle(
                connection,
                character_id,
                conversation_id,
            )
            connection.execute(
                "UPDATE conversations SET title = ?, title_is_manual = 1 "
                "WHERE character_id = ? AND conversation_id = ?",
                (normalized_title, character_id, str(conversation_id)),
            )
            return self._select_conversation_for_lifecycle(
                connection,
                character_id,
                conversation_id,
            )

    def archive_conversation(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> Conversation:
        _require_uuid4(conversation_id)
        with self._database.transaction() as connection:
            current = self._select_conversation_for_lifecycle(
                connection,
                character_id,
                conversation_id,
            )
            if current.archived_at is not None:
                raise ConversationNotFoundError()
            connection.execute(
                "UPDATE conversations SET archived_at = ? "
                "WHERE character_id = ? AND conversation_id = ?",
                (format_datetime(self._now()), character_id, str(conversation_id)),
            )
            return self._select_conversation_for_lifecycle(
                connection,
                character_id,
                conversation_id,
            )

    def unarchive_conversation(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> Conversation:
        _require_uuid4(conversation_id)
        with self._database.transaction() as connection:
            current = self._select_conversation_for_lifecycle(
                connection,
                character_id,
                conversation_id,
            )
            if current.archived_at is None:
                raise ConversationNotFoundError()
            connection.execute(
                "UPDATE conversations SET archived_at = NULL "
                "WHERE character_id = ? AND conversation_id = ?",
                (character_id, str(conversation_id)),
            )
            return self._select_conversation_for_lifecycle(
                connection,
                character_id,
                conversation_id,
            )

    def hard_delete_conversation(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> None:
        _require_uuid4(conversation_id)
        with self._database.transaction() as connection:
            self._select_conversation_for_lifecycle(
                connection,
                character_id,
                conversation_id,
            )
            connection.execute(
                "DELETE FROM conversation_turns WHERE character_id = ? "
                "AND conversation_id = ?",
                (character_id, str(conversation_id)),
            )
            connection.execute(
                "DELETE FROM conversations WHERE character_id = ? "
                "AND conversation_id = ?",
                (character_id, str(conversation_id)),
            )
        self._wal_cleanup.after_hard_delete(character_id, conversation_id)

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
            sanitizer_version=None,
            policy_version=None,
        )

    def consolidation_activity(self) -> tuple[int, datetime | None]:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(CASE WHEN status = ? THEN 1 END), MAX(updated_at) "
                "FROM conversation_turns",
                (TurnStatus.PROCESSING.value,),
            ).fetchone()
        if row is None:
            raise RuntimeError("conversation activity query returned no row")
        latest = None if row[1] is None else parse_datetime(str(row[1]))
        return int(row[0]), latest

    def get_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> ConversationTurn | None:
        _require_non_empty(character_id, "character_id")
        _require_uuid4(conversation_id)
        _require_uuid4(turn_id)
        with self._database.connection() as connection:
            row = connection.execute(
                f"SELECT {TURN_COLUMNS} FROM conversation_turns "
                "WHERE character_id = ? AND conversation_id = ? AND turn_id = ?",
                (character_id, str(conversation_id), str(turn_id)),
            ).fetchone()
        return None if row is None else turn_from_row(row)

    def get_previous_completed_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> ConversationTurn | None:
        _require_non_empty(character_id, "character_id")
        _require_uuid4(conversation_id)
        _require_uuid4(turn_id)
        cutoff = format_datetime(self._now() - self._retention)
        with self._database.connection() as connection:
            anchor = connection.execute(
                "SELECT created_at, rowid FROM conversation_turns "
                "WHERE character_id = ? AND conversation_id = ? AND turn_id = ?",
                (character_id, str(conversation_id), str(turn_id)),
            ).fetchone()
            if anchor is None:
                return None
            row = connection.execute(
                f"SELECT {TURN_COLUMNS} FROM conversation_turns "
                "WHERE character_id = ? AND conversation_id = ? AND status = ? "
                "AND created_at >= ? AND (created_at, rowid) < (?, ?) "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (
                    character_id,
                    str(conversation_id),
                    TurnStatus.COMPLETED.value,
                    cutoff,
                    str(anchor[0]),
                    int(anchor[1]),
                ),
            ).fetchone()
        return None if row is None else turn_from_row(row)

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
            sanitizer_version=turn_input.sanitizer_version,
            policy_version=turn_input.policy_version,
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
            select_conversation(connection, character_id, conversation_id)
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

    def interrupt_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        sanitized_assistant_content: str,
    ) -> ConversationTurn:
        now = self._now()
        with self._database.transaction() as connection:
            select_conversation(connection, character_id, conversation_id)
            current = select_turn(
                connection,
                character_id,
                conversation_id,
                turn_id,
            )
            require_turn_transition(current.status, TurnStatus.INTERRUPTED)
            connection.execute(
                "UPDATE conversation_turns "
                "SET assistant_content = ?, status = ?, updated_at = ? "
                "WHERE character_id = ? AND conversation_id = ? AND turn_id = ?",
                (
                    sanitized_assistant_content,
                    TurnStatus.INTERRUPTED.value,
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
            select_conversation(connection, character_id, conversation_id)
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

    def skip_processing_turn_for_privacy(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
        turn_input: PrivacySkippedTurnInput,
    ) -> ConversationTurn:
        now = self._now()
        with self._database.transaction() as connection:
            select_conversation(connection, character_id, conversation_id)
            current = select_turn(
                connection,
                character_id,
                conversation_id,
                turn_id,
            )
            require_turn_transition(current.status, TurnStatus.PRIVACY_SKIPPED)
            connection.execute(
                "UPDATE conversation_turns "
                "SET user_content = NULL, assistant_content = NULL, "
                "status = ?, privacy_reason_code = ?, sanitizer_version = ?, "
                "policy_version = ?, updated_at = ? "
                "WHERE character_id = ? AND conversation_id = ? AND turn_id = ?",
                (
                    TurnStatus.PRIVACY_SKIPPED.value,
                    turn_input.reason_code.value,
                    turn_input.sanitizer_version,
                    turn_input.policy_version,
                    format_datetime(now),
                    character_id,
                    str(conversation_id),
                    str(turn_id),
                ),
            )
            self._rebuild_automatic_title(
                connection,
                character_id,
                conversation_id,
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
                "AND EXISTS (SELECT 1 FROM conversations AS c "
                "WHERE c.character_id = conversation_turns.character_id "
                "AND c.conversation_id = conversation_turns.conversation_id "
                "AND c.archived_at IS NULL) "
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
                "AND EXISTS (SELECT 1 FROM conversations AS c "
                "WHERE c.character_id = conversation_turns.character_id "
                "AND c.conversation_id = conversation_turns.conversation_id "
                "AND c.archived_at IS NULL) "
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

    def list_history_turns(
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
                "AND status IN (?, ?, ?) AND created_at >= ? "
                "ORDER BY created_at, turn_id",
                (
                    character_id,
                    str(conversation_id),
                    TurnStatus.COMPLETED.value,
                    TurnStatus.INTERRUPTED.value,
                    TurnStatus.PRIVACY_SKIPPED.value,
                    cutoff,
                ),
            ).fetchall()
        return [turn_from_row(row) for row in rows]

    def list_prompt_turns_page(
        self,
        character_id: str,
        conversation_id: UUID,
        *,
        cursor: PromptHistoryCursor | None = None,
        page_size: int,
    ) -> PromptHistoryPage:
        _require_uuid4(conversation_id)
        if not MIN_PROMPT_PAGE_SIZE <= page_size <= MAX_PROMPT_PAGE_SIZE:
            raise ValueError(
                f"page_size must be between {MIN_PROMPT_PAGE_SIZE} "
                f"and {MAX_PROMPT_PAGE_SIZE}"
            )
        if cursor is None:
            self.recover_stale_processing()
        cutoff = (
            self._now() - self._retention
            if cursor is None
            else cursor.retention_cutoff
        )
        parameters: list[object] = [
            character_id,
            str(conversation_id),
            TurnStatus.COMPLETED.value,
            TurnStatus.INTERRUPTED.value,
            TurnStatus.FAILED.value,
            format_datetime(cutoff),
        ]
        cursor_clause = ""
        if cursor is not None:
            cursor_clause = "AND (created_at, turn_id) < (?, ?) "
            parameters.extend(
                (format_datetime(cursor.created_at), str(cursor.turn_id))
            )
        parameters.append(page_size)
        with self._database.connection() as connection:
            select_conversation(connection, character_id, conversation_id)
            rows = connection.execute(
                f"SELECT {TURN_COLUMNS} FROM conversation_turns "
                "WHERE character_id = ? AND conversation_id = ? "
                "AND status IN (?, ?, ?) AND created_at >= ? "
                f"{cursor_clause}"
                "ORDER BY created_at DESC, turn_id DESC LIMIT ?",
                parameters,
            ).fetchall()
        turns = tuple(turn_from_row(row) for row in rows)
        next_cursor = None
        if len(turns) == page_size:
            oldest = turns[-1]
            next_cursor = PromptHistoryCursor(
                created_at=oldest.created_at,
                turn_id=oldest.turn_id,
                retention_cutoff=cutoff,
            )
        return PromptHistoryPage(turns=turns, next_cursor=next_cursor)

    def _create_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        *,
        user_content: str | None,
        status: TurnStatus,
        privacy_reason_code: HistoryDecisionReasonCode | None,
        sanitizer_version: str | None,
        policy_version: str | None,
    ) -> ConversationTurn:
        _require_uuid4(conversation_id)
        now = self._now()
        timestamp = format_datetime(now)
        with self._database.transaction() as connection:
            select_conversation(connection, character_id, conversation_id)
            turn_id = self._new_uuid4()
            connection.execute(
                "INSERT INTO conversation_turns "
                f"({TURN_COLUMNS}) VALUES "
                "(?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    str(turn_id),
                    character_id,
                    str(conversation_id),
                    user_content,
                    status.value,
                    None
                    if privacy_reason_code is None
                    else privacy_reason_code.value,
                    sanitizer_version,
                    policy_version,
                    timestamp,
                    timestamp,
                ),
            )
            if status is TurnStatus.PROCESSING and user_content is not None:
                self._set_initial_automatic_title(
                    connection,
                    character_id,
                    conversation_id,
                    user_content,
                )
            return select_turn(
                connection,
                character_id,
                conversation_id,
                turn_id,
            )

    @staticmethod
    def _set_initial_automatic_title(
        connection: sqlite3.Connection,
        character_id: str,
        conversation_id: UUID,
        user_content: str,
    ) -> None:
        connection.execute(
            "UPDATE conversations SET title = ? "
            "WHERE character_id = ? AND conversation_id = ? "
            "AND title_is_manual = 0 AND title = ?",
            (
                generate_conversation_title(user_content),
                character_id,
                str(conversation_id),
                DEFAULT_CONVERSATION_TITLE,
            ),
        )

    @staticmethod
    def _rebuild_automatic_title(
        connection: sqlite3.Connection,
        character_id: str,
        conversation_id: UUID,
    ) -> None:
        current = connection.execute(
            "SELECT title_is_manual FROM conversations "
            "WHERE character_id = ? AND conversation_id = ?",
            (character_id, str(conversation_id)),
        ).fetchone()
        if current is None or bool(current[0]):
            return
        first_content = connection.execute(
            "SELECT user_content FROM conversation_turns "
            "WHERE character_id = ? AND conversation_id = ? "
            "AND user_content IS NOT NULL "
            "ORDER BY created_at, rowid LIMIT 1",
            (character_id, str(conversation_id)),
        ).fetchone()
        title = (
            DEFAULT_CONVERSATION_TITLE
            if first_content is None
            else generate_conversation_title(str(first_content[0]))
        )
        connection.execute(
            "UPDATE conversations SET title = ? "
            "WHERE character_id = ? AND conversation_id = ?",
            (title, character_id, str(conversation_id)),
        )

    def _select_conversations(
        self,
        character_id: str,
        *,
        archived_clause: str,
    ) -> list[Conversation]:
        with self._database.connection() as connection:
            rows = connection.execute(
                f"SELECT {CONVERSATION_SELECT_COLUMNS} "
                "FROM conversations AS c LEFT JOIN conversation_turns AS t "
                "ON t.character_id = c.character_id "
                "AND t.conversation_id = c.conversation_id "
                f"WHERE c.character_id = ? AND c.archived_at {archived_clause} "
                "GROUP BY c.character_id, c.conversation_id "
                "ORDER BY COALESCE(MAX(t.updated_at), c.created_at) DESC, "
                "c.conversation_id DESC",
                (character_id,),
            ).fetchall()
        return [conversation_from_row(row) for row in rows]

    def _select_conversation_for_lifecycle(
        self,
        connection: sqlite3.Connection,
        character_id: str,
        conversation_id: UUID,
    ) -> Conversation:
        row = connection.execute(
            f"SELECT {CONVERSATION_SELECT_COLUMNS} "
            "FROM conversations AS c LEFT JOIN conversation_turns AS t "
            "ON t.character_id = c.character_id "
            "AND t.conversation_id = c.conversation_id "
            "WHERE c.character_id = ? AND c.conversation_id = ? "
            "GROUP BY c.character_id, c.conversation_id",
            (character_id, str(conversation_id)),
        ).fetchone()
        if row is None:
            raise conversation_not_found_error(
                connection,
                character_id,
                conversation_id,
            )
        return conversation_from_row(row)

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
