"""直接取得と#100の派生結果に共通する、検証・保存・失効の入口。"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from app.memory.episodic.contracts import ExtractionIdentity
from app.memory.episodic.privacy import PrivacyAssessmentUnavailable, PrivacyReview
from app.memory.episodic.read_repository import EpisodicReadRepository
from app.memory.episodic.sources import ConversationSourceGuard, InvalidConversationSource
from app.memory.semantic.contracts import (
    FormationType, Proposition, SemanticCandidate, SemanticOperation, SemanticRecord,
    SemanticSource, SemanticStatus,
)
from app.memory.semantic.repository import SemanticConflict, SemanticRepository
from app.memory.semantic.sources import is_masked, validate_sources


class Reviewer(Protocol):
    def review(self, proposition: Proposition, source_texts: tuple[str, ...]) -> PrivacyReview: ...


class SemanticRejected(ValueError):
    """候補は正本へ保存せず、許可されたreasonのみ返す。"""


class SemanticStore:
    def __init__(
        self, *, repository: SemanticRepository, source_guard: ConversationSourceGuard,
        episode_reader: EpisodicReadRepository, reviewer: Reviewer,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository, self.source_guard = repository, source_guard
        self.episode_reader, self.reviewer, self.clock = episode_reader, reviewer, clock

    def save(
        self, *, character_id: str, candidate: SemanticCandidate, receipt_key: str,
        operation: SemanticOperation = SemanticOperation.NEW, target_id: UUID | None = None,
        target_version: int | None = None, extraction: ExtractionIdentity | None = None,
    ) -> SemanticRecord:
        if any(s.kind == "MANUAL" for s in candidate.sources):
            raise SemanticRejected("manual evidence requires the correction entrypoint")
        return self._save(character_id=character_id, candidate=candidate, receipt_key=receipt_key,
                          operation=operation, target_id=target_id, target_version=target_version,
                          extraction=extraction)

    def _save(
        self, *, character_id: str, candidate: SemanticCandidate, receipt_key: str,
        operation: SemanticOperation, target_id: UUID | None, target_version: int | None,
        extraction: ExtractionIdentity | None = None, manual: bool = False,
    ) -> SemanticRecord:
        with self.source_guard.snapshot() as (history, cutoff), self.repository.read() as tx:
            replay = tx.replay(character_id, receipt_key)
            if replay is not None:
                return replay
            texts = validate_sources(history, cutoff, tx, character_id=character_id,
                                     sources=candidate.sources, episode_reader=self.episode_reader)
            if is_masked(candidate.sources, tx.masks(character_id)):
                raise SemanticRejected("deleted source cannot regenerate semantic memory")
        review = self.reviewer.review(candidate.proposition, (candidate.proposition.content,) if manual else texts)
        if review.retryable:
            raise PrivacyAssessmentUnavailable(review.reason)
        if not review.allowed or review.stamp is None:
            raise SemanticRejected(review.reason)
        stamp = review.stamp.model_copy(update={"extraction": extraction}) if extraction else review.stamp
        with self.source_guard.snapshot() as (history, cutoff), self.repository.transaction(now=self.clock()) as tx:
            validate_sources(history, cutoff, tx, character_id=character_id,
                             sources=candidate.sources, episode_reader=self.episode_reader)
            if is_masked(candidate.sources, tx.masks(character_id)):
                raise SemanticRejected("deleted source cannot regenerate semantic memory")
            if target_id is not None:
                target = tx.get(character_id, target_id)
                if target is None:
                    raise SemanticConflict("semantic target is unavailable")
                validate_sources(history, cutoff, tx, character_id=character_id,
                                 sources=target.sources, episode_reader=self.episode_reader)
            return tx.apply(
                character_id=character_id, candidate=candidate, stamp=stamp,
                receipt_key=receipt_key, operation=operation,
                target_id=target_id, target_version=target_version,
            )

    def correct(
        self, *, character_id: str, record_id: UUID, version: int,
        proposition: Proposition, receipt_id: UUID,
    ) -> SemanticRecord:
        with self.repository.read() as tx:
            current = tx.get(character_id, record_id)
            if (current is None or current.formation_type is not FormationType.DIRECT_EXTRACTION
                    or current.proposition is None or not current.proposition.self_report):
                raise SemanticRejected("only self-report semantic can be manually corrected")
            if not proposition.self_report:
                raise SemanticRejected("manual correction must remain a self report")
        candidate = SemanticCandidate(
            formation_type=FormationType.DIRECT_EXTRACTION, proposition=proposition,
            sources=(SemanticSource(kind="MANUAL", source_id=receipt_id, revision=1),),
            confidence=1,
        )
        return self._save(
            character_id=character_id, candidate=candidate, receipt_key="manual:"+str(receipt_id),
            operation=SemanticOperation.CORRECT, target_id=record_id, target_version=version, manual=True,
        )

    def reconcile(self, character_id: str) -> tuple[SemanticRecord, ...]:
        """出典更新のworkerを待たず、読み取り時にも現在版で失効させる。"""
        with self.source_guard.snapshot() as (history, cutoff), self.repository.transaction(now=self.clock()) as tx:
            for record in tx.list_records(character_id):
                if record.status in {SemanticStatus.DELETED, SemanticStatus.INACTIVE}:
                    continue
                try:
                    validate_sources(history, cutoff, tx, character_id=character_id,
                                     sources=record.sources, episode_reader=self.episode_reader)
                except (InvalidConversationSource, SemanticConflict):
                    tx.invalidate(record, reason="SOURCE_INVALID")
            return tx.list_records(character_id)

    def delete(self, *, character_id: str, record_id: UUID, version: int) -> None:
        with self.repository.transaction(now=self.clock()) as tx:
            tx.hard_delete(character_id, record_id, version=version)
        self.repository.database.truncate_wal()
