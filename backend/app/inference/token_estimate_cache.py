from __future__ import annotations

from collections import OrderedDict
import hashlib
import hmac
import secrets
from threading import Lock


class ExactTokenEstimateCache:
    """本文を保持せず、adapter固有HMACと正確なtoken数だけを有界に保持する。"""

    def __init__(self, capacity: int = 128) -> None:
        if capacity < 1:
            raise ValueError("cache capacity must be positive")
        self._capacity = capacity
        self._key = secrets.token_bytes(32)
        self._counts: OrderedDict[bytes, int] = OrderedDict()
        self._lock = Lock()

    def key(self, canonical_request: bytes) -> bytes:
        return hmac.digest(self._key, canonical_request, hashlib.sha256)

    def get(self, key: bytes) -> int | None:
        with self._lock:
            value = self._counts.get(key)
            if value is not None:
                self._counts.move_to_end(key)
            return value

    def put(self, key: bytes, count: int) -> None:
        with self._lock:
            self._counts[key] = count
            self._counts.move_to_end(key)
            while len(self._counts) > self._capacity:
                self._counts.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()
