import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from uuid import UUID

import pytest


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _repository(tmp_path: Path):
    return _repository_with_clock(tmp_path, clock=lambda: NOW)


def _repository_with_clock(tmp_path: Path, *, clock):
    from app.memory.persistence.schema import initialize_persona_memory_schema
    from app.memory.persistence.temporary_repository import (
        TemporaryProviderRecordRepository,
    )
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
    identifiers = count(1)
    repository = TemporaryProviderRecordRepository(
        database_path=paths.persona_memory_sqlite_path,
        clock=clock,
        uuid_factory=lambda: UUID(
            f"00000000-0000-4000-8000-{next(identifiers):012d}"
        ),
    )
    return repository, paths.persona_memory_sqlite_path


@pytest.mark.parametrize(
    "provider_id", ["temporary:agriculture", "temporary:recipe"]
)
def test_temporary_repository_accepts_only_the_two_temporary_providers(
    provider_id: str, tmp_path: Path
) -> None:
    from app.memory.persistence.contracts import TemporaryProviderRecordInput

    repository, _database_path = _repository(tmp_path)

    saved = repository.save(
        character_id="miori",
        record=TemporaryProviderRecordInput(
            provider_id=provider_id,
            source_ref="source-1",
            record_type="ENTRY",
            structured_value='{"value":"synthetic"}',
            effective_at=NOW,
        ),
    )

    assert saved.character_id == "miori"
    assert saved.provider_id == provider_id


