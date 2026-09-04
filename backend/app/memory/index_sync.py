from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.memory.chroma_store import (
    EmbeddingFingerprint,
    activate_memory_index,
    active_memory_index_fingerprint,
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
        embedding_provider_id: str,
        embedding_model_id: str,
        clock: Clock,
    ) -> None:
        self._approved_repository = approved_repository
        self._outbox_repository = outbox_repository
        self._chroma_path = chroma_path
        self._runtime_report_dir = runtime_report_dir
        self._embedder = embedder
        self._embedding_provider_id = embedding_provider_id
        self._embedding_model_id = embedding_model_id
        self._clock = clock
        self._consecutive_error_code: str | None = None
        self._consecutive_failure_count = 0
        self._last_success_at: str | None = None
        self._reindex_required = False

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

    def delete_after_commit(self, *, character_id: str, memory_id: UUID) -> None:
        try:
            fingerprint = active_memory_index_fingerprint(
                character_id, self._chroma_path
            )
            if fingerprint is not None:
                delete_memory_index_entry(
                    character_id=character_id,
                    memory_id=str(memory_id),
                    chroma_path=self._chroma_path,
                    fingerprint=fingerprint,
                )
        except Exception as error:
            self._record_failure("CHROMA_WRITE_FAILED", type(error).__name__)
            return
        try:
            self._outbox_repository.mark_memory_operation_completed(
                character_id=character_id,
                memory_id=str(memory_id),
                operation="DELETE",
            )
        except Exception as error:
            self._record_failure("SQLITE_WRITE_FAILED", type(error).__name__)
            return
        self._record_success()

    def reconcile_once(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        self._reindex_required = False
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
            fingerprint: EmbeddingFingerprint | None = None
            first_embedding: tuple[str, list[float]] | None = None
            failed_seed_ids: set[str] = set()
            if active:
                for first_memory_id in sorted(active):
                    try:
                        embedding = self._embed(active[first_memory_id].normalized_text)
                    except _SyncFailure as failure:
                        primary_error_code = primary_error_code or failure.error_code
                        self._record_failure(failure.error_code)
                        self._reindex_required = True
                        failed_seed_ids.add(first_memory_id)
                        continue
                    fingerprint = self._fingerprint(embedding)
                    first_embedding = (first_memory_id, embedding)
                    break
                if fingerprint is None:
                    continue
            else:
                try:
                    fingerprint = active_memory_index_fingerprint(
                        character_id, self._chroma_path
                    )
                except Exception:
                    primary_error_code = primary_error_code or "CHROMA_READ_FAILED"
                    self._record_failure("CHROMA_READ_FAILED")
                    self._reindex_required = True
                    continue
            if fingerprint is None:
                # fingerprint導入前のCollectionは暗黙に変更せず、そのまま保持する。
                indexed_ids: set[str] = set()
            else:
                try:
                    indexed_ids = list_memory_index_ids(
                        character_id=character_id,
                        chroma_path=self._chroma_path,
                        fingerprint=fingerprint,
                    )
                except Exception:
                    primary_error_code = primary_error_code or "CHROMA_READ_FAILED"
                    self._record_failure("CHROMA_READ_FAILED")
                    self._reindex_required = True
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
                        fingerprint=fingerprint,
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
                if memory_id in failed_seed_ids:
                    continue
                try:
                    self._reconcile_memory(
                        memory,
                        memory_id in indexed_ids,
                        force_upsert="UPSERT" in incomplete_by_memory[memory_id],
                        fingerprint=fingerprint,
                        embedding=(
                            first_embedding[1]
                            if first_embedding is not None
                            and first_embedding[0] == memory_id
                            else None
                        ),
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

            if fingerprint is not None:
                try:
                    complete_ids = list_memory_index_ids(
                        character_id=character_id,
                        chroma_path=self._chroma_path,
                        fingerprint=fingerprint,
                    )
                    if complete_ids != set(active):
                        raise _SyncFailure("REINDEX_REQUIRED")
                    activate_memory_index(character_id, fingerprint, self._chroma_path)
                except _SyncFailure as failure:
                    primary_error_code = primary_error_code or failure.error_code
                    self._record_failure(failure.error_code)
                    self._reindex_required = True
                except Exception:
                    primary_error_code = primary_error_code or "CHROMA_WRITE_FAILED"
                    self._record_failure("CHROMA_WRITE_FAILED")
                    self._reindex_required = True

        if primary_error_code is None and not interrupted:
            self._last_success_at = format_datetime(self._now())
            self._record_success()
        self._write_runtime_report(primary_error_code)

    def _apply_outbox_entry(self, entry: IndexOutboxEntry) -> None:
        if entry.operation == "DELETE":
            try:
                fingerprint = active_memory_index_fingerprint(
                    entry.character_id, self._chroma_path
                )
                if fingerprint is not None:
                    delete_memory_index_entry(
                        character_id=entry.character_id,
                        memory_id=entry.memory_id,
                        chroma_path=self._chroma_path,
                        fingerprint=fingerprint,
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
                fingerprint = active_memory_index_fingerprint(
                    entry.character_id, self._chroma_path
                )
                if fingerprint is not None:
                    delete_memory_index_entry(
                        character_id=entry.character_id,
                        memory_id=entry.memory_id,
                        chroma_path=self._chroma_path,
                        fingerprint=fingerprint,
                    )
            except Exception as error:
                raise _SyncFailure("CHROMA_WRITE_FAILED") from error
            return
        fingerprint = self._upsert_memory(memory)
        try:
            active_fingerprint = active_memory_index_fingerprint(
                memory.character_id, self._chroma_path
            )
        except Exception as error:
            raise _SyncFailure("CHROMA_READ_FAILED") from error
        if active_fingerprint != fingerprint:
            try:
                active_ids = {
                    str(candidate.id)
                    for candidate in self._approved_repository.list_active(
                        character_id=memory.character_id
                    )
                }
                indexed_ids = list_memory_index_ids(
                    character_id=memory.character_id,
                    chroma_path=self._chroma_path,
                    fingerprint=fingerprint,
                )
                if indexed_ids == active_ids:
                    activate_memory_index(
                        memory.character_id, fingerprint, self._chroma_path
                    )
                else:
                    self._reindex_required = True
            except Exception as error:
                self._reindex_required = True
                raise _SyncFailure("CHROMA_WRITE_FAILED") from error

    def _reconcile_memory(
        self,
        memory: ApprovedMemory,
        is_indexed: bool,
        *,
        force_upsert: bool,
        fingerprint: EmbeddingFingerprint | None,
        embedding: list[float] | None,
    ) -> None:
        if fingerprint is None:
            raise _SyncFailure("REINDEX_REQUIRED")
        if is_indexed and not force_upsert:
            try:
                metadata = get_memory_index_metadata(
                    character_id=memory.character_id,
                    memory_id=str(memory.id),
                    chroma_path=self._chroma_path,
                    fingerprint=fingerprint,
                )
            except Exception as error:
                raise _SyncFailure("CHROMA_READ_FAILED") from error
            if metadata == _memory_metadata(memory, fingerprint):
                return
        self._upsert_memory(memory, fingerprint=fingerprint, embedding=embedding)

    def _complete_absent_operations(self, character_id: str, memory_id: str) -> None:
        for operation in ("UPSERT", "DELETE"):
            self._outbox_repository.mark_memory_operation_completed(
                character_id=character_id,
                memory_id=memory_id,
                operation=operation,
            )

    def _upsert_memory(
        self,
        memory: ApprovedMemory,
        *,
        fingerprint: EmbeddingFingerprint | None = None,
        embedding: list[float] | None = None,
    ) -> EmbeddingFingerprint:
        resolved_embedding = (
            self._embed(memory.normalized_text) if embedding is None else embedding
        )
        resolved_fingerprint = self._fingerprint(resolved_embedding)
        if fingerprint is not None and resolved_fingerprint != fingerprint:
            raise _SyncFailure("REINDEX_REQUIRED")
        try:
            upsert_memory_index_entry(
                character_id=memory.character_id,
                memory_id=str(memory.id),
                embedding=resolved_embedding,
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
                fingerprint=resolved_fingerprint,
            )
        except Exception as error:
            raise _SyncFailure("CHROMA_WRITE_FAILED") from error
        return resolved_fingerprint

    def _embed(self, text: str) -> list[float]:
        try:
            embedding = self._embedder(text)
        except Exception as error:
            raise _SyncFailure("EMBEDDING_UNAVAILABLE") from error
        if not embedding or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in embedding
        ):
            raise _SyncFailure("INVALID_EMBEDDING")
        return [float(value) for value in embedding]

    def _fingerprint(self, embedding: list[float]) -> EmbeddingFingerprint:
        return EmbeddingFingerprint(
            self._embedding_provider_id,
            self._embedding_model_id,
            len(embedding),
        )

    def _record_failure(self, error_code: str, error_type: str | None = None) -> None:
        logger.debug(
            "memory index sync failure: %s error_type=%s",
            error_code,
            error_type or "none",
        )
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
            "index_state": ("reindex_required" if self._reindex_required else "ready"),
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


def _memory_metadata(
    memory: ApprovedMemory, fingerprint: EmbeddingFingerprint
) -> dict[str, str]:
    return memory_index_metadata(
        character_id=memory.character_id,
        provider_id=memory.provider_id,
        memory_kind=memory.memory_kind,
        memory_type=memory.memory_type.value,
        policy_version=memory.policy_version,
        occurred_at=(
            None if memory.occurred_at is None else format_datetime(memory.occurred_at)
        ),
        expires_at=(
            None if memory.expires_at is None else format_datetime(memory.expires_at)
        ),
        fingerprint=fingerprint,
    )
