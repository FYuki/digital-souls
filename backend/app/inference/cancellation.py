"""バックグラウンド処理全体へ中断を伝え、前景の推論とは分離する。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.inference.contracts import InferenceCancellationToken
from app.inference.errors import InferenceError, InferenceErrorCategory

_current: ContextVar[tuple[InferenceCancellationToken, ...]] = ContextVar("inference_cancellation", default=())


def current_cancellation_token() -> InferenceCancellationToken | None:
    return _current.get()[-1] if _current.get() else None


def raise_if_cancelled(token: InferenceCancellationToken | None = None) -> None:
    if any(item is not None and item.is_cancelled for item in (token, *_current.get())):
        raise InferenceError(InferenceErrorCategory.CANCELLED, retryable=False)


@contextmanager
def cancellation_scope(token: InferenceCancellationToken | None) -> Iterator[None]:
    # 内側の明示tokenと外側のworker tokenのどちらの中断も維持する。
    previous = _current.set((*_current.get(), token) if token is not None else _current.get())
    try:
        raise_if_cancelled()
        yield
    finally:
        _current.reset(previous)
