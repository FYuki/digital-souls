from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from io import BytesIO
import json
from uuid import UUID

import pytest
from PIL import Image

from app.screen_perception.service import (
    RoutingPolicy,
    ScreenPerceptionError,
    ScreenPerceptionService,
    ScreenSurface,
)
from app.screen_perception.vision import VisionObservation, VisionTargetCandidate


CLIENT_ID = UUID("10000000-0000-4000-8000-000000000001")
CONVERSATION_ID = UUID("20000000-0000-4000-8000-000000000001")


class Clock:
    monotonic = 100.0
    utc = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_clock() -> None:
    Clock.monotonic = 100.0
    Clock.utc = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class FakeVision:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def observe(self, **request: object) -> VisionObservation:
        self.calls.append(request)
        return VisionObservation(
            "identified",
            (
                VisionTargetCandidate(
                    "合成画面", "center", "警告", "中央に表示", ()
                ),
            ),
            (),
            "不確実性なし",
        )


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (1, 1), "white").save(output, format="PNG")
    return output.getvalue()


def _service(
    vision: FakeVision,
    policy: RoutingPolicy = RoutingPolicy("a" * 64, "local", "local"),
) -> ScreenPerceptionService:
    return ScreenPerceptionService(
        vision=vision,  # type: ignore[arg-type]
        routing_policy=lambda: policy,
        validate_context=lambda _character, _conversation: None,
        monotonic=lambda: Clock.monotonic,
        utc_now=lambda: Clock.utc,
    )


async def _start(
    service: ScreenPerceptionService,
    surface: ScreenSurface = "monitor",
) -> dict[str, object]:
    return await service.start_session(
        client_session_id=CLIENT_ID,
        generation=1,
        character_id="miori",
        conversation_id=CONVERSATION_ID,
        requested_surface=surface,
        actual_surface=surface,
        routing_revision="a" * 64,
        cloud_vision_consent=False,
        cloud_derived_chat_consent=False,
    )


def test_browser_surface_is_preserved_through_session_and_image() -> None:
    async def exercise() -> None:
        vision = FakeVision()
        service = _service(vision)
        session = await _start(service, "browser")
        request, _future = await service.begin_request(
            client_session_id=CLIENT_ID,
            character_id="miori",
            conversation_id=CONVERSATION_ID,
            question="このタブを見て",
            source="explicit_ui",
        )

        _accepted, completed = await service.accept_image(
            request_id=UUID(str(request["request_id"])),
            screen_session_id=UUID(str(session["screen_session_id"])),
            client_session_id=CLIENT_ID,
            generation=1,
            turn_id=UUID(str(request["turn_id"])),
            image_id=UUID("30000000-0000-4000-8000-000000000002"),
            actual_surface="browser",
            captured_at=Clock.utc,
            mime_type="image/png",
            width=1,
            height=1,
            image_data=_png(),
        )

        assert session["actual_surface"] == "browser"
        assert completed.material.lineages[0].surface == "browser"
        assert len(vision.calls) == 1

    asyncio.run(exercise())


def test_request_binds_image_to_owner_generation_turn_and_invalidates_on_revoke() -> None:
    async def exercise() -> None:
        vision = FakeVision()
        service = _service(vision)
        session = await _start(service)
        request, future = await service.begin_request(
            client_session_id=CLIENT_ID,
            character_id="miori",
            conversation_id=CONVERSATION_ID,
            question="今の画面を見て",
            source="explicit_ui",
            target_hint="中央の警告",
        )

        accepted, completed = await service.accept_image(
            request_id=UUID(str(request["request_id"])),
            screen_session_id=UUID(str(session["screen_session_id"])),
            client_session_id=CLIENT_ID,
            generation=1,
            turn_id=UUID(str(request["turn_id"])),
            image_id=UUID("30000000-0000-4000-8000-000000000001"),
            actual_surface="monitor",
            captured_at=Clock.utc,
            mime_type="image/png",
            width=1,
            height=1,
            image_data=_png(),
        )

        assert accepted["type"] == "screen_snapshot_upload_accepted"
        assert completed.material.observation == VisionObservation(
            "identified",
            (
                VisionTargetCandidate(
                    "合成画面", "center", "警告", "中央に表示", ()
                ),
            ),
            (),
            "不確実性なし",
        )
        assert await future is completed.material
        assert len(vision.calls) == 1
        assert vision.calls[0]["target_hint"] == "中央の警告"
        assert len(completed.material.lineages) == 1
        assert completed.material.lineages[0].origin_generation == 1
        await service.revoke(
            screen_session_id=UUID(str(session["screen_session_id"])),
            client_session_id=CLIENT_ID,
            generation=1,
            reason="user_off",
        )
        assert completed.material.is_current is False

    asyncio.run(exercise())


