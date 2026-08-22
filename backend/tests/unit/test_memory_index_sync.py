import json
import importlib
import logging
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID
import pytest

from tests.unit.test_approved_memory_repository import _candidate, _context, _repository


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedder):
    # backup/restore側が共有SQLite leaseを登録する副作用を先に反映する。
    importlib.import_module("app.backup_restore.service")
    from app.memory import index_sync
    from app.memory.persistence.index_outbox_repository import IndexOutboxRepository

    approved, database_path = _repository(tmp_path)
    outbox = IndexOutboxRepository(database_path=database_path, clock=lambda: NOW)
    records: dict[tuple[str, str], dict[str, object]] = {}
    deleted: list[tuple[str, str]] = []

    def upsert_memory_index_entry(**entry: object) -> None:
        assert entry["chroma_path"] == tmp_path / "data" / "chroma"
        key = (str(entry["character_id"]), str(entry["memory_id"]))
        records[key] = dict(entry)

    def delete_memory_index_entry(**entry: object) -> None:
        assert entry["chroma_path"] == tmp_path / "data" / "chroma"
        key = (str(entry["character_id"]), str(entry["memory_id"]))
        deleted.append(key)
        records.pop(key, None)

    def list_memory_index_ids(*, character_id: str, chroma_path: Path) -> set[str]:
        assert chroma_path == tmp_path / "data" / "chroma"
        return {memory_id for owner, memory_id in records if owner == character_id}

    def get_memory_index_metadata(
        *, character_id: str, memory_id: str, chroma_path: Path
    ) -> dict[str, str] | None:
        assert chroma_path == tmp_path / "data" / "chroma"
        record = records.get((character_id, memory_id))
        if record is None:
            return None
        return {
            key: str(value)
            for key, value in record.items()
            if key
            in {
                "character_id",
                "provider_id",
                "memory_kind",
                "memory_type",
                "policy_version",
                "occurred_at",
                "effective_at",
                "expires_at",
            }
            and value is not None
        }

    monkeypatch.setattr(
        index_sync, "upsert_memory_index_entry", upsert_memory_index_entry
    )
    monkeypatch.setattr(
        index_sync, "delete_memory_index_entry", delete_memory_index_entry
    )
    monkeypatch.setattr(index_sync, "list_memory_index_ids", list_memory_index_ids)
    monkeypatch.setattr(
        index_sync, "get_memory_index_metadata", get_memory_index_metadata
    )
    service = index_sync.MemoryIndexSync(
        approved_repository=approved,
        outbox_repository=outbox,
        chroma_path=tmp_path / "data" / "chroma",
        runtime_report_dir=tmp_path / "data" / "runtime-reports",
        embedder=embedder,
        clock=lambda: NOW,
    )
    return service, approved, database_path, records, deleted


def _outbox_rows(database_path: Path) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        return list(
            connection.execute("SELECT * FROM memory_index_outbox ORDER BY rowid")
        )


def _log_text(records: list[logging.LogRecord]) -> str:
    return "\n".join(
        f"{record.getMessage()}\n{record.exc_text or ''}\n{record.args!r}"
        for record in records
    )


def test_worker_rereads_latest_sqlite_memory_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedded: list[str] = []
    service, approved, database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, lambda text: embedded.append(text) or [0.1]
    )
    original = approved.save(
        character_id="miori", candidate=_candidate("古い本文"), context=_context()
    )
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(
                "UPDATE approved_memories SET normalized_text = ? WHERE id = ?",
                ("SQLiteの最新本文", str(original.id)),
            )

    service.run_worker_once()
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(
                "UPDATE memory_index_outbox SET status = 'FAILED', attempt_count = 1"
            )
    service.run_worker_once()

    record = records[("miori", str(original.id))]
    assert embedded == ["SQLiteの最新本文", "SQLiteの最新本文"]
    assert record["normalized_text"] == "SQLiteの最新本文"
    assert record["embedding"] == [0.1]
    assert [row["status"] for row in _outbox_rows(database_path)] == ["COMPLETED"]


