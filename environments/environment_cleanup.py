from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, cast

from environment_constants import READY_GATE_CLEANUP_TARGET, RUN_REPORT_CLEANUP_TARGET
from orchestrator import cleanup_owned_services
from ready_gate import ReadyGate
from run_report import record_phase, restore_ownership_history
from service_registry import ServiceRegistry, require_service_operations


STOP_GRACE_SECONDS = 5.0


class CleanupReportStore(Protocol):
    def load(self) -> dict[str, object]: ...

    def update(
        self, transform: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class EnvironmentCleanupResult:
    report: dict[str, object]
    results: list[dict[str, object]]


def cleanup_environment(
    *,
    report: dict[str, object],
    registry: ServiceRegistry,
    store: CleanupReportStore,
    ready_gate: ReadyGate,
    ready_gate_open: bool,
    prior_failures: list[dict[str, object]],
) -> EnvironmentCleanupResult:
    cleanup_report = report
    infrastructure_results = _close_ready_gate(ready_gate, ready_gate_open)
    current_report, report_results = _prepare_cleanup_report(report, store)
    results = [
        *prior_failures,
        *infrastructure_results,
        *report_results,
        *cleanup_environment_services(cleanup_report, registry),
    ]
    return EnvironmentCleanupResult(current_report, results)


def _close_ready_gate(
    ready_gate: ReadyGate, ready_gate_open: bool
) -> list[dict[str, object]]:
    if not ready_gate_open:
        return []
    try:
        ready_gate.close()
    except Exception as error:
        return [
            {
                "service": READY_GATE_CLEANUP_TARGET,
                "result": "failed",
                "message": str(error),
            }
        ]
    return []


def _prepare_cleanup_report(
    report: dict[str, object], store: CleanupReportStore
) -> tuple[dict[str, object], list[dict[str, object]]]:
    current_report = report
    failures: list[dict[str, object]] = []
    try:
        current_report = store.load()
    except Exception as error:
        failures.append(_report_cleanup_failure("load", error))
    try:
        current_report = store.update(
            lambda stored: record_phase(
                restore_ownership_history(stored, report), "cleanup"
            )
        )
    except Exception as error:
        failures.append(_report_cleanup_failure("update", error))
    return current_report, failures


def _report_cleanup_failure(operation: str, error: Exception) -> dict[str, object]:
    return {
        "service": RUN_REPORT_CLEANUP_TARGET,
        "result": "failed",
        "message": f"run report {operation} failed: {error}",
    }


def cleanup_environment_services(
    report: Mapping[str, object], registry: ServiceRegistry
) -> list[dict[str, object]]:
    services = cast(dict[str, dict[str, object]], report["services"])
    sequence = cast(list[str], report["startSequence"])

    def stopper_for(name: str) -> Callable[[], Mapping[str, object]]:
        def stop() -> Mapping[str, object]:
            result = require_service_operations(registry, name).stop(
                services[name], STOP_GRACE_SECONDS
            )
            entry: dict[str, object] = {"result": result.result}
            if result.message is not None:
                entry["message"] = result.message
            return entry

        return stop

    return cleanup_owned_services(
        report, {name: stopper_for(name) for name in sequence}
    )
