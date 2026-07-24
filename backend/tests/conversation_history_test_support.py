import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.schema import initialize_conversation_history_schema


FIXED_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
CONVERSATION_ID = UUID("e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010")
OTHER_CONVERSATION_ID = UUID("e98d6c65-1ae9-4d6f-a8c8-d59b0ad09011")
TURN_ID = UUID("9e70795d-e5d5-431d-baa2-67f884403010")
OTHER_TURN_ID = UUID("9e70795d-e5d5-431d-baa2-67f884403011")


class SequenceUuidFactory:
    def __init__(self, *values: UUID) -> None:
        self._values: Iterator[UUID] = iter(values)

    def __call__(self) -> UUID:
        return next(self._values)


def create_repository(
    database_path: Path,
    *,
    now: datetime = FIXED_NOW,
    stale_after: timedelta = timedelta(seconds=300),
    retention: timedelta = timedelta(days=365),
    uuid_factory: Callable[[], UUID] | None = None,
    connection_factory: Callable[[Path], sqlite3.Connection] | None = None,
) -> ConversationHistoryRepository:
    initialize_conversation_history_schema(database_path)
    if uuid_factory is None:
        resolved_uuid_factory: Callable[[], UUID] = SequenceUuidFactory(
            CONVERSATION_ID,
            TURN_ID,
            OTHER_TURN_ID,
        )
    else:
        resolved_uuid_factory = uuid_factory
    arguments: dict[str, object] = {
        "database_path": database_path,
        "stale_after": stale_after,
        "retention": retention,
        "clock": lambda: now,
        "uuid_factory": resolved_uuid_factory,
    }
    if connection_factory is not None:
        arguments["connection_factory"] = connection_factory
    return ConversationHistoryRepository(**arguments)


def set_turn_times(
    database_path: Path,
    turn_id: UUID,
    *,
    created_at: datetime,
    updated_at: datetime,
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE conversation_turns SET created_at = ?, updated_at = ? "
            "WHERE turn_id = ?",
            (
                created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                updated_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                str(turn_id),
            ),
        )
