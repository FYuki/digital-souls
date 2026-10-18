import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from threading import Barrier, Event
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import Connection, sql

from digital_souls_core import postgres_history, postgres_schema
from digital_souls_core.application import CoreError
from digital_souls_core.character import AccessScope
from digital_souls_core.contracts import Message
from digital_souls_core.history import Binding, ConversationControls, SourceReference
from digital_souls_core.postgres_db import PostgresConfig, PostgresDatabase
from digital_souls_core.postgres_history import PostgresHistory
from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

from .postgres_record_support import events, register_sources

pytestmark = pytest.mark.postgres
BINDING = Binding(AccessScope(), "synthetic")
MESSAGES = (
    Message(role="user", content="I like synthetic tea."),
    Message(role="assistant", content="Synthetic answer."),
)


@contextmanager
def raw_connection(config: PostgresConfig) -> Iterator[Connection[tuple[Any, ...]]]:
    with psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.database,
        user=config.user,
        password="",
        passfile="/dev/null/no-passfile",
        connect_timeout=5,
        sslmode="disable",
        gssencmode="disable",
        autocommit=True,
    ) as connection:
        yield connection


@dataclass
class Stores:
    config: PostgresConfig
    database: PostgresDatabase
    history: PostgresHistory
    records: PostgresMemoryRecords


@pytest.fixture
def stores() -> Iterator[Stores]:
    names = (
        "DSC_TEST_POSTGRES_SOCKET",
        "DSC_TEST_POSTGRES_PORT",
        "DSC_TEST_POSTGRES_DATABASE",
        "DSC_TEST_POSTGRES_USER",
    )
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.fail("PostgreSQL synthetic test configuration is required: " + ", ".join(missing))
    config = PostgresConfig(
        host=os.environ[names[0]],
        port=int(os.environ[names[1]]),
        database=os.environ[names[2]],
        user=os.environ[names[3]],
        schema_name="dsc_test_" + uuid4().hex,
    )
    assert config.host.startswith("/"), "PostgreSQL tests require a local Unix socket"
    database = PostgresDatabase(config)
    try:
        history = PostgresHistory(database)
        records = PostgresMemoryRecords(database)
        yield Stores(config, database, history, records)
    finally:
        # The only dropped object is this fixture's freshly generated schema.
        with raw_connection(config) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(config.schema_name)
                )
            )


def seed(stores: Stores, text: str = "I like synthetic tea.") -> SourceReference:
    conversation = stores.history.create(BINDING)
    stores.history.append(
        BINDING,
        conversation.conversation_id,
        "synthetic-request",
        "synthetic-fingerprint",
        0,
        (Message(role="user", content=text), MESSAGES[-1]),
        "stop",
    )
    return SourceReference(conversation.conversation_id, 1, 0)


def revoke(history: PostgresHistory, ref: SourceReference, action: str) -> None:
    if action == "delete":
        history.delete(BINDING, ref.conversation_id)
    else:
        history.controls(
            BINDING,
            ref.conversation_id,
            ConversationControls(expected_revision=1, private_mode=True),
        )


def test_history_order_retry_cas_reopen_and_delete(stores: Stores) -> None:
    history = stores.history
    first, second = history.create(BINDING), history.create(BINDING)
    assert history.list(BINDING) == [first.conversation_id, second.conversation_id]
    receipt = history.append(BINDING, first.conversation_id, "r1", "fp1", 0, MESSAGES, "stop")
    history = PostgresHistory(PostgresDatabase(stores.config))
    assert (
        history.append(BINDING, first.conversation_id, "r1", "fp1", 0, MESSAGES, "stop") == receipt
    )
    assert history.receipt(BINDING, first.conversation_id, "r1") == receipt
    for request, fingerprint, code in (
        ("r1", "different", "request_conflict"),
        ("r2", "fp2", "revision_conflict"),
    ):
        with pytest.raises(CoreError) as error:
            history.append(
                BINDING, first.conversation_id, request, fingerprint, 0, MESSAGES, "stop"
            )
        assert error.value.code == code
    history.append(BINDING, first.conversation_id, "r2", "fp2", 1, MESSAGES, "stop")
    snapshot = history.read(BINDING, first.conversation_id)
    assert snapshot.revision == 2 and snapshot.messages == MESSAGES * 2
    history.delete(BINDING, first.conversation_id)
    history.delete(BINDING, first.conversation_id)
    assert history.receipt(BINDING, first.conversation_id, "r1") is None
    assert history.list(BINDING) == [second.conversation_id]
    with pytest.raises(CoreError) as error:
        history.read(BINDING, first.conversation_id)
    assert error.value.code == "conversation_not_found"
    deletions = history.deletions(BINDING)
    assert len(deletions) == 1
    assert deletions[0].conversation_id == first.conversation_id
    assert deletions[0].through_revision == 2
    history.acknowledge_deletion(BINDING, deletions[0].event_id)
    history.acknowledge_deletion(BINDING, deletions[0].event_id)
    assert history.deletions(BINDING) == ()


