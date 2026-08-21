import importlib
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

from app.prompting import (
    CharacterPrompt,
    CurrentUserMessage,
    PromptRole,
    RagContext,
)
from app.chat_prompt import build_chat_prompt
from app.chat_service import PersistedContentTurn
from tests.prompt_test_support import prompt_build_input, prompt_builder
from app.conversation_history.models import ProcessingTurnInput
from app.conversation_history.service import ConversationHistorySession
from app.privacy.contracts import (
    ConversationHistoryAction,
    ConversationHistoryDecision,
    HistoryDecisionReasonCode,
)
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    SequenceUuidFactory,
    create_repository,
)


class UnitMessageCounter:
    def count_input_tokens(self, messages) -> int:
        return len(messages)


class WeightedMessageCounter:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def count_input_tokens(self, messages) -> int:
        return sum(self._counts.get(message.content, 1) for message in messages)


def _complete_turn(
    repository,
    character_id: str,
    conversation_id: UUID,
    user_content: str,
    assistant_content: str,
) -> None:
    turn = repository.create_processing_turn(
        character_id,
        conversation_id,
        ProcessingTurnInput(sanitized_user_content=user_content),
    )
    repository.complete_turn(
        character_id,
        conversation_id,
        turn.turn_id,
        sanitized_assistant_content=assistant_content,
    )


def test_should_place_current_user_last_exactly_once_with_existing_builder_entrypoint() -> None:
    prompt_input = prompt_build_input()

    result = prompt_builder().build(prompt_input)

    assert result.messages[-1].role is PromptRole.USER
    assert result.messages[-1].content == "現在user原文"
    assert sum(
        message.content == "現在user原文" for message in result.messages
    ) == 1


def test_should_build_system_rag_saved_history_then_current_user() -> None:
    prompting = importlib.import_module("app.prompting")
    turn_type = getattr(prompting, "MaskedHistoryTurn")
    history = prompting.MaskedHistory(
        turns=(
            turn_type("MASKED_OLD_USER", "MASKED_OLD_ASSISTANT", True),
            turn_type("MASKED_FAILED_USER", None, False),
        ),
        omitted_turns=0,
    )
    prompt_input = prompting.PromptBuildInput(
        character=CharacterPrompt(
            description="character",
            personality="",
            scenario="",
            system_prompt="system",
            mes_example="",
            post_history_instructions="final instruction",
        ),
        rag=RagContext(
            items=(prompting.RagItem("RAG_CONTEXT", raw_distance=1.25),)
        ),
        history=prompting.HistoryCandidates(
            newest_first_factory=lambda: reversed(history.turns),
            omitted_turns=history.omitted_turns,
        ),
        current_user=CurrentUserMessage("RAW_CURRENT_USER"),
        budget=prompting.TokenBudget(
            total=20,
            character=10,
            rag=10,
            history=10,
            current_user=10,
            post_history=10,
        ),
    )

    result = prompting.PromptBuilder(UnitMessageCounter()).build(prompt_input)

    assert [(message.role, message.content) for message in result.messages] == [
        (
            PromptRole.SYSTEM,
            "## キャラクター概要\ncharacter\n\n## 応答方針\nsystem",
        ),
        (PromptRole.SYSTEM, "## 関連する記憶\nRAG_CONTEXT"),
        (PromptRole.USER, "MASKED_OLD_USER"),
        (PromptRole.ASSISTANT, "MASKED_OLD_ASSISTANT"),
        (PromptRole.USER, "MASKED_FAILED_USER"),
        (PromptRole.SYSTEM, "final instruction"),
        (PromptRole.USER, "RAW_CURRENT_USER"),
    ]
    assert sum(
        message.content == "RAW_CURRENT_USER" for message in result.messages
    ) == 1


def test_should_keep_history_when_rag_context_is_empty() -> None:
    prompting = importlib.import_module("app.prompting")
    turn_type = getattr(prompting, "MaskedHistoryTurn")
    history = prompting.MaskedHistory(
        turns=(turn_type("MASKED_USER", "MASKED_ASSISTANT", True),),
        omitted_turns=0,
    )
    prompt_input = prompting.PromptBuildInput(
        character=CharacterPrompt("", "", "", "system", "", ""),
        rag=RagContext(items=()),
        history=prompting.HistoryCandidates(
            newest_first_factory=lambda: reversed(history.turns),
            omitted_turns=history.omitted_turns,
        ),
        current_user=CurrentUserMessage("current"),
        budget=prompting.TokenBudget(20, 10, 10, 10, 10, 10),
    )

    result = prompting.PromptBuilder(UnitMessageCounter()).build(prompt_input)

    assert [message.content for message in result.messages[-3:]] == [
        "MASKED_USER",
        "MASKED_ASSISTANT",
        "current",
    ]


