from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import sqlite3
from uuid import uuid4

import pytest

from app.conversation_history.models import ProcessingTurnInput
from app.conversation_history.schema import (
    initialize_conversation_history_schema,
    inspect_conversation_history_schema,
)
from app.conversation_history.memory_queue_schema import MEMORY_QUEUE_DEFINITIONS
from app.memory.formation.thread_queue import (
    ThreadFormationQueue,
    ThreadOutcome,
    StaleThreadSnapshot,
)
from tests.conversation_history_test_support import create_repository, FIXED_NOW


def setup(tmp_path):
    path = tmp_path / "conversation.db"
    repository = create_repository(path, uuid_factory=uuid4)
    now = [FIXED_NOW]
    queue = ThreadFormationQueue(
        database_path=path,
        clock=lambda: now[0],
        retention=timedelta(days=365),
        lease_seconds=30,
        retry_seconds=5,
    )
    conversation = repository.create_conversation("miori")
    return repository, queue, path, conversation, now


def complete(repository, conversation, text="静岡へ旅行した"):
    turn = repository.create_processing_turn(
        conversation.character_id,
        conversation.conversation_id,
        ProcessingTurnInput(text),
    )
    return repository.complete_turn(
        conversation.character_id,
        conversation.conversation_id,
        turn.turn_id,
        sanitized_assistant_content="旅行の思い出を聞いた",
    )


def rows(path, table):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


def finish(queue, snapshot, saved=0, rejected=0):
    with queue.guard(snapshot) as connection:
        return queue.finish(
            connection, snapshot, saved_count=saved, rejected_count=rejected
        )


def test_multiple_updates_coalesce_and_snapshot_covers_all_eligible_turns(tmp_path):
    repository, queue, path, conversation, _ = setup(tmp_path)
    turns = [complete(repository, conversation, str(i)) for i in range(12)]
    assert len(rows(path, "memory_thread_jobs")) == 1
    lease = queue.claim()
    assert lease.revision == 24
    assert queue.claim() is None
    snapshot = queue.snapshot(lease)
    assert [source.turn for source in snapshot.sources] == turns
    assert {source.revision for source in snapshot.sources} == {2}
    assert finish(queue, snapshot, saved=3, rejected=1) is ThreadOutcome.MIXED
    assert not queue.has_pending()
    assert queue.claim() is None
    assert len(rows(path, "memory_thread_receipts")) == 1


def test_in_progress_update_invalidates_old_snapshot_and_remains_reserved(tmp_path):
    repository, queue, _, conversation, _ = setup(tmp_path)
    complete(repository, conversation)
    first = queue.claim()
    snapshot = queue.snapshot(first)
    complete(repository, conversation, "別の日の経験")
    with pytest.raises(StaleThreadSnapshot):
        finish(queue, snapshot, saved=1)
    queue.release(first, failed=False)
    next_lease = queue.claim()
    assert next_lease.revision > first.revision
    assert len(queue.snapshot(next_lease).sources) == 2


@pytest.mark.parametrize("change", ["correction", "delete", "screen"])
def test_changed_or_deleted_or_screen_source_cannot_be_committed(tmp_path, change):
    repository, queue, path, conversation, _ = setup(tmp_path)
    turn = complete(repository, conversation)
    lease = queue.claim()
    snapshot = queue.snapshot(lease)
    with sqlite3.connect(path) as connection:
        if change == "correction":
            connection.execute(
                "UPDATE conversation_turns SET user_content = '訂正した本文' WHERE turn_id = ?",
                (str(turn.turn_id),),
            )
        elif change == "delete":
            connection.execute(
                "DELETE FROM conversation_turns WHERE turn_id = ?", (str(turn.turn_id),)
            )
        else:
            connection.execute(
                "INSERT INTO screen_turn_provenance VALUES(?, ?, ?, 1, 'routing', 'explicit_ui', 'window', 'direct_observation')",
                (str(turn.turn_id), str(uuid4()), str(uuid4())),
            )
    with pytest.raises(StaleThreadSnapshot):
        finish(queue, snapshot, saved=1)
    assert not rows(path, "memory_thread_receipts")
    queue.release(lease, failed=False)
    next_snapshot = queue.snapshot(queue.claim())
    assert len(next_snapshot.sources) == (1 if change == "correction" else 0)


