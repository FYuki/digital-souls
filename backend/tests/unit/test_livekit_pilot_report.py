from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.livekit_pilot_report import finalize_livekit_controlled, finalize_livekit_pilot


@pytest.fixture
def pilot_inputs(tmp_path):
    root = Path(__file__).resolve().parents[3]
    fixture = json.loads((root / 'frontend/playwright/fixtures/speech.metadata.json').read_text())
    trials = []
    events = []
    for phase in ('warmup', 'measured'):
        trial = {
            **{name: str(uuid4()) for name in ('sessionId', 'utteranceId', 'responseId', 'conversationId')},
            'phase': phase, 'outcome': 'success', 'transcript_matches': True,
            'fixture_version': fixture['fixture_version'], 'audio_sha256': fixture['audio_sha256'],
            'initial_state_hash': 'requested-state', 'fixtureStartedAt': 100,
            'startedAt': 1500, 'fixture_speech_end_client_ms': 1040,
        }
        trials.append(trial)
        for index, name in enumerate(('stt_completed', 'llm_completed', 'tts_pipeline_completed',
                                      'utterance_finalized', 'first_playback')):
            client = name == 'first_playback'
            events.append({
                'schema_version': '1.0', 'measurement_kind': 'controlled_baseline',
                'event_id': str(uuid4()), 'character_id': 'miori',
                'session_id': trial['sessionId'], 'utterance_id': trial['utteranceId'],
                'response_id': trial['responseId'], 'name': name, 'stage': 'playback' if client else 'stt',
                'outcome': 'success', 'timestamp': 1500 if client else 1000000000 + index,
                'clock_domain': 'client_monotonic' if client else 'server_monotonic',
                'unit': 'millisecond' if client else 'nanosecond',
            })
    manifest = {'measurement_scope': 'pilot', 'expected_warmup': 1, 'expected_measured': 1,
                'fixture': fixture, 'initial_state_hash': 'requested-state', 'trials': trials}
    profile = {'effectiveProfile': 'integration-voice', 'derivedEnvironment': {'WHISPER_MODEL': 'medium', 'RAG_ENABLED': 'false'}}
    paths = {name: tmp_path / (name + '.json') for name in ('manifest', 'trace', 'output', 'profile')}
    paths['profile'].write_text(json.dumps(profile))
    def run(*, controlled=False, resource_rows=None):
        paths['manifest'].write_text(json.dumps(manifest))
        paths['trace'].write_text('\n'.join(json.dumps(event) for event in events))
        resource_path = tmp_path / "resources.jsonl"
        if resource_rows is not None:
            resource_path.write_text('\n'.join(json.dumps(row) for row in resource_rows))
        finalize = finalize_livekit_controlled if controlled else finalize_livekit_pilot
        finalize(manifest_path=paths['manifest'], trace_path=paths['trace'],
                              output_path=paths['output'], profile_report_path=paths['profile'],
                              schema_path=root / 'docs/schemas/voice-quality-artifact-v1.schema.json',
                              run_id='unit-pilot', resource_observations_path=resource_path if resource_rows is not None else None)
        return json.loads(paths['output'].read_text())
    return manifest, events, run


def test_pilot_filters_warmup_and_exports_anonymous_valid_artifact(pilot_inputs):
    manifest, _, run = pilot_inputs
    artifact = run()
    assert artifact['run_counts']['measured'] == 1
    metrics = {item['name']: item for item in artifact['metrics']}
    assert metrics['ttfa']['p95'] == 460
    assert metrics['vad_trailing_boundary']['missing_outcomes'] == {'metric_boundary_clock_mismatch': 1}
    for trial in manifest['trials']:
        assert trial['sessionId'] not in json.dumps(artifact)


