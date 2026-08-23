from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.unit.test_approved_memory_repository import (
    _candidate,
    _context,
    _repository,
    _rows,
)


CONSOLIDATED_AT = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)


def _save_pair(repository):
    first = repository.save(
        character_id="miori",
        candidate=_candidate("ユーザーは紅茶を好む"),
        context=replace(_context(), idempotency_key="source-1"),
    )
    second = repository.save(
        character_id="miori",
        candidate=_candidate("紅茶が好き"),
        context=replace(_context(), idempotency_key="source-2"),
    )
    return first, second


def _snapshot(repository, memory_id):
    from app.memory.persistence.contracts import ConsolidationInputSnapshot

    detail = repository.get_detail(
        character_id="miori", provider_id="core", memory_id=memory_id
    )
    assert detail is not None
    return ConsolidationInputSnapshot(
        memory_id=detail.memory.id,
        content_version=detail.memory.content_version,
        sources=detail.sources,
        lineage=detail.lineage,
    )


def _consolidation_context(
    first_id,
    second_id,
    *,
    key="consolidation-key",
    relation_name="CONSOLIDATED_FROM",
):
    from app.memory.persistence.contracts import (
        FormationMethod,
        MemoryLineageInput,
        MemoryLineageRelation,
        MemorySourceInput,
        MemorySourceType,
    )

    return replace(
        _context(),
        formation_method=FormationMethod.CONSOLIDATED,
        idempotency_key=key,
        prompt_version="consolidation-v1",
        sources=tuple(
            MemorySourceInput(
                source_type=MemorySourceType.CONSOLIDATION,
                source_provider_id="core",
                source_ref=str(memory_id),
            )
            for memory_id in (first_id, second_id)
        ),
        lineage=tuple(
            MemoryLineageInput(
                related_memory_id=memory_id,
                relation=MemoryLineageRelation(relation_name),
            )
            for memory_id in (first_id, second_id)
        ),
    )


@pytest.mark.parametrize(
    ("operation_name", "relation_name"),
    (("MERGE", "CONSOLIDATED_FROM"), ("SUPERSEDE", "SUPERSEDES")),
)
def test_content_change_atomically_creates_consolidated_memory_and_deactivates_sources(
    tmp_path: Path,
    operation_name: str,
    relation_name: str,
) -> None:
    from app.memory.persistence.contracts import ConsolidationOperation

    repository, database_path = _repository(tmp_path)
    first, second = _save_pair(repository)

    merged = repository.apply_consolidation(
        character_id="miori",
        operation=ConsolidationOperation(operation_name),
        inputs=(_snapshot(repository, first.id), _snapshot(repository, second.id)),
        candidate=_candidate("ユーザーは紅茶を好む"),
        context=_consolidation_context(
            first.id,
            second.id,
            relation_name=relation_name,
        ),
        canonical_memory_id=None,
        consolidated_at=CONSOLIDATED_AT,
    )

    memory_rows = _rows(database_path, "approved_memories")
    by_id = {row["id"]: row for row in memory_rows}
    assert by_id[str(merged.id)]["formation_method"] == "CONSOLIDATED"
    assert by_id[str(merged.id)]["prompt_version"] == "consolidation-v1"
    assert by_id[str(first.id)]["status"] == "INACTIVE"
    assert by_id[str(second.id)]["status"] == "INACTIVE"
    assert by_id[str(first.id)]["last_consolidated_at"] == "2026-08-18T13:00:00.000000Z"
    source_rows = [
        row
        for row in _rows(database_path, "memory_sources")
        if row["memory_id"] == str(merged.id)
    ]
    assert {
        (row["source_type"], row["source_provider_id"], row["source_ref"])
        for row in source_rows
    } == {
        ("CONSOLIDATION", "core", str(first.id)),
        ("CONSOLIDATION", "core", str(second.id)),
    }
    lineage_rows = [
        row
        for row in _rows(database_path, "memory_lineage")
        if row["memory_id"] == str(merged.id)
    ]
    assert {(row["related_memory_id"], row["relation"]) for row in lineage_rows} == {
        (str(first.id), relation_name),
        (str(second.id), relation_name),
    }
    new_outbox = _rows(database_path, "memory_index_outbox")[2:]
    assert {
        (row["memory_id"], row["operation"], row["status"]) for row in new_outbox
    } == {
        (str(merged.id), "UPSERT", "PENDING"),
        (str(first.id), "UPSERT", "PENDING"),
        (str(second.id), "UPSERT", "PENDING"),
    }


def test_consolidation_rolls_back_every_write_when_old_memory_outbox_fails(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import ConsolidationOperation

    repository, database_path = _repository(tmp_path)
    first, second = _save_pair(repository)
    before = {
        table: [dict(row) for row in _rows(database_path, table)]
        for table in (
            "approved_memories",
            "memory_sources",
            "memory_lineage",
            "memory_write_receipts",
            "memory_index_outbox",
        )
    }
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_old_consolidation_outbox "
            "BEFORE INSERT ON memory_index_outbox "
            f"WHEN NEW.memory_id = '{first.id}' "
            "BEGIN SELECT RAISE(ABORT, 'outbox rejected'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        repository.apply_consolidation(
            character_id="miori",
            operation=ConsolidationOperation.MERGE,
            inputs=(_snapshot(repository, first.id), _snapshot(repository, second.id)),
            candidate=_candidate("統合後本文"),
            context=_consolidation_context(first.id, second.id),
            canonical_memory_id=None,
            consolidated_at=CONSOLIDATED_AT,
        )

    after = {
        table: [dict(row) for row in _rows(database_path, table)] for table in before
    }
    assert after == before


