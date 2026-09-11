"""固定した合成contextで通常のPromptBuilder/OllamaAdapterを比較する診断。

他の音声測定が終了してから実行する。音声100件や記憶検索自体の受入とは別のscope。
本文は保存しない。固定した合成事実の出現だけを数値JSONLへ記録する。
これは人格・記憶の品質全体を証明する判定器ではない。
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import sys
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.characters.loader import load_character_card
from app.chat_prompt import build_chat_prompt
from app.conversation_history.prompt_history import RestoredHistoryTurn
from app.inference.adapters.ollama import OllamaAdapter
from app.inference.contracts import InferenceMessage, TextGenerationRequest, TokenEstimateRequest
from app.inference.diagnostics import collect_diagnostics, diagnostic, estimate_diagnostics
from app.prompting import CurrentUserMessage, RagContext, RagItem
from app.model_settings import MODEL_ENVIRONMENT_KEYS, resolve_model_settings
from dotenv import dotenv_values
from run_pilot import measurement_revision
from app.inference.registry import default_provider_registry

CASES = ((0, 0), (2, 0), (8, 0), (0, 1), (0, 4), (8, 4))
QUESTION = 'あなたの名前と、会話で伝えた私の飲み物の好み、記憶にある鍵の置き場所を短く教えて。分からないことは分からないと答えて。'


def fixtures(history_count, memory_count):
    history = [RestoredHistoryTurn('私の好きな飲み物は麦茶です。', '麦茶が好きなんだね。', True)]
    history += [RestoredHistoryTurn(f'今日は読書のメモを{i}ページ書きました。', '少しずつ読書の記録が増えているね。', True) for i in range(1, 8)]
    memories = ['利用者は鍵を玄関の棚に置いている。', '利用者は雨の日に読書を楽しむ。',
                '利用者は散歩の途中で空を眺めるのが好きだ。', '利用者は週末に机を片付ける。']
    return tuple(history[:history_count]), tuple(RagItem(v, raw_distance=0.1 + i * 0.01) for i, v in enumerate(memories[:memory_count]))


def messages(values):
    return tuple(InferenceMessage(v.role.value, v.content) for v in values)


class SyntheticHistory:
    def __init__(self, turns):
        self.turns = turns

    def prompt_turns(self, *, max_completed_turns, page_size):
        return iter(self.turns[:max_completed_turns])


def build_context(card, history, memory, config, counter):
    return build_chat_prompt(character=card.to_character_prompt(), character_book=card.data.character_book,
                             rag=RagContext(memory), current_user=CurrentUserMessage(QUESTION),
                             history_session=SyntheticHistory(history), config=config, token_counter=counter)


def private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w')


async def run(args):
    output = Path(args.output).resolve()
    if Path('/tmp') not in output.parents or output.exists():
        raise ValueError('新しい/tmp出力ディレクトリが必要です')
    revision = measurement_revision(ROOT)
    env = dotenv_values(args.inference_env)
    reference = env.get('INFERENCE_TARGET_CHAT', '')
    endpoint = env.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')
    if not reference.startswith('ollama/') or endpoint.rstrip('/') not in ('http://127.0.0.1:11434', 'http://localhost:11434'):
        raise ValueError('既存ローカルOllamaのCHAT設定が必要です')
    options = json.loads(env.get('INFERENCE_TARGET_CHAT_OPTIONS_JSON') or '{}')
    if not isinstance(options, dict) or set(options) - {'temperature', 'top_p', 'seed', 'think'}:
        raise ValueError('CHAT optionsを検証できません')
    default_provider_registry().descriptor('ollama').validate_options(options)
    input_limit = int(env['INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS'])
    output_limit = int(env['INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS'])
    timeout = float(env.get('INFERENCE_TARGET_CHAT_TIMEOUT_SECONDS') or '30')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('CHAT timeoutは正の有限値である必要があります')
    card = load_character_card('miori')
    config = resolve_model_settings({key: env[key] for key in MODEL_ENVIRONMENT_KEYS if env.get(key) is not None},
                                    chat_context_tokens=input_limit + output_limit,
                                    assistant_max_generation_tokens=output_limit)
    card_hash = hashlib.sha256((ROOT/'characters/miori/miori.card.json').read_bytes()).hexdigest()
    output.mkdir(mode=0o700)
    with private_file(output/'plan.json') as f:
        json.dump({'scope': 'synthetic_prompt_context_comparison', 'measurement_revision': revision,
                   'character_card_sha256': card_hash, 'cases': CASES, 'repetitions': args.repetitions,
                   'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   'warmup_per_case_and_variant': 1, 'memory_retrieval_exercised': False,
                   'normal_options_think': options.get('think'), 'full_persona_maintained': True,
                   'character_book_selection_exercised': True,
                   'input_limit': input_limit, 'output_limit': output_limit}, f, ensure_ascii=False)
    with private_file(output/'numeric.jsonl') as numeric:
        for case_index, (h, m) in enumerate(CASES):
            history, memory = fixtures(h, m)
            # 順序の片寄りを減らすが、warm cacheを消去したと装わない。
            variants = ('configured', 'thinking_disabled') if case_index % 2 == 0 else ('thinking_disabled', 'configured')
            for variant in variants:
                selected_options = dict(options)
                if variant == 'thinking_disabled':
                    selected_options['think'] = False
                adapter = OllamaAdapter(base_url=endpoint)
                class Counter:
                    def count_input_tokens(self, values):
                        started = perf_counter_ns()
                        with estimate_diagnostics():
                            result = adapter.estimate_input_tokens(TokenEstimateRequest(
                                messages=messages(values), model_id=reference.split('/', 1)[1], options=selected_options,
                                max_input_tokens=input_limit, timeout_seconds=timeout,
                                context_window_tokens=input_limit + output_limit, allow_cached_exact_result=True))
                        diagnostic('token_estimate_requests', result.external_request_count)
                        diagnostic('token_estimate_total_ms', (perf_counter_ns() - started) / 1e6)
                        return result.count
                try:
                    for index in range(args.repetitions + 1):
                        row = {'case_index': case_index, 'history_count': h, 'memory_count': m,
                               'variant': variant, 'trial_index': index, 'warmup': index == 0}
                        text_parts = []
                        with collect_diagnostics() as collector:
                            start = perf_counter_ns()
                            try:
                                diagnostic('prompt_preparation_started')
                                prompt = build_context(card, history, memory, config, Counter())
                                diagnostic('prompt_preparation_completed')
                                row['prompt_usage'] = asdict(prompt.usage)
                                if (prompt.usage.omitted_history_exchanges or prompt.usage.omitted_rag_items
                                        or prompt.usage.omitted_character_lore_entries):
                                    raise ValueError('固定contextが削除されました')
                                request = TextGenerationRequest(messages(prompt.messages), reference.split('/', 1)[1],
                                                                selected_options, input_limit, output_limit, timeout)
                                diagnostic('llm_request_started')
                                async for part in adapter.stream_text(request):
                                    if not text_parts:
                                        diagnostic('llm_first_token')
                                    text_parts.append(part)
                                diagnostic('llm_stream_completed')
                                row['outcome'] = 'success'
                            except Exception as error:
                                row['outcome'] = 'failure'
                                row['failure_type'] = type(error).__name__
                            row['diagnostics'] = [dict(name=e.name, elapsed_ms=(e.timestamp_ns-start)/1e6, value=e.value)
                                                  for e in collector.finish()]
                        response = ''.join(text_parts)
                        row['quality_observations'] = {
                            'character_name_present': '光織' in response or 'みおり' in response,
                            'history_fact_present': '麦茶' in response,
                            'memory_fact_present': '玄関' in response and '棚' in response,
                            'nonempty_response': bool(response.strip()),
                        }
                        numeric.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n'); numeric.flush()
                        print(json.dumps({k: row[k] for k in ('case_index', 'variant', 'trial_index', 'outcome')}), flush=True)
                finally:
                    adapter.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--inference-env', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, choices=range(1, 6), default=3)
    args = parser.parse_args()
    try:
        lock_path = ROOT / 'frontend/test-results/livekit-quality/cohorts/.execution.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            asyncio.run(run(args))
    except Exception as error:
        print(json.dumps({'context_matrix': 'failed', 'error_type': type(error).__name__}), file=sys.stderr)
        raise SystemExit(2)
