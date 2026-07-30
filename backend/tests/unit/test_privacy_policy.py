from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests.privacy_test_support import (
    POLICY_VERSION,
    config_with,
    policy_config,
    write_policy_config,
)


def _load_from(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    config: dict[str, object],
):
    from app.memory import memory_policy

    write_policy_config(path, config)
    monkeypatch.setattr(memory_policy, "MEMORY_POLICY_CONFIG_PATH", path)
    return memory_policy.resolved_memory_policy()


def test_should_load_policy_version_as_required_root_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_from(monkeypatch, tmp_path / "policy.json", policy_config())

    assert policy.policy_version == POLICY_VERSION
    assert policy.privacy.policy_version == POLICY_VERSION


@pytest.mark.parametrize("invalid_version", [None, "", "   ", 1])
def test_should_reject_missing_blank_or_non_string_policy_version(
    invalid_version: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_with(policy_config(), root={"policy_version": invalid_version})

    with pytest.raises(ValueError, match="policy_version"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


def test_should_preserve_existing_rag_policy_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_from(monkeypatch, tmp_path / "policy.json", policy_config())

    assert policy.terms.explicit_memory_terms == ("覚えて",)
    assert policy.rag_service.max_retrieved_memories == 5


def test_should_resolve_nested_privacy_policy_as_immutable_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_from(monkeypatch, tmp_path / "policy.json", policy_config())

    assert isinstance(policy.privacy.required_recognizers, tuple)
    assert isinstance(policy.privacy.absolute_deny_categories, frozenset)
    assert isinstance(policy.privacy.placeholders, tuple)
    assert isinstance(policy.privacy.storage_opt_out_rules, tuple)
    with pytest.raises(FrozenInstanceError):
        policy.privacy.policy_version = "changed"


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("required_recognizers", None),
        ("required_recognizers", {}),
        ("absolute_deny_categories", "API_KEY"),
        ("placeholders", []),
        ("storage_opt_out_rules", {}),
        ("regional_patterns", None),
        ("additional_sensitive_patterns", {}),
    ],
)
def test_should_reject_valid_json_with_wrong_privacy_field_shape(
    field_name: str,
    invalid_value: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_with(policy_config(), privacy={field_name: invalid_value})

    with pytest.raises(ValueError, match=field_name):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


def test_should_reject_missing_required_recognizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    recognizers = privacy["required_recognizers"]
    assert isinstance(recognizers, list)
    privacy["required_recognizers"] = recognizers[1:]

    with pytest.raises(ValueError, match="required_recognizers"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


def test_should_reject_duplicate_required_recognizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    recognizers = privacy["required_recognizers"]
    assert isinstance(recognizers, list)
    recognizers.append(recognizers[0])

    with pytest.raises(ValueError, match="required_recognizers"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


def test_should_reject_duplicate_storage_opt_out_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy["storage_opt_out_rules"]
    assert isinstance(rules, list)
    rules.append(dict(rules[0]))

    with pytest.raises(ValueError, match="storage_opt_out_rules"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


def test_should_reject_duplicate_absolute_deny_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    categories = privacy["absolute_deny_categories"]
    assert isinstance(categories, list)
    categories.append(categories[0])

    with pytest.raises(ValueError, match="absolute_deny_categories"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


@pytest.mark.parametrize(
    ("equivalent_phrases", "normalized_phrases"),
    [
        (("覚えないで", "覚えな\u200bいで"), ("覚えないで", "覚えないで")),
        (
            ("DO NOT REMEMBER", "ｄｏ　ｎｏｔ　ｒｅｍｅｍｂｅｒ"),
            ("do not remember", "do not remember"),
        ),
        (
            ("do  not remember", "do\tnot remember"),
            ("do not remember", "do not remember"),
        ),
    ],
)
def test_should_accept_normalization_equivalent_opt_out_phrases(
    equivalent_phrases: tuple[str, str],
    normalized_phrases: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy["storage_opt_out_rules"]
    assert isinstance(rules, list)
    rag_rule = rules[0]
    assert isinstance(rag_rule, dict)
    rag_rule["phrases"] = list(equivalent_phrases)

    policy = _load_from(monkeypatch, tmp_path / "policy.json", config)

    assert (
        policy.privacy.storage_opt_out_rules[0].normalized_phrases
        == normalized_phrases
    )


def test_should_reject_exact_duplicate_opt_out_phrases_within_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy["storage_opt_out_rules"]
    assert isinstance(rules, list)
    rag_rule = rules[0]
    assert isinstance(rag_rule, dict)
    rag_rule["phrases"] = ["DO NOT REMEMBER", "DO NOT REMEMBER"]

    with pytest.raises(ValueError, match="storage_opt_out_rules phrases"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


def test_should_reject_opt_out_phrase_empty_after_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy["storage_opt_out_rules"]
    assert isinstance(rules, list)
    rag_rule = rules[0]
    assert isinstance(rag_rule, dict)
    phrases = rag_rule["phrases"]
    assert isinstance(phrases, list)
    phrases.append("\u200b")

    with pytest.raises(ValueError, match="empty normalized phrase"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


def test_should_reject_duplicate_opt_out_patterns_within_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy["storage_opt_out_rules"]
    assert isinstance(rules, list)
    rag_rule = rules[0]
    assert isinstance(rag_rule, dict)
    rag_rule["patterns"] = ["(?:do not|don't) remember"] * 2

    with pytest.raises(ValueError, match="storage_opt_out_rules patterns"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


@pytest.mark.parametrize(
    ("first_scope_index", "second_scope_index"),
    [(0, 1), (0, 2), (1, 2)],
)
def test_should_accept_normalization_equivalent_opt_out_phrases_across_scopes(
    first_scope_index: int,
    second_scope_index: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy["storage_opt_out_rules"]
    assert isinstance(rules, list)
    first_rule = rules[first_scope_index]
    second_rule = rules[second_scope_index]
    assert isinstance(first_rule, dict)
    assert isinstance(second_rule, dict)
    first_rule["phrases"] = ["DO NOT REMEMBER"]
    second_rule["phrases"] = ["ｄｏ　ｎｏｔ　ｒｅｍｅｍｂｅｒ"]

    policy = _load_from(monkeypatch, tmp_path / "policy.json", config)

    assert policy.privacy.storage_opt_out_rules[
        first_scope_index
    ].normalized_phrases == (
        "do not remember",
    )
    assert policy.privacy.storage_opt_out_rules[
        second_scope_index
    ].normalized_phrases == (
        "do not remember",
    )


def test_should_accept_exact_duplicate_opt_out_phrases_across_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy["storage_opt_out_rules"]
    assert isinstance(rules, list)
    rag_rule = rules[0]
    history_rule = rules[1]
    assert isinstance(rag_rule, dict)
    assert isinstance(history_rule, dict)
    rag_rule["phrases"] = ["DO NOT REMEMBER"]
    history_rule["phrases"] = ["DO NOT REMEMBER"]

    policy = _load_from(monkeypatch, tmp_path / "policy.json", config)

    assert policy.privacy.storage_opt_out_rules[0].normalized_phrases == (
        "do not remember",
    )
    assert policy.privacy.storage_opt_out_rules[1].normalized_phrases == (
        "do not remember",
    )


def test_should_accept_exact_duplicate_opt_out_patterns_across_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy["storage_opt_out_rules"]
    assert isinstance(rules, list)
    rag_rule = rules[0]
    history_rule = rules[1]
    assert isinstance(rag_rule, dict)
    assert isinstance(history_rule, dict)
    rag_rule["patterns"] = ["DO NOT REMEMBER"]
    history_rule["patterns"] = ["DO NOT REMEMBER"]

    policy = _load_from(monkeypatch, tmp_path / "policy.json", config)

    assert policy.privacy.storage_opt_out_rules[0].patterns[0].pattern == (
        "DO NOT REMEMBER"
    )
    assert policy.privacy.storage_opt_out_rules[1].patterns[0].pattern == (
        "DO NOT REMEMBER"
    )


def test_should_accept_patterns_that_differ_only_by_case_across_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy["storage_opt_out_rules"]
    assert isinstance(rules, list)
    rag_rule = rules[0]
    history_rule = rules[1]
    assert isinstance(rag_rule, dict)
    assert isinstance(history_rule, dict)
    rag_rule["patterns"] = ["DO NOT REMEMBER"]
    history_rule["patterns"] = ["do not remember"]

    policy = _load_from(monkeypatch, tmp_path / "policy.json", config)

    assert policy.privacy.storage_opt_out_rules[0].patterns[0].pattern == (
        "DO NOT REMEMBER"
    )
    assert policy.privacy.storage_opt_out_rules[1].patterns[0].pattern == (
        "do not remember"
    )


@pytest.mark.parametrize(
    "pattern",
    [
        "ALPHA",
        "ALPHA BETA",
        r"(?!Z)ALPHA BETA",
        r"[A]LPHA BETA",
        r"(?:ALPHA) BETA",
        r"ALPHA{1} BETA",
        "alpha beta",
    ],
)
def test_should_accept_valid_opt_out_pattern_regardless_of_phrase_semantics(
    pattern: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy["storage_opt_out_rules"]
    assert isinstance(rules, list)
    rag_rule = rules[0]
    history_rule = rules[1]
    assert isinstance(rag_rule, dict)
    assert isinstance(history_rule, dict)
    rag_rule["phrases"] = ["ALPHA"]
    history_rule["patterns"] = [pattern]

    policy = _load_from(monkeypatch, tmp_path / "policy.json", config)

    assert policy.privacy.storage_opt_out_rules[1].patterns[0].pattern == pattern


@pytest.mark.parametrize(
    "field_name",
    ["regional_patterns", "additional_sensitive_patterns"],
)
@pytest.mark.parametrize(
    "pattern",
    ["ALPHA", "ALPHA BETA", r"(?!Z)ALPHA BETA", r"[A]LPHA BETA", "alpha beta"],
)
def test_should_accept_valid_privacy_pattern_regardless_of_phrase_semantics(
    field_name: str,
    pattern: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    storage_rules = privacy["storage_opt_out_rules"]
    pattern_rules = privacy[field_name]
    assert isinstance(storage_rules, list)
    assert isinstance(pattern_rules, list)
    storage_rule = storage_rules[0]
    pattern_rule = pattern_rules[0]
    assert isinstance(storage_rule, dict)
    assert isinstance(pattern_rule, dict)
    storage_rule["phrases"] = ["ALPHA"]
    pattern_rule["pattern"] = pattern
    pattern_rule["view"] = "casefold"

    policy = _load_from(monkeypatch, tmp_path / "policy.json", config)
    loaded_rules = (
        policy.privacy.regional_patterns
        if field_name == "regional_patterns"
        else policy.privacy.additional_sensitive_patterns
    )

    assert loaded_rules[0].pattern.pattern == pattern


@pytest.mark.parametrize(
    "field_name",
    ["regional_patterns", "additional_sensitive_patterns"],
)
def test_should_reject_exact_duplicate_pattern_rules(
    field_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy[field_name]
    assert isinstance(rules, list)
    duplicate = dict(rules[0])
    rules.append(duplicate)

    with pytest.raises(ValueError, match=field_name):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


@pytest.mark.parametrize(
    "field_name",
    ["regional_patterns", "additional_sensitive_patterns"],
)
def test_should_accept_differently_named_pattern_rules_with_same_detection_inputs(
    field_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy[field_name]
    assert isinstance(rules, list)
    original_count = len(rules)
    duplicate = dict(rules[0])
    duplicate["name"] = "different_name_with_same_detection_inputs"
    rules.append(duplicate)

    policy = _load_from(monkeypatch, tmp_path / "policy.json", config)

    loaded_rules = (
        policy.privacy.regional_patterns
        if field_name == "regional_patterns"
        else policy.privacy.additional_sensitive_patterns
    )
    assert len(loaded_rules) == original_count + 1


@pytest.mark.parametrize(
    "field_name",
    ["regional_patterns", "additional_sensitive_patterns"],
)
def test_should_accept_pattern_rules_that_differ_only_by_case(
    field_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy[field_name]
    assert isinstance(rules, list)
    original_count = len(rules)
    duplicate = dict(rules[0])
    duplicate["name"] = "case_insensitive_duplicate"
    pattern = duplicate["pattern"]
    assert isinstance(pattern, str)
    duplicate["pattern"] = (
        pattern.replace("CA DL", "ca dl").replace("Z0000000", "z0000000")
        if field_name == "regional_patterns"
        else pattern.swapcase()
    )
    rules.append(duplicate)

    policy = _load_from(monkeypatch, tmp_path / "policy.json", config)

    loaded_rules = (
        policy.privacy.regional_patterns
        if field_name == "regional_patterns"
        else policy.privacy.additional_sensitive_patterns
    )
    assert len(loaded_rules) == original_count + 1


@pytest.mark.parametrize(
    "field_name",
    ["regional_patterns", "additional_sensitive_patterns"],
)
def test_should_accept_same_pattern_rule_across_normalization_views(
    field_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy[field_name]
    assert isinstance(rules, list)
    original_count = len(rules)
    duplicate = dict(rules[0])
    duplicate["name"] = "different_view_duplicate"
    duplicate["view"] = (
        "normalized" if duplicate["view"] == "casefold" else "casefold"
    )
    rules.append(duplicate)

    policy = _load_from(monkeypatch, tmp_path / "policy.json", config)

    loaded_rules = (
        policy.privacy.regional_patterns
        if field_name == "regional_patterns"
        else policy.privacy.additional_sensitive_patterns
    )
    assert len(loaded_rules) == original_count + 1


def test_should_accept_same_pattern_across_regional_and_additional_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    regional_rules = privacy["regional_patterns"]
    additional_rules = privacy["additional_sensitive_patterns"]
    assert isinstance(regional_rules, list)
    assert isinstance(additional_rules, list)
    regional_rule = regional_rules[0]
    assert isinstance(regional_rule, dict)
    additional_rules[0] = {
        "name": "cross_input_duplicate",
        "pattern": r"CA DL: (?P<value>Z0000000)",
        "view": "normalized",
    }

    policy = _load_from(monkeypatch, tmp_path / "policy.json", config)

    assert policy.privacy.regional_patterns[0].pattern.pattern == (
        r"CA DL: (?P<value>Z0000000)"
    )
    assert policy.privacy.additional_sensitive_patterns[0].pattern.pattern == (
        r"CA DL: (?P<value>Z0000000)"
    )


def test_should_accept_case_variant_pattern_across_policy_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    opt_out_rules = privacy["storage_opt_out_rules"]
    additional_rules = privacy["additional_sensitive_patterns"]
    assert isinstance(opt_out_rules, list)
    assert isinstance(additional_rules, list)
    rag_rule = opt_out_rules[0]
    assert isinstance(rag_rule, dict)
    rag_rule["patterns"] = ["PROJECT-SECRET-[0-9]{4}"]
    additional_rule = additional_rules[0]
    assert isinstance(additional_rule, dict)
    additional_rule["pattern"] = "project-secret-[0-9]{4}"

    policy = _load_from(monkeypatch, tmp_path / "policy.json", config)

    assert policy.privacy.storage_opt_out_rules[0].patterns[0].pattern == (
        "PROJECT-SECRET-[0-9]{4}"
    )
    assert policy.privacy.additional_sensitive_patterns[0].pattern.pattern == (
        "project-secret-[0-9]{4}"
    )


@pytest.mark.parametrize(
    "field_name",
    ["storage_opt_out_rules", "regional_patterns", "additional_sensitive_patterns"],
)
def test_should_reject_invalid_regex_syntax(
    field_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    rules = privacy[field_name]
    assert isinstance(rules, list)
    rule = rules[0]
    assert isinstance(rule, dict)
    if field_name == "storage_opt_out_rules":
        rule["patterns"] = ["("]
    else:
        rule["pattern"] = "("

    with pytest.raises(ValueError, match="invalid"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("recognizer", "configured"),
        ("category", "STORAGE_OPT_OUT"),
    ],
)
def test_should_reject_unsupported_regional_pattern_target(
    field_name: str,
    invalid_value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    patterns = privacy["regional_patterns"]
    assert isinstance(patterns, list)
    pattern = patterns[0]
    assert isinstance(pattern, dict)
    pattern[field_name] = invalid_value

    with pytest.raises(ValueError, match=field_name):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


def test_should_reject_compact_phone_view_for_additional_pattern(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    patterns = privacy["additional_sensitive_patterns"]
    assert isinstance(patterns, list)
    pattern = patterns[0]
    assert isinstance(pattern, dict)
    pattern["view"] = "compact_phone"

    with pytest.raises(ValueError, match="additional_sensitive_patterns"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


def test_should_reject_missing_required_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    placeholders = privacy["placeholders"]
    assert isinstance(placeholders, dict)
    del placeholders["PRIVATE_KEY"]

    with pytest.raises(ValueError, match="placeholder"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


def test_should_reject_removing_absolute_deny_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    categories = privacy["absolute_deny_categories"]
    assert isinstance(categories, list)
    privacy["absolute_deny_categories"] = [
        category for category in categories if category != "API_KEY"
    ]

    with pytest.raises(ValueError, match="absolute_deny_categories"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


@pytest.mark.parametrize("relaxation", ["disabled_categories", "allowed_categories"])
def test_should_reject_policy_keys_that_relax_absolute_denies(
    relaxation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_with(policy_config(), privacy={relaxation: ["API_KEY"]})

    with pytest.raises(ValueError, match=relaxation):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


def test_should_reject_unknown_category_in_placeholder_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    placeholders = privacy["placeholders"]
    assert isinstance(placeholders, dict)
    placeholders["DYNAMIC_SECRET"] = "[DYNAMIC]"

    with pytest.raises(ValueError, match="DYNAMIC_SECRET"):
        _load_from(monkeypatch, tmp_path / "policy.json", config)


def test_should_normalize_additional_patterns_to_fixed_sensitive_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_from(monkeypatch, tmp_path / "policy.json", policy_config())

    rule = policy.privacy.additional_sensitive_patterns[0]
    assert rule.category.value == "POLICY_ADDED_SENSITIVE"
    assert policy.privacy.placeholder_for(rule.category) == "[SENSITIVE]"


def test_should_supply_scoped_opt_out_and_regional_patterns_from_typed_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_from(monkeypatch, tmp_path / "policy.json", policy_config())

    assert {
        rule.scope.value for rule in policy.privacy.storage_opt_out_rules
    } == {"RAG", "HISTORY", "BOTH"}
    assert policy.privacy.regional_patterns[0].name == (
        "synthetic_us_driver_license"
    )
    assert not isinstance(policy.privacy.regional_patterns[0].pattern, dict)


def test_should_load_shipped_policy_with_privacy_contract() -> None:
    from app.memory.memory_policy import resolved_memory_policy

    policy = resolved_memory_policy()

    assert policy.policy_version
    assert policy.privacy.required_recognizers
    assert policy.privacy.storage_opt_out_rules


def test_should_not_expose_mutable_raw_privacy_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_from(monkeypatch, tmp_path / "policy.json", policy_config())

    serialized = json.dumps(policy_config(), ensure_ascii=False)
    assert not hasattr(policy.privacy, "raw_config")
    assert serialized not in repr(policy.privacy)
