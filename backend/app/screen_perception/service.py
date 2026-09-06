from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import time
from typing import Literal
from uuid import UUID, uuid4

from app.async_worker import run_sync
from app.inference import (
    InferenceCancellationToken,
    InferenceError,
    InferenceErrorCategory,
    InferenceImagePart,
    InferenceMessage,
    InferenceSettings,
    InferenceTarget,
    ProviderKind,
)
from app.inference.images import SCREEN_IMAGE_INPUT_LIMITS, validate_multimodal_messages
from app.inference.registry import ProviderRegistry
from app.screen_perception.detector import (
    ScreenReferenceDecision,
    ScreenReferenceHistoryItem,
    StructuredReferenceRouter,
    decide_screen_reference,
)
from app.screen_perception.provenance import ScreenLineage
from app.screen_perception.vision import VisionInferenceClient, VisionObservation


PROTOCOL_VERSION = "1.0"
LEASE_DURATION_MS = 15_000
HEARTBEAT_INTERVAL_MS = 5_000
CAPTURE_TIMEOUT_MS = 5_000
VISION_TIMEOUT_SECONDS = 30.0
REQUEST_TIMEOUT_MS = 45_000
SNAPSHOT_MAX_AGE_MS = 5_000
MAX_IMAGE_BYTES = 5_242_880
MAX_WIDTH = 2_560
MAX_HEIGHT = 2_560
MAX_PIXELS = 4_194_304
MAX_SESSIONS = 64
MAX_TERMINAL_REQUESTS = 256

ScreenSource = Literal[
    "explicit_ui", "natural_language_text", "natural_language_voice"
]
ScreenSurface = Literal["monitor", "window"]
Destination = Literal["local", "cloud", "unconfigured"]


class ScreenPerceptionError(RuntimeError):
    def __init__(
        self,
        reason_code: str,
        *,
        status_code: int = 409,
        stage: str = "session",
    ) -> None:
        self.reason_code = reason_code
        self.status_code = status_code
        self.stage = stage
        super().__init__(reason_code)


@dataclass(frozen=True)
class RoutingPolicy:
    revision: str
    vision_destination: Destination
    chat_destination: Literal["local", "cloud"]


@dataclass(frozen=True, repr=False)
class ScreenHistoryAccess:
    chat_destination: Literal["local", "cloud"]
    screen_session_id: UUID | None
    generation: int | None
    routing_revision: str
    cloud_derived_chat_consent: bool
    validity: InferenceCancellationToken = field(repr=False)

    def allows(self, lineage: ScreenLineage) -> bool:
        if self.chat_destination == "local":
            return True
        return (
            not self.validity.is_cancelled
            and self.cloud_derived_chat_consent
            and self.screen_session_id is not None
            and self.generation is not None
            and lineage.belongs_to(
                screen_session_id=self.screen_session_id,
                generation=self.generation,
                routing_revision=self.routing_revision,
            )
        )


@dataclass(frozen=True, repr=False)
class ScreenTurnMaterial:
    source: ScreenSource
    surface: ScreenSurface | None
    captured_at: datetime | None
    observation: VisionObservation | None
    unavailable_reason: str | None
    request_id: UUID | None
    validity: InferenceCancellationToken = field(repr=False)
    lineages: tuple[ScreenLineage, ...] = ()

    @property
    def is_current(self) -> bool:
        return not self.validity.is_cancelled


@dataclass(frozen=True, repr=False)
class CompletedScreenRequest:
    client_session_id: UUID
    character_id: str
    conversation_id: UUID
    question: str = field(repr=False)
    material: ScreenTurnMaterial


@dataclass
class _ScreenSession:
    screen_session_id: UUID
    client_session_id: UUID
    generation: int
    character_id: str
    conversation_id: UUID
    actual_surface: ScreenSurface
    routing_revision: str
    cloud_vision_consent: bool
    cloud_derived_chat_consent: bool
    lease_expires_monotonic: float
    pending_request_id: UUID | None = None
    revoked: bool = False
    validity: InferenceCancellationToken = field(
        default_factory=InferenceCancellationToken,
        repr=False,
    )


