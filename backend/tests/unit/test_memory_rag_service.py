from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, call
from uuid import UUID

import pytest

from app.memory.admission.contracts import (
    MemoryType,
    PreferencePolarity,
    UserPreferenceValue,
)
from app.memory.chroma_store import MemorySearchCandidate
from app.memory.memory_policy import resolved_memory_policy
from app.memory.persistence.contracts import (
    ApprovedMemory,
    MemoryStatus,
    TemporalPrecision,
)
from app.privacy.contracts import (
    FindingReasonCode,
    PrivacyCategory,
    PrivacyFinding,
    ScanFailure,
    ScanFailureReasonCode,
    ScanSuccess,
    StorageScope,
)
from app.privacy.semantic.contracts import (
    QUERY_GATE,
    PrivacyAssessment,
    SemanticAssessmentReasonCode,
    SemanticClassification,
    SemanticPrivacyCategory,
    SubjectScope,
)


_CHROMA_PATH = Path("/test/runtime-data/chroma")
_MEMORY_ID = UUID("00000000-0000-4000-8000-000000000042")


def _assessment(
    classification: SemanticClassification,
    reason_code: SemanticAssessmentReasonCode,
) -> PrivacyAssessment:
    if classification is SemanticClassification.SENSITIVE:
        subject_scope = SubjectScope.SELF
        category = SemanticPrivacyCategory.HEALTH
    elif classification is SemanticClassification.NOT_SENSITIVE:
        subject_scope = SubjectScope.GENERAL
        category = SemanticPrivacyCategory.NONE
    else:
        subject_scope = SubjectScope.UNKNOWN
        category = SemanticPrivacyCategory.UNKNOWN
    return PrivacyAssessment(
        classification=classification,
        subject_scope=subject_scope,
        category=category,
        reason_code=reason_code,
        classifier_version="classifier-v1",
        model_id="model-v1",
        model_digest="sha256:test",
        prompt_version="prompt-v1",
        policy_version=resolved_memory_policy().policy_version,
    )


def _approved_memory(**overrides: object) -> ApprovedMemory:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    values: dict[str, object] = {
        "id": _MEMORY_ID,
        "character_id": "miori",
        "provider_id": "core",
        "memory_kind": "SEMANTIC",
        "memory_type": MemoryType.USER_PREFERENCE,
        "structured_value": UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object="紅茶",
        ),
        "normalized_text": "SQLiteに保存された紅茶の好み",
        "policy_version": resolved_memory_policy().policy_version,
        "content_version": 1,
        "status": MemoryStatus.ACTIVE,
        "effective_at": now,
        "effective_timezone": "Asia/Tokyo",
        "temporal_precision": TemporalPrecision.SECOND,
        "expires_at": datetime(2999, 1, 1, tzinfo=UTC),
        "last_user_mentioned_at": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return ApprovedMemory(**values)  # type: ignore[arg-type]


def _candidate(
    memory_id: UUID | str = _MEMORY_ID, raw_distance: float = 1.75
) -> MemorySearchCandidate:
    return MemorySearchCandidate(memory_id=str(memory_id), raw_distance=raw_distance)


def _dependencies(
    *,
    query_scan: object = ScanSuccess(()),
    body_scan: object = ScanSuccess(()),
    assessment: PrivacyAssessment | None = None,
    memories: dict[UUID, ApprovedMemory | None] | None = None,
):
    scanner = MagicMock()
    scanner.scan.side_effect = [query_scan, body_scan]
    classifier = MagicMock()
    classifier.classify.return_value = assessment or _assessment(
        SemanticClassification.NOT_SENSITIVE,
        SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT,
    )
    repository = MagicMock()
    records = memories if memories is not None else {_MEMORY_ID: _approved_memory()}
    repository.get.side_effect = lambda *, character_id, memory_id: records.get(
        memory_id
    )
    return scanner, classifier, repository


def _retrieve(
    rag_service,
    *,
    scanner,
    classifier,
    repository,
    user_message: str = "紅茶の好みは？",
):
    return rag_service.retrieve_prompt_memories(
        "miori",
        user_message,
        resolved_memory_policy(),
        scanner=scanner,
        classifier=classifier,
        approved_repository=repository,
        chroma_path=_CHROMA_PATH,
    )


def _absolute_deny_finding(category: PrivacyCategory) -> PrivacyFinding:
    return PrivacyFinding(
        category=category,
        start=0,
        end=1,
        confidence=1.0,
        reason_code=FindingReasonCode.DETERMINISTIC_MATCH,
        recognizer_version="recognizer-v1",
        policy_version=resolved_memory_policy().policy_version,
        storage_scope=None,
    )


