"""The same observable refusal contracts against the disposable PostgreSQL fixture."""

from collections.abc import Iterator

import pytest

from digital_souls_core.application import CoreError
from digital_souls_core.history import SourceReference
from digital_souls_core.postgres_db import PostgresDatabase
from digital_souls_core.postgres_history import PostgresHistory
from digital_souls_core.postgres_memory_records import PostgresMemoryRecords
from digital_souls_core.postgres_schema import SCHEMA_VERSION

from . import memory_confirmation_contracts as contracts
from . import test_postgres_stores
from .memory_confirmation_contracts import (
    BASE,
    Harness,
    answer,
    complete,
    make_harness,
)
from .postgres_record_support import events, register_sources
from .test_postgres_stated_at import CID, install_v1
from .test_postgres_stores import BINDING, Stores
from .time_support import FIRST

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores

test_accept_erases_existing_memory_and_never_restores_accepted_source = (
    contracts.test_accept_erases_existing_memory_and_never_restores_accepted_source
)
test_assistant_refusal_quotes_and_history_do_not_hold_new_user = (
    contracts.test_assistant_refusal_quotes_and_history_do_not_hold_new_user
)
test_confirmation_invalidates_inflight_completion = (
    contracts.test_confirmation_invalidates_inflight_completion
)
test_confirmation_obeys_history_policy = contracts.test_confirmation_obeys_history_policy
test_confirmation_cannot_change_another_binding = (
    contracts.test_confirmation_cannot_change_another_binding
)
test_decline_does_not_disable_private_mode = contracts.test_decline_does_not_disable_private_mode
test_decline_releases_only_target_and_preserves_explicit_exclusion = (
    contracts.test_decline_releases_only_target_and_preserves_explicit_exclusion
)
test_pending_source_is_rejected_for_record_registration = (
    contracts.test_pending_source_is_rejected_for_record_registration
)
test_refusal_signal_identifies_each_user_source_without_content = (
    contracts.test_refusal_signal_identifies_each_user_source_without_content
)
test_resolved_confirmation_retry_keeps_receipt_dates_and_does_not_rehold = (
    contracts.test_resolved_confirmation_retry_keeps_receipt_dates_and_does_not_rehold
)
test_stale_and_repeated_confirmation_leave_state_unchanged = (
    contracts.test_stale_and_repeated_confirmation_leave_state_unchanged
)
test_tool_refusal_text_does_not_create_confirmation = (
    contracts.test_tool_refusal_text_does_not_create_confirmation
)
test_unanswered_source_is_rejected_before_record_registration = (
    contracts.test_unanswered_source_is_rejected_before_record_registration
)


@pytest.fixture
def harness(stores: Stores) -> Iterator[Harness]:
    value = make_harness(stores.history, stores.records)
    with value.http:
        yield value


def test_pending_confirmation_and_receipt_survive_postgres_restart(
    harness: Harness,
    stores: Stores,
) -> None:
    cid = harness.conversation.create("synthetic").conversation_id
    changes = {"messages": [{"role": "user", "content": "覚えないで"}]}
    first = complete(harness, cid, **changes)
    before = harness.conversation.read("synthetic", cid)
    database = PostgresDatabase(stores.config)
    restored = make_harness(PostgresHistory(database), PostgresMemoryRecords(database))
    with restored.http:
        assert restored.conversation.read("synthetic", cid) == before
        assert restored.http.get(f"{BASE}/{cid}").json()["memory_confirmations"] == [
            {"turn_revision": 1, "message_index": 0}
        ]
        assert not restored.conversation.store.source_eligible(BINDING, SourceReference(cid, 1, 0))
        with pytest.raises(CoreError):
            register_sources(
                restored.records, restored.conversation.store, (SourceReference(cid, 1, 0),)
            )
        assert complete(restored, cid, **changes) == first
        assert restored.provider.calls == []
        answer(restored, cid, 1, 0, False)


async def test_postgres_accept_rollback_preserves_memory_and_allows_retry(
    harness: Harness,
    stores: Stores,
) -> None:
    cid = harness.conversation.create("synthetic").conversation_id
    complete(harness, cid)
    old = register_sources(
        harness.records, harness.conversation.store, (SourceReference(cid, 1, 0),)
    )
    complete(
        harness,
        cid,
        request_id="r2",
        expected_revision=1,
        messages=[{"role": "user", "content": "覚えないで"}],
    )
    before = harness.conversation.read("synthetic", cid)
    with stores.database.transaction(BINDING) as db:
        db.execute(
            "CREATE FUNCTION reject_revoke() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'synthetic'; END $$"
        )
        db.execute(
            "CREATE TRIGGER reject_revoke BEFORE UPDATE OF normalized_text ON memory_episodes "
            "FOR EACH ROW EXECUTE FUNCTION reject_revoke()"
        )
    response = harness.http.post(
        f"{BASE}/{cid}/memory-confirmations",
        json={
            "expected_revision": 2,
            "turn_revision": 2,
            "message_index": 0,
            "accept_private_mode": True,
        },
    )
    assert response.status_code >= 400
    assert harness.conversation.read("synthetic", cid) == before
    assert harness.records.current(BINDING, old)
    assert events(stores.records) == ()
    with stores.database.transaction(BINDING) as db:
        row = db.execute("SELECT normalized_text FROM memory_episodes").fetchone()
        assert row is not None and row[0] == old[0].record.normalized_text
        db.execute("DROP TRIGGER reject_revoke ON memory_episodes")
        db.execute("DROP FUNCTION reject_revoke()")
    answer(harness, cid, 2, 0, True, turn_revision=2)
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT normalized_text FROM memory_episodes").fetchall() == [(None,)]
    assert not harness.records.current(BINDING, old)


@pytest.mark.parametrize("timestamp", [None, FIRST])
def test_postgres_v2_migration_keeps_old_dates_receipts_and_does_not_scan_history(
    stores: Stores,
    timestamp: object,
) -> None:
    install_v1(stores)
    with stores.database.transaction(BINDING) as db:
        db.execute("ALTER TABLE turns ADD COLUMN stated_at timestamptz")
        db.execute("UPDATE schema_version SET version=2")
        db.execute(
            "UPDATE turns SET stated_at=%s,messages=%s",
            (
                timestamp,
                '[{"role":"user","content":"覚えないで"},'
                '{"role":"assistant","content":"Synthetic reply"}]',
            ),
        )
    database = PostgresDatabase(stores.config)
    restored = make_harness(PostgresHistory(database), PostgresMemoryRecords(database))
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
    with restored.http:
        snapshot = restored.conversation.read("synthetic", CID)
        assert [m.content for m in snapshot.messages] == ["覚えないで", "Synthetic reply"]
        assert snapshot.memory_sources[0].reference == SourceReference(CID, 1, 0)
        assert snapshot.memory_sources[0].stated_at == timestamp
        assert restored.conversation.store.source_eligible(BINDING, SourceReference(CID, 1, 0))
        receipt = restored.conversation.store.receipt(BINDING, CID, "r1")
        assert receipt is not None and receipt.fingerprint == "original-fingerprint"
