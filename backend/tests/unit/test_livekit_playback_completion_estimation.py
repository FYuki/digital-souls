"""出力完了を「送出完了 + d」で判定する完了判定のテスト。

対象: Issue #540。PCM送出完了後に下り遅延 d だけ待って完了とする。
FE の `playback_completed` への待機は行わない。
待機中の取消は有効であることを確認する。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.livekit_transport.response_audio import ResponseAudioTracks


def _fake_track(response_id: str, *, finish_ns: int = 1234):
    """finish が即時完了するResponseTrack相当のfake。"""

    class _Pacer:
        input_sample_count = 960
        captured_sample_count = 1920
        padding_sample_count = 960
        max_queued_samples = 960
        capture_wait_ns = 0
        maximum_capture_wait_ns = 0
        input_wait_ns = 0
        schedule_reset_ns = 0
        finished = False

        async def finish(self):
            self.finished = True
            return finish_ns

        def stop(self):
            return None

    class _Source:
        def clear_queue(self):
            return None

    class _RtcTrack:
        def mute(self):
            return None

    class _Track:
        def __init__(self) -> None:
            self.response_id = response_id
            self.pacer = _Pacer()
            self.source = _Source()
            self.track = _RtcTrack()
            self.client_ready = asyncio.Event()
            self.source_closed = False
            self.segment_sample_ends = []

    return _Track()


class TestPlaybackCompletionBySendPosition:
    """送出完了 + d での応答完了判定を検証する。"""

    def test_completion_after_send_plus_delay(self):
        """送出完了後、d だけ待って完了する。"""
        async def exercise():
            tracks = ResponseAudioTracks(room=object(), downlink_delay_seconds=0.05)
            tracks._current = _fake_track("r1")
            tracks._requested_response_id = "r1"
            started = time.monotonic()
            first = await tracks.finish_response("r1")
            elapsed = time.monotonic() - started
            assert first == 1234
            assert elapsed >= 0.05
        asyncio.run(exercise())

    def test_cancellation_during_delay_is_effective(self):
        """d 待機中の取消は完了待ちを中断する。"""
        async def exercise():
            tracks = ResponseAudioTracks(room=object(), downlink_delay_seconds=5.0)
            tracks._current = _fake_track("r1")
            tracks._requested_response_id = "r1"
            finishing = asyncio.create_task(tracks.finish_response("r1"))
            await asyncio.sleep(0.05)  # pacer.finish()は即時完了し、sleep(d)に入る
            finishing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await finishing
        asyncio.run(exercise())

    def test_no_fe_notification_still_completes(self):
        """FE 通知なしでも送出完了 + d で完了する。"""
        async def exercise():
            tracks = ResponseAudioTracks(room=object(), downlink_delay_seconds=0.01)
            tracks._current = _fake_track("r1")
            tracks._requested_response_id = "r1"
            # FE通知を一切起こさなくても finish_response が完了する。
            assert await tracks.finish_response("r1") == 1234
        asyncio.run(exercise())


class TestPlaybackCompletionGateRemoval:
    """`PlaybackCompletionGate` の FE 待機除去を検証する。"""

    def test_gate_module_is_removed(self):
        """FE完了待ちのゲートは削除済み。完了は送出+dのみで判定される。"""
        with pytest.raises(ModuleNotFoundError):
            __import__("app.livekit_transport.playback_completion")


def test_finish_response_does_not_wait_for_stalled_finished_notification():
    """private `response_audio_finished` 送信が滞留しても、送出完了+dで
    Core完了が確定する（Issue #540）。"""
    import asyncio
    import json
    import time
    from types import SimpleNamespace

    from app.conversation_core.models import AudioSegment, Response, ResponseState
    from app.livekit_transport import coordinator as module
    from app.livekit_transport.core_delivery import _ConversationCoreDelivery
    from tests.unit.test_livekit_delivery_and_lifecycle import (
        RecordingCorePort, _coordinator,
    )

    response_id = "30000000-0000-4000-8000-000000000010"

    async def exercise() -> None:
        stall_entered = asyncio.Event()
        release_stall = asyncio.Event()
        published: list[tuple[bytes, str]] = []
        scheduled: list[asyncio.Task[None]] = []
        notify_failures: list[tuple[str, str]] = []

        async def publish(payload: bytes, topic: str) -> None:
            if topic == module.PRIVATE_TOPIC:
                frame = json.loads(payload)
                if frame.get("type") == "response_audio_finished":
                    stall_entered.set()
                    await release_stall.wait()
                    return
            published.append((payload, topic))

        def collect(operation, _response_id):
            task = asyncio.create_task(operation)
            scheduled.append(task)
            return task

        coordinator = _coordinator(
            module, published, [],
            schedule=collect,
            notify_failure=lambda response_id, name: notify_failures.append(
                (response_id, name)
            ),
        )
        # publishを差し替える（_coordinatorの固定publishは録画のみ）。
        coordinator._dependencies = coordinator._dependencies.__class__(
            **{**coordinator._dependencies.__dict__, "publish_data": publish},
        )
        coordinator.participant_connected(
            identity=coordinator.user_identity,
            participant_sid="PA_current",
            room_sid="RM_one",
        )

        class _Pacer:
            input_sample_count = 960
            captured_sample_count = 1920
            padding_sample_count = 960
            max_queued_samples = 960
            capture_wait_ns = 0
            maximum_capture_wait_ns = 0
            input_wait_ns = 0
            schedule_reset_ns = 0

            async def finish(self):
                return 1234

            def stop(self):
                return None

        class _Source:
            def clear_queue(self):
                return None

        class _RtcTrack:
            def mute(self):
                return None

        audio = ResponseAudioTracks(room=SimpleNamespace(local_participant=SimpleNamespace()),
                                    downlink_delay_seconds=0.01)
        audio._current = type("T", (), {
            "response_id": response_id, "pacer": _Pacer(),
            "source": _Source(), "track": _RtcTrack(),
            "client_ready": asyncio.Event(), "source_closed": False,
            "segment_sample_ends": [960],
        })()
        audio._requested_response_id = response_id

        delivery = _ConversationCoreDelivery(
            coordinator=coordinator,
            audio_source=audio,
            character_participant_id="40000000-0000-4000-8000-000000000010",
            character_id="miori",
        )
        # first_audio_out観測のsession_idを確定させる。
        delivery._session_id = "20000000-0000-4000-8000-000000000010"
        response = Response(
            response_id=response_id,
            generation=1,
            source_utterance_ids=(),
            state=ResponseState.IN_PROGRESS,
            audio_segments=(AudioSegment(1, b"audio", (0, 1)),),
        )

        started = time.monotonic()
        await delivery.finish_response(response)
        elapsed = time.monotonic() - started
        # 送出完了+dで確定し、private送信の滞留を待たない。
        assert elapsed >= 0.01
        assert elapsed < 0.5
        # 送信は非同期taskとして滞留している。
        await asyncio.wait_for(stall_entered.wait(), 0.5)
        release_stall.set()
        await asyncio.gather(*scheduled)
        await coordinator.cleanup("test_complete")
        assert notify_failures == []
    asyncio.run(exercise())


