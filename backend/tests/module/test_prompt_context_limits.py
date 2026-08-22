import importlib
import os
from pathlib import Path
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app import chat_service
from app.chat_prompt import build_chat_prompt
from app.conversation_history.models import ConversationTurn, TurnStatus
from app.conversation_history.service import StartedHistoryTurn
from app.prompting import CharacterPrompt
from tests.conversation_history_test_support import CONVERSATION_ID


class _HistorySession:
    def __init__(self, turns: tuple[object, ...]) -> None:
        self._turns = turns

    def start_turn(self, user_content: str) -> StartedHistoryTurn:
        return StartedHistoryTurn(
            UUID("9e70795d-e5d5-431d-baa2-67f884403080"),
            content_skipped=False,
        )

    def complete_turn(
        self,
        started_turn: StartedHistoryTurn,
        assistant_content: str,
    ) -> ConversationTurn:
        timestamp = datetime(2026, 8, 2, tzinfo=UTC)
        return ConversationTurn(
            turn_id=started_turn.turn_id,
            character_id="miori",
            conversation_id=CONVERSATION_ID,
            user_content="MASKED_USER",
            assistant_content=assistant_content,
            status=TurnStatus.COMPLETED,
            privacy_reason_code=None,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def fail_turn(self, started_turn) -> None:
        return None

    def prompt_turns(self, *, max_completed_turns: int, page_size: int):
        return iter(self._turns)


class _HistoryService:
    def __init__(self, session: _HistorySession) -> None:
        self._session = session

    def open_session(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> _HistorySession:
        return self._session


def _service(
    *,
    turns: tuple[object, ...],
    count_input_tokens,
    generate_response,
):
    from app import _chat_runtime

    card = MagicMock()
    card.to_character_prompt.return_value = CharacterPrompt(
        "", "", "", "system", "", ""
    )
    config = importlib.import_module("app.model_settings").resolve_model_settings(
        os.environ
    )
    return _chat_runtime.ChatService(
        _chat_runtime.ChatRuntimeConfig(
            rag_enabled=False,
            memory_policy=None,
            prompt_config=config,
            chroma_path=Path("/test/runtime-data/chroma"),
        ),
        _HistoryService(_HistorySession(turns)),
        _chat_runtime.ChatRuntimeDependencies(
            character_prompt_loader=lambda character: card.to_character_prompt(),
            prompt_builder=build_chat_prompt,
            llm_response_generator=generate_response,
            input_token_counter=count_input_tokens,
            privacy_scanner=MagicMock(),
            semantic_classifier=MagicMock(),
            approved_memory_repository=MagicMock(),
            memory_formation_submitter=MagicMock(),
        ),
    )


def test_should_reject_user_input_using_formal_provider_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USER_INPUT_TOKEN_LIMIT", "1")
    monkeypatch.setenv("ASSISTANT_MAX_GENERATION_TOKENS", "1")
    monkeypatch.setenv("OLLAMA_CONTEXT_TOKENS", "20")
    monkeypatch.setenv("LLM_CONTEXT_TOKEN_LIMIT", "20")

    def count(messages) -> int:
        if len(messages) == 1 and messages[0].content == "RAW_TOO_LARGE":
            return 2
        return len(messages)

    generate = MagicMock()

    with pytest.raises(chat_service.ChatInputLimitError) as exc_info:
        _service(
            turns=(),
            count_input_tokens=count,
            generate_response=generate,
        ).generate_chat_reply(
            "miori", CONVERSATION_ID, "RAW_TOO_LARGE"
        )

    assert exc_info.value.region == "current_user"
    assert exc_info.value.used == 2
    assert exc_info.value.limit == 1
    generate.assert_not_called()


def test_should_return_persisted_reply_with_history_session_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USER_INPUT_TOKEN_LIMIT", "10")
    monkeypatch.setenv("ASSISTANT_MAX_GENERATION_TOKENS", "10")
    monkeypatch.setenv("OLLAMA_CONTEXT_TOKENS", "100")
    monkeypatch.setenv("LLM_CONTEXT_TOKEN_LIMIT", "100")
    generate = MagicMock(return_value="MASKED_ASSISTANT")

    reply = _service(
        turns=(),
        count_input_tokens=lambda messages: len(messages),
        generate_response=generate,
    ).generate_chat_reply("miori", CONVERSATION_ID, "RAW_CURRENT")

    assert isinstance(reply.persisted_turn, chat_service.PersistedContentTurn)
    assert reply.persisted_turn.user_content == "MASKED_USER"
    assert reply.persisted_turn.assistant_content == "MASKED_ASSISTANT"


def test_should_reserve_assistant_tokens_and_keep_latest_completed_or_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USER_INPUT_TOKEN_LIMIT", "10")
    monkeypatch.setenv("ASSISTANT_MAX_GENERATION_TOKENS", "18")
    monkeypatch.setenv("OLLAMA_CONTEXT_TOKENS", "20")
    monkeypatch.setenv("LLM_CONTEXT_TOKEN_LIMIT", "20")
    restored_type = getattr(
        importlib.import_module("app.conversation_history.prompt_history"),
        "RestoredHistoryTurn",
    )
    latest_completed = restored_type(
        user_content="MASKED_LATEST_USER",
        assistant_content="MASKED_LATEST_ASSISTANT",
        is_completed=True,
    )
    generate = MagicMock()

    with pytest.raises(chat_service.ChatInputLimitError) as exc_info:
        _service(
            turns=(latest_completed,),
            count_input_tokens=lambda messages: len(messages),
            generate_response=generate,
        ).generate_chat_reply(
            "miori", CONVERSATION_ID, "RAW_CURRENT"
        )

    assert exc_info.value.region == "total"
    assert exc_info.value.used > exc_info.value.limit
    assert exc_info.value.limit == 20
    generate.assert_not_called()
