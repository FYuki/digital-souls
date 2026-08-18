import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.memory.admission.contracts import (
    ApprovedMemoryCandidate,
    MemoryCandidate,
    MemoryType,
    PreferencePolarity,
    UserPreferenceValue,
)


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _candidate(text: str = "ユーザーは短い回答を好む") -> ApprovedMemoryCandidate:
    return ApprovedMemoryCandidate(
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object="短い回答",
        ),
        normalized_text=text,
    )


def _context(*, expires_at: datetime | None = None):
    from app.memory.persistence.contracts import (
        FormationMethod,
        MemoryWriteContext,
        TemporalPrecision,
    )

    return MemoryWriteContext(
        formation_method=FormationMethod.EXTRACTED,
        idempotency_key="conversation-1:turn-1:0:extractor-v1",
        effective_at=NOW,
        effective_timezone="Asia/Tokyo",
        temporal_precision=TemporalPrecision.SECOND,
        expires_at=expires_at,
        policy_version="policy-v1",
        classifier_version="classifier-v1",
        model_id="gemma4:e4b",
        model_digest="model-digest",
        prompt_version="prompt-v1",
    )


def _repository(tmp_path: Path):
    from app.memory.persistence.approved_repository import ApprovedMemoryRepository
    from app.memory.persistence.schema import initialize_persona_memory_schema
    from app.runtime_paths import resolve_runtime_paths

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = resolve_runtime_paths(
        {
            "DS_ENVIRONMENT_ID": "test",
            "DS_DATA_DIR": str(tmp_path / "data"),
        },
        repository_root,
    )
    initialize_persona_memory_schema(paths, repository_root)
    uuid_sequence = count(1)
    repository = ApprovedMemoryRepository(
        database_path=paths.persona_memory_sqlite_path,
        clock=lambda: NOW,
        uuid_factory=lambda: UUID(
            f"00000000-0000-4000-8000-{next(uuid_sequence):012d}"
        ),
        outbox_uuid_factory=lambda: UUID(
            f"10000000-0000-4000-8000-{next(uuid_sequence):012d}"
        ),
    )
    return repository, paths.persona_memory_sqlite_path


def _rows(database_path: Path, table: str) -> list[sqlite3.Row]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return list(connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))


def test_save_accepts_an_approved_candidate_and_creates_pending_upsert_atomically(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)

    memory = repository.save(
        character_id="miori",
        candidate=_candidate(),
        context=_context(),
    )

    memory_rows = _rows(database_path, "approved_memories")
    outbox_rows = _rows(database_path, "memory_index_outbox")
    assert memory.character_id == "miori"
    assert memory.normalized_text == "ユーザーは短い回答を好む"
    assert len(memory_rows) == 1
    assert (outbox_rows[0]["memory_id"], outbox_rows[0]["operation"]) == (
        str(memory.id),
        "UPSERT",
    )
    assert outbox_rows[0]["status"] == "PENDING"


def test_save_normalizes_instants_to_utc_and_preserves_temporal_metadata(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import TemporalPrecision

    repository, database_path = _repository(tmp_path)
    chatham_timezone = ZoneInfo("Pacific/Chatham")
    effective_at = datetime(2026, 8, 18, 21, 34, 56, tzinfo=chatham_timezone)
    expires_at = datetime(2026, 8, 25, 21, 34, 56, tzinfo=chatham_timezone)

    repository.save(
        character_id="miori",
        candidate=_candidate(),
        context=replace(
            _context(),
            effective_at=effective_at,
            effective_timezone="Pacific/Chatham",
            temporal_precision=TemporalPrecision.DAY,
            expires_at=expires_at,
        ),
    )

    row = _rows(database_path, "approved_memories")[0]
    assert row["effective_at"] == "2026-08-18T08:49:56.000000Z"
    assert row["expires_at"] == "2026-08-25T08:49:56.000000Z"
    assert row["effective_timezone"] == "Pacific/Chatham"
    assert row["temporal_precision"] == "DAY"


def test_save_rolls_back_memory_when_outbox_insert_fails(tmp_path: Path) -> None:
    repository, database_path = _repository(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_upsert BEFORE INSERT ON memory_index_outbox "
            "WHEN NEW.operation = 'UPSERT' BEGIN SELECT RAISE(ABORT, 'outbox rejected'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        repository.save(
            character_id="miori",
            candidate=_candidate(),
            context=_context(),
        )

    assert _rows(database_path, "approved_memories") == []
    assert _rows(database_path, "memory_index_outbox") == []


def test_content_api_rejects_unapproved_candidates(tmp_path: Path) -> None:
    repository, database_path = _repository(tmp_path)
    unapproved = MemoryCandidate(
        memory_type=MemoryType.USER_PREFERENCE,
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object="短い回答",
        ),
        source=None,
    )

    with pytest.raises(TypeError):
        repository.save(
            character_id="miori",
            candidate=unapproved,
            context=_context(),
        )

    assert _rows(database_path, "approved_memories") == []
    assert _rows(database_path, "memory_index_outbox") == []


