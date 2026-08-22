from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from uuid import UUID

from app.memory.admission.contracts import (
    MemoryCandidate,
    RagAdmissionDecision,
)
from app.memory.admission.evaluator import RagAdmissionEvaluator
from app.memory.admission.templates import render_normalized_text
from app.memory.index_sync import MemoryIndexSync
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.contracts import (
    ApprovedMemory,
    ApprovedMemoryDetail,
    FormationMethod,
    MemorySourceInput,
    MemorySourceType,
    MemoryStatus,
    MemoryWriteContext,
    TemporaryProviderRecord,
    TemporaryProviderRecordCorrection,
)
from app.memory.persistence.temporary_repository import TemporaryProviderRecordRepository
from app.privacy.contracts import PrivacyScanner
from app.privacy.semantic.classifier import SemanticPrivacyClassifier
from app.privacy.semantic.contracts import ADMISSION


class MemoryCorrectionRejected(Exception):
    def __init__(self, *, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class PersonaMemoryProvider:
    def __init__(
        self,
        *,
        approved_repository: ApprovedMemoryRepository,
        scanner: PrivacyScanner,
        classifier: SemanticPrivacyClassifier,
        admission_evaluator: RagAdmissionEvaluator,
        index_sync: MemoryIndexSync,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = approved_repository
        self._scanner = scanner
        self._classifier = classifier
        self._evaluator = admission_evaluator
        self._index_sync = index_sync
        self._clock = clock

    def list(self, *, character_id: str, status: str) -> list[dict[str, object]]:
        memories = self._repository.list_by_provider(
            character_id=character_id,
            provider_id="core",
            status=MemoryStatus(status),
        )
        responses: list[dict[str, object]] = []
        for memory in memories:
            detail = self._repository.get_detail(
                character_id=character_id,
                provider_id="core",
                memory_id=memory.id,
            )
            if detail is None:
                raise RuntimeError("listed memory could not be read")
            responses.append(self._detail_response(detail))
        return responses

    def get(self, *, character_id: str, memory_id: UUID) -> dict[str, object] | None:
        detail = self._repository.get_detail(
            character_id=character_id,
            provider_id="core",
            memory_id=memory_id,
        )
        if detail is None:
            return None
        return self._detail_response(detail)

    def correct(
        self,
        *,
        character_id: str,
        memory_id: UUID,
        candidate: MemoryCandidate,
        idempotency_key: UUID,
    ) -> ApprovedMemory:
        current = self._repository.get(character_id=character_id, memory_id=memory_id)
        if current is None:
            raise LookupError("approved memory was not found")
        slot_scans = {
            key: self._scanner.scan(value)
            for key, value in self._evaluator.slot_values(
                candidate.structured_value
            ).items()
        }
        normalized_text = render_normalized_text(candidate.structured_value)
        assessment = self._classifier.classify(normalized_text, ADMISSION)
        result = self._evaluator.evaluate_manual_correction(
            candidate_slot_scans=slot_scans,
            assessment=assessment,
            candidate=candidate,
        )
        if result.decision is not RagAdmissionDecision.ALLOW_STRUCTURED:
            raise MemoryCorrectionRejected(reason_code=result.decision.value)
        if result.candidate is None:
            raise RuntimeError("ALLOW_STRUCTURED requires an approved candidate")
        return self._repository.correct(
            character_id=character_id,
            memory_id=memory_id,
            candidate=result.candidate,
            context=MemoryWriteContext(
                formation_method=FormationMethod.DIRECT,
                idempotency_key=str(idempotency_key),
                occurred_at=current.occurred_at,
                occurred_timezone=current.occurred_timezone,
                occurred_precision=current.occurred_precision,
                stated_at=current.stated_at,
                expires_at=current.expires_at,
                policy_version=assessment.policy_version,
                classifier_version=assessment.classifier_version,
                model_id=assessment.model_id,
                model_digest=assessment.model_digest,
                prompt_version="manual-correction",
                sources=(
                    MemorySourceInput(
                        source_type=MemorySourceType.USER_CORRECTION,
                        source_provider_id="core",
                        source_ref=str(idempotency_key),
                    ),
                ),
            ),
        )

    def hard_delete(self, *, character_id: str, memory_id: UUID) -> None:
        self._repository.hard_delete(character_id=character_id, memory_id=memory_id)
        self._index_sync.delete_after_commit(
            character_id=character_id, memory_id=memory_id
        )

    def _detail_response(self, detail: ApprovedMemoryDetail) -> dict[str, object]:
        return self._memory_response(detail.memory, detail.sources, detail.lineage)

    def _memory_response(
        self, memory: ApprovedMemory, sources: tuple[object, ...], lineage: tuple[object, ...]
    ) -> dict[str, object]:
        return {
            "id": memory.id,
            "character_id": memory.character_id,
            "provider_id": memory.provider_id,
            "memory_kind": memory.memory_kind,
            "memory_type": memory.memory_type.value,
            "normalized_text": memory.normalized_text,
            "structured_value": asdict(memory.structured_value),
            "effective_at": memory.occurred_at,
            "status": memory.status.value,
            "content_version": memory.content_version,
            "index_pending": self._repository.is_index_pending(
                character_id=memory.character_id, memory_id=memory.id
            ),
            "sources": sources,
            "lineage": lineage,
        }


class AddonRecordProvider:
    def __init__(self, repository: TemporaryProviderRecordRepository) -> None:
        self._repository = repository

    def list(
        self, *, character_id: str, provider_id: str
    ) -> list[TemporaryProviderRecord]:
        return self._repository.list_by_provider(
            character_id=character_id, provider_id=provider_id
        )

    def get(
        self, *, character_id: str, provider_id: str, record_id: UUID
    ) -> TemporaryProviderRecord | None:
        return self._repository.get(
            character_id=character_id, provider_id=provider_id, record_id=record_id
        )

    def correct(
        self,
        *,
        character_id: str,
        provider_id: str,
        record_id: UUID,
        correction: TemporaryProviderRecordCorrection,
    ) -> TemporaryProviderRecord:
        return self._repository.correct(
            character_id=character_id,
            provider_id=provider_id,
            record_id=record_id,
            correction=correction,
        )

    def hard_delete(
        self, *, character_id: str, provider_id: str, record_id: UUID
    ) -> None:
        self._repository.hard_delete(
            character_id=character_id, provider_id=provider_id, record_id=record_id
        )
