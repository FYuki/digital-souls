from dataclasses import asdict, replace
from datetime import UTC, datetime
import json
import sqlite3
from uuid import uuid4

import pytest

from app.memory.admission.contracts import ApprovedMemoryCandidate
from app.memory.admission.evaluator import RagAdmissionEvaluator
from app.memory.episode import (
    EpisodeParticipant,
    EpisodicEventType,
    EpisodicEventValue,
    ParticipantRole,
    RelatedEpisode,
    TemporalPrecision,
    parse_episode_value,
    render_episode,
)
from tests.unit.test_approved_memory_repository import NOW, _context, _repository


def episode(**kwargs):
    return EpisodicEventValue(
        **{
            "event_type": EpisodicEventType.ACTIVITY,
            "character_id": "miori",
            "character_name": "光織",
            "topic": "静岡への旅行",
            "action": "静岡へ行った",
            **kwargs,
        }
    )


def approved(value):
    return ApprovedMemoryCandidate(value, render_episode(value))


def context(key="trip", **kwargs):
    return replace(_context(), experienced_at=NOW, idempotency_key=key, **kwargs)


def test_trip_reminiscence_and_hearing_have_distinct_owners_and_participants():
    user = EpisodeParticipant("ユーザー", ParticipantRole.COMPANION)
    ao = EpisodeParticipant("蒼", ParticipantRole.COMPANION, "ao", "character")
    trip = episode(participants=(user, ao))
    topic_people = (
        EpisodeParticipant("光織", ParticipantRole.SUBJECT, "miori", "character"),
        replace(user, role=ParticipantRole.SUBJECT),
        replace(ao, role=ParticipantRole.SUBJECT),
    )
    reminiscence = episode(
        action="思い出を語った",
        participants=(user,),
        related_event=RelatedEpisode("静岡へ行った", topic_people),
    )
    hearing = episode(
        action="話を聞いた",
        participants=(replace(user, role=ParticipantRole.SPEAKER),),
        related_event=RelatedEpisode("静岡へ行った", topic_people[1:]),
    )

    assert render_episode(trip) == "光織はユーザー、蒼と、静岡へ行った。"
    assert (
        render_episode(reminiscence)
        == "光織はユーザーと、光織、ユーザー、蒼が静岡へ行ったことについて、思い出を語った。"
    )
    assert (
        render_episode(hearing)
        == "光織はユーザーから、ユーザー、蒼が静岡へ行ったことについて、話を聞いた。"
    )
    assert render_episode(
        replace(hearing, character_id="ao", character_name="蒼")
    ).startswith("蒼は")
    assert trip != reminiscence != hearing


def test_unknown_identity_is_not_inferred_from_a_name():
    unknown = EpisodeParticipant("蒼", ParticipantRole.COMPANION)
    first = replace(unknown, entity_id="1", entity_namespace="site-a")
    other = replace(unknown, entity_id="1", entity_namespace="site-b")
    assert unknown.identity is None
    assert first.identity != other.identity
    assert len(episode(participants=(first, other)).participants) == 2


@pytest.mark.parametrize(
    "participants",
    [
        (EpisodeParticipant("光織", ParticipantRole.COMPANION, "miori", "character"),),
        (EpisodeParticipant("蒼", ParticipantRole.SUBJECT),),
        (EpisodeParticipant("蒼", ParticipantRole.COMPANION, "ao", "character"),) * 2,
    ],
)
def test_episode_rejects_owner_duplicate_topic_subject_and_duplicate_identity(
    participants,
):
    with pytest.raises(ValueError):
        episode(participants=participants)


def test_related_subjects_are_separate_from_current_participants():
    with pytest.raises(ValueError):
        RelatedEpisode(
            "静岡へ行った", (EpisodeParticipant("蒼", ParticipantRole.COMPANION),)
        )


@pytest.mark.parametrize("field", ["character_id", "character_name", "topic", "action"])
def test_owner_and_action_slots_reject_overlong_text(field):
    with pytest.raises(ValueError):
        episode(**{field: "あ" * 61})


def test_new_structured_person_fields_are_all_scanned():
    value = episode(
        participants=(
            EpisodeParticipant("ユーザー", ParticipantRole.SPEAKER, "42", "site"),
        ),
        related_event=RelatedEpisode(
            "散歩をした", (EpisodeParticipant("蒼", ParticipantRole.SUBJECT),)
        ),
    )
    slots = RagAdmissionEvaluator.slot_values(value)
    assert slots == {
        "character_id": "miori",
        "character_name": "光織",
        "topic": "静岡への旅行",
        "action": "静岡へ行った",
        "participants.0.name": "ユーザー",
        "participants.0.entity_id": "42",
        "participants.0.entity_namespace": "site",
        "related_event.description": "散歩をした",
        "related_event.participants.0.name": "蒼",
    }


@pytest.mark.parametrize(
    "extra", [{"subject": "SELF"}, {"direct_experience": True}, {"unexpected": "field"}]
)
def test_common_parser_rejects_old_subject_and_undeclared_fields(extra):
    value = json.loads(json.dumps(asdict(episode())))
    with pytest.raises(ValueError):
        parse_episode_value({**value, **extra})


