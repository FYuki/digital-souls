from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

import pytest

from app.livekit_transport.response_audio import ResponseAudioTracks

A = "50000000-0000-4000-8000-000000000001"
B = "50000000-0000-4000-8000-000000000002"


@pytest.fixture
def rig(monkeypatch):
    sources, tracks, operations = [], [], []
    flags = SimpleNamespace(publish_error=False, publish_entered=None, publish_release=None, auto_ready=True,
                            publications={}, unpublish_error=None, remove_before_error=False)

    class Source:
        def __init__(self, rate, channels, *, queue_size_ms):
            assert (rate, channels, queue_size_ms) == (48000, 1, 0)
            self.frames, self.closed, self.clears = [], False, 0
            self.capture_entered = self.capture_release = None
            sources.append(self)

        async def capture_frame(self, frame):
            assert not self.closed
            self.frames.append(frame.data)
            if self.capture_entered is not None:
                self.capture_entered.set()
                await self.capture_release.wait()

        def clear_queue(self):
            assert not self.closed
            self.clears += 1
            if self.capture_release is not None:
                self.capture_release.set()

        async def aclose(self):
            self.closed = True

    class Track:
        def __init__(self, name, source):
            self.name, self.source, self.muted = name, source, False
            tracks.append(self)

        def mute(self):
            self.muted = True

    async def publish(track, options):
        assert options["dtx"] is False
        if flags.publish_entered is not None:
            flags.publish_entered.set()
            await flags.publish_release.wait()
        if flags.publish_error:
            raise RuntimeError('publish failed')
        sid = 'TR_' + str(len(tracks))
        operations.append(('publish', sid, track.name))
        if flags.auto_ready:
            asyncio.get_running_loop().call_soon(output.confirm_ready, track.name.split(':', 1)[1], sid)
        publication = SimpleNamespace(sid=sid)
        flags.publications[sid] = publication
        return publication

    class UnpublishTrackError(Exception):
        pass

    flags.error_type = UnpublishTrackError

    async def unpublish(sid):
        operations.append(('unpublish', sid))
        if flags.remove_before_error:
            flags.publications.pop(sid, None)
        if flags.unpublish_error is not None:
            raise flags.unpublish_error
        if sid not in flags.publications:
            raise UnpublishTrackError('track not found')
        flags.publications.pop(sid)

    rtc = SimpleNamespace(
        AudioSource=Source,
        LocalAudioTrack=SimpleNamespace(create_audio_track=Track),
        TrackSource=SimpleNamespace(SOURCE_MICROPHONE='microphone'),
        TrackPublishOptions=lambda **kwargs: kwargs,
        AudioFrame=lambda data, rate, channels, count: SimpleNamespace(data=data),
    )
    monkeypatch.setitem(sys.modules, 'livekit.rtc', rtc)
    monkeypatch.setitem(sys.modules, 'livekit.rtc.participant', SimpleNamespace(UnpublishTrackError=UnpublishTrackError))
    room = SimpleNamespace(local_participant=SimpleNamespace(publish_track=publish, unpublish_track=unpublish, track_publications=flags.publications))
    output = ResponseAudioTracks(room, ready_timeout_seconds=.05)
    return output, sources, tracks, operations, flags


def test_responses_use_distinct_sources_and_release_previous_track(rig):
    output, sources, tracks, operations, _ = rig
    async def exercise():
        observed = []
        output.set_observer(lambda name, response_id: observed.append((name, response_id)))
        assert sources == []
        await output.begin_response(A)
        await output.begin_response(A)
        await output.publish(b'\x01\x00' * 480, response_id=A)
        await output.begin_response(B)
        await output.publish(b'\x02\x00' * 480, response_id=B)
        assert sources[0].closed and tracks[0].muted
        assert not sources[1].closed
        assert sources[0].frames == [b'\x01\x00' * 480]
        assert sources[1].frames == [b'\x02\x00' * 480]
        assert operations == [('publish', 'TR_1', 'ds-response-v1:' + A),
                              ('unpublish', 'TR_1'), ('publish', 'TR_2', 'ds-response-v1:' + B)]
        await output.aclose()
        await output.aclose()
        assert sources[1].closed and tracks[1].muted
        assert observed == [
            ('response_audio_track_published', A),
            ('response_audio_track_ready', A),
            ('response_audio_track_unpublished', A),
            ('response_audio_source_closed', A),
            ('response_audio_track_published', B),
            ('response_audio_track_ready', B),
            ('response_audio_source_closed', B),
        ]
    asyncio.run(exercise())


