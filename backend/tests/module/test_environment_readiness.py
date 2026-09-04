from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import tests.environment_test_support


def test_should_dispatch_bounded_inference_readiness_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import environment_cli

    calls: list[tuple[str, list[str], object]] = []
    monkeypatch.setattr(
        environment_cli,
        "wait_for_services",
        lambda profile, services, timing: calls.append(
            (profile, services, timing)
        )
        or 0,
    )
    arguments = environment_cli._parser().parse_args(
        [
            "wait-readiness",
            "--profile",
            "dogfood",
            "--service",
            "ollama",
            "--service",
            "voicevox",
            "--max-attempts",
            "30",
            "--interval-seconds",
            "1",
            "--request-timeout-seconds",
            "1",
        ]
    )

    result = environment_cli._dispatch(arguments)

    assert result == 0
    profile, services, timing = calls[0]
    assert profile == "dogfood"
    assert services == ["ollama", "voicevox"]
    assert timing.readiness_attempts == 30
    assert timing.readiness_interval_seconds == 1
    assert timing.request_timeout_seconds == 1


def test_should_accept_managed_application_services_for_bounded_wait() -> None:
    import environment_cli

    arguments = environment_cli._parser().parse_args(
        [
            "wait-readiness",
            "--profile",
            "dogfood",
            "--service",
            "frontend",
            "--service",
            "backend",
            "--max-attempts",
            "180",
            "--interval-seconds",
            "1",
            "--request-timeout-seconds",
            "2",
        ]
    )

    assert arguments.service == ["frontend", "backend"]


