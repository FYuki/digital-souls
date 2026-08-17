from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import pytest


_DIGEST_HEX = "d" * 64


def test_startup_resolves_semantic_dependencies_once_and_cleans_up_state(
    monkeypatch: pytest.MonkeyPatch,
    semantic_model_digest_http: None,
) -> None:
    import app.main as main
    from app.privacy.semantic.contracts import QUERY_GATE

    actual_policy = main.resolved_memory_policy()
    resolve_policy = MagicMock(return_value=actual_policy)
    create_scanner = MagicMock(wraps=main.create_privacy_scanner)
    requests: list[str] = []

    def post(url: str, **kwargs: object) -> MagicMock:
        requests.append(url)
        response = MagicMock()
        response.raise_for_status.return_value = None
        if url.endswith("/api/show"):
            response.json.return_value = {
                "modelfile": f"FROM /models/blobs/sha256-{_DIGEST_HEX}"
            }
        elif url.endswith("/api/chat"):
            response.json.return_value = {
                "message": {
                    "content": (
                        '{"classification":"SENSITIVE",'
                        '"subject_scope":"SELF","category":"HEALTH",'
                        '"reason_code":"SENSITIVE_CONTENT"}'
                    )
                }
            }
        else:
            raise AssertionError(f"unexpected Ollama endpoint: {url}")
        return response

    monkeypatch.setenv("RAG_ENABLED", "false")
    monkeypatch.setattr(main, "resolved_memory_policy", resolve_policy)
    monkeypatch.setattr(main, "create_privacy_scanner", create_scanner)
    monkeypatch.setattr(
        "app.privacy.semantic.ollama_classifier_client.httpx.post", post
    )

    with TestClient(main.app):
        classifier = main.app.state.semantic_privacy_classifier
        assessment = classifier.classify("合成した健康情報", QUERY_GATE)
        assert assessment.policy_version == actual_policy.policy_version

    resolve_policy.assert_called_once_with()
    create_scanner.assert_called_once_with(actual_policy.privacy)
    assert sum(url.endswith("/api/show") for url in requests) == 1
    assert sum(url.endswith("/api/chat") for url in requests) == 1
    assert not hasattr(main.app.state, "semantic_privacy_classifier")