@pytest.mark.parametrize('invalid', ['cohort', 'identity', 'failure', 'fixture', 'clock', 'playback'])
def test_pilot_rejects_invalid_evidence(pilot_inputs, invalid):
    manifest, events, run = pilot_inputs
    if invalid == 'cohort':
        manifest['expected_measured'] = 100
    elif invalid == 'identity':
        manifest['trials'][1]['sessionId'] = manifest['trials'][0]['sessionId']
    elif invalid == 'failure':
        manifest['trials'][1]['outcome'] = 'failure'
    elif invalid == 'fixture':
        manifest['fixture']['audio_sha256'] = 'different'
    elif invalid == 'clock':
        events[-1]['clock_domain'] = 'server_monotonic'
    else:
        events[-1]['timestamp'] += 5
    with pytest.raises(ValueError):
        run()


@pytest.fixture
def controlled_inputs(pilot_inputs):
    import copy
    import hashlib
    from app.memory.persistence.schema import PERSONA_MEMORY_TABLES

    manifest, events, run = pilot_inputs
    evidence = {
        'method': 'sqlite_empty_state_and_configuration_v1', 'rag_enabled': False,
        'row_counts': {name: 0 for name in (*PERSONA_MEMORY_TABLES, 'conversation_turns', 'screen_turn_provenance')},
        'schema_versions': {'history': 1, 'memory': 2},
        'configuration_sha256': {'characters/miori/miori.card.json': 'a' * 64,
                                 'backend/app/memory/memory_policy.json': 'b' * 64},
    }
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    trial_template = copy.deepcopy(manifest['trials'][1])
    event_template = copy.deepcopy(events[5:])
    manifest.update(measurement_scope='controlled', expected_warmup=5, expected_measured=100,
                    initial_state_hash=digest, trials=[])
    events.clear()
    for index in range(105):
        trial = copy.deepcopy(trial_template)
        trial.update({name: str(uuid4()) for name in ('sessionId', 'utteranceId', 'responseId', 'conversationId')})
        trial.update(phase='warmup' if index < 5 else 'measured', initial_state_hash=digest,
                     initial_state_evidence=copy.deepcopy(evidence), first_playback_method='audio_worklet_output_timestamp',
                     session_end_confirmed=True,
                     fixture_clock_method='audio_worklet_pcm_causal_bounds',
                     fixture_clock_maximum_uncertainty_ms=20,
                     fixture_clock_bounds={
                         'sourceStart': {'lowerMs': 100, 'upperMs': 102, 'sourceSample': 0},
                         'speechStart': {'lowerMs': 120, 'upperMs': 122, 'sourceSample': manifest['fixture']['speech_start_sample']},
                         'speechEnd': {'lowerMs': 1040, 'upperMs': 1042, 'sourceSample': manifest['fixture']['speech_end_sample']},
                     },
                     media_observation_method='response_track_stateful_opus_worklet_output',
                     trackReceivedAt=50, audioReceivedAt=1400, audioDecodeAt=1410)
        packet_evidence = packet_playback_trial()
        packet_evidence['startedAt'] = 1500
        packet_evidence['track_media_observation'].update(trackReceivedAtMs=50,
            firstPacketReceivedAtMs=1400, firstPacketDecodedAtMs=1410)
        packet_evidence['packet_playback_observation'].update(receivedAtMs=1400, decodedAtMs=1410,
            firstOutputAtMs=1500, outputClockPerformanceTime=1510, confirmationObservedAtMs=1520)
        trial.update(packet_evidence)
        manifest['trials'].append(trial)
        for original in event_template:
            event = {**original, 'event_id': str(uuid4()), 'session_id': trial['sessionId'],
                     'utterance_id': trial['utteranceId'], 'response_id': trial['responseId']}
            events.append(event)
        for name, timestamp in (('client_track_received', 50), ('client_audio_received', 1400), ('client_audio_decoded', 1410)):
            events.append({**event, 'event_id': str(uuid4()), 'name': name, 'stage': 'transport',
                           'timestamp': timestamp, 'clock_domain': 'client_monotonic', 'unit': 'millisecond'})
    return manifest, events, run


