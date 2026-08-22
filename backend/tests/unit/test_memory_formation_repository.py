from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from app.conversation_history.models import TurnStatus
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    FIXED_NOW,
    SequenceUuidFactory,
    create_repository,
)


ANCHOR_ID = UUID("9e70795d-e5d5-431d-baa2-67f884403040")
PREVIOUS_ID = UUID("9e70795d-e5d5-431d-baa2-67f884403041")
OLDER_ID = UUID("9e70795d-e5d5-431d-baa2-67f884403042")
FAILED_ID = UUID("9e70795d-e5d5-431d-baa2-67f884403043")
OTHER_CONVERSATION_ID = UUID("e98d6c65-1ae9-4d6f-a8c8-d59b0ad09012")
OTHER_ID = UUID("9e70795d-e5d5-431d-baa2-67f884403044")
FOLLOWING_ID = UUID("9e70795d-e5d5-431d-baa2-67f884403039")


def _insert_conversation(database_path: Path, conversation_id: UUID) -> None:
    timestamp = FIXED_NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO conversations "
            "(character_id, conversation_id, created_at) VALUES (?, ?, ?)",
            ("miori", str(conversation_id), timestamp),
        )


def _insert_turn(
    database_path: Path,
    turn_id: UUID,
    *,
    seconds_ago: int,
    status: TurnStatus = TurnStatus.COMPLETED,
    conversation_id: UUID = CONVERSATION_ID,
) -> None:
    timestamp = (FIXED_NOW - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    assistant = "assistant" if status is TurnStatus.COMPLETED else None
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO conversation_turns "
            "(turn_id, character_id, conversation_id, user_content, "
            "assistant_content, status, privacy_reason_code, sanitizer_version, "
            "policy_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
            (
                str(turn_id),
                "miori",
                str(conversation_id),
                f"user-{turn_id}",
                assistant,
                status.value,
                timestamp,
                timestamp,
            ),
        )


def test_previous_turn_is_latest_retained_completed_turn_in_the_same_conversation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    repository = create_repository(
        database_path,
        retention=timedelta(seconds=100),
        uuid_factory=SequenceUuidFactory(CONVERSATION_ID),
    )
    repository.create_conversation("miori")
    _insert_conversation(database_path, OTHER_CONVERSATION_ID)
    _insert_turn(database_path, ANCHOR_ID, seconds_ago=1)
    _insert_turn(database_path, PREVIOUS_ID, seconds_ago=2)
    _insert_turn(database_path, FAILED_ID, seconds_ago=1, status=TurnStatus.FAILED)
    _insert_turn(database_path, OLDER_ID, seconds_ago=101)
    _insert_turn(
        database_path,
        OTHER_ID,
        seconds_ago=1,
        conversation_id=OTHER_CONVERSATION_ID,
    )

    previous = repository.get_previous_completed_turn(
        "miori",
        CONVERSATION_ID,
        ANCHOR_ID,
    )

    assert previous is not None
    assert previous.turn_id == PREVIOUS_ID
    assert previous.status is TurnStatus.COMPLETED
    assert previous.conversation_id == CONVERSATION_ID


def test_previous_turn_uses_persistence_order_for_equal_timestamps(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    repository = create_repository(
        database_path,
        retention=timedelta(seconds=100),
        uuid_factory=SequenceUuidFactory(CONVERSATION_ID),
    )
    repository.create_conversation("miori")
    _insert_turn(database_path, PREVIOUS_ID, seconds_ago=1)
    _insert_turn(database_path, ANCHOR_ID, seconds_ago=1)
    _insert_turn(database_path, FOLLOWING_ID, seconds_ago=1)

    previous = repository.get_previous_completed_turn(
        "miori",
        CONVERSATION_ID,
        ANCHOR_ID,
    )

    assert previous is not None
    assert previous.turn_id == PREVIOUS_ID
