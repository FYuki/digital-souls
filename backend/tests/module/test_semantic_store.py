"""実SQLiteで意味記憶の共通保存・訂正・失効・削除を検証する。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.conversation_history.models import ProcessingTurnInput
from app.memory.episodic.contracts import FiveW, FormationStamp, RecordKind, SourceSpan, What
from app.memory.episodic.privacy import PrivacyReview
from app.memory.episodic.read_repository import EpisodicReadRepository
from app.memory.episodic.repository import EpisodicRepository
from app.memory.episodic.sources import ConversationSourceGuard, InvalidConversationSource
from app.memory.persistence.schema import initialize_persona_memory_schema
from app.memory.semantic.contracts import (
    FormationType, Proposition, SemanticCandidate, SemanticOperation as Op,
    SemanticSource, SemanticStatus as Status,
)
from app.memory.semantic.repository import SemanticConflict, SemanticRepository
from app.memory.semantic.service import SemanticRejected, SemanticStore
from app.runtime_paths import resolve_runtime_paths
from tests.conversation_history_test_support import create_repository

NOW = datetime(2026, 9, 15, tzinfo=UTC)
STAMP = FormationStamp(policy_version="p1", classifier_version="c1", model_id="test",
                       model_digest="test-digest", prompt_version="prompt1")


class Reviewer:
    callback = None
    allowed = True

    def review(self, proposition, source_texts):
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
        return PrivacyReview(self.allowed, "ALLOW" if self.allowed else "SENSITIVE",
                             STAMP if self.allowed else None)


@pytest.fixture
def h(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    paths = resolve_runtime_paths({"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(tmp_path / "data")}, root)
    initialize_persona_memory_schema(paths, root)
    history = create_repository(paths.sqlite_path, now=NOW, uuid_factory=uuid4)
    conversation = history.create_conversation("miori")
    guard = ConversationSourceGuard(paths.sqlite_path, clock=lambda: NOW, retention=timedelta(days=365))
    episodic = EpisodicRepository(paths.persona_memory_sqlite_path)
    reader = EpisodicReadRepository(episodic, guard)
    repo, reviewer = SemanticRepository(paths.persona_memory_sqlite_path), Reviewer()
    store = SemanticStore(repository=repo, source_guard=guard, episode_reader=reader,
                          reviewer=reviewer, clock=lambda: NOW)
    return SimpleNamespace(root=root, paths=paths, history=history, conversation=conversation,
                           repo=repo, store=store, reviewer=reviewer, episodic=episodic)


def source(h, text="私は大阪に住んでいる", *, conversation=None):
    cid = (conversation or h.conversation).conversation_id
    turn = h.history.create_processing_turn("miori", cid, ProcessingTurnInput(text))
    turn = h.history.complete_turn("miori", cid, turn.turn_id, sanitized_assistant_content="わかった")
    with sqlite3.connect(h.paths.sqlite_path) as connection:
        revision = connection.execute("SELECT revision FROM memory_turn_versions WHERE turn_id=?",
                                      (str(turn.turn_id),)).fetchone()[0]
    return SemanticSource(kind="CONVERSATION", source_id=turn.turn_id, revision=revision,
                          conversation_id=cid, span=SourceSpan(source_id=turn.turn_id, revision=revision,
                            role="user", start=0, end=len(text), stated_at=turn.created_at))


def candidate(s, *, value="大阪", predicate="居住地", fixed=False, self_report=True, derived=False):
    return SemanticCandidate(
        formation_type=FormationType.EXPERIENCE_DERIVED if derived else FormationType.DIRECT_EXTRACTION,
        proposition=Proposition(subject="ユーザー", predicate=predicate, value=value,
                                content=f"ユーザーの{predicate}は{value}", self_report=self_report,
                                mutability="FIXED" if fixed else "CHANGEABLE"),
        sources=tuple(s) if isinstance(s, list) else (s,), confidence=0.9,
    )


def save(h, value, *, key=None, target=None, op=Op.NEW):
    return h.store.save(character_id="miori", candidate=value, receipt_key=key or str(uuid4()),
                        operation=op, target_id=target.id if target else None,
                        target_version=target.content_version if target else None)


def test_common_store_cross_thread_and_idempotent_outbox(h):
    first = candidate(source(h))
    a = save(h, first, key="one")
    replay = save(h, first, key="one")
    other = h.history.create_conversation("miori")
    b = save(h, candidate(source(h, "東京へ引っ越した", conversation=other), value="東京"),
             target=a, op=Op.CHANGE)
    with h.repo.read() as tx:
        assert replay.id == a.id
        assert tx.get("miori", a.id).status is Status.HISTORICAL
        assert tx.get("other", a.id) is None
        assert b.proposition.valid_from is None
        assert len(tx.list_records("miori")) == 2
        assert tx.relations("miori")[0].relation is Op.CHANGE
        assert tx.connection.execute("SELECT COUNT(*) FROM memory_index_outbox").fetchone()[0] == 3


def test_explicit_correction_and_fixed_conflict_have_distinct_states(h):
    a = save(h, candidate(source(h, "誕生日は5月10日"), value="5月10日", predicate="誕生日", fixed=True))
    b = save(h, candidate(source(h, "誕生日は5月11日"), value="5月11日", predicate="誕生日", fixed=True),
             target=a, op=Op.CONFLICT)
    with h.repo.read() as tx:
        assert tx.get("miori", a.id).status is Status.CONFLICTED
        assert b.status is Status.CONFLICTED
    c = save(h, candidate(source(h, "5月10日は言い間違い"), value="5月11日", predicate="誕生日", fixed=True),
             target=a, op=Op.CORRECT)
    with h.repo.read() as tx:
        assert tx.get("miori", a.id).status is Status.SUPERSEDED
        assert c.status is Status.ACTIVE


def test_fixed_attribute_cannot_be_silently_changed_and_transaction_rolls_back(h):
    a = save(h, candidate(source(h, "誕生日は5月10日"), predicate="誕生日", fixed=True))
    with pytest.raises(SemanticConflict):
        save(h, candidate(source(h, "誕生日は5月11日"), value="5月11日", predicate="誕生日", fixed=True),
             target=a, op=Op.CHANGE)
    with h.repo.read() as tx:
        assert len(tx.list_records("miori")) == 1
        assert tx.get("miori", a.id).status is Status.ACTIVE


def test_stale_or_cross_character_target_is_rejected(h):
    a = save(h, candidate(source(h)))
    current = save(h, candidate(source(h)), target=a, op=Op.REAFFIRM)
    assert current.content_version == 2
    with pytest.raises(SemanticConflict):
        save(h, candidate(source(h, "東京へ引っ越した"), value="東京"), target=a, op=Op.CHANGE)
    with h.repo.transaction() as tx:
        with pytest.raises(SemanticConflict):
            tx.apply(character_id="other", candidate=candidate(source(h)), stamp=STAMP,
                     receipt_key="cross", target_id=a.id, target_version=2, operation=Op.CORRECT)


def test_assistant_alone_and_invented_source_cannot_be_saved(h):
    user_source = source(h)
    assistant = user_source.model_copy(update={"span": user_source.span.model_copy(update={"role": "assistant"})})
    with pytest.raises(ValidationError):
        candidate(assistant)
    forged = user_source.model_copy(update={"revision": user_source.revision + 1,
                     "span": user_source.span.model_copy(update={"revision": user_source.revision + 1})})
    with pytest.raises(InvalidConversationSource):
        save(h, candidate(forged))
    with h.repo.read() as tx:
        assert tx.list_records("miori") == ()


def test_privacy_rejection_and_source_change_during_review_do_not_commit(h):
    s = source(h)
    h.reviewer.allowed = False
    with pytest.raises(SemanticRejected):
        save(h, candidate(s))
    h.reviewer.allowed = True
    def change():
        with sqlite3.connect(h.paths.sqlite_path) as connection:
            connection.execute("UPDATE conversation_turns SET user_content='訂正' WHERE turn_id=?",
                               (str(s.source_id),))
    h.reviewer.callback = change
    with pytest.raises(InvalidConversationSource):
        save(h, candidate(s))
    with h.repo.read() as tx:
        assert tx.list_records("miori") == ()


def test_source_edit_immediately_invalidates_and_queues_reassessment(h):
    s = source(h)
    a = save(h, candidate(s))
    with sqlite3.connect(h.paths.sqlite_path) as connection:
        connection.execute("UPDATE conversation_turns SET user_content='東京' WHERE turn_id=?", (str(s.source_id),))
    records = h.store.reconcile("miori")
    assert records[0].status is Status.INACTIVE
    with h.repo.read() as tx:
        row = tx.connection.execute("SELECT * FROM semantic_reassessment").fetchone()
        assert row["record_id"] == str(a.id)
        assert row["formation_type"] == "DIRECT_EXTRACTION"


def test_manual_correction_is_only_for_self_report_and_preserves_source_history(h):
    a = save(h, candidate(source(h)))
    replacement = a.proposition.model_copy(update={"value": "東京", "content": "ユーザーの居住地は東京"})
    b = h.store.correct(character_id="miori", record_id=a.id, version=1,
                        proposition=replacement, receipt_id=uuid4())
    assert b.sources[0].kind == "MANUAL"
    with h.repo.read() as tx:
        assert tx.get("miori", a.id).sources == a.sources
        assert tx.get("miori", a.id).status is Status.SUPERSEDED
    world = save(h, candidate(source(h, "このプロジェクトではSQLiteを使う"), self_report=False))
    with pytest.raises(SemanticRejected):
        h.store.correct(character_id="miori", record_id=world.id, version=1,
                        proposition=replacement, receipt_id=uuid4())


def test_delete_wipes_all_versions_and_blocks_same_evidence_but_allows_new_statement(h):
    s = source(h, "私は紅茶が好き")
    original = save(h, candidate(s, value="紅茶", predicate="好み"), key="original")
    h.store.delete(character_id="miori", record_id=original.id, version=1)
    replay = save(h, candidate(s, value="紅茶", predicate="好み"), key="original")
    assert replay.status is Status.DELETED and replay.proposition is None
    with pytest.raises(SemanticRejected):
        save(h, candidate(s, value="紅茶", predicate="好み"))
    fresh = save(h, candidate(source(h, "改めて言うけど紅茶が好き"), value="紅茶", predicate="好み"))
    assert fresh.status is Status.ACTIVE
    assert h.history.get_turn("miori", h.conversation.conversation_id, s.source_id) is not None
    with h.repo.read() as tx:
        assert all(row[0] is None for row in tx.connection.execute(
            "SELECT proposition FROM semantic_versions WHERE record_id=?", (str(original.id),)))
    with h.repo.transaction() as tx:
        with pytest.raises(sqlite3.IntegrityError):
            tx.connection.execute("UPDATE semantic_records SET status='ACTIVE',proposition=? WHERE id=?",
                                  (original.proposition.model_dump_json(), str(original.id)))


def derived(h):
    roots = []
    for text in ("喫茶店でコーヒーを選んだ", "朝にもコーヒーを選んだ"):
        s = source(h, text)
        with h.episodic.transaction(now=NOW) as tx:
            result = tx.create(character_id="miori", conversation_id=s.conversation_id,
                               kind=RecordKind.EPISODE, five_w=FiveW(what=What(predicate="聞いた", object=text)),
                               sources=(s.span,), stamp=STAMP, receipt_id=uuid4())
        roots.append(SemanticSource(kind="EPISODE", source_id=result.record.id, revision=1))
    return roots


def test_derived_contract_coexists_with_self_report_and_checks_episode_validity(h):
    roots = derived(h)
    a = save(h, candidate(roots, predicate="コーヒーの好み", value="好き", derived=True, self_report=False))
    b = save(h, candidate(source(h, "コーヒーは好きではない"), predicate="コーヒーの好み", value="好きではない"),
             target=a, op=Op.SELF_REPORT)
    with h.repo.read() as tx:
        assert tx.get("miori", a.id).status is Status.ACTIVE
        assert b.status is Status.ACTIVE
        assert {r.formation_type for r in tx.list_records("miori")} == set(FormationType)
    with sqlite3.connect(h.paths.persona_memory_sqlite_path) as connection:
        connection.execute("UPDATE episodic_records SET status='INACTIVE' WHERE id=?", (str(roots[0].source_id),))
    records = {r.id: r for r in h.store.reconcile("miori")}
    assert records[a.id].status is Status.INACTIVE
    assert records[b.id].status is Status.ACTIVE


def test_derived_rejects_another_character_or_stale_episode(h):
    roots = derived(h)
    bad = candidate([roots[0].model_copy(update={"revision": 2}), roots[1]],
                    derived=True, self_report=False)
    with pytest.raises(SemanticConflict):
        save(h, bad)
    with pytest.raises(SemanticConflict):
        h.store.save(character_id="other", candidate=candidate(roots, derived=True, self_report=False),
                     receipt_key="foreign")

def test_v5_migration_preserves_records_and_readonly_backup_validation(h):
    from app.backup_restore.sqlite_snapshot import verify_sqlite_database
    from app.memory.semantic.schema import TABLES
    s = source(h)
    with h.episodic.transaction(now=NOW) as tx:
        record = tx.create(character_id="miori", conversation_id=s.conversation_id,
                           kind=RecordKind.EPISODE, five_w=FiveW(what=What(predicate="聞いた")),
                           sources=(s.span,), stamp=STAMP, receipt_id=uuid4()).record
    with sqlite3.connect(h.paths.persona_memory_sqlite_path) as connection:
        for table in TABLES:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=5")
        before = connection.execute("SELECT * FROM episodic_records").fetchall()
    assert verify_sqlite_database(h.paths.persona_memory_sqlite_path, "persona-memory.db").schema_version == 5
    initialize_persona_memory_schema(h.paths, h.root)
    initialize_persona_memory_schema(h.paths, h.root)
    with sqlite3.connect(h.paths.persona_memory_sqlite_path) as connection:
        assert connection.execute("SELECT * FROM episodic_records").fetchall() == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    contents = h.paths.persona_memory_sqlite_path.read_bytes()
    verification = verify_sqlite_database(h.paths.persona_memory_sqlite_path, "persona-memory.db")
    assert verification.record_count == 1
    assert h.paths.persona_memory_sqlite_path.read_bytes() == contents
    with h.episodic.read() as tx:
        assert tx.get("miori", record.id) is not None
