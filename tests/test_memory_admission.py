import asyncio
from dataclasses import FrozenInstanceError, replace
from itertools import combinations
from typing import Any, get_args

import pytest

from digital_souls_core.memory_admission import (
    AdmissionPolicy,
    AdmissionReason,
    AdmissionResult,
    AdmissionState,
    Candidate,
    ClassifierProvenance,
    SourceState,
    admit,
    evaluate,
)
from digital_souls_core.memory_records import RecordKind
from digital_souls_core.privacy_classifier import (
    CLASSIFIER_VERSION,
    PROMPT_VERSION,
    Assessment,
    AssessmentFailure,
    AssessmentResult,
    LocalClassifier,
)
from digital_souls_core.privacy_scan import POLICY_VERSION, SCANNER_VERSION, Scan, scan

from .privacy_support import assessment, local_profile
from .support import FakeProvider

pytestmark = pytest.mark.ut
POLICY = AdmissionPolicy(allowed_kinds=frozenset(RecordKind), version=POLICY_VERSION)
SOURCE = SourceState(excluded=False, private=False, refusal_pending=False, history_saved=True)
CANDIDATE = Candidate(kind=RecordKind.EPISODE, memory_worthy=True)
PROVENANCE = ClassifierProvenance(
    classifier_version=CLASSIFIER_VERSION,
    model_id="synthetic-model",
    model_digest="synthetic-digest",
    prompt_version=PROMPT_VERSION,
)
EMPTY_SCAN = Scan()
SAFE = AssessmentResult(assessment=Assessment.model_validate_json(assessment()))
SENSITIVE = AssessmentResult(
    assessment=Assessment.model_validate_json(
        assessment(classification="SENSITIVE", subject="SELF", category="HEALTH")
    )
)
ABSTAIN = AssessmentResult(
    assessment=Assessment.model_validate_json(
        assessment(classification="ABSTAIN", subject="UNKNOWN", category="UNKNOWN")
    )
)


def decide(
    *,
    candidate: Candidate = CANDIDATE,
    checked: Scan = EMPTY_SCAN,
    classified: AssessmentResult | None = SAFE,
    policy: AdmissionPolicy = POLICY,
    source: SourceState = SOURCE,
    provenance: ClassifierProvenance | None = PROVENANCE,
) -> AdmissionResult:
    return evaluate(
        candidate,
        checked,
        classified,
        policy=policy,
        source=source,
        classifier_provenance=provenance,
    )


@pytest.mark.parametrize(
    "classified,state,reason",
    [
        (SAFE, AdmissionState.ALLOW_STRUCTURED, AdmissionReason.ALLOWED),
        (SENSITIVE, AdmissionState.DENY_SENSITIVE, AdmissionReason.ASSESSMENT_SENSITIVE),
        (ABSTAIN, AdmissionState.ABSTAIN_UNKNOWN, AdmissionReason.ASSESSMENT_ABSTAIN),
        (None, AdmissionState.ABSTAIN_UNKNOWN, AdmissionReason.UNCLASSIFIED),
    ],
)
def test_assessment_states(
    classified: AssessmentResult | None, state: AdmissionState, reason: AdmissionReason
) -> None:
    result = decide(classified=classified)
    assert result.state is state and reason in result.reasons
    assert result.can_store is (state is AdmissionState.ALLOW_STRUCTURED)
    assert result == decide(classified=classified)


@pytest.mark.parametrize("failure", get_args(AssessmentFailure.__value__))
def test_every_classifier_failure_abstains(failure: Any) -> None:
    result = decide(classified=AssessmentResult(failure=failure))
    assert result.state is AdmissionState.ABSTAIN_UNKNOWN
    assert result.assessment_failure == failure and not result.can_store


@pytest.mark.parametrize(
    "category",
    [
        "HEALTH",
        "MENTAL_STATE",
        "SELF_HARM",
        "ABUSE",
        "FINANCIAL",
        "THIRD_PARTY_PRIVATE",
        "OTHER_SENSITIVE",
    ],
)
def test_all_sensitive_categories_deny(category: str) -> None:
    classified = AssessmentResult(
        assessment=Assessment.model_validate_json(
            assessment(classification="SENSITIVE", subject="THIRD_PARTY", category=category)
        )
    )
    assert decide(classified=classified).state is AdmissionState.DENY_SENSITIVE