def test_worker_classifies_sqlite_read_failure_and_continues_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "本文SECRET embedding=[9.9]"
    service, approved, database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    failed = approved.save(
        character_id="miori", candidate=_candidate("読込失敗"), context=_context()
    )
    succeeded = approved.save(
        character_id="miori",
        candidate=_candidate("後続本文"),
        context=replace(_context(), idempotency_key="second-key"),
    )
    original_get = approved.get

    def fail_selected_read(*, character_id: str, memory_id: UUID):
        if memory_id == failed.id:
            raise sqlite3.OperationalError(secret)
        return original_get(character_id=character_id, memory_id=memory_id)

    monkeypatch.setattr(approved, "get", fail_selected_read)
    caplog.set_level(logging.DEBUG)

    service.run_worker_once()

    failed_row, succeeded_row = _outbox_rows(database_path)
    assert (
        failed_row["status"],
        failed_row["attempt_count"],
        failed_row["last_error_code"],
    ) == ("FAILED", 1, "SQLITE_READ_FAILED")
    assert succeeded_row["status"] == "COMPLETED"
    assert ("miori", str(failed.id)) not in records
    assert records[("miori", str(succeeded.id))]["normalized_text"] == "後続本文"
    outbox_projection = " ".join(
        str(value)
        for row in (failed_row, succeeded_row)
        for value in dict(row).values()
        if value is not None
    )
    log_text = _log_text(caplog.records)
    assert secret not in outbox_projection
    assert secret not in log_text


def test_worker_delete_can_be_reprocessed_without_changing_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, database_path, records, deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    memory = approved.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    service.run_worker_once()
    approved.hard_delete(character_id="miori", memory_id=memory.id)

    service.run_worker_once()
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(
                "UPDATE memory_index_outbox SET status = 'FAILED', attempt_count = 1 "
                "WHERE operation = 'DELETE'"
            )
    service.run_worker_once()

    assert ("miori", str(memory.id)) not in records
    assert deleted.count(("miori", str(memory.id))) == 2


def test_delete_after_commit_synchronously_deletes_chroma_and_completes_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, database_path, records, deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    memory = approved.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    service.run_worker_once()
    approved.hard_delete(character_id="miori", memory_id=memory.id)

    service.delete_after_commit(character_id="miori", memory_id=memory.id)

    assert ("miori", str(memory.id)) not in records
    assert deleted[-1] == ("miori", str(memory.id))
    delete_row = [
        row for row in _outbox_rows(database_path) if row["operation"] == "DELETE"
    ][0]
    assert delete_row["status"] == "COMPLETED"


def test_delete_after_commit_failure_keeps_recoverable_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.memory import index_sync

    service, approved, database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    memory = approved.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    service.run_worker_once()
    approved.hard_delete(character_id="miori", memory_id=memory.id)

    def fail_delete(**_entry: object) -> None:
        raise RuntimeError("secret body must not escape")

    successful_delete = index_sync.delete_memory_index_entry
    monkeypatch.setattr(index_sync, "delete_memory_index_entry", fail_delete)
    caplog.set_level(logging.DEBUG, logger=index_sync.__name__)

    service.delete_after_commit(character_id="miori", memory_id=memory.id)

    assert approved.get(character_id="miori", memory_id=memory.id) is None
    assert ("miori", str(memory.id)) in records
    delete_row = [
        row for row in _outbox_rows(database_path) if row["operation"] == "DELETE"
    ][0]
    assert delete_row["status"] in {"PENDING", "FAILED"}
    assert "RuntimeError" in caplog.text
    assert "secret body must not escape" not in caplog.text
    assert _candidate().normalized_text not in caplog.text

    monkeypatch.setattr(index_sync, "delete_memory_index_entry", successful_delete)
    service.reconcile_once()
    assert ("miori", str(memory.id)) not in records
    delete_row = [
        row for row in _outbox_rows(database_path) if row["operation"] == "DELETE"
    ][0]
    assert delete_row["status"] == "COMPLETED"


def test_delete_after_commit_completion_failure_is_recovered_by_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.memory.persistence.index_outbox_repository import IndexOutboxRepository

    service, approved, database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    memory = approved.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    service.run_worker_once()
    approved.hard_delete(character_id="miori", memory_id=memory.id)
    complete_operation = IndexOutboxRepository.mark_memory_operation_completed

    def fail_completion(_repository: object, **_operation: object) -> None:
        raise sqlite3.OperationalError("completion failed")

    monkeypatch.setattr(
        IndexOutboxRepository,
        "mark_memory_operation_completed",
        fail_completion,
    )

    service.delete_after_commit(character_id="miori", memory_id=memory.id)

    assert approved.get(character_id="miori", memory_id=memory.id) is None
    assert ("miori", str(memory.id)) not in records
    delete_row = [
        row for row in _outbox_rows(database_path) if row["operation"] == "DELETE"
    ][0]
    assert delete_row["status"] in {"PENDING", "FAILED"}

    monkeypatch.setattr(
        IndexOutboxRepository,
        "mark_memory_operation_completed",
        complete_operation,
    )
    service.reconcile_once()

    delete_row = [
        row for row in _outbox_rows(database_path) if row["operation"] == "DELETE"
    ][0]
    assert delete_row["status"] == "COMPLETED"


