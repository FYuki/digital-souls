"""画面入力の識別子を、その入力から開始した応答にのみ渡す。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_control_request: ContextVar[str | None] = ContextVar(
    "core_control_request", default=None
)


def current_control_request() -> str | None:
    return _control_request.get()


@contextmanager
def response_control_scope(request_id: str | None) -> Iterator[None]:
    token = _control_request.set(request_id)
    try:
        yield
    finally:
        _control_request.reset(token)
