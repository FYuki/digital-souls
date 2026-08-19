import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.conversation_history.config import ConversationHistoryConfig
from app.conversation_history.config import resolve_conversation_history_config
from app.conversation_history.models import ProcessingTurnInput, TurnStatus
from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.wal_cleanup import ConversationWalCleanup
from app.conversation_history.schema import initialize_conversation_history_schema
from tests.conversation_history_test_support import set_turn_times


def _runtime_config(
    database_path: Path,
    *,
    stale_after: timedelta = timedelta(seconds=300),
    retention: timedelta = timedelta(days=365),
) -> ConversationHistoryConfig:
    return ConversationHistoryConfig(
        database_path=database_path,
        stale_after=stale_after,
        retention=retention,
    )


def _patch_runtime_config(monkeypatch, config: ConversationHistoryConfig) -> None:
    import app.main as main

    monkeypatch.setattr(
        main,
        "resolve_conversation_history_config",
        lambda _runtime_paths: config,
    )
    monkeypatch.setenv("RAG_ENABLED", "false")


class TestConversationHistoryRuntime:
    def test_should_isolate_default_database_for_each_test(
        self,
        conversation_history_database_path: Path,
        runtime_paths,
    ) -> None:
        config = resolve_conversation_history_config(runtime_paths)

        assert config.database_path == conversation_history_database_path

    def test_should_run_full_repository_flow_from_empty_database(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import app.main as main

        database_path = tmp_path / "runtime-history.db"
        _patch_runtime_config(monkeypatch, _runtime_config(database_path))

        with TestClient(main.app):
            repository = main.app.state.conversation_history_repository
            conversation = repository.create_conversation("miori")
            processing = repository.create_processing_turn(
                "miori",
                conversation.conversation_id,
                ProcessingTurnInput(sanitized_user_content="処理済みの質問"),
            )
            completed = repository.complete_turn(
                "miori",
                conversation.conversation_id,
                processing.turn_id,
                sanitized_assistant_content="完全な回答",
            )
            turns = repository.list_turns(
                "miori",
                conversation.conversation_id,
            )

        assert completed.status is TurnStatus.COMPLETED
        assert turns == [completed]
        with sqlite3.connect(database_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        assert tables == {
            "conversations",
            "conversation_turns",
            "wal_cleanup_jobs",
        }

    def test_should_recover_stale_processing_during_startup(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import app.main as main

        database_path = tmp_path / "runtime-history.db"
        config = _runtime_config(
            database_path,
            stale_after=timedelta(seconds=1),
        )
        initialize_conversation_history_schema(database_path)
        seed_repository = ConversationHistoryRepository(
            database_path=database_path,
            stale_after=timedelta(days=1),
            retention=timedelta(days=365),
            clock=lambda: datetime.now(UTC),
            uuid_factory=uuid4,
            wal_cleanup=ConversationWalCleanup(
                database_path=database_path,
                clock=lambda: datetime.now(UTC),
                connection_factory=sqlite3.connect,
            ),
        )
        conversation = seed_repository.create_conversation("miori")
        turn = seed_repository.create_processing_turn(
            "miori",
            conversation.conversation_id,
            ProcessingTurnInput(sanitized_user_content="起動前のturn"),
        )
        stale_time = datetime.now(UTC) - timedelta(seconds=10)
        set_turn_times(
            database_path,
            turn.turn_id,
            created_at=stale_time,
            updated_at=stale_time,
        )
        _patch_runtime_config(monkeypatch, config)

        with TestClient(main.app):
            assert hasattr(main.app.state, "conversation_history_repository")
            assert hasattr(main.app.state, "conversation_lifecycle_service")

        with sqlite3.connect(database_path) as connection:
            status = connection.execute(
                "SELECT status FROM conversation_turns WHERE turn_id = ?",
                (str(turn.turn_id),),
            ).fetchone()[0]
        assert status == "failed"

    def test_should_propagate_retention_config_to_runtime_repository(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import app.main as main

        database_path = tmp_path / "runtime-history.db"
        config = _runtime_config(
            database_path,
            retention=timedelta(days=500),
        )
        initialize_conversation_history_schema(database_path)
        seed_repository = ConversationHistoryRepository(
            database_path=database_path,
            stale_after=timedelta(days=1),
            retention=timedelta(days=500),
            clock=lambda: datetime.now(UTC),
            uuid_factory=uuid4,
            wal_cleanup=ConversationWalCleanup(
                database_path=database_path,
                clock=lambda: datetime.now(UTC),
                connection_factory=sqlite3.connect,
            ),
        )
        conversation = seed_repository.create_conversation("miori")
        turn = seed_repository.create_processing_turn(
            "miori",
            conversation.conversation_id,
            ProcessingTurnInput(sanitized_user_content="保持対象のturn"),
        )
        stored_at = datetime.now(UTC) - timedelta(days=400)
        set_turn_times(
            database_path,
            turn.turn_id,
            created_at=stored_at,
            updated_at=datetime.now(UTC),
        )
        _patch_runtime_config(monkeypatch, config)

        with TestClient(main.app):
            turns = main.app.state.conversation_history_repository.list_turns(
                "miori",
                conversation.conversation_id,
            )

        assert [stored.turn_id for stored in turns] == [turn.turn_id]

    def test_should_remove_repository_state_after_shutdown(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import app.main as main

        _patch_runtime_config(
            monkeypatch,
            _runtime_config(tmp_path / "runtime-history.db"),
        )

        with TestClient(main.app):
            assert hasattr(main.app.state, "conversation_history_repository")
            assert hasattr(main.app.state, "conversation_lifecycle_service")

        assert not hasattr(main.app.state, "conversation_history_repository")
        assert not hasattr(main.app.state, "conversation_lifecycle_service")
