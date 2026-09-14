"""意味記憶の管理・実SQLite/Chroma・応答参照を合成Embeddingで接続検証する。"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from app._chat_runtime import _rag_context_for_reply
from app.memory.episodic.privacy import PrivacyReview
from app.memory.episodic.read_repository import CombinedMemoryReadRepository
from app.memory.index_sync import MemoryIndexSync
from app.memory.memory_policy import resolved_memory_policy
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.index_outbox_repository import IndexOutboxRepository
from app.memory.rag_service import retrieve_prompt_memories
from app.memory.semantic.management import SemanticMemoryManagement
from app.memory.semantic.read_repository import SemanticReadRepository, WithSemanticReadRepository
from app.privacy.contracts import ScanSuccess
from app.privacy.semantic.contracts import SemanticAssessmentReasonCode, SemanticClassification
from tests.module.test_episodic_retrieval import FakeEmbedder
from tests.module.test_semantic_store import STAMP, candidate, h as h, save, source
from tests.unit.test_memory_rag_service import _assessment


def test_semantic_correction_and_deletion_reach_canonical_chroma_and_prompt_reference(h):
    policy = resolved_memory_policy()
    h.reviewer.review = lambda *_: PrivacyReview(True, "ALLOW", STAMP.model_copy(update={"policy_version": policy.policy_version}))
    legacy = ApprovedMemoryRepository(database_path=h.paths.persona_memory_sqlite_path,
        clock=h.store.clock, uuid_factory=uuid4, outbox_uuid_factory=uuid4)
    reader = WithSemanticReadRepository(CombinedMemoryReadRepository(legacy, h.store.episode_reader))
    reader.bind(SemanticReadRepository(h.store))
    index = MemoryIndexSync(approved_repository=reader,
        outbox_repository=IndexOutboxRepository(database_path=h.paths.persona_memory_sqlite_path, clock=h.store.clock),
        chroma_path=h.paths.chroma_path, runtime_report_dir=h.paths.runtime_report_dir,
        embedder=FakeEmbedder(), embedding_provider_id="fake", embedding_model_id="fake", clock=h.store.clock)
    manager = SemanticMemoryManagement(h.store, index)
    scanner, classifier = Mock(), Mock()
    scanner.scan.return_value = ScanSuccess(())
    classifier.classify.return_value = _assessment(SemanticClassification.NOT_SENSITIVE,
                                                  SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT)

    def retrieve(character="miori"):
        return retrieve_prompt_memories(character, "私の居住地は？", policy, scanner=scanner, classifier=classifier,
            approved_repository=reader, embedder=FakeEmbedder(), chroma_path=h.paths.chroma_path,
            now=h.store.clock(), timezone="Asia/Tokyo")

    original_source = source(h)
    original = save(h, candidate(original_source))
    index.run_worker_once()
    assert [item.memory_id for item in retrieve().memories] == [str(original.id)]
    assert not retrieve("other-character").memories
    corrected = manager.correct(character_id="miori", record_id=original.id, version=1, value="東京", key=uuid4())
    assert all(item.memory_id != str(original.id) for item in retrieve().memories)
    index.run_worker_once()
    found = retrieve().memories
    assert len(found) == 1 and found[0].memory_id == corrected["id"]
    dependencies = SimpleNamespace(privacy_scanner=scanner, semantic_classifier=classifier,
        approved_memory_repository=reader, memory_embedder=FakeEmbedder(), clock=h.store.clock)
    context = SimpleNamespace(memory_policy=policy, chroma_path=h.paths.chroma_path, occurred_timezone="Asia/Tokyo")
    prompt = _rag_context_for_reply("miori", "私の居住地は？", context, dependencies)
    assert prompt.items[0].reference.memory_id == corrected["id"]
    assert prompt.items[0].reference.content_version == corrected["content_version"]
    assert "現在有効な自己申告: ユーザーの居住地は東京" == prompt.items[0].content
    assert "訂正前の内容を現在の答えとして使わない" in prompt.required_instruction
    assert found[0].normalized_text == "ユーザーの居住地は東京"
    from uuid import UUID
    manager.delete(character_id="miori", record_id=UUID(corrected["id"]), version=corrected["content_version"])
    assert not retrieve().memories
    with sqlite3.connect(h.paths.sqlite_path) as connection:
        assert connection.execute("SELECT user_content FROM conversation_turns WHERE turn_id=?",
                                  (str(original_source.source_id),)).fetchone()[0] == "私は大阪に住んでいる"

def test_historical_and_derived_memories_are_not_marked_as_current_self_report(h):
    from app.memory.semantic.contracts import SemanticOperation
    from tests.module.test_semantic_store import derived

    original = save(h, candidate(source(h)))
    current = save(h, candidate(source(h, "東京へ引っ越した"), value="東京"),
                   target=original, op=SemanticOperation.CHANGE)
    generalization = save(h, candidate(derived(h), predicate="コーヒーの好み", value="好き",
                                      derived=True, self_report=False))
    reader = SemanticReadRepository(h.store)
    views = {view.id: view for view in reader.list_active(character_id="miori")}
    assert views[current.id].current_self_report
    assert not views[original.id].current_self_report
    assert "過去の状態" in views[original.id].normalized_text
    assert not views[generalization.id].current_self_report


def test_historical_vector_match_includes_current_value_and_rechecks_its_source(h, monkeypatch):
    from app.memory.chroma_store import MemorySearchCandidate
    from app.memory.semantic.contracts import SemanticOperation

    policy = resolved_memory_policy()
    h.reviewer.review = lambda *_: PrivacyReview(True, "ALLOW", STAMP.model_copy(
        update={"policy_version": policy.policy_version}))
    old = save(h, candidate(source(h)))
    current_conversation = h.history.create_conversation("miori")
    current_source = source(h, "東京に引っ越した", conversation=current_conversation)
    current = save(h, candidate(current_source, value="東京"), target=old, op=SemanticOperation.CHANGE)
    unrelated = save(h, candidate(source(h, "猫が好き"), predicate="動物の好み", value="猫"))
    reader = SemanticReadRepository(h.store)
    monkeypatch.setattr("app.memory.rag_service.query_memories",
                        lambda *_args, **_kwargs: [MemorySearchCandidate(str(old.id), 0.01)])
    scanner, classifier = Mock(), Mock()
    scanner.scan.return_value = ScanSuccess(())
    classifier.classify.return_value = _assessment(SemanticClassification.NOT_SENSITIVE,
                                                  SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT)

    def retrieve():
        return retrieve_prompt_memories("miori", "今どこに住んでいる？", policy,
            scanner=scanner, classifier=classifier, approved_repository=reader,
            embedder=FakeEmbedder(), chroma_path=h.paths.chroma_path,
            now=h.store.clock(), timezone="Asia/Tokyo").memories

    found = retrieve()
    assert [item.memory_id for item in found] == [str(current.id), str(old.id)]
    assert found[0].current_self_report and found[0].raw_distance == float("inf")
    assert str(unrelated.id) not in {item.memory_id for item in found}
    h.history.hard_delete_conversation("miori", current_conversation.conversation_id)
    assert [item.memory_id for item in retrieve()] == [str(old.id)]


def test_current_value_expansion_excludes_foreign_or_incompatible_snapshot(h):
    from dataclasses import replace
    from app.memory.chroma_store import MemorySearchCandidate
    from app.memory.rag_service import _include_current_self_reports, _VerifiedCandidate
    from app.memory.semantic.contracts import SemanticOperation

    policy = resolved_memory_policy()
    h.reviewer.review = lambda *_: PrivacyReview(True, "ALLOW", STAMP.model_copy(
        update={"policy_version": policy.policy_version}))
    old = save(h, candidate(source(h)))
    current = save(h, candidate(source(h, "東京に引っ越した"), value="東京"),
                   target=old, op=SemanticOperation.CHANGE)
    reader = SemanticReadRepository(h.store)
    old_view = reader.get(character_id="miori", memory_id=old.id)
    current_view = reader.get(character_id="miori", memory_id=current.id)
    ranked = (_VerifiedCandidate(MemorySearchCandidate(str(old.id), 0.1), old_view),)
    scanner = Mock()
    scanner.scan.return_value = ScanSuccess(())
    for invalid in (replace(current_view, character_id="other"),
                    replace(current_view, policy_version="incompatible")):
        repository = Mock()
        repository.list_active.return_value = [invalid]
        repository.get.return_value = invalid
        assert _include_current_self_reports(ranked, character="miori", policy=policy,
            scanner=scanner, approved_repository=repository, now=h.store.clock()) == ranked

def test_conflicted_attribute_constrains_reply_without_exposing_either_value(h, monkeypatch):
    from app.memory.semantic.contracts import SemanticOperation

    policy = resolved_memory_policy()
    h.reviewer.review = lambda *_: PrivacyReview(True, "ALLOW", STAMP.model_copy(
        update={"policy_version": policy.policy_version}))
    first_conversation = h.history.create_conversation("miori")
    first = save(h, candidate(source(h, "誕生日は6月12日", conversation=first_conversation),
                              predicate="誕生日", value="6月12日", fixed=True))
    second_conversation = h.history.create_conversation("miori")
    save(h, candidate(source(h, "誕生日は6月13日", conversation=second_conversation),
                      predicate="誕生日", value="6月13日", fixed=True),
         target=first, op=SemanticOperation.CONFLICT)
    legacy = ApprovedMemoryRepository(database_path=h.paths.persona_memory_sqlite_path,
        clock=h.store.clock, uuid_factory=uuid4, outbox_uuid_factory=uuid4)
    reader = WithSemanticReadRepository(CombinedMemoryReadRepository(legacy, h.store.episode_reader))
    reader.bind(SemanticReadRepository(h.store))
    monkeypatch.setattr("app.memory.rag_service.query_memories", lambda *_a, **_k: [])
    scanner, classifier = Mock(), Mock()
    scanner.scan.return_value = ScanSuccess(())
    classifier.classify.return_value = _assessment(SemanticClassification.NOT_SENSITIVE,
                                                  SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT)
    dependencies = SimpleNamespace(privacy_scanner=scanner, semantic_classifier=classifier,
        approved_memory_repository=reader, memory_embedder=FakeEmbedder(), clock=h.store.clock)
    context = SimpleNamespace(memory_policy=policy, chroma_path=h.paths.chroma_path, occurred_timezone="Asia/Tokyo")
    prompt = _rag_context_for_reply("miori", "私の誕生日は？", context, dependencies)
    assert '"attribute": "誕生日"' in prompt.required_instruction
    assert "現在は確定できない" in prompt.required_instruction
    assert "6月12日" not in prompt.required_instruction and "6月13日" not in prompt.required_instruction
    assert not prompt.items
    assert not _rag_context_for_reply("miori", "好きな楽器は？", context, dependencies).required_instruction
    assert not _rag_context_for_reply("other", "私の誕生日は？", context, dependencies).required_instruction
    h.history.hard_delete_conversation("miori", first_conversation.conversation_id)
    h.history.hard_delete_conversation("miori", second_conversation.conversation_id)
    assert not _rag_context_for_reply("miori", "私の誕生日は？", context, dependencies).required_instruction

def test_lexical_source_match_recovers_current_residence_outside_vector_pool(h, monkeypatch):
    from app.memory.chroma_store import MemorySearchCandidate, RetrievalMatchKind
    from app.memory.semantic.contracts import SemanticOperation

    policy = resolved_memory_policy()
    h.reviewer.review = lambda *_: PrivacyReview(True, "ALLOW", STAMP.model_copy(
        update={"policy_version": policy.policy_version}))
    old_conversation = h.history.create_conversation("miori")
    old = save(h, candidate(source(h, "大阪に住んでいます。", conversation=old_conversation)))
    current = save(h, candidate(source(h, "東京に引っ越しました。"), value="東京"),
                   target=old, op=SemanticOperation.CHANGE)
    noise = [save(h, candidate(source(h, "私の好きな果物は桃です。"), predicate=f"好み{index}", value="桃"))
             for index in range(6)]
    legacy = ApprovedMemoryRepository(database_path=h.paths.persona_memory_sqlite_path,
        clock=h.store.clock, uuid_factory=uuid4, outbox_uuid_factory=uuid4)
    reader = WithSemanticReadRepository(CombinedMemoryReadRepository(legacy, h.store.episode_reader))
    reader.bind(SemanticReadRepository(h.store))
    monkeypatch.setattr("app.memory.rag_service.query_memories",
                        lambda *_a, **_k: [MemorySearchCandidate(str(item.id), 0.01) for item in noise])
    scanner, classifier = Mock(), Mock()
    scanner.scan.return_value = ScanSuccess(())
    classifier.classify.return_value = _assessment(SemanticClassification.NOT_SENSITIVE,
                                                  SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT)
    found = retrieve_prompt_memories("miori", "私は今どこに住んでいたかな？", policy,
        scanner=scanner, classifier=classifier, approved_repository=reader,
        embedder=FakeEmbedder(), chroma_path=h.paths.chroma_path,
        now=h.store.clock(), timezone="Asia/Tokyo").memories
    assert len(found) == 5
    assert [item.memory_id for item in found[:2]] == [str(current.id), str(old.id)]
    assert [item.match_kind for item in found[:2]] == [RetrievalMatchKind.RELATED, RetrievalMatchKind.LEXICAL]
    assert not reader.semantic.search_by_text(character_id="other", query="大阪に住んでいた")
    h.history.hard_delete_conversation("miori", old_conversation.conversation_id)
    assert all(item.id != old.id for item in reader.semantic.search_by_text(
        character_id="miori", query="大阪に住んでいた"))


def test_lexical_match_ignores_common_self_reference_and_preserves_precise_attribute():
    from app.memory.semantic.search import lexical_relevance
    assert lexical_relevance("私の今の住まいは？", predicate="好みの花", value="桜",
                             quotes=("私の好きな花は桜です",)) == 0
    assert lexical_relevance("私の居住地は？", predicate="居住地", value="東京", quotes=()) > 0
    assert lexical_relevance("私は今どこに住んでいたかな？", predicate="居住地", value="大阪",
                             quotes=("大阪に住んでいます。",)) > 0

def test_explicit_correction_resolves_connected_conflicts_but_keeps_other_attributes(h):
    from app.memory.semantic.contracts import SemanticOperation, SemanticStatus
    from app.memory.rag_service import _semantic_response_cautions

    policy = resolved_memory_policy()
    h.reviewer.review = lambda *_: PrivacyReview(True, "ALLOW", STAMP.model_copy(
        update={"policy_version": policy.policy_version}))
    first = save(h, candidate(source(h, "誕生日は6月12日"), predicate="誕生日", value="6月12日", fixed=True))
    second = save(h, candidate(source(h, "誕生日は6月13日"), predicate="誕生日", value="6月13日", fixed=True),
                  target=first, op=SemanticOperation.CONFLICT)
    third = save(h, candidate(source(h, "誕生日は6月14日"), predicate="誕生日", value="6月14日", fixed=True),
                 target=second, op=SemanticOperation.CONFLICT)
    birthplace = save(h, candidate(source(h, "出身地は大阪"), predicate="出身地", value="大阪", fixed=True))
    birthplace_other = save(h, candidate(source(h, "出身地は京都"), predicate="出身地", value="京都", fixed=True),
                           target=birthplace, op=SemanticOperation.CONFLICT)
    from tests.module.test_semantic_management import Index
    manager = SemanticMemoryManagement(h.store, Index())
    corrected = manager.correct(character_id="miori", record_id=second.id, version=second.content_version,
                                value="6月12日", key=uuid4())
    with h.repo.read() as tx:
        for old in (first, second, third):
            saved = tx.get("miori", old.id)
            assert saved.status is SemanticStatus.SUPERSEDED
            assert saved.proposition == old.proposition
            assert any(str(r.source_id)==str(old.id) and str(r.target_id)==corrected['id']
                       and r.relation is SemanticOperation.CORRECT for r in tx.relations("miori"))
        for unrelated in (birthplace, birthplace_other):
            assert tx.get("miori", unrelated.id).status is SemanticStatus.CONFLICTED
    legacy = ApprovedMemoryRepository(database_path=h.paths.persona_memory_sqlite_path,
        clock=h.store.clock, uuid_factory=uuid4, outbox_uuid_factory=uuid4)
    reader = WithSemanticReadRepository(CombinedMemoryReadRepository(legacy, h.store.episode_reader))
    reader.bind(SemanticReadRepository(h.store))
    scanner = Mock();scanner.scan.return_value = ScanSuccess(())
    assert not _semantic_response_cautions(reader, character="miori", query="私の誕生日は？", policy=policy, scanner=scanner)
    assert _semantic_response_cautions(reader, character="miori", query="私の出身地は？", policy=policy, scanner=scanner)