import importlib
from typing import Any

import pytest

from digital_souls_core.application import CoreError
from digital_souls_core.provider import LiteLLMProvider, litellm

from .support import TOOL, character

pytestmark = pytest.mark.it1


class RouteObserved(BaseException):
    """Stop actual SDK routing before it can acquire any transport."""


@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-5.4-mini", "gpt-6-astra"])
@pytest.mark.parametrize(
    "source,base,tools,effort,expected",
    [
        ("default", None, True, None, "responses"),
        ("default", None, False, None, "chat"),
        ("env", "https://api.openai.com/v1", True, None, "responses"),
        ("env", "https://synthetic.invalid/v1", True, None, "chat"),
        ("global", "https://region.privatelink.api.openai.com/v1", True, None, "responses"),
        ("global", "https://synthetic.invalid/v1", True, None, "chat"),
        ("argument", "https://api.openai.com/v1", True, None, "responses"),
        ("argument", "https://synthetic.invalid/v1", True, "medium", "responses"),
        ("argument", "https://api.openai.com/v1", True, "none", "chat"),
    ],
)
async def test_stream_guard_matches_actual_sdk_request_route(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    source: str,
    base: str | None,
    tools: bool,
    effort: str | None,
    expected: str,
) -> None:
    main = importlib.import_module("litellm.main")
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(litellm, "api_base", None)
    monkeypatch.setattr(litellm, "route_all_chat_openai_to_responses", False)
    if source == "env":
        assert base is not None
        monkeypatch.setenv("OPENAI_BASE_URL", base)
    if source == "global":
        monkeypatch.setattr(litellm, "api_base", base)
    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": "synthetic"}],
        "stream": True,
    }
    if tools:
        payload["tools"] = [TOOL]
    if effort is not None:
        payload["reasoning_effort"] = effort
    if source == "argument":
        payload["api_base"] = base
    original = main.responses_api_bridge_check
    observed: list[str] = []
    calls = 0

    def probe(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        # The real SDK's second check includes tools/reasoning after normalization.
        if calls == 2:
            observed.append(result[0].get("mode"))
            raise RouteObserved
        return result

    monkeypatch.setattr(main, "responses_api_bridge_check", probe)
    with pytest.raises(RouteObserved):
        await litellm.acompletion(model=f"openai/{model}", api_key="synthetic", **payload)
    assert observed == [expected]

    async def reached_sdk(**kwargs: Any) -> Any:
        raise RouteObserved

    monkeypatch.setattr(litellm, "acompletion", reached_sdk)
    profile = character(native=True).config.profile.model_copy(update={"model": f"openai/{model}"})
    if expected == "responses":
        with pytest.raises(CoreError) as info:
            await anext(LiteLLMProvider().stream(profile, payload))
        assert info.value.code == "unsupported_stream_provider"
    else:
        with pytest.raises(RouteObserved):
            await anext(LiteLLMProvider().stream(profile, payload))