def test_stopped_and_old_response_pcm_cannot_enter_new_source(rig):
    output, sources, tracks, _, _ = rig
    async def exercise():
        await output.begin_response(A)
        output.clear(A)
        with pytest.raises(asyncio.CancelledError):
            await output.publish(b'\x01\x00', response_id=A)
        with pytest.raises(asyncio.CancelledError):
            await output.begin_response(A)
        await output.begin_response(B)
        output.clear(A)
        assert not tracks[1].muted
        with pytest.raises(asyncio.CancelledError):
            await output.publish(b'\x01\x00', response_id=A)
        await output.publish(b'\x02\x00' * 480, response_id=B)
        assert sources[0].frames == []
        assert sources[1].frames == [b'\x02\x00' * 480]
        await output.aclose()
    asyncio.run(exercise())


def test_clear_releases_native_queue_while_capture_waits(rig):
    output, sources, tracks, _, _ = rig
    async def exercise():
        await output.begin_response(A)
        source = sources[0]
        source.capture_entered, source.capture_release = asyncio.Event(), asyncio.Event()
        capture = asyncio.create_task(output.publish(b'\x01\x00' * 480, response_id=A))
        await source.capture_entered.wait()
        output.clear(A)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(capture, .5)
        assert tracks[0].muted and source.clears == 1
        await output.aclose()
    asyncio.run(exercise())


def test_failed_track_publication_releases_native_source(rig):
    output, sources, _, _, flags = rig
    flags.publish_error = True
    async def exercise():
        with pytest.raises(RuntimeError, match='publish failed'):
            await output.begin_response(A)
        assert sources[0].closed
        await output.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('operation', ['clear', 'close', 'cancel_task'])
def test_stop_during_track_publication_never_leaves_a_writable_source(rig, operation):
    output, sources, tracks, _, flags = rig
    async def exercise():
        flags.publish_entered, flags.publish_release = asyncio.Event(), asyncio.Event()
        begin = asyncio.create_task(output.begin_response(A))
        await flags.publish_entered.wait()
        close = None
        if operation == 'clear':
            output.clear(A)
        elif operation == 'close':
            close = asyncio.create_task(output.aclose())
            await asyncio.sleep(0)
        else:
            begin.cancel()
        flags.publish_release.set()
        with pytest.raises(asyncio.CancelledError):
            await begin
        if close is not None:
            await asyncio.wait_for(close, .5)
        else:
            await output.aclose()
        assert sources[0].closed
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await output.publish(b'\x01\x00', response_id=A)
    asyncio.run(exercise())


def test_invalid_response_or_partial_pcm_is_rejected(rig):
    output, sources, _, _, _ = rig
    async def exercise():
        with pytest.raises(ValueError):
            await output.begin_response('not-a-response-id')
        assert sources == []
        await output.begin_response(A)
        for pcm in [b'', b'x']:
            with pytest.raises(ValueError):
                await output.publish(pcm, response_id=A)
        assert sources[0].frames == []
        await output.aclose()
        with pytest.raises(RuntimeError):
            await output.begin_response(B)
    asyncio.run(exercise())


def test_finish_drains_partial_samples_and_reports_padding_separately(rig):
    output, sources, _, _, _ = rig
    async def exercise():
        await output.begin_response(A)
        assert await output.publish(b"\x03\x00" * 481, response_id=A) is not None
        await output.finish_response(A)
        stats = output.statistics(A)
        assert stats["response_audio_input_samples"] == 481
        assert stats["response_audio_captured_samples"] == 1920
        assert stats["response_audio_padding_samples"] == 1439
        assert stats["response_audio_max_queued_samples"] <= 48000
        assert b"".join(sources[0].frames) == b"\x03\x00" * 481 + bytes(1439 * 2)
        await output.aclose()
    asyncio.run(exercise())