@pytest.mark.parametrize("excluded", [(0, 0), (1,), (-1,), (True,), (99,)])
def test_invalid_exclusion_rolls_back_revision_and_turn(
    stores: Stores, excluded: tuple[int, ...]
) -> None:
    conversation = stores.history.create(BINDING)
    with pytest.raises(CoreError) as error:
        stores.history.append(
            BINDING,
            conversation.conversation_id,
            "r1",
            "fp1",
            0,
            MESSAGES,
            "stop",
            memory_excluded_indices=excluded,
        )
    assert error.value.code == "invalid_exclusions"
    assert stores.history.read(BINDING, conversation.conversation_id) == conversation
    assert stores.history.receipt(BINDING, conversation.conversation_id, "r1") is None


def test_private_turn_and_explicit_exclusions_remain_ineligible_after_reopening(
    stores: Stores,
) -> None:
    history = stores.history
    conversation = history.create(BINDING).conversation_id
    history.append(
        BINDING, conversation, "r1", "fp1", 0, MESSAGES, "stop", memory_excluded_indices=(0,)
    )
    assert not any(source.eligible for source in history.read(BINDING, conversation).memory_sources)
    history.controls(
        BINDING, conversation, ConversationControls(expected_revision=1, private_mode=True)
    )
    history.append(BINDING, conversation, "r2", "fp2", 2, MESSAGES, "stop")
    history.controls(
        BINDING, conversation, ConversationControls(expected_revision=3, private_mode=False)
    )
    history.append(BINDING, conversation, "r3", "fp3", 4, MESSAGES, "stop")
    snapshot = history.read(BINDING, conversation)
    assert snapshot.revision == 5
    assert [source.eligible for source in snapshot.memory_sources] == [False] * 4 + [True, False]
    for ref in (SourceReference(conversation, 1, 0), SourceReference(conversation, 3, 0)):
        assert not history.source_eligible(BINDING, ref)
        with pytest.raises(CoreError):
            register_sources(stores.records, history, (ref,))
    assert history.source_eligible(BINDING, SourceReference(conversation, 5, 0))


def test_archive_controls_and_stale_cas_preserve_memory(stores: Stores) -> None:
    ref = seed(stores)
    memories = register_sources(stores.records, stores.history, (ref,))
    with pytest.raises(CoreError) as error:
        stores.history.controls(
            BINDING,
            ref.conversation_id,
            ConversationControls(expected_revision=0, private_mode=True),
        )
    assert error.value.code == "revision_conflict"
    assert events(stores.records) == ()
    updated = stores.history.controls(
        BINDING, ref.conversation_id, ConversationControls(expected_revision=1, archived=True)
    )
    assert updated.archived and updated.revision == 2
    assert stores.history.list(BINDING) == []
    assert stores.history.list(BINDING, include_archived=True) == [ref.conversation_id]
    assert stores.records.retrievable(BINDING) == memories


