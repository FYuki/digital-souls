from __future__ import annotations

import json
from pathlib import Path

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


def test_promptfoo_is_an_independent_pinned_manual_entrypoint() -> None:
    package = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )

    promptfoo_version = package["devDependencies"]["promptfoo"]
    assert not str(promptfoo_version).startswith(("^", "~", ">", "<"))
    scripts = package["scripts"]
    assert "prompt-lab.yaml" in scripts["eval:privacy:prompt-lab"]
    assert "conformance.yaml" in scripts["eval:privacy:conformance"]
    assert "--no-cache" in scripts["eval:privacy:prompt-lab"]
    assert "--no-share" in scripts["eval:privacy:prompt-lab"]
    assert "--no-cache" in scripts["eval:privacy:conformance"]
    assert "--no-share" in scripts["eval:privacy:conformance"]
    for standard_entrypoint in ("test:unit", "test:module"):
        assert "promptfoo" not in scripts[standard_entrypoint]
        assert "eval:privacy" not in scripts[standard_entrypoint]
