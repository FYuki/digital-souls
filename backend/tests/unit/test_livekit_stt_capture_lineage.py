from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

import pytest

from tests.voice_capture_test_support import begin_capture, finish_capture

from app.livekit_transport.stt_audio import PcmCaptureSpan, stt_preparation_statistics

# microphone prerollは16kHz・16bit monoの2秒分。維持対象の契約値として固定する。
STT_MICROPHONE_PREROLL_BYTES = 16_000 * 2 * 2


def test_capture_span_keeps_received_offsets_without_pcm_or_clock():
    span=PcmCaptureSpan()
    span.append(12800,6400)
    span.append(19200,320)
    assert span.statistics(bytes(6720)) == {
        'stt_capture_received_span_valid':1,'stt_capture_raw_sample_count':3360,
        'stt_capture_received_start_sample':6400,'stt_capture_received_end_sample':9760}
    assert all(type(value) is int for value in span.statistics(bytes(6720)).values())


@pytest.mark.parametrize('next_start,byte_count', [(6402,320),(6398,320),(0,320),(6401,320),(6400,319),(-2,320),(True,320)])
def test_gap_overlap_reorder_and_partial_samples_are_sticky_invalid(next_start,byte_count):
    span=PcmCaptureSpan();span.append(0,6400);span.append(next_start,byte_count);span.append(6720,320)
    stats=span.statistics(bytes(7040))
    assert stats['stt_capture_received_span_valid']==0
    assert 'stt_capture_received_start_sample' not in stats


def test_capture_buffer_length_mismatch_or_absent_span_is_not_verified():
    span=PcmCaptureSpan()
    assert span.statistics(b'')['stt_capture_received_span_valid']==0
    span.append(0,4)
    assert span.statistics(b'123456')['stt_capture_received_span_valid']==0


def test_suffix_check_detects_equal_length_replacement_and_tail_truncation():
    original=struct.pack('<8h',0,0,0,1,3,9,-9,2)
    assert stt_preparation_statistics(original,original[6:],3)=={
        'stt_input_raw_sample_count':8,'stt_input_prepared_sample_count':5,
        'stt_input_removed_prefix_samples':3,'stt_input_suffix_preserved':1}
    assert stt_preparation_statistics(original,original[6:-2],3)['stt_input_suffix_preserved']==0
    assert stt_preparation_statistics(original,b'0'*10,3)['stt_input_suffix_preserved']==0


def test_bridge_preroll_queue_and_final_stt_preserve_the_exact_received_suffix():
    from app.livekit_transport.microphone_bridge import _ConversationCoreBridge
    observed=[];requests=[];tasks=[]
    measurement=SimpleNamespace(record_utterance_event=lambda **row:observed.append(row))
    class Core:
        accepting_input=True
        def start_transcription(self,**request):
            requests.append(request)
            task=asyncio.create_task(asyncio.sleep(0));tasks.append(task);return task
    def schedule(operation):tasks.append(asyncio.create_task(operation))
    bridge=_ConversationCoreBridge(Core(),schedule,measurement=measurement)
    bridge._transcription_active=True
    async def exercise():
        # 2秒を超えるprerollは実際の受信連番の末尾2秒へ対応する。
        quiet=bytes(STT_MICROPHONE_PREROLL_BYTES+6400)
        bridge.receive_microphone(quiet)
        begin_capture(bridge, 'first')
        voice=struct.pack('<4h',301,-702,1301,-2202)*1600
        bridge.receive_microphone(voice)
        tail=struct.pack('<2h',707,-909)*160
        bridge.receive_microphone(tail)
        await finish_capture(bridge, 'first')
        bridge.receive_microphone(b'\x66\x00' * 320)
        await asyncio.gather(*tasks);tasks.clear()
        assert not requests and len(bridge._pending_transcriptions)==1
        bridge._transcription_active=False
        bridge._start_next_transcription()
        await asyncio.gather(*tasks)
        expected=bytes(5120*2)+voice+tail
        assert requests[0]['audio']==expected
        stats={r['name']:r.get('value') for r in observed if r['utterance_id']=='first'}
        assert stats['stt_capture_received_span_valid']==1
        assert stats['stt_capture_received_start_sample']==3200
        assert stats['stt_capture_received_end_sample']==len(quiet+voice+tail)//2
        assert stats['stt_capture_raw_sample_count']==len(quiet[-STT_MICROPHONE_PREROLL_BYTES:]+voice+tail)//2
        assert stats['stt_input_raw_sample_count']==stats['stt_capture_raw_sample_count']
        assert stats['stt_input_removed_prefix_samples']==32000-5120
        assert stats['stt_input_suffix_preserved']==1
        assert stats['stt_input_sample_count']==stats['stt_input_prepared_sample_count']==len(expected)//2
        assert stats['stt_input_raw_sample_count']==stats['stt_input_removed_prefix_samples']+stats['stt_input_sample_count']
    asyncio.run(exercise())