@pytest.mark.parametrize(
    "other",
    [
        Binding(AccessScope(subject="other"), "synthetic"),
        Binding(AccessScope(client="other"), "synthetic"),
        Binding(AccessScope(audience="other"), "synthetic"),  # type: ignore[arg-type]
        replace(BINDING, character_id="other"),
    ],
)
def test_scope_isolation_covers_history_records_and_outboxes(
    stores: Stores, other: Binding
) -> None:
    ref = seed(stores)
    memories = register_sources(stores.records, stores.history, (ref,))
    assert stores.history.list(other) == []
    assert stores.history.receipt(other, ref.conversation_id, "synthetic-request") is None
    assert not stores.history.source_eligible(other, ref)
    with pytest.raises(CoreError):
        stores.history.read(other, ref.conversation_id)
    with pytest.raises(CoreError):
        stores.history.append(other, ref.conversation_id, "r2", "fp2", 1, MESSAGES, "stop")
    assert stores.records.retrievable(other) == ()
    assert not stores.records.current(other, memories)
    stores.history.delete(other, ref.conversation_id)
    assert stores.history.read(BINDING, ref.conversation_id).revision == 1
    stores.history.delete(BINDING, ref.conversation_id)
    assert len(events(stores.records)) == 1
    deletion = stores.history.deletions(BINDING)[0]
    stores.history.acknowledge_deletion(other, deletion.event_id)
    assert stores.history.deletions(BINDING) == (deletion,)
    assert stores.history.deletions(other) == ()


@pytest.mark.parametrize("role", ["assistant", "tool"])
def test_non_user_source_is_not_memory_evidence(stores: Stores, role: str) -> None:
    conversation = stores.history.create(BINDING).conversation_id
    message = (
        Message(role="assistant", content="Synthetic suggestion")
        if role == "assistant"
        else Message(role="tool", content="Synthetic tool output", tool_call_id="synthetic-call")
    )
    stores.history.append(BINDING, conversation, "r1", "fp1", 0, (message, MESSAGES[-1]), "stop")
    with pytest.raises(CoreError) as error:
        register_sources(stores.records, stores.history, (SourceReference(conversation, 1, 0),))
    assert error.value.code == "memory_source_invalid"


def test_unknown_schema_version_is_preserved_and_rejected(stores: Stores) -> None:
    ref = seed(stores)
    with stores.database.transaction(BINDING) as connection:
        connection.execute("UPDATE schema_version SET version=99")
    with pytest.raises(CoreError) as error:
        PostgresDatabase(stores.config).initialize()
    assert error.value.code == "storage_schema"
    assert stores.history.read(BINDING, ref.conversation_id).messages == MESSAGES
    with stores.database.transaction(BINDING) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(99,)]


@pytest.mark.parametrize(
    "alteration",
    [
        "ALTER TABLE turns DROP CONSTRAINT turns_binding_conversation_fkey",
        "ALTER TABLE turns DROP CONSTRAINT turns_binding_conversation_revision_key",
        "ALTER TABLE memory_episodes ALTER COLUMN normalized_text TYPE varchar",
        "ALTER TABLE conversations ALTER COLUMN private_mode SET DEFAULT true",
        "ALTER TABLE turns ALTER COLUMN messages DROP NOT NULL",
        "CREATE VIEW unexpected_view AS SELECT 1 AS value",
        "DROP TABLE source_deletions",
        "ALTER TABLE memory_episodes ADD COLUMN unexpected text",
    ],
)
def test_changed_schema_contract_is_rejected_without_rewriting_data(
    stores: Stores, alteration: str
) -> None:
    ref = seed(stores)
    with stores.database.transaction(BINDING) as connection:
        connection.execute(alteration)
    with pytest.raises(CoreError) as error:
        PostgresDatabase(stores.config).initialize()
    assert error.value.code == "storage_schema"
    assert stores.history.read(BINDING, ref.conversation_id).messages == MESSAGES


@pytest.mark.parametrize("action", ["private", "delete"])
def test_failed_revocation_rolls_back_history_memory_and_outboxes_together(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    ref = seed(stores)
    memories = register_sources(stores.records, stores.history, (ref,))
    original_revoke = postgres_schema.revoke

    def abort_after_revoke(
        connection: Connection[tuple[Any, ...]],
        binding: str,
        conversation: str,
        epoch: int,
        kind: str,
    ) -> None:
        original_revoke(connection, binding, conversation, epoch, kind)
        raise RuntimeError("Synthetic transaction interruption")

    monkeypatch.setattr(postgres_history, "revoke", abort_after_revoke)
    with pytest.raises(RuntimeError, match="Synthetic transaction interruption"):
        revoke(stores.history, ref, action)
    snapshot = stores.history.read(BINDING, ref.conversation_id)
    assert snapshot.revision == 1 and not snapshot.private_mode
    assert stores.history.source_eligible(BINDING, ref)
    assert stores.records.retrievable(BINDING) == memories
    assert stores.records.current(BINDING, memories)
    assert events(stores.records) == ()
    assert stores.history.deletions(BINDING) == ()


def test_concurrent_revision_cas_commits_exactly_one_turn(stores: Stores) -> None:
    conversation = stores.history.create(BINDING).conversation_id
    barrier = Barrier(2)
    histories = [PostgresHistory(PostgresDatabase(stores.config)) for _ in range(2)]

    def append(index: int) -> str:
        barrier.wait(timeout=3)
        try:
            histories[index].append(
                BINDING, conversation, f"r{index}", f"fp{index}", 0, MESSAGES, "stop"
            )
            return "committed"
        except CoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append, range(2)))
    assert sorted(results) == ["committed", "revision_conflict"]
    snapshot = stores.history.read(BINDING, conversation)
    assert snapshot.revision == 1 and snapshot.messages == MESSAGES


