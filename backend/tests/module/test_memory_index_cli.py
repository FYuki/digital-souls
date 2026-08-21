import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from tests.unit.test_approved_memory_repository import _candidate, _context


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _prepare_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, object]:
    from app.memory import index_cli
    from app.memory.persistence.approved_repository import ApprovedMemoryRepository
    from app.memory.persistence.schema import initialize_persona_memory_schema
    from app.runtime_paths import resolve_runtime_paths

    data_root = tmp_path / "runtime-data"
    monkeypatch.setenv("DS_ENVIRONMENT_ID", "test")
    monkeypatch.setenv("DS_DATA_DIR", str(data_root))
    repository_root = Path(index_cli.__file__).resolve().parents[3]
    paths = resolve_runtime_paths(dict(index_cli.os.environ), repository_root)
    initialize_persona_memory_schema(paths, repository_root)
    repository = ApprovedMemoryRepository(
        database_path=paths.persona_memory_sqlite_path,
        clock=lambda: NOW,
        uuid_factory=uuid4,
        outbox_uuid_factory=uuid4,
    )
    return paths.persona_memory_sqlite_path, repository


def _install_index_double(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[tuple[str, str], dict[str, object]]:
    from app.memory import index_cli, index_sync

    records: dict[tuple[str, str], dict[str, object]] = {}

    def upsert(**entry: object) -> None:
        records[(str(entry["character_id"]), str(entry["memory_id"]))] = dict(entry)

    monkeypatch.setattr(index_sync, "upsert_memory_index_entry", upsert)
    monkeypatch.setattr(
        index_sync,
        "delete_memory_index_entry",
        lambda **entry: records.pop(
            (str(entry["character_id"]), str(entry["memory_id"])), None
        ),
    )
    monkeypatch.setattr(
        index_sync,
        "list_memory_index_ids",
        lambda *, character_id, chroma_path: {
            memory_id for owner, memory_id in records if owner == character_id
        },
    )
    monkeypatch.setattr(
        index_sync,
        "get_memory_index_metadata",
        lambda *, character_id, memory_id, chroma_path: None,
    )
    monkeypatch.setattr(index_cli, "embed_text", lambda _text: [0.1])
    return records


def test_cli_worker_processes_outbox_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.memory import index_cli

    database_path, repository = _prepare_runtime(tmp_path, monkeypatch)
    records = _install_index_double(monkeypatch)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )

    assert index_cli.main(["worker"]) == 0

    with sqlite3.connect(database_path) as connection:
        status = connection.execute(
            "SELECT status FROM memory_index_outbox"
        ).fetchone()[0]
    assert status == "COMPLETED"
    assert (
        records[("miori", str(memory.id))]["normalized_text"] == memory.normalized_text
    )


def test_cli_reconcile_rebuilds_missing_index_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.memory import index_cli

    _database_path, repository = _prepare_runtime(tmp_path, monkeypatch)
    records = _install_index_double(monkeypatch)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )

    assert index_cli.main(["reconcile"]) == 0

    assert (
        records[("miori", str(memory.id))]["normalized_text"] == memory.normalized_text
    )


def test_cli_rejects_pending_restore_before_schema_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory import index_cli

    class RestorePendingError(Exception):
        pass

    observed: list[Path] = []

    def reject_restore(marker_path: Path) -> None:
        observed.append(marker_path)
        raise RestorePendingError

    monkeypatch.setattr(index_cli, "require_no_restore_intent", reject_restore)
    monkeypatch.setattr(
        index_cli,
        "initialize_persona_memory_schema",
        lambda *_args: pytest.fail("復元保留中にスキーマを初期化してはならない"),
    )

    with pytest.raises(RestorePendingError):
        index_cli.main(["worker"])

    assert len(observed) == 1
