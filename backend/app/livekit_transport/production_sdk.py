"""LiveKit SDKの遅延import、token発行、Room API操作を所有する。

livekit SDKは任意依存のため、importは利用時まで遅らせる。
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from app.livekit_transport.token import IssuedToken, LiveKitTokenSigner

if TYPE_CHECKING:
    import livekit.api as livekit_api

PCM_SAMPLE_RATE = 48_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH_BYTES = 2


def livekit_rtc_module() -> Any:
    return importlib.import_module("livekit.rtc")


def livekit_api_module() -> Any:
    return importlib.import_module("livekit.api")


def _required_int(value: object, field: str) -> int:
    if not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return value


def _required_string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{field} must be a list of strings")
    return value


class ProductionTokenSigner:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self._signer = LiveKitTokenSigner(api_key=api_key, api_secret=api_secret)

    async def issue(self, **request: object) -> str:
        return await self._signer.issue(**request)  # type: ignore[arg-type]

    async def issue_with_expiration(self, **request: object) -> IssuedToken:
        return await self._signer.issue_with_expiration(**request)  # type: ignore[arg-type]

    async def issue_token(self, request: dict[str, object]) -> str:
        return await self.issue(
            identity=str(request["identity"]),
            room=str(request["room"]),
            ttl_seconds=_required_int(request["ttl_seconds"], "ttl_seconds"),
            grant={
                "room_join": True,
                "can_subscribe": bool(request["can_subscribe"]),
                "can_publish": bool(request["can_publish"]),
                "can_publish_data": bool(request["can_publish_data"]),
                "can_publish_sources": _required_string_list(
                    request["can_publish_sources"], "can_publish_sources"
                ),
            },
        )


class ProductionRoomManager:
    def __init__(self, livekit_api: livekit_api.LiveKitAPI) -> None:
        self._api = livekit_api

    async def create(self, room_name: str) -> None:
        api = livekit_api_module()

        await self._api.room.create_room(api.CreateRoomRequest(name=room_name))

    async def delete(self, room_name: str) -> None:
        api = livekit_api_module()

        try:
            await self._api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception as error:
            # 最後のparticipant切断時にLiveKitがRoomを先に削除することがある。
            # その場合だけは所有resourceが既に消えているためcleanup成功とする。
            if (
                getattr(error, "code", None) == "not_found"
                and getattr(error, "status", None) == 404
            ):
                return
            raise
