"""画面の明示的な続行を、そのHTTP応答または音声応答のtaskにだけ渡す。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_resume: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "action_resume", default=None
)


@contextmanager
def confirmation_resume_scope(
    character: str, conversation: str, request_id: str | None
) -> Iterator[None]:
    token = _resume.set((character, conversation, request_id) if request_id else None)
    try:
        yield
    finally:
        _resume.reset(token)


def confirmation_resume_id(character: str, conversation: str) -> str | None:
    value = _resume.get()
    return (
        value[2]
        if value is not None and value[:2] == (character, conversation)
        else None
    )
