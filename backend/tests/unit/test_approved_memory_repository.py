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
        MemorySourceInput,
        MemorySourceType,
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
        sources=(
            MemorySourceInput(
                source_type=MemorySourceType.CONVERSATION_TURN,
                source_provider_id="core",
                source_ref="conversation-1:turn-1",
            ),
        ),
    )


def _repository(tmp_path: Path, *, connection_factory=sqlite3.connect):
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
        connection_factory=connection_factory,
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
    assert memory.provider_id == "core"
    assert memory.memory_kind == "SEMANTIC"
    assert memory.normalized_text == "ユーザーは短い回答を好む"
    assert memory.policy_version == "policy-v1"
    assert len(memory_rows) == 1
    assert (outbox_rows[0]["memory_id"], outbox_rows[0]["operation"]) == (
        str(memory.id),
        "UPSERT",
    )
    assert outbox_rows[0]["status"] == "PENDING"


def test_save_retry_returns_the_existing_memory_without_duplicate_rows(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)

    memories = [
        repository.save(
            character_id="miori",
            candidate=_candidate(),
            context=_context(),
        )
        for _ in range(3)
    ]

    assert [memory.id for memory in memories] == [memories[0].id] * 3
    assert len(_rows(database_path, "approved_memories")) == 1
    assert len(_rows(database_path, "memory_index_outbox")) == 1


def test_save_retry_uses_stable_row_names_with_qualified_column_pragmas(
    tmp_path: Path,
) -> None:
    def connection_factory(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA full_column_names = ON")
        connection.execute("PRAGMA short_column_names = OFF")
        return connection

    repository, _database_path = _repository(
        tmp_path,
        connection_factory=connection_factory,
    )

    first = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    retried = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )

    assert retried == first


def test_same_idempotency_key_is_independent_between_characters(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)

    miori = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    other = repository.save(
        character_id="other", candidate=_candidate(), context=_context()
    )

    assert miori.id != other.id
    assert {row["character_id"] for row in _rows(database_path, "approved_memories")} == {
        "miori",
        "other",
    }
    assert len(_rows(database_path, "memory_index_outbox")) == 2


def test_save_persists_typed_sources_and_lineage_in_the_same_transaction(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import (
        MemoryLineageInput,
        MemoryLineageRelation,
        MemorySourceInput,
        MemorySourceType,
    )

    repository, database_path = _repository(tmp_path)
    original = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    derived_context = replace(
        _context(),
        idempotency_key="consolidation-1",
        sources=(
            MemorySourceInput(
                source_type=MemorySourceType.PROVIDER_RECORD,
                source_provider_id="temporary:recipe",
                source_ref="recipe-1",
            ),
        ),
        lineage=(
            MemoryLineageInput(
                related_memory_id=original.id,
                relation=MemoryLineageRelation.CONSOLIDATED_FROM,
            ),
        ),
    )

    derived = repository.save(
        character_id="miori",
        candidate=_candidate("統合された記憶"),
        context=derived_context,
    )

    source = _rows(database_path, "memory_sources")[-1]
    lineage = _rows(database_path, "memory_lineage")[0]
    assert (source["memory_id"], source["source_type"], source["source_provider_id"]) == (
        str(derived.id),
        "PROVIDER_RECORD",
        "temporary:recipe",
    )
    assert (lineage["memory_id"], lineage["related_memory_id"], lineage["relation"]) == (
        str(derived.id),
        str(original.id),
        "CONSOLIDATED_FROM",
    )


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


def test_save_rolls_back_memory_and_receipt_when_source_insert_fails(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_source BEFORE INSERT ON memory_sources "
            "BEGIN SELECT RAISE(ABORT, 'source rejected'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        repository.save(
            character_id="miori",
            candidate=_candidate(),
            context=_context(),
        )

    assert _rows(database_path, "approved_memories") == []
    assert _rows(database_path, "memory_write_receipts") == []
    assert _rows(database_path, "memory_sources") == []
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
    from app.memory.persistence.contracts import MemorySourceInput, MemorySourceType

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
            sources=(
                MemorySourceInput(
                    source_type=MemorySourceType.CONVERSATION_TURN,
                    source_provider_id="core",
                    source_ref="conversation-2:turn-2",
                ),
            ),
        ),
    )

    outbox_rows = _rows(database_path, "memory_index_outbox")
    source_rows = _rows(database_path, "memory_sources")
    assert corrected.normalized_text == "ユーザーは簡潔な回答を好む"
    assert corrected.content_version == 2
    assert [row["operation"] for row in outbox_rows] == ["UPSERT", "UPSERT"]
    assert [row["status"] for row in outbox_rows] == ["PENDING", "PENDING"]
    assert [row["source_ref"] for row in source_rows] == [
        "conversation-1:turn-1",
        "conversation-2:turn-2",
    ]


