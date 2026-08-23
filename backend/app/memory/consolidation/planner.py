from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from app.memory.admission.contracts import (
    EpisodicEventType,
    EpisodicEventValue,
    EpisodicSubject,
    InteractionAspect,
    InteractionPreferenceValue,
    MemoryType,
    PreferencePolarity,
    StructuredValue,
    UserPreferenceValue,
)
from app.memory.persistence.contracts import ApprovedMemory

from .contracts import (
    ConsolidationPlan,
    ConsolidationPlanType,
    ConsolidationResponse,
    MemoryVersionRef,
)


logger = logging.getLogger(__name__)
_LLM_REASON_CODES = frozenset({"MODEL_SELECTED"})
_CONTENT_PLAN_TYPES = {
    ConsolidationPlanType.MERGE,
    ConsolidationPlanType.SUPERSEDE,
}


class ConsolidationPlanParseError(ValueError):
    pass


class ConsolidationClient(Protocol):
    def chat(
        self,
        messages: tuple[dict[str, str], ...],
        *,
        json_schema: dict[str, object],
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> str: ...


class ConsolidationPlanner:
    def __init__(
        self,
        *,
        client: ConsolidationClient,
        max_output_tokens: int,
        model_id: str,
        prompt_version: str,
        policy_version: str,
    ) -> None:
        self._client = client
        self._max_output_tokens = max_output_tokens
        self._model_id = model_id
        self._prompt_version = prompt_version
        self._policy_version = policy_version

    def plan(
        self,
        memories: tuple[ApprovedMemory, ...],
        *,
        timeout_seconds: float,
    ) -> ConsolidationResponse:
        refs = tuple(
            MemoryVersionRef(memory.id, memory.content_version) for memory in memories
        )
        started_at = time.monotonic()
        reason_code = "MODEL_SELECTED"
        try:
            raw = self._client.chat(
                _build_messages(memories),
                json_schema=_response_schema(),
                timeout_seconds=timeout_seconds,
                max_output_tokens=self._max_output_tokens,
            )
            response = parse_consolidation_response(raw, expected_memories=refs)
        except ConsolidationPlanParseError:
            reason_code = "AMBIGUOUS"
            response = _noop_response(refs, reason_code)
        except Exception:
            reason_code = "MODEL_FAILURE"
            response = _noop_response(refs, reason_code)
        elapsed_ms = max(0, round((time.monotonic() - started_at) * 1_000))
        logger.info(
            "Memory consolidation planning completed: reason_code=%s "
            "latency_ms=%d model_id=%s prompt_version=%s policy_version=%s",
            reason_code,
            elapsed_ms,
            self._model_id,
            self._prompt_version,
            self._policy_version,
        )
        return response


def parse_consolidation_response(
    raw_output: str,
    *,
    expected_memories: tuple[MemoryVersionRef, ...],
) -> ConsolidationResponse:
    try:
        value: object = json.loads(raw_output)
        root = _mapping(value)
        if set(root) != {"plans"} or not isinstance(root["plans"], list):
            raise ConsolidationPlanParseError("response shape is invalid")
        raw_plans = root["plans"]
        if not raw_plans:
            raise ConsolidationPlanParseError("plans must not be empty")
        plans = tuple(_parse_plan(item) for item in raw_plans)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, ConsolidationPlanParseError):
            raise
        raise ConsolidationPlanParseError("response is invalid") from None
    expected = set(expected_memories)
    observed = [ref for plan in plans for ref in plan.memories]
    if len(observed) != len(set(observed)) or set(observed) != expected:
        raise ConsolidationPlanParseError("plans must partition the expected batch")
    return ConsolidationResponse(plans)


def _parse_plan(value: object) -> ConsolidationPlan:
    item = _mapping(value)
    plan_type = ConsolidationPlanType(item.get("plan_type"))
    required = {"plan_type", "reason_code", "memories"}
    if plan_type in _CONTENT_PLAN_TYPES:
        required |= {"memory_type", "structured_value"}
    elif plan_type is ConsolidationPlanType.DELETE_EXACT_DUPLICATE:
        required.add("canonical_memory_id")
    if set(item) != required:
        raise ConsolidationPlanParseError("plan fields are invalid")
    reason_code = item["reason_code"]
    if not isinstance(reason_code, str) or reason_code not in _LLM_REASON_CODES:
        raise ConsolidationPlanParseError("reason_code is invalid")
    raw_memories = item["memories"]
    if not isinstance(raw_memories, list) or not raw_memories:
        raise ConsolidationPlanParseError("memories must not be empty")
    memories = tuple(_parse_memory_ref(raw) for raw in raw_memories)
    if len(memories) != len(set(memories)):
        raise ConsolidationPlanParseError("plan memories must be unique")
    memory_type: MemoryType | None = None
    structured_value: StructuredValue | None = None
    canonical_memory_id: UUID | None = None
    if plan_type in _CONTENT_PLAN_TYPES:
        memory_type = MemoryType(item["memory_type"])
        structured_value = _parse_structured_value(
            memory_type, _mapping(item["structured_value"])
        )
    elif plan_type is ConsolidationPlanType.DELETE_EXACT_DUPLICATE:
        canonical_memory_id = _uuid4(item["canonical_memory_id"])
        if canonical_memory_id not in {ref.memory_id for ref in memories}:
            raise ConsolidationPlanParseError("canonical memory must be in the plan")
    return ConsolidationPlan(
        plan_type=plan_type,
        reason_code=reason_code,
        memories=memories,
        memory_type=memory_type,
        structured_value=structured_value,
        canonical_memory_id=canonical_memory_id,
    )


