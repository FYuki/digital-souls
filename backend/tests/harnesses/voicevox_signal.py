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
from commands.voicevox_command import start_voicevox  # noqa: E402
from environment_timing import EnvironmentTiming  # noqa: E402
from http_readiness import ReadinessResult  # noqa: E402
from service_registry import ServiceRegistration, ServiceRegistry  # noqa: E402


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


names = ("frontend", "backend", "ollama", "voicevox", "whisper", "chroma")
adapter = SignalVoicevox()
registry = ServiceRegistry(
    {
        name: ServiceRegistration(
            name,
            adapter if name == "voicevox" else None,
            "backend" if name in {"whisper", "chroma"} else None,
        )
        for name in names
    },
    ("voicevox",),
    ("voicevox",),
)
raise SystemExit(
    start_voicevox(
        root,
        "dev",
        registry=registry,
        timing=EnvironmentTiming(readiness_interval_seconds=0.01),
    )
)
