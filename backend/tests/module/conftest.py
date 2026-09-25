import json
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


@pytest.fixture(autouse=True)
def isolate_semantic_model_digest(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    if request.node.get_closest_marker("semantic_digest_http") is not None:
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


@pytest.fixture(autouse=True)
def isolate_episodic_inference(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    from app.memory.episodic.contracts import ExtractionIdentity
    from app.memory.formation import runtime as episodic_runtime
    from app.memory.formation.compact_extractor import COMPACT_EXTRACTOR_VERSION
    # Module既定では外部LLMへ接続しない。予約・起動・停止は実装を通す。
    monkeypatch.setattr(episodic_runtime, "extraction_identity", lambda *args, **kwargs: ExtractionIdentity(
        provider_id="ollama", model_id="gemma4:e4b", model_digest=_MODEL_DIGEST,
        prompt_version=COMPACT_EXTRACTOR_VERSION,
    ))
    if request.node.get_closest_marker("episodic_model_output") is None:
        import app.runtime.memory as memory_runtime

        class EmptyEpisodeClient:
            def fits(self, *args, **kwargs):
                return True

            def chat(self, *args, **kwargs):
                properties = kwargs["json_schema"].get("properties", {})
                key = "facts" if "facts" in properties else "episodes"
                return json.dumps({"has_unprocessed_input": False, key: []})

        build = episodic_runtime.build_episodic_scheduler

        def build_without_external_episode_llm(**kwargs):
            return build(**{
                **kwargs, "client": EmptyEpisodeClient(),
                "entity_labels": lambda character: {
                    "speaker:user": "ユーザー", f"character:{character}": "合成キャラクター",
                },
            })

        monkeypatch.setattr(
            memory_runtime,
            "build_episodic_scheduler",
            build_without_external_episode_llm,
        )
