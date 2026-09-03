from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.inference import (
    InferenceCaller,
    InferenceError,
    InferenceErrorCategory,
    InferenceTarget,
)
from app.memory.inference_client import (
    MemoryInferenceEmbedder,
    StructuredMemoryInferenceClient,
)


def _settings(*, max_input_tokens: int = 8192) -> MagicMock:
    settings = MagicMock()
    settings.target.side_effect = lambda target: SimpleNamespace(
        max_input_tokens=max_input_tokens,
        reference=SimpleNamespace(
            provider_id="ollama",
            model_id=(
                "nomic-embed-text:latest"
                if target is InferenceTarget.EMBEDDING
                else "gemma4:e4b"
            ),
        ),
    )
    return settings


def test_structured_client_routes_memory_contract_through_fixed_target() -> None:
    router = MagicMock()
    router.generate_structured.return_value = SimpleNamespace(value={"candidates": []})
    client = StructuredMemoryInferenceClient(
        router=router,
        caller=InferenceCaller.MEMORY_EXTRACTION,
        target=InferenceTarget.MEMORY_EXTRACTION,
        settings=_settings(),
    )
    schema = {
        "type": "object",
        "properties": {"candidates": {"type": "array"}},
        "required": ["candidates"],
    }

    result = client.chat(
        ({"role": "user", "content": "synthetic input"},),
        json_schema=schema,
        timeout_seconds=12.0,
        max_output_tokens=321,
    )

    assert result == '{"candidates":[]}'
    call = router.generate_structured.call_args
    assert call.kwargs["caller"] is InferenceCaller.MEMORY_EXTRACTION
    assert call.kwargs["target"] is InferenceTarget.MEMORY_EXTRACTION
    assert call.kwargs["timeout_seconds"] == 12.0
    assert call.kwargs["response_schema"] == schema
    assert call.kwargs["messages"][0].content == "synthetic input"


def test_structured_client_rejects_oversize_input_before_provider_call() -> None:
    router = MagicMock()
    client = StructuredMemoryInferenceClient(
        router=router,
        caller=InferenceCaller.MEMORY_CONSOLIDATION,
        target=InferenceTarget.MEMORY_CONSOLIDATION,
        settings=_settings(max_input_tokens=1),
    )

    with pytest.raises(InferenceError) as exc_info:
        client.chat(
            ({"role": "user", "content": "long synthetic input"},),
            json_schema={"type": "object"},
            timeout_seconds=5.0,
            max_output_tokens=10,
        )

    assert exc_info.value.category is InferenceErrorCategory.INVALID_REQUEST
    assert exc_info.value.retryable is False
    router.generate_structured.assert_not_called()


def test_memory_embedder_routes_one_input_and_exposes_fingerprint_ids() -> None:
    router = MagicMock()
    router.embed.return_value = SimpleNamespace(vectors=((0.1, 0.2),))
    embedder = MemoryInferenceEmbedder(router=router, settings=_settings())

    assert embedder("検索したい発話") == [0.1, 0.2]
    assert (embedder.provider_id, embedder.model_id) == (
        "ollama",
        "nomic-embed-text:latest",
    )
    router.embed.assert_called_once_with(
        caller=InferenceCaller.MEMORY_INDEX,
        target=InferenceTarget.EMBEDDING,
        inputs=("検索したい発話",),
    )


def test_memory_embedder_rejects_an_unexpected_batch_shape() -> None:
    router = MagicMock()
    router.embed.return_value = SimpleNamespace(vectors=())
    embedder = MemoryInferenceEmbedder(router=router, settings=_settings())

    with pytest.raises(RuntimeError, match="exactly one vector"):
        embedder("検索したい発話")
