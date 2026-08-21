from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from app.conversation_history.models import ConversationTurn, TurnStatus
from app.memory.admission.contracts import (
    ApprovedMemoryCandidate,
    ConversationSource,
    MemoryCandidate,
    RagAdmissionDecision,
    RagAdmissionResult,
)
from app.memory.admission.evaluator import RagAdmissionEvaluator
from app.memory.persistence.contracts import (
    ApprovedMemory,
    FormationMethod,
    MemorySourceInput,
    MemorySourceType,
    MemoryWriteContext,
    TemporalPrecision,
    build_conversation_idempotency_key,
)
from app.privacy.contracts import PrivacyScanner
from app.privacy.semantic.classifier import SemanticPrivacyClassifier
from app.privacy.semantic.contracts import ADMISSION, PrivacyAssessment


class ConversationTurnRepository(Protocol):
    def get_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> ConversationTurn | None: ...


class ApprovedMemoryWriter(Protocol):
    def list_active(self, *, character_id: str) -> list[ApprovedMemory]: ...

    def save(
        self,
        *,
        character_id: str,
        candidate: ApprovedMemoryCandidate,
        context: MemoryWriteContext,
    ) -> ApprovedMemory: ...

    def touch(
        self,
        *,
        character_id: str,
        memory_id: UUID,
        candidate: ApprovedMemoryCandidate,
        mentioned_at: datetime,
    ) -> ApprovedMemory: ...


class RagAdmissionService:
    def __init__(
        self,
        *,
        conversation_repository: ConversationTurnRepository,
        approved_repository: ApprovedMemoryWriter,
        privacy_scanner: PrivacyScanner,
        semantic_classifier: SemanticPrivacyClassifier,
        evaluator: RagAdmissionEvaluator,
        effective_timezone: str,
        extractor_version: str,
    ) -> None:
        self._conversation_repository = conversation_repository
        self._approved_repository = approved_repository
        self._privacy_scanner = privacy_scanner
        self._semantic_classifier = semantic_classifier
        self._evaluator = evaluator
        self._effective_timezone = effective_timezone
        if not extractor_version.strip():
            raise ValueError("extractor_version must not be blank")
        self._extractor_version = extractor_version

    def admit(
        self,
        candidate: MemoryCandidate,
        *,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
        candidate_index: int,
    ) -> RagAdmissionResult:
        source_turn = self._valid_source_turn(
            character_id,
            conversation_id,
            turn_id,
        )
        if source_turn is None:
            return RagAdmissionResult(RagAdmissionDecision.ABSTAIN_UNKNOWN, None)
        source_text = cast(str, source_turn.user_content)

        authoritative_candidate = replace(
            candidate,
            source=ConversationSource(TurnStatus.COMPLETED, True),
        )
        source_scan = self._privacy_scanner.scan(source_text)
        slot_scans = {
            key: self._privacy_scanner.scan(value)
            for key, value in self._evaluator.slot_values(
                authoritative_candidate.structured_value
            ).items()
        }
        assessment: PrivacyAssessment | None = None
        if self._evaluator.requires_semantic_assessment(
            source_scan=source_scan,
            candidate_slot_scans=slot_scans,
            candidate=authoritative_candidate,
        ):
            assessment = self._semantic_classifier.classify(
                source_text,
                ADMISSION,
            )
        result = self._evaluator.evaluate(
            source_scan=source_scan,
            candidate_slot_scans=slot_scans,
            assessment=assessment,
            candidate=authoritative_candidate,
        )
        if result.decision is not RagAdmissionDecision.ALLOW_STRUCTURED:
            return result
        if result.candidate is None or assessment is None:
            raise RuntimeError("ALLOW_STRUCTURED requires a semantic assessment")

        active_memories = self._approved_repository.list_active(
            character_id=source_turn.character_id
        )
        validated_turn = self._valid_source_turn(
            character_id,
            conversation_id,
            turn_id,
        )
        if validated_turn is None:
            return RagAdmissionResult(RagAdmissionDecision.ABSTAIN_UNKNOWN, None)
        if validated_turn.character_id != source_turn.character_id:
            return RagAdmissionResult(RagAdmissionDecision.ABSTAIN_UNKNOWN, None)

        matching = next(
            (
                memory
                for memory in active_memories
                if memory.structured_value == result.candidate.structured_value
            ),
            None,
        )
        if matching is not None:
            self._approved_repository.touch(
                character_id=validated_turn.character_id,
                memory_id=matching.id,
                candidate=result.candidate,
                mentioned_at=validated_turn.created_at,
            )
            return result

        self._approved_repository.save(
            character_id=validated_turn.character_id,
            candidate=result.candidate,
            context=self._write_context(
                validated_turn,
                conversation_id,
                turn_id,
                candidate_index,
                assessment,
            ),
        )
        return result

    def _valid_source_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> ConversationTurn | None:
        turn = self._conversation_repository.get_turn(
            character_id,
            conversation_id,
            turn_id,
        )
        if (
            turn is None
            or turn.character_id != character_id
            or turn.status is not TurnStatus.COMPLETED
            or turn.user_content is None
            or turn.assistant_content is None
        ):
            return None
        return turn

    def _write_context(
        self,
        turn: ConversationTurn,
        conversation_id: UUID,
        turn_id: UUID,
        candidate_index: int,
        assessment: PrivacyAssessment,
    ) -> MemoryWriteContext:
        return MemoryWriteContext(
            formation_method=FormationMethod.EXTRACTED,
            idempotency_key=build_conversation_idempotency_key(
                character_id=turn.character_id,
                conversation_id=str(conversation_id),
                turn_id=str(turn_id),
                candidate_index=candidate_index,
                extractor_version=self._extractor_version,
            ),
            effective_at=turn.created_at,
            effective_timezone=self._effective_timezone,
            temporal_precision=TemporalPrecision.SECOND,
            expires_at=None,
            policy_version=assessment.policy_version,
            classifier_version=assessment.classifier_version,
            model_id=assessment.model_id,
            model_digest=assessment.model_digest,
            prompt_version=assessment.prompt_version,
            sources=(
                MemorySourceInput(
                    MemorySourceType.CONVERSATION_TURN,
                    "core",
                    str(turn_id),
                ),
            ),
            lineage=(),
        )
