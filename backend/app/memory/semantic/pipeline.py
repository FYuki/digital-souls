"""引用解決・privacy・出典版・原子的保存を本番と性能評価で共用する。"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from app.memory.episodic.contracts import ExtractionIdentity, FormationStamp
from app.memory.episodic.privacy import PrivacyAssessmentUnavailable
from app.memory.semantic.contracts import SemanticRecord, SemanticSource
from app.memory.semantic.extractor import InputPart, ResolvedProposal, SemanticExtractor, resolve_proposals
from app.memory.semantic.repository import SemanticConflict
from app.memory.semantic.service import SemanticStore
from app.memory.semantic.sources import is_masked, validate_sources


@dataclass(frozen=True)
class PipelineResult:
    proposed: tuple[ResolvedProposal, ...]
    saved: tuple[SemanticRecord, ...]
    rejected: int
    replayed: bool = False


class SemanticDeferred(RuntimeError):
    """会話優先・終了要求によりチェックポイントを進めず延期する。"""


class SemanticPipeline:
    def __init__(self, store: SemanticStore, extractor: SemanticExtractor, *, timezone: str) -> None:
        self.store, self.extractor, self.timezone = store, extractor, timezone

    def process(
        self, *, character_id: str, conversation_id: UUID, source_id: UUID, revision: int,
        batches: tuple[tuple[InputPart, ...], ...], catalog: tuple[SemanticRecord, ...],
        extraction: ExtractionIdentity, should_defer: Callable[[], bool] = lambda: False,
    ) -> PipelineResult:
        proposed: list[ResolvedProposal] = []
        for parts in batches:
            if should_defer():
                raise SemanticDeferred()
            output = self.extractor.extract(parts=parts, catalog=catalog)
            proposed.extend(resolve_proposals(output, parts=parts, catalog=catalog,
                                             character_id=character_id, timezone=self.timezone))
        prepared: list[tuple[ResolvedProposal, FormationStamp]] = []
        rejected = 0
        for proposal in proposed:
            if should_defer():
                raise SemanticDeferred()
            with self.store.source_guard.snapshot() as (history, cutoff), self.store.repository.read() as tx:
                texts = validate_sources(history, cutoff, tx, character_id=character_id,
                                         sources=proposal.candidate.sources, episode_reader=self.store.episode_reader)
                masked = is_masked(proposal.candidate.sources, tx.masks(character_id))
            if masked:
                rejected += 1
                continue
            review = self.store.reviewer.review(proposal.candidate.proposition, texts)
            if review.retryable:
                raise PrivacyAssessmentUnavailable(review.reason)
            if not review.allowed or review.stamp is None:
                rejected += 1
                continue
            prepared.append((proposal, review.stamp.model_copy(update={"extraction": extraction})))
        all_sources = tuple({source.identity: source for parts in batches for part in parts
                             if (source := self._source(part)) is not None}.values())
        if should_defer():
            raise SemanticDeferred()
        with self.store.source_guard.snapshot() as (history, cutoff), self.store.repository.transaction(now=self.store.clock()) as tx:
            validate_sources(history, cutoff, tx, character_id=character_id,
                             sources=all_sources, episode_reader=self.store.episode_reader)
            if tx.processed(character_id, conversation_id, source_id, revision):
                return PipelineResult(tuple(proposed), (), rejected, replayed=True)
            saved: list[SemanticRecord] = []
            seen: set[str] = set()
            for index, (proposal, stamp) in enumerate(prepared):
                # 分割の重なりで同じ命題を再提示されても同一batchでは重ねて保存しない。
                identity = proposal.candidate.proposition.model_dump_json()
                if identity in seen:
                    continue
                seen.add(identity)
                if is_masked(proposal.candidate.sources, tx.masks(character_id)):
                    rejected += 1
                    continue
                target = proposal.target
                if target is not None:
                    current = tx.get(character_id, target.id)
                    if current is None or current.content_version != target.content_version:
                        raise SemanticConflict("semantic catalog changed during extraction")
                    validate_sources(history, cutoff, tx, character_id=character_id,
                                     sources=current.sources, episode_reader=self.store.episode_reader)
                saved.append(tx.apply(
                    character_id=character_id, candidate=proposal.candidate, stamp=stamp,
                    receipt_key=f"turn:{conversation_id}:{source_id}:{revision}:{index}",
                    operation=proposal.operation, target_id=target.id if target else None,
                    target_version=target.content_version if target else None,
                ))
            tx.mark_processed(character_id, conversation_id, source_id, revision)
            return PipelineResult(tuple(proposed), tuple(saved), rejected)

    @staticmethod
    def _source(part: InputPart) -> SemanticSource | None:
        from app.memory.episodic.contracts import SourceSpan
        fragment = part.fragment
        turn = fragment.source.turn
        if not fragment.text:
            return None
        span = SourceSpan(source_id=turn.turn_id, revision=fragment.source.revision, role=fragment.role,
                          start=fragment.start, end=fragment.end,
                          stated_at=turn.created_at if fragment.role == "user" else turn.updated_at)
        return SemanticSource(kind="CONVERSATION", source_id=turn.turn_id, revision=fragment.source.revision,
                              conversation_id=turn.conversation_id, span=span)
