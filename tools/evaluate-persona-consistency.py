#!/usr/bin/env python3
"""Persona evaluation: offline fixtures or explicitly selected real model mode."""

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from digital_souls_core.character import Profile, load_characters
from digital_souls_core.persona_evaluation import FakeJudge, load_cases
from digital_souls_core.persona_fixtures import fixture_entries
from digital_souls_core.persona_real import real_entries, real_metadata
from digital_souls_core.persona_runtime import evaluate
from digital_souls_core.semantic_retrieval_evaluation import execution_commit

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--mode", choices=("fixture", "real"), default="fixture")
    parser.add_argument("--characters", type=Path)
    parser.add_argument("--character-id")
    parser.add_argument("--profile", type=Path, help="Fixture-only profile override")
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--max-completion-tokens", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument(
        "--fixture", type=Path, default=ROOT / "evals/persona/responses.fixture.json"
    )
    parser.add_argument(
        "--scenario",
        choices=(
            "pass",
            "fail",
            "persona_break",
            "mismatch",
            "card_hash_mismatch",
            "card_hash_missing",
            "execution_incomplete",
        ),
        default="pass",
    )
    parser.add_argument("--mismatch-policy", choices=("warn", "gate"), default="warn")
    parser.add_argument("--include-responses", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.mode == "real" and (
            args.characters is None
            or args.character_id is None
            or args.cases is None
            or args.profile is not None
            or args.scenario != "pass"
        ):
            raise ValueError("real mode requires explicit characters, character id and cases")
        cases = load_cases(args.cases or ROOT / "evals/persona/synthetic.cases.json")
        character = next(
            c
            for c in load_characters(
                args.characters or ROOT / "evals/persona/synthetic.characters.json"
            )
            if c.config.character_id == (args.character_id or "luma")
        )
        if args.profile is not None:
            profile = Profile.model_validate_json(args.profile.read_bytes())
            character = replace(
                character, config=character.config.model_copy(update={"profile": profile})
            )
        profile = character.config.profile
        settings = {
            name: value
            for name, value in {
                "temperature": args.temperature,
                "max_completion_tokens": args.max_completion_tokens,
                "max_tokens": args.max_tokens,
            }.items()
            if value is not None
        }
        if args.mode == "fixture":
            # Real transport/counter remains rejected before inference in fixture mode.
            if (
                profile.model != "openai/fixture-only"
                or profile.transport != "sdk"
                or profile.token_counter != "estimate"
                or settings
            ):
                raise ValueError("only the offline fixture profile is executable")
            entries, _ = fixture_entries(character, cases, args.fixture, args.scenario)
        else:
            entries = real_entries(character, settings)
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
        if args.mode == "real":
            report.update(real_metadata(character, settings))
        else:
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
