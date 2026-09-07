from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / 'scripts/voice_quality/run_reconnect_cohort.py'
sys.path.insert(0, str(_SCRIPT.parent))
try:
    _SPEC = importlib.util.spec_from_file_location('voice_quality_reconnect_cohort', _SCRIPT)
    assert _SPEC and _SPEC.loader
    cohort = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(cohort)
finally:
    sys.path.pop(0)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(cohort, 'ROOT', tmp_path)
    monkeypatch.setattr(cohort, 'run_root', lambda run_id: tmp_path / 'runs' / run_id)
    monkeypatch.setattr(cohort, 'measurement_revision', lambda root: 'a' * 40)
    monkeypatch.setattr(cohort, 'resolve_target', lambda name: 'same-labelled-target')
    calls = []
    def run(run_id, args, log):
        calls.append(('run', run_id))
        return 1 if run_id.endswith('002') else 0
    def verify(run_id, revision):
        calls.append(('verify', run_id))
        return dict(session_end_confirmed=True, reported_success=not run_id.endswith('002'), owned_containers_deleted=True)
    def aggregate(runs, output):
        calls.append(('aggregate', runs))
        return 1
    monkeypatch.setattr(cohort, 'run_trial', run)
    monkeypatch.setattr(cohort, 'verify_trial', verify)
    monkeypatch.setattr(cohort, 'aggregate', aggregate)
    args = argparse.Namespace(cohort_id='cohort', sessions=3, disable_thinking=True,
                              inference_env=tmp_path/'inference.env', livekit_env=tmp_path/'keys.env')
    return args, calls, tmp_path/'frontend/test-results/livekit-quality/cohorts/cohort'


def test_sequential_cleanup_before_next_trial_and_failed_trial_remains_in_denominator(rig):
    args, calls, directory = rig
    assert cohort.execute(args) == 1
    assert calls == [('run', 'cohort-001'), ('verify', 'cohort-001'), ('run', 'cohort-002'), ('verify', 'cohort-002'),
                     ('run', 'cohort-003'), ('verify', 'cohort-003'), ('aggregate', ['cohort-001', 'cohort-002', 'cohort-003'])]
    plan = json.loads((directory/'plan.json').read_text())
    assert plan['expected_sessions'] == 3 and len(plan['run_ids']) == 3
    records = [json.loads(line) for line in (directory/'execution.jsonl').read_text().splitlines()]
    assert [r['exit_code'] for r in records if r['event']=='trial_completed'] == [0, 1, 0]
    assert records[-1] == dict(event='cohort_report_created', completed=3, passed=False)


@pytest.mark.parametrize('problem', ['cleanup', 'revision', 'stop'])
def test_does_not_start_next_environment_after_uncertain_cleanup_or_changed_revision(rig, monkeypatch, problem):
    args, calls, directory = rig
    def verify(run_id, revision):
        if problem == 'cleanup':
            raise ValueError('private service error')
        if problem == 'revision':
            monkeypatch.setattr(cohort, 'measurement_revision', lambda root: 'b'*40)
        if problem == 'stop':
            (directory/'stop-requested').touch()
        return dict(session_end_confirmed=True, reported_success=True, owned_containers_deleted=True)
    monkeypatch.setattr(cohort, 'verify_trial', verify)
    if problem == 'stop':
        assert cohort.execute(args) == 2
    else:
        with pytest.raises(ValueError):
            cohort.execute(args)
    assert calls == [('run', 'cohort-001')]
    records=(directory/'execution.jsonl').read_text()
    assert 'cohort_stopped' in records and 'private service error' not in records
    assert 'cohort_report_created' not in records


def test_existing_cohort_or_planned_run_cannot_be_overwritten(rig, monkeypatch):
    args, calls, directory = rig
    existing=cohort.run_root('cohort-002'); existing.mkdir(parents=True)
    with pytest.raises(ValueError, match='already exists'):
        cohort.execute(args)
    assert not calls and not directory.exists()


@pytest.mark.parametrize('identifier,count', [('unsafe/path',100), ('x'*61,100), ('ok',0), ('ok',101), ('ok',True)])
def test_plan_rejects_path_escape_or_invalid_denominator(identifier, count):
    with pytest.raises(ValueError):
        cohort.planned_runs(identifier, count)


@pytest.mark.parametrize('problem', ['none','live_container','permission_error','wrong_root','wrong_profile','wrong_owner','open_child'])
def test_verifier_requires_actual_deletion_and_exclusive_test_ownership(tmp_path, monkeypatch, problem):
    base=tmp_path/'test-001'; runtime=base/'runtime-data/runtime/standalone'; runtime.mkdir(parents=True)
    monkeypatch.setattr(cohort,'run_root',lambda run_id:base)
    manifest=dict(measurement_scope='livekit_fault_recovery_session_diagnostic', measurement_revision='a'*40,
                  fault_clock_process_closed=problem!='open_child', session_end_confirmed=False, outcome='failure')
    (base/'trial-manifest.json').write_text(json.dumps(manifest))
    env=dict(runtime=dict(environmentId='test', dataRoot=str(base/'runtime-data') if problem!='wrong_root' else '/other'),
             effectiveProfile=dict(effectiveProfile='integration-voice-fault' if problem!='wrong_profile' else 'dogfood'),
             teardown=dict(status='completed'), services={name:dict(owned=problem!='wrong_owner',
                containerIdentity=dict(containerId=('a' if name=='frontend' else 'b')*64)) for name in ['frontend','backend']})
    (runtime/'environment-run.json').write_text(json.dumps(env))
    monkeypatch.setattr(cohort.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(
        returncode=0 if problem=='live_container' else 1, stderr='permission denied' if problem=='permission_error' else 'Error: No such object'))
    if problem=='none':
        assert cohort.verify_trial('test-001','a'*40) == dict(session_end_confirmed=False,reported_success=False,owned_containers_deleted=True)
    else:
        with pytest.raises(ValueError):
            cohort.verify_trial('test-001','a'*40)


def test_timeout_signals_only_the_owned_child_process_group(tmp_path, monkeypatch):
    signals, launches, waits = [], [], []
    class Child:
        pid = 45678
        def wait(self, timeout):
            waits.append(timeout)
            if timeout == 300:
                raise cohort.subprocess.TimeoutExpired('owned pilot', timeout)
            return 130
    def launch(command, **options):
        launches.append((command, options))
        return Child()
    monkeypatch.setattr(cohort.subprocess, 'Popen', launch)
    monkeypatch.setattr(cohort.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    args = argparse.Namespace(inference_env=tmp_path/'inference.env', livekit_env=tmp_path/'keys.env', disable_thinking=True)
    with pytest.raises(cohort.subprocess.TimeoutExpired):
        cohort.run_trial('test-001', args, tmp_path/'child.log')
    assert signals == [(45678, cohort.signal.SIGINT)]
    assert waits == [300, 45]
    assert launches[0][1]['start_new_session'] is True
    assert launches[0][0][-1] == '--disable-thinking'
    assert launches[0][0][launches[0][0].index('--trials') + 1] == '3'


def test_concurrent_cohort_is_rejected_before_any_trial_starts(rig):
    args, calls, directory = rig
    directory.parent.mkdir(parents=True)
    with (directory.parent/'.execution.lock').open('a+') as lock:
        cohort.fcntl.flock(lock, cohort.fcntl.LOCK_EX | cohort.fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            cohort.execute(args)
    assert not calls and not directory.exists()
