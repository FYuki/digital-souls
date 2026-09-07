"""全take-turn試行をサーバーcancel区間へ相関し、匿名の上下限と欠測分母を保存する。"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from uuid import UUID

from jsonschema import Draft202012Validator

# 直接実行とpytestのimportの双方で同じ境界検証器を使う。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_take_turn import bounds_valid
from server_clock import bound_transition

ROOT = Path(__file__).resolve().parents[2]
CHANNELS = {
    'audio_presented_samples': 'sample', 'audio_received_packets': 'packet',
    'audio_received_samples': 'sample', 'text_received_characters': 'utf16_code_unit',
    'live_text_presented_characters': 'utf16_code_unit',
    'history_text_presented_characters': 'utf16_code_unit',
    'server_generated_text_characters': 'utf16_code_unit', 'server_generated_audio_samples': 'sample',
}
REASONS = ('identity_missing', 'fixture_injection_unverified', 'session_end_unconfirmed',
           'cancel_state_unobserved', 'clock_window_unobserved', 'cleanup_unobserved',
           'observation_identity_unverified', 'output_window_unobserved',
           'receipt_window_unobserved', 'text_window_unobserved', 'replay_invalid',
           'history_observer_unimplemented', 'server_generation_observer_unimplemented')


def count(value):
    if type(value) is not int or not 0 <= value <= 9007199254740991:
        raise ValueError('invalid_count')
    return value


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid_timestamp')
    return value


def identity(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('invalid_identity')
    return value


def prepare_trial(trial, traces):
    """欠測は固定理由で残す。生IDと本文はreplay入力だけにとどめる。"""
    if not trial.get('session_id') or not trial.get('old_response_id'):
        return None, 'identity_missing'
    sid, rid = identity(trial['session_id']), identity(trial['old_response_id'])
    if trial.get('session_end_confirmed') is not True:
        return None, 'session_end_unconfirmed'
    cleanup = trial.get('cleanup_observation')
    if not isinstance(cleanup, dict):
        return None, 'cleanup_unobserved'
    clock = cleanup.get('server_clock', {})
    if clock.get('closed') is not True or clock.get('overflow') is not False:
        return None, 'clock_window_unobserved'
    matches = [r for r in traces if r.get('session_id') == sid and r.get('response_id') == rid]
    times = []
    for name in ('cancel_state_lower', 'cancel_state_upper'):
        rows = [r for r in matches if r.get('name') == name]
        if not rows:
            return None, 'cancel_state_unobserved'
        if any(r.get('clock_domain') != 'server_monotonic' or r.get('unit') != 'nanosecond'
               or type(r.get('timestamp')) is not int or r['timestamp'] < 0 for r in rows):
            raise ValueError('invalid_cancel_trace')
        values = {r['timestamp'] for r in rows}
        if len(values) != 1:
            raise ValueError('conflicting_cancel_trace')
        times.append(values.pop())
    observations = clock.get('observations', [])
    generations = {r.get('generation') for r in observations if r.get('status') == 'received'}
    if len(generations) != 1:
        return None, 'clock_window_unobserved'
    generation = count(generations.pop())
    bounds = bound_transition(observations, times[0], times[1], generation)
    if bounds['missing_reason'] is not None:
        return None, 'clock_window_unobserved'
    audio = [r for r in cleanup.get('stale_audio', []) if r.get('sessionId') == sid and r.get('responseId') == rid]
    receipts = [r for r in cleanup.get('decoded_receipts', []) if r.get('sessionId') == sid and r.get('responseId') == rid]
    text = cleanup.get('stale_text', {})
    text_rows = [r for r in text.get('rows', []) if r.get('sessionId') == sid and r.get('responseId') == rid]
    if not audio or not receipts or len(text_rows) != 1:
        return None, 'observation_identity_unverified'
    # 別graph/世代の履歴を最後の1行で置換しない。
    if (any(count(r.get('generation')) != generation for r in audio + receipts)
            or len({r.get('beganAtMs') for r in receipts}) != 1):
        return None, 'observation_identity_unverified'
    if cleanup.get('stale_audio_overflow') is not False or cleanup.get('decoded_receipts_overflow') is not False:
        return None, 'cleanup_unobserved'
    text_row = text_rows[0]
    if text_row.get('cancelledAtMs') is None:
        return None, 'cancel_state_unobserved'
    upper = min(bounds['upper_ms'], timestamp(text_row['cancelledAtMs']) + 0.2)
    if upper < bounds['lower_ms']:
        raise ValueError('contradictory_cancel_bounds')
    return {'output': audio[-1], 'receipts': receipts[-1], 'text': text_row,
            'textClosed': text.get('closed') is True, 'textOverflow': text.get('overflow') is not False,
            'bounds': {'lowerMs': bounds['lower_ms'], 'upperMs': upper}}, None


def validate_manifest(manifest, fixtures_bytes, traces):
    if (manifest.get('measurement_scope') != 'labeled_livekit_interruption_diagnostic'
            or manifest.get('cohort') != 'take_turn'
            or not re.fullmatch('[a-f0-9]{40}', manifest.get('measurement_revision', ''))):
        raise ValueError('invalid_stale_manifest')
    expected, trials = count(manifest.get('expected_measured')), manifest.get('trials')
    if not 1 <= expected <= 100 or not isinstance(trials, list) or len(trials) != expected:
        raise ValueError('incomplete_stale_cohort')
    if manifest.get('fixture_indices') is not None:
        raise ValueError('targeted_cohort_not_supported')
    if manifest.get('labeled_manifest_sha256') != hashlib.sha256(fixtures_bytes).hexdigest():
        raise ValueError('fixture_manifest_hash_mismatch')
    fixtures = [f for f in json.loads(fixtures_bytes)['trials'] if f['cohort'] == 'take_turn'][:expected]
    if len(fixtures) != expected or any(t.get('fixture_sha256') != f['audio_sha256']
        or t.get('cohort') != 'take_turn' for t, f in zip(trials, fixtures, strict=True)):
        raise ValueError('fixture_order_mismatch')
    for key in ('session_id', 'conversation_id', 'old_response_id'):
        ids = [identity(t[key]) for t in trials if t.get(key)]
        if len(set(ids)) != len(ids):
            raise ValueError('cohort_not_independent')
    if len({r.get('character_id') for r in traces}) > 1 or any(
            r.get('measurement_kind') != 'controlled_baseline' for r in traces):
        raise ValueError('mixed_trace_scope')
    if any(t.get('outcome') not in ('success', 'failure') for t in trials):
        raise ValueError('invalid_trial_outcome')
    return trials, fixtures


def empty_channel(unit):
    return dict(unit=unit, observed=0, missing=0, missing_reasons={}, definitely_stale=0,
                possibly_stale=0, verified_zero=0, lower_total=0, upper_total=0)


def add_channel(channel, lower=None, upper=None, reason=None):
    if reason is not None:
        if reason not in REASONS:
            raise ValueError('invalid_missing_reason')
        channel['missing'] += 1
        channel['missing_reasons'][reason] = channel['missing_reasons'].get(reason, 0) + 1
        return
    lower, upper = count(lower), count(upper)
    if lower > upper:
        raise ValueError('inverted_stale_counts')
    channel['observed'] += 1
    channel['definitely_stale'] += lower > 0
    channel['possibly_stale'] += upper > 0
    channel['verified_zero'] += upper == 0
    channel['lower_total'] = count(channel['lower_total'] + lower)
    channel['upper_total'] = count(channel['upper_total'] + upper)


def evaluate(report):
    n = report['counts']['expected']
    c = report['counts']
    channels = report['channels']
    coverage = (n >= 100 and c['recorded'] == n and c['independent_sessions'] == n
                and c['session_end_confirmed'] == n and c['verified_injection'] == n
                and c['replayed_cancel_windows'] == n and c['failure'] == 0)
    complete = coverage and all(row['observed'] == n and row['missing'] == 0 for row in channels.values())
    presented = ('audio_presented_samples', 'live_text_presented_characters', 'history_text_presented_characters')
    zero = complete and all(channels[k]['upper_total'] == 0 for k in presented)
    return {'minimum_trials': 100, 'cohort_coverage_complete': coverage,
            'all_boundaries_observed': complete, 'stale_presented_passed': zero}


def summarize(manifest, fixtures_bytes, traces, replay):
    trials, fixtures = validate_manifest(manifest, fixtures_bytes, traces)
    channels = {name: empty_channel(unit) for name, unit in CHANNELS.items()}
    counts = Counter(expected=len(trials), recorded=len(trials))
    for trial, fixture in zip(trials, fixtures, strict=True):
        counts[trial['outcome']] += 1
        counts['independent_sessions'] += bool(trial.get('session_id'))
        counts['session_end_confirmed'] += trial.get('session_end_confirmed') is True
        injected = (bool(trial.get('session_id') and trial.get('old_response_id'))
                    and trial.get('injection_playback', {}).get('active') is True and bounds_valid(trial, fixture))
        counts['verified_injection'] += injected
        prepared, reason = prepare_trial(trial, traces) if injected else (None, 'fixture_injection_unverified')
        result = None
        if prepared is not None:
            try:
                result = replay(prepared)
            except (ValueError, KeyError, TypeError):
                reason = 'replay_invalid'
        values = {}
        if result is not None:
            if result.get('bounds') != prepared['bounds']:
                raise ValueError('replay_boundary_changed')
            counts['replayed_cancel_windows'] += 1
            audio, received, text = result['audio'], result['received'], result['text']
            if audio.get('complete') is True and audio.get('missingReason') is None:
                a = audio['audit']
                if a.get('complete') is not True or a.get('drained') is not True or a.get('missingReason') is not None:
                    raise ValueError('inconsistent_audio_replay')
                values['audio_presented_samples'] = (a['nonzeroSamplesAfterCancelLower'], a['nonzeroSamplesAfterCancelUpper'])
            if received.get('complete') is True and received.get('missingReason') is None:
                v = received['counts']
                values['audio_received_packets'] = (v['itemsLower'], v['itemsUpper'])
                values['audio_received_samples'] = (v['unitsLower'], v['unitsUpper'])
            if text.get('complete') is True and text.get('missingReason') is None:
                for name, key in (('text_received_characters', 'received'), ('live_text_presented_characters', 'presented')):
                    values[name] = (text[key]['unitsLower'], text[key]['unitsUpper'])
        for name, missing in (
            ('audio_presented_samples', 'output_window_unobserved'),
            ('audio_received_packets', 'receipt_window_unobserved'), ('audio_received_samples', 'receipt_window_unobserved'),
            ('text_received_characters', 'text_window_unobserved'), ('live_text_presented_characters', 'text_window_unobserved')):
            if name in values:
                add_channel(channels[name], *values[name])
            else:
                add_channel(channels[name], reason=reason or missing)
        add_channel(channels['history_text_presented_characters'], reason='history_observer_unimplemented')
        for name in ('server_generated_text_characters', 'server_generated_audio_samples'):
            add_channel(channels[name], reason='server_generation_observer_unimplemented')
    report = {'schema_version': '1.0', 'scope': 'labeled_livekit_stale_output_report',
              'cohort': 'take_turn', 'cancel_boundary': 'server_cancel_state_transition',
              'audio_presentation_boundary': 'post_gain_browser_output_clock',
              'counts': {k: counts[k] for k in ('expected', 'recorded', 'success', 'failure',
                  'independent_sessions', 'session_end_confirmed', 'verified_injection', 'replayed_cancel_windows')},
              'channels': channels}
    report['evaluation'] = evaluate(report)
    return report


def validate_report(report, schema):
    Draft202012Validator(schema).validate(report)
    c = report['counts']
    if c['recorded'] != c['expected'] or c['success'] + c['failure'] != c['recorded']:
        raise ValueError('report_denominator_mismatch')
    if any(v > c['expected'] for v in c.values()):
        raise ValueError('report_count_exceeds_cohort')
    for row in report['channels'].values():
        if (row['observed'] + row['missing'] != c['expected']
                or sum(row['missing_reasons'].values()) != row['missing']
                or row['verified_zero'] + row['possibly_stale'] != row['observed']
                or row['definitely_stale'] > row['possibly_stale']
                or row['lower_total'] > row['upper_total']
                or (row['upper_total'] == 0) != (row['possibly_stale'] == 0)
                or (row['lower_total'] == 0) != (row['definitely_stale'] == 0)):
            raise ValueError('report_channel_mismatch')
    if report['evaluation'] != evaluate(report):
        raise ValueError('report_evaluation_mismatch')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('manifest', 'trace', 'fixtures', 'output'):
        parser.add_argument('--' + key, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError('output_exists')
        raw, trace, fixtures = args.manifest.read_bytes(), args.trace.read_bytes(), args.fixtures.read_bytes()
        manifest = json.loads(raw)
        traces = [json.loads(line) for line in trace.splitlines() if line.strip()]
        schema_path = ROOT / 'docs/schemas/voice-quality-stale-report-v1.schema.json'
        schema_bytes = schema_path.read_bytes()
        sources = ('frontend/scripts/replay-stale-observations.ts', 'frontend/playwright/replay-stale-window.ts',
                   'frontend/src/livekit/post-gain-archive.ts', 'frontend/src/livekit/post-gain-audit.ts',
                   'frontend/src/livekit/render-quantum-clock.ts')
        # 1つのCLIから現在の実装をbuildし、そのまま実行する。別revisionのreplay結果は受け取らない。
        with tempfile.TemporaryDirectory(prefix='voice-stale-') as directory:
            base = Path(directory)
            bundle = base / 'replay.mjs'
            build = subprocess.run([str(ROOT / 'frontend/node_modules/.bin/esbuild'), str(ROOT / sources[0]),
                '--bundle', '--platform=node', '--format=esm', '--outfile=' + str(bundle)],
                capture_output=True, timeout=60, check=False)
            if build.returncode:
                raise ValueError('replay_build_failed')
            serial = 0
            def replay(prepared):
                nonlocal serial
                serial += 1
                source, target = base / f'input-{serial}.json', base / f'output-{serial}.json'
                with os.fdopen(os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as stream:
                    json.dump({'measurementRevision': manifest['measurement_revision'], 'trials': [prepared]}, stream)
                run = subprocess.run(['node', str(bundle), str(source), str(target)], capture_output=True, timeout=30, check=False)
                if run.returncode:
                    raise ValueError('replay_failed')
                result = json.loads(target.read_bytes())
                if result['measurementRevision'] != manifest['measurement_revision'] or len(result['trials']) != 1:
                    raise ValueError('replay_identity_changed')
                return result['trials'][0]
            report = summarize(manifest, fixtures, traces, replay)
        digest = lambda value: hashlib.sha256(value).hexdigest()
        report['provenance'] = {'measurement_revision': manifest['measurement_revision'],
            'raw_manifest_sha256': digest(raw), 'raw_trace_sha256': digest(trace), 'fixtures_sha256': digest(fixtures),
            'reporter_sha256': digest(Path(__file__).read_bytes()), 'schema_sha256': digest(schema_bytes),
            'clock_validator_sha256': digest((ROOT / 'scripts/voice_quality/server_clock.py').read_bytes()),
            'fixture_validator_sha256': digest((ROOT / 'scripts/voice_quality/report_take_turn.py').read_bytes()),
            'replay_sources_sha256': digest(b''.join((ROOT / path).read_bytes() for path in sources))}
        validate_report(report, json.loads(schema_bytes))
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        # schemaは固定キー・固定enum・数値・hash以外を許さず、ID/本文/clock時点を出力しない。
        with os.fdopen(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as stream:
            stream.write(serialized)
        print(json.dumps({'report_saved': True, 'evaluation': report['evaluation']}))
        return 0 if report['evaluation']['stale_presented_passed'] else 1
    except Exception:
        # 入力・provider・プロセスの任意の例外本文をpublic logへ流さない。
        print('stale_report_failed', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
