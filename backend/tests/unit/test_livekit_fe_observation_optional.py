"""FE の再生報告が任意の差分記録として扱われることを検証する。

対象: Issue #540。`playback_completed`/`playback_stopped`/`last_played_audio_sequence`
は BE の推定値を上書きせず、差分記録のみに使われる。
schema では optional として残る。
"""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable
from uuid import uuid4

import pytest

from app.livekit_transport import delivery, microphone_bridge


def _bridge_with_recording_core() -> tuple[
    microphone_bridge._ConversationCoreBridge,
    list[tuple[str, int | None, str]],
    list[Awaitable[None]],
]:
    observed: list[tuple[str, int | None, str]] = []
    scheduled: list[Awaitable[None]] = []

    class RecordingCoreSession:
        async def confirm_playback(self, **_request: object) -> bool:
            raise AssertionError("FE報告はCoreの再生済み状態を更新しない")

    bridge = microphone_bridge._ConversationCoreBridge(
        RecordingCoreSession(),
        scheduled.append,
        playback_observation=lambda r, s, k, f: observed.append((r, s, k, f)),
    )
    return bridge, observed, scheduled


async def _drain(scheduled: list[Awaitable[None]]) -> None:
    if scheduled:
        await asyncio.gather(*scheduled)


def _playback_event(
    event_type: str,
    *,
    response_id: str = "50000000-0000-4000-8000-000000000010",
    **fields: object,
) -> dict[str, object]:
    event: dict[str, object] = {
        "type": event_type,
        "protocol_version": "2.0",
        "event_id": str(uuid4()),
        "session_id": "20000000-0000-4000-8000-000000000010",
        "response_id": response_id,
        "monotonic_timestamp_ms": 1_000,
        **fields,
    }
    if event_type == "playback_stopped":
        event.setdefault("reason", "barge_in")
    return event


class TestFePlaybackObservationIsOptional:
    """FE の再生観測が任意入力であることを検証する。"""

    def test_playback_stopped_does_not_call_stop_audio(self):
        """playback_stopped は送出停止を起こさない（BE が送出停止を所有）。"""
        bridge, observed, scheduled = _bridge_with_recording_core()
        bridge.notify(json.dumps({
            "type": "playback_stopped",
            "response_id": "50000000-0000-4000-8000-000000000010",
            "reason": "barge_in",
            "last_played_audio_sequence": 1,
        }).encode())
        asyncio.run(_drain(scheduled))
        assert observed == [("50000000-0000-4000-8000-000000000010", 1, "stopped", True)]

    def test_playback_completed_does_not_call_confirm_playback(self):
        """playback_completed は Core の confirm_playback を呼ばない。"""
        bridge, observed, scheduled = _bridge_with_recording_core()
        bridge.notify(json.dumps({
            "type": "playback_completed",
            "response_id": "50000000-0000-4000-8000-000000000010",
            "last_played_audio_sequence": 2,
            "response_finished": True,
        }).encode())
        asyncio.run(_drain(scheduled))
        assert observed == [("50000000-0000-4000-8000-000000000010", 2, "completed", True)]

    def test_last_played_audio_sequence_is_optional_in_schema(self):
        """last_played_audio_sequence が schema で optional であることを確認。"""
        from pathlib import Path
        repo = Path(__file__).resolve().parents[3]
        schema = json.loads(
            (repo / "contracts/voice-session/voice-session.schema.json").read_text()
        )
        stopped = schema["$defs"]["PlaybackStopped"]
        completed = schema["$defs"]["PlaybackCompleted"]
        assert "last_played_audio_sequence" not in stopped["required"]
        assert "last_played_audio_sequence" not in completed["required"]

        transport = json.loads(
            (repo / "contracts/livekit-transport/livekit-transport.schema.json").read_text()
        )
        confirmed = transport["$defs"]["OutputStopConfirmed"]
        assert "last_played_audio_sequence" not in confirmed["required"]

    def test_omitted_last_played_audio_sequence_passes_schema_and_bridge(self):
        """SCN-C8-P1: last_played_audio_sequence を省略した報告が実 validator と
        bridge を通り、観測値 None として転送される。
        """
        bridge, observed, scheduled = _bridge_with_recording_core()
        stopped = _playback_event("playback_stopped")
        # decode_core_event は実 schema の Draft202012Validator で検証する。
        decoded = delivery.decode_core_event(json.dumps(stopped).encode())
        assert decoded["type"] == "playback_stopped"
        assert "last_played_audio_sequence" not in decoded

        bridge.notify(json.dumps(decoded).encode())
        asyncio.run(_drain(scheduled))
        assert observed == [(decoded["response_id"], None, "stopped", True)]

    def test_omitted_last_played_audio_sequence_completed_passes_bridge(self):
        """playback_completed の省略報告も同じ契約で観測へ渡る。"""
        bridge, observed, scheduled = _bridge_with_recording_core()
        completed = _playback_event(
            "playback_completed", response_finished=True
        )
        decoded = delivery.decode_core_event(json.dumps(completed).encode())
        bridge.notify(json.dumps(decoded).encode())
        asyncio.run(_drain(scheduled))
        assert observed == [(decoded["response_id"], None, "completed", True)]

    def test_invalid_typed_last_played_audio_sequence_is_discarded(self):
        """SCN-C8-N1: 型不正（bool）の last_played_audio_sequence は有効な観測値
        として採用されず破棄される。
        """
        bridge, observed, scheduled = _bridge_with_recording_core()
        invalid = _playback_event(
            "playback_stopped", last_played_audio_sequence=True
        )
        # bridge は event.get() で値を取り型検証する。schema上も integer 以外は
        # 不正だが、bridge 側の型チェックが独立した防御として動く。
        bridge.notify(json.dumps(invalid).encode())
        asyncio.run(_drain(scheduled))
        assert observed == []


