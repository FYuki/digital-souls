"""明示実行時に合成contextの回答値を実Ollamaで照合する。音声・検索受入とは別scope。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys

import pytest
from dotenv import dotenv_values
from jsonschema import Draft202012Validator
from app.voice_baseline import _assert_anonymous

from app.chat_prompt import build_chat_prompt
from app.inference import InferenceCaller, InferenceTarget
from app.inference.diagnostics import collect_diagnostics
from app.inference.runtime import create_inference_runtime
from app.model_settings import MODEL_ENVIRONMENT_KEYS, resolve_model_settings
from app.prompting import CurrentUserMessage, RagContext

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/voice_quality'))
try:
    from compare_context import CASES, SyntheticHistory, fixtures, load_character_card, messages
finally:
    sys.path.remove(str(ROOT / 'scripts/voice_quality'))

pytestmark = pytest.mark.inference_real
QUESTION = (
    'あなたの表記名と、会話で私が伝えた飲み物の好み、記憶にある私の鍵の置き場所を確認します。'
    'JSONオブジェクトだけで回答してください。キーはname、drink、key_locationの3つです。'
    '値は会話または記憶にある短い名称を文字列で書き、分からない値はnullにしてください。'
    '説明文やMarkdownは付けないでください。'
)


def check_answer(text, *, history_count, memory_count):
    # 単一のJSONコードブロックだけを展開する。前後の説明や複数候補は採用しない。
    fence = re.fullmatch(r"\s*```(?:json)?[ \t]*\n([\s\S]*?)\n```\s*", text)
    candidate = fence.group(1) if fence else text
    try:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = value
            return result
        value = json.loads(candidate, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        value = None
    valid = isinstance(value, dict) and set(value) == {'name', 'drink', 'key_location'}
    valid = valid and all(v is None or type(v) is str for v in value.values())
    if not valid:
        return {'strict_format_valid': False, 'format_valid': False, 'name_correct': False, 'history_correct': False, 'memory_correct': False}
    return {'strict_format_valid': fence is None, 'format_valid': True, 'name_correct': value['name'] == '光織',
            'history_correct': value['drink'] == ('麦茶' if history_count else None),
            'memory_correct': value['key_location'] == ('玄関の棚' if memory_count else None)}



def validate_evidence(evidence):
    version = evidence.get('schema_version')
    schema_file = {'1.0': 'voice-quality-context-answer-v1.schema.json',
                   '1.1': 'voice-quality-context-answer-v1.1.schema.json'}.get(version)
    if schema_file is None:
        raise ValueError('unsupported context answer schema')
    schema = json.loads((ROOT/'docs/schemas'/schema_file).read_text())
    if not Draft202012Validator(schema).is_valid(evidence):
        raise ValueError('context answer schema mismatch')
    _assert_anonymous(evidence)
    trials = evidence['trials']
    identities = {(r['case_index'], r['variant'], r['trial_index']) for r in trials}
    if len(identities) != 36 or evidence['recorded'] != len(trials):
        raise ValueError('context answer trial denominator mismatch')
    for row in trials:
        if version == '1.1' and row['strict_format_valid'] and not row['format_valid']:
            raise ValueError('context answer format evidence conflict')
        if row['passed'] != all(row[k] for k in ('request_completed', 'format_valid', 'name_correct', 'history_correct', 'memory_correct')):
            raise ValueError('context answer outcome mismatch')
    passed = sum(r['passed'] for r in trials)
    if evidence['passed'] != passed or evidence['failed'] != 36 - passed:
        raise ValueError('context answer aggregate mismatch')

def test_real_chat_keeps_persona_and_synthetic_context_facts():
    if os.environ.get('RUN_VOICE_CONTEXT_QUALITY') != 'true':
        pytest.skip('RUN_VOICE_CONTEXT_QUALITY=true の明示時だけ実行する')
    source = Path(os.environ['VOICE_CONTEXT_INFERENCE_ENV'])
    output = Path(os.environ['VOICE_CONTEXT_EVIDENCE_PATH'])
    if output.exists():
        pytest.fail('既存のcontext品質証跡は上書きできません')
    # 他のproviderの資格情報・個人設定をruntimeへ持ち込まない。
    values = dotenv_values(source)
    prefixes = tuple('INFERENCE_TARGET_' + name for name in (
        'CHAT', 'PRIVACY', 'MEMORY_EXTRACTION', 'MEMORY_CONSOLIDATION', 'EMBEDDING'))
    environment = {k: v for k, v in values.items() if v is not None and (
        k == 'OLLAMA_BASE_URL' or k in MODEL_ENVIRONMENT_KEYS
        or any(k == prefix or k.startswith(prefix + '_') for prefix in prefixes))}
    if not environment.get('INFERENCE_TARGET_CHAT', '').startswith('ollama/') or environment.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/') not in ('http://localhost:11434', 'http://127.0.0.1:11434'):
        pytest.fail('既存ローカルOllamaのCHAT設定が必要です')
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    if subprocess.run(['git', 'diff', '--name-only', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip():
        pytest.fail('commit済みの測定版が必要です')
    card = load_character_card('miori')
    base_options = json.loads(environment.get('INFERENCE_TARGET_CHAT_OPTIONS_JSON', '{}'))
    records = []

    async def exercise():
        for variant in ('configured', 'thinking_disabled'):
            selected = dict(environment)
            options = dict(base_options)
            if variant == 'thinking_disabled':
                options['think'] = False
            selected['INFERENCE_TARGET_CHAT_OPTIONS_JSON'] = json.dumps(options)
            runtime = create_inference_runtime(selected)
            resolved = runtime.settings.target(InferenceTarget.CHAT)
            assert resolved.max_output_tokens is not None
            config = resolve_model_settings(selected, chat_context_tokens=resolved.max_input_tokens + resolved.max_output_tokens,
                                            assistant_max_generation_tokens=resolved.max_output_tokens)
            class Counter:
                def count_input_tokens(self, values):
                    return runtime.router.estimate_input_tokens(caller=InferenceCaller.CHAT, target=InferenceTarget.CHAT,
                                                                messages=messages(values)).count
            try:
                for case, (h, m) in enumerate(CASES):
                    history, memory = fixtures(h, m)
                    for trial in range(3):
                        row = {'case_index': case, 'variant': variant, 'trial_index': trial}
                        with collect_diagnostics() as collector:
                            try:
                                prompt = build_chat_prompt(character=card.to_character_prompt(), character_book=card.data.character_book,
                                                           rag=RagContext(memory), current_user=CurrentUserMessage(QUESTION),
                                                           history_session=SyntheticHistory(history), config=config, token_counter=Counter())
                                assert prompt.usage.omitted_history_exchanges == prompt.usage.omitted_rag_items == 0
                                chunks = [part async for part in runtime.router.stream_text(caller=InferenceCaller.CHAT,
                                                                                          target=InferenceTarget.CHAT, messages=messages(prompt.messages))]
                                row.update(check_answer(''.join(chunks), history_count=h, memory_count=m))
                                row['request_completed'] = True
                            except Exception:
                                # provider例外・生成本文はpytestの失敗出力にも渡さない。
                                row.update(check_answer('', history_count=h, memory_count=m))
                                row['request_completed'] = False
                            row['provider_internal_timing_unavailable_recorded'] = any(
                                e.name == 'ollama_internal_timing_unavailable' for e in collector.finish())
                        row['passed'] = all(row[k] for k in ('request_completed', 'format_valid', 'name_correct', 'history_correct', 'memory_correct'))
                        records.append(row)
            finally:
                runtime.close()

    asyncio.run(exercise())
    evidence = {'schema_version': '1.1', 'configured_think': base_options.get('think'), 'measurement_scope': 'synthetic_context_answer_conformance', 'measurement_revision': revision,
                'character_card_sha256': hashlib.sha256((ROOT/'characters/miori/miori.card.json').read_bytes()).hexdigest(),
                'expected': 36, 'recorded': len(records), 'passed': sum(r['passed'] for r in records),
                'failed': sum(not r['passed'] for r in records), 'memory_retrieval_exercised': False,
                'natural_speech_style_verified': False, 'response_content_persisted': False, 'trials': records}
    validate_evidence(evidence)
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as handle:
        json.dump(evidence, handle, indent=2)
        handle.write('\n')
    assert evidence['recorded'] == evidence['passed'] == 36, 'contextの回答値が正解と一致しない試行があります'
    assert all(r['provider_internal_timing_unavailable_recorded'] for r in records), 'provider内部時刻の取得可否が未記録です'
