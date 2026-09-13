"""回答を経由する旧内容の復活と、版・characterの境界を実SQLiteで検証する。"""

import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.memory.episodic.contracts import FiveW, FormationStamp, RecordKind, SourceSpan, What
from app.memory.episodic.repository import EpisodicRepository, RecordConflict
from app.memory.episodic.response_provenance import record_response
from app.memory.persistence.schema import initialize_persona_memory_schema
from app.runtime_paths import resolve_runtime_paths

NOW = datetime(2026, 9, 13, tzinfo=UTC)
STAMP = FormationStamp(policy_version="p", classifier_version="c", model_id="test",
                       model_digest="test-digest", prompt_version="test-prompt")


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    paths = resolve_runtime_paths(
        {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(tmp_path / "data")}, root)
    initialize_persona_memory_schema(paths, root)
    return EpisodicRepository(paths.persona_memory_sqlite_path), paths, root


def span(turn=None, role="user"):
    return SourceSpan(source_id=turn or uuid4(), revision=1, role=role, start=0, end=4, stated_at=NOW)


def create(tx, *, source=None, thread=None, kind=RecordKind.FACT, character="miori"):
    return tx.create(character_id=character, conversation_id=thread or uuid4(), kind=kind,
                     five_w=FiveW(what=What(predicate="食べた", object="うどん")),
                     sources=(source or span(),), stamp=STAMP, receipt_id=uuid4()).record


def record(tx, fact, turn, thread, *, parents=()):
    record_response(tx._connection, character_id="miori", conversation_id=thread,
                    turn_id=turn, references=() if fact is None else ((fact.id, fact.content_version),),
                    history_turn_ids=parents, created_at=NOW.isoformat())


def correct(tx, fact):
    return tx.update(character_id="miori", record_id=fact.id, expected_version=fact.content_version,
                     five_w=FiveW(what=What(predicate="食べた", object="そば")),
                     sources=(span(role="manual"),), stamp=STAMP, receipt_id=uuid4()).record


def test_correction_blocks_prior_recall_derived_fact_and_pending_extraction(setup):
    repo, _, _ = setup
    turn, thread = uuid4(), uuid4()
    with repo.transaction() as tx:
        original = create(tx)
        record(tx, original, turn, thread)
        derived_source = span(turn, "assistant")
        derived = create(tx, source=derived_source, thread=thread)
        assert tx.response_sources_valid("miori", (derived_source,))
        corrected = correct(tx, original)
        assert tx.get("miori", derived.id).content_version == 1
        assert tx.invalid_response_ids("miori") == {turn}
        assert not tx.response_sources_valid("miori", (derived_source,))
        with pytest.raises(RecordConflict, match="invalid memory version"):
            create(tx, source=derived_source, thread=thread)
        # ユーザーの独立した発言と、新版を用いた回答は保存できる。
        create(tx, source=span(turn, "user"), thread=thread)
        fresh_turn = uuid4()
        record(tx, corrected, fresh_turn, thread)
        create(tx, source=span(fresh_turn, "assistant"), thread=thread)
        assert tx.invalid_response_ids("miori") == {turn}
    with repo.read() as tx:
        assert tx.invalid_response_ids("miori") == {turn}


def test_invalidity_crosses_episode_reference_and_multiple_recalled_facts(setup):
    repo, _, _ = setup
    turns = [uuid4(), uuid4()]
    with repo.transaction() as tx:
        original = create(tx)
        episode = create(tx, kind=RecordKind.EPISODE)
        tx.add_reference(character_id="miori", episode_id=episode.id, episode_version=1,
                         fact_id=original.id, fact_version=1, sources=(span(),))
        record(tx, episode, turns[0], uuid4())
        derived = create(tx, source=span(turns[0], "assistant"))
        record(tx, derived, turns[1], uuid4())
        correct(tx, original)
        assert tx.invalid_response_ids("miori") == set(turns)


