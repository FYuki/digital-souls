import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from digital_souls_core.contracts import CompletionInput
from digital_souls_core.history import TurnInput
from digital_souls_core.privacy_classifier import Assessment, LocalClassifier
from digital_souls_core.privacy_scan import POLICY_VERSION
from digital_souls_core.provider import LiteLLMProvider

from .privacy_support import assessment, local_profile
from .support import completion

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("mode", ["valid", "unsupported", "fenced", "invalid"])
async def test_fixed_schema_over_actual_sdk_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    sent: list[dict[str, Any]] = []
    model = Assessment
    raw = assessment()
    if mode == "fenced":
        raw = "```json\n" + raw + "\n```"
    elif mode == "invalid":
        raw = '{"unexpected":"synthetic"}'

    async def send(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:18080/v1/chat/completions"
        payload = json.loads(request.content)
        sent.append(payload)
        wrapper = payload["response_format"]
        assert wrapper["type"] == "json_schema"
        assert wrapper["json_schema"]["strict"] is True
        assert wrapper["json_schema"]["schema"] == model.model_json_schema()
        if mode == "unsupported":
            return httpx.Response(400, json={"error": {"message": "unsupported schema"}})
        result = completion("gemma4-12b")
        result["choices"][0]["message"]["content"] = raw
        return httpx.Response(200, json=result)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    classifier = LocalClassifier(LiteLLMProvider(), local_profile(), model_digest="synthetic")
    assert await classifier.safe("Synthetic tea", POLICY_VERSION) is (mode == "valid")
    assert len(sent) == 1  # No retry with weaker output constraints.


@pytest.mark.parametrize("contract", [CompletionInput, TurnInput])
def test_public_caller_cannot_supply_provider_output_schema(contract: Any) -> None:
    value: dict[str, Any] = {
        "messages": [{"role": "user", "content": "Synthetic"}],
        "response_format": {"type": "json_object"},
    }
    if contract is TurnInput:
        value.update(request_id="synthetic", expected_revision=0)
    with pytest.raises(ValidationError):
        contract.model_validate(value)
