from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.conversation_history.models import ConversationTurn, TurnStatus
from app.memory.admission.contracts import (
    ConversationSource,
    MemoryCandidate,
    MemoryType,
    PreferencePolarity,
    UserPreferenceValue,
)


CONVERSATION_ID = UUID("10000000-0000-4000-8000-000000000001")
TURN_ID = UUID("20000000-0000-4000-8000-000000000001")
PREVIOUS_TURN_ID = UUID("20000000-0000-4000-8000-000000000002")
PRIVATE_SOURCE = "synthetic-private-source"
PRIVATE_CANDIDATE = "synthetic-private-candidate"


def _turn(
    *,
    turn_id: UUID = TURN_ID,
    status: TurnStatus = TurnStatus.COMPLETED,
    user_content: str | None = PRIVATE_SOURCE,
    assistant_content: str | None = "synthetic assistant",
) -> ConversationTurn:
    timestamp = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    return ConversationTurn(
        turn_id=turn_id,
        character_id="miori",
        conversation_id=CONVERSATION_ID,
        user_content=user_content,
        assistant_content=assistant_content,
        status=status,
        privacy_reason_code=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _candidate(value: str) -> MemoryCandidate:
    return MemoryCandidate(
        memory_type=MemoryType.USER_PREFERENCE,
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object=value,
        ),
        source=ConversationSource(TurnStatus.COMPLETED, True),
    )


@dataclass
class FakeRepository:
    current: ConversationTurn | None
    previous: ConversationTurn | None = None
    get_calls: list[tuple[str, UUID, UUID]] = field(default_factory=list)
    previous_calls: list[tuple[str, UUID, UUID]] = field(default_factory=list)

    def get_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> ConversationTurn | None:
        self.get_calls.append((character_id, conversation_id, turn_id))
        return self.current

    def get_previous_completed_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> ConversationTurn | None:
        self.previous_calls.append((character_id, conversation_id, turn_id))
        return self.previous


def _job():
    from app.memory.formation.contracts import MemoryFormationJob

    return MemoryFormationJob(
        character_id="miori",
        conversation_id=CONVERSATION_ID,
        turn_id=TURN_ID,
    )


def _worker(
    repository: FakeRepository,
    extractor: MagicMock,
    admission: MagicMock,
    router: object | None,
):
    from app.memory.formation.worker import MemoryFormationWorker

    return MemoryFormationWorker(
        conversation_repository=repository,
        extractor=extractor,
        admission_service=admission,
        domain_router=router,
    )


def test_worker_rereads_source_and_previous_then_admits_candidates_serially() -> None:
    current = _turn()
    previous = _turn(turn_id=PREVIOUS_TURN_ID, user_content="previous user")
    repository = FakeRepository(current=current, previous=previous)
    candidates = (_candidate("紅茶"), _candidate("静かな場所"), _candidate("短い返答"))
    extractor = MagicMock()
    extractor.extract.return_value = candidates
    admission = MagicMock()
    router = MagicMock()

    _worker(repository, extractor, admission, router).process(_job())

    assert repository.get_calls == [("miori", CONVERSATION_ID, TURN_ID)]
    assert repository.previous_calls == [("miori", CONVERSATION_ID, TURN_ID)]
    extractor.extract.assert_called_once_with(
        current_turn=current,
        previous_turn=previous,
    )
    router.dispatch.assert_called_once_with(current)
    assert [call.kwargs["candidate_index"] for call in admission.admit.call_args_list] == [
        0,
        1,
        2,
    ]
    assert [call.args[0] for call in admission.admit.call_args_list] == list(candidates)
    assert all(
        call.kwargs
        == {
            "character_id": "miori",
            "conversation_id": CONVERSATION_ID,
            "turn_id": TURN_ID,
            "candidate_index": index,
        }
        for index, call in enumerate(admission.admit.call_args_list)
    )


