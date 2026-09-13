"""実SQLiteとHTTP境界でFact管理・履歴保護・削除の伝播を検証する。推論はfake。"""

from datetime import timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.memory.episodic.contracts import FiveW, MergeRelation, RecordKind, RecordStatus, SourceSpan, What
from app.memory.episodic.management import EpisodicMemoryManagement
from app.memory.episodic.read_repository import EpisodicReadRepository
from app.memory.episodic.repository import RecordConflict
from app.memory.episodic.sources import ConversationSourceGuard
from app.memory.episodic.extraction_contracts import ExtractionBatch
from app.memory.formation.thread_chunks import split_thread
from app.routers.episodic_memories import router
from tests.module.test_episodic_registration import (
    harness, initial, quote, episode, fact, batch, STAMP,  # noqa: F401
)

COLLECTION = "/characters/miori/episodic-memories"


class Purger:
    def __init__(self):
        self.ids = []

    def delete_after_commit(self, *, character_id, memory_id):
        self.ids.append((character_id, memory_id))


@pytest.fixture
def management(harness):
    purger = Purger()
    service = EpisodicMemoryManagement(
        reader=EpisodicReadRepository(harness.repository, ConversationSourceGuard(
            harness.paths.sqlite_path, clock=lambda: harness.now[0], retention=timedelta(days=365))),
        reviewer=harness.reviewer, clock=lambda: harness.now[0], index_sync=purger,
    )
    app = FastAPI()
    app.include_router(router)
    app.state.episodic_memory_management = service
    with TestClient(app) as client:
        yield service, client, purger


def operation(version):
    return {"expected_version": version, "idempotency_key": str(uuid4())}


def correction(version, object="そば"):
    return {**operation(version), "five_w": FiveW(what=What(predicate="食べた", object=object)).model_dump(mode="json")}


def test_correction_preserves_fact_id_and_history_but_does_not_serve_old_body(harness, management):
    ep, fa, turn = initial(harness)
    service, client, purger = management
    payload = correction(1)
    response = client.patch(f"{COLLECTION}/{fa.id}", json=payload)
    assert response.status_code == 200
    value = response.json()
    assert value["id"] == str(fa.id) and value["content_version"] == 2
    assert value["five_w"]["what"]["object"] == "そば"
    assert value["versions"][1]["sources"][0]["role"] == "manual"
    assert "うどん" not in response.text
    assert all("five_w" not in version for version in value["versions"])
    assert not value["references"][0]["valid"]
    assert client.get(f"{COLLECTION}/{ep.id}").json()["five_w"] is None
    assert client.patch(f"{COLLECTION}/{fa.id}", json=payload).json()["content_version"] == 2
    assert purger.ids == [("miori", fa.id)]
    with harness.repository.read() as tx:
        assert len(tx.versions("miori", fa.id)) == 2
        assert tx.source_masks("miori")[0].source_id == turn.turn_id
    assert service._reader.get(character_id="miori", memory_id=fa.id).five_w.what.object == "そば"


def test_stale_or_cross_character_or_episode_update_is_rejected(harness, management):
    ep, fa, _ = initial(harness)
    _, client, _ = management
    assert client.patch(f"{COLLECTION}/{fa.id}", json=correction(7)).status_code == 409
    assert client.patch(f"{COLLECTION}/{ep.id}", json=correction(1)).status_code == 422
    assert client.patch(f"/characters/other/episodic-memories/{fa.id}", json=correction(1)).status_code == 404
    invalid = correction(1)
    invalid["five_w"]["who"] = [{"name": "別人", "role": "ACTOR", "entity_id": "character:other"}]
    assert client.patch(f"{COLLECTION}/{fa.id}", json=invalid).status_code == 422
    assert client.get("/characters/other/episodic-memories").json() == []


def test_privacy_rejection_does_not_store_or_echo_body(harness, management):
    _, fa, _ = initial(harness)
    _, client, _ = management
    response = client.patch(f"{COLLECTION}/{fa.id}", json=correction(1, "保存しないで"))
    assert response.status_code == 422
    assert response.json() == {"reason_code": "USER_REQUEST"}
    assert "保存しないで" not in response.text
    assert harness.records(RecordKind.FACT)[0] == fa


