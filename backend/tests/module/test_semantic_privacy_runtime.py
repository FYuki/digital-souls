from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
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
    requested_models: list[str] = []
    closed_clients: list[object] = []
    original_close = main.OllamaClassifierClient.close

    def post(url: str, **kwargs: object) -> MagicMock:
        requests.append(url)
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        requested_models.append(str(payload["model"]))
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

    def close(client: object) -> None:
        closed_clients.append(client)
        original_close(client)

    monkeypatch.setenv("RAG_ENABLED", "false")
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "chat-only:9b")
    monkeypatch.setenv("OLLAMA_CLASSIFIER_MODEL", "classifier-only:4b")
    monkeypatch.setattr(main, "resolved_memory_policy", resolve_policy)
    monkeypatch.setattr(main, "create_privacy_scanner", create_scanner)
    http_client = MagicMock(spec=httpx.Client)
    http_client.post.side_effect = post
    monkeypatch.setattr(
        "app.inference.adapters.ollama.httpx.Client",
        lambda **_kwargs: http_client,
    )
    monkeypatch.setattr(main.OllamaClassifierClient, "close", close)

    async def exercise_lifespan() -> None:
        async with main.lifespan(main.app):
            classifier = main.app.state.semantic_privacy_classifier
            assessment = classifier.classify("合成した健康情報", QUERY_GATE)
            assert assessment.policy_version == actual_policy.policy_version
            assert assessment.model_id == "classifier-only:4b"

    asyncio.run(exercise_lifespan())

    resolve_policy.assert_called_once_with()
    create_scanner.assert_called_once_with(actual_policy.privacy)
    assert sum(url.endswith("/api/show") for url in requests) == 1
    assert sum(url.endswith("/api/chat") for url in requests) == 1
    assert requested_models == ["classifier-only:4b", "classifier-only:4b"]
    assert len(closed_clients) == 2
    assert closed_clients[0] is not closed_clients[1]
    assert not hasattr(main.app.state, "semantic_privacy_classifier")


def test_startup_continues_when_semantic_model_digest_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main
    from app.privacy.semantic.contracts import QUERY_GATE

    monkeypatch.setenv("RAG_ENABLED", "false")
    monkeypatch.setattr(
        main.OllamaClassifierClient,
        "resolve_model_digest",
        lambda _client, **_kwargs: (_ for _ in ()).throw(
            TimeoutError("lookup timeout")
        ),
    )

    async def exercise_lifespan() -> None:
        async with main.lifespan(main.app):
            assessment = main.app.state.semantic_privacy_classifier.classify(
                "合成した健康情報", QUERY_GATE
            )
            assert assessment.classification.value == "ABSTAIN"
            assert assessment.reason_code.value == "TIMEOUT"

    asyncio.run(exercise_lifespan())

    assert not hasattr(main.app.state, "semantic_privacy_classifier")
