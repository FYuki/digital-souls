import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app.conversation_history.models import TurnStatus
from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.wal_cleanup import ConversationWalCleanup
from app.conversation_history.schema import initialize_conversation_history_schema
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    FIXED_NOW,
    SequenceUuidFactory,
    create_repository,
)


OTHER_CONVERSATION_ID = UUID("e98d6c65-1ae9-4d6f-a8c8-d59b0ad09012")
TURN_IDS = tuple(
    UUID(f"9e70795d-e5d5-431d-baa2-67f8844030{index:02d}")
    for index in range(20, 32)
)


def _insert_conversation(
    database_path: Path,
    character_id: str,
    conversation_id: UUID,
) -> None:
    timestamp = FIXED_NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO conversations "
            "(character_id, conversation_id, created_at) VALUES (?, ?, ?)",
            (character_id, str(conversation_id), timestamp),
        )


def _insert_turn(
    database_path: Path,
    turn_id: UUID,
    *,
    character_id: str = "miori",
    conversation_id: UUID = CONVERSATION_ID,
    status: TurnStatus = TurnStatus.COMPLETED,
    seconds_ago: int = 0,
    assistant_content: str | None = "assistant",
) -> None:
    timestamp = (FIXED_NOW - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    user_content = None if status is TurnStatus.PRIVACY_SKIPPED else f"user-{turn_id}"
    privacy_reason = "SCAN_FAILURE" if status is TurnStatus.PRIVACY_SKIPPED else None
    sanitizer_version = (
        "test-sanitizer-v1" if status is TurnStatus.PRIVACY_SKIPPED else None
    )
    policy_version = "test-policy-v1" if status is TurnStatus.PRIVACY_SKIPPED else None
    if status in {TurnStatus.PROCESSING, TurnStatus.PRIVACY_SKIPPED}:
        assistant_content = None
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO conversation_turns "
            "(turn_id, character_id, conversation_id, user_content, "
            "assistant_content, status, privacy_reason_code, sanitizer_version, "
            "policy_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(turn_id),
                character_id,
                str(conversation_id),
                user_content,
                assistant_content,
                status.value,
                privacy_reason,
                sanitizer_version,
                policy_version,
                timestamp,
                timestamp,
            ),
        )


def _repository(database_path: Path):
    repository = create_repository(
        database_path,
        uuid_factory=SequenceUuidFactory(CONVERSATION_ID),
    )
    repository.create_conversation("miori")
    return repository


class TestPromptHistoryPage:
    def test_should_separate_character_conversation_and_statuses(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = _repository(database_path)
        _insert_conversation(database_path, "other", CONVERSATION_ID)
        _insert_conversation(database_path, "miori", OTHER_CONVERSATION_ID)
        _insert_turn(database_path, TURN_IDS[0], status=TurnStatus.COMPLETED)
        _insert_turn(database_path, TURN_IDS[1], status=TurnStatus.FAILED)
        _insert_turn(database_path, TURN_IDS[2], status=TurnStatus.PROCESSING)
        _insert_turn(database_path, TURN_IDS[3], status=TurnStatus.PRIVACY_SKIPPED)
        _insert_turn(database_path, TURN_IDS[4], character_id="other")
        _insert_turn(
            database_path,
            TURN_IDS[5],
            conversation_id=OTHER_CONVERSATION_ID,
        )

        page = repository.list_prompt_turns_page(
            "miori", CONVERSATION_ID, page_size=10
        )

        assert [turn.turn_id for turn in page.turns] == [TURN_IDS[1], TURN_IDS[0]]
        assert {turn.status for turn in page.turns} == {
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
        }

    def test_should_page_newest_first_with_a_stable_composite_cursor(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = _repository(database_path)
        for turn_id in TURN_IDS[:3]:
            _insert_turn(database_path, turn_id)

        first = repository.list_prompt_turns_page(
            "miori", CONVERSATION_ID, page_size=2
        )
        second = repository.list_prompt_turns_page(
            "miori",
            CONVERSATION_ID,
            cursor=first.next_cursor,
            page_size=2,
        )

        assert [turn.turn_id for turn in first.turns] == [TURN_IDS[2], TURN_IDS[1]]
        assert [turn.turn_id for turn in second.turns] == [TURN_IDS[0]]
        assert first.next_cursor.created_at == first.turns[-1].created_at
        assert first.next_cursor.turn_id == first.turns[-1].turn_id
        assert second.next_cursor is None

    def test_should_apply_retention_and_recover_stale_processing(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = _repository(database_path)
        _insert_turn(database_path, TURN_IDS[0], seconds_ago=365 * 24 * 60 * 60)
        _insert_turn(database_path, TURN_IDS[1], seconds_ago=365 * 24 * 60 * 60 + 1)
        _insert_turn(
            database_path,
            TURN_IDS[2],
            status=TurnStatus.PROCESSING,
            seconds_ago=301,
        )

        page = repository.list_prompt_turns_page(
            "miori", CONVERSATION_ID, page_size=10
        )

        assert [turn.turn_id for turn in page.turns] == [TURN_IDS[2], TURN_IDS[0]]
        assert page.turns[0].status is TurnStatus.FAILED

    @pytest.mark.parametrize("page_size", [0, 101])
    def test_should_reject_page_size_outside_fixed_bounds(
        self,
        tmp_path: Path,
        page_size: int,
    ) -> None:
        repository = _repository(tmp_path / "history.db")

        with pytest.raises(ValueError, match="page_size"):
            repository.list_prompt_turns_page(
                "miori", CONVERSATION_ID, page_size=page_size
            )

    def test_should_keep_retention_cutoff_fixed_across_pages(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        current_time = [FIXED_NOW]
        repository = ConversationHistoryRepository(
            database_path=database_path,
            stale_after=timedelta(seconds=300),
            retention=timedelta(days=365),
            clock=lambda: current_time[0],
            uuid_factory=SequenceUuidFactory(CONVERSATION_ID),
            wal_cleanup=ConversationWalCleanup(
                database_path=database_path,
                clock=lambda: current_time[0],
                connection_factory=sqlite3.connect,
            ),
        )
        initialize_conversation_history_schema(database_path)
        repository.create_conversation("miori")
        _insert_turn(database_path, TURN_IDS[0], seconds_ago=365 * 24 * 60 * 60)
        _insert_turn(database_path, TURN_IDS[1])

        first = repository.list_prompt_turns_page(
            "miori", CONVERSATION_ID, page_size=1
        )
        current_time[0] = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        second = repository.list_prompt_turns_page(
            "miori",
            CONVERSATION_ID,
            cursor=first.next_cursor,
            page_size=1,
        )

        assert [turn.turn_id for turn in second.turns] == [TURN_IDS[0]]

    def test_should_use_existing_history_index_for_prompt_range_query(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        _repository(database_path)

        with sqlite3.connect(database_path) as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT turn_id FROM conversation_turns "
                "WHERE character_id = ? AND conversation_id = ? "
                "AND status IN ('completed', 'failed') AND created_at >= ? "
                "AND (created_at, turn_id) < (?, ?) "
                "ORDER BY created_at DESC, turn_id DESC LIMIT ?",
                (
                    "miori",
                    str(CONVERSATION_ID),
                    "2025-07-24T12:00:00.000000Z",
                    "2026-07-24T12:00:00.000000Z",
                    str(TURN_IDS[0]),
                    32,
                ),
            ).fetchall()

        details = " ".join(str(row[3]) for row in plan)
        assert "conversation_turns_history_idx" in details
        assert "USE TEMP B-TREE" not in details
