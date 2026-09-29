"""output_stop通知は確認を待たず送り、confirm応答は要求相関付きの差分観測として受理する。"""
import asyncio
import json
from uuid import uuid4

import pytest

from app.livekit_transport import coordinator as module
from tests.unit.test_livekit_delivery_and_lifecycle import (
    RecordingCorePort,
    _coordinator,
)

RESPONSE = "30000000-0000-4000-8000-000000000010"


def setup():
    published = []
    observed = []
    scheduled = []

    def collect(operation, _response_id):
        task = asyncio.create_task(operation)
        scheduled.append(task)
        return task

    coordinator = _coordinator(
        module, published, [],
        output_stop_observation=lambda response_id, sequence: observed.append(
            (response_id, sequence)
        ),
        schedule=collect,
    )
    coordinator.participant_connected(
        identity=coordinator.user_identity, participant_sid="PA_current", room_sid="RM_one",
    )
    return coordinator, published, observed, scheduled


async def drain(scheduled):
    if scheduled:
        await asyncio.gather(*scheduled)
        scheduled.clear()


async def acknowledge(coordinator, request, *, identity=None, sid="PA_current", **changes):
    frame = {**request, "type": "output_stop_confirmed", "last_played_audio_sequence": 0,
        "output_confirmation": "never_connected", **changes}
    await coordinator.receive_data(identity=identity or coordinator.user_identity,
        participant_sid=sid, topic=module.PRIVATE_TOPIC, payload=json.dumps(frame).encode())


@pytest.mark.parametrize("wrong", ["session", "response", "request", "generation", "prefix"])
def test_observation_requires_current_session_response_request_generation_and_nonce(wrong):
    """相関の取れないconfirmは受理しない。通知自体は完了している。"""
    async def exercise():
        coordinator, published, observed, scheduled = setup()
        coordinator.notify_output_stop(RESPONSE)
        await drain(scheduled)
        request = json.loads(published[-1][0])
        changes = {"session": {"session_id": str(uuid4())}, "response": {"response_id": str(uuid4())},
            "request": {"request_id": str(uuid4())}, "generation": {"generation": 1},
            "prefix": {"last_played_audio_sequence": 1}}[wrong]
        await acknowledge(coordinator, request, **changes)
        assert observed == []
        await acknowledge(coordinator, request)
        assert observed == [(RESPONSE, 0)]
        await coordinator.cleanup("test_complete")
    asyncio.run(exercise())


def test_replacement_request_rejects_old_confirm():
    """同一応答の後発要求には最新request_idのconfirmだけを受理する。"""
    async def exercise():
        coordinator, published, observed, scheduled = setup()
        coordinator.notify_output_stop(RESPONSE)
        await drain(scheduled)
        old_request = json.loads(published[-1][0])
        coordinator.notify_output_stop(RESPONSE)
        await drain(scheduled)
        request = json.loads(published[-1][0])
        assert request["request_id"] != old_request["request_id"]
        await acknowledge(coordinator, old_request)
        assert observed == []
        await acknowledge(
            coordinator, request,
            output_confirmation="output_clock_passed", last_played_audio_sequence=2,
        )
        assert observed == [(RESPONSE, 2)]
        await coordinator.cleanup("test_complete")
    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["cleanup", "disconnect", "unavailable"])
def test_connection_loss_clears_pending_requests(action):
    """接続断・終了後は保留requestが破棄され、後着confirmは受理されない。"""
    async def exercise():
        coordinator, published, observed, scheduled = setup()
        coordinator.notify_output_stop(RESPONSE)
        await drain(scheduled)
        request = json.loads(published[-1][0])
        if action == "cleanup":
            await coordinator.cleanup("test_complete")
        elif action == "unavailable":
            await coordinator.mark_unavailable()
        else:
            coordinator.participant_disconnected(
                identity=coordinator.user_identity, participant_sid="PA_current",
            )
        await acknowledge(coordinator, request)
        assert observed == []
        await coordinator.cleanup("test_complete")
    asyncio.run(exercise())


