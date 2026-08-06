from __future__ import annotations

import os
from pathlib import Path

from environment_options import resolve_existing_run_report_path
from run_report import record_test_result
from run_report_store import RunReportStore
from run_report_timestamps import next_lifecycle_timestamp
from app.runtime_data_root import (
    validate_existing_runtime_data_root,
    validate_runtime_projection,
)
from app.runtime_paths import resolve_runtime_paths


def record_playwright_result(
    run_report_argument: str, status: str, message: str
) -> int:
    root_dir = Path(__file__).resolve().parents[2]
    runtime_paths = resolve_runtime_paths(os.environ, root_dir)
    validate_existing_runtime_data_root(runtime_paths, root_dir)
    report_path = resolve_existing_run_report_path(run_report_argument, runtime_paths)
    store = RunReportStore(report_path)
    report = store.load()
    validate_runtime_projection(report.get("runtime"), runtime_paths)
    store.update(
        lambda report: record_test_result(
            report,
            status=status,
            ended_at=next_lifecycle_timestamp(report),
            message=message,
        )
    )
    return 0