def test_processing_turn_is_reserved_but_does_not_start_partial_extraction(tmp_path):
    repository, queue, _, conversation, _ = setup(tmp_path)
    turn = repository.create_processing_turn(
        "miori", conversation.conversation_id, ProcessingTurnInput("途中の発言")
    )
    assert queue.has_pending()
    assert queue.claim() is None
    repository.complete_turn(
        "miori",
        conversation.conversation_id,
        turn.turn_id,
        sanitized_assistant_content="応答",
    )
    assert queue.claim() is not None


def test_failed_attempt_and_process_restart_recover_only_unfinished_revision(tmp_path):
    repository, queue, path, conversation, now = setup(tmp_path)
    complete(repository, conversation)
    lease = queue.claim()
    queue.release(lease, failed=True)
    assert queue.claim() is None
    now[0] += timedelta(seconds=6)
    restarted = ThreadFormationQueue(
        database_path=path,
        clock=lambda: now[0],
        retention=timedelta(days=365),
        lease_seconds=30,
    )
    second = restarted.claim()
    assert second.revision == lease.revision
    assert second.token != lease.token
    assert rows(path, "memory_thread_jobs")[0]["attempts"] == 2
    finish(restarted, restarted.snapshot(second))
    assert restarted.claim() is None
    assert rows(path, "memory_thread_jobs")[0]["last_outcome"] == "EMPTY"


def test_expired_owner_cannot_write_after_another_process_claims(tmp_path):
    repository, queue, path, conversation, now = setup(tmp_path)
    complete(repository, conversation)
    first = queue.claim()
    snapshot = queue.snapshot(first)
    now[0] += timedelta(seconds=31)
    other = ThreadFormationQueue(
        database_path=path, clock=lambda: now[0], retention=timedelta(days=365)
    )
    new = other.claim()
    assert new.token != first.token
    with pytest.raises(StaleThreadSnapshot):
        finish(queue, snapshot)
    assert not queue.renew(first)
    queue.release(first, failed=True)
    assert rows(path, "memory_thread_jobs")[0]["lease_token"] == str(new.token)
    finish(other, other.snapshot(new))


def test_renewal_preserves_live_lease_and_does_not_hide_later_update(tmp_path):
    repository, queue, path, conversation, now = setup(tmp_path)
    complete(repository, conversation)
    lease = queue.claim()
    now[0] += timedelta(seconds=20)
    assert queue.renew(lease)
    now[0] += timedelta(seconds=20)
    assert queue.claim() is None
    complete(repository, conversation, "追加の発言")
    assert not queue.renew(lease)
    with pytest.raises(StaleThreadSnapshot):
        queue.snapshot(lease)
    queue.release(lease, failed=True)
    assert queue.claim().revision > lease.revision


def test_only_one_concurrent_worker_claims_same_thread(tmp_path):
    repository, queue, _, conversation, _ = setup(tmp_path)
    complete(repository, conversation)
    with ThreadPoolExecutor(max_workers=8) as executor:
        leases = list(executor.map(lambda _: queue.claim(), range(8)))
    assert sum(lease is not None for lease in leases) == 1


def test_turn_update_and_reservation_roll_back_together(tmp_path):
    repository, _, path, conversation, _ = setup(tmp_path)
    turn = complete(repository, conversation)
    before = rows(path, "memory_thread_jobs")
    with pytest.raises(RuntimeError):
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE conversation_turns SET user_content = '未確定の訂正' WHERE turn_id = ?",
                (str(turn.turn_id),),
            )
            raise RuntimeError("abort")
    assert rows(path, "memory_thread_jobs") == before
    assert (
        repository.get_turn("miori", conversation.conversation_id, turn.turn_id) == turn
    )


