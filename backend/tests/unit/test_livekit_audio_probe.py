from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from app.livekit_transport import audio_probe

A = "50000000-0000-4000-8000-000000000001"
B = "50000000-0000-4000-8000-000000000002"
SID = "TR_probe"


@pytest.fixture
def rig(monkeypatch):
    sources, tracks, frames, tasks = [], [], [], []
    state = SimpleNamespace(generation=1, available=True, early_ready=True, auto_complete=True,
                            publish_error=False, unpublish_error=False, removed=False, block_cleanup=False)
    published, captured, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    publications = {}

    class Source:
        def __init__(self, rate, channels, *, queue_size_ms):
            assert (rate, channels, queue_size_ms) == (48000, 1, 0)
            self.pcm, self.closed = bytearray(), False
            sources.append(self)

        async def capture_frame(self, frame):
            assert not self.closed
            self.pcm.extend(frame.data)
            captured.set()

        async def aclose(self):
            self.closed = True

    class Track:
        def __init__(self, name, source):
            self.name, self.source, self.muted = name, source, False
            tracks.append(self)

        def mute(self):
            self.muted = True

    class PublishTrackError(Exception):
        pass

    class UnpublishTrackError(Exception):
        pass

    async def publish(track, options):
        assert options == dict(source='microphone', dtx=False)
        if state.publish_error:
            raise PublishTrackError('private SDK details must not be logged')
        if state.early_ready:
            output.receive('audio_probe_ready', track.name.split(':')[1], 1, SID)
        publications[SID] = SimpleNamespace(sid=SID)
        published.set()
        return publications[SID]

    async def unpublish(sid):
        if state.block_cleanup:
            await asyncio.Event().wait()
        if state.removed:
            publications.pop(sid)
        if state.unpublish_error:
            raise UnpublishTrackError('not found')
        publications.pop(sid, None)

    async def notify(frame):
        frames.append(frame)
        finished.set()
        if state.auto_complete:
            output.receive('audio_probe_complete', str(frame['probe_id']), 1, SID)

    def schedule(operation):
        task = asyncio.create_task(operation)
        tasks.append(task)
        return task

    monkeypatch.setitem(sys.modules, 'livekit.rtc', SimpleNamespace(
        AudioSource=Source, AudioFrame=lambda data, *args: SimpleNamespace(data=data),
        LocalAudioTrack=SimpleNamespace(create_audio_track=Track),
        TrackSource=SimpleNamespace(SOURCE_MICROPHONE='microphone'),
        TrackPublishOptions=lambda **kwargs: kwargs, UnpublishTrackError=UnpublishTrackError,
    ))
    room = SimpleNamespace(local_participant=SimpleNamespace(publish_track=publish,
        unpublish_track=unpublish, track_publications=publications))
    output = audio_probe.AudioProbePublisher(room, current=lambda gen: state.available and gen == state.generation,
        publish=notify, schedule=schedule)
    return SimpleNamespace(output=output, sources=sources, tracks=tracks, frames=frames, tasks=tasks,
        state=state, published=published, captured=captured, finished=finished, publications=publications)


def test_probe_uses_real_pacer_and_waits_for_complete_before_retiring_track(rig):
    async def exercise():
        rig.state.auto_complete = False
        rig.output.request(A, 1)
        await asyncio.wait_for(rig.finished.wait(), 1)
        assert not rig.tasks[0].done() and not rig.sources[0].closed
        assert bytes(rig.sources[0].pcm) == audio_probe.probe_pcm() + bytes(1920)
        assert rig.tracks[0].name == 'ds-audio-probe-v1:' + A
        assert rig.frames == [dict(protocol_version='1.0', type='audio_probe_finished', probe_id=A,
            generation=1, track_sid=SID, input_sample_count=9600, captured_sample_count=10560, padding_sample_count=960)]
        rig.output.receive('audio_probe_complete', A, 1, SID)
        await asyncio.wait_for(rig.tasks[0], 1)
        assert rig.sources[0].closed and rig.tracks[0].muted and not rig.publications
    asyncio.run(exercise())


def test_no_pcm_until_matching_current_ready_and_no_premature_complete(rig):
    async def exercise():
        rig.state.early_ready = rig.state.auto_complete = False
        rig.output.request(A, 1)
        await rig.published.wait()
        for nonce, gen, sid in [(B, 1, SID), (A, 0, SID), (A, 2, SID), (A, 1, 'TR_other')]:
            rig.output.receive('audio_probe_ready', nonce, gen, sid)
        rig.output.receive('audio_probe_complete', A, 1, SID)
        await asyncio.sleep(.02)
        assert not rig.captured.is_set()
        rig.output.receive('audio_probe_ready', A, 1, SID)
        await asyncio.wait_for(rig.finished.wait(), 1)
        await asyncio.sleep(0)
        assert not rig.tasks[0].done()
        await rig.output.cancel()
        assert rig.sources[0].closed and not rig.publications
    asyncio.run(exercise())


