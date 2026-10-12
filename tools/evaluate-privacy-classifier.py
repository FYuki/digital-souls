#!/usr/bin/env python3
"""Privacy classifier evaluation: offline fixtures or operator-enabled real mode."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from digital_souls_core.application import Provider
from digital_souls_core.character import load_characters
from digital_souls_core.privacy_classifier_evaluation import (
    DEFAULT_ABSTAIN_POLICY,
    validate_options,
)
from digital_souls_core.privacy_classifier_runtime import (
    evaluate,
    fixture_profile,
    fixture_provider,
    real_provider,
)
from digital_souls_core.privacy_evaluation_cases import load_cases
from digital_souls_core.semantic_retrieval_evaluation import execution_commit

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "real"), default="fixture")
    parser.add_argument(
        "--abstain-policy",
        choices=("accept", "reject", "separate"),
        default=DEFAULT_ABSTAIN_POLICY,
        help="Default separate (ADR 0032 user decision C); others are comparison only",
    )
    parser.add_argument(
        "--runs", type=int, help="At least 3; fixture default 3, explicit in real mode"
    )
    parser.add_argument("--characters", type=Path)
    parser.add_argument("--character-id")
    parser.add_argument("--model-digest")
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/privacy/cases.json")
    parser.add_argument(
        "--expectations", type=Path, default=ROOT / "evals/privacy/expectations.json"
    )
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--scenario", default="pass")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        runs = args.runs if args.runs is not None else 3
        validate_options(runs, args.abstain_policy)
        cases = load_cases(args.cases, args.expectations)
        provider: Provider
        if args.mode == "real":
            if (
                args.characters is None
                or not args.character_id
                or not args.model_digest
                or not args.model_digest.strip()
                or args.runs is None
                or args.fixture is not None
                or args.scenario != "pass"
                or args.characters.resolve().is_relative_to(ROOT)
            ):
                raise ValueError("explicit external configuration and pinned digest required")
            character = next(
                c
                for c in load_characters(args.characters)
                if c.config.character_id == args.character_id
            )
            profile = character.config.profile
            digest = args.model_digest
            provider = real_provider()
        else:
            if (
                args.characters is not None
                or args.character_id is not None
                or args.model_digest is not None
            ):
                raise ValueError("fixture profile is fixed and offline")
            profile = fixture_profile()
            digest = "fixture-only-no-quality-evidence"
            provider = fixture_provider(
                cases, args.fixture or ROOT / "evals/privacy/responses.fixture.json", args.scenario
            )
        revision = execution_commit(ROOT)
        report = asyncio.run(
            evaluate(
                cases,
                provider,
                profile,
                model_digest=digest,
                mode=args.mode,
                runs=runs,
                abstain_policy=args.abstain_policy,
                commit=revision.sha,
                commit_dirty=revision.dirty,
            )
        )
        if args.mode == "fixture":
            report["scenario"] = args.scenario
        else:
            report["character_id"] = character.config.character_id
            report["config_version"] = character.config.config_version
        output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(output)
        else:
            args.output.write_text(output, encoding="utf-8")
            print(
                f"{report['status']}: {runs} runs, {len(cases.cases.cases)} cases/run, "
                f"abstain_policy={args.abstain_policy}, "
                f"quality_evidence={report['quality_evidence']}"
            )
            for reason in report["fail_reasons"]:
                print(reason)
        return 0 if report["passed"] else 1
    except Exception:
        print(
            "privacy 分類器評価の設定・入力エラー（本文・パス・例外詳細は非表示）。",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
