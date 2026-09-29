"""ResponseAudioTracks + _ConversationCoreDelivery + CoordinatorのBE推定再生済み連鎖を検証する。

C1: 中断応答のprefixを、BE送出位置から固定dを引き、audio_sequence境界へ丸めた値で確定する。
FEの output_stop_confirmed / playback_stopped は確定値を上書きせず差分観測のみ。
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, Mock


class FakeResponse:
    def __init__(self, response_id: str) -> None:
        self.response_id = response_id


class RecordingLifecycle:
    phase = "available"
    generation = 0

    def __init__(self) -> None:
        self.confirmed: list[tuple[str, int]] = []

    def confirm_playback(self, *, response_id: str, confirmed_audio_sequence: int) -> None:
        self.confirmed.append((response_id, confirmed_audio_sequence))


def _delivery(coordinator, audio_source):
    from app.livekit_transport.core_delivery import _ConversationCoreDelivery
    return _ConversationCoreDelivery(
        coordinator=coordinator, audio_source=audio_source,
        character_participant_id="participant", character_id="miori",
    )


def _tracks_returning(played: int):
    """isinstance(ResponseAudioTracks)を通し、stop_responseがplayedを返すfake。"""
    from app.livekit_transport.response_audio import ResponseAudioTracks

    tracks = Mock(spec=ResponseAudioTracks)
    tracks.stop_response = AsyncMock(return_value=played)
    return tracks


def test_be_position_estimate_falls_back_when_no_audio_served():
    """送出開始前の停止は推定0で確定する。"""
    async def exercise() -> None:
        from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator
        from app.livekit_transport import coordinator as module

        tracks = _tracks_returning(0)
        published = []
        coordinator = _coordinator(module, published, [])
        lifecycle = RecordingLifecycle()
        coordinator._lifecycle = lifecycle
        delivery = _delivery(coordinator, tracks)
        result = await delivery.stop_response(
            FakeResponse("response-1"), decided_at_ns=time.monotonic_ns(),
        )
        assert result.last_played_audio_sequence == 0
    asyncio.run(exercise())


def test_estimated_prefix_drives_lifecycle_and_stays_after_fe_report():
    """BE推定prefixがlifecycleへ確定し、FE報告で上書きされない。"""
    async def exercise() -> None:
        from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator
        from app.livekit_transport import coordinator as module

        tracks = _tracks_returning(2)
        published = []
        coordinator = _coordinator(module, published, [])
        lifecycle = RecordingLifecycle()
        coordinator._lifecycle = lifecycle
        delivery = _delivery(coordinator, tracks)
        result = await delivery.stop_response(
            FakeResponse("r1"), decided_at_ns=time.monotonic_ns(),
        )
        assert result.last_played_audio_sequence == 2
        assert lifecycle.confirmed == [("r1", 2)]

        # 後から届くFE報告（0や1など）は確定値を上書きしない。
        await coordinator.receive_data(
            identity=coordinator.user_identity,
            participant_sid="PA_current",
            topic=module.APPLICATION_TOPIC,
            payload=json.dumps({
                "type": "playback_stopped", "response_id": "r1",
                "last_played_audio_sequence": 0, "reason": "barge_in",
            }).encode(),
        )
        assert lifecycle.confirmed == [("r1", 2)]
    asyncio.run(exercise())


def test_output_stop_confirmed_is_observation_only():
    """output_stop_confirmed は validate して観測へ回すだけ。推定を上書きしない。"""
    async def exercise() -> None:
        from app.livekit_transport import coordinator as module
        from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator

        response_id = "30000000-0000-4000-8000-000000000010"
        observed: list[tuple[str, int]] = []
        coordinator = _coordinator(
            module, [], [],
            output_stop_observation=lambda r, s: observed.append((r, s)),
        )
        coordinator.participant_connected(
            identity=coordinator.user_identity, participant_sid="PA_current", room_sid="RM_one",
        )

        def confirm_frame(request_id: str, sequence: int, confirmation: str) -> bytes:
            return json.dumps({
                "protocol_version": "2.0", "type": "output_stop_confirmed",
                "session_id": coordinator.session_id, "response_id": response_id,
                "request_id": request_id, "generation": coordinator.generation,
                "last_played_audio_sequence": sequence,
                "output_confirmation": confirmation,
            }).encode()

        # requestなしのconfirmは受理しない（unknown request）。
        await coordinator.receive_data(
            identity=coordinator.user_identity, participant_sid="PA_current",
            topic=module.PRIVATE_TOPIC,
            payload=confirm_frame("00000000-0000-4000-8000-000000000000", 0, "never_connected"),
        )
        assert observed == []

        coordinator.notify_output_stop(response_id=response_id)
        pending = coordinator._output_stop_requests
        assert response_id in pending
        # output_clock_passed は任意prefixを観測として受理する。
        await coordinator.receive_data(
            identity=coordinator.user_identity, participant_sid="PA_current",
            topic=module.PRIVATE_TOPIC,
            payload=confirm_frame(pending[response_id][0], 3, "output_clock_passed"),
        )
        assert observed == [(response_id, 3)]
    asyncio.run(exercise())


def test_early_fe_report_is_paired_with_later_be_estimate():
    """SCN-C3-P1: 推定確定前に届いたFE停止観測へ、確定後に差分を記録する。"""
    async def exercise() -> None:
        from app.livekit_transport import coordinator as module
        from app.livekit_transport.measurement import LiveKitMeasurementSession
        from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator

        tracks = _tracks_returning(2)
        coordinator = _coordinator(module, [], [])
        lifecycle = RecordingLifecycle()
        coordinator._lifecycle = lifecycle
        delivery = _delivery(coordinator, tracks)
        trace = []
        measurement = LiveKitMeasurementSession(
            session_id=coordinator.session_id, character_id="miori",
            measurement_kind="controlled_baseline", record=trace.append,
            clock_ns=time.monotonic_ns,
        )
        delivery.attach_measurement(measurement)
        measurement.bind_response(response_id="50000000-0000-4000-8000-000000000010", source_utterance_ids=("30000000-0000-4000-8000-000000000010",))

        # 停止観測が推定確定より先に届く（送信順は同一Session内で逆転し得る）。
        delivery.observe_fe_playback("50000000-0000-4000-8000-000000000010", 1, kind="stopped", final=True)
        assert "playback_estimate_delta_stopped" not in {
            event.name for event in trace
        }

        result = await delivery.stop_response(
            FakeResponse("50000000-0000-4000-8000-000000000010"), decided_at_ns=time.monotonic_ns(),
        )
        assert result.last_played_audio_sequence == 2
        deltas = {
            event.name: event.value
            for event in trace
            if event.name.startswith("playback_estimate_delta_")
        }
        assert deltas == {"playback_estimate_delta_stopped": -1.0}
        assert lifecycle.confirmed == [("50000000-0000-4000-8000-000000000010", 2)]
    asyncio.run(exercise())


def test_completed_response_estimate_reaches_reconnect_terminal_outcome():
    """P-001: 通常完了のBE推定値が切断・再同期の公開結果へ保持される。

    FE報告を省略した通常完了後にparticipantが切断・再接続したとき、
    `authoritative_state` の `response_interrupted` が当該応答のBE推定区間数を示す。
    """
    async def exercise() -> None:
        from app.conversation_core.models import AudioSegment, Response, ResponseState
        from app.livekit_transport import coordinator as module
        from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator

        response_id = "30000000-0000-4000-8000-000000000010"
        tracks = _tracks_returning(0)
        tracks.finish_response = AsyncMock(return_value=None)
        tracks.statistics.return_value = {
            "response_audio_input_samples": 1920,
            "response_audio_captured_samples": 2880,
            "response_audio_padding_samples": 960,
        }
        published: list[tuple[bytes, str]] = []
        scheduled: list[asyncio.Task[None]] = []

        def collect(operation, _response_id):
            task = asyncio.create_task(operation)
            scheduled.append(task)
            return task

        coordinator = _coordinator(module, published, [], schedule=collect)
        identity = coordinator.user_identity
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_current", room_sid="RM_one",
        )
        delivery = _delivery(coordinator, tracks)
        coordinator.begin_response(response_id=response_id)
        response = Response(
            response_id=response_id, generation=1, source_utterance_ids=(),
            state=ResponseState.IN_PROGRESS,
            audio_segments=(
                AudioSegment(1, b"pcm-1", (0, 1)),
                AudioSegment(2, b"pcm-2", (1, 2)),
            ),
        )

        # 通常完了: FE報告なしでBE推定2区間が確定する。
        await delivery.finish_response(response)
        if scheduled:
            await asyncio.gather(*scheduled)
            scheduled.clear()

        # 次応答開始前に切断し、再接続の公開結果を検査する。
        coordinator.participant_disconnected(identity=identity, participant_sid="PA_current")
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_rejoined", room_sid="RM_one",
        )
        await coordinator.synchronize_reconnection()

        authoritative = json.loads(published[-1][0])
        assert authoritative["type"] == "authoritative_state"
        assert authoritative["terminal_outcomes"] == [
            {
                "type": "response_interrupted",
                "session_id": coordinator.session_id,
                "response_id": response_id,
                "confirmed_audio_sequence": 2,
            }
        ]
        await coordinator.cleanup("test_complete")

    asyncio.run(exercise())


def test_completed_response_estimate_reaches_state_sync_outcome():
    """P-001: 同一世代の state_sync_request でも通常完了の推定値が公開される。"""
    async def exercise() -> None:
        from app.conversation_core.models import AudioSegment, Response, ResponseState
        from app.livekit_transport import coordinator as module
        from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator

        response_id = "30000000-0000-4000-8000-000000000010"
        tracks = _tracks_returning(0)
        tracks.finish_response = AsyncMock(return_value=None)
        tracks.statistics.return_value = {
            "response_audio_input_samples": 1920,
            "response_audio_captured_samples": 2880,
            "response_audio_padding_samples": 960,
        }
        published: list[tuple[bytes, str]] = []
        scheduled: list[asyncio.Task[None]] = []

        def collect(operation, _response_id):
            task = asyncio.create_task(operation)
            scheduled.append(task)
            return task

        coordinator = _coordinator(module, published, [], schedule=collect)
        identity = coordinator.user_identity
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_current", room_sid="RM_one",
        )
        delivery = _delivery(coordinator, tracks)
        coordinator.begin_response(response_id=response_id)
        await delivery.finish_response(Response(
            response_id=response_id, generation=1, source_utterance_ids=(),
            state=ResponseState.IN_PROGRESS,
            audio_segments=(
                AudioSegment(1, b"pcm-1", (0, 1)),
                AudioSegment(2, b"pcm-2", (1, 2)),
            ),
        ))
        if scheduled:
            await asyncio.gather(*scheduled)
            scheduled.clear()

        sync_request = json.dumps({
            "protocol_version": "2.0",
            "type": "state_sync_request",
            "generation": coordinator.generation,
        }, separators=(",", ":")).encode()
        await coordinator.receive_data(
            identity=identity, participant_sid="PA_current",
            topic=module.PRIVATE_TOPIC, payload=sync_request,
        )

        authoritative = json.loads(published[-1][0])
        assert authoritative["type"] == "authoritative_state"
        assert authoritative["terminal_outcomes"] == [
            {
                "type": "response_interrupted",
                "session_id": coordinator.session_id,
                "response_id": response_id,
                "confirmed_audio_sequence": 2,
            }
        ]
        await coordinator.cleanup("test_complete")

    asyncio.run(exercise())


def test_interim_completed_report_is_not_used_for_terminal_delta():
    """SCN-C3-N1: 途中経過の playback_completed は終端差分に使わない。"""
    async def exercise() -> None:
        from app.livekit_transport import coordinator as module
        from app.livekit_transport.measurement import LiveKitMeasurementSession
        from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator

        tracks = _tracks_returning(2)
        coordinator = _coordinator(module, [], [])
        delivery = _delivery(coordinator, tracks)
        trace = []
        measurement = LiveKitMeasurementSession(
            session_id=coordinator.session_id, character_id="miori",
            measurement_kind="controlled_baseline", record=trace.append,
            clock_ns=time.monotonic_ns,
        )
        delivery.attach_measurement(measurement)
        measurement.bind_response(response_id="50000000-0000-4000-8000-000000000010", source_utterance_ids=("30000000-0000-4000-8000-000000000010",))

        # 途中経過（final=False）は終端差分の対象にしない。
        delivery.observe_fe_playback("50000000-0000-4000-8000-000000000010", 1, kind="completed", final=False)
        # 最終報告（final=True）だけが終端差分へ対応付けられる。
        delivery.observe_fe_playback("50000000-0000-4000-8000-000000000010", 2, kind="completed", final=True)
        result = await delivery.stop_response(
            FakeResponse("50000000-0000-4000-8000-000000000010"), decided_at_ns=time.monotonic_ns(),
        )
        assert result.last_played_audio_sequence == 2
        deltas = {
            event.name: event.value
            for event in trace
            if event.name.startswith("playback_estimate_delta_")
        }
        assert deltas == {"playback_estimate_delta_completed": 0.0}
    asyncio.run(exercise())
