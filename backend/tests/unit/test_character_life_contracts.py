from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.character_life.contracts import (
    BigFiveAspect,
    CharacterLifeResult,
    CharacterLifeSliceInput,
    LifeStateKind,
    LifeStateRecord,
    LifeStateStatus,
    ReflectionRecord,
    ReflectionStatus,
    RelationshipAxis,
    SelfEpisodeInput,
)


EPISODE_A = UUID("11111111-1111-4111-8111-111111111111")
EPISODE_B = UUID("22222222-2222-4222-8222-222222222222")
REFLECTION_ID = UUID("33333333-3333-4333-8333-333333333333")
LIFE_STATE_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)


def test_personality_and_relationship_contracts_are_fixed() -> None:
    assert len(BigFiveAspect) == 10
    assert {axis.value for axis in RelationshipAxis} == {
        "affective_valence",
        "relational_proximity",
    }


def test_runtime_result_contract_contains_no_change_and_result_unknown() -> None:
    assert CharacterLifeResult.NO_CHANGE.value == "NO_CHANGE"
    assert CharacterLifeResult.RESULT_UNKNOWN.value == "RESULT_UNKNOWN"


def test_reflection_requires_independent_episode_sources() -> None:
    reflection = ReflectionRecord(
        id=REFLECTION_ID,
        character_id="miori",
        content="共同作業の時間を楽しいと感じる",
        status=ReflectionStatus.ACTIVE,
        source_episode_ids=(EPISODE_A, EPISODE_B),
        model_id="test-model",
        prompt_version="reflection-v1",
        policy_version="character-life-v1",
        created_at=NOW,
        updated_at=NOW,
    )

    assert reflection.source_episode_ids == (EPISODE_A, EPISODE_B)

    with pytest.raises(ValueError, match="duplicates"):
        ReflectionRecord(
            id=REFLECTION_ID,
            character_id="miori",
            content="重複した根拠",
            status=ReflectionStatus.ACTIVE,
            source_episode_ids=(EPISODE_A, EPISODE_A),
            model_id="test-model",
            prompt_version="reflection-v1",
            policy_version="character-life-v1",
            created_at=NOW,
            updated_at=NOW,
        )


def test_life_state_can_refer_to_reflection_without_being_personality() -> None:
    state = LifeStateRecord(
        id=LIFE_STATE_ID,
        character_id="miori",
        kind=LifeStateKind.GOAL_INTENTION,
        status=LifeStateStatus.ACTIVE,
        content="色彩表現についてもう少し知りたい",
        source_reflection_ids=(REFLECTION_ID,),
        created_at=NOW,
        updated_at=NOW,
    )

    assert state.kind is LifeStateKind.GOAL_INTENTION


def test_self_episode_requires_experienced_at_and_typed_activity() -> None:
    episode = SelfEpisodeInput(
        character_id="miori",
        event_type="OBSERVATION",
        topic="許可済み資料を読んだ",
        experienced_at=NOW,
        source_provider_id="mcp:docs",
        source_ref="doc-123",
    )

    assert episode.experienced_at is NOW

    with pytest.raises(ValueError, match="SELF episode"):
        SelfEpisodeInput(
            character_id="miori",
            event_type="DECISION",
            topic="対象外",
            experienced_at=NOW,
            source_provider_id="core",
            source_ref="x",
        )


def test_vertical_slice_requires_multiple_episode_ids() -> None:
    with pytest.raises(ValueError, match="at least two"):
        CharacterLifeSliceInput(
            character_id="miori",
            episode_ids=(EPISODE_A,),
            requested_at=NOW,
        )
