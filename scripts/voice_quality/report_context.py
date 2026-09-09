"""合成context比較の全試行から匿名集計を再計算する。本文や任意provider値は転記しない。"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.inference.diagnostics import DIAGNOSTIC_NAMES, POINT_NAMES
from app.voice_baseline import _assert_anonymous
from jsonschema import Draft202012Validator

CASES = ((0, 0), (2, 0), (8, 0), (0, 1), (0, 4), (8, 4))
VARIANTS = ('configured', 'thinking_disabled')
USAGE = ('total', 'character', 'character_lore', 'rag', 'history', 'current_user', 'post_history',
         'omitted_character_lore_entries', 'omitted_rag_items', 'omitted_history_exchanges')
OBSERVATIONS = ('character_name_present', 'history_fact_present', 'memory_fact_present', 'nonempty_response')
METRICS = {
    'first_token_ms': ('llm_request_started', 'llm_first_token'),
    'prompt_preparation_ms': ('prompt_preparation_started', 'prompt_preparation_completed'),
    'provider_load_ms': ('ollama_generation_load_ms',),
    'provider_prompt_eval_ms': ('ollama_generation_prompt_eval_ms',),
    'provider_generation_ms': ('ollama_generation_generation_ms',),
}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def statistics(values, expected):
    values = sorted(values)
    def quantile(p):
        if not values:
            return None
        position = (len(values) - 1) * p
        lower = math.floor(position)
        return values[lower] + (values[math.ceil(position)] - values[lower]) * (position - lower)
    return {'count': len(values), 'missing': expected - len(values),
            'missing_reason': 'required_observation_missing' if len(values) < expected else None,
            'p50': quantile(.5), 'p95': quantile(.95),
            'minimum': min(values) if values else None, 'maximum': max(values) if values else None}


def validate_trial(row, repetitions):
    required = {'case_index', 'history_count', 'memory_count', 'variant', 'trial_index', 'warmup',
                'outcome', 'diagnostics', 'quality_observations'}
    require(isinstance(row, dict) and required <= set(row) <= required | {'prompt_usage', 'failure_type'}, 'trial fields invalid')
    case, index = row['case_index'], row['trial_index']
    require(type(case) is int and 0 <= case < len(CASES), 'case invalid')
    require(type(index) is int and 0 <= index <= repetitions, 'trial index invalid')
    require(type(row['history_count']) is int and type(row['memory_count']) is int
            and (row['history_count'], row['memory_count']) == CASES[case], 'context count mismatch')
    require(row['variant'] in VARIANTS and row['outcome'] in ('success', 'failure'), 'trial status invalid')
    require(type(row['warmup']) is bool and row['warmup'] == (index == 0), 'warmup mismatch')
    usage = row.get('prompt_usage')
    require(usage is None or (isinstance(usage, dict) and set(usage) == set(USAGE)
                              and all(type(v) is int and v >= 0 for v in usage.values())), 'prompt usage invalid')
    observation = row['quality_observations']
    require(isinstance(observation, dict) and set(observation) == set(OBSERVATIONS)
            and all(type(v) is bool for v in observation.values()), 'quality observation invalid')
    if row['outcome'] == 'success':
        require(usage is not None and 'failure_type' not in row, 'success evidence missing')
    else:
        require(isinstance(row.get('failure_type'), str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', row['failure_type']), 'failure type invalid')
    events = {}
    require(isinstance(row['diagnostics'], list), 'diagnostics invalid')
    for event in row['diagnostics']:
        require(isinstance(event, dict) and set(event) == {'name', 'elapsed_ms', 'value'}, 'diagnostic fields invalid')
        name = event['name']
        require(isinstance(name, str) and name in DIAGNOSTIC_NAMES and name not in events, 'diagnostic name invalid')
        require(finite(event['elapsed_ms']) and (event['value'] is None if name in POINT_NAMES else finite(event['value'])), 'diagnostic number invalid')
        events[name] = event
    return events


def summarize(plan, rows):
    required_plan = {'scope', 'measurement_revision', 'character_card_sha256', 'cases', 'repetitions',
                     'warmup_per_case_and_variant', 'memory_retrieval_exercised', 'normal_options_think',
                     'full_persona_maintained', 'character_book_selection_exercised', 'input_limit', 'output_limit'}
    require(isinstance(plan, dict) and required_plan <= set(plan) <= required_plan | {'runner_sha256'}, 'plan fields invalid')
    require(plan['scope'] == 'synthetic_prompt_context_comparison' and plan['cases'] == [list(c) for c in CASES], 'plan scope invalid')
    for key, length in (('measurement_revision', 40), ('character_card_sha256', 64)):
        require(isinstance(plan[key], str) and re.fullmatch('[a-f0-9]{%d}' % length, plan[key]), 'plan provenance invalid')
    if 'runner_sha256' in plan:
        require(isinstance(plan['runner_sha256'], str) and re.fullmatch('[a-f0-9]{64}', plan['runner_sha256']), 'runner provenance invalid')
    require(plan['memory_retrieval_exercised'] is False and plan['full_persona_maintained'] is True
            and plan['character_book_selection_exercised'] is True, 'plan context invalid')
    require(plan['normal_options_think'] is None or type(plan['normal_options_think']) is bool, 'thinking option invalid')
    require(type(plan['warmup_per_case_and_variant']) is int and plan['warmup_per_case_and_variant'] == 1, 'warmup plan invalid')
    count = plan['repetitions']
    require(type(count) is int and 1 <= count <= 5, 'repetition count invalid')
    require(all(type(plan[k]) is int and plan[k] > 0 for k in ('input_limit', 'output_limit')), 'token limits invalid')
    require(isinstance(rows, list) and len(rows) == len(CASES) * len(VARIANTS) * (count + 1), 'all planned trials required')
    indexed = {}
    for row in rows:
        events = validate_trial(row, count)
        key = row['case_index'], row['variant'], row['trial_index']
        require(key not in indexed, 'duplicate trial')
        indexed[key] = row, events
    conditions = []
    for case, (h, m) in enumerate(CASES):
        for variant in VARIANTS:
            sample = [indexed[case, variant, index] for index in range(1, count + 1)]
            metrics = {}
            for name, event_names in METRICS.items():
                values = []
                for row, events in sample:
                    if not all(k in events for k in event_names):
                        continue
                    value = (events[event_names[1]]['elapsed_ms'] - events[event_names[0]]['elapsed_ms']
                             if len(event_names) == 2 else events[event_names[0]]['value'])
                    require(finite(value), 'metric clock order invalid')
                    values.append(value)
                metrics[name] = statistics(values, count)
            prompt_tokens = {key: statistics([row['prompt_usage'][key] for row, _ in sample if 'prompt_usage' in row], count)
                             for key in USAGE}
            success = sum(row['outcome'] == 'success' for row, _ in sample)
            matches = sum(row['outcome'] == 'success' and row['quality_observations'] == {
                'character_name_present': True, 'history_fact_present': h > 0,
                'memory_fact_present': m > 0, 'nonempty_response': True} for row, _ in sample)
            conditions.append({'case_index': case, 'history_count': h, 'memory_count': m, 'variant': variant,
                               'measured': count, 'request_success': success, 'request_failure': count - success,
                               'expected_marker_match': matches, 'expected_marker_mismatch': success - matches,
                               'marker_observation_unavailable': count - success, 'metrics': metrics, 'prompt_tokens': prompt_tokens})
    warmup = [row for row in rows if row['warmup']]
    measured = [row for row in rows if not row['warmup']]
    return {'schema_version': '1.0', 'measurement_scope': 'synthetic_prompt_context_comparison',
            'measurement_revision': plan['measurement_revision'], 'character_card_sha256': plan['character_card_sha256'],
            'runner_sha256': plan.get('runner_sha256'), 'normal_options_think': plan['normal_options_think'],
            'counts': {'recorded': len(rows), 'warmup': len(warmup), 'warmup_failure': sum(r['outcome'] == 'failure' for r in warmup),
                       'measured': len(measured), 'request_failure': sum(r['outcome'] == 'failure' for r in measured)},
            'limits': {'memory_retrieval_exercised': False, 'general_personality_quality_verified': False,
                       'pure_thinking_causal_effect_verified': False, 'response_content_persisted': False},
            'conditions': conditions}


def validate_report(report, schema):
    require(Draft202012Validator(schema).is_valid(report), 'context report schema mismatch')
    _assert_anonymous(report)
    require(len({(c['case_index'], c['variant']) for c in report['conditions']}) == 12, 'condition coverage mismatch')
    require(report['counts']['measured'] == sum(c['measured'] for c in report['conditions']), 'measurement denominator mismatch')
    require(report['counts']['recorded'] == report['counts']['measured'] + report['counts']['warmup'], 'recorded denominator mismatch')
    require(report['counts']['request_failure'] == sum(c['request_failure'] for c in report['conditions']), 'failure denominator mismatch')
    for condition in report['conditions']:
        n = condition['measured']
        require((condition['history_count'], condition['memory_count']) == CASES[condition['case_index']], 'context count mismatch')
        require(condition['request_success'] + condition['request_failure'] == n, 'request denominator mismatch')
        require(condition['expected_marker_match'] + condition['expected_marker_mismatch'] == condition['request_success']
                and condition['marker_observation_unavailable'] == condition['request_failure'], 'quality denominator mismatch')
        for metric in [*condition['metrics'].values(), *condition['prompt_tokens'].values()]:
            require(metric['count'] + metric['missing'] == n, 'metric denominator mismatch')
            require((metric['missing_reason'] is not None) == (metric['missing'] > 0), 'missing reason mismatch')
            if metric['count']:
                require(all(finite(metric[k]) for k in ('minimum', 'p50', 'p95', 'maximum'))
                        and metric['minimum'] <= metric['p50'] <= metric['p95'] <= metric['maximum'], 'quantile order invalid')
            else:
                require(all(metric[k] is None for k in ('minimum', 'p50', 'p95', 'maximum')), 'missing is not zero')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        plan = json.loads((args.run / 'plan.json').read_text())
        rows = [json.loads(line) for line in (args.run / 'numeric.jsonl').read_text().splitlines()]
        report = summarize(plan, rows)
        schema = json.loads((ROOT / 'docs/schemas/voice-quality-context-report-v1.schema.json').read_text())
        validate_report(report, schema)
        with args.output.open('x') as output:
            json.dump(report, output, indent=2, allow_nan=False)
            output.write('\n')
        print(json.dumps({'context_report_saved': True, 'counts': report['counts']}))
    except Exception:
        print('context集計の入力・schema・出力先を検証できません。', file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
