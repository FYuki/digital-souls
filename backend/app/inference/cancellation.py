"""バックグラウンド処理全体へ中断を伝え、前景の推論とは分離する。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.inference.contracts import InferenceCancellationToken
from app.inference.errors import InferenceError, InferenceErrorCategory

_current: ContextVar[InferenceCancellationToken | None] = ContextVar("inference_cancellation", default=None)


def current_cancellation_token() -> InferenceCancellationToken | None:
    return _current.get()


def raise_if_cancelled(token: InferenceCancellationToken | None = None) -> None:
    if any(item is not None and item.is_cancelled for item in (token, _current.get())):
        raise InferenceError(InferenceErrorCategory.CANCELLED, retryable=False)


@contextmanager
def cancellation_scope(token: InferenceCancellationToken) -> Iterator[None]:
    previous = _current.set(token)
    try:
        raise_if_cancelled()
        yield
    finally:
        _current.reset(previous)
