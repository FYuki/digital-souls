#!/usr/bin/env python3
"""Explicit local-profile candidate CLI: only the fixed 89 tuning cases are read.

Each invocation runs at most three repeats for one candidate. Run under the
existing disposable PostgreSQL runner. stdout/stderr never contain case bodies.
A completed FAIL comparison exits 1, an execution failure exits 2.
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from digital_souls_core.postgres_db import PostgresConfig
from digital_souls_core.semantic_embedding_candidates import (
    PrefixEmbedding,
    evaluate_candidate,
    tuning_paths,
)
from digital_souls_core.semantic_embedding_supplements import evaluate_supplements
from digital_souls_core.semantic_evaluation_cases import load_evaluation_cases
from digital_souls_core.semantic_retrieval_evaluation import (
    case_version,
    execution_commit,
    select_embedding,
)

ROOT = Path(__file__).resolve().parents[1]
PREFIXES = {
    "none": ("", ""),
    "nomic": ("search_query: ", "search_document: "),
    "e5": ("query: ", "passage: "),
}


async def run(args: argparse.Namespace) -> dict[str, object]:
    cases, expectations = tuning_paths(ROOT)
    data = load_evaluation_cases(cases, expectations)
    delegate, mode = select_embedding(data, args.profile)
    if mode != "local_model":
        raise ValueError("Explicit local model required")
    embedding = PrefixEmbedding(delegate, *PREFIXES[args.prefix])
    if args.supplements_only:
        if args.supplements is None:
            raise ValueError("Supplements required")
        return {
            "schema_version": 1,
            "purpose": "local supplements only; descriptive",
            "commit": execution_commit(ROOT).model_dump(),
            "space": asdict(embedding.space),
            "supplements": await evaluate_supplements(args.supplements, embedding),
        }
    config = PostgresConfig(
        host=os.environ["DSC_TEST_POSTGRES_SOCKET"],
        port=int(os.environ["DSC_TEST_POSTGRES_PORT"]),
        database=os.environ["DSC_TEST_POSTGRES_DATABASE"],
        user=os.environ["DSC_TEST_POSTGRES_USER"],
    )
    if not config.host.startswith("/"):
        raise ValueError("Disposable socket required")
    report = await evaluate_candidate(
        data,
        config,
        embedding,
        runs=args.runs,
        commit=execution_commit(ROOT),
        version=case_version(data, cases, expectations),
        progress=lambda n: print(f"Completed tuning run {n}.", file=sys.stderr, flush=True),
    )
    if args.supplements is not None:
        report["supplements"] = await evaluate_supplements(args.supplements, embedding)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--prefix", choices=tuple(PREFIXES), required=True)
    parser.add_argument("--runs", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--supplements", type=Path)
    parser.add_argument("--supplements-only", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        baseline = report.get("baseline")
        return 0 if not isinstance(baseline, dict) or baseline["passed"] else 1
    except Exception:
        print("Candidate evaluation failed; no completed report was produced.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
