from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from app.livekit_trace_report import finalize_livekit_dogfood_report
from app.voice_dogfood_resources import DogfoodResourceObservations, load_dogfood_resources
from app.voice_metrics import TraceEvent


@pytest.fixture
def observations(tmp_path: Path):
    trace = tmp_path / 'trace.jsonl'
    event = TraceEvent(schema_version='1.0', measurement_kind='dogfood', event_id='event-private',
        character_id='miori', session_id='session-private', utterance_id='utterance-private',
        response_id='response-private', name='first_playback', stage='playback', outcome='success',
        timestamp=1000, clock_domain='client_monotonic', unit='millisecond')
    trace.write_text(event.model_dump_json() + '\n')
    body = {'schema_version': '1.0', 'measurement_kind': 'dogfood',
            'method': 'manual_docker_stats_and_host_gpu_v1',
            'trace_sha256': [hashlib.sha256(trace.read_bytes()).hexdigest()],
            'samples': [
                {'elapsed_ms': 0, 'cpu_percent': 150, 'memory_bytes': 100,
                 'gpu_utilization_percent': 20, 'gpu_memory_bytes': 200},
                {'elapsed_ms': 1000, 'cpu_percent': None, 'memory_bytes': 300,
                 'gpu_utilization_percent': None, 'gpu_memory_bytes': None},
                {'elapsed_ms': 3000, 'cpu_percent': 50, 'memory_bytes': 200,
                 'gpu_utilization_percent': 80, 'gpu_memory_bytes': 400},
            ]}
    return trace, body


def write_observations(tmp_path, body):
    path = tmp_path / 'observations.json'
    path.write_text(json.dumps(body))
    return path


def test_manual_resources_keep_scope_missing_counts_and_sampled_values(tmp_path, observations):
    trace, body = observations
    result = load_dogfood_resources(write_observations(tmp_path, body), trace_paths=[trace])
    assert result.cpu_percent.value == 100
    assert result.memory_bytes.value == 300
    assert result.gpu_utilization_percent.value == 80
    assert result.gpu_memory_bytes.value == 400
    assert result.collection.cpu_scope == 'backend_container'
    assert result.collection.sample_window_ms == 3000
    assert result.collection.maximum_interval_ms == 2000
    assert result.collection.measured_samples['cpu_percent'] == 2
    assert result.collection.missing_samples['cpu_percent'] == 1
    assert result.collection.missing_samples['memory_bytes'] == 0


def test_unobserved_gpu_is_missing_and_zero_cpu_is_measured(tmp_path, observations):
    trace, body = observations
    for row in body['samples']:
        row.update(cpu_percent=0, gpu_utilization_percent=None, gpu_memory_bytes=None)
    result = load_dogfood_resources(write_observations(tmp_path, body), trace_paths=[trace])
    assert result.cpu_percent.value == 0
    assert result.gpu_utilization_percent.value is None
    assert result.gpu_utilization_percent.status == 'missing'
    assert result.gpu_utilization_percent.reason == 'manual_dogfood_sample_not_recorded'
    assert result.collection.missing_samples['gpu_utilization_percent'] == 3


@pytest.mark.parametrize('change', [
    lambda b: b.update(measurement_kind='controlled_baseline'),
    lambda b: b.update(prompt='must-not-be-imported'),
    lambda b: b.update(trace_sha256=['a' * 64]),
    lambda b: b['trace_sha256'].append(b['trace_sha256'][0]),
    lambda b: b['samples'][1].update(elapsed_ms=0),
    lambda b: b['samples'][1].update(cpu_percent=-1),
    lambda b: b['samples'][1].update(cpu_percent=True),
    lambda b: b['samples'][1].update(cpu_percent=float('nan')),
    lambda b: b['samples'][1].update(gpu_utilization_percent=101),
    lambda b: b['samples'][1].update(memory_bytes=1.5),
    lambda b: b['samples'][1].pop('cpu_percent'),
    lambda b: b.update(samples=[]),
])
def test_inconsistent_private_or_invalid_manual_evidence_rejected(tmp_path, observations, change):
    trace, body = observations
    change(body)
    with pytest.raises(ValueError):
        load_dogfood_resources(write_observations(tmp_path, body), trace_paths=[trace])


def test_dogfood_report_imports_resource_metadata_and_remains_anonymous(tmp_path, observations):
    trace, body = observations
    path = write_observations(tmp_path, body)
    root = Path(__file__).resolve().parents[3]
    schema = root / 'docs/schemas/voice-quality-artifact-v1.schema.json'
    output = tmp_path / 'report.json'
    finalize_livekit_dogfood_report(trace_paths=[trace], output_path=output, schema_path=schema,
        run_id='manual-test', resource_observations_path=path)
    artifact = json.loads(output.read_text())
    Draft202012Validator(json.loads(schema.read_text())).validate(artifact)
    assert artifact['resources']['cpu_percent']['value'] == 100
    assert artifact['resources']['collection']['sample_count'] == 3
    assert artifact['network']['sent_bytes']['status'] == 'missing'
    assert all(value not in output.read_text() for value in ['miori', 'session-private', 'response-private', 'trace_sha256'])
    input_schema = json.loads((root / 'docs/schemas/voice-dogfood-resource-observations-v1.schema.json').read_text())
    Draft202012Validator(input_schema).validate(body)
    DogfoodResourceObservations.model_validate(body)
