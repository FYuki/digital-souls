from uuid import UUID, uuid4

import pytest

from app.character_life.models import Kind, LifeError, LifeState, Result, StateStatus
from app.character_life.store import Store


def seeded(tmp_path):
    store = Store(tmp_path / "life.db")
    state = store.save_state(
        LifeState(
            character_id="miori",
            kind=Kind.GOAL_INTENTION,
            content="色彩について話題を探す",
            target_id="elyth",
            source="user",
        )
    )
    grant = store.set_grant("miori", "elyth", "identity", True)
    run = store.create_run(state, grant, "request", True)
    return store, state, grant, run


def test_run_idempotency_and_atomic_state_publication(tmp_path):
    store, state, grant, run = seeded(tmp_path)
    assert store.create_run(state, grant, "request", True).id == run.id
    store.finish(run, Result.APPLIED, "topic_ready", summary="色彩の話題")
    store.finish(run, Result.APPLIED, "topic_ready", summary="二重に保存してはいけない")
    reopened = Store(store.path)
    assert reopened.run(str(run.id)).result is Result.APPLIED
    assert [
        s.content for s in reopened.states("miori") if s.kind is Kind.SHARE_CANDIDATE
    ] == ["色彩の話題"]


def test_request_id_reuse_for_different_goal_conflicts(tmp_path):
    store, state, grant, run = seeded(tmp_path)
    another = store.save_state(state.model_copy(update={"id": uuid4()}))
    with pytest.raises(LifeError, match="request_id_conflict"):
        store.create_run(another, grant, run.request_id, True)


@pytest.mark.parametrize("change", ["revoke", "edit", "pause"])
def test_commit_rechecks_authority_and_intention(tmp_path, change):
    store, state, grant, run = seeded(tmp_path)
    if change == "revoke":
        store.set_grant("miori", "elyth", "identity", False)
    elif change == "edit":
        store.save_state(
            state.model_copy(update={"status": StateStatus.ABANDONED}),
            expected_revision=1,
        )
    else:
        store.save_run(run.model_copy(update={"phase": "paused"}))
    final = store.finish(run, Result.APPLIED, "topic_ready", summary="保存されない話題")
    assert final.result is not Result.APPLIED
    assert all(s.kind is not Kind.SHARE_CANDIDATE for s in store.states("miori"))


def test_character_boundary_and_optimistic_updates(tmp_path):
    store, state, _, _ = seeded(tmp_path)
    with pytest.raises(LifeError, match="state_not_found"):
        store.state("other", state.id)
    store.save_state(
        state.model_copy(update={"content": "新しい意図"}), expected_revision=1
    )
    with pytest.raises(LifeError, match="state_revision_conflict"):
        store.save_state(state, expected_revision=1)
    assert store.states("other") == []


def test_reflection_invalidation_preserves_history_but_removes_active_context(tmp_path):
    store, _, _, _ = seeded(tmp_path)
    source = uuid4()
    state = store.save_state(
        LifeState(
            character_id="miori",
            kind=Kind.INTEREST,
            content="色彩への関心",
            source="reflection",
            source_ids=(source,),
            reflection_revisions={source: "1"},
        )
    )
    assert store.invalidate_reflections("other", frozenset({source})) == 0
    assert store.invalidate_reflections("miori", frozenset({source})) == 1
    assert store.state("miori", state.id).status is StateStatus.DORMANT


