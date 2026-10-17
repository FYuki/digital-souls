#!/usr/bin/env python3
"""Content-free report CLI. Requires an explicit disposable PostgreSQL socket."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from digital_souls_core.postgres_db import PostgresConfig
from digital_souls_core.semantic_evaluation_cases import load_evaluation_cases
from digital_souls_core.semantic_retrieval_evaluation import (
    case_version,
    evaluate_retrieval,
    execution_commit,
    select_embedding,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/semantic/cases.json")
    parser.add_argument(
        "--expectations", type=Path, default=ROOT / "evals/semantic/expectations.json"
    )
    args = parser.parse_args()
    try:
        if args.runs < 1:
            raise ValueError
        data = load_evaluation_cases(args.cases, args.expectations)
        embedding, mode = select_embedding(data, args.profile)
        config = PostgresConfig(
            host=os.environ["DSC_TEST_POSTGRES_SOCKET"],
            port=int(os.environ["DSC_TEST_POSTGRES_PORT"]),
            database=os.environ["DSC_TEST_POSTGRES_DATABASE"],
            user=os.environ["DSC_TEST_POSTGRES_USER"],
        )
        if not config.host.startswith("/"):
            raise ValueError
        report = asyncio.run(
            evaluate_retrieval(
                data,
                config,
                embedding,
                runs=args.runs,
                mode=mode,
                commit=execution_commit(ROOT),
                case_version=case_version(data, args.cases, args.expectations),
            )
        )
        output = report.model_dump_json(indent=2) + "\n"
        if args.output is not None:
            args.output.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        return 0 if report.passed else 1
    except Exception:
        print("検索評価に失敗しました（設定・入力・結果を確認してください）。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
