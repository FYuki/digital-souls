from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.conversation_history.models import ConversationTurn, TurnStatus
from app.memory.admission.contracts import (
    EpisodicEventValue,
    InteractionPreferenceValue,
    MemoryType,
    UserPreferenceValue,
)


CONVERSATION_ID = UUID("10000000-0000-4000-8000-000000000001")
TURN_ID = UUID("20000000-0000-4000-8000-000000000001")
PREVIOUS_TURN_ID = UUID("20000000-0000-4000-8000-000000000002")
CURRENT_USER = "合成入力: 今日、資格試験に合格した"
CURRENT_ASSISTANT = "合成応答: おめでとうございます"
PREVIOUS_USER = "合成入力: 来月に資格試験を受ける"
PREVIOUS_ASSISTANT = "合成応答: 応援しています"


@dataclass
class FakeExtractorClient:
    outcomes: list[str | BaseException]
    calls: list[dict[str, object]] = field(default_factory=list)
    after_call: Callable[[int], None] | None = None

    def chat(
        self,
        messages: tuple[dict[str, str], ...],
        *,
        json_schema: dict[str, object],
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> str:
        self.calls.append(
            {
                "messages": messages,
                "json_schema": json_schema,
                "timeout_seconds": timeout_seconds,
                "max_output_tokens": max_output_tokens,
            }
        )
        if self.after_call is not None:
            self.after_call(len(self.calls))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _turn(
    turn_id: UUID,
    *,
    user_content: str,
    assistant_content: str,
) -> ConversationTurn:
    timestamp = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    return ConversationTurn(
        turn_id=turn_id,
        character_id="miori",
        conversation_id=CONVERSATION_ID,
        user_content=user_content,
        assistant_content=assistant_content,
        status=TurnStatus.COMPLETED,
        privacy_reason_code=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _settings(**overrides: str):
    from app.memory.formation.config import resolve_memory_formation_settings

    return resolve_memory_formation_settings(overrides)


def _extractor(client: FakeExtractorClient, **settings: str):
    from app.memory.formation.extractor import MemoryCandidateExtractor

    return MemoryCandidateExtractor(client=client, settings=_settings(**settings))


def _response(*candidates: dict[str, object]) -> str:
    return json.dumps(
        {
            "candidates": [
                {**candidate, "date_expressions": candidate.get("date_expressions", [])}
                for candidate in candidates
            ]
        },
        ensure_ascii=False,
    )


@pytest.mark.parametrize(
    ("candidate", "expected_type", "expected_value_type"),
    [
        (
            {
                "memory_type": "EPISODIC_EVENT",
                "structured_value": {
                    "event_type": "ACHIEVEMENT",
                    "subject": "USER",
                    "topic": "資格試験への合格",
                },
            },
            MemoryType.EPISODIC_EVENT,
            EpisodicEventValue,
        ),
        (
            {
                "memory_type": "USER_PREFERENCE",
                "structured_value": {"polarity": "LIKE", "object": "紅茶"},
            },
            MemoryType.USER_PREFERENCE,
            UserPreferenceValue,
        ),
        (
            {
                "memory_type": "INTERACTION_PREFERENCE",
                "structured_value": {"aspect": "TONE", "value": "穏やかな口調"},
            },
            MemoryType.INTERACTION_PREFERENCE,
            InteractionPreferenceValue,
        ),
    ],
)
def test_extracts_each_allowlisted_persona_memory_type(
    candidate: dict[str, object],
    expected_type: MemoryType,
    expected_value_type: type[object],
) -> None:
    client = FakeExtractorClient([_response(candidate)])

    result = _extractor(client).extract(
        current_turn=_turn(
            TURN_ID,
            user_content=CURRENT_USER,
            assistant_content=CURRENT_ASSISTANT,
        ),
        previous_turn=None,
    )

    assert len(result) == 1
    assert result[0].candidate.memory_type is expected_type
    assert isinstance(result[0].candidate.structured_value, expected_value_type)
    assert result[0].candidate.source is not None
    assert result[0].candidate.source.turn_status is TurnStatus.COMPLETED
    assert result[0].candidate.source.history_content_stored is True
    assert result[0].date_expressions == ()


def test_extractor_returns_typed_date_expressions_without_resolving_timestamps() -> None:
    from app.memory.formation.temporal_resolution import (
        DateExpressionRole,
        RelativeDateExpression,
    )

    client = FakeExtractorClient(
        [
            _response(
                {
                    "memory_type": "EPISODIC_EVENT",
                    "structured_value": {
                        "event_type": "ACHIEVEMENT",
                        "subject": "USER",
                        "topic": "資格試験への合格",
                    },
                    "date_expressions": [
                        {
                            "kind": "RELATIVE",
                            "role": "PRIMARY",
                            "year_offset": -1,
                            "month": 3,
                        }
                    ],
                }
            )
        ]
    )

    result = _extractor(client).extract(
        current_turn=_turn(
            TURN_ID,
            user_content="去年3月に資格試験へ合格した",
            assistant_content=CURRENT_ASSISTANT,
        ),
        previous_turn=None,
    )

    assert result[0].date_expressions == (
        RelativeDateExpression(
            role=DateExpressionRole.PRIMARY,
            year_offset=-1,
            month=3,
        ),
    )


def test_extractor_preserves_explicit_zero_offset_as_specified() -> None:
    from app.memory.formation.temporal_resolution import (
        DateExpressionRole,
        RelativeDateExpression,
    )

    client = FakeExtractorClient(
        [
            _response(
                {
                    "memory_type": "EPISODIC_EVENT",
                    "structured_value": {
                        "event_type": "ACHIEVEMENT",
                        "subject": "USER",
                        "topic": "資格試験への合格",
                    },
                    "date_expressions": [
                        {
                            "kind": "RELATIVE",
                            "role": "PRIMARY",
                            "day_offset": 0,
                        }
                    ],
                }
            )
        ]
    )

    result = _extractor(client).extract(
        current_turn=_turn(
            TURN_ID,
            user_content=CURRENT_USER,
            assistant_content=CURRENT_ASSISTANT,
        ),
        previous_turn=None,
    )

    assert result[0].date_expressions == (
        RelativeDateExpression(
            role=DateExpressionRole.PRIMARY,
            day_offset=0,
        ),
    )


@pytest.mark.parametrize(
    "date_expressions",
    [
        [
            {
                "kind": "ABSOLUTE",
                "role": "PRIMARY",
                "year": 2025,
                "month": 2,
                "day": 30,
            }
        ],
        [
            {
                "kind": "RELATIVE",
                "role": "PRIMARY",
                "day_offset": offset,
            }
            for offset in range(4)
        ],
    ],
    ids=["invalid-calendar-date", "too-many-date-expressions"],
)
def test_invalid_date_expression_batch_is_discarded(
    date_expressions: list[dict[str, object]],
) -> None:
    client = FakeExtractorClient(
        [
            _response(
                {
                    "memory_type": "EPISODIC_EVENT",
                    "structured_value": {
                        "event_type": "ACHIEVEMENT",
                        "subject": "USER",
                        "topic": "資格試験への合格",
                    },
                    "date_expressions": date_expressions,
                }
            )
        ]
    )

    result = _extractor(client).extract(
        current_turn=_turn(
            TURN_ID,
            user_content=CURRENT_USER,
            assistant_content=CURRENT_ASSISTANT,
        ),
        previous_turn=None,
    )

    assert result == ()


def test_extractor_transfers_only_current_user_and_one_previous_sanitized_turn() -> None:
    client = FakeExtractorClient([_response()])

    _extractor(client).extract(
        current_turn=_turn(
            TURN_ID,
            user_content=CURRENT_USER,
            assistant_content=CURRENT_ASSISTANT,
        ),
        previous_turn=_turn(
            PREVIOUS_TURN_ID,
            user_content=PREVIOUS_USER,
            assistant_content=PREVIOUS_ASSISTANT,
        ),
    )

    transferred = json.dumps(client.calls[0]["messages"], ensure_ascii=False)
    assert CURRENT_USER in transferred
    assert PREVIOUS_USER in transferred
    assert PREVIOUS_ASSISTANT in transferred
    assert CURRENT_ASSISTANT not in transferred


@pytest.mark.parametrize(
    "raw_output",
    [
        "not-json",
        json.dumps({"candidates": [{"memory_type": "UNKNOWN", "structured_value": {}}]}),
        json.dumps({"candidates": [{"memory_type": "USER_PREFERENCE"}]}),
        json.dumps(
            {
                "candidates": [
                    {
                        "memory_type": "USER_PREFERENCE",
                        "structured_value": {"polarity": "LIKE", "object": "紅茶"},
                        "unexpected": "field",
                    }
                ]
            }
        ),
        _response(
            {
                "memory_type": "USER_PREFERENCE",
                "structured_value": {
                    "polarity": "LIKE",
                    "object": "紅茶",
                    "alternative": None,
                },
            }
        ),
        _response(
            {
                "memory_type": "EPISODIC_EVENT",
                "structured_value": {
                    "event_type": "ACHIEVEMENT",
                    "subject": "USER",
                    "topic": 123,
                },
            }
        ),
        _response(
            {
                "memory_type": "USER_PREFERENCE",
                "structured_value": {"polarity": "LIKE", "object": {"tea": True}},
            }
        ),
        _response(
            {
                "memory_type": "INTERACTION_PREFERENCE",
                "structured_value": {"aspect": "TONE", "value": ["穏やか"]},
            }
        ),
        _response(
            {
                "memory_type": "USER_PREFERENCE",
                "structured_value": {
                    "polarity": "LIKE",
                    "object": "紅茶",
                    "alternative": "コーヒー",
                },
            }
        ),
        _response(
            {
                "memory_type": "USER_PREFERENCE",
                "structured_value": {
                    "polarity": "PREFER_OVER",
                    "object": "紅茶",
                },
            }
        ),
        _response(
            *(
                {
                    "memory_type": "USER_PREFERENCE",
                    "structured_value": {"polarity": "LIKE", "object": f"対象{index}"},
                }
                for index in range(4)
            )
        ),
    ],
    ids=[
        "invalid-json",
        "unknown-enum",
        "missing-field",
        "extra-field",
        "null-alternative",
        "non-string-topic",
        "non-string-object",
        "non-string-value",
        "unexpected-alternative",
        "missing-prefer-over-alternative",
        "over-limit",
    ],
)
def test_contract_external_batches_are_discarded_as_a_whole(raw_output: str) -> None:
    result = _extractor(FakeExtractorClient([raw_output])).extract(
        current_turn=_turn(
            TURN_ID,
            user_content=CURRENT_USER,
            assistant_content=CURRENT_ASSISTANT,
        ),
        previous_turn=None,
    )

    assert result == ()


def test_timeout_retries_only_to_the_configured_attempt_limit_then_discards() -> None:
    client = FakeExtractorClient([TimeoutError("first"), TimeoutError("second")])

    result = _extractor(
        client,
        MEMORY_FORMATION_MAX_ATTEMPTS="2",
        MEMORY_FORMATION_LLM_TIMEOUT_SECONDS="15",
        MEMORY_FORMATION_TOTAL_TIMEOUT_SECONDS="35",
    ).extract(
        current_turn=_turn(
            TURN_ID,
            user_content=CURRENT_USER,
            assistant_content=CURRENT_ASSISTANT,
        ),
        previous_turn=None,
    )

    assert result == ()
    assert len(client.calls) == 2
    assert [call["timeout_seconds"] for call in client.calls] == [15.0, 15.0]


def test_each_retry_is_bounded_by_the_remaining_total_timeout() -> None:
    from app.memory.formation.extractor import MemoryCandidateExtractor

    current_time = [0.0]

    def advance_after_first(call_count: int) -> None:
        if call_count == 1:
            current_time[0] = 34.0

    client = FakeExtractorClient(
        [TimeoutError("first"), TimeoutError("second")],
        after_call=advance_after_first,
    )
    extractor = MemoryCandidateExtractor(
        client=client,
        settings=_settings(
            MEMORY_FORMATION_LLM_TIMEOUT_SECONDS="30",
            MEMORY_FORMATION_MAX_ATTEMPTS="2",
            MEMORY_FORMATION_TOTAL_TIMEOUT_SECONDS="35",
        ),
        monotonic_clock=lambda: current_time[0],
    )

    result = extractor.extract(
        current_turn=_turn(
            TURN_ID,
            user_content=CURRENT_USER,
            assistant_content=CURRENT_ASSISTANT,
        ),
        previous_turn=None,
    )

    assert result == ()
    assert [call["timeout_seconds"] for call in client.calls] == [30.0, 1.0]


def test_extractor_uses_json_schema_output_limit_and_declared_version() -> None:
    from app.memory.formation.extractor import EXTRACTOR_VERSION

    client = FakeExtractorClient([_response()])

    _extractor(client, MEMORY_FORMATION_MAX_OUTPUT_TOKENS="321").extract(
        current_turn=_turn(
            TURN_ID,
            user_content=CURRENT_USER,
            assistant_content=CURRENT_ASSISTANT,
        ),
        previous_turn=None,
    )

    schema = client.calls[0]["json_schema"]
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    candidates = schema["properties"]["candidates"]  # type: ignore[index]
    variants = candidates["items"]["oneOf"]  # type: ignore[index]
    assert all(
        variant["properties"]["date_expressions"]["maxItems"] == 3
        for variant in variants
    )
    assert client.calls[0]["max_output_tokens"] == 321
    assert isinstance(EXTRACTOR_VERSION, str)
    assert EXTRACTOR_VERSION.strip()
