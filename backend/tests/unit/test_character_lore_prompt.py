import logging
from collections.abc import Iterator
from typing import cast

import pytest

from app import chat_prompt
from app.characters.lore_selector import (
    CharacterLoreSelection,
    LoreActivationKind,
    LoreSelectionDecision,
    LoreSelectionReason,
    SelectedCharacterLore,
)
from app.characters.models import (
    CharacterBook,
    CharacterBookEntry,
    CharacterLorePosition,
)
from app.conversation_history.prompt_history import RestoredHistoryTurn
from app.conversation_history.service import HistorySession
from app.prompting import (
    BuiltPrompt,
    CharacterPrompt,
    CurrentUserMessage,
    MaskedHistory,
    MaskedHistoryTurn,
    PromptBuilder,
    PromptRole,
    RagContext,
    RagItem,
)
from app.model_settings import resolve_model_settings
from tests.prompt_test_support import (
    UnitTokenCounter,
    prompt_build_input,
    token_budget,
)


def _selected_lore(
    content: str,
    *,
    source_index: int,
    position: CharacterLorePosition = CharacterLorePosition.AFTER_CHAR,
    insertion_order: int = 10,
    priority: int = 10,
) -> SelectedCharacterLore:
    return SelectedCharacterLore(
        source_index=source_index,
        content=content,
        position=position,
        insertion_order=insertion_order,
        effective_priority=priority,
        activation_kind=LoreActivationKind.PRIMARY,
    )


def _selection(
    *entries: SelectedCharacterLore,
    omitted_by_book_budget: int = 0,
) -> CharacterLoreSelection:
    selected_indices = frozenset(entry.source_index for entry in entries)
    omitted_indices = tuple(
        index
        for index in range(len(entries) + omitted_by_book_budget)
        if index not in selected_indices
    )[:omitted_by_book_budget]
    decisions = tuple(
        sorted(
            (
                *(
                    LoreSelectionDecision(
                        entry.source_index,
                        LoreSelectionReason.SELECTED_PRIMARY,
                    )
                    for entry in entries
                ),
                *(
                    LoreSelectionDecision(
                        index,
                        LoreSelectionReason.OMITTED_BY_LORE_BUDGET,
                    )
                    for index in omitted_indices
                ),
            ),
            key=lambda decision: decision.source_index,
        )
    )
    return CharacterLoreSelection(entries, decisions, None, False)


def _decision_reasons(result: BuiltPrompt) -> dict[int, LoreSelectionReason]:
    return {
        decision.source_index: decision.reason
        for decision in result.character_lore_decisions
    }


def test_prompt_composes_lore_around_character_before_rag_and_history() -> None:
    lore = _selection(
        _selected_lore(
            "before-earlier",
            source_index=1,
            position=CharacterLorePosition.BEFORE_CHAR,
            insertion_order=10,
        ),
        _selected_lore(
            "before-later",
            source_index=0,
            position=CharacterLorePosition.BEFORE_CHAR,
            insertion_order=20,
        ),
        _selected_lore("after", source_index=2),
    )
    prompt_input = prompt_build_input(character_lore=lore)

    result = PromptBuilder(UnitTokenCounter()).build(prompt_input)

    assert [(message.role, message.content) for message in result.messages] == [
        (PromptRole.SYSTEM, "## キャラクターLore\nbefore-earlier"),
        (PromptRole.SYSTEM, "## キャラクターLore\nbefore-later"),
        (
            PromptRole.SYSTEM,
            "## キャラクター概要\n概要\n\n"
            "## 性格と話し方\n性格\n\n"
            "## 関係と世界観\n関係\n\n"
            "## 応答方針\nシステム指示\n\n"
            "## 会話例\n会話例",
        ),
        (PromptRole.SYSTEM, "## キャラクターLore\nafter"),
        (PromptRole.SYSTEM, "## 関連する記憶\nRAG本文"),
        (PromptRole.USER, "過去user"),
        (PromptRole.ASSISTANT, "過去assistant"),
        (PromptRole.SYSTEM, "最終指示"),
        (PromptRole.USER, "現在user原文"),
    ]
    assert result.usage.character_lore == 3
    assert result.usage.omitted_character_lore_entries == 0
    assert set(_decision_reasons(result).values()) == {
        LoreSelectionReason.SELECTED_PRIMARY
    }


