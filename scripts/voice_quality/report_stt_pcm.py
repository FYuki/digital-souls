"""実Whisper入力とCore最終STTのsample数を相関し、固定発話端部の匿名集計を作る。"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Literal
from uuid import UUID

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.voice_baseline import _assert_anonymous
from pcm_boundary_alignment import BOUNDED_EDGE_METHOD, valid_bounded_pcm_edges


class Counts(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected: int
    recorded: int
    final_inputs_correlated: int
    edges_matched: int
    missing: int
    session_cleanup_confirmed: int
    voice_success: int
    other_stt_requests: int
    nonuniform_global_alignment: int


class PcmReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal['1.0'] = '1.0'
    edge_alignment_method: Literal['bandlimited_100ms_multi_seed_edge_witness_v4'] = BOUNDED_EDGE_METHOD
    evaluation_scope: Literal['actual_stt_pcm_edge_coverage_only'] = 'actual_stt_pcm_edge_coverage_only'
    cohort: Literal['normal', 'pause', 'backchannel', 'take_turn']
    measurement_revision: str | None
    evidence_sha256: dict[str, str]
    counts: Counts
    missing_reasons: dict[str, int]
    minimum_edge_correlation: float | None
    passed: bool
    gates: dict[str, bool]
    interior_continuity_verified: Literal[False] = False
    all_vad_acceptance_verified: Literal[False] = False


def number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def valid_edges(row: dict, start: int, end: int) -> bool:
    return valid_bounded_pcm_edges(row.get('bounded_edge_alignment'), start, end, row.get('input_sample_count'))


def summarize(manifest: dict, observed: list[dict], events: list[dict], catalog: dict[str, tuple[int, int]]) -> dict:
    cohort = manifest.get('cohort', 'normal')
    if cohort not in ('normal', 'pause', 'backchannel', 'take_turn'):
        raise ValueError('unsupported PCM cohort')
    expected = manifest.get('expected_measured')
    trials = manifest.get('trials')
    warmup = manifest.get('expected_warmup') if cohort == 'normal' else 0
    if (type(expected) is not int or not 1 <= expected <= 100 or type(warmup) is not int
            or warmup not in (0, 1, 5) or (cohort == 'normal' and warmup != (5 if expected == 100 else 1))
            or not isinstance(trials, list) or len(trials) > expected + warmup):
        raise ValueError('invalid PCM trial denominator')
    revision = manifest.get('measurement_revision')
    if revision is not None and (not isinstance(revision, str) or re.fullmatch('[0-9a-f]{40}', revision) is None):
        raise ValueError('invalid measurement revision')
    ordinals = [row.get('request_ordinal') for row in observed]
    if any(type(n) is not int or n < 1 for n in ordinals) or len(set(ordinals)) != len(ordinals):
        raise ValueError('duplicate or invalid input request ordinal')
    counts = Counts(expected=expected, recorded=max(0, len(trials) - warmup), final_inputs_correlated=0,
        edges_matched=0, missing=0, session_cleanup_confirmed=0, voice_success=0,
        other_stt_requests=0, nonuniform_global_alignment=0)
    missing = Counter()
    correlations = []
    identities = set()
    for index in range(warmup, expected + warmup):
        if index >= len(trials):
            missing['trial_not_recorded'] += 1
            continue
        trial = trials[index]
        if cohort == 'normal' and trial.get('phase') != 'measured':
            missing['invalid_trial_phase'] += 1
            continue
        counts.session_cleanup_confirmed += trial.get('session_end_confirmed') is True
        counts.voice_success += trial.get('outcome') == 'success'
        session = trial.get('sessionId') if cohort == 'normal' else trial.get('session_id')
        try:
            if str(UUID(session)) != session or session in identities:
                raise ValueError('invalid session')
            identities.add(session)
        except (ValueError, TypeError, AttributeError):
            missing['independent_session_unavailable'] += 1
            continue
        if cohort == 'normal':
            utterance = trial.get('utteranceId')
            digest = trial.get('audio_sha256')
            phase = 'initial'
        else:
            finals = [e for e in trial.get('evidence', {}).get('core_events', [])
                      if e.get('type') == 'utterance_finalized' and e.get('utteranceId')
                      and e['utteranceId'] != trial.get('initial_utterance_id') and e.get('sessionId') == session]
            if len(finals) != 1:
                missing['final_utterance_unavailable'] += 1
                continue
            utterance = finals[0]['utteranceId']
            digest = trial.get('fixture_sha256')
            phase = 'labeled'
        if digest not in catalog:
            raise ValueError('fixture not in verified catalog')
        trace = [e for e in events if e.get('session_id') == session and e.get('utterance_id') == utterance]
        def values(name):
            return [e.get('value') for e in trace if e.get('name') == name]
        prepared = values('stt_input_prepared_sample_count')
        if (len(prepared) != 1 or not number(prepared[0]) or prepared[0] <= 0
                or values('stt_input_suffix_preserved') != [1]
                or values('stt_capture_received_span_valid') != [1]):
            missing['final_stt_capture_evidence_unavailable'] += 1
            continue
        snapshot = trial.get('pcm_input_observation')
        if (not isinstance(snapshot, dict) or snapshot.get('overflow') is not False
                or snapshot.get('active_requests') != 0):
            missing['input_observer_not_closed'] += 1
            continue
        rows = [r for r in observed if r.get('trial_ordinal') == index + 1 and r.get('phase') == phase]
        saved = [r for r in snapshot.get('rows', []) if r.get('phase') == phase]
        if sorted(rows, key=lambda r: r['request_ordinal']) != sorted(saved, key=lambda r: r['request_ordinal']):
            missing['observer_snapshot_mismatch'] += 1
            continue
        candidates = [r for r in rows if r.get('preparation_silence') is False
                      and r.get('input_sample_count') == prepared[0]]
        counts.other_stt_requests += sum(r.get('preparation_silence') is False for r in rows) - len(candidates)
        if len(candidates) != 1:
            missing['final_http_input_not_unique'] += 1
            continue
        row = candidates[0]
        if (row.get('fixture_sha256') != digest or row.get('upstream_status') != 200
                or not number(row.get('started_ns')) or not number(row.get('completed_ns'))
                or row['completed_ns'] < row['started_ns']):
            missing['final_http_input_incomplete'] += 1
            continue
        counts.final_inputs_correlated += 1
        global_alignment = row.get('alignment') or {}
        counts.nonuniform_global_alignment += global_alignment.get('reason') == 'inconsistent_anchor_offsets'
        if not valid_edges(row, *catalog[digest]):
            missing['speech_edges_unverified'] += 1
            continue
        counts.edges_matched += 1
        correlations.extend(block['correlation'] for edge in row['bounded_edge_alignment']['edges'] for block in edge['blocks'])
    counts.missing = sum(missing.values())
    gates = {'independent_100': expected == 100, 'all_final_inputs_correlated': counts.final_inputs_correlated == expected,
             'all_speech_edges_matched': counts.edges_matched == expected, 'no_missing': counts.missing == 0,
             'all_sessions_ended': counts.session_cleanup_confirmed == expected,
             'measurement_revision_recorded': revision is not None}
    return dict(cohort=cohort, measurement_revision=revision, counts=counts,
                missing_reasons=dict(sorted(missing.items())),
                minimum_edge_correlation=min(correlations) if correlations else None,
                gates=gates, passed=all(gates.values()))


def build_report(run_root: Path) -> PcmReport:
    paths = {'manifest': run_root / 'trial-manifest.json', 'observer': run_root / 'whisper-input-pcm.jsonl',
             'trace': run_root / 'runtime-data/voice-metrics/controlled-trace.jsonl',
             'profile': run_root / 'runtime-data/runtime/standalone/resolved-profile.json'}
    raw = {name: path.read_bytes() for name, path in paths.items()}
    profile = json.loads(raw['profile'])
    if (profile.get('effectiveProfile') != 'integration-voice-pcm'
            or profile.get('derivedEnvironment', {}).get('DS_ENVIRONMENT_ID') != 'test'
            or profile.get('derivedEnvironment', {}).get('WHISPER_BASE_URL') != 'http://127.0.0.1:50023'):
        raise ValueError('resolved PCM observer test profile required')
    fixture_root = ROOT / 'frontend/playwright/fixtures'
    normal = json.loads((fixture_root / 'speech.metadata.json').read_text())
    labeled_bytes = (fixture_root / 'voice-quality-v2/manifest.json').read_bytes()
    fixtures = json.loads(labeled_bytes)
    catalog = {normal['audio_sha256']: (normal['speech_start_sample'] // 3, (normal['speech_end_sample'] + 2) // 3)}
    for row in fixtures['trials']:
        catalog[row['audio_sha256']] = (row['speech_intervals'][0]['start_sample'] // 3,
                                       (row['speech_intervals'][-1]['end_sample'] + 2) // 3)
    manifest = json.loads(raw['manifest'])
    if manifest.get('cohort') and manifest.get('labeled_manifest_sha256') != hashlib.sha256(labeled_bytes).hexdigest():
        raise ValueError('fixture manifest hash mismatch')
    if manifest.get('cohort'):
        available = [row for row in fixtures['trials'] if row['cohort'] == manifest['cohort']]
        indices = manifest.get('fixture_indices')
        count = manifest.get('expected_measured')
        if type(count) is not int or not 1 <= count <= 100:
            raise ValueError('invalid labeled denominator')
        if indices is None:
            selected = available[:count]
        else:
            if (not isinstance(indices, list) or count > 10 or len(indices) != count
                    or any(type(i) is not int or not 1 <= i <= len(available) for i in indices)
                    or len(set(indices)) != count):
                raise ValueError('invalid diagnostic selection')
            selected = [available[i - 1] for i in indices]
        if len(selected) != count or any(t.get('fixture_sha256') != selected[i]['audio_sha256']
                for i, t in enumerate(manifest['trials'])):
            raise ValueError('labeled fixture order mismatch')
    elif manifest.get('fixture', {}).get('audio_sha256') != normal['audio_sha256']:
        raise ValueError('normal fixture mismatch')
    report = PcmReport(evidence_sha256={name: hashlib.sha256(body).hexdigest() for name, body in raw.items()},
        **summarize(manifest, [json.loads(line) for line in raw['observer'].splitlines()],
                    [json.loads(line) for line in raw['trace'].splitlines()], catalog))
    _assert_anonymous(report.model_dump(mode='json'))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.run_root)
    schema = json.loads((ROOT / 'docs/schemas/voice-quality-stt-pcm-report-v1.schema.json').read_text())
    Draft202012Validator(schema).validate(report.model_dump(mode='json'))
    with args.output.open('x') as output:
        output.write(report.model_dump_json(indent=2) + '\n')
    return 0 if report.passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
