"""履歴・出典guard・privacy境界・Episode/Fact正本を実SQLiteで結合する。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from app.conversation_history.models import ProcessingTurnInput
from app.memory.episodic.contracts import (
    ExtractedFiveW, FormationStamp, RecordKind, RecordStatus, What,
)
from app.memory.episodic.extraction_contracts import (
    ExistingTarget, ExtractedLink, ExtractedRecord, ExtractionBatch, SourceQuote,
)
from app.memory.episodic.privacy import PrivacyReview
from app.memory.episodic.quotes import InvalidExtraction
from app.memory.episodic.registration import EpisodicRegistrationService
from app.memory.episodic.repository import EpisodicRepository, RecordConflict
from app.memory.formation.thread_chunks import split_thread
from app.memory.formation.thread_queue import ThreadFormationQueue, StaleThreadSnapshot
from app.memory.persistence.schema import initialize_persona_memory_schema
from app.runtime_paths import resolve_runtime_paths
from tests.conversation_history_test_support import create_repository

NOW = datetime(2026, 9, 13, 3, tzinfo=UTC)
STAMP = FormationStamp(policy_version="p1", classifier_version="c1", model_id="test",
                       model_digest="test-digest", prompt_version="prompt1")


class Reviewer:
    def __init__(self):
        self.calls = []
        self.callback = None

    def review(self, *, kind, value, source_texts):
        self.calls.append((kind, value, source_texts))
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
        if any("保存しないで" in text for text in source_texts):
            return PrivacyReview(False, "USER_REQUEST")
        return PrivacyReview(True, "ALLOW", STAMP)


@dataclass
class Harness:
    paths: object
    conversation_id: object
    repository: EpisodicRepository
    queue: ThreadFormationQueue
    service: EpisodicRegistrationService
    reviewer: Reviewer
    now: list

    def turn(self, text, *, days=0):
        self.now[0] = NOW + timedelta(days=days, seconds=len(self.reviewer.calls) + 1)
        history = create_repository(self.paths.sqlite_path, now=self.now[0], uuid_factory=uuid4)
        turn = history.create_processing_turn("miori", self.conversation_id, ProcessingTurnInput(text))
        return history.complete_turn("miori", self.conversation_id, turn.turn_id,
                                      sanitized_assistant_content="話を聞きました")

    def snapshot(self):
        snapshot = self.queue.snapshot(self.queue.claim())
        self.service.reconcile_sources(snapshot)
        return snapshot

    def apply(self, snapshot, batch, *, finish=True):
        chunk = split_thread(snapshot)[0]
        prepared = self.service.prepare(snapshot, chunk, batch, self.service.catalog(snapshot))
        result = self.service.commit(prepared)
        if finish:
            with self.queue.guard(snapshot) as connection:
                self.queue.finish(connection, snapshot, saved_count=result.saved, rejected_count=result.rejected)
        return result

    def records(self, kind=None):
        with self.repository.read() as tx:
            return tx.list_records("miori", kind=kind, active_only=False)


@pytest.fixture
def harness(tmp_path: Path):
    root = tmp_path / "repository"
    root.mkdir()
    paths = resolve_runtime_paths({"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(tmp_path / "data")}, root)
    initialize_persona_memory_schema(paths, root)
    history = create_repository(paths.sqlite_path, now=NOW, uuid_factory=uuid4)
    conversation = history.create_conversation("miori")
    now = [NOW]
    queue = ThreadFormationQueue(database_path=paths.sqlite_path, clock=lambda: now[0],
                                  retention=timedelta(days=365), lease_seconds=120)
    repository = EpisodicRepository(paths.persona_memory_sqlite_path)
    reviewer = Reviewer()
    service = EpisodicRegistrationService(repository=repository, queue=queue, reviewer=reviewer,
                                          timezone="Asia/Tokyo", clock=lambda: now[0])
    return Harness(paths, conversation.conversation_id, repository, queue, service, reviewer, now)


def quote(snapshot, turn, text=None):
    source = next(s for s in snapshot.sources if s.turn.turn_id == turn.turn_id)
    return SourceQuote(source_id=turn.turn_id, revision=source.revision, role="user",
                        quote=text or turn.user_content)


def episode(q, existing=None):
    return ExtractedRecord(
        key="episode", kind=RecordKind.EPISODE, operation="CONTINUE" if existing else "NEW",
        target=ExistingTarget(id=existing.id, version=existing.content_version) if existing else None,
        five_w=ExtractedFiveW(what=What(predicate="聞いた", object="昼食の話")),
        anchor=q, sources=(q,), changes=("what",) if existing else (),
    )


def fact(q, *, existing=None, object="うどん", reference=False):
    return ExtractedRecord(
        key="fact", kind=RecordKind.FACT, operation="REFERENCE" if reference else "UPDATE" if existing else "NEW",
        target=ExistingTarget(id=existing.id, version=existing.content_version) if existing else None,
        five_w=None if reference else ExtractedFiveW(what=What(predicate="食べた", object=object)),
        anchor=q, sources=(q,), changes=("what",) if existing and not reference else (),
    )


def batch(ep, fa, q):
    return ExtractionBatch(records=(ep, fa), links=(ExtractedLink(episode="episode", fact="fact", sources=(q,)),))


def initial(harness):
    turn = harness.turn("うどんを食べた")
    snapshot = harness.snapshot()
    q = quote(snapshot, turn)
    harness.apply(snapshot, batch(episode(q), fact(q), q))
    return harness.records(RecordKind.EPISODE)[0], harness.records(RecordKind.FACT)[0], turn


def test_daily_experience_is_one_episode_across_multiple_async_extractions(harness):
    first_episode, first_fact, _ = initial(harness)
    turn = harness.turn("駅前で食べた")
    snapshot = harness.snapshot()
    q = quote(snapshot, turn)
    harness.apply(snapshot, batch(episode(q, first_episode), fact(q, existing=first_fact, reference=True), q))
    episodes = harness.records(RecordKind.EPISODE)
    assert len(episodes) == 1
    assert episodes[0].id == first_episode.id
    assert episodes[0].five_w.when == first_episode.five_w.when
    with harness.repository.read() as tx:
        assert len(tx.versions("miori", episodes[0].id)[-1].sources) == 2


def test_retelling_adds_episode_and_explicit_correction_updates_stable_fact(harness):
    first_episode, first_fact, _ = initial(harness)
    turn = harness.turn("あの昼食の話だけど、ごめん、そばだった", days=1)
    snapshot = harness.snapshot()
    q = quote(snapshot, turn)
    result = harness.apply(snapshot, batch(episode(q), fact(q, existing=first_fact, object="そば"), q))
    assert result.saved == 2
    episodes = harness.records(RecordKind.EPISODE)
    assert len(episodes) == 2
    assert episodes[0] == first_episode
    assert episodes[1].five_w.when != first_episode.five_w.when
    updated = harness.records(RecordKind.FACT)[0]
    assert updated.id == first_fact.id and updated.content_version == 2
    assert updated.five_w.what.object == "そば"
    with harness.repository.read() as tx:
        assert not tx.references("miori", first_episode.id)[0].valid
        new_link = tx.references("miori", episodes[1].id)[0]
        assert new_link.valid and new_link.fact_id == first_fact.id and new_link.fact_version == 2


def test_unchanged_retelling_keeps_fact_body_and_records_new_acquisition(harness):
    _, first_fact, _ = initial(harness)
    turn = harness.turn("この前話したうどんの昼食を思い出した", days=2)
    snapshot = harness.snapshot()
    q = quote(snapshot, turn)
    harness.apply(snapshot, batch(episode(q), fact(q, existing=first_fact, reference=True), q))
    assert harness.records(RecordKind.FACT) == (first_fact,)
    assert len(harness.records(RecordKind.EPISODE)) == 2


def test_reextracting_old_sources_under_new_thread_revision_does_not_duplicate(harness):
    first_episode, first_fact, old_turn = initial(harness)
    harness.turn("ありがとう")
    snapshot = harness.snapshot()
    old_quote = quote(snapshot, old_turn)
    result = harness.apply(snapshot, batch(episode(old_quote), fact(old_quote), old_quote))
    assert result.saved == 0 and result.replayed == 2
    assert harness.records(RecordKind.EPISODE) == (first_episode,)
    assert harness.records(RecordKind.FACT) == (first_fact,)


def test_commit_receipts_survive_failed_queue_finish_without_duplicate_records(harness):
    turn = harness.turn("うどんを食べた")
    snapshot = harness.snapshot()
    q = quote(snapshot, turn)
    candidate_batch = batch(episode(q), fact(q), q)
    first = harness.apply(snapshot, candidate_batch, finish=False)
    assert first.saved == 2
    harness.queue.release(snapshot.lease, failed=False)
    retried = harness.snapshot()
    result = harness.apply(retried, candidate_batch)
    assert result.saved == 0 and result.replayed == 2
    assert len(harness.records()) == 2


def test_privacy_denial_marks_processed_without_saving_or_requiring_importance(harness):
    turn = harness.turn("うどんを食べた。保存しないで")
    snapshot = harness.snapshot()
    q = quote(snapshot, turn, "うどんを食べた")
    result = harness.apply(snapshot, batch(episode(q), fact(q), q))
    assert result.saved == 0 and result.rejected == 2
    assert harness.records() == ()
    assert all("保存しないで" in call[2][0] for call in harness.reviewer.calls)


def test_edit_during_privacy_assessment_invalidates_entire_registration(harness):
    turn = harness.turn("うどんを食べた")
    snapshot = harness.snapshot()
    q = quote(snapshot, turn)
    def edit():
        with sqlite3.connect(harness.paths.sqlite_path) as connection:
            connection.execute("UPDATE conversation_turns SET user_content = 'そばを食べた' WHERE turn_id = ?",
                               (str(turn.turn_id),))
    harness.reviewer.callback = edit
    with pytest.raises(StaleThreadSnapshot):
        harness.apply(snapshot, batch(episode(q), fact(q), q))
    assert harness.records() == ()


def test_management_delete_during_extraction_cannot_be_overwritten(harness):
    _, first_fact, _ = initial(harness)
    turn = harness.turn("そばだった", days=1)
    snapshot = harness.snapshot()
    q = quote(snapshot, turn)
    candidate_batch = batch(episode(q), fact(q, existing=first_fact, object="そば"), q)
    prepared = harness.service.prepare(snapshot, split_thread(snapshot)[0], candidate_batch,
                                       harness.service.catalog(snapshot))
    with harness.repository.transaction() as tx:
        sources = tx.versions("miori", first_fact.id)[-1].sources
        tx.delete(character_id="miori", record_id=first_fact.id, expected_version=1,
                  sources=sources, receipt_id=uuid4())
    with pytest.raises(RecordConflict):
        harness.service.commit(prepared)
    assert harness.records(RecordKind.FACT)[0].status is RecordStatus.DELETED
    assert len(harness.records(RecordKind.EPISODE)) == 1


def test_invented_quote_or_unsupplied_target_is_rejected(harness):
    turn = harness.turn("うどんを食べた")
    snapshot = harness.snapshot()
    q = quote(snapshot, turn, "機密の創作引用")
    with pytest.raises(InvalidExtraction) as error:
        harness.apply(snapshot, batch(episode(q), fact(q), q))
    assert "機密の創作引用" not in str(error.value)
    assert harness.records() == ()


def test_new_source_revision_stops_old_records_before_reextraction(harness):
    first_episode, first_fact, old_turn = initial(harness)
    with sqlite3.connect(harness.paths.sqlite_path) as connection:
        connection.execute("UPDATE conversation_turns SET user_content = 'そばを食べた' WHERE turn_id = ?",
                           (str(old_turn.turn_id),))
    snapshot = harness.snapshot()
    assert all(record.status is RecordStatus.INACTIVE for record in harness.records())
    current_turn = snapshot.sources[0].turn
    q = quote(snapshot, current_turn)
    catalog = harness.records()
    ep = next(r for r in catalog if r.id == first_episode.id)
    fa = next(r for r in catalog if r.id == first_fact.id)
    harness.apply(snapshot, batch(episode(q, ep), fact(q, existing=fa, object="そば"), q))
    assert all(record.status is RecordStatus.ACTIVE for record in harness.records())
    assert harness.records(RecordKind.FACT)[0].five_w.what.object == "そば"


def test_same_thread_confirmed_five_w_merge_keeps_original_ids_and_experiences(harness):
    from app.memory.episodic.contracts import Person, PersonRole, Place, TimeExpression, TimeParts
    from app.memory.episodic.extraction_contracts import ExtractedMerge
    turn = harness.turn("2026年9月12日、昼食のため駅前の店でうどんを食べた")
    snapshot = harness.snapshot()
    q = quote(snapshot, turn)
    data = ExtractedFiveW(
        who=(Person(name="ユーザー", role=PersonRole.ACTOR, entity_id="speaker:user"),),
        what=What(predicate="食べた", object="うどん", polarity="AFFIRMED", actuality="OCCURRED"),
        when=TimeExpression(parts=TimeParts(year=2026, month=9, day=12)),
        where=Place(name="駅前の店"), why="昼食のため", context="REPORTED")
    first = fact(q).model_copy(update={"five_w": data, "time_source": q})
    harness.apply(snapshot, batch(episode(q), first, q))
    original = harness.records(RecordKind.FACT)[0]
    turn = harness.turn("前に話した同じ昼食だよ。2026年9月12日、昼食のため駅前の店でうどんを食べた", days=1)
    snapshot = harness.snapshot()
    q = quote(snapshot, turn)
    data = data.model_copy(update={"where": original.five_w.where})
    new_fact = fact(q).model_copy(update={"five_w": data, "time_source": q})
    old_fact = fact(q, existing=original, reference=True).model_copy(update={"key": "old_fact"})
    merged_batch = ExtractionBatch(
        records=(episode(q), new_fact, old_fact),
        links=(ExtractedLink(episode="episode", fact="fact", sources=(q,)),),
        merges=(ExtractedMerge(source="fact", target="old_fact", evidence=(q,), same_event=True),))
    harness.apply(snapshot, merged_batch)
    assert len(harness.records(RecordKind.EPISODE)) == 2
    assert len(harness.records(RecordKind.FACT)) == 2
    duplicate = next(r for r in harness.records(RecordKind.FACT) if r.id != original.id)
    with harness.repository.read() as tx:
        assert tx.representative("miori", duplicate.id).id == original.id
        assert len(tx.merges("miori")) == 1
        assert tx.merges("miori")[0].evidence
