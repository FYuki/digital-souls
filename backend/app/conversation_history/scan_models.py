import math
from dataclasses import dataclass
from enum import Enum

RECOGNIZER_VERSION = "history-deterministic-v2"
POLICY_VERSION = "2026-07"


class FindingCategory(str, Enum):
    SECRET = "secret"
    DIRECT_IDENTIFIER = "direct_identifier"
    STORAGE_DIRECTIVE = "storage_directive"


class StorageScope(str, Enum):
    RAG = "rag"
    HISTORY = "history"
    BOTH = "both"


@dataclass(frozen=True)
class ScanFinding:
    start: int
    end: int
    category: FindingCategory
    confidence: float
    reason_code: str
    storage_scope: StorageScope | None = None

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("finding span must be a non-empty half-open interval")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("finding confidence must be between zero and one")
        if (self.category is FindingCategory.STORAGE_DIRECTIVE) != (
            self.storage_scope is not None
        ):
            raise ValueError("only storage directives may define storage_scope")


@dataclass(frozen=True)
class ScanSuccess:
    findings: tuple[ScanFinding, ...]
    recognizer_version: str = RECOGNIZER_VERSION
    policy_version: str = POLICY_VERSION


@dataclass(frozen=True)
class ScanFailure:
    reason_code: str
    recognizer_version: str = RECOGNIZER_VERSION
    policy_version: str = POLICY_VERSION


ScanResult = ScanSuccess | ScanFailure
