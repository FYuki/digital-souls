"""意味記憶の監査・UI訂正制限・完全削除をHTTP境界から確認する。"""

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.memory.semantic.management import SemanticMemoryManagement
from app.routers.semantic_memories import router
from tests.module.test_semantic_store import candidate, derived, h as h, save, source


class Index:
    def __init__(self):
        self.deleted = []

    def delete_after_commit(self, **kwargs):
        self.deleted.append(kwargs)


@pytest.fixture
def client(h):
    app = FastAPI()
    app.state.semantic_memory_management = SemanticMemoryManagement(h.store, Index())
    app.include_router(router)
    return TestClient(app)


def url(record, character="miori"):
    return f"/characters/{character}/semantic-memories/{record.id}"


def correction(record, value="東京", key=None):
    return {"expected_version": record.content_version, "value": value, "idempotency_key": key or str(uuid4())}


def test_self_report_can_be_corrected_with_history_and_idempotent_response(h, client):
    record = save(h, candidate(source(h)))
    request = correction(record)
    response = client.patch(url(record), json=request)
    assert response.status_code == 200
    current = response.json()
    assert current["id"] != str(record.id)
    assert current["proposition"]["value"] == "東京"
    assert current["sources"][0]["kind"] == "MANUAL"
    assert current["relations"][0]["relation"] == "CORRECT"
    replay = client.patch(url(record), json=request)
    assert replay.status_code == 200 and replay.json()["id"] == current["id"]
    assert client.patch(url(record), json={**request, "value": "京都"}).status_code == 409
    assert client.get(url(record)).json()["status"] == "SUPERSEDED"


@pytest.mark.parametrize("is_derived", [False, True])
def test_general_knowledge_and_experience_derived_are_not_ui_correctable(h, client, is_derived):
    roots = derived(h) if is_derived else source(h, "紅茶は飲み物です")
    record = save(h, candidate(roots, derived=is_derived, self_report=False))
    assert client.get(url(record)).json()["can_correct"] is False
    response = client.patch(url(record), json=correction(record))
    assert response.status_code == 422
    assert "東京" not in response.text


def test_cross_character_management_and_stale_mutations_are_rejected(h, client):
    record = save(h, candidate(source(h)))
    assert client.get(url(record, "other")).status_code == 404
    assert client.patch(url(record, "other"), json=correction(record)).status_code == 404
    assert client.request("DELETE", url(record, "other"), json={"expected_version": 1}).status_code == 404
    assert client.patch(url(record), json={**correction(record), "expected_version": 9}).status_code == 409
    assert client.request("DELETE", url(record), json={"expected_version": 9}).status_code == 409


def test_delete_erases_body_from_audit_and_preserves_original_conversation(h, client):
    root = source(h)
    record = save(h, candidate(root))
    assert client.request("DELETE", url(record), json={"expected_version": 1}).status_code == 204
    audit = client.get(url(record)).json()
    assert audit["status"] == "DELETED" and audit["proposition"] is None
    assert all(v["content_erased"] for v in audit["versions"])
    assert "大阪" not in str(audit)
    assert h.history.list_turns("miori", root.conversation_id)
    assert client.request("DELETE", url(record), json={"expected_version": 1}).status_code == 204


def test_rejected_or_privacy_denied_correction_has_no_side_effect(h, client):
    record = save(h, candidate(source(h)))
    h.reviewer.allowed = False
    response = client.patch(url(record), json=correction(record, "保存禁止の合成内容"))
    assert response.status_code == 422
    assert "保存禁止の合成内容" not in response.text
    assert client.get(url(record)).json()["proposition"]["value"] == "大阪"
    assert client.patch(url(record), json={**correction(record), "self_report": False}).status_code == 422

def test_wrong_llm_correction_cannot_deactivate_generalization(h):
    from app.memory.semantic.contracts import SemanticOperation, SemanticStatus
    previous = save(h, candidate(derived(h), derived=True, self_report=False,
                                  predicate="コーヒーの好み", value="好き"))
    current = save(h, candidate(source(h, "コーヒーは苦手です"), predicate="コーヒーの好み", value="苦手"),
                   target=previous, op=SemanticOperation.CORRECT)
    with h.repo.read() as tx:
        assert tx.get("miori", previous.id).status is SemanticStatus.ACTIVE
        assert tx.get("miori", current.id).status is SemanticStatus.ACTIVE
        assert tx.relations("miori")[-1].relation is SemanticOperation.SELF_REPORT


@pytest.mark.parametrize("text", ["保存禁止です。私は福岡出身です。", "この話は記憶に残さないで。私は大阪在住です。"])
def test_storage_refusal_never_reaches_semantic_classifier(text):
    from app.memory.memory_policy import resolved_memory_policy
    from app.memory.semantic.privacy import SemanticPrivacyReviewer
    from app.privacy.scanner import create_privacy_scanner
    from app.memory.semantic.contracts import Proposition

    class ForbiddenClassifier:
        def classify(self, *_args):
            raise AssertionError("storage refusal must be deterministic")

    policy = resolved_memory_policy()
    reviewer = SemanticPrivacyReviewer(scanner=create_privacy_scanner(policy.privacy),
                                       classifier=ForbiddenClassifier(), policy=policy.privacy)
    review = reviewer.review(Proposition(subject="ユーザー", predicate="居住地", value="大阪",
                                        content="ユーザーの居住地は大阪", mutability="CHANGEABLE", self_report=True), (text,))
    assert not review.allowed and not review.retryable
