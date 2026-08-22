from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, call
from uuid import UUID

import pytest

from app.memory.admission.contracts import (
    ApprovedMemoryCandidate,
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
        "occurred_at": now,
        "occurred_timezone": "Asia/Tokyo",
        "occurred_precision": TemporalPrecision.SECOND,
        "stated_at": now,
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
    outcome = rag_service.retrieve_prompt_memories(
        "miori",
        user_message,
        resolved_memory_policy(),
        scanner=scanner,
        classifier=classifier,
        approved_repository=repository,
        chroma_path=_CHROMA_PATH,
        now=datetime(2026, 8, 20, 3, 30, tzinfo=UTC),
        timezone="Asia/Tokyo",
    )
    return outcome.memories


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

    outcome = rag_service.retrieve_prompt_memories(
        "miori",
        "去年3月の連絡先 user@example.com を教えて",
        policy,
        scanner=scanner,
        classifier=classifier,
        approved_repository=repository,
        chroma_path=_CHROMA_PATH,
        now=datetime(2026, 8, 20, 3, 30, tzinfo=UTC),
        timezone="Asia/Tokyo",
    )

    assert outcome.memories == ()
    classifier.classify.assert_not_called()
    embed.assert_not_called()
    query.assert_not_called()
    repository.search_by_occurred_range.assert_not_called()


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
    repository.search_by_occurred_range.assert_not_called()


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


def test_invalid_semantic_assessment_returns_empty_with_fixed_warning(
    monkeypatch, caplog
):
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies()
    classifier.classify.return_value = object()
    embed = MagicMock()
    query = MagicMock()
    monkeypatch.setattr(rag_service, "embed_text", embed)
    monkeypatch.setattr(rag_service, "query_memories", query)

    with caplog.at_level("WARNING", logger=rag_service.__name__):
        memories = _retrieve(
            rag_service,
            scanner=scanner,
            classifier=classifier,
            repository=repository,
        )

    assert memories == ()
    assert [record.getMessage() for record in caplog.records] == [
        "Skipped RAG memory lookup: invalid semantic assessment"
    ]
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


def test_unsupported_body_scan_result_excludes_only_current_candidate(monkeypatch):
    from app.memory import rag_service

    second_id = UUID("00000000-0000-4000-8000-000000000043")
    third_id = UUID("00000000-0000-4000-8000-000000000044")
    scanner, classifier, repository = _dependencies(
        memories={
            _MEMORY_ID: _approved_memory(),
            second_id: _approved_memory(
                id=second_id,
                normalized_text="契約外scan結果の候補",
            ),
            third_id: _approved_memory(
                id=third_id,
                normalized_text="後続の有効な候補",
            ),
        }
    )
    scanner.scan.side_effect = [
        ScanSuccess(()),
        ScanSuccess(()),
        object(),
        ScanSuccess(()),
    ]
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(
            return_value=[
                _candidate(),
                _candidate(second_id, 2.0),
                _candidate(third_id, 2.25),
            ]
        ),
    )

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert [memory.memory_id for memory in memories] == [
        str(_MEMORY_ID),
        str(third_id),
    ]


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


