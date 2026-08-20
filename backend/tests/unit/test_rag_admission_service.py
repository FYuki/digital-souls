from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock, call
from uuid import UUID

import pytest

from app.conversation_history.models import ConversationTurn, TurnStatus
from app.memory.admission.contracts import (
    ApprovedMemoryCandidate,
    ConversationSource,
    MemoryCandidate,
    MemoryType,
    PreferencePolarity,
    RagAdmissionDecision,
    UserPreferenceValue,
)
from app.memory.admission.evaluator import create_rag_admission_evaluator
from app.memory.memory_policy import resolved_memory_policy
from app.memory.persistence.contracts import (
    ApprovedMemory,
    FormationMethod,
    MemorySourceType,
    MemoryStatus,
    TemporalPrecision,
)
from app.privacy.contracts import (
    FindingReasonCode,
    PrivacyCategory,
    PrivacyFinding,
    ScanSuccess,
    StorageScope,
)
from app.privacy.semantic.contracts import (
    ADMISSION,
    PrivacyAssessment,
    SemanticAssessmentReasonCode,
    SemanticClassification,
    SemanticPrivacyCategory,
    SubjectScope,
)


CREATED_AT = datetime(2026, 8, 19, 12, 34, 56, tzinfo=UTC)
CONVERSATION_ID = UUID("10000000-0000-4000-8000-000000000001")
TURN_ID = UUID("20000000-0000-4000-8000-000000000001")
MEMORY_ID = UUID("30000000-0000-4000-8000-000000000001")
SOURCE_TEXT = "紅茶が好きです"
ASSISTANT_TEXT = "紅茶がお好きなのですね"
SECRET_SLOT_TEXT = "slot-secret-value"
CHARACTER_ID = "miori"


def _candidate(
    *,
    object_value: str = "紅茶",
    source: ConversationSource | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        memory_type=MemoryType.USER_PREFERENCE,
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object=object_value,
        ),
        source=source
        or ConversationSource(
            turn_status=TurnStatus.FAILED,
            history_content_stored=False,
        ),
    )