def test_failed_finish_transaction_preserves_retryable_lease(tmp_path):
    repository, queue, path, conversation, _ = setup(tmp_path)
    complete(repository, conversation)
    snapshot = queue.snapshot(queue.claim())
    with pytest.raises(RuntimeError):
        with queue.guard(snapshot) as connection:
            queue.finish(connection, snapshot, saved_count=1, rejected_count=0)
            raise RuntimeError("abort")
    assert not rows(path, "memory_thread_receipts")
    assert queue.has_pending()
    assert finish(queue, snapshot, saved=1) is ThreadOutcome.SAVED


def test_deleting_thread_keeps_tombstone_reservation_and_completes_empty_snapshot(
    tmp_path,
):
    repository, queue, path, conversation, _ = setup(tmp_path)
    complete(repository, conversation)
    repository.hard_delete_conversation("miori", conversation.conversation_id)
    assert queue.has_pending()
    snapshot = queue.snapshot(queue.claim())
    assert snapshot.sources == ()
    assert rows(path, "memory_turn_versions")[0]["deleted"] == 1
    finish(queue, snapshot)
    assert not queue.has_pending()


def test_queue_metadata_never_copies_conversation_body(tmp_path):
    repository, queue, path, conversation, _ = setup(tmp_path)
    text = "合成した本文のみに存在する文字列"
    complete(repository, conversation, text)
    finish(queue, queue.snapshot(queue.claim()), rejected=1)
    for table in (
        "memory_thread_jobs",
        "memory_turn_versions",
        "memory_thread_receipts",
    ):
        assert text not in repr(rows(path, table))


def test_version_eight_upgrade_preserves_all_conversation_data_and_reserves_existing_threads(
    tmp_path,
):
    repository, _, path, conversation, _ = setup(tmp_path)
    complete(repository, conversation)
    before = rows(path, "conversation_turns")
    with sqlite3.connect(path) as connection:
        for kind, name, _ in reversed(MEMORY_QUEUE_DEFINITIONS):
            connection.execute(f'DROP {kind.upper()} "{name}"')
        connection.execute("PRAGMA user_version = 8")
    assert inspect_conversation_history_schema(path).migration_required
    initialize_conversation_history_schema(path)
    assert inspect_conversation_history_schema(path).is_current
    assert rows(path, "conversation_turns") == before
    assert len(rows(path, "memory_thread_jobs")) == 1
    assert rows(path, "memory_thread_jobs")[0]["completed_revision"] == 0


def test_retention_expiry_invalidates_snapshot_before_commit(tmp_path):
    repository, queue, _, conversation, now = setup(tmp_path)
    complete(repository, conversation)
    snapshot = queue.snapshot(queue.claim())
    now[0] += timedelta(days=366)
    with pytest.raises(StaleThreadSnapshot):
        finish(queue, snapshot)


def test_screen_provenance_retarget_invalidates_both_original_and_new_threads(tmp_path):
    repository, queue, path, conversation, _ = setup(tmp_path)
    first = complete(repository, conversation)
    other = repository.create_conversation("miori")
    second = complete(repository, other)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO screen_turn_provenance VALUES(?, ?, ?, 1, 'routing', 'explicit_ui', 'window', 'direct_observation')",
            (str(first.turn_id), str(uuid4()), str(uuid4())))
    snapshots = [queue.snapshot(queue.claim()), queue.snapshot(queue.claim())]
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE screen_turn_provenance SET turn_id = ? WHERE turn_id = ?",
                           (str(second.turn_id), str(first.turn_id)))
    for snapshot in snapshots:
        with pytest.raises(StaleThreadSnapshot):
            finish(queue, snapshot)


def test_turn_identity_cannot_move_between_thread_scopes(tmp_path):
    repository, _, path, conversation, _ = setup(tmp_path)
    turn = complete(repository, conversation)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE conversation_turns SET conversation_id = ? WHERE turn_id = ?",
                               (str(uuid4()), str(turn.turn_id)))