def test_temporary_repository_rejects_core_without_creating_any_record(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import TemporaryProviderRecordInput

    repository, database_path = _repository(tmp_path)

    with pytest.raises(ValueError):
        repository.save(
            character_id="miori",
            record=TemporaryProviderRecordInput(
                provider_id="core",
                source_ref="source-1",
                record_type="ENTRY",
                structured_value='{"value":"synthetic"}',
                effective_at=NOW,
            ),
        )

    with sqlite3.connect(database_path) as connection:
        count_rows = connection.execute(
            "SELECT COUNT(*) FROM temporary_provider_records"
        ).fetchone()[0]
    assert count_rows == 0


def test_temporary_save_and_delete_never_create_persona_outbox_rows(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import TemporaryProviderRecordInput

    repository, database_path = _repository(tmp_path)
    saved = repository.save(
        character_id="miori",
        record=TemporaryProviderRecordInput(
            provider_id="temporary:recipe",
            source_ref="recipe-1",
            record_type="ENTRY",
            structured_value='{"value":"synthetic"}',
            effective_at=NOW,
        ),
    )

    repository.hard_delete(
        character_id="miori",
        provider_id="temporary:recipe",
        record_id=saved.id,
    )

    with sqlite3.connect(database_path) as connection:
        temporary_count = connection.execute(
            "SELECT COUNT(*) FROM temporary_provider_records"
        ).fetchone()[0]
        approved_count = connection.execute(
            "SELECT COUNT(*) FROM approved_memories"
        ).fetchone()[0]
        outbox_count = connection.execute(
            "SELECT COUNT(*) FROM memory_index_outbox"
        ).fetchone()[0]
    assert temporary_count == 0
    assert approved_count == 0
    assert outbox_count == 0


def test_temporary_delete_requires_character_and_provider_boundaries(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import TemporaryProviderRecordInput

    repository, database_path = _repository(tmp_path)
    saved = repository.save(
        character_id="miori",
        record=TemporaryProviderRecordInput(
            provider_id="temporary:agriculture",
            source_ref="harvest-1",
            record_type="ENTRY",
            structured_value='{"value":"synthetic"}',
            effective_at=NOW,
        ),
    )

    operations = (
        lambda: repository.hard_delete(
            character_id="other",
            provider_id="temporary:agriculture",
            record_id=saved.id,
        ),
        lambda: repository.hard_delete(
            character_id="miori",
            provider_id="temporary:recipe",
            record_id=saved.id,
        ),
    )
    for operation in operations:
        operation()

    with sqlite3.connect(database_path) as connection:
        remaining = connection.execute(
            "SELECT character_id, provider_id FROM temporary_provider_records"
        ).fetchone()
    assert remaining == ("miori", "temporary:agriculture")


def test_temporary_list_and_get_are_scoped_by_character_and_provider(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import TemporaryProviderRecordInput

    repository, _database_path = _repository(tmp_path)
    recipe = repository.save(
        character_id="miori",
        record=TemporaryProviderRecordInput(
            provider_id="temporary:recipe",
            source_ref="recipe-1",
            record_type="RECIPE",
            structured_value='{"name":"カレー"}',
            effective_at=NOW,
        ),
    )
    repository.save(
        character_id="miori",
        record=TemporaryProviderRecordInput(
            provider_id="temporary:agriculture",
            source_ref="crop-1",
            record_type="CROP",
            structured_value='{"name":"トマト"}',
            effective_at=NOW,
        ),
    )
    repository.save(
        character_id="other",
        record=TemporaryProviderRecordInput(
            provider_id="temporary:recipe",
            source_ref="recipe-2",
            record_type="RECIPE",
            structured_value='{"name":"スープ"}',
            effective_at=NOW,
        ),
    )

    records = repository.list_by_provider(
        character_id="miori", provider_id="temporary:recipe"
    )

    assert records == [recipe]
    assert repository.get(
        character_id="miori",
        provider_id="temporary:recipe",
        record_id=recipe.id,
    ) == recipe
    assert repository.get(
        character_id="other",
        provider_id="temporary:recipe",
        record_id=recipe.id,
    ) is None
    assert repository.get(
        character_id="miori",
        provider_id="temporary:agriculture",
        record_id=recipe.id,
    ) is None


def test_temporary_correction_updates_only_changed_content_and_has_natural_idempotency(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import (
        TemporaryProviderRecordCorrection,
        TemporaryProviderRecordInput,
    )

    timestamps = iter(
        (NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=2))
    )
    repository, database_path = _repository_with_clock(
        tmp_path, clock=lambda: next(timestamps)
    )
    saved = repository.save(
        character_id="miori",
        record=TemporaryProviderRecordInput(
            provider_id="temporary:recipe",
            source_ref="recipe-1",
            record_type="DRAFT",
            structured_value='{"name":"旧レシピ"}',
            effective_at=NOW,
        ),
    )
    correction = TemporaryProviderRecordCorrection(
        record_type="RECIPE",
        structured_value='{"name":"新レシピ"}',
        effective_at=datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
    )

    corrected = repository.correct(
        character_id="miori",
        provider_id="temporary:recipe",
        record_id=saved.id,
        correction=correction,
    )
    retried = repository.correct(
        character_id="miori",
        provider_id="temporary:recipe",
        record_id=saved.id,
        correction=correction,
    )

    assert corrected == retried
    assert corrected.source_ref == "recipe-1"
    assert corrected.record_type == "RECIPE"
    assert corrected.structured_value == '{"name":"新レシピ"}'
    assert corrected.effective_at == datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    assert corrected.updated_at == NOW + timedelta(minutes=1)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_index_outbox"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("character_id", "provider_id"),
    [
        ("other", "temporary:recipe"),
        ("miori", "temporary:agriculture"),
    ],
    ids=["other-character", "other-provider"],
)
def test_temporary_correction_requires_character_and_provider_boundaries(
    character_id: str,
    provider_id: str,
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import (
        TemporaryProviderRecordCorrection,
        TemporaryProviderRecordInput,
    )

    repository, database_path = _repository(tmp_path)
    saved = repository.save(
        character_id="miori",
        record=TemporaryProviderRecordInput(
            provider_id="temporary:recipe",
            source_ref="recipe-1",
            record_type="DRAFT",
            structured_value='{"name":"元のレシピ"}',
            effective_at=NOW,
        ),
    )
    with sqlite3.connect(database_path) as connection:
        record_rows_before = connection.execute(
            "SELECT * FROM temporary_provider_records ORDER BY rowid"
        ).fetchall()
        outbox_rows_before = connection.execute(
            "SELECT * FROM memory_index_outbox ORDER BY rowid"
        ).fetchall()

    with pytest.raises(LookupError):
        repository.correct(
            character_id=character_id,
            provider_id=provider_id,
            record_id=saved.id,
            correction=TemporaryProviderRecordCorrection(
                record_type="RECIPE",
                structured_value='{"name":"越境した訂正"}',
                effective_at=NOW + timedelta(days=1),
            ),
        )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT * FROM temporary_provider_records ORDER BY rowid"
        ).fetchall() == record_rows_before
        assert connection.execute(
            "SELECT * FROM memory_index_outbox ORDER BY rowid"
        ).fetchall() == outbox_rows_before


def test_temporary_delete_is_repeatable_and_removes_plaintext_from_sqlite_files(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import TemporaryProviderRecordInput

    marker = "TEMPORARY_SECRET_MARKER_12"
    repository, database_path = _repository(tmp_path)
    saved = repository.save(
        character_id="miori",
        record=TemporaryProviderRecordInput(
            provider_id="temporary:recipe",
            source_ref="recipe-secret",
            record_type="RECIPE",
            structured_value=f'{{"secret":"{marker}"}}',
            effective_at=NOW,
        ),
    )

    for _ in range(2):
        repository.hard_delete(
            character_id="miori",
            provider_id="temporary:recipe",
            record_id=saved.id,
        )

    assert marker.encode() not in database_path.read_bytes()
    wal_path = database_path.with_name(f"{database_path.name}-wal")
    assert not wal_path.exists() or marker.encode() not in wal_path.read_bytes()