def _turn(
    *,
    status: TurnStatus = TurnStatus.COMPLETED,
    user_content: str | None = SOURCE_TEXT,
    assistant_content: str | None = ASSISTANT_TEXT,
    character_id: str = "miori",
) -> ConversationTurn:
    return ConversationTurn(
        turn_id=TURN_ID,
        character_id=character_id,
        conversation_id=CONVERSATION_ID,
        user_content=user_content,
        assistant_content=assistant_content,
        status=status,
        privacy_reason_code=None,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def _assessment(
    classification: SemanticClassification = SemanticClassification.NOT_SENSITIVE,
    subject_scope: SubjectScope = SubjectScope.SELF,
) -> PrivacyAssessment:
    if classification is SemanticClassification.ABSTAIN:
        category = SemanticPrivacyCategory.UNKNOWN
        reason_code = SemanticAssessmentReasonCode.INVALID_OUTPUT
        subject_scope = SubjectScope.UNKNOWN
    elif classification is SemanticClassification.SENSITIVE:
        category = SemanticPrivacyCategory.HEALTH
        reason_code = SemanticAssessmentReasonCode.SENSITIVE_CONTENT
    else:
        category = SemanticPrivacyCategory.NONE
        reason_code = SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT
    return PrivacyAssessment(
        classification=classification,
        subject_scope=subject_scope,
        category=category,
        reason_code=reason_code,
        classifier_version="classifier-v1",
        model_id="gemma4:e4b",
        model_digest="model-digest",
        prompt_version="prompt-v1",
        policy_version=resolved_memory_policy().privacy.policy_version,
    )


def _finding(
    category: PrivacyCategory,
    storage_scope: StorageScope | None = None,
) -> PrivacyFinding:
    return PrivacyFinding(
        category=category,
        start=0,
        end=1,
        confidence=1.0,
        reason_code=FindingReasonCode.DETERMINISTIC_MATCH,
        recognizer_version="recognizer-v1",
        policy_version=resolved_memory_policy().privacy.policy_version,
        storage_scope=storage_scope,
    )


class _TurnRepository:
    def __init__(self, turns: list[ConversationTurn | None]) -> None:
        self._turns = iter(turns)
        self.calls: list[tuple[str, UUID, UUID]] = []

    def get_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> ConversationTurn | None:
        self.calls.append((character_id, conversation_id, turn_id))
        return next(self._turns)


@dataclass
class _ApprovedRepository:
    active: list[ApprovedMemory]

    def __post_init__(self) -> None:
        self.list_active_calls: list[str] = []
        self.save_calls: list[dict[str, object]] = []
        self.touch_calls: list[dict[str, object]] = []

    def list_active(self, *, character_id: str) -> list[ApprovedMemory]:
        self.list_active_calls.append(character_id)
        return list(self.active)

    def save(self, **kwargs: object) -> object:
        self.save_calls.append(kwargs)
        return object()

    def touch(self, **kwargs: object) -> object:
        self.touch_calls.append(kwargs)
        return object()


def _approved_memory(
    *,
    structured_value: UserPreferenceValue | None = None,
) -> ApprovedMemory:
    return ApprovedMemory(
        id=MEMORY_ID,
        character_id="miori",
        provider_id="core",
        memory_kind="SEMANTIC",
        memory_type=MemoryType.USER_PREFERENCE,
        structured_value=structured_value
        or UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object="紅茶",
        ),
        normalized_text=(
            "ユーザーは紅茶を好まない。"
            if structured_value is not None
            and structured_value.polarity is PreferencePolarity.DISLIKE
            else "ユーザーは紅茶を好む。"
        ),
        policy_version="policy-v1",
        content_version=1,
        status=MemoryStatus.ACTIVE,
        effective_at=CREATED_AT,
        effective_timezone="Asia/Tokyo",
        temporal_precision=TemporalPrecision.SECOND,
        expires_at=None,
        last_user_mentioned_at=CREATED_AT,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def _service(
    *,
    turns: list[ConversationTurn | None] | None = None,
    source_scan: ScanSuccess | None = None,
    slot_scan: ScanSuccess | None = None,
    assessment: PrivacyAssessment | None = None,
    active: list[ApprovedMemory] | None = None,
):
    module = importlib.import_module("app.memory.admission_service")
    turn_repository = _TurnRepository(turns or [_turn(), _turn()])
    approved_repository = _ApprovedRepository(active or [])
    scanner = MagicMock()
    scans = {
        SOURCE_TEXT: source_scan or ScanSuccess(()),
        "紅茶": slot_scan or ScanSuccess(()),
        SECRET_SLOT_TEXT: slot_scan or ScanSuccess(()),
    }
    scanner.scan.side_effect = lambda text: scans[text]
    classifier = MagicMock()
    classifier.classify.return_value = assessment or _assessment()
    service = module.RagAdmissionService(
        conversation_repository=turn_repository,
        approved_repository=approved_repository,
        privacy_scanner=scanner,
        semantic_classifier=classifier,
        evaluator=create_rag_admission_evaluator(
            resolved_memory_policy().privacy
        ),
        effective_timezone="Asia/Tokyo",
    )
    return service, turn_repository, approved_repository, scanner, classifier


def _admit(service, candidate: MemoryCandidate | None = None):
    return service.admit(
        candidate or _candidate(),
        character_id=CHARACTER_ID,
        conversation_id=CONVERSATION_ID,
        turn_id=TURN_ID,
        candidate_index=2,
    )


@pytest.mark.parametrize(
    ("source_scan", "assessment", "expected"),
    [
        (
            ScanSuccess((_finding(PrivacyCategory.API_KEY),)),
            _assessment(),
            RagAdmissionDecision.DENY_SENSITIVE,
        ),
        (
            ScanSuccess(
                (
                    _finding(
                        PrivacyCategory.STORAGE_OPT_OUT,
                        StorageScope.RAG,
                    ),
                )
            ),
            _assessment(),
            RagAdmissionDecision.DENY_USER_REQUEST,
        ),
        (
            ScanSuccess(()),
            _assessment(SemanticClassification.ABSTAIN),
            RagAdmissionDecision.ABSTAIN_UNKNOWN,
        ),
        (
            ScanSuccess(()),
            _assessment(subject_scope=SubjectScope.GENERAL),
            RagAdmissionDecision.NOT_MEMORY_WORTHY,
        ),
    ],
)
def test_non_allow_decisions_never_reach_the_approved_repository(
    source_scan: ScanSuccess,
    assessment: PrivacyAssessment,
    expected: RagAdmissionDecision,
) -> None:
    service, _turns, repository, _scanner, _classifier = _service(
        source_scan=source_scan,
        assessment=assessment,
    )

    result = _admit(service)

    assert result.decision is expected
    assert repository.list_active_calls == []
    assert repository.save_calls == []
    assert repository.touch_calls == []


def test_allow_scans_source_and_each_slot_then_classifies_the_source_with_admission_profile() -> None:
    candidate = MemoryCandidate(
        memory_type=MemoryType.USER_PREFERENCE,
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.PREFER_OVER,
            object="紅茶",
            alternative=SECRET_SLOT_TEXT,
        ),
        source=ConversationSource(TurnStatus.FAILED, False),
    )
    service, _turns, repository, scanner, classifier = _service()

    result = _admit(service, candidate)

    assert result.decision is RagAdmissionDecision.ALLOW_STRUCTURED
    assert scanner.scan.call_args_list == [
        call(SOURCE_TEXT),
        call("紅茶"),
        call(SECRET_SLOT_TEXT),
    ]
    classifier.classify.assert_called_once_with(SOURCE_TEXT, ADMISSION)
    assert len(repository.save_calls) == 1


