from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from uuid import UUID

import pytest
from jsonschema import ValidationError

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('voice_quality_stale_report', ROOT / 'scripts/voice_quality/report_stale_output.py')
reporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reporter
SPEC.loader.exec_module(reporter)
SCHEMA = json.loads((ROOT / 'docs/schemas/voice-quality-stale-report-v1.schema.json').read_text())


def cohort(n=3):
    fixtures = {'trials': [dict(cohort='take_turn', audio_sha256=sha256(str(i).encode()).hexdigest(),
        speech_intervals=[dict(start_sample=4800, end_sample=48000)]) for i in range(n)]}
    fb = json.dumps(fixtures).encode()
    trials, traces = [], []
    for i, f in enumerate(fixtures['trials']):
        sid, rid = str(UUID(int=i+1)), str(UUID(int=i+1001))
        common = dict(sessionId=sid, responseId=rid, generation=1)
        trials.append(dict(cohort='take_turn', fixture_sha256=f['audio_sha256'], outcome='success',
            session_id=sid, old_response_id=rid, session_end_confirmed=True,
            injection_playback={'active': True}, fixture_clock_bounds={
                'sourceStart': dict(sourceSample=0, lowerMs=0, upperMs=1),
                'speechStart': dict(sourceSample=4800, lowerMs=10, upperMs=11),
                'speechEnd': dict(sourceSample=48000, lowerMs=20, upperMs=21)},
            cleanup_observation=dict(server_clock=dict(closed=True, overflow=False, observations=[
                dict(status='received', generation=1, sentAtMs=100, receivedAtMs=110,
                    serverReceivedAtUs=5000, serverSentAtUs=5001),
                dict(status='received', generation=1, sentAtMs=120, receivedAtMs=130,
                    serverReceivedAtUs=5004, serverSentAtUs=5005)]),
                stale_audio=[{**common, 'graphClosed':True}], stale_audio_overflow=False,
                decoded_receipts=[{**common, 'beganAtMs':90}], decoded_receipts_overflow=False,
                stale_text=dict(closed=True, overflow=False, rows=[{**common,'cancelledAtMs':135}]))))
        for name, value in (('cancel_state_lower',5002100),('cancel_state_upper',5002200)):
            traces.append(dict(session_id=sid,response_id=rid,name=name,timestamp=value,
                clock_domain='server_monotonic',unit='nanosecond',character_id='private-character',measurement_kind='controlled_baseline'))
    return dict(measurement_scope='labeled_livekit_interruption_diagnostic',cohort='take_turn',
        measurement_revision='a'*40, expected_measured=n,labeled_manifest_sha256=sha256(fb).hexdigest(),trials=trials),fb,traces


def replay(prepared, lower=0, upper=0):
    counts = dict(itemsLower=0,itemsUpper=0,unitsLower=0,unitsUpper=0)
    return dict(bounds=prepared['bounds'], audio=dict(complete=True,missingReason=None,audit=dict(
        complete=True,drained=True,missingReason=None,nonzeroSamplesAfterCancelLower=lower,nonzeroSamplesAfterCancelUpper=upper)),
        received=dict(complete=True,missingReason=None,counts=counts),
        text=dict(complete=True,missingReason=None,received=counts,presented=counts))


def provenance(result):
    result['provenance']={key:('a'*40 if key=='measurement_revision' else 'b'*64)
        for key in SCHEMA['properties']['provenance']['required']}
    return result


def test_definite_possible_and_zero_are_distinct_and_unimplemented_not_complete():
    m,fb,traces=cohort()
    values=iter([(0,1537),(254,2305),(0,1025)])
    result=reporter.summarize(m,fb,traces,lambda p:replay(p,*next(values)))
    audio=result['channels']['audio_presented_samples']
    assert audio==dict(unit='sample',observed=3,missing=0,missing_reasons={},definitely_stale=1,
        possibly_stale=3,verified_zero=0,lower_total=254,upper_total=4867)
    assert result['channels']['live_text_presented_characters']['verified_zero']==3
    assert result['channels']['history_text_presented_characters']['missing']==3
    assert result['evaluation']['stale_presented_passed'] is False
    reporter.validate_report(provenance(result),SCHEMA)
    serialized=json.dumps(result)
    assert 'private-character' not in serialized
    assert all(t['session_id'] not in serialized and t['old_response_id'] not in serialized for t in m['trials'])


def test_failed_and_missing_trials_remain_in_all_denominators():
    m,fb,traces=cohort()
    m['trials'][1]['outcome']='failure'
    m['trials'][1]['session_end_confirmed']=False
    m['trials'][2]['cleanup_observation']['server_clock']['observations']=[]
    result=reporter.summarize(m,fb,traces,replay)
    assert result['counts']['success']==2 and result['counts']['failure']==1
    for row in result['channels'].values():
        assert row['observed']+row['missing']==3
    assert result['channels']['audio_presented_samples']['missing_reasons']==dict(session_end_unconfirmed=1,clock_window_unobserved=1)
    reporter.validate_report(provenance(result),SCHEMA)


@pytest.mark.parametrize('mutation', ['short','duplicate_session','duplicate_response','order','hash','targeted','mixed_trace'])
def test_mismatched_or_partial_cohort_is_rejected(mutation):
    m,fb,traces=cohort()
    if mutation=='short':m['trials'].pop()
    if mutation=='duplicate_session':m['trials'][1]['session_id']=m['trials'][0]['session_id']
    if mutation=='duplicate_response':m['trials'][1]['old_response_id']=m['trials'][0]['old_response_id']
    if mutation=='order':m['trials'].reverse()
    if mutation=='hash':m['labeled_manifest_sha256']='b'*64
    if mutation=='targeted':m['fixture_indices']=[1,2,3]
    if mutation=='mixed_trace':traces[0]['measurement_kind']='dogfood'
    with pytest.raises(ValueError):reporter.summarize(m,fb,traces,replay)


