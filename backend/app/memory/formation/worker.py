from __future__ import annotations

import logging
from typing import Protocol, TypeGuard
from uuid import UUID

from app.conversation_history.models import ConversationTurn, TurnStatus
from app.memory.admission.contracts import MemoryCandidate
from app.memory.formation.contracts import MemoryFormationJob
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
    ) -> tuple[MemoryCandidate, ...]: ...


class AdmissionService(Protocol):
    def admit(
        self,
        candidate: MemoryCandidate,
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
        if not _is_eligible(current):
            return
        previous = self._repository.get_previous_completed_turn(
            job.character_id, job.conversation_id, job.turn_id
        )
        try:
            candidates = self._extractor.extract(
                current_turn=current,
                previous_turn=previous,
            )
            if self._domain_router is not None:
                self._domain_router.dispatch(current)
            for index, candidate in enumerate(candidates):
                self._admission.admit(
                    candidate,
                    character_id=job.character_id,
                    conversation_id=job.conversation_id,
                    turn_id=job.turn_id,
                    candidate_index=index,
                )
        except Exception as error:
            logger.warning(
                "memory formation job failed: error_type=%s",
                type(error).__name__,
            )


def _is_eligible(turn: ConversationTurn | None) -> TypeGuard[ConversationTurn]:
    return (
        turn is not None
        and turn.status is TurnStatus.COMPLETED
        and turn.user_content is not None
        and turn.assistant_content is not None
    )
