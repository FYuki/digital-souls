from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.livekit_trace_report import finalize_livekit_dogfood_report
from app.livekit_transport.measurement import LiveKitMeasurementSession
from app.voice_metrics import TraceEvent
from app.voice_network_metrics import aggregate_network_trace, network_observation_from_trace, network_summary_values
from app.voice_session.validation import parse_voice_session_event


def summary(*, lost=1):
    return {"method": "browser_audio_rtp_counters_v1", "uplink": {"status": "measured", "bytes": 100, "packets": 8},
            "downlink": {"status": "measured", "bytes": 200, "packets": 4, "lostPackets": lost}}


def setup(*, utterances=("utterance-1",)):
    events: list[TraceEvent] = []
    measurement = LiveKitMeasurementSession(session_id="session-1", character_id="miori", measurement_kind="dogfood",
        record=events.append, clock_ns=lambda: 1_000_000)
    measurement.bind_response(response_id="response-1", source_utterance_ids=utterances)
    measurement.record_response_event(response_id="response-1", name="response_audio_captured_samples", stage="tts", value=3840)
    event = {"type": "observation", "session_id": "session-1", "response_id": "response-1", "event_id": "network-event",
             "measurement": "network_summary", "timestamp": 1000, "clock_domain": "client_monotonic", "unit": "millisecond", "network_summary": summary()}
    return measurement, events, event


def test_native_snapshot_is_correlated_and_deduplicated_before_aggregation():
    measurement, events, event = setup()
    assert measurement.record_client_observation(event)
    assert not measurement.record_client_observation(event)
    assert not measurement.record_client_observation({**event, "event_id": "second-event"})
    assert network_observation_from_trace(events) == event["network_summary"]
    result = aggregate_network_trace(events, condition="unit")
    assert result.sent_bytes.value == 100
    assert result.received_bytes.value == 200
    assert result.packet_loss_basis_points.value == 2000
    assert result.collection.trial_count == result.collection.loss_trials == 1


def test_merged_utterances_share_one_network_snapshot_not_double_bandwidth():
    measurement, events, event = setup(utterances=("utterance-1", "utterance-2"))
    assert measurement.record_client_observation(event)
    result = aggregate_network_trace(events, condition="unit")
    assert result.sent_bytes.value == 100
    assert result.collection.trial_count == 1


def test_missing_stats_remain_missing_without_marking_processing_failed(tmp_path):
    measurement, events, event = setup()
    event["network_summary"]["uplink"] = {"status": "missing", "reason": "stats_timeout"}
    assert measurement.record_client_observation(event)
    assert all(e.outcome == "success" for e in events)
    trace = tmp_path / 'trace.jsonl'
    trace.write_text(''.join(e.model_dump_json() + '\n' for e in events))
    output = tmp_path / 'dogfood.json'
    finalize_livekit_dogfood_report(trace_paths=[trace], output_path=output,
        schema_path=Path(__file__).resolve().parents[3] / 'docs/schemas/voice-quality-artifact-v1.schema.json', run_id='test')
    result = json.loads(output.read_text())
    assert result['run_counts']['success'] == 1
    assert result['network']['sent_bytes']['status'] == 'missing'
    assert result['network']['received_bytes']['value'] == 200
    assert result['network']['collection']['missing_trials'] == {'uplink:stats_timeout': 1}
    assert all(value not in output.read_text() for value in ['session-1', 'response-1', 'miori'])


def test_old_trace_is_missing_with_denominator_and_negative_loss_is_retained():
    measurement, events, event = setup()
    before = aggregate_network_trace(events, condition='unit')
    assert before.collection.trial_count == 1
    assert before.sent_bytes.status == 'missing'
    event['network_summary'] = summary(lost=-1)
    assert measurement.record_client_observation(event)
    result = aggregate_network_trace(events, condition='unit')
    assert result.collection.negative_loss_trials == 1
    assert network_observation_from_trace(events)['downlink']['lostPackets'] == -1


@pytest.mark.parametrize('change', [
    lambda e: e.update(session_id='other-session'),
    lambda e: e.update(response_id='unknown-response'),
    lambda e: e.update(timestamp=-1),
    lambda e: e.update(timestamp=True),
    lambda e: e.update(clock_domain='server_monotonic'),
    lambda e: e['network_summary'].update(prompt='private'),
    lambda e: e['network_summary']['downlink'].update(bytes=True),
    lambda e: e['network_summary']['downlink'].update(bytes=2**54),
    lambda e: e['network_summary']['downlink'].update(packets=3),
    lambda e: e['network_summary']['downlink'].update(lostPackets=-5),
    lambda e: e['network_summary']['uplink'].update(status='missing', reason='secret-error'),
])
def test_invalid_or_unrelated_summary_is_rejected_without_partial_trace(change):
    measurement, events, event = setup()
    count = len(events)
    change(event)
    assert not measurement.record_client_observation(event)
    assert len(events) == count


@pytest.mark.parametrize('damage', ['duplicate', 'partial', 'conflict', 'response'])
def test_trace_import_rejects_inconsistent_native_snapshot(damage):
    measurement, events, event = setup()
    assert measurement.record_client_observation(event)
    selected = [e for e in events if e.name.startswith('network_rtp_')]
    if damage == 'duplicate': selected.append(selected[0])
    if damage == 'partial': selected.pop()
    if damage == 'response': selected[0] = selected[0].model_copy(update={'response_id':'another'})
    if damage == 'conflict': selected.append(selected[0].model_copy(update={'utterance_id':'another', 'value':999}))
    with pytest.raises(ValueError): network_observation_from_trace(selected)


@pytest.mark.parametrize('change', [None, 'private', 'response', 'measurement', 'clock'])
def test_network_summary_protocol_is_numeric_and_client_response_bound(change):
    event = {'type':'observation', 'protocol_version':'1.0', 'event_id':str(uuid4()), 'session_id':str(uuid4()),
             'response_id':str(uuid4()), 'measurement':'network_summary', 'timestamp':1000,
             'clock_domain':'client_monotonic', 'unit':'millisecond', 'network_summary':summary()}
    if change == 'private': event['network_summary']['prompt']='private'
    if change == 'response': event.pop('response_id')
    if change == 'measurement': event['measurement']='playback_started'
    if change == 'clock': event.update(clock_domain='server_monotonic', unit='nanosecond', timestamp='1000')
    if change is None:
        parsed = parse_voice_session_event(event)
        assert parsed.network_summary.downlink.bytes == 200
    else:
        with pytest.raises(ValueError): parse_voice_session_event(event)