def test_classifier_is_not_called_when_deterministic_checks_finish_the_decision() -> None:
    service, _turns, repository, _scanner, classifier = _service(
        source_scan=ScanSuccess((_finding(PrivacyCategory.API_KEY),)),
    )

    result = _admit(service)

    assert result.decision is RagAdmissionDecision.DENY_SENSITIVE
    classifier.classify.assert_not_called()
    assert repository.save_calls == []


@pytest.mark.parametrize(
    "invalid_turn",
    [
        None,
        _turn(status=TurnStatus.PRIVACY_SKIPPED, user_content=None, assistant_content=None),
        _turn(status=TurnStatus.PROCESSING, assistant_content=None),
        _turn(user_content=None),
        _turn(assistant_content=None),
        _turn(character_id="akira"),
    ],
    ids=[
        "missing",
        "privacy-skipped",
        "not-completed",
        "user-missing",
        "assistant-missing",
        "other-character",
    ],
)
def test_invalid_source_at_initial_validation_has_no_processing_or_persistence(
    invalid_turn: ConversationTurn | None,
) -> None:
    service, turns, repository, scanner, classifier = _service(
        turns=[invalid_turn],
    )

    result = _admit(service)

    assert result.decision is RagAdmissionDecision.ABSTAIN_UNKNOWN
    assert turns.calls == [(CHARACTER_ID, CONVERSATION_ID, TURN_ID)]
    scanner.scan.assert_not_called()
    classifier.classify.assert_not_called()
    assert repository.list_active_calls == []
    assert repository.save_calls == []
    assert repository.touch_calls == []


