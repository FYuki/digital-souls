from __future__ import annotations

import copy
import asyncio
from types import SimpleNamespace
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/voice_quality'))
try:
    import compare_context as runner
    import report_context as report
finally:
    sys.path.remove(str(ROOT / 'scripts/voice_quality'))


@pytest.fixture
def inputs():
    plan = {'scope': 'synthetic_prompt_context_comparison', 'measurement_revision': 'a' * 40,
            'character_card_sha256': 'b' * 64, 'cases': [list(c) for c in report.CASES], 'repetitions': 2,
            'warmup_per_case_and_variant': 1, 'memory_retrieval_exercised': False, 'normal_options_think': None,
            'full_persona_maintained': True, 'character_book_selection_exercised': True,
            'input_limit': 7168, 'output_limit': 1024}
    rows = []
    for case, (h, m) in enumerate(report.CASES):
        for variant in report.VARIANTS:
            for index in range(3):
                events = [{'name': k, 'elapsed_ms': v, 'value': None} for k, v in {
                    'prompt_preparation_started': 0, 'prompt_preparation_completed': 9,
                    'llm_request_started': 10, 'llm_first_token': 30 if index else 10000}.items()]
                events += [{'name': k, 'elapsed_ms': 10001, 'value': v} for k, v in {
                    'ollama_generation_load_ms': 3, 'ollama_generation_prompt_eval_ms': 4,
                    'ollama_generation_generation_ms': 8}.items()]
                usage = dict.fromkeys(report.USAGE, 0)
                usage.update(total=50+h+m, character=10, history=h, rag=m)
                rows.append({'case_index': case, 'history_count': h, 'memory_count': m, 'variant': variant,
                             'trial_index': index, 'warmup': index == 0, 'outcome': 'success',
                             'diagnostics': events, 'prompt_usage': usage, 'quality_observations': {
                                 'character_name_present': True, 'history_fact_present': h > 0,
                                 'memory_fact_present': m > 0, 'nonempty_response': True}})
    return plan, rows


@pytest.fixture
def schema():
    return json.loads((ROOT / 'docs/schemas/voice-quality-context-report-v1.schema.json').read_text())


def test_warmup_is_excluded_from_every_condition_and_provenance_is_not_invented(inputs, schema):
    result = report.summarize(*inputs)
    report.validate_report(result, schema)
    assert result['counts'] == {'recorded': 36, 'warmup': 12, 'warmup_failure': 0,
                                'measured': 24, 'request_failure': 0}
    assert result['runner_sha256'] is None
    for condition in result['conditions']:
        assert condition['metrics']['first_token_ms']['p95'] == 20
        assert condition['expected_marker_match'] == 2


@pytest.mark.parametrize('mutation', ['drop', 'duplicate', 'warmup', 'case', 'field', 'event', 'nan', 'reverse'])
def test_incomplete_or_tampered_raw_data_is_rejected(inputs, mutation):
    plan, rows = inputs
    row = rows[1]
    if mutation == 'drop':
        rows.pop()
    elif mutation == 'duplicate':
        rows[-1] = rows[0]
    elif mutation == 'warmup':
        row['warmup'] = True
    elif mutation == 'case':
        row['memory_count'] = 9
    elif mutation == 'field':
        row['response'] = 'private text must not appear in errors'
    elif mutation == 'event':
        row['diagnostics'].append(row['diagnostics'][0])
    elif mutation == 'nan':
        row['diagnostics'][0]['elapsed_ms'] = float('nan')
    else:
        row['diagnostics'][3]['elapsed_ms'] = 1
    with pytest.raises(ValueError) as error:
        report.summarize(plan, rows)
    assert 'private text' not in str(error.value)


def test_request_failure_and_unobserved_metric_remain_in_denominators(inputs, schema):
    plan, rows = inputs
    rows[1].update(outcome='failure', failure_type='InferenceError', diagnostics=[],
                   quality_observations=dict.fromkeys(report.OBSERVATIONS, False))
    del rows[1]['prompt_usage']
    result = report.summarize(plan, rows)
    report.validate_report(result, schema)
    group = result['conditions'][0]
    assert group['request_failure'] == group['marker_observation_unavailable'] == 1
    assert group['expected_marker_match'] == 1
    assert group['metrics']['first_token_ms']['missing'] == 1
    assert group['metrics']['first_token_ms']['missing_reason'] == 'required_observation_missing'
    assert result['counts']['request_failure'] == 1