def test_last_cleanup_is_used_but_multiple_generations_or_graphs_are_not_overwritten():
    m,fb,traces=cohort(1)
    t=m['trials'][0]
    t['evidence']={'stale_audio':'must-not-use-earlier-snapshot'}
    prepared,reason=reporter.prepare_trial(t,traces)
    assert reason is None and prepared['output']['graphClosed'] is True
    assert prepared['bounds']=={'lowerMs':99.8,'upperMs':130.2}
    c=t['cleanup_observation']
    c['decoded_receipts'].insert(0,{**c['decoded_receipts'][0],'beganAtMs':80})
    assert reporter.prepare_trial(t,traces)[1]=='observation_identity_unverified'
    c['decoded_receipts'].pop(0)
    c['stale_audio'][0]['generation']=2
    assert reporter.prepare_trial(t,traces)[1]=='observation_identity_unverified'


def test_trace_conflict_and_clock_units_cannot_be_hidden_as_zero():
    m,fb,traces=cohort(1)
    traces.append({**traces[0],'timestamp':5002101})
    with pytest.raises(ValueError,match='conflicting_cancel_trace'):reporter.summarize(m,fb,traces,replay)
    traces.pop();traces[0]['unit']='millisecond'
    with pytest.raises(ValueError,match='invalid_cancel_trace'):reporter.summarize(m,fb,traces,replay)


def test_replay_rejection_is_missing_and_bounds_substitution_is_rejected():
    m,fb,traces=cohort(1)
    def failed(_):raise ValueError('private-sentinel')
    result=reporter.summarize(m,fb,traces,failed)
    assert result['channels']['audio_presented_samples']['missing_reasons']=={'replay_invalid':1}
    assert 'private-sentinel' not in json.dumps(result)
    def substituted(p):return {**replay(p),'bounds':{'lowerMs':130,'upperMs':130.2}}
    with pytest.raises(ValueError,match='replay_boundary_changed'):reporter.summarize(m,fb,traces,substituted)


def test_schema_rejects_ids_text_timestamps_and_false_pass_even_when_other_counts_are_valid():
    m,fb,traces=cohort(100)
    result=provenance(reporter.summarize(m,fb,traces,replay))
    assert result['evaluation']['cohort_coverage_complete'] is True
    assert result['evaluation']['stale_presented_passed'] is False
    for key in ('session_id','text','bounds'):
        invalid=deepcopy(result);invalid[key]='private-sentinel'
        with pytest.raises(ValidationError):reporter.validate_report(invalid,SCHEMA)
    invalid=deepcopy(result);invalid['evaluation']['stale_presented_passed']=True
    with pytest.raises(ValueError,match='report_evaluation_mismatch'):reporter.validate_report(invalid,SCHEMA)
    invalid=deepcopy(result);invalid['channels']['audio_presented_samples']['missing']=1
    with pytest.raises(ValueError,match='report_channel_mismatch'):reporter.validate_report(invalid,SCHEMA)


def test_cli_saves_missing_report_without_leaking_arbitrary_process_errors_or_overwriting(tmp_path,capsys):
    m,fb,traces=cohort(1)
    # 合成inputには音声archiveがないため実Node replayが拒否する。その試行も欠測として残す。
    (tmp_path/'manifest.json').write_text(json.dumps(m))
    (tmp_path/'trace.jsonl').write_text('\n'.join(json.dumps(r) for r in traces))
    (tmp_path/'fixtures.json').write_bytes(fb)
    argv=[item for key,name in (('manifest','manifest.json'),('trace','trace.jsonl'),('fixtures','fixtures.json'),('output','report.json'))
          for item in ('--'+key,str(tmp_path/name))]
    assert reporter.main(argv)==1
    out=(tmp_path/'report.json').read_bytes()
    assert json.loads(out)['channels']['audio_presented_samples']['missing_reasons']=={'replay_invalid':1}
    assert reporter.main(argv)==2
    assert (tmp_path/'report.json').read_bytes()==out
    (tmp_path/'report.json').unlink()
    (tmp_path/'manifest.json').write_text('{"private-sentinel":')
    assert reporter.main(argv)==2
    captured=capsys.readouterr()
    assert 'private-sentinel' not in captured.out+captured.err
    assert str(tmp_path) not in captured.out+captured.err


def test_startup_failure_without_trace_remains_missing_and_has_no_fabricated_trace_hash(tmp_path):
    m,fb,_=cohort(1)
    t=m['trials'][0]
    t['outcome']='failure'
    t['injection_playback']['active']=False
    t['session_end_confirmed']=False
    (tmp_path/'manifest.json').write_text(json.dumps(m))
    (tmp_path/'fixtures.json').write_bytes(fb)
    argv=[item for key,name in (('manifest','manifest.json'),('trace','not-created.jsonl'),('fixtures','fixtures.json'),('output','report.json'))
          for item in ('--'+key,str(tmp_path/name))]
    assert reporter.main(argv)==1
    result=json.loads((tmp_path/'report.json').read_bytes())
    assert result['counts']['failure']==1
    assert result['provenance']['raw_trace_sha256'] is None
    assert result['channels']['audio_presented_samples']['observed']==0
    assert result['channels']['audio_presented_samples']['missing_reasons']=={'fixture_injection_unverified':1}
    assert result['evaluation']['stale_presented_passed'] is False