def test_readiness_requires_current_response_and_exact_track_before_capture(rig):
    output, sources, _, _, flags = rig
    flags.auto_ready = False
    async def exercise():
        await output.begin_response(A)
        publish = asyncio.create_task(output.publish(bytes(960), response_id=A))
        await asyncio.sleep(0)
        assert not output.confirm_ready(B, "TR_1")
        assert not output.confirm_ready(A, "TR_other")
        assert sources[0].frames == []
        assert not publish.done()
        assert output.confirm_ready(A, "TR_1")
        await publish
        assert len(sources[0].frames) == 1
        await output.aclose()
    asyncio.run(exercise())


def test_readiness_before_publish_completion_is_bound_to_actual_sid(rig):
    output, sources, _, _, flags = rig
    flags.auto_ready = False
    async def exercise():
        flags.publish_entered, flags.publish_release = asyncio.Event(), asyncio.Event()
        begin = asyncio.create_task(output.begin_response(A))
        await flags.publish_entered.wait()
        assert not output.confirm_ready(A, "TR_1")
        flags.publish_release.set()
        await begin
        await output.publish(bytes(960), response_id=A)
        assert len(sources[0].frames) == 1
        await output.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["timeout", "clear", "close", "cancel_task"])
def test_no_audio_on_missing_readiness_or_stop_while_waiting(rig, operation):
    output, sources, tracks, _, flags = rig
    flags.auto_ready = False
    async def exercise():
        await output.begin_response(A)
        publish = asyncio.create_task(output.publish(bytes(960), response_id=A))
        await asyncio.sleep(0)
        close = None
        if operation == "clear":
            output.clear(A)
        elif operation == "close":
            close = asyncio.create_task(output.aclose())
        elif operation == "cancel_task":
            publish.cancel()
        with pytest.raises(TimeoutError if operation == "timeout" else asyncio.CancelledError):
            await publish
        if close is not None:
            await close
        assert sources[0].frames == []
        assert tracks[0].muted
        assert not output.confirm_ready(A, "TR_1")
        await output.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('case', ['already_removed', 'removed_during_call', 'still_present', 'other_error'])
def test_retire_only_verified_absent_publication_after_reconnect(rig, case):
    output, sources, tracks, operations, flags = rig
    async def exercise():
        await output.begin_response(A)
        if case == 'already_removed':
            flags.publications.pop('TR_1')
        else:
            flags.unpublish_error = flags.error_type('private-sentinel') if case != 'other_error' else RuntimeError('private-sentinel')
            flags.remove_before_error = case != 'still_present'
        if case in ('still_present', 'other_error'):
            with pytest.raises(type(flags.unpublish_error)):
                await output.begin_response(B)
            assert len(tracks) == 1
        else:
            await output.begin_response(B)
            await output.publish(bytes(960), response_id=B)
            assert sources[0].closed and tracks[0].muted
            assert len(sources[1].frames) == 1
            assert 'TR_2' in flags.publications
            if case == 'already_removed':
                assert ('unpublish', 'TR_1') not in operations
        await output.aclose()
    asyncio.run(exercise())


def test_confirmed_stop_joins_capture_and_retains_muted_track(rig):
    output, sources, tracks, operations, _ = rig
    async def exercise():
        await output.begin_response(A)
        source = sources[0]
        entered, cancelled, release, closed = [asyncio.Event() for _ in range(4)]
        async def capture(frame):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                source.frames.append(frame.data)
            finally:
                closed.set()
        source.capture_frame = capture
        publishing = asyncio.create_task(output.publish(b"\x01\x00" * 480, response_id=A))
        await entered.wait()
        stopping = asyncio.create_task(output.stop_response(A))
        await cancelled.wait()
        assert not stopping.done()
        assert tracks[0].muted and source.clears == 1
        release.set()
        await asyncio.wait_for(stopping, 0.5)
        with pytest.raises(asyncio.CancelledError):
            await publishing
        assert closed.is_set() and source.clears == 2
        assert not source.closed
        assert not [operation for operation in operations if operation[0] == "unpublish"]
        with pytest.raises(asyncio.CancelledError):
            await output.begin_response(A)
        await output.aclose()
    asyncio.run(exercise())


