import importlib
import os
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app import chat_service
from app.chat_prompt import build_chat_prompt
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

    def complete_turn(self, started_turn, assistant_content: str) -> bool:
        return True

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


class _TaskQueue:
    def add_task(self, func, *args, **kwargs) -> None:
        return None


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
            privacy_scanner=None,
            prompt_config=config,
        ),
        _TaskQueue(),
        _HistoryService(_HistorySession(turns)),
        _chat_runtime.ChatRuntimeDependencies(
            character_prompt_loader=lambda character: card.to_character_prompt(),
            prompt_builder=build_chat_prompt,
            llm_response_generator=generate_response,
            input_token_counter=count_input_tokens,
        ),
    )


def test_should_reject_user_input_using_formal_provider_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import _chat_runtime

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


def test_should_reserve_assistant_tokens_and_keep_latest_completed_or_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import _chat_runtime

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
