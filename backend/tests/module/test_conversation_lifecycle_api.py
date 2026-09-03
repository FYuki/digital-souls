from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.conversation_history.models import (
    ConversationTurn,
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
    TurnStatus,
)
from app.privacy.contracts import HistoryDecisionReasonCode


BASE_PATH = "/characters/miori/conversations"
INVALID_UUIDS = (
    "not-a-uuid",
    "e98d6c65-1ae9-3d6f-a8c8-d59b0ad09010",
    "E98D6C65-1AE9-4D6F-A8C8-D59B0AD09010",
)
SENSITIVE_VALUE = "SECRET_RAW_VALUE_7F91"


def _create_completed_conversation(client):  # type: ignore[no-untyped-def]
    repository = client.app.state.conversation_history_repository
    conversation = repository.create_conversation("miori")
    turn = repository.create_processing_turn(
        "miori",
        conversation.conversation_id,
        ProcessingTurnInput(sanitized_user_content="連絡先は[REDACTED]です"),
    )
    repository.complete_turn(
        "miori",
        conversation.conversation_id,
        turn.turn_id,
        sanitized_assistant_content="保存済みの回答です",
    )
    return conversation


def test_should_create_conversation_through_public_api(client) -> None:
    created = client.post(BASE_PATH)

    assert created.status_code == 201
    assert created.json()["character_id"] == "miori"
    assert created.json()["title"] == "新しい会話"


def test_should_list_active_conversations_through_public_api(client) -> None:
    conversation = _create_completed_conversation(client)

    listed = client.get(BASE_PATH)

    assert listed.status_code == 200
    assert [item["conversation_id"] for item in listed.json()] == [
        str(conversation.conversation_id)
    ]
    assert listed.json()[0]["title"] == "連絡先は[REDACTED]です"


def test_should_rename_active_and_archived_conversation(client) -> None:
    conversation = _create_completed_conversation(client)

    renamed = client.patch(
        f"{BASE_PATH}/{conversation.conversation_id}",
        json={"title": "  手動で変更した名前  "},
    )
    archived = client.post(f"{BASE_PATH}/{conversation.conversation_id}/archive")
    renamed_archived = client.patch(
        f"{BASE_PATH}/{conversation.conversation_id}",
        json={"title": "アーカイブ後の名前"},
    )

    assert renamed.status_code == 200
    assert renamed.json()["title"] == "手動で変更した名前"
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert renamed_archived.status_code == 200
    assert renamed_archived.json()["title"] == "アーカイブ後の名前"
    assert renamed_archived.json()["archived_at"] is not None


@pytest.mark.parametrize("title", ["", "   ", "改行\nタイトル", "a" * 41])
def test_should_reject_invalid_conversation_title(client, title: str) -> None:
    conversation = _create_completed_conversation(client)

    response = client.patch(
        f"{BASE_PATH}/{conversation.conversation_id}",
        json={"title": title},
    )

    assert response.status_code == 422


def test_should_hide_rename_character_boundary_as_not_found(client) -> None:
    conversation = _create_completed_conversation(client)

    response = client.patch(
        f"/characters/akira/conversations/{conversation.conversation_id}",
        json={"title": "境界外の変更"},
    )

    assert response.status_code == 404


def test_should_list_archived_conversations_through_separate_public_api(client) -> None:
    conversation = _create_completed_conversation(client)
    archived = client.post(f"{BASE_PATH}/{conversation.conversation_id}/archive")
    assert archived.status_code == 200

    listed = client.get(f"{BASE_PATH}/archived")

    assert listed.status_code == 200
    assert [item["conversation_id"] for item in listed.json()] == [
        str(conversation.conversation_id)
    ]


@pytest.mark.parametrize("conversation_id", INVALID_UUIDS)
def test_should_reject_non_uuid4_path_parameter_without_echoing_input(
    client,
    conversation_id: str,
) -> None:
    response = client.get(f"{BASE_PATH}/{conversation_id}/turns")

    assert response.status_code == 422
    assert conversation_id not in response.text


def test_should_hide_character_boundary_violation_as_not_found(client) -> None:
    conversation = _create_completed_conversation(client)
    paths = client.get("/openapi.json").json()["paths"]
    assert (
        "/characters/{character_id}/conversations/{conversation_id}/turns" in paths
    ), "conversation turn history API route is not implemented"

    missing = client.get(f"{BASE_PATH}/e98d6c65-1ae9-4d6f-a8c8-d59b0ad09019/turns")
    boundary = client.get(
        f"/characters/akira/conversations/{conversation.conversation_id}/turns"
    )

    assert missing.status_code == 404
    assert boundary.status_code == 404
    assert boundary.json() == missing.json()
    assert "miori" not in boundary.text
    assert "保存済み" not in boundary.text


def test_should_return_only_persisted_masked_turn_content(client) -> None:
    conversation = _create_completed_conversation(client)

    response = client.get(f"{BASE_PATH}/{conversation.conversation_id}/turns")

    assert response.status_code == 200
    assert response.json()[0]["user_content"] == "連絡先は[REDACTED]です"
    assert response.json()[0]["assistant_content"] == "保存済みの回答です"
    assert SENSITIVE_VALUE not in response.text