def test_query_absolute_deny_uses_resolved_policy_before_any_search(monkeypatch):
    from app.memory import rag_service

    policy = resolved_memory_policy()
    configured_category = PrivacyCategory.EMAIL
    policy = replace(
        policy,
        privacy=replace(
            policy.privacy,
            absolute_deny_categories=frozenset({configured_category}),
        ),
    )
    scanner, classifier, repository = _dependencies(
        query_scan=ScanSuccess((_absolute_deny_finding(configured_category),))
    )
    embed = MagicMock()
    query = MagicMock()
    monkeypatch.setattr(rag_service, "embed_text", embed)
    monkeypatch.setattr(rag_service, "query_memories", query)

    memories = rag_service.retrieve_prompt_memories(
        "miori",
        "x",
        policy,
        scanner=scanner,
        classifier=classifier,
        approved_repository=repository,
        chroma_path=_CHROMA_PATH,
    )

    assert memories == ()
    classifier.classify.assert_not_called()
    embed.assert_not_called()
    query.assert_not_called()


def test_query_scan_failure_skips_classifier_embedding_and_chroma(monkeypatch):
    from app.memory import rag_service

    failure = ScanFailure(
        ScanFailureReasonCode.RECOGNIZER_ERROR,
        "recognizer-v1",
        resolved_memory_policy().policy_version,
    )
    scanner, classifier, repository = _dependencies(query_scan=failure)
    embed = MagicMock()
    query = MagicMock()
    monkeypatch.setattr(rag_service, "embed_text", embed)
    monkeypatch.setattr(rag_service, "query_memories", query)

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert memories == ()
    classifier.classify.assert_not_called()
    embed.assert_not_called()
    query.assert_not_called()


def test_query_scanner_exception_returns_empty_without_starting_rag(monkeypatch):
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies()
    scanner.scan.side_effect = RuntimeError("scanner unavailable")
    embed = MagicMock()
    query = MagicMock()
    monkeypatch.setattr(rag_service, "embed_text", embed)
    monkeypatch.setattr(rag_service, "query_memories", query)

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert memories == ()
    classifier.classify.assert_not_called()
    embed.assert_not_called()
    query.assert_not_called()


def test_scan_failure_warning_contains_metadata_only(monkeypatch, caplog):
    from app.memory import rag_service

    secret_query = "SECRET_QUERY_7F3A"
    failure = ScanFailure(
        ScanFailureReasonCode.RECOGNIZER_ERROR,
        "recognizer-v1",
        resolved_memory_policy().policy_version,
    )
    scanner, classifier, repository = _dependencies(query_scan=failure)
    monkeypatch.setattr(rag_service, "embed_text", MagicMock())
    monkeypatch.setattr(rag_service, "query_memories", MagicMock())

    with caplog.at_level("WARNING", logger=rag_service.__name__):
        _retrieve(
            rag_service,
            scanner=scanner,
            classifier=classifier,
            repository=repository,
            user_message=secret_query,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        failure.reason_code.value in message
        and failure.recognizer_version in message
        and failure.policy_version in message
        for message in messages
    )
    assert all(secret_query not in message for message in messages)


@pytest.mark.parametrize(
    ("classification", "reason_code"),
    (
        (
            SemanticClassification.SENSITIVE,
            SemanticAssessmentReasonCode.SENSITIVE_CONTENT,
        ),
        (SemanticClassification.ABSTAIN, SemanticAssessmentReasonCode.TIMEOUT),
        (
            SemanticClassification.ABSTAIN,
            SemanticAssessmentReasonCode.INVALID_OUTPUT,
        ),
        (
            SemanticClassification.ABSTAIN,
            SemanticAssessmentReasonCode.MODEL_NOT_LOADED,
        ),
    ),
)
def test_semantic_gate_only_allows_not_sensitive_queries(
    monkeypatch, classification, reason_code
):
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies(
        assessment=_assessment(classification, reason_code)
    )
    embed = MagicMock()
    query = MagicMock()
    monkeypatch.setattr(rag_service, "embed_text", embed)
    monkeypatch.setattr(rag_service, "query_memories", query)

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert memories == ()
    classifier.classify.assert_called_once_with("紅茶の好みは？", QUERY_GATE)
    embed.assert_not_called()
    query.assert_not_called()


def test_safe_query_returns_only_scanned_sqlite_text_with_raw_distance(monkeypatch):
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies()
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service, "query_memories", MagicMock(return_value=[_candidate()])
    )

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert len(memories) == 1
    assert memories[0].memory_id == str(_MEMORY_ID)
    assert memories[0].normalized_text == "SQLiteに保存された紅茶の好み"
    assert memories[0].raw_distance == 1.75
    classifier.classify.assert_called_once_with("紅茶の好みは？", QUERY_GATE)
    assert scanner.scan.call_args_list == [
        call("紅茶の好みは？"),
        call("SQLiteに保存された紅茶の好み"),
    ]


def test_each_retrieval_classifies_independently_without_assessment_cache(monkeypatch):
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies()
    scanner.scan.side_effect = [ScanSuccess(())] * 4
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service, "query_memories", MagicMock(return_value=[_candidate()])
    )

    for _index in range(2):
        _retrieve(
            rag_service,
            scanner=scanner,
            classifier=classifier,
            repository=repository,
        )

    assert classifier.classify.call_args_list == [
        call("紅茶の好みは？", QUERY_GATE),
        call("紅茶の好みは？", QUERY_GATE),
    ]