def test_save_retry_after_correction_returns_the_corrected_memory_without_duplicates(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)
    original_context = _context()
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=original_context
    )
    repository.correct(
        character_id="miori",
        memory_id=memory.id,
        candidate=_candidate("ユーザーは簡潔な回答を好む"),
        context=replace(original_context, idempotency_key="correction-key"),
    )

    retried = repository.save(
        character_id="miori",
        candidate=_candidate(),
        context=original_context,
    )

    assert retried.id == memory.id
    assert retried.normalized_text == "ユーザーは簡潔な回答を好む"
    assert retried.content_version == 2
    assert len(_rows(database_path, "approved_memories")) == 1
    assert len(_rows(database_path, "memory_index_outbox")) == 2


def test_correct_preserves_the_save_key_and_records_the_correction_key(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)
    original_context = _context()
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=original_context
    )

    repository.correct(
        character_id="miori",
        memory_id=memory.id,
        candidate=_candidate("ユーザーは簡潔な回答を好む"),
        context=replace(original_context, idempotency_key="correction-key"),
    )

    row = _rows(database_path, "approved_memories")[0]
    assert row["idempotency_key"] == original_context.idempotency_key
    assert row["last_write_idempotency_key"] == "correction-key"


def test_correct_retry_remains_idempotent_after_a_later_correction(
    tmp_path: Path,
) -> None:
    repository, database_path = _repository(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    first_context = replace(_context(), idempotency_key="correction-key-1")
    second_context = replace(_context(), idempotency_key="correction-key-2")

    first = repository.correct(
        character_id="miori",
        memory_id=memory.id,
        candidate=_candidate("最初の訂正"),
        context=first_context,
    )
    retried = repository.correct(
        character_id="miori",
        memory_id=memory.id,
        candidate=_candidate("再試行で上書きしてはいけない"),
        context=first_context,
    )
    second = repository.correct(
        character_id="miori",
        memory_id=memory.id,
        candidate=_candidate("後続の訂正"),
        context=second_context,
    )
    retried_after_second = repository.correct(
        character_id="miori",
        memory_id=memory.id,
        candidate=_candidate("過去キーで上書きしてはいけない"),
        context=first_context,
    )

    assert (first.content_version, retried.content_version) == (2, 2)
    assert retried.normalized_text == "最初の訂正"
    assert second.content_version == 3
    assert retried_after_second.content_version == 3
    assert retried_after_second.normalized_text == "後続の訂正"
    assert len(_rows(database_path, "memory_write_receipts")) == 3
    assert len(_rows(database_path, "memory_index_outbox")) == 3


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
    memory_rows_before = [
        dict(row) for row in _rows(database_path, "approved_memories")
    ]
    outbox_rows_before = [
        dict(row) for row in _rows(database_path, "memory_index_outbox")
    ]

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
    )
    for operation in operations:
        with pytest.raises(LookupError):
            operation()
    repository.hard_delete(character_id="other", memory_id=memory.id)

    assert [dict(row) for row in _rows(database_path, "approved_memories")] == (
        memory_rows_before
    )
    assert [dict(row) for row in _rows(database_path, "memory_index_outbox")] == (
        outbox_rows_before
    )


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


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("character_id", "mi:ori"),
        ("conversation_id", "conversation:1"),
        ("turn_id", "turn:1"),
        ("extractor_version", "extractor:v1"),
    ),
)
def test_idempotency_key_rejects_delimiters_in_string_components(
    field_name: str,
    value: str,
) -> None:
    from app.memory.persistence.contracts import build_conversation_idempotency_key

    arguments = {
        "character_id": "miori",
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "candidate_index": 0,
        "extractor_version": "extractor-v1",
    }
    arguments[field_name] = value

    with pytest.raises(ValueError):
        build_conversation_idempotency_key(**arguments)