def test_hard_deleted_memory_is_excluded_when_chroma_still_returns_its_id(
    tmp_path: Path, monkeypatch
):
    from app.memory import rag_service
    from app.memory.persistence.approved_repository import ApprovedMemoryRepository
    from app.memory.persistence.contracts import (
        FormationMethod,
        MemorySourceInput,
        MemorySourceType,
        MemoryWriteContext,
        TemporalPrecision,
    )
    from app.memory.persistence.schema import initialize_persona_memory_schema
    from app.runtime_paths import resolve_runtime_paths

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = resolve_runtime_paths(
        {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(tmp_path / "data")},
        repository_root,
    )
    initialize_persona_memory_schema(paths, repository_root)
    outbox_ids = iter(
        (
            UUID("10000000-0000-4000-8000-000000000042"),
            UUID("10000000-0000-4000-8000-000000000043"),
        )
    )
    repository = ApprovedMemoryRepository(
        database_path=paths.persona_memory_sqlite_path,
        clock=lambda: datetime(2026, 8, 20, tzinfo=UTC),
        uuid_factory=lambda: _MEMORY_ID,
        outbox_uuid_factory=lambda: next(outbox_ids),
    )
    policy = resolved_memory_policy()
    memory = repository.save(
        character_id="miori",
        candidate=ApprovedMemoryCandidate(
            structured_value=UserPreferenceValue(
                polarity=PreferencePolarity.LIKE,
                object="紅茶",
            ),
            normalized_text="削除対象の紅茶の好み",
        ),
        context=MemoryWriteContext(
            formation_method=FormationMethod.EXTRACTED,
            idempotency_key="conversation-1:turn-1:0:extractor-v1",
            effective_at=datetime(2026, 8, 20, tzinfo=UTC),
            effective_timezone="Asia/Tokyo",
            temporal_precision=TemporalPrecision.SECOND,
            expires_at=None,
            policy_version=policy.policy_version,
            classifier_version="classifier-v1",
            model_id="model-v1",
            model_digest="sha256:test",
            prompt_version="prompt-v1",
            sources=(
                MemorySourceInput(
                    source_type=MemorySourceType.CONVERSATION_TURN,
                    source_provider_id="core",
                    source_ref="conversation-1:turn-1",
                ),
            ),
        ),
    )
    repository.hard_delete(character_id="miori", memory_id=memory.id)
    scanner, classifier, _ = _dependencies()
    stale_query = MagicMock(return_value=[_candidate(memory.id)])
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(rag_service, "query_memories", stale_query)

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    stale_query.assert_called_once()
    assert memories == ()


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


def _ranking_dependencies(
    memories: dict[UUID, ApprovedMemory | None],
):
    scanner, classifier, repository = _dependencies(memories=memories)
    scanner.scan.side_effect = None
    scanner.scan.return_value = ScanSuccess(())
    return scanner, classifier, repository


def _distance_for_relevance(relevance: float) -> float:
    return (1.0 / relevance - 1.0) ** 2


def test_retrieval_uses_candidate_pool_before_sqlite_verification(monkeypatch):
    from app.memory import rag_service

    valid_id = UUID("00000000-0000-4000-8000-000000000049")
    invalid_ids = tuple(
        UUID(f"00000000-0000-4000-8000-{index:012d}")
        for index in range(43, 48)
    )
    memories = {memory_id: None for memory_id in invalid_ids}
    memories[valid_id] = _approved_memory(id=valid_id)
    scanner, classifier, repository = _ranking_dependencies(memories)
    candidates = [
        *(_candidate(memory_id, 0.01) for memory_id in invalid_ids),
        _candidate(valid_id, 0.02),
    ]
    query = MagicMock(
        side_effect=lambda _character, _embedding, n_results, **_kwargs: candidates[
            :n_results
        ]
    )
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(rag_service, "query_memories", query)

    results = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert [result.memory_id for result in results] == [str(valid_id)]
    assert query.call_args.kwargs["n_results"] == 20


def test_retrieval_filters_below_threshold_and_orders_by_relevance(monkeypatch):
    from app.memory import rag_service

    exact_id = UUID("00000000-0000-4000-8000-000000000042")
    high_id = UUID("00000000-0000-4000-8000-000000000043")
    low_id = UUID("00000000-0000-4000-8000-000000000044")
    below_id = UUID("00000000-0000-4000-8000-000000000045")
    scanner, classifier, repository = _ranking_dependencies(
        {
            exact_id: _approved_memory(id=exact_id),
            high_id: _approved_memory(id=high_id),
            low_id: _approved_memory(id=low_id),
            below_id: _approved_memory(id=below_id),
        }
    )
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
            MagicMock(
                return_value=[
                    _candidate(below_id, _distance_for_relevance(0.0499)),
                    _candidate(low_id, _distance_for_relevance(0.05)),
                    _candidate(high_id, _distance_for_relevance(0.80)),
                    _candidate(exact_id, _distance_for_relevance(1.0)),
                ]
            ),
        )

    results = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert [result.memory_id for result in results] == [
        str(exact_id),
        str(high_id),
        str(low_id),
    ]


