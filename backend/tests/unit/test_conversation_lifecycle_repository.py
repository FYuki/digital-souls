import sqlite3
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app.conversation_history.errors import (
    ConversationCharacterBoundaryError,
    ConversationNotFoundError,
)
from app.conversation_history.models import ProcessingTurnInput
from app.conversation_history.repository import ConversationHistoryRepository
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    FIXED_NOW,
    OTHER_CONVERSATION_ID,
    OTHER_TURN_ID,
    TURN_ID,
    SequenceUuidFactory,
    create_repository,
    set_turn_times,
)


def _repository_with_two_conversations(
    database_path: Path,
) -> ConversationHistoryRepository:
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
        ProcessingTurnInput(sanitized_user_content="先の保存済み本文"),
    )
    second = repository.create_processing_turn(
        "miori",
        OTHER_CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="後の保存済み本文"),
    )
    repository.complete_turn(
        "miori",
        CONVERSATION_ID,
        first.turn_id,
        sanitized_assistant_content="先の保存済み回答",
    )
    repository.complete_turn(
        "miori",
        OTHER_CONVERSATION_ID,
        second.turn_id,
        sanitized_assistant_content="後の保存済み回答",
    )
    set_turn_times(
        database_path,
        first.turn_id,
        created_at=FIXED_NOW - timedelta(minutes=2),
        updated_at=FIXED_NOW - timedelta(minutes=2),
    )
    set_turn_times(
        database_path,
        second.turn_id,
        created_at=FIXED_NOW - timedelta(minutes=1),
        updated_at=FIXED_NOW - timedelta(minutes=1),
    )
    required_operations = {
        "list_active_conversations",
        "list_archived_conversations",
        "archive_conversation",
        "unarchive_conversation",
        "hard_delete_conversation",
    }
    assert required_operations <= set(dir(repository)), (
        "conversation lifecycle repository operations are not implemented"
    )
    return repository


def test_should_list_active_conversations_by_latest_turn_descending(
    tmp_path: Path,
) -> None:
    repository = _repository_with_two_conversations(tmp_path / "history.db")

    conversations = repository.list_active_conversations("miori")

    assert [item.conversation_id for item in conversations] == [
        OTHER_CONVERSATION_ID,
        CONVERSATION_ID,
    ]


def test_should_separate_active_and_archived_conversations(tmp_path: Path) -> None:
    repository = _repository_with_two_conversations(tmp_path / "history.db")

    repository.archive_conversation("miori", CONVERSATION_ID)

    assert [item.conversation_id for item in repository.list_active_conversations("miori")] == [
        OTHER_CONVERSATION_ID
    ]
    assert [item.conversation_id for item in repository.list_archived_conversations("miori")] == [
        CONVERSATION_ID
    ]


def test_should_keep_conversation_and_turns_when_archived(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    repository = _repository_with_two_conversations(database_path)

    repository.archive_conversation("miori", CONVERSATION_ID)

    with sqlite3.connect(database_path) as connection:
        conversation_count = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE character_id = ? AND conversation_id = ?",
            ("miori", str(CONVERSATION_ID)),
        ).fetchone()[0]
        turn_count = connection.execute(
            "SELECT COUNT(*) FROM conversation_turns WHERE character_id = ? AND conversation_id = ?",
            ("miori", str(CONVERSATION_ID)),
        ).fetchone()[0]
    assert conversation_count == 1
    assert turn_count == 1


def test_should_not_recover_stale_processing_turn_after_archive(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    repository = create_repository(database_path)
    repository.create_conversation("miori")
    turn = repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="処理中の保存済み本文"),
    )
    set_turn_times(
        database_path,
        turn.turn_id,
        created_at=FIXED_NOW - timedelta(minutes=10),
        updated_at=FIXED_NOW - timedelta(minutes=10),
    )
    repository.archive_conversation("miori", CONVERSATION_ID)

    recovered = repository.recover_stale_processing()

    with sqlite3.connect(database_path) as connection:
        stored_status = connection.execute(
            "SELECT status FROM conversation_turns WHERE turn_id = ?",
            (str(turn.turn_id),),
        ).fetchone()[0]
    assert recovered == []
    assert stored_status == "processing"


