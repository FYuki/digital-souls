from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID
from typing import cast

import pytest

from app import _chat_runtime, chat_service
from app.chat_prompt import build_chat_prompt
from app.characters.loader import load_character_card
from app.conversation_history.models import ConversationTurn, TurnStatus
from app.conversation_history.prompt_history import RestoredHistoryTurn
from app.conversation_history.service import (
    HistoryService,
    HistorySession,
    StartedHistoryTurn,
)
from app.model_settings import resolve_model_settings
from app.prompting import BuiltPrompt
from tests.character_card_test_support import (
    character_book,
    character_book_entry,
    character_card_data,
    character_card_document,
    use_character_repo_root,
    write_character_card,
)

CONVERSATION_ID = UUID("00000000-0000-4000-8000-000000000117")
TURN_ID = UUID("00000000-0000-4000-8000-000000000118")


class _HistorySession:
    def start_turn(self, user_content: str) -> StartedHistoryTurn:
        self.user_content = user_content
        return StartedHistoryTurn(TURN_ID, content_skipped=False)

    def complete_turn(
        self,
        started_turn: StartedHistoryTurn,
        assistant_content: str,
    ) -> ConversationTurn:
        now = datetime(2026, 8, 28, tzinfo=UTC)
        return ConversationTurn(
            turn_id=started_turn.turn_id,
            character_id="test",
            conversation_id=CONVERSATION_ID,
            user_content=self.user_content,
            assistant_content=assistant_content,
            status=TurnStatus.COMPLETED,
            privacy_reason_code=None,
            created_at=now,
            updated_at=now,
        )

    def fail_turn(self, started_turn: StartedHistoryTurn) -> None:
        raise AssertionError(f"unexpected failed turn: {started_turn.turn_id}")

    def prompt_turns(
        self,
        *,
        max_completed_turns: int,
        page_size: int,
    ) -> Iterator[RestoredHistoryTurn]:
        del max_completed_turns, page_size
        return iter(())


class _HistoryService:
    def __init__(self) -> None:
        self.opened: list[_HistorySession] = []

    def open_session(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> _HistorySession:
        assert character_id == "test"
        assert conversation_id == CONVERSATION_ID
        session = _HistorySession()
        self.opened.append(session)
        return session


def _write_lore_card(repo_root: Path) -> None:
    book = character_book(
        scan_depth=1,
        entries=[
            character_book_entry(
                keys=["月"],
                content="before-lore",
                position="before_char",
                insertion_order=10,
            ),
            character_book_entry(
                keys=[],
                content="after-lore",
                constant=True,
                position="after_char",
                insertion_order=20,
            ),
        ],
    )
    write_character_card(
        repo_root,
        "test",
        character_card_document(
            data=character_card_data(
                description="",
                personality="",
                scenario="",
                system_prompt="core",
                mes_example="",
                post_history_instructions="post",
                character_book=book,
            )
        ),
    )


def test_loader_to_shared_http_and_websocket_runtime_uses_same_lore_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_lore_card(tmp_path)
    use_character_repo_root(monkeypatch, tmp_path)
    load_calls: list[str] = []

    def load_definition(character: str) -> _chat_runtime.CharacterRuntimeDefinition:
        load_calls.append(character)
        card = load_character_card(character)
        return _chat_runtime.CharacterRuntimeDefinition(
            prompt=card.to_character_prompt(),
            character_book=card.data.character_book,
        )

    prompts: list[BuiltPrompt] = []

    def generate(prompt: BuiltPrompt, *, max_output_tokens: int) -> str:
        assert max_output_tokens > 0
        prompts.append(prompt)
        return "reply"

    history_service = _HistoryService()
    submitter = MagicMock()
    service = _chat_runtime.ChatService(
        _chat_runtime.ChatRuntimeConfig(
            rag_enabled=False,
            memory_policy=None,
            prompt_config=resolve_model_settings({}),
            chroma_path=tmp_path / "chroma",
        ),
        cast(HistoryService, history_service),
        _chat_runtime.ChatRuntimeDependencies(
            character_definition_loader=load_definition,
            prompt_builder=build_chat_prompt,
            llm_response_generator=generate,
            input_token_counter=lambda messages: len(messages),
            privacy_scanner=MagicMock(),
            semantic_classifier=MagicMock(),
            approved_memory_repository=MagicMock(),
            memory_formation_submitter=submitter,
        ),
    )

    http_reply = service.generate_chat_reply("test", CONVERSATION_ID, "月の話")
    websocket_reply, _ = service._generate_chat_reply(
        "test",
        "月の話",
        cast(
            HistorySession,
            history_service.open_session("test", CONVERSATION_ID),
        ),
    )

    assert isinstance(http_reply.persisted_turn, chat_service.PersistedContentTurn)
    assert isinstance(
        websocket_reply.persisted_turn,
        chat_service.PersistedContentTurn,
    )
    assert http_reply.persisted_turn.assistant_content == "reply"
    assert websocket_reply.persisted_turn.assistant_content == "reply"
    assert load_calls == ["test", "test"]
    assert len(prompts) == 2
    expected_contents = [
        "## キャラクターLore\nbefore-lore",
        "## 応答方針\ncore",
        "## キャラクターLore\nafter-lore",
        "post",
        "月の話",
    ]
    assert [message.content for message in prompts[0].messages] == expected_contents
    assert prompts[0] == prompts[1]
    assert prompts[0].usage.character_lore == 2
    submitter.submit.assert_called()
