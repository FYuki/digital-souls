"""実SQLiteで、保存と参照・版・削除・再試行をまとめて検証する。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.memory.episodic.contracts import (
    FiveW, FormationStamp, MergeRelation, RecordKind, RecordStatus, SourceSpan, What,
)
from app.memory.episodic.repository import EpisodicRepository, EpisodicTransaction, RecordConflict
from app.memory.persistence.schema import initialize_persona_memory_schema
from app.runtime_paths import resolve_runtime_paths

NOW = datetime(2026, 9, 13, tzinfo=UTC)
THREAD = UUID("77777777-7777-4777-8777-777777777777")
STAMP = FormationStamp(policy_version="p1", classifier_version="c1", model_id="test",
                       model_digest="digest", prompt_version="prompt1")
VALUE = FiveW(what=What(predicate="食べた", object="うどん"))
CORRECTED = FiveW(what=What(predicate="食べた", object="そば"))


def source(*, source_id: UUID | None = None, revision: int = 1) -> SourceSpan:
    return SourceSpan(source_id=source_id or uuid4(), revision=revision, role="user",
                      start=0, end=8, stated_at=NOW)


@pytest.fixture
def repository(tmp_path: Path) -> EpisodicRepository:
    root = tmp_path / "repository"
    root.mkdir()
    paths = resolve_runtime_paths(
        {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(tmp_path / "data")}, root)
    initialize_persona_memory_schema(paths, root)
    return EpisodicRepository(paths.persona_memory_sqlite_path)


def create(tx: EpisodicTransaction, *, kind: RecordKind = RecordKind.FACT,
           character: str = "miori", thread: UUID = THREAD,
           sources: tuple[SourceSpan, ...] | None = None, receipt: UUID | None = None):
    return tx.create(character_id=character, conversation_id=thread, kind=kind,
                     five_w=VALUE, sources=sources or (source(),), stamp=STAMP,
                     receipt_id=receipt or uuid4()).record


def merge(source_id: UUID, target_id: UUID, *, sources: tuple[SourceSpan, ...] | None = None):
    return MergeRelation(id=uuid4(), character_id="miori", source_fact_id=source_id,
                         source_version=1, target_fact_id=target_id, target_version=1,
                         conversation_id=THREAD, policy="full-five-w-v1",
                         evidence=sources or (source(),), valid=True)


def test_update_preserves_ids_history_and_invalidates_old_links_and_merges(repository):
    with repository.transaction(now=NOW) as tx:
        episode = create(tx, kind=RecordKind.EPISODE)
        fact = create(tx)
        duplicate = create(tx)
        link = tx.add_reference(character_id="miori", episode_id=episode.id, episode_version=1,
                                fact_id=fact.id, fact_version=1, sources=(source(),))
        tx.add_merge(merge(duplicate.id, fact.id))
        corrected = tx.update(character_id="miori", record_id=fact.id, expected_version=1,
                              five_w=CORRECTED, sources=(source(),), stamp=STAMP, receipt_id=uuid4()).record
        assert corrected.id == fact.id
        assert corrected.content_version == 2
        assert corrected.five_w == CORRECTED
        assert tx.get("miori", episode.id) == episode
        assert [v.five_w for v in tx.versions("miori", fact.id)] == [VALUE, CORRECTED]
        assert tx.versions("miori", fact.id)[1].stamp == STAMP
        assert not tx.references("miori", episode.id)[0].valid
        assert not tx.merges("miori")[0].valid
        assert tx.representative("miori", duplicate.id).id == duplicate.id
        with pytest.raises(sqlite3.IntegrityError, match="stale"):
            tx.add_reference(character_id="miori", episode_id=episode.id, episode_version=1,
                             fact_id=fact.id, fact_version=1, sources=(source(),))
        new = tx.add_reference(character_id="miori", episode_id=episode.id, episode_version=1,
                               fact_id=fact.id, fact_version=2, sources=(source(),))
        assert new.id != link.id


def test_delete_erases_all_versions_and_replayed_create_cannot_revive(repository):
    receipt = uuid4()
    with repository.transaction() as tx:
        fact = create(tx, receipt=receipt)
        tx.update(character_id="miori", record_id=fact.id, expected_version=1,
                  five_w=CORRECTED, sources=(source(),), stamp=STAMP, receipt_id=uuid4())
        delete_receipt = uuid4()
        deleted = tx.delete(character_id="miori", record_id=fact.id, expected_version=2,
                            sources=(source(),), receipt_id=delete_receipt)
        assert deleted.record.status is RecordStatus.DELETED
        assert all(v.five_w is None for v in tx.versions("miori", fact.id))
        replay = tx.create(character_id="miori", conversation_id=THREAD, kind=RecordKind.FACT,
                           five_w=VALUE, sources=(source(),), stamp=STAMP, receipt_id=receipt)
        assert replay.replayed
        assert replay.record.five_w is None
        assert replay.record.status is RecordStatus.DELETED
        assert tx.delete(character_id="miori", record_id=fact.id, expected_version=2,
                         sources=(source(),), receipt_id=delete_receipt).replayed
        with pytest.raises(RecordConflict):
            tx.update(character_id="miori", record_id=fact.id, expected_version=3,
                      five_w=VALUE, sources=(source(),), stamp=STAMP, receipt_id=uuid4())
    repository.truncate_wal()
    path = repository._database._database_path
    assert "うどん".encode() not in path.read_bytes()
    assert "そば".encode() not in path.read_bytes()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT operation FROM memory_index_outbox ORDER BY rowid").fetchall() == [
            ("UPSERT",), ("UPSERT",), ("DELETE",)]


def test_stale_update_rolls_back_other_writes_in_same_registration(repository):
    with repository.transaction() as tx:
        fact = create(tx)
    with pytest.raises(RecordConflict):
        with repository.transaction() as tx:
            create(tx, kind=RecordKind.EPISODE)
            tx.update(character_id="miori", record_id=fact.id, expected_version=7,
                      five_w=CORRECTED, sources=(source(),), stamp=STAMP, receipt_id=uuid4())
    with repository.read() as tx:
        assert tx.list_records("miori") == (fact,)


def test_source_invalidation_uses_current_version_and_relation_evidence(repository):
    old_source = source()
    evidence_source = source()
    with repository.transaction() as tx:
        old = create(tx, sources=(old_source,))
        updated = create(tx, sources=(old_source,))
        tx.update(character_id="miori", record_id=updated.id, expected_version=1,
                  five_w=CORRECTED, sources=(source(),), stamp=STAMP, receipt_id=uuid4())
        episode = create(tx, kind=RecordKind.EPISODE)
        linked = create(tx)
        tx.add_reference(character_id="miori", episode_id=episode.id, episode_version=1,
                         fact_id=linked.id, fact_version=1, sources=(evidence_source,))
        assert tx.invalidate_source("miori", old_source.source_id, 2) == ()
        assert tx.invalidate_source("other", old_source.source_id, 1) == ()
        assert tx.invalidate_source("miori", old_source.source_id, 1) == (old.id,)
        assert tx.get("miori", old.id).status is RecordStatus.INACTIVE
        assert tx.get("miori", updated.id).status is RecordStatus.ACTIVE
        assert tx.invalidate_source("miori", evidence_source.source_id, 1) == ()
        assert not tx.references("miori", episode.id)[0].valid
        assert tx.get("miori", episode.id).status is RecordStatus.ACTIVE


@pytest.mark.parametrize("bad_target", ["character", "thread", "kind"])
def test_merge_rejects_cross_scope_and_wrong_record_kind(repository, bad_target):
    with repository.transaction() as tx:
        left = create(tx)
        target = create(tx, character="other" if bad_target == "character" else "miori",
                        thread=uuid4() if bad_target == "thread" else THREAD,
                        kind=RecordKind.EPISODE if bad_target == "kind" else RecordKind.FACT)
        with pytest.raises(sqlite3.IntegrityError):
            tx.add_merge(merge(left.id, target.id))


def test_merge_chain_rejects_cycle_and_multiple_targets(repository):
    with repository.transaction() as tx:
        first, second, third = create(tx), create(tx), create(tx)
        tx.add_merge(merge(first.id, second.id))
        tx.add_merge(merge(second.id, third.id))
        assert tx.representative("miori", first.id).id == third.id
        assert tx.representative("other", first.id) is None
        with pytest.raises(sqlite3.IntegrityError, match="cyclic"):
            tx.add_merge(merge(third.id, first.id))
        with pytest.raises(sqlite3.IntegrityError):
            tx.add_merge(merge(first.id, third.id))


def test_link_idempotence_and_new_relation_after_evidence_reevaluation(repository):
    evidence = (source(),)
    with repository.transaction() as tx:
        episode, fact = create(tx, kind=RecordKind.EPISODE), create(tx)
        args = dict(character_id="miori", episode_id=episode.id, episode_version=1,
                    fact_id=fact.id, fact_version=1, sources=evidence)
        first = tx.add_reference(**args)
        assert tx.add_reference(**args).id == first.id
        tx.invalidate_source("miori", evidence[0].source_id, 1)
        with pytest.raises(RecordConflict, match="source revision"):
            tx.add_reference(**args)
        args["sources"] = (source(),)
        next_link = tx.add_reference(**args)
        assert next_link.id != first.id
        assert [link.valid for link in tx.references("miori", episode.id)].count(True) == 1


def test_sql_cannot_retarget_or_revive_relations(repository):
    with repository.transaction() as tx:
        episode, fact, other = create(tx, kind=RecordKind.EPISODE), create(tx), create(tx)
        link = tx.add_reference(character_id="miori", episode_id=episode.id, episode_version=1,
                                fact_id=fact.id, fact_version=1, sources=(source(),))
        relation = merge(fact.id, other.id)
        tx.add_merge(relation)
        db = tx._connection
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE episodic_links SET fact_id = ? WHERE id = ?", (str(other.id), str(link.id)))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE episodic_merges SET target_version = 2 WHERE id = ?", (str(relation.id),))
        db.execute("UPDATE episodic_links SET valid = 0 WHERE id = ?", (str(link.id),))
        with pytest.raises(sqlite3.IntegrityError, match="revived"):
            db.execute("UPDATE episodic_links SET valid = 1 WHERE id = ?", (str(link.id),))


def test_concurrent_create_retry_has_one_record_receipt_and_outbox(repository):
    receipt = uuid4()
    evidence = (source(),)
    def run(_):
        with repository.transaction() as tx:
            return create(tx, receipt=receipt, sources=evidence).id
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = tuple(pool.map(run, range(12)))
    assert len(set(ids)) == 1
    with repository.read() as tx:
        assert len(tx.list_records("miori")) == 1
        assert tx._connection.execute("SELECT COUNT(*) FROM episodic_receipts").fetchone()[0] == 1
        assert tx._connection.execute("SELECT COUNT(*) FROM memory_index_outbox").fetchone()[0] == 1


def test_receipt_cannot_be_reused_for_different_target_or_operation(repository):
    receipt = uuid4()
    with repository.transaction() as tx:
        fact = create(tx, receipt=receipt)
        with pytest.raises(RecordConflict, match="another operation"):
            tx.update(character_id="miori", record_id=fact.id, expected_version=1,
                      five_w=CORRECTED, sources=(source(),), stamp=STAMP, receipt_id=receipt)
        with pytest.raises(RecordConflict, match="another scope"):
            create(tx, receipt=receipt, thread=uuid4())


def test_read_transaction_refuses_mutation(repository):
    with repository.read() as tx:
        with pytest.raises(ValueError, match="read-only"):
            create(tx)


def test_invalidated_source_blocks_new_receipt_but_not_new_source_revision(repository):
    old = source()
    with repository.transaction() as tx:
        tx.invalidate_source("miori", old.source_id, 1)
        with pytest.raises(RecordConflict, match="source revision"):
            create(tx, sources=(old,))
        assert tx.list_records("miori") == ()
        fresh = source(source_id=old.source_id, revision=2)
        assert create(tx, sources=(fresh,)).status is RecordStatus.ACTIVE