def test_notify_is_fire_and_forget():
    """通知はconfirmを待たず即時完了する。"""
    async def exercise():
        coordinator, published, observed, scheduled = setup()
        coordinator.notify_output_stop(RESPONSE)
        await drain(scheduled)
        assert observed == []
        await coordinator.cleanup("test_complete")
    asyncio.run(exercise())


def test_output_stop_does_not_wait_for_stalled_publish():
    """private送信が滞留しても、BEの送出停止決定はCoreへ戻る。

    Issue #540: FEへの通知送信をCore確定の外へ切り離す。`publish_data`が
    完了しない条件でも`stop_response`はBE停止結果を返す。
    """
    async def exercise():
        import time
        from types import SimpleNamespace

        from app.livekit_transport.core_delivery import _ConversationCoreDelivery
        from app.livekit_transport.response_audio import ResponseAudioTracks

        stall_entered = asyncio.Event()
        release_stall = asyncio.Event()
        published: list[tuple[bytes, str]] = []
        observed: list[tuple[str, int]] = []
        notify_failures: list[tuple[str, str]] = []

        async def publish(payload: bytes, topic: str) -> None:
            if topic == module.PRIVATE_TOPIC:
                stall_entered.set()
                await release_stall.wait()
                return
            published.append((payload, topic))

        async def cleanup(_session_id: str) -> None:
            return None

        async def generation_ready() -> None:
            return None

        from tests.unit.test_livekit_delivery_and_lifecycle import RecordingCorePort
        coordinator = module.ProductionSessionCoordinator(
            session_id="20000000-0000-4000-8000-000000000010",
            user_identity="user-20000000-0000-4000-8000-000000000010",
            core_participant_id="40000000-0000-4000-8000-000000000010",
            reconnect_grace_ms=60_000,
            dependencies=module.SessionCoordinatorDependencies(
                publish_data=publish,
                cleanup=cleanup,
                generation_ready=generation_ready,
                output_stop_observation=lambda response_id, sequence: observed.append(
                    (response_id, sequence)
                ),
                notify_failure=lambda response_id, name: notify_failures.append(
                    (response_id, name)
                ),
            ),
            core_port=RecordingCorePort(),
        )
        coordinator.participant_connected(
            identity=coordinator.user_identity,
            participant_sid="PA_current",
            room_sid="RM_one",
        )

        class _Source:
            def clear_queue(self) -> None:
                return None

            async def aclose(self) -> None:
                return None

        class _Track:
            def mute(self) -> None:
                return None

        audio = ResponseAudioTracks(room=SimpleNamespace(local_participant=SimpleNamespace()))

        class _Pacer:
            input_sample_count = 960
            def sent_sample_count(self, _at_ns: int) -> int:
                return 960
            async def aclose(self) -> None:
                return None
            def stop(self) -> None:
                return None

        audio._current = type("T", (), {
            "response_id": RESPONSE, "pacer": _Pacer(),
            "source": _Source(), "track": _Track(),
            "client_ready": asyncio.Event(), "source_closed": False,
            "segment_sample_ends": [960],
        })()
        audio._requested_response_id = RESPONSE

        delivery = _ConversationCoreDelivery(
            coordinator=coordinator,
            audio_source=audio,
            character_participant_id="40000000-0000-4000-8000-000000000010",
            character_id="miori",
        )

        from app.conversation_core.models import AudioSegment, Response, ResponseState
        response = Response(
            response_id=RESPONSE,
            generation=1,
            source_utterance_ids=(),
            state=ResponseState.IN_PROGRESS,
            audio_segments=(AudioSegment(1, b"audio", (0, 1)),),
        )

        stopped = await delivery.stop_response(response, decided_at_ns=time.monotonic_ns())
        assert stopped.last_played_audio_sequence == 1
        # 通知送信は滞留しているが、BE停止は確定している。
        await asyncio.wait_for(stall_entered.wait(), 0.5)
        release_stall.set()
        await asyncio.sleep(0)
        await coordinator.cleanup("test_complete")
        assert observed == []
        assert notify_failures == []
    asyncio.run(exercise())