def test_should_serialize_interrupted_partial_as_existing_content_response(
    client,
) -> None:
    conversation_id = uuid4()
    turn_id = uuid4()
    now = datetime.now(UTC)
    interrupted = ConversationTurn(
        turn_id=turn_id,
        character_id="miori",
        conversation_id=conversation_id,
        user_content="中断前の質問",
        assistant_content="実際に再生された部分",
        status=TurnStatus.INTERRUPTED,
        privacy_reason_code=None,
        created_at=now,
        updated_at=now,
    )
    service = client.app.state.conversation_lifecycle_service
    service.list_conversation_turns = MagicMock(return_value=[interrupted])

    response = client.get(f"{BASE_PATH}/{conversation_id}/turns")

    assert response.status_code == 200
    assert response.json() == [
        {
            "kind": "content",
            "turn_id": str(turn_id),
            "user_content": "中断前の質問",
            "assistant_content": "実際に再生された部分",
        }
    ]


def test_should_serialize_privacy_skipped_turn_as_metadata_only(client) -> None:
    repository = client.app.state.conversation_history_repository
    conversation = repository.create_conversation("miori")
    repository.create_privacy_skipped_turn(
        "miori",
        conversation.conversation_id,
        PrivacySkippedTurnInput(
            reason_code=HistoryDecisionReasonCode.STORAGE_OPT_OUT,
            sanitizer_version="history-sanitizer-v1",
            policy_version="privacy-policy-v1",
        ),
    )

    response = client.get(f"{BASE_PATH}/{conversation.conversation_id}/turns")

    assert response.status_code == 200
    turn = response.json()[0]
    assert turn["kind"] == "privacy_skipped"
    assert turn["reason_code"] == "STORAGE_OPT_OUT"
    assert "user_content" not in turn
    assert "assistant_content" not in turn
    assert "content" not in turn


def test_should_exclude_non_persisted_turn_states_from_history_api(client) -> None:
    repository = client.app.state.conversation_history_repository
    conversation = repository.create_conversation("miori")
    failed = repository.create_processing_turn(
        "miori",
        conversation.conversation_id,
        ProcessingTurnInput(sanitized_user_content="失敗した保存済み質問"),
    )
    repository.fail_turn(
        "miori",
        conversation.conversation_id,
        failed.turn_id,
    )
    repository.create_processing_turn(
        "miori",
        conversation.conversation_id,
        ProcessingTurnInput(sanitized_user_content="処理中の保存済み質問"),
    )

    response = client.get(f"{BASE_PATH}/{conversation.conversation_id}/turns")

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("operation", ["archive", "unarchive"])
def test_should_expose_archive_transitions_through_public_api(
    client,
    operation: str,
) -> None:
    conversation = _create_completed_conversation(client)
    if operation == "unarchive":
        client.post(f"{BASE_PATH}/{conversation.conversation_id}/archive")

    response = client.post(f"{BASE_PATH}/{conversation.conversation_id}/{operation}")

    assert response.status_code == 200
    expected_archived = operation == "archive"
    assert (response.json()["archived_at"] is not None) is expected_archived


def test_should_not_log_persisted_content_during_lifecycle_operations(
    client,
    caplog,
) -> None:
    repository = client.app.state.conversation_history_repository
    conversation = repository.create_conversation("miori")
    repository.create_processing_turn(
        "miori",
        conversation.conversation_id,
        ProcessingTurnInput(sanitized_user_content=SENSITIVE_VALUE),
    )

    archived = client.post(f"{BASE_PATH}/{conversation.conversation_id}/archive")
    deleted = client.delete(f"{BASE_PATH}/{conversation.conversation_id}")

    assert archived.status_code == 200
    assert deleted.status_code == 204
    assert SENSITIVE_VALUE not in caplog.text


def test_conversation_delete_does_not_invoke_persona_memory_deletion(client) -> None:
    conversation = _create_completed_conversation(client)
    persona_memory_provider = MagicMock()
    client.app.state.persona_memory_provider = persona_memory_provider

    response = client.delete(f"{BASE_PATH}/{conversation.conversation_id}")

    assert response.status_code == 204
    persona_memory_provider.hard_delete.assert_not_called()


def test_should_not_use_another_conversation_history_for_prompt(client) -> None:
    first = _create_completed_conversation(client)
    second = _create_completed_conversation(client)
    repository = client.app.state.conversation_history_repository

    first_turns = repository.list_prompt_turns_page(
        "miori",
        first.conversation_id,
        page_size=10,
    ).turns
    second_turns = repository.list_prompt_turns_page(
        "miori",
        second.conversation_id,
        page_size=10,
    ).turns

    assert {turn.conversation_id for turn in first_turns} == {first.conversation_id}
    assert {turn.conversation_id for turn in second_turns} == {second.conversation_id}


@pytest.mark.parametrize(
    "expected_fragment",
    (
        "SQLite",
        "conversation",
        "全turn",
        "復元できません",
        "RAG長期記憶は削除されません",
        "backup",
        "snapshot",
        "ファイルシステム上の複製",
        "消去を保証しません",
    ),
)
def test_should_document_hard_delete_scope_in_openapi(
    client,
    expected_fragment: str,
) -> None:
    operation = client.get("/openapi.json").json()["paths"][
        "/characters/{character_id}/conversations/{conversation_id}"
    ]["delete"]
    documentation = f"{operation.get('summary', '')}\n{operation.get('description', '')}"

    assert expected_fragment in documentation
