from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RetrievalRankingCandidate:
    memory_id: str
    raw_distance: float
    last_user_mentioned_at: datetime | None
    created_at: datetime

    @property
    def relevance(self) -> float:
        # Chromaの既定L2 distanceを、0距離=1かつ単調減少する有界値へ写像する。
        return 1.0 / (1.0 + math.sqrt(self.raw_distance))


def rank_retrieval_candidates(
    candidates: tuple[RetrievalRankingCandidate, ...],
    *,
    relevance_threshold: float,
    equivalence_margin: float,
) -> tuple[RetrievalRankingCandidate, ...]:
    relevant = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.relevance >= relevance_threshold
        ),
        key=lambda candidate: -candidate.relevance,
    )
    ranked: list[RetrievalRankingCandidate] = []
    band: list[RetrievalRankingCandidate] = []
    band_leader_relevance: float | None = None
    for candidate in relevant:
        if band_leader_relevance is None or _within_equivalence_margin(
            band_leader_relevance - candidate.relevance,
            equivalence_margin,
        ):
            band.append(candidate)
            if band_leader_relevance is None:
                band_leader_relevance = candidate.relevance
            continue
        ranked.extend(sorted(band, key=_tie_break_key))
        band = [candidate]
        band_leader_relevance = candidate.relevance
    ranked.extend(sorted(band, key=_tie_break_key))
    return tuple(ranked)


def _within_equivalence_margin(gap: float, margin: float) -> bool:
    return gap <= margin or math.isclose(gap, margin, abs_tol=1e-12)


def _tie_break_key(
    candidate: RetrievalRankingCandidate,
) -> tuple[object, ...]:
    mentioned_at = candidate.last_user_mentioned_at
    return (
        mentioned_at is None,
        -mentioned_at.timestamp() if mentioned_at is not None else 0.0,
        -candidate.created_at.timestamp(),
        candidate.memory_id,
    )
