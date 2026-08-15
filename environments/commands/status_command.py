from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from app.runtime_data_root import (
    validate_existing_runtime_data_root,
    validate_runtime_projection,
)
from app.runtime_paths import resolve_runtime_paths
from environment_constants import DEPENDENCY_NAMES, RUN_REPORT_ENV
from environment_options import resolve_existing_run_report_path
from http_readiness import probe_http
from process_control import ProcessIdentity, process_identity_matches
from run_report_store import RunReportStore


STATUS_PROBE_TIMEOUT_SECONDS = 1.0


def render_environment_status(
    report: Mapping[str, object], live_states: Mapping[str, str]
) -> list[str]:
    profile = _require_mapping(report.get("effectiveProfile"), "effectiveProfile")
    dependencies = _require_mapping(profile.get("dependencies"), "dependencies")
    services = _require_mapping(report.get("services"), "services")
    lines: list[str] = []
    for name in DEPENDENCY_NAMES:
        dependency = _require_mapping(dependencies.get(name), f"dependencies.{name}")
        service = _require_mapping(services.get(name), f"services.{name}")
        source = dependency.get("source")
        rendered_source = "disabled" if dependency.get("mode") == "disabled" else source
        ownership = "owned" if service.get("owned") is True else "unowned"
        stored_state = service.get("state")
        if not isinstance(stored_state, str):
            raise ValueError(f"services.{name}.state must be a string")
        state = live_states.get(name, stored_state)
        lines.append(
            f"{name} source={rendered_source} ownership={ownership} state={state}"
        )
    return lines


def status_environment(root_dir: Path, run_report_argument: str | None) -> int:
    configured = (
        run_report_argument
        if run_report_argument is not None
        else os.environ.get(RUN_REPORT_ENV)
    )
    if not configured:
        raise ValueError("status requires an explicit run report path")
    report = load_environment_report(root_dir, configured)
    live_states = _observe_http_services(report)
    for line in render_environment_status(report, live_states):
        print(line)
    print(render_orchestrator_status(report))
    return 0


def load_environment_report(
    root_dir: Path, configured_path: str
) -> Mapping[str, object]:
    runtime_paths = resolve_runtime_paths(os.environ, root_dir)
    validate_existing_runtime_data_root(runtime_paths, root_dir)
    report_path = resolve_existing_run_report_path(configured_path, runtime_paths)
    report = RunReportStore(report_path).load()
    validate_runtime_projection(report.get("runtime"), runtime_paths)
    return report


def render_orchestrator_status(report: Mapping[str, object]) -> str:
    identity_record = _require_mapping(
        report.get("orchestratorIdentity"), "orchestratorIdentity"
    )
    identity = ProcessIdentity.from_report(identity_record)
    state = "alive" if process_identity_matches(identity) else "dead"
    return f"orchestrator state={state}"


def _observe_http_services(report: Mapping[str, object]) -> dict[str, str]:
    profile = _require_mapping(report.get("effectiveProfile"), "effectiveProfile")
    dependencies = _require_mapping(profile.get("dependencies"), "dependencies")
    observations: dict[str, str] = {}
    for name in DEPENDENCY_NAMES:
        dependency = _require_mapping(dependencies.get(name), f"dependencies.{name}")
        readiness_url = dependency.get("readinessUrl")
        if isinstance(readiness_url, str):
            result = probe_http(
                readiness_url, timeout_seconds=STATUS_PROBE_TIMEOUT_SECONDS
            )
            observations[name] = "ready" if result.result == "ready" else "unavailable"
    return observations


def _require_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value
