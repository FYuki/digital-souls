from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.memory.chroma_store import (
    delete_memory_index_entry,
    get_memory_index_metadata,
    list_memory_index_ids,
    memory_index_metadata,
    upsert_memory_index_entry,
)
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.contracts import ApprovedMemory, MemoryStatus
from app.memory.persistence.index_outbox_repository import (
    IndexOutboxEntry,
    IndexOutboxRepository,
)
from app.memory.persistence.sqlite import format_datetime


logger = logging.getLogger(__name__)
WORKER_BATCH_SIZE = 50
OUTBOX_ATTEMPT_LIMIT = 5
FAILURE_WARNING_THRESHOLD = 3
RUNTIME_REPORT_FILENAME = "memory-index-sync.json"

Embedder = Callable[[str], list[float]]
Clock = Callable[[], datetime]


class _SyncFailure(Exception):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class MemoryIndexSync:
    def __init__(
        self,
        *,
        approved_repository: ApprovedMemoryRepository,
        outbox_repository: IndexOutboxRepository,
        chroma_path: Path,
        runtime_report_dir: Path,
        embedder: Embedder,
        clock: Clock,
    ) -> None:
        self._approved_repository = approved_repository
        self._outbox_repository = outbox_repository
        self._chroma_path = chroma_path
        self._runtime_report_dir = runtime_report_dir
        self._embedder = embedder
        self._clock = clock
        self._consecutive_error_code: str | None = None
        self._consecutive_failure_count = 0
        self._last_success_at: str | None = None

    def run_worker_once(self) -> None:
        entries = self._outbox_repository.list_processable(
            limit=WORKER_BATCH_SIZE, attempt_limit=OUTBOX_ATTEMPT_LIMIT
        )
        for entry in entries:
            try:
                self._apply_outbox_entry(entry)
            except _SyncFailure as failure:
                self._outbox_repository.mark_failed(
                    outbox_id=entry.id, error_code=failure.error_code
                )
                self._record_failure(failure.error_code)
            else:
                self._outbox_repository.mark_completed(outbox_id=entry.id)
                self._record_success()

    def reconcile_once(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        primary_error_code: str | None = None
        interrupted = False
        character_ids = (
            self._approved_repository.list_character_ids()
            | self._outbox_repository.list_character_ids()
        )
        for character_id in sorted(character_ids):
            if should_stop is not None and should_stop():
                interrupted = True
                break
            active = {
                str(memory.id): memory
                for memory in self._approved_repository.list_active(
                    character_id=character_id
                )
            }
            try:
                indexed_ids = list_memory_index_ids(
                    character_id=character_id, chroma_path=self._chroma_path
                )
            except Exception:
                primary_error_code = primary_error_code or "CHROMA_READ_FAILED"
                self._record_failure("CHROMA_READ_FAILED")
                continue

            incomplete_by_memory = (
                self._outbox_repository.list_incomplete_operations_by_memory(
                    character_id=character_id
                )
            )
            for memory_id in indexed_ids | active.keys():
                incomplete_by_memory.setdefault(memory_id, set())

            for memory_id in sorted(indexed_ids - active.keys()):
                try:
                    delete_memory_index_entry(
                        character_id=character_id,
                        memory_id=memory_id,
                        chroma_path=self._chroma_path,
                    )
                except Exception:
                    primary_error_code = primary_error_code or "CHROMA_WRITE_FAILED"
                    self._record_failure("CHROMA_WRITE_FAILED")
                else:
                    self._complete_absent_operations(character_id, memory_id)

            for memory_id in sorted(
                incomplete_by_memory.keys() - active.keys() - indexed_ids
            ):
                self._complete_absent_operations(character_id, memory_id)

            for memory_id, memory in active.items():
                try:
                    self._reconcile_memory(
                        memory,
                        memory_id in indexed_ids,
                        force_upsert="UPSERT" in incomplete_by_memory[memory_id],
                    )
                except _SyncFailure as failure:
                    primary_error_code = primary_error_code or failure.error_code
                    self._record_failure(failure.error_code)
                else:
                    self._outbox_repository.mark_memory_operation_completed(
                        character_id=character_id,
                        memory_id=memory_id,
                        operation="UPSERT",
                    )

        if primary_error_code is None and not interrupted:
            self._last_success_at = format_datetime(self._now())
            self._record_success()
        self._write_runtime_report(primary_error_code)

    def _apply_outbox_entry(self, entry: IndexOutboxEntry) -> None:
        if entry.operation == "DELETE":
            try:
                delete_memory_index_entry(
                    character_id=entry.character_id,
                    memory_id=entry.memory_id,
                    chroma_path=self._chroma_path,
                )
            except Exception as error:
                raise _SyncFailure("CHROMA_WRITE_FAILED") from error
            return
        if entry.operation != "UPSERT":
            raise _SyncFailure("INVALID_OUTBOX_OPERATION")
        try:
            memory_uuid = UUID(entry.memory_id)
        except ValueError as error:
            raise _SyncFailure("MEMORY_NOT_FOUND") from error
        try:
            memory = self._approved_repository.get(
                character_id=entry.character_id, memory_id=memory_uuid
            )
        except Exception as error:
            raise _SyncFailure("SQLITE_READ_FAILED") from error
        if memory is None:
            raise _SyncFailure("MEMORY_NOT_FOUND")
        if memory.status is not MemoryStatus.ACTIVE or (
            memory.expires_at is not None and memory.expires_at <= self._now()
        ):
            try:
                delete_memory_index_entry(
                    character_id=entry.character_id,
                    memory_id=entry.memory_id,
                    chroma_path=self._chroma_path,
                )
            except Exception as error:
                raise _SyncFailure("CHROMA_WRITE_FAILED") from error
            return
        self._upsert_memory(memory)

    def _reconcile_memory(
        self, memory: ApprovedMemory, is_indexed: bool, *, force_upsert: bool
    ) -> None:
        if is_indexed and not force_upsert:
            try:
                metadata = get_memory_index_metadata(
                    character_id=memory.character_id,
                    memory_id=str(memory.id),
                    chroma_path=self._chroma_path,
                )
            except Exception as error:
                raise _SyncFailure("CHROMA_READ_FAILED") from error
            if metadata == _memory_metadata(memory):
                return
        self._upsert_memory(memory)

    def _complete_absent_operations(self, character_id: str, memory_id: str) -> None:
        for operation in ("UPSERT", "DELETE"):
            self._outbox_repository.mark_memory_operation_completed(
                character_id=character_id,
                memory_id=memory_id,
                operation=operation,
            )

    def _upsert_memory(self, memory: ApprovedMemory) -> None:
        try:
            embedding = self._embedder(memory.normalized_text)
        except Exception as error:
            raise _SyncFailure("EMBEDDING_UNAVAILABLE") from error
        try:
            upsert_memory_index_entry(
                character_id=memory.character_id,
                memory_id=str(memory.id),
                embedding=embedding,
                normalized_text=memory.normalized_text,
                provider_id=memory.provider_id,
                memory_kind=memory.memory_kind,
                memory_type=memory.memory_type.value,
                policy_version=memory.policy_version,
                occurred_at=(
                    None
                    if memory.occurred_at is None
                    else format_datetime(memory.occurred_at)
                ),
                expires_at=(
                    None
                    if memory.expires_at is None
                    else format_datetime(memory.expires_at)
                ),
                chroma_path=self._chroma_path,
            )
        except Exception as error:
            raise _SyncFailure("CHROMA_WRITE_FAILED") from error

    def _record_failure(self, error_code: str) -> None:
        logger.debug("memory index sync failure: %s", error_code)
        if self._consecutive_error_code == error_code:
            self._consecutive_failure_count += 1
        else:
            self._consecutive_error_code = error_code
            self._consecutive_failure_count = 1
        if self._consecutive_failure_count == FAILURE_WARNING_THRESHOLD:
            logger.warning(
                "memory index sync failure: %s count=%d",
                error_code,
                self._consecutive_failure_count,
            )

    def _record_success(self) -> None:
        if self._consecutive_error_code is not None:
            logger.info("memory index sync recovered: %s", self._consecutive_error_code)
        self._consecutive_error_code = None
        self._consecutive_failure_count = 0

    def _write_runtime_report(self, last_error_code: str | None) -> None:
        pending_count, failed_count = self._outbox_repository.status_counts()
        report = {
            "pending_count": pending_count,
            "failed_count": failed_count,
            "last_error_code": last_error_code,
            "last_success_at": self._last_success_at,
        }
        temporary_path: Path | None = None
        try:
            self._runtime_report_dir.mkdir(parents=True, exist_ok=True)
            report_path = self._runtime_report_dir / RUNTIME_REPORT_FILENAME
            serialized = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._runtime_report_dir,
                prefix=f".{RUNTIME_REPORT_FILENAME}.",
                delete=False,
            ) as temporary:
                temporary.write(serialized)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, report_path)
            temporary_path = None
        except OSError as error:
            logger.warning(
                "memory index runtime report publication failed: %s",
                type(error).__name__,
            )
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError as error:
                    logger.warning(
                        "memory index runtime report cleanup failed: %s",
                        type(error).__name__,
                    )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _memory_metadata(memory: ApprovedMemory) -> dict[str, str]:
    return memory_index_metadata(
        character_id=memory.character_id,
        provider_id=memory.provider_id,
        memory_kind=memory.memory_kind,
        memory_type=memory.memory_type.value,
        policy_version=memory.policy_version,
        occurred_at=(
            None
            if memory.occurred_at is None
            else format_datetime(memory.occurred_at)
        ),
        expires_at=(
            None if memory.expires_at is None else format_datetime(memory.expires_at)
        ),
    )
