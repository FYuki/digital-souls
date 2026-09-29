from __future__ import annotations

import importlib.util
import io
import os
import subprocess
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


class GitRunner:
    """git subcommandごとの結果を返し、fetchだけ指定回数失敗させる実行関数。"""

    def __init__(self, fetch_failures: int = 0, fetched: str = "a" * 40) -> None:
        self.fetch_failures = fetch_failures
        self.fetched = fetched
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["check"] is True
        env = kwargs["env"]
        assert isinstance(env, dict) and env["GIT_TERMINAL_PROMPT"] == "0"
        self.commands.append(command)
        subcommand = command[5]
        if subcommand == "fetch" and self.fetch_failures:
            self.fetch_failures -= 1
            raise subprocess.CalledProcessError(128, command)
        stdout = {"rev-parse": f"{self.fetched}\n".encode(), "archive": b"archive"}.get(subcommand, b"")
        return subprocess.CompletedProcess(command, 0, stdout)


def test_git_archive_fetches_pinned_revision_with_fixed_umask() -> None:
    runner = GitRunner()

    body = prepare.fetch_git_archive(URL, "a" * 40, run=runner, sleep=lambda _: None)

    assert body == b"archive"
    assert [command[5] for command in runner.commands] == ["init", "fetch", "rev-parse", "archive"]
    assert all(command[3:5] == ["-c", "tar.umask=022"] for command in runner.commands)
    assert runner.commands[1][-2:] == [URL, "a" * 40]


def test_git_fetch_failures_are_retried_then_raised() -> None:
    waits: list[float] = []
    recovered = GitRunner(fetch_failures=2)

    prepare.fetch_git_archive(URL, "a" * 40, run=recovered, sleep=waits.append)
    assert waits == [2.0, 4.0]

    with pytest.raises(subprocess.CalledProcessError):
        prepare.fetch_git_archive(
            URL, "a" * 40, run=GitRunner(fetch_failures=3), sleep=lambda _: None, attempts=3
        )


def test_git_archive_rejects_unexpected_commit() -> None:
    runner = GitRunner(fetched="b" * 40)

    with pytest.raises(ValueError):
        prepare.fetch_git_archive(URL, "a" * 40, run=runner, sleep=lambda _: None)

    assert [command[5] for command in runner.commands] == ["init", "fetch", "rev-parse"]


def test_git_env_excludes_inherited_git_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GIT_CONFIG_COUNT経由のurl.insteadOf等、GIT_*全般とSSH_ASKPASSを継承しない。
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.https://redirect.invalid/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    monkeypatch.setenv("GIT_ASKPASS", "/usr/bin/fake-askpass")
    monkeypatch.setenv("SSH_ASKPASS", "/usr/bin/fake-ssh-askpass")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o ProxyCommand=evil")

    env = prepare._git_env()

    # GIT_*は明示設定の4本だけが残り、継承した設定変数・helperは全て外れる。
    assert {key for key in env if key.startswith("GIT_")} == {
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_ASKPASS",
    }
    assert "SSH_ASKPASS" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_ASKPASS"] == ""
