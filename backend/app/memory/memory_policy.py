import json
from dataclasses import dataclass
from pathlib import Path

from app.memory.conversation_log import ConversationRecord

MEMORY_POLICY_CONFIG_PATH = Path(__file__).with_name("memory_policy.json")
COMMON_SECTION_KEY = "common"
SERVICES_SECTION_KEY = "services"
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
class MemoryPolicy:
    terms: MemoryPolicyTerms
    rag_service: RagServicePolicy


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


def _load_policy(path: Path) -> MemoryPolicy:
    config = _load_config(path)
    common = _section(config, COMMON_SECTION_KEY)
    services = _service_sections(_section(config, SERVICES_SECTION_KEY))
    return MemoryPolicy(
        terms=_terms_with_service_override(common, services, RAG_SERVICE_KEY),
        rag_service=_required_rag_service_policy(services),
    )


def resolved_memory_policy() -> MemoryPolicy:
    return _load_policy(MEMORY_POLICY_CONFIG_PATH)


def rag_service_policy(policy: MemoryPolicy) -> RagServicePolicy:
    return policy.rag_service


def contains_sensitive_memory(content: str, policy: MemoryPolicy) -> bool:
    terms = policy.terms
    normalized = content.lower()
    return any(term.lower() in normalized for term in terms.sensitive_terms)


def contains_non_storable_memory(content: str, policy: MemoryPolicy) -> bool:
    terms = policy.terms
    normalized = content.lower()
    return contains_sensitive_memory(content, policy) or any(
        term.lower() in normalized for term in terms.do_not_store_terms
    )


def is_long_term_memory_candidate(
    record: ConversationRecord,
    policy: MemoryPolicy,
) -> bool:
    if record.role != "user":
        return False
    terms = policy.terms
    normalized = record.content.lower()
    if contains_non_storable_memory(record.content, policy):
        return False
    return any(term.lower() in normalized for term in terms.explicit_memory_terms) or any(
        marker in record.content for marker in terms.long_term_memory_markers
    )
