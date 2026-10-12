"""Private stdio scoring bridge for the pinned promptfoo JavaScript adapter."""

import asyncio
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from .local_embedding import LocalEmbedding
from .postgres_db import PostgresConfig
from .semantic_answer_evaluation import aggregate_answers, score_answer
from .semantic_answer_runtime import (
    CountingAnswerProvider,
    answer_character,
    complete_answer,
    load_profile,
    runtime_identity,
)
from .semantic_evaluation_cases import load_evaluation_cases, parse_evaluation_inputs
from .semantic_evaluation_runtime import isolated_case
from .semantic_retrieval_evaluation import case_version, execution_commit

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "evals/semantic"


def bridge(request: dict[str, Any]) -> dict[str, Any]:
    if request["operation"] == "dry-run":
        return asyncio.run(dry_run(request))
    data = load_evaluation_cases(EVAL / "cases.json", EVAL / "expectations.json")
    gold = {g.id: g for g in data.expectations.cases}
    op = request["operation"]
    if op == "score":
        return score_answer(gold[request["id"]], request["observation"])
    if op == "batch":
        rows = [score_answer(gold[o["id"]], o) for o in request["observations"]]
        return aggregate_answers(data, rows)
    if op == "manifest":
        return {
            "schema_version": 1,
            "commit": execution_commit(ROOT).model_dump(),
            **runtime_identity(request["mode"], request.get("profile")),
            "case_version": case_version(
                data, EVAL / "cases.json", EVAL / "expectations.json"
            ).model_dump(),
            "promptfoo_version": "0.117.2",
        }
    raise ValueError("Invalid bridge operation")


async def dry_run(request: dict[str, Any]) -> dict[str, Any]:
    profile = load_profile(request.get("profile"), "external_model")
    assert profile is not None
    database = PostgresConfig(
        host=os.environ["DSC_TEST_POSTGRES_SOCKET"],
        port=int(os.environ["DSC_TEST_POSTGRES_PORT"]),
        database=os.environ["DSC_TEST_POSTGRES_DATABASE"],
        user=os.environ["DSC_TEST_POSTGRES_USER"],
    )
    if not database.host.startswith("/"):
        raise ValueError("Isolated PostgreSQL required")
    inputs = parse_evaluation_inputs((EVAL / "cases.json").read_text(encoding="utf-8"))
    counter = CountingAnswerProvider()
    for case in inputs.cases:
        with isolated_case(
            database, case, LocalEmbedding(profile.embedding), external=True
        ) as runtime:
            await complete_answer(
                runtime, answer_character(EVAL / "characters.json", case, profile), counter
            )
    return {
        "cases": len(inputs.cases),
        "calls_per_run": counter.calls,
        "input_tokens_per_run": counter.input_tokens,
        "token_estimator": "utf8-byte-estimate",
    }


def main() -> int:
    try:
        request = json.load(sys.stdin)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = bridge(request)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception:
        print('{"error":"answer_evaluation_failed"}')
        return 1


if __name__ == "__main__":
    sys.exit(main())