def test_notify_failure_is_contained_inside_session_owned_task():
    """本番のtask所有者を通した送信失敗はSession cleanupを先行させない。

    Issue #540: `ProductionSessionOwner.schedule_task`が所有するtask内で
    `publish_data`が例外を出しても、`_run_owned_task`のruntime_error cleanup
    へ伝播せず、応答単位の失敗観測だけを記録する。
    """
    async def exercise():
        from app.livekit_transport.session_runtime import ProductionSessionOwner

        cleanup_reasons: list[str] = []
        notify_failures: list[tuple[str, str]] = []

        async def publish(_payload: bytes, topic: str) -> None:
            if topic == module.PRIVATE_TOPIC:
                raise RuntimeError("publish failed")

        owner = ProductionSessionOwner("20000000-0000-4000-8000-000000000010")

        async def cleanup(_session_id: str) -> None:
            cleanup_reasons.append("cleanup")

        async def generation_ready() -> None:
            return None

        coordinator = module.ProductionSessionCoordinator(
            session_id="20000000-0000-4000-8000-000000000010",
            user_identity="user-20000000-0000-4000-8000-000000000010",
            core_participant_id="40000000-0000-4000-8000-000000000010",
            reconnect_grace_ms=60_000,
            dependencies=module.SessionCoordinatorDependencies(
                publish_data=publish,
                cleanup=cleanup,
                generation_ready=generation_ready,
                schedule=lambda operation, _response_id: owner.schedule_task(
                    operation
                ),
                notify_failure=lambda response_id, name: notify_failures.append(
                    (response_id, name)
                ),
            ),
            core_port=RecordingCorePort(),
        )
        owner.coordinator = coordinator
        coordinator.participant_connected(
            identity=coordinator.user_identity,
            participant_sid="PA_current",
            room_sid="RM_one",
        )

        coordinator.notify_output_stop(RESPONSE)
        assert owner.tasks is not None and len(owner.tasks) == 1
        await asyncio.gather(*owner.tasks, return_exceptions=True)
        # 送信例外はSession cleanupを起こさず、失敗観測だけを残す。
        assert cleanup_reasons == []
        assert notify_failures == [(RESPONSE, "output_stop_notify_failed")]
        assert coordinator.phase == "available"
        await coordinator.cleanup("test_complete")
    asyncio.run(exercise())


def test_finished_notification_failure_is_contained_inside_session_owned_task():
    """response_audio_finishedの送信失敗も同じ境界で隔離される。"""
    async def exercise():
        from app.livekit_transport.session_runtime import ProductionSessionOwner

        cleanup_reasons: list[str] = []
        notify_failures: list[tuple[str, str]] = []

        async def publish(_payload: bytes, topic: str) -> None:
            if topic == module.PRIVATE_TOPIC:
                raise RuntimeError("publish failed")

        owner = ProductionSessionOwner("20000000-0000-4000-8000-000000000010")

        async def cleanup(_session_id: str) -> None:
            cleanup_reasons.append("cleanup")

        async def generation_ready() -> None:
            return None

        coordinator = module.ProductionSessionCoordinator(
            session_id="20000000-0000-4000-8000-000000000010",
            user_identity="user-20000000-0000-4000-8000-000000000010",
            core_participant_id="40000000-0000-4000-8000-000000000010",
            reconnect_grace_ms=60_000,
            dependencies=module.SessionCoordinatorDependencies(
                publish_data=publish,
                cleanup=cleanup,
                generation_ready=generation_ready,
                schedule=lambda operation, _response_id: owner.schedule_task(
                    operation
                ),
                notify_failure=lambda response_id, name: notify_failures.append(
                    (response_id, name)
                ),
            ),
            core_port=RecordingCorePort(),
        )
        owner.coordinator = coordinator
        coordinator.participant_connected(
            identity=coordinator.user_identity,
            participant_sid="PA_current",
            room_sid="RM_one",
        )

        coordinator.send_response_audio_finished(
            response_id=RESPONSE,
            input_sample_count=960,
            captured_sample_count=1920,
            padding_sample_count=960,
        )
        assert owner.tasks is not None and len(owner.tasks) == 1
        await asyncio.gather(*owner.tasks, return_exceptions=True)
        assert cleanup_reasons == []
        assert notify_failures == [
            (RESPONSE, "response_audio_finished_notify_failed")
        ]
        assert coordinator.phase == "available"
        await coordinator.cleanup("test_complete")
    asyncio.run(exercise())
