from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import UUID

from app.memory.persistence.schema import PERSONA_MEMORY_TABLES
from app.voice_measurement_memory import DISABLE_FORMATION_ENV, POLICY_PATH


def inspect_controlled_initial_state(
    *, data_root: Path, profile_path: Path, repository_root: Path,
    conversation_id: str,
) -> dict[str, object]:
    """専用controlled runnerの空履歴・空記憶を実DBで確認し、本文を出さずhash化する。"""
    UUID(conversation_id)
    identity = json.loads((data_root / '.environment-identity.json').read_text())
    profile = json.loads(profile_path.read_text())
    environment = profile.get('derivedEnvironment', {})
    if identity.get('environmentId') != 'test' or (
        profile.get('effectiveProfile') not in ('integration-voice', 'integration-voice-pcm', 'integration-irodori')
        or environment.get('DS_ENVIRONMENT_ID') != 'test'
        or Path(environment.get('DS_DATA_DIR', '')).resolve() != data_root.resolve()
        or environment.get('RAG_ENABLED') != 'false'
    ):
        raise ValueError('controlled state requires its resolved integration-voice test data root')
    # 設定値だけで無効化済みとみなさず、起動を完了したBackendの記録と照合する。
    requested = environment.get(DISABLE_FORMATION_ENV, 'false')
    if requested not in ('true', 'false'):
        raise ValueError('invalid memory formation isolation setting')
    policy_path = data_root / POLICY_PATH
    memory_policy = None
    if policy_path.exists():
        memory_policy = json.loads(policy_path.read_text())
        if (not isinstance(memory_policy, dict)
                or set(memory_policy) != {'method', 'formation_disabled', 'consolidation_disabled'}
                or memory_policy.get('method') != 'controlled_memory_schedulers_v1'
                or memory_policy.get('formation_disabled') is not (requested == 'true')
                or memory_policy.get('consolidation_disabled') is not (requested == 'true')):
            raise ValueError('memory scheduler policy does not match requested configuration')
    elif requested == 'true':
        raise ValueError('memory scheduler policy receipt is missing')
    # mode=roでDBの作成・変更を禁止する。immutableはWALの最新状態を無視するため使わない。
    history_uri = (data_root / 'conversation-history.db').resolve().as_uri() + '?mode=ro'
    memory_uri = (data_root / 'persona-memory.db').resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(history_uri, uri=True) as history, sqlite3.connect(memory_uri, uri=True) as memory:
        history.execute('BEGIN')
        memory.execute('BEGIN')
        selected = history.execute(
            'SELECT archived_at FROM conversations WHERE character_id=? AND conversation_id=?',
            ('miori', conversation_id),
        ).fetchall()
        if selected != [(None,)]:
            raise ValueError('controlled trial requires an existing active conversation')
        # 前試行の残存も検出する。UI設定・会話の生成時刻はprompt初期状態に含めない。
        counts = {
            'conversation_turns': history.execute('SELECT count(*) FROM conversation_turns').fetchone()[0],
            'screen_turn_provenance': history.execute('SELECT count(*) FROM screen_turn_provenance').fetchone()[0],
        }
        for table in sorted(PERSONA_MEMORY_TABLES):
            counts[table] = memory.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        if any(counts.values()):
            raise ValueError('controlled trial requires empty history and memory stores')
        schema_versions = {
            'history': history.execute('PRAGMA user_version').fetchone()[0],
            'memory': memory.execute('PRAGMA user_version').fetchone()[0],
        }
    files = (
        'characters/miori/miori.card.json',
        'backend/app/memory/memory_policy.json',
    )
    evidence: dict[str, object] = {
        'method': 'sqlite_empty_state_and_configuration_v1',
        'rag_enabled': False,
        'row_counts': counts,
        'schema_versions': schema_versions,
        'configuration_sha256': {
            name: hashlib.sha256((repository_root / name).read_bytes()).hexdigest()
            for name in files
        },
    }
    if memory_policy is not None:
        evidence['memory_scheduler_policy'] = memory_policy
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'initial_state_hash': digest, 'evidence': evidence}


def main() -> None:
    parser = argparse.ArgumentParser(description='controlled trial開始前の実DB状態を検証する')
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--repository-root', type=Path, required=True)
    parser.add_argument('--conversation-id', required=True)
    args = parser.parse_args()
    print(json.dumps(inspect_controlled_initial_state(
        data_root=args.data_root, profile_path=args.profile,
        repository_root=args.repository_root, conversation_id=args.conversation_id,
    ), sort_keys=True))


if __name__ == '__main__':
    main()
