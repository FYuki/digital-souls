from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'voice_context_quality', Path(__file__).resolve().parents[1] / 'integration/test_voice_context_quality_integration.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_known_values_are_checked_by_meaningful_fields_not_marker_presence():
    value = '{"name":"光織","drink":"麦茶ではありません","key_location":"玄関の棚"}'
    result = module.check_answer(value, history_count=2, memory_count=1)
    assert result['format_valid'] and result['name_correct'] and result['memory_correct']
    assert not result['history_correct']


def test_absent_context_requires_null_instead_of_invented_known_value():
    value = '{"name":"光織","drink":"麦茶","key_location":"玄関の棚"}'
    result = module.check_answer(value, history_count=0, memory_count=0)
    assert not result['history_correct'] and not result['memory_correct']
    unknown = '{"name":"光織","drink":null,"key_location":null}'
    assert all(module.check_answer(unknown, history_count=0, memory_count=0).values())


@pytest.mark.parametrize('value', [
    'not json', '[]', 'null', '{"name":"光織"}',
    '{"name":"光織","drink":false,"key_location":null}',
    '{"name":"光織","drink":null,"key_location":null,"extra":"private"}',
    '{"name":"別名","name":"光織","drink":null,"key_location":null}',
])
def test_invalid_or_ambiguous_answer_formats_do_not_pass(value):
    assert not any(module.check_answer(value, history_count=0, memory_count=0).values())


@pytest.mark.parametrize('mutation', [None, 'duplicate', 'counts', 'outcome', 'prompt'])
def test_evidence_schema_and_counts_keep_all_trials(mutation):
    rows = [dict(case_index=c,variant=v,trial_index=i,format_valid=True,name_correct=True,history_correct=True,
                 memory_correct=True,request_completed=True,provider_internal_timing_unavailable_recorded=True,passed=True)
            for c in range(6) for v in ('configured','thinking_disabled') for i in range(3)]
    evidence = dict(schema_version='1.0',configured_think=None,measurement_scope='synthetic_context_answer_conformance',
                    measurement_revision='a'*40,character_card_sha256='b'*64,expected=36,recorded=36,passed=36,failed=0,
                    memory_retrieval_exercised=False,natural_speech_style_verified=False,response_content_persisted=False,trials=rows)
    if mutation == 'duplicate':rows[-1]=rows[0]
    elif mutation == 'counts':evidence['passed']=35
    elif mutation == 'outcome':rows[0]['passed']=False
    elif mutation == 'prompt':evidence['prompt']='private'
    if mutation is None:
        module.validate_evidence(evidence)
    else:
        with pytest.raises(ValueError):module.validate_evidence(evidence)


@pytest.mark.parametrize('wrapper', ['```json\n%s\n```', '```\n%s\n```'])
def test_single_json_fence_preserves_content_checks_and_records_format_failure(wrapper):
    value = '{"name":"光織","drink":"麦茶","key_location":null}'
    result = module.check_answer(wrapper % value, history_count=2, memory_count=0)
    assert not result['strict_format_valid']
    assert all(result[k] for k in ('format_valid', 'name_correct', 'history_correct', 'memory_correct'))
    negative = module.check_answer(wrapper % value.replace('麦茶', '麦茶ではない'), history_count=2, memory_count=0)
    assert not negative['history_correct']


@pytest.mark.parametrize('wrapper', ['説明\n```json\n%s\n```', '```json\n%s\n```\n説明',
                                     '```json\n%s\n```\n```json\n{}\n```'])
def test_prose_or_multiple_json_blocks_are_not_silently_discarded(wrapper):
    value = '{"name":"光織","drink":null,"key_location":null}'
    assert not module.check_answer(wrapper % value, history_count=0, memory_count=0)['format_valid']


@pytest.mark.parametrize('contradiction', [False, True])
def test_new_schema_keeps_strict_format_separate_from_content(contradiction):
    rows = [dict(case_index=c, variant=v, trial_index=i, strict_format_valid=False,
                 format_valid=True, name_correct=True, history_correct=True, memory_correct=True,
                 request_completed=True, provider_internal_timing_unavailable_recorded=True, passed=True)
            for c in range(6) for v in ('configured', 'thinking_disabled') for i in range(3)]
    evidence = dict(schema_version='1.1', configured_think=None, measurement_scope='synthetic_context_answer_conformance',
                    measurement_revision='a'*40, character_card_sha256='b'*64, expected=36, recorded=36, passed=36, failed=0,
                    memory_retrieval_exercised=False, natural_speech_style_verified=False, response_content_persisted=False, trials=rows)
    if contradiction:
        rows[0].update(strict_format_valid=True, format_valid=False)
        with pytest.raises(ValueError, match='format evidence conflict'):
            module.validate_evidence(evidence)
    else:
        module.validate_evidence(evidence)