@pytest.mark.parametrize(
    "memory",
    (
        None,
        _approved_memory(status=MemoryStatus.INACTIVE),
        _approved_memory(expires_at=datetime(2000, 1, 1, tzinfo=UTC)),
        _approved_memory(character_id="other"),
        _approved_memory(provider_id="addon"),
        _approved_memory(policy_version="unsupported-policy"),
    ),
)
def test_sqlite_authority_excludes_each_unapproved_candidate(monkeypatch, memory):
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies(memories={_MEMORY_ID: memory})
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service, "query_memories", MagicMock(return_value=[_candidate()])
    )

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert memories == ()


@pytest.mark.parametrize(
    "body_scan",
    (
        ScanFailure(
            ScanFailureReasonCode.INVALID_RECOGNIZER_RESULT,
            "recognizer-v1",
            resolved_memory_policy().policy_version,
        ),
        ScanSuccess((_absolute_deny_finding(PrivacyCategory.EMAIL),)),
    ),
)
def test_retrieval_scan_failure_or_absolute_deny_excludes_whole_candidate(
    monkeypatch, body_scan
):
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies(body_scan=body_scan)
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service, "query_memories", MagicMock(return_value=[_candidate()])
    )

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert memories == ()


def test_unsupported_retrieved_body_scan_result_discards_rag_result(monkeypatch):
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies(body_scan=object())
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service, "query_memories", MagicMock(return_value=[_candidate()])
    )

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert memories == ()


def test_storage_opt_out_in_retrieved_text_does_not_exclude_candidate(monkeypatch):
    from app.memory import rag_service

    storage_opt_out = PrivacyFinding(
        category=PrivacyCategory.STORAGE_OPT_OUT,
        start=0,
        end=1,
        confidence=1.0,
        reason_code=FindingReasonCode.STORAGE_OPT_OUT_MATCH,
        recognizer_version="recognizer-v1",
        policy_version=resolved_memory_policy().policy_version,
        storage_scope=StorageScope.RAG,
    )
    memory = _approved_memory(normalized_text="覚えないでと言った出来事")
    scanner, classifier, repository = _dependencies(
        body_scan=ScanSuccess((storage_opt_out,)), memories={_MEMORY_ID: memory}
    )
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service, "query_memories", MagicMock(return_value=[_candidate()])
    )

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert [memory.normalized_text for memory in memories] == [
        "覚えないでと言った出来事"
    ]


def test_invalid_candidate_does_not_remove_valid_sibling(monkeypatch):
    from app.memory import rag_service

    invalid_id = UUID("00000000-0000-4000-8000-000000000043")
    scanner, classifier, repository = _dependencies(
        memories={_MEMORY_ID: _approved_memory(), invalid_id: None}
    )
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(return_value=[_candidate(), _candidate(invalid_id, 2.0)]),
    )

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert [memory.memory_id for memory in memories] == [str(_MEMORY_ID)]


@pytest.mark.parametrize(
    "candidates",
    (
        [_candidate("corrupt-id", 2.0), _candidate()],
        [_candidate(), _candidate("corrupt-id", 2.0)],
    ),
)
def test_malformed_uuid_candidate_does_not_remove_valid_sibling(
    monkeypatch, candidates
):
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies()
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(return_value=candidates),
    )

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert len(memories) == 1
    assert memories[0].memory_id == str(_MEMORY_ID)
    assert memories[0].normalized_text == "SQLiteに保存された紅茶の好み"
    assert memories[0].raw_distance == 1.75
    repository.get.assert_called_once_with(
        character_id="miori",
        memory_id=_MEMORY_ID,
    )


def test_sqlite_failure_discards_already_verified_results_without_fallback(monkeypatch):
    from app.memory import rag_service

    second_id = UUID("00000000-0000-4000-8000-000000000043")
    scanner, classifier, repository = _dependencies()
    repository.get.side_effect = [_approved_memory(), OSError("sqlite unavailable")]
    scanner.scan.side_effect = [ScanSuccess(()), ScanSuccess(())]
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(return_value=[_candidate(), _candidate(second_id, 2.0)]),
    )

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert memories == ()


def test_chroma_failure_returns_no_unverified_memory(monkeypatch):
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies()
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service, "query_memories", MagicMock(side_effect=RuntimeError("chroma"))
    )

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert memories == ()
    repository.get.assert_not_called()


def test_retrieval_filter_is_read_only(monkeypatch):
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies(
        memories={_MEMORY_ID: _approved_memory(status=MemoryStatus.INACTIVE)}
    )
    repository.deactivate = MagicMock()
    repository.hard_delete = MagicMock()
    delete_index = MagicMock()
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service, "query_memories", MagicMock(return_value=[_candidate()])
    )
    monkeypatch.setattr(
        rag_service, "delete_memory_index_entry", delete_index, raising=False
    )

    _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    repository.deactivate.assert_not_called()
    repository.hard_delete.assert_not_called()
    delete_index.assert_not_called()
