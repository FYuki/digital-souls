from __future__ import annotations

from enum import Enum
from typing import NoReturn

import httpx


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


def raise_for_http_status(status_code: int, error_code: str | None = None) -> NoReturn:
    """providerのHTTP statusをInferenceErrorへ正規化して送出する。"""
    if status_code == 401:
        category, retryable = InferenceErrorCategory.AUTHENTICATION_FAILED, False
    elif status_code == 403:
        category, retryable = InferenceErrorCategory.PERMISSION_DENIED, False
    elif status_code == 404 or error_code == "model_not_found":
        category, retryable = InferenceErrorCategory.MODEL_NOT_FOUND, False
    elif status_code == 429:
        category, retryable = InferenceErrorCategory.RATE_LIMITED, True
    elif status_code in {408, 504}:
        category, retryable = InferenceErrorCategory.TIMEOUT, True
    elif status_code == 400:
        category, retryable = InferenceErrorCategory.INVALID_REQUEST, False
    elif status_code >= 500:
        category, retryable = InferenceErrorCategory.UNAVAILABLE, True
    else:
        category, retryable = InferenceErrorCategory.PROVIDER_ERROR, False
    raise InferenceError(category, retryable=retryable)


def raise_inference_error(error: Exception) -> NoReturn:
    """provider境界の例外をInferenceErrorへ正規化して送出する。"""
    if isinstance(error, InferenceError):
        raise error
    if isinstance(error, httpx.HTTPStatusError):
        raise_for_http_status(error.response.status_code)
    if isinstance(error, httpx.TimeoutException):
        raise InferenceError(InferenceErrorCategory.TIMEOUT, retryable=True) from None
    if isinstance(error, httpx.HTTPError):
        raise InferenceError(InferenceErrorCategory.UNAVAILABLE, retryable=True) from None
    raise InferenceError(InferenceErrorCategory.PROVIDER_ERROR, retryable=False) from None