def test_delete_during_privacy_assessment_cannot_be_overwritten(harness, management):
    _, fa, _ = initial(harness)
    service, client, _ = management
    harness.reviewer.callback = lambda: service.delete(
        character_id="miori", record_id=fa.id, expected_version=1, idempotency_key=uuid4())
    assert client.patch(f"{COLLECTION}/{fa.id}", json=correction(1)).status_code == 409
    assert harness.records(RecordKind.FACT)[0].status is RecordStatus.DELETED


def test_delete_scrubs_fact_and_related_episode_bodies_but_keeps_experience_identity(harness, management):
    ep, fa, _ = initial(harness)
    _, client, purger = management
    payload = operation(1)
    assert client.request("DELETE", f"{COLLECTION}/{fa.id}", json=payload).status_code == 204
    assert client.request("DELETE", f"{COLLECTION}/{fa.id}", json=payload).status_code == 204
    response = client.get(COLLECTION)
    assert "うどん" not in response.text and "昼食の話" not in response.text
    assert {str(ep.id), str(fa.id)} == {r["id"] for r in response.json()}
    with harness.repository.read() as tx:
        saved_episode = tx.get("miori", ep.id)
        assert saved_episode.status is RecordStatus.INACTIVE
        assert saved_episode.five_w.when == ep.five_w.when
        assert all(v.five_w is None for v in tx.versions("miori", fa.id))
        assert tx.versions("miori", ep.id)[0].five_w is None
    assert {("miori", fa.id), ("miori", ep.id)} <= set(purger.ids)
    db = harness.paths.persona_memory_sqlite_path.read_bytes()
    assert "うどん".encode() not in db and "昼食の話".encode() not in db


def duplicate(harness, fa):
    with harness.repository.transaction() as tx:
        copy = tx.create(character_id="miori", conversation_id=harness.conversation_id, kind=RecordKind.FACT,
                         five_w=fa.five_w, sources=tx.versions("miori", fa.id)[-1].sources,
                         stamp=STAMP, receipt_id=uuid4()).record
        tx.add_merge(MergeRelation(
            id=uuid4(), character_id="miori", source_fact_id=copy.id, source_version=1,
            target_fact_id=fa.id, target_version=1, conversation_id=harness.conversation_id,
            policy="same-event-test", evidence=tx.versions("miori", fa.id)[-1].sources, valid=True))
    return copy


def test_management_shows_merged_fact_and_delete_erases_both_current_equivalents(harness, management):
    _, fa, _ = initial(harness)
    copy = duplicate(harness, fa)
    service, client, _ = management
    assert service._reader.get(character_id="miori", memory_id=copy.id).five_w is None
    assert client.get(f"{COLLECTION}/{copy.id}").json()["five_w"] is not None
    assert client.get(f"{COLLECTION}/{copy.id}").json()["representative_id"] == str(fa.id)
    assert client.request("DELETE", f"{COLLECTION}/{fa.id}", json=operation(1)).status_code == 204
    assert all(r.status is RecordStatus.DELETED for r in harness.records(RecordKind.FACT))


def test_delete_traverses_old_merge_version_without_erasing_corrected_independent_current_fact(harness, management):
    _, fa, _ = initial(harness)
    copy = duplicate(harness, fa)
    _, client, _ = management
    assert client.patch(f"{COLLECTION}/{fa.id}", json=correction(1)).status_code == 200
    assert client.request("DELETE", f"{COLLECTION}/{copy.id}", json=operation(1)).status_code == 204
    current = client.get(f"{COLLECTION}/{fa.id}").json()
    assert current["status"] == "ACTIVE"
    assert current["five_w"]["what"]["object"] == "そば"
    with harness.repository.read() as tx:
        assert tx.versions("miori", fa.id)[0].five_w is None
        assert tx.versions("miori", fa.id)[1].five_w.what.object == "そば"


def test_old_source_proposal_is_rejected_without_blocking_new_unrelated_record(harness, management):
    _, fa, old_turn = initial(harness)
    _, client, _ = management
    assert client.request("DELETE", f"{COLLECTION}/{fa.id}", json=operation(1)).status_code == 204
    new_turn = harness.turn("晴れている")
    snapshot = harness.snapshot()
    old, new = quote(snapshot, old_turn), quote(snapshot, new_turn)
    old_proposal = fact(old).model_copy(update={"key": "old"})
    new_proposal = fact(new, object="晴れ").model_copy(update={"key": "new"})
    prior_calls = len(harness.reviewer.calls)
    result = harness.apply(snapshot, ExtractionBatch(records=(old_proposal, new_proposal)))
    assert result.rejected == 1 and result.saved == 1
    assert len(harness.reviewer.calls) == prior_calls + 1


