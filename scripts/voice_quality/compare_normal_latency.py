"""凍結WS baselineと通常100試行を比較する。実観測区間の違いは合格へ変換しない。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.voice_baseline import _assert_anonymous
from app.voice_metrics import (
    AggregateArtifact, MetricAggregate, TargetEvaluation,
    evaluate_artifact, evaluate_latency_target, evaluate_relative_latency,
)

BASELINE_REVISION = '4d71fbbf111ac87f22e7801106169ee6e4f1f532'
BASELINE_SHA256 = '26b92cd07dd36be8e952e2e89927afa5e0cfc972f8f0959f06265e404e36e466'
# 行番号だけでなく、確認した版のファイル全体を照合する。変更時には区間の再監査が必要。
AUDITED_SOURCES = {
    'baseline': {
        'frontend/src/lib/AudioRecorder.svelte': '0a78b64940d90baf67e6d3b066dcd3fd9f53bdfb1173c1b30dc1811c8bd94046',
        'frontend/src/App.svelte': 'da8b59251e0132da4692ea1bc933f06df6200694830f0321701138224c0204c0',
        'backend/app/routers/ws.py': 'db4a53ade6159b8b55ef4f0eeff128eba698f11e0eef68e1411c430fc0b39e27',
    },
    'candidate': {
        'backend/app/livekit_transport/production.py': 'c3f69802f6beaa7f257896651ac4e85cbd132227dd9e83b65a3f1b9d8dc9358a',
        'backend/app/conversation_core/session.py': '77df1eb129b35f6d0a84d4eb95be369d4b2927ab27227a17acf9a622953109c3',
    },
}
# 2026-09-08再監査: 生成音声の統計を完了通知より先に記録し、最終playback summaryを受信する変更。
# 下記3区間の起点・終点は変更されていない。旧測定版も同じ厳密なhashで再検証する。
AUDITED_CANDIDATE_VARIANTS = (
    AUDITED_SOURCES['candidate'],
    {
        'backend/app/livekit_transport/production.py': 'c128656f318ddaf50c7d5badef566bc5eb9244f4e1dce8df36668c2c02ba1b23',
        'backend/app/conversation_core/session.py': '77df1eb129b35f6d0a84d4eb95be369d4b2927ab27227a17acf9a622953109c3',
    },
    # 2026-09-08: session専用journalを追加。VAD受信、PCM受信、Core確定の境界は変更なし。
    {
        'backend/app/livekit_transport/production.py': 'fee46d48d5198b9ce9d6a1be0c2f45d684b1210f7b0590969ae7d7e84beaca70',
        'backend/app/conversation_core/session.py': '77df1eb129b35f6d0a84d4eb95be369d4b2927ab27227a17acf9a622953109c3',
    },
)
BOUNDARY_AUDIT = {
    'utterance_finalized': {
        'baseline': 'client VAD終了callback → recorder.stopAndTake完了',
        'candidate': 'server VAD終了通知受信 → STT後のCore発話確定',
        'baseline_evidence': ['frontend/src/lib/AudioRecorder.svelte:140-151'],
        'candidate_evidence': ['backend/app/livekit_transport/production.py:512-520',
                               'backend/app/livekit_transport/production.py:937-941',
                               'backend/app/conversation_core/session.py:698-700',
                               'backend/app/conversation_core/session.py:757-765'],
    },
    'response_decision': {
        'baseline': 'client VAD終了callback → 全PCM送信直前',
        'candidate': 'server VAD終了通知受信 → Core response_started',
        'baseline_evidence': ['frontend/src/lib/AudioRecorder.svelte:140-151', 'frontend/src/App.svelte:233-239'],
        'candidate_evidence': ['backend/app/livekit_transport/production.py:494-508',
                               'backend/app/livekit_transport/production.py:937-941'],
    },
    'stt_start_latency': {
        'baseline': '発話全体のWebSocket frame受信 → STT開始',
        'candidate': '発話captureへの最初のPCM受信 → STT開始',
        'baseline_evidence': ['backend/app/routers/ws.py:246-247', 'backend/app/routers/ws.py:590-627'],
        'candidate_evidence': ['backend/app/livekit_transport/production.py:973-991',
                               'backend/app/conversation_core/session.py:698-700'],
    },
}
NORMAL_ABSOLUTE = ('ttfa', 'utterance_finalized')


class BoundaryEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    baseline: str
    candidate: str
    baseline_evidence: list[str]
    candidate_evidence: list[str]


class ComparisonRow(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['passed', 'failed', 'missing', 'not_comparable']
    candidate_p95_ms: float | None
    baseline_p95_ms: float | None
    relative_limit_ms: float | None
    reason: Literal['audited_observation_intervals_differ', 'incomplete_measurement',
                    'measurement_boundary_mismatch'] | None
    boundary_evidence: BoundaryEvidence | None = None


class NormalLatencyReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal['1.0'] = '1.0'
    evaluation_scope: Literal['normal_response_latency_only'] = 'normal_response_latency_only'
    passed: bool
    candidate_revision: str
    baseline_revision: Literal[BASELINE_REVISION] = BASELINE_REVISION
    candidate_sha256: str
    baseline_sha256: Literal[BASELINE_SHA256] = BASELINE_SHA256
    audited_source_sha256: dict[str, dict[str, str]]
    absolute_results: dict[str, TargetEvaluation]
    relative_results: dict[str, ComparisonRow]
    coverage_errors: list[str]
    # default evaluatorの結果も失わない。割り込み等の欠測は別cohortの証拠が必要。
    strict_evaluation_passed: bool
    strict_coverage_errors: list[str]


def _source_bytes(revision: str, path: str) -> bytes:
    return subprocess.check_output(['git', 'show', f'{revision}:{path}'], cwd=ROOT, stderr=subprocess.DEVNULL)


def verify_sources(candidate_revision: str) -> dict[str, dict[str, str]]:
    if re.fullmatch(r'[a-f0-9]{40}', candidate_revision) is None:
        raise ValueError('full candidate revision required')
    actual = {
        side: {path: hashlib.sha256(_source_bytes(revision, path)).hexdigest()
               for path in AUDITED_SOURCES[side]}
        for side, revision in (('baseline', BASELINE_REVISION), ('candidate', candidate_revision))
    }
    if actual['baseline'] != AUDITED_SOURCES['baseline'] or actual['candidate'] not in AUDITED_CANDIDATE_VARIANTS:
        raise ValueError('measurement source changed; boundary audit required')
    return actual


def complete(metric: MetricAggregate | None) -> bool:
    return metric is not None and (
        metric.status == 'measured' and metric.unit == 'millisecond'
        and metric.p50 is not None and metric.p95 is not None
        and metric.trial_count == metric.success_count == metric.rate_denominator == 100
        and metric.failure_count == metric.missing_count == metric.not_applicable_count
        == metric.excluded_count == 0
    )


def compare(candidate: AggregateArtifact, baseline: AggregateArtifact) -> dict:
    """数値判定。CLIは呼出前にartifactと測定版を監査済みsourceへ結び付ける。"""
    strict = evaluate_artifact(candidate, baseline)
    coverage = []
    for label, artifact in (('candidate', candidate), ('baseline', baseline)):
        counts = artifact.run_counts
        if (artifact.measurement_kind != 'controlled_baseline'
                or (counts.warmup, counts.measured, counts.success, counts.failure, counts.excluded)
                != (5, 100, 100, 0, 0)):
            coverage.append(f'{label}:controlled_run_count')
        if len({m.name for m in artifact.metrics}) != len(artifact.metrics):
            coverage.append(f'{label}:duplicate_metric')
    if candidate.fixture_version != baseline.fixture_version:
        coverage.append('fixture_version_mismatch')
    current = {m.name: m for m in candidate.metrics}
    absolute = {}
    for name in NORMAL_ABSOLUTE:
        m = current.get(name)
        if not complete(m):
            coverage.append(f'{name}:incomplete_measurement')
            absolute[name] = TargetEvaluation(passed=False)
        else:
            absolute[name] = evaluate_latency_target(metric_name=name, p50_ms=m.p50, p95_ms=m.p95,
                                                      websocket_p95_ms=None)
    relative = {}
    for b in baseline.metrics:
        if b.unit != 'millisecond' or b.status != 'measured' or b.p95 is None:
            continue
        m = current.get(b.name)
        row = dict(candidate_p95_ms=None if m is None else m.p95, baseline_p95_ms=b.p95,
                   relative_limit_ms=b.p95 + max(b.p95 * .1, 50), reason=None)
        if b.name in BOUNDARY_AUDIT:
            # 除外は相対比較だけ。絶対上限・観測不足は独立して残す。
            row.update(status='not_comparable', relative_limit_ms=None,
                       reason='audited_observation_intervals_differ', boundary_evidence=BOUNDARY_AUDIT[b.name])
            if not complete(m):
                coverage.append(f'{b.name}:incomplete_measurement')
        elif not complete(m) or not complete(b):
            coverage.append(f'{b.name}:incomplete_measurement')
            row.update(status='missing', reason='incomplete_measurement')
        elif (m.start_point, m.end_point) != (b.start_point, b.end_point):
            coverage.append(f'{b.name}:measurement_boundary_mismatch')
            row.update(status='failed', reason='measurement_boundary_mismatch')
        else:
            row.update(status='passed' if evaluate_relative_latency(m.p95, b.p95).passed else 'failed')
        relative[b.name] = ComparisonRow(**row)
    return dict(passed=not coverage and all(r.passed for r in absolute.values())
                and all(r.status in ('passed', 'not_comparable') for r in relative.values()),
                absolute_results=absolute, relative_results=relative, coverage_errors=sorted(set(coverage)),
                strict_evaluation_passed=strict.passed, strict_coverage_errors=strict.coverage_errors)


def build_report(candidate_path: Path, baseline_path: Path, verification_path: Path) -> NormalLatencyReport:
    cbytes, bbytes = candidate_path.read_bytes(), baseline_path.read_bytes()
    candidate_hash = hashlib.sha256(cbytes).hexdigest()
    if hashlib.sha256(bbytes).hexdigest() != BASELINE_SHA256:
        raise ValueError('frozen baseline hash mismatch')
    evidence = json.loads(verification_path.read_text())
    if evidence.get('artifact_sha256') != candidate_hash:
        raise ValueError('candidate verification hash mismatch')
    revision = evidence.get('measurement_revision')
    if not isinstance(revision, str):
        raise ValueError('candidate measurement revision required')
    audited_sources = verify_sources(revision)
    schema = json.loads((ROOT / 'docs/schemas/voice-quality-artifact-v1.schema.json').read_text())
    for raw in (cbytes, bbytes):
        value = json.loads(raw)
        Draft202012Validator(schema).validate(value)
        _assert_anonymous(value)
    result = NormalLatencyReport(candidate_revision=revision, candidate_sha256=candidate_hash,
        audited_source_sha256=audited_sources,
        **compare(AggregateArtifact.model_validate_json(cbytes), AggregateArtifact.model_validate_json(bbytes)))
    _assert_anonymous(result.model_dump(mode='json'))
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidate', type=Path, required=True)
    p.add_argument('--verification', type=Path, required=True)
    p.add_argument('--baseline', type=Path, default=ROOT / 'docs/artifacts/websocket-baseline-v1.json')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    result = build_report(args.candidate, args.baseline, args.verification)
    schema = json.loads((ROOT / 'docs/schemas/voice-quality-normal-latency-report-v1.schema.json').read_text())
    Draft202012Validator(schema).validate(result.model_dump(mode='json'))
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(result.model_dump_json(indent=2) + '\n')
    # 未達・欠測もartifactを保存したうえでCIへ失敗として返す。
    raise SystemExit(0 if result.passed else 1)


if __name__ == '__main__':
    main()
