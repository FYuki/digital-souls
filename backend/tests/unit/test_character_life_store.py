from uuid import uuid4

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
            async def form(self, reflections):
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