def test_correct_updates_content_version_and_creates_another_pending_upsert(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )

    corrected = repository.correct(
        character_id="miori",
        memory_id=memory.id,
        candidate=_candidate("ユーザーは簡潔な回答を好む"),
        context=replace(
            _context(),
            idempotency_key="conversation-2:turn-2:0:extractor-v1",
        ),
    )

    outbox_rows = _rows(database_path, "memory_index_outbox")
    assert corrected.normalized_text == "ユーザーは簡潔な回答を好む"
    assert corrected.content_version == 2
    assert [row["operation"] for row in outbox_rows] == ["UPSERT", "UPSERT"]
    assert [row["status"] for row in outbox_rows] == ["PENDING", "PENDING"]


def test_correct_rolls_back_content_when_outbox_insert_fails(tmp_path: Path) -> None:
    repository, database_path = _repository(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    before = dict(_rows(database_path, "approved_memories")[0])
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_correction BEFORE INSERT ON memory_index_outbox "
            "WHEN NEW.operation = 'UPSERT' BEGIN SELECT RAISE(ABORT, 'outbox rejected'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        repository.correct(
            character_id="miori",
            memory_id=memory.id,
            candidate=_candidate("変更してはいけない"),
            context=replace(_context(), idempotency_key="correction-key"),
        )

    assert dict(_rows(database_path, "approved_memories")[0]) == before
    assert len(_rows(database_path, "memory_index_outbox")) == 1