def test_formation_deduplication_and_source_change(tmp_path):
    import asyncio
    from app.character_life.models import ReflectionView
    from app.character_life.formation import LifeFormation

    async def scenario():
        store, _, _, _ = seeded(tmp_path)
        reflection = ReflectionView(
            id=uuid4(),
            character_id="miori",
            revision="1",
            content="色彩に関心を感じた",
            active=True,
        )

        class Source:
            async def active(self, character):
                return (reflection,)

            def current_revisions(self, character):
                return {reflection.id: reflection.revision}

        class Proposal:
            async def form(self, reflections, cancellation):
                return {
                    "states": [
                        {
                            "kind": "INTEREST",
                            "content": "色彩への関心",
                            "source_ids": [str(reflection.id)],
                        }
                    ]
                }

        class Privacy:
            async def allowed(self, text):
                return True

        formation = LifeFormation(store, Source(), Proposal(), Privacy())
        assert await formation.run("miori") is Result.APPLIED
        assert await formation.run("miori") is Result.NO_CHANGE
        with pytest.raises(LifeError, match="reflection_boundary_invalid"):
            await formation.run("other")
        assert len([s for s in store.states("miori") if s.source == "reflection"]) == 1

    asyncio.run(scenario())


def test_old_attempt_cannot_overwrite_resumed_run(tmp_path):
    store, _, _, old = seeded(tmp_path)
    resumed = old.model_copy(update={"attempt": 2})
    store.save_run(resumed, expected_attempt=1)
    with pytest.raises(LifeError, match="attempt_superseded"):
        store.save_run(old)
    final = store.finish(old, Result.APPLIED, "topic_ready", summary="古い応答")
    assert final.attempt == 2
    assert final.phase == "queued"
    assert len(store.states("miori")) == 1


def test_revision_history_and_audit_survive_restart_and_isolate_character(tmp_path):
    store, state, _, run = seeded(tmp_path)
    store.save_state(
        state.model_copy(update={"content": "次の意図"}), expected_revision=1
    )
    store.set_grant("miori", "elyth", "identity", False)
    store.finish(run, Result.REJECTED, "autonomy_grant_changed")
    reopened = Store(store.path)
    history = reopened.state_history("miori", state.id)
    assert [s.revision for s in history] == [1, 2]
    assert history[0].content == state.content
    assert reopened.state_history("other", state.id) == []
    events = reopened.audit("miori")
    assert [e["entity"] for e in events] == ["grant", "activity", "grant", "activity"]
    assert reopened.audit("other") == []
    assert reopened.audit("miori", after=events[-1]["sequence"]) == []


def test_reflection_invalidation_covers_old_records_and_preserves_new_revision(
    tmp_path,
):
    store, _, _, _ = seeded(tmp_path)
    source = uuid4()
    stale = store.save_state(
        LifeState(
            character_id="miori",
            kind=Kind.INTEREST,
            content="以前の関心",
            source="reflection",
            source_ids=(source,),
            reflection_revisions={source: "v1"},
        )
    )
    current = store.save_state(
        stale.model_copy(update={"id": uuid4(), "reflection_revisions": {source: "v2"}})
    )
    for index in range(201):
        store.save_state(
            LifeState(
                character_id="miori",
                kind=Kind.INTEREST,
                content=f"関心{index}",
                source="user",
            )
        )
    assert store.reconcile_reflections("miori", {source: "v2"}) == 1
    assert store.state("miori", stale.id).status is StateStatus.DORMANT
    assert store.state("miori", current.id).status is StateStatus.ACTIVE
    assert store.invalidate_reflections("miori", frozenset({source})) == 1
    assert store.state("miori", current.id).status is StateStatus.DORMANT
    assert store.reconcile_reflections("miori", {}) == 0


def test_scheduler_does_not_accumulate_same_active_goal_and_job(tmp_path):
    store, state, grant, _ = seeded(tmp_path)
    with pytest.raises(LifeError, match="activity_already_pending"):
        store.create_run(state, grant, "next-cron", False)
    store.register_formation_job("miori", "first")
    store.register_formation_job("miori", "first")
    with pytest.raises(LifeError, match="formation_already_pending"):
        store.register_formation_job("miori", "second")
    store.finish_formation_job("miori", "first", Result.DEFERRED)
    store.register_formation_job("miori", "second")
    assert len(store.formation_jobs("miori")) == 2


