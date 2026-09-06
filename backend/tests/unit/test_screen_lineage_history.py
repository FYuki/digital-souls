import sqlite3
from uuid import UUID

from app.conversation_history.models import ProcessingTurnInput
from app.screen_perception.provenance import ScreenLineage
from tests.conversation_history_test_support import create_repository


def test_multiple_lineages_round_trip_without_raw_screen_content(tmp_path) -> None:
    database = tmp_path / "conversation.sqlite3"
    repository = create_repository(database)
    conversation = repository.create_conversation("miori")
    turn = repository.create_processing_turn(
        "miori",
        conversation.conversation_id,
        ProcessingTurnInput(sanitized_user_content="続きの質問"),
    )
    lineages = (
        ScreenLineage(
            UUID("10000000-0000-4000-8000-000000000011"),
            UUID("20000000-0000-4000-8000-000000000011"),
            3,
            "routing-a",
            "natural_language_text",
            "monitor",
        ),
        ScreenLineage(
            UUID("10000000-0000-4000-8000-000000000012"),
            UUID("20000000-0000-4000-8000-000000000012"),
            7,
            "routing-b",
            "natural_language_voice",
            "window",
            "conversation_follow_up",
        ),
    )

    repository.mark_screen_derived(
        "miori", conversation.conversation_id, turn.turn_id, lineages
    )

    assert repository.list_screen_lineages(
        "miori", conversation.conversation_id, turn.turn_id
    ) == lineages
    assert repository.is_screen_derived(
        "miori", conversation.conversation_id, turn.turn_id
    )
    with sqlite3.connect(database) as connection:
        dump = "\n".join(connection.iterdump())
    assert "SCREEN_RAW_CANARY" not in dump
    assert "recognized_content" not in dump


def test_lineage_is_deleted_with_its_conversation(tmp_path) -> None:
    database = tmp_path / "conversation.sqlite3"
    repository = create_repository(database)
    conversation = repository.create_conversation("miori")
    turn = repository.create_processing_turn(
        "miori",
        conversation.conversation_id,
        ProcessingTurnInput(sanitized_user_content="画面の質問"),
    )
    lineage = ScreenLineage(
        UUID("10000000-0000-4000-8000-000000000013"),
        UUID("20000000-0000-4000-8000-000000000013"),
        1,
        "routing",
        "explicit_ui",
        "monitor",
    )
    repository.mark_screen_derived(
        "miori", conversation.conversation_id, turn.turn_id, (lineage,)
    )

    repository.hard_delete_conversation("miori", conversation.conversation_id)

    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM screen_turn_provenance"
        ).fetchone()[0]
    assert count == 0
