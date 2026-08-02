import sqlite3
from pathlib import Path

from app.conversation_history.models import ProcessingTurnInput
from app.conversation_history.wal_cleanup import ConversationWalCleanup
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    FIXED_NOW,
    TURN_ID,
    SequenceUuidFactory,
    create_repository,
)


def test_should_checkpoint_and_truncate_wal_after_hard_delete(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    observed: list[str] = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=()):  # type: ignore[no-untyped-def]
            normalized = " ".join(sql.upper().split())
            if normalized == "PRAGMA WAL_CHECKPOINT(TRUNCATE)":
                observed.append(normalized)
            return super().execute(sql, parameters)

    def connection_factory(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(path, factory=ObservedConnection)

    repository = create_repository(
        database_path,
        uuid_factory=SequenceUuidFactory(CONVERSATION_ID, TURN_ID),
        connection_factory=connection_factory,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
    required_operations = {"archive_conversation", "hard_delete_conversation"}
    assert required_operations <= set(dir(repository)), (
        "conversation hard delete operations are not implemented"
    )
    repository.create_conversation("miori")
    repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="保存済み本文"),
    )
    repository.archive_conversation("miori", CONVERSATION_ID)

    repository.hard_delete_conversation("miori", CONVERSATION_ID)

    assert observed == ["PRAGMA WAL_CHECKPOINT(TRUNCATE)"]


def test_should_retry_failed_wal_cleanup_and_remove_completed_job(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    checkpoint_attempts = 0

    class FailingOnceConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=()):  # type: ignore[no-untyped-def]
            nonlocal checkpoint_attempts
            normalized = " ".join(sql.upper().split())
            if normalized == "PRAGMA WAL_CHECKPOINT(TRUNCATE)":
                checkpoint_attempts += 1
                if checkpoint_attempts == 1:
                    raise sqlite3.OperationalError("forced checkpoint failure")
            return super().execute(sql, parameters)

    def connection_factory(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(path, factory=FailingOnceConnection)

    repository = create_repository(
        database_path,
        uuid_factory=SequenceUuidFactory(CONVERSATION_ID, TURN_ID),
        connection_factory=connection_factory,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
    required_operations = {
        "archive_conversation",
        "hard_delete_conversation",
    }
    assert required_operations <= set(dir(repository)), (
        "WAL cleanup retry operation is not implemented"
    )
    repository.create_conversation("miori")
    repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="保存済み本文"),
    )
    repository.archive_conversation("miori", CONVERSATION_ID)

    repository.hard_delete_conversation("miori", CONVERSATION_ID)

    with sqlite3.connect(database_path) as connection:
        pending = connection.execute(
            "SELECT character_id, conversation_id, reason_code, attempt_count "
            "FROM wal_cleanup_jobs"
        ).fetchall()
    assert pending == [
        ("miori", str(CONVERSATION_ID), "WAL_CHECKPOINT_FAILED", 1)
    ]

    wal_cleanup = ConversationWalCleanup(
        database_path=database_path,
        clock=lambda: FIXED_NOW,
        connection_factory=connection_factory,
    )
    wal_cleanup.retry_pending()

    with sqlite3.connect(database_path) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM wal_cleanup_jobs"
        ).fetchone()[0]
    assert checkpoint_attempts == 2
    assert remaining == 0


def test_should_record_retry_job_when_wal_checkpoint_raises_database_error(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"

    class DatabaseErrorConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=()):  # type: ignore[no-untyped-def]
            normalized = " ".join(sql.upper().split())
            if normalized == "PRAGMA WAL_CHECKPOINT(TRUNCATE)":
                raise sqlite3.DatabaseError("forced checkpoint database failure")
            return super().execute(sql, parameters)

    def connection_factory(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(path, factory=DatabaseErrorConnection)

    repository = create_repository(
        database_path,
        uuid_factory=SequenceUuidFactory(CONVERSATION_ID, TURN_ID),
        connection_factory=connection_factory,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
    repository.create_conversation("miori")
    repository.archive_conversation("miori", CONVERSATION_ID)

    repository.hard_delete_conversation("miori", CONVERSATION_ID)

    with sqlite3.connect(database_path) as connection:
        conversations = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE character_id = ? "
            "AND conversation_id = ?",
            ("miori", str(CONVERSATION_ID)),
        ).fetchone()[0]
        pending = connection.execute(
            "SELECT character_id, conversation_id, reason_code, attempt_count "
            "FROM wal_cleanup_jobs"
        ).fetchall()
    assert conversations == 0
    assert pending == [
        ("miori", str(CONVERSATION_ID), "WAL_CHECKPOINT_FAILED", 1)
    ]


def test_should_leave_retry_job_when_wal_checkpoint_returns_busy(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"

    class BusyCheckpointResult:
        def fetchone(self) -> tuple[int, int, int]:
            return (1, 3, 0)

    class BusyCheckpointConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=()):  # type: ignore[no-untyped-def]
            normalized = " ".join(sql.upper().split())
            if normalized == "PRAGMA WAL_CHECKPOINT(TRUNCATE)":
                return BusyCheckpointResult()
            return super().execute(sql, parameters)

    def connection_factory(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(path, factory=BusyCheckpointConnection)

    repository = create_repository(
        database_path,
        uuid_factory=SequenceUuidFactory(CONVERSATION_ID, TURN_ID),
        connection_factory=connection_factory,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
    required_operations = {"archive_conversation", "hard_delete_conversation"}
    assert required_operations <= set(dir(repository)), (
        "conversation hard delete operations are not implemented"
    )
    repository.create_conversation("miori")
    repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="保存済み本文"),
    )
    repository.archive_conversation("miori", CONVERSATION_ID)

    repository.hard_delete_conversation("miori", CONVERSATION_ID)

    with sqlite3.connect(database_path) as connection:
        pending = connection.execute(
            "SELECT character_id, conversation_id, reason_code, attempt_count "
            "FROM wal_cleanup_jobs"
        ).fetchall()
    assert pending == [
        ("miori", str(CONVERSATION_ID), "WAL_CHECKPOINT_FAILED", 1)
    ]
