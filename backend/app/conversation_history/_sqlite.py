import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.conversation_history.errors import (
    ConversationCharacterBoundaryError,
    ConversationNotFoundError,
    TurnNotFoundError,
)
from app.conversation_history.models import (
    Conversation,
    ConversationTurn,
    TurnStatus,
)
from app.conversation_history.sqlite_lease import normal_sqlite_access
from app.privacy.contracts import HistoryDecisionReasonCode

ConnectionFactory = Callable[[Path], sqlite3.Connection]
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
TURN_COLUMNS = (
    "turn_id, character_id, conversation_id, user_content, "
    "assistant_content, status, privacy_reason_code, sanitizer_version, "
    "policy_version, created_at, updated_at"
)


class SqliteSession:
    def __init__(
        self,
        database_path: Path,
        connection_factory: ConnectionFactory,
    ) -> None:
        from app.restore_intent import require_sqlite_available

        require_sqlite_available(database_path)
        self._database_path = database_path
        self._connection_factory = connection_factory

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with normal_sqlite_access(self._database_path):
            connection = self._connection_factory(self._database_path)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA secure_delete = ON")
                yield connection
            finally:
                connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


def select_conversation(
    connection: sqlite3.Connection,
    character_id: str,
    conversation_id: UUID,
) -> Conversation:
    row = connection.execute(
        "SELECT c.character_id, c.conversation_id, c.created_at, "
        "COALESCE(MAX(t.updated_at), c.created_at), c.archived_at "
        "FROM conversations AS c LEFT JOIN conversation_turns AS t "
        "ON t.character_id = c.character_id "
        "AND t.conversation_id = c.conversation_id "
        "WHERE c.character_id = ? AND c.conversation_id = ? "
        "AND c.archived_at IS NULL GROUP BY c.character_id, c.conversation_id",
        (character_id, str(conversation_id)),
    ).fetchone()
    if row is None:
        raise conversation_not_found_error(
            connection,
            character_id,
            conversation_id,
        )
    return Conversation(
        character_id=str(row[0]),
        conversation_id=UUID(str(row[1])),
        created_at=parse_datetime(str(row[2])),
        updated_at=parse_datetime(str(row[3])),
        archived_at=None,
    )


def conversation_not_found_error(
    connection: sqlite3.Connection,
    character_id: str,
    conversation_id: UUID,
) -> ConversationNotFoundError:
    inside_boundary = connection.execute(
        "SELECT 1 FROM conversations WHERE character_id = ? "
        "AND conversation_id = ? LIMIT 1",
        (character_id, str(conversation_id)),
    ).fetchone()
    if inside_boundary is not None:
        return ConversationNotFoundError()
    outside_boundary = connection.execute(
        "SELECT 1 FROM conversations WHERE character_id <> ? "
        "AND conversation_id = ? LIMIT 1",
        (character_id, str(conversation_id)),
    ).fetchone()
    if outside_boundary is not None:
        return ConversationCharacterBoundaryError()
    return ConversationNotFoundError()


def conversation_from_row(row: sqlite3.Row) -> Conversation:
    return Conversation(
        character_id=str(row[0]),
        conversation_id=UUID(str(row[1])),
        created_at=parse_datetime(str(row[2])),
        updated_at=parse_datetime(str(row[3])),
        archived_at=None if row[4] is None else parse_datetime(str(row[4])),
    )


def select_turn(
    connection: sqlite3.Connection,
    character_id: str,
    conversation_id: UUID,
    turn_id: UUID,
) -> ConversationTurn:
    row = connection.execute(
        f"SELECT {TURN_COLUMNS} FROM conversation_turns "
        "WHERE character_id = ? AND conversation_id = ? AND turn_id = ?",
        (character_id, str(conversation_id), str(turn_id)),
    ).fetchone()
    if row is None:
        raise TurnNotFoundError()
    return turn_from_row(row)


def turn_from_row(row: sqlite3.Row) -> ConversationTurn:
    return ConversationTurn(
        turn_id=UUID(str(row[0])),
        character_id=str(row[1]),
        conversation_id=UUID(str(row[2])),
        user_content=None if row[3] is None else str(row[3]),
        assistant_content=None if row[4] is None else str(row[4]),
        status=TurnStatus(str(row[5])),
        privacy_reason_code=(
            None if row[6] is None else HistoryDecisionReasonCode(str(row[6]))
        ),
        sanitizer_version=None if row[7] is None else str(row[7]),
        policy_version=None if row[8] is None else str(row[8]),
        created_at=parse_datetime(str(row[9])),
        updated_at=parse_datetime(str(row[10])),
    )


def format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).strftime(DATETIME_FORMAT)


def parse_datetime(value: str) -> datetime:
    return datetime.strptime(value, DATETIME_FORMAT).replace(tzinfo=UTC)