def test_should_wait_for_managed_application_services(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import commands.readiness_wait_command as command
    from environment_timing import EnvironmentTiming
    from http_readiness import ReadinessResult

    monkeypatch.setattr(
        command,
        "load_profile",
        lambda name: {
            "name": name,
            "dependencies": {
                "frontend": {
                    "mode": "real",
                    "source": "managed",
                    "baseUrl": "http://localhost:15173",
                    "readinessPath": "/",
                },
                "backend": {
                    "mode": "real",
                    "source": "managed",
                    "baseUrl": "http://localhost:18000",
                    "readinessPath": "/health/ready",
                },
            },
        },
    )
    calls: list[tuple[str, int, float, float]] = []

    def fake_wait(
        url: str,
        *,
        max_attempts: int,
        interval_seconds: float,
        request_timeout_seconds: float,
    ) -> ReadinessResult:
        calls.append((url, max_attempts, interval_seconds, request_timeout_seconds))
        return ReadinessResult(url, 4, 3.0, "ready")

    monkeypatch.setattr(command, "wait_for_http", fake_wait)

    result = command.wait_for_services(
        "dogfood",
        ("frontend", "backend"),
        EnvironmentTiming(
            readiness_attempts=180,
            readiness_interval_seconds=1,
            request_timeout_seconds=2,
        ),
    )

    assert result == 0
    assert calls == [
        ("http://localhost:15173/", 180, 1, 2),
        ("http://localhost:18000/health/ready", 180, 1, 2),
    ]
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ready"
    assert report["services"]["frontend"]["attempts"] == 4


def test_should_retry_each_external_inference_service_until_ready(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import commands.readiness_wait_command as command
    from environment_timing import EnvironmentTiming
    from http_readiness import ReadinessResult

    monkeypatch.setattr(
        command,
        "load_profile",
        lambda name: {
            "name": name,
            "dependencies": {
                "ollama": {
                    "mode": "real",
                    "source": "external",
                    "baseUrl": "http://localhost:11434",
                    "readinessPath": "/api/tags",
                },
                "voicevox": {
                    "mode": "real",
                    "source": "external",
                    "baseUrl": "http://127.0.0.1:50021",
                    "readinessPath": "/version",
                },
            },
        },
    )
    calls: list[tuple[str, int, float, float]] = []

    def fake_wait(
        url: str,
        *,
        max_attempts: int,
        interval_seconds: float,
        request_timeout_seconds: float,
    ) -> ReadinessResult:
        calls.append((url, max_attempts, interval_seconds, request_timeout_seconds))
        return ReadinessResult(url, 3, 2.0, "ready")

    monkeypatch.setattr(command, "wait_for_http", fake_wait)

    result = command.wait_for_services(
        "dogfood",
        ("ollama", "voicevox"),
        EnvironmentTiming(
            readiness_attempts=30,
            readiness_interval_seconds=1,
            request_timeout_seconds=1,
        ),
    )

    assert result == 0
    assert calls == [
        ("http://localhost:11434/api/tags", 30, 1, 1),
        ("http://127.0.0.1:50021/version", 30, 1, 1),
    ]
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ready"
    assert report["services"]["ollama"]["attempts"] == 3


def test_should_fail_inference_gate_after_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import commands.readiness_wait_command as command
    from environment_timing import EnvironmentTiming
    from http_readiness import ReadinessResult

    monkeypatch.setattr(
        command,
        "load_profile",
        lambda name: {
            "name": name,
            "dependencies": {
                "ollama": {
                    "mode": "real",
                    "source": "external",
                    "baseUrl": "http://localhost:11434",
                    "readinessPath": "/api/tags",
                },
                "voicevox": {
                    "mode": "real",
                    "source": "external",
                    "baseUrl": "http://127.0.0.1:50021",
                    "readinessPath": "/version",
                },
            },
        },
    )
    monkeypatch.setattr(
        command,
        "wait_for_http",
        lambda url, **kwargs: ReadinessResult(url, 30, 30.0, "timeout"),
    )

    result = command.wait_for_services(
        "dogfood",
        ("ollama", "voicevox"),
        EnvironmentTiming(
            readiness_attempts=30,
            readiness_interval_seconds=1,
            request_timeout_seconds=1,
        ),
    )

    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "not_ready"
    assert all(
        service["result"] == "timeout" for service in report["services"].values()
    )


def test_should_report_missing_inference_dependency_as_profile_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import commands.readiness_wait_command as command
    from environment_timing import EnvironmentTiming
    from profile_types import ProfileError

    monkeypatch.setattr(
        command,
        "load_profile",
        lambda name: {
            "name": name,
            "dependencies": {
                "ollama": {
                    "mode": "real",
                    "source": "external",
                    "baseUrl": "http://localhost:11434",
                    "readinessPath": "/api/tags",
                }
            },
        },
    )
    wait_called = False

    def unexpected_wait(*args: object, **kwargs: object) -> None:
        nonlocal wait_called
        wait_called = True

    monkeypatch.setattr(command, "wait_for_http", unexpected_wait)

    with pytest.raises(ProfileError, match="voicevox dependency is required"):
        command.wait_for_services(
            "dogfood",
            ("ollama", "voicevox"),
            EnvironmentTiming(),
        )

    assert wait_called is False


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


def test_should_report_not_ready_when_any_profile_service_probe_fails(
    http_endpoint,
):
    from http_readiness import probe_http_services

    services, ready = probe_http_services(
        {
            "frontend": http_endpoint(204),
            "backend": "http://127.0.0.1:1/",
        },
        timeout_seconds=0.05,
    )

    assert not ready
    assert services["frontend"]["result"] == "ready"
    assert services["backend"]["result"] == "not_ready"


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


def test_should_retry_with_injected_probe_until_ready():
    from http_readiness import ReadinessResult, wait_for_http

    url = "http://service.test/health"
    observations = iter(("not_ready", "ready"))
    calls: list[tuple[str, float]] = []

    def probe(probe_url: str, *, timeout_seconds: float) -> ReadinessResult:
        calls.append((probe_url, timeout_seconds))
        return ReadinessResult(probe_url, 1, 0.0, next(observations))

    result = wait_for_http(
        url,
        max_attempts=3,
        interval_seconds=0,
        request_timeout_seconds=0.25,
        probe=probe,
    )

    assert result.result == "ready"
    assert result.attempts == 2
    assert calls == [(url, 0.25), (url, 0.25)]


def test_should_stop_injected_probe_at_attempt_limit():
    from http_readiness import ReadinessResult, wait_for_http

    url = "http://service.test/health"
    calls: list[tuple[str, float]] = []

    def probe(probe_url: str, *, timeout_seconds: float) -> ReadinessResult:
        calls.append((probe_url, timeout_seconds))
        return ReadinessResult(probe_url, 1, 0.0, "not_ready")

    result = wait_for_http(
        url,
        max_attempts=2,
        interval_seconds=0,
        request_timeout_seconds=0.125,
        probe=probe,
    )

    assert result.result == "timeout"
    assert result.attempts == 2
    assert calls == [(url, 0.125), (url, 0.125)]


def test_should_resolve_default_probe_when_wait_is_called(
    monkeypatch: pytest.MonkeyPatch,
):
    import http_readiness

    url = "http://service.test/health"
    calls: list[tuple[str, float]] = []

    def replacement_probe(
        probe_url: str, *, timeout_seconds: float
    ) -> http_readiness.ReadinessResult:
        calls.append((probe_url, timeout_seconds))
        return http_readiness.ReadinessResult(probe_url, 1, 0.0, "ready")

    monkeypatch.setattr(http_readiness, "probe_http", replacement_probe)

    result = http_readiness.wait_for_http(
        url,
        max_attempts=1,
        interval_seconds=0,
        request_timeout_seconds=0.375,
    )

    assert result.result == "ready"
    assert calls == [(url, 0.375)]
