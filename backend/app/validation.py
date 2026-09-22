"""ドメイン間で共通する値検証プリミティブ。

domain固有の例外型が必要な場合は predicate（is_*）だけを共有し、
呼び出し側で例外へ変換する。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeGuard
from uuid import UUID


def is_uuid4(value: object) -> TypeGuard[UUID]:
    return isinstance(value, UUID) and value.version == 4


def require_uuid4(value: object, field_name: str) -> UUID:
    if not is_uuid4(value):
        raise ValueError(f"{field_name} must be a UUID4")
    return value


def is_aware_datetime(value: object) -> TypeGuard[datetime]:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def require_aware_datetime(value: object, field_name: str) -> datetime:
    if not is_aware_datetime(value):
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def require_non_empty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def utc_now(clock: Callable[[], datetime]) -> datetime:
    """clock()の返値がtimezone-awareであることを検証し、UTCへ正規化する。"""
    value = clock()
    if not is_aware_datetime(value):
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