def test_sqlite_round_trip_preserves_participants_experience_time_and_related_date(
    tmp_path,
):
    repository, _ = _repository(tmp_path)
    past = datetime(2026, 7, 1, tzinfo=UTC)
    value = episode(
        action="旅行の話を聞いた",
        participants=(EpisodeParticipant("ユーザー", ParticipantRole.SPEAKER),),
        related_event=RelatedEpisode(
            "静岡へ行った",
            occurred_at=past,
            occurred_timezone="UTC",
            occurred_precision=TemporalPrecision.MONTH,
        ),
    )
    saved = repository.save(
        character_id="miori",
        candidate=approved(value),
        context=context(
            occurred_at=NOW,
            occurred_timezone="UTC",
            occurred_precision=TemporalPrecision.SECOND,
        ),
    )
    result = repository.get(character_id="miori", memory_id=saved.id)
    assert result.structured_value == value
    assert result.occurred_at == NOW
    assert result.experienced_at == NOW
    assert result.structured_value.related_event.occurred_at == past
    assert (
        result.structured_value.related_event.occurred_precision
        is TemporalPrecision.MONTH
    )


def test_unknown_occurrence_remains_unknown_despite_experience_and_statement_dates(
    tmp_path,
):
    repository, _ = _repository(tmp_path)
    result = repository.save(
        character_id="miori",
        candidate=approved(episode()),
        context=context(
            occurred_at=None, occurred_timezone=None, occurred_precision=None
        ),
    )
    assert result.occurred_at is None
    assert result.experienced_at == NOW
    assert result.stated_at != result.created_at


def test_linked_episode_date_is_resolved_from_same_character_sqlite_source(tmp_path):
    repository, _ = _repository(tmp_path)
    trip = repository.save(
        character_id="miori", candidate=approved(episode()), context=context()
    )
    recall = episode(
        action="思い出を語った",
        related_event=RelatedEpisode("静岡へ行った", memory_id=trip.id),
    )
    saved = repository.save(
        character_id="miori", candidate=approved(recall), context=context("recall")
    )
    assert saved.structured_value.related_event.memory_id == trip.id
    assert saved.structured_value.related_event.occurred_at == trip.occurred_at
    assert (
        saved.structured_value.related_event.occurred_precision
        == trip.occurred_precision
    )

    conflicting = replace(
        recall,
        related_event=replace(
            recall.related_event,
            occurred_at=NOW,
            occurred_timezone="UTC",
            occurred_precision=TemporalPrecision.SECOND,
        ),
    )
    with pytest.raises(ValueError, match="conflicts"):
        repository.save(
            character_id="miori",
            candidate=approved(conflicting),
            context=context("bad-date"),
        )


def test_owner_and_related_episode_cannot_cross_character_boundary(tmp_path):
    repository, _ = _repository(tmp_path)
    ao_value = episode(character_id="ao", character_name="蒼")
    with pytest.raises(ValueError, match="owner"):
        repository.save(
            character_id="miori", candidate=approved(ao_value), context=context()
        )
    trip = repository.save(
        character_id="ao", candidate=approved(ao_value), context=context()
    )
    other = episode(related_event=RelatedEpisode("静岡へ行った", memory_id=trip.id))
    with pytest.raises(ValueError, match="same character"):
        repository.save(
            character_id="miori", candidate=approved(other), context=context()
        )
    assert repository.list_active(character_id="miori") == []


def test_nonexistent_reference_and_missing_experience_time_are_rejected(tmp_path):
    repository, path = _repository(tmp_path)
    with pytest.raises(ValueError, match="experienced_at"):
        repository.save(
            character_id="miori", candidate=approved(episode()), context=_context()
        )
    with pytest.raises(ValueError, match="same character"):
        repository.save(
            character_id="miori",
            candidate=approved(
                episode(related_event=RelatedEpisode("旅行した", memory_id=uuid4()))
            ),
            context=context(),
        )
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM approved_memories").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM memory_index_outbox").fetchone()[0]
            == 0
        )


def test_episode_correction_preserves_experience_time_and_rejects_owner_change(
    tmp_path,
):
    repository, _ = _repository(tmp_path)
    original = repository.save(
        character_id="miori", candidate=approved(episode()), context=context()
    )
    correction = episode(action="静岡市へ行った")
    changed = repository.correct(
        character_id="miori",
        memory_id=original.id,
        candidate=approved(correction),
        context=context("correction"),
    )
    assert changed.experienced_at == original.experienced_at
    assert changed.normalized_text == "光織は静岡市へ行った。"
    with pytest.raises(ValueError, match="owner"):
        repository.correct(
            character_id="miori",
            memory_id=original.id,
            candidate=approved(replace(correction, character_id="ao")),
            context=context("bad-owner"),
        )


def test_related_month_is_rendered_without_inventing_day_or_overwriting_hearing_time():
    value = episode(
        action="話を聞いた",
        related_event=RelatedEpisode(
            "静岡へ行った",
            occurred_at=datetime(2026, 6, 30, 15, tzinfo=UTC),
            occurred_timezone="Asia/Tokyo",
            occurred_precision=TemporalPrecision.MONTH,
        ),
    )
    assert (
        render_episode(value)
        == "光織は2026年07月に静岡へ行ったことについて、話を聞いた。"
    )


def test_expired_episode_cannot_be_linked_even_when_status_is_active(tmp_path):
    repository, _ = _repository(tmp_path)
    target = repository.save(
        character_id="miori",
        candidate=approved(episode()),
        context=context(expires_at=NOW),
    )
    with pytest.raises(ValueError, match="related Episode must be active"):
        repository.save(
            character_id="miori",
            candidate=approved(
                episode(
                    action="思い出を語った",
                    related_event=RelatedEpisode("静岡へ行った", memory_id=target.id),
                )
            ),
            context=context("reminiscence"),
        )
