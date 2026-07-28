import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.conversation_history.errors import (
    ConversationNotFoundError,
    TurnNotFoundError,
)
from app.conversation_history.models import (
    Conversation,
    ConversationTurn,
    PrivacySkipReason,
    TurnStatus,
)

ConnectionFactory = Callable[[Path], sqlite3.Connection]
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
TURN_COLUMNS = (
    "turn_id, character_id, conversation_id, user_content, "
    "assistant_content, status, privacy_reason_code, created_at, updated_at"
)


class SqliteSession:
    def __init__(
        self,
        database_path: Path,
        connection_factory: ConnectionFactory,
    ) -> None:
        self._database_path = database_path
        self._connection_factory = connection_factory

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection_factory(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
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
        "SELECT character_id, conversation_id, created_at FROM conversations "
        "WHERE character_id = ? AND conversation_id = ?",
        (character_id, str(conversation_id)),
    ).fetchone()
    if row is None:
        raise ConversationNotFoundError()
    return Conversation(
        character_id=str(row[0]),
        conversation_id=UUID(str(row[1])),
        created_at=parse_datetime(str(row[2])),
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
            None if row[6] is None else PrivacySkipReason(str(row[6]))
        ),
        created_at=parse_datetime(str(row[7])),
        updated_at=parse_datetime(str(row[8])),
    )


def format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).strftime(DATETIME_FORMAT)


def parse_datetime(value: str) -> datetime:
    return datetime.strptime(value, DATETIME_FORMAT).replace(tzinfo=UTC)