def test_management_delete_between_prepare_and_commit_refuses_even_reference(harness, management):
    _, fa, _ = initial(harness)
    service, _, _ = management
    new_turn = harness.turn("その話を思い出した")
    snapshot = harness.snapshot()
    q = quote(snapshot, new_turn)
    prepared = harness.service.prepare(snapshot, split_thread(snapshot)[0],
        ExtractionBatch(records=(fact(q, existing=fa, reference=True),)), harness.service.catalog(snapshot))
    service.delete(character_id="miori", record_id=fa.id, expected_version=1, idempotency_key=uuid4())
    with pytest.raises(RecordConflict):
        harness.service.commit(prepared)


def test_correction_mask_survives_following_conversation_update(harness, management):
    _, fa, old_turn = initial(harness)
    _, client, _ = management
    assert client.patch(f"{COLLECTION}/{fa.id}", json=correction(1)).status_code == 200
    current = harness.records(RecordKind.FACT)[0]
    new_turn = harness.turn("そのそばは温かかった")
    snapshot = harness.snapshot()
    q = quote(snapshot, new_turn)
    result = harness.apply(snapshot, ExtractionBatch(records=(fact(q, existing=current, object="温かいそば"),)))
    assert result.saved == 1
    with harness.repository.read() as tx:
        masks = tx.source_masks("miori")
        assert any(s.source_id == old_turn.turn_id for s in masks)
        assert not any(s.source_id == new_turn.turn_id for s in masks)


def test_reusing_correct_receipt_after_delete_returns_tombstone(harness, management):
    _, fa, _ = initial(harness)
    _, client, _ = management
    payload = correction(1)
    assert client.patch(f"{COLLECTION}/{fa.id}", json=payload).status_code == 200
    assert client.request("DELETE", f"{COLLECTION}/{fa.id}", json=operation(2)).status_code == 204
    replay = client.patch(f"{COLLECTION}/{fa.id}", json=payload)
    assert replay.status_code == 200
    assert replay.json()["status"] == "DELETED"
    assert replay.json()["five_w"] is None


def test_invalid_body_validation_never_echoes_input(harness, management):
    _, fa, _ = initial(harness)
    _, client, _ = management
    payload = correction(1)
    payload["five_w"]["unexpected"] = "秘密の本文"
    response = client.patch(f"{COLLECTION}/{fa.id}", json=payload)
    assert response.status_code == 422 and "秘密の本文" not in response.text


def test_delete_reaches_new_version_that_still_inherits_removed_source(harness, management):
    _, fa, _ = initial(harness)
    copy = duplicate(harness, fa)
    _, client, _ = management
    with harness.repository.transaction() as tx:
        tx.update(character_id="miori", record_id=fa.id, expected_version=1,
                  five_w=FiveW(what=What(predicate="食べた", object="温かいうどん")),
                  sources=tx.versions("miori", fa.id)[-1].sources, stamp=STAMP, receipt_id=uuid4())
    assert client.request("DELETE", f"{COLLECTION}/{copy.id}", json=operation(1)).status_code == 204
    assert all(record.status is RecordStatus.DELETED for record in harness.records(RecordKind.FACT))


def test_delete_erases_assistant_paraphrase_even_without_fact_merge(harness, management):
    _, fa, _ = initial(harness)
    _, client, _ = management
    with harness.repository.transaction() as tx:
        original = tx.versions("miori", fa.id)[-1].sources[0]
        paraphrase = tx.create(
            character_id="miori", conversation_id=harness.conversation_id, kind=RecordKind.FACT,
            five_w=fa.five_w, sources=(original.model_copy(update={"role": "assistant", "end": 4}),),
            stamp=STAMP, receipt_id=uuid4()).record
    assert client.request("DELETE", f"{COLLECTION}/{fa.id}", json=operation(1)).status_code == 204
    with harness.repository.read() as tx:
        assert tx.get("miori", paraphrase.id).status is RecordStatus.DELETED
        assert all(v.five_w is None for v in tx.versions("miori", paraphrase.id))



