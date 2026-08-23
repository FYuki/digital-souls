import inspect
import sqlite3
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from app.conversation_history.errors import (
    ConversationNotFoundError,
    InvalidConversationIdError,
)
from app.conversation_history.models import (
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
    TurnStatus,
)
from app.privacy.contracts import HistoryDecisionReasonCode
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    OTHER_CONVERSATION_ID,
    OTHER_TURN_ID,
    TURN_ID,
    SequenceUuidFactory,
    create_repository,
)


class TestConversationLifecycle:
    def test_should_generate_uuid4_for_new_conversation(self, tmp_path: Path) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)

        conversation = repository.create_conversation("miori")

        assert conversation.character_id == "miori"
        assert conversation.conversation_id == CONVERSATION_ID
        assert conversation.conversation_id.version == 4

    def test_should_resume_existing_uuid4_conversation(self, tmp_path: Path) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)
        created = repository.create_conversation("miori")

        resumed = repository.resume_conversation(
            "miori",
            created.conversation_id,
        )

        assert resumed == created
        with sqlite3.connect(database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()[0]
        assert count == 1

    def test_should_reject_missing_character_conversation_boundary(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)
        repository.create_conversation("miori")

        with pytest.raises(ConversationNotFoundError):
            repository.resume_conversation("akira", CONVERSATION_ID)

    def test_should_classify_other_character_resume_separately_from_missing(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)
        repository.create_conversation("miori")

        with pytest.raises(ConversationNotFoundError) as boundary:
            repository.resume_conversation("akira", CONVERSATION_ID)
        with pytest.raises(ConversationNotFoundError) as missing:
            repository.resume_conversation("akira", OTHER_CONVERSATION_ID)

        assert type(boundary.value) is not type(missing.value)

    def test_should_classify_other_character_lifecycle_separately_from_missing(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)
        repository.create_conversation("miori")

        with pytest.raises(ConversationNotFoundError) as boundary:
            repository.archive_conversation("akira", CONVERSATION_ID)
        with pytest.raises(ConversationNotFoundError) as missing:
            repository.archive_conversation("akira", OTHER_CONVERSATION_ID)

        assert type(boundary.value) is not type(missing.value)

    def test_should_keep_missing_conversation_classified_as_not_found(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)

        with pytest.raises(ConversationNotFoundError) as missing:
            repository.resume_conversation("miori", CONVERSATION_ID)

        assert type(missing.value) is ConversationNotFoundError

    def test_should_reject_non_uuid4_conversation_id(self, tmp_path: Path) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)
        uuid_v1 = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

        with pytest.raises(InvalidConversationIdError):
            repository.resume_conversation("miori", uuid_v1)

    def test_should_allow_same_conversation_id_for_different_characters(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(
            database_path,
            uuid_factory=SequenceUuidFactory(
                CONVERSATION_ID,
                CONVERSATION_ID,
                TURN_ID,
                OTHER_TURN_ID,
            ),
        )

        miori = repository.create_conversation("miori")
        akira = repository.create_conversation("akira")
        miori_turn = repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="光織の履歴"),
        )
        akira_turn = repository.create_processing_turn(
            "akira",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="晶の履歴"),
        )

        assert miori.conversation_id == akira.conversation_id
        assert repository.resume_conversation("miori", CONVERSATION_ID) == miori
        assert repository.resume_conversation("akira", CONVERSATION_ID) == akira
        assert repository.list_turns("miori", CONVERSATION_ID) == [miori_turn]
        assert repository.list_turns("akira", CONVERSATION_ID) == [akira_turn]


class TestTurnCreation:
    def test_should_atomically_create_processing_turn_with_sanitized_body(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)
        conversation = repository.create_conversation("miori")

        turn = repository.create_processing_turn(
            "miori",
            conversation.conversation_id,
            ProcessingTurnInput(sanitized_user_content="連絡先は[REDACTED]です"),
        )

        assert turn.turn_id == TURN_ID
        assert turn.turn_id.version == 4
        assert turn.user_content == "連絡先は[REDACTED]です"
        assert turn.assistant_content is None
        assert turn.status is TurnStatus.PROCESSING
        assert repository.list_turns("miori", CONVERSATION_ID) == [turn]

    def test_consolidation_activity_returns_only_processing_count_and_latest_time(
        self,
        tmp_path: Path,
    ) -> None:
        repository = create_repository(tmp_path / "history.db")
        conversation = repository.create_conversation("miori")
        turn = repository.create_processing_turn(
            "miori",
            conversation.conversation_id,
            ProcessingTurnInput(sanitized_user_content="metadataには返さない本文"),
        )

        processing_count, latest_activity_at = repository.consolidation_activity()

        assert processing_count == 1
        assert latest_activity_at == turn.updated_at

    def test_should_create_privacy_skipped_turn_with_reason_only(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)
        repository.create_conversation("miori")

        turn = repository.create_privacy_skipped_turn(
            "miori",
            CONVERSATION_ID,
            PrivacySkippedTurnInput(
                reason_code=HistoryDecisionReasonCode.SCAN_FAILURE,
                sanitizer_version="test-sanitizer-v1",
                policy_version="test-policy-v1",
            ),
        )

        assert turn.turn_id == TURN_ID
        assert turn.status is TurnStatus.PRIVACY_SKIPPED
        assert turn.user_content is None
        assert turn.assistant_content is None
        assert turn.privacy_reason_code is HistoryDecisionReasonCode.SCAN_FAILURE

    def test_should_not_expose_raw_body_fields_on_privacy_skip_input(self) -> None:
        parameters = inspect.signature(PrivacySkippedTurnInput).parameters

        accepted_fields = set(parameters)

        assert accepted_fields == {
            "reason_code",
            "sanitizer_version",
            "policy_version",
        }

    def test_should_accept_only_sanitized_body_for_processing_input(self) -> None:
        parameters = inspect.signature(ProcessingTurnInput).parameters

        accepted_fields = set(parameters)

        assert accepted_fields == {"sanitized_user_content"}

    def test_should_reject_unregistered_privacy_reason_code(self) -> None:
        with pytest.raises(TypeError):
            PrivacySkippedTurnInput(
                reason_code=cast(HistoryDecisionReasonCode, "secret=raw-value"),
                sanitizer_version="test-sanitizer-v1",
                policy_version="test-policy-v1",
            )

    def test_should_reject_turn_creation_for_other_character(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)
        repository.create_conversation("miori")

        with pytest.raises(ConversationNotFoundError):
            repository.create_processing_turn(
                "akira",
                CONVERSATION_ID,
                ProcessingTurnInput(sanitized_user_content="処理済み本文"),
            )

    def test_should_keep_conversation_histories_isolated(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(
            database_path,
            uuid_factory=SequenceUuidFactory(
                CONVERSATION_ID,
                OTHER_CONVERSATION_ID,
                TURN_ID,
                OTHER_TURN_ID,
            ),
        )
        repository.create_conversation("miori")
        repository.create_conversation("miori")
        first = repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="最初の会話"),
        )
        repository.create_processing_turn(
            "miori",
            OTHER_CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="別の会話"),
        )

        turns = repository.list_turns("miori", CONVERSATION_ID)

        assert turns == [first]
