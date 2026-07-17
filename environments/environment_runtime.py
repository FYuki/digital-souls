from __future__ import annotations

import os
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Mapping, Protocol, cast

from adapters.base import AdapterOperationError, ServiceStartResult
from environment_constants import (
    HTTP_SERVICE_NAMES,
    PROFILE_REPORT_ENV,
)
from environment_cleanup import cleanup_environment
from environment_verification import (
    record_and_validate_verification,
    require_service_readiness,
    verification_checks,
)
from http_readiness import wait_for_http
from orchestrator import classify_preprobe, readiness_complete
from environment_signals import defer_interrupt_signals
from ready_gate import ReadyGate
from run_report import record_phase, record_ready, update_readiness, update_service
from run_report_timestamps import next_lifecycle_timestamp
from service_registry import (
    ServiceRegistry,
    create_service_registry,
    operation_context_for,
    require_service_operations,
    resolve_runtime_services,
)


READINESS_ATTEMPTS = 120
READINESS_INTERVAL_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 1.0

class SupervisionError(RuntimeError):
    pass


class ReportStore(Protocol):
    def save(self, report: dict[str, object]) -> None: ...

    def load(self) -> dict[str, object]: ...

    def update(
        self, transform: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]: ...


class EnvironmentRun:
    def __init__(
        self,
        *,
        profile: dict[str, object],
        profile_path: Path,
        store: ReportStore,
        report: dict[str, object],
        root_dir: Path,
        ready_gate_url: str,
        registry: ServiceRegistry | None = None,
    ) -> None:
        self.profile = profile
        self.profile_path = profile_path
        self.store = store
        self.report = report
        self.registry = registry if registry is not None else create_service_registry(root_dir)
        self.runtime = resolve_runtime_services(profile, self.registry)
        dependencies = profile["dependencies"]
        if not isinstance(dependencies, dict):
            raise ValueError("resolved dependencies are required")
        self.dependencies = dependencies
        self.cleanup_failures: list[dict[str, object]] = []
        self.ready_gate = ReadyGate(ready_gate_url)
        self.ready_gate_open = False

    def _save_phase(self, phase: str) -> None:
        self._update_report(lambda report: record_phase(report, phase))

    def _update_report(
        self, transform: Callable[[dict[str, object]], dict[str, object]]
    ) -> None:
        self.report = self.store.update(transform)

    def verify(self) -> None:
        self._save_phase("verify")
        results = verification_checks(self.profile, self.registry)
        record_and_validate_verification(results, self._record_verification_readiness)

    def _record_verification_readiness(
        self, service: str, readiness: Mapping[str, object]
    ) -> None:
        self._update_report(
            partial(update_readiness, service=service, readiness=readiness)
        )

    def prepare(self) -> None:
        self._save_phase("prepare")
        for name in self.runtime.prepare_order:
            require_service_operations(self.registry, name).prepare(
                self.dependencies[name],
                operation_context_for(name, self.dependencies, self.registry),
            )

    def pre_probe(self) -> dict[str, str]:
        self._save_phase("pre_probe")
        decisions: dict[str, str] = {}
        for name in HTTP_SERVICE_NAMES:
            dependency = self.dependencies[name]
            if dependency.get("mode") == "disabled" or dependency.get("source") == "browser":
                continue
            observation = require_service_operations(self.registry, name).probe(
                dependency, timeout_seconds=REQUEST_TIMEOUT_SECONDS
            )
            observation_report = observation.to_report()
            self._update_report(
                partial(
                    update_readiness,
                    service=name,
                    readiness=observation_report,
                )
            )
            decision = classify_preprobe(dependency, observation_report)
            decisions[name] = decision.state
            if decision.failure_category is not None:
                raise RuntimeError(f"external service is not ready: {name}")
            if decision.state == "reused":
                self._validate_service_readiness(name, dependency)
                self._update_report(
                    partial(
                        update_service,
                        service=name,
                        state="reused",
                        owned=False,
                    )
                )
            elif decision.state == "external":
                self._update_report(
                    partial(
                        update_service,
                        service=name,
                        state="external",
                        owned=False,
                    )
                )
        return decisions

    def _validate_service_readiness(
        self, name: str, dependency: Mapping[str, object]
    ) -> None:
        validation = require_service_operations(
            self.registry, name
        ).validate_readiness(dependency)
        require_service_readiness(validation)

    def _service_environment(self) -> dict[str, str]:
        environment = {**os.environ, PROFILE_REPORT_ENV: str(self.profile_path)}
        derived = self.profile.get("derivedEnvironment")
        if not isinstance(derived, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in derived.items()
        ):
            raise ValueError("resolved derived environment is invalid")
        return {**environment, **derived}

    def start_or_reuse(self, decisions: Mapping[str, str]) -> None:
        self._save_phase("start_or_reuse")
        environment = self._service_environment()
        for name in self.runtime.start_order:
            if decisions.get(name) == "reused":
                continue
            with defer_interrupt_signals():
                try:
                    result = require_service_operations(self.registry, name).start(
                        self.dependencies[name], environment
                    )
                except AdapterOperationError as error:
                    if error.ownership is not None:
                        ownership_update = _service_start_update(name, error.ownership)
                        self.report = ownership_update(self.report)
                    if error.cleanup_failure is not None:
                        cleanup_failure: dict[str, object] = {
                            "service": name,
                            "result": error.cleanup_failure.result,
                        }
                        if error.cleanup_failure.message is not None:
                            cleanup_failure["message"] = error.cleanup_failure.message
                        self.cleanup_failures.append(cleanup_failure)
                    if error.ownership is not None:
                        self.report = self.store.update(ownership_update)
                    raise
                ownership_update = _service_start_update(name, result)
                self.report = ownership_update(self.report)
                self.report = self.store.update(ownership_update)

    def _assert_owned_services_running(self) -> None:
        services = cast(dict[str, dict[str, object]], self.report["services"])
        for name in cast(list[str], self.report["startSequence"]):
            service = services[name]
            if not require_service_operations(self.registry, name).is_running(service):
                raise SupervisionError(
                    f"managed service exited unexpectedly: {name}"
                )

    def wait_until_ready(self) -> None:
        self._save_phase("readiness")
        observations: dict[str, dict[str, object]] = {}
        for name in HTTP_SERVICE_NAMES:
            dependency = self.dependencies[name]
            if dependency.get("mode") == "disabled" or dependency.get("source") == "browser":
                continue
            result = wait_for_http(
                str(dependency["readinessUrl"]),
                max_attempts=READINESS_ATTEMPTS,
                interval_seconds=READINESS_INTERVAL_SECONDS,
                request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
                assert_environment_running=self._assert_owned_services_running,
            )
            observations[name] = result.to_report()
            readiness_report = result.to_report()
            self._update_report(
                partial(
                    update_readiness,
                    service=name,
                    readiness=readiness_report,
                )
            )
            if result.result == "ready":
                self._validate_service_readiness(name, dependency)
        if not readiness_complete(self.profile, observations):
            raise RuntimeError("one or more services did not become ready")
        self._update_report(
            lambda report: record_ready(
                report, ready_at=next_lifecycle_timestamp(report)
            )
        )

    def begin_supervision(self) -> None:
        with defer_interrupt_signals():
            self._save_phase("supervise")
            self.ready_gate.open()
            self.ready_gate_open = True

    def supervise(self) -> None:
        import time

        while True:
            self._assert_owned_services_running()
            time.sleep(0.5)

    def cleanup(self) -> list[dict[str, object]]:
        outcome = cleanup_environment(
            report=self.report,
            registry=self.registry,
            store=self.store,
            ready_gate=self.ready_gate,
            ready_gate_open=self.ready_gate_open,
            prior_failures=self.cleanup_failures,
        )
        self.report = outcome.report
        self.ready_gate_open = False
        return outcome.results


def _service_start_update(
    name: str, result: ServiceStartResult
) -> Callable[[dict[str, object]], dict[str, object]]:
    return lambda report: update_service(
        report, name, state=result.state, owned=result.owned,
        process_identity=result.process_identity if result.owned else None,
        container_identity=result.container_identity if result.owned else None,
    )
