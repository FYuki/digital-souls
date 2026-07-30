from app.conversation_history.scan_models import (
    FindingCategory,
    ScanFailure,
    ScanResult,
    StorageScope,
)


class RagAdmissionEvaluator:
    def allows(self, scan_result: ScanResult) -> bool:
        if isinstance(scan_result, ScanFailure):
            return False
        for finding in scan_result.findings:
            if finding.category in (
                FindingCategory.SECRET,
                FindingCategory.DIRECT_IDENTIFIER,
            ):
                return False
            if (
                finding.category is FindingCategory.STORAGE_DIRECTIVE
                and finding.storage_scope in (StorageScope.RAG, StorageScope.BOTH)
            ):
                return False
        return True
