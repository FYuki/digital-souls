"""本番の抽出・引用解決・privacy・SQLite処理位置を同じ経路で検証する。"""

import json
import sqlite3
from pydantic import ValidationError

import pytest

from app.memory.episodic.contracts import ExtractionIdentity
from app.memory.episodic.sources import InvalidConversationSource
from app.memory.semantic.extractor import SemanticExtractor
from app.memory.semantic.pipeline import SemanticDeferred, SemanticPipeline
from app.memory.semantic.read_repository import SemanticReadRepository
from app.memory.semantic.worker import SemanticWorkQueue, SemanticWorker, input_batches
from tests.module.test_semantic_store import candidate, h as h, save, source

IDENTITY = ExtractionIdentity(provider_id="test", model_id="synthetic", model_digest="fixture",
                              prompt_version="fixture")


class Client:
    def __init__(self, output=None):
        self.calls = []
        self.output = output
        self.callback = None

    def chat(self, messages, **kwargs):
        data = json.loads(messages[-1]["content"])
        self.calls.append(data)
        if self.callback:
            self.callback()
        if self.output is not None:
            return json.dumps(self.output, ensure_ascii=False)
        item = next(p for p in data["conversation"] if p["scope"] == "NEW_USER")
        return json.dumps({"items": [{
            "operation": "NEW", "target_key": None, "subject": "ユーザー", "predicate": "居住地",
            "value": "大阪", "mutability": "CHANGEABLE", "self_report": True, "confidence": 0.9,
            "sources": [{"source_key": item["key"], "quote": item["text"]}],
        }]}, ensure_ascii=False)


def pipeline(h, client):
    return SemanticPipeline(h.store, SemanticExtractor(client, timeout_seconds=5, max_output_tokens=1024),
                            timezone="Asia/Tokyo")


def process(h, client, **kwargs):
    pending = SemanticWorkQueue(h.store).peek()
    assert pending
    turn = pending.primary.turn
    return pipeline(h, client).process(
        character_id=turn.character_id, conversation_id=turn.conversation_id,
        source_id=turn.turn_id, revision=pending.primary.revision, batches=input_batches(pending),
        catalog=tuple(h.store.reconcile(turn.character_id)), extraction=IDENTITY, **kwargs,
    )


def test_saved_conversation_is_extracted_and_checkpoint_survives_queue_restart(h):
    s = source(h)
    client = Client()
    result = process(h, client)
    assert len(result.saved) == 1
    assert result.saved[0].stamp.extraction == IDENTITY
    assert result.saved[0].sources[0].source_id == s.source_id
    assert SemanticWorkQueue(h.store).peek() is None
    assert len(SemanticReadRepository(h.store).list_active(character_id="miori")) == 1
    assert SemanticReadRepository(h.store).list_active(character_id="another") == []


def test_empty_result_advances_checkpoint_but_transient_error_does_not(h):
    source(h, "今日は雨だった")
    with pytest.raises(ValidationError):
        class Broken(Client):
            def chat(self, *args, **kwargs):
                return "{"
        process(h, Broken())
    assert SemanticWorkQueue(h.store).has_pending()
    assert process(h, Client({"items": []})).saved == ()
    assert not SemanticWorkQueue(h.store).has_pending()


def test_source_edit_during_inference_rolls_back_and_new_revision_is_pending(h):
    s = source(h)
    client = Client()
    def edit():
        with sqlite3.connect(h.paths.sqlite_path) as connection:
            connection.execute("UPDATE conversation_turns SET user_content='東京へ引っ越した' WHERE turn_id=?",
                               (str(s.source_id),))
    client.callback = edit
    with pytest.raises(InvalidConversationSource):
        process(h, client)
    with h.repo.read() as tx:
        assert tx.list_records("miori") == ()
        assert not tx.processed("miori", s.conversation_id, s.source_id, s.revision)
    assert SemanticWorkQueue(h.store).peek().primary.revision > s.revision


def test_prior_assistant_context_cannot_alone_ground_a_new_candidate(h):
    old = source(h, "私の住まいを当てて")
    with h.repo.transaction() as tx:
        tx.mark_processed("miori", old.conversation_id, old.source_id, old.revision)
    source(h, "そうなの？")
    bad = Client({"items": [{
        "operation": "NEW", "target_key": None, "subject": "ユーザー", "predicate": "居住地",
        "value": "大阪", "mutability": "CHANGEABLE", "self_report": True, "confidence": 0.9,
        "sources": [{"source_key": "c0:assistant", "quote": "わかった"}],
    }]})
    with pytest.raises(ValueError):
        process(h, bad)
    assert SemanticWorkQueue(h.store).has_pending()
    with h.repo.read() as tx:
        assert tx.list_records("miori") == ()


def test_worker_defers_during_chat_and_resumes_without_losing_work(h):
    source(h)
    client = Client()
    available = False
    worker = SemanticWorker(queue=SemanticWorkQueue(h.store), pipeline=pipeline(h, client),
                            identity=lambda: IDENTITY, priority_available=lambda: available)
    assert worker.process_next(should_stop=lambda: False) is False
    assert client.calls == []
    available = True
    assert worker.process_next(should_stop=lambda: False) is True
    assert not SemanticWorkQueue(h.store).has_pending()


def test_chat_arriving_after_inference_defers_before_privacy_and_checkpoint(h):
    source(h)
    client = Client()
    deferred = False
    def on_call():
        nonlocal deferred
        deferred = True
    client.callback = on_call
    with pytest.raises(SemanticDeferred):
        process(h, client, should_defer=lambda: deferred)
    assert SemanticWorkQueue(h.store).has_pending()
    with h.repo.read() as tx:
        assert tx.list_records("miori") == ()


