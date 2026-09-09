from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from app.memory.persistence.schema import PERSONA_MEMORY_TABLES
from app.voice_quality_state import inspect_controlled_initial_state


@pytest.fixture
def state_inputs(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    (data / '.environment-identity.json').write_text(json.dumps({'environmentId': 'test'}))
    profile_path = tmp_path / 'profile.json'
    profile = {'effectiveProfile': 'integration-voice', 'derivedEnvironment': {
        'DS_ENVIRONMENT_ID': 'test', 'DS_DATA_DIR': str(data), 'RAG_ENABLED': 'false',
    }}
    profile_path.write_text(json.dumps(profile))
    conversation = str(uuid4())
    with sqlite3.connect(data / 'conversation-history.db') as db:
        db.execute('CREATE TABLE conversations(character_id TEXT, conversation_id TEXT, archived_at TEXT)')
        db.execute('INSERT INTO conversations VALUES (?, ?, NULL)', ('miori', conversation))
        db.execute('CREATE TABLE conversation_turns(content TEXT)')
        db.execute('CREATE TABLE screen_turn_provenance(content TEXT)')
    with sqlite3.connect(data / 'persona-memory.db') as db:
        for name in PERSONA_MEMORY_TABLES:
            db.execute(f'CREATE TABLE "{name}" (content TEXT)')
    for name in ('characters/miori/miori.card.json', 'backend/app/memory/memory_policy.json'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"example":"private fixture"}')
    return {'data_root': data, 'profile_path': profile_path, 'repository_root': tmp_path,
            'conversation_id': conversation}


def test_same_empty_state_is_independent_of_conversation_identity(state_inputs):
    first = inspect_controlled_initial_state(**state_inputs)
    second_id = str(uuid4())
    with sqlite3.connect(state_inputs['data_root'] / 'conversation-history.db') as db:
        db.execute('UPDATE conversations SET conversation_id=?', (second_id,))
    second = inspect_controlled_initial_state(**{**state_inputs, 'conversation_id': second_id})
    assert first == second
    assert 'private fixture' not in json.dumps(first)
    assert len(first['initial_state_hash']) == 64


@pytest.mark.parametrize('table', ['conversation_turns', 'screen_turn_provenance', *sorted(PERSONA_MEMORY_TABLES)])
def test_nonempty_state_is_rejected_instead_of_reset(state_inputs, table):
    database = 'conversation-history.db' if table in ('conversation_turns', 'screen_turn_provenance') else 'persona-memory.db'
    with sqlite3.connect(state_inputs['data_root'] / database) as db:
        db.execute(f'INSERT INTO "{table}" VALUES (?)', ('preserve this data',))
    with pytest.raises(ValueError, match='empty history and memory'):
        inspect_controlled_initial_state(**state_inputs)
    with sqlite3.connect(state_inputs['data_root'] / database) as db:
        assert db.execute(f'SELECT content FROM "{table}"').fetchone() == ('preserve this data',)


def test_changed_character_configuration_changes_state_digest(state_inputs):
    first = inspect_controlled_initial_state(**state_inputs)
    (state_inputs['repository_root'] / 'characters/miori/miori.card.json').write_text('{}')
    assert inspect_controlled_initial_state(**state_inputs)['initial_state_hash'] != first['initial_state_hash']


@pytest.mark.parametrize('change', ['dogfood', 'root', 'rag', 'missing_conversation', 'missing_database'])
def test_mismatched_runtime_state_is_rejected(state_inputs, change):
    if change in ('dogfood', 'root', 'rag'):
        profile = json.loads(state_inputs['profile_path'].read_text())
        key, value = {'dogfood': ('DS_ENVIRONMENT_ID', 'dogfood'), 'root': ('DS_DATA_DIR', '/tmp/wrong-root'),
                      'rag': ('RAG_ENABLED', 'true')}[change]
        profile['derivedEnvironment'][key] = value
        state_inputs['profile_path'].write_text(json.dumps(profile))
    elif change == 'missing_conversation':
        state_inputs['conversation_id'] = str(uuid4())
    else:
        (state_inputs['data_root'] / 'persona-memory.db').unlink()
    with pytest.raises((ValueError, sqlite3.OperationalError)):
        inspect_controlled_initial_state(**state_inputs)
    if change == 'missing_database':
        assert not (state_inputs['data_root'] / 'persona-memory.db').exists()
