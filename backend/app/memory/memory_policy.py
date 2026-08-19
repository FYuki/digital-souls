import json
import re
from dataclasses import dataclass
from pathlib import Path
from re import Pattern

from app.privacy.contracts import PrivacyCategory, StorageScope
from app.privacy.normalization import build_normalized_view

MEMORY_POLICY_CONFIG_PATH = Path(__file__).with_name("memory_policy.json")
POLICY_VERSION_KEY = "policy_version"
COMMON_SECTION_KEY = "common"
SERVICES_SECTION_KEY = "services"
PRIVACY_SECTION_KEY = "privacy"
RAG_SERVICE_KEY = "rag_service"
SENSITIVE_TERMS_KEY = "sensitive_terms"
DO_NOT_STORE_TERMS_KEY = "do_not_store_terms"
EXPLICIT_MEMORY_TERMS_KEY = "explicit_memory_terms"
LONG_TERM_MEMORY_MARKERS_KEY = "long_term_memory_markers"
MAX_RETRIEVED_MEMORIES_KEY = "max_retrieved_memories"
POLICY_TERM_KEYS = (
    SENSITIVE_TERMS_KEY,
    DO_NOT_STORE_TERMS_KEY,
    EXPLICIT_MEMORY_TERMS_KEY,
    LONG_TERM_MEMORY_MARKERS_KEY,
)
REQUIRED_RECOGNIZERS = frozenset(
    {
        "credentials",
        "keys",
        "financial",
        "contact",
        "government",
        "location",
        "configured",
    }
)
REGIONAL_PATTERN_CATEGORIES_BY_RECOGNIZER = {
    "contact": frozenset({PrivacyCategory.PHONE}),
    "financial": frozenset({PrivacyCategory.BANK_ACCOUNT}),
    "government": frozenset({PrivacyCategory.GOVERNMENT_ID}),
    "location": frozenset({PrivacyCategory.PRECISE_ADDRESS}),
}
REGIONAL_PATTERN_VIEWS = frozenset({"normalized", "casefold", "compact_phone"})
ADDITIONAL_PATTERN_VIEWS = frozenset({"normalized", "casefold"})
ABSOLUTE_DENY_CATEGORIES = frozenset(
    category
    for category in PrivacyCategory
    if category is not PrivacyCategory.STORAGE_OPT_OUT
)


@dataclass(frozen=True)
class MemoryPolicyTerms:
    sensitive_terms: tuple[str, ...]
    do_not_store_terms: tuple[str, ...]
    explicit_memory_terms: tuple[str, ...]
    long_term_memory_markers: tuple[str, ...]


@dataclass(frozen=True)
class RagServicePolicy:
    max_retrieved_memories: int


@dataclass(frozen=True)
class PatternRule:
    name: str
    category: PrivacyCategory
    pattern: Pattern[str]
    view: str
    recognizer: str | None


@dataclass(frozen=True)
class StorageOptOutRule:
    scope: StorageScope
    normalized_phrases: tuple[str, ...]
    patterns: tuple[Pattern[str], ...]


@dataclass(frozen=True)
class PrivacyPolicy:
    policy_version: str
    required_recognizers: tuple[str, ...]
    absolute_deny_categories: frozenset[PrivacyCategory]
    placeholders: tuple[tuple[PrivacyCategory, str], ...]
    storage_opt_out_rules: tuple[StorageOptOutRule, ...]
    regional_patterns: tuple[PatternRule, ...]
    additional_sensitive_patterns: tuple[PatternRule, ...]

    def placeholder_for(self, category: PrivacyCategory) -> str | None:
        return dict(self.placeholders).get(category)


@dataclass(frozen=True)
class MemoryPolicy:
    policy_version: str
    terms: MemoryPolicyTerms
    rag_service: RagServicePolicy
    privacy: PrivacyPolicy


def _object_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"memory policy config '{label}' must be an object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"memory policy config '{label}' keys must be strings")
        result[key] = item
    return result


def _load_config(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as file:
        loaded: object = json.load(file)
    return _object_mapping(loaded, "root")


def _section(config: dict[str, object], key: str) -> dict[str, object]:
    return _object_mapping(config.get(key), key)


def _string_terms(section: dict[str, object], key: str) -> tuple[str, ...]:
    value = section.get(key)
    if not isinstance(value, list):
        raise ValueError(f"memory policy config '{key}' must be a string array")
    if not all(isinstance(term, str) for term in value):
        raise ValueError(f"memory policy config '{key}' must contain only strings")
    return tuple(value)


def _required_non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"memory policy config '{label}' must be a non-empty string")
    return value


def _string_array(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"memory policy config '{label}' must be a string array")
    return tuple(value)


def _compile_pattern(value: object, label: str) -> Pattern[str]:
    pattern = _required_non_empty_string(value, label)
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"memory policy config '{label}' is invalid") from exc


def _require_unique(values: tuple[object, ...], label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"memory policy config '{label}' contains duplicates")


