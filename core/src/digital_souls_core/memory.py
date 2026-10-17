"""Canonical memory context with current-policy and source authorization."""

import json
from collections.abc import Callable

from .application import CoreError
from .character import AccessScope, Character, GuardedContext
from .history import Binding
from .memory_record_store import RetrievalCandidate
from .memory_records import Episode, PartialDateTime, TemporalValue
from .memory_retrieval import MemoryQueryUnavailable as MemoryQueryUnavailable
from .memory_retrieval import MemoryRetrieval
from .privacy import PrivacyPolicy


def _time(value: TemporalValue) -> dict[str, object]:
    def partial(v: PartialDateTime | None) -> dict[str, object] | None:
        if v is None:
            return None
        return {
            "precision": v.precision.value,
            **{
                k: getattr(v, k)
                for k in ("year", "month", "day", "hour", "minute", "second")
                if getattr(v, k) is not None
            },
        }

    return {"start": partial(value.start), "end": partial(value.end), "timezone": value.timezone}


class MemoryContext:
    """Opt-in conversation-only context carrying an authorization guard until dispatch."""

    def __init__(self, service: MemoryRetrieval) -> None:
        self.service = service

    @property
    def policy(self) -> PrivacyPolicy:
        return self.service.policy

    async def context(
        self,
        character: Character,
        scope: AccessScope,
        user_text: str,
        *,
        authorized: Callable[[], bool],
    ) -> GuardedContext:
        binding = Binding(scope, character.config.character_id)
        policy = self.service.policy
        stamp = policy.stamp
        memories: tuple[RetrievalCandidate, ...] = ()
        try:
            versions = self.service._classifier()
            retrieval = self.service._retrieval_stamp()
            memory_store = self.service.records
            ranking = self.service.retrieval
            ranking_stamp = self.service._ranking_stamp()
            if user_text and policy.permits(binding, "memory"):
                memories = await self.service.search(
                    binding, user_text[:256], authorized=authorized
                )
        except CoreError:
            # Failed retrieval is optional, but conversation authorization remains
            # pinned before lookup; discarded memory/configuration is not reused.
            return GuardedContext(
                "",
                lambda: (authorized() and self.service.policy is policy and policy.stamp == stamp),
                policy,
            )
        # Opaque storage identifiers are not model evidence. Use request-local
        # references while retaining the real identities in the dispatch guard.
        conversation_refs: dict[str, str] = {}
        for memory in memories:
            for citation in memory.citations:
                source = citation.source
                cid = source.reference.conversation_id
                if cid not in conversation_refs:
                    conversation_refs[cid] = f"conversation-{len(conversation_refs) + 1}"
        # Keep immutable memory/source objects in the closure after serialization.
        items = tuple(
            json.dumps(
                {
                    "memory_ref": f"memory-{index + 1}",
                    "kind": "episode" if isinstance(m.record, Episode) else "semantic",
                    "text": m.record.normalized_text,
                    **(
                        {"experience_time": _time(m.record.experience_time)}
                        if isinstance(m.record, Episode)
                        else {"applicability": _time(m.record.applicability)}
                    ),
                    "facts": [
                        {"text": f.normalized_text, "target_time": _time(f.target_time)}
                        for f in m.facts
                    ],
                    "sources": [
                        {
                            "conversation_ref": conversation_refs[source.reference.conversation_id],
                            "turn_revision": source.reference.turn_revision,
                            "message_index": source.reference.message_index,
                            "epoch": source.epoch,
                        }
                        for source in dict.fromkeys(c.source for c in m.citations)
                    ],
                },
                ensure_ascii=False,
            )
            for index, m in enumerate(memories)
        )
        text = "[" + ", ".join(items) + "]" if items else ""

        def valid() -> bool:
            try:
                self.service._classifier()
                return (
                    authorized()
                    and self.service.policy is policy
                    and policy.stamp == stamp
                    and self.service._classifier() == versions
                    and self.service.retrieval is ranking
                    and self.service._ranking_stamp() == ranking_stamp
                    and self.service._retrieval_stamp() == retrieval
                    and self.service.records is memory_store
                    and (
                        not memories
                        or (
                            policy.permits(binding, "memory")
                            and policy.permits(binding, "local")
                            and memory_store.current(binding, memories)
                        )
                    )
                )
            except CoreError:
                return False

        return GuardedContext(text, valid, policy, items)