def test_stop_before_track_creation_prevents_late_start_and_allows_next_response(rig):
    output, sources, _, _, _ = rig
    async def exercise():
        await output.stop_response(A)
        with pytest.raises(asyncio.CancelledError):
            await output.begin_response(A)
        assert sources == []
        await output.begin_response(B)
        await output.publish(b"\x01\x00" * 480, response_id=B)
        await output.aclose()
    asyncio.run(exercise())


def test_livekit_ack_completes_core_cancellation_while_control_queue_is_waiting(rig):
    import json
    from app.conversation_core import ConversationCoreSession, ResponseState
    from app.livekit_transport import coordinator as module
    from app.livekit_transport.production import _ConversationCoreBridge, _ConversationCoreDelivery
    from tests.unit.test_livekit_delivery_and_lifecycle import _coordinator
    from tests.conversation_core_test_support import BlockingLlm, RecordingPersistence, RecordingObservation, RecordingStt, RecordingTts
    output, _, _, _, _ = rig
    async def exercise():
        from app.livekit_transport.measurement import LiveKitMeasurementSession
        published, scheduled, trace = [], [], []
        coordinator = _coordinator(module, published, [])
        coordinator.participant_connected(identity=coordinator.user_identity, participant_sid="PA_current", room_sid="RM_one")
        delivery = _ConversationCoreDelivery(coordinator=coordinator, audio_source=output,
            character_participant_id=B, character_id="miori")
        delivery.attach_measurement(LiveKitMeasurementSession(session_id=coordinator.session_id,
            character_id="miori", measurement_kind="controlled_baseline", record=trace.append, clock_ns=time.monotonic_ns))
        session = ConversationCoreSession(session_id=coordinator.session_id, response_id_factory=lambda: A,
            delivery=delivery, cancellation=delivery, persistence=RecordingPersistence(),
            observation=RecordingObservation(), stt=RecordingStt(), llm=BlockingLlm(), tts=RecordingTts())
        await session.finalize_utterance(utterance_id=B, transcript="開始", should_response=True)
        bridge = _ConversationCoreBridge(session, lambda operation: scheduled.append(asyncio.create_task(operation)))
        bridge.notify(json.dumps({"type": "response_cancel_requested", "response_id": A, "reason": "barge_in"}).encode())
        async def request_sent():
            while True:
                for payload, _ in published:
                    frame = json.loads(payload)
                    if frame["type"] == "output_stop_request":
                        return frame
                await asyncio.sleep(0)
        request = await asyncio.wait_for(request_sent(), 0.5)
        assert session.response(A).state is ResponseState.CANCELLING
        assert not scheduled[0].done()
        await coordinator.receive_data(identity=coordinator.user_identity, participant_sid="PA_current",
            topic=module.PRIVATE_TOPIC, payload=json.dumps({**request, "type": "output_stop_confirmed",
                "last_played_audio_sequence": 0, "output_confirmation": "never_connected"}).encode())
        await asyncio.wait_for(asyncio.gather(*scheduled), 0.5)
        assert session.response(A).state is ResponseState.CANCELLED
        assert [event.event_id for event in trace if event.name == "output_stop_confirmed"] == [request["request_id"]]
        times = {event.name: event.timestamp for event in trace}
        assert times["output_stop_requested"] <= times["response_audio_source_stopped"]
        assert times["response_audio_source_stopped"] <= times["output_stop_confirmed"] <= times["cancel_state_lower"]
        await session.end()
        await output.aclose()
        await coordinator.cleanup("test_complete")
    asyncio.run(exercise())


def test_cancellation_during_unpublish_preserves_cancellation_and_closes_owned_source(rig):
    output, sources, tracks, _, flags = rig
    async def exercise():
        await output.begin_response(A)
        flags.unpublish_error = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await output.begin_response(B)
        assert len(sources) == len(tracks) == 1
        await output.aclose()
        assert sources[0].closed
    asyncio.run(exercise())
