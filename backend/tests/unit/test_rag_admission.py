import pytest

from app.conversation_history.scan_models import (
    FindingCategory,
    ScanFailure,
    ScanFinding,
    ScanSuccess,
    StorageScope,
)
from app.memory.rag_admission import RagAdmissionEvaluator


@pytest.mark.parametrize(
    "scan_result",
    (
        ScanFailure(reason_code="recognizer_failure"),
        ScanSuccess(
            findings=(
                ScanFinding(
                    start=0,
                    end=4,
                    category=FindingCategory.SECRET,
                    confidence=1.0,
                    reason_code="vendor_api_key",
                ),
            )
        ),
        ScanSuccess(
            findings=(
                ScanFinding(
                    start=0,
                    end=4,
                    category=FindingCategory.DIRECT_IDENTIFIER,
                    confidence=1.0,
                    reason_code="phone_number",
                ),
            )
        ),
        ScanSuccess(
            findings=(
                ScanFinding(
                    start=0,
                    end=4,
                    category=FindingCategory.STORAGE_DIRECTIVE,
                    confidence=1.0,
                    reason_code="rag_storage_denied",
                    storage_scope=StorageScope.RAG,
                ),
            )
        ),
        ScanSuccess(
            findings=(
                ScanFinding(
                    start=0,
                    end=4,
                    category=FindingCategory.STORAGE_DIRECTIVE,
                    confidence=1.0,
                    reason_code="all_storage_denied",
                    storage_scope=StorageScope.BOTH,
                ),
            )
        ),
    ),
)
def test_should_reject_fail_closed_rag_admission(scan_result) -> None:
    assert not RagAdmissionEvaluator().allows(scan_result)


@pytest.mark.parametrize(
    "scan_result",
    (
        ScanSuccess(findings=()),
        ScanSuccess(
            findings=(
                ScanFinding(
                    start=0,
                    end=4,
                    category=FindingCategory.STORAGE_DIRECTIVE,
                    confidence=1.0,
                    reason_code="history_storage_denied",
                    storage_scope=StorageScope.HISTORY,
                ),
            )
        ),
    ),
)
def test_should_allow_rag_safe_scanner_results(scan_result) -> None:
    assert RagAdmissionEvaluator().allows(scan_result)
