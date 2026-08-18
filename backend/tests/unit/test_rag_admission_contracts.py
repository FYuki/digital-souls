from dataclasses import fields
import importlib

import pytest



def _contracts():
    return importlib.import_module("app.memory.admission.contracts")


def test_admission_enums_are_closed_to_the_allowlist_contract() -> None:
    contracts = _contracts()

    assert {item.value for item in contracts.MemoryType} == {
        "EPISODIC_EVENT",
        "USER_PREFERENCE",
        "INTERACTION_PREFERENCE",
    }
    assert {item.value for item in contracts.EpisodicEventType} == {
        "SHARED_MILESTONE",
        "ACHIEVEMENT",
        "DECISION",
        "OUTCOME",
        "CHANGE",
    }
    assert {item.value for item in contracts.EpisodicSubject} == {"USER", "SHARED"}
    assert {item.value for item in contracts.PreferencePolarity} == {
        "LIKE",
        "DISLIKE",
        "PREFER_OVER",
    }
    assert {item.value for item in contracts.InteractionAspect} == {
        "ADDRESSING",
        "TONE",
        "RESPONSE_FORMAT",
        "RESPONSE_LENGTH",
        "LANGUAGE",
    }
    assert {item.value for item in contracts.RagAdmissionDecision} == {
        "DENY_SENSITIVE",
        "DENY_USER_REQUEST",
        "ABSTAIN_UNKNOWN",
        "NOT_MEMORY_WORTHY",
        "ALLOW_STRUCTURED",
    }


def test_candidate_and_source_expose_only_the_required_fields() -> None:
    contracts = _contracts()

    assert {field.name for field in fields(contracts.ConversationSource)} == {
        "turn_status",
        "history_content_stored",
    }
    assert {field.name for field in fields(contracts.MemoryCandidate)} == {
        "memory_type",
        "structured_value",
        "source",
    }
    assert {field.name for field in fields(contracts.ApprovedMemoryCandidate)} == {
        "structured_value",
        "normalized_text",
    }


@pytest.mark.parametrize(
    "value_kind",
    ["episodic", "preference", "preference_alternative", "interaction"],
)
def test_free_text_slots_accept_one_to_sixty_characters(value_kind: str) -> None:
    contracts = _contracts()
    values = {
        "episodic": contracts.EpisodicEventValue(
            contracts.EpisodicEventType.ACHIEVEMENT,
            contracts.EpisodicSubject.USER,
            "資格取得",
        ),
        "preference": contracts.UserPreferenceValue(
            contracts.PreferencePolarity.LIKE,
            "コーヒー",
        ),
        "preference_alternative": contracts.UserPreferenceValue(
            contracts.PreferencePolarity.PREFER_OVER,
            "コーヒー",
            "紅茶",
        ),
        "interaction": contracts.InteractionPreferenceValue(
            contracts.InteractionAspect.LANGUAGE,
            "日本語",
        ),
    }
    value = values[value_kind]
    slot_name = {
        "episodic": "topic",
        "preference": "object",
        "preference_alternative": "alternative",
        "interaction": "value",
    }[value_kind]
    constructor_values = {
        field.name: getattr(value, field.name) for field in fields(value)
    }

    constructor_values[slot_name] = "あ" * 60
    accepted = type(value)(**constructor_values)

    assert getattr(accepted, slot_name) == "あ" * 60


@pytest.mark.parametrize("slot_name", ["topic", "object", "alternative", "value"])
@pytest.mark.parametrize("invalid_text", ["", "   ", "あ" * 61])
def test_free_text_slots_reject_blank_or_overlong_values(
    slot_name: str,
    invalid_text: str,
) -> None:
    contracts = _contracts()
    constructors = {
        "topic": lambda: contracts.EpisodicEventValue(
            event_type=contracts.EpisodicEventType.CHANGE,
            subject=contracts.EpisodicSubject.SHARED,
            topic=invalid_text,
        ),
        "object": lambda: contracts.UserPreferenceValue(
            polarity=contracts.PreferencePolarity.LIKE,
            object=invalid_text,
        ),
        "alternative": lambda: contracts.UserPreferenceValue(
            polarity=contracts.PreferencePolarity.PREFER_OVER,
            object="コーヒー",
            alternative=invalid_text,
        ),
        "value": lambda: contracts.InteractionPreferenceValue(
            aspect=contracts.InteractionAspect.LANGUAGE,
            value=invalid_text,
        ),
    }

    with pytest.raises(ValueError):
        constructors[slot_name]()


@pytest.mark.parametrize(
    ("polarity_name", "alternative"),
    [
        ("LIKE", "紅茶"),
        ("DISLIKE", "紅茶"),
        ("PREFER_OVER", None),
    ],
)
def test_preference_alternative_is_exclusive_to_prefer_over(
    polarity_name: str,
    alternative: str | None,
) -> None:
    contracts = _contracts()

    with pytest.raises(ValueError):
        contracts.UserPreferenceValue(
            polarity=contracts.PreferencePolarity[polarity_name],
            object="コーヒー",
            alternative=alternative,
        )


@pytest.mark.parametrize(
    ("memory_type_name", "value_kind"),
    [
        ("EPISODIC_EVENT", "preference"),
        ("USER_PREFERENCE", "interaction"),
        ("INTERACTION_PREFERENCE", "episodic"),
    ],
)
def test_candidate_rejects_memory_type_and_value_mismatch(
    memory_type_name: str,
    value_kind: str,
) -> None:
    contracts = _contracts()
    values = {
        "preference": contracts.UserPreferenceValue(
            contracts.PreferencePolarity.LIKE, "コーヒー"
        ),
        "interaction": contracts.InteractionPreferenceValue(
            contracts.InteractionAspect.TONE, "穏やか"
        ),
        "episodic": contracts.EpisodicEventValue(
            contracts.EpisodicEventType.DECISION,
            contracts.EpisodicSubject.USER,
            "転職",
        ),
    }

    with pytest.raises(ValueError):
        contracts.MemoryCandidate(
            contracts.MemoryType[memory_type_name], values[value_kind], None
        )


@pytest.mark.parametrize(
    "decision_name",
    [
        "DENY_SENSITIVE",
        "DENY_USER_REQUEST",
        "ABSTAIN_UNKNOWN",
        "NOT_MEMORY_WORTHY",
    ],
)
def test_non_allow_result_rejects_approved_candidate(
    decision_name: str,
) -> None:
    contracts = _contracts()
    approved = contracts.ApprovedMemoryCandidate(
        contracts.UserPreferenceValue(contracts.PreferencePolarity.LIKE, "コーヒー"),
        "ユーザーはコーヒーを好む。",
    )

    with pytest.raises(ValueError):
        contracts.RagAdmissionResult(
            contracts.RagAdmissionDecision[decision_name], approved
        )


def test_allow_result_requires_approved_candidate() -> None:
    contracts = _contracts()

    with pytest.raises(ValueError):
        contracts.RagAdmissionResult(
            contracts.RagAdmissionDecision.ALLOW_STRUCTURED, None
        )


def test_conversation_source_accepts_existing_turn_status_contract() -> None:
    from app.conversation_history.models import TurnStatus

    contracts = _contracts()
    source = contracts.ConversationSource(TurnStatus.COMPLETED, True)

    assert source.turn_status is TurnStatus.COMPLETED
    assert source.history_content_stored is True
