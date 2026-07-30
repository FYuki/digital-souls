from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.conversation_history.errors import InvalidUtcDatetimeError
from app.conversation_history.models import ProcessingTurnInput, TurnStatus
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    OTHER_TURN_ID,
    TURN_ID,
    SequenceUuidFactory,
    create_repository,
    set_turn_times,
)


def _repository_with_two_processing_turns(database_path: Path):
    repository = create_repository(
        database_path,
        uuid_factory=SequenceUuidFactory(
            CONVERSATION_ID,
            TURN_ID,
            OTHER_TURN_ID,
        ),
    )
    repository.create_conversation("miori")
    repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="古いturn"),
    )
    repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="新しいturn"),
    )
    return repository


class TestStaleRecovery:
    def test_should_recover_only_processing_older_than_cutoff(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = _repository_with_two_processing_turns(database_path)
        now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        set_turn_times(
            database_path,
            TURN_ID,
            created_at=now - timedelta(minutes=10),
            updated_at=now - timedelta(seconds=301),
        )
        set_turn_times(
            database_path,
            OTHER_TURN_ID,
            created_at=now - timedelta(minutes=5),
            updated_at=now - timedelta(seconds=300),
        )

        recovered = repository.recover_stale_processing()

        assert [turn.turn_id for turn in recovered] == [TURN_ID]
        turns = repository.list_turns("miori", CONVERSATION_ID)
        statuses = {turn.turn_id: turn.status for turn in turns}
        assert statuses == {
            TURN_ID: TurnStatus.FAILED,
            OTHER_TURN_ID: TurnStatus.PROCESSING,
        }

    def test_should_not_recover_completed_turn_even_when_old(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)
        repository.create_conversation("miori")
        repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="処理済み本文"),
        )
        repository.complete_turn(
            "miori",
            CONVERSATION_ID,
            TURN_ID,
            sanitized_assistant_content="完全回答",
        )
        old = datetime(2026, 7, 24, 11, 0, tzinfo=UTC)
        set_turn_times(
            database_path,
            TURN_ID,
            created_at=old,
            updated_at=old,
        )

        recovered = repository.recover_stale_processing()

        assert recovered == []
        turns = repository.list_turns("miori", CONVERSATION_ID)
        assert turns[0].status is TurnStatus.COMPLETED

    def test_should_recover_stale_processing_before_listing_history(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = _repository_with_two_processing_turns(database_path)
        set_turn_times(
            database_path,
            TURN_ID,
            created_at=datetime(2026, 7, 24, 11, 0, tzinfo=UTC),
            updated_at=datetime(2026, 7, 24, 11, 54, 59, tzinfo=UTC),
        )

        turns = repository.list_turns("miori", CONVERSATION_ID)

        stale_turn = next(turn for turn in turns if turn.turn_id == TURN_ID)
        assert stale_turn.status is TurnStatus.FAILED


class TestHistoryRetention:
    def test_should_include_turn_exactly_at_retention_cutoff(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = _repository_with_two_processing_turns(database_path)
        cutoff = datetime(2025, 7, 24, 12, 0, tzinfo=UTC)
        set_turn_times(
            database_path,
            TURN_ID,
            created_at=cutoff,
            updated_at=cutoff,
        )

        turns = repository.list_turns("miori", CONVERSATION_ID)

        assert TURN_ID in {turn.turn_id for turn in turns}

    def test_should_exclude_without_deleting_turn_older_than_retention(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = _repository_with_two_processing_turns(database_path)
        expired = datetime(2025, 7, 24, 11, 59, 59, 999999, tzinfo=UTC)
        set_turn_times(
            database_path,
            TURN_ID,
            created_at=expired,
            updated_at=expired,
        )

        turns = repository.list_turns("miori", CONVERSATION_ID)

        assert TURN_ID not in {turn.turn_id for turn in turns}
        extended_repository = create_repository(
            database_path,
            retention=timedelta(days=366),
        )
        assert TURN_ID in {
            turn.turn_id
            for turn in extended_repository.list_turns("miori", CONVERSATION_ID)
        }

    def test_should_apply_configured_retention_period(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(
            database_path,
            retention=timedelta(days=30),
            uuid_factory=SequenceUuidFactory(
                CONVERSATION_ID,
                TURN_ID,
                OTHER_TURN_ID,
            ),
        )
        repository.create_conversation("miori")
        repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="古いturn"),
        )
        repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="新しいturn"),
        )
        old = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
        set_turn_times(
            database_path,
            TURN_ID,
            created_at=old,
            updated_at=old,
        )

        turns = repository.list_turns("miori", CONVERSATION_ID)

        assert TURN_ID not in {turn.turn_id for turn in turns}

    def test_should_compare_offset_aware_clock_in_utc(self, tmp_path: Path) -> None:
        database_path = tmp_path / "history.db"
        now_jst = datetime.fromisoformat("2026-07-24T21:00:00+09:00")
        repository = create_repository(database_path, now=now_jst)
        repository.create_conversation("miori")
        turn = repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="UTC保存確認"),
        )

        created_at = turn.created_at

        assert created_at == datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        assert created_at.tzinfo is UTC

    def test_should_reject_naive_clock_at_sqlite_boundary(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(
            database_path,
            now=datetime(2026, 7, 24, 12, 0),
        )

        with pytest.raises(InvalidUtcDatetimeError):
            repository.create_conversation("miori")