def test_worker_stops_retrying_after_five_metadata_only_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "本文SECRET embedding=[9.9]"
    attempts = 0

    def fail_embedding(_text: str) -> list[float]:
        nonlocal attempts
        attempts += 1
        raise RuntimeError(secret)

    service, approved, database_path, _records, _deleted = _sync(
        tmp_path, monkeypatch, fail_embedding
    )
    memory = approved.save(
        character_id="miori", candidate=_candidate(secret), context=_context()
    )
    caplog.set_level(logging.DEBUG)

    for _ in range(6):
        service.run_worker_once()

    row = _outbox_rows(database_path)[0]
    assert attempts == 5
    assert (row["status"], row["attempt_count"], row["last_error_code"]) == (
        "FAILED",
        5,
        "EMBEDDING_UNAVAILABLE",
    )
    assert approved.get(character_id="miori", memory_id=memory.id) is not None
    outbox_projection = " ".join(
        str(value) for value in dict(row).values() if value is not None
    )
    log_text = _log_text(caplog.records)
    assert secret not in outbox_projection
    assert secret not in log_text
    assert all("[9.9]" not in record.getMessage() for record in caplog.records)


def test_worker_classifies_missing_sqlite_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, database_path, _records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    memory = approved.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(
                "DELETE FROM approved_memories WHERE character_id = ? AND id = ?",
                ("miori", str(memory.id)),
            )

    service.run_worker_once()

    row = _outbox_rows(database_path)[0]
    assert (row["status"], row["attempt_count"], row["last_error_code"]) == (
        "FAILED",
        1,
        "MEMORY_NOT_FOUND",
    )


def test_worker_classifies_chroma_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, database_path, _records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    approved.save(character_id="miori", candidate=_candidate(), context=_context())
    from app.memory import index_sync

    def fail_write(**_entry: object) -> None:
        raise RuntimeError("private body")

    monkeypatch.setattr(index_sync, "upsert_memory_index_entry", fail_write)

    service.run_worker_once()

    row = _outbox_rows(database_path)[0]
    assert (row["status"], row["attempt_count"], row["last_error_code"]) == (
        "FAILED",
        1,
        "CHROMA_WRITE_FAILED",
    )


def test_reconciliation_classifies_chroma_id_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, _database_path, _records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    approved.save(character_id="miori", candidate=_candidate(), context=_context())
    from app.memory import index_sync

    def fail_id_read(**_kwargs: object) -> set[str]:
        raise RuntimeError("private body")

    monkeypatch.setattr(index_sync, "list_memory_index_ids", fail_id_read)

    service.reconcile_once()

    report_path = tmp_path / "data" / "runtime-reports" / "memory-index-sync.json"
    assert json.loads(report_path.read_text(encoding="utf-8"))["last_error_code"] == (
        "CHROMA_READ_FAILED"
    )


def test_reconciliation_classifies_chroma_metadata_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    memory = approved.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    records[("miori", str(memory.id))] = {"character_id": "miori"}
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute("UPDATE memory_index_outbox SET status = 'COMPLETED'")
    from app.memory import index_sync

    def fail_metadata_read(**_kwargs: object) -> dict[str, str] | None:
        raise RuntimeError("private body")

    monkeypatch.setattr(index_sync, "get_memory_index_metadata", fail_metadata_read)

    service.reconcile_once()

    report_path = tmp_path / "data" / "runtime-reports" / "memory-index-sync.json"
    assert json.loads(report_path.read_text(encoding="utf-8"))["last_error_code"] == (
        "CHROMA_READ_FAILED"
    )