def test_unexpected_known_fact_in_empty_context_is_a_mismatch(inputs, schema):
    inputs[1][1]['quality_observations']['memory_fact_present'] = True
    result = report.summarize(*inputs)
    report.validate_report(result, schema)
    assert result['conditions'][0]['expected_marker_mismatch'] == 1


@pytest.mark.parametrize('mutation', ['unknown_field', 'denominator', 'missing_zero', 'missing_reason', 'quality', 'nan'])
def test_public_report_rejects_privacy_and_arithmetic_drift(inputs, schema, mutation):
    result = report.summarize(*inputs)
    group = result['conditions'][0]
    metric = group['metrics']['first_token_ms']
    if mutation == 'unknown_field':
        result['prompt'] = 'private prompt'
    elif mutation == 'denominator':
        metric['count'] = 1
    elif mutation == 'missing_zero':
        metric.update(count=0, missing=2, missing_reason='required_observation_missing', p50=0, p95=0, minimum=0, maximum=0)
    elif mutation == 'missing_reason':
        metric['missing_reason'] = 'required_observation_missing'
    elif mutation == 'quality':
        group['expected_marker_match'] = 1
    else:
        metric['p50'] = float('nan')
    with pytest.raises(ValueError) as error:
        report.validate_report(result, schema)
    assert 'private prompt' not in str(error.value)


def test_all_synthetic_contexts_use_character_book_selection_and_keep_history():
    class Counter:
        def count_input_tokens(self, values):
            return sum(len(v.content) for v in values)
    card = runner.load_character_card('miori')
    original = copy.deepcopy(card)
    config = runner.resolve_model_settings({})
    for h, m in runner.CASES:
        history, memory = runner.fixtures(h, m)
        built = runner.build_context(card, history, memory, config, Counter())
        assert len(built.character_lore_decisions) == len(card.data.character_book.entries)
        assert built.usage.character > 0
        assert bool(built.usage.history) == bool(h) and bool(built.usage.rag) == bool(m)
        assert built.usage.omitted_history_exchanges == built.usage.omitted_rag_items == 0
    assert card == original


def test_runner_keeps_options_closes_clients_and_does_not_save_content(tmp_path, monkeypatch, capsys):
    inference = tmp_path / 'inference.env'
    inference.write_text('INFERENCE_TARGET_CHAT=ollama/private-model-label\n'
                         'INFERENCE_TARGET_CHAT_OPTIONS_JSON={"temperature":0.2,"think":true}\n'
                         'INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS=7168\n'
                         'INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS=1024\n')
    clients, calls = [], []
    class Adapter:
        def __init__(self, *, base_url):
            self.closed = False
            clients.append(self)
        def estimate_input_tokens(self, request):
            return SimpleNamespace(count=sum(len(v.content) for v in request.messages), external_request_count=0)
        async def stream_text(self, request):
            calls.append(dict(request.options))
            if len(calls) == 2:
                raise ValueError('private exception detail')
            yield 'private generated response marker'
        def close(self):
            self.closed = True
    monkeypatch.setattr(runner, 'OllamaAdapter', Adapter)
    monkeypatch.setattr(runner, 'measurement_revision', lambda root: 'a' * 40)
    output = tmp_path / 'run'
    asyncio.run(runner.run(SimpleNamespace(output=output, inference_env=inference, repetitions=1)))
    assert len(clients) == 12 and all(client.closed for client in clients)
    assert len(calls) == 24
    assert all(option['temperature'] == 0.2 for option in calls)
    assert sum(option['think'] is True for option in calls) == 12
    assert sum(option['think'] is False for option in calls) == 12
    rows = [json.loads(line) for line in (output/'numeric.jsonl').read_text().splitlines()]
    assert len(rows) == 24 and sum(r['outcome'] == 'failure' for r in rows) == 1
    serialized = ''.join(p.read_text() for p in output.iterdir()) + capsys.readouterr().out
    assert 'private' not in serialized
    assert runner.QUESTION not in serialized
    assert all(p.stat().st_mode & 0o077 == 0 for p in output.iterdir())
    before = (output/'numeric.jsonl').read_bytes()
    with pytest.raises(ValueError):
        asyncio.run(runner.run(SimpleNamespace(output=output, inference_env=inference, repetitions=1)))
    assert (output/'numeric.jsonl').read_bytes() == before