@pytest.mark.parametrize(
    "source",
    [
        None,
        _turn(
            status=TurnStatus.PRIVACY_SKIPPED,
            user_content=None,
            assistant_content=None,
        ),
        _turn(status=TurnStatus.PROCESSING, assistant_content=None),
        _turn(user_content=None),
        _turn(assistant_content=None),
    ],
    ids=["deleted", "privacy-skipped", "not-completed", "user-erased", "assistant-erased"],
)
def test_ineligible_source_has_no_extraction_dispatch_or_admission_side_effects(
    source: ConversationTurn | None,
) -> None:
    repository = FakeRepository(current=source)
    extractor = MagicMock()
    admission = MagicMock()
    router = MagicMock()

    _worker(repository, extractor, admission, router).process(_job())

    extractor.extract.assert_not_called()
    router.dispatch.assert_not_called()
    admission.admit.assert_not_called()
    assert repository.previous_calls == []


@pytest.mark.parametrize("router", [None, "noop"])
def test_absent_or_noop_domain_router_does_not_change_persona_admission(
    router: object | None,
) -> None:
    from app.memory.formation.domain_router import NoOpDomainRecordRouter

    extractor = MagicMock()
    candidate = _candidate("紅茶")
    extractor.extract.return_value = (candidate,)
    admission = MagicMock()
    resolved_router = None if router is None else NoOpDomainRecordRouter()

    _worker(FakeRepository(current=_turn()), extractor, admission, resolved_router).process(
        _job()
    )

    admission.admit.assert_called_once()
    assert admission.admit.call_args.args == (candidate,)


@pytest.mark.parametrize(
    "previous",
    [
        _turn(turn_id=PREVIOUS_TURN_ID, user_content=None),
        _turn(turn_id=PREVIOUS_TURN_ID, assistant_content=None),
    ],
    ids=["user-erased", "assistant-erased"],
)
def test_incomplete_previous_turn_is_ignored_without_losing_current_candidates(
    previous: ConversationTurn,
) -> None:
    current = _turn()
    repository = FakeRepository(current=current, previous=previous)
    candidate = _candidate("紅茶")
    extractor = MagicMock()
    extractor.extract.return_value = (candidate,)
    admission = MagicMock()

    _worker(repository, extractor, admission, None).process(_job())

    extractor.extract.assert_called_once_with(
        current_turn=current,
        previous_turn=None,
    )
    admission.admit.assert_called_once_with(
        candidate,
        character_id="miori",
        conversation_id=CONVERSATION_ID,
        turn_id=TURN_ID,
        candidate_index=0,
    )


def test_admission_failure_is_isolated_to_its_candidate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidates = (
        _candidate("紅茶"),
        _candidate(PRIVATE_CANDIDATE),
        _candidate("短い返答"),
    )
    extractor = MagicMock()
    extractor.extract.return_value = candidates
    admission = MagicMock()
    admission.admit.side_effect = [None, RuntimeError(PRIVATE_CANDIDATE), None]
    caplog.set_level(logging.DEBUG)

    _worker(FakeRepository(current=_turn()), extractor, admission, None).process(_job())

    assert [call.args[0] for call in admission.admit.call_args_list] == list(candidates)
    assert "candidate_index=1" in caplog.text
    assert "memory_type=USER_PREFERENCE" in caplog.text
    assert PRIVATE_CANDIDATE not in caplog.text


def test_worker_logs_metadata_only_when_extraction_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    extractor = MagicMock()
    extractor.extract.side_effect = RuntimeError(
        f"{PRIVATE_SOURCE} {PRIVATE_CANDIDATE} synthetic-private-prompt"
    )
    admission = MagicMock()
    caplog.set_level(logging.DEBUG)

    _worker(FakeRepository(current=_turn()), extractor, admission, None).process(_job())

    admission.admit.assert_not_called()
    observed = caplog.text
    assert PRIVATE_SOURCE not in observed
    assert PRIVATE_CANDIDATE not in observed
    assert "synthetic-private-prompt" not in observed


def test_public_job_payload_contains_identifiers_only() -> None:
    from dataclasses import asdict

    payload = asdict(_job())

    assert payload == {
        "character_id": "miori",
        "conversation_id": CONVERSATION_ID,
        "turn_id": TURN_ID,
    }