@pytest.mark.parametrize(
    ("relevance_gap", "input_first", "expected_first"),
    (
        (0.0019, "relevant", "recent"),
        (0.0020000000005, "recent", "relevant"),
        (0.0021, "recent", "relevant"),
    ),
)
def test_mention_tie_break_applies_only_within_margin_boundary(
    monkeypatch,
    relevance_gap: float,
    input_first: str,
    expected_first: str,
):
    from app.memory import rag_service

    relevant_id = UUID("00000000-0000-4000-8000-000000000043")
    recent_id = UUID("00000000-0000-4000-8000-000000000044")
    scanner, classifier, repository = _ranking_dependencies(
        {
            relevant_id: _approved_memory(
                id=relevant_id,
                last_user_mentioned_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
            recent_id: _approved_memory(
                id=recent_id,
                last_user_mentioned_at=datetime(2026, 8, 20, tzinfo=UTC),
            ),
        }
    )
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    candidates = [
        _candidate(relevant_id, _distance_for_relevance(0.8)),
        _candidate(recent_id, _distance_for_relevance(0.8 - relevance_gap)),
    ]
    if input_first == "recent":
        candidates.reverse()
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(return_value=candidates),
    )

    results = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    expected_id = recent_id if expected_first == "recent" else relevant_id
    assert results[0].memory_id == str(expected_id)


def test_equivalence_margin_uses_only_the_configured_inclusive_boundary():
    from app.memory.ranking import _within_equivalence_margin

    assert _within_equivalence_margin(0.002, 0.002)
    assert not _within_equivalence_margin(0.0020000000005, 0.002)


def test_equivalence_band_does_not_chain_margin_across_candidates(monkeypatch):
    from app.memory import rag_service

    leader_id = UUID("00000000-0000-4000-8000-000000000043")
    bridge_id = UUID("00000000-0000-4000-8000-000000000044")
    outside_id = UUID("00000000-0000-4000-8000-000000000045")
    scanner, classifier, repository = _ranking_dependencies(
        {
            leader_id: _approved_memory(
                id=leader_id,
                last_user_mentioned_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
            bridge_id: _approved_memory(
                id=bridge_id,
                last_user_mentioned_at=datetime(2026, 8, 10, tzinfo=UTC),
            ),
            outside_id: _approved_memory(
                id=outside_id,
                last_user_mentioned_at=datetime(2026, 8, 20, tzinfo=UTC),
            ),
        }
    )
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(
            return_value=[
                _candidate(leader_id, _distance_for_relevance(0.8)),
                _candidate(bridge_id, _distance_for_relevance(0.799)),
                _candidate(outside_id, _distance_for_relevance(0.7979)),
            ]
        ),
    )

    results = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert [result.memory_id for result in results] == [
        str(bridge_id),
        str(leader_id),
        str(outside_id),
    ]


def test_tie_break_places_null_mention_after_mentioned_memory(monkeypatch):
    from app.memory import rag_service

    null_id = UUID("00000000-0000-4000-8000-000000000043")
    mentioned_id = UUID("00000000-0000-4000-8000-000000000044")
    scanner, classifier, repository = _ranking_dependencies(
        {
            null_id: _approved_memory(id=null_id, last_user_mentioned_at=None),
            mentioned_id: _approved_memory(
                id=mentioned_id,
                last_user_mentioned_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
        }
    )
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(
            return_value=[
                _candidate(null_id, _distance_for_relevance(0.8)),
                _candidate(mentioned_id, _distance_for_relevance(0.8)),
            ]
        ),
    )

    results = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert [result.memory_id for result in results] == [
        str(mentioned_id),
        str(null_id),
    ]