@pytest.mark.parametrize("field", ["excluded", "private", "refusal_pending"])
def test_user_source_controls_deny(field: str) -> None:
    result = decide(source=replace(SOURCE, **{field: True}))
    assert result.state is AdmissionState.DENY_USER_REQUEST and not result.can_store


@pytest.mark.parametrize("field", ["no_memory", "no_history"])
def test_scanned_refusal_deny(field: str) -> None:
    assert (
        decide(checked=Scan(no_memory=field == "no_memory", no_history=field == "no_history")).state
        is AdmissionState.DENY_USER_REQUEST
    )


@pytest.mark.parametrize("field", ["excluded", "private", "refusal_pending", "history_saved"])
def test_unknown_source_abstains(field: str) -> None:
    assert decide(source=replace(SOURCE, **{field: None})).state is AdmissionState.ABSTAIN_UNKNOWN


def test_unsaved_history_and_unknown_value_abstain() -> None:
    assert (
        decide(source=replace(SOURCE, history_saved=False)).state is AdmissionState.ABSTAIN_UNKNOWN
    )
    assert (
        decide(candidate=replace(CANDIDATE, memory_worthy=None)).state
        is AdmissionState.ABSTAIN_UNKNOWN
    )
    assert decide(checked=Scan(failed=True)).state is AdmissionState.ABSTAIN_UNKNOWN
    assert (
        decide(policy=replace(POLICY, version="unsupported")).state
        is AdmissionState.ABSTAIN_UNKNOWN
    )
    assert decide(provenance=None).state is AdmissionState.ABSTAIN_UNKNOWN


@pytest.mark.parametrize("kind", list(RecordKind))
def test_trusted_allowed_kinds_and_explicit_value(kind: RecordKind) -> None:
    candidate = replace(CANDIDATE, kind=kind)
    assert decide(candidate=candidate).can_store
    assert (
        decide(candidate=candidate, policy=replace(POLICY, allowed_kinds=frozenset())).state
        is AdmissionState.NOT_MEMORY_WORTHY
    )
    assert (
        decide(candidate=replace(candidate, memory_worthy=False)).state
        is AdmissionState.NOT_MEMORY_WORTHY
    )


# Pairwise combinations exercise every strict priority, with multiple reasons retained.
PRIORITIES = list(AdmissionState)


@pytest.mark.parametrize("higher,lower", list(combinations(PRIORITIES, 2)))
def test_priority_for_every_pair(higher: AdmissionState, lower: AdmissionState) -> None:
    states = {higher, lower}
    result = decide(
        checked=Scan(secret=AdmissionState.DENY_SENSITIVE in states),
        source=replace(SOURCE, excluded=AdmissionState.DENY_USER_REQUEST in states),
        classified=None if AdmissionState.ABSTAIN_UNKNOWN in states else SAFE,
        candidate=replace(CANDIDATE, memory_worthy=AdmissionState.NOT_MEMORY_WORTHY not in states),
    )
    assert result.state is higher and not result.can_store


def test_sensitive_overrides_all_other_reasons() -> None:
    result = decide(
        checked=Scan(secret=True, no_memory=True, failed=True),
        source=replace(SOURCE, private=True, history_saved=False),
        classified=None,
        candidate=replace(CANDIDATE, memory_worthy=False),
    )
    assert result.state is AdmissionState.DENY_SENSITIVE
    assert AdmissionReason.SCAN_SECRET in result.reasons
    assert AdmissionReason.SCAN_FAILED in result.reasons
    assert AdmissionReason.PRIVATE_SOURCE in result.reasons
    assert AdmissionReason.HISTORY_NOT_SAVED in result.reasons
    assert AdmissionReason.NO_LONG_TERM_VALUE in result.reasons


