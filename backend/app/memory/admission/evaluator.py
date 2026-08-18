from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from app.conversation_history.models import TurnStatus
from app.memory.admission.contracts import (
    ApprovedMemoryCandidate,
    EpisodicEventValue,
    InteractionPreferenceValue,
    MemoryCandidate,
    MemoryType,
    RagAdmissionDecision,
    RagAdmissionResult,
    StructuredValue,
    UserPreferenceValue,
)
from app.memory.admission.templates import render_normalized_text
from app.memory.memory_policy import PrivacyPolicy
from app.privacy.contracts import (
    PrivacyCategory,
    ScanFailure,
    ScanResult,
    ScanSuccess,
    StorageScope,
)
from app.privacy.semantic.contracts import (
    PrivacyAssessment,
    SemanticClassification,
    SubjectScope,
)


class RagAdmissionEvaluator:
    def __init__(self, policy: PrivacyPolicy) -> None:
        self._policy = policy
        self._placeholders = frozenset(
            placeholder for _category, placeholder in policy.placeholders
        )

    def requires_semantic_assessment(
        self,
        *,
        source_scan: ScanResult,
        candidate_slot_scans: Mapping[str, ScanResult],
        candidate: MemoryCandidate,
    ) -> bool:
        result = self._deterministic_result(
            source_scan,
            candidate_slot_scans,
            candidate,
        )
        return result is None

    def evaluate(
        self,
        *,
        source_scan: ScanResult,
        candidate_slot_scans: Mapping[str, ScanResult],
        assessment: PrivacyAssessment | None,
        candidate: MemoryCandidate,
    ) -> RagAdmissionResult:
        deterministic_result = self._deterministic_result(
            source_scan,
            candidate_slot_scans,
            candidate,
        )
        if deterministic_result is not None:
            return deterministic_result
        if (
            assessment is None
            or not isinstance(assessment, PrivacyAssessment)
            or assessment.policy_version != self._policy.policy_version
            or assessment.classification is SemanticClassification.ABSTAIN
        ):
            return self._result(RagAdmissionDecision.ABSTAIN_UNKNOWN)
        if assessment.classification is SemanticClassification.SENSITIVE:
            return self._result(RagAdmissionDecision.DENY_SENSITIVE)
        if (
            assessment.classification is not SemanticClassification.NOT_SENSITIVE
            or assessment.subject_scope is SubjectScope.UNKNOWN
        ):
            return self._result(RagAdmissionDecision.ABSTAIN_UNKNOWN)
        if assessment.subject_scope is SubjectScope.GENERAL:
            return self._result(RagAdmissionDecision.NOT_MEMORY_WORTHY)
        return RagAdmissionResult(
            RagAdmissionDecision.ALLOW_STRUCTURED,
            ApprovedMemoryCandidate(
                candidate.structured_value,
                render_normalized_text(candidate.structured_value),
            ),
        )

    def _deterministic_result(
        self,
        source_scan: ScanResult,
        candidate_slot_scans: Mapping[str, ScanResult],
        candidate: MemoryCandidate,
    ) -> RagAdmissionResult | None:
        source_result = self._source_scan_result(source_scan)
        if source_result is not None:
            return source_result
        structured_value = self._allowlist_value(candidate)
        if structured_value is None:
            return self._result(RagAdmissionDecision.NOT_MEMORY_WORTHY)
        if not isinstance(candidate_slot_scans, Mapping):
            return self._result(RagAdmissionDecision.ABSTAIN_UNKNOWN)
        if set(candidate_slot_scans) != set(self._slot_values(structured_value)):
            return self._result(RagAdmissionDecision.ABSTAIN_UNKNOWN)
        scans = tuple(candidate_slot_scans.values())
        if any(isinstance(scan, ScanFailure) for scan in scans):
            return self._result(RagAdmissionDecision.ABSTAIN_UNKNOWN)
        if any(not isinstance(scan, ScanSuccess) for scan in scans):
            return self._result(RagAdmissionDecision.ABSTAIN_UNKNOWN)
        successful_scans = cast(tuple[ScanSuccess, ...], scans)
        if any(scan.findings for scan in successful_scans):
            return self._result(RagAdmissionDecision.DENY_SENSITIVE)
        if any(
            placeholder in slot_value
            for slot_value in self._slot_values(structured_value).values()
            for placeholder in self._placeholders
        ):
            return self._result(RagAdmissionDecision.DENY_SENSITIVE)
        if (
            candidate.source is None
            or candidate.source.turn_status is not TurnStatus.COMPLETED
            or not candidate.source.history_content_stored
        ):
            return self._result(RagAdmissionDecision.ABSTAIN_UNKNOWN)
        return None

    @staticmethod
    def _source_scan_result(source_scan: ScanResult) -> RagAdmissionResult | None:
        if isinstance(source_scan, ScanFailure):
            return RagAdmissionEvaluator._result(RagAdmissionDecision.ABSTAIN_UNKNOWN)
        if not isinstance(source_scan, ScanSuccess):
            return RagAdmissionEvaluator._result(RagAdmissionDecision.ABSTAIN_UNKNOWN)
        opt_out = any(
            finding.category is PrivacyCategory.STORAGE_OPT_OUT
            and finding.storage_scope in {StorageScope.RAG, StorageScope.BOTH}
            for finding in source_scan.findings
        )
        if opt_out:
            return RagAdmissionEvaluator._result(RagAdmissionDecision.DENY_USER_REQUEST)
        if source_scan.findings:
            return RagAdmissionEvaluator._result(RagAdmissionDecision.DENY_SENSITIVE)
        return None

    @staticmethod
    def _allowlist_value(candidate: object) -> StructuredValue | None:
        if not isinstance(candidate, MemoryCandidate):
            return None
        value = candidate.structured_value
        expected_types = {
            MemoryType.EPISODIC_EVENT: EpisodicEventValue,
            MemoryType.USER_PREFERENCE: UserPreferenceValue,
            MemoryType.INTERACTION_PREFERENCE: InteractionPreferenceValue,
        }
        expected_type = expected_types.get(candidate.memory_type)
        if expected_type is not None and isinstance(value, expected_type):
            return value
        return None

    @staticmethod
    def _slot_values(value: StructuredValue) -> dict[str, str]:
        if isinstance(value, EpisodicEventValue):
            return {"topic": value.topic}
        if isinstance(value, UserPreferenceValue):
            values = {"object": value.object}
            if value.alternative is not None:
                values["alternative"] = value.alternative
            return values
        return {"value": value.value}

    @staticmethod
    def _result(decision: RagAdmissionDecision) -> RagAdmissionResult:
        return RagAdmissionResult(decision, None)


def create_rag_admission_evaluator(policy: PrivacyPolicy) -> RagAdmissionEvaluator:
    return RagAdmissionEvaluator(policy)