def test_revoke_during_pending_request_unblocks_voice_without_old_observation() -> None:
    async def exercise() -> None:
        service = _service(FakeVision())
        session = await _start(service)
        _request, future = await service.begin_request(
            client_session_id=CLIENT_ID,
            character_id="miori",
            conversation_id=CONVERSATION_ID,
            question="画面を読んで",
            source="natural_language_voice",
        )

        await service.revoke(
            screen_session_id=UUID(str(session["screen_session_id"])),
            client_session_id=CLIENT_ID,
            generation=1,
            reason="user_off",
        )

        material = await future
        assert material.observation is None
        assert material.unavailable_reason == "request_cancelled"

    asyncio.run(exercise())


def test_expired_lease_and_different_owner_are_rejected_before_vision() -> None:
    async def exercise() -> None:
        vision = FakeVision()
        service = _service(vision)
        await _start(service)
        Clock.monotonic += 16

        with pytest.raises(ScreenPerceptionError) as error:
            await service.begin_request(
                client_session_id=CLIENT_ID,
                character_id="miori",
                conversation_id=CONVERSATION_ID,
                question="画面を見て",
                source="explicit_ui",
            )

        assert error.value.reason_code == "session_not_found"
        assert vision.calls == []
        Clock.monotonic = 100.0

    asyncio.run(exercise())


def test_cloud_destinations_require_each_bound_consent() -> None:
    async def exercise() -> None:
        cloud_vision = _service(
            FakeVision(), RoutingPolicy("a" * 64, "cloud", "cloud")
        )
        with pytest.raises(ScreenPerceptionError) as error:
            await _start(cloud_vision)
        assert error.value.reason_code == "cloud_consent_required"

        local_vision_cloud_chat = _service(
            FakeVision(), RoutingPolicy("a" * 64, "local", "cloud")
        )
        with pytest.raises(ScreenPerceptionError) as error:
            await _start(local_vision_cloud_chat)
        assert error.value.reason_code == "cloud_consent_required"

    asyncio.run(exercise())


