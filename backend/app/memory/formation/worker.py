from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from app.conversation_history.models import ConversationTurn, TurnStatus
from app.memory.formation.contracts import ExtractedMemoryCandidate, MemoryFormationJob
from app.memory.formation.domain_router import DomainRecordRouter

logger = logging.getLogger(__name__)


class FormationConversationRepository(Protocol):
    def get_turn(
        self, character_id: str, conversation_id: UUID, turn_id: UUID
    ) -> ConversationTurn | None: ...

    def get_previous_completed_turn(
        self, character_id: str, conversation_id: UUID, turn_id: UUID
    ) -> ConversationTurn | None: ...


class CandidateExtractor(Protocol):
    def extract(
        self,
        *,
        current_turn: ConversationTurn,
        previous_turn: ConversationTurn | None,
    ) -> tuple[ExtractedMemoryCandidate, ...]: ...


class AdmissionService(Protocol):
    def admit(
        self,
        candidate: ExtractedMemoryCandidate,
        *,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
        candidate_index: int,
    ) -> object: ...


class MemoryFormationWorker:
    def __init__(
        self,
        *,
        conversation_repository: FormationConversationRepository,
        extractor: CandidateExtractor,
        admission_service: AdmissionService,
        domain_router: DomainRecordRouter | None,
    ) -> None:
        self._repository = conversation_repository
        self._extractor = extractor
        self._admission = admission_service
        self._domain_router = domain_router

    def process(self, job: MemoryFormationJob) -> None:
        current = self._repository.get_turn(
            job.character_id, job.conversation_id, job.turn_id
        )
        if current is None or not _is_eligible(current):
            return
        previous = self._repository.get_previous_completed_turn(
            job.character_id, job.conversation_id, job.turn_id
        )
        if previous is not None and (
            previous.user_content is None or previous.assistant_content is None
        ):
            previous = None
        try:
            candidates = self._extractor.extract(
                current_turn=current,
                previous_turn=previous,
            )
            if self._domain_router is not None:
                self._domain_router.dispatch(current)
        except Exception as error:
            logger.warning(
                "memory formation job failed: error_type=%s",
                type(error).__name__,
            )
            return
        for index, candidate in enumerate(candidates):
            try:
                self._admission.admit(
                    candidate,
                    character_id=job.character_id,
                    conversation_id=job.conversation_id,
                    turn_id=job.turn_id,
                    candidate_index=index,
                )
            except Exception as error:
                logger.warning(
                    "memory candidate admission failed: candidate_index=%d "
                    "memory_type=%s error_type=%s",
                    index,
                    candidate.candidate.memory_type.value,
                    type(error).__name__,
                )


def _is_eligible(turn: ConversationTurn) -> bool:
    return (
        turn.status is TurnStatus.COMPLETED
        and turn.user_content is not None
        and turn.assistant_content is not None
    )