@pytest.mark.parametrize('phase', ['waiting_ready', 'sending', 'waiting_complete'])
def test_cancellation_stops_pcm_and_releases_every_owned_resource(rig, phase):
    async def exercise():
        rig.state.early_ready = phase != 'waiting_ready'
        rig.state.auto_complete = False
        rig.output.request(A, 1)
        await asyncio.wait_for({'waiting_ready': rig.published, 'sending': rig.captured,
                                'waiting_complete': rig.finished}[phase].wait(), 1)
        rig.state.generation = 2
        await rig.output.cancel()
        count = len(rig.sources[0].pcm)
        await asyncio.sleep(.02)
        assert len(rig.sources[0].pcm) == count
        assert rig.sources[0].closed and rig.tracks[0].muted and not rig.publications
        assert rig.tasks[0].done()
        if phase != 'waiting_complete':
            assert not rig.frames
    asyncio.run(exercise())


def test_invalid_reused_and_busy_requests_cannot_create_extra_tracks(rig):
    async def exercise():
        for nonce, generation in [('invalid', 1), (A, 0), (A, 2)]:
            rig.output.request(nonce, generation)
        assert not rig.tasks
        rig.state.early_ready = False
        rig.output.request(A, 1)
        rig.output.request(B, 1)
        await rig.published.wait()
        await rig.output.cancel()
        rig.output.request(A, 1)
        assert len(rig.tasks) == len(rig.sources) == 1
        rig.state.early_ready = True
        rig.output.request(B, 1)
        await asyncio.wait_for(rig.tasks[-1], 1)
        assert len(rig.sources) == 2 and all(s.closed for s in rig.sources)
    asyncio.run(exercise())


@pytest.mark.parametrize('failure', ['publish', 'ready_timeout', 'complete_timeout'])
def test_failure_never_leaves_sending_track_or_leaks_sdk_details(rig, monkeypatch, caplog, failure):
    monkeypatch.setattr(audio_probe, 'READY_TIMEOUT_SECONDS', .02)
    monkeypatch.setattr(audio_probe, 'COMPLETE_TIMEOUT_SECONDS', .02)
    async def exercise():
        rig.state.publish_error = failure == 'publish'
        rig.state.early_ready = failure != 'ready_timeout'
        rig.state.auto_complete = failure != 'complete_timeout'
        rig.output.request(A, 1)
        await asyncio.wait_for(rig.tasks[0], 1)
        assert all(s.closed for s in rig.sources) and not rig.publications
        assert all(t.muted for t in rig.tracks)
        assert len(rig.frames) == (1 if failure == 'complete_timeout' else 0)
        assert 'Audio probe failed: type=' in caplog.text
        assert 'private SDK details' not in caplog.text
    asyncio.run(exercise())


@pytest.mark.parametrize('removed', [False, True])
def test_unpublish_race_only_ignores_already_removed_track_and_always_closes_source(rig, removed):
    async def exercise():
        rig.state.unpublish_error, rig.state.removed = True, removed
        rig.output.request(A, 1)
        if removed:
            await asyncio.wait_for(rig.tasks[0], 1)
        else:
            with pytest.raises(Exception, match='not found'):
                await asyncio.wait_for(rig.tasks[0], 1)
        assert rig.sources[0].closed and rig.tracks[0].muted
    asyncio.run(exercise())


def test_cleanup_timeout_does_not_block_session_teardown(rig, monkeypatch):
    monkeypatch.setattr(audio_probe, 'CLEANUP_TIMEOUT_SECONDS', .02)
    async def exercise():
        rig.state.block_cleanup = True
        rig.output.request(A, 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(rig.tasks[0], 1)
        assert rig.sources[0].closed and rig.tracks[0].muted
    asyncio.run(exercise())


def test_probe_gate_requires_every_isolation_setting():
    env = dict(DS_ENVIRONMENT_ID='test', DS_PROFILE='integration-voice-fault', VOICE_MEASUREMENT_KIND='controlled_baseline')
    for url in ['ws://127.0.0.1:19880', 'ws://localhost:19880']:
        assert audio_probe.audio_probe_enabled(env, url)
        for key in env:
            assert not audio_probe.audio_probe_enabled({k: v for k, v in env.items() if k != key}, url)
            assert not audio_probe.audio_probe_enabled({**env, key: 'dogfood'}, url)
    for url in ['ws://localhost:7880', 'ws://127.0.0.1:17880', 'wss://example.com:19880']:
        assert not audio_probe.audio_probe_enabled(env, url)
