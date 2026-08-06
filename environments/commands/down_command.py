from __future__ import annotations

import os
import sys
from pathlib import Path

from environment_cleanup import cleanup_environment_services
from environment_constants import RUN_REPORT_ENV
from environment_options import resolve_existing_run_report_path
from process_control import (
    PROCESS_IDENTITY_MISMATCH,
    PROCESS_NOT_RUNNING,
    PROCESS_STOP_REQUESTED,
    ProcessIdentity,
    request_process_stop,
)
from run_report import record_cleanup, record_cleanup_started
from run_report_store import RunReportStore
from run_report_timestamps import next_lifecycle_timestamp
from service_registry import create_service_registry
from app.runtime_data_root import (
    validate_existing_runtime_data_root,
    validate_runtime_projection,
)
from app.runtime_paths import resolve_runtime_paths


def down_environment(root_dir: Path, run_report_argument: str | None) -> int:
    configured = (
        run_report_argument
        if run_report_argument is not None
        else os.environ.get(RUN_REPORT_ENV)
    )
    if not configured:
        print("ERROR: down requires an explicit run report path", file=sys.stderr)
        return 2
    runtime_paths = resolve_runtime_paths(os.environ, root_dir)
    validate_existing_runtime_data_root(runtime_paths, root_dir)
    report_path = resolve_existing_run_report_path(configured, runtime_paths)
    store = RunReportStore(report_path)
    current = store.load()
    validate_runtime_projection(current.get("runtime"), runtime_paths)
    identity_value = current["orchestratorIdentity"]
    if not isinstance(identity_value, dict):
        raise ValueError("orchestratorIdentity is required")
    stop_result = request_process_stop(ProcessIdentity.from_report(identity_value))
    if stop_result.result == PROCESS_STOP_REQUESTED:
        return 0
    if stop_result.result not in {PROCESS_NOT_RUNNING, PROCESS_IDENTITY_MISMATCH}:
        raise RuntimeError(f"unexpected orchestrator stop result: {stop_result.result}")
    report = store.update(record_cleanup_started)
    results = cleanup_environment_services(
        report, create_service_registry(root_dir, runtime_paths)
    )
    store.update(
        lambda current: record_cleanup(
            current,
            results=results,
            ended_at=next_lifecycle_timestamp(current),
        )
    )
    return 1 if any(result["result"] == "failed" for result in results) else 0
