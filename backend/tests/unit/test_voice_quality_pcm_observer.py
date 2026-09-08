from __future__ import annotations

import json
from pathlib import Path
import sys
import wave

import httpx
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/voice_quality'))
try:
    from whisper_pcm_observer import WhisperPcmObserver
finally:
    sys.path.remove(str(ROOT / 'scripts/voice_quality'))


def selection():
    metadata = json.loads((ROOT / 'frontend/playwright/fixtures/speech.metadata.json').read_text())
    return {'fixture_sha256': metadata['audio_sha256'], 'trial_ordinal': 1, 'phase': 'initial'}


def fixture_pcm():
    with wave.open(str(ROOT / 'frontend/playwright/fixtures/speech.wav')) as source:
        return np.frombuffer(source.readframes(source.getnframes()), dtype='<i2')[::3].tobytes()


def test_http_forwards_actual_bytes_and_response_without_storing_either(tmp_path):
    original = fixture_pcm()
    sent = []
    private_reply = b'{"text":"private response text"}'
    def upstream(request):
        sent.append(request.content)
        return httpx.Response(200, content=private_reply, headers={'Content-Type': 'application/json'})
    output = tmp_path / 'observations.jsonl'
    with WhisperPcmObserver(output, port=0, transport=httpx.MockTransport(upstream)) as observer:
        base = 'http://127.0.0.1:' + str(observer.server.server_port)
        with httpx.Client(base_url=base, trust_env=False) as client:
            assert client.post('/fixture', json=selection()).status_code == 200
            response = client.post('/v1/transcriptions', content=original)
            assert response.status_code == 200 and response.content == private_reply
            result = client.get('/observations').json()
            assert result['rows'][0]['alignment']['status'] == 'aligned'
            assert result['rows'][0]['input_sample_count'] == len(original) // 2
            assert result['active_requests'] == 0
    assert sent == [original]
    saved = output.read_text()
    assert 'private response' not in saved and 'reference"' not in saved
    assert len(saved) < 2500
    assert not observer.thread.is_alive()


def test_prepare_and_upstream_errors_are_not_successful_boundary_observations(tmp_path):
    observer = WhisperPcmObserver(tmp_path / 'unused', transport=httpx.MockTransport(
        lambda _: httpx.Response(429, content=b'{"text":"private capacity body"}')))
    try:
        observer.select(selection())
        status, body, _ = observer.transcribe(bytes(3200))
        assert status == 429 and b'private capacity body' in body
        row = observer.snapshot()['rows'][0]
        assert row['reason'] == 'preparation_silence' and row['alignment'] is None
        assert row['upstream_status'] == 429
    finally:
        observer.close()


def test_missing_selection_and_network_failure_preserve_reason(tmp_path):
    def unavailable(_):
        raise httpx.ConnectError('private endpoint')
    observer = WhisperPcmObserver(tmp_path / 'unused', transport=httpx.MockTransport(unavailable))
    try:
        assert observer.transcribe(fixture_pcm())[0] == 502
        row = observer.snapshot()['rows'][0]
        assert row['trial_ordinal'] is None
        assert row['reason'] == 'upstream_request_failed'
        assert 'private endpoint' not in json.dumps(observer.snapshot())
    finally:
        observer.close()


def test_fixture_cannot_change_during_actual_request(tmp_path):
    observer = WhisperPcmObserver(tmp_path / 'unused')
    try:
        observer.select(selection())
        observer.active = 1
        with pytest.raises(ValueError, match='still active'):
            observer.select(selection())
    finally:
        observer.close()


@pytest.mark.parametrize('change', [{'trial_ordinal': 0}, {'phase': 'private'}, {'fixture_sha256': '../private'}])
def test_reference_is_fixed_catalog_not_arbitrary_path(tmp_path, change):
    observer = WhisperPcmObserver(tmp_path / 'unused')
    try:
        with pytest.raises(ValueError):
            observer.select({**selection(), **change})
    finally:
        observer.close()