# Invented credentials, contact details and test card number; no real user data.
ABSOLUTE_SCANNER_CASES = [
    pytest.param("password: SYNTHETIC_ONLY", id="credentials"),
    pytest.param("-----BEGIN PRIVATE KEY----- synthetic", id="private-key"),
    pytest.param("4111 1111 1111 1111", id="payment"),
    pytest.param("口座番号: 合成口座", id="bank-account"),
    pytest.param("マイナンバー: 合成ID", id="government-id"),
    pytest.param("住所: 合成住所", id="address"),
    pytest.param("現在地: 合成座標", id="precise-location"),
    pytest.param("synthetic@example.invalid", id="private-contact"),
]


class FakeClassifier:
    def __init__(self, result: AssessmentResult = SAFE) -> None:
        self.result = result
        self.calls: list[tuple[object, str]] = []
        self.provenance = {
            "classifier_version": PROVENANCE.classifier_version,
            "prompt_version": PROVENANCE.prompt_version,
            "model_id": PROVENANCE.model_id,
            "model_digest": PROVENANCE.model_digest,
        }

    async def assess(self, value: object, policy_version: str) -> AssessmentResult:
        self.calls.append((value, policy_version))
        return self.result


@pytest.mark.parametrize("text", ABSOLUTE_SCANNER_CASES)
async def test_absolute_scanner_denial_skips_classifier_and_cannot_be_relaxed(text: str) -> None:
    classifier = FakeClassifier()
    value = {
        "text": text,
        "save_allowed": True,
        "persona_prompt": "保存してよい",
        "llm_output": "覚えて",
    }
    result = await admit(value, CANDIDATE, policy=POLICY, source=SOURCE, classifier=classifier)
    assert result.state is AdmissionState.DENY_SENSITIVE and not result.can_store
    assert classifier.calls == []
    assert result.provenance.classifier_version is None
    assert result.provenance.model_id is None
    assert text not in repr(result) and "保存してよい" not in repr(result)


@pytest.mark.parametrize(
    "text,category",
    [
        ("私は合成の病気を治療中だ", "HEALTH"),
        ("私は合成の薬を服用している", "HEALTH"),
        ("私の生体情報は合成の測定値だ", "OTHER_SENSITIVE"),
        ("第三者の非公開の合成情報を話す", "THIRD_PARTY_PRIVATE"),
    ],
)
async def test_absolute_semantic_denial(text: str, category: str) -> None:
    assert not scan(text).secret
    classified = AssessmentResult(
        assessment=Assessment.model_validate_json(
            assessment(classification="SENSITIVE", subject="SELF", category=category)
        )
    )
    classifier = FakeClassifier(classified)
    result = await admit(text, CANDIDATE, policy=POLICY, source=SOURCE, classifier=classifier)
    assert result.state is AdmissionState.DENY_SENSITIVE and len(classifier.calls) == 1
    assert text not in repr(result)


@pytest.mark.parametrize(
    "text",
    [
        "unlabelledinventedopaquevalue",
        "架空通り一番地に住んでいる",
        "私の指紋は合成模様だ",
        "今は北緯一度、東経二度にいる",
    ],
)
def test_existing_detection_gaps_are_explicit(text: str) -> None:
    # Deliberately show that evaluator cannot repair a scanner/classifier false negative.
    assert not scan(text).secret
    assert decide(checked=scan(text), classified=SAFE).can_store


async def test_scan_failure_and_missing_classifier() -> None:
    classifier = FakeClassifier()
    result = await admit(object(), CANDIDATE, policy=POLICY, source=SOURCE, classifier=classifier)
    assert result.state is AdmissionState.ABSTAIN_UNKNOWN and classifier.calls == []
    result = await admit("合成の好み", CANDIDATE, policy=POLICY, source=SOURCE, classifier=None)
    assert result.state is AdmissionState.ABSTAIN_UNKNOWN


@pytest.mark.parametrize("condition", ["allowed", "private", "unworthy", "unpermitted", "refusal"])
async def test_no_semantic_prefilter_and_strict_priority(condition: str) -> None:
    classifier = FakeClassifier(SENSITIVE)
    result = await admit(
        "覚えないで" if condition == "refusal" else "合成の好み",
        replace(CANDIDATE, memory_worthy=condition != "unworthy"),
        policy=replace(
            POLICY,
            allowed_kinds=frozenset() if condition == "unpermitted" else POLICY.allowed_kinds,
        ),
        source=replace(SOURCE, private=condition == "private"),
        classifier=classifier,
    )
    assert len(classifier.calls) == 1 and result.state is AdmissionState.DENY_SENSITIVE


