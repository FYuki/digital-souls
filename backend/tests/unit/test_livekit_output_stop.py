"""停止確認は認証済みの接続・要求と照合し、Core制御待ちとは独立に返す。"""
import asyncio
import json
from uuid import uuid4

import pytest

from app.livekit_transport import coordinator as module
from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator

RESPONSE = "30000000-0000-4000-8000-000000000010"


def setup():
    published = []
    coordinator = _coordinator(module, published, [])
    coordinator.participant_connected(identity=coordinator.user_identity, participant_sid="PA_current", room_sid="RM_one")
    return coordinator, published


async def acknowledge(coordinator, request, *, identity=None, sid="PA_current", **changes):
    frame = {**request, "type": "output_stop_confirmed", "last_played_audio_sequence": 0,
        "output_confirmation": "never_connected", **changes}
    await coordinator.receive_data(identity=identity or coordinator.user_identity,
        participant_sid=sid, topic=module.PRIVATE_TOPIC, payload=json.dumps(frame).encode())


@pytest.mark.parametrize("wrong", ["identity", "participant", "session", "response", "request", "generation", "prefix"])
def test_ack_requires_current_participant_session_response_generation_and_nonce(wrong):
    async def exercise():
        coordinator, published = setup()
        pending = asyncio.create_task(coordinator.request_output_stop(RESPONSE))
        await asyncio.sleep(0)
        request = json.loads(published[-1][0])
        if wrong == "identity":
            await acknowledge(coordinator, request, identity="unrelated")
        elif wrong == "participant":
            await acknowledge(coordinator, request, sid="PA_old")
        else:
            changes = {"session": {"session_id": str(uuid4())}, "response": {"response_id": str(uuid4())},
                "request": {"request_id": str(uuid4())}, "generation": {"generation": 1},
                "prefix": {"last_played_audio_sequence": 1}}[wrong]
            await acknowledge(coordinator, request, **changes)
        await asyncio.sleep(0)
        assert not pending.done()
        await acknowledge(coordinator, request)
        result = await asyncio.wait_for(pending, 0.5)
        assert result.last_played_audio_sequence == 0
        assert result.request_id == request["request_id"] and result.generation == request["generation"]
        await coordinator.cleanup("test_complete")
    asyncio.run(exercise())


def test_cancelled_request_cannot_confirm_its_replacement():
    async def exercise():
        coordinator, published = setup()
        first = asyncio.create_task(coordinator.request_output_stop(RESPONSE))
        await asyncio.sleep(0)
        old_request = json.loads(published[-1][0])
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(coordinator.request_output_stop(RESPONSE))
        await asyncio.sleep(0)
        request = json.loads(published[-1][0])
        assert request["request_id"] != old_request["request_id"]
        await acknowledge(coordinator, old_request)
        assert not second.done()
        await acknowledge(coordinator, request, output_confirmation="output_clock_passed", last_played_audio_sequence=2)
        assert (await second).last_played_audio_sequence == 2
        await coordinator.cleanup("test_complete")
    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["cleanup", "disconnect", "unavailable"])
def test_connection_loss_aborts_waiter_without_stop_success(action):
    async def exercise():
        coordinator, _ = setup()
        pending = asyncio.create_task(coordinator.request_output_stop(RESPONSE))
        await asyncio.sleep(0)
        if action == "cleanup":
            await coordinator.cleanup("test_complete")
        elif action == "unavailable":
            await coordinator.mark_unavailable()
        else:
            coordinator.participant_disconnected(identity=coordinator.user_identity, participant_sid="PA_current")
        with pytest.raises(RuntimeError, match="connection lost"):
            await asyncio.wait_for(pending, 0.5)
        await coordinator.cleanup("test_complete")
    asyncio.run(exercise())
