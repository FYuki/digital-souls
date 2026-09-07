import asyncio

from app.conversation_core import AudioSegment, ConversationCoreSession, StageObservation, TextDelta
from app.conversation_core.provider_result_audit import PROVIDER_RESULT_METRICS, ProviderResultAudit
from app.livekit_transport.measurement import LiveKitMeasurementSession
from tests.conversation_core_test_support import RecordingDelivery, RecordingObservation, RecordingPersistence, RecordingStt


def test_only_post_cancel_results_are_counted_in_utf16_and_bytes_without_payload_storage():
    audit=ProviderResultAudit()
    audit.text('before',cancelled=False);audit.audio(b'before',cancelled=False)
    audit.text('後😀',cancelled=True);audit.text('後😀',cancelled=True)
    audit.audio(b'private-audio-sentinel',cancelled=True)
    stats=audit.closed_statistics()
    assert stats['provider_result_text_after_cancel_events']==2
    assert stats['provider_result_text_after_cancel_utf16_units']==6
    assert stats['provider_result_audio_after_cancel_events']==1
    assert stats['provider_result_audio_after_cancel_bytes']==len(b'private-audio-sentinel')
    assert stats['provider_result_observation_valid']==1
    assert set(stats)==PROVIDER_RESULT_METRICS
    assert all(type(v) in (int,bool) for v in vars(audit).values())
    assert 'private-audio-sentinel' not in repr(stats)


def test_invalid_provider_payload_cannot_be_a_valid_zero_result():
    audit=ProviderResultAudit()
    audit.text(None,cancelled=True)
    audit.audio('not-bytes',cancelled=True)
    assert audit.closed_statistics()['provider_result_observation_valid']==0


def test_provider_callbacks_after_cancellation_are_counted_and_closed_only_after_both_consumers_finish():
    async def exercise():
        class Llm:
            def __init__(self):self.late=asyncio.Event();self.release=asyncio.Event()
            async def generate(self,_):
                yield TextDelta(1,'開始。',(0,3))
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    yield TextDelta(2,'後😀',(3,5))
                    self.late.set()
                    await self.release.wait()
        class Tts:
            def __init__(self):self.entered=asyncio.Event();self.late=asyncio.Event();self.release=asyncio.Event()
            async def synthesize(self,text):
                self.entered.set()
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    yield AudioSegment(1,b'private-late-audio',(0,len(text)))
                    self.late.set()
                    await self.release.wait()
        llm,tts=Llm(),Tts()
        observations=RecordingObservation();delivery=RecordingDelivery()
        session=ConversationCoreSession(session_id='s',response_id_factory=lambda:'r',
            delivery=delivery,persistence=RecordingPersistence(),observation=observations,
            stt=RecordingStt(),llm=llm,tts=tts)
        response=await session.finalize_utterance(utterance_id='u',transcript='開始',should_response=True)
        await asyncio.wait_for(tts.entered.wait(),.5)
        await session.cancel_response(response_id=response.response_id,reason='barge_in')
        await asyncio.wait_for(asyncio.gather(llm.late.wait(),tts.late.wait()),.5)
        assert not [r for r in observations.observations if r.stage in PROVIDER_RESULT_METRICS]
        llm.release.set();tts.release.set()
        async def drained():
            while session.running_stage_count:await asyncio.sleep(0)
        await asyncio.wait_for(drained(),.5)
        stats={r.stage:r.value for r in observations.observations if r.stage in PROVIDER_RESULT_METRICS}
        assert stats==dict(provider_result_text_after_cancel_events=1,provider_result_text_after_cancel_utf16_units=3,
            provider_result_audio_after_cancel_events=1,provider_result_audio_after_cancel_bytes=len(b'private-late-audio'),
            provider_result_observation_closed=1,provider_result_observation_valid=1)
        assert session.response('r').generated_text=='開始。'
        assert not session.response('r').audio_segments
        assert not [r for r in observations.observations if r.outcome=='failed']
        assert [e.text for e in delivery.events if e.type=='response_delta']==['開始。']
        await session.end()
    asyncio.run(exercise())


def test_closed_zero_counts_are_trace_diagnostics_not_failed_cancel_stages():
    events=[]
    measurement=LiveKitMeasurementSession(session_id='s',character_id='c',measurement_kind='controlled_baseline',record=events.append,clock_ns=lambda:1000)
    measurement.bind_response(response_id='r',source_utterance_ids=('u',))
    async def exercise():
        for name,value in ProviderResultAudit().closed_statistics().items():
            await measurement.record(StageObservation(session_id='s',response_id='r',generation=1,stage=name,outcome='completed',value=value))
    asyncio.run(exercise())
    assert len(events)==6 and {e.name for e in events}==PROVIDER_RESULT_METRICS
    assert all(e.stage=='provider_result_received' and e.outcome=='success' and e.reason_code is None for e in events)
    assert all(e.response_id=='r' and e.utterance_id=='u' for e in events)


def test_stopping_receipts_have_separate_trace_stage_and_do_not_change_after_cancel_counts():
    from app.conversation_core.provider_result_audit import PROVIDER_STOPPING_METRICS
    audit = ProviderResultAudit()
    audit.text("後😀", cancelled=False, stopping=True)
    audit.audio(b"pcm", cancelled=False, stopping=True)
    events = []
    measurement = LiveKitMeasurementSession(session_id="s", character_id="c",
        measurement_kind="controlled_baseline", record=events.append, clock_ns=lambda: 1000)
    measurement.bind_response(response_id="r", source_utterance_ids=("u",))
    async def exercise():
        for name, value in audit.stopping_statistics().items():
            await measurement.record(StageObservation(session_id="s", response_id="r",
                generation=1, stage=name, outcome="completed", value=value))
    asyncio.run(exercise())
    assert {event.name for event in events} == PROVIDER_STOPPING_METRICS
    assert all(event.stage == "provider_result_stopping" for event in events)
    assert audit.closed_statistics()["provider_result_text_after_cancel_utf16_units"] == 0
    assert audit.stopping_statistics()["provider_result_text_during_stop_utf16_units"] == 3
    assert audit.stopping_statistics()["provider_result_audio_during_stop_bytes"] == 3
    assert all(type(value) in (int, bool) for value in vars(audit).values())
