#!/usr/bin/env python3
"""Offline persona fixture evaluation. Real profiles are examples only in this stage."""

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from digital_souls_core.character import Profile, load_characters
from digital_souls_core.persona_evaluation import FakeJudge, load_cases
from digital_souls_core.persona_fixtures import fixture_entries
from digital_souls_core.persona_runtime import evaluate
from digital_souls_core.semantic_retrieval_evaluation import execution_commit

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--characters", type=Path, default=ROOT / "evals/persona/synthetic.characters.json"
    )
    parser.add_argument("--character-id", default="luma")
    parser.add_argument(
        "--profile", type=Path, help="Fixture profile override; real transport rejected"
    )
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/persona/synthetic.cases.json")
    parser.add_argument(
        "--fixture", type=Path, default=ROOT / "evals/persona/responses.fixture.json"
    )
    parser.add_argument(
        "--scenario",
        choices=(
            "pass",
            "fail",
            "persona_break",
            "override_followed",
            "mismatch",
            "card_hash_mismatch",
        ),
        default="pass",
    )
    parser.add_argument("--mismatch-policy", choices=("warn", "gate"), default="warn")
    parser.add_argument("--include-responses", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        cases = load_cases(args.cases)
        character = next(
            c
            for c in load_characters(args.characters)
            if c.config.character_id == args.character_id
        )
        if args.profile is not None:
            profile = Profile.model_validate_json(args.profile.read_bytes())
            character = replace(
                character, config=character.config.model_copy(update={"profile": profile})
            )
        # An operator cannot accidentally enable network/GPU via a profile. No
        # LiteLLMProvider or token-count HTTP client is instantiated by this CLI.
        profile = character.config.profile
        if (
            profile.model != "openai/fixture-only"
            or profile.transport != "sdk"
            or profile.token_counter != "estimate"
        ):
            raise ValueError("only the offline fixture profile is executable")
        entries, _ = fixture_entries(character, cases, args.fixture, args.scenario)
        revision = execution_commit(ROOT)
        report = asyncio.run(
            evaluate(
                cases,
                entries,
                judge=FakeJudge(),
                runs=args.runs,
                commit=revision.sha,
                commit_dirty=revision.dirty,
                mismatch_policy=args.mismatch_policy,
                include_responses=args.include_responses,
            )
        )
        report["mode"] = "fixture"
        report["profile"] = profile.model_dump(mode="json", exclude_none=True)
        report["character_id"] = character.config.character_id
        report["scenario"] = args.scenario
        output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(output)
        else:
            args.output.write_text(output, encoding="utf-8")
        return 0 if report["passed"] else 1
    except Exception:
        print("人格評価に失敗しました（設定・入力・結果を確認してください）。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
