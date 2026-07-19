from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from tests.environment_test_support import resolved_profile, single_adapter_registry


def test_should_pass_injected_registry_and_timing_from_up_command_to_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.up_command as up_command
    from environment_timing import EnvironmentTiming
    from run_report import record_ready
    from run_report_timestamps import next_lifecycle_timestamp

    registry = object()
    timing = EnvironmentTiming(
        readiness_attempts=2,
        readiness_interval_seconds=0,
        request_timeout_seconds=0.25,
        supervision_interval_seconds=0,
    )
    captured: dict[str, object] = {}

    class SuccessfulRun:
        def __init__(self, *, report, store, registry, timing, **options):
            captured["registry"] = registry
            captured["timing"] = timing
            self.report = report
            self.store = store
            self.cleanup_failures: list[dict[str, object]] = []

        def verify(self):
            return None

        def prepare(self):
            return None

        def pre_probe(self):
            return {}

        def start_or_reuse(self, decisions):
            return None

        def wait_until_ready(self):
            self.report = self.store.update(
                lambda report: record_ready(
                    report, ready_at=next_lifecycle_timestamp(report)
                )
            )

        def begin_supervision(self):
            return None

        def supervise(self):
            return None

        def cleanup(self):
            return []

    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda environment, default, report, legacy: resolved_profile("test-mocked"),
    )
    monkeypatch.setattr(up_command, "EnvironmentRun", SuccessfulRun)
    arguments = argparse.Namespace(
        run_report=str(tmp_path / "environment-run.json"),
        profile_report=None,
        default_profile="test-mocked",
    )

    exit_code = up_command.up_environment(
        tmp_path,
        tmp_path / ".runtime",
        arguments,
        registry=registry,
        timing=timing,
    )

    assert exit_code == 0
    assert captured == {"registry": registry, "timing": timing}


class _ReadyVoicevoxOperations:
    def __init__(self) -> None:
        self.probe_timeouts: list[float] = []

    def verify(self, dependency, context):
        from adapters.base import Check, VerificationResult

        return VerificationResult((Check("voicevox", "ready", "voicevox", False),))

    def prepare(self, dependency, context):
        return None

    def probe(self, dependency, timeout_seconds):
        from http_readiness import ReadinessResult

        self.probe_timeouts.append(timeout_seconds)
        return ReadinessResult(str(dependency["readinessUrl"]), 1, 0.0, "ready")

    def start(self, dependency, environment):
        from adapters.base import ServiceStartResult

        return ServiceStartResult("reused", False)

    def validate_readiness(self, dependency):
        from adapters.base import ReadinessValidationResult

        return ReadinessValidationResult("ready")

    def is_running(self, service):
        return True

    def stop(self, service, grace_seconds):
        raise AssertionError("a reused service must not be stopped")


def test_should_use_injected_registry_probe_and_timing_for_voicevox_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.voicevox_command as voicevox_command
    from environment_timing import EnvironmentTiming

    adapter = _ReadyVoicevoxOperations()
    registry = single_adapter_registry("voicevox", adapter)
    timing = EnvironmentTiming(
        readiness_attempts=1,
        readiness_interval_seconds=0,
        request_timeout_seconds=0.375,
        supervision_interval_seconds=0,
    )
    monkeypatch.setattr(
        voicevox_command,
        "resolve_profile",
        lambda environment, default: resolved_profile("integration-voice"),
    )

    exit_code = voicevox_command.start_voicevox(
        tmp_path,
        "integration-voice",
        registry=registry,
        timing=timing,
    )

    assert exit_code == 0
    assert adapter.probe_timeouts == [0.375]


def test_should_use_injected_registry_and_request_timeout_for_verify_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    import commands.verify_command as verify_command
    from environment_timing import EnvironmentTiming

    registry = object()
    timing = EnvironmentTiming(request_timeout_seconds=0.625)
    captured: dict[str, object] = {}
    profile = resolved_profile("test-mocked")

    def verification_checks(
        checked_profile,
        checked_registry,
        *,
        request_timeout_seconds,
    ):
        captured.update(
            profile=checked_profile,
            registry=checked_registry,
            request_timeout_seconds=request_timeout_seconds,
        )
        return {}

    monkeypatch.setattr(
        verify_command,
        "resolve_profile",
        lambda environment, default: profile,
    )
    monkeypatch.setattr(verify_command, "verification_checks", verification_checks)

    exit_code = verify_command.verify_environment(
        tmp_path,
        "test-mocked",
        registry=registry,
        timing=timing,
    )

    assert exit_code == 0
    assert captured == {
        "profile": profile,
        "registry": registry,
        "request_timeout_seconds": 0.625,
    }
    assert json.loads(capsys.readouterr().out) == {
        "effectiveProfile": "test-mocked",
        "services": {},
    }