def test_sqlite_pages_restore_select_and_reach_existing_builder(
    tmp_path: Path,
) -> None:
    turn_ids = (
        UUID("9e70795d-e5d5-431d-baa2-67f884403061"),
        UUID("9e70795d-e5d5-431d-baa2-67f884403062"),
        UUID("9e70795d-e5d5-431d-baa2-67f884403063"),
    )
    repository = create_repository(
        tmp_path / "history.db",
        uuid_factory=SequenceUuidFactory(CONVERSATION_ID, *turn_ids),
    )
    repository.create_conversation("miori")
    oldest = repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="MASKED_OLD_USER"),
    )
    repository.complete_turn(
        "miori",
        CONVERSATION_ID,
        oldest.turn_id,
        sanitized_assistant_content="MASKED_OLD_ASSISTANT",
    )
    failed = repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="MASKED_FAILED_USER"),
    )
    repository.fail_turn("miori", CONVERSATION_ID, failed.turn_id)
    newest = repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="MASKED_NEW_USER"),
    )
    repository.complete_turn(
        "miori",
        CONVERSATION_ID,
        newest.turn_id,
        sanitized_assistant_content="MASKED_NEW_ASSISTANT",
    )
    session = ConversationHistorySession(
        "miori",
        CONVERSATION_ID,
        repository,
        MagicMock(),
    )
    prompting = importlib.import_module("app.prompting")
    prompt_input = prompting.PromptBuildInput(
        character=CharacterPrompt("", "", "", "system", "", ""),
        rag=RagContext(items=()),
        history=prompting.HistoryCandidates(
            newest_first_factory=lambda: session.prompt_turns(
                max_completed_turns=2,
                page_size=1,
            ),
            omitted_turns=0,
        ),
        current_user=CurrentUserMessage("RAW_CURRENT_USER"),
        budget=prompting.TokenBudget(20, 10, 10, 10, 10, 10),
    )

    result = prompting.PromptBuilder(UnitMessageCounter()).build(prompt_input)

    assert [message.content for message in result.messages[-6:]] == [
        "MASKED_OLD_USER",
        "MASKED_OLD_ASSISTANT",
        "MASKED_FAILED_USER",
        "MASKED_NEW_USER",
        "MASKED_NEW_ASSISTANT",
        "RAW_CURRENT_USER",
    ]


def test_sqlite_flow_should_trim_only_oldest_optional_turn(
    tmp_path: Path,
) -> None:
    turn_ids = (
        UUID("9e70795d-e5d5-431d-baa2-67f884403071"),
        UUID("9e70795d-e5d5-431d-baa2-67f884403072"),
        UUID("9e70795d-e5d5-431d-baa2-67f884403073"),
    )
    repository = create_repository(
        tmp_path / "trimmed-history.db",
        uuid_factory=SequenceUuidFactory(CONVERSATION_ID, *turn_ids),
    )
    repository.create_conversation("miori")
    completed = repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="MASKED_COMPLETED_USER"),
    )
    repository.complete_turn(
        "miori",
        CONVERSATION_ID,
        completed.turn_id,
        sanitized_assistant_content="MASKED_COMPLETED_ASSISTANT",
    )
    old_failed = repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="MASKED_OLD_FAILED"),
    )
    repository.fail_turn("miori", CONVERSATION_ID, old_failed.turn_id)
    new_failed = repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="MASKED_NEW_FAILED"),
    )
    repository.fail_turn("miori", CONVERSATION_ID, new_failed.turn_id)
    session = ConversationHistorySession(
        "miori",
        CONVERSATION_ID,
        repository,
        MagicMock(),
    )
    restored_newest_first = session.prompt_turns(
        max_completed_turns=1,
        page_size=1,
    )
    counter = WeightedMessageCounter(
        {
            "MASKED_NEW_FAILED": 1,
            "MASKED_OLD_FAILED": 3,
            "MASKED_COMPLETED_USER": 1,
            "MASKED_COMPLETED_ASSISTANT": 1,
        }
    )
    selection = importlib.import_module("app.prompting.history")

    history = selection.select_history(
        restored_newest_first,
        token_counter=counter,
        token_limit=5,
    )
    result = prompt_builder().build(
        prompt_build_input(
            character=CharacterPrompt("", "", "", "system", "", ""),
            history=history,
            current_user=CurrentUserMessage("RAW_CURRENT_USER"),
        )
    )

    assert [turn.user_content for turn in history.turns] == [
        "MASKED_COMPLETED_USER",
        "MASKED_NEW_FAILED",
    ]
    assert history.omitted_turns == 1
    assert [message.content for message in result.messages[-4:]] == [
        "MASKED_COMPLETED_USER",
        "MASKED_COMPLETED_ASSISTANT",
        "MASKED_NEW_FAILED",
        "RAW_CURRENT_USER",
    ]


