from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.memory.episodic.contracts import FiveW, NarrativeContext, RecordKind, What
from app.memory.episodic.privacy import EpisodicPrivacyReviewer
from app.memory.memory_policy import resolved_memory_policy
from app.privacy.contracts import ScanSuccess, PrivacyCategory, StorageScope
from app.privacy.semantic.contracts import SemanticClassification, SubjectScope
from tests.unit.test_rag_admission_service import _assessment, _finding


def reviewer():
    scanner = Mock()
    scanner.scan.return_value = ScanSuccess(())
    classifier = Mock()
    classifier.classify.return_value = _assessment(subject_scope=SubjectScope.GENERAL)
    return EpisodicPrivacyReviewer(scanner=scanner, classifier=classifier,
                                    policy=resolved_memory_policy().privacy), scanner, classifier


def test_ordinary_or_hypothetical_experience_is_not_rejected_for_general_subject_scope():
    gate, scanner, classifier = reviewer()
    value = FiveW(what=What(predicate="想像した", object="月旅行"), context=NarrativeContext.HYPOTHETICAL)
    result = gate.review(kind=RecordKind.FACT, value=value, source_texts=("もし月に旅行したら",))
    assert result.allowed and result.stamp is not None
    scanned = [call.args[0] for call in scanner.scan.call_args_list]
    assert "想像した" in scanned and "月旅行" in scanned
    classified = [call.args[0] for call in classifier.classify.call_args_list]
    assert "もし月に旅行したら" in classified
    assert any("仮定の話" in text for text in classified)


@pytest.mark.parametrize("classification", [SemanticClassification.SENSITIVE, SemanticClassification.ABSTAIN])
def test_semantic_denial_in_generated_content_cannot_pass_clean_source(classification):
    gate, _, classifier = reviewer()
    classifier.classify.side_effect = [_assessment(), _assessment(classification)]
    result = gate.review(kind=RecordKind.FACT, value=FiveW(what=What(predicate="話した")),
                         source_texts=("日常の会話",))
    assert not result.allowed and result.stamp is None


def test_opt_out_in_full_source_prevents_semantic_calls():
    gate, scanner, classifier = reviewer()
    scanner.scan.side_effect = lambda text: (
        ScanSuccess((_finding(PrivacyCategory.STORAGE_OPT_OUT, StorageScope.RAG),))
        if "保存しないで" in text else ScanSuccess(()))
    result = gate.review(kind=RecordKind.FACT, value=FiveW(what=What(predicate="食べた")),
                         source_texts=("うどんを食べた。保存しないで",))
    assert not result.allowed
    classifier.classify.assert_not_called()


def test_masked_placeholder_and_old_policy_assessment_are_not_stored():
    gate, _, classifier = reviewer()
    placeholder = resolved_memory_policy().privacy.placeholders[0][1]
    assert not gate.review(kind=RecordKind.FACT, value=FiveW(what=What(predicate="聞いた", object=placeholder)),
                           source_texts=("話をした",)).allowed
    classifier.classify.return_value = replace(_assessment(), policy_version="old-policy")
    assert not gate.review(kind=RecordKind.FACT, value=FiveW(what=What(predicate="聞いた")),
                           source_texts=("話をした",)).allowed
