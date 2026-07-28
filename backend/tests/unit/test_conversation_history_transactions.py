import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest

from app.conversation_history.errors import InvalidStateTransitionError
from app.conversation_history.models import (
    ConversationTurn,
    ProcessingTurnInput,
    TurnStatus,
)
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    FIXED_NOW,
    TURN_ID,
    create_repository,
    set_turn_times,
)


class TestTransactionBoundaries:
    def test_should_serialize_competing_updates_to_same_turn(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)
        repository.create_conversation("miori")
        repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="処理済みの質問"),
        )
        barrier = Barrier(2)

        def complete(answer: str) -> ConversationTurn:
            barrier.wait()
            return repository.complete_turn(
                "miori",
                CONVERSATION_ID,
                TURN_ID,
                sanitized_assistant_content=answer,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(complete, "回答A"),
                executor.submit(complete, "回答B"),
            ]

        successes, failures = _partition_futures(futures)

        assert len(successes) == 1
        assert successes[0].status is TurnStatus.COMPLETED
        assert len(failures) == 1
        assert isinstance(failures[0], InvalidStateTransitionError)

    def test_should_serialize_stale_recovery_and_completion(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)
        repository.create_conversation("miori")
        repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="処理済みの質問"),
        )
        set_turn_times(
            database_path,
            TURN_ID,
            created_at=FIXED_NOW - timedelta(minutes=10),
            updated_at=FIXED_NOW - timedelta(seconds=301),
        )
        with sqlite3.connect(database_path) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
        barrier = Barrier(2)

        def recover() -> list[ConversationTurn]:
            barrier.wait()
            return repository.recover_stale_processing()

        def complete() -> ConversationTurn:
            barrier.wait()
            return repository.complete_turn(
                "miori",
                CONVERSATION_ID,
                TURN_ID,
                sanitized_assistant_content="完全回答",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            recovery_future = executor.submit(recover)
            completion_future = executor.submit(complete)

        recovery_error = recovery_future.exception()
        completion_error = completion_future.exception()
        assert recovery_error is None
        assert completion_error is None or isinstance(
            completion_error,
            InvalidStateTransitionError,
        )
        recovered = recovery_future.result()
        if completion_error is None:
            assert recovered == []
            assert completion_future.result().status is TurnStatus.COMPLETED
        else:
            assert [turn.turn_id for turn in recovered] == [TURN_ID]
            assert recovered[0].status is TurnStatus.FAILED

    def test_should_rollback_processing_creation_when_returning_row_fails(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"

        class FailingConnection(sqlite3.Connection):
            inject_failure = False

            def execute(self, sql, parameters=()):
                normalized = " ".join(sql.lower().split())
                if (
                    self.inject_failure
                    and normalized.startswith("select")
                    and "conversation_turns" in normalized
                ):
                    raise sqlite3.OperationalError("injected create return failure")
                return super().execute(sql, parameters)

        repository = create_repository(
            database_path,
            connection_factory=lambda path: sqlite3.connect(
                path,
                factory=FailingConnection,
            ),
        )
        repository.create_conversation("miori")
        FailingConnection.inject_failure = True

        with pytest.raises(sqlite3.OperationalError, match="injected"):
            repository.create_processing_turn(
                "miori",
                CONVERSATION_ID,
                ProcessingTurnInput(sanitized_user_content="部分保存禁止"),
            )

        with sqlite3.connect(database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM conversation_turns"
            ).fetchone()[0]
        assert count == 0

    def test_should_return_write_result_without_opening_second_connection(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        calls = 0
        enforce_single_connection = False

        def one_connection_factory(path: Path) -> sqlite3.Connection:
            nonlocal calls, enforce_single_connection
            calls += 1
            if enforce_single_connection and calls > 1:
                raise AssertionError("write result must use the transaction connection")
            return sqlite3.connect(path)

        repository = create_repository(
            database_path,
            connection_factory=one_connection_factory,
        )
        repository.create_conversation("miori")
        repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="処理済みの質問"),
        )
        calls = 0
        enforce_single_connection = True

        completed = repository.complete_turn(
            "miori",
            CONVERSATION_ID,
            TURN_ID,
            sanitized_assistant_content="完全回答",
        )

        assert completed.status is TurnStatus.COMPLETED
        assert calls == 1

    def test_should_rollback_when_returning_updated_row_fails(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"

        class FailingConnection(sqlite3.Connection):
            inject_failure = False
            select_count = 0

            def execute(self, sql, parameters=()):
                normalized = " ".join(sql.lower().split())
                if (
                    self.inject_failure
                    and normalized.startswith("select")
                    and "conversation_turns" in normalized
                ):
                    self.select_count += 1
                    if self.select_count == 2:
                        raise sqlite3.OperationalError("injected return-row failure")
                return super().execute(sql, parameters)

        repository = create_repository(
            database_path,
            connection_factory=lambda path: sqlite3.connect(
                path,
                factory=FailingConnection,
            ),
        )
        repository.create_conversation("miori")
        repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="処理済みの質問"),
        )
        FailingConnection.inject_failure = True

        with pytest.raises(sqlite3.OperationalError, match="injected"):
            repository.complete_turn(
                "miori",
                CONVERSATION_ID,
                TURN_ID,
                sanitized_assistant_content="部分保存してはいけない回答",
            )

        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                "SELECT status, assistant_content FROM conversation_turns "
                "WHERE turn_id = ?",
                (str(TURN_ID),),
            ).fetchone()
        assert row == ("processing", None)


def _partition_futures(
    futures: list[Future[ConversationTurn]],
) -> tuple[list[ConversationTurn], list[BaseException]]:
    successes: list[ConversationTurn] = []
    failures: list[BaseException] = []
    for future in futures:
        error = future.exception()
        if error is None:
            successes.append(future.result())
        else:
            failures.append(error)
    return successes, failures
