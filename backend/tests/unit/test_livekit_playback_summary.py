import asyncio
from uuid import uuid4

import pytest

from app.livekit_transport.measurement import LiveKitMeasurementSession
from app.livekit_transport.playback_completion import PlaybackCompletionGate
from app.livekit_transport.production import _ConversationCoreBridge
from app.voice_session.validation import parse_voice_session_event
from app.voice_session_metrics import SessionMetrics


def summary():
    return dict(expected_samples=2880, input_samples=1900, padding_samples=980,
                rendered_samples=2880, packet_count=3, first_output_frame=48000,
                last_output_end_frame=51008, gap_samples=128, maximum_gap_samples=128,
                gap_count=1, first_rtp_timestamp=4294967000,
                last_rtp_timestamp=(4294967000 + 1920) % 2**32,
                output_clock_context_time=1.1, output_clock_performance_time=1200,
                confirmation_observed_at_ms=1210, sample_rate=48000)


def measurement():
    events = []
    result = LiveKitMeasurementSession(session_id='session', character_id='fixture',
                                      measurement_kind='dogfood', record=events.append, clock_ns=lambda: 1000)
    result.bind_response(response_id='response', source_utterance_ids=('utterance',))
    for name, value in [('response_audio_input_samples', 1900),
                        ('response_audio_captured_samples', 2880),
                        ('response_audio_padding_samples', 980)]:
        result.record_response_event(response_id='response', name=name, value=value, stage='transport')
    return result, events


def test_dogfood_native_summary_preserves_gap_and_deduplicates():
    recorder, events = measurement()
    for _ in range(2):
        assert recorder.record_playback_summary(response_id='response', summary=summary())
    playback = {event.name: event for event in events if event.stage == 'playback'}
    assert len(events) == 9
    assert playback['playback_gap_total_ms'].value == pytest.approx(128 / 48)
    assert playback['playback_underrun_count'].value == 1
    assert playback['playback_duration_ms'].value == pytest.approx(3008 / 48)
    assert playback['frame_playout'].timestamp - playback['scheduled_playout'].timestamp == pytest.approx(128 / 48)
    assert all(event.measurement_kind == 'dogfood' and event.response_id == 'response' for event in events)


@pytest.mark.parametrize('damage', ['source', 'clock', 'unknown'])
def test_invalid_summary_never_becomes_zero_gap(damage):
    recorder, events = measurement()
    raw = summary()
    response_id = 'response'
    if damage == 'source': raw['input_samples'] += 1
    if damage == 'clock': raw['confirmation_observed_at_ms'] = 1
    if damage == 'unknown': response_id = 'unknown'
    assert not recorder.record_playback_summary(response_id=response_id, summary=raw)
    assert not any(event.stage == 'playback' for event in events)


def test_bridge_records_only_accepted_full_response_completion():
    async def exercise():
        recorder, events = measurement()
        gate = PlaybackCompletionGate()
        class Core:
            async def confirm_playback(self, **kwargs): pass
        session_events = []
        session_metrics = SessionMetrics(character_id="fixture", session_id="session",
            measurement_kind="dogfood", record=session_events.append)
        session_metrics.activate()
        bridge = _ConversationCoreBridge(Core(), lambda task: None,
                                         confirm_response_playback=gate.confirm, measurement=recorder,
                                         session_metrics=session_metrics)
        async def prepare(): pass
        pending = asyncio.create_task(gate.wait('response', 2, prepare))
        await asyncio.sleep(0)
        raw = dict(type='playback_completed', response_id='response',
                   last_played_audio_sequence=2, response_finished=True, playback_summary=summary())
        for invalid in ({**raw, 'response_finished': False},
                        {**raw, 'last_played_audio_sequence': 1},
                        {**raw, 'response_id': 'old-response'}):
            await bridge._receive(invalid)
        assert not any(event.stage == 'playback' for event in events)
        assert not any(event.name == "playback_completed" for event in session_events)
        await bridge._receive(raw)
        await bridge._receive(raw)
        await pending
        assert len([event for event in events if event.stage == 'playback']) == 6
        assert [event.response_id for event in session_events if event.name == 'playback_completed'] == ['response']
    asyncio.run(exercise())


@pytest.mark.parametrize('finished', [None, False, True])
def test_summary_contract_requires_finished_response(finished):
    event = dict(type='playback_completed', protocol_version="1.1", event_id=str(uuid4()),
                 session_id=str(uuid4()), response_id=str(uuid4()), last_played_audio_sequence=2,
                 monotonic_timestamp_ms=1300, playback_summary=summary())
    if finished is not None: event['response_finished'] = finished
    if finished is True:
        assert parse_voice_session_event(event).playback_summary is not None
    else:
        with pytest.raises(ValueError): parse_voice_session_event(event)