@pytest.mark.parametrize(
    "operation",
    ["resume_conversation", "list_turns", "list_prompt_turns_page", "create_processing_turn"],
)
def test_should_reject_normal_history_paths_for_archived_conversation(
    tmp_path: Path,
    operation: str,
) -> None:
    repository = _repository_with_two_conversations(tmp_path / "history.db")
    repository.archive_conversation("miori", CONVERSATION_ID)

    with pytest.raises(ConversationNotFoundError):
        if operation == "resume_conversation":
            repository.resume_conversation("miori", CONVERSATION_ID)
        elif operation == "list_turns":
            repository.list_turns("miori", CONVERSATION_ID)
        elif operation == "list_prompt_turns_page":
            repository.list_prompt_turns_page("miori", CONVERSATION_ID, page_size=10)
        else:
            repository.create_processing_turn(
                "miori",
                CONVERSATION_ID,
                ProcessingTurnInput(sanitized_user_content="追記してはいけない本文"),
            )


def test_should_unarchive_and_resume_the_same_conversation_id(tmp_path: Path) -> None:
    repository = _repository_with_two_conversations(tmp_path / "history.db")
    repository.archive_conversation("miori", CONVERSATION_ID)

    restored = repository.unarchive_conversation("miori", CONVERSATION_ID)
    resumed = repository.resume_conversation("miori", CONVERSATION_ID)

    assert restored.conversation_id == CONVERSATION_ID
    assert resumed.conversation_id == CONVERSATION_ID
    assert repository.list_turns("miori", CONVERSATION_ID)[0].turn_id == TURN_ID