def test_controlled_report_verifies_all_trials_and_excludes_five_warmups(controlled_inputs):
    _, _, run = controlled_inputs
    result = run(controlled=True)
    assert result['run_counts'] == {'warmup': 5, 'measured': 100, 'success': 100, 'failure': 0, 'excluded': 0}
    ttfa = next(item for item in result['metrics'] if item['name'] == 'ttfa')
    assert ttfa['success_count'] == ttfa['trial_count'] == 100
    assert ttfa['p95'] == 460


@pytest.mark.parametrize('invalid', ['missing_state', 'changed_state', 'nonempty_state', 'unknown_success', 'playback_method', 'cleanup', 'wrong_warmup'])
def test_controlled_report_rejects_incomplete_or_inconsistent_evidence(controlled_inputs, invalid):
    manifest, _, run = controlled_inputs
    trial = manifest['trials'][5]
    if invalid == 'missing_state':
        del trial['initial_state_evidence']
    elif invalid == 'changed_state':
        trial['initial_state_evidence']['configuration_sha256']['characters/miori/miori.card.json'] = 'c' * 64
    elif invalid == 'nonempty_state':
        trial['initial_state_evidence']['row_counts']['conversation_turns'] = 1
    elif invalid == 'unknown_success':
        del trial['outcome']
    elif invalid == 'playback_method':
        trial['first_playback_method'] = 'callback_arrival'
    elif invalid == 'cleanup':
        trial['session_end_confirmed'] = False
    else:
        manifest['expected_warmup'] = 1
    with pytest.raises(ValueError):
        run(controlled=True)


@pytest.mark.parametrize('invalid', ['missing_point', 'timestamp_mismatch', 'mixed_clock', 'reversed_order', 'wrong_method'])
def test_controlled_media_evidence_requires_real_matching_boundaries(controlled_inputs, invalid):
    manifest, events, run = controlled_inputs
    trial = manifest['trials'][5]
    event = next(e for e in events if e['session_id'] == trial['sessionId'] and e['name'] == 'client_audio_decoded')
    if invalid == 'missing_point':
        events.remove(event)
    elif invalid == 'timestamp_mismatch':
        trial['audioDecodeAt'] = 1440
    elif invalid == 'mixed_clock':
        event['clock_domain'] = 'server_monotonic'
    elif invalid == 'reversed_order':
        trial['audioDecodeAt'] = event['timestamp'] = 1600
    else:
        trial['media_observation_method'] = 'playback_callback_reused'
    with pytest.raises(ValueError):
        run(controlled=True)


def test_pilot_exports_observed_pcm_boundaries_without_reconstructing_wall_clock(pilot_inputs):
    manifest, events, run = pilot_inputs
    fixture = manifest['fixture']
    for trial in manifest['trials']:
        trial.update(
            fixture_clock_method='audio_worklet_pcm_causal_bounds',
            fixture_clock_maximum_uncertainty_ms=20,
            fixture_speech_end_client_ms=1100,
            fixture_clock_bounds={
                'sourceStart': {'lowerMs': 100, 'upperMs': 102, 'sourceSample': 0},
                'speechStart': {'lowerMs': 130, 'upperMs': 132, 'sourceSample': fixture['speech_start_sample']},
                'speechEnd': {'lowerMs': 1100, 'upperMs': 1102, 'sourceSample': fixture['speech_end_sample']},
            },
        )
    result = run()
    ttfa = next(item for item in result['metrics'] if item['name'] == 'ttfa')
    assert ttfa['p95'] == 400


