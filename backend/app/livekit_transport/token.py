from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import importlib
import json


@dataclass(frozen=True)
class IssuedToken:
    token: str
    expires_at: datetime


class LiveKitTokenSigner:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret

    async def issue(
        self,
        *,
        identity: str,
        room: str,
        ttl_seconds: int,
        grant: dict[str, object],
    ) -> str:
        return (
            await self.issue_with_expiration(
                identity=identity,
                room=room,
                ttl_seconds=ttl_seconds,
                grant=grant,
            )
        ).token

    async def issue_with_expiration(
        self,
        *,
        identity: str,
        room: str,
        ttl_seconds: int,
        grant: dict[str, object],
    ) -> IssuedToken:
        api = importlib.import_module("livekit.api")
        sources = grant.get("can_publish_sources")
        if sources is not None and (
            not isinstance(sources, list)
            or not all(isinstance(source, str) for source in sources)
        ):
            raise TypeError("can_publish_sources must be a list of strings")
        token = (
            api.AccessToken(self._api_key, self._api_secret)
            .with_identity(identity)
            .with_ttl(timedelta(seconds=ttl_seconds))
            .with_grants(
                api.VideoGrants(
                    room=room,
                    room_join=bool(grant.get("room_join", False)),
                    can_subscribe=bool(grant.get("can_subscribe", False)),
                    can_publish=bool(grant.get("can_publish", False)),
                    can_publish_data=bool(grant.get("can_publish_data", False)),
                    can_publish_sources=sources,
                )
            )
            .to_jwt()
        )
        encoded_claims = token.split(".")[1]
        padding = "=" * (-len(encoded_claims) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded_claims + padding))
        expires_at = claims.get("exp") if isinstance(claims, dict) else None
        if not isinstance(expires_at, int):
            raise RuntimeError("LiveKit token does not contain an integer expiration")
        return IssuedToken(
            token=token,
            expires_at=datetime.fromtimestamp(expires_at, UTC),
        )