def test_character_lore_individual_budget_uses_removal_key_without_truncation() -> None:
    lore = _selection(
        _selected_lore("low", source_index=0, priority=1),
        _selected_lore("high-a", source_index=1, priority=2),
        _selected_lore("high-b", source_index=2, priority=3),
        omitted_by_book_budget=1,
    )
    prompt_input = prompt_build_input(
        character_lore=lore,
        rag=RagContext(items=()),
        history=MaskedHistory((), 0),
        budget=token_budget(total=20, character_lore=2),
    )

    result = PromptBuilder(UnitTokenCounter()).build(prompt_input)

    lore_messages = [
        message.content
        for message in result.messages
        if message.content.startswith("## キャラクターLore")
    ]
    assert lore_messages == [
        "## キャラクターLore\nhigh-a",
        "## キャラクターLore\nhigh-b",
    ]
    assert all("low" not in message for message in lore_messages)
    assert result.usage.character_lore == 2
    assert result.usage.omitted_character_lore_entries == 2
    assert _decision_reasons(result) == {
        0: LoreSelectionReason.OMITTED_BY_LORE_BUDGET,
        1: LoreSelectionReason.SELECTED_PRIMARY,
        2: LoreSelectionReason.SELECTED_PRIMARY,
        3: LoreSelectionReason.OMITTED_BY_LORE_BUDGET,
    }


def test_final_lore_diagnostics_distinguish_lore_and_total_budget() -> None:
    lore = _selection(
        _selected_lore("TOTAL_BUDGET_SECRET_LOW", source_index=1, priority=1),
        _selected_lore("kept", source_index=2, priority=2),
        omitted_by_book_budget=1,
    )
    prompt_input = prompt_build_input(
        character=CharacterPrompt("", "", "", "required-core", "", ""),
        character_lore=lore,
        rag=RagContext(items=()),
        history=MaskedHistory((), 0),
        budget=token_budget(
            total=3,
            character=20,
            character_lore=20,
            rag=20,
            history=20,
            current_user=20,
            post_history=20,
        ),
    )

    result = PromptBuilder(UnitTokenCounter()).build(prompt_input)

    assert _decision_reasons(result) == {
        0: LoreSelectionReason.OMITTED_BY_LORE_BUDGET,
        1: LoreSelectionReason.OMITTED_BY_TOTAL_BUDGET,
        2: LoreSelectionReason.SELECTED_PRIMARY,
    }
    assert result.usage.omitted_character_lore_entries == 2
    assert "TOTAL_BUDGET_SECRET_LOW" not in repr(
        result.character_lore_decisions
    )


@pytest.mark.parametrize(
    (
        "total_limit",
        "expected_rag",
        "expected_old_history",
        "expected_lore",
        "expected_post_history",
    ),
    [
        (10, 1, 1, 2, 1),
        (7, 0, 0, 2, 1),
        (6, 0, 0, 1, 1),
        (4, 0, 0, 0, 0),
    ],
)
def test_total_budget_removes_rag_then_old_history_then_lore_then_post_history(
    total_limit: int,
    expected_rag: int,
    expected_old_history: int,
    expected_lore: int,
    expected_post_history: int,
) -> None:
    lore = _selection(
        _selected_lore("lore-low", source_index=0, priority=1),
        _selected_lore("lore-high", source_index=1, priority=2),
    )
    history = MaskedHistory(
        (
            MaskedHistoryTurn("old-user", "old-assistant", True),
            MaskedHistoryTurn("latest-user", "latest-assistant", True),
        ),
        0,
    )
    prompt_input = prompt_build_input(
        character=CharacterPrompt("", "", "", "required-core", "", "post"),
        character_lore=lore,
        rag=RagContext(
            items=(
                RagItem("rag-1", raw_distance=1.0),
                RagItem("rag-2", raw_distance=2.0),
            )
        ),
        history=history,
        budget=token_budget(
            total=total_limit,
            character=20,
            character_lore=20,
            rag=20,
            history=20,
            current_user=20,
            post_history=20,
        ),
    )

    result = PromptBuilder(UnitTokenCounter()).build(prompt_input)
    contents = tuple(message.content for message in result.messages)

    assert sum(content.startswith("## 関連する記憶") for content in contents) == (
        expected_rag
    )
    assert sum(content == "old-user" for content in contents) == expected_old_history
    assert sum(content.startswith("## キャラクターLore") for content in contents) == (
        expected_lore
    )
    assert sum(content == "post" for content in contents) == expected_post_history
    assert "latest-user" in contents
    assert "latest-assistant" in contents
    assert result.usage.total <= total_limit
    reasons = _decision_reasons(result)
    expected_reasons = {
        0: (
            LoreSelectionReason.SELECTED_PRIMARY
            if expected_lore == 2
            else LoreSelectionReason.OMITTED_BY_TOTAL_BUDGET
        ),
        1: (
            LoreSelectionReason.OMITTED_BY_TOTAL_BUDGET
            if expected_lore == 0
            else LoreSelectionReason.SELECTED_PRIMARY
        ),
    }
    assert reasons == expected_reasons
    assert result.usage.omitted_character_lore_entries == 2 - expected_lore


def test_prompt_log_contains_only_lore_counts_not_lore_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    prompt_input = prompt_build_input(
        character_lore=_selection(
            _selected_lore("SECRET_LORE_BODY", source_index=0)
        )
    )

    with caplog.at_level(logging.DEBUG, logger="app.prompting.builder"):
        PromptBuilder(UnitTokenCounter()).build(prompt_input)

    assert "omitted_lore=0" in caplog.text
    assert "SECRET_LORE_BODY" not in caplog.text


