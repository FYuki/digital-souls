from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.characters.loader import load_character_card, load_tts_config
from app.prompting import (
    CurrentUserMessage,
    HistoryCandidates,
    PromptBuildInput,
    PromptBuilder,
    PromptMessage,
    RagContext,
    TokenBudget,
)
from tests.character_card_test_support import (
    character_card_data,
    character_card_document,
    use_character_repo_root,
    write_character_card,
)
from tests.conversation_history_test_support import CONVERSATION_ID

pytestmark = pytest.mark.usefixtures("existing_chat_conversations")


class UnitTokenCounter:
    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        return sum(message.content != "" for message in messages)


def test_should_build_runtime_prompt_from_shipped_character_card() -> None:
    card = load_character_card("miori")
    character_prompt = card.to_character_prompt()
    tts_config = load_tts_config("miori")
    prompt_input = PromptBuildInput(
        character=character_prompt,
        rag=RagContext(items=()),
        history=HistoryCandidates(
            newest_first_factory=lambda: iter(()), omitted_turns=0
        ),
        current_user=CurrentUserMessage("現在ターンの入力"),
        budget=TokenBudget(
            total=20,
            character=10,
            character_lore=10,
            rag=10,
            history=10,
            current_user=10,
            post_history=10,
        ),
    )

    result = PromptBuilder(token_counter=UnitTokenCounter()).build(prompt_input)

    prompt_contents = [message.content for message in result.messages]
    character_region = prompt_contents[0]
    assert character_prompt.description in character_region
    assert character_prompt.personality in character_region
    assert character_prompt.scenario in character_region
    assert character_prompt.system_prompt in character_region
    assert character_prompt.mes_example in character_region
    assert all(card.data.first_mes not in content for content in prompt_contents)
    assert prompt_contents[1] == character_prompt.post_history_instructions
    assert prompt_contents[-1] == "現在ターンの入力"
    assert tts_config.speaker_id == 14


def test_should_omit_final_instruction_when_card_field_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = character_card_data()
    data.pop("post_history_instructions")
    write_character_card(
        tmp_path,
        "test",
        character_card_document(data=data),
    )
    use_character_repo_root(monkeypatch, tmp_path)
    card = load_character_card("test")
    prompt_input = PromptBuildInput(
        character=card.to_character_prompt(),
        rag=RagContext(items=()),
        history=HistoryCandidates(
            newest_first_factory=lambda: iter(()), omitted_turns=0
        ),
        current_user=CurrentUserMessage("現在ターンの入力"),
        budget=TokenBudget(
            total=20,
            character=10,
            character_lore=10,
            rag=10,
            history=10,
            current_user=10,
            post_history=10,
        ),
    )

    result = PromptBuilder(token_counter=UnitTokenCounter()).build(prompt_input)

    assert card.data.post_history_instructions == ""
    assert result.messages[-1].role == "user"
    assert result.messages[-1].content == "現在ターンの入力"


def test_should_send_builder_messages_from_http_entrypoint(
    monkeypatch,
) -> None:
    from app.main import app

    card = load_character_card("miori")
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "message": {"role": "assistant", "content": "応答"},
        "prompt_eval_count": 1,
    }
    monkeypatch.setenv("RAG_ENABLED", "false")

    with patch(
        "app.llm.ollama_client.httpx.post",
        return_value=response,
    ) as ollama_post:
        with TestClient(app) as client:
            result = client.post(
                "/chat",
                json={
                    "character": "miori",
                    "conversation_id": str(CONVERSATION_ID),
                    "message": "HTTP_CURRENT_USER",
                },
            )

    assert result.status_code == 200
    messages = ollama_post.call_args.kwargs["json"]["messages"]
    contents = [message["content"] for message in messages]
    assert card.data.description in contents[0]
    assert card.data.system_prompt in contents[0]
    assert all(card.data.first_mes not in content for content in contents)
    assert contents[1] == card.data.post_history_instructions
    assert contents[-1] == "HTTP_CURRENT_USER"