def test_equal_candidates_are_stabilized_by_created_at_then_id(monkeypatch):
    from app.memory import rag_service

    older_id = UUID("00000000-0000-4000-8000-000000000045")
    later_id = UUID("00000000-0000-4000-8000-000000000044")
    earlier_id = UUID("00000000-0000-4000-8000-000000000043")
    old_created = datetime(2026, 8, 1, tzinfo=UTC)
    new_created = datetime(2026, 8, 2, tzinfo=UTC)
    scanner, classifier, repository = _ranking_dependencies(
        {
            older_id: _approved_memory(id=older_id, created_at=old_created),
            later_id: _approved_memory(id=later_id, created_at=new_created),
            earlier_id: _approved_memory(id=earlier_id, created_at=new_created),
        }
    )
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(
            return_value=[
                _candidate(older_id, _distance_for_relevance(0.8)),
                _candidate(later_id, _distance_for_relevance(0.8)),
                _candidate(earlier_id, _distance_for_relevance(0.8)),
            ]
        ),
    )

    results = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert [result.memory_id for result in results] == [
        str(earlier_id),
        str(later_id),
        str(older_id),
    ]


def test_retrieval_limits_results_after_ranking(monkeypatch):
    from app.memory import rag_service

    ids = tuple(
        UUID(f"00000000-0000-4000-8000-{index:012d}")
        for index in range(43, 49)
    )
    scanner, classifier, repository = _ranking_dependencies(
        {memory_id: _approved_memory(id=memory_id) for memory_id in ids}
    )
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(
            return_value=[
                _candidate(memory_id, _distance_for_relevance(relevance))
                for memory_id, relevance in zip(
                    reversed(ids),
                    (0.5, 0.55, 0.6, 0.65, 0.7, 0.75),
                    strict=True,
                )
            ]
        ),
    )

    results = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    assert [result.memory_id for result in results] == [
        str(ids[0]),
        str(ids[1]),
        str(ids[2]),
        str(ids[3]),
        str(ids[4]),
    ]


def test_retrieval_does_not_update_last_user_mentioned_at(monkeypatch):
    from app.memory import rag_service

    scanner, classifier, repository = _ranking_dependencies(
        {_MEMORY_ID: _approved_memory()}
    )
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(return_value=[_candidate(_MEMORY_ID, 0.01)]),
    )

    _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
    )

    repository.touch.assert_not_called()


def test_temporal_retrieval_unions_both_paths_and_ranks_by_one_match_kind(
    monkeypatch,
) -> None:
    from app.memory import rag_service
    from app.memory.rag_service import RetrievalMatchKind

    both_id = UUID("00000000-0000-4000-8000-000000000043")
    semantic_id = UUID("00000000-0000-4000-8000-000000000044")
    period_id = UUID("00000000-0000-4000-8000-000000000045")
    memories = {
        both_id: _approved_memory(
            id=both_id,
            normalized_text="静岡へ旅行した",
            occurred_at=datetime(2025, 3, 10, tzinfo=UTC),
            occurred_precision=TemporalPrecision.DAY,
        ),
        semantic_id: _approved_memory(
            id=semantic_id,
            normalized_text="静岡へ旅行した",
            occurred_at=datetime(2025, 5, 4, tzinfo=UTC),
            occurred_precision=TemporalPrecision.DAY,
        ),
        period_id: _approved_memory(
            id=period_id,
            normalized_text="長野ではなく静岡へ旅行した",
            occurred_at=datetime(2025, 3, 20, tzinfo=UTC),
            occurred_precision=TemporalPrecision.DAY,
        ),
    }
    scanner, classifier, repository = _dependencies(memories=memories)
    scanner.scan.side_effect = None
    scanner.scan.return_value = ScanSuccess(())
    repository.search_by_occurred_range.return_value = [
        memories[both_id],
        memories[period_id],
    ]
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(
            return_value=[
                _candidate(semantic_id, 0.01),
                _candidate(both_id, 0.02),
            ]
        ),
    )

    outcome = rag_service.retrieve_prompt_memories(
        "miori",
        "昨年3月の静岡旅行を教えて",
        resolved_memory_policy(),
        scanner=scanner,
        classifier=classifier,
        approved_repository=repository,
        chroma_path=_CHROMA_PATH,
        now=datetime(2026, 3, 25, tzinfo=UTC),
        timezone="Asia/Tokyo",
    )

    assert [memory.memory_id for memory in outcome.memories] == [
        str(both_id),
        str(semantic_id),
        str(period_id),
    ]
    assert [memory.match_kind for memory in outcome.memories] == [
        RetrievalMatchKind.BOTH,
        RetrievalMatchKind.SEMANTIC,
        RetrievalMatchKind.PERIOD,
    ]
    assert outcome.memories[1].occurred_at == "2025-05-04T09:00:00+09:00"
    assert outcome.memories[1].occurred_precision is TemporalPrecision.DAY
    repository.touch.assert_not_called()