def test_privacy_denial_is_processed_without_storing_body(h):
    source(h)
    h.reviewer.allowed = False
    result = process(h, Client())
    assert result.rejected == 1
    assert result.saved == ()
    assert not SemanticWorkQueue(h.store).has_pending()


def test_source_deletion_invalidates_retrieval_even_before_index_sync(h):
    s = source(h)
    record = process(h, Client()).saved[0]
    reader = SemanticReadRepository(h.store)
    assert reader.get(character_id="miori", memory_id=record.id).normalized_text
    with sqlite3.connect(h.paths.sqlite_path) as connection:
        connection.execute("DELETE FROM conversation_turns WHERE turn_id=?", (str(s.source_id),))
    assert reader.list_active(character_id="miori") == []
    assert reader.get(character_id="miori", memory_id=record.id).normalized_text == ""


def test_changed_residence_keeps_history_distinct_from_current(h):
    from app.memory.semantic.contracts import SemanticOperation
    first = save(h, candidate(source(h)))
    second = save(h, candidate(source(h, "東京へ引っ越した"), value="東京"),
                  target=first, op=SemanticOperation.CHANGE)
    records = {r.id: r for r in SemanticReadRepository(h.store).list_active(character_id="miori")}
    assert records[first.id].normalized_text.startswith("過去の状態")
    assert records[second.id].normalized_text == "ユーザーの居住地は東京"


def test_input_chunks_cover_all_primary_text_and_overlap(h):
    text = "あ" * 12000 + "私は大阪に住んでいる"
    source(h, text)
    pending = SemanticWorkQueue(h.store).peek()
    batches = input_batches(pending, max_characters=1000)
    primary = [b[-1].fragment for b in batches]
    assert primary[0].start == 0 and primary[-1].end == len(text)
    assert all(left.end > right.start for left, right in zip(primary, primary[1:]))
    assert all(p.end - p.start <= 1000 for p in primary)


def test_response_schema_restricts_operation_and_target_pairs():
    from jsonschema import Draft202012Validator
    from app.memory.semantic.extractor import _response_schema
    item = {"operation": "NEW", "target_key": None, "subject": "ユーザー", "predicate": "居住地",
            "value": "大阪", "mutability": "CHANGEABLE", "self_report": True, "confidence": 1,
            "sources": [{"source_key": "u0", "quote": "大阪に住んでいます"}]}
    empty = Draft202012Validator(_response_schema(()))
    assert empty.is_valid({"items": [item]})
    assert not empty.is_valid({"items": [{**item, "operation": "REAFFIRM", "target_key": "c0:assistant"}]})
    known = Draft202012Validator(_response_schema(("m0",)))
    assert known.is_valid({"items": [{**item, "operation": "REAFFIRM", "target_key": "m0"}]})
    assert not known.is_valid({"items": [{**item, "operation": "REAFFIRM", "target_key": None}]})
    assert not known.is_valid({"items": [{**item, "operation": "NEW", "target_key": "m0"}]})
    assert not known.is_valid({"items": [{**item, "operation": "CORRECT", "target_key": "m1"}]})


@pytest.mark.parametrize("operation,target,keys", [("NEW", None, ()), ("CHANGE", "m0", ("m0",))])
def test_self_report_subject_is_user_in_schema_and_decoded_output(operation, target, keys):
    from jsonschema import Draft202012Validator
    from app.memory.semantic.extractor import SemanticBatch, _response_schema

    item = {"operation": operation, "target_key": target, "subject": "居住地", "predicate": "居住地",
            "value": "大阪", "mutability": "CHANGEABLE", "self_report": True, "confidence": 1,
            "sources": [{"source_key": "u0", "quote": "大阪に住んでいます"}]}
    validator = Draft202012Validator(_response_schema(keys))
    assert not validator.is_valid({"items": [item]})
    with pytest.raises(ValidationError, match="self report must describe the user"):
        SemanticBatch.model_validate({"items": [item]})
    for valid in ({**item, "subject": "ユーザー"}, {**item, "subject": "富士山", "self_report": False}):
        assert validator.is_valid({"items": [valid]})
        assert SemanticBatch.model_validate({"items": [valid]}).items[0].subject == valid["subject"]

def test_unique_exact_quote_uses_actual_span_when_model_offset_is_wrong(h):
    from app.memory.semantic.extractor import EvidenceQuote, _resolve_quote
    source(h, "最近のことですが、大阪に住んでいます。")
    part = input_batches(SemanticWorkQueue(h.store).peek())[0][-1]
    quote = EvidenceQuote(source_key=part.key, quote="大阪に住んでいます。", start=0)
    resolved = _resolve_quote(quote, {part.key: part})
    assert resolved.span.start == len("最近のことですが、")
    assert resolved.span.end == len("最近のことですが、大阪に住んでいます。")


def test_ambiguous_quote_still_needs_valid_exact_offset_and_no_fuzzy_matching(h):
    from app.memory.semantic.extractor import EvidenceQuote, _resolve_quote
    source(h, "大阪です。大阪です。")
    part = input_batches(SemanticWorkQueue(h.store).peek())[0][-1]
    for quote in (EvidenceQuote(source_key=part.key, quote="大阪", start=1),
                  EvidenceQuote(source_key=part.key, quote="大阪"),
                  EvidenceQuote(source_key=part.key, quote="東京", start=0)):
        with pytest.raises(ValueError):
            _resolve_quote(quote, {part.key: part})
    exact = _resolve_quote(EvidenceQuote(source_key=part.key, quote="大阪", start=5), {part.key: part})
    assert exact.span.start == 5 and exact.span.end == 7