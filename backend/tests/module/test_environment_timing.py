from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

import tests.environment_test_support
from tests.environment_test_support import DEPENDENCY_NAMES, profile_with_dependencies


def test_should_expose_documented_environment_timing_defaults():
    from environment_timing import EnvironmentTiming

    timing = EnvironmentTiming()

    assert timing.readiness_attempts == 120
    assert timing.readiness_interval_seconds == 0.5
    assert timing.request_timeout_seconds == 1.0
    assert timing.supervision_interval_seconds == 0.5


def test_should_keep_environment_timing_immutable():
    from environment_timing import EnvironmentTiming

    timing = EnvironmentTiming()

    with pytest.raises(dataclasses.FrozenInstanceError):
        timing.readiness_attempts = 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("readiness_attempts", 0),
        ("readiness_attempts", -1),
        ("readiness_attempts", 1.5),
        ("readiness_attempts", True),
        ("readiness_interval_seconds", -0.1),
        ("readiness_interval_seconds", True),
        ("request_timeout_seconds", -0.1),
        ("request_timeout_seconds", True),
        ("supervision_interval_seconds", -0.1),
        ("supervision_interval_seconds", True),
    ],
)
def test_should_reject_invalid_environment_timing(field: str, value: object):
    from environment_timing import EnvironmentTiming

    with pytest.raises(ValueError):
        EnvironmentTiming(**{field: value})


def test_should_allow_zero_duration_environment_timing():
    from environment_timing import EnvironmentTiming

    timing = EnvironmentTiming(
        readiness_attempts=1,
        readiness_interval_seconds=0,
        request_timeout_seconds=0,
        supervision_interval_seconds=0,
    )

    assert timing.readiness_attempts == 1
    assert timing.readiness_interval_seconds == 0
    assert timing.request_timeout_seconds == 0
    assert timing.supervision_interval_seconds == 0


class _TimingProbeOperations:
    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.results = iter(("not_ready", "not_ready", "not_ready", "ready"))

    def verify(self, dependency, context):
        from adapters.base import Check, VerificationResult

        return VerificationResult((Check("frontend", "ready", "frontend", False),))

    def prepare(self, dependency, context):
        return None

    def probe(self, dependency, timeout_seconds):
        from http_readiness import ReadinessResult

        self.timeouts.append(timeout_seconds)
        return ReadinessResult(
            str(dependency["readinessUrl"]), 1, 0.0, next(self.results)
        )

    def start(self, dependency, environment):
        raise AssertionError("start is outside this timing contract")

    def validate_readiness(self, dependency):
        from adapters.base import ReadinessValidationResult

        return ReadinessValidationResult("ready")

    def is_running(self, service):
        return True

    def stop(self, service, grace_seconds):
        raise AssertionError("stop is outside this timing contract")


def _timing_registry(adapter: _TimingProbeOperations):
    from service_registry import ServiceRegistration, ServiceRegistry

    return ServiceRegistry(
        services={
            name: ServiceRegistration(
                name,
                adapter if name == "frontend" else None,
                "backend" if name in {"whisper", "chroma"} else None,
            )
            for name in DEPENDENCY_NAMES
        },
        prepare_order=("frontend",),
        start_order=("frontend",),
    )


@pytest.mark.parametrize("missing_dependency", ["registry", "timing"])
def test_should_require_resolved_runtime_dependencies(
    missing_dependency: str, tmp_path: Path
):
    from environment_runtime import EnvironmentRun
    from environment_timing import EnvironmentTiming

    options: dict[str, object] = {
        "profile": profile_with_dependencies(),
        "profile_path": tmp_path / "resolved-profile.json",
        "store": object(),
        "report": {},
        "ready_gate_url": "http://127.0.0.1:0/ready",
        "was_interrupted": lambda: False,
        "registry": _timing_registry(_TimingProbeOperations()),
        "timing": EnvironmentTiming(),
    }
    del options[missing_dependency]

    with pytest.raises(TypeError, match=missing_dependency):
        EnvironmentRun(**options)


def test_should_apply_one_timing_object_to_verification_preprobe_and_readiness(
    tmp_path: Path,
):
    from environment_runtime import EnvironmentRun
    from environment_timing import EnvironmentTiming
    from run_report import create_initial_report
    from run_report_store import RunReportStore

    disabled = {"mode": "disabled", "source": None}
    profile = profile_with_dependencies(
        backend=disabled,
        ollama=disabled,
        voicevox=disabled,
        whisper=disabled,
        chroma=disabled,
    )
    report = create_initial_report(
        run_id="timing-integration",
        started_at="2026-07-18T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    store = RunReportStore(tmp_path / "environment-run.json")
    store.save(report)
    adapter = _TimingProbeOperations()
    timing = EnvironmentTiming(
        readiness_attempts=2,
        readiness_interval_seconds=0,
        request_timeout_seconds=0.25,
        supervision_interval_seconds=0,
    )
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=_timing_registry(adapter),
        timing=timing,
    )

    run.verify()
    decisions = run.pre_probe()
    run.wait_until_ready()

    assert decisions == {"frontend": "start_required"}
    assert adapter.timeouts == [0.25, 0.25, 0.25, 0.25]
    readiness = store.load()["services"]["frontend"]["readiness"]
    assert readiness["result"] == "ready"
    assert readiness["attempts"] == 2


def test_should_apply_injected_supervision_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from environment_runtime import EnvironmentRun
    from environment_timing import EnvironmentTiming
    from run_report import create_initial_report
    from run_report_store import RunReportStore

    profile = profile_with_dependencies()
    report = create_initial_report(
        run_id="supervision-timing",
        started_at="2026-07-18T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    store = RunReportStore(tmp_path / "environment-run.json")
    store.save(report)
    interrupted = False
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal interrupted
        sleeps.append(seconds)
        interrupted = True

    monkeypatch.setattr("time.sleep", sleep)
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: interrupted,
        registry=_timing_registry(_TimingProbeOperations()),
        timing=EnvironmentTiming(supervision_interval_seconds=0.125),
    )

    run.supervise()

    assert sleeps == [0.125]