def test_history_follow_up_copies_only_offered_response_dependencies(setup):
    repo, _, _ = setup
    thread, first, follow_up, unrelated = (uuid4() for _ in range(4))
    with repo.transaction() as tx:
        fact = create(tx)
        record(tx, fact, first, thread)
        record(tx, None, follow_up, thread, parents=(first,))
        record(tx, None, unrelated, thread)
        correct(tx, fact)
        assert tx.invalid_response_ids("miori") == {first, follow_up}


def test_character_and_history_conversation_cannot_be_crossed(setup):
    repo, _, _ = setup
    with repo.transaction() as tx:
        foreign = create(tx, character="other")
        with pytest.raises(ValueError, match="outside"):
            record(tx, foreign, uuid4(), uuid4())
        fact = create(tx)
        parent = uuid4()
        record(tx, fact, parent, uuid4())
        with pytest.raises(ValueError, match="another conversation"):
            record(tx, None, uuid4(), uuid4(), parents=(parent,))


def test_captured_old_version_is_recorded_after_concurrent_correction(setup):
    repo, _, _ = setup
    turn, thread = uuid4(), uuid4()
    with repo.transaction() as tx:
        fact = create(tx)
        correct(tx, fact)
        record(tx, fact, turn, thread)
        record(tx, fact, turn, thread)
        assert tx.invalid_response_ids("miori") == {turn}
        assert tx._connection.execute("SELECT COUNT(*) FROM memory_response_dependencies").fetchone()[0] == 1


def test_cycles_and_deleted_dependencies_fail_closed(setup):
    repo, _, _ = setup
    turn = uuid4()
    with repo.transaction() as tx:
        # 保存済みデータに循環があっても、その回答を独立した根拠として使わない。
        fact = create(tx, source=span(turn, "assistant"))
        record(tx, fact, turn, uuid4())
        assert tx.invalid_response_ids("miori") == {turn}
        tx.delete(character_id="miori", record_id=fact.id, expected_version=1,
                  sources=(span(role="manual"),), receipt_id=uuid4())
        assert tx.invalid_response_ids("miori") == {turn}


def test_v3_migration_preserves_records_and_v3_backup_is_readable(setup):
    from app.backup_restore.sqlite_snapshot import verify_sqlite_database

    repo, paths, root = setup
    with repo.transaction() as tx:
        fact = create(tx)
    with sqlite3.connect(paths.persona_memory_sqlite_path) as db:
        db.execute("DROP TABLE memory_response_dependencies")
        db.execute("DROP TABLE memory_response_origins")
        db.execute("PRAGMA user_version=3")
    verification = verify_sqlite_database(paths.persona_memory_sqlite_path, "persona-memory.db")
    assert verification.schema_version == 3
    initialize_persona_memory_schema(paths, root)
    initialize_persona_memory_schema(paths, root)
    with repo.read() as tx:
        assert tx.get("miori", fact.id) == fact
        assert tx.invalid_response_ids("miori") == set()
        assert tx._connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_invalid_response_is_removed_from_prompt_history_without_changing_saved_user(setup):
    from app.conversation_history.prompt_history import RestoredHistoryTurn
    from app.memory.response_provenance_recorder import ResponseProvenanceRecorder

    repo, paths, _ = setup
    turn_id = uuid4()
    turn = RestoredHistoryTurn("昼食は何だった？", "うどんでした", True, turn_id=turn_id)
    recorder = ResponseProvenanceRecorder(paths.persona_memory_sqlite_path)
    with repo.transaction() as tx:
        fact = create(tx)
        record(tx, fact, turn_id, uuid4())
    assert recorder.filter_history("miori", turn) == turn
    with repo.transaction() as tx:
        correct(tx, fact)
    filtered = recorder.filter_history("miori", turn)
    assert filtered.user_content == turn.user_content
    assert filtered.assistant_content is None and filtered.turn_id == turn_id
    assert turn.assistant_content == "うどんでした"
    assert recorder.filter_history("other", turn) == turn
