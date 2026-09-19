import json
import sqlite3
from uuid import uuid4

import pytest

from app.memory.chroma_store import EmbeddingFingerprint
from app.memory.episodic.response_provenance import record_response
from app.voice_measurement_memory import record_memory_policy
from app.voice_quality_reference_state import (
    FIXTURE, MEMORY_IDS, PROFILE, inspect_initial, inspect_references, prepare,
)
from app.voice_quality_state import inspect_controlled_initial_state


@pytest.fixture
def reference(tmp_path, monkeypatch):
    root = tmp_path / "frontend/test-results/livekit-quality/runs/350-memory-reference-unit/runtime-data"
    root.parent.mkdir(parents=True)
    receipt = prepare(root, tmp_path)
    for name in ("characters/miori/miori.card.json", "backend/app/memory/memory_policy.json"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
    conversation = str(uuid4())
    with sqlite3.connect(root / "conversation-history.db") as db:
        db.execute("CREATE TABLE conversations(character_id TEXT, conversation_id TEXT, archived_at TEXT)")
        db.execute("INSERT INTO conversations VALUES ('miori', ?, NULL)", (conversation,))
        db.execute("CREATE TABLE conversation_turns(content TEXT)")
        db.execute("CREATE TABLE screen_turn_provenance(content TEXT)")
    with sqlite3.connect(root / "persona-memory.db") as db:
        db.execute("UPDATE memory_index_outbox SET status='COMPLETED'")
    record_memory_policy(root, disabled=True)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"effectiveProfile": PROFILE, "derivedEnvironment": {
        "DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(root), "RAG_ENABLED": "true",
        "VOICE_MEASUREMENT_DISABLE_MEMORY_FORMATION": "true",
    }}))
    monkeypatch.setattr("app.memory.chroma_store.active_memory_index_fingerprint",
                        lambda *args: EmbeddingFingerprint("ollama", "nomic-embed-text", 768))
    monkeypatch.setattr("app.memory.chroma_store.list_memory_index_ids",
                        lambda **kwargs: {str(value) for value in MEMORY_IDS})
    return dict(data_root=root, repository_root=tmp_path, profile_path=profile, conversation_id=conversation)


def test_fixed_content_and_actual_index_required(reference):
    first = inspect_initial(**reference)
    assert first["evidence"]["memory_count"] == 3
    assert first["evidence"]["rag_enabled"] is True
    assert not any(text in json.dumps(first, ensure_ascii=False) for _, text in FIXTURE)
    assert inspect_initial(**reference) == first
    # 第1段階の空状態検査は参照ありを拒否する。
    with pytest.raises(ValueError):
        inspect_controlled_initial_state(**reference)


def test_preparation_refuses_existing_root_and_outside_trial(reference):
    root, repo = reference["data_root"], reference["repository_root"]
    before = (root / "persona-memory.db").read_bytes()
    with pytest.raises(ValueError, match="never overwrites"):
        prepare(root, repo)
    assert (root / "persona-memory.db").read_bytes() == before
    with pytest.raises(ValueError, match="dedicated"):
        prepare(repo / "dogfood-data", repo)
    target = root.parent.parent / "350-memory-reference-link"
    target.symlink_to(root.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        prepare(target / "runtime-data", repo)


@pytest.mark.parametrize("mutation,reason", [
    ("UPDATE approved_memories SET normalized_text='changed'", "content changed"),
    ("UPDATE memory_index_outbox SET status='FAILED'", "index preparation"),
    ("INSERT INTO memory_response_origins VALUES ('miori','x','y','z')", "unexpected"),
])
def test_mutated_memory_or_unprepared_index_rejected(reference, mutation, reason):
    with sqlite3.connect(reference["data_root"] / "persona-memory.db") as db:
        db.execute(mutation)
    with pytest.raises(ValueError, match=reason):
        inspect_initial(**reference)


@pytest.mark.parametrize("key,value", [
    ("RAG_ENABLED", "false"), ("DS_ENVIRONMENT_ID", "dogfood"),
    ("VOICE_MEASUREMENT_DISABLE_MEMORY_FORMATION", "false"),
])
def test_isolation_cannot_be_disabled(reference, key, value):
    path = reference["profile_path"]
    profile = json.loads(path.read_text())
    profile["derivedEnvironment"][key] = value
    path.write_text(json.dumps(profile))
    with pytest.raises(ValueError, match="isolation mismatch"):
        inspect_initial(**reference)


def test_missing_index_and_nonempty_history_rejected(reference, monkeypatch):
    monkeypatch.setattr("app.memory.chroma_store.list_memory_index_ids", lambda **kwargs: set())
    with pytest.raises(ValueError, match="index contents"):
        inspect_initial(**reference)
    with sqlite3.connect(reference["data_root"] / "conversation-history.db") as db:
        db.execute("INSERT INTO conversation_turns VALUES ('synthetic')")
    with pytest.raises(ValueError, match="empty history"):
        inspect_initial(**reference)


@pytest.mark.parametrize("referenced", [False, True])
def test_real_prompt_provenance_distinguishes_no_reference(reference, referenced):
    from uuid import UUID
    with sqlite3.connect(reference["data_root"] / "persona-memory.db") as db:
        record_response(db, character_id="miori", conversation_id=UUID(reference["conversation_id"]),
            turn_id=uuid4(), references=[(MEMORY_IDS[0], 1)] if referenced else [], created_at="2026-09-19")
    args = {k:v for k,v in reference.items() if k != "profile_path"}
    result = inspect_references(**args)
    assert result["referenced_count"] == int(referenced)
    assert result["reference_outcome"] == ("referenced" if referenced else "not_referenced")
    assert "memory_id" not in json.dumps(result)


def test_missing_or_wrong_response_provenance_rejected(reference):
    args = {k:v for k,v in reference.items() if k != "profile_path"}
    with pytest.raises(ValueError, match="one response"):
        inspect_references(**args)
    with sqlite3.connect(reference["data_root"] / "persona-memory.db") as db:
        db.execute("INSERT INTO memory_response_origins VALUES ('miori',?,?, 'now')",
                   (reference["conversation_id"], "turn"))
        db.execute("INSERT INTO memory_response_dependencies VALUES ('miori','turn',?,2)", (str(MEMORY_IDS[0]),))
    with pytest.raises(ValueError, match="unexpected memory"):
        inspect_references(**args)
