import sqlite3

import pytest

from app.conversation_history.schema import initialize_conversation_history_schema
from app.inference.runtime import InferenceRuntime
from app.llm import router
from app.privacy.semantic.inference_client import InferenceSemanticClassifierClient
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    OTHER_CONVERSATION_ID,
)


_MODEL_DIGEST = "sha256:" + "f" * 64


@pytest.fixture(autouse=True)
def successful_inference_startup_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Module testは外部Providerへ接続せず、起動後の統合だけを検証する。"""

    def probe(runtime: InferenceRuntime) -> None:
        for target in runtime.settings.targets:
            runtime.health.record_success(target)

    monkeypatch.setattr(InferenceRuntime, "probe_startup", probe)


@pytest.fixture
def semantic_model_digest_http() -> None:
    return None


@pytest.fixture(autouse=True)
def isolate_semantic_model_digest(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    if "semantic_model_digest_http" in request.fixturenames:
        return
    monkeypatch.setattr(
        InferenceSemanticClassifierClient,
        "resolve_model_digest",
        lambda _client, **_kwargs: _MODEL_DIGEST,
    )


@pytest.fixture(autouse=True)
def mock_provider_token_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        router,
        "count_input_tokens",
        lambda messages, *, settings: len(messages),
    )


@pytest.fixture
def existing_chat_conversations(conversation_history_database_path) -> None:
    initialize_conversation_history_schema(conversation_history_database_path)
    rows = (
        ("miori", str(CONVERSATION_ID)),
        ("miori", str(OTHER_CONVERSATION_ID)),
        ("other", str(CONVERSATION_ID)),
    )
    with sqlite3.connect(conversation_history_database_path) as connection:
        connection.executemany(
            "INSERT INTO conversations "
            "(character_id, conversation_id, created_at) VALUES (?, ?, ?)",
            (
                (character_id, conversation_id, "2026-08-01T00:00:00.000000Z")
                for character_id, conversation_id in rows
            ),
        )


@pytest.fixture
def unknown_chat_conversation(
    existing_chat_conversations,
    conversation_history_database_path,
) -> None:
    with sqlite3.connect(conversation_history_database_path) as connection:
        connection.execute(
            "DELETE FROM conversations WHERE character_id = ?",
            ("miori",),
        )
