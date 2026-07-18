from __future__ import annotations

from typing import Mapping

from run_report_contract import FAILURE_CATEGORIES, PHASES, STATES, RunReportError
from run_report_lifecycle_validation import validate_run_report_lifecycle
from run_report_shape_validation import validate_run_report_shape


def validate_run_report(report: Mapping[str, object]) -> None:
    validate_run_report_shape(report)
    validate_run_report_lifecycle(report)


__all__ = [
    "FAILURE_CATEGORIES",
    "PHASES",
    "STATES",
    "RunReportError",
    "validate_run_report",
]
