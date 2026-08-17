from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import pytest


SENSITIVE_TEXT = "合成テスト用: 最近ずっと眠れず、治療を受けている"
RAW_OUTPUT_SENTINEL = "raw-model-output-must-not-leak"
PARSER_SENTINEL = "parser-frame-must-not-leak"


@dataclass
class FakeClassifierClient:
    outcomes: list[str | BaseException]
    calls: list[tuple[tuple[dict[str, str], ...], float]] = field(
        default_factory=list
    )

    def chat(
        self,
        messages: tuple[dict[str, str], ...],
        *,
        timeout_seconds: float,
    ) -> str:
        self.calls.append((messages, timeout_seconds))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(
    *,
    classification: str = "SENSITIVE",
    subject_scope: str = "SELF",
    category: str = "HEALTH",
    reason_code: str = "SENSITIVE_CONTENT",
) -> str:
    return json.dumps(
        {
            "classification": classification,
            "subject_scope": subject_scope,
            "category": category,
            "reason_code": reason_code,
        }
    )


def _classifier(client: FakeClassifierClient):
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.semantic.classifier import OllamaSemanticPrivacyClassifier

    return OllamaSemanticPrivacyClassifier(
        client=client,
        privacy_policy=resolved_memory_policy().privacy,
        model_id="gemma4:e4b",
        model_digest="sha256:" + "b" * 64,
    )


def test_classifier_builds_versioned_few_shot_prompt_and_keeps_target_separate() -> None:
    from app.privacy.semantic.classifier import SEMANTIC_PROMPT_VERSION
    from app.privacy.semantic.contracts import QUERY_GATE

    client = FakeClassifierClient([_response()])

    _classifier(client).classify(SENSITIVE_TEXT, QUERY_GATE)

    messages, _timeout = client.calls[0]
    assert SEMANTIC_PROMPT_VERSION in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": SENSITIVE_TEXT}
    assert [message["role"] for message in messages[:-1]].count("assistant") >= 2
    assert [message["role"] for message in messages[:-1]].count("user") >= 2


def test_classifier_prompt_lists_contract_enum_values_explicitly() -> None:
    from app.privacy.semantic.contracts import (
        QUERY_GATE,
        SemanticAssessmentReasonCode,
        SemanticClassification,
        SemanticPrivacyCategory,
        SubjectScope,
    )

    client = FakeClassifierClient([_response()])

    _classifier(client).classify(SENSITIVE_TEXT, QUERY_GATE)

    system_prompt = client.calls[0][0][0]["content"]
    for enum_type in (
        SemanticClassification,
        SubjectScope,
        SemanticPrivacyCategory,
        SemanticAssessmentReasonCode,
    ):
        assert all(item.value in system_prompt for item in enum_type)


def test_classifier_parses_valid_response_and_propagates_provenance() -> None:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.semantic.classifier import (
        SEMANTIC_CLASSIFIER_VERSION,
        SEMANTIC_PROMPT_VERSION,
    )
    from app.privacy.semantic.contracts import (
        QUERY_GATE,
        SemanticAssessmentReasonCode,
        SemanticClassification,
        SemanticPrivacyCategory,
        SubjectScope,
    )

    assessment = _classifier(FakeClassifierClient([_response()])).classify(
        SENSITIVE_TEXT, QUERY_GATE
    )

    assert assessment.classification is SemanticClassification.SENSITIVE
    assert assessment.subject_scope is SubjectScope.SELF
    assert assessment.category is SemanticPrivacyCategory.HEALTH
    assert assessment.reason_code is SemanticAssessmentReasonCode.SENSITIVE_CONTENT
    assert assessment.classifier_version == SEMANTIC_CLASSIFIER_VERSION
    assert assessment.model_id == "gemma4:e4b"
    assert assessment.model_digest == "sha256:" + "b" * 64
    assert assessment.prompt_version == SEMANTIC_PROMPT_VERSION
    assert assessment.policy_version == resolved_memory_policy().policy_version