def _parse_memory_ref(value: object) -> MemoryVersionRef:
    item = _mapping(value)
    if set(item) != {"memory_id", "content_version"}:
        raise ConsolidationPlanParseError("memory reference fields are invalid")
    return MemoryVersionRef(
        memory_id=_uuid4(item["memory_id"]),
        content_version=_positive_integer(item["content_version"]),
    )


def _parse_structured_value(
    memory_type: MemoryType, value: Mapping[str, object]
) -> StructuredValue:
    if memory_type is MemoryType.EPISODIC_EVENT:
        if set(value) != {"event_type", "subject", "topic"}:
            raise ConsolidationPlanParseError("episodic value fields are invalid")
        return EpisodicEventValue(
            event_type=EpisodicEventType(value["event_type"]),
            subject=EpisodicSubject(value["subject"]),
            topic=_string(value["topic"]),
        )
    if memory_type is MemoryType.USER_PREFERENCE:
        if set(value) not in ({"polarity", "object"}, {"polarity", "object", "alternative"}):
            raise ConsolidationPlanParseError("preference value fields are invalid")
        alternative = value.get("alternative")
        return UserPreferenceValue(
            polarity=PreferencePolarity(value["polarity"]),
            object=_string(value["object"]),
            alternative=None if alternative is None else _string(alternative),
        )
    if set(value) != {"aspect", "value"}:
        raise ConsolidationPlanParseError("interaction value fields are invalid")
    return InteractionPreferenceValue(
        aspect=InteractionAspect(value["aspect"]), value=_string(value["value"])
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ConsolidationPlanParseError("object is required")
    return value


def _uuid4(value: object) -> UUID:
    if not isinstance(value, str):
        raise ConsolidationPlanParseError("UUID string is required")
    parsed = UUID(value)
    if parsed.version != 4:
        raise ConsolidationPlanParseError("UUID4 is required")
    return parsed


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConsolidationPlanParseError("positive integer is required")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ConsolidationPlanParseError("string is required")
    return value


def _noop_response(
    memories: tuple[MemoryVersionRef, ...], reason_code: str
) -> ConsolidationResponse:
    return ConsolidationResponse(
        (
            ConsolidationPlan(
                plan_type=ConsolidationPlanType.NOOP,
                reason_code=reason_code,
                memories=memories,
            ),
        )
    )


def _build_messages(memories: tuple[ApprovedMemory, ...]) -> tuple[dict[str, str], ...]:
    payload = [
        {
            "memory_id": str(memory.id),
            "content_version": memory.content_version,
            "memory_type": memory.memory_type.value,
            "normalized_text": memory.normalized_text,
        }
        for memory in memories
    ]
    return (
        {
            "role": "system",
            "content": (
                "Return only a JSON consolidation plan. Use KEEP, MERGE, SUPERSEDE, "
                "DELETE_EXACT_DUPLICATE, CONFLICT, or NOOP. Never cross memory types."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    )


def _response_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "plans": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "plan_type": {
                            "type": "string",
                            "enum": [item.value for item in ConsolidationPlanType],
                        },
                        "reason_code": {
                            "type": "string",
                            "enum": sorted(_LLM_REASON_CODES),
                        },
                        "memories": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "memory_id": {"type": "string"},
                                    "content_version": {
                                        "type": "integer",
                                        "minimum": 1,
                                    },
                                },
                                "required": ["memory_id", "content_version"],
                                "additionalProperties": False,
                            },
                        },
                        "memory_type": {
                            "type": "string",
                            "enum": [item.value for item in MemoryType],
                        },
                        "structured_value": {"type": "object"},
                        "canonical_memory_id": {"type": "string"},
                    },
                    "required": ["plan_type", "reason_code", "memories"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["plans"],
        "additionalProperties": False,
    }
