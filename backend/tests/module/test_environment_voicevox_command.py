from __future__ import annotations

from pathlib import Path

import pytest
def test_should_stop_owned_voicevox_when_single_service_readiness_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.voicevox_command as voicevox_command
    from adapters.base import Check, ServiceStartResult, StopResult, VerificationResult
    from http_readiness import ReadinessResult
    from tests.environment_test_support import resolved_profile

    class OwnedVoicevox:
        def __init__(self):
            self.stopped: list[dict[str, object]] = []

        def verify(self, dependency, context):
            return VerificationResult((Check("voicevox", "pending", "exists", False),))

        def prepare(self, dependency, context):
            return None

        def probe(self, dependency, timeout_seconds):
            return ReadinessResult(
                str(dependency["readinessUrl"]), 1, 0.0, "not_ready"
            )

        def start(self, dependency, environment):
            return ServiceStartResult(
                "started",
                True,
                container_identity={"containerId": "owned", "startedAt": "now"},
            )

        def stop(self, service, grace_seconds):
            self.stopped.append(dict(service))
            return StopResult("stopped")

    adapter = OwnedVoicevox()
    monkeypatch.setattr(
        voicevox_command, "resolve_profile", lambda env, default: resolved_profile()
    )
    monkeypatch.setattr(voicevox_command, "create_service_registry", lambda root: object())
    monkeypatch.setattr(
        voicevox_command, "require_service_operations", lambda registry, name: adapter
    )
    monkeypatch.setattr(
        voicevox_command,
        "operation_context_for",
        lambda name, dependencies, registry: object(),
    )
    monkeypatch.setattr(
        voicevox_command,
        "wait_for_http",
        lambda url, **options: ReadinessResult(url, 120, 60.0, "timeout"),
    )

    with pytest.raises(RuntimeError, match="did not become ready"):
        voicevox_command.start_voicevox(tmp_path, "integration-voice")

    assert adapter.stopped == [
        {
            "state": "started",
            "owned": True,
            "containerIdentity": {"containerId": "owned", "startedAt": "now"},
        }
    ]


def test_should_preserve_voicevox_readiness_failure_when_cleanup_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.voicevox_command as voicevox_command
    from adapters.base import (
        AdapterOperationError,
        Check,
        ServiceStartResult,
        VerificationResult,
    )
    from http_readiness import ReadinessResult
    from tests.environment_test_support import resolved_profile

    class FailingCleanupVoicevox:
        def verify(self, dependency, context):
            return VerificationResult((Check("voicevox", "pending", "exists", False),))

        def prepare(self, dependency, context):
            return None

        def probe(self, dependency, timeout_seconds):
            return ReadinessResult(
                str(dependency["readinessUrl"]), 1, 0.0, "not_ready"
            )

        def start(self, dependency, environment):
            return ServiceStartResult(
                "started",
                True,
                container_identity={"containerId": "owned", "startedAt": "now"},
            )

        def stop(self, service, grace_seconds):
            raise OSError("docker daemon disconnected")

    adapter = FailingCleanupVoicevox()
    monkeypatch.setattr(
        voicevox_command, "resolve_profile", lambda env, default: resolved_profile()
    )
    monkeypatch.setattr(voicevox_command, "create_service_registry", lambda root: object())
    monkeypatch.setattr(
        voicevox_command, "require_service_operations", lambda registry, name: adapter
    )
    monkeypatch.setattr(
        voicevox_command,
        "operation_context_for",
        lambda name, dependencies, registry: object(),
    )
    monkeypatch.setattr(
        voicevox_command,
        "wait_for_http",
        lambda url, **options: ReadinessResult(url, 120, 60.0, "timeout"),
    )

    with pytest.raises(AdapterOperationError) as error:
        voicevox_command.start_voicevox(tmp_path, "integration-voice")

    assert error.value.category == "readiness"
    assert str(error.value) == "VOICEVOX did not become ready"
    assert error.value.cleanup_failure is not None
    assert error.value.cleanup_failure.message == "docker daemon disconnected"


def test_should_retry_voicevox_cleanup_from_startup_error_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.voicevox_command as voicevox_command
    from adapters.base import (
        AdapterOperationError,
        Check,
        ServiceStartResult,
        StopResult,
        VerificationResult,
    )
    from tests.environment_test_support import resolved_profile

    initial_cleanup_failure = StopResult("failed", "rollback stop failed")
    ownership = ServiceStartResult(
        "started",
        True,
        container_identity={"containerId": "owned", "startedAt": "now"},
    )

    class StartupFailureVoicevox:
        def __init__(self):
            self.stopped: list[dict[str, object]] = []

        def verify(self, dependency, context):
            return VerificationResult((Check("voicevox", "pending", "exists", False),))

        def prepare(self, dependency, context):
            return None

        def start(self, dependency, environment):
            raise AdapterOperationError(
                "startup",
                "VOICEVOX startup failed",
                ownership=ownership,
                cleanup_failure=initial_cleanup_failure,
            )

        def stop(self, service, grace_seconds):
            self.stopped.append(dict(service))
            return StopResult("stopped")

    adapter = StartupFailureVoicevox()
    monkeypatch.setattr(
        voicevox_command, "resolve_profile", lambda env, default: resolved_profile()
    )
    monkeypatch.setattr(voicevox_command, "create_service_registry", lambda root: object())
    monkeypatch.setattr(
        voicevox_command, "require_service_operations", lambda registry, name: adapter
    )
    monkeypatch.setattr(
        voicevox_command,
        "operation_context_for",
        lambda name, dependencies, registry: object(),
    )

    with pytest.raises(AdapterOperationError) as error:
        voicevox_command.start_voicevox(tmp_path, "integration-voice")

    assert error.value.category == "startup"
    assert error.value.cleanup_failure is initial_cleanup_failure
    assert adapter.stopped == [
        {
            "state": "started",
            "owned": True,
            "containerIdentity": {"containerId": "owned", "startedAt": "now"},
        }
    ]
