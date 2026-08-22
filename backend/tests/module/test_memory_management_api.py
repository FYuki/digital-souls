from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest


PERSONA_COLLECTION = "/characters/{character_id}/persona-memories"
PERSONA_ITEM = f"{PERSONA_COLLECTION}/{{memory_id}}"
TEMPORARY_COLLECTION = "/characters/{character_id}/temporary-records/{provider_id}"
TEMPORARY_ITEM = f"{TEMPORARY_COLLECTION}/{{record_id}}"
MISSING_ID = "00000000-0000-4000-8000-000000000099"
OPERATION_ID = "10000000-0000-4000-8000-000000000099"
MEMORY_ID = "00000000-0000-4000-8000-000000000012"
RECORD_ID = "10000000-0000-4000-8000-000000000012"
EFFECTIVE_AT = "2026-08-20T00:00:00.000000Z"


def _persona_response() -> dict[str, object]:
    return {
        "id": MEMORY_ID,
        "character_id": "miori",
        "provider_id": "core",
        "memory_kind": "SEMANTIC",
        "memory_type": "USER_PREFERENCE",
        "normalized_text": "ユーザーは紅茶を好む",
        "effective_at": EFFECTIVE_AT,
        "status": "ACTIVE",
        "content_version": 2,
        "index_pending": True,
        "sources": [],
        "lineage": [],
    }


def _temporary_response() -> dict[str, object]:
    return {
        "id": RECORD_ID,
        "character_id": "miori",
        "provider_id": "temporary:recipe",
        "source_ref": "recipe-12",
        "record_type": "RECIPE",
        "structured_value": '{"name":"カレー"}',
        "effective_at": EFFECTIVE_AT,
        "updated_at": EFFECTIVE_AT,
    }


def test_memory_management_uses_separate_persona_and_temporary_routes(client) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths[PERSONA_COLLECTION]) >= {"get"}
    assert set(paths[PERSONA_ITEM]) >= {"get", "patch", "delete"}
    assert set(paths[TEMPORARY_COLLECTION]) >= {"get"}
    assert set(paths[TEMPORARY_ITEM]) >= {"get", "patch", "delete"}


def test_persona_list_route_delegates_to_persona_provider(client) -> None:
    provider = MagicMock()
    provider.list.return_value = []
    client.app.state.persona_memory_provider = provider

    response = client.get("/characters/miori/persona-memories?status=ACTIVE")

    assert response.status_code == 200
    assert response.json() == []
    provider.list.assert_called_once_with(character_id="miori", status="ACTIVE")


def test_temporary_list_route_delegates_to_addon_provider(client) -> None:
    provider = MagicMock()
    provider.list.return_value = []
    client.app.state.addon_record_provider = provider

    response = client.get(
        "/characters/miori/temporary-records/temporary:recipe"
    )

    assert response.status_code == 200
    assert response.json() == []
    provider.list.assert_called_once_with(
        character_id="miori", provider_id="temporary:recipe"
    )


def test_persona_detail_route_delegates_to_persona_provider(client) -> None:
    expected = _persona_response()
    provider = MagicMock()
    provider.get.return_value = expected
    client.app.state.persona_memory_provider = provider

    response = client.get(f"/characters/miori/persona-memories/{MEMORY_ID}")

    assert response.status_code == 200
    assert response.json() == expected
    provider.get.assert_called_once_with(
        character_id="miori", memory_id=UUID(MEMORY_ID)
    )


def test_persona_patch_route_delegates_validated_candidate(client) -> None:
    expected = _persona_response()
    provider = MagicMock()
    provider.correct.return_value = expected
    client.app.state.persona_memory_provider = provider

    response = client.patch(
        f"/characters/miori/persona-memories/{MEMORY_ID}",
        json={
            "idempotency_key": OPERATION_ID,
            "memory_type": "USER_PREFERENCE",
            "structured_value": {"polarity": "LIKE", "object": "紅茶"},
        },
    )

    assert response.status_code == 200
    assert response.json() == expected
    call = provider.correct.call_args
    assert call.kwargs["character_id"] == "miori"
    assert call.kwargs["memory_id"] == UUID(MEMORY_ID)
    assert call.kwargs["idempotency_key"] == UUID(OPERATION_ID)
    assert call.kwargs["candidate"].memory_type.value == "USER_PREFERENCE"
    assert call.kwargs["candidate"].structured_value.polarity.value == "LIKE"
    assert call.kwargs["candidate"].structured_value.object == "紅茶"


