from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import tests.environment_test_support


class _Handler(BaseHTTPRequestHandler):
    statuses: list[int] = []

    def do_GET(self) -> None:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        self.send_response(status)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def http_endpoint():
    servers: list[ThreadingHTTPServer] = []

    def create(*statuses: int) -> str:
        _Handler.statuses = list(statuses)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host, port = server.server_address
        return f"http://{host}:{port}/health"

    yield create
    for server in servers:
        server.shutdown()
        server.server_close()


def test_should_return_structured_success_for_single_probe(http_endpoint):
    from http_readiness import probe_http

    url = http_endpoint(200)

    result = probe_http(url, timeout_seconds=1.0)

    assert result.url == url
    assert result.attempts == 1
    assert result.elapsed_seconds >= 0
    assert result.result == "ready"


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_should_treat_non_success_http_status_as_not_ready(http_endpoint, status: int):
    from http_readiness import probe_http

    result = probe_http(http_endpoint(status), timeout_seconds=1.0)

    assert result.result == "not_ready"
    assert result.attempts == 1


def test_should_return_not_ready_for_connection_refusal():
    from http_readiness import probe_http

    result = probe_http("http://127.0.0.1:1/health", timeout_seconds=0.05)

    assert result.result == "not_ready"
    assert result.attempts == 1


def test_should_retry_until_ready_and_report_exact_attempt_count(http_endpoint):
    from http_readiness import wait_for_http

    result = wait_for_http(
        http_endpoint(503, 503, 200),
        max_attempts=3,
        interval_seconds=0,
        request_timeout_seconds=1.0,
    )

    assert result.result == "ready"
    assert result.attempts == 3
    assert result.elapsed_seconds >= 0


def test_should_stop_after_attempt_limit_without_extra_request(http_endpoint):
    from http_readiness import wait_for_http

    result = wait_for_http(
        http_endpoint(503, 200),
        max_attempts=1,
        interval_seconds=0,
        request_timeout_seconds=1.0,
    )

    assert result.result == "timeout"
    assert result.attempts == 1


@pytest.mark.parametrize(
    ("max_attempts", "interval_seconds"),
    [(0, 0), (-1, 0), (1, -0.1), (True, 0), (1, True)],
)
def test_should_reject_invalid_retry_boundaries_before_probe(
    max_attempts: object,
    interval_seconds: object,
):
    from http_readiness import ReadinessConfigurationError, wait_for_http

    with pytest.raises(ReadinessConfigurationError):
        wait_for_http(
            "http://127.0.0.1:1/health",
            max_attempts=max_attempts,
            interval_seconds=interval_seconds,
            request_timeout_seconds=0.1,
        )


def test_should_abort_readiness_when_managed_environment_exits():
    from http_readiness import wait_for_http

    def report_exit() -> None:
        raise RuntimeError("managed service exited")

    with pytest.raises(RuntimeError, match="managed service exited"):
        wait_for_http(
            "http://127.0.0.1:1/",
            max_attempts=2,
            interval_seconds=0,
            request_timeout_seconds=0.01,
            assert_environment_running=report_exit,
        )