def _pattern_key(pattern: Pattern[str]) -> tuple[str, int]:
    return pattern.pattern, pattern.flags


def _service_sections(services: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        service_name: _object_mapping(service_config, service_name)
        for service_name, service_config in services.items()
    }


def _terms_with_service_override(
    common: dict[str, object],
    services: dict[str, dict[str, object]],
    service_name: str,
) -> MemoryPolicyTerms:
    service_overrides = services.get(service_name, {})

    resolved: dict[str, tuple[str, ...]] = {}
    for key in POLICY_TERM_KEYS:
        source = service_overrides if key in service_overrides else common
        resolved[key] = _string_terms(source, key)

    return MemoryPolicyTerms(
        sensitive_terms=resolved[SENSITIVE_TERMS_KEY],
        do_not_store_terms=resolved[DO_NOT_STORE_TERMS_KEY],
        explicit_memory_terms=resolved[EXPLICIT_MEMORY_TERMS_KEY],
        long_term_memory_markers=resolved[LONG_TERM_MEMORY_MARKERS_KEY],
    )


def _rag_service_policy_from_section(
    rag_service: dict[str, object],
) -> RagServicePolicy:
    value = rag_service.get(MAX_RETRIEVED_MEMORIES_KEY)
    if not isinstance(value, int) or value < 1:
        raise ValueError(
            "memory policy config 'max_retrieved_memories' must be a positive integer"
        )
    return RagServicePolicy(max_retrieved_memories=value)


def _required_rag_service_policy(
    services: dict[str, dict[str, object]],
) -> RagServicePolicy:
    rag_service = services.get(RAG_SERVICE_KEY)
    if rag_service is None:
        raise ValueError("memory policy config 'rag_service' must be an object")
    return _rag_service_policy_from_section(rag_service)


def _placeholder_mapping(value: object) -> tuple[tuple[PrivacyCategory, str], ...]:
    mapping = _object_mapping(value, "placeholders")
    parsed: dict[PrivacyCategory, str] = {}
    for category_name, placeholder in mapping.items():
        try:
            category = PrivacyCategory(category_name)
        except ValueError as exc:
            raise ValueError(
                f"memory policy config contains unknown category '{category_name}'"
            ) from exc
        if category is PrivacyCategory.STORAGE_OPT_OUT:
            raise ValueError("storage opt-out must not have a placeholder")
        parsed[category] = _required_non_empty_string(placeholder, "placeholder")
    missing = ABSOLUTE_DENY_CATEGORIES.difference(parsed)
    if missing:
        raise ValueError("memory policy config is missing a required placeholder")
    return tuple(sorted(parsed.items(), key=lambda item: item[0].value))


def _storage_opt_out_rules(value: object) -> tuple[StorageOptOutRule, ...]:
    if not isinstance(value, list):
        raise ValueError(
            "memory policy config 'storage_opt_out_rules' must be an array"
        )
    rules: list[StorageOptOutRule] = []
    for item in value:
        rule = _object_mapping(item, "storage_opt_out_rules")
        try:
            scope = StorageScope(
                _required_non_empty_string(rule.get("scope"), "storage_opt_out_rules")
            )
        except ValueError as exc:
            raise ValueError(
                "memory policy config 'storage_opt_out_rules' has an invalid scope"
            ) from exc
        phrases = _string_array(rule.get("phrases"), "storage_opt_out_rules")
        normalized_phrases = tuple(
            build_normalized_view(phrase, casefold=True).text for phrase in phrases
        )
        if not all(normalized_phrases):
            raise ValueError(
                "memory policy config 'storage_opt_out_rules' contains an empty "
                "normalized phrase"
            )
        _require_unique(phrases, "storage_opt_out_rules phrases")
        pattern_values = rule.get("patterns")
        if not isinstance(pattern_values, list):
            raise ValueError(
                "memory policy config 'storage_opt_out_rules' patterns must be an array"
            )
        patterns = tuple(
            _compile_pattern(pattern, "storage_opt_out_rules")
            for pattern in pattern_values
        )
        _require_unique(
            tuple(_pattern_key(pattern) for pattern in patterns),
            "storage_opt_out_rules patterns",
        )
        rules.append(StorageOptOutRule(scope, normalized_phrases, patterns))
    configured_scopes = tuple(rule.scope for rule in rules)
    if (
        set(configured_scopes) != set(StorageScope)
        or len(configured_scopes) != len(StorageScope)
    ):
        raise ValueError(
            "storage_opt_out_rules must define every storage scope exactly once"
        )
    return tuple(rules)


def _validate_regional_pattern_target(
    recognizer: str,
    category: PrivacyCategory,
    label: str,
) -> None:
    allowed_categories = REGIONAL_PATTERN_CATEGORIES_BY_RECOGNIZER.get(recognizer)
    if allowed_categories is None:
        raise ValueError(
            f"memory policy config '{label}' has an unsupported recognizer"
        )
    if category not in allowed_categories:
        raise ValueError(
            f"memory policy config '{label}' has an unsupported category "
            "for its recognizer"
        )