def recalled_record(harness, reference, *, kind=RecordKind.FACT, value=None):
    from app.conversation_history.models import ProcessingTurnInput
    from app.memory.episodic.response_provenance import record_response
    from tests.conversation_history_test_support import create_repository

    history = create_repository(harness.paths.sqlite_path, now=harness.now[0], uuid_factory=uuid4)
    conversation = history.create_conversation("miori").conversation_id
    turn = history.create_processing_turn("miori", conversation, ProcessingTurnInput("思い出して"))
    with harness.repository.transaction() as tx:
        record_response(
            tx._connection, character_id="miori", conversation_id=conversation, turn_id=turn.turn_id,
            references=((reference.id, reference.content_version),), created_at=harness.now[0].isoformat(),
        )
    turn = history.complete_turn("miori", conversation, turn.turn_id,
                                 sanitized_assistant_content="うどんを食べたそうです")
    source = SourceSpan(source_id=turn.turn_id, revision=2, role="assistant",
                        start=0, end=len(turn.assistant_content), stated_at=turn.updated_at)
    with harness.repository.transaction() as tx:
        record = tx.create(character_id="miori", conversation_id=conversation, kind=kind,
                           five_w=value or reference.five_w, sources=(source,), stamp=STAMP,
                           receipt_id=uuid4()).record
    return record, source


@pytest.mark.parametrize("reference_kind", [RecordKind.FACT, RecordKind.EPISODE])
def test_delete_erases_recalled_fact_across_threads(harness, management, reference_kind):
    ep, fa, _ = initial(harness)
    reference = fa if reference_kind is RecordKind.FACT else ep
    derived, source = recalled_record(harness, reference)
    _, client, purger = management
    request = operation(1)
    assert client.request("DELETE", f"{COLLECTION}/{fa.id}", json=request).status_code == 204
    with harness.repository.read() as tx:
        assert tx.get("miori", derived.id).status is RecordStatus.DELETED
        assert all(v.five_w is None for v in tx.versions("miori", derived.id))
        assert source.source_id in tx.invalid_response_ids("miori")
        assert tx.get("miori", ep.id).status is RecordStatus.INACTIVE
    assert ("miori", derived.id) in purger.ids
    assert client.request("DELETE", f"{COLLECTION}/{fa.id}", json=request).status_code == 204


def test_delete_erases_old_recalled_version_but_keeps_independent_correction(harness, management):
    _, fa, _ = initial(harness)
    derived, _ = recalled_record(harness, fa)
    _, client, _ = management
    assert client.patch(f"{COLLECTION}/{derived.id}", json=correction(1, "ラーメン")).status_code == 200
    assert client.request("DELETE", f"{COLLECTION}/{fa.id}", json=operation(1)).status_code == 204
    with harness.repository.read() as tx:
        current = tx.get("miori", derived.id)
        assert current.status is RecordStatus.ACTIVE and current.content_version == 2
        assert current.five_w.what.object == "ラーメン"
        versions = tx.versions("miori", derived.id)
        assert versions[0].five_w is None and versions[1].five_w == current.five_w


def test_delete_follows_multiple_responses_and_preserves_episode_identity(harness, management):
    ep, fa, _ = initial(harness)
    first, _ = recalled_record(harness, fa)
    retold, _ = recalled_record(harness, first, kind=RecordKind.EPISODE)
    last, _ = recalled_record(harness, retold)
    _, client, _ = management
    assert client.request("DELETE", f"{COLLECTION}/{fa.id}", json=operation(1)).status_code == 204
    with harness.repository.read() as tx:
        for record in (fa, first, last):
            assert tx.get("miori", record.id).status is RecordStatus.DELETED
            assert all(v.five_w is None for v in tx.versions("miori", record.id))
        kept = tx.get("miori", retold.id)
        assert kept.id == retold.id and kept.status is RecordStatus.INACTIVE
        assert kept.five_w.when == retold.five_w.when
        assert tx.versions("miori", retold.id)[0].five_w is None
        assert tx.get("miori", ep.id).id == ep.id


