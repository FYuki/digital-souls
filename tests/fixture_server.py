"""Local HTTP fixture for external-client integration; never a real LLM.

Run from the checkout: uvicorn tests.fixture_server:create_fixture_app --factory
--host 127.0.0.1 --port 18080 --no-access-log
Supply a zero-argument function named fixture_ping for one tool round trip.
"""

from typing import Any

from fastapi import FastAPI

from digital_souls_core.api import create_app
from digital_souls_core.application import Inference
from digital_souls_core.character import Profile

from .support import FakeProvider, character, completion


class IntegrationFixture(FakeProvider):
    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        response = completion(profile.model)
        if payload["messages"][-1]["role"] == "tool":
            response["choices"][0]["message"]["content"] = "fixture tool result received"
        elif any(tool["function"]["name"] == "fixture_ping" for tool in payload.get("tools", [])):
            response["choices"][0] = {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "fixture_call_1",
                            "type": "function",
                            "function": {"name": "fixture_ping", "arguments": "{}"},
                        }
                    ],
                },
            }
        return response


def create_fixture_app() -> FastAPI:
    return create_app(Inference((character(), character("other")), IntegrationFixture()))


def create_token_fixture_app() -> FastAPI:
    """Synthetic chat template/tokenizer HTTP fixture; no model is loaded."""
    import json

    from fastapi import HTTPException, Request

    app = FastAPI()
    app.state.templates = []
    app.state.tokenizations = []

    @app.post("/apply-template")
    async def apply_template(request: Request) -> dict[str, str]:
        body = await request.json()
        app.state.templates.append(body)
        messages = body["messages"]
        # Gemma 4 rejects dialogue without a user, but accepts system-only input.
        if not messages or (
            any(message["role"] != "system" for message in messages)
            and not any(message["role"] == "user" for message in messages)
        ):
            raise HTTPException(500, "Unsupported synthetic dialogue")
        return {"prompt": json.dumps(body, ensure_ascii=False)}

    @app.post("/tokenize")
    async def tokenize(request: Request) -> dict[str, list[int]]:
        body = await request.json()
        app.state.tokenizations.append(body)
        prompt = json.loads(body["content"])
        # One per message and one per tool: fixture-only units, never model quality.
        return {"tokens": [1] * (len(prompt["messages"]) + len(prompt.get("tools", [])))}

    return app
