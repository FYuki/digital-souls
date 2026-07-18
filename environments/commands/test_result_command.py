from __future__ import annotations

from pathlib import Path

from run_report import record_test_result
from run_report_store import RunReportStore
from run_report_timestamps import next_lifecycle_timestamp


def record_playwright_result(
    run_report_argument: str, status: str, message: str
) -> int:
    store = RunReportStore(Path(run_report_argument).resolve())
    store.update(
        lambda report: record_test_result(
            report,
            status=status,
            ended_at=next_lifecycle_timestamp(report),
            message=message,
        )
    )
    return 0
