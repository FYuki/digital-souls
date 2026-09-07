from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

_PATH = Path(__file__).resolve().parents[3] / "scripts/voice_quality/run_pilot.py"
_SPEC = importlib.util.spec_from_file_location("voice_quality_pilot", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
pilot = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = pilot
_SPEC.loader.exec_module(pilot)


def test_residency_omits_model_names_and_arbitrary_provider_data():
    body = {"models": [
        {"name": "test-model", "context_length": 8192, "size": 100, "size_vram": 90,
         "digest": "private-digest", "prompt": "private-prompt"},
        {"name": "another-private-model", "context_length": 13312},
    ], "secret": "private-value"}
    result = pilot.residency_values(body, "test-model")
    assert result["resident_model_count"] == 2
    assert result["target_context_tokens"] == [8192]
    assert result["target_vram_bytes"] == [90]
    serialized = json.dumps(result)
    assert "private" not in serialized and "test-model" not in serialized


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan"), "private-value"])
def test_invalid_provider_numbers_remain_missing(value):
    result = pilot.residency_values({"models": [{"name": "test", "context_length": value}]}, "test")
    assert result["target_context_tokens"] == [None]
    json.dumps(result, allow_nan=False)


def test_unloaded_model_is_distinct_from_probe_failure():
    assert pilot.residency_values({"models": []}, "test")["target_model_present"] is False
    assert pilot.residency_values({"error": "private"}, "test")["outcome"] == "missing"


@pytest.mark.parametrize("raw", ["N/A, 10, 20", "101, 10, 20", "10, 30, 20", "nan, 10, 20", ""])
def test_gpu_unavailable_or_invalid_does_not_become_zero_usage(raw):
    assert pilot.gpu_values(raw)["outcome"] == "missing"


def test_gpu_memory_units_and_scope_are_explicit():
    result = pilot.gpu_values("25, 10, 20\n50, 5, 15")
    assert result["scope"] == "host_gpu"
    assert result["devices"][0] == {"utilization_percent": 25, "used_bytes": 10 * 1024**2,
                                    "total_bytes": 20 * 1024**2}
    assert len(result["devices"]) == 2


@pytest.mark.parametrize("run_id", ["../other", "/tmp/other", "a/b", "", "x" * 65])
def test_run_id_cannot_escape_the_isolated_directory(run_id):
    with pytest.raises(ValueError):
        pilot.run_root(run_id)


def test_pilot_environment_keeps_existing_generation_options_and_isolates_state(tmp_path, monkeypatch):
    inference = tmp_path / "inference.env"
    inference.write_text('INFERENCE_TARGET_CHAT=ollama/test\n'
                         'INFERENCE_TARGET_CHAT_OPTIONS_JSON={"temperature":0.2}\n'
                         'DS_DATA_DIR=/private/dogfood\n'
                         'INFERENCE_TARGET_VISION=ollama/other\n')
    livekit = tmp_path / "livekit.env"
    livekit.write_text('LIVEKIT_KEYS="test-key: test-value"\n')
    monkeypatch.delenv("DS_DATA_DIR", raising=False)
    monkeypatch.setenv("VOICE_QUALITY_RUN_ID", "previous-run")
    monkeypatch.setenv("VOICE_QUALITY_CONTINUOUS_TURNS", "9")
    env = pilot.pilot_environment(inference, livekit, "fresh-run", 3, False)
    assert env["VOICE_QUALITY_CONTINUOUS_TURNS"] == "0"
    continuous = pilot.pilot_environment(inference, livekit, "fresh-run", 3, False, True, 3)
    assert continuous["VOICE_QUALITY_CONTINUOUS_TURNS"] == "3"
    with pytest.raises(ValueError):
        pilot.pilot_environment(inference, livekit, "fresh-run", 3, False, False, 3)
    assert "DS_DATA_DIR" not in env
    assert "INFERENCE_TARGET_VISION" not in env
    assert env["VOICE_QUALITY_RUN_ID"] == "fresh-run"
    assert env["VOICE_QUALITY_PILOT_TRIALS"] == "3"
    assert json.loads(env["INFERENCE_TARGET_CHAT_OPTIONS_JSON"]) == {"temperature": 0.2}
    changed = pilot.pilot_environment(inference, livekit, "fresh-run", 3, True)
    assert json.loads(changed["INFERENCE_TARGET_CHAT_OPTIONS_JSON"]) == {"temperature": 0.2, "think": False}


@pytest.mark.parametrize('count,scheduled,continuous,valid', [
    (100, True, 0, True), (99, True, 0, False), (100, False, 0, False), (100, True, 3, False),
])
def test_controlled_run_selects_exact_independent_scope(tmp_path, monkeypatch, count, scheduled, continuous, valid):
    inference = tmp_path / 'inference.env'
    inference.write_text('INFERENCE_TARGET_CHAT=ollama/test\n')
    livekit = tmp_path / 'livekit.env'
    livekit.write_text('LIVEKIT_KEYS="test-key: test-value"\n')
    monkeypatch.setenv('VOICE_QUALITY_PILOT_TRIALS', '3')
    if not valid:
        with pytest.raises(ValueError):
            pilot.pilot_environment(inference, livekit, 'controlled-test', count, False, scheduled, continuous, True)
        return
    env = pilot.pilot_environment(inference, livekit, 'controlled-test', count, False, scheduled, continuous, True)
    assert 'VOICE_QUALITY_PILOT_TRIALS' not in env
    assert env['VOICE_QUALITY_CONTINUOUS_TURNS'] == '0'
    assert env['VOICE_QUALITY_SCHEDULED_FIXTURE'] == '1'
    with pytest.raises(ValueError):
        pilot.pilot_environment(inference, livekit, 'not-a-pilot', 100, False, True)


@pytest.mark.parametrize('cohort,scheduled,continuous,controlled,count,valid', [
    ('take_turn', True, 0, False, 100, True),
    ('backchannel', True, 0, False, 4, True),
    ('take_turn', False, 0, False, 4, False),
    ('take_turn', True, 3, False, 4, False),
    ('take_turn', True, 0, True, 100, False),
    ('unknown', True, 0, False, 4, False),
])
def test_interruption_scope_cannot_mix_with_other_measurements(tmp_path, cohort, scheduled, continuous, controlled, count, valid):
    inference, livekit = tmp_path / 'inference.env', tmp_path / 'livekit.env'
    inference.write_text('INFERENCE_TARGET_CHAT=ollama/test\n')
    livekit.write_text('LIVEKIT_KEYS=test:test-only\n')
    if not valid:
        with pytest.raises(ValueError, match='interruption'):
            pilot.pilot_environment(inference, livekit, 'interrupt-test', count, False, scheduled, continuous, controlled, cohort)
    else:
        result = pilot.pilot_environment(inference, livekit, 'interrupt-test', count, False, scheduled, continuous, controlled, cohort)
        assert result['VOICE_QUALITY_INTERRUPTION_COHORT'] == cohort
        assert result['VOICE_QUALITY_PILOT_TRIALS'] == str(count)


@pytest.mark.parametrize('controlled,cohort,continuous,scheduled,count,valid', [
    (False, None, 0, True, 3, True), (True, None, 0, True, 100, False),
    (False, 'take_turn', 0, True, 3, False), (False, None, 3, True, 3, False),
    (False, None, 0, False, 3, False), (False, None, 0, True, 11, False),
])
def test_control_probe_scope_is_explicit_and_separate(tmp_path, monkeypatch, controlled, cohort, continuous, scheduled, count, valid):
    inference, livekit = tmp_path / 'inference.env', tmp_path / 'livekit.env'
    inference.write_text('INFERENCE_TARGET_CHAT=ollama/test\n')
    livekit.write_text('LIVEKIT_KEYS=test:test-only\n')
    monkeypatch.setenv('VOICE_QUALITY_CONTROL_PROBE', '1')
    if valid:
        env = pilot.pilot_environment(inference, livekit, 'probe-test', count, False,
                                      scheduled, continuous, controlled, cohort, True)
        assert env['VOICE_QUALITY_CONTROL_PROBE'] == '1'
        ordinary = pilot.pilot_environment(inference, livekit, 'ordinary-test', 3, False, True)
        assert 'VOICE_QUALITY_CONTROL_PROBE' not in ordinary
    else:
        with pytest.raises(ValueError, match='control probe'):
            pilot.pilot_environment(inference, livekit, 'probe-test', count, False,
                                    scheduled, continuous, controlled, cohort, True)


def test_fault_profile_and_token_endpoint_are_selected_together(tmp_path, monkeypatch):
    inference, livekit = tmp_path / 'inference.env', tmp_path / 'livekit.env'
    inference.write_text('INFERENCE_TARGET_CHAT=ollama/test\n')
    livekit.write_text('LIVEKIT_KEYS=test:test-only\n')
    monkeypatch.setenv('VOICE_QUALITY_FAULT_BRIDGE', '1')
    monkeypatch.setenv('LIVEKIT_URL', 'ws://127.0.0.1:17880')
    arguments = (inference, livekit, 'fault-probe', 3, False, True)
    env = pilot.pilot_environment(*arguments, control_probe=True, fault_bridge=True)
    assert env['DS_PROFILE'] == 'integration-voice-fault'
    assert env['LIVEKIT_URL'] == 'ws://127.0.0.1:19880'
    assert env['VOICE_QUALITY_FAULT_BRIDGE'] == '1'
    ordinary = pilot.pilot_environment(*arguments)
    assert ordinary['DS_PROFILE'] == 'integration-voice'
    assert ordinary['LIVEKIT_URL'] == 'ws://127.0.0.1:7880'
    assert 'VOICE_QUALITY_FAULT_BRIDGE' not in ordinary
    with pytest.raises(ValueError, match='fault bridge'):
        pilot.pilot_environment(*arguments, fault_bridge=True)


def test_network_fault_requires_explicit_bridge_and_clears_inherited_flag(tmp_path, monkeypatch):
    inference = tmp_path / 'inference.env'
    inference.write_text('INFERENCE_TARGET_CHAT=ollama/test\n')
    livekit = tmp_path / 'livekit.env'
    livekit.write_text('LIVEKIT_KEYS="test-key: test-value"\n')
    args = (inference, livekit, 'fault-test', 3, False)
    monkeypatch.setenv('VOICE_QUALITY_NETWORK_FAULT', '1')
    assert 'VOICE_QUALITY_NETWORK_FAULT' not in pilot.pilot_environment(*args)
    with pytest.raises(ValueError, match='dedicated bridge'):
        pilot.pilot_environment(*args, network_fault=True)
    env = pilot.pilot_environment(*args, scheduled_fixture=True, control_probe=True,
                                  fault_bridge=True, network_fault=True)
    assert env['VOICE_QUALITY_NETWORK_FAULT'] == '1'
    assert env['DS_PROFILE'] == 'integration-voice-fault'


@pytest.mark.parametrize('state', ['clean', 'modified', 'untracked', 'symlink', 'directory'])
def test_measurement_revision_requires_committed_sources(tmp_path, monkeypatch, state):
    from types import SimpleNamespace
    calls = []
    if state in ('symlink', 'directory'):
        (tmp_path / 'backend').mkdir()
        if state == 'symlink':
            (tmp_path / 'backend/.venv').symlink_to('/tmp/nonexistent-test-runtime')
        else:
            (tmp_path / 'backend/.venv').mkdir()
    status = {'clean': '', 'modified': ' M frontend/src/App.svelte\n',
              'untracked': '?? backend/app/new_runtime.py\n',
              'symlink': '?? backend/.venv\n', 'directory': '?? backend/.venv\n'}[state]
    def run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs['cwd'] == tmp_path
        return SimpleNamespace(stdout=status if cmd[1] == 'status' else 'a' * 40 + '\n')
    monkeypatch.setattr(pilot.subprocess, 'run', run)
    if state in ('clean', 'symlink'):
        assert pilot.measurement_revision(tmp_path) == 'a' * 40
        assert len(calls) == 2
    else:
        with pytest.raises(ValueError, match='committed worktree'):
            pilot.measurement_revision(tmp_path)
        assert len(calls) == 1


@pytest.mark.parametrize('selection,count,cohort,controlled,valid', [
    ('51,56,72', 3, 'take_turn', False, True),
    ('100', 1, 'backchannel', False, True),
    ('51,51,72', 3, 'take_turn', False, False),
    ('51,56', 3, 'take_turn', False, False),
    ('0', 1, 'take_turn', False, False),
    ('101', 1, 'take_turn', False, False),
    ('01', 1, 'take_turn', False, False),
    ('1, 2', 2, 'take_turn', False, False),
    ('1', 1, None, False, False),
    (','.join(str(i) for i in range(1, 101)), 100, 'take_turn', False, False),
    ('1', 100, 'take_turn', True, False),
])
def test_fixture_selection_cannot_replace_the_formal_denominator(tmp_path, selection, count, cohort, controlled, valid):
    inference, livekit = tmp_path / 'inference.env', tmp_path / 'livekit.env'
    inference.write_text('INFERENCE_TARGET_CHAT=ollama/test\n')
    livekit.write_text('LIVEKIT_KEYS=test:test-only\n')
    arguments = (inference, livekit, 'selected-diagnostic', count, False)
    options = dict(scheduled_fixture=True, interruption_cohort=cohort,
                   controlled=controlled, fixture_indices=selection)
    if valid:
        result = pilot.pilot_environment(*arguments, **options)
        assert result['VOICE_QUALITY_FIXTURE_INDICES'] == selection
    else:
        with pytest.raises(ValueError, match='fixture selection'):
            pilot.pilot_environment(*arguments, **options)


def test_formal_cohort_does_not_inherit_diagnostic_fixture_selection(tmp_path, monkeypatch):
    inference, livekit = tmp_path / 'inference.env', tmp_path / 'livekit.env'
    inference.write_text('INFERENCE_TARGET_CHAT=ollama/test\n')
    livekit.write_text('LIVEKIT_KEYS=test:test-only\n')
    monkeypatch.setenv('VOICE_QUALITY_FIXTURE_INDICES', '51,56,72')
    result = pilot.pilot_environment(inference, livekit, 'complete-cohort', 100, False,
                                    scheduled_fixture=True, interruption_cohort='take_turn')
    assert 'VOICE_QUALITY_FIXTURE_INDICES' not in result
    assert result['VOICE_QUALITY_PILOT_TRIALS'] == '100'
