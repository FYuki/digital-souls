"""実SQLite/Chromaと合成Embeddingで検索境界を検証する。実LLM受入とは区別する。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import sqlite3
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.conversation_history.models import ProcessingTurnInput
from app.memory.chroma_store import MemorySearchCandidate
from app.memory.episodic.contracts import (
    FiveW, FormationStamp, MergeRelation, NarrativeContext, RecordKind, ResolvedTime, SourceSpan,
    TimeParts, What,
)
from app.memory.episodic.read_repository import CombinedMemoryReadRepository, EpisodicReadRepository
from app.memory.episodic.repository import EpisodicRepository
from app.memory.episodic.sources import ConversationSourceGuard
from app.memory.index_sync import MemoryIndexSync
from app.memory.memory_policy import resolved_memory_policy
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.contracts import MemoryStatus
from app.memory.persistence.index_outbox_repository import IndexOutboxRepository
from app.memory.persistence.schema import initialize_persona_memory_schema
from app.memory.rag_service import retrieve_prompt_memories
from app.privacy.contracts import ScanSuccess
from app.privacy.semantic.contracts import SemanticAssessmentReasonCode, SemanticClassification
from app.runtime_paths import resolve_runtime_paths
from tests.conversation_history_test_support import create_repository
from tests.unit.test_approved_memory_repository import _candidate, _context
from tests.unit.test_memory_rag_service import _assessment

NOW = datetime(2026, 9, 13, 3, tzinfo=UTC)
POLICY = resolved_memory_policy()
STAMP = FormationStamp(policy_version=POLICY.policy_version, classifier_version="fake",
                       model_id="fake", model_digest="fake", prompt_version="fake")


class FakeEmbedder:
    provider_id = "fake"
    model_id = "fake"

    def __call__(self, text):
        return [0.25, 0.5, 0.75]


class Harness:
    def __init__(self, tmp_path):
        root = tmp_path / "repository"
        root.mkdir()
        self.paths = resolve_runtime_paths(
            {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(tmp_path / "data")}, root,
        )
        initialize_persona_memory_schema(self.paths, root)
        self.history = create_repository(self.paths.sqlite_path, now=NOW, uuid_factory=uuid4)
        self.conversation = self.history.create_conversation("miori").conversation_id
        turn = self.history.create_processing_turn(
            "miori", self.conversation, ProcessingTurnInput("先月、仮に月へ旅行したらと話した"),
        )
        turn = self.history.complete_turn("miori", self.conversation, turn.turn_id,
                                          sanitized_assistant_content="想像の旅行ですね")
        self.span = SourceSpan(source_id=turn.turn_id, revision=2, role="user", start=0,
                               end=len(turn.user_content), stated_at=turn.created_at)
        self.episodic = EpisodicRepository(self.paths.persona_memory_sqlite_path)
        self.legacy = ApprovedMemoryRepository(
            database_path=self.paths.persona_memory_sqlite_path, clock=lambda: NOW,
            uuid_factory=uuid4, outbox_uuid_factory=uuid4,
        )
        self.read = CombinedMemoryReadRepository(self.legacy, EpisodicReadRepository(
            self.episodic, ConversationSourceGuard(self.paths.sqlite_path, clock=lambda: NOW,
                                                  retention=timedelta(days=365)),
        ))
        self.index = MemoryIndexSync(
            approved_repository=self.read,
            outbox_repository=IndexOutboxRepository(
                database_path=self.paths.persona_memory_sqlite_path, clock=lambda: NOW,
            ),
            chroma_path=self.paths.chroma_path, runtime_report_dir=self.paths.runtime_report_dir,
            embedder=FakeEmbedder(), embedding_provider_id="fake",
            embedding_model_id="fake", clock=lambda: NOW,
        )

    def value(self, predicate="旅行する", context=NarrativeContext.HYPOTHETICAL):
        return FiveW(what=What(predicate=predicate, object="月", actuality="CONDITIONAL"),
                     context=context, when=ResolvedTime(parts=TimeParts(year=2026, month=8),
                                                       timezone="Asia/Tokyo", reference_at=NOW))

    def create(self, *, kind=RecordKind.FACT, value=None):
        with self.episodic.transaction(now=NOW) as tx:
            return tx.create(character_id="miori", conversation_id=self.conversation,
                             kind=kind, five_w=value or self.value(), sources=(self.span,),
                             stamp=STAMP, receipt_id=uuid4()).record

    def link(self, episode, fact):
        with self.episodic.transaction(now=NOW) as tx:
            tx.add_reference(character_id="miori", episode_id=episode.id,
                             episode_version=episode.content_version, fact_id=fact.id,
                             fact_version=fact.content_version, sources=(self.span,))

    def delete(self, record):
        with self.episodic.transaction(now=NOW) as tx:
            tx.delete(character_id="miori", record_id=record.id,
                      expected_version=record.content_version, sources=(self.span,), receipt_id=uuid4())

    def retrieve(self, message="月の旅行の話を覚えている？", embedder=None):
        scanner = Mock()
        scanner.scan.return_value = ScanSuccess(())
        classifier = Mock()
        classifier.classify.return_value = _assessment(
            SemanticClassification.NOT_SENSITIVE, SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT,
        )
        return retrieve_prompt_memories(
            "miori", message, POLICY, scanner=scanner, classifier=classifier,
            approved_repository=self.read, embedder=embedder or FakeEmbedder(),
            chroma_path=self.paths.chroma_path, now=NOW, timezone="Asia/Tokyo",
        )


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


@pytest.mark.parametrize("change", ["edit", "delete", "screen"])
def test_read_revalidates_history_before_worker_catches_up(h, change):
    record = h.create()
    assert h.read.get(character_id="miori", memory_id=record.id).status is MemoryStatus.ACTIVE
    with sqlite3.connect(h.paths.sqlite_path) as connection:
        if change == "edit":
            connection.execute("UPDATE conversation_turns SET user_content = '訂正' WHERE turn_id = ?",
                               (str(h.span.source_id),))
        elif change == "delete":
            connection.execute("DELETE FROM conversation_turns WHERE turn_id = ?", (str(h.span.source_id),))
        else:
            connection.execute(
                "INSERT INTO screen_turn_provenance VALUES(?, ?, ?, 1, 'route', 'explicit_ui', 'window', 'direct_observation')",
                (str(h.span.source_id), str(uuid4()), str(uuid4())))
    view = h.read.get(character_id="miori", memory_id=record.id)
    assert view.status is MemoryStatus.INACTIVE
    assert view.normalized_text == ""
    assert view.five_w is None
    assert h.read.list_active(character_id="miori") == []


def test_legacy_preferences_and_new_records_keep_separate_write_models(h):
    legacy = h.legacy.save(character_id="miori", candidate=_candidate(),
                           context=replace(_context(), policy_version=POLICY.policy_version))
    fact = h.create()
    assert {v.id for v in h.read.list_active(character_id="miori")} == {legacy.id, fact.id}
    assert {v.id for v in h.legacy.list_active(character_id="miori")} == {legacy.id}
    assert h.read.get(character_id="other", memory_id=fact.id) is None
    assert h.read.list_character_ids() == {"miori"}


@pytest.mark.parametrize("context, label", [
    (NarrativeContext.HYPOTHETICAL, "仮定の話"),
    (NarrativeContext.FICTIONAL, "創作の話"),
    (NarrativeContext.REPORTED, "申告・観測された内容"),
    (NarrativeContext.UNKNOWN, "実際の出来事か不明"),
])
def test_chroma_and_prompt_preserve_context_and_month_precision(h, context, label):
    record = h.create(value=h.value(context=context))
    h.index.run_worker_once()
    outcome = h.retrieve()
    assert len(outcome.memories) == 1
    memory = outcome.memories[0]
    assert memory.memory_id == str(record.id)
    assert memory.memory_type == "FACT"
    assert label in memory.normalized_text
    assert memory.occurred_at is None
    assert "2026年8月" in memory.temporal_text
    assert "1日" not in memory.temporal_text
    assert memory.content_version == 1
    from app._chat_runtime import _memory_prompt_content
    assert _memory_prompt_content(memory, "America/New_York") == memory.normalized_text
    assert "2026-08-01" not in memory.normalized_text


def test_fact_correction_does_not_retrieve_stale_episode_body(h, monkeypatch):
    fact = h.create()
    episode = h.create(kind=RecordKind.EPISODE)
    h.link(episode, fact)
    with h.episodic.transaction(now=NOW) as tx:
        updated = tx.update(
            character_id="miori", record_id=fact.id, expected_version=1,
            five_w=h.value(predicate="訂正した旅行を話す"), sources=(h.span,), stamp=STAMP, receipt_id=uuid4(),
        ).record
    assert h.read.get(character_id="miori", memory_id=episode.id).status is MemoryStatus.INACTIVE
    assert h.read.get(character_id="miori", memory_id=fact.id).content_version == 2
    from app.memory import rag_service
    monkeypatch.setattr(rag_service, "query_memories", lambda *args, **kwargs: [
        MemorySearchCandidate(str(episode.id), 0.01), MemorySearchCandidate(str(fact.id), 0.01),
    ])
    result = h.retrieve()
    assert [m.memory_id for m in result.memories] == [str(updated.id)]
    assert "訂正した旅行を話す" in result.memories[0].normalized_text


def test_deleted_fact_and_old_episode_are_removed_from_index_and_prompt(h):
    fact = h.create()
    episode = h.create(kind=RecordKind.EPISODE)
    h.link(episode, fact)
    h.index.run_worker_once()
    h.delete(fact)
    # 索引にはまだ旧IDがあるが、返却時にはSQLiteの削除と依存関係を検証する。
    assert h.retrieve().memories == ()
    h.index.run_worker_once()
    h.index.reconcile_once()
    assert h.retrieve().memories == ()
    from app.memory.chroma_store import active_memory_index_fingerprint, list_memory_index_ids
    fingerprint = active_memory_index_fingerprint("miori", h.paths.chroma_path)
    assert list_memory_index_ids(character_id="miori", chroma_path=h.paths.chroma_path,
                                 fingerprint=fingerprint) == set()


def test_period_candidate_deleted_while_embedding_waits_is_not_returned(h, monkeypatch):
    fact = h.create()
    from app.memory import rag_service
    monkeypatch.setattr(rag_service, "query_memories", lambda *args, **kwargs: [])

    def embed(_):
        h.delete(fact)
        return [0.25, 0.5, 0.75]

    result = h.retrieve("2026年8月の話を教えて", embedder=embed)
    assert result.memories == ()
    assert result.no_match


def test_period_search_does_not_assume_month_only_fact_happened_on_specific_day(h):
    fact = h.create()
    kwargs = dict(character_id="miori", compatible_policy_versions=frozenset({POLICY.policy_version}))
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("Asia/Tokyo")
    august = datetime(2026, 8, 1, tzinfo=zone)
    september = datetime(2026, 9, 1, tzinfo=zone)
    assert [m.id for m in h.read.search_by_occurred_range(
        **kwargs, start=august, end=september,
    )] == [fact.id]
    assert h.read.search_by_occurred_range(**kwargs, start=august, end=august + timedelta(days=1)) == []


def test_valid_same_thread_merge_indexes_representative_only(h):
    first, second = h.create(), h.create()
    with h.episodic.transaction(now=NOW) as tx:
        tx.add_merge(MergeRelation(id=uuid4(), character_id="miori", source_fact_id=first.id,
                                  source_version=1, target_fact_id=second.id, target_version=1,
                                  conversation_id=h.conversation, policy="p1", evidence=(h.span,), valid=True))
    assert {v.id for v in h.read.list_active(character_id="miori")} == {second.id}
    assert h.read.get(character_id="miori", memory_id=first.id).normalized_text == ""


def test_read_only_projection_does_not_hold_history_lock_during_embedding(h):
    record = h.create()

    def embed(_):
        with sqlite3.connect(h.paths.sqlite_path, timeout=0) as connection:
            connection.execute("UPDATE conversation_turns SET user_content = '変更' WHERE turn_id = ?",
                               (str(h.span.source_id),))
        return [0.25, 0.5, 0.75]

    h.index._embedder = embed
    h.index.run_worker_once()
    # 更新と競合した索引の本文を取得しても、会話へは返さない。
    assert h.read.get(character_id="miori", memory_id=record.id).status is MemoryStatus.INACTIVE
    assert h.retrieve().memories == ()


@pytest.mark.parametrize("deleted_side", ["source", "target"])
def test_deleted_equivalent_fact_does_not_return_through_invalid_merge(h, deleted_side):
    first, second = h.create(), h.create()
    with h.episodic.transaction(now=NOW) as tx:
        tx.add_merge(MergeRelation(id=uuid4(), character_id="miori", source_fact_id=first.id,
                                  source_version=1, target_fact_id=second.id, target_version=1,
                                  conversation_id=h.conversation, policy="p1", evidence=(h.span,), valid=True))
    h.delete(first if deleted_side == "source" else second)
    assert h.read.list_active(character_id="miori") == []
    assert h.read.get(character_id="miori", memory_id=first.id).normalized_text == ""
    assert h.read.get(character_id="miori", memory_id=second.id).normalized_text == ""


def test_invalidated_merge_does_not_restore_old_source_as_independent_memory(h):
    first, second = h.create(), h.create()
    with h.episodic.transaction(now=NOW) as tx:
        tx.add_merge(MergeRelation(id=uuid4(), character_id="miori", source_fact_id=first.id,
                                  source_version=1, target_fact_id=second.id, target_version=1,
                                  conversation_id=h.conversation, policy="p1", evidence=(h.span,), valid=True))
        tx.update(character_id="miori", record_id=second.id, expected_version=1,
                  five_w=h.value(predicate="訂正後の内容"), sources=(h.span,), stamp=STAMP, receipt_id=uuid4())
    assert {r.id for r in h.read.list_active(character_id="miori")} == {second.id}
    assert h.read.get(character_id="miori", memory_id=first.id).status is MemoryStatus.INACTIVE


def test_deleted_merge_source_also_blocks_episode_referring_to_representative(h):
    first, second = h.create(), h.create()
    episode = h.create(kind=RecordKind.EPISODE)
    h.link(episode, second)
    with h.episodic.transaction(now=NOW) as tx:
        tx.add_merge(MergeRelation(id=uuid4(), character_id="miori", source_fact_id=first.id,
                                  source_version=1, target_fact_id=second.id, target_version=1,
                                  conversation_id=h.conversation, policy="p1", evidence=(h.span,), valid=True))
    h.delete(first)
    assert h.read.get(character_id="miori", memory_id=episode.id).normalized_text == ""


def test_chat_prompt_receives_month_precision_context_and_version(h):
    from types import SimpleNamespace
    from app._chat_runtime import _rag_context_for_reply
    fact = h.create()
    h.index.run_worker_once()
    scanner, classifier = Mock(), Mock()
    scanner.scan.return_value = ScanSuccess(())
    classifier.classify.return_value = _assessment(
        SemanticClassification.NOT_SENSITIVE, SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT,
    )
    dependencies = SimpleNamespace(privacy_scanner=scanner, semantic_classifier=classifier,
                                   approved_memory_repository=h.read, memory_embedder=FakeEmbedder(),
                                   clock=lambda: NOW)
    context = SimpleNamespace(memory_policy=POLICY, chroma_path=h.paths.chroma_path,
                              occurred_timezone="America/New_York")
    rag = _rag_context_for_reply("miori", "旅行の話", context, dependencies)
    assert len(rag.items) == 1
    item = rag.items[0]
    assert "仮定の話" in item.content and "2026年8月" in item.content
    assert "Asia/Tokyo" in item.content and "1日" not in item.content
    assert item.reference.memory_id == str(fact.id)
    assert item.reference.content_version == 1
    assert item.reference.occurred_at is None
    assert item.reference.occurred_precision == "MONTH"


def test_management_correction_and_deletion_reach_actual_chroma_and_prompt(h):
    from app.memory.episodic.management import EpisodicMemoryManagement
    from app.memory.episodic.privacy import PrivacyReview
    fact = h.create()
    episode = h.create(kind=RecordKind.EPISODE)
    h.link(episode, fact)
    h.index.run_worker_once()
    reviewer = Mock()
    reviewer.review.return_value = PrivacyReview(True, "ALLOW", STAMP)
    management = EpisodicMemoryManagement(
        reader=h.read.episodic, reviewer=reviewer, clock=lambda: NOW, index_sync=h.index)
    management.correct(character_id="miori", record_id=fact.id, expected_version=1,
                       five_w=h.value(predicate="訂正した月旅行を想像する"), idempotency_key=uuid4())
    h.index.run_worker_once()
    result = h.retrieve()
    assert [m.memory_id for m in result.memories] == [str(fact.id)]
    assert result.memories[0].content_version == 2
    assert "訂正した月旅行を想像する" in result.memories[0].normalized_text
    management.delete(character_id="miori", record_id=fact.id, expected_version=2, idempotency_key=uuid4())
    assert h.retrieve().memories == ()
    h.index.run_worker_once()
    h.index.reconcile_once()
    assert h.retrieve().memories == ()
    assert all(v.five_w is None for v in (
        h.read.get(character_id="miori", memory_id=fact.id),
        h.read.get(character_id="miori", memory_id=episode.id),
    ))


def test_removed_input_also_blocks_fact_derived_from_assistant_paraphrase(h):
    from app.memory.episodic.contracts import RecordStatus
    fact = h.create()
    reply_source = h.span.model_copy(update={"role": "assistant", "start": 0, "end": len("想像の旅行ですね")})
    with h.episodic.transaction(now=NOW) as tx:
        paraphrase = tx.create(
            character_id="miori", conversation_id=h.conversation, kind=RecordKind.FACT,
            five_w=h.value(), sources=(reply_source,), stamp=STAMP, receipt_id=uuid4(),
        ).record
    h.delete(fact)
    assert h.read.get(character_id="miori", memory_id=paraphrase.id).five_w is None
    with h.episodic.read() as tx:
        assert tx.get("miori", paraphrase.id).status is RecordStatus.ACTIVE



def test_prior_recall_answer_cannot_reintroduce_manually_corrected_fact(h):
    from app.memory.response_provenance_recorder import ResponseProvenanceRecorder
    from app.memory.episodic.management import EpisodicMemoryManagement
    from app.prompting import PromptMemoryReference, RagContext, RagItem
    from tests.prompt_test_support import prompt_build_input, prompt_builder
    from app.memory.episodic.privacy import PrivacyReview

    original = h.create()
    conversation = h.history.create_conversation("miori").conversation_id
    started = h.history.create_processing_turn(
        "miori", conversation, ProcessingTurnInput("旅行先を思い出して"),
    )
    prompt = prompt_builder().build(prompt_build_input(rag=RagContext(items=(
        RagItem("旅行の記憶", 0.1, PromptMemoryReference(
            str(original.id), None, None, "SEMANTIC", original.content_version,
        )),
    ))))
    recorder = ResponseProvenanceRecorder(h.paths.persona_memory_sqlite_path)
    recorder.record(started, prompt)
    response = h.history.complete_turn(
        "miori", conversation, started.turn_id, sanitized_assistant_content="月を想像した旅行でした",
    )
    source = SourceSpan(source_id=response.turn_id, revision=2, role="assistant",
                        start=0, end=len(response.assistant_content), stated_at=response.updated_at)
    with h.episodic.transaction() as tx:
        recalled = tx.create(
            character_id="miori", conversation_id=conversation, kind=RecordKind.FACT,
            five_w=h.value(), sources=(source,), stamp=STAMP, receipt_id=uuid4(),
        ).record
    h.index.run_worker_once()
    assert str(recalled.id) in {result.memory_id for result in h.retrieve().memories}
    reviewer = Mock()
    reviewer.review.return_value = PrivacyReview(True, "ALLOW", STAMP)
    service = EpisodicMemoryManagement(
        reader=h.read.episodic, reviewer=reviewer, clock=lambda: NOW, index_sync=h.index,
    )
    service.correct(character_id="miori", record_id=original.id, expected_version=1,
                    five_w=h.value(predicate="訂正済みの旅行"), idempotency_key=uuid4())
    # 派生Factの古いChroma行が残っていてもSQLiteの出典検証で即座に除外する。
    view = h.read.get(character_id="miori", memory_id=recalled.id)
    assert view.status is MemoryStatus.INACTIVE and view.normalized_text == ""
    h.index.run_worker_once()
    results = h.retrieve()
    assert str(recalled.id) not in {result.memory_id for result in results.memories}
    assert any(result.memory_id == str(original.id) and result.content_version == 2 for result in results.memories)


def test_generated_reply_using_concurrently_corrected_memory_is_not_committed(h):
    from app.memory.response_provenance_recorder import ResponseProvenanceRecorder
    from app.prompting import PromptMemoryReference, RagContext, RagItem
    from tests.prompt_test_support import prompt_build_input, prompt_builder

    original = h.create()
    started = h.history.create_processing_turn(
        "miori", h.conversation, ProcessingTurnInput("思い出して"),
    )
    prompt = prompt_builder().build(prompt_build_input(rag=RagContext(items=(
        RagItem("採用済みの記憶", 0.1, PromptMemoryReference(
            str(original.id), None, None, "SEMANTIC", 1,
        )),
    ))))
    with h.episodic.transaction() as tx:
        tx.update(character_id="miori", record_id=original.id, expected_version=1,
                  five_w=h.value(predicate="訂正した"),
                  sources=(SourceSpan(source_id=uuid4(), revision=1, role="manual",
                                      start=0, end=1, stated_at=NOW),),
                  stamp=STAMP, receipt_id=uuid4())
    recorder = ResponseProvenanceRecorder(h.paths.persona_memory_sqlite_path)
    with pytest.raises(ValueError, match="invalid memory version"):
        recorder.record(started, prompt)
    with h.episodic.read() as tx:
        assert started.turn_id in tx.invalid_response_ids("miori")



def legacy_from_source(h, source_id):
    from app.memory.persistence.contracts import MemorySourceInput, MemorySourceType
    context = replace(_context(), policy_version=POLICY.policy_version, idempotency_key=str(uuid4()),
                      sources=(MemorySourceInput(source_type=MemorySourceType.CONVERSATION_TURN,
                                                 source_provider_id="core", source_ref=str(source_id)),))
    return h.legacy.save(character_id="miori", candidate=_candidate("うどんを好む"), context=context), context


@pytest.mark.parametrize("change", ["correct", "delete"])
def test_fact_management_excludes_legacy_preference_from_same_source_and_reextract(h, change):
    from app.memory.episodic.management import EpisodicMemoryManagement
    from app.memory.episodic.privacy import PrivacyReview

    fact = h.create()
    preference, context = legacy_from_source(h, h.span.source_id)
    from app.memory.persistence.contracts import MemorySourceInput, MemorySourceType
    derived = h.legacy.save(
        character_id="miori", candidate=_candidate("統合してもうどんを好む"),
        context=replace(context, idempotency_key=str(uuid4()),
                        sources=(MemorySourceInput(source_type=MemorySourceType.CONSOLIDATION,
                                                   source_provider_id="core", source_ref=str(preference.id)),)),
    )
    h.index.run_worker_once()
    assert str(preference.id) in {m.memory_id for m in h.retrieve().memories}
    reviewer = Mock()
    reviewer.review.return_value = PrivacyReview(True, "ALLOW", STAMP)
    management = EpisodicMemoryManagement(reader=h.read.episodic, reviewer=reviewer,
                                          clock=lambda: NOW, index_sync=h.index)
    if change == "correct":
        management.correct(character_id="miori", record_id=fact.id, expected_version=1,
                           five_w=h.value(predicate="訂正した"), idempotency_key=uuid4())
    else:
        management.delete(character_id="miori", record_id=fact.id, expected_version=1, idempotency_key=uuid4())
    assert h.read.get(character_id="miori", memory_id=preference.id) is None
    assert h.read.get(character_id="miori", memory_id=derived.id) is None
    if change == "correct":
        assert h.legacy.get(character_id="miori", memory_id=preference.id).status is MemoryStatus.INACTIVE
        assert h.legacy.get(character_id="miori", memory_id=derived.id).status is MemoryStatus.INACTIVE
    if change == "delete":
        assert h.legacy.get(character_id="miori", memory_id=derived.id) is None
        assert h.legacy.get(character_id="miori", memory_id=preference.id) is None
        assert "うどんを好む".encode() not in h.paths.persona_memory_sqlite_path.read_bytes()
    assert preference.id not in {m.id for m in h.read.list_active(character_id="miori")}
    assert str(preference.id) not in {m.memory_id for m in h.retrieve().memories}
    with pytest.raises(ValueError, match="removed memory content"):
        h.legacy.save(character_id="miori", candidate=_candidate("うどんを好む"),
                      context=replace(context, idempotency_key=str(uuid4())))


def test_manually_corrected_preference_is_independent_of_old_fact_source(h):
    from app.memory.persistence.contracts import FormationMethod, MemorySourceInput, MemorySourceType

    fact = h.create()
    preference, context = legacy_from_source(h, h.span.source_id)
    child = h.legacy.save(
        character_id="miori", candidate=_candidate("旧preferenceを統合した内容"),
        context=replace(context, idempotency_key=str(uuid4()),
                        sources=(MemorySourceInput(source_type=MemorySourceType.CONSOLIDATION,
                                                   source_provider_id="core", source_ref=str(preference.id)),)),
    )
    key = str(uuid4())
    corrected = h.legacy.correct(
        character_id="miori", memory_id=preference.id, candidate=_candidate("そばを好む"),
        context=replace(context, formation_method=FormationMethod.DIRECT, idempotency_key=key,
                        sources=(MemorySourceInput(source_type=MemorySourceType.USER_CORRECTION,
                                                   source_provider_id="core", source_ref=key),)),
    )
    h.delete(fact)
    assert h.read.get(character_id="miori", memory_id=preference.id) == corrected
    assert h.read.get(character_id="miori", memory_id=child.id) is None



def test_fact_correction_invalidates_an_already_planned_legacy_consolidation(h):
    from app.memory.persistence.contracts import ConsolidationOperation, ConsolidationConflictError
    from tests.unit.test_memory_consolidation_repository import _snapshot

    fact = h.create()
    preference, _ = legacy_from_source(h, h.span.source_id)
    snapshot = _snapshot(h.legacy, preference.id)
    with h.episodic.transaction() as tx:
        tx.update(character_id="miori", record_id=fact.id, expected_version=1,
                  five_w=h.value(predicate="訂正した"),
                  sources=(SourceSpan(source_id=uuid4(), revision=1, role="manual",
                                      start=0, end=1, stated_at=NOW),),
                  stamp=STAMP, receipt_id=uuid4())
    # preference自体の版が不変でも、根拠が失効した計画を適用しない。
    with pytest.raises(ConsolidationConflictError):
        h.legacy.apply_consolidation(
            character_id="miori", operation=ConsolidationOperation.KEEP,
            inputs=(snapshot,), candidate=None, context=None,
            canonical_memory_id=preference.id, consolidated_at=NOW,
        )