@pytest.mark.parametrize('invalid', [
    'missing_method', 'legacy_method', 'old_media_method', 'missing_bounds', 'missing_end',
    'wrong_sample', 'bool_sample', 'negative', 'nonfinite', 'bool_time', 'reversed',
    'too_wide', 'relaxed_limit', 'negative_limit', 'missing_limit', 'out_of_order',
    'origin_mismatch', 'end_mismatch',
])
def test_controlled_rejects_unverified_or_invalid_pcm_clock(controlled_inputs, invalid):
    manifest, _, run = controlled_inputs
    trial = manifest['trials'][5]
    bounds = trial['fixture_clock_bounds']
    if invalid == 'missing_method':
        del trial['fixture_clock_method']
    elif invalid == 'legacy_method':
        trial['fixture_clock_method'] = 'get_user_media_completion_unverified'
    elif invalid == 'old_media_method':
        trial['media_observation_method'] = 'rtc_encoded_transform_and_decoded_track_first_response'
    elif invalid == 'missing_bounds':
        del trial['fixture_clock_bounds']
    elif invalid == 'missing_end':
        del bounds['speechEnd']
    elif invalid == 'wrong_sample':
        bounds['speechEnd']['sourceSample'] += 1
    elif invalid == 'bool_sample':
        bounds['sourceStart']['sourceSample'] = False
    elif invalid == 'negative':
        bounds['sourceStart']['lowerMs'] = -1
    elif invalid == 'nonfinite':
        bounds['speechEnd']['upperMs'] = float('nan')
    elif invalid == 'bool_time':
        bounds['sourceStart']['lowerMs'] = True
    elif invalid == 'reversed':
        bounds['speechEnd']['upperMs'] = 1039
    elif invalid == 'too_wide':
        bounds['speechEnd']['upperMs'] = 1061
    elif invalid == 'relaxed_limit':
        trial['fixture_clock_maximum_uncertainty_ms'] = 21
    elif invalid == 'negative_limit':
        trial['fixture_clock_maximum_uncertainty_ms'] = -1
    elif invalid == 'missing_limit':
        del trial['fixture_clock_maximum_uncertainty_ms']
    elif invalid == 'out_of_order':
        bounds['speechStart'].update(lowerMs=80, upperMs=82)
    elif invalid == 'origin_mismatch':
        trial['fixtureStartedAt'] = 101
    else:
        trial['fixture_speech_end_client_ms'] = 1041
    with pytest.raises(ValueError):
        run(controlled=True)


def packet_playback_trial():
    return {
        "track_response_matches": True, "startedAt": 1100,
        "track_media_observation": {"firstPacketReceivedAtMs": 998, "firstPacketDecodedAtMs": 1000,
                                    "firstPacketDecodedSamples": 960},
        "packet_playback_observation": {
            "packetIndex": 0, "receivedAtMs": 998, "decodedAtMs": 1000,
            "firstOutputFrame": 48000, "firstOutputEndFrame": 48128,
            "firstOutputAtMs": 1100, "outputClockContextTime": 1.01,
            "outputClockPerformanceTime": 1110, "confirmationObservedAtMs": 1120,
            "sampleRate": 48000, "outputClockPassed": True, "sourcePcmOffsetVerified": False,
        },
    }


def test_packet_output_clock_can_be_revalidated_without_pcm():
    from app.livekit_pilot_report import validate_packet_playback_observation
    assert validate_packet_playback_observation(packet_playback_trial()) == (2, 100)


@pytest.mark.parametrize("damage", ["future", "unpassed", "mapping", "different_packet", "response", "source_claim", "nonfinite", "bool", "cycle"])
def test_packet_playback_rejects_false_evidence(damage):
    from app.livekit_pilot_report import validate_packet_playback_observation
    trial = packet_playback_trial()
    evidence = trial["packet_playback_observation"]
    if damage == "future": evidence["confirmationObservedAtMs"] = 1099
    elif damage == "unpassed": evidence["outputClockContextTime"] = 1
    elif damage == "mapping": evidence["outputClockPerformanceTime"] += 1
    elif damage == "different_packet": trial["track_media_observation"]["firstPacketDecodedAtMs"] += 1
    elif damage == "response": trial["track_response_matches"] = False
    elif damage == "source_claim": evidence["sourcePcmOffsetVerified"] = True
    elif damage == "nonfinite": evidence["firstOutputFrame"] = float("nan")
    elif damage == "bool": evidence["packetIndex"] = False
    elif damage == "cycle": trial["startedAt"] += 1
    with pytest.raises(ValueError):
        validate_packet_playback_observation(trial)