class _RecordingHistorySession:
    def __init__(self, turns: tuple[RestoredHistoryTurn, ...]) -> None:
        self._turns = turns
        self.calls = 0

    def prompt_turns(
        self,
        *,
        max_completed_turns: int,
        page_size: int,
    ) -> Iterator[RestoredHistoryTurn]:
        del max_completed_turns, page_size
        self.calls += 1
        return iter(self._turns)


def _book_for_history_match() -> CharacterBook:
    return CharacterBook(
        name=None,
        description=None,
        scan_depth=5,
        token_budget=None,
        recursive_scanning=None,
        extensions={},
        entries=(
            CharacterBookEntry(
                keys=("OLDER_MASKED_ASSISTANT",),
                content="matched-from-sanitized-history",
                extensions={},
                enabled=True,
                insertion_order=10,
                use_regex=False,
                case_sensitive=None,
                constant=None,
                name=None,
                priority=None,
                id=None,
                comment=None,
                selective=None,
                secondary_keys=None,
                position=None,
                extra_fields={},
            ),
        ),
        extra_fields={},
    )


def test_chat_prompt_matches_before_history_budget_using_same_sanitized_source() -> None:
    history_session = _RecordingHistorySession(
        (
            RestoredHistoryTurn(
                user_content="MASKED_USER",
                assistant_content="MASKED_ASSISTANT",
                is_completed=True,
            ),
            RestoredHistoryTurn(
                user_content="OLDER_MASKED_USER",
                assistant_content="OLDER_MASKED_ASSISTANT",
                is_completed=True,
            ),
        )
    )
    config = resolve_model_settings(
        {
            "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": "2",
            "CONVERSATION_HISTORY_TOKEN_LIMIT": "2",
            "USER_INPUT_TOKEN_LIMIT": "10",
            "OLLAMA_RESPONSE_RESERVE_TOKENS": "1",
            "OLLAMA_CONTEXT_TOKENS": "20",
        }
    )

    result = chat_prompt.build_chat_prompt(
        character=CharacterPrompt("", "", "", "core", "", ""),
        character_book=_book_for_history_match(),
        rag=RagContext(items=()),
        current_user=CurrentUserMessage("RAW_CURRENT"),
        history_session=cast(HistorySession, history_session),
        config=config,
        token_counter=UnitTokenCounter(),
    )

    assert history_session.calls == 1
    assert "## キャラクターLore\nmatched-from-sanitized-history" in (
        message.content for message in result.messages
    )
    assert result.usage.character_lore == 1
    assert result.usage.history == 2
    assert result.usage.omitted_history_exchanges == 1


def test_chat_prompt_does_not_scan_history_at_default_depth() -> None:
    history_session = _RecordingHistorySession(
        (
            RestoredHistoryTurn(
                user_content="MASKED_USER",
                assistant_content="MASKED_ASSISTANT",
                is_completed=True,
            ),
        )
    )
    book = _book_for_history_match()
    book = CharacterBook(
        name=book.name,
        description=book.description,
        scan_depth=None,
        token_budget=book.token_budget,
        recursive_scanning=book.recursive_scanning,
        extensions=book.extensions,
        entries=book.entries,
        extra_fields=book.extra_fields,
    )

    result = chat_prompt.build_chat_prompt(
        character=CharacterPrompt("", "", "", "core", "", ""),
        character_book=book,
        rag=RagContext(items=()),
        current_user=CurrentUserMessage("RAW_CURRENT"),
        history_session=cast(HistorySession, history_session),
        config=resolve_model_settings({}),
        token_counter=UnitTokenCounter(),
    )

    assert all("キャラクターLore" not in message.content for message in result.messages)
    assert history_session.calls == 1


def test_chat_prompt_with_empty_book_does_not_add_lore_messages() -> None:
    history_session = _RecordingHistorySession(())
    empty_book = CharacterBook(
        name=None,
        description=None,
        scan_depth=None,
        token_budget=None,
        recursive_scanning=None,
        extensions={},
        entries=(),
        extra_fields={},
    )

    result = chat_prompt.build_chat_prompt(
        character=CharacterPrompt("", "", "", "core", "", ""),
        character_book=empty_book,
        rag=RagContext(items=()),
        current_user=CurrentUserMessage("RAW_CURRENT"),
        history_session=cast(HistorySession, history_session),
        config=resolve_model_settings({}),
        token_counter=UnitTokenCounter(),
    )

    assert [(message.role, message.content) for message in result.messages] == [
        (PromptRole.SYSTEM, "## 応答方針\ncore"),
        (PromptRole.USER, "RAW_CURRENT"),
    ]
    assert result.usage.character_lore == 0
    assert result.usage.omitted_character_lore_entries == 0
    assert result.character_lore_decisions == ()
