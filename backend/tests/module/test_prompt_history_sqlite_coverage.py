import sqlite3
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

from app.conversation_history.models import ProcessingTurnInput
from app.conversation_history.service import ConversationHistorySession
from app.prompting import CharacterPrompt, CurrentUserMessage, RagContext
from app.prompting.history import select_history
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    SequenceUuidFactory,
    create_repository,
)
from tests.prompt_test_support import (
    UnitTokenCounter,
    prompt_build_input,
    prompt_builder,
)


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


def _build_sqlite_history_prompt(
    session: ConversationHistorySession,
    *,
    max_completed_turns: int,
    page_size: int,
):
    history = select_history(
        session.prompt_turns(
            max_completed_turns=max_completed_turns,
            page_size=page_size,
        ),
        token_counter=UnitTokenCounter(),
        token_limit=100,
    )
    return prompt_builder().build(
        prompt_build_input(
            character=CharacterPrompt("", "", "", "system", "", ""),
            rag=RagContext(items=()),
            history=history,
            current_user=CurrentUserMessage("RAW_CURRENT_USER"),
        )
    )


def test_sqlite_builder_flow_should_separate_character_history(
    tmp_path: Path,
) -> None:
    target_turn_id = UUID("9e70795d-e5d5-431d-baa2-67f884403081")
    other_turn_id = UUID("9e70795d-e5d5-431d-baa2-67f884403082")
    repository = create_repository(
        tmp_path / "character-separated-history.db",
        uuid_factory=SequenceUuidFactory(
            CONVERSATION_ID,
            target_turn_id,
            CONVERSATION_ID,
            other_turn_id,
        ),
    )
    repository.create_conversation("miori")
    _complete_turn(
        repository,
        "miori",
        CONVERSATION_ID,
        "MASKED_TARGET_CHARACTER_USER",
        "MASKED_TARGET_CHARACTER_ASSISTANT",
    )
    repository.create_conversation("other")
    _complete_turn(
        repository,
        "other",
        CONVERSATION_ID,
        "MASKED_OTHER_CHARACTER_USER",
        "MASKED_OTHER_CHARACTER_ASSISTANT",
    )
    session = ConversationHistorySession(
        "miori", CONVERSATION_ID, repository, MagicMock()
    )

    result = _build_sqlite_history_prompt(
        session,
        max_completed_turns=10,
        page_size=1,
    )

    contents = [message.content for message in result.messages]
    assert "MASKED_TARGET_CHARACTER_USER" in contents
    assert "MASKED_TARGET_CHARACTER_ASSISTANT" in contents
    assert "MASKED_OTHER_CHARACTER_USER" not in contents
    assert "MASKED_OTHER_CHARACTER_ASSISTANT" not in contents


def test_sqlite_builder_flow_should_separate_conversation_history(
    tmp_path: Path,
) -> None:
    other_conversation_id = UUID("e98d6c65-1ae9-4d6f-a8c8-d59b0ad09012")
    target_turn_id = UUID("9e70795d-e5d5-431d-baa2-67f884403083")
    other_turn_id = UUID("9e70795d-e5d5-431d-baa2-67f884403084")
    repository = create_repository(
        tmp_path / "conversation-separated-history.db",
        uuid_factory=SequenceUuidFactory(
            CONVERSATION_ID,
            target_turn_id,
            other_conversation_id,
            other_turn_id,
        ),
    )
    repository.create_conversation("miori")
    _complete_turn(
        repository,
        "miori",
        CONVERSATION_ID,
        "MASKED_TARGET_CONVERSATION_USER",
        "MASKED_TARGET_CONVERSATION_ASSISTANT",
    )
    repository.create_conversation("miori")
    _complete_turn(
        repository,
        "miori",
        other_conversation_id,
        "MASKED_OTHER_CONVERSATION_USER",
        "MASKED_OTHER_CONVERSATION_ASSISTANT",
    )
    session = ConversationHistorySession(
        "miori", CONVERSATION_ID, repository, MagicMock()
    )

    result = _build_sqlite_history_prompt(
        session,
        max_completed_turns=10,
        page_size=1,
    )

    contents = [message.content for message in result.messages]
    assert "MASKED_TARGET_CONVERSATION_USER" in contents
    assert "MASKED_TARGET_CONVERSATION_ASSISTANT" in contents
    assert "MASKED_OTHER_CONVERSATION_USER" not in contents
    assert "MASKED_OTHER_CONVERSATION_ASSISTANT" not in contents


def test_sqlite_builder_flow_should_stop_querying_at_completed_boundary(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "completed-boundary-history.db"
    turn_ids = tuple(
        UUID(f"9e70795d-e5d5-431d-baa2-67f884403{index:03d}")
        for index in range(100, 133)
    )
    prompt_queries: list[str] = []

    def traced_connection(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path)

        def trace(statement: str) -> None:
            normalized = " ".join(statement.upper().split())
            if (
                normalized.startswith("SELECT TURN_ID")
                and "FROM CONVERSATION_TURNS" in normalized
                and "WHERE CHARACTER_ID" in normalized
            ):
                prompt_queries.append(statement)

        connection.set_trace_callback(trace)
        return connection

    repository = create_repository(
        database_path,
        uuid_factory=SequenceUuidFactory(CONVERSATION_ID, *turn_ids),
        connection_factory=traced_connection,
    )
    repository.create_conversation("miori")
    _complete_turn(
        repository,
        "miori",
        CONVERSATION_ID,
        "MASKED_OLDER_THAN_BOUNDARY_USER",
        "MASKED_OLDER_THAN_BOUNDARY_ASSISTANT",
    )
    _complete_turn(
        repository,
        "miori",
        CONVERSATION_ID,
        "MASKED_COMPLETED_BOUNDARY_USER",
        "MASKED_COMPLETED_BOUNDARY_ASSISTANT",
    )
    for index in range(2, len(turn_ids)):
        turn = repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content=f"MASKED_FAILED_{index}"),
        )
        repository.fail_turn("miori", CONVERSATION_ID, turn.turn_id)
    prompt_queries.clear()
    session = ConversationHistorySession(
        "miori", CONVERSATION_ID, repository, MagicMock()
    )

    result = _build_sqlite_history_prompt(
        session,
        max_completed_turns=1,
        page_size=32,
    )

    contents = [message.content for message in result.messages]
    assert "MASKED_COMPLETED_BOUNDARY_USER" in contents
    assert "MASKED_OLDER_THAN_BOUNDARY_USER" not in contents
    assert len(prompt_queries) == 1
