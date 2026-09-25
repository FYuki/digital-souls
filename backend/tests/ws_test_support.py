import sqlite3
import threading
import time

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from unittest.mock import ANY, MagicMock, patch

from app.audio_pipeline import resolve_audio_runtime_config
from app.inference.contracts import ProviderTextResult
from app.main import app
from app.memory.chroma_store import MemorySearchResult
from app.memory.rag_service import RetrievalOutcome
from app.prompting import CharacterPrompt, PromptInputLimitError
from app.prompting.builder import PromptBuilder
from tests.chat_reply_test_support import persisted_reply
from tests.character_card_test_support import (
    character_card_data,
    character_card_document,
    write_character_card,
)
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    OTHER_CONVERSATION_ID,
    TURN_ID,
)

_LOAD_PERSONALITY = "app.main.load_character_card"
_GENERATE_RESPONSE = "app.llm.router.generate_response"
_COUNT_INPUT_TOKENS = "app.llm.router.count_input_tokens"
_BUILD_AUGMENTED_SYSTEM_PROMPT = (
    "app._chat_runtime._rag_service.retrieve_prompt_memories"
)
_RESOLVED_MEMORY_POLICY = "app.runtime.application.resolved_memory_policy"
_LOAD_TTS_CONFIG = "app.audio_pipeline.load_tts_config"
_TRANSCRIBE = "app.stt.remote_whisper_client.RemoteWhisperTranscriber.transcribe"
_SYNTHESIZE = "app.tts.voicevox_client.VoicevoxClient.synthesize"
_BUILD_PROMPT = "app.chat_prompt.PromptBuilder.build"

_PERSONALITY = "# 光織\n穏やかなAIです。"
_LLM_REPLY = "光織です。よろしくお願いします。"


@pytest.fixture(autouse=True)
def _formal_token_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _COUNT_INPUT_TOKENS, lambda messages, *, settings: len(messages)
    )
_PCM_AUDIO = b"\x01\x00\x02\x00"
_ODD_LENGTH_PCM_AUDIO = b"\x01\x00\x03"
_TTS_CONFIG_MISSING_MESSAGE = "'tts_config' field is missing in character card data"
_WS_URL = f"/ws/miori?conversation_id={CONVERSATION_ID}"

pytestmark = pytest.mark.usefixtures("existing_chat_conversations")


class _StubDeliverySession:
    def mark_delivered(self, turn_id):
        return None

    def mark_delivery_failed(self, turn_id):
        return None

    def close(self):
        return None


def _character_card(system_prompt: str = _PERSONALITY) -> MagicMock:
    card = MagicMock()
    card.data.character_book = None
    card.to_character_prompt.return_value = CharacterPrompt(
        description="",
        personality="",
        scenario="",
        system_prompt=system_prompt,
        mes_example="",
        post_history_instructions="",
    )
    return card


def _generated_contents(generate: MagicMock) -> list[str]:
    prompt = generate.call_args.args[0]
    return [message.content for message in prompt.messages]


def _generated_user_messages(generate: MagicMock) -> list[str]:
    return [
        next(
            message.content
            for message in reversed(call.args[0].messages)
            if message.role.value == "user"
        )
        for call in generate.call_args_list
    ]


def _wait_for_event(event: threading.Event, label: str, timeout: float = 5.0) -> None:
    if not event.wait(timeout=timeout):
        raise AssertionError(f"{label} was not observed before timeout")


def _assert_persisted_content_frame(payload: dict, assistant_content: str) -> None:
    assert set(payload) == {"type", "turn"}
    assert payload["type"] == "text"
    assert payload["turn"]["kind"] == "content"
    assert payload["turn"]["assistant_content"] == assistant_content


def _persisted_content_frame(assistant_content: str) -> dict:
    return {
        "type": "text",
        "turn": {
            "kind": "content",
            "turn_id": str(TURN_ID),
            "user_content": "saved user content",
            "assistant_content": assistant_content,
        },
    }


def _tts_config():
    from app.characters.loader import VoicevoxTtsConfig

    return VoicevoxTtsConfig(speaker_id=14)


def _ollama_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "message": {"role": "assistant", "content": content},
    }
    response.raise_for_status.return_value = None
    return response


def _write_character(tmp_path, character: str, system_prompt: str) -> None:
    data = character_card_data(
        description="",
        personality="",
        scenario="",
        system_prompt=system_prompt,
        mes_example="",
        post_history_instructions="",
    )
    write_character_card(
        tmp_path,
        character,
        character_card_document(data=data),
    )


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")
