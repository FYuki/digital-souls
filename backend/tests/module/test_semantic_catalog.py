"""入力予算を超えた際も、別スレッドの関連属性を候補に残す。"""

from uuid import uuid4

import pytest

from app.memory.semantic.catalog import select_catalog
from tests.module.test_semantic_store import candidate, h as h, save, source


def test_catalog_retains_all_records_when_the_request_fits(h):
    record = save(h, candidate(source(h)))
    assert select_catalog((record,), conversation="新しい話題", fits=lambda _: True) == (record,)


def test_catalog_prefers_matching_attribute_over_unrelated_recent_values(h):
    base = save(h, candidate(source(h)))
    known = base.model_copy(update={
        "id": uuid4(), "proposition": base.proposition.model_copy(update={
            "predicate": "誕生日", "value": "5月10日", "content": "誕生日は5月10日",
        }),
    })
    unrelated = tuple(base.model_copy(update={
        "id": uuid4(), "proposition": base.proposition.model_copy(update={
            "predicate": f"趣味{index}", "value": "映画", "content": "映画が好き",
        }),
    }) for index in range(200))
    catalog = (*unrelated, known)
    chosen = select_catalog(catalog, conversation="あなたの誕生日は？ 私の誕生日は5月11日です",
                            fits=lambda records: len(records) <= 3)
    assert len(chosen) == 3
    assert chosen[0].id == known.id
    assert len(catalog) == 201


def test_catalog_fails_explicitly_if_even_input_without_records_exceeds_budget(h):
    record = save(h, candidate(source(h)))
    with pytest.raises(ValueError, match="configured token budget"):
        select_catalog((record,), conversation="長い発言", fits=lambda _: False)


def test_budget_reordered_catalog_keeps_the_canonical_target_identity(h):
    from app.memory.semantic.contracts import SemanticStatus
    from tests.module.test_semantic_runtime import Client, process

    old_root = source(h)
    unrelated = save(h, candidate(old_root, predicate="映画の好み", value="好き"))
    known = save(h, candidate(old_root))
    with h.repo.transaction() as tx:
        tx.mark_processed("miori", old_root.conversation_id, old_root.source_id, old_root.revision)
    source(h, "私の居住地は東京です")

    class BudgetClient(Client):
        def fits(self, messages, schema):
            import json
            return len(json.loads(messages[-1]["content"])["existing"]) <= 1

    client = BudgetClient({"items": [{
        "operation": "CHANGE", "target_key": "m0", "subject": "ユーザー", "predicate": "居住地",
        "value": "東京", "mutability": "CHANGEABLE", "self_report": True, "confidence": 1,
        "sources": [{"source_key": "u0", "quote": "私の居住地は東京です"}],
    }]})
    result = process(h, client)
    assert client.calls[0]["existing"][0]["predicate"] == "居住地"
    assert result.saved[0].proposition.value == "東京"
    with h.repo.read() as tx:
        assert tx.get("miori", known.id).status is SemanticStatus.HISTORICAL
        assert tx.get("miori", unrelated.id).status is SemanticStatus.ACTIVE
        assert any(relation.source_id == known.id and relation.target_id == result.saved[0].id
                   for relation in tx.relations("miori"))
