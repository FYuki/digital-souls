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