@pytest.mark.parametrize(
    ("operation", "initially_archived"),
    [("archive_conversation", False), ("unarchive_conversation", True)],
)
def test_should_roll_back_archive_state_when_lifecycle_update_fails(
    tmp_path: Path,
    operation: str,
    initially_archived: bool,
) -> None:
    database_path = tmp_path / "history.db"
    repository = _repository_with_two_conversations(database_path)
    if initially_archived:
        repository.archive_conversation("miori", CONVERSATION_ID)
    expected_archived_at = (
        FIXED_NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if initially_archived
        else None
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_archive_update BEFORE UPDATE OF archived_at "
            "ON conversations BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        getattr(repository, operation)("miori", CONVERSATION_ID)

    with sqlite3.connect(database_path) as connection:
        archived_at = connection.execute(
            "SELECT archived_at FROM conversations "
            "WHERE character_id = ? AND conversation_id = ?",
            ("miori", str(CONVERSATION_ID)),
        ).fetchone()[0]
    assert archived_at == expected_archived_at


def test_should_not_archive_a_conversation_owned_by_another_character(tmp_path: Path) -> None:
    repository = _repository_with_two_conversations(tmp_path / "history.db")

    with pytest.raises(ConversationNotFoundError):
        repository.archive_conversation("akira", CONVERSATION_ID)

    assert [item.conversation_id for item in repository.list_active_conversations("miori")] == [
        OTHER_CONVERSATION_ID,
        CONVERSATION_ID,
    ]


def test_should_hard_delete_conversation_and_all_of_its_turns(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    repository = _repository_with_two_conversations(database_path)
    repository.archive_conversation("miori", CONVERSATION_ID)

    repository.hard_delete_conversation("miori", CONVERSATION_ID)

    with sqlite3.connect(database_path) as connection:
        deleted_conversations = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE conversation_id = ?",
            (str(CONVERSATION_ID),),
        ).fetchone()[0]
        deleted_turns = connection.execute(
            "SELECT COUNT(*) FROM conversation_turns WHERE conversation_id = ?",
            (str(CONVERSATION_ID),),
        ).fetchone()[0]
        remaining_turns = connection.execute(
            "SELECT COUNT(*) FROM conversation_turns WHERE conversation_id = ?",
            (str(OTHER_CONVERSATION_ID),),
        ).fetchone()[0]
    assert (deleted_conversations, deleted_turns, remaining_turns) == (0, 0, 1)


def test_should_hard_delete_only_the_selected_character_conversation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    repository = create_repository(
        database_path,
        uuid_factory=SequenceUuidFactory(
            *(UUID(f"00000000-0000-4000-8000-{index:012d}") for index in range(1, 10))
        ),
    )
    conversation_a1 = repository.create_conversation("miori")
    conversation_a2 = repository.create_conversation("miori")
    conversation_b1 = repository.create_conversation("akira")

    for character_id, conversation in (
        ("miori", conversation_a1),
        ("miori", conversation_a2),
        ("akira", conversation_b1),
    ):
        for index in range(2):
            turn = repository.create_processing_turn(
                character_id,
                conversation.conversation_id,
                ProcessingTurnInput(
                    sanitized_user_content=f"{character_id}の保存済み本文{index}"
                ),
            )
            repository.complete_turn(
                character_id,
                conversation.conversation_id,
                turn.turn_id,
                sanitized_assistant_content=f"{character_id}の保存済み回答{index}",
            )

    repository.archive_conversation("miori", conversation_a1.conversation_id)
    repository.hard_delete_conversation("miori", conversation_a1.conversation_id)

    with sqlite3.connect(database_path) as connection:
        selected_conversation_count = connection.execute(
            "SELECT COUNT(*) FROM conversations "
            "WHERE character_id = ? AND conversation_id = ?",
            ("miori", str(conversation_a1.conversation_id)),
        ).fetchone()[0]
        selected_turn_count = connection.execute(
            "SELECT COUNT(*) FROM conversation_turns "
            "WHERE character_id = ? AND conversation_id = ?",
            ("miori", str(conversation_a1.conversation_id)),
        ).fetchone()[0]

    with pytest.raises(ConversationNotFoundError):
        repository.list_turns("miori", conversation_a1.conversation_id)
    assert selected_conversation_count == 0
    assert selected_turn_count == 0
    assert len(repository.list_turns("miori", conversation_a2.conversation_id)) == 2
    assert len(repository.list_turns("akira", conversation_b1.conversation_id)) == 2


def test_should_roll_back_all_deletions_when_hard_delete_fails(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    repository = _repository_with_two_conversations(database_path)
    repository.archive_conversation("miori", CONVERSATION_ID)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_conversation_delete BEFORE DELETE ON conversations "
            "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        repository.hard_delete_conversation("miori", CONVERSATION_ID)

    with sqlite3.connect(database_path) as connection:
        conversation_count = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE conversation_id = ?",
            (str(CONVERSATION_ID),),
        ).fetchone()[0]
        turn_count = connection.execute(
            "SELECT COUNT(*) FROM conversation_turns WHERE conversation_id = ?",
            (str(CONVERSATION_ID),),
        ).fetchone()[0]
    assert (conversation_count, turn_count) == (1, 1)


def test_should_classify_archived_same_character_before_other_character_boundary(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    repository = create_repository(
        database_path,
        uuid_factory=SequenceUuidFactory(CONVERSATION_ID, CONVERSATION_ID),
    )
    repository.create_conversation("miori")
    repository.create_conversation("akira")
    repository.archive_conversation("miori", CONVERSATION_ID)

    with pytest.raises(ConversationNotFoundError) as archived:
        repository.resume_conversation("miori", CONVERSATION_ID)

    assert type(archived.value) is ConversationNotFoundError


def test_should_keep_other_character_and_missing_classifications_distinct(
    tmp_path: Path,
) -> None:
    repository = create_repository(tmp_path / "history.db")
    repository.create_conversation("miori")

    with pytest.raises(ConversationCharacterBoundaryError):
        repository.resume_conversation("akira", CONVERSATION_ID)
    with pytest.raises(ConversationNotFoundError) as missing:
        repository.resume_conversation("akira", OTHER_CONVERSATION_ID)

    assert type(missing.value) is ConversationNotFoundError


@pytest.mark.parametrize(
    "operation",
    [
        "resume_conversation",
        "unarchive_conversation",
        "list_turns",
        "list_prompt_turns_page",
        "create_processing_turn",
    ],
)
def test_should_reject_all_normal_paths_for_hard_deleted_conversation(
    tmp_path: Path,
    operation: str,
) -> None:
    repository = _repository_with_two_conversations(tmp_path / "history.db")
    repository.archive_conversation("miori", CONVERSATION_ID)
    repository.hard_delete_conversation("miori", CONVERSATION_ID)

    with pytest.raises(ConversationNotFoundError):
        if operation == "list_prompt_turns_page":
            repository.list_prompt_turns_page("miori", CONVERSATION_ID, page_size=10)
        elif operation == "create_processing_turn":
            repository.create_processing_turn(
                "miori",
                CONVERSATION_ID,
                ProcessingTurnInput(sanitized_user_content="再作成してはいけない本文"),
            )
        else:
            getattr(repository, operation)("miori", CONVERSATION_ID)
