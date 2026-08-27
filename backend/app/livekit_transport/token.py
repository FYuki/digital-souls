from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import hmac
import json
from typing import Callable


def _urlencode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class LiveKitTokenSigner:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._utc_now = utc_now

    async def issue(
        self,
        *,
        identity: str,
        room: str,
        ttl_seconds: int,
        grant: dict[str, object],
    ) -> str:
        issued_at = int(self._utc_now().timestamp())
        video: dict[str, object] = {"room": room}
        grant_names = {
            "room_join": "roomJoin",
            "can_subscribe": "canSubscribe",
            "can_publish": "canPublish",
            "can_publish_data": "canPublishData",
            "can_publish_sources": "canPublishSources",
        }
        for source_name, claim_name in grant_names.items():
            if source_name in grant:
                video[claim_name] = grant[source_name]
        header = {"alg": "HS256", "typ": "JWT"}
        claims = {
            "iss": self._api_key,
            "sub": identity,
            "nbf": issued_at,
            "exp": issued_at + ttl_seconds,
            "video": video,
        }
        encoded_header = _urlencode(json.dumps(header, separators=(",", ":")).encode())
        encoded_claims = _urlencode(json.dumps(claims, separators=(",", ":")).encode())
        signing_input = f"{encoded_header}.{encoded_claims}".encode()
        signature = hmac.new(
            self._api_secret.encode(), signing_input, hashlib.sha256
        ).digest()
        return f"{encoded_header}.{encoded_claims}.{_urlencode(signature)}"
