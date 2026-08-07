from __future__ import annotations

import sys
import time
from pathlib import Path


root = Path(sys.argv[1])
ready_marker = Path(sys.argv[2])
stop_log = Path(sys.argv[3])
sys.path[:0] = [str(root / "environments"), str(root / "backend")]

from adapters.base import (  # noqa: E402
    Check,
    ReadinessValidationResult,
    ServiceStartResult,
    StopResult,
    VerificationResult,
)
from commands import voicevox_command  # noqa: E402
from environment_timing import EnvironmentTiming  # noqa: E402
from http_readiness import ReadinessResult  # noqa: E402
from tests.environment_test_support import (  # noqa: E402
    resolved_profile,
    single_adapter_registry,
)


class SignalVoicevox:
    def verify(self, dependency, context):
        return VerificationResult((Check("voicevox", "ready", "ready", False),))

    def prepare(self, dependency, context):
        return None

    def probe(self, dependency, timeout_seconds):
        ready_marker.touch()
        time.sleep(0.05)
        return ReadinessResult(
            str(dependency["readinessUrl"]), 1, 0.0, "not_ready"
        )

    def start(self, dependency, environment):
        return ServiceStartResult(
            "started",
            True,
            container_identity={"containerId": "owned", "startedAt": "now"},
        )

    def validate_readiness(self, dependency):
        return ReadinessValidationResult("ready")

    def is_running(self, service):
        return True

    def stop(self, service, grace_seconds):
        stop_log.write_text(service["containerIdentity"]["containerId"])
        return StopResult("stopped")


adapter = SignalVoicevox()
registry = single_adapter_registry("voicevox", adapter)
voicevox_command.resolve_profile = lambda _env, _default, _paths: resolved_profile()
raise SystemExit(
    voicevox_command.start_voicevox(
        root,
        "dev",
        registry=registry,
        timing=EnvironmentTiming(readiness_interval_seconds=0.01),
    )
)