@pytest.fixture
def completion_evidence():
    from app.voice_metrics import TraceEvent
    raw = dict(expectedSamples=2880, inputSamples=1900, paddingSamples=980,
               renderedSamples=2880, packetCount=3, firstOutputFrame=48000,
               lastOutputEndFrame=51008, gapSamples=128, maximumGapSamples=128,
               gapCount=1, firstRtpTimestamp=4294967000,
               lastRtpTimestamp=(4294967000 + 1920) % 2**32,
               outputClockContextTime=1.1, outputClockPerformanceTime=1200,
               confirmationObservedAtMs=1210, sampleRate=48000)
    points = {name: TraceEvent(
        schema_version='1.0', measurement_kind='controlled_baseline', event_id=name,
        character_id='fixture', session_id='session', utterance_id='utterance', response_id='response',
        name=name, stage='transport', outcome='success', timestamp=1,
        clock_domain='server_monotonic', unit='nanosecond', value=value,
    ) for name, value in [('response_audio_input_samples', 1900),
                          ('response_audio_captured_samples', 2880),
                          ('response_audio_padding_samples', 980)]}
    return {'playback_completion': raw, 'packet_playback_observation': {'firstOutputFrame': 48000}}, points


def test_completion_revalidates_source_counts_rtp_wrap_and_observed_gap(completion_evidence):
    from app.livekit_pilot_report import validate_playback_completion
    values = validate_playback_completion(*completion_evidence)
    assert values == {'playback_gap_total_ms': 128 / 48, 'playback_gap_maximum_ms': 128 / 48,
                      'playback_underrun_count': 1, 'playback_duration_ms': 3008 / 48}


@pytest.mark.parametrize('field,value', [
    ('expectedSamples', 3840), ('renderedSamples', 1920), ('inputSamples', 1901),
    ('lastOutputEndFrame', 50900), ('gapSamples', 0), ('gapCount', 0),
    ('maximumGapSamples', 64), ('lastRtpTimestamp', 1625),
    ('outputClockContextTime', 1.0), ('confirmationObservedAtMs', 1000),
    ('sampleRate', 24000), ('gapCount', True), ('outputClockPerformanceTime', float('nan')),
])
def test_completion_rejects_unproven_output_or_contradictions(completion_evidence, field, value):
    from app.livekit_pilot_report import validate_playback_completion
    trial, points = completion_evidence
    trial['playback_completion'][field] = value
    with pytest.raises(ValueError):
        validate_playback_completion(trial, points)


def test_completion_requires_matching_source_trace(completion_evidence):
    from app.livekit_pilot_report import validate_playback_completion
    trial, points = completion_evidence
    del points['response_audio_captured_samples']
    with pytest.raises(ValueError, match='source trace'):
        validate_playback_completion(trial, points)


def test_completion_aggregate_excludes_warmup_and_preserves_nonzero_gap(pilot_inputs, completion_evidence):
    import copy
    manifest, events, run = pilot_inputs
    trial_evidence, source_points = completion_evidence
    for index, trial in enumerate(manifest['trials']):
        trial.update(packet_playback_trial())
        raw = copy.deepcopy(trial_evidence['playback_completion'])
        if index == 0:
            raw.update(gapSamples=256, maximumGapSamples=256, lastOutputEndFrame=51136)
        trial['playback_completion'] = raw
        for point in source_points.values():
            events.append({**point.model_dump(mode='json'), 'event_id': str(uuid4()), 'character_id': 'miori',
                           'session_id': trial['sessionId'], 'utterance_id': trial['utteranceId'],
                           'response_id': trial['responseId']})
    for event in events:
        if event['name'] == 'first_playback':
            event['timestamp'] = 1100
    artifact = run()
    metrics = {item['name']: item for item in artifact['metrics']}
    for name in ('playback_continuity', 'playback_gap_total_ms', 'playback_gap_maximum_ms'):
        assert metrics[name]['trial_count'] == metrics[name]['success_count'] == 1
        assert metrics[name]['p95'] == pytest.approx(128 / 48)
    assert metrics['playback_underrun_count']['p95'] == 1
    for trial in manifest['trials']:
        assert trial['sessionId'] not in json.dumps(artifact)