@dataclass
class _ScreenRequest:
    request_id: UUID
    turn_id: UUID
    session_id: UUID
    generation: int
    character_id: str
    conversation_id: UUID
    question: str = field(repr=False)
    source: ScreenSource
    target_hint: str | None
    requested_monotonic: float
    capture_deadline: datetime
    expires_monotonic: float
    future: asyncio.Future[ScreenTurnMaterial]
    cancellation: InferenceCancellationToken
    accepted_image_id: UUID | None = None


def resolve_routing_policy(
    settings: InferenceSettings, registry: ProviderRegistry
) -> RoutingPolicy:
    def destination(target: InferenceTarget) -> Destination:
        resolved = settings.targets.get(target)
        if resolved is None:
            return "unconfigured"
        kind = registry.descriptor(resolved.reference.provider_id).kind
        return "cloud" if kind is ProviderKind.CLOUD else "local"

    vision = destination(InferenceTarget.VISION)
    chat = destination(InferenceTarget.CHAT)
    if chat == "unconfigured":
        raise ValueError("chat inference target must be configured")
    normalized = json.dumps(
        {"chat": chat, "vision": vision},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return RoutingPolicy(
        revision=hashlib.sha256(normalized.encode()).hexdigest(),
        vision_destination=vision,
        chat_destination=chat,
    )


class ScreenPerceptionService:
    """画面画像と観測をrequest内だけに閉じ込めるCoreサービス。"""

    def __init__(
        self,
        *,
        vision: VisionInferenceClient,
        routing_policy: Callable[[], RoutingPolicy],
        validate_context: Callable[[str, UUID], None],
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
        reference_router: StructuredReferenceRouter | None = None,
    ) -> None:
        self._vision = vision
        self._routing_policy = routing_policy
        self._validate_context = validate_context
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._uuid = uuid_factory
        self._reference_router = reference_router
        self._sessions: dict[UUID, _ScreenSession] = {}
        self._active_by_client: dict[UUID, UUID] = {}
        self._requests: dict[UUID, _ScreenRequest] = {}
        self._terminal_request_reasons: dict[UUID, str] = {}
        self._terminal_request_order: deque[UUID] = deque()
        self._lock = asyncio.Lock()

    def routing_disclosure(self, client_session_id: UUID) -> dict[str, object]:
        policy = self._routing_policy()
        return {
            "protocol_version": PROTOCOL_VERSION,
            "type": "screen_routing_disclosed",
            "event_id": str(self._uuid()),
            "client_session_id": str(client_session_id),
            "routing_revision": policy.revision,
            "vision_destination": policy.vision_destination,
            "chat_destination": policy.chat_destination,
            "limits": {
                "allowed_mime_types": ["image/png", "image/jpeg"],
                "max_width": MAX_WIDTH,
                "max_height": MAX_HEIGHT,
                "max_pixels": MAX_PIXELS,
                "max_bytes": MAX_IMAGE_BYTES,
                "snapshot_max_age_ms": SNAPSHOT_MAX_AGE_MS,
                "capture_timeout_ms": CAPTURE_TIMEOUT_MS,
                "vision_timeout_ms": int(VISION_TIMEOUT_SECONDS * 1000),
                "request_timeout_ms": REQUEST_TIMEOUT_MS,
                "max_concurrency": 1,
            },
        }

    async def start_session(
        self,
        *,
        client_session_id: UUID,
        generation: int,
        character_id: str,
        conversation_id: UUID,
        requested_surface: ScreenSurface,
        actual_surface: ScreenSurface,
        routing_revision: str,
        cloud_vision_consent: bool,
        cloud_derived_chat_consent: bool,
    ) -> dict[str, object]:
        if requested_surface != actual_surface:
            raise ScreenPerceptionError("context_mismatch")
        self._validate_context(character_id, conversation_id)
        policy = self._routing_policy()
        self._require_routing_and_consent(
            policy,
            routing_revision,
            cloud_vision_consent,
            cloud_derived_chat_consent,
        )
        async with self._lock:
            self._purge_expired_locked()
            previous_id = self._active_by_client.get(client_session_id)
            if previous_id is not None:
                self._revoke_locked(previous_id, "target_change")
            if len(self._sessions) >= MAX_SESSIONS:
                raise ScreenPerceptionError(
                    "backend_unavailable", status_code=503
                )
            session_id = self._uuid()
            expires = self._monotonic() + LEASE_DURATION_MS / 1000
            session = _ScreenSession(
                screen_session_id=session_id,
                client_session_id=client_session_id,
                generation=generation,
                character_id=character_id,
                conversation_id=conversation_id,
                actual_surface=actual_surface,
                routing_revision=routing_revision,
                cloud_vision_consent=cloud_vision_consent,
                cloud_derived_chat_consent=cloud_derived_chat_consent,
                lease_expires_monotonic=expires,
            )
            self._sessions[session_id] = session
            self._active_by_client[client_session_id] = session_id
            lease_expires_at = self._utc_now() + timedelta(
                milliseconds=LEASE_DURATION_MS
            )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "type": "screen_session_started",
            "event_id": str(self._uuid()),
            "screen_session_id": str(session_id),
            "client_session_id": str(client_session_id),
            "generation": generation,
            "character_id": character_id,
            "conversation_id": str(conversation_id),
            "actual_surface": actual_surface,
            "routing_revision": routing_revision,
            "lease_expires_at": _format_utc(lease_expires_at),
            "lease_duration_ms": LEASE_DURATION_MS,
            "heartbeat_interval_ms": HEARTBEAT_INTERVAL_MS,
        }

    async def heartbeat(
        self, *, screen_session_id: UUID, client_session_id: UUID, generation: int
    ) -> dict[str, object]:
        async with self._lock:
            session = self._require_session_locked(
                screen_session_id, client_session_id, generation
            )
            self._require_current_routing_locked(session)
            session.lease_expires_monotonic = (
                self._monotonic() + LEASE_DURATION_MS / 1000
            )
            lease_expires_at = self._utc_now() + timedelta(
                milliseconds=LEASE_DURATION_MS
            )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "type": "screen_session_heartbeat_accepted",
            "event_id": str(self._uuid()),
            "screen_session_id": str(screen_session_id),
            "generation": generation,
            "lease_expires_at": _format_utc(lease_expires_at),
        }

    async def revoke(
        self,
        *,
        screen_session_id: UUID,
        client_session_id: UUID,
        generation: int,
        reason: str,
    ) -> dict[str, object]:
        async with self._lock:
            self._require_session_locked(
                screen_session_id, client_session_id, generation
            )
            self._revoke_locked(screen_session_id, reason)
        return {
            "protocol_version": PROTOCOL_VERSION,
            "type": "screen_session_revoked",
            "event_id": str(self._uuid()),
            "screen_session_id": str(screen_session_id),
            "generation": generation,
            "reason": reason,
        }

    async def begin_request(
        self,
        *,
        client_session_id: UUID,
        character_id: str,
        conversation_id: UUID,
        question: str,
        source: ScreenSource,
        target_hint: str | None = None,
    ) -> tuple[dict[str, object], asyncio.Future[ScreenTurnMaterial]]:
        async with self._lock:
            self._purge_expired_locked()
            session_id = self._active_by_client.get(client_session_id)
            if session_id is None:
                raise ScreenPerceptionError("session_not_found", stage="capture")
            session = self._require_session_locked(
                session_id, client_session_id, None
            )
            if (
                session.character_id != character_id
                or session.conversation_id != conversation_id
            ):
                raise ScreenPerceptionError("context_mismatch")
            policy = self._require_current_routing_locked(session)
            if policy.vision_destination == "unconfigured":
                raise ScreenPerceptionError("vision_unconfigured", stage="vision")
            self._require_routing_and_consent(
                policy,
                session.routing_revision,
                session.cloud_vision_consent,
                session.cloud_derived_chat_consent,
            )
            if session.pending_request_id is not None:
                self._cancel_request_locked(
                    session.pending_request_id, "request_cancelled"
                )
            request_id = self._uuid()
            turn_id = self._uuid()
            now = self._monotonic()
            capture_deadline = self._utc_now() + timedelta(
                milliseconds=CAPTURE_TIMEOUT_MS
            )
            future: asyncio.Future[ScreenTurnMaterial] = (
                asyncio.get_running_loop().create_future()
            )
            pending = _ScreenRequest(
                request_id=request_id,
                turn_id=turn_id,
                session_id=session.screen_session_id,
                generation=session.generation,
                character_id=character_id,
                conversation_id=conversation_id,
                question=question,
                source=source,
                target_hint=target_hint,
                requested_monotonic=now,
                capture_deadline=capture_deadline,
                expires_monotonic=now + REQUEST_TIMEOUT_MS / 1000,
                future=future,
                cancellation=InferenceCancellationToken(),
            )
            self._requests[request_id] = pending
            session.pending_request_id = request_id
        event = {
            "protocol_version": PROTOCOL_VERSION,
            "type": "screen_snapshot_requested",
            "event_id": str(self._uuid()),
            "screen_session_id": str(session.screen_session_id),
            "generation": session.generation,
            "request_id": str(request_id),
            "turn_id": str(turn_id),
            "source": source,
            "requested_at": _format_utc(self._utc_now()),
            "capture_deadline": _format_utc(capture_deadline),
        }
        return event, future

    async def accept_image(
        self,
        *,
        request_id: UUID,
        screen_session_id: UUID,
        client_session_id: UUID,
        generation: int,
        turn_id: UUID,
        image_id: UUID,
        actual_surface: ScreenSurface,
        captured_at: datetime,
        mime_type: str,
        width: int,
        height: int,
        image_data: bytes,
    ) -> tuple[dict[str, object], CompletedScreenRequest]:
        async with self._lock:
            pending, session = self._require_request_locked(
                request_id,
                screen_session_id,
                client_session_id,
                generation,
                turn_id,
            )
            if pending.accepted_image_id is not None:
                raise ScreenPerceptionError("duplicate_request", stage="upload")
            if actual_surface != session.actual_surface:
                raise ScreenPerceptionError("context_mismatch", stage="upload")
            captured_age = (self._utc_now() - captured_at).total_seconds() * 1000
            if captured_age < -1000 or captured_age > SNAPSHOT_MAX_AGE_MS:
                raise ScreenPerceptionError("request_expired", stage="upload")
        image = InferenceImagePart(
            data=image_data,
            mime_type=mime_type,
            width=width,
            height=height,
        )
        try:
            await run_sync(
                validate_multimodal_messages,
                (InferenceMessage("user", (image,)),),
                SCREEN_IMAGE_INPUT_LIMITS,
            )
        except InferenceError as error:
            raise ScreenPerceptionError(
                _inference_reason(error.category),
                status_code=422,
                stage="upload",
            ) from error
        async with self._lock:
            pending, session = self._require_request_locked(
                request_id,
                screen_session_id,
                client_session_id,
                generation,
                turn_id,
            )
            if pending.accepted_image_id is not None:
                raise ScreenPerceptionError("duplicate_request", stage="upload")
            pending.accepted_image_id = image_id
            self._require_provider_send_allowed_locked(pending, session)
        try:
            observation = await run_sync(
                self._vision.observe,
                question=pending.question,
                image=image,
                target_hint=pending.target_hint,
                timeout_seconds=VISION_TIMEOUT_SECONDS,
                cancellation_token=pending.cancellation,
            )
            material = ScreenTurnMaterial(
                source=pending.source,
                surface=session.actual_surface,
                captured_at=captured_at,
                observation=observation,
                unavailable_reason=None,
                request_id=request_id,
                lineages=(
                    ScreenLineage(
                        screen_lineage_id=self._uuid(),
                        origin_screen_session_id=session.screen_session_id,
                        origin_generation=session.generation,
                        origin_routing_revision=session.routing_revision,
                        source=pending.source,
                        surface=session.actual_surface,
                    ),
                ),
                validity=session.validity,
            )
        except InferenceError as error:
            material = ScreenTurnMaterial(
                source=pending.source,
                surface=session.actual_surface,
                captured_at=captured_at,
                observation=None,
                unavailable_reason=_inference_reason(error.category),
                request_id=request_id,
                validity=session.validity,
            )
        except Exception:
            material = ScreenTurnMaterial(
                source=pending.source,
                surface=session.actual_surface,
                captured_at=captured_at,
                observation=None,
                unavailable_reason="vision_unavailable",
                request_id=request_id,
                validity=session.validity,
            )
        async with self._lock:
            current, current_session = self._require_request_locked(
                request_id,
                screen_session_id,
                client_session_id,
                generation,
                turn_id,
            )
            self._require_provider_send_allowed_locked(current, current_session)
            self._finish_request_locked(current, material)
        accepted = {
            "protocol_version": PROTOCOL_VERSION,
            "type": "screen_snapshot_upload_accepted",
            "event_id": str(self._uuid()),
            "screen_session_id": str(screen_session_id),
            "generation": generation,
            "request_id": str(request_id),
            "image_id": str(image_id),
            "received_at": _format_utc(self._utc_now()),
        }
        return accepted, CompletedScreenRequest(
            client_session_id=client_session_id,
            character_id=pending.character_id,
            conversation_id=pending.conversation_id,
            question=pending.question,
            material=material,
        )

    async def fail_request(
        self,
        *,
        request_id: UUID,
        screen_session_id: UUID,
        client_session_id: UUID,
        generation: int,
        turn_id: UUID,
        reason_code: str,
    ) -> CompletedScreenRequest:
        async with self._lock:
            pending, session = self._require_request_locked(
                request_id,
                screen_session_id,
                client_session_id,
                generation,
                turn_id,
            )
            material = ScreenTurnMaterial(
                source=pending.source,
                surface=session.actual_surface,
                captured_at=None,
                observation=None,
                unavailable_reason=reason_code,
                request_id=request_id,
                validity=session.validity,
            )
            self._finish_request_locked(pending, material)
            return CompletedScreenRequest(
                client_session_id=client_session_id,
                character_id=pending.character_id,
                conversation_id=pending.conversation_id,
                question=pending.question,
                material=material,
            )

    async def await_voice_material(
        self,
        *,
        client_session_id: UUID | None,
        character_id: str,
        conversation_id: UUID,
        question: str,
        publish_request: Callable[[bytes], Awaitable[None]],
        history: tuple[ScreenReferenceHistoryItem, ...] = (),
    ) -> ScreenTurnMaterial | None:
        active = (
            None
            if client_session_id is None
            else await self.active_session_for_client(client_session_id)
        )
        authorized = active is not None and self._session_screen_use_authorized(active)
        decision = decide_screen_reference(
            question,
            sharing_active=active is not None,
            screen_use_authorized=authorized,
            history=history,
            router=self._reference_router,
            cloud_judge_history_allowed=self._cloud_history_allowed(active),
        )
        if decision.screen_candidate and decision.decision == "answer_without_screen":
            decision = replace(
                decision,
                unavailable_reason=self._screen_use_unavailable_reason(active),
            )
        if decision.decision == "clarify_reference":
            return ScreenTurnMaterial(
                source="natural_language_voice", surface=None, captured_at=None,
                observation=None, unavailable_reason="clarify_reference",
                request_id=None, validity=InferenceCancellationToken(),
            )
        if decision.unavailable_reason is not None:
            return ScreenTurnMaterial(
                source="natural_language_voice", surface=None, captured_at=None,
                observation=None, unavailable_reason=decision.unavailable_reason,
                request_id=None, validity=InferenceCancellationToken(),
            )
        if decision.decision != "inspect_screen":
            return None
        if client_session_id is None:
            return ScreenTurnMaterial(
                source="natural_language_voice",
                surface=None,
                captured_at=None,
                observation=None,
                unavailable_reason="session_not_found",
                request_id=None,
                validity=InferenceCancellationToken(),
            )
        try:
            event, future = await self.begin_request(
                client_session_id=client_session_id,
                character_id=character_id,
                conversation_id=conversation_id,
                question=question,
                source="natural_language_voice",
                target_hint=decision.target_hint,
            )
        except ScreenPerceptionError as error:
            return ScreenTurnMaterial(
                source="natural_language_voice",
                surface=None,
                captured_at=None,
                observation=None,
                unavailable_reason=error.reason_code,
                request_id=None,
                validity=InferenceCancellationToken(),
            )
        request_id = UUID(str(event["request_id"]))
        try:
            await publish_request(
                json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
            )
        except asyncio.CancelledError:
            async with self._lock:
                self._cancel_request_locked(request_id, "request_cancelled")
            raise
        except Exception:
            async with self._lock:
                self._cancel_request_locked(request_id, "backend_unavailable")
            return await future
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), REQUEST_TIMEOUT_MS / 1000
            )
        except asyncio.CancelledError:
            async with self._lock:
                self._cancel_request_locked(
                    request_id, "request_cancelled"
                )
            raise
        except TimeoutError:
            async with self._lock:
                self._cancel_request_locked(
                    request_id, "request_expired"
                )
            return ScreenTurnMaterial(
                source="natural_language_voice",
                surface=None,
                captured_at=None,
                observation=None,
                unavailable_reason="request_expired",
                request_id=request_id,
                validity=InferenceCancellationToken(),
            )

    async def active_session_for_client(
        self, client_session_id: UUID
    ) -> _ScreenSession | None:
        async with self._lock:
            self._purge_expired_locked()
            session_id = self._active_by_client.get(client_session_id)
            if session_id is None:
                return None
            session = self._sessions.get(session_id)
            return None if session is None or session.revoked else session

    async def decide_reference(
        self,
        *,
        client_session_id: UUID | None,
        question: str,
        explicit_ui: bool,
        history: tuple[ScreenReferenceHistoryItem, ...],
    ) -> ScreenReferenceDecision:
        active = (
            None
            if client_session_id is None
            else await self.active_session_for_client(client_session_id)
        )
        decision = decide_screen_reference(
            question,
            sharing_active=active is not None,
            screen_use_authorized=(
                active is not None and self._session_screen_use_authorized(active)
            ),
            explicit_ui=explicit_ui,
            history=history,
            router=self._reference_router,
            cloud_judge_history_allowed=self._cloud_history_allowed(active),
        )
        if decision.screen_candidate and decision.decision == "answer_without_screen":
            return replace(
                decision,
                unavailable_reason=self._screen_use_unavailable_reason(active),
            )
        return decision

    async def history_access(
        self, client_session_id: UUID | None
    ) -> ScreenHistoryAccess:
        policy = self._routing_policy()
        active = (
            None
            if client_session_id is None
            else await self.active_session_for_client(client_session_id)
        )
        if active is None:
            return ScreenHistoryAccess(
                chat_destination=policy.chat_destination,
                screen_session_id=None,
                generation=None,
                routing_revision=policy.revision,
                cloud_derived_chat_consent=False,
                validity=InferenceCancellationToken(),
            )
        return ScreenHistoryAccess(
            chat_destination=policy.chat_destination,
            screen_session_id=active.screen_session_id,
            generation=active.generation,
            routing_revision=active.routing_revision,
            cloud_derived_chat_consent=active.cloud_derived_chat_consent,
            validity=active.validity,
        )

    def _session_screen_use_authorized(self, session: _ScreenSession) -> bool:
        try:
            policy = self._routing_policy()
            self._require_routing_and_consent(
                policy,
                session.routing_revision,
                session.cloud_vision_consent,
                session.cloud_derived_chat_consent,
            )
        except ScreenPerceptionError:
            return False
        return policy.vision_destination != "unconfigured"

    def _screen_use_unavailable_reason(
        self, session: _ScreenSession | None
    ) -> str:
        if session is None:
            return "session_not_found"
        try:
            policy = self._routing_policy()
            self._require_routing_and_consent(
                policy,
                session.routing_revision,
                session.cloud_vision_consent,
                session.cloud_derived_chat_consent,
            )
        except ScreenPerceptionError as error:
            return error.reason_code
        if policy.vision_destination == "unconfigured":
            return "vision_unconfigured"
        return "vision_unavailable"

    def _cloud_history_allowed(self, session: _ScreenSession | None) -> bool:
        if session is None:
            return False
        policy = self._routing_policy()
        return policy.chat_destination == "local" or (
            session.cloud_derived_chat_consent
            and policy.revision == session.routing_revision
        )

    async def revoke_client(self, client_session_id: UUID, reason: str) -> None:
        async with self._lock:
            session_id = self._active_by_client.get(client_session_id)
            if session_id is not None:
                self._revoke_locked(session_id, reason)

    def _require_session_locked(
        self,
        session_id: UUID,
        client_session_id: UUID,
        generation: int | None,
    ) -> _ScreenSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise ScreenPerceptionError("session_not_found")
        if session.revoked:
            raise ScreenPerceptionError("session_revoked")
        if session.client_session_id != client_session_id:
            raise ScreenPerceptionError("context_mismatch")
        if generation is not None and session.generation != generation:
            raise ScreenPerceptionError("generation_mismatch")
        if self._monotonic() >= session.lease_expires_monotonic:
            self._revoke_locked(session_id, "lease_expired")
            raise ScreenPerceptionError("session_expired")
        return session

    def _require_request_locked(
        self,
        request_id: UUID,
        session_id: UUID,
        client_session_id: UUID,
        generation: int,
        turn_id: UUID,
    ) -> tuple[_ScreenRequest, _ScreenSession]:
        terminal_reason = self._terminal_request_reasons.get(request_id)
        if terminal_reason is not None:
            raise ScreenPerceptionError(terminal_reason, stage="upload")
        pending = self._requests.get(request_id)
        if pending is None:
            raise ScreenPerceptionError("request_not_found", stage="upload")
        if pending.expires_monotonic <= self._monotonic():
            self._cancel_request_locked(request_id, "request_expired")
            raise ScreenPerceptionError("request_expired", stage="upload")
        if (
            pending.accepted_image_id is None
            and pending.requested_monotonic + CAPTURE_TIMEOUT_MS / 1000
            <= self._monotonic()
        ):
            self._cancel_request_locked(request_id, "request_expired")
            raise ScreenPerceptionError("request_expired", stage="upload")
        if (
            pending.session_id != session_id
            or pending.generation != generation
            or pending.turn_id != turn_id
        ):
            raise ScreenPerceptionError("context_mismatch", stage="upload")
        session = self._require_session_locked(
            session_id, client_session_id, generation
        )
        return pending, session

    def _require_provider_send_allowed_locked(
        self, pending: _ScreenRequest, session: _ScreenSession
    ) -> None:
        if session.pending_request_id != pending.request_id:
            raise ScreenPerceptionError("request_cancelled", stage="vision")
        policy = self._require_current_routing_locked(session)
        self._require_routing_and_consent(
            policy,
            session.routing_revision,
            session.cloud_vision_consent,
            session.cloud_derived_chat_consent,
        )
        if pending.cancellation.is_cancelled:
            raise ScreenPerceptionError("request_cancelled", stage="vision")

    def _require_current_routing_locked(
        self, session: _ScreenSession
    ) -> RoutingPolicy:
        policy = self._routing_policy()
        if policy.revision != session.routing_revision:
            self._revoke_locked(session.screen_session_id, "routing_changed")
            raise ScreenPerceptionError("routing_changed")
        return policy

    @staticmethod
    def _require_routing_and_consent(
        policy: RoutingPolicy,
        routing_revision: str,
        cloud_vision_consent: bool,
        cloud_derived_chat_consent: bool,
    ) -> None:
        if routing_revision != policy.revision:
            raise ScreenPerceptionError("routing_changed")
        if policy.vision_destination == "cloud" and not cloud_vision_consent:
            raise ScreenPerceptionError("cloud_consent_required")
        if policy.chat_destination == "cloud" and not cloud_derived_chat_consent:
            raise ScreenPerceptionError("cloud_consent_required")

    def _finish_request_locked(
        self, pending: _ScreenRequest, material: ScreenTurnMaterial
    ) -> None:
        self._requests.pop(pending.request_id, None)
        session = self._sessions.get(pending.session_id)
        if session is not None and session.pending_request_id == pending.request_id:
            session.pending_request_id = None
        self._remember_terminal_request_locked(pending.request_id, "duplicate_request")
        if not pending.future.done():
            pending.future.set_result(material)

    def _cancel_request_locked(self, request_id: UUID, reason: str) -> None:
        pending = self._requests.pop(request_id, None)
        if pending is None:
            return
        pending.cancellation.cancel()
        session = self._sessions.get(pending.session_id)
        if session is not None and session.pending_request_id == request_id:
            session.pending_request_id = None
        self._remember_terminal_request_locked(request_id, reason)
        if not pending.future.done():
            pending.future.set_result(
                ScreenTurnMaterial(
                    source=pending.source,
                    surface=None,
                    captured_at=None,
                    observation=None,
                    unavailable_reason=reason,
                    request_id=request_id,
                    validity=InferenceCancellationToken(),
                )
            )

    def _revoke_locked(self, session_id: UUID, reason: str) -> None:
        session = self._sessions.get(session_id)
        if session is None or session.revoked:
            return
        session.revoked = True
        session.validity.cancel()
        if self._active_by_client.get(session.client_session_id) == session_id:
            self._active_by_client.pop(session.client_session_id, None)
        if session.pending_request_id is not None:
            request_reason = {
                "lease_expired": "session_expired",
                "routing_changed": "routing_changed",
            }.get(reason, "request_cancelled")
            self._cancel_request_locked(session.pending_request_id, request_reason)

    def _purge_expired_locked(self) -> None:
        now = self._monotonic()
        for session_id, session in tuple(self._sessions.items()):
            if not session.revoked and now >= session.lease_expires_monotonic:
                self._revoke_locked(session_id, "lease_expired")
        for request_id, pending in tuple(self._requests.items()):
            if now >= pending.expires_monotonic:
                self._cancel_request_locked(request_id, "request_expired")

    def _remember_terminal_request_locked(self, request_id: UUID, reason: str) -> None:
        if request_id in self._terminal_request_reasons:
            return
        self._terminal_request_reasons[request_id] = reason
        self._terminal_request_order.append(request_id)
        while len(self._terminal_request_order) > MAX_TERMINAL_REQUESTS:
            expired = self._terminal_request_order.popleft()
            self._terminal_request_reasons.pop(expired, None)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _inference_reason(category: InferenceErrorCategory) -> str:
    return {
        InferenceErrorCategory.TIMEOUT: "vision_timeout",
        InferenceErrorCategory.UNSUPPORTED_CAPABILITY: "vision_unsupported",
        InferenceErrorCategory.INVALID_RESPONSE: "vision_invalid_response",
        InferenceErrorCategory.INVALID_REQUEST: "image_decode_failed",
        InferenceErrorCategory.CANCELLED: "request_cancelled",
    }.get(category, "vision_unavailable")