def test_classifier_parses_not_sensitive_general_response() -> None:
    from app.privacy.semantic.contracts import QUERY_GATE

    assessment = _classifier(
        FakeClassifierClient(
            [
                _response(
                    classification="NOT_SENSITIVE",
                    subject_scope="GENERAL",
                    category="NONE",
                    reason_code="NO_SENSITIVE_CONTENT",
                )
            ]
        )
    ).classify("睡眠とは何ですか", QUERY_GATE)

    assert assessment.classification.value == "NOT_SENSITIVE"
    assert assessment.subject_scope.value == "GENERAL"
    assert assessment.category.value == "NONE"
    assert assessment.reason_code.value == "NO_SENSITIVE_CONTENT"


@pytest.mark.parametrize(
    "body",
    [
        {"subject_scope": "SELF", "category": "HEALTH", "reason_code": "SENSITIVE_CONTENT"},
        {
            "classification": "SENSITIVE",
            "subject_scope": "SELF",
            "category": "HEALTH",
            "reason_code": "SENSITIVE_CONTENT",
            "unexpected": "field",
        },
        {
            "classification": "SENSITIVE",
            "subject_scope": "SOMEONE",
            "category": "HEALTH",
            "reason_code": "SENSITIVE_CONTENT",
        },
    ],
)
def test_classifier_rejects_contract_external_response_shapes(
    body: dict[str, str],
) -> None:
    from app.privacy.semantic.contracts import QUERY_GATE

    assessment = _classifier(
        FakeClassifierClient([json.dumps(body)])
    ).classify(SENSITIVE_TEXT, QUERY_GATE)

    assert assessment.classification.value == "ABSTAIN"
    assert assessment.subject_scope.value == "UNKNOWN"
    assert assessment.category.value == "UNKNOWN"
    assert assessment.reason_code.value == "INVALID_OUTPUT"


@pytest.mark.parametrize(
    ("outcome", "expected_reason"),
    [
        (TimeoutError("timeout"), "TIMEOUT"),
        ("not-json", "INVALID_OUTPUT"),
        (_response(category="FUTURE_PRIVATE_KIND"), "UNKNOWN_CATEGORY"),
        (
            _response(
                classification="ABSTAIN",
                subject_scope="UNKNOWN",
                category="UNKNOWN",
                reason_code="UNKNOWN_LANGUAGE",
            ),
            "UNKNOWN_LANGUAGE",
        ),
    ],
)
def test_classifier_failures_are_fail_closed(
    outcome: str | BaseException,
    expected_reason: str,
) -> None:
    from app.privacy.semantic.contracts import (
        QUERY_GATE,
        SemanticClassification,
        SemanticPrivacyCategory,
        SubjectScope,
    )

    assessment = _classifier(FakeClassifierClient([outcome])).classify(
        SENSITIVE_TEXT, QUERY_GATE
    )

    assert assessment.classification is SemanticClassification.ABSTAIN
    assert assessment.subject_scope is SubjectScope.UNKNOWN
    assert assessment.category is SemanticPrivacyCategory.UNKNOWN
    assert assessment.reason_code.value == expected_reason


@pytest.mark.parametrize(
    "outcome",
    [
        "not-json",
        _response(category="FUTURE_PRIVATE_KIND"),
        _response(
            classification="ABSTAIN",
            subject_scope="UNKNOWN",
            category="UNKNOWN",
            reason_code="UNKNOWN_LANGUAGE",
        ),
    ],
)
def test_admission_does_not_retry_deterministic_abstentions(outcome: str) -> None:
    from app.privacy.semantic.contracts import ADMISSION

    client = FakeClassifierClient([outcome])

    _classifier(client).classify(SENSITIVE_TEXT, ADMISSION)

    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("classification", "subject_scope", "category", "reason_code"),
    [
        ("NOT_SENSITIVE", "SELF", "HEALTH", "SENSITIVE_CONTENT"),
        ("SENSITIVE", "SELF", "HEALTH", "NO_SENSITIVE_CONTENT"),
    ],
)
def test_classifier_rejects_internally_inconsistent_enum_combinations(
    classification: str,
    subject_scope: str,
    category: str,
    reason_code: str,
) -> None:
    from app.privacy.semantic.contracts import QUERY_GATE

    assessment = _classifier(
        FakeClassifierClient(
            [
                _response(
                    classification=classification,
                    subject_scope=subject_scope,
                    category=category,
                    reason_code=reason_code,
                )
            ]
        )
    ).classify(SENSITIVE_TEXT, QUERY_GATE)

    assert assessment.classification.value == "ABSTAIN"
    assert assessment.reason_code.value == "INVALID_OUTPUT"