def test_failure_warning_is_emitted_once_at_three_and_success_logs_one_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcomes = iter(
        (
            RuntimeError("secret-one"),
            RuntimeError("secret-two"),
            RuntimeError("secret-three"),
            [0.1],
        )
    )

    def intermittent(_text: str) -> list[float]:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    service, approved, _database_path, _records, _deleted = _sync(
        tmp_path, monkeypatch, intermittent
    )
    approved.save(character_id="miori", candidate=_candidate(), context=_context())
    caplog.set_level(logging.DEBUG)

    for _ in range(4):
        service.run_worker_once()

    sync_records = [
        record for record in caplog.records if record.name == "app.memory.index_sync"
    ]
    warnings = [record for record in sync_records if record.levelno == logging.WARNING]
    recoveries = [record for record in sync_records if record.levelno == logging.INFO]
    assert len(warnings) == 1
    assert (
        warnings[0].getMessage()
        == "memory index sync failure: EMBEDDING_UNAVAILABLE count=3"
    )
    assert len(recoveries) == 1
    assert (
        recoveries[0].getMessage()
        == "memory index sync recovered: EMBEDDING_UNAVAILABLE"
    )


def test_reconciliation_repairs_only_approved_memory_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, database_path, records, deleted = _sync(
        tmp_path, monkeypatch, lambda text: [float(len(text))]
    )
    missing = approved.save(
        character_id="miori", candidate=_candidate("欠落"), context=_context()
    )
    mismatched = approved.save(
        character_id="miori",
        candidate=_candidate("修復"),
        context=replace(_context(), idempotency_key="second-key"),
    )
    records[("miori", "orphan")] = {"character_id": "miori", "memory_id": "orphan"}
    records[("miori", str(mismatched.id))] = {
        "character_id": "miori",
        "memory_id": str(mismatched.id),
        "normalized_text": "古い本文",
        "policy_version": "old-policy",
    }
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO temporary_provider_records "
                "(id, character_id, provider_id, source_ref, record_type, "
                "structured_value, effective_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "20000000-0000-4000-8000-000000000001",
                    "other",
                    "temporary:recipe",
                    "recipe-secret",
                    "recipe",
                    "temporary-private-body",
                    "2026-08-20T12:00:00.000000Z",
                    "2026-08-20T12:00:00.000000Z",
                    "2026-08-20T12:00:00.000000Z",
                ),
            )

    service.reconcile_once()

    assert ("miori", "orphan") not in records
    assert ("miori", "orphan") in deleted
    assert records[("miori", str(missing.id))]["normalized_text"] == "欠落"
    assert records[("miori", str(mismatched.id))]["normalized_text"] == "修復"
    assert records[("miori", str(mismatched.id))]["policy_version"] == "policy-v1"
    assert all(character_id != "other" for character_id, _memory_id in records)


def test_reconciliation_keeps_partial_progress_and_converges_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fail_first(text: str) -> list[float]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private body")
        return [float(len(text))]

    service, approved, _database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, fail_first
    )
    first = approved.save(
        character_id="miori", candidate=_candidate("最初"), context=_context()
    )
    second = approved.save(
        character_id="miori",
        candidate=_candidate("次"),
        context=replace(_context(), idempotency_key="second-key"),
    )

    service.reconcile_once()
    assert len(records) == 1
    report_path = tmp_path / "data" / "runtime-reports" / "memory-index-sync.json"
    failed_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert failed_report == {
        "pending_count": 1,
        "failed_count": 0,
        "last_error_code": "EMBEDDING_UNAVAILABLE",
        "last_success_at": None,
    }

    service.reconcile_once()
    assert set(records) == {("miori", str(first.id)), ("miori", str(second.id))}
    recovered_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert recovered_report["last_error_code"] is None
    assert recovered_report["last_success_at"] == "2026-08-20T12:00:00.000000Z"


def test_reconciliation_stops_between_characters_without_recording_full_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, _database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    first = approved.save(
        character_id="miori", candidate=_candidate("最初"), context=_context()
    )
    approved.save(
        character_id="other",
        candidate=_candidate("未処理"),
        context=replace(_context(), idempotency_key="other-key"),
    )
    stop_checks = iter((False, True))

    service.reconcile_once(should_stop=lambda: next(stop_checks))

    assert set(records) == {("miori", str(first.id))}
    report_path = tmp_path / "data" / "runtime-reports" / "memory-index-sync.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["last_error_code"] is None
    assert report["last_success_at"] is None


