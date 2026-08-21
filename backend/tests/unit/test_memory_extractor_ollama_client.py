from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest


PRIVATE_VALUES = (
    "synthetic-private-source",
    "synthetic-private-prompt",
    "synthetic-private-candidate",
    "synthetic-private-raw-output",
)


def _response(body: object) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


def test_client_requests_strict_schema_at_temperature_zero_with_output_limit() -> None:
    from app.memory.formation.ollama_client import OllamaMemoryExtractorClient

    schema = {
        "type": "object",
        "properties": {"candidates": {"type": "array", "maxItems": 3}},
        "required": ["candidates"],
        "additionalProperties": False,
    }
    messages = ({"role": "user", "content": "synthetic input"},)
    response = _response({"message": {"content": '{"candidates": []}'}})

    with patch(
        "app.memory.formation.ollama_client.httpx.Client.post",
        return_value=response,
    ) as post:
        result = OllamaMemoryExtractorClient(model_id="gemma4:e4b").chat(
            messages,
            json_schema=schema,
            timeout_seconds=15,
            max_output_tokens=321,
        )

    assert result == '{"candidates": []}'
    payload = post.call_args.kwargs["json"]
    assert payload["model"] == "gemma4:e4b"
    assert payload["format"] == schema
    assert payload["options"] == {"temperature": 0, "num_predict": 321}
    assert payload["messages"] == list(messages)
    timeout = post.call_args.kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 15


def test_transport_and_response_failures_never_expose_private_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.memory.formation.ollama_client import OllamaMemoryExtractorClient

    response = _response({"message": " ".join(PRIVATE_VALUES)})
    caplog.set_level("DEBUG")

    with patch(
        "app.memory.formation.ollama_client.httpx.Client.post",
        return_value=response,
    ):
        with pytest.raises(Exception) as exc_info:
            OllamaMemoryExtractorClient(model_id="gemma4:e4b").chat(
                ({"role": "user", "content": PRIVATE_VALUES[0]},),
                json_schema={"type": "object"},
                timeout_seconds=15,
                max_output_tokens=321,
            )

    observed = "\n".join((str(exc_info.value), repr(exc_info.value), caplog.text))
    for private_value in PRIVATE_VALUES:
        assert private_value not in observed
