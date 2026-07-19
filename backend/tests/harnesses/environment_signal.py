from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


root = Path(sys.argv[1])
report_path = sys.argv[2]
stop_log = Path(sys.argv[3])
sys.path[:0] = [str(root / "environments"), str(root / "backend")]

import commands.up_command as up_command  # noqa: E402
from adapters.base import (  # noqa: E402
    Check,
    ProcessServiceOperations,
    ReadinessValidationResult,
    StartSpecification,
    VerificationResult,
)
from environment_timing import EnvironmentTiming  # noqa: E402
from http_readiness import ReadinessResult  # noqa: E402
from profile_resolution import resolve_profile  # noqa: E402
from service_registry import ServiceRegistration, ServiceRegistry  # noqa: E402


class SignalAdapter(ProcessServiceOperations):
    def __init__(self, root_dir, label, runner):
        super().__init__(root_dir, label, runner)
        self.probe_count = 0

    def verify(self, dependency, context):
        return VerificationResult((Check(self.label, "ready", "ready", False),))

    def prepare(self, dependency, context):
        return None

    def probe(self, dependency, timeout_seconds):
        self.probe_count += 1
        result = "ready" if self.probe_count >= 3 else "not_ready"
        return ReadinessResult(
            str(dependency["readinessUrl"]), 1, 0.0, result
        )

    def start_specification(self, dependency):
        return StartSpecification(
            (sys.executable, "-c", "import time; time.sleep(60)"), self.root_dir
        )

    def validate_readiness(self, dependency):
        return ReadinessValidationResult("ready")

    def stop(self, service, grace_seconds):
        with stop_log.open("a") as log:
            log.write(f"begin:{self.label}\n")
            log.flush()
        time.sleep(0.5)
        result = super().stop(service, grace_seconds)
        with stop_log.open("a") as log:
            log.write(f"{self.label}\n")
        return result


profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
profile["dependencies"] = {
    **profile["dependencies"],
    "backend": {
        "mode": "real",
        "source": "managed",
        "baseUrl": "http://localhost:8000",
        "readinessPath": "/",
        "readinessUrl": "http://localhost:8000/",
    },
}
up_command.resolve_and_write_profile = (
    lambda environment, default, report, legacy: profile
)
adapters = {
    name: SignalAdapter(root, name, None) for name in ("frontend", "backend")
}
names = ("frontend", "backend", "ollama", "voicevox", "whisper", "chroma")
registry = ServiceRegistry(
    {
        name: ServiceRegistration(
            name,
            adapters.get(name),
            "backend" if name in {"whisper", "chroma"} else None,
        )
        for name in names
    },
    ("backend", "frontend"),
    ("backend", "frontend"),
)
arguments = argparse.Namespace(
    run_report=report_path,
    profile_report=None,
    default_profile="test-mocked",
)
raise SystemExit(
    up_command.up_environment(
        root,
        root / ".runtime",
        arguments,
        registry=registry,
        timing=EnvironmentTiming(
            readiness_attempts=1,
            readiness_interval_seconds=0,
            request_timeout_seconds=0,
            supervision_interval_seconds=0.01,
        ),
    )
)
