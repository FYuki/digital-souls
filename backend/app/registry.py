"""モジュールレベルの「最後に登録されたものが現在値」レジストリ。"""

from __future__ import annotations

import threading
from typing import Generic, TypeVar


T = TypeVar("T")


class RegistrationStack(Generic[T]):
    """登録順を保持し、最新の登録を現在値として返すスレッドセーフなスタック。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[T] = []

    def push(self, item: T) -> None:
        with self._lock:
            self._items.append(item)

    def remove(self, item: T) -> None:
        with self._lock:
            self._items.remove(item)

    def current(self) -> T | None:
        with self._lock:
            return self._items[-1] if self._items else None
