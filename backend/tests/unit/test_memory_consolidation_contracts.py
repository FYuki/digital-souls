from __future__ import annotations

import json
import re
from uuid import UUID

import pytest


MEMORY_ONE = UUID("00000000-0000-4000-8000-000000000001")
MEMORY_TWO = UUID("00000000-0000-4000-8000-000000000002")


def _version_ref(memory_id: UUID, content_version: int = 1):
    from app.memory.consolidation.contracts import MemoryVersionRef

    return MemoryVersionRef(memory_id=memory_id, content_version=content_version)


@pytest.mark.parametrize(
    ("plan_type", "extra"),
    (
        ("KEEP", {}),
        (
            "MERGE",
            {
                "memory_type": "USER_PREFERENCE",
                "structured_value": {"polarity": "LIKE", "object": "紅茶"},
            },
        ),
        (
            "SUPERSEDE",
            {
                "memory_type": "USER_PREFERENCE",
                "structured_value": {"polarity": "LIKE", "object": "緑茶"},
            },
        ),
        ("DELETE_EXACT_DUPLICATE", {"canonical_memory_id": str(MEMORY_ONE)}),
        ("CONFLICT", {}),
        ("NOOP", {}),
    ),
)
def test_parser_accepts_each_typed_plan(
    plan_type: str, extra: dict[str, object]
) -> None:
    from app.memory.consolidation.contracts import ConsolidationPlanType
    from app.memory.consolidation.planner import parse_consolidation_response

    payload = {
        "plans": [
            {
                "plan_type": plan_type,
                "reason_code": "MODEL_SELECTED",
                "memories": [
                    {"memory_id": str(MEMORY_ONE), "content_version": 1},
                    {"memory_id": str(MEMORY_TWO), "content_version": 2},
                ],
                **extra,
            }
        ]
    }

    response = parse_consolidation_response(
        json.dumps(payload),
        expected_memories=(_version_ref(MEMORY_ONE), _version_ref(MEMORY_TWO, 2)),
    )

    assert len(response.plans) == 1
    assert response.plans[0].plan_type is ConsolidationPlanType(plan_type)
    assert response.plans[0].memories == (
        _version_ref(MEMORY_ONE),
        _version_ref(MEMORY_TWO, 2),
    )


@pytest.mark.parametrize("plan_type", ("MERGE", "SUPERSEDE"))
@pytest.mark.parametrize(
    ("memory_type", "structured_value", "expected_type"),
    (
        (
            "EPISODIC_EVENT",
            {"event_type": "ACHIEVEMENT", "subject": "USER", "topic": "散歩"},
            "EpisodicEventValue",
        ),
        (
            "USER_PREFERENCE",
            {"polarity": "LIKE", "object": "紅茶"},
            "UserPreferenceValue",
        ),
        (
            "INTERACTION_PREFERENCE",
            {"aspect": "TONE", "value": "簡潔"},
            "InteractionPreferenceValue",
        ),
    ),
)
def test_parser_accepts_content_plan_schema_for_each_memory_type(
    plan_type: str,
    memory_type: str,
    structured_value: dict[str, str],
    expected_type: str,
) -> None:
    from app.memory.consolidation.planner import parse_consolidation_response

    response = parse_consolidation_response(
        json.dumps(
            {
                "plans": [
                    {
                        "plan_type": plan_type,
                        "reason_code": "MODEL_SELECTED",
                        "memories": [
                            {"memory_id": str(MEMORY_ONE), "content_version": 1}
                        ],
                        "memory_type": memory_type,
                        "structured_value": structured_value,
                    }
                ]
            }
        ),
        expected_memories=(_version_ref(MEMORY_ONE),),
    )

    assert type(response.plans[0].structured_value).__name__ == expected_type


def test_parser_rejects_untrusted_reason_code_even_when_its_shape_is_safe() -> None:
    from app.memory.consolidation.planner import ConsolidationPlanParseError
    from app.memory.consolidation.planner import parse_consolidation_response

    with pytest.raises(ConsolidationPlanParseError):
        parse_consolidation_response(
            json.dumps(
                {
                    "plans": [
                        {
                            "plan_type": "KEEP",
                            "reason_code": "SYNTHETIC_CANDIDATE_BODY",
                            "memories": [
                                {"memory_id": str(MEMORY_ONE), "content_version": 1}
                            ],
                        }
                    ]
                }
            ),
            expected_memories=(_version_ref(MEMORY_ONE),),
        )