def test_persona_patch_returns_privacy_reason_without_rejected_body(client) -> None:
    from app.memory.providers import MemoryCorrectionRejected

    marker = "REJECTED_PERSONA_API_SECRET_12"
    provider = MagicMock()
    provider.correct.side_effect = MemoryCorrectionRejected(
        reason_code="DENY_SENSITIVE"
    )
    client.app.state.persona_memory_provider = provider

    response = client.patch(
        f"/characters/miori/persona-memories/{MEMORY_ID}",
        json={
            "idempotency_key": OPERATION_ID,
            "memory_type": "USER_PREFERENCE",
            "structured_value": {"polarity": "LIKE", "object": marker},
        },
    )

    assert response.status_code == 422
    assert response.json() == {"reason_code": "DENY_SENSITIVE"}
    assert marker not in response.text


def test_persona_delete_route_delegates_and_returns_no_content(client) -> None:
    provider = MagicMock()
    client.app.state.persona_memory_provider = provider

    response = client.delete(f"/characters/miori/persona-memories/{MEMORY_ID}")

    assert response.status_code == 204
    assert response.content == b""
    provider.hard_delete.assert_called_once_with(
        character_id="miori", memory_id=UUID(MEMORY_ID)
    )


def test_temporary_detail_route_delegates_to_addon_provider(client) -> None:
    expected = _temporary_response()
    provider = MagicMock()
    provider.get.return_value = expected
    client.app.state.addon_record_provider = provider

    response = client.get(
        f"/characters/miori/temporary-records/temporary:recipe/{RECORD_ID}"
    )

    assert response.status_code == 200
    assert response.json() == expected
    provider.get.assert_called_once_with(
        character_id="miori",
        provider_id="temporary:recipe",
        record_id=UUID(RECORD_ID),
    )


def test_temporary_patch_route_delegates_validated_correction(client) -> None:
    expected = _temporary_response()
    provider = MagicMock()
    provider.correct.return_value = expected
    client.app.state.addon_record_provider = provider

    response = client.patch(
        f"/characters/miori/temporary-records/temporary:recipe/{RECORD_ID}",
        json={
            "record_type": "RECIPE",
            "structured_value": '{"name":"カレー"}',
            "effective_at": EFFECTIVE_AT,
        },
    )

    assert response.status_code == 200
    assert response.json() == expected
    call = provider.correct.call_args
    assert call.kwargs["character_id"] == "miori"
    assert call.kwargs["provider_id"] == "temporary:recipe"
    assert call.kwargs["record_id"] == UUID(RECORD_ID)
    assert call.kwargs["correction"].record_type == "RECIPE"
    assert call.kwargs["correction"].structured_value == '{"name":"カレー"}'
    assert call.kwargs["correction"].effective_at.isoformat() == (
        "2026-08-20T00:00:00+00:00"
    )


def test_temporary_delete_route_delegates_and_returns_no_content(client) -> None:
    provider = MagicMock()
    client.app.state.addon_record_provider = provider

    response = client.delete(
        f"/characters/miori/temporary-records/temporary:recipe/{RECORD_ID}"
    )

    assert response.status_code == 204
    assert response.content == b""
    provider.hard_delete.assert_called_once_with(
        character_id="miori",
        provider_id="temporary:recipe",
        record_id=UUID(RECORD_ID),
    )


@pytest.mark.parametrize(
    "path",
    [
        f"/characters/miori/persona-memories/{MISSING_ID}",
        f"/characters/miori/temporary-records/temporary:recipe/{MISSING_ID}",
    ],
)
def test_delete_of_missing_memory_is_a_repeatable_noop(client, path: str) -> None:
    first = client.delete(path)
    second = client.delete(path)

    assert first.status_code == 204
    assert second.status_code == 204
    assert first.content == second.content == b""


@pytest.mark.parametrize(
    "provider_id",
    ["core", "temporary:unknown", "TEMPORARY:RECIPE"],
)
def test_temporary_api_rejects_non_temporary_provider_ids(
    client, provider_id: str
) -> None:
    response = client.get(f"/characters/miori/temporary-records/{provider_id}")

    assert 400 <= response.status_code < 500
    assert provider_id not in response.text


def test_validation_error_does_not_echo_rejected_correction_body(client) -> None:
    marker = "REJECTED_CORRECTION_SECRET_12"
    response = client.patch(
        f"/characters/miori/persona-memories/{MISSING_ID}",
        json={
            "idempotency_key": OPERATION_ID,
            "memory_type": "USER_PREFERENCE",
            "structured_value": {"polarity": "LIKE", "object": marker * 20},
        },
    )

    assert response.status_code == 422
    assert marker not in response.text
