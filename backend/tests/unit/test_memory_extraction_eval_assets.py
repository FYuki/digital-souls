from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EVAL_ROOT = REPOSITORY_ROOT / "backend" / "evals" / "memory_extraction"
PACKAGE_JSON = REPOSITORY_ROOT / "package.json"
REQUIRED_ASSETS = {
    "conformance.yaml",
    "prompt-lab.yaml",
    "cases.jsonl",
    "thresholds.json",
    "gate.py",
    "provider.py",
}


def _cases() -> list[dict[str, object]]:
    with (EVAL_ROOT / "cases.jsonl").open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def _provider_ids(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _provider_ids(child)]
    if isinstance(value, dict):
        own = [str(value["id"])] if "id" in value else []
        return own + [
            item
            for key, child in value.items()
            if key != "id"
            for item in _provider_ids(child)
        ]
    return []


def test_eval_suite_contains_all_executable_assets_and_synthetic_cases() -> None:
    scripts = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"]
    assert {
        "eval:memory:conformance",
        "eval:memory:prompt-lab",
    } <= set(scripts)
    assert {path.name for path in EVAL_ROOT.iterdir()} >= REQUIRED_ASSETS

    cases = _cases()
    assert 30 <= len(cases) <= 50
    assert all(case["synthetic"] is True for case in cases)
    assert len({case["id"] for case in cases}) == len(cases)


def test_conformance_and_prompt_lab_use_their_declared_execution_boundaries() -> None:
    conformance = yaml.safe_load(
        (EVAL_ROOT / "conformance.yaml").read_text(encoding="utf-8")
    )
    prompt_lab = yaml.safe_load(
        (EVAL_ROOT / "prompt-lab.yaml").read_text(encoding="utf-8")
    )

    conformance_ids = _provider_ids(conformance["providers"])
    prompt_lab_ids = _provider_ids(prompt_lab["providers"])
    assert any("provider.py" in provider for provider in conformance_ids)
    assert not any(provider.startswith("ollama:") for provider in conformance_ids)
    assert len(prompt_lab_ids) == 1
    assert prompt_lab_ids[0].startswith("ollama:chat:")
    assert not any(provider.startswith("file://") for provider in prompt_lab_ids)


def test_eval_assertions_separate_enum_topic_and_hallucination_judgements() -> None:
    prompt_lab = yaml.safe_load(
        (EVAL_ROOT / "prompt-lab.yaml").read_text(encoding="utf-8")
    )
    assertions = prompt_lab["defaultTest"]["assert"]
    deterministic = [
        assertion
        for assertion in assertions
        if assertion["type"] in {"equals", "javascript"}
    ]
    rubrics = [
        assertion for assertion in assertions if assertion["type"] == "llm-rubric"
    ]

    assert deterministic
    assert len(rubrics) == 2
    declared_judges = set(_provider_ids(prompt_lab["providers"]))
    assert {rubric["provider"] for rubric in rubrics} == declared_judges
    assert all("{{text}}" in rubric["value"] for rubric in rubrics)
    assert "Do not wrap the JSON in Markdown code fences" in prompt_lab["prompts"][0]


def test_thresholds_define_independent_pass_rates() -> None:
    thresholds = json.loads((EVAL_ROOT / "thresholds.json").read_text(encoding="utf-8"))

    assert set(thresholds) == {
        "enum_match_rate",
        "topic_validity_rate",
        "hallucination_free_rate",
    }
    assert all(isinstance(value, (int, float)) for value in thresholds.values())


def test_gate_enforces_each_independent_pass_rate(tmp_path: Path) -> None:
    def result_row(*, enum_passed: bool) -> dict[str, object]:
        return {
            "gradingResult": {
                "componentResults": [
                    {
                        "pass": enum_passed,
                        "assertion": {"metric": "enum_match_rate"},
                    },
                    {
                        "pass": True,
                        "assertion": {"metric": "topic_validity_rate"},
                    },
                    {
                        "pass": True,
                        "assertion": {"metric": "hallucination_free_rate"},
                    },
                ]
            }
        }

    results_path = tmp_path / "results.json"
    results_path.write_text(
        json.dumps({"results": {"results": [result_row(enum_passed=True)]}}),
        encoding="utf-8",
    )
    passing = subprocess.run(
        [sys.executable, str(EVAL_ROOT / "gate.py"), str(results_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    results_path.write_text(
        json.dumps({"results": {"results": [result_row(enum_passed=False)]}}),
        encoding="utf-8",
    )
    failing = subprocess.run(
        [sys.executable, str(EVAL_ROOT / "gate.py"), str(results_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert passing.returncode == 0
    assert failing.returncode == 1
    assert "enum_match_rate" in failing.stdout


def test_gate_can_enforce_the_conformance_enum_metric_alone(tmp_path: Path) -> None:
    results_path = tmp_path / "results.json"
    results_path.write_text(
        json.dumps(
            {
                "results": {
                    "results": [
                        {
                            "gradingResult": {
                                "componentResults": [
                                    {
                                        "pass": True,
                                        "assertion": {
                                            "metric": "enum_match_rate"
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(EVAL_ROOT / "gate.py"),
            str(results_path),
            "--metric",
            "enum_match_rate",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
