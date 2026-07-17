from __future__ import annotations

import os
import sys
from pathlib import Path

from environment_cleanup import cleanup_environment_services
from environment_constants import RUN_REPORT_ENV
from run_report import record_cleanup, record_cleanup_started
from run_report_store import RunReportStore
from run_report_timestamps import next_lifecycle_timestamp
from service_registry import create_service_registry


def down_environment(root_dir: Path, run_report_argument: str | None) -> int:
    configured = (
        run_report_argument
        if run_report_argument is not None
        else os.environ.get(RUN_REPORT_ENV)
    )
    if not configured:
        print("ERROR: down requires an explicit run report path", file=sys.stderr)
        return 2
    store = RunReportStore(Path(configured).resolve())
    report = store.update(record_cleanup_started)
    results = cleanup_environment_services(report, create_service_registry(root_dir))
    store.update(
        lambda current: record_cleanup(
            current,
            results=results,
            ended_at=next_lifecycle_timestamp(current),
        )
    )
    return 1 if any(result["result"] == "failed" for result in results) else 0
