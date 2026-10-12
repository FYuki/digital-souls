"""Content-free memory admission; evaluation has no I/O or permission side effects."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .memory_records import RecordKind
from .privacy_classifier import AssessmentFailure, AssessmentResult
from .privacy_scan import POLICY_VERSION, Scan, scan

CLASSIFICATION_TIMEOUT_SECONDS = 15


class AdmissionState(Enum):
    DENY_SENSITIVE = "DENY_SENSITIVE"
    DENY_USER_REQUEST = "DENY_USER_REQUEST"
    ABSTAIN_UNKNOWN = "ABSTAIN_UNKNOWN"
    NOT_MEMORY_WORTHY = "NOT_MEMORY_WORTHY"
    ALLOW_STRUCTURED = "ALLOW_STRUCTURED"


class AdmissionReason(Enum):
    SCAN_SECRET = "scan_secret"
    ASSESSMENT_SENSITIVE = "assessment_sensitive"
    EXCLUDED_SOURCE = "excluded_source"
    PRIVATE_SOURCE = "private_source"
    REFUSAL_PENDING = "refusal_pending"
    SCANNED_REFUSAL = "scanned_refusal"
    SCAN_FAILED = "scan_failed"
    UNKNOWN_SOURCE = "unknown_source"
    HISTORY_NOT_SAVED = "history_not_saved"
    POLICY_UNSUPPORTED = "policy_unsupported"
    UNCLASSIFIED = "unclassified"
    CLASSIFIER_FAILURE = "classifier_failure"
    ASSESSMENT_ABSTAIN = "assessment_abstain"
    PROVENANCE_MISSING = "provenance_missing"
    VALUE_UNKNOWN = "value_unknown"
    KIND_NOT_ALLOWED = "kind_not_allowed"
    NO_LONG_TERM_VALUE = "no_long_term_value"
    ALLOWED = "allowed"


def _text(value: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError("nonempty deployment identifier required")


def _optional_bool(value: bool | None) -> None:
    if value is not None and type(value) is not bool:
        raise ValueError("explicit boolean or unknown required")


@dataclass(frozen=True, kw_only=True)
class Candidate:
    """Only type/value signals; never candidate body, consent or destination."""

    kind: RecordKind
    memory_worthy: bool | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RecordKind):
            raise ValueError("RecordKind required")
        _optional_bool(self.memory_worthy)


@dataclass(frozen=True, kw_only=True)
class AdmissionPolicy:
    """Explicit trusted startup allowlist; no defaults or LLM-selected types."""

    allowed_kinds: frozenset[RecordKind]
    version: str

    def __post_init__(self) -> None:
        if type(self.allowed_kinds) is not frozenset or any(
            not isinstance(kind, RecordKind) for kind in self.allowed_kinds
        ):
            raise ValueError("immutable RecordKind allowlist required")
        _text(self.version)


@dataclass(frozen=True, kw_only=True)
class SourceState:
    """Caller-verified source snapshot; unknown fields cannot authorize memory."""

    excluded: bool | None
    private: bool | None
    refusal_pending: bool | None
    history_saved: bool | None

    def __post_init__(self) -> None:
        for value in (self.excluded, self.private, self.refusal_pending, self.history_saved):
            _optional_bool(value)


@dataclass(frozen=True, kw_only=True)
class ClassifierProvenance:
    classifier_version: str
    model_id: str
    model_digest: str
    prompt_version: str

    def __post_init__(self) -> None:
        for value in (
            self.classifier_version,
            self.model_id,
            self.model_digest,
            self.prompt_version,
        ):
            _text(value)

    @classmethod
    def from_mapping(cls, value: Mapping[str, str]) -> "ClassifierProvenance":
        return cls(
            classifier_version=value["classifier_version"],
            model_id=value["model_id"],
            model_digest=value["model_digest"],
            prompt_version=value["prompt_version"],
        )


@dataclass(frozen=True, kw_only=True)
class AdmissionProvenance:
    """Absent classifier fields mean no classification was performed."""

    scanner_version: str
    policy_version: str
    classifier_version: str | None
    model_id: str | None
    model_digest: str | None
    prompt_version: str | None

    def __post_init__(self) -> None:
        _text(self.scanner_version)
        _text(self.policy_version)
        values = (self.classifier_version, self.model_id, self.model_digest, self.prompt_version)
        if any(value is None for value in values) and any(value is not None for value in values):
            raise ValueError("complete classifier provenance or none required")
        for value in values:
            if value is not None:
                _text(value)


_REASON_STATE = {
    AdmissionReason.SCAN_SECRET: AdmissionState.DENY_SENSITIVE,
    AdmissionReason.ASSESSMENT_SENSITIVE: AdmissionState.DENY_SENSITIVE,
    AdmissionReason.EXCLUDED_SOURCE: AdmissionState.DENY_USER_REQUEST,
    AdmissionReason.PRIVATE_SOURCE: AdmissionState.DENY_USER_REQUEST,
    AdmissionReason.REFUSAL_PENDING: AdmissionState.DENY_USER_REQUEST,
    AdmissionReason.SCANNED_REFUSAL: AdmissionState.DENY_USER_REQUEST,
    AdmissionReason.SCAN_FAILED: AdmissionState.ABSTAIN_UNKNOWN,
    AdmissionReason.UNKNOWN_SOURCE: AdmissionState.ABSTAIN_UNKNOWN,
    AdmissionReason.HISTORY_NOT_SAVED: AdmissionState.ABSTAIN_UNKNOWN,
    AdmissionReason.POLICY_UNSUPPORTED: AdmissionState.ABSTAIN_UNKNOWN,
    AdmissionReason.UNCLASSIFIED: AdmissionState.ABSTAIN_UNKNOWN,
    AdmissionReason.CLASSIFIER_FAILURE: AdmissionState.ABSTAIN_UNKNOWN,
    AdmissionReason.ASSESSMENT_ABSTAIN: AdmissionState.ABSTAIN_UNKNOWN,
    AdmissionReason.PROVENANCE_MISSING: AdmissionState.ABSTAIN_UNKNOWN,
    AdmissionReason.VALUE_UNKNOWN: AdmissionState.ABSTAIN_UNKNOWN,
    AdmissionReason.KIND_NOT_ALLOWED: AdmissionState.NOT_MEMORY_WORTHY,
    AdmissionReason.NO_LONG_TERM_VALUE: AdmissionState.NOT_MEMORY_WORTHY,
    AdmissionReason.ALLOWED: AdmissionState.ALLOW_STRUCTURED,
}
_PRIORITY = tuple(AdmissionState)


@dataclass(frozen=True, kw_only=True)
class AdmissionResult:
    state: AdmissionState
    reasons: tuple[AdmissionReason, ...]
    provenance: AdmissionProvenance
    assessment_failure: AssessmentFailure | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.state, AdmissionState)
            or type(self.reasons) is not tuple
            or not self.reasons
            or any(not isinstance(reason, AdmissionReason) for reason in self.reasons)
            or not isinstance(self.provenance, AdmissionProvenance)
        ):
            raise ValueError("typed admission result required")
        if self.assessment_failure is not None:
            AssessmentResult(failure=self.assessment_failure)
            if AdmissionReason.CLASSIFIER_FAILURE not in self.reasons:
                raise ValueError("classifier failure reason required")
        if self.state is not min(
            (_REASON_STATE[reason] for reason in self.reasons), key=_PRIORITY.index
        ) or (AdmissionReason.ALLOWED in self.reasons and len(self.reasons) != 1):
            raise ValueError("admission state and reasons disagree")

    @property
    def can_store(self) -> bool:
        """Admission only; caller must still check permission/schema/current sources."""
        return self.state is AdmissionState.ALLOW_STRUCTURED


def _assessment(value: AssessmentResult | None) -> AssessmentResult | None:
    if value is None:
        return None
    try:
        if not isinstance(value, AssessmentResult):
            raise ValueError("AssessmentResult required")
        # Revalidate even results whose nested model was constructed without validation.
        return AssessmentResult(assessment=value.assessment, failure=value.failure)
    except Exception:
        return AssessmentResult(failure="invalid_assessment")


def evaluate(
    candidate: Candidate,
    checked: Scan,
    classified: AssessmentResult | None,
    *,
    policy: AdmissionPolicy,
    source: SourceState,
    classifier_provenance: ClassifierProvenance | None,
) -> AdmissionResult:
    """Pure synchronous decision, preserving all reasons in strict priority order."""
    if (
        not isinstance(candidate, Candidate)
        or not isinstance(checked, Scan)
        or not isinstance(policy, AdmissionPolicy)
        or not isinstance(source, SourceState)
        or (
            classifier_provenance is not None
            and not isinstance(classifier_provenance, ClassifierProvenance)
        )
    ):
        raise ValueError("typed admission inputs required")
    candidate.__post_init__()
    policy.__post_init__()
    source.__post_init__()
    _text(checked.version)
    if classifier_provenance is not None:
        classifier_provenance.__post_init__()
    classified = _assessment(classified)
    reasons: list[AdmissionReason] = []
    if checked.secret is True:
        reasons.append(AdmissionReason.SCAN_SECRET)
    if checked.failed or any(
        type(value) is not bool
        for value in (checked.secret, checked.failed, checked.no_memory, checked.no_history)
    ):
        reasons.append(AdmissionReason.SCAN_FAILED)
    if checked.no_memory is True or checked.no_history is True:
        reasons.append(AdmissionReason.SCANNED_REFUSAL)
    for value, reason in (
        (source.excluded, AdmissionReason.EXCLUDED_SOURCE),
        (source.private, AdmissionReason.PRIVATE_SOURCE),
        (source.refusal_pending, AdmissionReason.REFUSAL_PENDING),
    ):
        if value is True:
            reasons.append(reason)
    if None in (source.excluded, source.private, source.refusal_pending, source.history_saved):
        reasons.append(AdmissionReason.UNKNOWN_SOURCE)
    if source.history_saved is False:
        reasons.append(AdmissionReason.HISTORY_NOT_SAVED)
    if policy.version != POLICY_VERSION:
        reasons.append(AdmissionReason.POLICY_UNSUPPORTED)
    if classified is None:
        reasons.append(AdmissionReason.UNCLASSIFIED)
    elif classified.failure is not None:
        reasons.append(AdmissionReason.CLASSIFIER_FAILURE)
    elif classified.assessment is not None:
        if classified.assessment.classification == "SENSITIVE":
            reasons.append(AdmissionReason.ASSESSMENT_SENSITIVE)
        elif classified.assessment.classification == "ABSTAIN":
            reasons.append(AdmissionReason.ASSESSMENT_ABSTAIN)
    if classified is not None and classifier_provenance is None:
        reasons.append(AdmissionReason.PROVENANCE_MISSING)
    if candidate.kind not in policy.allowed_kinds:
        reasons.append(AdmissionReason.KIND_NOT_ALLOWED)
    if candidate.memory_worthy is False:
        reasons.append(AdmissionReason.NO_LONG_TERM_VALUE)
    elif candidate.memory_worthy is None:
        reasons.append(AdmissionReason.VALUE_UNKNOWN)
    if not reasons:
        reasons.append(AdmissionReason.ALLOWED)
    reasons.sort(key=lambda reason: _PRIORITY.index(_REASON_STATE[reason]))
    provenance = AdmissionProvenance(
        scanner_version=checked.version,
        policy_version=policy.version,
        classifier_version=classifier_provenance.classifier_version
        if classifier_provenance
        else None,
        model_id=classifier_provenance.model_id if classifier_provenance else None,
        model_digest=classifier_provenance.model_digest if classifier_provenance else None,
        prompt_version=classifier_provenance.prompt_version if classifier_provenance else None,
    )
    return AdmissionResult(
        state=_REASON_STATE[reasons[0]],
        reasons=tuple(reasons),
        provenance=provenance,
        assessment_failure=classified.failure if classified else None,
    )


class AdmissionClassifier(Protocol):
    """Managed local classification port; no database or embedding operations."""

    @property
    def provenance(self) -> Mapping[str, str]: ...

    async def assess(self, value: object, policy_version: str) -> AssessmentResult: ...


async def admit(
    value: object,
    candidate: Candidate,
    *,
    policy: AdmissionPolicy,
    source: SourceState,
    classifier: AdmissionClassifier | None,
) -> AdmissionResult:
    """Scan first, optionally classify, then evaluate; never save or infer value.

    This helper requires an already authorized managed local classifier. It does not
    grant PrivacyPolicy permissions or validate live sources; formation must do that.
    Cancellation propagates. Exceptions and malformed results remain content-free.
    """
    checked = scan(value)
    # Validate trusted configuration before any classifier I/O.
    preflight = evaluate(
        candidate, checked, None, policy=policy, source=source, classifier_provenance=None
    )
    if checked.secret or checked.failed or classifier is None or policy.version != POLICY_VERSION:
        return preflight
    provenance = None
    classified = None
    if classifier is not None:
        try:
            provenance = ClassifierProvenance.from_mapping(classifier.provenance)
        except Exception:
            classified = AssessmentResult(failure="invalid_assessment")
        else:
            try:
                async with asyncio.timeout(CLASSIFICATION_TIMEOUT_SECONDS):
                    classified = _assessment(await classifier.assess(value, policy.version))
            except TimeoutError:
                classified = AssessmentResult(failure="timeout")
            except Exception:
                classified = AssessmentResult(failure="provider_exception")
    return evaluate(
        candidate,
        checked,
        classified,
        policy=policy,
        source=source,
        classifier_provenance=provenance,
    )
