import ast
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.conversation_history.config import ConversationHistoryConfig
from app.conversation_history.config import resolve_conversation_history_config
from app.conversation_history.models import (
    PersistedMaskedText,
    PrivacySkipReason,
    ProcessingTurnInput,
    TurnStatus,
)
from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.schema import initialize_conversation_history_schema
from tests.prompt_test_support import character_card, prompt_messages
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
                ProcessingTurnInput(
                    sanitized_user_content=PersistedMaskedText("処理済みの質問")
                ),
            )
            completed = repository.complete_turn(
                "miori",
                conversation.conversation_id,
                processing.turn_id,
                sanitized_assistant_content=PersistedMaskedText("完全な回答"),
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

    def test_should_feed_masked_completed_turn_into_second_session_prompt(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import app.main as main

        database_path = tmp_path / "runtime-history.db"
        _patch_runtime_config(monkeypatch, _runtime_config(database_path))
        prompts = []

        with TestClient(main.app) as client:
            monkeypatch.setattr(
                "app._chat_runtime._character_loader.load_character_card",
                lambda _character: character_card("# prompt"),
            )

            def generate_response(prompt):
                prompts.append(prompt)
                return (
                    "返信先はassistant@example.comです"
                    if len(prompts) == 1
                    else "二通目の回答"
                )

            monkeypatch.setattr(
                "app._chat_runtime._llm_router.generate_response",
                generate_response,
            )

            with client.websocket_connect("/ws/miori") as websocket:
                websocket.send_json(
                    {"type": "text", "message": "連絡先はuser@example.comです"}
                )
                assert websocket.receive_json()["response"] == (
                    "返信先はassistant@example.comです"
                )
                websocket.send_json(
                    {"type": "text", "message": "さっきの話を続けて"}
                )
                assert websocket.receive_json()["response"] == "二通目の回答"

        assert prompt_messages(prompts[1]) == [
            ("system", "# prompt"),
            ("user", "連絡先は[REDACTED]です"),
            ("assistant", "返信先は[REDACTED]です"),
            ("user", "さっきの話を続けて"),
        ]

    def test_should_send_first_message_without_adding_it_to_history_or_prompt(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import app.main as main

        database_path = tmp_path / "runtime-history.db"
        _patch_runtime_config(monkeypatch, _runtime_config(database_path))
        prompts = []

        with TestClient(main.app) as client:
            monkeypatch.setattr(
                "app._chat_runtime._character_loader.load_character_card",
                lambda _character: character_card(
                    "# prompt",
                    first_mes="はじめまして",
                ),
            )

            def generate_response(prompt):
                prompts.append(prompt)
                return "生成した回答"

            monkeypatch.setattr(
                "app._chat_runtime._llm_router.generate_response",
                generate_response,
            )

            with client.websocket_connect("/ws/miori") as websocket:
                assert websocket.receive_json() == {
                    "type": "text",
                    "response": "はじめまして",
                }
                websocket.send_json({"type": "text", "message": "こんにちは"})
                assert websocket.receive_json()["response"] == "生成した回答"

        assert prompt_messages(prompts[0]) == [
            ("system", "# prompt"),
            ("user", "こんにちは"),
        ]
        with sqlite3.connect(database_path) as connection:
            persisted_messages = connection.execute(
                "SELECT user_content, assistant_content FROM conversation_turns"
            ).fetchall()
        assert persisted_messages == [("こんにちは", "生成した回答")]

    @pytest.mark.parametrize(
        ("message", "persisted"),
        (
            ("電話は090-1234-5678です", "電話は[REDACTED]です"),
            ("電話は090(1234)5678です", "電話は[REDACTED]です"),
            ("カードは4111 1111 1111 1111です", "カードは[REDACTED]です"),
            (
                "カードは4111 (1111) 1111 1111です",
                "カードは[REDACTED]です",
            ),
            ("マイナンバー 1234-5678-9012", "[REDACTED]"),
        ),
    )
    def test_should_persist_masked_direct_identifiers_from_chat_runtime(
        self,
        tmp_path: Path,
        monkeypatch,
        message: str,
        persisted: str,
    ) -> None:
        import app.main as main

        database_path = tmp_path / "runtime-history.db"
        _patch_runtime_config(monkeypatch, _runtime_config(database_path))
        monkeypatch.setattr(
            "app._chat_runtime._character_loader.load_character_card",
            lambda _character: character_card("# prompt"),
        )
        monkeypatch.setattr(
            "app._chat_runtime._llm_router.generate_response",
            lambda _prompt: "回答",
        )

        with TestClient(main.app) as client:
            response = client.post(
                "/chat",
                json={"character": "miori", "message": message},
            )

        assert response.status_code == 200
        with sqlite3.connect(database_path) as connection:
            stored = connection.execute(
                "SELECT user_content, assistant_content, status "
                "FROM conversation_turns"
            ).fetchone()
        assert stored == (persisted, "回答", TurnStatus.COMPLETED.value)

    def test_should_store_only_metadata_when_user_denies_history_storage(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import app.main as main

        database_path = tmp_path / "runtime-history.db"
        _patch_runtime_config(monkeypatch, _runtime_config(database_path))
        monkeypatch.setattr(
            "app._chat_runtime._character_loader.load_character_card",
            lambda _character: character_card("# prompt"),
        )
        monkeypatch.setattr(
            "app._chat_runtime._llm_router.generate_response",
            lambda _prompt: "保存しない回答",
        )

        with TestClient(main.app) as client:
            response = client.post(
                "/chat",
                json={
                    "character": "miori",
                    "message": "履歴に残さないで、この質問に答えて",
                },
            )

        assert response.status_code == 200
        assert response.json()["response"] == "保存しない回答"
        with sqlite3.connect(database_path) as connection:
            stored = connection.execute(
                "SELECT user_content, assistant_content, status, "
                "privacy_reason_code FROM conversation_turns"
            ).fetchone()
        assert stored == (
            None,
            None,
            TurnStatus.PRIVACY_SKIPPED.value,
            PrivacySkipReason.POLICY_DENIED.value,
        )

    def test_should_atomically_remove_turn_bodies_when_assistant_cannot_be_masked(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import app.main as main

        database_path = tmp_path / "runtime-history.db"
        _patch_runtime_config(monkeypatch, _runtime_config(database_path))
        unsafe_reply = "API key: sk-abcdefghijklmnopqrstuvwxyz"
        monkeypatch.setattr(
            "app._chat_runtime._character_loader.load_character_card",
            lambda _character: character_card("# prompt"),
        )
        monkeypatch.setattr(
            "app._chat_runtime._llm_router.generate_response",
            lambda _prompt: unsafe_reply,
        )

        with TestClient(main.app) as client:
            response = client.post(
                "/chat",
                json={"character": "miori", "message": "質問"},
            )

        assert response.status_code == 200
        assert response.json()["response"] == unsafe_reply
        with sqlite3.connect(database_path) as connection:
            stored = connection.execute(
                "SELECT user_content, assistant_content, status, "
                "privacy_reason_code FROM conversation_turns"
            ).fetchone()
        assert stored == (
            None,
            None,
            TurnStatus.PRIVACY_SKIPPED.value,
            PrivacySkipReason.SENSITIVE_CONTENT.value,
        )

    def test_should_resume_http_conversation_and_feed_masked_history(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import app.main as main

        database_path = tmp_path / "runtime-history.db"
        _patch_runtime_config(monkeypatch, _runtime_config(database_path))
        prompts = []
        monkeypatch.setattr(
            "app._chat_runtime._character_loader.load_character_card",
            lambda _character: character_card("# prompt"),
        )

        def generate_response(prompt):
            prompts.append(prompt)
            return (
                "返信先はassistant@example.comです"
                if len(prompts) == 1
                else "二通目の回答"
            )

        monkeypatch.setattr(
            "app._chat_runtime._llm_router.generate_response",
            generate_response,
        )

        with TestClient(main.app) as client:
            first = client.post(
                "/chat",
                json={
                    "character": "miori",
                    "message": "連絡先はuser@example.comです",
                },
            )
            second = client.post(
                "/chat",
                json={
                    "character": "miori",
                    "message": "さっきの話を続けて",
                    "conversation_id": first.json()["conversation_id"],
                },
            )

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["conversation_id"] == first.json()["conversation_id"]
        assert prompt_messages(prompts[1]) == [
            ("system", "# prompt"),
            ("user", "連絡先は[REDACTED]です"),
            ("assistant", "返信先は[REDACTED]です"),
            ("user", "さっきの話を続けて"),
        ]

    def test_should_return_404_for_unknown_http_conversation(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        import app.main as main

        database_path = tmp_path / "runtime-history.db"
        _patch_runtime_config(monkeypatch, _runtime_config(database_path))

        with TestClient(main.app) as client:
            response = client.post(
                "/chat",
                json={
                    "character": "miori",
                    "message": "続けて",
                    "conversation_id": str(uuid4()),
                },
            )

        assert response.status_code == 404
        assert response.json() == {"detail": "Conversation not found"}

    @pytest.mark.parametrize(
        "conversation_id",
        ["00000000-0000-1000-8000-000000000001", "not-a-uuid"],
        ids=["uuid-v1", "malformed-uuid"],
    )
    def test_should_reject_invalid_http_conversation_id(
        self,
        tmp_path: Path,
        monkeypatch,
        conversation_id: str,
    ) -> None:
        import app.main as main

        database_path = tmp_path / "runtime-history.db"
        _patch_runtime_config(monkeypatch, _runtime_config(database_path))

        with TestClient(main.app) as client:
            response = client.post(
                "/chat",
                json={
                    "character": "miori",
                    "message": "続けて",
                    "conversation_id": conversation_id,
                },
            )

        assert response.status_code == 422

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
            ProcessingTurnInput(
                sanitized_user_content=PersistedMaskedText("起動前のturn")
            ),
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
            ProcessingTurnInput(
                sanitized_user_content=PersistedMaskedText("保持対象のturn")
            ),
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
            if module
            in {
                "app.conversation_history.models",
                "app.conversation_history.repository",
                "app.conversation_history.sanitizer",
                "app.memory.conversation_log",
            }
        }
        forbidden_sql = {
            value
            for value in string_literals
            if "conversation_turns" in value
            or "insert into conversations" in value
            or "from conversations" in value
        }

        assert callable(rag_service.build_rag_context_for_scanned_user)
        assert callable(rag_service.record_scanned_user_memory_candidate)
        assert forbidden_imports == set()
        assert forbidden_sql == set()