@pytest.mark.parametrize(
    "invalid_turn",
    [
        None,
        _turn(status=TurnStatus.PRIVACY_SKIPPED, user_content=None, assistant_content=None),
        _turn(status=TurnStatus.PROCESSING, assistant_content=None),
        _turn(user_content=None),
        _turn(assistant_content=None),
        _turn(character_id="akira"),
    ],
    ids=[
        "deleted",
        "privacy-skipped",
        "not-completed",
        "user-missing",
        "assistant-missing",
        "other-character",
    ],
)
def test_invalid_source_at_transaction_validation_blocks_save_and_touch(
    invalid_turn: ConversationTurn | None,
) -> None:
    service, turns, repository, _scanner, _classifier = _service(
        turns=[_turn(), invalid_turn],
    )

    result = _admit(service)

    assert result.decision is RagAdmissionDecision.ABSTAIN_UNKNOWN
    assert turns.calls == [
        (CHARACTER_ID, CONVERSATION_ID, TURN_ID),
        (CHARACTER_ID, CONVERSATION_ID, TURN_ID),
    ]
    assert repository.list_active_calls == ["miori"]
    assert repository.save_calls == []
    assert repository.touch_calls == []


def test_allow_save_uses_authoritative_turn_and_assessment_provenance() -> None:
    caller_source = ConversationSource(TurnStatus.FAILED, False)
    service, _turns, repository, _scanner, _classifier = _service()

    result = _admit(service, _candidate(source=caller_source))

    assert result.decision is RagAdmissionDecision.ALLOW_STRUCTURED
    call = repository.save_calls[0]
    assert call["character_id"] == "miori"
    saved_candidate = cast(ApprovedMemoryCandidate, call["candidate"])
    assert saved_candidate.structured_value == _candidate().structured_value
    context = call["context"]
    assert context.formation_method is FormationMethod.EXTRACTED
    idempotency_parts = context.idempotency_key.split(":")
    assert idempotency_parts[:4] == [
        "miori",
        str(CONVERSATION_ID),
        str(TURN_ID),
        "2",
    ]
    assert len(idempotency_parts) == 5
    assert idempotency_parts[4]
    assert context.effective_at == CREATED_AT
    assert context.effective_timezone == "Asia/Tokyo"
    assert context.temporal_precision is TemporalPrecision.SECOND
    assert context.expires_at is None
    assert context.lineage == ()
    assert (
        context.policy_version,
        context.classifier_version,
        context.model_id,
        context.model_digest,
        context.prompt_version,
    ) == (
        resolved_memory_policy().privacy.policy_version,
        "classifier-v1",
        "gemma4:e4b",
        "model-digest",
        "prompt-v1",
    )
    assert len(context.sources) == 1
    assert context.sources[0].source_type is MemorySourceType.CONVERSATION_TURN
    assert context.sources[0].source_provider_id == "core"
    assert context.sources[0].source_ref == str(TURN_ID)


def test_exact_active_match_is_touched_at_the_source_turn_time() -> None:
    existing = _approved_memory()
    service, _turns, repository, _scanner, _classifier = _service(active=[existing])

    result = _admit(service)

    assert result.decision is RagAdmissionDecision.ALLOW_STRUCTURED
    assert repository.save_calls == []
    assert repository.touch_calls == [
        {
            "character_id": "miori",
            "memory_id": MEMORY_ID,
            "candidate": result.candidate,
            "mentioned_at": CREATED_AT,
        }
    ]


def test_different_active_structure_is_saved_not_touched() -> None:
    existing = _approved_memory(
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.DISLIKE,
            object="紅茶",
        )
    )
    service, _turns, repository, _scanner, _classifier = _service(active=[existing])

    result = _admit(service)

    assert result.decision is RagAdmissionDecision.ALLOW_STRUCTURED
    assert len(repository.save_calls) == 1
    assert repository.touch_calls == []


def test_admission_logs_neither_source_slots_nor_privacy_findings(caplog) -> None:
    service, _turns, _repository, _scanner, _classifier = _service(
        slot_scan=ScanSuccess((_finding(PrivacyCategory.API_KEY),)),
    )

    with caplog.at_level(logging.DEBUG):
        result = _admit(service, _candidate(object_value=SECRET_SLOT_TEXT))

    assert result.decision is RagAdmissionDecision.DENY_SENSITIVE
    rendered_records = "\n".join(record.getMessage() for record in caplog.records)
    assert SOURCE_TEXT not in rendered_records
    assert SECRET_SLOT_TEXT not in rendered_records
    assert "PrivacyFinding" not in rendered_records
