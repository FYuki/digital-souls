import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conversation_history_test_support import CONVERSATION_ID

pytestmark = pytest.mark.usefixtures("existing_chat_conversations")


_GENERATE_RESPONSE = "app.llm.router.generate_response"
_COUNT_INPUT_TOKENS = "app.llm.router.count_input_tokens"


@pytest.fixture(autouse=True)
def _formal_token_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _COUNT_INPUT_TOKENS, lambda messages, *, settings: len(messages)
    )


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
    with patch(
        _GENERATE_RESPONSE,
        return_value="確認: password: synthetic-assistant-secret",
    ):
        response = client.post(
            "/chat",
            json={
                "character": "miori",
                "conversation_id": str(CONVERSATION_ID),
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
    with patch(_GENERATE_RESPONSE, return_value="承知しました"):
        response = client.post(
            "/chat",
            json={
                "character": "miori",
                "conversation_id": str(CONVERSATION_ID),
                "message": "このターンは履歴に残さないで",
            },
        )

    assert response.status_code == 200
    assert _stored_turn(conversation_history_database_path) == (
        None,
        None,
        "privacy_skipped",
        "STORAGE_OPT_OUT",
    )


def test_should_mark_turn_failed_for_empty_assistant_response(
    client,
    conversation_history_database_path: Path,
) -> None:
    with patch(_GENERATE_RESPONSE, return_value=""):
        response = client.post(
            "/chat",
            json={
                "character": "miori",
                "conversation_id": str(CONVERSATION_ID),
                "message": "通常の質問です",
            },
        )

    assert response.status_code == 502
    assert _stored_turn(conversation_history_database_path) == (
        "通常の質問です",
        None,
        "failed",
        None,
    )