class TestPrivateOutputStopConfirmedOptional:
    """private protocol の output_stop_confirmed で last_played_audio_sequence が
    optional であることを検証する。
    """

    def test_omitted_last_played_audio_sequence_passes_private_schema(self):
        """last_played_audio_sequence を省略した output_stop_confirmed が
        実 schema validator を通る。
        """
        frame = {
            "type": "output_stop_confirmed",
            "protocol_version": "2.0",
            "session_id": "20000000-0000-4000-8000-000000000010",
            "response_id": "50000000-0000-4000-8000-000000000010",
            "request_id": str(uuid4()),
            "generation": 0,
            "output_confirmation": "output_clock_passed",
        }
        decoded = delivery.decode_private_frame(json.dumps(frame).encode())
        assert decoded["type"] == "output_stop_confirmed"
        assert "last_played_audio_sequence" not in decoded

    def test_omitted_last_played_audio_sequence_is_not_observed(self):
        """区間番号のない確認報告は差分観測値を生成しない。停止確定は
        BE の送出停止で決まり、この報告への依存はない。
        """
        from app.livekit_transport import coordinator as coordinator_module
        from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator

        observed: list[tuple[str, int]] = []
        published: list[tuple[bytes, str]] = []
        scheduled: list[asyncio.Task[None]] = []

        def collect(operation, _response_id):
            task = asyncio.create_task(operation)
            scheduled.append(task)
            return task

        coordinator = _coordinator(
            coordinator_module,
            published,
            [],
            output_stop_observation=lambda response_id, sequence: observed.append(
                (response_id, sequence)
            ),
            schedule=collect,
        )
        coordinator.participant_connected(
            identity=coordinator.user_identity,
            participant_sid="PA_current",
            room_sid="RM_one",
        )

        async def exercise() -> None:
            coordinator.notify_output_stop(
                "50000000-0000-4000-8000-000000000010"
            )
            await asyncio.gather(*scheduled)
            request = json.loads(published[-1][0])
            frame = {
                **request,
                "type": "output_stop_confirmed",
                "output_confirmation": "output_clock_passed",
            }
            assert "last_played_audio_sequence" not in frame
            await coordinator.receive_data(
                identity=coordinator.user_identity,
                participant_sid="PA_current",
                topic=coordinator_module.PRIVATE_TOPIC,
                payload=json.dumps(frame).encode(),
            )
            # prefix がないので差分観測へは渡されない。停止確定は変わらない。
            assert observed == []
            await coordinator.cleanup("test_complete")

        asyncio.run(exercise())


