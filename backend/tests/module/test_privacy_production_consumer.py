import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


_LOAD_PERSONALITY = "app._chat_runtime._character_loader.load_personality"
_GENERATE_RESPONSE = "app._chat_runtime._llm_router.generate_response"


def _stored_turn(database_path: Path) -> tuple[object, ...]:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT user_content, assistant_content, status, privacy_reason_code "
            "FROM conversation_turns"
        ).fetchone()
    if row is None:
        raise AssertionError("conversation turn was not persisted")
    return row


def test_should_persist_only_sanitized_user_and_assistant_content(
    client,
    conversation_history_database_path: Path,
) -> None:
    with patch(_LOAD_PERSONALITY, return_value="# prompt"):
        with patch(
            _GENERATE_RESPONSE,
            return_value="確認: password: synthetic-assistant-secret",
        ):
            response = client.post(
                "/chat",
                json={
                    "character": "miori",
                    "message": "password: synthetic-user-secret",
                },
            )

    assert response.status_code == 200
    assert _stored_turn(conversation_history_database_path) == (
        "password: [PASSWORD]",
        "確認: password: [PASSWORD]",
        "completed",
        None,
    )


def test_should_persist_metadata_only_for_current_user_history_opt_out(
    client,
    conversation_history_database_path: Path,
) -> None:
    with patch(_LOAD_PERSONALITY, return_value="# prompt"):
        with patch(_GENERATE_RESPONSE, return_value="承知しました"):
            response = client.post(
                "/chat",
                json={
                    "character": "miori",
                    "message": "このターンは履歴に残さないで",
                },
            )

    assert response.status_code == 200
    assert _stored_turn(conversation_history_database_path) == (
        None,
        None,
        "privacy_skipped",
        "policy_denied",
    )


def test_should_mark_turn_failed_for_empty_assistant_response(
    client,
    conversation_history_database_path: Path,
) -> None:
    with patch(_LOAD_PERSONALITY, return_value="# prompt"):
        with patch(_GENERATE_RESPONSE, return_value=""):
            response = client.post(
                "/chat",
                json={"character": "miori", "message": "通常の質問です"},
            )

    assert response.status_code == 502
    assert _stored_turn(conversation_history_database_path) == (
        "通常の質問です",
        None,
        "failed",
        None,
    )


def test_should_reject_policy_sensitive_content_at_rag_storage_entry(
    monkeypatch,
) -> None:
    import app.main as main
    import app.memory.rag_service as rag_service

    user_message = "PROJECT-SECRET-0001 を覚えて"
    create_record = MagicMock()
    monkeypatch.setenv("RAG_ENABLED", "true")
    monkeypatch.setattr(rag_service, "create_memory_candidate_record", create_record)

    with patch(_LOAD_PERSONALITY, return_value="# prompt"):
        with patch(
            "app._chat_runtime._rag_service.build_augmented_system_prompt",
            return_value="# prompt",
        ):
            with patch(_GENERATE_RESPONSE, return_value="承知しました"):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/chat",
                        json={"character": "miori", "message": user_message},
                    )

    assert response.status_code == 200
    create_record.assert_not_called()