def test_reconciliation_recovers_attempt_limit_and_writes_metadata_only_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "private-body embedding=[7.7]"
    service, approved, database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, lambda text: [float(len(text))]
    )
    memory = approved.save(
        character_id="miori", candidate=_candidate(secret), context=_context()
    )
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(
                "UPDATE memory_index_outbox SET status = 'FAILED', attempt_count = 5, "
                "last_error_code = 'EMBEDDING_UNAVAILABLE'"
            )

    service.reconcile_once()

    assert ("miori", str(memory.id)) in records
    assert _outbox_rows(database_path)[0]["status"] == "COMPLETED"
    report_path = tmp_path / "data" / "runtime-reports" / "memory-index-sync.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert set(report) == {
        "pending_count",
        "failed_count",
        "last_error_code",
        "last_success_at",
    }
    assert report == {
        "pending_count": 0,
        "failed_count": 0,
        "last_error_code": None,
        "last_success_at": "2026-08-20T12:00:00.000000Z",
    }
    assert secret not in report_path.read_text(encoding="utf-8")


def test_worker_removes_deactivated_memory_from_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    memory = approved.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    records[("miori", str(memory.id))] = {"character_id": "miori"}
    approved.deactivate(character_id="miori", memory_id=memory.id)

    service.run_worker_once()

    assert ("miori", str(memory.id)) not in records
    assert _outbox_rows(database_path)[0]["status"] == "COMPLETED"


def test_worker_removes_expired_memory_from_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    memory = approved.save(
        character_id="miori",
        candidate=_candidate(),
        context=_context(expires_at=NOW),
    )
    records[("miori", str(memory.id))] = {"character_id": "miori"}

    service.run_worker_once()

    assert ("miori", str(memory.id)) not in records
    assert _outbox_rows(database_path)[0]["status"] == "COMPLETED"


def test_reconciliation_replaces_legacy_date_metadata_and_stale_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    memory = approved.save(
        character_id="miori", candidate=_candidate("現在の本文"), context=_context()
    )
    records[("miori", str(memory.id))] = {
        "character_id": "miori",
        "provider_id": memory.provider_id,
        "memory_kind": memory.memory_kind,
        "memory_type": memory.memory_type.value,
        "policy_version": memory.policy_version,
        "effective_at": "2026-08-18T12:00:00.000000Z",
        "normalized_text": "古い本文",
    }

    service.reconcile_once()

    assert records[("miori", str(memory.id))]["normalized_text"] == "現在の本文"
    assert records[("miori", str(memory.id))]["occurred_at"] == (
        "2026-08-16T12:00:00.000000Z"
    )
    assert "effective_at" not in records[("miori", str(memory.id))]
    assert _outbox_rows(database_path)[0]["status"] == "COMPLETED"


def test_reconciliation_completes_hard_deleted_memory_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    memory = approved.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(
                "UPDATE memory_index_outbox SET status = 'FAILED', attempt_count = 5"
            )
    approved.hard_delete(character_id="miori", memory_id=memory.id)

    service.reconcile_once()

    assert ("miori", str(memory.id)) not in records
    assert [row["status"] for row in _outbox_rows(database_path)] == [
        "COMPLETED",
        "COMPLETED",
    ]


def test_reconciliation_completes_absent_delete_after_attempt_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, approved, database_path, records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    memory = approved.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )
    approved.hard_delete(character_id="miori", memory_id=memory.id)
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(
                "UPDATE memory_index_outbox SET status = 'COMPLETED' "
                "WHERE operation = 'UPSERT'"
            )
            connection.execute(
                "UPDATE memory_index_outbox SET status = 'FAILED', attempt_count = 5, "
                "last_error_code = 'CHROMA_WRITE_FAILED' WHERE operation = 'DELETE'"
            )

    service.reconcile_once()

    assert ("miori", str(memory.id)) not in records
    delete_row = next(
        row for row in _outbox_rows(database_path) if row["operation"] == "DELETE"
    )
    assert (
        delete_row["status"],
        delete_row["attempt_count"],
        delete_row["last_error_code"],
    ) == ("COMPLETED", 5, None)


def test_runtime_report_keeps_previous_json_when_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _approved, _database_path, _records, _deleted = _sync(
        tmp_path, monkeypatch, lambda _text: [0.1]
    )
    service.reconcile_once()
    report_path = tmp_path / "data" / "runtime-reports" / "memory-index-sync.json"
    previous = report_path.read_text(encoding="utf-8")
    from app.memory import index_sync

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("publication failed")

    monkeypatch.setattr(index_sync.os, "replace", fail_replace)

    caplog.set_level(logging.WARNING, logger="app.memory.index_sync")
    service.reconcile_once()

    assert json.loads(report_path.read_text(encoding="utf-8")) == json.loads(previous)
    sync_records = [
        record for record in caplog.records if record.name == "app.memory.index_sync"
    ]
    assert [record.getMessage() for record in sync_records] == [
        "memory index runtime report publication failed: OSError"
    ]