@pytest.mark.parametrize("fault", ["exception", "timeout", "invalid", "cancel"])
async def test_adapter_failure_and_cancellation(fault: str) -> None:
    class Broken(FakeClassifier):
        async def assess(self, value: object, policy_version: str) -> AssessmentResult:
            if fault == "cancel":
                raise asyncio.CancelledError
            if fault == "invalid":
                return "PRIVATE_SYNTHETIC_MARKER"  # type: ignore[return-value]
            if fault == "timeout":
                raise TimeoutError("PRIVATE_SYNTHETIC_MARKER")
            raise RuntimeError("PRIVATE_SYNTHETIC_MARKER")

    if fault == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await admit("合成入力", CANDIDATE, policy=POLICY, source=SOURCE, classifier=Broken())
    else:
        result = await admit(
            "合成入力", CANDIDATE, policy=POLICY, source=SOURCE, classifier=Broken()
        )
        assert (
            result.state is AdmissionState.ABSTAIN_UNKNOWN
            and "PRIVATE_SYNTHETIC_MARKER" not in repr(result)
        )


@pytest.mark.parametrize(
    "fault",
    [
        "invalid_json",
        "invalid_assessment",
        "invalid_response",
        "provider_exception",
        "timeout",
        "safe",
    ],
)
async def test_local_classifier_integration_with_fake_provider(fault: str) -> None:
    provider = FakeProvider()
    provider.response["choices"][0]["message"]["content"] = assessment()
    if fault == "invalid_json":
        provider.response["choices"][0]["message"]["content"] = "not-json"
    elif fault == "invalid_assessment":
        provider.response["choices"][0]["message"]["content"] = assessment(extra="synthetic")
    elif fault == "invalid_response":
        provider.response["choices"][0]["finish_reason"] = "length"
    elif fault == "provider_exception":
        provider.error = RuntimeError("PRIVATE_SYNTHETIC_MARKER")
    elif fault == "timeout":
        provider.error = TimeoutError("PRIVATE_SYNTHETIC_MARKER")
    profile = local_profile()
    classifier = LocalClassifier(provider, profile, model_digest="synthetic-local-digest")
    result = await admit(
        "合成の好み", CANDIDATE, policy=POLICY, source=SOURCE, classifier=classifier
    )
    assert len(provider.calls) == 1
    assert result.can_store is (fault == "safe")
    if fault != "safe":
        assert result.state is AdmissionState.ABSTAIN_UNKNOWN and result.assessment_failure == fault
    assert result.provenance.model_id == profile.model
    assert result.provenance.model_digest == "synthetic-local-digest"
    assert result.provenance.classifier_version == CLASSIFIER_VERSION
    assert result.provenance.prompt_version == PROMPT_VERSION
    assert result.provenance.policy_version == POLICY_VERSION
    assert result.provenance.scanner_version == SCANNER_VERSION
    assert "合成の好み" not in repr(result) and "PRIVATE_SYNTHETIC_MARKER" not in repr(result)


def test_provenance_versions_are_independent() -> None:
    provenance = replace(
        PROVENANCE, classifier_version="classifier-next", prompt_version="prompt-next"
    )
    result = decide(checked=Scan(version="scanner-next"), provenance=provenance)
    assert result.provenance.classifier_version == "classifier-next"
    assert result.provenance.prompt_version == "prompt-next"
    assert result.provenance.scanner_version == "scanner-next"
    assert result.provenance.policy_version == POLICY_VERSION
    assert result.provenance.model_id == PROVENANCE.model_id
    assert result.provenance.model_digest == PROVENANCE.model_digest