def test_temporal_retrieval_reports_no_match_only_after_both_paths_succeed(
    monkeypatch,
) -> None:
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies(memories={})
    scanner.scan.side_effect = None
    scanner.scan.return_value = ScanSuccess(())
    repository.search_by_occurred_range.return_value = []
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(rag_service, "query_memories", MagicMock(return_value=[]))

    outcome = rag_service.retrieve_prompt_memories(
        "miori",
        "昨年3月の旅行を教えて",
        resolved_memory_policy(),
        scanner=scanner,
        classifier=classifier,
        approved_repository=repository,
        chroma_path=_CHROMA_PATH,
        now=datetime(2026, 3, 25, tzinfo=UTC),
        timezone="Asia/Tokyo",
    )

    assert outcome.memories == ()
    assert outcome.no_match is True


def test_temporal_retrieval_failure_is_not_reported_as_no_match(monkeypatch) -> None:
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies(memories={})
    scanner.scan.side_effect = None
    scanner.scan.return_value = ScanSuccess(())
    repository.search_by_occurred_range.return_value = []
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(side_effect=RuntimeError("synthetic Chroma failure")),
    )

    outcome = rag_service.retrieve_prompt_memories(
        "miori",
        "昨年3月の旅行を教えて",
        resolved_memory_policy(),
        scanner=scanner,
        classifier=classifier,
        approved_repository=repository,
        chroma_path=_CHROMA_PATH,
        now=datetime(2026, 3, 25, tzinfo=UTC),
        timezone="Asia/Tokyo",
    )

    assert outcome.memories == ()
    assert outcome.no_match is False
    repository.search_by_occurred_range.assert_called_once()


def test_unparseable_temporal_text_keeps_chroma_primary_without_range_search(
    monkeypatch,
) -> None:
    from app.memory import rag_service

    scanner, classifier, repository = _dependencies()
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.1]))
    monkeypatch.setattr(
        rag_service,
        "query_memories",
        MagicMock(return_value=[_candidate()]),
    )

    memories = _retrieve(
        rag_service,
        scanner=scanner,
        classifier=classifier,
        repository=repository,
        user_message="いつか行った旅行を教えて",
    )

    assert [memory.memory_id for memory in memories] == [str(_MEMORY_ID)]
    repository.search_by_occurred_range.assert_not_called()


@pytest.mark.parametrize(
    ("query_text", "allowed_precisions"),
    [
        (
            "2025年3月の出来事",
            {
                TemporalPrecision.MONTH,
                TemporalPrecision.DAY,
                TemporalPrecision.HOUR,
                TemporalPrecision.MINUTE,
                TemporalPrecision.SECOND,
            },
        ),
        (
            "2025-03-01から2025-03-31の出来事",
            {
                TemporalPrecision.DAY,
                TemporalPrecision.HOUR,
                TemporalPrecision.MINUTE,
                TemporalPrecision.SECOND,
            },
        ),
    ],
)
def test_period_filter_excludes_memories_with_too_coarse_or_unknown_precision(
    query_text: str,
    allowed_precisions: set[TemporalPrecision],
) -> None:
    from app.memory import rag_service
    from app.memory.temporal_query import parse_temporal_query

    query = parse_temporal_query(
        query_text,
        now=datetime(2026, 8, 20, tzinfo=UTC),
        timezone="Asia/Tokyo",
    )
    assert query is not None
    memories = [
        _approved_memory(occurred_precision=precision)
        for precision in (*TemporalPrecision, None)
    ]

    result = rag_service._filter_period_memories(memories, query)

    assert {memory.occurred_precision for memory in result} == allowed_precisions


def test_occurrence_formatting_degrades_unknown_timezone_to_unknown_date() -> None:
    from app.memory import rag_service

    memory = _approved_memory(occurred_timezone="Asia/Nowhere")

    assert rag_service._format_occurred_at(memory) is None