def test_model_not_loaded_is_fail_closed() -> None:
    from app.privacy.semantic.contracts import QUERY_GATE
    from app.privacy.semantic.ollama_classifier_client import (
        OllamaModelNotLoadedError,
    )

    assessment = _classifier(
        FakeClassifierClient([OllamaModelNotLoadedError()])
    ).classify(SENSITIVE_TEXT, QUERY_GATE)

    assert assessment.classification.value == "ABSTAIN"
    assert assessment.subject_scope.value == "UNKNOWN"
    assert assessment.category.value == "UNKNOWN"
    assert assessment.reason_code.value == "MODEL_NOT_LOADED"


def test_query_gate_does_not_retry_and_uses_its_timeout() -> None:
    from app.privacy.semantic.contracts import QUERY_GATE

    client = FakeClassifierClient([TimeoutError("timeout")])

    _classifier(client).classify(SENSITIVE_TEXT, QUERY_GATE)

    assert [timeout for _messages, timeout in client.calls] == [2.0]


def test_admission_retries_only_up_to_its_bound_and_uses_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.privacy.semantic.contracts import ADMISSION, SemanticClassification

    sleeps: list[float] = []
    monkeypatch.setattr(
        "app.privacy.semantic.classifier.time.sleep",
        sleeps.append,
    )
    client = FakeClassifierClient(
        [TimeoutError("one"), TimeoutError("two"), _response()]
    )

    assessment = _classifier(client).classify(SENSITIVE_TEXT, ADMISSION)

    assert assessment.classification is SemanticClassification.SENSITIVE
    assert [timeout for _messages, timeout in client.calls] == [15.0, 15.0, 15.0]
    assert sleeps == [1.0, 2.0]


def test_final_retry_outcome_alone_controls_fail_closed_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.privacy.semantic.contracts import ADMISSION

    monkeypatch.setattr(
        "app.privacy.semantic.classifier.time.sleep",
        lambda _seconds: None,
    )
    client = FakeClassifierClient(
        [
            TimeoutError("first"),
            TimeoutError("second"),
            _response(category="FUTURE_PRIVATE_KIND"),
        ]
    )

    assessment = _classifier(client).classify(SENSITIVE_TEXT, ADMISSION)

    assert assessment.reason_code.value == "UNKNOWN_CATEGORY"
    assert assessment.classification.value == "ABSTAIN"
    assert assessment.subject_scope.value == "UNKNOWN"
    assert assessment.category.value == "UNKNOWN"


def test_sensitive_material_is_absent_from_logs_and_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.privacy.semantic.contracts import QUERY_GATE

    caplog.set_level(logging.DEBUG)
    secret_hash = "8f6a28d55e4f5f2e"
    client = FakeClassifierClient(
        [RuntimeError(f"{SENSITIVE_TEXT} {secret_hash} {RAW_OUTPUT_SENTINEL} {PARSER_SENTINEL}")]
    )

    assessment = _classifier(client).classify(SENSITIVE_TEXT, QUERY_GATE)

    assert assessment.reason_code.value == "MODEL_UNAVAILABLE"
    observed = caplog.text + repr(assessment)
    for forbidden in (
        SENSITIVE_TEXT,
        secret_hash,
        RAW_OUTPUT_SENTINEL,
        PARSER_SENTINEL,
    ):
        assert forbidden.casefold() not in observed.casefold()

    assert "classification=ABSTAIN" in caplog.text
    assert "reason_code=MODEL_UNAVAILABLE" in caplog.text
    assert "profile=QUERY_GATE" in caplog.text
    assert "attempt_count=1" in caplog.text
