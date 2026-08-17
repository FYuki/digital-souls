from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EVAL_ROOT = REPOSITORY_ROOT / "backend" / "evals" / "privacy_classifier"
SENSITIVE_CATEGORIES = {
    "HEALTH",
    "MENTAL_STATE",
    "SELF_HARM",
    "ABUSE_OR_SEXUAL_VIOLENCE",
    "FINANCIAL_SITUATION",
}


def _cases() -> list[dict[str, object]]:
    rows = []
    with (EVAL_ROOT / "cases.jsonl").open(encoding="utf-8") as file:
        for line in file:
            value = json.loads(line)
            assert isinstance(value, dict)
            rows.append(value)
    return rows


def test_corpus_is_synthetic_normal_case_data_with_japanese_english_pairs() -> None:
    cases = _cases()

    assert 50 <= len(cases) <= 70
    assert all(case["synthetic"] is True for case in cases)
    assert all(case["case_kind"] == "NORMAL" for case in cases)
    pairs: dict[str, set[str]] = {}
    for case in cases:
        pairs.setdefault(str(case["pair_id"]), set()).add(str(case["language"]))
    assert pairs
    assert all(languages == {"ja", "en"} for languages in pairs.values())


def test_case_vars_contain_only_scalar_prompt_inputs() -> None:
    cases = _cases()

    assert len(cases) == 60
    assert len({case["id"] for case in cases}) == len(cases)
    for case in _cases():
        variables = case["vars"]
        assert isinstance(variables, dict)
        assert set(variables) == {"text", "case_id"}
        assert variables["text"] == case["text"]
        assert variables["case_id"] == case["id"]
        assert all(isinstance(value, str) for value in variables.values())


def test_corpus_covers_required_sensitive_category_and_scope_product() -> None:
    observed = {
        (str(case["topic_category"]), str(case["expected_subject_scope"]))
        for case in _cases()
    }

    assert {
        (category, scope)
        for category in SENSITIVE_CATEGORIES
        for scope in {"SELF", "THIRD_PARTY", "GENERAL"}
    }.issubset(observed)


def test_sensitive_cases_never_allow_not_sensitive() -> None:
    sensitive_cases = [
        case
        for case in _cases()
        if case["sensitive"] is True
    ]

    assert sensitive_cases
    for case in sensitive_cases:
        allowed = set(case["allowed_classifications"])
        assert allowed
        assert allowed <= {"SENSITIVE", "ABSTAIN"}


def test_corpus_includes_required_semantic_case_kinds() -> None:
    tags = {str(tag) for case in _cases() for tag in case["tags"]}

    assert {
        "implicit",
        "out_of_dictionary",
        "allowlist_contamination",
        "safe_general_question",
        "safe_preference",
    }.issubset(tags)


def test_corpus_text_does_not_duplicate_classifier_few_shots() -> None:
    from app.privacy.semantic.classifier import SEMANTIC_FEW_SHOT_TEXTS

    corpus_texts = {str(case["text"]).strip() for case in _cases()}

    assert corpus_texts.isdisjoint(
        {text.strip() for text in SEMANTIC_FEW_SHOT_TEXTS}
    )


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


def test_prompt_lab_and_conformance_use_distinct_execution_boundaries() -> None:
    prompt_lab = yaml.safe_load(
        (EVAL_ROOT / "prompt-lab.yaml").read_text(encoding="utf-8")
    )
    conformance = yaml.safe_load(
        (EVAL_ROOT / "conformance.yaml").read_text(encoding="utf-8")
    )

    prompt_lab_ids = _provider_ids(prompt_lab.get("providers"))
    conformance_ids = _provider_ids(conformance.get("providers"))
    assert any(provider.startswith("ollama:") for provider in prompt_lab_ids)
    assert not any(provider.startswith("file://") for provider in prompt_lab_ids)
    assert any("provider.py" in provider for provider in conformance_ids)
    assert not any(provider.startswith("ollama:") for provider in conformance_ids)


def test_eval_configs_delegate_only_json_validation_to_promptfoo() -> None:
    for config_name in ("prompt-lab.yaml", "conformance.yaml"):
        config = yaml.safe_load(
            (EVAL_ROOT / config_name).read_text(encoding="utf-8")
        )

        assert config["defaultTest"]["assert"] == [{"type": "is-json"}]


def test_conformance_config_executes_both_privacy_profiles() -> None:
    config = yaml.safe_load(
        (EVAL_ROOT / "conformance.yaml").read_text(encoding="utf-8")
    )

    providers = config["providers"]
    assert isinstance(providers, list)
    assert {
        (provider["label"], provider["config"]["profile"])
        for provider in providers
    } == {
        ("ADMISSION", "ADMISSION"),
        ("QUERY_GATE", "QUERY_GATE"),
    }


def test_conformance_thresholds_are_profile_specific_rate_limits() -> None:
    thresholds = json.loads(
        (EVAL_ROOT / "thresholds.json").read_text(encoding="utf-8")
    )

    assert thresholds == {
        profile: {
            "abstain_rate": 0.5,
            "false_negative_rate": 0.5,
            "false_positive_rate": 0.5,
        }
        for profile in ("ADMISSION", "QUERY_GATE")
    }


def test_promptfoo_is_pinned_and_eval_commands_use_the_wrapper() -> None:
    package = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )

    promptfoo_version = package["devDependencies"]["promptfoo"]
    assert promptfoo_version == "0.117.2"
    scripts = package["scripts"]
    for mode in ("prompt-lab", "conformance"):
        arguments = shlex.split(scripts[f"eval:privacy:{mode}"])
        assert arguments[0] == "python3"
        assert arguments[-2:] == ["scripts/eval_privacy_conformance.py", mode]
        assert all("promptfoo" not in argument for argument in arguments)
        assert all(argument not in {"--share", "--no-share"} for argument in arguments)
    for standard_entrypoint in ("test:unit", "test:module"):
        assert "promptfoo" not in scripts[standard_entrypoint]
        assert "eval:privacy" not in scripts[standard_entrypoint]


def test_conformance_cli_runs_one_case_for_each_profile_without_ollama() -> None:
    promptfoo = REPOSITORY_ROOT / "node_modules" / ".bin" / "promptfoo"
    if not promptfoo.is_file():
        pytest.skip("npm ci が未実施のため Promptfoo CLI smoke を省略します")
    env = os.environ.copy()
    env["PRIVACY_EVAL_STUB"] = "1"

    result = subprocess.run(
        [
            "npm",
            "run",
            "eval:privacy:conformance",
            "--",
            "--filter-first-n",
            "1",
        ],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ADMISSION: cases=1" in result.stdout
    assert "QUERY_GATE: cases=1" in result.stdout