@pytest.mark.parametrize("action", ["private", "delete"])
@pytest.mark.parametrize("first", ["commit", "revoke"])
def test_concurrent_revocation_and_commit_serialize_and_leave_no_visible_memory(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, action: str, first: str
) -> None:
    ref = seed(stores)
    from digital_souls_core.memory_record_store import RecordBatch

    from .postgres_record_support import source_episode

    selected = source_episode(stores.history, (ref,))
    commit_db, revoke_db = PostgresDatabase(stores.config), PostgresDatabase(stores.config)
    records, history = PostgresMemoryRecords(commit_db), PostgresHistory(revoke_db)
    first_db, second_db = (commit_db, revoke_db) if first == "commit" else (revoke_db, commit_db)
    entered, release, connected = Event(), Event(), Event()
    backend_pids: list[int] = []
    original_transaction = first_db.transaction
    original_connect = second_db._connect

    @contextmanager
    def hold_after_lock(binding: Binding) -> Iterator[Connection[tuple[Any, ...]]]:
        with original_transaction(binding) as connection:
            entered.set()
            assert release.wait(3), "Synthetic transaction gate was not released"
            yield connection

    @contextmanager
    def observe_second_connection() -> Iterator[Connection[tuple[Any, ...]]]:
        with original_connect() as connection:
            row = connection.execute("SELECT pg_backend_pid()").fetchone()
            assert row is not None
            backend_pids.append(row[0])
            connected.set()
            yield connection

    monkeypatch.setattr(first_db, "transaction", hold_after_lock)
    monkeypatch.setattr(second_db, "_connect", observe_second_connection)

    def commit() -> str:
        try:
            records.register(BINDING, RecordBatch(episodes=(selected,)), "fixture-v1")
            return "committed"
        except CoreError as error:
            return error.code

    def revoke_source() -> str:
        revoke(history, ref, action)
        return "revoked"

    operations: dict[str, Callable[[], str]] = {"commit": commit, "revoke": revoke_source}
    with ThreadPoolExecutor(max_workers=2) as executor:
        leading = executor.submit(operations[first])
        try:
            assert entered.wait(3)
            following = executor.submit(operations["revoke" if first == "commit" else "commit"])
            assert connected.wait(3)
            # Observe a real second backend waiting for an advisory lock, rather
            # than inferring serialization from Python thread scheduling.
            with raw_connection(stores.config) as observer:
                deadline = time.monotonic() + 2
                while True:
                    waiting = observer.execute(
                        "SELECT 1 FROM pg_locks WHERE pid=%s "
                        "AND locktype='advisory' AND NOT granted",
                        (backend_pids[0],),
                    ).fetchone()
                    if waiting is not None:
                        break
                    assert time.monotonic() < deadline, "Second backend did not wait on scope lock"
                    Event().wait(0.01)
            assert not following.done()
        finally:
            release.set()
        outcomes = {leading.result(timeout=5), following.result(timeout=5)}
    assert outcomes == {"revoked", "committed" if first == "commit" else "memory_source_invalid"}
    assert stores.records.retrievable(BINDING) == ()
    with stores.database.transaction(BINDING) as connection:
        assert (
            connection.execute(
                "SELECT normalized_text FROM memory_episodes WHERE normalized_text IS NOT NULL"
            ).fetchall()
            == []
        )
    assert len(events(stores.records)) == 1
    if action == "delete":
        assert len(stores.history.deletions(BINDING)) == 1