def test_delete_follows_old_preference_version_and_preserves_manual_new_version(harness, management):
    from dataclasses import replace
    from app.memory.persistence.approved_repository import ApprovedMemoryRepository
    from app.memory.persistence.contracts import FormationMethod, MemorySourceInput, MemorySourceType
    from tests.unit.test_approved_memory_repository import _candidate, _context

    _, fa, turn = initial(harness)
    legacy = ApprovedMemoryRepository(database_path=harness.paths.persona_memory_sqlite_path,
                                      clock=lambda: harness.now[0], uuid_factory=uuid4,
                                      outbox_uuid_factory=uuid4)
    context = replace(_context(), idempotency_key=str(uuid4()),
                      sources=(MemorySourceInput(source_type=MemorySourceType.CONVERSATION_TURN,
                                                 source_provider_id="core", source_ref=str(turn.turn_id)),))
    preference = legacy.save(character_id="miori", candidate=_candidate("うどんを好む"), context=context)
    recalled, _ = recalled_record(harness, preference, value=fa.five_w)
    key = str(uuid4())
    corrected = legacy.correct(
        character_id="miori", memory_id=preference.id, candidate=_candidate("そばを好む"),
        context=replace(context, formation_method=FormationMethod.DIRECT, idempotency_key=key,
                        sources=(MemorySourceInput(source_type=MemorySourceType.USER_CORRECTION,
                                                   source_provider_id="core", source_ref=key),)),
    )
    _, client, _ = management
    assert client.request("DELETE", f"{COLLECTION}/{fa.id}", json=operation(1)).status_code == 204
    assert legacy.get(character_id="miori", memory_id=preference.id) == corrected
    with harness.repository.read() as tx:
        assert tx.get("miori", recalled.id).status is RecordStatus.DELETED
        assert all(v.five_w is None for v in tx.versions("miori", recalled.id))


@pytest.mark.parametrize("invalidity", ["expired", "deleted"])
def test_management_masks_experienced_time_with_invalid_episode(harness, management, invalidity):
    ep, fa, _ = initial(harness)
    service, client, _ = management
    assert client.get(f"{COLLECTION}/{ep.id}").json()["experienced_when"] is not None
    if invalidity == "expired":
        harness.now[0] += timedelta(days=366)
    else:
        service.delete(character_id="miori", record_id=fa.id, expected_version=1, idempotency_key=uuid4())
    for value in (client.get(f"{COLLECTION}/{ep.id}").json(),
                  next(x for x in client.get(COLLECTION).json() if x["id"] == str(ep.id))):
        assert value["five_w"] is None
        assert value["experienced_when"] is None
        assert value["normalized_text"] == ""


@pytest.mark.parametrize("operation", ["list_active", "get", "management"])
def test_read_snapshot_shares_provenance_and_keeps_conversation_masks_separate(harness, management, monkeypatch, operation):
    from collections import Counter
    from app.memory.episodic.repository import EpisodicTransaction
    from tests.conversation_history_test_support import create_repository
    ep1, fa1, _ = initial(harness)
    first_thread = harness.conversation_id
    history = create_repository(harness.paths.sqlite_path, now=harness.now[0], uuid_factory=uuid4)
    harness.conversation_id = history.create_conversation("miori").conversation_id
    initial(harness)
    ep2 = next(x for x in harness.records(RecordKind.EPISODE) if x.conversation_id == harness.conversation_id)
    fa2 = next(x for x in harness.records(RecordKind.FACT) if x.conversation_id == harness.conversation_id)
    service, _, _ = management
    counts = Counter()
    original_masks = EpisodicTransaction.source_masks
    original_invalid = EpisodicTransaction.invalid_response_ids
    original_merges = EpisodicTransaction.merges
    with harness.repository.read() as tx:
        mask = tx.versions("miori", fa1.id)[0].sources

    def masks(tx, character_id, conversation_id=None):
        counts[("masks", conversation_id)] += 1
        return mask if conversation_id == first_thread else original_masks(tx, character_id, conversation_id)

    def invalid(tx, *args, **kwargs):
        counts["invalid"] += 1
        return original_invalid(tx, *args, **kwargs)

    def merges(tx, *args, **kwargs):
        counts["merges"] += 1
        return original_merges(tx, *args, **kwargs)

    monkeypatch.setattr(EpisodicTransaction, "source_masks", masks)
    monkeypatch.setattr(EpisodicTransaction, "invalid_response_ids", invalid)
    monkeypatch.setattr(EpisodicTransaction, "merges", merges)
    if operation == "management":
        values = service.list(character_id="miori")
        assert {x["id"] for x in values if x["status"] == "ACTIVE"} == {str(ep2.id), str(fa2.id)}
    elif operation == "get":
        assert service._reader.get(character_id="miori", memory_id=ep2.id).five_w is not None
    else:
        assert {x.id for x in service._reader.list_active(character_id="miori")} == {ep2.id, fa2.id}
    assert counts["invalid"] == counts["merges"] == 1
    assert counts[("masks", first_thread)] == 1
    assert counts[("masks", harness.conversation_id)] == 1
    assert counts[("masks", None)] == 1
