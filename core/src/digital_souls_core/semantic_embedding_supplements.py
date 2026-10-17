"""Local-only, body-free descriptive analyses, separate from canonical retrieval.

NoMIRACL text is deterministically clipped to 200 Unicode characters (including
title) and queries to 128, for equal bounded inputs across 512-token models.
MKQA compares symmetric query/query embeddings, all non-pairs in both directions.
These are descriptive aids, never a memory-quality or privacy acceptance gate.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .memory_ranking import MemoryEmbedding
from .privacy_scan import scan
from .semantic_embedding_candidates import (
    PrefixEmbedding,
    TimedEmbedding,
    distribution,
    separation_auc,
)
from .semantic_retrieval_evaluation import independent_relevance


class _Input(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class _Row(_Input):
    source_id: str


class Passage(_Input):
    id: str
    title: str = Field(min_length=1, max_length=10000, repr=False)
    text: str = Field(min_length=1, max_length=1000000, repr=False)
    relevance: Literal[0, 1]


class NoMiracl(_Row):
    schema_version: Literal[1]
    dataset: Literal["nomiracl-ja"]
    source_id: str
    subset: Literal["relevant", "non_relevant"]
    query: str = Field(min_length=1, max_length=10000, repr=False)
    passages: list[Passage] = Field(min_length=1, max_length=100, repr=False)


class Mkqa(_Row):
    schema_version: Literal[1]
    dataset: Literal["mkqa-ja-en"]
    source_id: str
    ja: str = Field(min_length=1, max_length=10000, repr=False)
    en: str = Field(min_length=1, max_length=10000, repr=False)


def load_rows[T: _Row](path: Path, model: type[T], maximum: int) -> list[T]:
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Supplement too large")
    rows = [
        model.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    if not rows or len(rows) > maximum or len({r.source_id for r in rows}) != len(rows):
        raise ValueError("Invalid supplement rows")
    return rows


async def symmetric_vectors(
    embedding: MemoryEmbedding, prefix: str, texts: tuple[str, ...]
) -> tuple[tuple[float, ...], ...]:
    vectors: list[tuple[float, ...]] = []
    for i in range(0, len(texts), 4):
        vectors.extend(await embedding.embed(tuple(prefix + t for t in texts[i : i + 4])))
    return tuple(vectors)


async def evaluate_supplements(directory: Path, embedding: PrefixEmbedding) -> dict[str, Any]:
    nomiracl_path = directory / "nomiracl-ja.jsonl"
    mkqa_path = directory / "mkqa-ja-en.jsonl"
    manifest = json.loads((directory / "manifest.json").read_text())
    if (
        manifest["nomiracl"]["revision"] != "ecd08778d0426a5ca28ac99763b0c9ddc2c78e68"
        or manifest["mkqa"]["revision"] != "f964fe1bac4d580cee3d4928572df1f676f8e656"
    ):
        raise ValueError("Unexpected supplement revision")
    nomiracl = load_rows(nomiracl_path, NoMiracl, 400)
    mkqa = load_rows(mkqa_path, Mkqa, 1000)
    if (
        len(nomiracl) != manifest["nomiracl"]["counts"]["queries"]
        or len(mkqa) != manifest["mkqa"]["counts"]["pairs"]
        or len(mkqa) < 2
    ):
        raise ValueError("Incomplete supplements")
    # Apply the same raw-input scanner preflight to every candidate. Do not
    # bypass LocalEmbedding's fail-closed contract for quote-leading questions.
    # The original files remain intact; excluded pairs are explicitly NOT RUN.
    original_pairs = len(mkqa)
    blocked = [row for row in mkqa if (finding := scan((row.ja, row.en))).secret or finding.failed]
    blocked_ids = {row.source_id for row in blocked}
    mkqa = [row for row in mkqa if row.source_id not in blocked_ids]
    if len(mkqa) < 2:
        raise ValueError("Insufficient scanner-authorized bilingual pairs")
    timed = TimedEmbedding(embedding.delegate)
    asymmetric = PrefixEmbedding(timed, embedding.query_prefix, embedding.document_prefix)
    positive: list[float] = []
    negative: list[float] = []
    for row in nomiracl:
        for offset in range(0, len(row.passages), 2):
            passages = row.passages[offset : offset + 2]
            vectors = await asymmetric.embed(
                (row.query[:128], *((p.title + "\n" + p.text)[:200] for p in passages))
            )
            for p, vector in zip(passages, vectors[1:], strict=True):
                score = independent_relevance(vectors[0], vector)
                (positive if p.relevance else negative).append(score)
    ja = await symmetric_vectors(timed, embedding.query_prefix, tuple(row.ja for row in mkqa))
    en = await symmetric_vectors(timed, embedding.query_prefix, tuple(row.en for row in mkqa))
    bilingual: dict[str, Any] = {}
    for name, queries, documents in (("ja_to_en", ja, en), ("en_to_ja", en, ja)):
        pairs: list[float] = []
        non_pairs: list[float] = []
        for i, query in enumerate(queries):
            for j, document in enumerate(documents):
                score = independent_relevance(query, document)
                (pairs if i == j else non_pairs).append(score)
        bilingual[name] = {
            "pairs": distribution(tuple(pairs)),
            "non_pairs": distribution(tuple(non_pairs)),
            "auc": separation_auc(tuple(pairs), tuple(non_pairs)),
        }
    return {
        "status": "COMPLETED with NOT RUN pairs; descriptive only"
        if blocked
        else "COMPLETED; descriptive only",
        "sources": {
            "nomiracl_revision": manifest["nomiracl"]["revision"],
            "mkqa_revision": manifest["mkqa"]["revision"],
            "converted_files": {
                p.name: {
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                    "bytes": p.stat().st_size,
                }
                for p in (nomiracl_path, mkqa_path)
            },
            "nomiracl_queries": len(nomiracl),
            "mkqa_original_pairs": original_pairs,
            "mkqa_measured_pairs": len(mkqa),
            "mkqa_not_run_pairs": len(blocked),
            "mkqa_not_run_reason": "raw-input scanner secret/failed; universal preflight",
        },
        "nomiracl": {
            "query_max_characters": 128,
            "title_plus_passage_max_characters": 200,
            "input_truncation": "first Unicode characters; identical limits for all candidates",
            "relevant": distribution(tuple(positive)),
            "non_relevant": distribution(tuple(negative)),
            "auc": separation_auc(tuple(positive), tuple(negative)),
        },
        "mkqa": {"prefix_roles": "query/query (symmetric)", **bilingual},
        "timing": timed.summary(),
    }