@pytest.mark.parametrize(
    "payload",
    (
        {
            "plans": [
                {"plan_type": "ERASE", "reason_code": "MODEL_SELECTED", "memories": []}
            ]
        },
        {
            "plans": [
                {"plan_type": "KEEP", "reason_code": "FREE FORM BODY", "memories": []}
            ]
        },
        {
            "plans": [
                {
                    "plan_type": "KEEP",
                    "reason_code": "MODEL_SELECTED",
                    "memories": [],
                    "unexpected": True,
                }
            ]
        },
        {
            "plans": [
                {"plan_type": "MERGE", "reason_code": "MODEL_SELECTED", "memories": []}
            ]
        },
    ),
)
def test_parser_rejects_unknown_values_missing_result_and_extra_fields(
    payload: dict[str, object],
) -> None:
    from app.memory.consolidation.planner import ConsolidationPlanParseError
    from app.memory.consolidation.planner import parse_consolidation_response

    with pytest.raises(ConsolidationPlanParseError):
        parse_consolidation_response(
            json.dumps(payload),
            expected_memories=(_version_ref(MEMORY_ONE),),
        )


@pytest.mark.parametrize(
    "plans",
    (
        [
            {
                "plan_type": "KEEP",
                "reason_code": "MODEL_SELECTED",
                "memories": [{"memory_id": str(MEMORY_ONE), "content_version": 1}],
            },
        ],
        [
            {
                "plan_type": "KEEP",
                "reason_code": "MODEL_SELECTED",
                "memories": [{"memory_id": str(MEMORY_ONE), "content_version": 1}],
            },
            {
                "plan_type": "NOOP",
                "reason_code": "AMBIGUOUS",
                "memories": [
                    {"memory_id": str(MEMORY_ONE), "content_version": 1},
                    {"memory_id": str(MEMORY_TWO), "content_version": 1},
                ],
            },
        ],
        [
            {
                "plan_type": "KEEP",
                "reason_code": "MODEL_SELECTED",
                "memories": [
                    {"memory_id": str(MEMORY_ONE), "content_version": 1},
                    {
                        "memory_id": "00000000-0000-4000-8000-000000000099",
                        "content_version": 1,
                    },
                ],
            },
        ],
    ),
)
def test_parser_rejects_missing_duplicate_and_out_of_batch_memory_membership(
    plans: list[dict[str, object]],
) -> None:
    from app.memory.consolidation.planner import ConsolidationPlanParseError
    from app.memory.consolidation.planner import parse_consolidation_response

    with pytest.raises(ConsolidationPlanParseError):
        parse_consolidation_response(
            json.dumps({"plans": plans}),
            expected_memories=(_version_ref(MEMORY_ONE), _version_ref(MEMORY_TWO)),
        )


def test_consolidation_idempotency_key_is_order_independent_and_version_sensitive() -> (
    None
):
    from app.memory.persistence.contracts import build_consolidation_idempotency_key

    first = build_consolidation_idempotency_key(
        character_id="miori",
        plan_type="MERGE",
        memories=((MEMORY_TWO, 7), (MEMORY_ONE, 3)),
        prompt_version="consolidation-v1",
    )
    reordered = build_consolidation_idempotency_key(
        character_id="miori",
        plan_type="MERGE",
        memories=((MEMORY_ONE, 3), (MEMORY_TWO, 7)),
        prompt_version="consolidation-v1",
    )
    changed_version = build_consolidation_idempotency_key(
        character_id="miori",
        plan_type="MERGE",
        memories=((MEMORY_ONE, 4), (MEMORY_TWO, 7)),
        prompt_version="consolidation-v1",
    )
    assert first == reordered
    assert re.fullmatch(
        r"consolidation:miori:MERGE:consolidation-v1:[0-9a-f]{64}", first
    )
    assert changed_version != first
    assert str(MEMORY_ONE) not in first
    assert str(MEMORY_TWO) not in first


@pytest.mark.parametrize(
    ("plan_type", "memories"),
    (
        ("KEEP", ((MEMORY_ONE, 1),)),
        ("CONFLICT", ((MEMORY_ONE, 1),)),
        ("NOOP", ((MEMORY_ONE, 1),)),
        ("MERGE", ()),
        ("MERGE", ((MEMORY_ONE, 0),)),
        ("MERGE", ((MEMORY_ONE, 1), (MEMORY_ONE, 1))),
    ),
)
def test_consolidation_idempotency_key_rejects_non_mutating_or_ambiguous_inputs(
    plan_type: str,
    memories: tuple[tuple[UUID, int], ...],
) -> None:
    from app.memory.persistence.contracts import build_consolidation_idempotency_key

    with pytest.raises(ValueError):
        build_consolidation_idempotency_key(
            character_id="miori",
            plan_type=plan_type,
            memories=memories,
            prompt_version="consolidation-v1",
        )