def test_touch_changes_only_last_user_mentioned_at_and_creates_no_outbox(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    before = dict(_rows(database_path, "approved_memories")[0])
    outbox_count = len(_rows(database_path, "memory_index_outbox"))
    mentioned_at = NOW + timedelta(minutes=5)

    touched = repository.touch(
        character_id="miori",
        memory_id=memory.id,
        candidate=_candidate(),
        mentioned_at=mentioned_at,
    )

    after = dict(_rows(database_path, "approved_memories")[0])
    changed_columns = {key for key in before if before[key] != after[key]}
    assert changed_columns == {"last_user_mentioned_at"}
    assert touched.last_user_mentioned_at == mentioned_at
    assert len(_rows(database_path, "memory_index_outbox")) == outbox_count


def test_touch_rejects_an_unapproved_candidate_without_side_effects(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    before = dict(_rows(database_path, "approved_memories")[0])
    unapproved = MemoryCandidate(
        memory_type=MemoryType.USER_PREFERENCE,
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object="短い回答",
        ),
        source=None,
    )

    with pytest.raises(TypeError):
        repository.touch(
            character_id="miori",
            memory_id=memory.id,
            candidate=unapproved,
            mentioned_at=NOW + timedelta(minutes=5),
        )

    assert dict(_rows(database_path, "approved_memories")[0]) == before
    assert len(_rows(database_path, "memory_index_outbox")) == 1


def test_touch_rejects_a_different_approved_memory_without_side_effects(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    before = dict(_rows(database_path, "approved_memories")[0])

    with pytest.raises(ValueError, match="does not match"):
        repository.touch(
            character_id="miori",
            memory_id=memory.id,
            candidate=_candidate("別の記憶"),
            mentioned_at=NOW + timedelta(minutes=5),
        )

    assert dict(_rows(database_path, "approved_memories")[0]) == before
    assert len(_rows(database_path, "memory_index_outbox")) == 1


def test_active_lookup_excludes_inactive_and_expiring_at_the_current_instant(
    tmp_path: Path,
) -> None:
    repository, _database_path = _repository(tmp_path)
    repository.save(
        character_id="miori",
        candidate=_candidate("期限境界の記憶"),
        context=_context(expires_at=NOW),
    )
    inactive = repository.save(
        character_id="miori",
        candidate=_candidate("無効化された記憶"),
        context=replace(
            _context(),
            idempotency_key="conversation-1:turn-1:1:extractor-v1",
        ),
    )
    repository.save(
        character_id="miori",
        candidate=_candidate("有効な記憶"),
        context=replace(
            _context(expires_at=NOW + timedelta(microseconds=1)),
            idempotency_key="conversation-1:turn-1:2:extractor-v1",
        ),
    )
    repository.deactivate(character_id="miori", memory_id=inactive.id)

    active = repository.list_active(character_id="miori")

    assert [memory.normalized_text for memory in active] == ["有効な記憶"]


def test_character_boundary_applies_to_read_correct_touch_and_delete(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )

    assert repository.get(character_id="other", memory_id=memory.id) is None
    operations = (
        lambda: repository.correct(
            character_id="other",
            memory_id=memory.id,
            candidate=_candidate("変更してはいけない"),
            context=replace(_context(), idempotency_key="other-key"),
        ),
        lambda: repository.touch(
            character_id="other",
            memory_id=memory.id,
            candidate=_candidate(),
            mentioned_at=NOW + timedelta(minutes=5),
        ),
        lambda: repository.hard_delete(character_id="other", memory_id=memory.id),
    )
    for operation in operations:
        try:
            operation()
        except Exception:
            continue

    assert len(_rows(database_path, "approved_memories")) == 1
    assert len(_rows(database_path, "memory_index_outbox")) == 1


def test_hard_delete_removes_memory_and_leaves_pending_delete_outbox(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )

    repository.hard_delete(character_id="miori", memory_id=memory.id)

    outbox_rows = _rows(database_path, "memory_index_outbox")
    assert _rows(database_path, "approved_memories") == []
    assert outbox_rows[-1]["memory_id"] == str(memory.id)
    assert (outbox_rows[-1]["operation"], outbox_rows[-1]["status"]) == (
        "DELETE",
        "PENDING",
    )


def test_hard_delete_rolls_back_when_delete_outbox_insert_fails(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_delete BEFORE INSERT ON memory_index_outbox "
            "WHEN NEW.operation = 'DELETE' BEGIN SELECT RAISE(ABORT, 'outbox rejected'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        repository.hard_delete(character_id="miori", memory_id=memory.id)

    assert len(_rows(database_path, "approved_memories")) == 1
    assert [row["operation"] for row in _rows(database_path, "memory_index_outbox")] == [
        "UPSERT"
    ]


def test_idempotency_key_is_deterministic_from_source_metadata() -> None:
    from app.memory.persistence.contracts import build_conversation_idempotency_key

    key = build_conversation_idempotency_key(
        character_id="miori",
        conversation_id="conversation-1",
        turn_id="turn-1",
        candidate_index=0,
        extractor_version="extractor-v1",
    )

    assert key == build_conversation_idempotency_key(
        character_id="miori",
        conversation_id="conversation-1",
        turn_id="turn-1",
        candidate_index=0,
        extractor_version="extractor-v1",
    )
    assert key != build_conversation_idempotency_key(
        character_id="miori",
        conversation_id="conversation-1",
        turn_id="turn-2",
        candidate_index=0,
        extractor_version="extractor-v1",
    )