def test_handoff_owner_is_validated_and_legacy_records_migrate(tmp_path):
    import json
    from app.character_life.models import ObservationHandoff, Run

    store, _, _, run = seeded(tmp_path)
    handoff = ObservationHandoff(
        character_id="other", topic="公開の観測", source_revisions=("source",)
    )
    invalid = run.model_copy(update={"handoff": handoff})
    with pytest.raises(ValueError, match="handoff owner mismatch"):
        Run.model_validate(invalid.model_dump())
    with pytest.raises(ValueError, match="handoff owner mismatch"):
        store.save_run(invalid)
    assert store.run(str(run.id)).handoff is None
    with pytest.raises(LifeError, match="run_owner_mismatch"):
        store.finish(run.model_copy(update={"character_id": "other"}), Result.NO_CHANGE, "invalid_owner")
    legacy = run.model_dump(mode="json")
    legacy["handoff"] = handoff.model_dump(mode="json", exclude={"character_id"})
    with store.transaction() as db:
        db.execute(
            "UPDATE life_runs SET document=? WHERE id=?",
            (json.dumps(legacy), str(run.id)),
        )
        db.execute("PRAGMA user_version=1")
    migrated = Store(store.path).run(str(run.id))
    assert migrated.handoff.character_id == "miori"
    assert migrated.handoff.topic == handoff.topic
    assert Store(store.path).run(str(run.id)) == migrated


def test_reads_do_not_take_a_reserved_write_lock(tmp_path):
    store, state, _, run = seeded(tmp_path)
    # 別connectionの書込transactionが開いていても、確定済みsnapshotを読める。
    with store.transaction():
        assert store.state("miori", state.id) == state
        assert store.open_runs() == [run]
        assert store.eligible_states() == [state]


def test_scheduler_queries_use_indexes_and_exclude_ineligible_records(tmp_path, monkeypatch):
    from contextlib import contextmanager

    store, state, grant, run = seeded(tmp_path)
    store.finish(run, Result.NO_CHANGE, "complete")
    pending = store.create_run(state, grant, "next", False)
    store.save_state(LifeState(character_id="other", kind=Kind.INTEREST, content="関心", source="user"))
    queries = []
    transaction = store.transaction

    @contextmanager
    def traced_transaction(*args, **kwargs):
        with transaction(*args, **kwargs) as db:
            db.set_trace_callback(queries.append)
            yield db

    monkeypatch.setattr(store, "transaction", traced_transaction)
    assert store.open_runs() == [pending]
    assert store.eligible_states() == [state]
    with transaction(write=False) as db:
        for table, index in [("life_runs", "life_runs_phase"), ("life_states", "life_states_eligible")]:
            query = next(q for q in queries if q.startswith("SELECT document FROM " + table))
            plan = db.execute("EXPLAIN QUERY PLAN " + query).fetchall()
            assert any(index in row[3] for row in plan)


@pytest.mark.parametrize("invalid", ["uuid", "duplicate_uuid", "blank_content"])
def test_invalid_formation_output_is_rejected_without_consuming_ledger(
    tmp_path, invalid
):
    import asyncio
    from app.character_life.models import ReflectionView
    from app.character_life.formation import LifeFormation

    async def scenario():
        store, _, _, _ = seeded(tmp_path)
        reflection = ReflectionView(
            id=UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
            character_id="miori",
            revision="1",
            content="色彩への内省",
            active=True,
        )

        class Source:
            async def active(self, character):
                return (reflection,)

            def current_revisions(self, character):
                return {reflection.id: reflection.revision}

        class Proposal:
            async def form(self, reflections, cancellation):
                source_ids = [str(reflection.id)]
                if invalid == "uuid":
                    source_ids = ["not-a-uuid"]
                if invalid == "duplicate_uuid":
                    source_ids.append(str(reflection.id).upper())
                return {
                    "states": [
                        {
                            "kind": "INTEREST",
                            "content": "   " if invalid == "blank_content" else "色彩",
                            "source_ids": source_ids,
                        }
                    ]
                }

        class Privacy:
            async def allowed(self, text):
                return True

        with pytest.raises(LifeError) as caught:
            await LifeFormation(store, Source(), Proposal(), Privacy()).run("miori")
        assert caught.value.result is Result.REJECTED
        assert not any(s.source == "reflection" for s in store.states("miori"))

    asyncio.run(scenario())