@pytest.mark.parametrize(
    "factory,fields",
    [
        (Candidate, {"kind": "PRIVATE_SYNTHETIC_MARKER", "memory_worthy": True}),
        (Candidate, {"kind": RecordKind.FACT, "memory_worthy": 1}),
        (
            SourceState,
            {
                "excluded": "PRIVATE_SYNTHETIC_MARKER",
                "private": False,
                "refusal_pending": False,
                "history_saved": True,
            },
        ),
        (AdmissionPolicy, {"allowed_kinds": {RecordKind.FACT}, "version": POLICY_VERSION}),
        (
            AdmissionPolicy,
            {"allowed_kinds": frozenset({"PRIVATE_SYNTHETIC_MARKER"}), "version": POLICY_VERSION},
        ),
        (
            ClassifierProvenance,
            {"classifier_version": 1, "prompt_version": "p", "model_id": "m", "model_digest": "d"},
        ),
    ],
)
def test_strict_types_without_content_in_error(factory: Any, fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError) as raised:
        factory(**fields)
    assert "PRIVATE_SYNTHETIC_MARKER" not in str(raised.value)


def test_value_types_are_immutable() -> None:
    for value, field in [
        (CANDIDATE, "memory_worthy"),
        (SOURCE, "private"),
        (POLICY, "version"),
        (PROVENANCE, "model_id"),
        (decide(), "state"),
        (decide().provenance, "policy_version"),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, None)


async def test_real_timeout_closes_classifier_coroutine(monkeypatch: pytest.MonkeyPatch) -> None:
    import digital_souls_core.memory_admission as admission

    closed = asyncio.Event()

    class Waiting(FakeClassifier):
        async def assess(self, value: object, policy_version: str) -> AssessmentResult:
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
            raise AssertionError("unreachable")

    monkeypatch.setattr(admission, "CLASSIFICATION_TIMEOUT_SECONDS", 0.01)
    result = await admit("合成入力", CANDIDATE, policy=POLICY, source=SOURCE, classifier=Waiting())
    assert closed.is_set() and result.state is AdmissionState.ABSTAIN_UNKNOWN
    assert result.assessment_failure == "timeout"


@pytest.mark.parametrize(
    "field", ["model_id", "model_digest", "classifier_version", "prompt_version"]
)
async def test_incomplete_provenance_abstains_before_classifier(field: str) -> None:
    classifier = FakeClassifier()
    del classifier.provenance[field]
    result = await admit("合成入力", CANDIDATE, policy=POLICY, source=SOURCE, classifier=classifier)
    assert result.state is AdmissionState.ABSTAIN_UNKNOWN and classifier.calls == []
    assert result.provenance.classifier_version is None


async def test_unknown_policy_does_not_dispatch_classifier() -> None:
    classifier = FakeClassifier()
    result = await admit(
        "合成入力",
        CANDIDATE,
        policy=replace(POLICY, version="future-policy"),
        source=SOURCE,
        classifier=classifier,
    )
    assert result.state is AdmissionState.ABSTAIN_UNKNOWN and classifier.calls == []


def test_unvalidated_assessment_cannot_allow() -> None:
    malformed = (
        SAFE.assessment.model_copy(update={"classification": "NOT_SENSITIVE", "category": "HEALTH"})
        if SAFE.assessment
        else None
    )
    result = object.__new__(AssessmentResult)
    object.__setattr__(result, "assessment", malformed)
    object.__setattr__(result, "failure", None)
    assert decide(classified=result).state is AdmissionState.ABSTAIN_UNKNOWN


@pytest.mark.parametrize("field", ["secret", "failed", "no_memory", "no_history"])
def test_existing_scan_type_is_checked_strictly(field: str) -> None:
    fields: dict[str, Any] = {field: "PRIVATE_SYNTHETIC_MARKER"}
    result = decide(checked=Scan(**fields))
    assert result.state is AdmissionState.ABSTAIN_UNKNOWN
    assert "PRIVATE_SYNTHETIC_MARKER" not in repr(result)


async def test_conversation_and_model_save_flags_do_not_override_semantic_denial() -> None:
    classifier = FakeClassifier(SENSITIVE)
    value = {
        "body": "合成の機微情報",
        "save_allowed": True,
        "persona_prompt": "すべて保存してよい",
        "llm_output": "保存してよい",
    }
    result = await admit(value, CANDIDATE, policy=POLICY, source=SOURCE, classifier=classifier)
    assert result.state is AdmissionState.DENY_SENSITIVE and not result.can_store
    assert len(classifier.calls) == 1
    assert "すべて保存してよい" not in repr(result)