class TestProductionFeObservationWiring:
    """P-002: 本番と同じ bound method 接続でFE報告が差分観測へ届き、Sessionが継続する。"""

    def test_bound_observe_fe_playback_receives_reports_without_session_end(self):
        """bridgeの4位置引数呼出しが本番callback（delivery.observe_fe_playback）と一致する。

        現participantからの有効な `playback_stopped`・`playback_completed` が、
        Session所有タスク経由で差分観測へ届き、runtime_error cleanupを起こさない。
        """
        import time
        from unittest.mock import AsyncMock, Mock

        from app.livekit_transport import coordinator as coordinator_module
        from app.livekit_transport.core_delivery import (
            ProductionCoreEventInbox,
            _ConversationCoreDelivery,
        )
        from app.livekit_transport.measurement import LiveKitMeasurementSession
        from app.livekit_transport.response_audio import ResponseAudioTracks
        from app.livekit_transport.session_runtime import ProductionSessionOwner
        from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator

        class _CoreSession:
            async def end(self) -> None:
                return None

        response_id = "50000000-0000-4000-8000-000000000010"

        async def exercise() -> None:
            published: list[tuple[bytes, str]] = []
            cleaned: list[str] = []
            trace: list[object] = []

            inbox = ProductionCoreEventInbox()
            owner = ProductionSessionOwner(
                "20000000-0000-4000-8000-000000000010"
            )
            coordinator = _coordinator(
                coordinator_module, published, cleaned,
                core_port=inbox,
                schedule=lambda operation, _response_id: owner.schedule_task(
                    operation
                ),
            )
            owner.coordinator = coordinator
            coordinator.participant_connected(
                identity=coordinator.user_identity,
                participant_sid="PA_current",
                room_sid="RM_one",
            )
            tracks = Mock(spec=ResponseAudioTracks)
            tracks.stop_response = AsyncMock(return_value=2)
            delivery = _ConversationCoreDelivery(
                coordinator=coordinator,
                audio_source=tracks,
                character_participant_id="40000000-0000-4000-8000-000000000010",
                character_id="miori",
            )
            measurement = LiveKitMeasurementSession(
                session_id=coordinator.session_id, character_id="miori",
                measurement_kind="controlled_baseline", record=trace.append,
                clock_ns=time.monotonic_ns,
            )
            delivery.attach_measurement(measurement)
            measurement.bind_response(
                response_id=response_id,
                source_utterance_ids=("30000000-0000-4000-8000-000000000010",),
            )

            bridge = microphone_bridge._ConversationCoreBridge(
                _CoreSession(),
                lambda operation: owner.schedule_task(operation),
                playback_observation=delivery.observe_fe_playback,
                measurement=measurement,
            )
            inbox.bind(coordinator.session_id, bridge.notify)

            # BE推定2区間を確定させてからFE観測を受ける。
            class _Response:
                def __init__(self, rid: str) -> None:
                    self.response_id = rid

            await delivery.stop_response(
                _Response(response_id), decided_at_ns=time.monotonic_ns(),
            )

            event_base = {
                "protocol_version": "2.0",
                "session_id": coordinator.session_id,
                "response_id": response_id,
                "monotonic_timestamp_ms": 1_000,
            }
            stopped = {
                **event_base,
                "type": "playback_stopped",
                "event_id": str(uuid4()),
                "reason": "barge_in",
                "last_played_audio_sequence": 1,
            }
            completed = {
                **event_base,
                "type": "playback_completed",
                "event_id": str(uuid4()),
                "last_played_audio_sequence": 1,
                "response_finished": True,
            }
            for event in (stopped, completed):
                await coordinator.receive_data(
                    identity=coordinator.user_identity,
                    participant_sid="PA_current",
                    topic=coordinator_module.APPLICATION_TOPIC,
                    payload=json.dumps(event).encode(),
                )
            assert owner.tasks is not None
            await asyncio.gather(*owner.tasks)

            names = {getattr(event, "name", None) for event in trace}
            assert "fe_stopped_observed" in names
            assert "fe_completed_observed" in names
            deltas = {
                getattr(event, "name", ""): getattr(event, "value", None)
                for event in trace
                if str(getattr(event, "name", "")).startswith(
                    "playback_estimate_delta_"
                )
            }
            # BE推定2に対するFE報告1の差分が両終端報告へ記録される。
            assert deltas == {
                "playback_estimate_delta_stopped": -1.0,
                "playback_estimate_delta_completed": -1.0,
            }
            assert coordinator.phase == "available"
            assert cleaned == []
            await coordinator.cleanup("test_complete")
            if owner.tasks is not None:
                await asyncio.gather(*owner.tasks, return_exceptions=True)

        asyncio.run(exercise())

    def test_bound_observe_fe_playback_accepts_omitted_sequence(self):
        """区間番号を省略した有効報告も本番callback経由で受け、Sessionが継続する。"""
        import time
        from unittest.mock import AsyncMock, Mock

        from app.livekit_transport import coordinator as coordinator_module
        from app.livekit_transport.core_delivery import (
            ProductionCoreEventInbox,
            _ConversationCoreDelivery,
        )
        from app.livekit_transport.measurement import LiveKitMeasurementSession
        from app.livekit_transport.response_audio import ResponseAudioTracks
        from app.livekit_transport.session_runtime import ProductionSessionOwner
        from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator

        class _CoreSession:
            async def end(self) -> None:
                return None

        response_id = "50000000-0000-4000-8000-000000000010"

        async def exercise() -> None:
            published: list[tuple[bytes, str]] = []
            cleaned: list[str] = []
            trace: list[object] = []

            inbox = ProductionCoreEventInbox()
            owner = ProductionSessionOwner(
                "20000000-0000-4000-8000-000000000010"
            )
            coordinator = _coordinator(
                coordinator_module, published, cleaned,
                core_port=inbox,
                schedule=lambda operation, _response_id: owner.schedule_task(
                    operation
                ),
            )
            owner.coordinator = coordinator
            coordinator.participant_connected(
                identity=coordinator.user_identity,
                participant_sid="PA_current",
                room_sid="RM_one",
            )
            tracks = Mock(spec=ResponseAudioTracks)
            tracks.stop_response = AsyncMock(return_value=2)
            delivery = _ConversationCoreDelivery(
                coordinator=coordinator,
                audio_source=tracks,
                character_participant_id="40000000-0000-4000-8000-000000000010",
                character_id="miori",
            )
            measurement = LiveKitMeasurementSession(
                session_id=coordinator.session_id, character_id="miori",
                measurement_kind="controlled_baseline", record=trace.append,
                clock_ns=time.monotonic_ns,
            )
            delivery.attach_measurement(measurement)
            measurement.bind_response(
                response_id=response_id,
                source_utterance_ids=("30000000-0000-4000-8000-000000000010",),
            )
            bridge = microphone_bridge._ConversationCoreBridge(
                _CoreSession(),
                lambda operation: owner.schedule_task(operation),
                playback_observation=delivery.observe_fe_playback,
                measurement=measurement,
            )
            inbox.bind(coordinator.session_id, bridge.notify)

            omitted = {
                "type": "playback_stopped",
                "protocol_version": "2.0",
                "event_id": str(uuid4()),
                "session_id": coordinator.session_id,
                "response_id": response_id,
                "reason": "barge_in",
                "monotonic_timestamp_ms": 1_000,
            }
            assert "last_played_audio_sequence" not in omitted
            await coordinator.receive_data(
                identity=coordinator.user_identity,
                participant_sid="PA_current",
                topic=coordinator_module.APPLICATION_TOPIC,
                payload=json.dumps(omitted).encode(),
            )
            assert owner.tasks is not None
            await asyncio.gather(*owner.tasks)

            names = {getattr(event, "name", None) for event in trace}
            assert "fe_stopped_observed" in names
            # 区間番号なしの報告は数値差分を作らない。
            assert not any(
                str(getattr(event, "name", "")).startswith(
                    "playback_estimate_delta_"
                )
                for event in trace
            )
            assert coordinator.phase == "available"
            assert cleaned == []
            await coordinator.cleanup("test_complete")
            if owner.tasks is not None:
                await asyncio.gather(*owner.tasks, return_exceptions=True)

        asyncio.run(exercise())


class TestBeEstimationNotOverriddenByFe:
    """BE 推定値が FE 報告で上書きされないことを検証する。"""

    def test_different_response_fe_report_does_not_affect_estimation(self):
        """別応答の FE 報告は推定値へ混入しない。観測として別response_idで渡る。"""
        bridge, observed, scheduled = _bridge_with_recording_core()
        bridge.notify(json.dumps({
            "type": "playback_completed",
            "response_id": "other-response",
            "last_played_audio_sequence": 9,
            "response_finished": True,
        }).encode())
        asyncio.run(_drain(scheduled))
        # Coreの確定経路ではなく、response_idつきの観測としてだけ渡る。
        assert observed == [("other-response", 9, "completed", True)]