def test_writer_commits_while_reader_keeps_previous_snapshot(tmp_path):
    store, state, _, _ = seeded(tmp_path)
    with store.transaction(write=False) as reader:
        assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert reader.execute(
            "SELECT revision FROM life_states WHERE id=?", (str(state.id),)
        ).fetchone()[0] == 1
        updated = store.save_state(
            state.model_copy(update={"content": "新しい話題"}), expected_revision=1
        )
        assert updated.revision == 2
        assert reader.execute(
            "SELECT revision FROM life_states WHERE id=?", (str(state.id),)
        ).fetchone()[0] == 1
    assert store.state("miori", state.id).revision == 2


@pytest.mark.parametrize("method", ["invalidate", "reconcile"])
def test_reflection_invalidation_rolls_back_all_states_and_history(tmp_path, method):
    import sqlite3

    store, _, _, _ = seeded(tmp_path)
    source = uuid4()
    states = [store.save_state(LifeState(
        character_id="miori", kind=Kind.INTEREST, content=f"関心 {index}",
        source="reflection", source_ids=(source,), reflection_revisions={source: "1"},
    )) for index in range(2)]
    with store.transaction() as db:
        # 2件目の更新をDB側で拒否し、先行更新とhistoryもrollbackすることを確認する。
        db.execute("""CREATE TRIGGER refuse_partial_invalidation BEFORE UPDATE ON life_states
            WHEN (SELECT count(*) FROM life_states
                  WHERE json_extract(document,'$.status')='DORMANT') > 0
            BEGIN SELECT RAISE(ABORT, 'injected invalidation failure'); END""")
    def invalidate():
        if method == "invalidate":
            return store.invalidate_reflections("miori", frozenset({source}))
        return store.reconcile_reflections("miori", {})
    with pytest.raises(sqlite3.IntegrityError, match="injected invalidation failure"):
        invalidate()
    for state in states:
        assert store.state("miori", state.id) == state
        assert len(store.state_history("miori", state.id)) == 1
    with store.transaction() as db:
        db.execute("DROP TRIGGER refuse_partial_invalidation")
    assert invalidate() == 2
    for state in states:
        assert store.state("miori", state.id).status is StateStatus.DORMANT
        assert len(store.state_history("miori", state.id)) == 2


@pytest.mark.parametrize("invalid", ["empty", "missing", "extra", "user", "activity"])
def test_reflection_provenance_requires_exact_revision_keys(tmp_path, invalid):
    from pydantic import ValidationError

    source, other = uuid4(), uuid4()
    state = LifeState(
        character_id="miori", kind=Kind.INTEREST, content="色彩への関心",
        source="reflection", source_ids=(source,), reflection_revisions={source: "1"},
    )
    changes = {
        "empty": {"reflection_revisions": {}},
        "missing": {"source_ids": (source, other)},
        "extra": {"reflection_revisions": {source: "1", other: "2"}},
        "user": {"source": "user"},
        "activity": {"source": "activity"},
    }[invalid]
    data = state.model_dump() | changes
    with pytest.raises(ValidationError, match="reflection revision boundary"):
        LifeState.model_validate(data)
    store = Store(tmp_path / "life.db")
    with pytest.raises(ValidationError, match="reflection revision boundary"):
        store.save_state(state.model_copy(update=changes))
    assert not store.states("miori")


def test_state_copy_cannot_save_blank_content(tmp_path):
    from pydantic import ValidationError

    store, state, _, _ = seeded(tmp_path)
    with pytest.raises(ValidationError):
        store.save_state(state.model_copy(update={"content": "  "}), expected_revision=1)
    assert store.state("miori", state.id) == state