def _pattern_rules(
    value: object,
    label: str,
    *,
    additional: bool,
) -> tuple[PatternRule, ...]:
    if not isinstance(value, list):
        raise ValueError(f"memory policy config '{label}' must be an array")
    rules: list[PatternRule] = []
    for item in value:
        rule = _object_mapping(item, label)
        category = PrivacyCategory.POLICY_ADDED_SENSITIVE
        recognizer = None
        if not additional:
            category_name = _required_non_empty_string(rule.get("category"), label)
            try:
                category = PrivacyCategory(category_name)
            except ValueError as exc:
                raise ValueError(
                    f"memory policy config '{label}' has unknown category"
                ) from exc
            recognizer = _required_non_empty_string(rule.get("recognizer"), label)
            _validate_regional_pattern_target(recognizer, category, label)
        view = _required_non_empty_string(rule.get("view"), label)
        supported_views = (
            ADDITIONAL_PATTERN_VIEWS if additional else REGIONAL_PATTERN_VIEWS
        )
        if view not in supported_views:
            raise ValueError(f"memory policy config '{label}' has an invalid view")
        rules.append(
            PatternRule(
                name=_required_non_empty_string(rule.get("name"), label),
                category=category,
                pattern=_compile_pattern(rule.get("pattern"), label),
                view=view,
                recognizer=recognizer,
            )
        )
    _require_unique(
        tuple(
            (
                rule.name,
                rule.category,
                _pattern_key(rule.pattern),
                rule.view,
                rule.recognizer,
            )
            for rule in rules
        ),
        label,
    )
    return tuple(rules)


def _privacy_policy(
    section: dict[str, object],
    policy_version: str,
) -> PrivacyPolicy:
    for relaxation_key in ("disabled_categories", "allowed_categories"):
        if relaxation_key in section:
            raise ValueError(
                f"memory policy config '{relaxation_key}' cannot relax absolute denies"
            )
    required = _string_array(
        section.get("required_recognizers"),
        "required_recognizers",
    )
    if (
        set(required) != REQUIRED_RECOGNIZERS
        or len(required) != len(REQUIRED_RECOGNIZERS)
    ):
        raise ValueError(
            "memory policy config 'required_recognizers' must contain every "
            "supported recognizer exactly once"
        )
    category_names = section.get("absolute_deny_categories")
    if not isinstance(category_names, list):
        raise ValueError(
            "memory policy config 'absolute_deny_categories' must be an array"
        )
    try:
        categories = frozenset(PrivacyCategory(name) for name in category_names)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "memory policy config 'absolute_deny_categories' is invalid"
        ) from exc
    if (
        categories != ABSOLUTE_DENY_CATEGORIES
        or len(category_names) != len(ABSOLUTE_DENY_CATEGORIES)
    ):
        raise ValueError(
            "memory policy config 'absolute_deny_categories' must contain every "
            "absolute deny category exactly once"
        )
    storage_rules = _storage_opt_out_rules(section.get("storage_opt_out_rules"))
    regional_rules = _pattern_rules(
        section.get("regional_patterns"),
        "regional_patterns",
        additional=False,
    )
    additional_rules = _pattern_rules(
        section.get("additional_sensitive_patterns"),
        "additional_sensitive_patterns",
        additional=True,
    )
    return PrivacyPolicy(
        policy_version=policy_version,
        required_recognizers=required,
        absolute_deny_categories=categories,
        placeholders=_placeholder_mapping(section.get("placeholders")),
        storage_opt_out_rules=storage_rules,
        regional_patterns=regional_rules,
        additional_sensitive_patterns=additional_rules,
    )


def _load_policy(path: Path) -> MemoryPolicy:
    config = _load_config(path)
    policy_version = _required_non_empty_string(
        config.get(POLICY_VERSION_KEY),
        POLICY_VERSION_KEY,
    )
    common = _section(config, COMMON_SECTION_KEY)
    services = _service_sections(_section(config, SERVICES_SECTION_KEY))
    return MemoryPolicy(
        policy_version=policy_version,
        terms=_terms_with_service_override(common, services, RAG_SERVICE_KEY),
        rag_service=_required_rag_service_policy(services),
        privacy=_privacy_policy(
            _section(config, PRIVACY_SECTION_KEY),
            policy_version,
        ),
    )


def resolved_memory_policy() -> MemoryPolicy:
    return _load_policy(MEMORY_POLICY_CONFIG_PATH)


def rag_service_policy(policy: MemoryPolicy) -> RagServicePolicy:
    return policy.rag_service


def contains_sensitive_memory(content: str, policy: MemoryPolicy) -> bool:
    terms = policy.terms
    normalized = content.lower()
    return any(term.lower() in normalized for term in terms.sensitive_terms)
