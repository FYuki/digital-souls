from collections.abc import Iterator

from app import chat_prompt
from app.conversation_history.prompt_history import RestoredHistoryTurn
from app.prompting import (
    CharacterPrompt,
    CurrentUserMessage,
    PromptBuilder,
    PromptRole,
    RagContext,
    RagItem,
)
from app.model_settings import resolve_model_settings
from tests.prompt_test_support import UnitTokenCounter

EXPECTED_HISTORY_PAGE_SIZE = 32


class _HistorySession:
    def __init__(self, turns: tuple[RestoredHistoryTurn, ...]) -> None:
        self._turns = turns
        self.prompt_turns_calls: list[tuple[int, int]] = []

    def prompt_turns(
        self,
        *,
        max_completed_turns: int,
        page_size: int,
    ) -> Iterator[RestoredHistoryTurn]:
        self.prompt_turns_calls.append((max_completed_turns, page_size))
        return iter(self._turns)


def test_should_budget_prompt_from_runtime_context_not_model_maximum() -> None:
    config = resolve_model_settings(
        {
            "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": "2",
            "CONVERSATION_HISTORY_TOKEN_LIMIT": "10",
            "USER_INPUT_TOKEN_LIMIT": "10",
            "OLLAMA_RESPONSE_RESERVE_TOKENS": "3",
            "OLLAMA_CONTEXT_TOKENS": "20",
            "LLM_CONTEXT_TOKEN_LIMIT": "100",
        }
    )

    prompt_input = chat_prompt._build_prompt_input(
        character=CharacterPrompt("", "", "", "SYSTEM", "", ""),
        rag=RagContext(items=()),
        current_user=CurrentUserMessage("RAW_CURRENT_USER"),
        history_session=_HistorySession(()),
        config=config,
    )

    assert prompt_input.budget.total == 17
    assert prompt_input.budget.character == 17
    assert prompt_input.budget.post_history == 17
    assert prompt_input.budget.history == 10
    assert prompt_input.budget.current_user == 10


def test_should_coordinate_history_budget_and_existing_prompt_builder() -> None:
    config = resolve_model_settings(
        {
            "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": "2",
            "CONVERSATION_HISTORY_TOKEN_LIMIT": "10",
            "USER_INPUT_TOKEN_LIMIT": "10",
            "OLLAMA_RESPONSE_RESERVE_TOKENS": "3",
            "OLLAMA_CONTEXT_TOKENS": "20",
        }
    )
    history_session = _HistorySession(
        (
            RestoredHistoryTurn(
                user_content="MASKED_HISTORY_USER",
                assistant_content="MASKED_HISTORY_ASSISTANT",
                is_completed=True,
            ),
        )
    )
    result = chat_prompt.build_chat_prompt(
        character=CharacterPrompt("", "", "", "SYSTEM", "", ""),
        rag=RagContext(items=(RagItem("RAG", raw_distance=1.25),)),
        current_user=CurrentUserMessage("RAW_CURRENT_USER"),
        history_session=history_session,
        config=config,
        token_counter=UnitTokenCounter(),
    )

    assert history_session.prompt_turns_calls == [
        (config.max_completed_turns, EXPECTED_HISTORY_PAGE_SIZE)
    ]
    assert [(message.role, message.content) for message in result.messages] == [
        (PromptRole.SYSTEM, "## 応答方針\nSYSTEM"),
        (PromptRole.SYSTEM, "## 関連する記憶\nRAG"),
        (PromptRole.USER, "MASKED_HISTORY_USER"),
        (PromptRole.ASSISTANT, "MASKED_HISTORY_ASSISTANT"),
        (PromptRole.USER, "RAW_CURRENT_USER"),
    ]


def test_should_rebuild_same_prompt_input_with_fresh_history_iterator() -> None:
    config = resolve_model_settings(
        {
            "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": "2",
            "CONVERSATION_HISTORY_TOKEN_LIMIT": "10",
            "USER_INPUT_TOKEN_LIMIT": "10",
            "OLLAMA_RESPONSE_RESERVE_TOKENS": "3",
            "OLLAMA_CONTEXT_TOKENS": "20",
        }
    )
    history_session = _HistorySession(
        (
            RestoredHistoryTurn(
                user_content="MASKED_HISTORY_USER",
                assistant_content="MASKED_HISTORY_ASSISTANT",
                is_completed=True,
            ),
        )
    )
    prompt_input = chat_prompt._build_prompt_input(
        character=CharacterPrompt("", "", "", "SYSTEM", "", ""),
        rag=RagContext(items=()),
        current_user=CurrentUserMessage("RAW_CURRENT_USER"),
        history_session=history_session,
        config=config,
    )
    builder = PromptBuilder(UnitTokenCounter())

    first = builder.build(prompt_input)
    second = builder.build(prompt_input)

    assert first == second
    assert history_session.prompt_turns_calls == [
        (config.max_completed_turns, EXPECTED_HISTORY_PAGE_SIZE),
        (config.max_completed_turns, EXPECTED_HISTORY_PAGE_SIZE),
    ]
    assert [message.content for message in second.messages] == [
        "## 応答方針\nSYSTEM",
        "MASKED_HISTORY_USER",
        "MASKED_HISTORY_ASSISTANT",
        "RAW_CURRENT_USER",
    ]
