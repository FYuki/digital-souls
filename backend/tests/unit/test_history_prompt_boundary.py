from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

from app.conversation_history.models import (
    PrivacySkipReason,
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
)
from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.service import (
    CompletedHistoryExchange,
    ConversationHistorySession,
)
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    SequenceUuidFactory,
    create_repository,
)


TURN_IDS = (
    UUID("9e70795d-e5d5-431d-baa2-67f884403001"),
    UUID("9e70795d-e5d5-431d-baa2-67f884403002"),
    UUID("9e70795d-e5d5-431d-baa2-67f884403003"),
    UUID("9e70795d-e5d5-431d-baa2-67f884403004"),
)


def _history_session(
    database_path: Path,
) -> tuple[ConversationHistoryRepository, ConversationHistorySession]:
    repository = create_repository(
        database_path,
        uuid_factory=SequenceUuidFactory(CONVERSATION_ID, *TURN_IDS),
    )
    conversation = repository.create_conversation("miori")
    session = ConversationHistorySession(
        character_id="miori",
        conversation_id=conversation.conversation_id,
        repository=repository,
        sanitizer=MagicMock(),
    )
    return repository, session


class TestCompletedExchanges:
    def test_should_not_expose_masked_bodies_in_repr(self) -> None:
        exchange = CompletedHistoryExchange(
            user_content="MASKED_USER_REPR_SECRET",
            assistant_content="MASKED_ASSISTANT_REPR_SECRET",
        )

        representation = repr(exchange)

        assert "MASKED_USER_REPR_SECRET" not in representation
        assert "MASKED_ASSISTANT_REPR_SECRET" not in representation

    def test_should_return_only_complete_masked_user_assistant_exchanges(
        self,
        tmp_path: Path,
    ) -> None:
        repository, session = _history_session(tmp_path / "history.db")
        first = repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="MASKED_USER_1"),
        )
        repository.complete_turn(
            "miori",
            CONVERSATION_ID,
            first.turn_id,
            sanitized_assistant_content="MASKED_ASSISTANT_1",
        )
        repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="CURRENT_PROCESSING"),
        )
        repository.create_privacy_skipped_turn(
            "miori",
            CONVERSATION_ID,
            PrivacySkippedTurnInput(
                reason_code=PrivacySkipReason.SENSITIVE_CONTENT,
            ),
        )
        failed = repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="FAILED_USER"),
        )
        repository.fail_turn("miori", CONVERSATION_ID, failed.turn_id)

        exchanges = session.completed_exchanges()

        assert len(exchanges) == 1
        assert exchanges[0].user_content == "MASKED_USER_1"
        assert exchanges[0].assistant_content == "MASKED_ASSISTANT_1"

    def test_should_keep_completed_exchanges_in_storage_order(
        self,
        tmp_path: Path,
    ) -> None:
        repository, session = _history_session(tmp_path / "history.db")
        for index in (1, 2):
            turn = repository.create_processing_turn(
                "miori",
                CONVERSATION_ID,
                ProcessingTurnInput(
                    sanitized_user_content=f"MASKED_USER_{index}",
                ),
            )
            repository.complete_turn(
                "miori",
                CONVERSATION_ID,
                turn.turn_id,
                sanitized_assistant_content=f"MASKED_ASSISTANT_{index}",
            )

        exchanges = session.completed_exchanges()

        assert [exchange.user_content for exchange in exchanges] == [
            "MASKED_USER_1",
            "MASKED_USER_2",
        ]

    def test_should_return_empty_tuple_when_no_exchange_is_complete(
        self,
        tmp_path: Path,
    ) -> None:
        repository, session = _history_session(tmp_path / "history.db")
        repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="CURRENT_PROCESSING"),
        )

        exchanges = session.completed_exchanges()

        assert exchanges == ()