def test_idempotency_key_rejects_ambiguous_component_boundaries() -> None:
    from app.memory.persistence.contracts import build_conversation_idempotency_key

    ambiguous_inputs = (
        {"character_id": "a:b", "conversation_id": "c"},
        {"character_id": "a", "conversation_id": "b:c"},
    )

    for component_values in ambiguous_inputs:
        with pytest.raises(ValueError):
            build_conversation_idempotency_key(
                **component_values,
                turn_id="turn-1",
                candidate_index=0,
                extractor_version="extractor-v1",
            )


def test_management_list_filters_character_provider_and_status_without_expiry(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import MemoryStatus

    repository, database_path = _repository(tmp_path)
    active = repository.save(
        character_id="miori",
        candidate=_candidate("active"),
        context=replace(
            _context(), idempotency_key="active-key", expires_at=NOW - timedelta(days=1)
        ),
    )
    inactive = repository.save(
        character_id="miori",
        candidate=_candidate("inactive"),
        context=replace(_context(), idempotency_key="inactive-key"),
    )
    repository.save(
        character_id="other",
        candidate=_candidate("other"),
        context=replace(_context(), idempotency_key="other-key"),
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE approved_memories SET status = 'INACTIVE' WHERE id = ?",
            (str(inactive.id),),
        )

    assert repository.list_by_provider(
        character_id="miori", provider_id="core", status=MemoryStatus.ACTIVE
    ) == [active]
    assert [
        memory.id
        for memory in repository.list_by_provider(
            character_id="miori", provider_id="core", status=MemoryStatus.INACTIVE
        )
    ] == [inactive.id]


def test_management_detail_contains_sources_lineage_and_content_version(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import (
        MemoryLineageInput,
        MemoryLineageRelation,
        MemorySourceInput,
        MemorySourceType,
    )

    repository, _database_path = _repository(tmp_path)
    original = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    source = MemorySourceInput(
        source_type=MemorySourceType.PROVIDER_RECORD,
        source_provider_id="temporary:recipe",
        source_ref="recipe-1",
    )
    lineage = MemoryLineageInput(
        related_memory_id=original.id,
        relation=MemoryLineageRelation.CONSOLIDATED_FROM,
    )
    derived = repository.save(
        character_id="miori",
        candidate=_candidate("統合された記憶"),
        context=replace(
            _context(),
            idempotency_key="derived-key",
            sources=(source,),
            lineage=(lineage,),
        ),
    )

    detail = repository.get_detail(
        character_id="miori", provider_id="core", memory_id=derived.id
    )

    assert detail is not None
    assert detail.memory.id == derived.id
    assert detail.memory.normalized_text == "統合された記憶"
    assert detail.memory.content_version == 1
    assert detail.sources == (source,)
    assert detail.lineage == (lineage,)
    assert repository.get_detail(
        character_id="other", provider_id="core", memory_id=derived.id
    ) is None


def test_hard_delete_is_repeatable_and_removes_plaintext_from_sqlite_files(
    tmp_path: Path,
) -> None:
    marker = "PERSONA_SECRET_MARKER_12"
    repository, database_path = _repository(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(marker), context=_context()
    )

    repository.hard_delete(character_id="miori", memory_id=memory.id)
    repository.hard_delete(character_id="miori", memory_id=memory.id)

    assert repository.get(character_id="miori", memory_id=memory.id) is None
    delete_rows = [
        row
        for row in _rows(database_path, "memory_index_outbox")
        if row["operation"] == "DELETE"
    ]
    assert len(delete_rows) == 1
    assert delete_rows[0]["status"] == "PENDING"
    assert marker.encode() not in database_path.read_bytes()
    wal_path = database_path.with_name(f"{database_path.name}-wal")
    assert not wal_path.exists() or marker.encode() not in wal_path.read_bytes()


def test_hard_delete_immediately_excludes_memory_from_management_reads(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import MemoryStatus

    repository, _database_path = _repository(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )

    repository.hard_delete(character_id="miori", memory_id=memory.id)

    assert repository.list_by_provider(
        character_id="miori", provider_id="core", status=MemoryStatus.ACTIVE
    ) == []
    assert repository.get_detail(
        character_id="miori", provider_id="core", memory_id=memory.id
    ) is None