def test_consolidation_revalidates_content_version_inside_transaction(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import (
        ConsolidationConflictError,
        ConsolidationOperation,
    )

    repository, database_path = _repository(tmp_path)
    first, second = _save_pair(repository)
    snapshots = (_snapshot(repository, first.id), _snapshot(repository, second.id))
    repository.correct(
        character_id="miori",
        memory_id=first.id,
        candidate=_candidate("モデル実行中に訂正された本文"),
        context=replace(_context(), idempotency_key="concurrent-correction"),
    )
    before = [dict(row) for row in _rows(database_path, "approved_memories")]

    with pytest.raises(ConsolidationConflictError):
        repository.apply_consolidation(
            character_id="miori",
            operation=ConsolidationOperation.MERGE,
            inputs=snapshots,
            candidate=_candidate("古いsnapshotに基づく統合"),
            context=_consolidation_context(first.id, second.id),
            canonical_memory_id=None,
            consolidated_at=CONSOLIDATED_AT,
        )

    assert [dict(row) for row in _rows(database_path, "approved_memories")] == before
    assert len(_rows(database_path, "memory_write_receipts")) == 3


def test_keep_updates_only_consolidation_recency_without_creating_outbox(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import ConsolidationOperation

    repository, database_path = _repository(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    before = dict(_rows(database_path, "approved_memories")[0])
    outbox_before = [dict(row) for row in _rows(database_path, "memory_index_outbox")]

    kept = repository.apply_consolidation(
        character_id="miori",
        operation=ConsolidationOperation.KEEP,
        inputs=(_snapshot(repository, memory.id),),
        candidate=None,
        context=None,
        canonical_memory_id=memory.id,
        consolidated_at=CONSOLIDATED_AT,
    )

    after = dict(_rows(database_path, "approved_memories")[0])
    changed_columns = {name for name in before if before[name] != after[name]}
    assert kept.id == memory.id
    assert changed_columns == {"last_consolidated_at"}
    assert [dict(row) for row in _rows(database_path, "memory_index_outbox")] == (
        outbox_before
    )


def test_exact_duplicate_delete_is_idempotent_and_only_deletes_noncanonical_rows(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import ConsolidationOperation

    repository, database_path = _repository(tmp_path)
    first, second = _save_pair(repository)
    snapshots = (_snapshot(repository, first.id), _snapshot(repository, second.id))
    context = _consolidation_context(first.id, second.id, key="duplicate-key")

    results = [
        repository.apply_consolidation(
            character_id="miori",
            operation=ConsolidationOperation.DELETE_EXACT_DUPLICATE,
            inputs=snapshots,
            candidate=None,
            context=context,
            canonical_memory_id=first.id,
            consolidated_at=CONSOLIDATED_AT,
        )
        for _ in range(2)
    ]

    assert [result.id for result in results] == [first.id, first.id]
    assert repository.get(character_id="miori", memory_id=first.id) is not None
    assert repository.get(character_id="miori", memory_id=second.id) is None
    delete_rows = [
        row
        for row in _rows(database_path, "memory_index_outbox")
        if row["operation"] == "DELETE"
    ]
    assert [(row["memory_id"], row["status"]) for row in delete_rows] == [
        (str(second.id), "PENDING")
    ]
    receipt_rows = [
        row
        for row in _rows(database_path, "memory_write_receipts")
        if row["idempotency_key"] == "duplicate-key"
    ]
    assert [(row["memory_id"], row["operation"]) for row in receipt_rows] == [
        (str(first.id), "SAVE")
    ]


def test_inactive_source_upserts_remove_old_entries_through_index_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory.persistence.contracts import ConsolidationOperation
    from tests.unit.test_memory_index_sync import _sync

    sync, repository, _database_path, records, deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    first, second = _save_pair(repository)
    sync.run_worker_once()
    assert {memory_id for character, memory_id in records if character == "miori"} == {
        str(first.id),
        str(second.id),
    }

    merged = repository.apply_consolidation(
        character_id="miori",
        operation=ConsolidationOperation.MERGE,
        inputs=(_snapshot(repository, first.id), _snapshot(repository, second.id)),
        candidate=_candidate("統合後本文"),
        context=_consolidation_context(first.id, second.id),
        canonical_memory_id=None,
        consolidated_at=CONSOLIDATED_AT,
    )
    sync.run_worker_once()

    assert {memory_id for character, memory_id in records if character == "miori"} == {
        str(merged.id)
    }
    assert set(deleted) >= {
        ("miori", str(first.id)),
        ("miori", str(second.id)),
    }