def test_gap_between_two_received_pieces_is_not_hidden_by_equal_capture_length():
    from app.livekit_transport.microphone_bridge import _ConversationCoreBridge
    observed=[];tasks=[]
    class Core:
        accepting_input=True
        def start_transcription(self,**_):
            task=asyncio.create_task(asyncio.sleep(0));tasks.append(task);return task
    bridge=_ConversationCoreBridge(Core(),lambda op:tasks.append(asyncio.create_task(op)),measurement=SimpleNamespace(record_utterance_event=lambda **row:observed.append(row)))
    async def exercise():
        begin_capture(bridge, 'u')
        bridge.receive_microphone(b'\x11\x00'*320)
        # 別captureへ渡った範囲を診断上再現する。現在captureに追加されない受信位置を挟む。
        bridge._microphone_received_bytes+=640
        bridge.receive_microphone(b'\x12\x00'*320)
        await finish_capture(bridge, 'u')
        for _ in range(10):await asyncio.sleep(0)
        await asyncio.gather(*tasks)
    asyncio.run(exercise())
    assert next(r for r in observed if r['name']=='stt_capture_received_span_valid')['value']==0
    assert next(r for r in observed if r['name']=='stt_input_suffix_preserved')['value']==1


@pytest.mark.parametrize("media_end", [100000, 6400])
@pytest.mark.parametrize("end_shift", [0, 1])
def test_formal_vad_anchors_map_preroll_and_trimmed_capture_to_track_samples(media_end, end_shift):
    from unittest.mock import AsyncMock
    from app.livekit_transport.microphone_bridge import _ConversationCoreBridge
    from app.voice_input.detector import Detection
    from app.voice_input.session import InputGrant, SpeechBoundary
    async def scenario():
        observed, tasks, requests = [], [], []
        class Core:
            accepting_input = True
            session_id = "session"
            def start_transcription(self, **request):
                requests.append(request)
                task = asyncio.create_task(asyncio.sleep(0))
                tasks.append(task)
                return task
        bridge = _ConversationCoreBridge(
            Core(), lambda op: tasks.append(asyncio.create_task(op)),
            measurement=SimpleNamespace(record_utterance_event=lambda **row: observed.append(row)),
            publish_audio_event=AsyncMock(), verify_audio_integrity=AsyncMock(),
        )
        # 別入力で進んだbridge連番と、新しいtrackのsample位置を混同しない。
        bridge._microphone_received_bytes = 200000
        quiet = bytes(6400)
        bridge.receive_microphone(quiet)
        bridge._begin_capture({
            "type": "speech_started", "utterance_id": "u",
            "start_sample": media_end - 1600, "detected_sample": media_end,
        })
        voice = struct.pack("<2h", 700, -900) * 800
        bridge.receive_microphone(voice)
        end = media_end + len(voice) // 2
        await bridge._audio_stopped(SpeechBoundary(
            "u", InputGrant("TR_fixture", 1, "request", 1),
            Detection("ended", media_end - 1600, end + end_shift, end - 100),
        ))
        await asyncio.gather(*tasks)
        stats = {r["name"]: r.get("value") for r in observed}
        if end_shift:
            assert stats["stt_capture_media_span_valid"] == 0
            assert "stt_capture_media_start_sample" not in stats
            assert requests[0]["audio"] == quiet + voice
            return
        assert stats["stt_capture_media_span_valid"] == 1
        assert stats["stt_capture_media_start_sample"] == media_end - len(quiet) // 2
        assert stats["stt_capture_media_end_sample"] == end
        assert stats["vad_started_sample"] == media_end - 1600
        assert stats["vad_active_end_sample"] == end - 100
        assert stats["vad_detected_end_sample"] == end
        assert requests[0]["audio"] == quiet + voice
    asyncio.run(scenario())


def test_missing_formal_anchor_never_fabricates_media_positions():
    async def scenario():
        from tests.voice_capture_test_support import capture_harness
        bridge, _, tasks = capture_harness()
        observed = []
        bridge._measurement = SimpleNamespace(record_utterance_event=lambda **row: observed.append(row))
        begin_capture(bridge, "u")
        bridge.receive_microphone(b"\x11\x00" * 320)
        await finish_capture(bridge, "u")
        await asyncio.gather(*tasks)
        stats = {r["name"]: r.get("value") for r in observed}
        assert stats["stt_capture_received_span_valid"] == 1
        assert stats["stt_capture_media_span_valid"] == 0
        assert "stt_capture_media_start_sample" not in stats
    asyncio.run(scenario())