def test_capture_deadline_owner_generation_and_duplicate_are_rejected() -> None:
    async def exercise() -> None:
        service = _service(FakeVision())
        session = await _start(service)
        request, _future = await service.begin_request(
            client_session_id=CLIENT_ID,
            character_id="miori",
            conversation_id=CONVERSATION_ID,
            question="画面を見て",
            source="explicit_ui",
        )
        request_id = UUID(str(request["request_id"]))
        turn_id = UUID(str(request["turn_id"]))
        session_id = UUID(str(session["screen_session_id"]))

        with pytest.raises(ScreenPerceptionError) as owner_error:
            await service.fail_request(
                request_id=request_id,
                screen_session_id=session_id,
                client_session_id=UUID("10000000-0000-4000-8000-000000000002"),
                generation=1,
                turn_id=turn_id,
                reason_code="frame_unavailable",
            )
        assert owner_error.value.reason_code == "context_mismatch"

        with pytest.raises(ScreenPerceptionError) as generation_error:
            await service.fail_request(
                request_id=request_id,
                screen_session_id=session_id,
                client_session_id=CLIENT_ID,
                generation=2,
                turn_id=turn_id,
                reason_code="frame_unavailable",
            )
        assert generation_error.value.reason_code == "context_mismatch"

        Clock.monotonic += 6
        with pytest.raises(ScreenPerceptionError) as expired_error:
            await service.fail_request(
                request_id=request_id,
                screen_session_id=session_id,
                client_session_id=CLIENT_ID,
                generation=1,
                turn_id=turn_id,
                reason_code="frame_unavailable",
            )
        assert expired_error.value.reason_code == "request_expired"

        next_request, _future = await service.begin_request(
            client_session_id=CLIENT_ID,
            character_id="miori",
            conversation_id=CONVERSATION_ID,
            question="画面を見て",
            source="explicit_ui",
        )
        next_id = UUID(str(next_request["request_id"]))
        next_turn_id = UUID(str(next_request["turn_id"]))
        await service.fail_request(
            request_id=next_id,
            screen_session_id=session_id,
            client_session_id=CLIENT_ID,
            generation=1,
            turn_id=next_turn_id,
            reason_code="frame_unavailable",
        )
        with pytest.raises(ScreenPerceptionError) as duplicate_error:
            await service.fail_request(
                request_id=next_id,
                screen_session_id=session_id,
                client_session_id=CLIENT_ID,
                generation=1,
                turn_id=next_turn_id,
                reason_code="frame_unavailable",
            )
        assert duplicate_error.value.reason_code == "duplicate_request"

    asyncio.run(exercise())


def test_voice_cancellation_invalidates_pending_request_before_late_upload() -> None:
    async def exercise() -> None:
        service = _service(FakeVision())
        session = await _start(service)
        published: list[bytes] = []
        sent = asyncio.Event()

        async def publish(payload: bytes) -> None:
            published.append(payload)
            sent.set()

        task = asyncio.create_task(
            service.await_voice_material(
                client_session_id=CLIENT_ID,
                character_id="miori",
                conversation_id=CONVERSATION_ID,
                question="今の画面を見て",
                publish_request=publish,
            )
        )
        await sent.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        event = json.loads(published[0])
        with pytest.raises(ScreenPerceptionError) as late_error:
            await service.fail_request(
                request_id=UUID(event["request_id"]),
                screen_session_id=UUID(str(session["screen_session_id"])),
                client_session_id=CLIENT_ID,
                generation=1,
                turn_id=UUID(event["turn_id"]),
                reason_code="frame_unavailable",
            )
        assert late_error.value.reason_code == "request_cancelled"

    asyncio.run(exercise())


def test_voice_publish_failure_returns_unavailable_without_leaking_request() -> None:
    async def exercise() -> None:
        service = _service(FakeVision())
        await _start(service)

        async def publish(_payload: bytes) -> None:
            raise RuntimeError("transport is closed")

        material = await service.await_voice_material(
            client_session_id=CLIENT_ID,
            character_id="miori",
            conversation_id=CONVERSATION_ID,
            question="今の画面を見て",
            publish_request=publish,
        )

        assert material is not None
        assert material.observation is None
        assert material.unavailable_reason == "backend_unavailable"

    asyncio.run(exercise())


def test_screen_candidate_without_share_becomes_character_reply_material() -> None:
    async def exercise() -> None:
        service = _service(FakeVision())
        decision = await service.decide_reference(
            client_session_id=None,
            question="これ何？",
            explicit_ui=False,
            history=(),
        )
        published: list[bytes] = []
        material = await service.await_voice_material(
            client_session_id=None,
            character_id="miori",
            conversation_id=CONVERSATION_ID,
            question="これ何？",
            publish_request=lambda payload: _record_payload(published, payload),
        )

        assert decision.decision == "answer_without_screen"
        assert decision.unavailable_reason == "session_not_found"
        assert material is not None
        assert material.unavailable_reason == "session_not_found"
        assert published == []

    async def _record_payload(target: list[bytes], payload: bytes) -> None:
        target.append(payload)

    asyncio.run(exercise())
