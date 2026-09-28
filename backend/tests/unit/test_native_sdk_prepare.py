from __future__ import annotations

import importlib.util
import io
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts/voice_quality/native_sdk_experiment/prepare.py"
)
_SPEC = importlib.util.spec_from_file_location("native_sdk_prepare", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
prepare = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prepare)

URL = "https://example.invalid/archive.tar.gz"


def http_error(code: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(URL, code, "error", headers, io.BytesIO())


class Opener:
    """呼出ごとに指定した例外を送出し、最後に本文を返す取得関数。"""

    def __init__(self, *failures: Exception, body: bytes = b"archive") -> None:
        self.failures = list(failures)
        self.body = body
        self.calls = 0

    def __call__(self, url: str, timeout: float) -> io.BytesIO:
        assert url == URL
        assert timeout == 60
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return io.BytesIO(self.body)


def test_transient_http_errors_are_retried_until_success() -> None:
    opener = Opener(http_error(503), http_error(429), URLError("reset"))
    waits: list[float] = []

    body = prepare.fetch_archive(URL, opener=opener, sleep=waits.append)

    assert body == b"archive"
    assert opener.calls == 4
    assert waits == [2.0, 4.0, 8.0]


def test_retry_after_header_is_used_and_capped() -> None:
    opener = Opener(http_error(503, "5"), http_error(503, "600"))
    waits: list[float] = []

    prepare.fetch_archive(URL, opener=opener, sleep=waits.append)

    assert waits == [5.0, prepare.MAX_RETRY_WAIT_SECONDS]


def test_non_transient_http_error_is_not_retried() -> None:
    opener = Opener(http_error(404))
    waits: list[float] = []

    with pytest.raises(HTTPError):
        prepare.fetch_archive(URL, opener=opener, sleep=waits.append)

    assert opener.calls == 1
    assert waits == []


def test_gives_up_after_configured_attempts() -> None:
    opener = Opener(*(http_error(503) for _ in range(3)))
    waits: list[float] = []

    with pytest.raises(HTTPError):
        prepare.fetch_archive(URL, opener=opener, sleep=waits.append, attempts=3)

    assert opener.calls == 3
    assert len(waits) == 2


def test_reads_at_most_one_byte_over_limit() -> None:
    opener = Opener(body=b"x" * (prepare.MAX_ARCHIVE_BYTES + 10))

    body = prepare.fetch_archive(URL, opener=opener, sleep=lambda _: None)

    assert len(body) == prepare.MAX_ARCHIVE_BYTES + 1
