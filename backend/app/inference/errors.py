from __future__ import annotations

from enum import Enum


class InferenceErrorCategory(str, Enum):
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    MODEL_NOT_FOUND = "model_not_found"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"
    INVALID_REQUEST = "invalid_request"
    CANCELLED = "cancelled"
    PROVIDER_ERROR = "provider_error"
    ACCESS_DENIED = "access_denied"


class InferenceError(RuntimeError):
    """Providerの生情報を上位層へ露出しない共通エラー。"""

    def __init__(
        self,
        category: InferenceErrorCategory,
        *,
        retryable: bool,
        message: str | None = None,
    ) -> None:
        self.category = category
        self.retryable = retryable
        super().__init__(message or f"inference failed: {category.value}")
