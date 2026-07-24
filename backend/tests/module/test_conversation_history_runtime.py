import ast
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.conversation_history.config import ConversationHistoryConfig
from app.conversation_history.config import resolve_conversation_history_config
from app.conversation_history.models import ProcessingTurnInput, TurnStatus
from app.conversation_history.repository import ConversationHistoryRepository
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
        lambda: config,
    )
    monkeypatch.setenv("RAG_ENABLED", "false")


class TestConversationHistoryRuntime:
    def test_should_isolate_default_database_for_each_test(
        self,
        conversation_history_database_path: Path,
    ) -> None:
        config = resolve_conversation_history_config()

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
        assert tables == {"conversations", "conversation_turns"}

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

        assert not hasattr(main.app.state, "conversation_history_repository")

    def test_should_remove_repository_state_when_executor_creation_fails(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import app.main as main

        _patch_runtime_config(
            monkeypatch,
            _runtime_config(tmp_path / "runtime-history.db"),
        )

        def fail_executor_creation(*_args, **_kwargs):
            raise RuntimeError("executor creation failed")

        monkeypatch.setattr(main, "ThreadPoolExecutor", fail_executor_creation)

        with pytest.raises(RuntimeError, match="executor creation failed"):
            with TestClient(main.app):
                raise AssertionError("startup should fail before yielding")

        assert not hasattr(main.app.state, "conversation_history_repository")


class TestRagHistorySeparation:
    def test_should_keep_rag_operations_separate_from_history_tables(self) -> None:
        import app.memory.rag_service as rag_service
        import app.memory.memory_policy as memory_policy

        syntax_trees = [
            ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            for module in (rag_service, memory_policy)
        ]
        imported_modules = {
            node.module
            for syntax_tree in syntax_trees
            for node in ast.walk(syntax_tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
        }
        string_literals = {
            node.value.lower()
            for syntax_tree in syntax_trees
            for node in ast.walk(syntax_tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        forbidden_imports = {
            module
            for module in imported_modules
            if module.startswith("app.conversation_history")
            or module == "app.memory.conversation_log"
        }
        forbidden_sql = {
            value
            for value in string_literals
            if "conversation_turns" in value
            or "insert into conversations" in value
            or "from conversations" in value
        }

        assert callable(rag_service.build_augmented_system_prompt)
        assert callable(rag_service.record_user_memory_candidate)
        assert forbidden_imports == set()
        assert forbidden_sql == set()