class _RuntimeHistoryService:
    def __init__(self, session: ConversationHistorySession) -> None:
        self._session = session

    def open_session(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> ConversationHistorySession:
        return self._session


def test_runtime_should_inject_history_when_rag_is_disabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app import _chat_runtime

    runtime_turn_id = UUID("9e70795d-e5d5-431d-baa2-67f884403091")
    current_turn_id = UUID("9e70795d-e5d5-431d-baa2-67f884403092")
    repository = create_repository(
        tmp_path / "rag-disabled-runtime-history.db",
        uuid_factory=SequenceUuidFactory(
            CONVERSATION_ID,
            runtime_turn_id,
            current_turn_id,
        ),
    )
    repository.create_conversation("miori")
    _complete_turn(
        repository,
        "miori",
        CONVERSATION_ID,
        "MASKED_RUNTIME_USER",
        "MASKED_RUNTIME_ASSISTANT",
    )
    sanitizer = MagicMock()
    sanitizer.sanitize_current_user.return_value = ConversationHistoryDecision(
        action=ConversationHistoryAction.STORE_MASKED,
        reason_code=HistoryDecisionReasonCode.MASKED,
        sanitizer_version="test-sanitizer-v1",
        policy_version="test-policy-v1",
        content="MASKED_RUNTIME_CURRENT",
    )
    sanitizer.sanitize_assistant.return_value = ConversationHistoryDecision(
        action=ConversationHistoryAction.STORE_MASKED,
        reason_code=HistoryDecisionReasonCode.MASKED,
        sanitizer_version="test-sanitizer-v1",
        policy_version="test-policy-v1",
        content="MASKED_RUNTIME_REPLY",
    )
    session = ConversationHistorySession(
        "miori", CONVERSATION_ID, repository, sanitizer
    )
    prompt_config = importlib.import_module(
        "app.model_settings"
    ).resolve_model_settings({})
    card = MagicMock()
    card.to_character_prompt.return_value = CharacterPrompt(
        "", "", "", "system", "", ""
    )
    retrieve = MagicMock()
    monkeypatch.setattr(
        _chat_runtime._rag_service,
        "retrieve_prompt_memories",
        retrieve,
    )
    captured: dict[str, object] = {}

    def generate(prompt, *, max_output_tokens: int) -> str:
        captured["prompt"] = prompt
        captured["max_output_tokens"] = max_output_tokens
        return "reply"

    service = _chat_runtime.ChatService(
        _chat_runtime.ChatRuntimeConfig(
            rag_enabled=False,
            memory_policy=None,
            prompt_config=prompt_config,
            chroma_path=Path("/test/runtime-data/chroma"),
        ),
        _RuntimeHistoryService(session),
        _chat_runtime.ChatRuntimeDependencies(
            character_prompt_loader=lambda character: card.to_character_prompt(),
            prompt_builder=build_chat_prompt,
            llm_response_generator=generate,
            input_token_counter=lambda messages: len(messages),
            privacy_scanner=MagicMock(),
            semantic_classifier=MagicMock(),
            approved_memory_repository=MagicMock(),
            memory_formation_submitter=MagicMock(),
        ),
    )

    result = service.generate_chat_reply(
        "miori",
        CONVERSATION_ID,
        "RAW_RUNTIME_CURRENT",
    )

    assert isinstance(result.persisted_turn, PersistedContentTurn)
    assert result.persisted_turn.assistant_content == "MASKED_RUNTIME_REPLY"
    retrieve.assert_not_called()
    prompt = captured["prompt"]
    assert [message.content for message in prompt.messages[-3:]] == [
        "MASKED_RUNTIME_USER",
        "MASKED_RUNTIME_ASSISTANT",
        "RAW_RUNTIME_CURRENT",
    ]
    assert captured["max_output_tokens"] == prompt_config.assistant_max_generation_tokens
