from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import importlib
import json
from typing import cast

import pytest


def _token_module(contract: str):
    module_name = "app.livekit_transport.token"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name is None or not (
            error.name == module_name or module_name.startswith(f"{error.name}.")
        ):
            raise
    pytest.fail(f"{module_name} must implement {contract}")


def _decode_claims(token: str) -> dict[str, object]:
    payload = token.split(".")[1]
    padding = "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(payload + padding)
    return cast(dict[str, object], json.loads(decoded))


def test_sdk_user_token_has_fixed_ttl_and_least_privilege_grants() -> None:
    module = _token_module("fixed user-token TTL and least-privilege grants")
    signer = module.LiveKitTokenSigner(
        api_key="test-key",
        api_secret="LIVEKIT_SECRET_SENTINEL",
        utc_now=lambda: datetime(2026, 8, 27, tzinfo=UTC),
    )

    token = asyncio.run(
        signer.issue(
            identity="user-20000000-0000-4000-8000-000000000010",
            room="voice-20000000-0000-4000-8000-000000000010",
            ttl_seconds=90,
            grant={
                "room_join": True,
                "can_subscribe": True,
                "can_publish": True,
                "can_publish_data": True,
                "can_publish_sources": ["microphone"],
            },
        )
    )
    claims = _decode_claims(token)
    video = cast(dict[str, object], claims["video"])

    assert cast(int, claims["exp"]) - cast(int, claims["nbf"]) == 90
    assert claims["sub"] == "user-20000000-0000-4000-8000-000000000010"
    assert video == {
        "room": "voice-20000000-0000-4000-8000-000000000010",
        "roomJoin": True,
        "canSubscribe": True,
        "canPublish": True,
        "canPublishData": True,
        "canPublishSources": ["microphone"],
    }


def test_sdk_character_token_has_fixed_ttl_and_microphone_publish_grant() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    signer = production.ProductionTokenSigner(
        "test-key", "LIVEKIT_SECRET_SENTINEL"
    )

    token = asyncio.run(
        signer.issue_token(
            {
                "identity": (
                    "character-miori-20000000-0000-4000-8000-000000000010"
                ),
                "room": "voice-20000000-0000-4000-8000-000000000010",
                "ttl_seconds": 90,
                "can_subscribe": True,
                "can_publish": True,
                "can_publish_data": True,
                "can_publish_sources": ["microphone"],
            }
        )
    )
    claims = _decode_claims(token)
    video = cast(dict[str, object], claims["video"])

    assert cast(int, claims["exp"]) - cast(int, claims["nbf"]) == 90
    assert video == {
        "room": "voice-20000000-0000-4000-8000-000000000010",
        "roomJoin": True,
        "canSubscribe": True,
        "canPublish": True,
        "canPublishData": True,
        "canPublishSources": ["microphone"],
    }