def test_finish_response_does_not_wait_for_stalled_first_audio_observation():
    """短いPCMで末尾まで初回送出が確定しない場合でも、first_audio_out観測の
    application topic送信が滞留しても送出完了+dでCore完了が確定する（Issue #540）。
    """
    import asyncio
    import json
    import time
    from types import SimpleNamespace

    from app.conversation_core.models import AudioSegment, Response, ResponseState
    from app.livekit_transport import coordinator as module
    from app.livekit_transport.core_delivery import _ConversationCoreDelivery
    from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator

    response_id = "30000000-0000-4000-8000-000000000010"

    async def exercise() -> None:
        stall_entered = asyncio.Event()
        release_stall = asyncio.Event()
        published: list[tuple[bytes, str]] = []
        scheduled: list[asyncio.Task[None]] = []
        notify_failures: list[tuple[str, str]] = []
        finished_published = asyncio.Event()

        async def publish(payload: bytes, topic: str) -> None:
            if topic == module.PRIVATE_TOPIC:
                finished_published.set()
                return
            frame = json.loads(payload)
            if frame.get("measurement") == "first_audio_out":
                stall_entered.set()
                await release_stall.wait()
            published.append((payload, topic))

        def collect(operation, _response_id):
            task = asyncio.create_task(operation)
            scheduled.append(task)
            return task

        coordinator = _coordinator(
            module, published, [],
            schedule=collect,
            notify_failure=lambda rid, name: notify_failures.append((rid, name)),
        )
        coordinator._dependencies = coordinator._dependencies.__class__(
            **{**coordinator._dependencies.__dict__, "publish_data": publish},
        )
        coordinator.participant_connected(
            identity=coordinator.user_identity,
            participant_sid="PA_current",
            room_sid="RM_one",
        )

        class _Pacer:
            input_sample_count = 960
            captured_sample_count = 1920
            padding_sample_count = 960
            max_queued_samples = 960
            capture_wait_ns = 0
            maximum_capture_wait_ns = 0
            input_wait_ns = 0
            schedule_reset_ns = 0

            async def finish(self):
                # 480 sample未満のPCM相当: 初回送出時刻が末尾のflushで初めて確定する。
                return 4321

            def stop(self):
                return None

        class _Source:
            def clear_queue(self):
                return None

        class _RtcTrack:
            def mute(self):
                return None

        audio = ResponseAudioTracks(room=SimpleNamespace(local_participant=SimpleNamespace()),
                                    downlink_delay_seconds=0.01)
        audio._current = type("T", (), {
            "response_id": response_id, "pacer": _Pacer(),
            "source": _Source(), "track": _RtcTrack(),
            "client_ready": asyncio.Event(), "source_closed": False,
            "segment_sample_ends": [960],
        })()
        audio._requested_response_id = response_id

        delivery = _ConversationCoreDelivery(
            coordinator=coordinator,
            audio_source=audio,
            character_participant_id="40000000-0000-4000-8000-000000000010",
            character_id="miori",
        )
        delivery._session_id = "20000000-0000-4000-8000-000000000010"
        response = Response(
            response_id=response_id,
            generation=1,
            source_utterance_ids=(),
            state=ResponseState.IN_PROGRESS,
            audio_segments=(AudioSegment(1, b"audio", (0, 1)),),
        )

        started = time.monotonic()
        await delivery.finish_response(response)
        elapsed = time.monotonic() - started
        # 観測送信の滞留を待たず、送出完了+dで完了を返す。
        assert elapsed >= 0.01
        assert elapsed < 0.5
        # 観測送信taskは滞留中に入っている。
        await asyncio.wait_for(stall_entered.wait(), 0.5)
        await asyncio.wait_for(finished_published.wait(), 0.5)
        release_stall.set()
        await asyncio.gather(*scheduled)
        measurements = [
            json.loads(payload)["measurement"]
            for payload, topic in published
            if topic == module.APPLICATION_TOPIC
            and json.loads(payload).get("type") == "observation"
        ]
        assert measurements == ["first_audio_out"]
        await coordinator.cleanup("test_complete")
        assert notify_failures == []
    asyncio.run(exercise())
