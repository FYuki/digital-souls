"""LiveKit本番資源の環境設定解決とcomposition root。

token・Room操作、Core factory、delivery、microphone bridge、reader、
session資源所有は各責務moduleが担う。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.characters.loader import load_character_card
from app.livekit_transport.bootstrap import (
    BOOTSTRAP_TIMEOUT_SECONDS,
    BootstrapService,
    CharacterConversationBindingValidator,
    ConversationRepository,
    InMemorySessionBindingRepository,
)
from app.livekit_transport.core_factory import _CoreSessionFactory
from app.livekit_transport.session_runtime import _ScreenSessionRevoker
from app.voice_metrics import JsonlTraceRecorder, MeasurementKind

if TYPE_CHECKING:
    import livekit.api as livekit_api

    from app.livekit_transport.core_delivery import ProductionCoreEventInbox
    from app.livekit_transport.production_sdk import (
        ProductionRoomManager,
        ProductionTokenSigner,
    )
    from app.livekit_transport.session_runtime import ProductionRuntimeManager

LIVEKIT_URL_ENV = "LIVEKIT_URL"
LIVEKIT_API_KEY_ENV = "LIVEKIT_API_KEY"
LIVEKIT_API_SECRET_ENV = "LIVEKIT_API_SECRET"


class LiveKitConfigurationError(RuntimeError):
    pass


def resolve_livekit_settings() -> tuple[str, str, str] | None:
    livekit_url = os.environ.get(LIVEKIT_URL_ENV)
    api_key = os.environ.get(LIVEKIT_API_KEY_ENV)
    api_secret = os.environ.get(LIVEKIT_API_SECRET_ENV)
    if livekit_url is None and api_key is None and api_secret is None:
        return None
    if (
        livekit_url is None
        or not livekit_url.strip()
        or api_key is None
        or not api_key.strip()
        or api_secret is None
        or not api_secret.strip()
    ):
        raise LiveKitConfigurationError("LiveKit configuration is incomplete")
    return livekit_url, api_key, api_secret


@dataclass(frozen=True)
class ProductionResources:
    """LiveKit本番資源の構築結果。app.stateへの公開は呼出側の責務。"""

    api: livekit_api.LiveKitAPI
    room_manager: ProductionRoomManager
    session_repository: InMemorySessionBindingRepository
    runtime_manager: ProductionRuntimeManager
    token_signer: ProductionTokenSigner
    bootstrap_service: BootstrapService
    core_events: ProductionCoreEventInbox
    url: str


async def configure_production_resources(
    *,
    core_session_factory: _CoreSessionFactory | None,
    screen_session_revoker: _ScreenSessionRevoker,
    session_trace_recorder: JsonlTraceRecorder | None,
    measurement_kind: MeasurementKind,
    conversations: ConversationRepository,
) -> ProductionResources | None:
    from app.livekit_transport.audio_probe import audio_probe_enabled
    from app.livekit_transport.core_delivery import ProductionCoreEventInbox
    from app.livekit_transport.production_sdk import (
        ProductionRoomManager,
        ProductionTokenSigner,
        livekit_api_module,
    )
    from app.livekit_transport.session_runtime import ProductionRuntimeManager

    settings = resolve_livekit_settings()
    if settings is None:
        return None
    if core_session_factory is None:
        raise RuntimeError("Conversation Core session factory is required")
    livekit_url, api_key, api_secret = settings
    api = livekit_api_module()

    client: livekit_api.LiveKitAPI = api.LiveKitAPI(livekit_url, api_key, api_secret)
    room_manager = ProductionRoomManager(client)
    signer = ProductionTokenSigner(api_key, api_secret)
    sessions = InMemorySessionBindingRepository()
    core_events = ProductionCoreEventInbox()
    runtime = ProductionRuntimeManager(
        livekit_url=livekit_url,
        signer=signer,
        room_manager=room_manager,
        session_repository=sessions,
        core_port=core_events,
        core_session_factory=core_session_factory,
        screen_session_revoker=screen_session_revoker,
        audio_probe_enabled=audio_probe_enabled(os.environ, livekit_url),
        session_trace_recorder=session_trace_recorder,
        measurement_kind=measurement_kind,
    )
    validator = CharacterConversationBindingValidator(
        character_loader=load_character_card,
        conversations=conversations,
    )
    bootstrap = BootstrapService(
        session_repository=sessions,
        room_manager=room_manager,
        runtime_manager=runtime,
        token_signer=signer,
        timeout_seconds=BOOTSTRAP_TIMEOUT_SECONDS,
        binding_validator=validator,
    )
    return ProductionResources(
        api=client,
        room_manager=room_manager,
        session_repository=sessions,
        runtime_manager=runtime,
        token_signer=signer,
        bootstrap_service=bootstrap,
        core_events=core_events,
        url=livekit_url,
    )