def test_reconciled_render_clock_records_the_observed_anchor():
    from app.livekit_pilot_report import validate_packet_playback_observation
    trial = packet_playback_trial()
    trial['packet_playback_observation'].update(
        renderClockMethod='quantum_count_reconciled_with_global_frame',
        renderQuantumStartFrame=48000, renderClockConfirmationFrame=48256,
    )
    assert validate_packet_playback_observation(trial) == (2, 100)


@pytest.mark.parametrize('field,value', [
    ('renderClockMethod', 'guessed'), ('renderQuantumStartFrame', 48001),
    ('renderClockConfirmationFrame', 47999), ('renderClockConfirmationFrame', None),
    ('renderQuantumStartFrame', 47999.5),
])
def test_report_rejects_unreconciled_render_clock(field, value):
    from app.livekit_pilot_report import validate_packet_playback_observation
    trial = packet_playback_trial()
    trial['packet_playback_observation'].update(
        renderClockMethod='quantum_count_reconciled_with_global_frame',
        renderQuantumStartFrame=48000, renderClockConfirmationFrame=48256,
    )
    trial['packet_playback_observation'][field] = value
    with pytest.raises(ValueError):
        validate_packet_playback_observation(trial)


@pytest.mark.parametrize('damage', ['missing_packet', 'wrong_track', 'native_delivery_as_decode', 'old_method'])
def test_controlled_media_requires_the_packet_that_was_actually_played(controlled_inputs, damage):
    manifest, events, run = controlled_inputs
    trial = manifest['trials'][5]
    if damage == 'missing_packet':
        del trial['packet_playback_observation']
    elif damage == 'wrong_track':
        trial['track_media_observation']['trackReceivedAtMs'] += 10
    elif damage == 'native_delivery_as_decode':
        trial['audioDecodeAt'] += 20
        for event in events:
            if event['response_id'] == trial['responseId'] and event['name'] == 'client_audio_decoded':
                event['timestamp'] = trial['audioDecodeAt']
    else:
        trial['media_observation_method'] = 'rtc_encoded_transform_and_rtp_track_delivery'
    with pytest.raises(ValueError):
        run(controlled=True)


def test_pilot_includes_scoped_resources_and_excludes_warmup_network(pilot_inputs):
    manifest, _, run = pilot_inputs
    for index, trial in enumerate(manifest["trials"]):
        trial["network_observation"] = {"method": "browser_audio_rtp_counters_v1",
            "uplink": {"status": "measured", "bytes": 900 if index == 0 else 100, "packets": 10},
            "downlink": {"status": "measured", "bytes": 900 if index == 0 else 200, "packets": 20, "lostPackets": 0}}
    rows = [{"backend": {"status": "measured", "scope": "owned_backend_container", "started_ns": at-1,
                         "completed_ns": at, "cpu_total_ns": cpu, "memory_bytes": 100},
             "gpu": {"outcome": "observed", "scope": "host_gpu", "devices": [{"utilization_percent": 50, "used_bytes": 200, "total_bytes": 500}]}}
            for at, cpu in [(1_000_000_000, 100_000_000), (2_000_000_000, 300_000_000)]]
    result = run(resource_rows=rows)
    assert result["resources"]["cpu_percent"]["value"] == 20
    assert result["resources"]["memory_bytes"]["value"] == 100
    assert result["resources"]["gpu_memory_bytes"]["value"] == 200
    assert result["network"]["sent_bytes"]["value"] == 100
    assert result["network"]["received_bytes"]["value"] == 200
    assert result["network"]["packet_loss_basis_points"]["value"] == 0
    assert result["network"]["collection"]["trial_count"] == 1
